import tempfile
import unittest
import ast
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.smtp_manager as smtp_manager
from app.common.settings import Settings


def _build_settings(root: Path) -> Settings:
    # Unlike VPN, smtp_manager has no install-root-relative storage location
    # to patch -- settings (including the password) live entirely in the
    # shared app_state JSON blob, keyed off DRONE_STATE_DATABASE_FILE.
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "smtp-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


_VALID_SETTINGS = {
    "host": "smtp.example.com",
    "port": 587,
    "use_starttls": True,
    "use_ssl": False,
    "username": "me@example.com",
    "password": "hunter2",
    "from_address": "drone@example.com",
    "recipient_email": "owner@example.com",
}


class UpdateSettingsTests(unittest.TestCase):
    def test_requires_host_from_and_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for missing in ("host", "from_address", "recipient_email"):
                payload = {**_VALID_SETTINGS, missing: ""}
                with self.assertRaises(ValueError):
                    smtp_manager.update_settings(settings, payload)

    def test_rejects_invalid_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "port": 0})
            with self.assertRaises(ValueError):
                smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "port": "not-a-number"})

    def test_saves_and_sanitizes_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.update_settings(settings, _VALID_SETTINGS)
            self.assertTrue(result["has_config"])
            self.assertTrue(result["has_password"])
            self.assertNotIn("password", result)
            state = smtp_manager._load_state(settings)
            self.assertEqual(state["password"], "hunter2")  # stored in full internally

    def test_blank_password_on_update_keeps_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "password": ""})
            self.assertEqual(smtp_manager._load_state(settings)["password"], "hunter2")

    def test_direct_update_clears_prior_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1")
            self.assertEqual(smtp_manager._load_state(settings)["source_peer_id"], "peer-1")
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            self.assertEqual(smtp_manager._load_state(settings)["source_peer_id"], "")

    def test_default_state_before_any_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = smtp_manager.get_settings(settings)
            self.assertFalse(state["has_config"])
            self.assertTrue(state["smtp_enabled"])  # opt-out, not opt-in
            self.assertFalse(state["sharing_enabled"])
            for event_type in ("vpn_connected", "torrent_completed"):
                self.assertTrue(state["notify"][event_type])


class NotificationTogglesTests(unittest.TestCase):
    def test_update_merges_only_provided_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_notification_toggles(settings, {"vpn_connected": False})
            state = smtp_manager._load_state(settings)
            self.assertFalse(state["notify"]["vpn_connected"])
            self.assertTrue(state["notify"]["vpn_disconnected"])  # untouched key stays True

    def test_unknown_keys_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.update_notification_toggles(settings, {"not_a_real_event": False})
            self.assertNotIn("not_a_real_event", result["notify"])


class DigestIntervalTests(unittest.TestCase):
    def test_defaults_to_five_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(smtp_manager.get_settings(settings)["digest_interval_seconds"], 300)

    def test_saves_a_valid_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.update_digest_interval(settings, 900)
            self.assertEqual(result["digest_interval_seconds"], 900)
            self.assertEqual(smtp_manager._load_state(settings)["digest_interval_seconds"], 900)

    def test_accepts_the_one_minute_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.update_digest_interval(settings, 60)
            self.assertEqual(result["digest_interval_seconds"], 60)

    def test_accepts_the_twenty_four_hour_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.update_digest_interval(settings, 86400)
            self.assertEqual(result["digest_interval_seconds"], 86400)

    def test_rejects_below_one_minute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                smtp_manager.update_digest_interval(settings, 59)

    def test_rejects_above_twenty_four_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                smtp_manager.update_digest_interval(settings, 86401)

    def test_rejects_non_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                smtp_manager.update_digest_interval(settings, "not-a-number")

    def test_is_local_only_and_not_reset_by_settings_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_digest_interval(settings, 1800)
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            self.assertEqual(smtp_manager._load_state(settings)["digest_interval_seconds"], 1800)

    def test_excluded_from_shared_export_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.update_digest_interval(settings, 1800)
            smtp_manager.set_sharing_enabled(settings, True)
            payload = smtp_manager.export_payload(settings)
            self.assertNotIn("digest_interval_seconds", payload)

    def test_out_of_range_stored_value_is_clamped_on_load(self) -> None:
        # Defensive: a value that somehow landed outside the valid range
        # (manual DB edit, a future bug) is clamped on read rather than
        # trusted, matching every other bounds-checked field in this module.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager._save_state(settings, digest_interval_seconds=999999)
            self.assertEqual(smtp_manager._load_state(settings)["digest_interval_seconds"], 86400)
            smtp_manager._save_state(settings, digest_interval_seconds=1)
            self.assertEqual(smtp_manager._load_state(settings)["digest_interval_seconds"], 60)


class SharingEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(smtp_manager.get_settings(settings)["sharing_enabled"])

    def test_set_sharing_enabled_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            result = smtp_manager.set_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])

    def test_rejected_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1")
            with self.assertRaises(ValueError):
                smtp_manager.set_sharing_enabled(settings, True)

    def test_disabling_always_allowed_even_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1")
            result = smtp_manager.set_sharing_enabled(settings, False)
            self.assertFalse(result["sharing_enabled"])


class ExportPayloadTests(unittest.TestCase):
    def test_none_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            self.assertIsNone(smtp_manager.export_payload(settings))

    def test_none_when_no_config_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertIsNone(smtp_manager.export_payload(settings))

    def test_returns_shared_fields_including_password_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_sharing_enabled(settings, True)
            payload = smtp_manager.export_payload(settings)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["host"], "smtp.example.com")
            self.assertEqual(payload["password"], "hunter2")

    def test_excludes_local_only_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_sharing_enabled(settings, True)
            payload = smtp_manager.export_payload(settings)
            for local_only_field in ("smtp_enabled", "notify", "sharing_enabled", "source_peer_id"):
                self.assertNotIn(local_only_field, payload)

    def test_none_for_imported_config_even_if_sharing_somehow_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1")
            smtp_manager._save_state(settings, sharing_enabled=True)  # bypass the normal setter on purpose
            self.assertIsNone(smtp_manager.export_payload(settings))


class ImportFromPeerTests(unittest.TestCase):
    def test_requires_source_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="")

    def test_records_provenance_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1", source_peer_name="Peer One")
            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["source_peer_name"], "Peer One")
            self.assertEqual(state["host"], "smtp.example.com")
            self.assertEqual(state["password"], "hunter2")
            self.assertFalse(state["smtp_enabled"])

    def test_does_not_import_local_only_fields_from_a_malicious_payload(self) -> None:
        # notify/smtp_enabled are never part of _SHARED_FIELDS, so even a
        # peer payload that tries to smuggle them in must be ignored.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            payload = {**_VALID_SETTINGS, "smtp_enabled": False, "notify": {"vpn_connected": False}}
            smtp_manager.import_from_peer(settings, payload, source_peer_id="peer-1")
            state = smtp_manager._load_state(settings)
            self.assertFalse(state["smtp_enabled"])
            self.assertTrue(state["notify"]["vpn_connected"])


class CheckSharingRevocationTests(unittest.TestCase):
    def _imported_settings(self, tmp: str):
        settings = _build_settings(Path(tmp))
        smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1", source_peer_name="Peer One")
        return settings

    def test_noop_when_config_is_self_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(smtp_manager.check_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_revokes_when_source_peer_no_longer_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None):
                self.assertTrue(smtp_manager.check_sharing_revocation(settings))
            state = smtp_manager._load_state(settings)
            self.assertEqual(state["password"], "")
            self.assertIn("no longer paired", state["revoked_reason"])
            self.assertEqual(state["source_peer_id"], "peer-1")  # provenance persists
            self.assertEqual(state["host"], "smtp.example.com")  # rest of config survives

    def test_revokes_when_peer_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/smtp/config", 404, "not found", None, None)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertTrue(smtp_manager.check_sharing_revocation(settings))
            state = smtp_manager._load_state(settings)
            self.assertEqual(state["password"], "")
            self.assertIn("turned off sharing", state["revoked_reason"])

    def test_does_not_revoke_on_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=OSError("unreachable")):
                self.assertFalse(smtp_manager.check_sharing_revocation(settings))
            self.assertEqual(smtp_manager._load_state(settings)["password"], "hunter2")

    def test_never_raises_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=RuntimeError("boom")):
                self.assertFalse(smtp_manager.check_sharing_revocation(settings))


