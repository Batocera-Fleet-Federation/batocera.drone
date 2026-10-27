"""UAT for #598: compare-and-wipe is one critical section; genuine revokes remain.

A replacement import that arrives after the source is confirmed gone/404 must
not slip between the provenance re-read and cleanup (disconnect, ``auth.txt``
unlink, state update). Once cleanup finishes, that import is the live config.

A replacement import that *fails* leaves the original source in place, so the
in-flight 404 is still a live revocation and must wipe. After a stale answer
is ignored, a later 404 from the *new* source must still revoke.
"""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest import mock
from urllib.error import HTTPError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SOURCE_A = "source-a"
SOURCE_B = "source-b"
OVPN_A = "client\ndev tun\nproto udp\nremote vpn-a.example.net 1194\nauth-user-pass\n"
OVPN_B = "client\ndev tun\nproto udp\nremote vpn-b.example.net 1194\nauth-user-pass\n"


def _payload(username: str, password: str, config_text: str, filename: str = "client.ovpn") -> dict:
    return {
        "config_filename": filename,
        "config_text": config_text,
        "has_credentials": True,
        "username": username,
        "password": password,
    }


def _http_404(peer_id: str) -> HTTPError:
    return HTTPError(f"https://{peer_id}/v1/api/peer/vpn/config", 404, "Not Found", None, None)


class Issue598RevocationAtomicityUAT(unittest.TestCase):
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
            "DRONE_DEVICE_ID": "adversarial-598-atomicity",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload("user-a", "pass-a", OVPN_A),
            source_peer_id=SOURCE_A, source_peer_name="Peer A",
        )
        return settings

    def test_import_started_during_disconnect_finishes_after_wipe(self) -> None:
        settings = self._settings()
        started = Event()
        finished = Event()
        payload = _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn")

        def replace():
            started.set()
            try:
                vpn_manager.import_from_peer(settings, payload, source_peer_id=SOURCE_B, source_peer_name="Peer B")
            finally:
                finished.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = []

            def disconnect_during_import(_settings):
                pending.append(executor.submit(replace))
                self.assertTrue(started.wait(timeout=5))
                self.assertFalse(
                    finished.wait(timeout=0.2),
                    "replacement import must wait until revocation finishes cleanup",
                )

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", side_effect=disconnect_during_import):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            pending[0].result(timeout=5)

        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], "")
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

    def test_import_started_during_state_update_finishes_after_wipe(self) -> None:
        # The lock must cover the state write, not only disconnect/unlink.
        settings = self._settings()
        started = Event()
        finished = Event()
        real_save = vpn_manager._save_state
        pending = []

        with ThreadPoolExecutor(max_workers=1) as executor:
            def replace():
                started.set()
                try:
                    vpn_manager.import_from_peer(
                        settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                        source_peer_id=SOURCE_B, source_peer_name="Peer B",
                    )
                finally:
                    finished.set()

            def save_and_contend(settings_arg, **updates):
                if updates.get("has_credentials") is False:
                    pending.append(executor.submit(replace))
                    self.assertTrue(started.wait(timeout=5))
                    self.assertFalse(
                        finished.wait(timeout=0.2),
                        "replacement import must not commit while revocation still holds the lock",
                    )
                return real_save(settings_arg, **updates)

            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                    mock.patch.object(vpn_manager, "_save_state", side_effect=save_and_contend):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            self.assertTrue(pending, "revocation must record has_credentials=False")
            pending[0].result(timeout=5)

        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

    def test_failed_replacement_import_does_not_block_genuine_404_wipe(self) -> None:
        settings = self._settings()

        def bad_import_then_404(*_args, **_kwargs):
            with self.assertRaises(ValueError):
                vpn_manager.import_from_peer(
                    settings, {"config_text": "   ", "has_credentials": True, "username": "x", "password": "y"},
                    source_peer_id=SOURCE_B,
                )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=bad_import_then_404):
            self.assertTrue(vpn_manager.check_sharing_revocation(settings))
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["source_peer_id"], SOURCE_A)
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)

    def test_later_404_from_new_source_still_revokes_after_stale_skip(self) -> None:
        settings = self._settings()

        def import_b_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404):
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_B}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=_http_404(SOURCE_B)):
            self.assertTrue(vpn_manager.check_sharing_revocation(settings))
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)
        self.assertTrue(vpn_manager.config_path(settings).is_file())

    def test_overlapping_checks_do_not_wipe_a_newer_import(self) -> None:
        # Check-1 is in HTTP; check-2 revokes source-a (peer gone); import of
        # source-b waits on that wipe; check-1's stale 404 must then no-op.
        settings = self._settings()
        check2_done = Event()
        imported = Event()

        def first_check_http(*_args, **_kwargs):
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            check2_done.set()
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            imported.set()
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=first_check_http), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        self.assertTrue(check2_done.is_set())
        self.assertTrue(imported.is_set())
        disconnect.assert_not_called()
        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], SOURCE_B)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), "user-b\npass-b\n")


if __name__ == "__main__":
    unittest.main()
