"""Issue #626: stale SMTP/Tailnet revocation must preserve re-imported secrets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.smtp_manager as smtp_manager
import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings

SMTP_A = {
    "host": "smtp-a.example.com", "port": 587, "use_starttls": True,
    "use_ssl": False, "username": "a@example.com", "password": "password-a",
    "from_address": "drone-a@example.com", "recipient_email": "owner@example.com",
}
SMTP_B = {**SMTP_A, "host": "smtp-b.example.com", "username": "b@example.com", "password": "password-b"}


def _settings(root: Path, device_id: str) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
    }, clear=True):
        return Settings.from_env()


def _fake_tailscale_cli() -> mock.MagicMock:
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _fake_tailscale_run(args, **_kwargs):
    if "status" in args and "--json" in args:
        payload = {
            "BackendState": "Running", "Version": "1.70.0",
            "Self": {"TailscaleIPs": ["100.64.0.5"], "DNSName": "drone.tailxyz.ts.net.", "ID": "self-id"},
            "CurrentTailnet": {"Name": "example.ts.net", "MagicDNSSuffix": "tailxyz.ts.net"}, "Peer": {},
        }
        return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


class RevocationCheckToWriteRaceUAT(unittest.TestCase):
    def test_smtp_import_between_recheck_and_wipe_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-smtp")
            smtp_manager.import_from_peer(settings, SMTP_A, source_peer_id="smtp-a", source_peer_name="A")

            def import_before_wipe(_settings_arg, reason):
                smtp_manager.import_from_peer(settings, SMTP_B, source_peer_id="smtp-b", source_peer_name="B")
                original(settings, reason)

            original = smtp_manager._revoke_local_credentials
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "smtp-a"}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=HTTPError("https://smtp-a/config", 404, "Not Found", None, None)), \
                    mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True), \
                    mock.patch.object(smtp_manager, "_revoke_local_credentials", side_effect=import_before_wipe):
                smtp_manager.check_sharing_revocation(settings)

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "smtp-b")
            self.assertEqual(state["password"], "password-b")
            self.assertEqual(state["revoked_reason"], "")

    def test_tailnet_import_between_recheck_and_wipe_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-tailnet")
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_tailscale_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-a", "enrolled": True}, source_peer_id="ts-a", source_peer_name="A",
                )

                def import_before_wipe(_settings_arg, reason):
                    tailnet_service.import_tailnet_from_peer(
                        settings, {"auth_key": "tskey-auth-b", "enrolled": True}, source_peer_id="ts-b", source_peer_name="B",
                    )
                    original(settings, reason)

                original = tailnet_service._revoke_local_sharing
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "ts-a"}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=HTTPError("https://ts-a/config", 404, "Not Found", None, None)), \
                        mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True), \
                        mock.patch.object(tailnet_service, "_revoke_local_sharing", side_effect=import_before_wipe):
                    tailnet_service.check_tailnet_sharing_revocation(settings)

            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "ts-b")
            self.assertEqual(state["auth_key"], "tskey-auth-b")
            self.assertEqual(state["revoked_reason"], "")


if __name__ == "__main__":
    unittest.main()
