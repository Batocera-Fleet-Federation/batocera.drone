"""ROM-metadata filesystem scan + sampled-fingerprint hashing.

Extracted from ``drone_api.py``. ``_poll_rom_metadata_cache`` walks the ROM/BIOS/artwork
trees (via the passed ``repository``), reconciles them against the SQLite cache, and
persists the delta; ``_hash_rom_metadata_batches`` fills in sampled fingerprints in
time-budgeted batches. Both take the ``RomRepository`` as a parameter (query object).
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from ..common.logging_setup import _drone_log
    from ..common.settings import Settings
    from ..common.http_errors import _format_http_error
    from ..storage.rom_metadata_store import (
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_preserved_asset_fingerprint,
    )
    from ..storage import saves_store as _saves_store
    from .gamelist import _database_rom_metadata_fields
    from .rom_asset_bios import bios_systems_for_md5
    from .rom_inventory import _bios_cache_entry_key, _rom_cache_entry_key, _wire_rom_rows
    from .rom_metadata_state import (
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logging_setup import _drone_log  # type: ignore
    from common.settings import Settings  # type: ignore
    from common.http_errors import _format_http_error  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        _load_rom_metadata_cache,
        _persist_rom_metadata_cache,
        _read_preserved_asset_fingerprint,
    )
    from storage import saves_store as _saves_store  # type: ignore
    from roms.gamelist import _database_rom_metadata_fields  # type: ignore
    from roms.rom_asset_bios import bios_systems_for_md5  # type: ignore
    from roms.rom_inventory import _bios_cache_entry_key, _rom_cache_entry_key, _wire_rom_rows  # type: ignore
    from roms.rom_metadata_state import (  # type: ignore
        _begin_rom_metadata_activity,
        _build_rom_metadata_snapshot_from_cache,
        _end_rom_metadata_activity,
        _mark_rom_metadata_upload_clean,
    )

# Local copies of the scan/hash tuning knobs (drone_api keeps its own copies for the
# poller bootstrap + rom_metadata_state still resident/using them); all read the same env.
ROM_METADATA_PROGRESS_SECONDS = float(os.environ.get("ROM_METADATA_PROGRESS_SECONDS", "30"))
# ROM_METADATA_PROGRESS_FILES stays the single source in drone_api (tests patch it there);
# lazy-imported inside the scan/hash fns below so those patches take effect.
ROM_METADATA_FINGERPRINT_BATCH_SIZE = max(1, int(os.environ.get("ROM_METADATA_FINGERPRINT_BATCH_SIZE", "250")))
ROM_METADATA_HASH_BUDGET_SECONDS = max(0.0, float(os.environ.get("ROM_METADATA_HASH_BUDGET_SECONDS", "120")))
# Version of the ROM classification rules (the folder-unit table + resolution in
# rom_transfer_unit). The per-system gamelist.xml MD5 gate skips unchanged systems, so
# a rule change would otherwise never re-classify existing entries -- bump this to
# force a one-time full re-index on the next poll. v2: folder-unit ROMs (marker file in
# a per-game top-level folder) replace the immediate-parent resolution.
ROM_CLASSIFIER_VERSION = 2
# ROM_METADATA_HASH_ROMS_ENABLED stays the single source in drone_api (tests patch it there
# + rom_metadata_state reads it); lazy-imported inside _hash_rom_metadata_batches below.


def _poll_rom_metadata_cache(settings: Settings, repository: "RomRepository") -> Tuple[dict, bool, dict]:
    # RomRepository stays in drone_api (Phase 4 will move it); lazy-import to avoid a cycle.
    try:
        from ..drone_api import ROM_METADATA_PROGRESS_FILES, RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ROM_METADATA_PROGRESS_FILES, RomRepository  # type: ignore
    started = time.monotonic()
    _drone_log("Asset metadata poll started: phase=cache_load")
    cache_load_started = time.monotonic()
    cache, rebuilt = _load_rom_metadata_cache(settings)
    was_dirty = bool(cache.get("dirty"))
    resuming_scan = bool(cache.get("scan_in_progress"))
    existing_entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    existing_bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    existing_artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    # fingerprint snapshot kept by a cache purge so a clean rebuild does not re-hash files.
    preserved_fingerprint = _read_preserved_asset_fingerprint(settings)
    preserved_rom_fingerprint = preserved_fingerprint.get("rom") or {}
    preserved_bios_md5 = preserved_fingerprint.get("bios") or {}
    print(
        f"Asset metadata cache load completed: entries={len(existing_entries)} bios_entries={len(existing_bios_entries)} artwork_entries={len(existing_artwork_entries)} duration_ms={int((time.monotonic() - cache_load_started) * 1000)}",
        file=sys.stdout,
        flush=True,
    )
    previous_keys = set(existing_entries.keys())
    previous_bios_keys = set(existing_bios_entries.keys())
    previous_artwork_keys = set(existing_artwork_entries.keys())
    next_entries: Dict[str, dict] = {}
    next_bios_entries: Dict[str, dict] = {}
    next_artwork_entries: Dict[str, dict] = {}
    persisted_entries = dict(existing_entries)
    persisted_bios_entries = dict(existing_bios_entries)
    new_or_changed: List[Tuple[str, Path, dict]] = []
    bios_new_or_changed: List[Tuple[str, Path, dict]] = []
    systems_scanned = 0
    discovered = 0
    bios_discovered = 0
    artwork_discovered = 0
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    last_checkpoint = started

    def checkpoint_scan(phase: str, *, force: bool = False) -> None:
        nonlocal last_checkpoint
        has_new_work = bool(new_or_changed) or bool(bios_new_or_changed)
        if not (rebuilt or resuming_scan or has_new_work):
            return
        processed = discovered + bios_discovered
        now = time.monotonic()
        if (
            not force
            and processed % max(1, ROM_METADATA_PROGRESS_FILES) != 0
            and now - last_checkpoint < ROM_METADATA_PROGRESS_SECONDS
        ):
            return
        cache["entries"] = {**existing_entries, **next_entries}
        cache["bios_entries"] = {**existing_bios_entries, **next_bios_entries}
        cache["systems"] = systems
        cache["gamelists"] = gamelists
        cache["dirty"] = True
        cache["scan_in_progress"] = True
        cache["scan_checkpoint_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        rom_updates = {
            key: value for key, value in next_entries.items()
            if persisted_entries.get(key) != value
        }
        bios_updates = {
            key: value for key, value in next_bios_entries.items()
            if persisted_bios_entries.get(key) != value
        }
        _persist_rom_metadata_cache(
            settings,
            cache,
            rom_updates=rom_updates,
            bios_updates=bios_updates,
        )
        persisted_entries.update(rom_updates)
        persisted_bios_entries.update(bios_updates)
        print(
            f"Asset metadata checkpoint saved: phase={phase} roms={len(next_entries)} bios={len(next_bios_entries)}",
            file=sys.stdout,
            flush=True,
        )
        last_checkpoint = now

    _drone_log("Asset metadata poll phase=scan")
    # A classification-rule change must re-index every system once, even when no
    # gamelist.xml changed (the MD5 gate below would otherwise carry stale rows
    # forward forever). The stored version is only stamped after a COMPLETE pass, so
    # an interrupted re-index resumes on the next poll.
    classifier_stale = str(cache.get("rom_classifier_version") or "") != str(ROM_CLASSIFIER_VERSION)
    if classifier_stale:
        print(
            f"ROM metadata classifier version changed: stored={cache.get('rom_classifier_version')!r} "
            f"current={ROM_CLASSIFIER_VERSION} -> re-indexing every system this poll",
            file=sys.stdout,
            flush=True,
        )
    try:
        system_names = repository.list_system_names()
    except FileNotFoundError:
        system_names = []
    # Per-system gamelist.xml MD5 recorded on the previous poll -- the change signal.
    stored_gamelist_md5 = {
        str(entry.get("system") or entry.get("system_name") or "").strip().lower(): str(entry.get("gamelist_md5") or "")
        for entry in (cache.get("gamelists") if isinstance(cache.get("gamelists"), list) else [])
        if isinstance(entry, dict)
    }
    # Group cached ROM entries by system so an unchanged system can be carried forward
    # whole (no per-file stat/hash) when its gamelist.xml MD5 has not changed.
    existing_by_system: Dict[str, Dict[str, dict]] = {}
    for entry_key, entry in existing_entries.items():
        sys_key = str((entry or {}).get("system") or "").strip().lower() or entry_key.split(":", 1)[0]
        existing_by_system.setdefault(sys_key, {})[entry_key] = entry
    systems = []
    gamelists = []
    for system_name in system_names:
        system_name = str(system_name or "").strip()
        if not system_name:
            continue
        systems_scanned += 1
        sys_key = system_name.lower()
        try:
            system_dir = repository.get_system_dir(system_name)
        except Exception as error:
            print(f"ROM metadata scan warning: system={system_name} error={_format_http_error(error)}", file=sys.stderr, flush=True)
            continue
        gamelist_file = system_dir / "gamelist.xml"
        try:
            gamelist_bytes = gamelist_file.read_bytes() if gamelist_file.is_file() else None
        except OSError:
            gamelist_bytes = None
        # Strict gamelist-as-source-of-truth: a system with no gamelist.xml reports zero
        # games. Its cached rows are intentionally dropped from next_entries (-> deleted).
        if gamelist_bytes is None:
            continue
        current_md5 = hashlib.md5(gamelist_bytes).hexdigest()
        try:
            gamelist_stat = gamelist_file.stat()
        except OSError:
            gamelist_stat = None
        system_rom_count = 0
        if not classifier_stale and current_md5 == stored_gamelist_md5.get(sys_key) and sys_key in existing_by_system:
            # Unchanged gamelist.xml -> carry this system's cached rows forward untouched.
            # This is the whole point of the MD5 gate: no directory walk, stat, or hash.
            for entry_key, entry in existing_by_system[sys_key].items():
                next_entries[entry_key] = entry
                system_rom_count += 1
        else:
            # New or changed gamelist.xml -> re-index this system from gamelist.xml.
            try:
                _, roms = repository.list_gamelist_rom_metadata(system_name, system_dir)
            except Exception as error:
                print(f"ROM metadata scan warning: system={system_name} error={_format_http_error(error)}", file=sys.stderr, flush=True)
                continue
            for rom in roms:
                file_path = str(rom.get("file_path") or rom.get("relative_path") or rom.get("rom_path") or rom.get("rom_file") or "").strip()
                absolute_path = str(rom.get("absolute_path") or "").strip()
                if not file_path or not absolute_path:
                    continue
                absolute = Path(absolute_path)
                entry_type = str(rom.get("entry_type") or "file").strip().lower()
                discovered += 1
                system_rom_count += 1
                key = _rom_cache_entry_key(system_name, file_path)
                stat_size = int(rom.get("file_size") or rom.get("byte_count") or absolute.stat().st_size)
                stat_mtime = int(rom.get("modified_time") or rom.get("mtime") or absolute.stat().st_mtime)
                previous = existing_entries.get(key) if isinstance(existing_entries.get(key), dict) else {}
                base_entry = _database_rom_metadata_fields(rom, system_name, file_path, absolute, stat_size, stat_mtime)
                previous_fingerprint = (previous.get("rom_fingerprint") or previous.get("fingerprint")) if previous else None
                reuse_fingerprint = None
                if previous and previous.get("file_size") == stat_size and previous_fingerprint:
                    reuse_fingerprint = previous_fingerprint
                else:
                    # After a purge the entry rows are gone; reuse fingerprint from the
                    # snapshot for files whose size and mtime are unchanged.
                    kept = preserved_rom_fingerprint.get(key)
                    if kept and kept.get("fingerprint") and kept.get("file_size") == stat_size and kept.get("modified_time") == stat_mtime:
                        reuse_fingerprint = kept["fingerprint"]
                if reuse_fingerprint:
                    next_entries[key] = dict(base_entry)
                    next_entries[key].update({"fingerprint": reuse_fingerprint, "rom_fingerprint": reuse_fingerprint})
                else:
                    # No reusable fingerprint (new/changed file) -> queue it for hashing.
                    # True directory entries are never hashed and carry forward without a
                    # fingerprint; folder-unit ROMs (entry_type "folder" but absolute_path
                    # is the marker file) still hash the marker so identity keeps working.
                    next_entries[key] = base_entry
                    if absolute.is_file():
                        new_or_changed.append((key, absolute, base_entry))
                checkpoint_scan("rom_scan")
        if system_rom_count:
            systems.append({"name": system_name, "rom_count": system_rom_count})
        # Always record the gamelist (with its MD5) so the change gate works next poll,
        # even for an empty gamelist (0 games) -- otherwise it would re-index every poll.
        gamelists.append(
            {
                "system": system_name,
                "system_name": system_name,
                "path": str(gamelist_file),
                "file_path": str(gamelist_file),
                "exists": True,
                "rom_count": system_rom_count,
                "gamelist_md5": current_md5,
                "file_size": int(gamelist_stat.st_size) if gamelist_stat else len(gamelist_bytes),
                "modified_time": int(gamelist_stat.st_mtime) if gamelist_stat else 0,
            }
        )

    checkpoint_scan("rom_scan_complete", force=bool(discovered))
    deleted = previous_keys - set(next_entries.keys())
    try:
        bios_root = repository.get_bios_root()
        for bios_path in sorted(bios_root.rglob("*"), key=lambda item: str(item.relative_to(bios_root)).lower()):
            if not bios_path.is_file():
                continue
            relative_path = bios_path.relative_to(bios_root).as_posix()
            bios_discovered += 1
            key = _bios_cache_entry_key(relative_path)
            stat = bios_path.stat()
            stat_size = int(stat.st_size)
            stat_mtime = int(stat.st_mtime)
            previous = existing_bios_entries.get(key) if isinstance(existing_bios_entries.get(key), dict) else {}
            base_entry = {
                "entry_type": "file",
                "name": bios_path.name,
                "path": relative_path,
                "file_path": relative_path,
                "relative_path": relative_path,
                "unique_id": repository.build_unique_id(bios_path),
                "file_size": stat_size,
                "byte_count": stat_size,
                "size": stat_size,
                "modified_time": stat_mtime,
                "mtime": stat_mtime,
                "absolute_path": str(bios_path),
            }
            reuse_bios_md5 = None
            if previous and previous.get("file_size") == stat_size and previous.get("modified_time") == stat_mtime and previous.get("md5"):
                reuse_bios_md5 = previous.get("md5")
            else:
                kept = preserved_bios_md5.get(key)
                if kept and kept.get("md5") and kept.get("file_size") == stat_size and kept.get("modified_time") == stat_mtime:
                    reuse_bios_md5 = kept["md5"]
            if reuse_bios_md5:
                next_bios_entries[key] = {
                    **base_entry,
                    "md5": reuse_bios_md5,
                    "bios_md5": (previous.get("bios_md5") if previous else None) or reuse_bios_md5,
                    "systems": bios_systems_for_md5(reuse_bios_md5),
                }
            else:
                next_bios_entries[key] = base_entry
                bios_new_or_changed.append((key, bios_path, base_entry))
            checkpoint_scan("bios_scan")
    except FileNotFoundError:
        pass
    except Exception as error:
        print(f"BIOS metadata scan warning: error={_format_http_error(error)}", file=sys.stderr, flush=True)
    checkpoint_scan("bios_scan_complete", force=bool(bios_discovered))
    bios_deleted = previous_bios_keys - set(next_bios_entries.keys()) - {key for key, _, _ in bios_new_or_changed}
    # Artwork is no longer collected into the metadata cache. Artwork is resolved live
    # from gamelist.xml only at peer-to-peer transfer time (see rom_artwork_gamelist), so
    # the poll never scans it. Leaving next_artwork_entries empty purges any previously-
    # cached artwork rows through the normal delete path below.
    artwork_deleted = previous_artwork_keys - set(next_artwork_entries.keys())
    artwork_changed = next_artwork_entries != existing_artwork_entries
    print(
        f"Asset metadata poll scan complete: systems={systems_scanned} roms={discovered} bios={bios_discovered} artwork={artwork_discovered} new_or_changed={len(new_or_changed)} bios_new_or_changed={len(bios_new_or_changed)} deleted={len(deleted)} bios_deleted={len(bios_deleted)} artwork_deleted={len(artwork_deleted)}",
        file=sys.stdout,
        flush=True,
    )

    if bios_new_or_changed:
        hash_started = time.monotonic()
        last_log = hash_started
        print(f"BIOS metadata poll phase=md5_hashing count={len(bios_new_or_changed)}", file=sys.stdout, flush=True)
        for bios_index, (key, absolute, entry) in enumerate(bios_new_or_changed, start=1):
            # BIOS uses a full-file MD5 (exact emulator identity), not the sampled ROM fingerprint.
            md5_value = RomRepository.build_md5(absolute)
            next_bios_entries[key] = {
                **entry,
                "md5": md5_value,
                "bios_md5": md5_value,
                "systems": bios_systems_for_md5(md5_value),
            }
            now = time.monotonic()
            if bios_index == len(bios_new_or_changed) or bios_index % max(1, ROM_METADATA_PROGRESS_FILES) == 0 or now - last_log >= ROM_METADATA_PROGRESS_SECONDS:
                checkpoint_scan("bios_md5", force=True)
                print(f"BIOS metadata md5 progress: {bios_index}/{len(bios_new_or_changed)} files", file=sys.stdout, flush=True)
                last_log = now
        print(
            f"BIOS metadata md5 hashing completed: count={len(bios_new_or_changed)} duration_ms={int((time.monotonic() - hash_started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )

    rom_metadata_changed = next_entries != existing_entries
    gamelists_changed = gamelists != (cache.get("gamelists") if isinstance(cache.get("gamelists"), list) else [])
    systems_changed = systems != (cache.get("systems") if isinstance(cache.get("systems"), list) else [])
    changed = (
        rebuilt
        or bool(new_or_changed)
        or bool(deleted)
        or rom_metadata_changed
        or systems_changed
        or gamelists_changed
        or bool(bios_new_or_changed)
        or bool(bios_deleted)
        or artwork_changed
        or was_dirty
    )
    cache["entries"] = next_entries
    cache["bios_entries"] = next_bios_entries
    cache["artwork_entries"] = next_artwork_entries
    cache["systems"] = systems
    cache["gamelists"] = gamelists
    cache["last_full_scan_at"] = now_iso
    cache["dirty"] = changed
    cache["scan_in_progress"] = False
    # Stamp the classifier version only on a completed pass (see classifier_stale above).
    cache["rom_classifier_version"] = ROM_CLASSIFIER_VERSION
    _drone_log("Asset metadata poll phase=cache_write")
    cache_write_started = time.monotonic()
    rom_updates = {
        key: value for key, value in next_entries.items()
        if persisted_entries.get(key) != value
    }
    bios_updates = {
        key: value for key, value in next_bios_entries.items()
        if persisted_bios_entries.get(key) != value
    }
    artwork_updates = {
        key: value for key, value in next_artwork_entries.items()
        if existing_artwork_entries.get(key) != value
    }
    _persist_rom_metadata_cache(
        settings,
        cache,
        rom_updates=rom_updates,
        bios_updates=bios_updates,
        artwork_updates=artwork_updates,
        rom_deletes=set(persisted_entries) - set(next_entries),
        bios_deletes=set(persisted_bios_entries) - set(next_bios_entries),
        artwork_deletes=artwork_deleted,
        rom_deleted_rows={key: existing_entries[key] for key in deleted if key in existing_entries},
        bios_deleted_rows={key: existing_bios_entries[key] for key in bios_deleted if key in existing_bios_entries},
        artwork_deleted_rows={key: existing_artwork_entries[key] for key in artwork_deleted if key in existing_artwork_entries},
    )
    print(
        f"Asset metadata cache write completed: entries={len(next_entries)} bios_entries={len(next_bios_entries)} artwork_entries={len(next_artwork_entries)} changed={changed} write_duration_ms={int((time.monotonic() - cache_write_started) * 1000)} total_poll_duration_ms={int((time.monotonic() - started) * 1000)}",
        file=sys.stdout,
        flush=True,
    )
    stats = {
        "systems_scanned": systems_scanned,
        "roms_discovered": discovered,
        "bios_discovered": bios_discovered,
        "artwork_discovered": artwork_discovered,
        "new_or_changed": len(new_or_changed),
        "roms_pending_fingerprint": len(new_or_changed),
        "bios_new_or_changed": len(bios_new_or_changed),
        "deleted": len(deleted),
        "bios_deleted": len(bios_deleted),
        "artwork_deleted": len(artwork_deleted),
        "artwork_changed": artwork_changed,
        "rebuilt": rebuilt,
        "had_cached_assets": bool(existing_entries or existing_bios_entries or existing_artwork_entries),
        "had_successful_upload": bool(cache.get("last_successful_upload_at")),
        "full_refresh_pending": bool(cache.get("full_refresh_pending")),
    }
    return _build_rom_metadata_snapshot_from_cache(settings, cache), changed, stats


def _hash_rom_metadata_batches(settings: Settings, repository: "RomRepository", batch_size: int = ROM_METADATA_FINGERPRINT_BATCH_SIZE):
    """Yield bounded hash patches for ROM entries missing a current fingerprint."""
    try:
        from ..drone_api import ROM_METADATA_HASH_ROMS_ENABLED, ROM_METADATA_PROGRESS_FILES
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ROM_METADATA_HASH_ROMS_ENABLED, ROM_METADATA_PROGRESS_FILES  # type: ignore
    if not ROM_METADATA_HASH_ROMS_ENABLED:
        return
    cache, _ = _load_rom_metadata_cache(settings)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    pending = [
        (key, entry)
        for key, entry in entries.items()
        if isinstance(entry, dict) and not entry.get("rom_fingerprint") and entry.get("absolute_path")
    ]
    total = len(pending)
    if not total:
        return
    batch_size = max(1, int(batch_size))
    started = time.monotonic()
    last_checkpoint = started
    budget_seconds = ROM_METADATA_HASH_BUDGET_SECONDS
    print(f"ROM metadata phase=fingerprint_hashing count={total} batch_size={batch_size} budget_seconds={budget_seconds}", file=sys.stdout, flush=True)
    patch = []
    pending_updates = {}
    budget_exhausted = False
    hashed = 0
    for processed, (key, entry) in enumerate(pending, start=1):
        if budget_seconds and (time.monotonic() - started) >= budget_seconds:
            # Stop starting new files once the per-poll budget is spent; flush any
            # accumulated patch/updates below, then resume on the next poll.
            budget_exhausted = True
            break
        absolute = Path(str(entry.get("absolute_path") or ""))
        if absolute.exists() and absolute.is_file():
            fingerprint_value = repository.build_fingerprint(absolute)
            updated = {**entry, "fingerprint": fingerprint_value, "rom_fingerprint": fingerprint_value}
            entries[key] = updated
            pending_updates[key] = updated
            # Slim wire shape: a rom_hash_patch update only carries the fingerprint
            # fields keyed by (system, gamelist_id), not the full ROM record.
            patch.append(_wire_rom_rows([updated])[0])
            hashed += 1
        now = time.monotonic()
        checkpoint_due = (
            bool(patch)
            and (
                processed == total
                or processed % max(1, ROM_METADATA_PROGRESS_FILES) == 0
                or now - last_checkpoint >= ROM_METADATA_PROGRESS_SECONDS
            )
        )
        if checkpoint_due:
            cache["entries"] = entries
            cache["dirty"] = True
            _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
            pending_updates = {}
            print(f"ROM metadata fingerprint checkpoint: {processed}/{total} files hashed", file=sys.stdout, flush=True)
            last_checkpoint = now
        if not patch or (len(patch) < batch_size and processed != total):
            continue
        if not checkpoint_due:
            cache["entries"] = entries
            cache["dirty"] = True
            _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
            pending_updates = {}
        print(f"ROM metadata fingerprint progress: {processed}/{total} files", file=sys.stdout, flush=True)
        yield {
            "type": "asset_metadata",
            "update_mode": "rom_hash_patch",
            "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "roms": patch,
            "hash_progress": {"processed": processed, "total": total, "complete": processed == total},
        }
        patch = []
    # Flush any work accumulated before an early budget break (the in-loop yield only
    # fires on a full batch or on the final file, neither of which is reached on break).
    if pending_updates:
        cache["entries"] = entries
        cache["dirty"] = True
        _persist_rom_metadata_cache(settings, cache, rom_updates=pending_updates)
        pending_updates = {}
    if patch:
        yield {
            "type": "asset_metadata",
            "update_mode": "rom_hash_patch",
            "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "roms": patch,
            # complete only when we exhausted the pending list, not on a budget break,
            # so a caller does not treat the inventory fingerprint as clean prematurely.
            "hash_progress": {"processed": hashed, "total": total, "complete": not budget_exhausted},
        }
        patch = []
    if budget_exhausted:
        print(
            f"ROM metadata fingerprint hashing paused (budget {budget_seconds}s reached): "
            f"hashed={hashed} remaining≈{total - hashed}/{total} resume on next poll "
            f"duration_ms={int((time.monotonic() - started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )
    else:
        print(
            f"ROM metadata fingerprint hashing completed: count={total} duration_ms={int((time.monotonic() - started) * 1000)}",
            file=sys.stdout,
            flush=True,
        )


def _complete_local_rom_metadata_cache(settings: Settings, repository: "RomRepository", reason: str) -> dict:
    # ROM_METADATA_FINGERPRINT_BATCH_SIZE stays single-source in drone_api (tests patch it); lazy-import.
    try:
        from ..drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ROM_METADATA_FINGERPRINT_BATCH_SIZE  # type: ignore
    hash_batches = 0
    hashed_roms = 0
    for patch in _hash_rom_metadata_batches(settings, repository, batch_size=ROM_METADATA_FINGERPRINT_BATCH_SIZE):
        hash_batches += 1
        hashed_roms += len(patch.get("roms") or [])
    print(
        f"Asset metadata cached locally: reason={reason} hash_batches={hash_batches} hashed_roms={hashed_roms}",
        file=sys.stdout,
        flush=True,
    )
    # There is no upload destination anymore -- a completed local scan+hash pass is
    # the whole lifecycle, so mark the cache clean here (was previously only marked
    # clean after a successful Overmind upload).
    cache, _ = _load_rom_metadata_cache(settings)
    snapshot = _build_rom_metadata_snapshot_from_cache(settings, cache)
    _mark_rom_metadata_upload_clean(
        settings,
        fingerprint=snapshot.get("rom_inventory_fingerprint"),
        bios_thumbprint=snapshot.get("bios_files_thumbprint"),
    )
    return {
        "status": "cached",
        "reason": reason,
        "hash_batches": hash_batches,
        "hashed_roms": hashed_roms,
    }


def _poll_rom_metadata_once(settings: Settings, repository: "RomRepository") -> dict:
    """One scan+hash+local-cache cycle: filesystem scan, then saves cache scan.

    This fleet has no central hub to upload to -- ROM/BIOS metadata is scanned and
    cached locally, then served to paired peers on request (see transfer/peer_download.py).
    """
    if not _begin_rom_metadata_activity("poll"):
        return {"status": "skipped", "reason": "metadata_already_running", "changed": False}
    try:
        _poll_rom_metadata_cache(settings, repository)
        try:
            _saves_store.sync_saves_cache(settings.saves_root)
        except Exception as error:
            print(f"Local saves cache scan failed: {_format_http_error(error)}", file=sys.stderr, flush=True)
        return _complete_local_rom_metadata_cache(settings, repository, "scan_complete")
    finally:
        _end_rom_metadata_activity()
