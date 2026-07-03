"""ROM-metadata collection (scan + snapshot) for Overmind actions.

Extracted from ``drone_api.py``. ``_collect_rom_metadata`` runs a full ROM-metadata poll and
returns the upload snapshot; used by the ``collect_rom_metadata`` Overmind action.
"""

import sys
from datetime import datetime, timezone
from typing import Any

try:
    from ..common.settings import Settings
    from ..roms.rom_metadata_state import _build_rom_metadata_snapshot_from_cache
    from ..storage.rom_metadata_store import _load_rom_metadata_cache
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from roms.rom_metadata_state import _build_rom_metadata_snapshot_from_cache  # type: ignore
    from storage.rom_metadata_store import _load_rom_metadata_cache  # type: ignore
def _collect_rom_metadata(settings: Settings, repository: "RomRepository") -> dict:
    cache, _ = _load_rom_metadata_cache(settings)
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    bios_entries = cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {}
    artwork_entries = cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {}
    if entries or bios_entries or artwork_entries:
        result = _build_rom_metadata_snapshot_from_cache(settings, cache, rehydrate_gamelist=True)
        print(
            f"Asset metadata collected from local database: systems={len(result.get('systems') or [])} roms={len(result.get('roms') or [])} bios={len(result.get('bios') or [])} artwork={len(result.get('artwork') or [])}",
            file=sys.stdout,
            flush=True,
        )
        return result

    try:
        system_names = repository.list_system_names()
    except FileNotFoundError:
        system_names = []
    try:
        bios = repository.list_bios_entries()
    except FileNotFoundError:
        bios = []
    try:
        artwork = repository.list_artwork_metadata()
    except Exception:
        artwork = []
    roms = []
    systems = []
    gamelists = []
    for system_name in system_names:
        system_name = str(system_name or "").strip()
        if not system_name:
            continue
        try:
            system_dir = repository.get_system_dir(system_name)
            gamelist, system_roms = repository.list_gamelist_rom_metadata(system_name, system_dir)
        except Exception as error:
            roms.append({"system": system_name, "error": str(error)})
            continue
        gamelists.append(gamelist)
        if system_roms:
            systems.append({"name": system_name, "rom_count": len(system_roms)})
        for rom in system_roms:
            item = dict(rom)
            item["system"] = system_name
            item["system_name"] = system_name
            roms.append(item)
    result = {
        "type": "asset_metadata",
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "roms_root": str(settings.roms_root),
        "bios_root": str(settings.bios_root),
        "systems": systems,
        "roms": roms,
        "bios": bios,
        "artwork": artwork,
        "gamelists": gamelists,
    }
    print(
        f"Asset metadata scan root={settings.roms_root} systems={len(systems)} roms={len(roms)} bios={len(bios)} artwork={len(artwork)} source=database_or_filesystem",
        file=sys.stdout,
        flush=True,
    )
    return result


# ROM/BIOS inventory keys + fingerprints (_rom_cache_entry_key, _rom_inventory_fingerprint,
# _bios_inventory_fingerprint, ...) now live in roms/rom_inventory.py (re-exported above).


# Heartbeat-driven resync-request + thumbprint helpers now live in overmind/heartbeat_sync.py (re-exported below).


# ROM-metadata cache-state helpers (snapshot/upload-clean/status/activity) now live in roms/rom_metadata_state.py (re-exported below).


# ROM-metadata scan + fingerprint hashing now live in roms/rom_scanner.py (re-exported below).
