"""Invite Drone to Swarm: a persisted OAuth client lets the owner mint a
fresh, ready-to-use Tailscale auth key from this Drone's own admin UI,
instead of visiting Tailscale's site each time. Mirrors
test_tailnet_sharing.py's shapes closely.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings
from app.web import handlers_network


def _http_error(code: int, body: dict) -> HTTPError:
    return HTTPError(
        url="https://api.tailscale.com/api/v2/tailnet/-/keys",
        code=code,
        msg="Bad Request",
        hdrs=None,
        fp=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


def _build_settings(root: Path, **env_overrides) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "tailnet-invite-test",
        **env_overrides,
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class OauthClientStorageTests(unittest.TestCase):
    def test_defaults_to_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            status = tailnet_service.oauth_client_status(settings)
            self.assertFalse(status["configured"])
            self.assertEqual(status["client_id"], "")

    def test_set_persists_and_never_returns_the_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            self.assertTrue(result["configured"])
            self.assertEqual(result["client_id"], "client-abc")
            self.assertEqual(result["tag"], "tag:drone")
            self.assertNotIn("client_secret", result)
            self.assertNotIn("secret-xyz", json.dumps(result))
            status = tailnet_service.oauth_client_status(settings)
            self.assertTrue(status["configured"])

    def test_set_rejects_missing_client_id_secret_or_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                tailnet_service.set_tailnet_oauth_client(settings, "", "secret-xyz", "tag:drone")
            with self.assertRaises(ValueError):
                tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "", "tag:drone")
            with self.assertRaises(ValueError):
                tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "")

    def test_set_rejects_a_malformed_tag(self) -> None:
        # Tailscale rejects an OAuth-client-created key with no tag at all
        # ("tailnet-owned auth key must have tags set", found live against
        # a real account) -- validate the shape before ever calling the API.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            for bad_tag in ("drone", "tag:", "tag:has space", "tag:Has_Underscore"):
                with self.assertRaises(ValueError):
                    tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", bad_tag)

    def test_clear_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            result = tailnet_service.clear_tailnet_oauth_client(settings)
            self.assertFalse(result["configured"])
            self.assertFalse(tailnet_service.oauth_client_status(settings)["configured"])

    def test_resolve_prefers_persisted_over_env_vars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(
                Path(tmp),
                DRONE_TAILSCALE_OAUTH_CLIENT_ID="env-client",
                DRONE_TAILSCALE_OAUTH_CLIENT_SECRET="env-secret",
            )
            tailnet_service.set_tailnet_oauth_client(settings, "persisted-client", "persisted-secret", "tag:drone")
            self.assertEqual(
                tailnet_service._resolve_oauth_client(settings),
                ("persisted-client", "persisted-secret", "tag:drone"),
            )

    def test_resolve_falls_back_to_env_vars_when_nothing_persisted(self) -> None:
        # No tag from the env-var fallback -- that path predates invites and
        # has no tag concept; create_invite_auth_key raises its own clear
        # error for this case rather than calling Tailscale with none.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(
                Path(tmp),
                DRONE_TAILSCALE_OAUTH_CLIENT_ID="env-client",
                DRONE_TAILSCALE_OAUTH_CLIENT_SECRET="env-secret",
            )
            self.assertEqual(tailnet_service._resolve_oauth_client(settings), ("env-client", "env-secret", ""))

    def test_resolve_empty_when_neither_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertEqual(tailnet_service._resolve_oauth_client(settings), ("", "", ""))


class CreateInviteAuthKeyTests(unittest.TestCase):
    def test_raises_when_no_oauth_client_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(RuntimeError):
                tailnet_service.create_invite_auth_key(settings)

    def test_mints_a_reusable_preauthorized_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            with mock.patch.object(tailnet_service, "_tailscale_oauth_access_token", return_value="tok-123") as token_call, \
                    mock.patch.object(
                        tailnet_service, "_tailscale_api_request",
                        return_value={"key": "tskey-auth-fresh", "expires": "2027-01-01T00:00:00Z"},
                    ) as api_call:
                result = tailnet_service.create_invite_auth_key(settings)
        token_call.assert_called_once_with("client-abc", "secret-xyz")
        self.assertEqual(result["key"], "tskey-auth-fresh")
        self.assertEqual(result["expires"], "2027-01-01T00:00:00Z")
        url, kwargs = api_call.call_args.args[0], api_call.call_args.kwargs
        self.assertTrue(url.endswith("/tailnet/-/keys"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-123")
        body = json.loads(kwargs["data"].decode("utf-8"))
        create = body["capabilities"]["devices"]["create"]
        self.assertTrue(create["reusable"])
        self.assertTrue(create["preauthorized"])
        self.assertFalse(create["ephemeral"])
        # Tailscale rejects an empty tags list for an OAuth-client-created
        # key ("tailnet-owned auth key must have tags set") -- the
        # configured tag must always be included, never left empty.
        self.assertEqual(create["tags"], ["tag:drone"])

    def test_missing_key_in_response_is_a_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            with mock.patch.object(tailnet_service, "_tailscale_oauth_access_token", return_value="tok-123"), \
                    mock.patch.object(tailnet_service, "_tailscale_api_request", return_value={}):
                with self.assertRaises(RuntimeError):
                    tailnet_service.create_invite_auth_key(settings)

    def test_tailscale_rejection_reason_is_surfaced_not_discarded(self) -> None:
        # A non-2xx from Tailscale's API used to escape as a bare
        # urllib.error.HTTPError (str() == "HTTP Error 400: Bad Request"),
        # caught by nothing here (every caller only catches RuntimeError) --
        # the actual reason Tailscale gave was silently thrown away. Covers
        # _tailscale_api_request's fix directly, at the real urlopen boundary.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            error = _http_error(400, {"message": "tags are required for keys created by an OAuth client"})
            with mock.patch.object(tailnet_service, "_tailscale_oauth_access_token", return_value="tok-123"), \
                    mock.patch.object(tailnet_service.urllib.request, "urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as ctx:
                    tailnet_service.create_invite_auth_key(settings)
        self.assertIn("tags are required", str(ctx.exception))
        self.assertNotIn("secret-xyz", str(ctx.exception))

    def test_expiry_seconds_has_a_five_minute_floor(self) -> None:
        # DRONE_TAILNET_INVITE_KEY_EXPIRY_SECONDS is read live at call time
        # (not snapshotted onto Settings), so the override must still be in
        # effect when create_invite_auth_key actually runs.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service.set_tailnet_oauth_client(settings, "client-abc", "secret-xyz", "tag:drone")
            with mock.patch.dict("os.environ", {"DRONE_TAILNET_INVITE_KEY_EXPIRY_SECONDS": "10"}), \
                    mock.patch.object(tailnet_service, "_tailscale_oauth_access_token", return_value="tok-123"), \
                    mock.patch.object(tailnet_service, "_tailscale_api_request", return_value={"key": "tskey-auth-fresh"}):
                result = tailnet_service.create_invite_auth_key(settings)
        self.assertEqual(result["expiry_seconds"], 300)


class OauthClientHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.settings = _build_settings(Path(self._tmp.name))
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = self.settings
        self.handler._send_json = mock.Mock()

    def test_status_handler_reports_not_configured(self) -> None:
        self.handler._handle_admin_tailnet_oauth_client_status()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertFalse(payload["configured"])

    def test_set_handler_persists(self) -> None:
        self.handler._handle_admin_tailnet_oauth_client_set(
            {"client_id": "client-abc", "client_secret": "secret-xyz", "tag": "tag:drone"}
        )
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["tag"], "tag:drone")

    def test_set_handler_raises_value_error_for_missing_fields(self) -> None:
        with self.assertRaises(ValueError):
            self.handler._handle_admin_tailnet_oauth_client_set({"client_id": "", "client_secret": "", "tag": ""})

    def test_clear_handler(self) -> None:
        tailnet_service.set_tailnet_oauth_client(self.settings, "client-abc", "secret-xyz", "tag:drone")
        self.handler._handle_admin_tailnet_oauth_client_clear()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertFalse(payload["configured"])

    def test_invite_handler_returns_the_key_on_success(self) -> None:
        with mock.patch.object(
            handlers_network, "_create_invite_auth_key",
            return_value={"key": "tskey-auth-fresh", "expires": "", "expiry_seconds": 3600},
        ):
            self.handler._handle_admin_tailnet_invite()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload["key"], "tskey-auth-fresh")

    def test_invite_handler_maps_runtime_error_to_502(self) -> None:
        with mock.patch.object(
            handlers_network, "_create_invite_auth_key",
            side_effect=RuntimeError("No Tailscale OAuth client is configured."),
        ):
            self.handler._handle_admin_tailnet_invite()
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertIn("OAuth client", payload["error"])


if __name__ == "__main__":
    unittest.main()
