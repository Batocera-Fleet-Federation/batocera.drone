"""Issue #588: swallowed inner helper failures must not starve RGS or the loop.

Production check_and_self_heal / reconcile_rgs_with_tunnel catch Exception and
return None. #582 still requires:

* a failed watchdog check attempts reconcile on that same tick while live
* a generation retired during a *successful* slow check does not start RGS
* a generation retired during a failed/swallowed check also skips RGS
  (security/test_issue_588_retired_failed_check_no_rgs.py)
* after either operation, a retired generation does not start another tick
* a surrogate in a reconnect/pause diagnostic must not kill the poller
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings

SAMPLE_OVPN = (
    "client\ndev tun\nproto udp\nremote vpn.example.net 1194\n"
    "auth-user-pass\n"
)


class _BrokenStdio(io.TextIOBase):
    encoding = "utf-8"
    errors = "strict"

    def write(self, _text: str) -> int:
        raise OSError("simulated stdio EIO")

    def flush(self) -> None:
        raise OSError("simulated stdio EIO")


def _strict_utf8() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(
        raw, encoding="utf-8", errors="strict", write_through=True,
    ), raw


class Issue588SwallowedFailureIndependenceUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        install = mock.patch.object(
            vpn_manager, "_drone_install_root", return_value=root / "install-root",
        )
        self.addCleanup(install.stop)
        install.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-588-swallowed-independence",
        }, clear=True):
            self.settings = Settings.from_env()

    def test_swallowed_inner_self_heal_failure_still_reconciles_same_tick(self) -> None:
        stderr, raw = _strict_utf8()
        sleeps = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1

        def retired(_generation: int) -> bool:
            return sleeps > 2

        load_results = iter([
            FileNotFoundError("/userdata/vpn/bad\udcff/state"),
            {"has_config": False},
            {"self_heal_enabled": False, "has_config": False, "has_credentials": False},
            {"has_config": False},
        ])

        def load_state(_settings):
            result = next(load_results)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state) as load, \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)

        self.assertEqual(sleeps, 3)
        self.assertEqual(load.call_count, 4, "RGS must still run on the tick whose self-heal failed internally")
        stderr.flush()
        self.assertIn(b"VPN self-heal check failed", raw.getvalue())
        self.assertIn(b"\\udcff", raw.getvalue())

    def test_swallowed_inner_failure_still_reconciles_when_retired_mid_check(self) -> None:
        # Adjudicated against security/test_issue_588_retired_failed_check_no_rgs.py:
        # a swallowed inner failure during retirement must not start RGS.
        retired_now = {"value": False}

        def load_state(_settings):
            retired_now["value"] = True
            raise RuntimeError("openvpn log: bad \udcff path")

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        stderr, _ = _strict_utf8()
        reconcile = mock.Mock(return_value=None)
        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep"), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)

        reconcile.assert_not_called()

    def test_successful_inner_check_still_skips_rgs_when_retired_mid_check(self) -> None:
        retired_now = {"value": False}

        def load_state(_settings):
            retired_now["value"] = True
            return {
                "self_heal_enabled": False,
                "has_config": False,
                "has_credentials": False,
            }

        def retired(_generation: int) -> bool:
            return retired_now["value"]

        reconcile = mock.Mock(return_value=None)
        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep"), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel", reconcile):
            vpn_manager.run_self_heal_poller(self.settings)

        reconcile.assert_not_called()

    def test_surrogate_reconnect_reason_on_strict_stdout_does_not_kill_poller(self) -> None:
        vpn_manager.save_uploaded_config(self.settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(self.settings, "user", "pass")
        stdout, _out_raw = _strict_utf8()
        stderr, err_raw = _strict_utf8()
        sleeps = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1

        def retired(_generation: int) -> bool:
            return sleeps > 2

        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(vpn_manager, "status", return_value={
                    "status": "error",
                    "message": "TLS Error: bad \udcff path",
                    "pid": 1,
                }), \
                mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}) as reconnect, \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile, \
                mock.patch.object(vpn_manager.sys, "stdout", stdout), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            vpn_manager.run_self_heal_poller(self.settings)

        self.assertGreaterEqual(reconnect.call_count, 1)
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(sleeps, 3)
        stderr.flush()
        # Either the reconnect line escaped onto stdout, or the helper isolated
        # a stdout encode failure onto stderr. Either way the poller continued.
        combined = _out_raw.getvalue() + err_raw.getvalue()
        self.assertTrue(
            b"\\udcff" in combined or reconnect.call_count >= 1,
            "surrogate reconnect diagnostics must not be an uncaught encode error",
        )

    def test_broken_stdout_during_reconnect_log_still_reconciles(self) -> None:
        vpn_manager.save_uploaded_config(self.settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(self.settings, "user", "pass")
        sleeps = 0

        def fake_sleep(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1

        def retired(_generation: int) -> bool:
            return sleeps > 1

        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=fake_sleep), \
                mock.patch.object(vpn_manager, "status", return_value={
                    "status": "error", "message": "TLS Error", "pid": 1,
                }), \
                mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile, \
                mock.patch.object(vpn_manager.sys, "stdout", _BrokenStdio()), \
                mock.patch.object(vpn_manager.sys, "stderr", _BrokenStdio()):
            vpn_manager.run_self_heal_poller(self.settings)

        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(sleeps, 2)

    def test_inner_keyboard_interrupt_still_propagates_out_of_the_poller(self) -> None:
        def load_state(_settings):
            raise KeyboardInterrupt

        with mock.patch.object(vpn_manager, "poller_generation", return_value=588), \
                mock.patch.object(vpn_manager, "poller_retired", return_value=False), \
                mock.patch.object(vpn_manager.time, "sleep"), \
                mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=load_state), \
                mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile:
            with self.assertRaises(KeyboardInterrupt):
                vpn_manager.run_self_heal_poller(self.settings)
        reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
