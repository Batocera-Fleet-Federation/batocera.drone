"""Detect a pure "mock/demo" userdata tree vs. a real device.

Extracted from ``drone_api.py``. When ``USE_FAKE_DATA`` seeds a demo userdata tree,
these helpers recognise it (via a seed marker + state) so the app serves the mock
ROM/BIOS roots instead of the real ones.
"""

import os
from pathlib import Path
from typing import Tuple

try:
    from .settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore


def _mock_userdata_marker(userdata_root: Path) -> Path:
    return userdata_root / "system" / "drone-app" / "mock_userdata_seeded.json"


def _looks_like_pure_mock_userdata(userdata_root: Path) -> bool:
    roms_root = userdata_root / "roms"
    if not roms_root.exists():
        return False
    known_fake_files = {
        roms_root / "snes" / "Chrono Trigger (USA).zip": b"FAKE-SNES-ROM-1",
        roms_root / "snes" / "Super Mario World (USA).zip": b"FAKE-SNES-ROM-2",
        roms_root / "snes" / "The Legend of Zelda - A Link to the Past (USA).zip": b"FAKE-SNES-ROM-3",
        roms_root / "gba" / "Metroid Fusion (USA).zip": b"FAKE-GBA-ROM-1",
        roms_root / "gba" / "Mario Kart Super Circuit (USA).zip": b"FAKE-GBA-ROM-2",
        roms_root / "psx" / "Castlevania - Symphony of the Night (USA).chd": b"FAKE-PSX-ROM-1",
    }
    has_known_fake = False
    for path, expected in known_fake_files.items():
        try:
            if path.exists() and path.read_bytes() == expected:
                has_known_fake = True
        except OSError:
            continue
    seeded = _load_state_payload(
        _state_database_path(userdata_root),
        "mock_userdata_seeded",
        None,
        legacy_path=_mock_userdata_marker(userdata_root),
    )
    if not (has_known_fake or seeded):
        return False

    for path in roms_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() == "gamelist.xml" or "/images/" in path.as_posix() or "/videos/" in path.as_posix():
            continue
        expected = known_fake_files.get(path)
        if expected is None:
            return False
        try:
            if path.read_bytes() != expected:
                return False
        except OSError:
            return False
    return True


def _real_data_roots(settings: Settings) -> Tuple[Path, Path]:
    if os.environ.get("ROMS_ROOT") or os.environ.get("BIOS_ROOT"):
        return settings.roms_root, settings.bios_root
    if settings.use_fake_data or not _looks_like_pure_mock_userdata(settings.userdata_root):
        return settings.roms_root, settings.bios_root
    empty_root = settings.userdata_root / "system" / "drone-app" / "real-data-empty"
    return empty_root / "roms", empty_root / "bios"
