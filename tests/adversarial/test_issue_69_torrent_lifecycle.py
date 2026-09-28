"""Adversarial acceptance coverage for Issue #69 torrent lifecycle races.

These tests intentionally use an independent, stateful aria2 double.  A
successful forceRemove changes the daemon inventory, which lets the tests
exercise complete poll-to-poll behavior instead of merely checking that an
RPC method was called once.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.transfer.aria2_runtime import Aria2RpcError
from app.transfer.torrent_manager import TorrentManager


INFO_HASH = "0123456789abcdef0123456789abcdef01234567"


def build_settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-69-adversarial",
        "LOG_DIR": str(root / "logs"),
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class StatefulAria2:
    """Small aria2 model whose queue inventory reflects removals."""

    def __init__(self) -> None:
        self.calls = []
        self.statuses = {}
        self.next_gid = 0
        self.inventory_failures = 0

    def call(self, method, params=None, timeout=None):
        del timeout
        params = params or []
        self.calls.append((method, params))

        if method == "aria2.addTorrent":
            self.next_gid += 1
            gid = f"visible-{self.next_gid}"
            self.statuses[gid] = self._status(gid, status="paused")
            return gid
        if method == "aria2.tellStatus":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID {gid} is not found")
            return dict(self.statuses[gid])
        if method in ("aria2.tellActive", "aria2.tellWaiting"):
            if self.inventory_failures:
                self.inventory_failures -= 1
                raise Aria2RpcError("temporary inventory timeout")
            if method == "aria2.tellActive":
                accepted = {"active"}
            else:
                accepted = {"waiting", "paused"}
            return [dict(value) for value in self.statuses.values() if value.get("status") in accepted]
        if method == "aria2.unpause":
            gid = params[0]
            if gid in self.statuses:
                self.statuses[gid]["status"] = "active"
            return gid
        if method == "aria2.forceRemove":
            gid = params[0]
            if gid not in self.statuses:
                raise Aria2RpcError(f"GID#{gid} is not found in the queue")
            self.statuses.pop(gid)
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
            gid,
            completed=completed,
            total=total,
            status=status,
            info_hash=info_hash,
            name=name,
        )

    def removed_gids(self):
        return [params[0] for method, params in self.calls if method == "aria2.forceRemove"]

    @staticmethod
    def _status(
        gid: str,
        *,
        completed="0",
        total="0",
        status="paused",
        info_hash="",
        name="issue-69",
    ):
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
    def __init__(self, rpc: StatefulAria2) -> None:
        self.rpc = rpc
        self.running = True
        self.last_error = ""


def manager_with_visible_torrent(root: Path, rpc: StatefulAria2):
    manager = TorrentManager(build_settings(root), start_worker=False)
    manager._daemon = FakeDaemon(rpc)
    watch = root / "watch"
    manager.update_settings({"directory": str(watch)})
    source = watch / "issue-69.torrent"
    source.write_bytes(b"d8:announce0:e")
    manager._tick()
    row = manager.snapshot()["torrents"][0]
    with manager._lock:
        gid = manager._torrents[row["id"]]["gid"]
    return manager, row["id"], gid, source


def set_visible_state(
    manager: TorrentManager,
    rpc: StatefulAria2,
    gid: str,
    *,
    completed: str,
    total: str = "1000",
    status: str = "active",
    files=None,
) -> None:
    rpc.statuses[gid].update(
        {
            "status": status,
            "totalLength": total,
            "completedLength": completed,
            "infoHash": INFO_HASH.upper(),
            "files": [{"path": str(path)} for path in (files or [])],
        }
    )
    manager._tick()


class Issue69TorrentLifecycleAcceptanceTests(unittest.TestCase):
    def test_poll_keeps_only_the_most_advanced_of_multiple_same_hash_gids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = StatefulAria2()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            rpc.statuses[visible_gid].update(
                {
                    "status": "paused",
                    "totalLength": "1000",
                    "completedLength": "100",
                    "infoHash": INFO_HASH.upper(),
                }
            )
            rpc.add_hidden("hidden-best", completed="730", info_hash=INFO_HASH.lower())
            rpc.add_hidden(
                "hidden-lesser",
                completed="420",
                status="waiting",
                info_hash=INFO_HASH.upper(),
            )

            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], entry_id)
            self.assertEqual(rows[0]["status"], "downloading")
            self.assertEqual(rows[0]["progress_percent"], 73.0)
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["gid"], "hidden-best")
            self.assertCountEqual(rpc.removed_gids(), [visible_gid, "hidden-lesser"])
            self.assertEqual(set(rpc.statuses), {"hidden-best"})

            manager._tick()
            self.assertEqual([row["id"] for row in manager.snapshot()["torrents"]], [entry_id])

    def test_malformed_duplicate_progress_does_not_crash_or_leave_a_hidden_twin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = StatefulAria2()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="500")
            rpc.add_hidden("malformed-twin", completed="not-a-number")

            # Invalid progress is not evidence that the unknown registration
            # is ahead. Reconciliation must remain live and drain that twin.
            manager._tick()

            rows = manager.snapshot()["torrents"]
            self.assertEqual([row["id"] for row in rows], [entry_id])
            with manager._lock:
                self.assertEqual(manager._torrents[entry_id]["gid"], visible_gid)
            self.assertNotIn("malformed-twin", rpc.statuses)

    def test_manual_delete_is_terminal_for_incomplete_and_complete_torrents(self) -> None:
        for completed, status in (("350", "active"), ("1000", "complete")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rpc = StatefulAria2()
                manager, entry_id, visible_gid, source = manager_with_visible_torrent(root, rpc)
                set_visible_state(
                    manager,
                    rpc,
                    visible_gid,
                    completed=completed,
                    status=status,
                )
                rpc.add_hidden(
                    f"hidden-{status}",
                    completed=completed,
                    status="waiting",
                    info_hash=INFO_HASH.upper(),
                )

                result = manager.delete(entry_id)
                self.assertEqual(result["status"], "deleted")
                self.assertFalse(source.exists())
                manager._tick()
                manager._tick()

                self.assertEqual(manager.snapshot()["torrents"], [])
                self.assertEqual(rpc.statuses, {})
                with manager._lock:
                    self.assertEqual(manager._pending_removal_info_hashes, [])

    def test_delete_tombstone_survives_inventory_errors_and_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = StatefulAria2()
            manager, entry_id, visible_gid, _ = manager_with_visible_torrent(root, rpc)
            set_visible_state(manager, rpc, visible_gid, completed="250")
            rpc.add_hidden(
                "waiting-after-delete",
                completed="600",
                status="waiting",
                info_hash=INFO_HASH.upper(),
            )

            manager.delete(entry_id)
            rpc.inventory_failures = 2
            manager._tick()
            self.assertEqual(manager.snapshot()["torrents"], [])
            with manager._lock:
                self.assertEqual(manager._pending_removal_info_hashes, [INFO_HASH])

            restarted = TorrentManager(build_settings(root), start_worker=False)
            restarted._daemon = FakeDaemon(rpc)
            with restarted._lock:
                self.assertEqual(restarted._pending_removal_info_hashes, [INFO_HASH])

            restarted._tick()
            self.assertNotIn("waiting-after-delete", rpc.statuses)
            self.assertEqual(restarted.snapshot()["torrents"], [])
            # The first successful inventory removes the match; a later,
            # independent empty inventory is required to release the guard.
            with restarted._lock:
                self.assertEqual(restarted._pending_removal_info_hashes, [INFO_HASH])
            restarted._tick()
            with restarted._lock:
                self.assertEqual(restarted._pending_removal_info_hashes, [])

    def test_successful_move_cleanup_removes_hidden_registration_and_source_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rpc = StatefulAria2()
            manager, entry_id, visible_gid, source = manager_with_visible_torrent(root, rpc)
            payload_dir = root / "watch" / "issue-69"
            payload_dir.mkdir()
            payload = payload_dir / "payload.bin"
            payload.write_bytes(b"finished payload")
            size = str(payload.stat().st_size)
            set_visible_state(
                manager,
                rpc,
                visible_gid,
                completed=size,
                total=size,
                status="complete",
                files=[payload],
            )
            rpc.add_hidden(
                "hidden-after-move",
                completed=size,
                total=size,
                info_hash=INFO_HASH.lower(),
            )
            destination = root / "roms" / "moved"

            queued = manager.move_files(entry_id, [str(payload)], str(destination), cleanup=True)
            self.assertEqual(queued["status"], "queued")
            manager._move_tick()
            manager._tick()
            manager._tick()

            self.assertTrue((destination / payload.name).is_file())
            self.assertFalse(source.exists())
            self.assertEqual(manager.snapshot()["torrents"], [])
            self.assertEqual(rpc.statuses, {})


if __name__ == "__main__":
    unittest.main()
