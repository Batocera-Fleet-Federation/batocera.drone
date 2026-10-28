"""UAT for #587: BaseException is a shutdown signal; a later poller must still revoke.

Issue #587 absorbs ``Exception`` around each sharing-revocation tick so one
hostile failure cannot retire the only background path that wipes imported VPN
credentials. Shutdown signals (``KeyboardInterrupt``, ``SystemExit``,
``GeneratorExit``) must still propagate. After such a signal, starting the
poller again must still be able to revoke — the exception guard cannot leave
the revocation path permanently dead.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
import app.device.vpn_manager as vpn_manager
from app.common import runtime_state
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


class Issue587BaseExceptionAndRestartUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-587-baseexception-restart",
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
        runtime_state.retire_pollers()
        self.addCleanup(runtime_state.retire_pollers)

    def _run_until_retired(self, check, *, retire_after_sleeps: int = 3) -> None:
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) >= retire_after_sleeps:
                runtime_state.retire_pollers()

        with mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=check), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()):
            vpn_manager.run_sharing_revocation_poller(self.settings)

    def test_keyboard_interrupt_from_check_propagates_and_a_later_poller_still_revokes(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._run_until_retired(mock.Mock(side_effect=KeyboardInterrupt), retire_after_sleeps=5)

        self.assertTrue(vpn_manager.auth_path(self.settings).exists(), "shutdown must not itself wipe credentials")

        real_check = vpn_manager.check_sharing_revocation
        calls: list[int] = []

        def survive_then_check(settings):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("hostile restart tick")
            return real_check(settings)

        self._run_until_retired(survive_then_check, retire_after_sleeps=3)

        self.assertEqual(len(calls), 2)
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("no longer paired", state["revoked_reason"])

    def test_system_exit_from_check_is_not_absorbed(self) -> None:
        check = mock.Mock(side_effect=SystemExit(4))
        with self.assertRaises(SystemExit) as raised:
            self._run_until_retired(check, retire_after_sleeps=5)
        self.assertEqual(raised.exception.code, 4)
        self.assertEqual(check.call_count, 1, "a shutdown signal must not trigger another check")
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())

    def test_generator_exit_from_check_is_not_absorbed(self) -> None:
        check = mock.Mock(side_effect=GeneratorExit)
        with self.assertRaises(GeneratorExit):
            self._run_until_retired(check, retire_after_sleeps=5)
        self.assertEqual(check.call_count, 1)
        self.assertTrue(vpn_manager.auth_path(self.settings).exists())

    def test_keyboard_interrupt_during_sleep_is_not_absorbed(self) -> None:
        check = mock.Mock(return_value=False)

        def interrupt_sleep(_seconds: float) -> None:
            raise KeyboardInterrupt

        with mock.patch.object(vpn_manager.time, "sleep", side_effect=interrupt_sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", check):
            with self.assertRaises(KeyboardInterrupt):
                vpn_manager.run_sharing_revocation_poller(self.settings)

        check.assert_not_called()

    def test_smtp_keyboard_interrupt_is_not_absorbed(self) -> None:
        check = mock.Mock(side_effect=KeyboardInterrupt)
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) >= 5:
                runtime_state.retire_pollers()

        with mock.patch.object(smtp_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(smtp_manager, "check_sharing_revocation", check), \
                mock.patch.object(smtp_manager.sys, "stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                smtp_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(check.call_count, 1)

    def test_tailnet_system_exit_is_not_absorbed(self) -> None:
        check = mock.Mock(side_effect=SystemExit(7))
        sleeps: list[float] = []

        def fake_sleep(_seconds: float) -> None:
            sleeps.append(1)
            if len(sleeps) >= 5:
                runtime_state.retire_pollers()

        with mock.patch.object(tailnet_service.time, "sleep", new=fake_sleep), \
                mock.patch.object(tailnet_service, "check_tailnet_sharing_revocation", check), \
                mock.patch.object(tailnet_service.sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                tailnet_service.run_tailnet_sharing_revocation_poller(self.settings)

        self.assertEqual(raised.exception.code, 7)
        self.assertEqual(check.call_count, 1)


if __name__ == "__main__":
    unittest.main()
