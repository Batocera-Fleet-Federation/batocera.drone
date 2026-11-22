"""Backup asset restore: find a backup's games and media in the swarm and queue them.

A backup records which assets its source Drone held (``backups/assets.py``). This module
turns that wish list into downloads on *this* Drone, in three local-only steps (a peer can
never trigger any of it; nothing new is served over ``/peer/*``):

1. **Plan** (``build_plan``): drop what is already here or already queued, ask every online
   paired Drone for its paged ``/peer/inventory/{roms,bios,movies,music}``, and classify each
   item as ``here`` / ``available`` (same content fingerprint or BIOS MD5) / ``probable``
   (same system + relative path + size, shown but never selected by default) /
   ``unavailable``. Name-only matches are never offered. Every candidate peer is kept, for
   failover. A peer that times out, is too old or is offline becomes a *source status*, never
   a plan failure. Plans live in memory for ``PLAN_TTL_SECONDS`` (``PlanStore``).
2. **Assign** (``assign_sources``): whole files, one peer each, spread across peers: best
   transport tier first (LAN/tailnet-direct before direct-public), then the peer with the
   fewest bytes already assigned, the backup's source Drone only as a tie-breaker. This keeps
   the download slots pulling from different Drones. There is still no piece-level multi-peer
   fetch.
3. **Fetch** (``fetch_plan``): queue through the normal ``DownloadManager`` in one named batch
   (one bell entry, cancelled as a whole). Each job carries the *remaining* candidates; when a
   peer fails (or its copy fails verification) the manager retries from the next one, and only
   marks the job failed when none are left. Cancellation and ``PeerIdentityHold`` never fail over.

The manifest's fingerprints are matching hints only. Every download still goes through the
pinned-mTLS ``/peer/*`` path and its verification (signed content inventory + full SHA-256 for
ROMs, MD5 for BIOS, size checks); the expected fingerprint handed to a job is the **peer's**
own value from its inventory, never the manifest's. Existing ROM, BIOS, movie, track and
artwork files are never overwritten (artwork jobs run with ``overwrite=False``; the artwork
gamelist writer only sets the media field, so text fields of a restored gamelist win).
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.parse import urlencode

try:
    from . import assets as _assets
    from .archive import BackupError
    from ..common import identity_provisioning as _identity
    from ..roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path
    from ..storage import movies_store as _movies_store
    from ..storage import music_store as _music_store
    from ..storage import rom_metadata_store as _rom_store
    from ..transfer import local_network as _local_network
    from ..transfer.asset_enqueue import PeerAssetEnqueuer
    from ..transfer.disk_space import (
        ARTWORK_ALLOWANCE_BYTES,
        probe_filesystem as _probe_filesystem,
        reserve_bytes as _reserve_bytes,
    )
    from ..transfer.transfer_batch import format_size as _format_size
except ImportError:  # pragma: no cover - flat execution
    from backups import assets as _assets  # type: ignore
    from backups.archive import BackupError  # type: ignore
    from common import identity_provisioning as _identity  # type: ignore
    from roms.gamelist import ARTWORK_FIELDS, _normalize_gamelist_rom_path  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from storage import rom_metadata_store as _rom_store  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.asset_enqueue import PeerAssetEnqueuer  # type: ignore
    from transfer.disk_space import (  # type: ignore
        ARTWORK_ALLOWANCE_BYTES,
        probe_filesystem as _probe_filesystem,
        reserve_bytes as _reserve_bytes,
    )
    from transfer.transfer_batch import format_size as _format_size  # type: ignore

PLAN_TTL_SECONDS = 15 * 60
MAX_PLANS = 8
PEER_QUERY_WORKERS = 6
PEER_PAGE_TIMEOUT_SECONDS = 20.0
PEER_QUERY_BUDGET_SECONDS = 120.0
MAX_PAGES_PER_KIND = 250
# Smaller than the server's 2000 cap for ROMs: every row carries its gamelist entry and the
# client reads at most 8 MiB per response.
PAGE_LIMIT = {"roms": 1000, "bios": 2000, "movies": 2000, "music": 2000}
MAX_SYSTEMS_QUERY = 200
GROUP_ITEMS_LISTED = 200
PROBABLE_LISTED = 2000
UNAVAILABLE_LISTED = 500
SELECTABLE = ("available", "probable")
_CONFIG = {"network_mode": "local_network"}

STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_UNSUPPORTED = "unsupported"
STATUS_OFFLINE = "offline"


class SwarmUnavailable(BackupError):
    def __init__(self, message: str = "Swarm networking is paused while this Drone's identity is being verified.") -> None:
        super().__init__(message, 503)


def _lower(value: Any) -> str:
    return str(value or "").lower()


def _norm_path(value: Any) -> str:
    return _normalize_gamelist_rom_path(str(value or "")).lower()


# ------------------------------------------------------------------- plan store

class PlanStore:
    """Short-lived, bounded, in-memory plans so *confirm* never re-queries the swarm."""

    def __init__(self, ttl: float = PLAN_TTL_SECONDS, limit: int = MAX_PLANS, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._limit, self._clock = ttl, limit, clock
        self._lock = threading.Lock()
        self._plans: "OrderedDict[str, tuple]" = OrderedDict()

    def put(self, plan: dict) -> None:
        with self._lock:
            self._plans[plan["plan_id"]] = (self._clock() + self._ttl, plan)
            self._expire_locked()
            while len(self._plans) > self._limit:
                self._plans.popitem(last=False)

    def get(self, plan_id: Any, backup_id: Optional[str] = None) -> dict:
        with self._lock:
            self._expire_locked()
            entry = self._plans.get(str(plan_id or ""))
        if entry is None:
            raise BackupError("This plan has expired. Search the swarm again.", 409, {"plan_expired": True})
        plan = entry[1]
        if backup_id is not None and plan["backup_id"] != backup_id:
            raise BackupError("This plan belongs to a different backup.", 409)
        return plan

    def _expire_locked(self) -> None:
        now = self._clock()
        for key in [key for key, (expires, _plan) in self._plans.items() if expires <= now]:
            self._plans.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._plans.clear()


PLANS = PlanStore()


# ------------------------------------------------------------------ local state

class LocalState:
    """What this Drone already has, from its caches (never hashing) plus a path check."""

    def __init__(self, settings: Any, repository: Any = None) -> None:
        self.settings = settings
        self.repository = repository
        self.rom_fp: set = set()
        self.rom_paths: set = set()
        self.rom_ready = False
        self.bios_md5: set = set()
        self.bios_paths: set = set()
        self.media = {"movies": (set(), set()), "music": (set(), set())}
        page = _rom_store.list_rom_asset_rows(settings)
        if page is not None:
            self.rom_ready = True
            for system, path, _size, _kind, fingerprint, _name, _extra in page["rows"]:
                self.rom_paths.add((_lower(system), _norm_path(path)))
                if fingerprint:
                    self.rom_fp.add((_lower(system), _lower(fingerprint)))
        bios = _rom_store.list_bios_asset_rows(settings)
        for path, _size, md5 in bios or []:
            self.bios_paths.add(_norm_path(path))
            if md5:
                self.bios_md5.add(_lower(md5))
        for kind, loader, root in (("movies", _movies_store.list_movies, getattr(settings, "movies_root", None)),
                                   ("music", _music_store.list_music, getattr(settings, "music_root", None))):
            if root is None:
                continue
            try:
                rows = loader(root)
            except Exception:  # noqa: BLE001 - no cache yet means nothing known locally
                rows = []
            fps, paths = self.media[kind]
            for row in rows:
                paths.add(_norm_path(row.get("file_path")))
                fingerprint = row.get("movies_fingerprint") or row.get("music_fingerprint") or row.get("fingerprint")
                if fingerprint:
                    fps.add(_lower(fingerprint))
        self._artwork: Dict[str, dict] = {}

    # Path existence is checked on disk as well: a file the scanner has not indexed yet is
    # still "here", and an existing file is never overwritten.
    def _exists(self, root: Any, *parts: str) -> bool:
        if root is None:
            return False
        try:
            return os.path.lexists(os.path.join(str(root), *parts))
        except (OSError, ValueError):
            return False

    def has(self, wish: dict) -> bool:
        kind, path = wish["kind"], wish["path"]
        if kind == "roms":
            system = _lower(wish["system"])
            if wish.get("fp") and (system, wish["fp"]) in self.rom_fp:
                return True
            if (system, _norm_path(path)) in self.rom_paths:
                return True
            return self._exists(getattr(self.settings, "roms_root", None), wish["system"], *path.split("/"))
        if kind == "bios":
            if wish.get("fp") and wish["fp"] in self.bios_md5:
                return True
            return _norm_path(path) in self.bios_paths or self._exists(getattr(self.settings, "bios_root", None), *path.split("/"))
        fps, paths = self.media[kind]
        if wish.get("fp") and wish["fp"] in fps:
            return True
        root = getattr(self.settings, "movies_root" if kind == "movies" else "music_root", None)
        return _norm_path(path) in paths or self._exists(root, *path.split("/"))

    def missing_media(self, wish: dict) -> List[str]:
        """Recorded media fields whose file is not on disk for an ROM that is here."""
        if self.repository is None or not wish.get("media"):
            return []
        system = wish["system"]
        if system not in self._artwork:
            try:
                self._artwork[system] = self.repository.list_present_artwork(system) or {}
            except Exception:  # noqa: BLE001
                self._artwork[system] = {}
        present = self._artwork[system].get(_norm_path(wish["path"]), set())
        return [field for field in wish["media"] if field not in present]


# ---------------------------------------------------------------- peer querying

def _make_wishes(document: dict, kinds: Iterable[str]) -> List[dict]:
    wishes: List[dict] = []
    for kind in _assets.KINDS:
        if kind not in kinds:
            continue
        for entry in document.get(kind) or []:
            wish = {"kind": kind, "system": entry.get("system", ""), "path": entry["path"], "size": int(entry.get("size") or 0),
                    "fp": entry.get("fp") or entry.get("md5"), "name": entry.get("name") or "",
                    "type": entry.get("type", "file"), "unit": entry.get("unit"), "marker": entry.get("marker"),
                    "media": list(entry.get("media") or []) if kind == "roms" else []}
            wish["key"] = f"{kind[0]}{len(wishes)}"
            wish["exact"], wish["probable"] = {}, {}
            wishes.append(wish)
    return wishes


def _row_digest(row: dict, *names: str) -> Optional[str]:
    for name in names:
        value = row.get(name)
        if isinstance(value, str) and _assets.DIGEST.match(value.lower()):
            return value.lower()
    return None


def _row_size_opt(row: dict) -> Optional[int]:
    for name in ("file_size", "byte_count", "size"):
        value = row.get(name)
        if type(value) is int and 0 <= value < 1 << 50:
            return value
    return None


def _row_size(row: dict) -> int:
    value = _row_size_opt(row)
    return value if value is not None else 0


def _row_path(row: dict, *names: str) -> Optional[str]:
    for name in names:
        path = _assets.clean_relative(row.get(name))
        if path:
            return path
    return None


def _peer_artwork(row: dict) -> List[str]:
    gamelist = row.get("gamelist") if isinstance(row.get("gamelist"), dict) else {}
    return [field for field in ARTWORK_FIELDS if gamelist.get(field)]


def _peer_artwork_sizes(row: dict) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    raw_sizes = row.get("artwork_sizes") if isinstance(row.get("artwork_sizes"), dict) else row.get("media_sizes")
    if isinstance(raw_sizes, dict):
        for field in ARTWORK_FIELDS:
            val = raw_sizes.get(field)
            if type(val) is int and 0 < val < 1 << 40:
                sizes[field] = val
    gamelist = row.get("gamelist") if isinstance(row.get("gamelist"), dict) else {}
    for field in ARTWORK_FIELDS:
        if field not in sizes:
            val = gamelist.get(f"{field}_size")
            if type(val) is int and 0 < val < 1 << 40:
                sizes[field] = val
    return sizes


def _candidate(source: dict, wish: dict, row: dict, exact: bool) -> Optional[dict]:
    """One peer's copy of a wished asset, rebuilt from sanitized fields of its inventory row."""
    kind = wish["kind"]
    reported_size = _row_size_opt(row)
    size = reported_size if reported_size is not None else 0
    size_known = reported_size is not None and reported_size > 0
    if kind == "roms":
        system = _assets.clean_system(row.get("system") or row.get("system_name"))
        path = _row_path(row, "relative_path", "rom_path", "file_path")
        if system is None or path is None:
            return None
        entry_type = "folder" if row.get("entry_type") == "folder" else "file"
        unit = _row_path(row, "transfer_unit_path") if entry_type == "folder" else None
        marker = _row_path(row, "marker_relative_path") if entry_type == "folder" else None
        if entry_type == "folder" and not unit:
            entry_type = "file"
        return {"drone_id": source["drone_id"], "name": source["name"], "tier": source["tier"], "exact": exact,
                "system": system, "path": path, "size": size, "size_known": size_known,
                "fp": _row_digest(row, "rom_fingerprint", "fingerprint"),
                "type": entry_type, "unit": unit, "marker": marker or path, "artwork": _peer_artwork(row),
                "artwork_sizes": _peer_artwork_sizes(row)}
    path = _row_path(row, "relative_path", "file_path", "path")
    if path is None:
        return None
    digest = (_row_digest(row, "bios_md5", "md5") if kind == "bios"
              else _row_digest(row, f"{kind}_fingerprint", "fingerprint"))
    return {"drone_id": source["drone_id"], "name": source["name"], "tier": source["tier"], "exact": exact,
            "system": "", "path": path, "size": size, "size_known": size_known, "fp": digest,
            "type": "file", "unit": None, "marker": None, "artwork": [], "artwork_sizes": {}}


