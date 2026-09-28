"""Adversarial acceptance coverage for Issue #77: a pending-removal
``.torrent`` tombstone must not collaterally delete a brand-new file that
reuses the same filename after the original was removed out-of-band.

Issue #74 tombstones a resolved watch-folder path when unlink fails, and
retries that unlink at the top of every ``_scan_watch_directory_locked``
tick -- *before* candidates are enumerated. Unlinking purely by path would
delete a legitimate replacement dropped under the same name, drop the
tombstone as if the original failure had resolved, and never register the
new torrent.

The required identity check (skill ``drone-torrents-management``):

* missing file → drop the tombstone
* identity mismatch, or no fingerprint (pre-#77 persisted state) → drop the
  tombstone **without** unlinking, so the scan can register whatever is now
  at that path
* identity matches → unlink; keep the tombstone only if that still raises
  ``OSError``

Do not fall back to an unconditional ``Path.unlink(missing_ok=True)`` when
identity is unknown. Identity is the tombstoned ``(st_dev, st_ino, st_size,
st_mtime_ns)``, not the filename and not the payload bytes.

Related #74 invariant retained here: when the file at the path is still the
tombstoned identity, retry must unlink it and must not resurrect the row.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage.state_store import database_path, load_payload, save_payload
from app.transfer.aria2_runtime import Aria2RpcError
from app.transfer.torrent_manager import TORRENT_STATE_NAMESPACE, TorrentManager


NEW_PAYLOAD = b"d8:announce0:4:infod4:name11:replaced.ee"
OTHER_PAYLOAD = b"d8:announce0:4:infod4:name8:other.ee"


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-77-pending-removal-replacement-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    """Stateful aria2 double so ticks after delete()/clear() reconcile
    like the real poll loop instead of merely recording method names."""

    def __init__(self) -> None:
        self.calls = []
        self.statuses = {}
        self.next_gid = 0

    def call(self, method, params=None, timeout=None):
        del timeout
        params = list(params or [])
        self.calls.append((method, params))

        if method in ("aria2.addTorrent", "aria2.addUri"):
            self.next_gid += 1
            gid = f"gid-{self.next_gid}"
            self.statuses[gid] = self._status(gid, status="paused")
            return gid
        if method == "aria2.tellStatus":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID {gid} is not found")
            return dict(self.statuses[gid])
        if method in ("aria2.tellActive", "aria2.tellWaiting"):
            return [
                dict(status)
                for status in self.statuses.values()
                if status["status"] in ("active", "paused")
            ]
        if method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            gid = params[0] if params else None
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue.")
            del self.statuses[gid]
            return "OK"
        return "OK"

    @staticmethod
    def _status(gid, status):
        return {
            "gid": gid,
            "status": status,
            "totalLength": "0",
            "completedLength": "0",
            "files": [],
        }


class FakeDaemon:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.binary_path = "/fake/aria2c"
        self.last_error = ""
        self.bind_interface = None
        self.stopped = False

    @property
    def running(self):
        return not self.stopped

    def stop(self):
        self.stopped = True


def _write_torrent(directory: Path, name: str, payload: bytes = b"d8:announce0:e") -> Path:
    path = directory / f"{name}.torrent"
    path.write_bytes(payload)
    return path


def _failing_unlink_for(torrent_path: Path):
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self.resolve() == torrent_path.resolve():
            raise OSError("Permission denied")
        return real_unlink(self, *args, **kwargs)

    return failing_unlink


def _tracking_unlink(calls: list):
    real_unlink = Path.unlink

    def tracking_unlink(self, *args, **kwargs):
        try:
            resolved = str(self.resolve())
        except OSError:
            resolved = str(self)
        calls.append(resolved)
        return real_unlink(self, *args, **kwargs)

    return tracking_unlink


class PendingRemovalReplacementTests(unittest.TestCase):
    def _make_manager(self, root: Path, rpc: FakeRpc | None = None) -> tuple:
        manager = TorrentManager(build_settings(root), start_worker=False)
        manager._daemon = FakeDaemon(rpc or FakeRpc())
        watch = root / "watch"
        manager.update_settings({"directory": str(watch), "max_concurrent_downloads": 4})
        return manager, watch

    def _register(self, manager: TorrentManager, watch: Path, name: str, payload: bytes = b"d8:announce0:e") -> tuple:
        torrent_path = _write_torrent(watch, name, payload)
        manager._tick()
        with manager._lock:
            entry_id, entry = next(
                (eid, e) for eid, e in manager._torrents.items() if Path(e["torrent_file"]).resolve() == torrent_path.resolve()
            )
        return entry_id, torrent_path, entry

    def _snapshot_names(self, manager: TorrentManager) -> list:
        return [e["name"] for e in manager.snapshot()["torrents"]]

    def _tombstone_via_delete(self, manager: TorrentManager, entry_id: str, torrent_path: Path) -> None:
        with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
            result = manager.delete(entry_id)
            self.assertEqual(result["status"], "deleted")
            self.assertFalse(result["torrent_file_removed"])
        self.assertTrue(torrent_path.exists())
        with manager._lock:
            self.assertIn(str(torrent_path.resolve()), manager._pending_removal_torrent_files)

    def _reload(self, root: Path, watch: Path) -> TorrentManager:
        reloaded = TorrentManager(build_settings(root), start_worker=False)
        reloaded._daemon = FakeDaemon(FakeRpc())
        reloaded.update_settings({"directory": str(watch), "max_concurrent_downloads": 4})
        return reloaded

    def _persist_state(self, root: Path, mutate) -> None:
        db = database_path(root)
        stored = load_payload(db, TORRENT_STATE_NAMESPACE, {})
        self.assertIsInstance(stored, dict)
        mutate(stored)
        save_payload(db, TORRENT_STATE_NAMESPACE, stored)

    def test_delete_tombstone_does_not_unlink_out_of_band_replacement(self) -> None:
        # Canonical reproduction: watch-folder torrent, tick, delete() with
        # Path.unlink forced to OSError, then out-of-band unlink + drop a
        # new file at the identical path before the next tick.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "solo")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)
            unlinked = []
            with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            self.assertNotIn(str(torrent_path.resolve()), unlinked)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["solo"])

    def test_same_bytes_replacement_is_still_a_new_file(self) -> None:
        # Identity is inode/mtime, not payload: rewriting an identical copy
        # at the reused filename must survive the retry unlink.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "clone")
            original = torrent_path.read_bytes()
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.unlink()
            torrent_path.write_bytes(original)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), original)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["clone"])

    def test_os_replace_of_another_torrent_onto_tombstoned_path_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "target")
            incoming = _write_torrent(watch, "incoming-src", NEW_PAYLOAD)
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.unlink()
            os.replace(incoming, torrent_path)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            self.assertFalse(incoming.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["target"])

    def test_in_place_overwrite_is_treated_as_identity_mismatch(self) -> None:
        # Truncating/rewriting the bytes at the same path (typically same
        # inode, new size/mtime) is still a different identity.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "inplace")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.write_bytes(NEW_PAYLOAD)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["inplace"])

    def test_mtime_change_alone_drops_tombstone_without_unlinking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "touched")
            original = torrent_path.read_bytes()
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            os.utime(torrent_path, ns=(0, 1_000_000_000))
            unlinked = []
            with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), original)
            self.assertNotIn(str(torrent_path.resolve()), unlinked)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["touched"])

    def test_missing_original_drops_tombstone_and_does_not_invent_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "gone")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.unlink()
            manager._tick()

            self.assertFalse(torrent_path.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), [])

    def test_matching_identity_still_unlinks_and_does_not_resurrect(self) -> None:
        # Issue #74 must still hold when the file at the path is the one
        # that was tombstoned: retry unlinks it and the scan must not
        # re-add a queued row.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "original")
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            manager._tick()

            self.assertFalse(torrent_path.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), [])

    def test_matching_identity_keeps_tombstone_when_retry_unlink_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "locked")
            self._tombstone_via_delete(manager, entry_id, torrent_path)
            resolved = str(torrent_path.resolve())
            with manager._lock:
                fingerprint = manager._pending_removal_torrent_file_fingerprints[resolved]
            self.assertIsNotNone(fingerprint)

            with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
                manager._tick()

            self.assertTrue(torrent_path.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [resolved])
                self.assertEqual(
                    manager._pending_removal_torrent_file_fingerprints.get(resolved),
                    fingerprint,
                )
            self.assertEqual(self._snapshot_names(manager), [])

    def test_mixed_tombstones_unlink_original_and_keep_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            keep_id, keep_path, _ = self._register(manager, watch, "keep-me")
            drop_id, drop_path, _ = self._register(manager, watch, "replace-me")
            neighbor = _write_torrent(watch, "neighbor", OTHER_PAYLOAD)
            manager._tick()
            self.assertIn("neighbor", self._snapshot_names(manager))

            self._tombstone_via_delete(manager, keep_id, keep_path)
            self._tombstone_via_delete(manager, drop_id, drop_path)

            drop_path.unlink()
            drop_path.write_bytes(NEW_PAYLOAD)
            manager._tick()

            self.assertFalse(keep_path.exists())
            self.assertTrue(drop_path.exists())
            self.assertEqual(drop_path.read_bytes(), NEW_PAYLOAD)
            self.assertTrue(neighbor.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            names = self._snapshot_names(manager)
            self.assertIn("replace-me", names)
            self.assertIn("neighbor", names)
            self.assertNotIn("keep-me", names)

    def test_remove_from_list_tombstone_does_not_delete_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "unlist")
            with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
                result = manager.remove_from_list(entry_id)
                self.assertEqual(result["status"], "removed")
                self.assertFalse(result["torrent_file_removed"])

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["unlist"])

    def test_clear_all_tombstone_does_not_delete_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            _, torrent_path, _ = self._register(manager, watch, "bulk")
            with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
                result = manager.clear({"delete_from_ui": True, "scope": "all"})
                self.assertEqual(result["status"], "ok")

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["bulk"])

    def test_move_cleanup_tombstone_does_not_delete_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "moved")
            payload = watch / "moved.bin"
            payload.write_bytes(b"payload")
            with manager._lock:
                gid = manager._torrents[entry_id]["gid"]
                manager._torrents[entry_id]["files"] = [str(payload)]
            rpc = manager._daemon.rpc
            rpc.statuses[gid].update(
                {
                    "status": "complete",
                    "totalLength": "7",
                    "completedLength": "7",
                    "files": [{"path": str(payload)}],
                }
            )
            manager._tick()
            by_name = {e["name"]: e for e in manager.snapshot()["torrents"]}
            self.assertEqual(by_name["moved"]["status"], "complete")

            destination = root / "roms" / "moved-out"
            queued = manager.move_files(entry_id, [str(payload)], str(destination), cleanup=True)
            self.assertEqual(queued["status"], "queued")
            with mock.patch.object(Path, "unlink", _failing_unlink_for(torrent_path)):
                manager._move_tick()

            self.assertTrue(torrent_path.exists())
            with manager._lock:
                self.assertIn(str(torrent_path.resolve()), manager._pending_removal_torrent_files)

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)
            manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            self.assertTrue((destination / payload.name).is_file())
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["moved"])

    def test_fingerprint_survives_reload_and_still_skips_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "reloaded")
            resolved = str(torrent_path.resolve())
            self._tombstone_via_delete(manager, entry_id, torrent_path)
            with manager._lock:
                fingerprint = manager._pending_removal_torrent_file_fingerprints[resolved]
            self.assertIsNotNone(fingerprint)

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)

            reloaded = self._reload(root, watch)
            with reloaded._lock:
                self.assertEqual(reloaded._pending_removal_torrent_files, [resolved])
                self.assertEqual(
                    reloaded._pending_removal_torrent_file_fingerprints.get(resolved),
                    fingerprint,
                )

            reloaded._tick()
            self.assertTrue(torrent_path.exists())
            self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
            with reloaded._lock:
                self.assertEqual(reloaded._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(reloaded), ["reloaded"])

    def test_pre77_persisted_path_without_fingerprint_does_not_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "legacy")
            resolved = str(torrent_path.resolve())
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            self._persist_state(
                root,
                lambda stored: stored.pop("pending_removal_torrent_file_fingerprints", None),
            )
            reloaded = self._reload(root, watch)
            with reloaded._lock:
                self.assertEqual(reloaded._pending_removal_torrent_files, [resolved])
                self.assertIsNone(reloaded._pending_removal_torrent_file_fingerprints.get(resolved))

            unlinked = []
            with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                reloaded._tick()

            self.assertTrue(torrent_path.exists())
            self.assertNotIn(resolved, unlinked)
            with reloaded._lock:
                self.assertEqual(reloaded._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(reloaded), ["legacy"])

    def test_unknown_fingerprint_none_does_not_unlink_even_if_original_still_there(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "unknown")
            resolved = str(torrent_path.resolve())
            self._tombstone_via_delete(manager, entry_id, torrent_path)
            with manager._lock:
                manager._pending_removal_torrent_file_fingerprints[resolved] = None
                manager._persist_locked()

            unlinked = []
            with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                manager._tick()

            self.assertTrue(torrent_path.exists())
            self.assertNotIn(resolved, unlinked)
            with manager._lock:
                self.assertEqual(manager._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(manager), ["unknown"])

    def test_malformed_fingerprint_payloads_refuse_to_unlink(self) -> None:
        # Persisted fingerprints may be a dict, contain non-dicts, bools
        # (JSON has no bool/int distinction in some codecs / Python json
        # will decode true as bool), strings, or partial tuples. Unknown
        # identity must skip unlink rather than delete whatever is there.
        malformed_cases = [
            lambda _path, _fp: {"not": "a-list"},
            lambda path, fp: [
                "skip-me",
                None,
                12,
                {"path": ""},
                {"path": "   "},
                {"path": path, "dev": True, "ino": fp[1], "size": fp[2], "mtime_ns": fp[3]},
                {"path": path, "dev": fp[0], "ino": "123", "size": fp[2], "mtime_ns": fp[3]},
                {"path": path, "dev": fp[0], "ino": fp[1], "size": 1.5, "mtime_ns": fp[3]},
                {"path": path, "dev": fp[0], "ino": fp[1]},
            ],
        ]
        for index, builder in enumerate(malformed_cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manager, watch = self._make_manager(root)
                entry_id, torrent_path, _ = self._register(manager, watch, f"bad-{index}")
                resolved = str(torrent_path.resolve())
                self._tombstone_via_delete(manager, entry_id, torrent_path)
                with manager._lock:
                    fingerprint = manager._pending_removal_torrent_file_fingerprints[resolved]
                self.assertIsNotNone(fingerprint)

                torrent_path.unlink()
                torrent_path.write_bytes(NEW_PAYLOAD)

                def mutate(stored, _builder=builder, _resolved=resolved, _fp=fingerprint):
                    stored["pending_removal_torrent_file_fingerprints"] = _builder(_resolved, _fp)

                self._persist_state(root, mutate)
                reloaded = self._reload(root, watch)
                unlinked = []
                with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                    reloaded._tick()

                self.assertTrue(torrent_path.exists(), msg=f"case {index} deleted the replacement")
                self.assertEqual(torrent_path.read_bytes(), NEW_PAYLOAD)
                self.assertNotIn(resolved, unlinked)
                with reloaded._lock:
                    self.assertEqual(reloaded._pending_removal_torrent_files, [])
                self.assertEqual(self._snapshot_names(reloaded), [f"bad-{index}"])

    def test_integer_float_fingerprint_fields_still_match_original_identity(self) -> None:
        # JSON may round-trip integers as *.0 floats. Those must still be
        # treated as the original identity so retry unlinks the tombstoned
        # file rather than resurrecting it.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "floats")
            resolved = str(torrent_path.resolve())
            self._tombstone_via_delete(manager, entry_id, torrent_path)
            with manager._lock:
                fingerprint = manager._pending_removal_torrent_file_fingerprints[resolved]
            self.assertIsNotNone(fingerprint)

            def json_number(value: int):
                as_float = float(value)
                # IEEE floats cannot round-trip inode/mtime_ns when they
                # exceed 2**53; keep those as ints so this test isolates
                # the integer-float coercion path rather than precision
                # loss.
                if as_float.is_integer() and int(as_float) == value:
                    return as_float
                return value

            def mutate(stored):
                stored["pending_removal_torrent_file_fingerprints"] = [
                    {
                        "path": resolved,
                        "dev": json_number(fingerprint[0]),
                        "ino": json_number(fingerprint[1]),
                        "size": json_number(fingerprint[2]),
                        "mtime_ns": json_number(fingerprint[3]),
                    }
                ]

            self._persist_state(root, mutate)
            raw = load_payload(database_path(root), TORRENT_STATE_NAMESPACE, {})
            dumped = raw["pending_removal_torrent_file_fingerprints"][0]
            self.assertIsInstance(dumped["size"], float)

            reloaded = self._reload(root, watch)
            with reloaded._lock:
                loaded = reloaded._pending_removal_torrent_file_fingerprints.get(resolved)
            self.assertEqual(loaded, fingerprint)

            reloaded._tick()
            self.assertFalse(torrent_path.exists())
            with reloaded._lock:
                self.assertEqual(reloaded._pending_removal_torrent_files, [])
            self.assertEqual(self._snapshot_names(reloaded), [])

    def test_fingerprint_for_unrelated_path_does_not_cause_unlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, watch = self._make_manager(root)
            entry_id, torrent_path, _ = self._register(manager, watch, "extra")
            resolved = str(torrent_path.resolve())
            decoy = watch / "decoy.torrent"
            decoy.write_bytes(OTHER_PAYLOAD)
            self._tombstone_via_delete(manager, entry_id, torrent_path)

            torrent_path.unlink()
            torrent_path.write_bytes(NEW_PAYLOAD)

            def mutate(stored):
                stored["pending_removal_torrent_file_fingerprints"] = [
                    {
                        "path": str(decoy.resolve()),
                        "dev": 1,
                        "ino": 1,
                        "size": 1,
                        "mtime_ns": 1,
                    }
                ]

            self._persist_state(root, mutate)
            reloaded = self._reload(root, watch)
            unlinked = []
            with mock.patch.object(Path, "unlink", _tracking_unlink(unlinked)):
                reloaded._tick()

            self.assertTrue(torrent_path.exists())
            self.assertTrue(decoy.exists())
            self.assertNotIn(str(decoy.resolve()), unlinked)
            self.assertNotIn(resolved, unlinked)
            names = self._snapshot_names(reloaded)
            self.assertIn("extra", names)
            self.assertIn("decoy", names)


if __name__ == "__main__":
    unittest.main()