class BootstrapSmtpFromSwarmTests(unittest.TestCase):
    """Unlike VPN, there is no 'connected' concept to gate on -- any sharing
    peer with a host in its payload qualifies immediately."""

    def test_no_paired_peers_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[]):
                self.assertFalse(smtp_manager.bootstrap_smtp_from_swarm(settings))

    def test_imports_from_the_first_sharing_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=(_VALID_SETTINGS, "https://peer")):
                self.assertTrue(smtp_manager.bootstrap_smtp_from_swarm(settings))
            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["host"], "smtp.example.com")

    def test_skips_peer_with_no_host_in_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=({"host": ""}, "https://peer")):
                self.assertFalse(smtp_manager.bootstrap_smtp_from_swarm(settings))

    def test_skips_unreachable_peer_and_succeeds_on_the_next(self) -> None:
        offline_peer = {"drone_id": "peer-offline", "name": "Offline"}
        working_peer = {"drone_id": "peer-2", "name": "Peer Two"}
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))

            def fake_get_json(peer, *_args, **_kwargs):
                if peer["drone_id"] == "peer-offline":
                    raise OSError("unreachable")
                return _VALID_SETTINGS, "https://peer"

            with mock.patch("app.transfer.local_network.paired_peers", return_value=[offline_peer, working_peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=fake_get_json):
                self.assertTrue(smtp_manager.bootstrap_smtp_from_swarm(settings))
            self.assertEqual(smtp_manager._load_state(settings)["source_peer_id"], "peer-2")

    def test_maybe_bootstrap_smtp_never_raises_and_skips_when_already_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch("app.transfer.local_network.paired_peers", side_effect=RuntimeError("boom")):
                smtp_manager.maybe_bootstrap_smtp(settings)  # must not raise, must not even query peers
            self.assertEqual(smtp_manager._load_state(settings)["host"], "smtp.example.com")

    def test_maybe_bootstrap_migrates_legacy_imported_sender_to_relay_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="peer-1")
            smtp_manager._save_state(settings, smtp_enabled=True)  # legacy imported state
            with mock.patch("app.transfer.local_network.paired_peers") as peers:
                smtp_manager.maybe_bootstrap_smtp(settings)
            peers.assert_not_called()
            state = smtp_manager._load_state(settings)
            self.assertFalse(state["smtp_enabled"])
            self.assertEqual(state["source_peer_id"], "peer-1")


