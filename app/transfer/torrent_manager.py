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
import math
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs

try:
    from ..common.install_paths import drone_install_root as _drone_install_root
    from ..common.settings import Settings
    from ..device import notifications as _notifications
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
    from device import notifications as _notifications  # type: ignore
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
TORRENT_RETRY_BASE_SECONDS = max(1.0, float(os.environ.get("DRONE_TORRENT_RETRY_BASE_SECONDS", "15")))
TORRENT_RETRY_MAX_SECONDS = max(
    TORRENT_RETRY_BASE_SECONDS,
    float(os.environ.get("DRONE_TORRENT_RETRY_MAX_SECONDS", "300")),
)

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
    "followedBy",
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
    "magnet_uri",
    "download_dir",
    "status",
    "message",
    "added_at",
    "completed_at",
    "total_bytes",
    "completed_bytes",
    "progress_percent",
    "files",
    "queue_position",
    "retry_count",
    "retry_at",
    "last_error",
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

_STATUS_DISPLAY_PRIORITY = {"downloading": 0, "queued": 1, "error": 2, "complete": 3}

MOVE_RECENT_LOCATIONS_MAX = 8
CLEAR_SCOPES = ("completed", "all")


def _resolve_known_files(entry: dict) -> List[Path]:
    """Filesystem paths known to belong to this torrent, best-effort.

    Prefers the exact file list aria2 reported (captured at scan/completion
    time). Falls back to a name guess for entries that predate this field or
    where aria2 never reported one: single-file torrents guess a file
    directly; multi-file torrents land in a same-named subfolder under
    ``download_dir``, so that guess is walked recursively for the real files
    inside it rather than surfaced as-is (a directory is not a "file" a
    caller can move).
    """
    paths = [Path(p) for p in (entry.get("files") or []) if p]
    if paths:
        return paths
    download_dir = entry.get("download_dir")
    name = entry.get("name")
    if not download_dir or not name:
        return []
    guess = Path(download_dir) / name
    if guess.is_file():
        return [guess]
    if guess.is_dir():
        try:
            return sorted(p for p in guess.rglob("*") if p.is_file())
        except OSError:
            return []
    return []


def _torrent_root_dir(entry: dict, known_files: List[Path]) -> Optional[Path]:
    """The dedicated per-torrent subfolder under ``download_dir``, if aria2
    created one (typical for multi-file torrents) -- removing it cleans up
    every file belonging to the torrent (selected for move or not) plus any
    stray control files in one safe step. Returns ``None`` for single-file
    torrents that sit directly inside the (possibly shared) ``download_dir``,
    where only the specific known files should ever be touched.
    """
    download_dir = entry.get("download_dir")
    if not download_dir or not known_files:
        return None
    try:
        download_dir_resolved = Path(download_dir).resolve()
    except OSError:
        return None
    roots = set()
    for candidate in known_files:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        try:
            relative = resolved.relative_to(download_dir_resolved)
        except ValueError:
            continue
        roots.add(relative.parts[0] if len(relative.parts) > 1 else None)
    if len(roots) == 1:
        (only,) = roots
        if only is not None:
            return download_dir_resolved / only
    return None


def _remove_downloaded_payload(entry: dict) -> bool:
    """Best-effort removal of everything this torrent downloaded. Returns
    True when we're confident nothing belonging to the torrent is left."""
    known_files = _resolve_known_files(entry)
    torrent_root = _torrent_root_dir(entry, known_files)
    removed_ok = True
    if torrent_root is not None:
        try:
            shutil.rmtree(torrent_root)
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"Torrent payload cleanup failed for {torrent_root}: {error}", file=sys.stderr, flush=True)
            removed_ok = False
    else:
        for candidate in known_files:
            try:
                candidate.unlink(missing_ok=True)
            except OSError as error:
                print(f"Torrent payload file delete failed for {candidate}: {error}", file=sys.stderr, flush=True)
                removed_ok = False
    download_dir = entry.get("download_dir")
    name = entry.get("name")
    if download_dir and name:
        try:
            control = Path(download_dir) / f"{name}.aria2"
            if control.is_file():
                control.unlink()
        except OSError:
            pass
    return removed_ok


