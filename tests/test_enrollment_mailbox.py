"""Enrollment-mailbox config, single-hop peer sharing (mirrors
test_tailnet_sharing.py's shapes closely), and the GitHub-Issues
notify/check logic that ties tailnet_service.tailnet_enroll_interactive()
to a private repo. See app/device/enrollment_mailbox.py's module docstring
for the feature's design.

The GitHub REST API boundary (_github_request) and the Tailscale
interactive-enroll boundary (tailnet_enroll_interactive) are both mocked
directly -- neither makes a real network/subprocess call in this file.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.enrollment_mailbox as enrollment_mailbox
from app.drone_api import Settings


def _build_settings(root: Path, device_id: str = "mailbox-test") -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": device_id,
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _configure(settings: Settings, repo: str = "acct/mailbox-repo", token: str = "ghp_fake_token") -> dict:
    return enrollment_mailbox.update_settings(settings, {"github_repo": repo, "github_token": token})


class UpdateSettingsTests(unittest.TestCase):
    def test_requires_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                enrollment_mailbox.update_settings(settings, {"github_token": "ghp_x"})

    def test_rejects_malformed_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                enrollment_mailbox.update_settings(settings, {"github_repo": "not-a-repo", "github_token": "ghp_x"})

    def test_requires_token_on_first_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo"})

    def test_token_optional_on_update_keeps_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings, token="ghp_original")
            result = enrollment_mailbox.update_settings(settings, {"github_repo": "acct/repo"})
            self.assertTrue(result["has_token"])
            self.assertEqual(enrollment_mailbox._load_state(settings)["github_token"], "ghp_original")

    def test_get_settings_never_returns_raw_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings, token="ghp_super_secret")
            status = enrollment_mailbox.get_settings(settings)
            self.assertTrue(status["has_token"])
            self.assertNotIn("github_token", status)
            self.assertNotIn("ghp_super_secret", json.dumps(status))

    def test_changing_repo_resets_tracked_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            enrollment_mailbox._save_state(settings, tracked_issue_number=7, tracked_issue_url="https://github.com/acct/mailbox-repo/issues/7")
            _configure(settings, repo="acct/other-repo")
            state = enrollment_mailbox._load_state(settings)
            self.assertIsNone(state["tracked_issue_number"])
            self.assertEqual(state["tracked_issue_url"], "")


class SharingEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(enrollment_mailbox.get_settings(settings)["sharing_enabled"])

    def test_set_sharing_enabled_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            result = enrollment_mailbox.set_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])
            self.assertTrue(enrollment_mailbox.get_settings(settings)["sharing_enabled"])


class ExportPayloadTests(unittest.TestCase):
    def test_none_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            self.assertIsNone(enrollment_mailbox.export_payload(settings))

    def test_none_when_never_configured_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox._save_state(settings, sharing_enabled=True)  # bypass the normal setter on purpose
            self.assertIsNone(enrollment_mailbox.export_payload(settings))

    def test_returns_repo_and_token_when_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings, repo="acct/repo", token="ghp_shared_token")
            enrollment_mailbox.set_sharing_enabled(settings, True)
            payload = enrollment_mailbox.export_payload(settings)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["github_repo"], "acct/repo")
            self.assertEqual(payload["github_token"], "ghp_shared_token")


class ImportFromPeerTests(unittest.TestCase):
    def test_rejects_missing_source_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                enrollment_mailbox.import_from_peer(settings, {"github_repo": "acct/repo", "github_token": "ghp_x"}, source_peer_id="")

    def test_adopts_config_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.import_from_peer(
                settings, {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"},
                source_peer_id="peer-1", source_peer_name="Peer One",
            )
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["source_peer_name"], "Peer One")
            self.assertEqual(state["github_repo"], "acct/shared-repo")
            self.assertEqual(state["github_token"], "ghp_shared")

    def test_fresh_save_after_import_clears_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            enrollment_mailbox.import_from_peer(
                settings, {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"}, source_peer_id="peer-1",
            )
            self.assertEqual(enrollment_mailbox._load_state(settings)["source_peer_id"], "peer-1")
            _configure(settings, repo="acct/my-own-repo", token="ghp_my_own")
            self.assertEqual(enrollment_mailbox._load_state(settings)["source_peer_id"], "")


class SharingProvenanceGateTests(unittest.TestCase):
    def _imported_settings(self, tmp: str) -> Settings:
        settings = _build_settings(Path(tmp))
        enrollment_mailbox.import_from_peer(
            settings, {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"},
            source_peer_id="peer-1", source_peer_name="Peer One",
        )
        return settings

    def test_set_sharing_enabled_rejected_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with self.assertRaises(ValueError):
                enrollment_mailbox.set_sharing_enabled(settings, True)

    def test_disabling_sharing_is_always_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            result = enrollment_mailbox.set_sharing_enabled(settings, False)
            self.assertFalse(result["sharing_enabled"])

    def test_export_payload_none_for_imported_config_even_if_sharing_somehow_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            enrollment_mailbox._save_state(settings, sharing_enabled=True)  # bypass the normal setter
            self.assertIsNone(enrollment_mailbox.export_payload(settings))

    def test_self_owned_config_can_be_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            result = enrollment_mailbox.set_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])


class CheckSharingRevocationTests(unittest.TestCase):
    def _imported_settings(self, tmp: str) -> Settings:
        settings = _build_settings(Path(tmp))
        enrollment_mailbox.import_from_peer(
            settings, {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"},
            source_peer_id="peer-1", source_peer_name="Peer One",
        )
        return settings

    def test_noop_when_config_is_self_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(enrollment_mailbox.check_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_noop_when_never_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(enrollment_mailbox.check_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_revokes_when_source_peer_no_longer_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None):
                self.assertTrue(enrollment_mailbox.check_sharing_revocation(settings))
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["github_token"], "")
            self.assertIn("no longer paired", state["revoked_reason"])
            self.assertEqual(state["source_peer_id"], "peer-1")  # provenance persists

    def test_revokes_when_peer_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/mailbox/config", 404, "not found", None, None)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertTrue(enrollment_mailbox.check_sharing_revocation(settings))
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["github_token"], "")
            self.assertIn("turned off sharing", state["revoked_reason"])

    def test_does_not_revoke_on_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=OSError("unreachable")):
                self.assertFalse(enrollment_mailbox.check_sharing_revocation(settings))
            state = enrollment_mailbox._load_state(settings)
            self.assertNotEqual(state["github_token"], "")


class BootstrapFromSwarmTests(unittest.TestCase):
    def test_adopts_the_first_peer_sharing_a_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peers = [{"drone_id": "peer-1", "name": "Peer One"}]
            payload = {"github_repo": "acct/shared-repo", "github_token": "ghp_shared"}
            with mock.patch("app.transfer.local_network.paired_peers", return_value=peers), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=(payload, "peer-1")):
                self.assertTrue(enrollment_mailbox.bootstrap_mailbox_from_swarm(settings))
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")

    def test_skips_a_peer_with_no_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peers = [{"drone_id": "peer-1", "name": "Peer One"}]
            with mock.patch("app.transfer.local_network.paired_peers", return_value=peers), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=({}, "peer-1")):
                self.assertFalse(enrollment_mailbox.bootstrap_mailbox_from_swarm(settings))

    def test_no_paired_peers_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[]):
                self.assertFalse(enrollment_mailbox.bootstrap_mailbox_from_swarm(settings))


def _fake_issue(number: int = 42, state: str = "open") -> dict:
    return {"number": number, "state": state, "html_url": f"https://github.com/acct/mailbox-repo/issues/{number}"}


class CheckAndNotifyIfNeededTests(unittest.TestCase):
    def test_skipped_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "skipped")

    def test_already_enrolled_closes_tracked_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            enrollment_mailbox._save_state(settings, tracked_issue_number=42, tracked_issue_url="https://github.com/acct/mailbox-repo/issues/42")
            github_calls = []

            def fake_github_request(token, method, path, **kwargs):
                github_calls.append((method, path))
                return {}

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": True}), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "already_enrolled")
            self.assertIn(("PATCH", "/repos/acct/mailbox-repo/issues/42"), github_calls)
            state = enrollment_mailbox._load_state(settings)
            self.assertIsNone(state["tracked_issue_number"])

    def test_not_enrolled_no_existing_issue_creates_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)

            def fake_github_request(token, method, path, params=None, json_body=None):
                if method == "GET":
                    return []  # no existing open issue with this device's label
                if method == "POST" and path.endswith("/issues"):
                    return _fake_issue()
                raise AssertionError(f"unexpected call: {method} {path}")

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": False}), \
                    mock.patch("app.device.tailnet_service.tailnet_enroll_interactive", return_value="https://login.tailscale.com/a/abc123"), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "notified")
            self.assertEqual(result["issue_number"], 42)
            state = enrollment_mailbox._load_state(settings)
            self.assertEqual(state["tracked_issue_number"], 42)
            self.assertEqual(state["last_login_url"], "https://login.tailscale.com/a/abc123")

    def test_not_enrolled_with_open_tracked_issue_does_not_recreate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            enrollment_mailbox._save_state(settings, tracked_issue_number=42)
            enroll_interactive = mock.Mock()

            def fake_github_request(token, method, path, **kwargs):
                self.assertEqual((method, path), ("GET", "/repos/acct/mailbox-repo/issues/42"))
                return _fake_issue(state="open")

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": False}), \
                    mock.patch("app.device.tailnet_service.tailnet_enroll_interactive", enroll_interactive), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "already_pending")
            enroll_interactive.assert_not_called()

    def test_manually_closed_issue_gets_a_fresh_one(self) -> None:
        # A user closing the issue (to ask for a refreshed link) must result
        # in a brand new issue being created on the next check, not silence.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)
            enrollment_mailbox._save_state(settings, tracked_issue_number=42)

            def fake_github_request(token, method, path, params=None, json_body=None):
                if method == "GET" and path.endswith("/issues/42"):
                    return _fake_issue(number=42, state="closed")
                if method == "GET" and path.endswith("/issues"):
                    return []
                if method == "POST" and path.endswith("/issues"):
                    return _fake_issue(number=99)
                raise AssertionError(f"unexpected call: {method} {path}")

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": False}), \
                    mock.patch("app.device.tailnet_service.tailnet_enroll_interactive", return_value="https://login.tailscale.com/a/fresh"), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "notified")
            self.assertEqual(result["issue_number"], 99)

    def test_interactive_enroll_failure_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)

            def fake_github_request(token, method, path, **kwargs):
                return []  # label search finds nothing

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": False}), \
                    mock.patch("app.device.tailnet_service.tailnet_enroll_interactive", side_effect=RuntimeError("no url printed")), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "error")
            self.assertIn("tailscale", result["error"].lower())

    def test_github_create_failure_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _configure(settings)

            def fake_github_request(token, method, path, params=None, json_body=None):
                if method == "GET":
                    return []
                raise HTTPError("https://api.github.com/repos/acct/mailbox-repo/issues", 403, "forbidden", None, None)

            with mock.patch("app.device.tailnet_service.tailnet_status", return_value={"enrolled": False}), \
                    mock.patch("app.device.tailnet_service.tailnet_enroll_interactive", return_value="https://login.tailscale.com/a/abc"), \
                    mock.patch.object(enrollment_mailbox, "_github_request", side_effect=fake_github_request):
                result = enrollment_mailbox.check_and_notify_if_needed(settings)
            self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