class SendMailTests(unittest.TestCase):
    def test_low_level_smtp_is_only_called_by_the_queue_consumer(self) -> None:
        module_path = Path(smtp_manager.__file__)
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        callers = []
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                if isinstance(call.func, ast.Name) and call.func.id in {"send_mail", "send_mail_with_attachment"}:
                    callers.append(function.name)
        self.assertEqual(set(callers), {"_process_outbound_mail_queue_unlocked"})

        app_root = module_path.parents[1]
        smtp_importers = []
        for source_path in app_root.rglob("*.py"):
            if source_path == module_path:
                continue
            if "smtplib" in source_path.read_text(encoding="utf-8"):
                smtp_importers.append(str(source_path.relative_to(app_root)))
        self.assertEqual(smtp_importers, [])

    def test_raises_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(smtp_manager.SmtpSendError):
                smtp_manager.send_mail(settings, "subject", "body")

    def test_uses_starttls_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "use_starttls": True, "use_ssl": False})
            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                smtp_manager.send_mail(settings, "subj", "body")
            smtp_cls.assert_called_once()
            client.starttls.assert_called_once()
            client.login.assert_called_once_with("me@example.com", "hunter2")
            client.send_message.assert_called_once()
            client.quit.assert_called_once()

    def test_uses_ssl_and_skips_starttls_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "use_starttls": False, "use_ssl": True})
            with mock.patch("smtplib.SMTP_SSL") as smtp_ssl_cls, mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_ssl_cls.return_value
                smtp_manager.send_mail(settings, "subj", "body")
            smtp_ssl_cls.assert_called_once()
            smtp_cls.assert_not_called()
            client.starttls.assert_not_called()

    def test_skips_login_when_no_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, {**_VALID_SETTINGS, "username": "", "password": ""})
            with mock.patch("smtplib.SMTP") as smtp_cls:
                client = smtp_cls.return_value
                smtp_manager.send_mail(settings, "subj", "body")
            client.login.assert_not_called()

    def test_wraps_smtp_exceptions(self) -> None:
        import smtplib as real_smtplib
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch("smtplib.SMTP") as smtp_cls:
                smtp_cls.return_value.login.side_effect = real_smtplib.SMTPAuthenticationError(535, b"bad creds")
                with self.assertRaises(smtp_manager.SmtpSendError):
                    smtp_manager.send_mail(settings, "subj", "body")


class SendTestEmailTests(unittest.TestCase):
    def test_request_queues_without_sending_then_worker_persists_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.queue_test_email(settings)
            send_mail.assert_not_called()
            self.assertEqual(result["status"], "queued")
            self.assertEqual(len(smtp_manager._mail_store.pending(settings)), 1)
            self.assertEqual(smtp_manager._load_state(settings)["last_test_result"]["status"], "queued")

            with mock.patch.object(smtp_manager, "send_mail", return_value=None) as send_mail:
                processed = smtp_manager.process_outbound_mail_queue(settings)
            send_mail.assert_called_once()
            self.assertEqual(processed["sent_count"], 1)
            self.assertEqual(smtp_manager._load_state(settings)["last_test_result"]["status"], "ok")

    def test_explicit_mail_is_processed_when_automatic_digest_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_smtp_enabled(settings, False)
            smtp_manager.queue_test_email(settings)
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.process_outbound_mail_queue(settings)
            send_mail.assert_called_once()
            self.assertEqual(result["sent_count"], 1)

    def test_worker_failure_persists_error_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            result = smtp_manager.queue_test_email(settings)
            self.assertEqual(result["status"], "queued")
            with mock.patch.object(smtp_manager, "send_mail", side_effect=smtp_manager.SmtpSendError("nope")):
                processed = smtp_manager.process_outbound_mail_queue(settings)
            self.assertEqual(processed["failed_count"], 1)
            state_result = smtp_manager._load_state(settings)["last_test_result"]
            self.assertEqual(state_result["status"], "error")
            self.assertIn("nope", state_result["error"])
            pending = smtp_manager._mail_store.pending(settings)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["attempts"], 1)


