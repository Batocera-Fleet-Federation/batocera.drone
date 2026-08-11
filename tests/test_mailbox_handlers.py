"""HTTP-level tests for the enrollment-mailbox routes added to
HandlersMailboxMixin (admin: status, config, sharing, pull-from-peer,
check-now) and HandlersPeerMixin (_handle_peer_mailbox_config). Business
logic itself is covered in test_enrollment_mailbox.py -- these just verify
the handler layer calls into it correctly and maps results to the right
HTTP status codes. Mirrors test_tailnet_handlers.py's shape closely.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.enrollment_mailbox as enrollment_mailbox
from app.drone_api import Settings
from app.web import handlers_mailbox, handlers_peer


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "mailbox-handler-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class _FakeHandler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.response = None

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)


def _handler(settings: Settings, **kwargs) -> _FakeHandler:
    class Handler(handlers_mailbox.HandlersMailboxMixin, _FakeHandler):
        pass

    return Handler(settings, **kwargs)


class MailboxStatusHandlerTests(unittest.TestCase):
    def test_status_never_leaks_raw_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo", "github_token": "ghp_secret"})
            handler = _handler(settings)
            handler._handle_admin_mailbox_status()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["has_token"])
            self.assertNotIn("github_token", payload)


class MailboxConfigHandlerTests(unittest.TestCase):
    def test_saves_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_mailbox_config_update({"github_repo": "acct/repo", "github_token": "ghp_x"})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["has_config"])

    def test_missing_repo_is_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            handler._handle_admin_mailbox_config_update({"github_token": "ghp_x"})
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertIn("error", payload)


class MailboxSharingHandlerTests(unittest.TestCase):
    def test_enables_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo", "github_token": "ghp_x"})
            handler = _handler(settings)
            handler._handle_admin_mailbox_sharing({"enabled": True})
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertTrue(payload["sharing_enabled"])

    def test_rejects_sharing_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.import_from_peer(
                settings, {"github_repo": "acct/repo", "github_token": "ghp_x"}, source_peer_id="peer-1",
            )
            handler = _handler(settings)
            handler._handle_admin_mailbox_sharing({"enabled": True})
            status, payload = handler.response
            self.assertEqual(status, 400)
            self.assertIn("error", payload)


class MailboxPullFromPeerHandlerTests(unittest.TestCase):
    def test_requires_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with self.assertRaises(ValueError):
                handler._handle_admin_mailbox_pull_from_peer({})

    def test_unknown_peer_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(handlers_mailbox._local_network, "get_paired_peer", return_value=None):
                handler._handle_admin_mailbox_pull_from_peer({"peer_id": "ghost"})
            status, _payload = handler.response
            self.assertEqual(status, 404)

    def test_peer_sharing_disabled_maps_404_to_friendly_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/mailbox/config", 404, "not found", None, None)
            with mock.patch.object(handlers_mailbox._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_mailbox, "_peer_get_json_for_peer", side_effect=error):
                handler._handle_admin_mailbox_pull_from_peer({"peer_id": "peer-1"})
            status, payload = handler.response
            self.assertEqual(status, 404)
            self.assertIn("sharing", payload["error"].lower())

    def test_unreachable_peer_is_502(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch.object(handlers_mailbox._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_mailbox, "_peer_get_json_for_peer", side_effect=OSError("unreachable")):
                handler._handle_admin_mailbox_pull_from_peer({"peer_id": "peer-1"})
            status, _payload = handler.response
            self.assertEqual(status, 502)

    def test_successful_pull_adopts_config_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            remote_payload = {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"}
            with mock.patch.object(handlers_mailbox._local_network, "get_paired_peer", return_value=peer), \
                    mock.patch.object(handlers_mailbox, "_peer_get_json_for_peer", return_value=(remote_payload, "https://peer")):
                handler._handle_admin_mailbox_pull_from_peer({"peer_id": "peer-1"})
            status, _payload = handler.response
            self.assertEqual(status, 200)
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["github_repo"], "acct/shared-repo")


class MailboxCheckNowHandlerTests(unittest.TestCase):
    def test_returns_200_on_success_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(enrollment_mailbox, "check_and_notify_if_needed", return_value={"status": "skipped"}):
                handler._handle_admin_mailbox_check_now()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["status"], "skipped")

    def test_returns_502_on_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _handler(settings)
            with mock.patch.object(enrollment_mailbox, "check_and_notify_if_needed", return_value={"status": "error", "error": "boom"}):
                handler._handle_admin_mailbox_check_now()
            status, payload = handler.response
            self.assertEqual(status, 502)
            self.assertEqual(payload["error"], "boom")


class _FakePeerHandler:
    """Minimal stand-in for RomRequestHandler's peer-serving surface,
    mirroring test_tailnet_handlers.py's _FakePeerHandler pattern."""

    def __init__(self, settings: Settings, *, authorized: bool = True) -> None:
        self.settings = settings
        self._authorized = authorized
        self.response = None

    def _peer_request_authorized(self) -> bool:
        return self._authorized

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload)

    def log_message(self, *args, **kwargs) -> None:
        pass


def _peer_handler(settings: Settings, **kwargs) -> _FakePeerHandler:
    class Handler(_FakePeerHandler, handlers_peer.HandlersPeerMixin):
        pass

    return Handler(settings, **kwargs)


class MailboxPeerConfigHandlerTests(unittest.TestCase):
    def test_rejects_unauthorized_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = _peer_handler(settings, authorized=False)
            handler._handle_peer_mailbox_config()
            self.assertIsNone(handler.response)

    def test_404_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo", "github_token": "ghp_x"})
            handler = _peer_handler(settings)
            handler._handle_peer_mailbox_config()
            self.assertEqual(handler.response[0], 404)

    def test_404_when_never_configured_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox._save_state(settings, sharing_enabled=True)  # bypass the normal setter
            handler = _peer_handler(settings)
            handler._handle_peer_mailbox_config()
            self.assertEqual(handler.response[0], 404)

    def test_200_with_config_when_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo", "github_token": "ghp_reusable"})
            enrollment_mailbox.set_sharing_enabled(settings, True)
            handler = _peer_handler(settings)
            handler._handle_peer_mailbox_config()
            status, payload = handler.response
            self.assertEqual(status, 200)
            self.assertEqual(payload["github_repo"], "acct/repo")
            self.assertEqual(payload["github_token"], "ghp_reusable")


if __name__ == "__main__":
    unittest.main()
