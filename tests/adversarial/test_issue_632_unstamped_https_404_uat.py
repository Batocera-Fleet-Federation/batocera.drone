"""UAT for #632's deferred #599 security finding.

Issue #632 carries the #599 conclusion: "The #599 fix rejects forged
plaintext 404s before they can revoke VPN, SMTP, or Tailnet credentials."
An HTTPError with no verified-route stamp has no authenticated-channel
evidence, regardless of its diagnostic filename.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from app.common.settings import Settings
from app.device import smtp_manager, tailnet_service, vpn_manager
from app.transfer import peer_connectivity as pc


SOURCE = "issue-632-source"


def _settings(root: Path) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "adversarial-632",
    }, clear=True):
        return Settings.from_env()


class Issue632UnstampedHttps404UAT(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="issue-632-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.settings = _settings(root)
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        patcher.start()
        self.addCleanup(patcher.stop)
        vpn_manager.import_from_peer(self.settings, {
            "config_filename": "client.ovpn",
            "config_text": "client\ndev tun\nremote vpn.example.test 1194\nauth-user-pass\n",
            "has_credentials": True,
            "username": "vpn-user",
            "password": "vpn-secret",
        }, source_peer_id=SOURCE)
        smtp_manager.import_from_peer(self.settings, {
            "host": "smtp.example.test", "port": 587, "use_starttls": True,
            "use_ssl": False, "username": "smtp-user", "password": "smtp-secret",
            "from_address": "drone@example.test", "recipient_email": "owner@example.test",
        }, source_peer_id=SOURCE)
        tailnet_service._save_sharing_state(self.settings, auth_key="tskey-secret", source_peer_id=SOURCE)

    def _check_with_unstamped_https_404(self, checker) -> bool:
        error = HTTPError("https://forged.example/v1/api/peer/config", 404, "not found", None, None)
        self.assertNotIn("peer_route", vars(error))
        self.assertFalse(pc.peer_http_error_is_authenticated(error))
        peer = {"drone_id": SOURCE}
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch.object(pc, "_peer_get_json_for_peer", side_effect=error), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            result = checker(self.settings)
        disconnect.assert_not_called()
        return result

    def test_unstamped_https_filename_404_cannot_revoke_vpn_credentials(self) -> None:
        self.assertFalse(self._check_with_unstamped_https_404(vpn_manager.check_sharing_revocation))
        self.assertTrue(vpn_manager._load_state(self.settings)["has_credentials"])

    def test_unstamped_https_filename_404_cannot_revoke_smtp_credentials(self) -> None:
        self.assertFalse(self._check_with_unstamped_https_404(smtp_manager.check_sharing_revocation))
        self.assertEqual(smtp_manager._load_state(self.settings)["password"], "smtp-secret")

    def test_unstamped_https_filename_404_cannot_revoke_tailnet_credentials(self) -> None:
        self.assertFalse(self._check_with_unstamped_https_404(tailnet_service.check_tailnet_sharing_revocation))
        self.assertEqual(tailnet_service._load_sharing_state(self.settings)["auth_key"], "tskey-secret")


if __name__ == "__main__":
    unittest.main()
