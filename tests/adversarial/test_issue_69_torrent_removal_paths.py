"""Adversarial acceptance coverage for Issue #69 removal paths and races not
exercised by ``tests/adversarial/test_issue_69_torrent_lifecycle.py``.

Issue #69 reported three symptoms: (1) a torrent looks stuck/not-starting
and then suddenly shows far into downloading, (2) Move+delete moves files
but never deletes the row, (3) manually deleting a torrent re-adds itself.
The fix (see ``.claude/skills/drone-torrents-management/SKILL.md``, "Hidden
same-info-hash GIDs and terminal-removal tombstones") tombstones a torrent's
BitTorrent info-hash across every *terminal* removal call site -- delete(),
remove_from_list(), bulk clear(), and move+cleanup -- while intentionally
leaving cancel()/migrate_partial() GID-only, since those workflows keep (or
re-add) the same logical torrent rather than dropping it.

The existing adversarial suite for this issue only drives ``delete()`` and
move+cleanup through this machinery. This file independently exercises the
two other terminal-removal call sites (``remove_from_list()`` and bulk
``clear()``) against the same hidden-duplicate-GID race, verifies the
intentional negative space (``cancel()`` must NOT tombstone, and a
post-cancel hidden twin must still be reconciled rather than blocked), and
probes malformed/inconsistent aria2 responses the fix's reconciliation and
removal bookkeeping must tolerate without crashing or resurrecting a
terminally removed row.
"""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.aria2_runtime import Aria2RpcError
from app.transfer.torrent_manager import TorrentManager


INFO_HASH = "fedcba9876543210fedcba9876543210fedcba98"


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-69-removal-paths-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class FakeRpc:
    """Stateful aria2 double: a successful forceRemove actually drops the
    gid from the daemon's own inventory, so tests can assert on the full
    poll-to-poll convergence rather than merely "a method was called"."""

    def __init__(self) -> None:
        self.calls = []
        self.statuses = {}
        self.next_gid = 0
        self.add_error = None

    def call(self, method, params=None, timeout=None):
        del timeout
        params = list(params or [])
        self.calls.append((method, params))

        if method == "aria2.addTorrent":
            if self.add_error:
                raise self.add_error
            self.next_gid += 1
            gid = f"visible-{self.next_gid}"
            self.statuses[gid] = self._status(gid, status="paused")
            return gid
        if method == "aria2.tellStatus":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID {gid} is not found")
            return dict(self.statuses[gid])
        if method == "aria2.tellActive":
            return [dict(v) for v in self.statuses.values() if v.get("status") == "active"]
        if method == "aria2.tellWaiting":
            return [dict(v) for v in self.statuses.values() if v.get("status") in ("waiting", "paused")]
        if method == "aria2.unpause":
            gid = params[0]
            if gid in self.statuses:
                self.statuses[gid]["status"] = "active"
            return gid
        if method == "aria2.forceRemove":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue")
            del self.statuses[gid]
            return "OK"
        if method == "aria2.removeDownloadResult":
            return "OK"
        return "OK"

    def add_hidden(
        self,
        gid: str,
        *,
        completed="0",
        total="1000",
        status="active",
        info_hash=INFO_HASH,
        name="issue-69",
    ) -> None:
        self.statuses[gid] = self._status(
            gid, completed=completed, total=total, status=status, info_hash=info_hash, name=name
        )

    def removed_gids(self):
        return [params[0] for method, params in self.calls if method == "aria2.forceRemove"]

    @staticmethod
    def _status(gid, *, completed="0", total="0", status="paused", info_hash="", name="issue-69"):
        return {
            "gid": gid,
            "status": status,
            "totalLength": total,
            "completedLength": completed,
            "downloadSpeed": "19" if status == "active" else "0",
            "uploadSpeed": "0",
            "infoHash": info_hash,
            "bittorrent": {"info": {"name": name}},
            "files": [],
        }


class FakeDaemon:
    def __init__(self, rpc: FakeRpc) -> None:
        self.rpc = rpc
        self.running = True
        self.last_error = ""