class SendDigestIfNeededTests(unittest.TestCase):
    def test_skips_when_smtp_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_smtp_enabled(settings, False)
            result = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(result["status"], "skipped")

    def test_skips_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(result["status"], "skipped")

    def test_skips_when_no_notification_types_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.update_notification_toggles(
                settings, {event_type: False for event_type in smtp_manager._notifications.EVENT_TYPES}
            )
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.send_digest_if_needed(settings)
            send_mail.assert_not_called()
            self.assertEqual(result["status"], "skipped")

    def test_skips_when_nothing_unsent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.send_digest_if_needed(settings)
            send_mail.assert_not_called()
            self.assertEqual(result["status"], "skipped")

    def test_sends_and_marks_emailed_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager._notifications.record_event(settings, "torrent_completed", "My Game")
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.send_digest_if_needed(settings)
            send_mail.assert_not_called()
            self.assertEqual(result["status"], "queued")
            queued = smtp_manager._mail_store.pending(settings)
            self.assertEqual(len(queued), 1)
            subject, body = queued[0]["subject"], queued[0]["body"]
            self.assertIn("1 new notification", subject)
            self.assertIn("My Game", body)
            self.assertEqual(result["item_count"], 1)
            self.assertIsNone(smtp_manager._load_state(settings)["last_digest_sent_at"])
            with mock.patch.object(smtp_manager, "send_mail") as worker_send:
                processed = smtp_manager.process_outbound_mail_queue(settings)
            worker_send.assert_called_once()
            self.assertEqual(processed["sent_count"], 1)
            self.assertIsNotNone(smtp_manager._load_state(settings)["last_digest_sent_at"])
            # A second run must find nothing left unsent.
            with mock.patch.object(smtp_manager, "send_mail") as send_mail_again:
                second_result = smtp_manager.send_digest_if_needed(settings)
            send_mail_again.assert_not_called()
            self.assertEqual(second_result["status"], "skipped")

    def test_enforces_interval_inside_send_path_even_when_new_events_arrive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager._notifications.record_event(settings, "asset_downloaded", "First")
            with mock.patch.object(smtp_manager, "send_mail") as first_send:
                self.assertEqual(smtp_manager.send_digest_if_needed(settings)["status"], "queued")
            first_send.assert_not_called()
            with mock.patch.object(smtp_manager, "send_mail") as first_worker_send:
                smtp_manager.process_outbound_mail_queue(settings)
            first_worker_send.assert_called_once()

            smtp_manager._notifications.record_event(settings, "asset_downloaded", "Second")
            with mock.patch.object(smtp_manager, "send_mail") as too_soon:
                result = smtp_manager.send_digest_if_needed(settings)
            too_soon.assert_not_called()
            self.assertEqual(result["reason"], "digest interval has not elapsed")

            smtp_manager._save_state(settings, last_digest_attempt_at="2000-01-01T00:00:00+00:00")
            with mock.patch.object(smtp_manager, "send_mail") as after_interval:
                self.assertEqual(smtp_manager.send_digest_if_needed(settings)["status"], "queued")
            after_interval.assert_not_called()
            with mock.patch.object(smtp_manager, "send_mail") as second_worker_send:
                smtp_manager.process_outbound_mail_queue(settings)
            second_worker_send.assert_called_once()

    def test_imported_config_never_sends_smtp_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="smtp-owner")
            smtp_manager._notifications.record_event(settings, "asset_downloaded", "Game")
            with mock.patch.object(smtp_manager, "send_mail") as send_mail:
                result = smtp_manager.send_digest_if_needed(settings)
            send_mail.assert_not_called()
            self.assertIn("relayed", result["reason"])

    def test_imported_config_cannot_reenable_local_digest_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(settings, _VALID_SETTINGS, source_peer_id="smtp-owner")
            result = smtp_manager.set_smtp_enabled(settings, True)
            self.assertFalse(result["smtp_enabled"])
            self.assertFalse(smtp_manager._load_state(settings)["smtp_enabled"])

    def test_multiline_message_is_indented_on_every_line(self) -> None:
        # A drone_updated event's message can carry the release-notes commit
        # list as embedded newlines -- every line must stay indented under
        # its item, not just the first, or the notes read as a new top-level
        # digest entry.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager._notifications.record_event(
                settings,
                "drone_updated",
                "Drone app updated",
                "v1.0.0 -> v1.0.1; restarting to apply.\n\n- Fixed a bug (abc1234)\n- Added a feature (def5678)",
            )
            smtp_manager.send_digest_if_needed(settings)
            body = smtp_manager._mail_store.pending(settings)[0]["body"]
            body_lines = body.splitlines()
            self.assertIn("    v1.0.0 -> v1.0.1; restarting to apply.", body_lines)
            self.assertIn("    - Fixed a bug (abc1234)", body_lines)
            self.assertIn("    - Added a feature (def5678)", body_lines)

    def test_only_includes_enabled_event_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.update_notification_toggles(settings, {"vpn_connected": False})
            smtp_manager._notifications.record_event(settings, "vpn_connected", "should be excluded")
            smtp_manager._notifications.record_event(settings, "torrent_completed", "should be included")
            result = smtp_manager.send_digest_if_needed(settings)
            body = smtp_manager._mail_store.pending(settings)[0]["body"]
            self.assertNotIn("should be excluded", body)
            self.assertIn("should be included", body)
            self.assertEqual(result["item_count"], 1)

    def test_failure_does_not_mark_emailed_and_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager._notifications.record_event(settings, "torrent_completed", "My Game")
            queued = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(queued["status"], "queued")
            with mock.patch.object(smtp_manager, "send_mail", side_effect=smtp_manager.SmtpSendError("down")):
                result = smtp_manager.process_outbound_mail_queue(settings)
            self.assertEqual(result["failed_count"], 1)
            self.assertIn("down", smtp_manager._load_state(settings)["last_digest_error"])
            # Must still be there to retry on the next poll tick.
            unsent = smtp_manager._audit_store.list_unsent_events(settings, ["torrent_completed"])
            self.assertEqual(len(unsent), 1)

    def test_never_raises_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with mock.patch.object(smtp_manager, "_load_state", side_effect=RuntimeError("boom")):
                result = smtp_manager.send_digest_if_needed(settings)
            self.assertEqual(result["status"], "error")