def _index(wishes: List[dict]) -> Tuple[dict, dict]:
    by_fp: Dict[tuple, List[dict]] = {}
    by_path: Dict[tuple, List[dict]] = {}
    for wish in wishes:
        system = _lower(wish["system"])
        if wish.get("fp"):
            by_fp.setdefault((wish["kind"], system, wish["fp"]), []).append(wish)
        by_path.setdefault((wish["kind"], system, _norm_path(wish["path"])), []).append(wish)
    return by_fp, by_path


def _match_row(source: dict, kind: str, row: dict, by_fp: dict, by_path: dict, found: list) -> None:
    """Record every wish this inventory row satisfies as ``(wish, candidate)`` pairs.

    Exact: the same content fingerprint (BIOS: MD5). Probable: same system, relative path and
    size (size unknown in a legacy record counts as a path-only match). Nothing else matches."""
    system = _lower(row.get("system") or row.get("system_name")) if kind == "roms" else ""
    digest = (_row_digest(row, "rom_fingerprint", "fingerprint") if kind == "roms"
              else _row_digest(row, "bios_md5", "md5") if kind == "bios"
              else _row_digest(row, f"{kind}_fingerprint", "fingerprint"))
    exact_ids = set()
    if digest:
        for wish in by_fp.get((kind, system, digest), ()):
            exact_ids.add(id(wish))
            candidate = _candidate(source, wish, row, True)
            if candidate:
                found.append((wish, candidate))
    path = _norm_path(row.get("relative_path") or row.get("rom_path") or row.get("file_path") or row.get("path"))
    size = _row_size(row)
    for wish in by_path.get((kind, system, path), ()):
        if id(wish) in exact_ids or (wish["size"] and wish["size"] != size):
            continue
        candidate = _candidate(source, wish, row, False)
        if candidate:
            found.append((wish, candidate))


