"""UAT for #598: provenance identity boundaries around stale revocation.

Expected behaviour, taken from the issue and the VPN sharing invariants
before treating the current implementation as truth:

- A delayed 404 / peer-gone answer authorizes a wipe only while live
  ``source_peer_id`` still equals the snapshot taken before roster/HTTP I/O
  *and* ``has_credentials`` is still true.
- Equality is exact. A prefix, suffix, case variant, or lookalike id is a
  different owner, so the delayed answer is stale.
- Roster lookup and peer HTTP must not hold the credentials lock; a
  replacement import has to be able to finish while those calls are in
  flight.
- The in-flight GET is aimed at the snapshotted source, never the live one.
- A same-source credential refresh during the call is still a live
  revocation: provenance matches.
- A second overlapping 404 after the first wipe has already cleared
  credentials must not disconnect again or rewrite ``revoked_reason``.
- Only urllib ``HTTPError.code == 404`` (integer) is a 404 answer; a
  string/bool lookalike is transient and must not wipe.
- After a replacement import from another peer, single-hop sharing stays
  closed. A stale skip must not call ``_revoke_local_credentials``.
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


def _payload(username: str, password: str, config_text: str = OVPN_A, filename: str = "client.ovpn",
             has_credentials: bool = True) -> dict:
    return {
        "config_filename": filename,
        "config_text": config_text,
        "has_credentials": has_credentials,
        "username": username,
        "password": password,
    }


def _http_404(peer_id: str = SOURCE_A) -> HTTPError:
    return HTTPError(f"https://{peer_id}/v1/api/peer/vpn/config", 404, "Not Found", None, None)


class Issue598ProvenanceIdentityBoundariesUAT(unittest.TestCase):
    def _settings(self) -> Settings:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self._root = root
        install = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install")
        install.start()
        self.addCleanup(install.stop)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-598-provenance-identity",
        }, clear=True):
            settings = Settings.from_env()
        vpn_manager.import_from_peer(
            settings, _payload("user-a", "pass-a", OVPN_A),
            source_peer_id=SOURCE_A, source_peer_name="Peer A",
        )
        return settings

    def _assert_replacement(self, settings: Settings, *, source_peer_id: str, username: str, password: str) -> None:
        state = vpn_manager._load_state(settings)
        self.assertEqual(state["source_peer_id"], source_peer_id)
        self.assertTrue(state["has_credentials"])
        self.assertEqual(state["username"], username)
        self.assertEqual(state["revoked_reason"], "")
        self.assertIsNone(state["revoked_at"])
        self.assertEqual(vpn_manager.auth_path(settings).read_text(), f"{username}\n{password}\n")

    def test_near_match_peer_ids_are_not_the_snapshotted_source(self) -> None:
        for other_id in ("source-a2", "source", "source-a-extra", "Source-A", "source-а"):
            with self.subTest(other_id=other_id):
                settings = self._settings()

                def import_other_then_404(*_args, **_kwargs):
                    vpn_manager.import_from_peer(
                        settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                        source_peer_id=other_id, source_peer_name="Other",
                    )
                    raise _http_404(SOURCE_A)

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                                   side_effect=import_other_then_404), \
                        mock.patch.object(vpn_manager, "_revoke_local_credentials") as revoke:
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                revoke.assert_not_called()
                self._assert_replacement(settings, source_peer_id=other_id, username="user-b", password="pass-b")
                self.assertIn("vpn-b.example.net", vpn_manager.config_path(settings).read_text())

    def test_config_only_reimport_keeps_leftover_secrets_on_stale_404(self) -> None:
        # B's payload has no credentials. Provenance moves to B; auth.txt is
        # still the leftover A secret. A's delayed 404 must not wipe it,
        # because live source_peer_id no longer matches the snapshot.
        settings = self._settings()

        def import_b_config_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings,
                _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn", has_credentials=False),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                           side_effect=import_b_config_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-a", password="pass-a")
        self.assertEqual(vpn_manager._load_state(settings)["source_peer_name"], "Peer B")

    def test_folder_import_during_404_is_self_owned_and_not_wiped(self) -> None:
        settings = self._settings()
        drop = self._root / "vpn-import"
        drop.mkdir()
        (drop / "local.ovpn").write_text(OVPN_B, encoding="utf-8")

        def folder_then_404(*_args, **_kwargs):
            vpn_manager.import_from_folder(settings, "local.ovpn")
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=folder_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement(settings, source_peer_id="", username="user-a", password="pass-a")
        sharing = vpn_manager.set_sharing_enabled(settings, True)
        self.assertTrue(sharing["sharing_enabled"])

    def test_same_source_credential_refresh_during_404_is_still_revoked(self) -> None:
        # Positive control: changing the password without changing provenance
        # is not a replacement owner. The 404 is still live.
        settings = self._settings()

        def refresh_then_404(*_args, **_kwargs):
            vpn_manager.save_credentials(settings, "user-a2", "pass-a2")
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=refresh_then_404):
            self.assertTrue(vpn_manager.check_sharing_revocation(settings))
        self.assertFalse(vpn_manager.auth_path(settings).exists())
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["source_peer_id"], SOURCE_A)
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)

    def test_second_overlapping_404_after_wipe_does_not_disconnect_again(self) -> None:
        settings = self._settings()
        inner_done = Event()

        def delayed_404_after_inner_wipe(*_args, **_kwargs):
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=_http_404(SOURCE_A)):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            inner_done.set()
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                           side_effect=delayed_404_after_inner_wipe), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}) as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        self.assertTrue(inner_done.is_set())
        self.assertEqual(disconnect.call_count, 1)
        state = vpn_manager._load_state(settings)
        self.assertFalse(state["has_credentials"])
        self.assertEqual(state["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)
        first_revoked_at = state["revoked_at"]
        self.assertIsNotNone(first_revoked_at)
        self.assertFalse(vpn_manager.auth_path(settings).exists())

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=_http_404(SOURCE_A)), \
                mock.patch.object(vpn_manager, "disconnect") as later_disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        later_disconnect.assert_not_called()
        later = vpn_manager._load_state(settings)
        self.assertEqual(later["revoked_at"], first_revoked_at)
        self.assertEqual(later["revoked_reason"], vpn_manager._SHARING_REVOKED_PEER_OFF)

    def test_roster_lookup_does_not_hold_the_credentials_lock(self) -> None:
        settings = self._settings()
        in_lookup = Event()
        imported = Event()

        def slow_gone(*_args, **_kwargs):
            in_lookup.set()
            self.assertTrue(imported.wait(timeout=5), "import must complete during roster lookup")
            return None

        def replace():
            self.assertTrue(in_lookup.wait(timeout=5))
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            imported.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(replace)
            with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=slow_gone), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            pending.result(timeout=5)
            disconnect.assert_not_called()
        self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

    def test_peer_get_and_roster_lookup_use_the_snapshot_id(self) -> None:
        settings = self._settings()
        seen_peer_ids: list[str] = []
        seen_get_ids: list[object] = []

        def import_b_then_404(peer, path, settings_arg, peer_id=None, **_kwargs):
            seen_get_ids.append(peer_id)
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id=SOURCE_B, source_peer_name="Peer B",
            )
            raise _http_404(SOURCE_A)

        def record_roster(_settings, peer_id):
            seen_peer_ids.append(peer_id)
            return {"drone_id": SOURCE_A}

        with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=record_roster), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=import_b_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self.assertEqual(seen_peer_ids, [SOURCE_A])
        self.assertEqual(seen_get_ids, [SOURCE_A])
        self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

    def test_malformed_http_404_lookalikes_do_not_revoke(self) -> None:
        for label, code in (("str", "404"), ("true", True), ("none", None), ("bytes", b"404")):
            with self.subTest(code=label):
                settings = self._settings()
                error = HTTPError("https://source-a/v1/api/peer/vpn/config", 500, "not found", None, None)
                error.code = code

                def import_b_then_lookalike(*_args, **_kwargs):
                    vpn_manager.import_from_peer(
                        settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                        source_peer_id=SOURCE_B, source_peer_name="Peer B",
                    )
                    raise error

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                                   side_effect=import_b_then_lookalike), \
                        mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                disconnect.assert_not_called()
                self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

        # Provenance still matches: a non-integer lookalike is still not a 404.
        settings = self._settings()
        error = HTTPError("https://source-a/v1/api/peer/vpn/config", 500, "not found", None, None)
        error.code = "404"
        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement(settings, source_peer_id=SOURCE_A, username="user-a", password="pass-a")

    def test_imported_replacement_still_cannot_be_reshared_after_stale_skip(self) -> None:
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
        with self.assertRaises(ValueError):
            vpn_manager.set_sharing_enabled(settings, True)
        self.assertIsNone(vpn_manager.export_payload(settings))
        self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")

    def test_whitespace_padded_replacement_id_is_a_distinct_owner(self) -> None:
        settings = self._settings()

        def import_padded_then_404(*_args, **_kwargs):
            vpn_manager.import_from_peer(
                settings, _payload("user-b", "pass-b", OVPN_B, "from-b.ovpn"),
                source_peer_id="  source-b  ", source_peer_name="  Peer B  ",
            )
            raise _http_404(SOURCE_A)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value={"drone_id": SOURCE_A}), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer",
                           side_effect=import_padded_then_404), \
                mock.patch.object(vpn_manager, "disconnect") as disconnect:
            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
        disconnect.assert_not_called()
        self._assert_replacement(settings, source_peer_id=SOURCE_B, username="user-b", password="pass-b")
        self.assertEqual(vpn_manager._load_state(settings)["source_peer_name"], "Peer B")


if __name__ == "__main__":
    unittest.main()
