"""Path safety and local inventory checks for asset transfers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def safe_rom_relative_path(value: str) -> str:
    rel = str(value or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise ValueError("invalid rom path")
    return rel


def rom_exists(repository: Any, system: str, relative_path: str) -> bool:
    try:
        system_dir = repository.get_system_dir(system).resolve()
        target = (system_dir / safe_rom_relative_path(relative_path)).resolve()
        return target.exists() and target.is_file() and (target == system_dir or system_dir in target.parents)
    except Exception:
        return False


def rom_fingerprint_exists(repository: Any, expected_fingerprint: Optional[str]) -> bool:
    expected = str(expected_fingerprint or "").strip().lower()
    if not expected:
        return False
    try:
        for system in repository.list_systems():
            system_name = str(system.get("name") or "").strip()
            if not system_name:
                continue
            _, roms = repository.list_assets(system_name, "roms")
            for rom in roms:
                if str(rom.get("fingerprint") or rom.get("rom_fingerprint") or "").strip().lower() == expected:
                    return True
    except Exception:
        return False
    return False


def bios_md5_exists(repository: Any, expected_md5: Optional[str]) -> bool:
    expected = str(expected_md5 or "").strip().lower()
    if not expected:
        return False
    try:
        for bios in repository.list_bios_entries():
            if str(bios.get("md5") or bios.get("bios_md5") or "").strip().lower() == expected:
                return True
    except Exception:
        return False
    return False


def collision_safe_target(system_dir: Path, relative_path: str) -> Path:
    system_dir = system_dir.resolve()
    rel = safe_rom_relative_path(relative_path)
    requested = (system_dir / rel).resolve()
    if requested == system_dir or system_dir not in requested.parents:
        raise ValueError("invalid target path")
    if not requested.exists():
        return requested
    parent = requested.parent
    stem = requested.stem
    suffix = requested.suffix
    index = 2
    while True:
        candidate = (parent / f"{stem} ({index}){suffix}").resolve()
        if candidate == system_dir or system_dir not in candidate.parents:
            raise ValueError("invalid target path")
        if not candidate.exists():
            return candidate
        index += 1
