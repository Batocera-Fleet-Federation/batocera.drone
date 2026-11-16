"""Which userdata files a backup can hold, and which restorable category each belongs to.

A category is the unit a user picks when creating or restoring a backup:

* ``batocera``                -- ``system/batocera.conf`` (secrets removed, merged on restore)
* ``emulationstation``        -- ``system/configs/emulationstation/**`` (settings, collections, ...)
* ``controllers``             -- ``es_input.cfg`` plus every RetroArch remap (``*.rmp``)
* ``retroarch``               -- ``system/configs/retroarch/**`` (cfg/opt/overrides + shader presets)
* ``emulator:<dir>``          -- one per other ``system/configs/<dir>`` (dolphin-emu, PCSX2, ...)
* ``configs-misc``            -- loose files directly in ``system/configs``
* ``scripts``                 -- ``system/services``, ``custom.sh*``, ``pro-custom.sh``, custom script dirs
* ``gamelists``               -- ``roms/<system>/gamelist.xml``
* ``cheats``                  -- ``/userdata/cheats/**``
* ``saves:<system>``          -- in-game saves under ``saves/<system>``
* ``states:<system>``         -- save states (and their thumbnails) under ``saves/<system>``

``system/configs`` is not "just settings" on a real device: emulators keep firmware,
NAND trees, shader caches and their own UI resources there. Configuration is selected by
an extension allowlist, so anything an emulator adds later is excluded by default. Saves
keep every extension but drop caches, disk images and emulated OS partitions.

Walks never follow symbolic links; links are reported as skipped. The archive path of a
file is ``<root>/<relative path>`` where root is ``system``, ``roms``, ``saves`` or ``cheats``.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Set, Tuple

MAX_CONFIG_FILE_BYTES = 20 * 1024 * 1024
MAX_SAVE_FILE_BYTES = 512 * 1024 * 1024

ROOT_NAMES = ("system", "roms", "saves", "cheats")

# Human-editable settings formats, plus RetroArch shader presets and remaps. Images,
# fonts, audio, executables, databases and extension-less firmware dumps are excluded.
CONFIG_TEXT_EXTENSIONS = frozenset({
    ".cfg", ".conf", ".config", ".ini", ".xml", ".json", ".yaml", ".yml", ".toml",
    ".properties", ".txt", ".cnf", ".list", ".opt", ".lpl", ".rmp",
    ".slangp", ".glslp", ".cgp",
})
SCRIPT_DIRS = ("custom", "custom-scripts", "scripts")
CACHE_DIR_NAMES = frozenset({"cache", "mesa_shader_cache", "shader_cache", "shadercache", "radv_builtin_shaders"})
# Rebuilt automatically by the emulator: MAME's software-list hash cache and the
# yuzu-family (yuzu/Eden/Citron/Sudachi) game list cache, which they also copy into saves.
CACHE_FILE_NAMES = frozenset({"mess_hash_cache.json", "game_metadata_cache.json"})
# Zero-content folder markers (Batocera's stock /userdata ships hundreds of .gitkeep
# files). They hold nothing and restoring them only recreates empty folders, so they
# are skipped silently -- not even listed as "left out".
PLACEHOLDER_NAMES = frozenset({".gitkeep", ".keep"})
# Only under system/configs/<emulator>/: the yuzu-family game list cache folder.
CONFIG_CACHE_DIR_NAMES = frozenset({"game_list"})
DISK_IMAGE_EXTENSIONS = frozenset({".qcow2", ".vdi", ".vmdk", ".img", ".iso"})
# Vita3K firmware/system partitions; real game saves live under ux0.
EMULATED_OS_DIR_NAMES = frozenset({"vs0", "sa0", "os0", "vd0", "pd0"})
# Save states: RetroArch .stateN/.state.auto, PCSX2 .p2s, PPSSPP .ppst, Dolphin .sNN, ...
STATE_SUFFIX = re.compile(r"\.(?:state\d*(?:\.auto)?|state\.auto|st\d+|ss\d+|ppst|p2s|zst\d*|s\d\d)$", re.I)
CATEGORY_ID = re.compile(r"^(?:batocera|emulationstation|controllers|retroarch|configs-misc|scripts|gamelists|cheats"
                         r"|(?:emulator|saves|states):[A-Za-z0-9][A-Za-z0-9._+\- ]{0,79})$")
# Never backed up: the Drone's own state and the staging folders of save imports/restores.
_PRIVATE_DIR_PREFIXES = (".drone-save-", ".drone-backup-")
_TOP_LEVEL_SAVES = "_top"

GROUP_LABELS = {
    "batocera": "Batocera",
    "emulationstation": "EmulationStation",
    "controllers": "Controllers",
    "retroarch": "RetroArch",
    "emulators": "Standalone emulators",
    "scripts": "Custom scripts",
    "gamelists": "Game lists",
    "cheats": "Cheats",
    "saves": "Saves",
    "states": "Save states",
}
GROUP_ORDER = tuple(GROUP_LABELS)


@dataclass(frozen=True)
class Roots:
    """The real directories archive roots map onto."""

    userdata_root: Path
    roms_root: Path
    saves_root: Path

    @classmethod
    def from_settings(cls, settings) -> "Roots":
        return cls(Path(settings.userdata_root), Path(settings.roms_root), Path(settings.saves_root))

    def path(self, root: str) -> Path:
        if root == "system":
            return self.userdata_root / "system"
        if root == "roms":
            return self.roms_root
        if root == "saves":
            return self.saves_root
        if root == "cheats":
            return self.userdata_root / "cheats"
        raise ValueError(f"unknown backup root {root!r}")


@dataclass
class FileEntry:
    arcpath: str
    category: str
    size: int
    absolute: Path


@dataclass
class Collection:
    files: List[FileEntry] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)

    @property
    def skipped_bytes(self) -> int:
        return sum(int(item.get("size") or 0) for item in self.skipped)


def valid_category(value) -> bool:
    return isinstance(value, str) and bool(CATEGORY_ID.match(value))


def category_group(category_id: str) -> str:
    head = category_id.split(":", 1)[0]
    if head == "emulator" or head == "configs-misc":
        return "emulators"
    return head


def category_label(category_id: str) -> str:
    head, _, tail = category_id.partition(":")
    if head == "emulator":
        return tail
    if head == "saves":
        return "Top-level saves" if tail == _TOP_LEVEL_SAVES else f"Saves: {tail}"
    if head == "states":
        return "Top-level states" if tail == _TOP_LEVEL_SAVES else f"States: {tail}"
    if head == "configs-misc":
        return "Other configs"
    return GROUP_LABELS.get(head, head)


def describe(category_id: str) -> dict:
    return {"id": category_id, "label": category_label(category_id), "group": category_group(category_id),
            "group_label": GROUP_LABELS[category_group(category_id)]}


def sort_key(category_id: str) -> Tuple[int, str]:
    group = category_group(category_id)
    return (GROUP_ORDER.index(group), category_label(category_id).lower())


def is_state_file(name: str) -> bool:
    if STATE_SUFFIX.search(name):
        return True
    # State thumbnails: game.state1.png, game.state.auto.png
    return name.lower().endswith(".png") and bool(STATE_SUFFIX.search(name[:-4]))


def classify(arcpath: str) -> Optional[str]:
    """The category of an archive path, or ``None`` when no category accepts it.

    Pure path logic shared by the collector, archive validation and restore, so a
    peer-supplied manifest cannot file a path under a category it does not belong to.
    """
    parts = PurePosixPath(arcpath).parts
    if len(parts) < 2:
        return None
    root, rest = parts[0], parts[1:]
    name = rest[-1]
    if root == "system":
        if rest == ("batocera.conf",):
            return "batocera"
        if rest[0] == "services" and len(rest) > 1:
            return "scripts"
        if rest[0] in SCRIPT_DIRS and len(rest) > 1:
            return "scripts"
        if len(rest) == 1 and (name.startswith("custom.sh") or name == "pro-custom.sh"):
            return "scripts"
        if rest[0] != "configs" or len(rest) < 2:
            return None
        if Path(name).suffix.lower() not in CONFIG_TEXT_EXTENSIONS:
            return None
        if name.lower().endswith(".rmp") or rest[1:] == ("emulationstation", "es_input.cfg"):
            return "controllers"
        if len(rest) == 2:
            return "configs-misc"
        top = rest[1]
        if top == "emulationstation":
            return "emulationstation"
        if top == "retroarch":
            return "retroarch"
        category = f"emulator:{top}"
        return category if valid_category(category) else None
    if root == "roms":
        return "gamelists" if len(rest) == 2 and name == "gamelist.xml" else None
    if root == "cheats":
        return "cheats"
    if root == "saves":
        system = rest[0] if len(rest) > 1 else _TOP_LEVEL_SAVES
        category = ("states:" if is_state_file(name) else "saves:") + system
        return category if valid_category(category) else None
    return None


def _excluded_reason(root: str, dir_parts: Tuple[str, ...], name: str) -> Optional[str]:
    lowered = [part.lower() for part in dir_parts]
    if any(part.startswith(_PRIVATE_DIR_PREFIXES) for part in lowered):
        return "Drone staging folder"
    if any(part in CACHE_DIR_NAMES for part in lowered):
        return "cache directory"
    if name.lower() in CACHE_FILE_NAMES:
        return "emulator cache file"
    if root == "system" and lowered[:1] == ["configs"] and any(part in CONFIG_CACHE_DIR_NAMES for part in lowered[2:3]):
        return "cache directory"
    if root == "saves":
        if any(part in EMULATED_OS_DIR_NAMES for part in lowered):
            return "emulated firmware/system partition"
        if Path(name).suffix.lower() in DISK_IMAGE_EXTENSIONS:
            return "disk image, not a save"
        if "plugins" in lowered and "data" in lowered:
            return "plugin reference data, not a save"
    return None


def _walk(base: Path) -> Iterable[Tuple[Tuple[str, ...], os.DirEntry]]:
    """Yield (directory parts relative to base, entry) without following links."""
    stack: List[Tuple[Path, Tuple[str, ...]]] = [(base, ())]
    while stack:
        directory, parts = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in reversed(entries):
            yield parts, entry
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), parts + (entry.name,)))
            except OSError:
                continue


def _root_wanted(root: str, wanted: Optional[Set[str]]) -> bool:
    if wanted is None:
        return True
    groups = {category_group(category) for category in wanted}
    return {
        "system": bool(groups & {"batocera", "emulationstation", "controllers", "retroarch", "emulators", "scripts"}),
        "roms": "gamelists" in groups,
        "saves": bool(groups & {"saves", "states"}),
        "cheats": "cheats" in groups,
    }[root]


def _subtree_wanted(root: str, parts: Tuple[str, ...], wanted: Optional[Set[str]]) -> bool:
    """Prune walks early: only descend into directories a wanted category can live in."""
    if wanted is None or not parts:
        return True
    if root == "saves":
        systems = {category.split(":", 1)[1] for category in wanted if category_group(category) in ("saves", "states")}
        return parts[0] in systems
    if root == "roms":
        return len(parts) == 1
    if root == "system":
        top = parts[0]
        if top in ("services",) + SCRIPT_DIRS:
            return "scripts" in wanted
        if top != "configs":
            return False
        if len(parts) == 1:
            return True
        if "controllers" in wanted:
            return True  # remaps may live in any emulator's tree
        sub = parts[1]
        if sub == "emulationstation":
            return "emulationstation" in wanted
        if sub == "retroarch":
            return "retroarch" in wanted
        return f"emulator:{sub}" in wanted
    return True


def collect(roots: Roots, categories: Optional[Iterable[str]] = None, include_saves: bool = True) -> Collection:
    """Every backup-eligible file on this machine, optionally limited to ``categories``.

    ``include_saves=False`` (only meaningful without ``categories``) leaves out the
    saves and save-state parts -- the default for a new backup.
    """
    wanted = None if categories is None else set(categories)
    result = Collection()
    for root in ROOT_NAMES:
        if not _root_wanted(root, wanted) or (wanted is None and not include_saves and root == "saves"):
            continue
        base = roots.path(root)
        try:
            if not base.is_dir():
                continue
        except OSError:
            continue
        for dir_parts, entry in _walk(base):
            if not _subtree_wanted(root, dir_parts, wanted):
                continue
            rel_parts = dir_parts + (entry.name,)
            arcpath = "/".join((root,) + rel_parts)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                result.skipped.append({"path": arcpath, "size": 0, "reason": f"unreadable: {error.strerror or error}"})
                continue
            if stat.S_ISDIR(info.st_mode):
                continue
            category = classify(arcpath)
            if category is None or (wanted is not None and category not in wanted):
                continue
            if stat.S_ISLNK(info.st_mode):
                result.skipped.append({"path": arcpath, "size": 0, "reason": "symbolic link"})
                continue
            if not stat.S_ISREG(info.st_mode) or entry.name.lower() in PLACEHOLDER_NAMES:
                continue
            reason = _excluded_reason(root, dir_parts, entry.name)
            if reason:
                result.skipped.append({"path": arcpath, "size": info.st_size, "reason": reason})
                continue
            limit = MAX_SAVE_FILE_BYTES if root == "saves" else MAX_CONFIG_FILE_BYTES
            if info.st_size > limit:
                result.skipped.append({"path": arcpath, "size": info.st_size,
                                       "reason": f"larger than {limit // (1024 * 1024)} MB"})
                continue
            result.files.append(FileEntry(arcpath, category, int(info.st_size), Path(entry.path)))
    result.files.sort(key=lambda item: item.arcpath)
    return result


def summarize(files: Iterable) -> List[dict]:
    """Per-category counts for anything with ``category`` and ``size`` (entries or manifest dicts)."""
    totals: Dict[str, List[int]] = {}
    for item in files:
        category = item.category if hasattr(item, "category") else item["category"]
        size = item.size if hasattr(item, "size") else int(item.get("size") or 0)
        bucket = totals.setdefault(category, [0, 0])
        bucket[0] += 1
        bucket[1] += int(size)
    return [dict(describe(category), file_count=count, size_bytes=size)
            for category, (count, size) in sorted(totals.items(), key=lambda pair: sort_key(pair[0]))]
