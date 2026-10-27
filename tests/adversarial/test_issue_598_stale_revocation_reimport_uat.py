"""UAT for #598: a stale sharing-revocation answer must not wipe a replacement.

Expected behaviour, derived from the issue and the VPN sharing invariants
before treating the current implementation as truth:

- ``check_sharing_revocation`` may snapshot provenance, then talk to the
  roster / source peer with no lock held (network I/O must not block an
  import).
- Only two answers are revocation: the snapshotted source is no longer
  paired, or that source returns HTTP 404. Anything else changes nothing.
- Before acting on either answer, re-read live state. Revoke only while
  ``source_peer_id`` still matches the snapshot AND ``has_credentials`` is
  still true.
- A stale answer returns False and must not disconnect, delete ``auth.txt``,
  or write ``revoked_reason`` / ``revoked_at``.
- A genuine 404 from the peer that currently owns the credentials still
  wipes them, including a same-source refresh during the in-flight call.
"""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SOURCE_A = "source-a"
SOURCE_B = "source-b"
OVPN_A = "client\ndev tun\nproto udp\nremote vpn-a.example.net 1194\nauth-user-pass\n"
OVPN_B = "client\ndev tun\nproto udp\nremote vpn-b.example.net 1194\nauth-user-pass\n"


def _payload(username: str, password: str, config_text: str = OVPN_A, filename: str = "client.ovpn") -> dict:
    return {
        "config_filename": filename,
        "config_text": config_text,
        "has_credentials": True,
        "username": username,
        "password": password,
    }


def _http_404(peer_id: str = SOURCE_A) -> HTTPError:
    return HTTPError(f"https://{peer_id}/v1/api/peer/vpn/config", 404, "Not Found", None, None)


class Issue598StaleRevocationReimportUAT(unittest.TestCase):
    def _settings(self) -> Settings:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        install.start()
        self.addCleanup(install.stop)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-598-stale-reimport",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload("user-a", "pass-a", OVPN_A),
            source_peer_id=SOURCE_A, source_peer_name="Peer A",
        )
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-a\npass-a\n")
        return settings

    def _assert_replacement_intact(self, settings: Settings, *, source_peer_id: str, username: str, password: str) -> None:
        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], source_peer_id)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["username"], username)
        self.assertEqual(state["revoked_reason"], "")
        self.assertIsNone(state["revoked_at"])
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), f"{username}\n{password}\n")
        self.assertTrue(vpn_manager.config_path(settings).is_file())

    def test_stale_404_does_not_wipe_credentials_reimported_from_another_peer(self) -> None:
        # Original #598 reproduction: same thread as the in-flight peer GET
        # imports source-b, then source-a's delayed 404 arrives.
        settings = self._settings()

        def import_b_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement_intact(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")
        self.assertIn("vpn-b.example.net", vpn_manager.config_path(settings).read_text())
        self.assertEqual(vpn_manager._load_state(settings)["source_peer_name"], "Peer B")
        self.assertIsNone(vpn_manager.export_payload(settings))

    def test_stale_peer_gone_does_not_wipe_credentials_reimported_from_another_peer(self) -> None:
        settings = self._settings()

        def import_b_then_gone(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            return None

        with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=import_b_then_gone), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement_intact(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

    def test_stale_404_does_not_wipe_manual_self_owned_replacement(self) -> None:
        settings = self._settings()

        def upload_then_404(*_args, **_kwargs):
            vpn_manager.save_uploaded_config(settings, "mine.ovpn", OVPN_B.encode())
            vpn_manager.save_credentials(settings, "local-user", "local-pass")
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=upload_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement_intact(settings, source_peer_id="", username="local-user", password="local-pass")
        sharing = vpn_manager.set_sharing_enabled(settings, True)
        self.assertTrue(sharing["sharing_enabled"])

    def test_network_io_does_not_hold_the_credentials_lock(self) -> None:
        # If the peer GET held the credentials lock, the replacement import
        # could not finish until the 404 returned — and then the wipe would
        # still see source-a.
        settings = self._settings()
        in_flight = Event()
        imported = Event()

        def slow_404(*_args, **_kwargs):
            in_flight.set()
            self.assertTrue(imported.wait(timeout=5), "import must complete during the peer HTTP call")
            raise _http_404(SOURCE_A)

        def replace():
            self.assertTrue(in_flight.wait(timeout=5))
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            imported.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(replace)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=slow_404), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            pending.result(timeout=5)
            disconnect.assert_not_called()
        self._assert_replacement_intact(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

    def test_same_source_404_still_wipes_credentials_refreshed_during_the_call(self) -> None:
        # Provenance still matches the snapshot: a 404 from the current owner
        # is a live revocation, even if that owner was re-pulled while the
        # check was in flight.
        settings = self._settings()

        def refresh_a_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-a2", "pass-a2", OVPN_A),
                source_peer_id=SOURCE_A, source_peer_name="Peer A",
            )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=refresh_a_then_404):
            self.assertTrue(vpn_manager.check_sharing_revocation(settings))
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["source_peer_id"], SOURCE_A)
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)

    def test_urlerror_and_non_404_with_concurrent_import_leave_replacement(self) -> None:
        for label, error in (
            ("timeout", URLError("timed out")),
            ("http-500", HTTPError("https://source-a/v1/api/peer/vpn/config", 500, "boom", None, None)),
            ("http-403", HTTPError("https://source-a/v1/api/peer/vpn/config", 403, "forbidden", None, None)),
            ("http-410", HTTPError("https://source-a/v1/api/peer/vpn/config", 410, "gone", None, None)),
        ):
            with self.subTest(error=label):
                settings = self._settings()

                def import_b_then_error(*_args, **_kwargs):
                    vpn_manager.import_from_peer(
                        settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                        source_peer_id=SOURCE_B, source_peer_name="Peer B",
                    )
                    raise error

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_error), \
                        mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                disconnect.assert_not_called()
                self._assert_replacement_intact(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")


if __name__ == "__main__":
    unittest.main()