def _classify_http_error(error: BaseException) -> Tuple[str, str]:
    if isinstance(error, HTTPError):
        if error.code in (400, 404, 405, 422, 501):
            return STATUS_UNSUPPORTED, "That Drone is too old to list its assets. Update it to the latest Drone release."
        if error.code in (401, 403):
            return STATUS_OFFLINE, "That Drone refused the request. Check that the pairing is still approved on both Drones."
        return STATUS_OFFLINE, f"That Drone could not list its assets (HTTP {error.code})."
    if isinstance(error, (TimeoutError, socket.timeout)):
        return STATUS_TIMEOUT, "That Drone took too long to answer."
    return STATUS_OFFLINE, "Could not reach that Drone."


def _query_source(source: dict, wishes_by_kind: Dict[str, List[dict]], fetch_page: Callable, deadline_seconds: float) -> dict:
    """Ask one peer for the inventory pages that can satisfy the wishes."""
    result = {"drone_id": source["drone_id"], "name": source["name"], "tier": source["tier"], "status": STATUS_OK,
              "found": [], "matched": 0}
    _PeerIdentityHold = _identity_hold()
    for kind, wishes in wishes_by_kind.items():
        if not wishes:
            continue
        by_fp, by_path = _index(wishes)
        params_base: Dict[str, Any] = {}
        if kind == "roms":
            systems = sorted({_lower(wish["system"]) for wish in wishes})[:MAX_SYSTEMS_QUERY]
            params_base["systems"] = ",".join(systems)
        limit = PAGE_LIMIT[kind]
        started, offset = time.monotonic(), 0
        for _page in range(MAX_PAGES_PER_KIND):
            if time.monotonic() - started > deadline_seconds:
                result["status"], result["error"] = STATUS_TIMEOUT, "That Drone's list was too slow; some items may be missing."
                break
            try:
                page = fetch_page(source, kind, dict(params_base, limit=limit, offset=offset), PEER_PAGE_TIMEOUT_SECONDS)
            except _PeerIdentityHold:
                raise
            except Exception as error:  # noqa: BLE001 - becomes a source status, never a plan failure
                result["status"], result["error"] = _classify_http_error(error)
                break
            items = page.get("items") if isinstance(page, dict) and isinstance(page.get("items"), list) else None
            if items is None:
                result["status"], result["error"] = STATUS_UNSUPPORTED, "That Drone returned an unreadable asset list."
                break
            for row in items:
                if isinstance(row, dict):
                    _match_row(source, kind, row, by_fp, by_path, result["found"])
            offset += len(items)
            total = page.get("total")
            if not items or (type(total) is int and offset >= total):
                break
        if result["status"] != STATUS_OK:
            break
    result["matched"] = len({id(wish) for wish, _candidate in result["found"]})
    return result


