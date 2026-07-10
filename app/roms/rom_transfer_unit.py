"""Folder-unit ROM transfer resolution.

Some Batocera systems store one game as a FOLDER of many files while gamelist.xml's
``<path>`` points at a small marker/index file inside it (Sega Lindbergh's ``.game``
marker, disc systems' ``.gdi``/``.cue`` next to their ``.bin`` tracks). Transferring
only that marker file breaks the game on the receiving drone, so for a curated set of
systems (``data/folder_unit_systems.json``, vendored like ``bios_system_map.json``) a
file-in-subfolder gamelist entry resolves to its immediate parent folder as the
transfer unit. Systems NOT in the table (e.g. c64/scummvm sets that organize
single-file games into shared category folders) keep plain single-file behavior, and
a guard skips folders holding many gamelist entries -- a category folder inside a
listed system must not be claimed wholesale by one of its games. The gamelist entry's
identity (relative path, fingerprint) stays the marker file; only the transferred
bytes widen to the folder.
"""

import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional

try:
    from .gamelist import _normalize_gamelist_rom_path, _text_or_empty
except ImportError:  # pragma: no cover - direct script execution fallback
    from roms.gamelist import _normalize_gamelist_rom_path, _text_or_empty  # type: ignore


_FOLDER_UNIT_SYSTEMS_PATH = Path(__file__).resolve().parent / "data" / "folder_unit_systems.json"
_FOLDER_UNIT_SYSTEMS: Optional[frozenset] = None

# A per-game folder holds one game (or one game's discs); more gamelist entries than
# this in a single folder means it is a category folder, not a game folder.
FOLDER_UNIT_MAX_ENTRIES = max(1, int(os.environ.get("ROM_FOLDER_UNIT_MAX_ENTRIES", "8")))


def folder_unit_systems() -> frozenset:
    """Load the vendored folder-unit system list once (see
    ``data/folder_unit_systems.json`` for provenance). Missing/corrupt file degrades to
    an empty set (every system keeps single-file behavior) rather than failing a scan."""
    global _FOLDER_UNIT_SYSTEMS
    if _FOLDER_UNIT_SYSTEMS is None:
        try:
            with _FOLDER_UNIT_SYSTEMS_PATH.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            systems = data.get("folder_unit_systems")
            _FOLDER_UNIT_SYSTEMS = frozenset(
                str(value).strip().lower() for value in systems if str(value or "").strip()
            ) if isinstance(systems, list) else frozenset()
        except Exception:
            _FOLDER_UNIT_SYSTEMS = frozenset()
    return _FOLDER_UNIT_SYSTEMS


def gamelist_folder_entry_counts(root) -> Dict[str, int]:
    """One pass over an already-parsed gamelist.xml root: normalized TOP-LEVEL folder
    (the first path segment, lowercased; ``"."`` for files at the system root) -> count
    of distinct ``<game>`` entries anywhere under it. The transfer unit is the top-level
    per-game folder, so the guard counts everything that folder contains -- a multi-disc
    game folder stays small while a region/category bucket holding many games exceeds
    the limit. Both the scan and the serving-side resolver call this on the root they
    already hold, so the folder-unit guard is computed from identical inputs on both
    sides."""
    counts: Dict[str, int] = {}
    seen = set()
    for game in root.findall("game"):
        relative_path = _normalize_gamelist_rom_path(_text_or_empty(game, "path"))
        if not relative_path:
            continue
        normalized_key = relative_path.lower()
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        parts = PurePosixPath(relative_path).parts
        top = parts[0].lower() if len(parts) > 1 else "."
        counts[top] = counts.get(top, 0) + 1
    return counts


def resolve_transfer_unit(
    system: str,
    relative_path: str,
    rom_path: Path,
    system_dir: Path,
    folder_entry_counts: Mapping[str, int],
) -> Optional[dict]:
    """Resolve a gamelist entry to its folder transfer unit, or ``None`` to keep
    today's single-file behavior. Returns ``{"unit_rel_path", "unit_dir",
    "marker_rel_path"}`` where ``unit_dir`` is the entry's TOP-LEVEL folder under the
    system root -- the whole per-game folder, not the marker's immediate parent.
    (Lindbergh nests the marker: ``hotd4a/elf/hotd4a.game`` sits beside the game's
    ``fs/`` data dir, so the unit must be ``hotd4a``.)

    Folds only when ALL hold: the system is in the vendored table; the entry is a real
    file inside a subfolder of the system dir; and the top-level folder holds at most
    ``FOLDER_UNIT_MAX_ENTRIES`` gamelist entries (multi-disc folders pass, category
    folders do not)."""
    if str(system or "").strip().lower() not in folder_unit_systems():
        return None
    relative = PurePosixPath(str(relative_path or ""))
    parts = relative.parts
    if len(parts) < 2 or not str(relative.name):
        return None
    if not rom_path.is_file():
        return None
    top = parts[0]
    unit_dir = (system_dir / top).resolve()
    try:
        unit_dir.relative_to(system_dir)
    except ValueError:
        return None
    if unit_dir == system_dir or not unit_dir.is_dir():
        return None
    entry_count = int(folder_entry_counts.get(top.lower(), 1) or 1)
    if entry_count > FOLDER_UNIT_MAX_ENTRIES:
        print(
            f"ROM folder-unit fallback: system={system} folder={top!r} holds "
            f"{entry_count} gamelist entries (> {FOLDER_UNIT_MAX_ENTRIES}); keeping single-file behavior",
            file=sys.stderr,
            flush=True,
        )
        return None
    return {
        "unit_rel_path": top,
        "unit_dir": unit_dir,
        "marker_rel_path": relative.as_posix(),
    }