class NotificationRelayTests(unittest.TestCase):
    def test_owner_ingest_is_idempotent_and_identifies_source_in_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_sharing_enabled(settings, True)
            events = [{
                "source_event_id": "17",
                "event_type": "asset_downloaded",
                "title": "Asset downloaded",
                "message": "Zelda.zip",
                "created_at": "2026-08-14T12:00:00+00:00",
            }]
            first = smtp_manager.ingest_relayed_notifications(
                settings, events, source_drone_id="satellite-1", source_drone_name="Living Room"
            )
            second = smtp_manager.ingest_relayed_notifications(
                settings, events, source_drone_id="satellite-1", source_drone_name="Living Room"
            )
            self.assertEqual(first["accepted_event_ids"], ["17"])
            self.assertEqual(second["accepted_event_ids"], ["17"])
            items = smtp_manager._audit_store.list_unsent_events(settings, ["asset_downloaded"])
            self.assertEqual(len(items), 1)
            _subject, body = smtp_manager._compose_digest(items, settings)
            self.assertIn("on Living Room: Asset downloaded", body)

    def test_owner_rejects_relay_when_smtp_sharing_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            with self.assertRaises(PermissionError):
                smtp_manager.ingest_relayed_notifications(
                    settings, [], source_drone_id="satellite-1"
                )

    def test_satellite_relay_posts_to_owner_and_marks_acknowledged_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(
                settings,
                _VALID_SETTINGS,
                source_peer_id="smtp-owner",
                source_peer_name="Owner",
            )
            smtp_manager._notifications.record_event(
                settings, "asset_downloaded", "Asset downloaded", "Zelda.zip"
            )
            peer = {"drone_id": "smtp-owner", "name": "Owner"}

            def accept(_peer, endpoint, payload, _settings, **_kwargs):
                self.assertEqual(endpoint, "/v1/api/peer/smtp/notifications")
                event_ids = [event["source_event_id"] for event in payload["events"]]
                return {"accepted_event_ids": event_ids}, "https://owner"

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_post_json_for_peer", side_effect=accept):
                result = smtp_manager.relay_notifications_to_source(settings)
            self.assertEqual(result["status"], "relayed")
            self.assertEqual(result["item_count"], 1)


