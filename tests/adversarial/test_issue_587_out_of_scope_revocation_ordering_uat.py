"""Out-of-scope probes around #587 VPN sharing revocation.

These are NOT blocking #587 assertions. #587 is about the poller thread dying
on one unexpected tick; the two cases here are separate revocation defects
found while adversarially reviewing that path. They are kept in their own
suite so the in-scope #587 suites stay green and the findings can be filed
as separate labelled issues.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = "client\ndev tun\nproto udp\nremote vpn.example.net 1194\nauth-user-pass\n"


def _payload(username: str, password: str) -> dict:
    return {
        "config_filename": "client.ovpn",
        "config_text": SAMPLE_OVPN,
        "has_credentials": True,
        "username": username,
        "password": password,
    }


class OutOfScopeRevocationOrderingUAT(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        install.start()
        self.addCleanup(install.stop)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-587-out-of-scope",
        }, clear=True):
            return Settings.from_env()

    def test_disconnect_exception_does_not_block_credential_wipe_when_peer_gone(self) -> None:
        # Out-of-scope: revocation ordering. disconnect() runs before the unlink
        # in _revoke_local_credentials, so an exception from disconnect (here a
        # process-table read failure) aborts the whole revocation. Every later
        # tick hits the same failure, so auth.txt survives indefinitely even
        # though the source peer is gone. The wipe should not depend on the
        # process stop succeeding.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            vpn_manager.import_from_peer(
                settings, _payload("peer-user", "secret"), source_peer_id="source-peer", source_peer_name="Source",
            )
            auth_path = vpn_manager.auth_path(settings)
            self.assertTrue(auth_path.exists())

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid",
                                      side_effect=RuntimeError("process table unreadable")):
                try:
                    vpn_manager.check_sharing_revocation(settings)
                except Exception:
                    pass

            self.assertFalse(
                auth_path.exists(),
                "source peer left the roster; credentials must be wiped even if disconnect() raised",
            )

    def test_stale_revocation_does_not_wipe_credentials_reimported_from_another_peer(self) -> None:
        # Out-of-scope: provenance race. check_sharing_revocation snapshots
        # source_peer_id="source-a", then makes a slow peer call without any lock.
        # While that call is in flight, the HTTP thread imports a config from
        # peer-b. The stale 404 for peer-a then revokes unconditionally and
        # deletes peer-b's freshly imported credentials. Revocation should only
        # apply if the config still belongs to the peer that said "not sharing".
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings(Path(tmp))
            vpn_manager.import_from_peer(
                settings, _payload("user-a", "pass-a"), source_peer_id="source-a", source_peer_name="A",
            )

            def _import_from_b_then_404(*_args, **_kwargs):
                vpn_manager.import_from_peer(
                    settings, _payload("user-b", "pass-b"), source_peer_id="source-b", source_peer_name="B",
                )
                raise HTTPError("https://peer-a/v1/api/peer/vpn/config", 404, "Not Found", None, None)

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"device_id": "source-a"}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                               side_effect=_import_from_b_then_404):
                vpn_manager.check_sharing_revocation(settings)

            state = vpn_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "source-b")
            self.assertTrue(
                state["has_credentials"],
                "stale revocation from source-a must not strip credentials now owned by source-b",
            )
            self.assertTrue(vpn_manager.auth_path(settings).exists())


if __name__ == "__main__":
    unittest.main()
