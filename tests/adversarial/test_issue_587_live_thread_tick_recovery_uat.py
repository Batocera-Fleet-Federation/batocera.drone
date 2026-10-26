"""Exercise #587 recovery on a real poller thread through credential revocation."""

from __future__ import annotations

import io
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


class Issue587LiveThreadRecoveryUAT(unittest.TestCase):
    def test_real_thread_survives_tick_exception_then_revokes_when_source_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
            install.start()
            self.addCleanup(install.stop)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "adversarial-587-live-thread",
            }, clear=True):
                settings = Settings.from_env()

            vpn_manager.import_from_peer(
                settings,
                {
                    "config_filename": "client.ovpn",
                    "config_text": SAMPLE_OVPN,
                    "has_credentials": True,
                    "username": "peer-user",
                    "password": "secret",
                },
                source_peer_id="source-peer",
                source_peer_name="Source peer",
            )
            auth_path = vpn_manager.auth_path(settings)
            self.assertTrue(auth_path.exists(), "fixture must begin with imported credentials")

            # Each poller sleep is a rendezvous: the test releases exactly two
            # ticks, then retires the generation before the third wake-up.
            sleep_calls: queue.Queue[float] = queue.Queue()
            wakeups: queue.Queue[int] = queue.Queue()
            retired = threading.Event()
            poller_errors: list[BaseException] = []
            real_check = vpn_manager.check_sharing_revocation
            check_calls = 0

            def controlled_sleep(seconds: float) -> None:
                sleep_calls.put(seconds)
                if wakeups.get(timeout=5) != 1:
                    raise AssertionError("poller wake-up was not released")

            def fail_first_tick_then_check(current_settings: Settings) -> bool:
                nonlocal check_calls
                check_calls += 1
                if check_calls == 1:
                    raise RuntimeError("unexpected one-tick failure")
                return real_check(current_settings)

            def run_poller() -> None:
                try:
                    vpn_manager.run_sharing_revocation_poller(settings)
                except BaseException as error:  # capture thread failures for an assertion
                    poller_errors.append(error)

            thread = threading.Thread(target=run_poller, name="issue-587-uat-poller", daemon=True)
            with mock.patch.object(vpn_manager, "poller_generation", return_value=587), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=lambda _generation: retired.is_set()), \
                    mock.patch.object(vpn_manager.time, "sleep", side_effect=controlled_sleep), \
                    mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=fail_first_tick_then_check), \
                    mock.patch.object(vpn_manager.sys, "stderr", io.StringIO()), \
                    mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}) as disconnect:
                thread.start()
                try:
                    self.assertEqual(sleep_calls.get(timeout=5), 300.0)
                    wakeups.put(1)
                    self.assertEqual(sleep_calls.get(timeout=5), 300.0)
                    self.assertTrue(thread.is_alive(), "poller thread must survive its first failed check")
                    wakeups.put(1)
                    self.assertEqual(sleep_calls.get(timeout=5), 300.0)
                    self.assertFalse(auth_path.exists(), "source disappearance on tick two must wipe credentials")
                    retired.set()
                    wakeups.put(1)
                    thread.join(timeout=5)
                finally:
                    retired.set()
                    wakeups.put(1)
                    thread.join(timeout=5)

            self.assertFalse(thread.is_alive(), "retired poller thread must exit")
            self.assertEqual(poller_errors, [], "an unexpected tick exception must not escape the poller")
            self.assertEqual(check_calls, 2, "a later tick must run after the failed tick")
            disconnect.assert_called_once_with(settings)
            state = vpn_manager._load_state(settings)
            self.assertFalse(state["has_credentials"])
            self.assertIn("no longer paired", state["revoked_reason"])


if __name__ == "__main__":
    unittest.main()
