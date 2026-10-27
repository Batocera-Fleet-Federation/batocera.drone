"""Out of scope for #598: SMTP and Tailnet still apply stale 404 revocations.

#598 is the VPN credential race. SMTP and Tailnet copy the same snapshot-
then-HTTP-then-unconditional-wipe shape and do not re-read provenance before
acting. A re-import from another peer during the in-flight call is still
wiped. These assertions are not a delivery gate for the VPN fix.
"""

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
    "host": "smtp-a.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "a@example.com",
    "password": "password-a",
    "from_address": "drone-a@example.com",
    "recipient_email": "owner@example.com",
}
SMTP_B = {
    **SMTP_A,
    "host": "smtp-b.example.com",
    "username": "b@example.com",
    "password": "password-b",
    "from_address": "drone-b@example.com",
}


def _env_settings(root: Path, device_id: str) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
    }, clear=True):
        return Settings.from_env()


def _status_json_result() -> mock.Mock:
    payload = {
        "BackendState": "Running",
        "Version": "1.70.0",
        "Self": {"TailscaleIPs": ["100.64.0.5"], "DNSName": "drone.tailxyz.ts.net.", "ID": "self-id"},
        "CurrentTailnet": {"Name": "example.ts.net", "MagicDNSSuffix": "tailxyz.ts.net"},
        "Peer": {},
    }
    return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")


def _fake_tailscale_cli() -> mock.MagicMock:
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _fake_subprocess_run(cli_args, **_kwargs):
    if "status" in cli_args and "--json" in cli_args:
        return _status_json_result()
    return mock.Mock(returncode=0, stdout="", stderr="")


class OutOfScopeSiblingSharingRevocationUAT(unittest.TestCase):
    def test_smtp_stale_404_does_not_wipe_password_reimported_from_another_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _env_settings(Path(tmp), "adversarial-598-oos-smtp")
            smtp_manager.import_from_peer(settings, SMTP_A, source_peer_id="smtp-a", source_peer_name="A")

            def import_b_then_404(*_args, **_kwargs):
                smtp_manager.import_from_peer(settings, SMTP_B, source_peer_id="smtp-b", source_peer_name="B")
                raise HTTPError("https://smtp-a/v1/api/peer/smtp/config", 404, "Not Found", None, None)

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "smtp-a"}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404):
                smtp_manager.check_sharing_revocation(settings)

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "smtp-b")
            self.assertEqual(
                state["password"],
                "password-b",
                "stale SMTP 404 from smtp-a must not strip a password now owned by smtp-b",
            )
            self.assertEqual(state["revoked_reason"], "")

    def test_tailnet_stale_404_does_not_wipe_key_reimported_from_another_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _env_settings(Path(tmp), "adversarial-598-oos-tailnet")
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-a", "enrolled": True},
                    source_peer_id="ts-a", source_peer_name="A",
                )

                def import_b_then_404(*_args, **_kwargs):
                    tailnet_service.import_tailnet_from_peer(
                        settings, {"auth_key": "tskey-auth-b", "enrolled": True},
                        source_peer_id="ts-b", source_peer_name="B",
                    )
                    raise HTTPError("https://ts-a/v1/api/peer/tailnet/config", 404, "Not Found", None, None)

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "ts-a"}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404):
                    tailnet_service.check_tailnet_sharing_revocation(settings)

            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "ts-b")
            self.assertEqual(
                state["auth_key"],
                "tskey-auth-b",
                "stale Tailnet 404 from ts-a must not strip a key now owned by ts-b",
            )
            self.assertEqual(state["revoked_reason"], "")


if __name__ == "__main__":
    unittest.main()
