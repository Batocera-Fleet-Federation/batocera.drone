"""Issue #587 UAT: a log-hostile tick must not prevent later VPN revocation."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = """client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"""


class Issue587RecoveryUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-587-recovery",
        }, clear=True):
            self.settings = Settings.from_env()

    def test_surrogate_log_failure_is_followed_by_real_404_credential_revocation(self) -> None:
        payload = {
            "config_filename": "client.ovpn", "config_text": SAMPLE_OVPN,
            "has_credentials": True, "username": "peer-user", "password": "secret",
        }
        vpn_manager.import_from_peer(self.settings, payload, source_peer_id="peer-1", source_peer_name="Peer")
        real_check = vpn_manager.check_sharing_revocation
        calls = 0

        def fail_once_then_check(settings):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("broken stderr path: \udcff")
            return real_check(settings)

        raw = io.BytesIO()
        strict_stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
        sleeps = 0

        def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1

        def retired(_generation):
            return sleeps > 2

        peer = {"drone_id": "peer-1", "name": "Peer"}
        missing = vpn_manager.HTTPError("https://peer/v1/api/peer/vpn/config", 404, "missing", None, None)
        with mock.patch.object(vpn_manager, "poller_generation", return_value=587), \
                mock.patch.object(vpn_manager, "poller_retired", side_effect=retired), \
                mock.patch.object(vpn_manager.time, "sleep", side_effect=sleep), \
                mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=fail_once_then_check), \
                mock.patch.object(vpn_manager.sys, "stderr", strict_stderr), \
                mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=missing), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}):
            vpn_manager.run_sharing_revocation_poller(self.settings)

        self.assertEqual(calls, 2, "the first logging-hostile exception must not kill the next revocation check")
        strict_stderr.flush()
        self.assertIn(b"\\udcff", raw.getvalue(), "the poller should safely escape the surrogate while reporting")
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())
        self.assertIn("turned off sharing", state["revoked_reason"])


if __name__ == "__main__":
    unittest.main()
