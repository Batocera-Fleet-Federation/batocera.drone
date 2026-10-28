"""Issue #626: SMTP and Tailnet stale 404 revocation wipes secrets re-imported from another peer.

Issue text: "snapshot source_peer_id, then call get_paired_peer / _peer_get_json_for_peer
with no lock and no re-read. Importing smtp-b (password-b) or ts-b (tskey-auth-b) during
that call, then raising HTTP 404 for the original source, leaves source_peer_id on the
new peer but clears password/auth_key to '' and records a revocation."

Fix direction (same issue): "release for roster/HTTP I/O, re-read under the same lock,
and revoke only while source_peer_id still matches and the secret is still present."

These tests run the re-import on a real second thread while the revocation check is
blocked inside the roster read or the peer HTTP call, rather than re-entering on the
same thread as the sibling UAT does.
"""

from __future__ import annotations

import json
import tempfile
import threading
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
IMPORT_JOIN_SECONDS = 5


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


class _CrossThreadReimport:
    """Runs a re-import on its own thread and records what it raised."""

    def __init__(self, test: unittest.TestCase, action):
        self.test = test
        self.action = action
        self.errors: list[BaseException] = []

    def run_and_wait(self) -> None:
        def body():
            try:
                self.action()
            except BaseException as error:  # pragma: no cover - reported by assertions below
                self.errors.append(error)

        worker = threading.Thread(target=body, name="adversarial-626-reimport", daemon=True)
        worker.start()
        worker.join(IMPORT_JOIN_SECONDS)
        self.test.assertFalse(
            worker.is_alive(),
            "re-import from another peer must complete while the stale revocation check is doing peer I/O",
        )
        self.test.assertEqual(self.errors, [])


class CrossThreadReimportRevocationUAT(unittest.TestCase):
    def test_smtp_cross_thread_reimport_during_roster_read_is_not_wiped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-smtp-roster")
            smtp_manager.import_from_peer(settings, SMTP_A, source_peer_id="smtp-a", source_peer_name="A")
            reimport = _CrossThreadReimport(
                self, lambda: smtp_manager.import_from_peer(settings, SMTP_B, source_peer_id="smtp-b", source_peer_name="B"),
            )

            def roster_read_sees_peer_gone(_settings_arg, _peer_id):
                reimport.run_and_wait()
                return None  # smtp-a has left the roster while the re-import happens

            with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=roster_read_sees_peer_gone):
                smtp_manager.check_sharing_revocation(settings)

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "smtp-b")
            self.assertEqual(state["password"], "password-b")
            self.assertEqual(state["revoked_reason"], "")

    def test_smtp_cross_thread_reimport_during_404_http_call_is_not_wiped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-smtp-http")
            smtp_manager.import_from_peer(settings, SMTP_A, source_peer_id="smtp-a", source_peer_name="A")
            reimport = _CrossThreadReimport(
                self, lambda: smtp_manager.import_from_peer(settings, SMTP_B, source_peer_id="smtp-b", source_peer_name="B"),
            )

            def http_404_after_reimport(*_args, **_kwargs):
                reimport.run_and_wait()
                raise HTTPError("https://smtp-a/v1/api/peer/smtp/config", 404, "Not Found", None, None)

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "smtp-a"}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=http_404_after_reimport), \
                    mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True):
                smtp_manager.check_sharing_revocation(settings)

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "smtp-b")
            self.assertEqual(state["password"], "password-b")
            self.assertEqual(state["revoked_reason"], "")

    def test_tailnet_cross_thread_reimport_during_404_http_call_is_not_wiped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-tailnet-http")
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_tailscale_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-a", "enrolled": True}, source_peer_id="ts-a", source_peer_name="A",
                )
                reimport = _CrossThreadReimport(
                    self,
                    lambda: tailnet_service.import_tailnet_from_peer(
                        settings, {"auth_key": "tskey-auth-b", "enrolled": True}, source_peer_id="ts-b", source_peer_name="B",
                    ),
                )

                def http_404_after_reimport(*_args, **_kwargs):
                    reimport.run_and_wait()
                    raise HTTPError("https://ts-a/v1/api/peer/tailnet/config", 404, "Not Found", None, None)

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "ts-a"}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=http_404_after_reimport), \
                        mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True):
                    tailnet_service.check_tailnet_sharing_revocation(settings)

            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "ts-b")
            self.assertEqual(state["auth_key"], "tskey-auth-b")
            self.assertEqual(state["revoked_reason"], "")

    def test_tailnet_cross_thread_reimport_during_roster_read_is_not_wiped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-tailnet-roster")
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_tailscale_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-a", "enrolled": True}, source_peer_id="ts-a", source_peer_name="A",
                )
                reimport = _CrossThreadReimport(
                    self,
                    lambda: tailnet_service.import_tailnet_from_peer(
                        settings, {"auth_key": "tskey-auth-b", "enrolled": True}, source_peer_id="ts-b", source_peer_name="B",
                    ),
                )

                def roster_read_sees_peer_gone(_settings_arg, _peer_id):
                    reimport.run_and_wait()
                    return None

                with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=roster_read_sees_peer_gone):
                    tailnet_service.check_tailnet_sharing_revocation(settings)

            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["source_peer_id"], "ts-b")
            self.assertEqual(state["auth_key"], "tskey-auth-b")
            self.assertEqual(state["revoked_reason"], "")

    def test_control_smtp_404_from_unchanged_source_still_revokes(self) -> None:
        # Positive control: the fix must not disable revocation when provenance still matches.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-smtp-control")
            smtp_manager.import_from_peer(settings, SMTP_A, source_peer_id="smtp-a", source_peer_name="A")
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "smtp-a"}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                               side_effect=HTTPError("https://smtp-a/v1/api/peer/smtp/config", 404, "Not Found", None, None)), \
                    mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True):
                self.assertTrue(smtp_manager.check_sharing_revocation(settings))

            state = smtp_manager._load_state(settings)
            self.assertEqual(state["password"], "")
            self.assertNotEqual(state["revoked_reason"], "")

    def test_control_tailnet_404_from_unchanged_source_still_revokes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(Path(tmp), "adversarial-626-tailnet-control")
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", side_effect=_fake_tailscale_run):
                tailnet_service.import_tailnet_from_peer(
                    settings, {"auth_key": "tskey-auth-a", "enrolled": True}, source_peer_id="ts-a", source_peer_name="A",
                )
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "ts-a"}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                                   side_effect=HTTPError("https://ts-a/v1/api/peer/tailnet/config", 404, "Not Found", None, None)), \
                        mock.patch("app.transfer.peer_connectivity.peer_http_error_is_authenticated", return_value=True):
                    self.assertTrue(tailnet_service.check_tailnet_sharing_revocation(settings))

            state = tailnet_service._load_sharing_state(settings)
            self.assertEqual(state["auth_key"], "")
            self.assertNotEqual(state["revoked_reason"], "")


if __name__ == "__main__":
    unittest.main()
