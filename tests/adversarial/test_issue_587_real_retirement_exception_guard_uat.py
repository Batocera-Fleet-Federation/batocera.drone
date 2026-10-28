"""UAT for #587: the VPN sharing-revocation poller must survive ticks and honor real retirement.

Expected behavior, derived from issue #587 and the forever-loop poller contract
before reading this delivery's patch:

* An unexpected Exception from a tick is logged and the next tick still runs, so a
  later peer-gone / 404 can still wipe imported credentials.
* Retirement is generation-based: ``retire_pollers()`` / ``retire_background_pollers()``
  must stop the loop before the next check. Existing #587 suites mock
  ``poller_retired``; this suite drives the real runtime_state generation and claim
  so a mocked gate cannot hide a broken retire path.
* Forever-loop pollers sleep before the first check. Generation 0 is a live
  generation (the process starts at 0); it must not be treated as "unset".
* Only ``Exception`` is absorbed. Interval-setup failures must not kill the loop.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.common import runtime_state
from app.drone_api import Settings, retire_background_pollers


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


class Issue587RealRetirementExceptionGuardUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        self.addCleanup(install.stop)
        install.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-587-real-retirement",
        }, clear=True):
            self.settings = Settings.from_env()
        vpn_manager.import_from_peer(
            self.settings,
            {
                "config_filename": "client.ovpn", "config_text": SAMPLE_OVPN,
                "has_credentials": True, "username": "peer-user", "password": "secret",
            },
            source_peer_id="peer-1", source_peer_name="Peer",
        )
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())
        runtime_state.retire_pollers()
        self.addCleanup(runtime_state.retire_pollers)

    def _assert_revoked(self) -> None:
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("no longer paired", state["revoked_reason"])

    def test_hostile_tick_then_peer_gone_revokes_under_real_retire_pollers(self) -> None:
        # Real generation/claim: tick 1 raises, tick 2 runs the real check (peer gone),
        # sleep 3 retires the process-wide generation so the loop exits.
        real_check = vpn_manager.check_sharing_revocation
        calls: list[str] = []

        def scripted_check(settings):
            calls.append("tick")
            if len(calls) == 1:
                raise RuntimeError("unexpected tick: \udcff")
            return real_check(settings)

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=scripted_check), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(len(calls), 2, "the unexpected tick must not prevent the later revocation check")
        self._assert_revoked()

    def test_retire_pollers_after_failed_tick_stops_before_next_check(self) -> None:
        calls: list[int] = []

        def exploding_check(_settings) -> bool:
            calls.append(1)
            raise RuntimeError("tick boom")

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=exploding_check), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(len(calls), 1, "a retired generation must not run another check")
        self.assertTrue(vpn_manager.auth_path(self.settings).exists(), "no later tick ran, so nothing was revoked")

    def test_retire_background_pollers_stops_loop_before_next_check(self) -> None:
        # Production shutdown uses drone_api.retire_background_pollers, not the
        # raw runtime_state helper. That path must still stop revocation I/O.
        calls: list[int] = []

        def exploding_check(_settings) -> bool:
            calls.append(1)
            raise RuntimeError("tick boom")

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                retire_background_pollers()

        with mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=exploding_check), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(len(calls), 1)
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())

    def test_first_check_happens_after_the_opening_sleep(self) -> None:
        order: list[str] = []

        def fake_sleep(_seconds: float) -> None:
            order.append("sleep")
            if order.count("sleep") >= 2:
                runtime_state.retire_pollers()

        def check(_settings) -> bool:
            order.append("check")
            raise RuntimeError("first tick")

        with mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=check), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertGreaterEqual(len(order), 2)
        self.assertEqual(order[0], "sleep", "forever-loop pollers sleep before the first check")
        self.assertEqual(order[1], "check")

    def test_interval_setup_exception_does_not_prevent_later_revocation(self) -> None:
        real_check = vpn_manager.check_sharing_revocation
        calls: list[int] = []

        def scripted_check(settings):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("after interval fallback")
            return real_check(settings)

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= 3:
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager, "poller_interval_from_env", side_effect=RuntimeError("env unreadable")), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=scripted_check), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(sleeps[0], 300.0, "interval-setup failure must fall back to the default sleep")
        self.assertEqual(len(calls), 2)
        self._assert_revoked()

    def test_bound_generation_zero_is_not_treated_as_unset(self) -> None:
        # Generation 0 is live at process start. ``if not generation`` would treat it
        # as unset, re-read after retirement, and keep checking. Binding 0 must stick.
        check = mock.Mock(return_value=False)
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager, "poller_generation", side_effect=AssertionError("must use bound generation 0")), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check):
            vpn_manager.run_sharing_revocation_poller(self.settings, generation=0)

        check.assert_not_called()
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())


if __name__ == "__main__":
    unittest.main()