def _identity_hold():
    try:
        from ..transfer.peer_connectivity import PeerIdentityHold
    except ImportError:  # pragma: no cover - flat execution
        from transfer.peer_connectivity import PeerIdentityHold  # type: ignore
    return PeerIdentityHold


def peer_inventory_fetcher(settings: Any) -> Callable:
    """The real ``fetch_page``: one inventory page over the pinned-mTLS peer client."""
    try:
        from ..transfer.peer_connectivity import _peer_get_json_for_peer
    except ImportError:  # pragma: no cover - flat execution
        from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore

    def fetch(source: dict, kind: str, params: dict, timeout: float) -> dict:
        query = urlencode({key: value for key, value in params.items() if value != ""})
        payload, _route = _peer_get_json_for_peer(
            source["peer"], f"/v1/api/peer/inventory/{kind}?{query}", settings, peer_id=source["drone_id"],
            config=_CONFIG, timeout=timeout, overall_deadline=time.monotonic() + timeout)
        return payload

    return fetch


def online_sources(settings: Any, online_ids: Iterable[str], manager: Any = None) -> List[dict]:
    """Paired Drones that are online, each with its transport tier (0 = LAN/tailnet, 1 = public)."""
    online = {str(value) for value in online_ids}
    sources = []
    for peer in _local_network.paired_peers(settings):
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "").strip()
        if not peer_id or peer_id not in online:
            continue
        tier = manager.peer_tier(peer) if manager is not None and hasattr(manager, "peer_tier") else 1
        sources.append({"drone_id": peer_id, "name": str(peer.get("name") or peer.get("hostname") or peer_id),
                        "peer": peer, "tier": tier})
    return sources


# ------------------------------------------------------------------------ plan

def _queued_on_fs(manager: Any, group: str) -> int:
    if manager is None or not group:
        return 0
    fn = getattr(manager, "queued_bytes_on_filesystem", None)
    if not callable(fn):
        return 0
    try:
        value = fn(group)
    except Exception:  # noqa: BLE001
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _probe_root(root: Any, *, manager: Any = None, queued_cache: Optional[dict] = None) -> Optional[dict]:
    fs = _probe_filesystem(root, usage_fn=shutil.disk_usage)
    if fs is None:
        return None
    group = fs["group"]
    if queued_cache is not None and group in queued_cache:
        queued = queued_cache[group]
    else:
        queued = _queued_on_fs(manager, group)
        if queued_cache is not None:
            queued_cache[group] = queued
    return {
        "path": str(root),
        "mount": fs["mount"],
        "free": fs["free"],
        "total": fs["total"],
        "group": group,
        "reserve": _reserve_bytes(fs["total"]),
        "queued": queued,
    }


def space_report(settings: Any, *, manager: Any = None) -> Dict[str, dict]:
    """Free space for each asset root; roots on one device share a ``group`` (one filesystem budget)."""
    report: Dict[str, dict] = {}
    queued_cache: Dict[str, int] = {}
    for kind, attribute in (
        ("roms", "roms_root"),
        ("bios", "bios_root"),
        ("movies", "movies_root"),
        ("music", "music_root"),
        ("shows", "shows_root"),
    ):
        root = getattr(settings, attribute, None)
        if root is None:
            continue
        info = _probe_root(root, manager=manager, queued_cache=queued_cache)
        if info is not None:
            report[kind] = info
    return report