class OutboundMailRelayTests(unittest.TestCase):
    def test_satellite_relays_test_job_without_opening_smtp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(
                settings,
                _VALID_SETTINGS,
                source_peer_id="smtp-owner",
                source_peer_name="Owner",
            )
            queued = smtp_manager.queue_test_email(settings)
            self.assertEqual(queued["status"], "queued")
            peer = {"drone_id": "smtp-owner", "name": "Owner"}

            def accept(_peer, endpoint, payload, _settings, **_kwargs):
                self.assertEqual(endpoint, "/v1/api/peer/smtp/mail")
                ids = [job["source_job_id"] for job in payload["jobs"]]
                return {"accepted_job_ids": ids}, "https://owner"

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_post_json_for_peer", side_effect=accept), \
                    mock.patch.object(smtp_manager, "send_mail") as direct_send:
                result = smtp_manager.relay_mail_jobs_to_source(settings)
            direct_send.assert_not_called()
            self.assertEqual(result["status"], "relayed")
            self.assertEqual(smtp_manager._mail_store.pending(settings), [])
            self.assertEqual(smtp_manager._load_state(settings)["last_test_result"]["status"], "relayed")

    def test_owner_ingest_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_sharing_enabled(settings, True)
            jobs = [{
                "source_job_id": "22",
                "kind": "test",
                "subject": "Satellite test",
                "body": "body",
                "created_at": "2026-08-14T12:00:00+00:00",
            }]
            first = smtp_manager.ingest_relayed_mail_jobs(
                settings, jobs, source_drone_id="satellite-1", source_drone_name="Living Room"
            )
            second = smtp_manager.ingest_relayed_mail_jobs(
                settings, jobs, source_drone_id="satellite-1", source_drone_name="Living Room"
            )
            self.assertEqual(first["accepted_job_ids"], ["22"])
            self.assertEqual(second["accepted_job_ids"], ["22"])
            self.assertEqual(len(smtp_manager._mail_store.pending(settings)), 1)

    def test_owner_worker_fetches_remote_backup_over_peer_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.update_settings(settings, _VALID_SETTINGS)
            smtp_manager.set_sharing_enabled(settings, True)
            smtp_manager.ingest_relayed_mail_jobs(
                settings,
                [{
                    "source_job_id": "23",
                    "kind": "config_backup",
                    "subject": "Remote backup",
                    "body": "attached",
                    "attachment_filename": "weekly.tar.gz",
                }],
                source_drone_id="satellite-1",
                source_drone_name="Living Room",
            )
            peer = {"drone_id": "satellite-1", "name": "Living Room"}
            delivered = []

            def download(_peer, endpoint, destination, _settings, **_kwargs):
                self.assertEqual(endpoint, "/v1/api/peer/config-backups/weekly.tar.gz")
                destination.write_bytes(b"backup-data")
                return 11, "https://satellite"

            def send_attachment(_settings, subject, body, path, filename):
                delivered.append((subject, body, path.read_bytes(), filename))

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_download_file_for_peer", side_effect=download), \
                    mock.patch.object(smtp_manager, "send_mail_with_attachment", side_effect=send_attachment), \
                    mock.patch.object(smtp_manager, "send_mail") as plain_send:
                result = smtp_manager.process_outbound_mail_queue(settings)
            plain_send.assert_not_called()
            self.assertEqual(result["sent_count"], 1)
            self.assertEqual(delivered[0][2], b"backup-data")
            self.assertEqual(delivered[0][3], "weekly.tar.gz")
            self.assertEqual(smtp_manager._mail_store.pending(settings), [])

    def test_satellite_relay_does_not_depend_on_local_smtp_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            smtp_manager.import_from_peer(
                settings,
                _VALID_SETTINGS,
                source_peer_id="smtp-owner",
                source_peer_name="Owner",
            )
            self.assertFalse(smtp_manager._load_state(settings)["smtp_enabled"])
            smtp_manager._notifications.record_event(
                settings, "asset_downloaded", "Asset downloaded", "Metroid.zip"
            )
            peer = {"drone_id": "smtp-owner", "name": "Owner"}

            def accept(_peer, _endpoint, payload, _settings, **_kwargs):
                event_ids = [event["source_event_id"] for event in payload["events"]]
                return {"accepted_event_ids": event_ids}, "https://owner"

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_post_json_for_peer", side_effect=accept):
                result = smtp_manager.relay_notifications_to_source(settings)
            self.assertEqual(result["status"], "relayed")
            self.assertEqual(result["item_count"], 1)


if __name__ == "__main__":
    unittest.main()
