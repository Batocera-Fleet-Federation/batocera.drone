#!/usr/bin/env python3
"""Migrate Drone ROM metadata cache blobs into the relational SQLite schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace


def _repo_app_path() -> Path:
    return Path(__file__).resolve().parents[1] / "app"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--userdata-root",
        type=Path,
        default=Path("/userdata"),
        help="Batocera userdata root. Defaults to /userdata.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="Optional explicit rom_metadata_cache.sqlite3 path.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(_repo_app_path()))
    import rom_metadata_store  # type: ignore

    if args.database:
        import os

        os.environ["DRONE_STATE_DATABASE_FILE"] = str(args.database.resolve())

    settings = SimpleNamespace(userdata_root=args.userdata_root.resolve())
    cache, rebuilt = rom_metadata_store._load_rom_metadata_cache(settings)
    roms = len(cache.get("entries") if isinstance(cache.get("entries"), dict) else {})
    bios = len(cache.get("bios_entries") if isinstance(cache.get("bios_entries"), dict) else {})
    artwork = len(cache.get("artwork_entries") if isinstance(cache.get("artwork_entries"), dict) else {})
    path = rom_metadata_store._rom_metadata_cache_path(settings)
    print(
        f"Migrated Drone metadata cache: database={path} schema={cache.get('schema_version')} "
        f"roms={roms} bios={bios} artwork={artwork} rebuilt={rebuilt}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