def _extra_space_report(settings: Any, systems: Iterable[str], *, manager: Any = None) -> Dict[str, dict]:
    """Probe per-system ROM and artwork folders in case any system or artwork directory is on another mount."""
    roms_root = getattr(settings, "roms_root", None)
    if roms_root is None:
        return {}
    extra: Dict[str, dict] = {}
    queued_cache: Dict[str, int] = {}
    for system in sorted({str(s) for s in systems if s}):
        sys_dir = Path(str(roms_root)) / system
        sys_info = _probe_root(sys_dir, manager=manager, queued_cache=queued_cache)
        if sys_info is not None:
            extra[f"roms:{system}"] = sys_info
        art_dir = sys_dir / "images"
        art_info = _probe_root(art_dir, manager=manager, queued_cache=queued_cache)
        if art_info is not None:
            extra[f"artwork:{system}"] = art_info
    return extra


def _artwork_bytes(candidates: List[dict], artwork: List[str]) -> int:
    if not artwork:
        return 0
    total = 0
    for field in artwork:
        reported = max((int((c.get("artwork_sizes") or {}).get(field) or 0) for c in candidates), default=0)
        total += reported if reported > 0 else ARTWORK_ALLOWANCE_BYTES
    return total


def _item_view(wish: dict, status: str, action: str, candidates: List[dict], artwork: List[str]) -> dict:
    exact = [c for c in candidates if c["exact"]]
    if action == "download":
        known_sizes = [int(c["size"]) for c in candidates if c.get("size_known")]
        size_unknown = not known_sizes
        size = max(known_sizes) if known_sizes else 0
        default_selected = bool(exact) and not size_unknown
        size_display = "size unknown" if size_unknown else ""
    elif action == "artwork":
        size_unknown = False
        size = 0
        default_selected = True
        size_display = "media only"
    else:
        size = int(wish.get("size") or 0)
        size_unknown = size <= 0
        default_selected = False
        size_display = "size unknown" if size_unknown else ""
    return {
        "key": wish["key"], "kind": wish["kind"], "system": wish["system"], "path": wish["path"],
        "name": wish["name"] or wish["path"].rsplit("/", 1)[-1],
        "size": size, "size_unknown": size_unknown, "size_display": size_display,
        "artwork_bytes": _artwork_bytes(candidates, artwork) if action in ("download", "artwork") else 0,
        "status": status, "action": action, "artwork": artwork, "exact": bool(exact) or status == "here",
        "default_selected": default_selected,
        "peers": [{"drone_id": c["drone_id"], "name": c["name"], "exact": c["exact"], "tier": c["tier"]} for c in candidates],
        "_candidates": candidates,
    }


def build_plan(settings: Any, *, backup: dict, document: dict, kinds: Iterable[str], sources: List[dict],
               fetch_page: Callable, manager: Any = None, repository: Any = None) -> dict:
    """Classify a backup's wished assets against this Drone and the online swarm."""
    if not _identity.swarm_networking_allowed():
        raise SwarmUnavailable()
    kinds = [kind for kind in _assets.KINDS if kind in set(kinds)]
    wishes = _make_wishes(document, kinds)
    local = LocalState(settings, repository)
    here: List[dict] = []
    needed: List[dict] = []
    media_only: Dict[str, List[str]] = {}
    for wish in wishes:
        if local.has(wish):
            missing = local.missing_media(wish) if wish["kind"] == "roms" else []
            if missing:
                wish["local"] = "here"
                media_only[wish["key"]] = missing
                needed.append(wish)
            else:
                here.append(wish)
            continue
        pending = None
        if manager is not None and hasattr(manager, "find_pending_asset"):
            asset_type = {"roms": "rom"}.get(wish["kind"], wish["kind"])
            pending = manager.find_pending_asset(asset_type, wish["path"], system=wish["system"] or None, expected_fingerprint=wish.get("fp"))
        if pending:
            wish["local"] = "queued"
            here.append(wish)
        else:
            wish["local"] = None
            needed.append(wish)
    wishes_by_kind = {kind: [wish for wish in needed if wish["kind"] == kind] for kind in kinds}
    source_results: List[dict] = []
    if any(wishes_by_kind.values()) and sources:
        with ThreadPoolExecutor(max_workers=max(1, min(PEER_QUERY_WORKERS, len(sources)))) as pool:
            futures = [pool.submit(_query_source, source, wishes_by_kind, fetch_page, PEER_QUERY_BUDGET_SECONDS)
                       for source in sources]
            source_results = [future.result() for future in futures]
    else:
        source_results = [{"drone_id": s["drone_id"], "name": s["name"], "tier": s["tier"], "status": STATUS_OK,
                           "found": [], "matched": 0} for s in sources]
    for result in source_results:
        for wish, candidate in result["found"]:
            bucket = wish["exact"] if candidate["exact"] else wish["probable"]
            bucket.setdefault(candidate["drone_id"], candidate)
    items: List[dict] = []
    totals = {"here": len(here), "available": 0, "probable": 0, "unavailable": 0}
    per_kind = {kind: {"here": 0, "available": 0, "probable": 0, "unavailable": 0, "bytes": 0} for kind in kinds}
    for wish in here:
        per_kind[wish["kind"]]["here"] += 1
    for wish in needed:
        exact, probable = list(wish["exact"].values()), list(wish["probable"].values())
        candidates = exact or probable
        offered = sorted({field for c in candidates for field in c["artwork"]} & set(media_only.get(wish["key"]) or wish["media"]))
        if wish.get("local") == "here":
            if candidates and offered:
                items.append(_item_view(wish, "here", "artwork", candidates, offered))
            per_kind[wish["kind"]]["here"] += 1
            totals["here"] += 1
            continue
        status = "available" if exact else "probable" if probable else "unavailable"
        totals[status] += 1
        per_kind[wish["kind"]][status] += 1
        action = "download" if candidates else ""
        item = _item_view(wish, status, action, candidates, offered if candidates else [])
        if status == "available" and not item.get("size_unknown"):
            per_kind[wish["kind"]]["bytes"] += item["size"]
        items.append(item)
    plan_id = secrets.token_hex(16)
    rom_systems = {wish["system"] for wish in wishes if wish["kind"] == "roms" and wish.get("system")}
    plan = {
        "plan_id": plan_id, "backup_id": backup["backup_id"], "backup_name": backup.get("name") or "",
        "created_at": time.time(), "legacy": bool(document.get("legacy")), "truncated": bool(document.get("truncated")),
        "kinds": per_kind, "totals": totals, "items": items, "index": {item["key"]: item for item in items},
        "sources": [{"drone_id": r["drone_id"], "name": r["name"], "status": r["status"], "error": r.get("error", ""),
                     "matched": r["matched"], "tier": r["tier"]} for r in source_results],
        "source_drone_id": str((backup.get("source") or {}).get("drone_id") or ""),
        "space": space_report(settings, manager=manager),
        "_extra_space": _extra_space_report(settings, rom_systems, manager=manager),
        "queued_already": sum(1 for wish in here if wish.get("local") == "queued"),
        "wished": len(wishes),
    }
    PLANS.put(plan)
    return plan