def manager_with_visible_torrent(root: Path, rpc: FakeRpc, name: str = "issue-69"):
    manager = TorrentManager(build_settings(root), start_worker=False)
    manager._daemon = FakeDaemon(rpc)
    watch = root / "watch"
    manager.update_settings({"directory": str(watch)})
    watch.mkdir(parents=True, exist_ok=True)
    source = watch / f"{name}.torrent"
    source.write_bytes(b"d8:announce0:e")
    manager._tick()
    row = next(r for r in manager.snapshot()["torrents"] if r["name"] == name)
    with manager._lock:
        gid = manager._torrents[row["id"]]["gid"]
    return manager, row["id"], gid, source


def set_visible_state(manager, rpc, gid, *, completed, total="1000", status="active", info_hash=INFO_HASH):
    rpc.statuses[gid].update(
        {"status": status, "totalLength": total, "completedLength": completed, "infoHash": info_hash}
    )
    manager._tick()


class RemoveFromListHiddenDuplicateTests(unittest.TestCase):
    def test_remove_from_list_drains_hidden_same_hash_gid_and_stays_gone(self) -> None:
        """remove_from_list() is the "keep the files" terminal removal path
        (issue #51) -- the fix's tombstone must cover it exactly like
        delete(), or a hidden aria2 twin resurrects the row exactly as
        described in issue #69's third symptom."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, source = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="400")
            rpc.add_hidden("hidden-twin", completed="900", status="waiting", info_hash=INFO_HASH.upper())

            result = manager.remove_from_list(entry_id)
            self.assertEqual(result["status"], "removed")
            self.assertFalse(source.exists())

            manager._tick()
            manager._tick()

            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertNotIn("hidden-twin", rpc.statuses)
            self.assertNotIn(visible_gid, rpc.statuses)
            with manager._lock:
                self.assertEqual(manager._pending_removal_info_hashes, [])

    def test_remove_from_list_downloaded_payload_survives_hidden_twin_removal(self) -> None:
        """remove_from_list() promises to keep files on disk -- draining a
        hidden duplicate aria2 registration must not be conflated with
        deleting the payload; only the aria2 registrations and the queue
        row are removed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            download_dir = root / "watch" / "issue-69"
            download_dir.mkdir(parents=True)
            payload = download_dir / "payload.bin"
            payload.write_bytes(b"keep me")
            set_visible_state(manager, rpc, visible_gid, completed="7", total="7")
            rpc.add_hidden("hidden-twin", completed="7", total="7")

            manager.remove_from_list(entry_id)
            manager._tick()

            self.assertTrue(payload.exists())
            self.assertNotIn("hidden-twin", rpc.statuses)


