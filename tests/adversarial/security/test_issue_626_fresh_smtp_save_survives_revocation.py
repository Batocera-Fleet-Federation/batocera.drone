"""Protect a fresh SMTP credential save from a stale peer revocation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.smtp_manager as smtp_manager
from app.drone_api import Settings


def _settings(root: Path) -> Settings:
    with mock.patch.dict("os.environ", {
        "USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "adversarial-626-fresh-smtp-save",
    }, clear=True):
        return Settings.from_env()


class FreshSmtpSaveRevocationSecurityTest(unittest.TestCase):
    def test_fresh_local_password_saved_after_provenance_recheck_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp))
            smtp_manager.import_from_peer(settings, {
                "host": "smtp-a.example.com", "port": 587, "username": "a",
                "password": "password-a", "from_address": "a@example.com",
                "recipient_email": "owner@example.com",
            }, source_peer_id="smtp-a")

            original_save = smtp_manager._save_state
            injected = False

            def save_with_interleaved_local_update(settings_arg, **updates):
                nonlocal injected
                if not injected and updates.get("password") == "":
                    injected = True
                    smtp_manager.update_settings(settings_arg, {
                        "host": "smtp-local.example.com", "username": "local",
                        "password": "fresh-local-password", "from_address": "local@example.com",
                        "recipient_email": "owner@example.com",
                    })
                return original_save(settings_arg, **updates)

            with mock.patch.object(smtp_manager, "_save_state", side_effect=save_with_interleaved_local_update):
                smtp_manager._revoke_credentials_from_peer(settings, "smtp-a", "stale 404")

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["password"], "fresh-local-password")
            self.assertEqual(state["source_peer_id"], "")


if __name__ == "__main__":
    unittest.main()
