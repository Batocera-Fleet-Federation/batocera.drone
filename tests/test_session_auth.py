"""Session-cookie login: SessionStore lifecycle, cookie helpers, the
GET /auth/session + POST /auth/login + POST /auth/logout handlers, and the
credentials-update-revokes-other-sessions behavior.

Replaces the old Basic-Auth scheme (see app/common/auth.py's module
docstring for why); test_remote_admin.py covers the corresponding
peer-to-peer login+cookie proxy rework.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.common.auth import (
    SESSION_COOKIE_NAME,
    DroneCredentialStore,
    SessionAuth,
    SessionStore,
    build_session_cookie,
    clear_session_cookie,
)
from app.drone_api import Settings
from app.storage.state_store import create_session as _create_session
from app.storage.state_store import database_path_for_legacy_file
from app.storage.state_store import get_session as _get_session


def _build_settings(root: Path, *, http_only: bool = True) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_APP_USERNAME": "batocera",
        "DRONE_APP_PASSWORD": "batocera-test-password",
        "HTTP_ONLY": "1" if http_only else "0",
        "OVERMIND_DEVICE_ID": "test-drone",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _iso(delta_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).replace(microsecond=0).isoformat()


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "state.sqlite3"
        self.store = SessionStore(self.db_path, ttl_seconds=3600)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_and_validate_round_trip(self) -> None:
        token = self.store.create("batocera")
        self.assertEqual(self.store.validate(token), "batocera")

    def test_validate_rejects_unknown_token(self) -> None:
        self.assertIsNone(self.store.validate("nonexistent-token"))
        self.assertIsNone(self.store.validate(None))
        self.assertIsNone(self.store.validate(""))

    def test_validate_rejects_and_deletes_an_expired_session(self) -> None:
        token = "expired-token"
        _create_session(self.db_path, token, "batocera", _iso(-10))
        self.assertIsNone(self.store.validate(token))
        self.assertIsNone(_get_session(self.db_path, token))

    def test_validate_touches_expiry_when_last_seen_is_old(self) -> None:
        token = "stale-touch-token"
        _create_session(self.db_path, token, "batocera", _iso(3600))
        # Directly back-date last_seen_at far enough that the (mocked, short)
        # touch throttle is guaranteed to trigger a refresh.
        from app.storage.state_store import open_database

        with open_database(self.db_path) as connection:
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE token = ?",
                (_iso(-10 * 24 * 3600), token),
            )
        with mock.patch("app.common.auth.DRONE_SESSION_TOUCH_THROTTLE_SECONDS", 60.0):
            self.store.validate(token)
        refreshed = _get_session(self.db_path, token)
        self.assertNotEqual(refreshed["last_seen_at"], _iso(-10 * 24 * 3600))

    def test_validate_skips_touch_within_throttle_window(self) -> None:
        token = self.store.create("batocera")
        before = _get_session(self.db_path, token)
        with mock.patch("app.common.auth.DRONE_SESSION_TOUCH_THROTTLE_SECONDS", 6 * 3600.0):
            self.store.validate(token)
        after = _get_session(self.db_path, token)
        self.assertEqual(before["last_seen_at"], after["last_seen_at"])

    def test_revoke_deletes_the_session(self) -> None:
        token = self.store.create("batocera")
        self.store.revoke(token)
        self.assertIsNone(self.store.validate(token))

    def test_revoke_all_except_keeps_one_session_alive(self) -> None:
        kept = self.store.create("batocera")
        dropped = self.store.create("batocera")
        revoked_count = self.store.revoke_all(except_token=kept)
        self.assertEqual(revoked_count, 1)
        self.assertEqual(self.store.validate(kept), "batocera")
        self.assertIsNone(self.store.validate(dropped))

    def test_revoke_all_with_no_exception_clears_everything(self) -> None:
        token_a = self.store.create("batocera")
        token_b = self.store.create("batocera")
        self.store.revoke_all()
        self.assertIsNone(self.store.validate(token_a))
        self.assertIsNone(self.store.validate(token_b))


class CookieHelperTests(unittest.TestCase):
    def test_build_session_cookie_includes_expected_attributes(self) -> None:
        cookie = build_session_cookie("abc123", secure=True)
        self.assertIn(f"{SESSION_COOKIE_NAME}=abc123", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("Path=/", cookie)

    def test_build_session_cookie_omits_secure_over_http_only(self) -> None:
        cookie = build_session_cookie("abc123", secure=False)
        self.assertNotIn("Secure", cookie)

    def test_build_session_cookie_max_age(self) -> None:
        cookie = build_session_cookie("abc123", secure=False, max_age=120)
        self.assertIn("Max-Age=120", cookie)

    def test_clear_session_cookie_expires_immediately_and_empties_value(self) -> None:
        cookie = clear_session_cookie(secure=True)
        self.assertIn(f"{SESSION_COOKIE_NAME}=;", cookie)
        self.assertIn("Max-Age=0", cookie)


class _FakeHandler:
    """Minimal stand-in for RomRequestHandler -- same pattern as
    test_remote_admin.py/test_es_collections.py's fake handlers."""

    def __init__(self, settings: Settings, auth: SessionAuth, *, headers=None, body: bytes = b"") -> None:
        self.settings = settings
        self.auth = auth
        self.headers = headers or {}
        self.rfile = mock.Mock()
        self.rfile.read.return_value = body
        self.client_address = ("203.0.113.9", 51000)
        self.response = None  # (status, payload, extra_headers)

    def _send_json(self, status_code: int, payload: dict, cache_key=None, extra_headers=None) -> None:
        self.response = (status_code, payload, extra_headers or {})


