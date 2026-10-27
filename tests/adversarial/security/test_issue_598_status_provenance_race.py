"""Status bookkeeping must not authorize an old peer to wipe a new import."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event, current_thread
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

from app.common.settings import Settings
from app.device import vpn_manager as vpn


class StatusProvenanceRaceSecurityTests(unittest.TestCase):
    def test_stale_status_write_cannot_restore_old_peers_revocation_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict("os.environ", {
                "USERDATA_ROOT": str(root),
                "ROMS_ROOT": str(root / "roms"),
                "BIOS_ROOT": str(root / "bios"),
                "SAVES_ROOT": str(root / "saves"),
                "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
                "DRONE_DEVICE_ID": "security-598-status",
                "DRONE_VPN_DIR": str(root / "vpn"),
            }, clear=True), mock.patch.object(vpn, "_drone_install_root", return_value=root), \
                    mock.patch.object(vpn, "find_openvpn_binary", return_value=None):
                settings = Settings.from_env()

                def import_peer(peer):
                    return vpn.import_from_peer(settings, {
                        "config_filename": "client.ovpn",
                        "config_text": f"client\ndev tun\nremote {peer}.example 1194\nauth-user-pass\n",
                        "has_credentials": True,
                        "username": peer,
                        "password": peer + "-secret",
                    }, source_peer_id=peer)

                import_peer("source-a")
                # An exited VPN process makes the regular status poll clear this
                # timestamp through _save_state (a full-state read/modify/write).
                vpn._save_state(settings, connected_at="2026-01-01T00:00:00+00:00",
                                last_status="disconnected")
                status_read = Event()
                release_status = Event()
                real_save_payload = vpn._save_state_payload

                def delayed_status_save(*args, **kwargs):
                    if current_thread().name.startswith("security-status"):
                        status_read.set()
                        if not release_status.wait(5):
                            raise AssertionError("status write was not released")
                    return real_save_payload(*args, **kwargs)

                callback_errors = []
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="security-status") as executor, \
                        ThreadPoolExecutor(max_workers=1, thread_name_prefix="security-import") as imports:
                    def old_peer_answer(*args, **kwargs):
                        poll = executor.submit(vpn.status, settings)
                        try:
                            self.assertTrue(status_read.wait(5), "status did not reach its state write")
                            replacement = imports.submit(import_peer, "source-b")
                            try:
                                replacement.result(timeout=0.5)
                            except TimeoutError:
                                # A corrected common state lock may serialize
                                # the import behind status; permit that order.
                                release_status.set()
                                replacement.result(timeout=5)
                            self.assertEqual(vpn._load_state(settings)["source_peer_id"], "source-b")
                        except Exception as error:
                            callback_errors.append(error)
                            raise
                        finally:
                            release_status.set()
                        try:
                            poll.result(timeout=5)
                        except Exception as error:
                            callback_errors.append(error)
                            raise
                        self.assertEqual(vpn.auth_path(settings).read_text(), "source-b\nsource-b-secret\n")
                        raise HTTPError("https://source-a/v1/api/peer/vpn/config", 404,
                                        "Not Found", None, None)

                    with mock.patch.object(vpn, "_save_state_payload", side_effect=delayed_status_save), \
                            mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": "source-a"}), \
                            mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=old_peer_answer), \
                            mock.patch.object(vpn, "disconnect") as disconnect:
                        revoked = vpn.check_sharing_revocation(settings)

                self.assertEqual(callback_errors, [], "the revocation helper must not hide fixture failures")
                self.assertTrue(vpn.auth_path(settings).is_file(),
                                "peer A's stale 404 deleted peer B's newly imported secret after a status write restored A's provenance")
                self.assertFalse(revoked)
                disconnect.assert_not_called()
                self.assertEqual(vpn._load_state(settings)["source_peer_id"], "source-b")


if __name__ == "__main__":
    unittest.main()