class BulkClearHiddenDuplicateTests(unittest.TestCase):
    def test_bulk_clear_all_drains_each_targets_hidden_same_hash_gid(self) -> None:
        """Bulk clear (scope="all", delete_from_ui + delete_downloaded_files)
        walks every tracked row through the same terminal-removal helper as
        delete(). Each of two torrents here has its own independent hidden
        duplicate GID; neither must survive or resurrect its row."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            watch.mkdir(parents=True, exist_ok=True)
            (watch / "alpha.torrent").write_bytes(b"d8:announce0:e")
            (watch / "beta.torrent").write_bytes(b"d8:announce0:e")
            manager._tick()

            rows = {r["name"]: r["id"] for r in manager.snapshot()["torrents"]}
            self.assertEqual(set(rows), {"alpha", "beta"})
            alpha_hash = "aaaa000000000000000000000000000000000a"
            beta_hash = "bbbb000000000000000000000000000000000b"
            with manager._lock:
                alpha_gid = manager._torrents[rows["alpha"]]["gid"]
                beta_gid = manager._torrents[rows["beta"]]["gid"]
            set_visible_state(manager, rpc, alpha_gid, completed="10", info_hash=alpha_hash)
            set_visible_state(manager, rpc, beta_gid, completed="20", info_hash=beta_hash)
            rpc.add_hidden("alpha-hidden", completed="999", info_hash=alpha_hash.upper())
            rpc.add_hidden("beta-hidden", completed="999", info_hash=beta_hash.upper())

            result = manager.clear(
                {
                    "scope": "all",
                    "delete_from_ui": True,
                    "delete_torrent_file": True,
                    "delete_downloaded_files": True,
                }
            )
            self.assertEqual(result["status"], "ok")

            manager._tick()
            manager._tick()

            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertNotIn("alpha-hidden", rpc.statuses)
            self.assertNotIn("beta-hidden", rpc.statuses)
            with manager._lock:
                self.assertEqual(manager._pending_removal_info_hashes, [])

    def test_bulk_clear_completed_scope_leaves_incomplete_hidden_twin_untouched(self) -> None:
        """The "completed" scope must only tombstone rows that are actually
        complete -- an incomplete torrent's hidden twin (still legitimately
        downloading) must not be swept up just because it shares the clear
        call with a completed one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            watch.mkdir(parents=True, exist_ok=True)
            (watch / "done.torrent").write_bytes(b"d8:announce0:e")
            (watch / "active.torrent").write_bytes(b"d8:announce0:e")
            manager._tick()

            rows = {r["name"]: r["id"] for r in manager.snapshot()["torrents"]}
            done_hash = "cccc000000000000000000000000000000000c"
            active_hash = "dddd000000000000000000000000000000000d"
            with manager._lock:
                done_gid = manager._torrents[rows["done"]]["gid"]
                active_gid = manager._torrents[rows["active"]]["gid"]
            set_visible_state(manager, rpc, done_gid, completed="500", total="500", status="complete", info_hash=done_hash)
            set_visible_state(manager, rpc, active_gid, completed="50", total="500", status="active", info_hash=active_hash)
            rpc.add_hidden("active-hidden-twin", completed="200", info_hash=active_hash.upper(), name="active")

            manager.clear(
                {
                    "scope": "completed",
                    "delete_from_ui": True,
                    "delete_torrent_file": True,
                    "delete_downloaded_files": True,
                }
            )
            manager._tick()

            remaining = {r["name"]: r for r in manager.snapshot()["torrents"]}
            self.assertEqual(set(remaining), {"active"})
            # The still-tracked "active" row's hidden twin is reconciliation
            # work, not tombstoned removal -- it should still get merged in
            # (issue #69's first symptom), not silently dropped.
            self.assertIn("active-hidden-twin", rpc.statuses)


class CancelDoesNotTombstoneTests(unittest.TestCase):
    def test_cancel_does_not_register_an_info_hash_tombstone(self) -> None:
        """Cancel keeps the logical torrent (it requeues it) -- it must stay
        GID-only. If cancel ever starts tombstoning by info-hash the way
        terminal removal does, the very next add of the same torrent would
        have its own new GID pre-emptively drained by
        ``_retry_pending_removals`` before it can ever download."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="300")

            result = manager.cancel(entry_id)
            self.assertEqual(result["status"], "requeued")

            with manager._lock:
                self.assertEqual(manager._pending_removal_info_hashes, [])
                self.assertEqual(manager._torrents[entry_id]["status"], "queued")
                self.assertIsNone(manager._torrents[entry_id]["gid"])

    def test_hidden_twin_after_cancel_is_reconciled_not_blocked(self) -> None:
        """After a cancel, if aria2 still has a not-yet-drained registration
        for the same info-hash (the old gid's forceRemove hasn't been
        confirmed yet), the very next tick must fold it back into the
        requeued row via the normal reconciliation path rather than treating
        it as an orphan tombstoned by a terminal removal that never
        happened."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="300")
            with manager._lock:
                manager._torrents[entry_id]["info_hash"] = INFO_HASH

            manager.cancel(entry_id)
            # Simulate the old gid's forceRemove having failed to land yet
            # (still present in aria2) while a second, further-along
            # registration for the same content also exists.
            rpc.statuses.setdefault(visible_gid, rpc._status(visible_gid, status="waiting", info_hash=INFO_HASH))
            rpc.add_hidden("post-cancel-twin", completed="800", info_hash=INFO_HASH.upper())

            manager._tick()
            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], entry_id)
            with manager._lock:
                self.assertEqual(manager._pending_removal_info_hashes, [])