def _handler(settings: Settings, auth: SessionAuth, **kwargs) -> _FakeHandler:
    from app.web import handlers_auth

    class Handler(handlers_auth.HandlersAuthMixin, _FakeHandler):
        pass

    return Handler(settings, auth, **kwargs)


class HandlersAuthMixinTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "userdata"
        self.settings = _build_settings(self.root)
        db_path = database_path_for_legacy_file(self.settings.credentials_file)
        store = DroneCredentialStore(
            self.settings.credentials_file, self.settings.username, self.settings.password, state_database_file=db_path
        )
        self.auth = SessionAuth(credential_store=store, session_store=SessionStore(db_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_session_endpoint_unauthenticated_with_no_cookie(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_session()
        status, payload, _ = handler.response
        self.assertEqual(status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertFalse(payload["setup_required"])

    def test_session_endpoint_authenticated_with_valid_cookie(self) -> None:
        token = self.auth.session_store.create("batocera")
        handler = _handler(self.settings, self.auth, headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"})
        handler._handle_auth_session()
        status, payload, _ = handler.response
        self.assertEqual(status, 200)
        self.assertTrue(payload["authenticated"])
        self.assertEqual(payload["username"], "batocera")

    def test_login_success_sets_cookie(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_login({"username": "batocera", "password": "batocera-test-password"})
        status, payload, extra_headers = handler.response
        self.assertEqual(status, 200)
        self.assertEqual(payload["username"], "batocera")
        self.assertIn(SESSION_COOKIE_NAME, extra_headers.get("Set-Cookie", ""))

    def test_login_failure_is_401_and_sets_no_cookie(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_login({"username": "batocera", "password": "wrong"})
        status, payload, extra_headers = handler.response
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", extra_headers)

    def test_login_missing_fields_is_401(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_login({"username": "batocera"})
        self.assertEqual(handler.response[0], 401)

    def test_logout_clears_a_valid_session(self) -> None:
        token = self.auth.session_store.create("batocera")
        handler = _handler(self.settings, self.auth, headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"})
        handler._handle_auth_logout()
        status, payload, extra_headers = handler.response
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", extra_headers.get("Set-Cookie", ""))
        self.assertIsNone(self.auth.session_store.validate(token))

    def test_logout_with_no_session_is_still_200(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_logout()
        self.assertEqual(handler.response[0], 200)

    def test_secure_flag_follows_http_only_setting(self) -> None:
        tls_settings = _build_settings(self.root, http_only=False)
        handler = _handler(tls_settings, self.auth)
        handler._handle_auth_login({"username": "batocera", "password": "batocera-test-password"})
        _, _, extra_headers = handler.response
        self.assertIn("Secure", extra_headers["Set-Cookie"])

        handler2 = _handler(self.settings, self.auth)  # http_only=True from setUp
        handler2._handle_auth_login({"username": "batocera", "password": "batocera-test-password"})
        _, _, extra_headers2 = handler2.response
        self.assertNotIn("Secure", extra_headers2["Set-Cookie"])


class FirstBootSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "userdata"
        self.settings = _build_settings(self.root)
        db_path = self.root / "unconfigured-state.sqlite3"
        store = DroneCredentialStore(
            self.root / "system" / "drone-app" / "credentials.json",
            state_database_file=db_path,
        )
        self.auth = SessionAuth(credential_store=store, session_store=SessionStore(db_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_session_reports_setup_required_without_credentials(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_session()
        self.assertEqual(handler.response[0], 200)
        self.assertTrue(handler.response[1]["setup_required"])
        self.assertTrue(self.auth.credential_store.setup_token_path.is_file())

    def test_setup_initializes_once_and_authenticates_the_browser(self) -> None:
        setup_token = self.auth.credential_store.ensure_setup_token()
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_setup(
            {
                "setup_token": setup_token,
                "username": "arcade-admin",
                "password": "CorrectHorseBatteryStaple",
                "password_confirmation": "CorrectHorseBatteryStaple",
            }
        )
        status, payload, headers = handler.response
        self.assertEqual(status, 201)
        self.assertEqual(payload["username"], "arcade-admin")
        self.assertIn(SESSION_COOKIE_NAME, headers["Set-Cookie"])
        self.assertFalse(self.auth.credential_store.setup_token_path.exists())
        self.assertIsNotNone(self.auth.login("arcade-admin", "CorrectHorseBatteryStaple"))

        second = _handler(self.settings, self.auth)
        second._handle_auth_setup(
            {
                "setup_token": setup_token,
                "username": "attacker",
                "password": "AnotherLongPassword",
                "password_confirmation": "AnotherLongPassword",
            }
        )
        self.assertEqual(second.response[0], 409)

    def test_setup_rejects_wrong_code_and_weak_or_mismatched_passwords(self) -> None:
        wrong_code = _handler(self.settings, self.auth)
        wrong_code._handle_auth_setup(
            {
                "setup_token": "wrong",
                "username": "arcade-admin",
                "password": "CorrectHorseBatteryStaple",
                "password_confirmation": "CorrectHorseBatteryStaple",
            }
        )
        self.assertEqual(wrong_code.response[0], 403)

        mismatch = _handler(self.settings, self.auth)
        mismatch._handle_auth_setup(
            {
                "setup_token": self.auth.credential_store.ensure_setup_token(),
                "username": "arcade-admin",
                "password": "CorrectHorseBatteryStaple",
                "password_confirmation": "different-password",
            }
        )
        self.assertEqual(mismatch.response[0], 400)

        weak = _handler(self.settings, self.auth)
        with self.assertRaisesRegex(ValueError, "at least 12"):
            weak._handle_auth_setup(
                {
                    "setup_token": self.auth.credential_store.ensure_setup_token(),
                    "username": "arcade-admin",
                    "password": "too-short",
                    "password_confirmation": "too-short",
                }
            )

    def test_login_fails_closed_until_setup_completes(self) -> None:
        handler = _handler(self.settings, self.auth)
        handler._handle_auth_login({"username": "batocera", "password": "linux"})
        self.assertEqual(handler.response[0], 409)
        self.assertTrue(handler.response[1]["setup_required"])


class CredentialsUpdateRevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "userdata"
        self.settings = _build_settings(self.root)
        db_path = database_path_for_legacy_file(self.settings.credentials_file)
        store = DroneCredentialStore(
            self.settings.credentials_file, self.settings.username, self.settings.password, state_database_file=db_path
        )
        self.auth = SessionAuth(credential_store=store, session_store=SessionStore(db_path))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _handler(self, **kwargs):
        from app.web import handlers_system

        class Handler(handlers_system.HandlersSystemMixin, _FakeHandler):
            pass

        return Handler(self.settings, self.auth, **kwargs)

    def test_credentials_update_revokes_other_sessions_but_keeps_the_caller(self) -> None:
        caller_token = self.auth.session_store.create("batocera")
        other_token = self.auth.session_store.create("batocera")
        handler = self._handler()
        handler.session_token = caller_token
        handler._handle_admin_credentials_update({"username": "arcade-admin", "password": "BetterPass123"})
        status, payload, _ = handler.response
        self.assertEqual(status, 200)
        self.assertEqual(payload["other_sessions_revoked"], 1)
        # The caller's own session survives the credentials it was
        # authenticated under changing out from under it...
        self.assertEqual(self.auth.session_store.validate(caller_token), "batocera")
        # ...but every other session is gone.
        self.assertIsNone(self.auth.session_store.validate(other_token))
        # And the new credentials are the ones that work going forward.
        self.assertIsNone(self.auth.login("batocera", "batocera-test-password"))
        self.assertIsNotNone(self.auth.login("arcade-admin", "BetterPass123"))


if __name__ == "__main__":
    unittest.main()