def group_key(item: dict) -> str:
    return f"{item['kind']}:{item['system']}"


def _public_item(item: dict) -> dict:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def public_plan(plan: dict, *, manager: Any = None) -> dict:
    """The JSON the browser and Ports client get: counts for everything, bounded item lists."""
    groups: "OrderedDict[str, dict]" = OrderedDict()
    probable, unavailable = [], []
    default_items: List[dict] = []
    for item in plan["items"]:
        if item.get("default_selected"):
            default_items.append(item)
        if item["status"] == "unavailable":
            if len(unavailable) < UNAVAILABLE_LISTED:
                unavailable.append(_public_item(item))
            continue
        if item["status"] == "probable":
            if len(probable) < PROBABLE_LISTED:
                probable.append(_public_item(item))
            continue
        key = group_key(item)
        group = groups.setdefault(key, {"key": key, "kind": item["kind"], "system": item["system"], "count": 0,
                                        "default_count": 0, "unknown_size_count": 0, "bytes": 0, "items": [], "omitted": 0})
        group["count"] += 1
        if item.get("default_selected"):
            group["default_count"] += 1
            if item["action"] == "download":
                group["bytes"] += int(item.get("size") or 0)
            group["bytes"] += int(item.get("artwork_bytes") or 0)
        if item.get("size_unknown"):
            group["unknown_size_count"] += 1
        if len(group["items"]) < GROUP_ITEMS_LISTED:
            group["items"].append(_public_item(item))
        else:
            group["omitted"] += 1
    filesystems = selection_space(plan, default_items, manager=manager)
    return {
        "plan_id": plan["plan_id"], "backup_id": plan["backup_id"], "backup_name": plan["backup_name"],
        "legacy": plan["legacy"], "truncated": plan["truncated"], "totals": plan["totals"], "kinds": plan["kinds"],
        "groups": list(groups.values()), "probable": probable, "unavailable": unavailable,
        "unavailable_omitted": max(0, plan["totals"]["unavailable"] - len(unavailable)),
        "probable_omitted": max(0, plan["totals"]["probable"] - len(probable)),
        "sources": plan["sources"], "space": plan["space"], "filesystems": filesystems,
        "fits": all(fs["fits"] for fs in filesystems),
        "queued_already": plan["queued_already"],
        "swarm_available": True, "expires_in": PLAN_TTL_SECONDS,
    }


# -------------------------------------------------------------------- selection

def resolve_selection(plan: dict, selection: Any) -> List[dict]:
    """The plan items to fetch: defaults (exact items) minus ``deselected`` / ``deselected_groups``,
    plus explicitly ``selected`` keys (e.g. ticked probable matches). Unknown or unavailable keys
    are refused; nothing outside the plan can be requested."""
    selection = selection if isinstance(selection, dict) else {}
    index = plan["index"]

    def keys(name: str) -> List[str]:
        values = selection.get(name)
        if values is None:
            return []
        if not isinstance(values, list) or len(values) > 1_000_000 or any(not isinstance(v, str) for v in values):
            raise BackupError(f"Invalid {name} list.")
        return values

    explicit, removed = keys("selected"), set(keys("deselected"))
    removed_groups = set(keys("deselected_groups"))
    for key in explicit + list(removed):
        if key not in index:
            raise BackupError("That selection does not belong to this plan.", 400)
    chosen: "OrderedDict[str, dict]" = OrderedDict()
    for item in plan["items"]:
        if item["default_selected"] and item["key"] not in removed and group_key(item) not in removed_groups:
            chosen[item["key"]] = item
    for key in explicit:
        item = index[key]
        if not item["action"]:
            raise BackupError("That item cannot be fetched: no online Drone has it.", 400)
        chosen[key] = item
    return list(chosen.values())


def _root_for_item(item: dict, space: dict, extra_space: dict, *, art: bool = False) -> Optional[dict]:
    kind = str(item.get("kind") or "")
    system = str(item.get("system") or "")
    if art or item.get("action") == "artwork":
        return extra_space.get(f"artwork:{system}") or extra_space.get(f"roms:{system}") or space.get("roms")
    if kind == "roms":
        return extra_space.get(f"roms:{system}") or space.get("roms")
    if kind == "movies" and str(item.get("path") or "").lower().startswith("shows/") and "shows" in space:
        return space.get("shows")
    return space.get(kind)


