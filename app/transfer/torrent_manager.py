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
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, List, Optional, Set, Tuple
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
# aria2.addTorrent/addUri must parse the torrent's metadata before responding --
# slow on a resource-constrained device for a large multi-file torrent (e.g. a
# full TV series). The default 5s RPC timeout (aria2_runtime.ARIA2_RPC_TIMEOUT_
# SECONDS) is fine for lightweight calls but too tight for this one: confirmed
# live ("aria2 RPC aria2.addUri failed: timed out"), a client-side timeout here
# does not mean the add failed server-side -- aria2 can still finish registering
# the torrent after our request already gave up, leaving a real, paused GID we
# never learn about. A more generous timeout just for these two calls make that
# race less likely in the first place (see _recover_from_already_registered_locked
# below for what happens on the retry once it's already happened).
ARIA2_ADD_TIMEOUT_SECONDS = max(5.0, float(os.environ.get("DRONE_TORRENT_ADD_TIMEOUT_SECONDS", "30")))

# aria2's own wording for errorCode=12 ("InfoHash already registered"), e.g.
# "InfoHash 5a892b21006803f464c35df6d223938c9c85d3e1 is already registered."
_ALREADY_REGISTERED_INFOHASH_RE = re.compile(r"InfoHash\s+([0-9a-fA-F]{40})\s+is already registered")

# aria2's error for aria2.unpause on a GID that is not actually paused --
# harmless (the GID is already running under its own steam) but reachable
# whenever a same-tick already-registered recovery (see
# _apply_aria2_status_locked) hands _pick_startable_gids_locked a "queued"
# entry whose recovered gid turns out to already be active in aria2.
_CANNOT_BE_UNPAUSED_RE = re.compile(r"cannot be unpaused now", re.IGNORECASE)

# aria2's error when a gid we're trying to remove is already gone (either
# never existed or a prior removal already succeeded) -- e.g.
# "GID#a2b4c6 is not found". Treated as a successful removal by
# _remove_from_aria2 rather than something worth retrying forever.
_GID_NOT_FOUND_RE = re.compile(r"is not found", re.IGNORECASE)

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
    "following",
    "dir",
    "infoHash",
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
    "info_hash",
    # The in-progress/last "Move Downloaded Files" job for this entry, or
    # None if one was never requested. Deliberately NOT in
    # _ENTRY_LIVE_DEFAULTS below -- unlike gid/force_started/etc, this must
    # survive a restart intact (that's the whole point: _restore_state()
    # flags a mid-flight job `interrupted` and _move_worker resumes it from
    # its own `remaining_files` checkpoint rather than restarting the whole
    # selection). See move_files()/_move_tick() for the dict shape.
    "move_job",
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
    # BitTorrent info-hash, the one identity that's stable across a GID's
    # entire lifecycle (metadata GID -> content GID -> a restart's fresh
    # GID) -- unlike `gid` itself, which churns on every one of those.
    # Populated from aria2's own report (`_apply_aria2_status_locked`) or,
    # for a magnet, synchronously at add time (`add_magnet` already parses
    # it out of the URI for its own duplicate-submission check). Used to
    # stop `_adopt_orphaned_gids` from creating a source-less duplicate row
    # for a GID we already have an entry for, and as a fallback merge key
    # in `_deduplicate_shared_gid_entries_locked` for when that guard is
    # bypassed by timing (e.g. the real entry hasn't polled a status update
    # yet) or the duplicate's `gid` has since gone stale.
    "info_hash": "",
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


def _move_job_summary(move_job: dict) -> dict:
    """Curated subset of a move_job for API responses/snapshot() -- never
    the full remaining_files/moved_files path lists, no reason to ship those
    to the browser every 3s poll."""

    return {
        "status": move_job.get("status"),
        "destination": move_job.get("destination") or "",
        "total_files": int(move_job.get("total_files") or 0),
        "moved_count": len(move_job.get("moved_sources") or []),
        "error_count": len(move_job.get("errors") or []),
        "current_file": move_job.get("current_file") or "",
    }


def _relative_known_file_path(candidate: Path, download_dir_resolved: Optional[Path]) -> str:
    """``candidate``'s path relative to the torrent's ``download_dir``, falling
    back to just its filename when it isn't resolvable underneath it. This is
    the original directory layout aria2 downloaded into (e.g. a multi-file
    torrent's per-release folder and season subfolders), used both to display
    the file tree and, when preserving structure, to rebuild it at the move
    destination."""
    if download_dir_resolved is not None:
        try:
            return str(candidate.resolve().relative_to(download_dir_resolved))
        except (ValueError, OSError):
            pass
    return candidate.name


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