def _magnet_display_name(magnet_uri: str) -> str:
    """Best-effort display name for a freshly-added magnet link, from its
    ``dn=`` parameter or a truncated infohash. Purely a placeholder --
    ``_apply_aria2_status_locked``'s existing ``bittorrent.info.name``
    handling (unchanged, applies regardless of how the entry was added)
    overwrites this with the real torrent name once aria2 resolves metadata.
    """
    query = magnet_uri.split("?", 1)[1] if "?" in magnet_uri else ""
    params = parse_qs(query)
    for display_name in params.get("dn", []):
        if display_name.strip():
            return display_name.strip()
    for topic in params.get("xt", []):
        if topic.startswith("urn:btih:"):
            infohash = topic[len("urn:btih:") :]
            return f"Magnet ({infohash[:12]}…)"
    return "Magnet link"


class TorrentManager:
    """Watched-folder torrent queue backed by a local aria2c daemon."""

    def __init__(self, settings: Settings, *, start_worker: bool = True) -> None:
        self.settings = settings
        self._lock = Lock()
        self._wake = Event()
        self._daemon: Optional[Aria2Daemon] = None
        self._config: dict = _normalize_torrent_settings({}, settings)
        self._torrents: Dict[str, dict] = {}
        self._next_queue_position: int = 1
        self._paused: bool = False
        self._recent_move_locations: List[str] = []
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
        self._paused = bool(stored.get("paused"))
        recent = stored.get("recent_move_locations")
        self._recent_move_locations = [str(p) for p in recent][:MOVE_RECENT_LOCATIONS_MAX] if isinstance(recent, list) else []
        entries = stored.get("torrents") if isinstance(stored.get("torrents"), list) else []
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            entry_id = str(raw.get("id") or "").strip()
            torrent_file = str(raw.get("torrent_file") or "").strip()
            magnet_uri = str(raw.get("magnet_uri") or "").strip()
            # A magnet-only entry has no backing .torrent file on disk, so it
            # must be accepted here on magnet_uri alone -- requiring
            # torrent_file unconditionally would silently drop every magnet
            # entry on each restart (it would never be re-inserted into
            # self._torrents at all, not just lose its GID).
            if not entry_id or not (torrent_file or magnet_uri) or entry_id in self._torrents:
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
            entry["name"] = str(entry.get("name") or (Path(torrent_file).stem if torrent_file else "Magnet link"))
            entry["download_dir"] = str(entry.get("download_dir") or effective_download_directory(self._config))
            files_list = entry.get("files")
            entry["files"] = [str(p) for p in files_list] if isinstance(files_list, list) else []
            try:
                queue_position = int(entry.get("queue_position") or 0)
            except (TypeError, ValueError):
                queue_position = 0
            if queue_position <= 0:
                queue_position = self._take_queue_position_locked()
            else:
                self._next_queue_position = max(self._next_queue_position, queue_position + 1)
            entry["queue_position"] = queue_position
            entry["retry_count"] = max(0, int(entry.get("retry_count") or 0))
            try:
                entry["retry_at"] = float(entry.get("retry_at") or 0.0)
            except (TypeError, ValueError):
                entry["retry_at"] = 0.0
            entry["last_error"] = str(entry.get("last_error") or entry.get("message") or "")
            # Older persisted errors predate automatic retry metadata. Queue
            # them for the first worker tick unless they were intentionally
            # canceled by the user.
            if entry["status"] == "error" and entry["message"] != "Canceled" and entry["retry_at"] <= 0:
                entry["retry_at"] = time.time()
            self._torrents[entry_id] = entry

    def _persist_locked(self) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            TORRENT_STATE_NAMESPACE,
            {
                "version": 2,
                "settings": dict(self._config),
                "paused": bool(self._paused),
                "recent_move_locations": list(self._recent_move_locations),
                "torrents": [
                    {field: entry.get(field) for field in _ENTRY_PERSISTED_FIELDS}
                    for entry in self._sorted_entries_locked()
                ],
            },
        )

    def _sorted_entries_locked(self) -> List[dict]:
        return sorted(self._torrents.values(), key=lambda entry: (entry.get("added_at") or "", entry.get("id") or ""))

    def _take_queue_position_locked(self) -> int:
        position = self._next_queue_position
        self._next_queue_position += 1
        return position

    def _scheduler_entries_locked(self) -> List[dict]:
        """Queue order, independent from the user-visible original add time."""

        return sorted(
            self._torrents.values(),
            key=lambda entry: (
                int(entry.get("queue_position") or 0),
                entry.get("added_at") or "",
                entry.get("id") or "",
            ),
        )

    def _schedule_retry_locked(self, entry: dict, message: str) -> Optional[str]:
        """Keep a real failure visible, then retry it at the back of the queue."""

        gid = entry.get("gid")
        retry_count = max(0, int(entry.get("retry_count") or 0)) + 1
        delay = min(
            TORRENT_RETRY_MAX_SECONDS,
            TORRENT_RETRY_BASE_SECONDS * (2 ** min(retry_count - 1, 10)),
        )
        error_message = str(message or "aria2 reported an error")
        entry["status"] = "error"
        entry["message"] = f"{error_message} — automatic retry in {int(math.ceil(delay))}s"
        entry["last_error"] = error_message
        entry["retry_count"] = retry_count
        entry["retry_at"] = time.time() + delay
        entry["gid"] = None
        entry["force_started"] = False
        entry["seeding"] = False
        entry["download_speed_bps"] = 0
        entry["upload_speed_bps"] = 0
        entry["eta_seconds"] = None
        return gid

    def _requeue_due_errors_locked(self) -> bool:
        now = time.time()
        dirty = False
        for entry in self._scheduler_entries_locked():
            if entry.get("status") != "error" or entry.get("message") == "Canceled":
                continue
            retry_at = float(entry.get("retry_at") or 0.0)
            if retry_at <= 0 or retry_at > now:
                continue
            entry["status"] = "queued"
            entry["message"] = f"Retrying after error: {entry.get('last_error') or 'unknown error'}"
            entry["retry_at"] = 0.0
            entry["queue_position"] = self._take_queue_position_locked()
            dirty = True
        return dirty

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

    def _refresh_pending_download_dirs_locked(self, config: dict) -> List[str]:
        """Keep not-yet-started torrents tracking the current download
        location, so changing the setting and saving takes effect for
        anything that hasn't actually begun receiving bytes yet -- without
        disturbing a torrent that already has data on disk at its original
        location. Zero ``completed_bytes`` is used as "hasn't started":
        covers a freshly-scanned torrent, one still waiting for a
        concurrency slot, one globally paused with nothing downloaded yet,
        and an errored/canceled entry that never got anywhere. A ``queued``
        or ``error`` entry that already has bytes on disk (globally paused
        mid-download, or requeued after a stale GID with prior progress) is
        left alone.

        Entries with no GID yet simply pick up the refreshed
        ``download_dir`` when they're (re-)added this same tick. For a
        ``queued`` entry that was already added to aria2 (paused, waiting
        for a slot), the GID is torn down here and its ``gid`` cleared --
        confirmed against a real aria2c that ``aria2.changeOption``'s ``dir``
        change is *not* honored for an already-added BitTorrent download (the
        payload still landed at the original directory after changeOption +
        unpause), so the only way to actually retarget it is to drop it and
        let the normal ``to_add`` pass in this same tick re-add it fresh at
        the new location. Returned GIDs still need `_remove_from_aria2`
        called on them (outside the lock).
        """
        current = effective_download_directory(config)
        stale_gids: List[str] = []
        for entry in self._torrents.values():
            if entry.get("status") not in ("queued", "error"):
                continue
            if int(entry.get("completed_bytes") or 0) != 0:
                continue
            if entry.get("download_dir") == current:
                continue
            entry["download_dir"] = current
            if entry.get("status") == "queued":
                gid = entry.get("gid")
                if gid:
                    stale_gids.append(gid)
                    entry["gid"] = None
        return stale_gids

    def _tick(self) -> None:
        # Phase A (locked): scan the watched folder, register new .torrent
        # files, and collect the RPC work to do outside the lock.
        with self._lock:
            config = dict(self._config)
            dirty = self._scan_watch_directory_locked(config)
            if self._requeue_due_errors_locked():
                dirty = True
            # Clears `gid` on any queued-but-not-yet-started entry whose
            # download location just changed -- to_add (below) then picks it
            # right back up in this same tick, re-adding it fresh at the new
            # location.
            stale_gids = self._refresh_pending_download_dirs_locked(config)
            if stale_gids:
                dirty = True
            to_add = [
                dict(entry)
                for entry in self._scheduler_entries_locked()
                if entry.get("status") == "queued" and not entry.get("gid")
            ]
            to_query = [
                dict(entry)
                for entry in self._sorted_entries_locked()
                if entry.get("gid") and entry.get("status") != "error"
            ]

        # Phase B (unlocked): talk to aria2.
        rpc = self._ensure_rpc(bool(to_add or to_query or stale_gids), config["directory"])
        for gid in stale_gids:
            self._remove_from_aria2(gid)
        add_results: Dict[str, dict] = {}
        status_results: Dict[str, dict] = {}
        if rpc is not None:
            for entry in to_add:
                if entry.get("magnet_uri"):
                    add_results[entry["id"]] = self._add_magnet_via_rpc(rpc, entry, config)
                else:
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
                    entry["retry_at"] = 0.0
                    if result.get("started"):
                        entry["status"] = "downloading"
                        entry["force_started"] = False
                else:
                    self._schedule_retry_locked(entry, result.get("error") or "failed to add torrent")
            for entry_id, result in status_results.items():
                entry = self._torrents.get(entry_id)
                if entry is None:
                    continue
                retry_gid = self._apply_aria2_status_locked(entry, result)
                if retry_gid:
                    orphaned_gids.append(retry_gid)
            if rpc is None:
                aria2_missing = find_aria2c(self.settings) is None
                for entry in self._torrents.values():
                    if entry.get("status") == "queued" and not entry.get("gid") and aria2_missing:
                        if entry.get("message") != "aria2c is not installed":
                            entry["message"] = "aria2c is not installed"
                            dirty = True
            elif not self._paused:
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
                "files": [],
                "queue_position": self._take_queue_position_locked(),
                "retry_count": 0,
                "retry_at": 0.0,
                "last_error": "",
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

    def _add_magnet_via_rpc(self, rpc, entry: dict, config: dict) -> dict:
        # aria2's addUri handles magnet URIs directly -- Aria2Rpc.call() is
        # already a generic JSON-RPC passthrough (see aria2_runtime.py), so no
        # client-side method needs adding, only this caller. Same paused/dir
        # options shape as _add_torrent_via_rpc above.
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
            gid = rpc.call("aria2.addUri", [[entry["magnet_uri"]], options])
        except Aria2RpcError as error:
            return {"error": str(error)}
        return {"gid": str(gid), "started": force}

    def _query_torrent_via_rpc(self, rpc, entry: dict) -> dict:
        try:
            return {"result": rpc.call("aria2.tellStatus", [entry["gid"], _TELL_STATUS_KEYS])}
        except Aria2RpcError as error:
            return {"error": str(error)}

    def _apply_aria2_status_locked(self, entry: dict, outcome: dict) -> Optional[str]:
        if "error" in outcome:
            # A vanished GID (daemon restarted) is recoverable: queue the entry
            # for a fresh paused add; completed entries stay completed.
            entry["gid"] = None
            entry["seeding"] = False
            entry["download_speed_bps"] = 0
            entry["upload_speed_bps"] = 0
            if entry.get("status") in ("queued", "downloading"):
                entry["status"] = "queued"
            return None
        result = outcome.get("result") or {}
        followed_by = result.get("followedBy")
        if followed_by:
            # A magnet-added GID first only fetches the BitTorrent metadata
            # (the reconstructed .torrent info dict -- a few KB/MB) and then
            # reports itself "complete" at that tiny size, while aria2
            # automatically starts the real content download under a
            # brand-new GID (linked back via this GID's `followedBy` /
            # the new one's `following`). Without following this handoff, our
            # own tracked entry stays pinned to the metadata-only GID
            # forever: the UI shows the torrent as "complete" at a tiny size
            # (this is the exact "downloads the wrong/tiny file" bug) while
            # the real, much larger download runs to completion completely
            # untracked -- no queue slot accounting, no progress, no move-
            # files support. Confirmed live against a real aria2c and a real
            # multi-GB magnet link. Retarget this entry at the new GID and
            # bail out before writing this response's metadata-sized
            # total/completed/status onto the entry -- the very next tick's
            # query (now against the new GID) reports the real numbers.
            entry["gid"] = str(followed_by[0])
            return None
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
        files_field = result.get("files")
        if isinstance(files_field, list):
            paths = [str(f["path"]) for f in files_field if isinstance(f, dict) and f.get("path")]
            if paths:
                entry["files"] = paths
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
            entry["last_error"] = ""
            entry["retry_count"] = 0
            entry["retry_at"] = 0.0
            if finished and not entry.get("completed_at"):
                entry["completed_at"] = _now_iso()
                _notifications.record_event(
                    self.settings, "torrent_completed", "Torrent download completed", str(entry.get("name") or "")
                )
            entry["force_started"] = False
        elif aria2_status in ("waiting", "paused"):
            entry["seeding"] = False
            entry["status"] = "queued"
        elif aria2_status == "complete":
            entry["seeding"] = False
            entry["status"] = "complete"
            entry["last_error"] = ""
            entry["retry_count"] = 0
            entry["retry_at"] = 0.0
            entry["download_speed_bps"] = 0
            entry["eta_seconds"] = None
            if not entry.get("completed_at"):
                entry["completed_at"] = _now_iso()
                _notifications.record_event(
                    self.settings, "torrent_completed", "Torrent download completed", str(entry.get("name") or "")
                )
        elif aria2_status == "removed":
            entry["seeding"] = False
            entry["gid"] = None
            if entry.get("status") != "complete":
                entry["status"] = "error"
                entry["message"] = entry.get("message") or "Canceled"
        elif aria2_status == "error":
            return self._schedule_retry_locked(
                entry,
                str(result.get("errorMessage") or "aria2 reported an error"),
            )
        return None

    def _pick_startable_gids_locked(self, config: dict) -> List[str]:
        active = sum(1 for entry in self._torrents.values() if entry.get("status") == "downloading")
        slots = max(0, int(config["max_concurrent_downloads"]) - active)
        picked: List[str] = []
        for entry in self._scheduler_entries_locked():
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
            stale_gid = None
            if status == "queued" and gid and int(entry.get("completed_bytes") or 0) == 0:
                current_dir = effective_download_directory(self._config)
                if entry.get("download_dir") != current_dir:
                    # aria2 does not honor a `dir` change via
                    # aria2.changeOption for an already-added BitTorrent
                    # download (confirmed against a real aria2c -- the
                    # payload still landed at the original directory). Drop
                    # the GID and clear it so the code below re-adds fresh at
                    # the new location instead of unpausing the stale one.
                    entry["download_dir"] = current_dir
                    stale_gid = gid
                    gid = None
                    entry["gid"] = None
            if status == "error":
                entry["status"] = "queued"
                entry["message"] = ""
                entry["gid"] = None
                entry["last_error"] = ""
                entry["retry_count"] = 0
                entry["retry_at"] = 0.0
                entry["queue_position"] = self._take_queue_position_locked()
            entry["force_started"] = True
            if entry["status"] == "queued" and gid and status == "queued":
                entry["status"] = "downloading"
                entry["force_started"] = False
            self._persist_locked()
        if stale_gid is not None:
            self._remove_from_aria2(stale_gid)
        else:
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
        """Stop a queued/downloading/errored torrent and send it to the back
        of the queue -- lets a slow torrent be bumped out of an active slot
        (to free it for something faster), or an errored one be retried
        without jumping the queue via Force Start, all without losing
        progress. `_remove_from_aria2` only stops the aria2 process and
        clears its result -- it does not delete the partial payload or the
        `.aria2` resume file, so re-adding later continues where this left
        off, same as the post-restart GID-recovery path. Stopping a completed,
        still-seeding torrent is a distinct action (there is nothing to
        "queue") and keeps its own "Seeding stopped" outcome. A torrent
        already sitting in "queued" has nothing to do here -- the UI doesn't
        even offer the button for that status.
        """
        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            status = entry.get("status")
            seeding = bool(entry.get("seeding"))
            gid = entry.get("gid")
            if status == "complete" and not seeding:
                return {"status": "not_cancelable"}
            if status == "complete" and seeding:
                entry["seeding"] = False
                entry["message"] = "Seeding stopped"
                result_status = "seeding_stopped"
            else:
                entry["status"] = "queued"
                entry["message"] = ""
                entry["download_speed_bps"] = 0
                entry["eta_seconds"] = None
                entry["queue_position"] = self._take_queue_position_locked()
                result_status = "requeued"
            entry["gid"] = None
            entry["force_started"] = False
            entry["last_error"] = ""
            entry["retry_count"] = 0
            entry["retry_at"] = 0.0
            self._persist_locked()
        self._remove_from_aria2(gid)
        self.wake()
        return {"status": result_status}

    def delete(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.pop(entry_id, None)
            if entry is None:
                return {"status": "not_found"}
            self._persist_locked()
        self._remove_from_aria2(entry.get("gid"))
        torrent_file_removed = True
        if entry.get("torrent_file"):
            torrent_file_removed = False
            try:
                Path(entry["torrent_file"]).unlink(missing_ok=True)
                torrent_file_removed = True
            except OSError as error:
                print(f"Torrent file delete failed: {error}", file=sys.stderr, flush=True)
        downloaded_files_removed = _remove_downloaded_payload(entry)
        self.wake()
        return {
            "status": "deleted",
            "torrent_file_removed": torrent_file_removed,
            "downloaded_files_removed": downloaded_files_removed,
        }

    def pause(self) -> dict:
        with self._lock:
            self._paused = True
            self._persist_locked()
        rpc = self._rpc_if_running()
        if rpc is not None:
            try:
                rpc.call("aria2.pauseAll")
            except Aria2RpcError as error:
                print(f"Torrent pauseAll failed: {error}", file=sys.stderr, flush=True)
        return self.snapshot()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
            self._persist_locked()
        rpc = self._rpc_if_running()
        if rpc is not None:
            try:
                rpc.call("aria2.unpauseAll")
            except Aria2RpcError as error:
                print(f"Torrent unpauseAll failed: {error}", file=sys.stderr, flush=True)
        self.wake()
        return self.snapshot()

    def clear(self, payload) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        delete_from_ui = bool(payload.get("delete_from_ui"))
        delete_torrent_file = bool(payload.get("delete_torrent_file"))
        delete_downloaded_files = bool(payload.get("delete_downloaded_files"))
        scope = str(payload.get("scope") or "completed").strip().lower()
        if scope not in CLEAR_SCOPES:
            scope = "completed"
        if not (delete_from_ui or delete_torrent_file or delete_downloaded_files):
            return {"status": "no_action_selected"}

        with self._lock:
            if scope == "completed":
                targets = [dict(entry) for entry in self._torrents.values() if entry.get("status") == "complete"]
            else:
                targets = [dict(entry) for entry in self._torrents.values()]
            if delete_from_ui:
                for entry in targets:
                    self._torrents.pop(entry["id"], None)
                self._persist_locked()

        for entry in targets:
            if delete_torrent_file and entry.get("torrent_file"):
                try:
                    Path(entry["torrent_file"]).unlink(missing_ok=True)
                except OSError as error:
                    print(f"Torrent clear: file delete failed: {error}", file=sys.stderr, flush=True)
            if delete_downloaded_files or delete_from_ui:
                self._remove_from_aria2(entry.get("gid"))
            if delete_downloaded_files:
                _remove_downloaded_payload(entry)

        if delete_downloaded_files and not delete_from_ui:
            with self._lock:
                for entry in targets:
                    live = self._torrents.get(entry["id"])
                    if live is not None:
                        live["files"] = []
                        live["message"] = "Downloaded files removed"
                self._persist_locked()

        self.wake()
        return {"status": "ok", "cleared": len(targets), "scope": scope}

    def list_files(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            if entry.get("status") != "complete":
                return {"status": "not_applicable", "message": "torrent has not completed yet"}
            entry_copy = dict(entry)
        known_files = _resolve_known_files(entry_copy)
        download_dir = entry_copy.get("download_dir") or ""
        try:
            download_dir_resolved = Path(download_dir).resolve() if download_dir else None
        except OSError:
            download_dir_resolved = None
        files = []
        for candidate in known_files:
            try:
                exists = candidate.is_file()
            except OSError:
                exists = False
            try:
                size = candidate.stat().st_size if exists else None
            except OSError:
                size = None
            relative = candidate.name
            if download_dir_resolved is not None:
                try:
                    relative = str(candidate.resolve().relative_to(download_dir_resolved))
                except (ValueError, OSError):
                    pass
            files.append(
                {
                    "path": str(candidate),
                    "relative_path": relative,
                    "name": candidate.name,
                    "size": size,
                    "exists": exists,
                }
            )
        return {"status": "ok", "files": files, "download_dir": download_dir}

    def move_files(self, entry_id: str, requested_paths, destination: str, *, cleanup: bool) -> dict:
        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            if entry.get("status") != "complete":
                return {"status": "not_applicable", "message": "torrent has not completed yet"}
            entry_copy = dict(entry)

        known_by_str = {str(candidate): candidate for candidate in _resolve_known_files(entry_copy)}
        selected: List[Path] = []
        for raw in requested_paths or []:
            candidate = known_by_str.get(str(raw))
            if candidate is not None and candidate not in selected:
                selected.append(candidate)
        if not selected:
            return {"status": "no_files_selected"}

        roots = self._browse_roots()
        try:
            destination_path = Path(destination).resolve()
        except OSError:
            return {"status": "invalid_destination", "message": "destination path is invalid"}
        if not any(destination_path == root or root in destination_path.parents for root in roots):
            return {"status": "invalid_destination", "message": "destination is outside the allowed storage roots"}
        try:
            destination_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return {"status": "invalid_destination", "message": f"destination is not writable: {error}"}

        moved: List[str] = []
        moved_sources: List[Path] = []
        errors: List[dict] = []
        for source in selected:
            target = destination_path / source.name
            counter = 1
            while target.exists():
                counter += 1
                target = destination_path / f"{source.stem} ({counter}){source.suffix}"
            try:
                shutil.move(str(source), str(target))
                moved.append(str(target))
                moved_sources.append(source)
            except OSError as error:
                errors.append({"file": str(source), "error": str(error)})

        all_succeeded = not errors and len(moved) == len(selected)
        cleanup_performed = False
        if cleanup and all_succeeded:
            cleanup_performed = _remove_downloaded_payload(entry_copy)

        removed_from_list = False
        removed_gid = None
        removed_torrent_file = None
        with self._lock:
            live_entry = self._torrents.get(entry_id)
            if live_entry is not None:
                if cleanup_performed:
                    # Move + cleanup succeeded and swept up everything left
                    # behind -- nothing remains to track, so drop the
                    # torrent from the list too (matching Delete's
                    # full-removal semantics) rather than leaving a
                    # "complete" entry with an empty file list whose
                    # .torrent file would just get rescanned as new.
                    removed_gid = live_entry.get("gid")
                    removed_torrent_file = live_entry.get("torrent_file")
                    self._torrents.pop(entry_id, None)
                    removed_from_list = True
                elif moved_sources:
                    moved_set = {str(source) for source in moved_sources}
                    live_entry["files"] = [p for p in (live_entry.get("files") or []) if p not in moved_set]
                if all_succeeded:
                    self._remember_recent_location_locked(str(destination_path))
            self._persist_locked()

        if removed_from_list:
            self._remove_from_aria2(removed_gid)
            if removed_torrent_file:
                try:
                    Path(removed_torrent_file).unlink(missing_ok=True)
                except OSError as error:
                    print(f"Torrent file delete failed after move+cleanup: {error}", file=sys.stderr, flush=True)
            self.wake()

        return {
            "status": "ok" if all_succeeded else "partial",
            "moved": moved,
            "errors": errors,
            "cleanup_performed": cleanup_performed,
            "removed_from_list": removed_from_list,
        }

    def _remember_recent_location_locked(self, path: str) -> None:
        recent = [p for p in self._recent_move_locations if p != path]
        recent.insert(0, path)
        self._recent_move_locations = recent[:MOVE_RECENT_LOCATIONS_MAX]

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

    def add_magnet(self, magnet_uri: str) -> dict:
        """Register a magnet link, the direct sibling of a scanned/uploaded
        .torrent file -- same registry-entry shape (see
        _scan_watch_directory_locked), minus a torrent_file since there is no
        watched-folder file backing it. The next tick adds it to aria2 via
        aria2.addUri instead of aria2.addTorrent (_add_magnet_via_rpc).
        """
        magnet_uri = str(magnet_uri or "").strip()
        if not magnet_uri.startswith("magnet:?") or "xt=urn:btih:" not in magnet_uri:
            raise ValueError("That doesn't look like a valid magnet link.")
        name = _magnet_display_name(magnet_uri)
        with self._lock:
            config = dict(self._config)
            entry_id = uuid.uuid4().hex[:12]
            self._torrents[entry_id] = {
                "id": entry_id,
                "name": name,
                "torrent_file": "",
                "magnet_uri": magnet_uri,
                "download_dir": effective_download_directory(config),
                "status": "queued",
                "message": "",
                "added_at": _now_iso(),
                "completed_at": None,
                "total_bytes": 0,
                "completed_bytes": 0,
                "progress_percent": 0.0,
                "files": [],
                "queue_position": self._take_queue_position_locked(),
                "retry_count": 0,
                "retry_at": 0.0,
                "last_error": "",
                **dict(_ENTRY_LIVE_DEFAULTS),
            }
            self._persist_locked()
        self.wake()
        return {"status": "ok", "id": entry_id, "name": name}

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
            paused = bool(self._paused)
            recent_move_locations = list(self._recent_move_locations)
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
                    "magnet_uri": entry.get("magnet_uri"),
                    "is_magnet": bool(entry.get("magnet_uri")),
                    "download_dir": entry.get("download_dir"),
                    "added_at": entry.get("added_at"),
                    "completed_at": entry.get("completed_at"),
                }
            )
        # Display order only: actively-downloading torrents surface first,
        # then queued, then error, then complete. Internal scheduling
        # (_sorted_entries_locked) stays FIFO by added_at for fair slot
        # allocation -- this re-sort touches only the response payload.
        torrents.sort(key=lambda t: (_STATUS_DISPLAY_PRIORITY.get(t["status"], 9), t.get("added_at") or "", t.get("id") or ""))
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
            "paused": paused,
            "recent_move_locations": recent_move_locations,
        }