def selection_space(plan: dict, items: List[dict], *, manager: Any = None, fresh: bool = False,
                    settings: Any = None) -> List[dict]:
    """Per filesystem: required bytes (selection + queued + staging + reserve) vs. free space."""
    if fresh and settings is not None:
        space = space_report(settings, manager=manager)
        rom_systems = {item.get("system") for item in items if item.get("kind") == "roms" and item.get("system")}
        extra_space = _extra_space_report(settings, rom_systems, manager=manager)
    else:
        space = plan.get("space") or {}
        extra_space = plan.get("_extra_space") or {}
    pools: Dict[str, dict] = {}
    queued_cache: Dict[str, int] = {}

    def _pool_for(root: dict, kind: str) -> dict:
        group = str(root.get("group") or kind)
        if group not in pools:
            total = root.get("total")
            total_int = total if isinstance(total, int) and not isinstance(total, bool) else 0
            reserve = root.get("reserve")
            reserve_int = (reserve if isinstance(reserve, int) and not isinstance(reserve, bool)
                           else _reserve_bytes(total_int))
            if manager is not None:
                if group not in queued_cache:
                    queued_cache[group] = _queued_on_fs(manager, group)
                queued = queued_cache[group]
            else:
                queued = int(root.get("queued") or 0)
            pools[group] = {
                "group": group,
                "mount": str(root.get("mount") or root.get("path") or ""),
                "path": str(root.get("path") or root.get("mount") or ""),
                "free": int(root.get("free") or 0),
                "selected": 0,
                "queued": queued,
                "staging": 0,
                "reserve": reserve_int,
                "needed": 0,
                "required": 0,
                "fits": True,
                "short": False,
                "kinds": [],
            }
        pool = pools[group]
        if kind and kind not in pool["kinds"]:
            pool["kinds"].append(kind)
        return pool

    for item in items:
        action = item.get("action")
        if action not in ("download", "artwork"):
            continue
        kind = str(item.get("kind") or "")
        if action == "download":
            root = _root_for_item(item, space, extra_space, art=False)
            if root:
                _pool_for(root, kind)["selected"] += int(item.get("size") or 0)
        art_bytes = int(item.get("artwork_bytes") or 0)
        if action == "artwork" and art_bytes <= 0 and item.get("artwork"):
            art_bytes = len(item["artwork"]) * ARTWORK_ALLOWANCE_BYTES
        if art_bytes > 0:
            art_root = _root_for_item(item, space, extra_space, art=True)
            if art_root:
                _pool_for(art_root, kind)["selected"] += art_bytes

    out: List[dict] = []
    for pool in pools.values():
        required = pool["selected"] + pool["queued"] + pool["staging"] + pool["reserve"]
        fits = pool["free"] >= required
        pool["required"] = required
        pool["needed"] = required
        pool["fits"] = fits
        pool["short"] = not fits
        out.append(pool)
    return out


def ensure_space(settings: Any, plan: dict, items: List[dict], manager: Any = None) -> List[dict]:
    """Recompute per-filesystem space and raise 409 ``insufficient_space`` if any affected disk doesn't fit."""
    pools = selection_space(plan, items, manager=manager, fresh=True, settings=settings)
    short_pools = [pool for pool in pools if not pool["fits"]]
    if short_pools:
        summary = "; ".join(
            f"{p['mount'] or p['path'] or p['group']}: need {_format_size(p['required'])}, {_format_size(p['free'])} free"
            for p in short_pools
        )
        raise BackupError(
            f"Not enough free disk space ({summary}). Untick some items or free disk space before confirming.",
            409,
            {
                "error_code": "insufficient_space",
                "insufficient_space": True,
                "filesystems": pools,
                "space": pools,
            },
        )
    return pools


# ------------------------------------------------------------------- assignment

def assign_sources(items: List[dict], source_drone_id: str = "") -> Dict[str, float]:
    """Order each item's candidate peers (``item["_order"]``): spread files across peers.

    Largest first; each file goes to the peer in the best transport tier that has been assigned the
    fewest bytes so far (the backup's source Drone wins ties only), so concurrent downloads pull from
    different Drones. The remaining candidates follow, in the same preference, as failover order.
    Returns bytes assigned per peer."""
    load: Dict[str, float] = {}
    for item in sorted((i for i in items if i["_candidates"]), key=lambda i: (-int(i["size"] or 0), i["key"])):
        pool = [c for c in item["_candidates"] if c["exact"]] or list(item["_candidates"])

        def preference(candidate: dict) -> tuple:
            return (candidate["tier"], load.get(candidate["drone_id"], 0.0),
                    0 if candidate["drone_id"] == source_drone_id else 1, candidate["drone_id"])

        ordered = sorted(pool, key=preference)
        load[ordered[0]["drone_id"]] = load.get(ordered[0]["drone_id"], 0.0) + max(int(item["size"] or 0), 1)
        item["_order"] = ordered
    return load


# ---------------------------------------------------------------------- enqueue

def _rom_overrides(candidate: dict) -> dict:
    transfer = candidate["unit"] if candidate["type"] == "folder" and candidate["unit"] else candidate["path"]
    return {"relative_path": transfer, "file_path": transfer, "rom_name": transfer, "file_size": candidate["size"] or None,
            "total_bytes": candidate["size"] or None, "_expected_fingerprint": candidate["fp"],
            "_entry_type": candidate["type"], "entry_type": candidate["type"],
            "_marker_relative_path": candidate["marker"] if candidate["type"] == "folder" else None}