def _magnet_info_hash(magnet_uri: str) -> str:
    """Return a canonical BitTorrent info-hash for duplicate detection.

    Magnet links may spell the same hash with upper/lower-case hexadecimal or
    the older 32-character base32 form. aria2 treats those as one torrent, so
    Drone must do the same before allocating a second registry row.
    """
    query = magnet_uri.split("?", 1)[1] if "?" in magnet_uri else ""
    params = parse_qs(query)
    for topic in params.get("xt", []):
        prefix = "urn:btih:"
        if not topic.lower().startswith(prefix):
            continue
        raw_hash = topic[len(prefix) :].strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", raw_hash):
            return raw_hash.lower()
        if re.fullmatch(r"[A-Za-z2-7]{32}", raw_hash):
            try:
                return base64.b32decode(raw_hash.upper()).hex()
            except (ValueError, TypeError):
                pass
        return raw_hash.lower()
    return ""


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
        # Move-job ordering can't rely on move_job["started_at"] alone --
        # _now_iso() has 1-second resolution, so two "Move" clicks in the
        # same second would tie-break on random entry-id ordering instead
        # of enqueue order. Same fix shape as _next_queue_position above.
        self._next_move_sequence: int = 1
        self._paused: bool = False
        self._recent_move_locations: List[str] = []
        # gids we've told the UI are gone (delete/cancel/clear) but aria2
        # hasn't actually confirmed removing yet -- e.g. it was too busy on
        # a slow write to answer the RPC call. Retried every tick until
        # aria2 confirms, so a removal can never silently strand a still-
        # running download that nothing in the UI can see or control anymore.
        self._pending_removal_gids: List[str] = []
        # Info-hashes we've already fired a torrent_completed notification
        # for this process's lifetime -- belt-and-suspenders against a
        # source-less duplicate entry (see _deduplicate_shared_gid_entries_
        # locked) independently reaching "finished" and sending its own
        # email for the same underlying download. Not persisted: a fresh
        # process legitimately re-arms per entry via each entry's own
        # completed_at/_pending_complete_gid state, which IS persisted.
        self._notified_info_hashes: Set[str] = set()
        # Wakes the dedicated move-files worker (see _move_worker) -- a
        # separate thread/wake from self._wake/_worker's aria2 tick, because
        # a single shutil.move() of a multi-GB file can run for many minutes
        # and must never block aria2 bookkeeping (or any other torrent's
        # move) for that long.
        self._move_wake = Event()
        self._restore_state()
        if start_worker:
            thread = Thread(target=self._worker, name="drone-torrent-manager", daemon=True)
            thread.start()
            move_thread = Thread(target=self._move_worker, name="drone-torrent-mover", daemon=True)
            move_thread.start()

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
        # The watched folder stopped being user-configurable; self-heal any
        # value a pre-upgrade install had persisted so it doesn't linger
        # forever just because nothing else writes settings on its own.
        self._config["directory"] = str(default_torrent_directory(self.settings))
        self._paused = bool(stored.get("paused"))
        recent = stored.get("recent_move_locations")
        self._recent_move_locations = [str(p) for p in recent][:MOVE_RECENT_LOCATIONS_MAX] if isinstance(recent, list) else []
        pending_removals = stored.get("pending_removal_gids")
        self._pending_removal_gids = [str(g) for g in pending_removals if g] if isinstance(pending_removals, list) else []
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
            persisted_info_hash = str(entry.get("info_hash") or "")
            entry.update(dict(_ENTRY_LIVE_DEFAULTS))
            # info_hash IS persisted (unlike the rest of _ENTRY_LIVE_DEFAULTS,
            # e.g. `gid`) -- the blanket update above would otherwise wipe it
            # back to "" on every restart, right after pulling it from disk.
            entry["info_hash"] = persisted_info_hash
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
            move_job = entry.get("move_job")
            if isinstance(move_job, dict):
                try:
                    sequence = int(move_job.get("sequence") or 0)
                except (TypeError, ValueError):
                    sequence = 0
                # A future new move_job's sequence must never collide with
                # (or sort before) one that's just been restored.
                self._next_move_sequence = max(self._next_move_sequence, sequence + 1)
                if move_job.get("status") in ("queued", "moving") and move_job.get("remaining_files"):
                    # Left mid-flight by a restart/crash -- _move_worker
                    # resumes it from remaining_files on its next pass
                    # rather than restarting the whole selection, and fires
                    # torrent_move_resuming once. current_file is
                    # deliberately left as-is (not cleared): if it still
                    # matches remaining_files[0], that's the one file that
                    # may have a partial/truncated write at its target from
                    # an interrupted cross-filesystem copy -- see
                    # _move_tick()'s overwrite_leftover handling.
                    move_job["interrupted"] = True
                    move_job["status"] = "queued"
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
                "pending_removal_gids": list(self._pending_removal_gids),
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

    def _take_move_sequence_locked(self) -> int:
        sequence = self._next_move_sequence
        self._next_move_sequence += 1
        return sequence

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

    def _deduplicate_shared_gid_entries_locked(self) -> bool:
        """Collapse registry rows that provably own the same aria2 download.

        The magnet metadata/content handoff race used to let reconciliation
        adopt the content GID just before the original row learned about it.
        On the next poll both rows pointed at the same GID, producing two UI
        records with identical progress/rate/peer counts. Keep the real source
        row and discard only source-less adopted copies (or duplicate magnet
        rows with the same info-hash); never remove their shared aria2 GID.
        """
        by_gid: Dict[str, List[dict]] = {}
        for entry in self._torrents.values():
            gid = str(entry.get("gid") or "")
            if gid:
                by_gid.setdefault(gid, []).append(entry)

        dirty = False
        for entries in by_gid.values():
            if len(entries) < 2:
                continue
            ordered = sorted(entries, key=lambda entry: (entry.get("added_at") or "", entry.get("id") or ""))
            source_rows = [entry for entry in ordered if entry.get("torrent_file") or entry.get("magnet_uri")]
            adopted_rows = [entry for entry in ordered if not entry.get("torrent_file") and not entry.get("magnet_uri")]

            removals: List[dict] = []
            if source_rows and adopted_rows:
                keeper = source_rows[0]
                removals = [entry for entry in ordered if entry is not keeper and entry in adopted_rows]
            elif not source_rows:
                keeper = ordered[0]
                removals = ordered[1:]
            else:
                magnet_hashes = [_magnet_info_hash(str(entry.get("magnet_uri") or "")) for entry in source_rows]
                if not magnet_hashes[0] or any(info_hash != magnet_hashes[0] for info_hash in magnet_hashes):
                    # Separate watched .torrent files can be rediscovered if
                    # their rows are removed, so only collapse source rows we
                    # can prove are duplicate magnet submissions.
                    continue
                keeper = source_rows[0]
                removals = source_rows[1:]

            if self._merge_and_remove_duplicates_locked(keeper, removals):
                dirty = True

        # Second pass: collapse a source-less orphan row against a source row
        # sharing its BitTorrent info-hash, even when their `gid` values no
        # longer match. `gid` is churny by design (a fresh download gets a
        # new one on every restart, and `_schedule_retry_locked` clears it
        # back to `None` on any error) -- the pass above alone can permanently
        # miss a duplicate that already drifted onto a different/lost `gid`
        # before it ran. `info_hash` is intrinsic to the torrent's content,
        # so it's the identity that survives all of that. Confirmed live: on
        # a real drone, exactly this shape (a source-less "Adopted download"
        # twin whose `gid` had already errored back to `None`) left 3 of 4
        # duplicate torrents permanently stuck as an extra `error` row the
        # gid-keyed pass could never reach. Only ever removes the
        # source-less side, for the same "can be rediscovered from disk"
        # reason the gid-keyed pass above stays off genuine second
        # .torrent-file rows.
        by_info_hash: Dict[str, List[dict]] = {}
        for entry in self._torrents.values():
            info_hash = str(entry.get("info_hash") or "")
            if info_hash:
                by_info_hash.setdefault(info_hash, []).append(entry)
        for entries in by_info_hash.values():
            if len(entries) < 2:
                continue
            ordered = sorted(entries, key=lambda entry: (entry.get("added_at") or "", entry.get("id") or ""))
            source_rows = [entry for entry in ordered if entry.get("torrent_file") or entry.get("magnet_uri")]
            orphan_rows = [entry for entry in ordered if not entry.get("torrent_file") and not entry.get("magnet_uri")]
            if not source_rows or not orphan_rows:
                continue
            if self._merge_and_remove_duplicates_locked(source_rows[0], orphan_rows):
                dirty = True

        return dirty

    def _merge_and_remove_duplicates_locked(self, keeper: dict, removals: List[dict]) -> bool:
        """Fold whichever of `keeper`/`removals` has the most progress into
        `keeper`, then drop `removals` from the registry. Shared by both
        `_deduplicate_shared_gid_entries_locked` passes."""

        if not removals:
            return False
        freshest = max(
            [keeper, *removals],
            key=lambda entry: (
                int(entry.get("total_bytes") or 0),
                int(entry.get("completed_bytes") or 0),
                bool(entry.get("name")),
            ),
        )
        if freshest is not keeper:
            for field in (
                "name",
                "status",
                "message",
                "completed_at",
                "total_bytes",
                "completed_bytes",
                "progress_percent",
                "files",
                "download_dir",
                "retry_count",
                "retry_at",
                "last_error",
                "force_started",
                "seeding",
                "download_speed_bps",
                "upload_speed_bps",
                "num_seeders",
                "connections",
                "eta_seconds",
                "_pending_complete_gid",
            ):
                if field in freshest:
                    keeper[field] = freshest[field]
        for duplicate in removals:
            self._torrents.pop(str(duplicate.get("id") or ""), None)
        return True

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
            has_pending_removals = bool(self._pending_removal_gids)
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
        rpc = self._ensure_rpc(bool(to_add or to_query or stale_gids or has_pending_removals), config["directory"])
        for gid in stale_gids:
            if rpc is None or not self._remove_from_aria2(gid, rpc):
                self._queue_pending_removal(gid)
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
            if self._deduplicate_shared_gid_entries_locked():
                dirty = True
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
                if not _CANNOT_BE_UNPAUSED_RE.search(str(error)):
                    print(f"Torrent unpause failed for gid {gid}: {error}", file=sys.stderr, flush=True)
        for gid in orphaned_gids:
            if not self._remove_from_aria2(gid, rpc):
                self._queue_pending_removal(gid)

        # Phase E (unlocked): retry any removals aria2 hasn't confirmed yet,
        # and adopt any gid aria2 knows about that we've lost track of --
        # keeps the UI an honest mirror of aria2's actual state rather than
        # silently drifting out of sync with it. Confirmed live: a still-
        # writing multi-GB download kept running in aria2, invisible to the
        # UI and impossible to cancel from it, once its entry had been
        # removed from our own tracking while aria2 was too busy on a slow
        # drive to actually answer the removal RPC in time.
        if rpc is not None:
            self._retry_pending_removals(rpc)
            self._adopt_orphaned_gids(rpc)

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
            gid = rpc.call(
                "aria2.addTorrent",
                [base64.b64encode(torrent_bytes).decode("ascii"), [], options],
                timeout=ARIA2_ADD_TIMEOUT_SECONDS,
            )
        except Aria2RpcError as error:
            recovered_gid = self._recover_from_already_registered(rpc, str(error))
            if recovered_gid:
                return {"gid": recovered_gid, "started": False}
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
            gid = rpc.call("aria2.addUri", [[entry["magnet_uri"]], options], timeout=ARIA2_ADD_TIMEOUT_SECONDS)
        except Aria2RpcError as error:
            recovered_gid = self._recover_from_already_registered(rpc, str(error))
            if recovered_gid:
                return {"gid": recovered_gid, "started": False}
            return {"error": str(error)}
        return {"gid": str(gid), "started": force}

    def _recover_from_already_registered(self, rpc, error_message: str) -> Optional[str]:
        """aria2 rejects a duplicate add of an infohash it already has active or
        paused ("InfoHash ... is already registered", errorCode=12). This is
        recoverable, not a real failure: our own prior add attempt for the same
        torrent can still be sitting there even though *we* think it failed --
        confirmed live, caused by a client-side RPC timeout racing a slow-but-
        eventually-successful add (see ARIA2_ADD_TIMEOUT_SECONDS above). Without
        this, every retry just repeats the same failed add forever while aria2
        quietly accumulates one more orphaned paused GID per attempt (seen live:
        a single torrent with 6 duplicate paused GIDs for the same infohash,
        none of them ever progressing). Look up the existing registration by
        infohash and adopt whichever copy has the most progress instead.
        """
        match = _ALREADY_REGISTERED_INFOHASH_RE.search(error_message)
        if not match:
            return None
        return self._find_existing_gid_for_infohash(rpc, match.group(1))

    def _find_existing_gid_for_infohash(self, rpc, info_hash: str) -> Optional[str]:
        keys = ["gid", "infoHash", "completedLength"]
        candidates: List[dict] = []
        try:
            candidates.extend(rpc.call("aria2.tellActive", [keys]) or [])
            candidates.extend(rpc.call("aria2.tellWaiting", [0, 1000, keys]) or [])
        except Aria2RpcError:
            return None
        matches = [c for c in candidates if str(c.get("infoHash") or "").lower() == info_hash.lower()]
        if not matches:
            return None
        best = max(matches, key=lambda c: int(c.get("completedLength") or 0))
        gid = best.get("gid")
        return str(gid) if gid else None

    def _query_torrent_via_rpc(self, rpc, entry: dict) -> dict:
        try:
            result = rpc.call("aria2.tellStatus", [entry["gid"], _TELL_STATUS_KEYS])
        except Aria2RpcError as error:
            return {"error": str(error)}
        # A magnet-added GID can fail with "already registered" (errorCode=12)
        # *asynchronously* -- reported only here, on a later status query --
        # rather than synchronously from the addUri/addTorrent call itself.
        # Confirmed live and reproduced deterministically against a real
        # aria2c: a duplicate addUri for an info-hash aria2 already has
        # active is accepted with a brand-new GID, which then immediately
        # errors out on its own. _recover_from_already_registered (used by
        # _add_torrent_via_rpc/_add_magnet_via_rpc above) only ever ran for
        # the synchronous case, so this one fell through to a plain retry --
        # which discards this GID, adds *another* new one, which fails the
        # exact same asynchronous way, forever, even though the real
        # download (under its original GID, elsewhere in aria2) was healthy
        # the entire time. This is the actual live bug: a torrent that never
        # stops "seemingly downloading fine right before erroring."
        error_message = str((result or {}).get("errorMessage") or "") if isinstance(result, dict) else ""
        recovered_gid = self._recover_from_already_registered(rpc, error_message)
        if recovered_gid and recovered_gid != entry.get("gid"):
            return {"recovered_gid": recovered_gid}
        return {"result": result}

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
        if "recovered_gid" in outcome:
            # See _query_torrent_via_rpc's docstring: this GID asynchronously
            # errored with "already registered", and a different, real GID
            # for the same infohash was found instead -- retarget onto it
            # (same bail-out-early shape as the followedBy handoff below) and
            # hand back the doomed GID so it gets cleaned up rather than left
            # to accumulate in aria2 as dead history.
            doomed_gid = entry.get("gid")
            entry["gid"] = outcome["recovered_gid"]
            entry["last_error"] = ""
            entry["retry_count"] = 0
            entry["retry_at"] = 0.0
            return doomed_gid
        result = outcome.get("result") or {}
        info_hash = str(result.get("infoHash") or "")
        if info_hash:
            # Stable across the metadata-GID -> content-GID handoff below
            # (and across a restart's fresh GID) -- captured before the
            # early-return so the metadata GID's own poll already tags the
            # entry with it, closing the identity gap `_adopt_orphaned_gids`
            # would otherwise hit for the brief window before the content
            # GID's own status is first polled.
            entry["info_hash"] = info_hash
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
            if finished:
                self._confirm_finished_and_notify_locked(entry)
            else:
                entry["_pending_complete_gid"] = None
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
            self._confirm_finished_and_notify_locked(entry)
        elif aria2_status == "removed":
            entry["seeding"] = False
            entry["gid"] = None
            entry["_pending_complete_gid"] = None
            if entry.get("status") != "complete":
                entry["status"] = "error"
                entry["message"] = entry.get("message") or "Canceled"
        elif aria2_status == "error":
            return self._schedule_retry_locked(
                entry,
                str(result.get("errorMessage") or "aria2 reported an error"),
            )
        return None

    def _confirm_finished_and_notify_locked(self, entry: dict) -> None:
        """Only fire ``torrent_completed`` (and set ``completed_at``) once the
        same gid has reported "finished" on two consecutive ticks.

        A magnet link's BitTorrent metadata fetch is itself a tiny, genuinely
        "complete" aria2 download (a few KB -- the piece-hash/file-list info
        dict, not the real content) that runs under its own gid before aria2
        hands off to a new gid for the actual payload (see the ``followedBy``
        handling above). That handoff is normally caught there, but a narrow
        timing race can let a single poll observe the metadata gid as
        "finished" before ``followedBy`` is populated in that response --
        confirmed live: two real magnet-added torrents got ``completed_at``
        (and a "download completed" notification) set while only 11-24%
        through their actual content. A genuine completion trivially survives
        an extra poll tick (nothing changes), but a one-tick metadata blip
        does not -- its gid gets swapped out for the real content gid before
        the next tick, so the "same gid, still finished" check below fails
        for it and never fires.
        """
        gid = entry.get("gid")
        if entry.get("_pending_complete_gid") == gid:
            if not entry.get("completed_at"):
                entry["completed_at"] = _now_iso()
                # Defense in depth on top of the dedup passes above: even if
                # a source-less duplicate of this same torrent somehow still
                # exists and reaches "finished" independently, don't send a
                # second "download completed" email for the same underlying
                # content. Confirmed live: two registry rows sharing one
                # aria2 download each independently satisfied this method's
                # own "finished twice in a row" check and each sent their
                # own notification. Entries predating this field (no
                # info_hash yet) fall back to always notifying, same as
                # before.
                info_hash = str(entry.get("info_hash") or "")
                already_notified = bool(info_hash) and info_hash in self._notified_info_hashes
                if info_hash:
                    self._notified_info_hashes.add(info_hash)
                if not already_notified:
                    _notifications.record_event(
                        self.settings,
                        "torrent_completed",
                        "Torrent download completed",
                        str(entry.get("name") or ""),
                    )
        else:
            entry["_pending_complete_gid"] = gid

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
        if not self._remove_from_aria2(gid):
            self._queue_pending_removal(gid)
        self.wake()
        return {"status": result_status}

    def delete(self, entry_id: str) -> dict:
        with self._lock:
            entry = self._torrents.pop(entry_id, None)
            if entry is None:
                return {"status": "not_found"}
            self._persist_locked()
        if not self._remove_from_aria2(entry.get("gid")):
            self._queue_pending_removal(entry.get("gid"))
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
        # Only wake the gids the scheduler had already granted an active slot
        # to (status == "downloading") -- aria2.unpauseAll would also wake
        # every merely-queued, added-paused torrent that pause()'s
        # aria2.pauseAll swept up along with them, blowing straight past
        # max_concurrent_downloads (confirmed live: pausing then resuming
        # started every queued torrent at once instead of respecting the
        # configured limit). Queued entries stay paused here and get picked
        # up by the normal scheduler on the next tick (_pick_startable_gids_locked),
        # exactly like a freshly-added torrent waiting for a free slot.
        with self._lock:
            self._paused = False
            resume_gids = [
                entry["gid"]
                for entry in self._torrents.values()
                if entry.get("status") == "downloading" and entry.get("gid")
            ]
            self._persist_locked()
        rpc = self._rpc_if_running()
        if rpc is not None:
            for gid in resume_gids:
                try:
                    rpc.call("aria2.unpause", [gid])
                except Aria2RpcError as error:
                    if not _CANNOT_BE_UNPAUSED_RE.search(str(error)):
                        print(f"Torrent resume unpause failed for gid {gid}: {error}", file=sys.stderr, flush=True)
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
                if not self._remove_from_aria2(entry.get("gid")):
                    self._queue_pending_removal(entry.get("gid"))
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
            move_job = entry.get("move_job")
            already_moved = set(move_job.get("moved_sources") or []) if isinstance(move_job, dict) else set()
        # Excludes files a move_job has already relocated -- updated
        # incrementally per file (_move_tick), not just once at the end, so
        # reopening this picker mid-job (or after it finishes) never
        # re-offers a file that's already at its destination. This is the
        # direct fix for a live-confirmed bug: the old synchronous
        # move_files() only trimmed entry["files"] once, at the very end of
        # its whole (often very slow) batch, so reselecting an
        # already-relocated file while an earlier move was still running
        # silently produced a "(2)"-suffixed duplicate at the destination.
        known_files = [p for p in _resolve_known_files(entry_copy) if str(p) not in already_moved]
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
            relative = _relative_known_file_path(candidate, download_dir_resolved)
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

    def move_files(
        self,
        entry_id: str,
        requested_paths,
        destination: str,
        *,
        cleanup: bool,
        preserve_structure: bool = False,
    ) -> dict:
        """Validate the request and enqueue a background move_job -- the
        actual per-file shutil.move work happens on _move_worker's own
        thread, never in this (request-handling) thread. See _move_tick()
        for the worker side; ``_ENTRY_PERSISTED_FIELDS``'s ``move_job``
        comment for why this is safe across a restart."""

        with self._lock:
            entry = self._torrents.get(entry_id)
            if entry is None:
                return {"status": "not_found"}
            if entry.get("status") != "complete":
                return {"status": "not_applicable", "message": "torrent has not completed yet"}
            existing_job = entry.get("move_job")
            if isinstance(existing_job, dict) and existing_job.get("status") in ("queued", "moving"):
                return {"status": "already_in_progress", "move_job": _move_job_summary(existing_job)}
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

        now = _now_iso()
        move_job = {
            "status": "queued",
            "destination": str(destination_path),
            "cleanup": bool(cleanup),
            "preserve_structure": bool(preserve_structure),
            "remaining_files": [str(p) for p in selected],
            "moved_sources": [],
            "moved_files": [],
            "errors": [],
            "total_files": len(selected),
            "current_file": "",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "interrupted": False,
        }
        with self._lock:
            live_entry = self._torrents.get(entry_id)
            if live_entry is None:
                return {"status": "not_found"}
            # The tie-break _next_move_job_entry_locked needs when two jobs'
            # started_at land in the same second (_now_iso() only has
            # 1-second resolution) -- assigned under the lock, unlike
            # started_at above, since it must be strictly ordered.
            move_job["sequence"] = self._take_move_sequence_locked()
            live_entry["move_job"] = move_job
            entry_name = str(live_entry.get("name") or "")
            self._persist_locked()
        # Fired here (enqueue time), not lazily in the worker, so it's
        # immediate regardless of whether the single global mover is
        # currently busy with a different torrent's file.
        _notifications.record_event(
            self.settings,
            "torrent_move_started",
            "Moving downloaded torrent files started",
            entry_name,
            details={"destination": str(destination_path), "total_files": len(selected)},
        )
        self._move_wake.set()
        return {"status": "queued", "move_job": _move_job_summary(move_job)}

    def _move_worker(self) -> None:
        while True:
            try:
                self._move_tick()
            except Exception as error:  # Watchdog loop must never die.
                print(f"Torrent move worker tick failed: {error.__class__.__name__}: {error}", file=sys.stderr, flush=True)
            if self._move_wake.wait(timeout=2.0):
                self._move_wake.clear()

    def _next_move_job_entry_locked(self) -> Optional[dict]:
        """The oldest (by move_job sequence) entry with move work left to
        do. Deliberately a single global pick, not one per torrent -- this
        is what makes the mover process exactly one file at a time across
        every queued/active move_job, not just within one torrent, so a
        batch of several "Move" clicks can never pile concurrent disk I/O on
        top of each other. Ordered by ``sequence`` (assigned under the lock,
        strictly monotonic), not ``started_at`` -- ``_now_iso()`` only has
        1-second resolution, so two jobs enqueued in the same second would
        otherwise tie-break on random entry-id ordering instead of actual
        enqueue order."""

        candidates = [
            entry
            for entry in self._torrents.values()
            if isinstance(entry.get("move_job"), dict)
            and entry["move_job"].get("status") in ("queued", "moving")
            and entry["move_job"].get("remaining_files")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (int(entry["move_job"].get("sequence") or 0), entry.get("id") or ""))
        return candidates[0]

    def _move_tick(self) -> None:
        with self._lock:
            entry = self._next_move_job_entry_locked()
            if entry is None:
                return
            entry_id = entry["id"]
            entry_name = str(entry.get("name") or "")
            move_job = entry["move_job"]
            remaining = move_job.get("remaining_files") or []
            if not remaining:
                return
            source_str = remaining[0]
            # If this exact path was already `current_file` before this
            # tick touched anything, it was mid-copy when the process died
            # (see _restore_state()'s `interrupted` handling) -- its target
            # may already hold a partial/truncated write from that attempt,
            # so it must be overwritten directly rather than run through the
            # normal collision-suffix loop below (which would otherwise
            # mistake that leftover for a real, separate file and rename the
            # retried good copy to "name (2).ext" instead of replacing it).
            overwrite_leftover = bool(source_str) and move_job.get("current_file") == source_str
            was_interrupted = bool(move_job.get("interrupted"))
            move_job["interrupted"] = False
            move_job["current_file"] = source_str
            move_job["status"] = "moving"
            move_job["updated_at"] = _now_iso()
            destination = move_job.get("destination") or ""
            preserve_structure = bool(move_job.get("preserve_structure"))
            download_dir = entry.get("download_dir") or ""
            self._persist_locked()

        if was_interrupted:
            _notifications.record_event(
                self.settings,
                "torrent_move_resuming",
                "Moving downloaded torrent files resumed after interruption",
                entry_name,
            )

        # ---- outside the lock: the actual (possibly slow) filesystem work ----
        destination_path = Path(destination)
        if not destination_path.is_dir():
            # Systemic, not this-file's fault (e.g. an external drive got
            # unplugged mid-job) -- fail the whole job but leave
            # remaining_files untouched so a later retry (once the
            # destination is back) picks up cleanly rather than re-deciding
            # what's left from scratch.
            with self._lock:
                live_entry = self._torrents.get(entry_id)
                if live_entry is not None and live_entry.get("move_job") is move_job:
                    move_job["status"] = "failed"
                    move_job["current_file"] = ""
                    move_job["completed_at"] = _now_iso()
                    move_job["updated_at"] = _now_iso()
                    self._persist_locked()
            _notifications.record_event(
                self.settings,
                "torrent_move_failed",
                "Moving downloaded torrent files failed",
                entry_name,
                details={"reason": "destination is no longer available"},
            )
            return

        source = Path(source_str)
        try:
            download_dir_resolved = Path(download_dir).resolve() if download_dir else None
        except OSError:
            download_dir_resolved = None
        if preserve_structure:
            relative = _relative_known_file_path(source, download_dir_resolved)
            target = destination_path / relative
        else:
            target = destination_path / source.name
        if not overwrite_leftover:
            counter = 1
            while target.exists():
                counter += 1
                target = target.with_name(f"{target.stem} ({counter}){target.suffix}")
        error_message = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        except OSError as error:
            error_message = str(error)

        finalize_result = None
        with self._lock:
            live_entry = self._torrents.get(entry_id)
            if live_entry is None or live_entry.get("move_job") is not move_job:
                # Deleted/replaced while this file was moving -- nothing left
                # to record it against.
                return
            remaining_now = move_job.get("remaining_files") or []
            if remaining_now and remaining_now[0] == source_str:
                remaining_now.pop(0)
            move_job["current_file"] = ""
            move_job["updated_at"] = _now_iso()
            if error_message is None:
                move_job["moved_files"].append(str(target))
                move_job["moved_sources"].append(source_str)
            else:
                move_job["errors"].append({"file": source_str, "error": error_message})
            if not remaining_now:
                finalize_result = self._finalize_move_job_locked(live_entry, move_job)
            self._persist_locked()

        if finalize_result is not None:
            if finalize_result["removed_from_list"]:
                self._remove_from_aria2(finalize_result["removed_gid"])
                if finalize_result["removed_torrent_file"]:
                    try:
                        Path(finalize_result["removed_torrent_file"]).unlink(missing_ok=True)
                    except OSError as error:
                        print(f"Torrent file delete failed after move+cleanup: {error}", file=sys.stderr, flush=True)
                self.wake()
            _notifications.record_event(
                self.settings,
                finalize_result["notification_type"],
                finalize_result["notification_title"],
                entry_name,
                details=finalize_result["notification_details"],
            )

        self._move_wake.set()

    def _finalize_move_job_locked(self, entry: dict, move_job: dict) -> dict:
        """Called once move_job.remaining_files has just gone empty. Mirrors
        the original synchronous move_files()'s end-of-batch cleanup/
        entry-removal decision, plus decides the terminal status/
        notification. Returns what the caller needs to do outside the lock
        (aria2 removal, torrent-file unlink, and which notification to
        fire) -- kept out of this locked method the same way move_files()
        always kept those two RPC/filesystem side effects out of its own
        locked block."""

        moved_sources = move_job.get("moved_sources") or []
        errors = move_job.get("errors") or []
        all_succeeded = not errors
        cleanup_performed = False
        if move_job.get("cleanup") and all_succeeded:
            cleanup_performed = _remove_downloaded_payload(entry)

        removed_from_list = False
        removed_gid = None
        removed_torrent_file = None
        if cleanup_performed:
            removed_gid = entry.get("gid")
            removed_torrent_file = entry.get("torrent_file")
            self._torrents.pop(str(entry.get("id") or ""), None)
            removed_from_list = True
        elif moved_sources:
            moved_set = set(moved_sources)
            entry["files"] = [p for p in (entry.get("files") or []) if p not in moved_set]
        if all_succeeded:
            self._remember_recent_location_locked(str(move_job.get("destination") or ""))

        move_job["current_file"] = ""
        move_job["completed_at"] = _now_iso()
        move_job["updated_at"] = _now_iso()
        # Matches move_files()'s original "partial" philosophy: some
        # progress (even mixed with errors) is still a real finish, not a
        # failure -- "failed" is reserved for a job that made zero progress
        # at all (every remaining file errored) or the systemic
        # destination-unavailable path in _move_tick above.
        if moved_sources:
            move_job["status"] = "complete"
            notification_type = "torrent_move_finished"
            notification_title = "Moving downloaded torrent files finished"
        else:
            move_job["status"] = "failed"
            notification_type = "torrent_move_failed"
            notification_title = "Moving downloaded torrent files failed"

        return {
            "removed_from_list": removed_from_list,
            "removed_gid": removed_gid,
            "removed_torrent_file": removed_torrent_file,
            "notification_type": notification_type,
            "notification_title": notification_title,
            "notification_details": {
                "moved": len(moved_sources),
                "errors": len(errors),
                "cleanup_performed": cleanup_performed,
            },
        }

    def _remember_recent_location_locked(self, path: str) -> None:
        recent = [p for p in self._recent_move_locations if p != path]
        recent.insert(0, path)
        self._recent_move_locations = recent[:MOVE_RECENT_LOCATIONS_MAX]

    def _remove_from_aria2(self, gid: Optional[str], rpc=None) -> bool:
        """Best-effort stop+cleanup of a gid in aria2. Returns True once aria2
        has confirmed the gid is actually gone (removed just now, or already
        gone), False if the RPC calls couldn't be completed right now (aria2
        busy/unreachable -- e.g. blocked on a slow write, confirmed live).
        Callers that need this to eventually happen (delete/cancel/clear)
        must queue the gid via _queue_pending_removal on a False return
        rather than treating "we tried" as "it's actually gone" -- silently
        doing that left a real, still-writing download orphaned in aria2
        with zero representation anywhere in the UI once its entry was
        removed from our own tracking.
        """
        if not gid:
            return True
        if rpc is None:
            rpc = self._rpc_if_running()
        if rpc is None:
            return False
        ok = True
        for method in ("aria2.forceRemove", "aria2.removeDownloadResult"):
            try:
                rpc.call(method, [gid])
            except Aria2RpcError as error:
                if not _GID_NOT_FOUND_RE.search(str(error)):
                    ok = False
        return ok

    def _queue_pending_removal(self, gid: Optional[str]) -> None:
        if not gid:
            return
        with self._lock:
            if gid not in self._pending_removal_gids:
                self._pending_removal_gids.append(gid)
                self._persist_locked()

    def _retry_pending_removals(self, rpc) -> None:
        with self._lock:
            pending = list(self._pending_removal_gids)
        if not pending:
            return
        still_pending = [gid for gid in pending if not self._remove_from_aria2(gid, rpc)]
        with self._lock:
            if still_pending != self._pending_removal_gids:
                self._pending_removal_gids = still_pending
                self._persist_locked()

    def _adopt_orphaned_gids(self, rpc) -> None:
        """Aria2 can end up running a download this manager has no entry for
        -- most likely a removal RPC that couldn't land while aria2 was busy
        (the entry was already dropped from our own tracking by delete/
        cancel/clear regardless, per the docstring above), but also possibly
        something added directly against aria2's RPC port by another caller.
        Either way, the goal is a UI that's an honest mirror of what aria2 is
        actually doing and lets the user act on it -- so any gid aria2
        reports that isn't attached to one of our own entries gets a normal,
        fully manageable entry created for it, reusing the exact same status-
        mapping code path (_apply_aria2_status_locked) a regularly-tracked
        entry goes through on every tick.
        """
        try:
            active = rpc.call("aria2.tellActive", [_TELL_STATUS_KEYS]) or []
            waiting = rpc.call("aria2.tellWaiting", [0, 1000, _TELL_STATUS_KEYS]) or []
        except Aria2RpcError:
            return
        with self._lock:
            known_gids = {entry.get("gid") for entry in self._torrents.values() if entry.get("gid")}
            known_info_hashes = {
                entry.get("info_hash") for entry in self._torrents.values() if entry.get("info_hash")
            }
            dirty = False
            for result in [*active, *waiting]:
                gid = str(result.get("gid") or "")
                if not gid or gid in known_gids:
                    continue
                following = str(result.get("following") or "")
                if following and following in known_gids:
                    # This is the content GID generated by a tracked magnet
                    # metadata GID. Its parent's next tellStatus poll will
                    # expose it through followedBy and retarget the existing
                    # row; adopting it here would create a duplicate row.
                    continue
                if result.get("followedBy"):
                    # A magnet's metadata-only gid, about to hand off to the
                    # real content gid (see _apply_aria2_status_locked's
                    # followedBy handling) -- that gid will surface in its
                    # own right on this same sweep once aria2 reports it.
                    continue
                result_info_hash = str(result.get("infoHash") or "")
                if result_info_hash and result_info_hash in known_info_hashes:
                    # Belt-and-suspenders on top of the `following`/
                    # `followedBy` checks above: those key off the specific
                    # GID chain and can miss a real-world timing gap (a
                    # tracked entry's own status poll hasn't run yet this
                    # tick, or its stale metadata GID already dropped out of
                    # aria2's active/waiting lists before its parent's `gid`
                    # field got retargeted) -- confirmed live on a real
                    # drone: 4 of 6 magnet torrents each grew a source-less
                    # "Adopted download" twin of themselves this way, and the
                    # older ones calcified into permanent duplicate `error`
                    # rows once their orphan twin's gid was lost (nothing to
                    # re-add it from). The info-hash is intrinsic to the
                    # torrent's content, not to a particular GID, so it holds
                    # even when the GID-based checks race.
                    continue
                entry_id = uuid.uuid4().hex[:12]
                entry = {
                    "id": entry_id,
                    "name": "",
                    "torrent_file": "",
                    "magnet_uri": "",
                    "download_dir": str(result.get("dir") or ""),
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
                entry["gid"] = gid
                self._apply_aria2_status_locked(entry, {"result": result})
                if not entry.get("name"):
                    entry["name"] = f"Adopted download {gid[:8]}"
                self._torrents[entry_id] = entry
                known_gids.add(gid)
                if result_info_hash:
                    known_info_hashes.add(result_info_hash)
                dirty = True
            if dirty:
                self._persist_locked()

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
        info_hash = _magnet_info_hash(magnet_uri)
        with self._lock:
            for existing in self._sorted_entries_locked():
                existing_uri = str(existing.get("magnet_uri") or "")
                if info_hash and _magnet_info_hash(existing_uri) == info_hash:
                    return {
                        "status": "already_exists",
                        "id": existing["id"],
                        "name": existing.get("name") or name,
                    }
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
                "info_hash": info_hash,
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
            move_job = entry.get("move_job")
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
                    "move_job": _move_job_summary(move_job) if isinstance(move_job, dict) else None,
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
