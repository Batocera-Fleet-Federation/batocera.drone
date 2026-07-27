"""Watched-folder torrent downloads driven by a local aria2c daemon.

``TorrentManager`` owns one watched directory: every ``*.torrent`` file dropped
there is registered, handed to aria2c (added paused), and started by the
manager's own slot scheduler so "force start" can genuinely bypass the
configured concurrency limit. UI statuses are exactly
``queued`` / ``downloading`` / ``complete`` / ``error`` (a torrent that has
finished downloading but is still seeding reports ``complete`` with
``seeding: true``).

Changing the watched directory only changes what is scanned going forward:
already-registered torrents keep their original ``torrent_file`` /
``download_dir`` and finish where they started; torrents left in the old
folder are simply no longer picked up.

Durable state (settings + registry) lives in the Drone SQLite state store
under the ``torrent_manager.json`` namespace, mirroring ``download_manager``.
Pure stdlib.
"""

import base64
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, List, Optional

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from .aria2_runtime import (
        Aria2Daemon,
        Aria2RpcError,
        aria2_install_state,
        find_aria2c,
        install_aria2,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.install_paths import drone_install_root as _drone_install_root  # type: ignore
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transfer.aria2_runtime import (  # type: ignore
        Aria2Daemon,
        Aria2RpcError,
        aria2_install_state,
        find_aria2c,
        install_aria2,
    )

TORRENT_STATE_NAMESPACE = "torrent_manager.json"
TORRENT_POLL_SECONDS = float(os.environ.get("DRONE_TORRENT_POLL_SECONDS", "3"))
TORRENT_FILE_ALLOCATION_MODES = ("none", "prealloc", "trunc", "falloc")
TORRENT_STATUSES = ("queued", "downloading", "complete", "error")
TORRENT_BROWSE_MAX_ENTRIES = 500
TORRENT_UPLOAD_MAX_FILE_BYTES = 10 * 1024 * 1024

_TELL_STATUS_KEYS = [
    "gid",
    "status",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "uploadSpeed",
    "connections",
    "numSeeders",
    "errorMessage",
    "bittorrent",
    "files",
]


def default_torrent_directory(settings: Settings) -> Path:
    return _drone_install_root() / "torrents"


def _normalize_torrent_settings(raw, settings: Settings) -> dict:
    raw = raw if isinstance(raw, dict) else {}

    def _int_value(key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(float(raw.get(key, default)))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    try:
        seed_ratio = float(raw.get("seed_ratio", 1.0))
    except (TypeError, ValueError):
        seed_ratio = 1.0
    seed_ratio = max(0.0, min(1000.0, seed_ratio))

    file_allocation = str(raw.get("file_allocation") or "prealloc").strip().lower()
    if file_allocation not in TORRENT_FILE_ALLOCATION_MODES:
        file_allocation = "prealloc"

    directory = str(raw.get("directory") or "").strip() or str(default_torrent_directory(settings))
    # Empty/unset means "same as directory" -- resolved lazily via
    # effective_download_directory() rather than baked in here, so a later
    # change to `directory` keeps steering un-overridden downloads too.
    download_directory = str(raw.get("download_directory") or "").strip()

    return {
        "directory": directory,
        # Where aria2 actually writes downloaded file payloads. Defaults to
        # `directory` (today's behavior: files land next to the .torrent),
        # but can point anywhere -- including a different disk/mount than
        # wherever the Drone app itself is installed (e.g. /media/<usb-drive>
        # or /userdata/roms/<system>) -- since a downloaded ROM/ISO often
        # belongs somewhere else entirely, not next to the watched folder.
        "download_directory": download_directory,
        # aria2 --seed-time, in minutes; 0 stops seeding as soon as the
        # download completes.
        "seed_time": _int_value("seed_time", 60, 0, 60 * 24 * 30),
        "seed_ratio": round(seed_ratio, 2),
        # aria2 --bt-stop-timeout, in seconds; 0 disables the stall timeout.
        "bt_stop_timeout": _int_value("bt_stop_timeout", 0, 0, 24 * 3600),
        "file_allocation": file_allocation,
        "max_concurrent_downloads": _int_value("max_concurrent_downloads", 3, 1, 16),
    }


def effective_download_directory(config: dict) -> str:
    """Where new torrents should actually download to: the explicit override
    if set, else the watched folder itself (today's behavior)."""
    return config.get("download_directory") or config["directory"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_ENTRY_PERSISTED_FIELDS = (
    "id",
    "name",
    "torrent_file",
    "download_dir",
    "status",
    "message",
    "added_at",
    "completed_at",
    "total_bytes",
    "completed_bytes",
    "progress_percent",
)

_ENTRY_LIVE_DEFAULTS = {
    "gid": None,
    "force_started": False,
    "seeding": False,
    "download_speed_bps": 0,
    "upload_speed_bps": 0,
    "num_seeders": 0,
    "connections": 0,
    "eta_seconds": None,
}


class TorrentManager:
    """Watched-folder torrent queue backed by a local aria2c daemon."""

    def __init__(self, settings: Settings, *, start_worker: bool = True) -> None:
        self.settings = settings
        self._lock = Lock()
        self._wake = Event()
        self._daemon: Optional[Aria2Daemon] = None
        self._config: dict = _normalize_torrent_settings({}, settings)
        self._torrents: Dict[str, dict] = {}
        self._restore_state()
        if start_worker:
            thread = Thread(target=self._worker, name="drone-torrent-manager", daemon=True)
            thread.start()

    # ------------------------------------------------------------------ state

    def _restore_state(self) -> None:
        stored = _load_state_payload(
            _state_database_path(self.settings.userdata_root),
            TORRENT_STATE_NAMESPACE,
            {},
        )
        if not isinstance(stored, dict):
            return
        self._config = _normalize_torrent_settings(stored.get("settings"), self.settings)
        entries = stored.get("torrents") if isinstance(stored.get("torrents"), list) else []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            entry_id = str(raw.get("id") or "").strip()
            torrent_file = str(raw.get("torrent_file") or "").strip()
            if not entry_id or not torrent_file or entry_id in self._torrents:
                continue
            entry = {field: raw.get(field) for field in _ENTRY_PERSISTED_FIELDS}
            entry.update(dict(_ENTRY_LIVE_DEFAULTS))
            status = str(entry.get("status") or "queued")
            # aria2 GIDs do not survive a daemon restart: anything that was
            # mid-flight resumes from its .aria2 control file after a fresh
            # (paused) re-add on the next tick.
            entry["status"] = "queued" if status in ("queued", "downloading") else status
            if entry["status"] not in TORRENT_STATUSES:
                entry["status"] = "queued"
            entry["total_bytes"] = int(entry.get("total_bytes") or 0)
            entry["completed_bytes"] = int(entry.get("completed_bytes") or 0)
            entry["progress_percent"] = float(entry.get("progress_percent") or 0.0)
            entry["message"] = str(entry.get("message") or "")
            entry["name"] = str(entry.get("name") or Path(torrent_file).stem)
            entry["download_dir"] = str(entry.get("download_dir") or effective_download_directory(self._config))
            self._torrents[entry_id] = entry

    def _persist_locked(self) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            TORRENT_STATE_NAMESPACE,
            {
                "version": 1,
                "settings": dict(self._config),
                "torrents": [
                    {field: entry.get(field) for field in _ENTRY_PERSISTED_FIELDS}
                    for entry in self._sorted_entries_locked()
                ],
            },
        )

    def _sorted_entries_locked(self) -> List[dict]:
        return sorted(self._torrents.values(), key=lambda entry: (entry.get("added_at") or "", entry.get("id") or ""))

    # ----------------------------------------------------------------- worker

    def _worker(self) -> None:
        while True:
            try:
                self._tick()
            except Exception as error:  # Watchdog loop must never die.
                print(f"Torrent manager tick failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
            if self._wake.wait(max(1.0, TORRENT_POLL_SECONDS)):
                self._wake.clear()

    def wake(self) -> None:
        self._wake.set()

    def _aria2_log_path(self) -> Optional[Path]:
        try:
            return (self.settings.log_dir / "aria2.log").resolve()
        except OSError:
            return None

    def _ensure_rpc(self, needs_daemon: bool, directory: str):
        daemon = self._daemon
        if daemon is not None and daemon.running:
            return daemon.rpc
        if not needs_daemon:
            return None
        found = find_aria2c(self.settings)
        if not found:
            return None
        if daemon is None or daemon.binary_path != found["path"]:
            daemon = Aria2Daemon(found["path"], Path(directory), log_file=self._aria2_log_path())
            self._daemon = daemon
        if daemon.start():
            return daemon.rpc
        return None

    def _tick(self) -> None:
        # Phase A (locked): scan the watched folder, register new .torrent
        # files, and collect the RPC work to do outside the lock.
        with self._lock:
            config = dict(self._config)
            dirty = self._scan_watch_directory_locked(config)
            to_add = [
                dict(entry)
                for entry in self._sorted_entries_locked()
                if entry.get("status") == "queued" and not entry.get("gid")
            ]
            to_query = [
                dict(entry)
                for entry in self._sorted_entries_locked()
                if entry.get("gid") and entry.get("status") != "error"
            ]

        # Phase B (unlocked): talk to aria2.
        rpc = self._ensure_rpc(bool(to_add or to_query), config["directory"])
        add_results: Dict[str, dict] = {}
        status_results: Dict[str, dict] = {}
        if rpc is not None:
            for entry in to_add:
                add_results[entry["id"]] = self._add_torrent_via_rpc(rpc, entry, config)
            for entry in to_query:
                status_results[entry["id"]] = self._query_torrent_via_rpc(rpc, entry)

        # Phase C (locked): apply results and pick queued torrents to start.
        unpause_gids: List[str] = []
        orphaned_gids: List[str] = []
        with self._lock:
            if add_results or status_results:
                dirty = True
            for entry_id, result in add_results.items():
                entry = self._torrents.get(entry_id)
                if entry is None or entry.get("status") == "error":
                    # Deleted or canceled while the add RPC was in flight;
                    # drop the freshly created aria2 download instead of
                    # leaving it orphaned.
                    if result.get("gid"):
                        orphaned_gids.append(result["gid"])
                    continue
                if "gid" in result:
                    entry["gid"] = result["gid"]
                    entry["message"] = ""
                    if result.get("started"):
                        entry["status"] = "downloading"
                        entry["force_started"] = False
                else:
                    entry["status"] = "error"
                    entry["message"] = result.get("error") or "failed to add torrent"
            for entry_id, result in status_results.items():
                entry = self._torrents.get(entry_id)
                if entry is None:
                    continue
                self._apply_aria2_status_locked(entry, result)
            if rpc is None:
                aria2_missing = find_aria2c(self.settings) is None
                for entry in self._torrents.values():
                    if entry.get("status") == "queued" and not entry.get("gid") and aria2_missing:
                        if entry.get("message") != "aria2c is not installed":
                            entry["message"] = "aria2c is not installed"
                            dirty = True
            else:
                unpause_gids = self._pick_startable_gids_locked(config)
            if dirty:
                self._persist_locked()

        # Phase D (unlocked): start the picked torrents, drop any orphans.
        for gid in unpause_gids:
            try:
                rpc.call("aria2.unpause", [gid])
            except Aria2RpcError as error:
                print(f"Torrent unpause failed for gid {gid}: {error}", file=sys.stderr, flush=True)
        for gid in orphaned_gids:
            self._remove_from_aria2(gid)

    def _scan_watch_directory_locked(self, config: dict) -> bool:
        directory = Path(config["directory"])
        if not directory.is_dir():
            return False
        known_files = {entry.get("torrent_file") for entry in self._torrents.values()}
        dirty = False
        try:
            candidates = sorted(directory.iterdir())
        except OSError:
            return False
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() != ".torrent":
                continue
            resolved = str(candidate.resolve())
            if resolved in known_files:
                continue
            entry_id = uuid.uuid4().hex[:12]
            self._torrents[entry_id] = {
                "id": entry_id,
                "name": candidate.stem,
                "torrent_file": resolved,
                "download_dir": effective_download_directory(config),
                "status": "queued",
                "message": "",
                "added_at": _now_iso(),
                "completed_at": None,
                "total_bytes": 0,
                "completed_bytes": 0,
                "progress_percent": 0.0,
                **dict(_ENTRY_LIVE_DEFAULTS),
            }
            dirty = True
        return dirty

    def _add_torrent_via_rpc(self, rpc, entry: dict, config: dict) -> dict:
        try:
            torrent_bytes = Path(entry["torrent_file"]).read_bytes()
        except OSError as error:
            return {"error": f"torrent file unreadable: {error}"}
        force = bool(entry.get("force_started"))
        options = {
            "dir": str(entry.get("download_dir") or effective_download_directory(config)),
            "pause": "false" if force else "true",
            "seed-time": str(config["seed_time"]),
            "seed-ratio": str(config["seed_ratio"]),
            "bt-stop-timeout": str(config["bt_stop_timeout"]),
            "file-allocation": config["file_allocation"],
        }
        try:
            gid = rpc.call("aria2.addTorrent", [base64.b64encode(torrent_bytes).decode("ascii"), [], options])
        except Aria2RpcError as error:
            return {"error": str(error)}
        return {"gid": str(gid), "started": force}

    def _query_torrent_via_rpc(self, rpc, entry: dict) -> dict:
        try:
            return {"result": rpc.call("aria2.tellStatus", [entry["gid"], _TELL_STATUS_KEYS])}
        except Aria2RpcError as error:
            return {"error": str(error)}

    def _apply_aria2_status_locked(self, entry: dict, outcome: dict) -> None:
        if "error" in outcome:
            # A vanished GID (daemon restarted) is recoverable: queue the entry
            # for a fresh paused add; completed entries stay completed.
            entry["gid"] = None
            entry["seeding"] = False
            entry["download_speed_bps"] = 0
            entry["upload_speed_bps"] = 0
            if entry.get("status") in ("queued", "downloading"):
                entry["status"] = "queued"
            return
        result = outcome.get("result") or {}
        total = int(result.get("totalLength") or 0)
        completed = int(result.get("completedLength") or 0)
        download_speed = int(result.get("downloadSpeed") or 0)
        entry["total_bytes"] = total
        entry["completed_bytes"] = completed
        entry["progress_percent"] = round((completed / total) * 100.0, 1) if total else 0.0
        entry["download_speed_bps"] = download_speed
        entry["upload_speed_bps"] = int(result.get("uploadSpeed") or 0)
        entry["num_seeders"] = int(result.get("numSeeders") or 0)
        entry["connections"] = int(result.get("connections") or 0)
        bittorrent = result.get("bittorrent") if isinstance(result.get("bittorrent"), dict) else {}
        info = bittorrent.get("info") if isinstance(bittorrent.get("info"), dict) else {}
        if info.get("name"):
            entry["name"] = str(info["name"])
        remaining = max(0, total - completed)
        entry["eta_seconds"] = int(remaining / download_speed) if download_speed > 0 and remaining else None

        aria2_status = str(result.get("status") or "")
        finished = total > 0 and completed >= total
        if aria2_status == "active":
            entry["seeding"] = finished
            entry["status"] = "complete" if finished else "downloading"
            if finished and not entry.get("completed_at"):
                entry["completed_at"] = _now_iso()
            entry["force_started"] = False
        elif aria2_status in ("waiting", "paused"):
            entry["seeding"] = False
            entry["status"] = "queued"
        elif aria2_status == "complete":
            entry["seeding"] = False
            entry["status"] = "complete"
            entry["download_speed_bps"] = 0
            entry["eta_seconds"] = None
            if not entry.get("completed_at"):
                entry["completed_at"] = _now_iso()
        elif aria2_status == "removed":
            entry["seeding"] = False
            entry["gid"] = None
            if entry.get("status") != "complete":
                entry["status"] = "error"
                entry["message"] = entry.get("message") or "Canceled"
        elif aria2_status == "error":
            entry["seeding"] = False
            entry["status"] = "error"
            entry["message"] = str(result.get("errorMessage") or "aria2 reported an error")
            entry["download_speed_bps"] = 0
            entry["eta_seconds"] = None

    def _pick_startable_gids_locked(self, config: dict) -> List[str]:
        active = sum(1 for entry in self._torrents.values() if entry.get("status") == "downloading")
        slots = max(0, int(config["max_concurrent_downloads"]) - active)
        picked: List[str] = []
        for entry in self._sorted_entries_locked():
            if slots <= 0:
                break
            if entry.get("status") == "queued" and entry.get("gid"):
                picked.append(entry["gid"])
                # Optimistic: aria2.unpause activates immediately (its own
                # concurrency limit is far above ours); the next tick's
                # tellStatus reconciles if it did not.
                entry["status"] = "downloading"
                entry["force_started"] = False
                slots -= 1
        return picked

    # ---------------------------------------------------------------- actions

    def _rpc_if_running(self):
        daemon = self._daemon
        if daemon is not None and daemon.running:
            return daemon.rpc
        return None

    def force_start(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            status = entry.get("status")
            if status == "complete":
                return {"status": "not_applicable", "message": "torrent already completed"}
            if status == "downloading":
                return {"status": "already_active"}
            gid = entry.get("gid")
            if status == "error":
                entry["status"] = "queued"
                entry["message"] = ""
                entry["gid"] = None
            entry["force_started"] = True
            if entry["status"] == "queued" and gid and status == "queued":
                entry["status"] = "downloading"
                entry["force_started"] = False
            self._persist_locked()
        rpc = self._rpc_if_running()
        if rpc is not None:
            if status == "queued" and gid:
                try:
                    rpc.call("aria2.unpause", [gid])
                except Aria2RpcError as error:
                    print(f"Torrent force-start unpause failed: {error}", file=sys.stderr, flush=True)
            elif status == "error" and gid:
                try:
                    rpc.call("aria2.removeDownloadResult", [gid])
                except Aria2RpcError:
                    pass
        self.wake()
        return {"status": "ok"}

    def cancel(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            status = entry.get("status")
            seeding = bool(entry.get("seeding"))
            gid = entry.get("gid")
            if status == "error" or (status == "complete" and not seeding):
                return {"status": "not_cancelable"}
            if status == "complete" and seeding:
                entry["seeding"] = False
                entry["message"] = "Seeding stopped"
            else:
                entry["status"] = "error"
                entry["message"] = "Canceled"
                entry["download_speed_bps"] = 0
                entry["eta_seconds"] = None
            entry["gid"] = None
            entry["force_started"] = False
            self._persist_locked()
        self._remove_from_aria2(gid)
        self.wake()
        return {"status": "cancelled"}

    def delete(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.pop(entry_id, None)
            if entry is None:
                return {"status": "not_found"}
            self._persist_locked()
        self._remove_from_aria2(entry.get("gid"))
        torrent_file_removed = False
        try:
            Path(entry["torrent_file"]).unlink(missing_ok=True)
            torrent_file_removed = True
        except OSError as error:
            print(f"Torrent file delete failed: {error}", file=sys.stderr, flush=True)
        # Best-effort .aria2 control-file cleanup; downloaded payload files are
        # deliberately kept.
        try:
            control = Path(entry.get("download_dir") or "") / f"{entry.get('name') or ''}.aria2"
            if entry.get("name") and control.is_file():
                control.unlink()
        except OSError:
            pass
        self.wake()
        return {
            "status": "deleted",
            "torrent_file_removed": torrent_file_removed,
            "downloaded_files_kept": True,
        }

    def _remove_from_aria2(self, gid: Optional[str]) -> None:
        if not gid:
            return
        rpc = self._rpc_if_running()
        if rpc is None:
            return
        try:
            rpc.call("aria2.forceRemove", [gid])
        except Aria2RpcError:
            pass
        try:
            rpc.call("aria2.removeDownloadResult", [gid])
        except Aria2RpcError:
            pass

    # ------------------------------------------------------- settings/install

    def update_settings(self, payload) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        with self._lock:
            merged = {**self._config, **payload}
            self._config = _normalize_torrent_settings(merged, self.settings)
            config = dict(self._config)
            self._persist_locked()
        try:
            Path(config["directory"]).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            print(f"Torrent directory create failed: {error}", file=sys.stderr, flush=True)
        download_dir = effective_download_directory(config)
        if download_dir != config["directory"]:
            try:
                Path(download_dir).mkdir(parents=True, exist_ok=True)
            except OSError as error:
                print(f"Torrent download directory create failed: {error}", file=sys.stderr, flush=True)
        self.wake()
        return config

    def install_aria2(self) -> dict:
        result = install_aria2(self.settings)
        self.wake()
        return result

    def save_uploaded_torrents(self, files) -> dict:
        """Write uploaded ``(filename, payload)`` pairs into the watched folder.

        The next tick registers them exactly as if they had been dropped in by
        hand. Filenames are reduced to their basename (no traversal), must end
        in ``.torrent``, and collide into ``name (2).torrent`` style suffixes
        instead of overwriting an existing torrent's file.
        """
        with self._lock:
            directory = Path(self._config["directory"])
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError(f"torrent folder is not writable: {error}")
        saved: List[str] = []
        errors: List[dict] = []
        for raw_name, payload in files:
            name = Path(str(raw_name or "")).name.strip()
            if not name.lower().endswith(".torrent") or len(name) <= len(".torrent"):
                errors.append({"file": name or str(raw_name or ""), "error": "only .torrent files are accepted"})
                continue
            if not payload:
                errors.append({"file": name, "error": "file is empty"})
                continue
            if len(payload) > TORRENT_UPLOAD_MAX_FILE_BYTES:
                errors.append({"file": name, "error": "file is larger than 10 MB"})
                continue
            target = directory / name
            stem = target.stem
            counter = 1
            while target.exists():
                counter += 1
                target = directory / f"{stem} ({counter}).torrent"
            try:
                target.write_bytes(payload)
            except OSError as error:
                errors.append({"file": name, "error": str(error)})
                continue
            saved.append(target.name)
        if saved:
            self.wake()
        return {
            "status": "ok" if saved else "no_files_saved",
            "saved": saved,
            "errors": errors,
            "directory": str(directory),
        }

    # ----------------------------------------------------------------- browse

    def _browse_roots(self) -> List[Path]:
        roots = []
        try:
            roots.append(self.settings.userdata_root.resolve())
        except OSError:
            roots.append(self.settings.userdata_root)
        # /media covers Batocera external mounts; the install root makes the
        # default <install>/torrents folder reachable when it sits outside
        # userdata (development checkouts). Skip anything already inside an
        # existing root.
        for candidate in (Path("/media"), _drone_install_root()):
            if candidate.is_dir() and not any(candidate == root or root in candidate.parents for root in roots):
                roots.append(candidate)
        return roots

    def browse_directories(self, raw_path: str) -> dict:
        """Directory picker listing, restricted to the Batocera storage roots."""
        roots = self._browse_roots()
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            return {
                "path": "",
                "parent": None,
                "roots": [str(root) for root in roots],
                "dirs": [{"name": str(root), "path": str(root)} for root in roots],
            }
        target = Path(raw_path).resolve()
        if not any(target == root or root in target.parents for root in roots):
            raise ValueError("path is outside the browsable storage roots")
        if not target.is_dir():
            raise ValueError("path is not a directory")
        dirs = []
        try:
            children = sorted(target.iterdir(), key=lambda child: child.name.lower())
        except OSError as error:
            raise ValueError(f"directory is not readable: {error}")
        for child in children:
            if len(dirs) >= TORRENT_BROWSE_MAX_ENTRIES:
                break
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
            except OSError:
                continue
            dirs.append({"name": child.name, "path": str(child)})
        parent = None if any(target == root for root in roots) else str(target.parent)
        return {
            "path": str(target),
            "parent": parent,
            "roots": [str(root) for root in roots],
            "dirs": dirs,
        }

    # --------------------------------------------------------------- snapshot

    def snapshot(self) -> dict:
        with self._lock:
            config = dict(self._config)
            entries = [dict(entry) for entry in self._sorted_entries_locked()]
        daemon = self._daemon
        aria2 = aria2_install_state(self.settings)
        aria2["running"] = bool(daemon is not None and daemon.running)
        aria2["daemon_error"] = daemon.last_error if daemon is not None else ""
        counts = {status: 0 for status in TORRENT_STATUSES}
        torrents = []
        for entry in entries:
            status = entry.get("status") or "queued"
            counts[status] = counts.get(status, 0) + 1
            torrents.append(
                {
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "status": status,
                    "message": entry.get("message") or "",
                    "seeding": bool(entry.get("seeding")),
                    "progress_percent": float(entry.get("progress_percent") or 0.0),
                    "total_bytes": int(entry.get("total_bytes") or 0),
                    "completed_bytes": int(entry.get("completed_bytes") or 0),
                    "download_speed_bps": int(entry.get("download_speed_bps") or 0),
                    "upload_speed_bps": int(entry.get("upload_speed_bps") or 0),
                    "num_seeders": int(entry.get("num_seeders") or 0),
                    "connections": int(entry.get("connections") or 0),
                    "eta_seconds": entry.get("eta_seconds"),
                    "torrent_file": entry.get("torrent_file"),
                    "download_dir": entry.get("download_dir"),
                    "added_at": entry.get("added_at"),
                    "completed_at": entry.get("completed_at"),
                }
            )
        download_dir = effective_download_directory(config)
        return {
            "target_drone_id": self.settings.device_id,
            "settings": config,
            "directory_exists": Path(config["directory"]).is_dir(),
            "download_directory_exists": Path(download_dir).is_dir(),
            "effective_download_directory": download_dir,
            "aria2": aria2,
            "counts": counts,
            "torrents": torrents,
        }