def _flat_overrides(kind: str, candidate: dict) -> dict:
    out = {"relative_path": candidate["path"], "file_path": candidate["path"], "rom_name": candidate["path"],
           "file_size": candidate["size"] or None, "total_bytes": candidate["size"] or None,
           "_expected_fingerprint": candidate["fp"]}
    if kind == "bios":
        out.update(bios_name=candidate["path"], bios_md5=candidate["fp"])
    return out


def _peer_item(kind: str, candidate: dict, artwork: List[str]) -> dict:
    """The dict ``PeerAssetEnqueueMixin._enqueue_local_asset`` expects, from one candidate."""
    if kind == "roms":
        item = {"system": candidate["system"], "relative_path": candidate["path"], "file_size": candidate["size"] or None,
                "fingerprint": candidate["fp"], "entry_type": candidate["type"],
                "gamelist": {field: "1" for field in artwork}}
        if candidate["type"] == "folder":
            item.update(transfer_unit_path=candidate["unit"], marker_relative_path=candidate["marker"])
        return item
    if kind == "bios":
        return {"path": candidate["path"], "file_size": candidate["size"] or None, "md5": candidate["fp"]}
    return {"file_path": candidate["path"], "file_size": candidate["size"] or None, "fingerprint": candidate["fp"]}


def _failover_resolver(item: dict) -> Callable[[dict], List[dict]]:
    order = item["_order"]
    kind = item["kind"]
    rest = order[1:]

    def resolve(job: dict) -> List[dict]:
        if str(job.get("file_type") or "").upper() == "ARTWORK":
            field = job.get("artwork_type")
            return [{"drone_id": c["drone_id"], "set": {"rom_path": c["path"], "rom_name": c["path"]}}
                    for c in order if c["drone_id"] != job.get("source_drone_id") and field in c["artwork"]]
        overrides = _rom_overrides if kind == "roms" else (lambda c: _flat_overrides(kind, c))
        return [{"drone_id": c["drone_id"], "set": overrides(c)} for c in rest]

    return resolve


def fetch_plan(settings: Any, manager: Any, plan: dict, items: List[dict], *, batch_id: Optional[str] = None,
               label: Optional[str] = None) -> dict:
    """Queue the chosen items in one named batch. Returns the batch id, job ids and counts."""
    if not _identity.swarm_networking_allowed():
        raise SwarmUnavailable()
    if manager is None:
        raise BackupError("The download queue is unavailable.", 503)
    chosen = [item for item in items if item["_candidates"]]
    pools = ensure_space(settings, plan, chosen, manager)
    assign_sources(chosen, plan.get("source_drone_id", ""))
    batch_id = batch_id or secrets.token_hex(16)
    label = label or plan.get("backup_name") or "backup"
    enqueuer = PeerAssetEnqueuer(settings, getattr(manager, "repository", None))
    index_cache: dict = {}
    artwork_cache: dict = {}
    job_ids: List[str] = []
    skipped = 0
    unavailable = plan["totals"]["unavailable"]
    opener = getattr(type(manager), "named_batch", None)
    persistence = getattr(type(manager), "batch_persistence", None)
    with (manager.named_batch(batch_id=batch_id, label=label, extra={"unavailable": unavailable})
          if opener else nullcontext()) as batch, (manager.batch_persistence() if persistence else nullcontext()):
        for item in chosen:
            queued = _enqueue_item(settings, manager, enqueuer, item, index_cache, artwork_cache)
            if queued is None:
                skipped += 1
                continue
            job_ids.extend(queued)
        if batch is not None:
            batch.extra["skipped"] = skipped
    return {"batch_id": batch_id, "label": label, "queued": len(job_ids), "job_ids": job_ids, "skipped": skipped,
            "unavailable": unavailable, "selected": len(chosen), "space": pools, "filesystems": pools}


def _enqueue_item(settings: Any, manager: Any, enqueuer: Any, item: dict, index_cache: dict, artwork_cache: dict) -> Optional[List[str]]:
    """Queue one chosen item from its first reachable candidate; ``None`` when nothing was queued."""
    asset_type = {"roms": "rom"}.get(item["kind"], item["kind"])
    order = list(item["_order"])
    peer = None
    while order:
        peer = _local_network.get_paired_peer(settings, order[0]["drone_id"])
        if peer:
            break
        order.pop(0)  # unpaired since the plan was made
    if not order or not peer:
        return None
    item["_order"] = order
    candidate = order[0]
    if (item["action"] == "download" and hasattr(manager, "find_pending_asset")
            and manager.find_pending_asset(asset_type, item["path"], system=item["system"] or None,
                                           expected_fingerprint=candidate["fp"])):
        return None
    scope = (manager.failover_scope(_failover_resolver(item)) if hasattr(type(manager), "failover_scope") else nullcontext())
    try:
        with scope:
            jobs = enqueuer._enqueue_local_asset(
                manager, _CONFIG, peer, item["kind"], _peer_item(item["kind"], candidate, item["artwork"]),
                default_system=candidate["system"], include_artwork=bool(item["artwork"]),
                include_roms=item["action"] == "download", overwrite_files=False, overwrite_artwork=False,
                local_index_cache=index_cache, local_artwork_cache=artwork_cache, artwork_job_overwrite=False)
    except Exception:  # noqa: BLE001 - one bad item never stops the others
        return None
    ids = [str(job.get("job_id") or job.get("id")) for job in jobs if job.get("job_id") or job.get("id")]
    return ids or None