class MalformedAria2ResponseTests(unittest.TestCase):
    def test_hidden_gid_reported_in_both_active_and_waiting_does_not_crash_or_double_remove(self) -> None:
        """A daemon inconsistency (the same gid surfacing in both
        tellActive and tellWaiting during a status transition) must not
        crash reconciliation, and must not attempt to remove the same gid
        twice in a way that raises past the "not found" tolerance."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="10")
            rpc.add_hidden("dup-listed-twin", completed="999", status="active", info_hash=INFO_HASH.upper())

            original_call = rpc.call

            def call_with_duplicate_listing(method, params=None, timeout=None):
                if method == "aria2.tellWaiting":
                    active = original_call("aria2.tellActive", [], None)
                    waiting = original_call("aria2.tellWaiting", params, None)
                    return active + waiting
                return original_call(method, params, timeout)

            with mock.patch.object(rpc, "call", side_effect=call_with_duplicate_listing):
                manager._tick()
                manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], entry_id)

    def test_negative_completed_length_on_hidden_twin_is_not_treated_as_more_advanced(self) -> None:
        """Malformed/corrupt telemetry (a negative completedLength) must not
        let a hidden twin that reports it win reconciliation over a keeper
        with legitimate, non-negative progress."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="500")
            rpc.add_hidden("negative-twin", completed="-999", status="active", info_hash=INFO_HASH.upper())

            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual(len(rows), 1)
            with manager._lock:
                # The legitimate, non-negative keeper must be retained.
                self.assertEqual(manager._torrents[entry_id]["gid"], visible_gid)
            self.assertNotIn("negative-twin", rpc.statuses)


class TorrentFileAddRaceConvergenceTests(unittest.TestCase):
    def test_add_torrent_call_error_with_server_side_success_converges_to_one_row(self) -> None:
        """Issue #69 symptom 1, via a distinct trigger than the other
        reconciliation tests: the *initial* aria2.addTorrent call itself
        raises (a client-observed timeout) while aria2 has in fact already
        registered and progressed the download server-side. Unlike the
        other tests in this suite, the tracked entry here starts with NO
        known info_hash at all (the normal state for a freshly scanned
        watch-folder .torrent file before its first successful status
        query) -- exercising the harder case where reconciliation cannot
        key off a pre-populated info_hash on the tracked row. Whatever
        transiently happens on the first tick, the queue must not
        permanently fork into two rows for the same content, and the
        surviving row must end up reporting the real aria2 progress."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = FakeRpc()
            manager = TorrentManager(build_settings(root), start_worker=False)
            manager._daemon = FakeDaemon(rpc)
            watch = root / "watch"
            manager.update_settings({"directory": str(watch)})
            watch.mkdir(parents=True, exist_ok=True)
            (watch / "secret.torrent").write_bytes(b"d8:announce0:e")

            hidden_gid = "server-side-real-gid"
            rpc.add_hidden(hidden_gid, completed="900", total="1000", status="active")
            rpc.add_error = Aria2RpcError("timeout waiting for response")

            manager._tick()
            # Once the client "sees" the failure and retries, the daemon
            # now legitimately reports "already registered" for a repeat
            # add of the same content -- the ordinary self-heal path.
            rpc.add_error = Aria2RpcError(f"InfoHash {INFO_HASH} is already registered")
            with manager._lock:
                for entry in manager._torrents.values():
                    entry["retry_at"] = time.time() - 1

            manager._tick()
            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual(len(rows), 1, f"expected exactly one row, got {rows}")
            self.assertEqual(rows[0]["status"], "downloading")
            self.assertEqual(rows[0]["progress_percent"], 90.0)


if __name__ == "__main__":
    unittest.main()
