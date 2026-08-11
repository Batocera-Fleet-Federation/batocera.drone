"""Tailscale auth-key peer sharing: single-hop provenance, share-
revocation (never touches the live tailnet connection), and swarm
bootstrap. Mirrors test_vpn_manager.py's sharing test shapes closely --
see app/device/tailnet_service.py's "peer sharing" section and the
drone-vpn-management skill (which this feature's design deliberately
mirrors) for the shared rationale.

Enrollment shells out to the real tailscale CLI (_run_cli -> subprocess.run
-> TAILSCALE_CLI); every test here mocks that boundary rather than the
higher-level tailnet_enroll() itself, so the actual persist-and-provenance
side effect inside tailnet_enroll() -- which sharing depends on entirely --
is exercised for real, not assumed.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings


def _build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "tailnet-sharing-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
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
    """Replaces the module-level TAILSCALE_CLI Path entirely (rather than
    patching .exists on the real Path instance, which pathlib forbids --
    Path uses __slots__) with a MagicMock that reports installed=True and
    stringifies to a plausible path for the (also mocked) subprocess.run
    call to receive as its argv[0]."""
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _fake_subprocess_run(cli_args, **_kwargs):
    """Routes a mocked subprocess.run call by its CLI args -- "status
    --json" (used by both _start_daemon_if_needed's daemon-liveness check
    and by tailnet_status()) returns a fake enrolled snapshot; "up
    --authkey=..." (the actual enrollment) and "logout" (rotation's
    disconnect step) both just succeed with no output."""
    if "status" in cli_args and "--json" in cli_args:
        return _status_json_result()
    return mock.Mock(returncode=0, stdout="", stderr="")


def _enroll(settings: Settings, auth_key: str = "tskey-auth-fake-reusable-key") -> dict:
    """A real tailnet_enroll() call with only the tailscale-CLI boundary
    mocked out."""
    with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
            mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
        return tailnet_service.tailnet_enroll(auth_key, settings)


class SharingEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            self.assertFalse(tailnet_service.tailnet_sharing_status(settings)["sharing_enabled"])

    def test_set_sharing_enabled_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings)
            result = tailnet_service.set_tailnet_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])
            self.assertTrue(tailnet_service.tailnet_sharing_status(settings)["sharing_enabled"])


class ExportTailnetPayloadTests(unittest.TestCase):
    def test_none_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings)
            self.assertIsNone(tailnet_service.export_tailnet_payload(settings))

    def test_none_when_never_enrolled_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            tailnet_service._save_sharing_state(settings, sharing_enabled=True)  # bypass the normal setter on purpose
            self.assertIsNone(tailnet_service.export_tailnet_payload(settings))

    def test_returns_auth_key_and_enrolled_when_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings, auth_key="tskey-auth-reusable-abc")
            tailnet_service.set_tailnet_sharing_enabled(settings, True)
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                payload = tailnet_service.export_tailnet_payload(settings)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["auth_key"], "tskey-auth-reusable-abc")
            self.assertTrue(payload["enrolled"])

    def test_never_returns_a_raw_key_via_status(self) -> None:
        # has_shared_key is a bool, never the key itself -- mirrors
        # vpn_manager.status() never returning the raw password.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings, auth_key="tskey-auth-super-secret")
            status = tailnet_service.tailnet_sharing_status(settings)
            self.assertTrue(status["has_shared_key"])
            self.assertNotIn("auth_key", status)
            self.assertNotIn("tskey-auth-super-secret", json.dumps(status))


class ImportTailnetFromPeerTests(unittest.TestCase):
    def test_rejects_missing_auth_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                tailnet_service.import_tailnet_from_peer(settings, {}, source_peer_id="peer-1")

    def test_rejects_missing_source_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                tailnet_service.import_tailnet_from_peer(settings, {"auth_key": "tskey-auth-x"}, source_peer_id="")

    def test_enrolls_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                result = tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-shared", "enrolled": True},
                    source_peer_id="peer-1", source_peer_name="Peer One",
                )
            self.assertTrue(result.get("enrolled"))
            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["source_peer_name"], "Peer One")
            self.assertEqual(state["auth_key"], "tskey-auth-shared")

    def test_fresh_enroll_after_import_clears_provenance(self) -> None:
        # A genuine fresh paste is always self-owned -- this is how a drone
        # goes from "imported, can't share" back to being a real source.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-shared", "enrolled": True}, source_peer_id="peer-1",
                )
                self.assertEqual(tailnet_service._load_sharing_state(settings)["source_peer_id"], "peer-1")
                tailnet_service.tailnet_enroll("tskey-auth-my-own", settings)
            self.assertEqual(tailnet_service._load_sharing_state(settings)["source_peer_id"], "")


class SharingProvenanceGateTests(unittest.TestCase):
    def _imported_settings(self, tmp: str) -> Settings:
        settings = _build_settings(Path(tmp))
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
            tailnet_service.import_tailnet_from_peer(
                settings, {"auth_key": "tskey-auth-shared", "enrolled": True},
                source_peer_id="peer-1", source_peer_name="Peer One",
            )
        return settings

    def test_set_sharing_enabled_rejected_for_imported_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with self.assertRaises(ValueError):
                tailnet_service.set_tailnet_sharing_enabled(settings, True)
            self.assertFalse(tailnet_service._load_sharing_state(settings)["sharing_enabled"])

    def test_disabling_sharing_is_always_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            result = tailnet_service.set_tailnet_sharing_enabled(settings, False)
            self.assertFalse(result["sharing_enabled"])

    def test_export_payload_none_for_imported_key_even_if_sharing_somehow_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            tailnet_service._save_sharing_state(settings, sharing_enabled=True)  # bypass the normal setter
            self.assertIsNone(tailnet_service.export_tailnet_payload(settings))

    def test_self_enrolled_key_can_be_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings)
            result = tailnet_service.set_tailnet_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])


class CheckSharingRevocationTests(unittest.TestCase):
    def _imported_settings(self, tmp: str) -> Settings:
        settings = _build_settings(Path(tmp))
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
            tailnet_service.import_tailnet_from_peer(
                settings, {"auth_key": "tskey-auth-shared", "enrolled": True},
                source_peer_id="peer-1", source_peer_name="Peer One",
            )
        return settings

    def test_noop_when_key_is_self_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            _enroll(settings)
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_noop_when_no_key_to_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_revokes_when_source_peer_no_longer_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None):
                self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(settings))
            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["auth_key"], "")
            self.assertFalse(state["sharing_enabled"])
            self.assertIn("no longer paired", state["revoked_reason"])
            self.assertEqual(state["source_peer_id"], "peer-1")  # provenance persists

    def test_revokes_when_peer_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/tailnet/config", 404, "not found", None, None)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(settings))
            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["auth_key"], "")
            self.assertIn("turned off sharing", state["revoked_reason"])
            self.assertIsNotNone(state["revoked_at"])
            self.assertEqual(state["source_peer_id"], "peer-1")

    def test_does_not_revoke_on_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=OSError("unreachable")):
                self.assertFalse(tailnet_service.check_tailnet_sharing_revocation(settings))
            state = tailnet_service._load_sharing_state(settings)
            self.assertNotEqual(state["auth_key"], "")
            self.assertEqual(state["revoked_reason"], "")

    def test_revocation_never_touches_the_live_tailnet_connection(self) -> None:
        # The one deliberate deviation from vpn_manager's revocation: this
        # must never call `tailscale logout` (or any CLI command at all) --
        # this drone may depend on its own tailnet membership for unrelated
        # P2P networking regardless of whether the shared key is revoked.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(tailnet_service.subprocess, "run") as run_mock:
                self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(settings))
            run_mock.assert_not_called()


class BootstrapTailnetFromSwarmTests(unittest.TestCase):
    def test_adopts_the_first_peer_reporting_enrolled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peers = [{"drone_id": "peer-1", "name": "Peer One"}]
            with mock.patch("app.transfer.local_network.paired_peers", return_value=peers), \
                    mock.patch(
                        "app.transfer.peer_connectivity._peer_get_json_for_peer",
                        return_value=({"auth_key": "tskey-auth-shared", "enrolled": True}, "peer-1"),
                    ), \
                    mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_subprocess_run):
                self.assertTrue(tailnet_service.bootstrap_tailnet_from_swarm(settings))
            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")

    def test_skips_a_peer_that_is_sharing_but_not_actually_enrolled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            peers = [{"drone_id": "peer-1", "name": "Peer One"}]
            with mock.patch("app.transfer.local_network.paired_peers", return_value=peers), \
                    mock.patch(
                        "app.transfer.peer_connectivity._peer_get_json_for_peer",
                        return_value=({"auth_key": "tskey-auth-shared", "enrolled": False}, "peer-1"),
                    ):
                self.assertFalse(tailnet_service.bootstrap_tailnet_from_swarm(settings))

    def test_no_paired_peers_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[]):
                self.assertFalse(tailnet_service.bootstrap_tailnet_from_swarm(settings))


if __name__ == "__main__":
    unittest.main()
