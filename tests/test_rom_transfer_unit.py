"""Tests for folder-unit ROM resolution (multi-file games behind a marker file).

Systems like Sega Lindbergh or Dreamcast store one game as a folder of many files
while gamelist.xml points at a marker/index file inside it; the curated table +
guard in ``app.roms.rom_transfer_unit`` decide when a gamelist entry transfers its
whole parent folder instead of just the marker.
"""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.drone_api import RomRepository
from app.roms.rom_transfer_unit import (
    FOLDER_UNIT_MAX_ENTRIES,
    folder_unit_systems,
    gamelist_folder_entry_counts,
    resolve_transfer_unit,
)


def _repo(root: Path) -> RomRepository:
    with mock.patch.dict(
        "os.environ",
        {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
        clear=True,
    ):
        settings = Settings.from_env()
    return RomRepository(settings.roms_root, settings.bios_root)


def _write_gamelist(system_dir: Path, entries) -> None:
    body = "".join(
        f"<game id='{gid}'><path>{path}</path><name>{name}</name></game>"
        for gid, path, name in entries
    )
    (system_dir / "gamelist.xml").write_text(f"<gameList>{body}</gameList>", encoding="utf-8")


class FolderUnitTableTest(unittest.TestCase):
    def test_vendored_table_loads_expected_systems(self):
        systems = folder_unit_systems()
        self.assertIn("lindbergh", systems)
        self.assertIn("dreamcast", systems)
        self.assertNotIn("c64", systems)
        self.assertNotIn("scummvm", systems)

    def test_gamelist_folder_entry_counts(self):
        # Counts key on the TOP-LEVEL folder (the transfer unit), so nested markers
        # (lindbergh's hotd4a/elf/hotd4a.game) count against the game folder itself.
        root = ET.fromstring(
            "<gameList>"
            "<game><path>./1-hit/a.crt</path></game>"
            "<game><path>./1-hit/b.crt</path></game>"
            "<game><path>./1-hit/b.crt</path></game>"  # duplicate path counted once
            "<game><path>./Game/Game.gdi</path></game>"
            "<game><path>./hotd4a/elf/hotd4a.game</path></game>"
            "<game><path>./top.zip</path></game>"
            "</gameList>"
        )
        counts = gamelist_folder_entry_counts(root)
        self.assertEqual(counts["1-hit"], 2)
        self.assertEqual(counts["game"], 1)
        self.assertEqual(counts["hotd4a"], 1)
        self.assertEqual(counts["."], 1)


class ResolveTransferUnitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.system_dir = Path(self._tmp.name) / "roms" / "lindbergh"
        self.game_dir = self.system_dir / "hotd4"
        self.game_dir.mkdir(parents=True)
        self.marker = self.game_dir / "hotd4.game"
        self.marker.write_bytes(b"marker")
        (self.game_dir / "disk0.bin").write_bytes(b"x" * 64)

    def test_marker_in_folder_resolves_to_parent(self):
        unit = resolve_transfer_unit(
            "lindbergh", "hotd4/hotd4.game", self.marker, self.system_dir.resolve(), {"hotd4": 1}
        )
        self.assertIsNotNone(unit)
        self.assertEqual(unit["unit_rel_path"], "hotd4")
        self.assertEqual(unit["marker_rel_path"], "hotd4/hotd4.game")
        self.assertEqual(unit["unit_dir"], self.game_dir.resolve())

    def test_nested_marker_resolves_to_top_level_folder(self):
        # Real Lindbergh layout: the marker lives in a subfolder (elf/) BESIDE the
        # game data (fs/); the unit must be the whole top-level game folder.
        elf_dir = self.game_dir / "elf"
        elf_dir.mkdir()
        nested_marker = elf_dir / "hotd4.game"
        nested_marker.write_bytes(b"marker")
        (self.game_dir / "fs").mkdir()
        (self.game_dir / "fs" / "data.bin").write_bytes(b"d" * 128)
        unit = resolve_transfer_unit(
            "lindbergh", "hotd4/elf/hotd4.game", nested_marker, self.system_dir.resolve(), {"hotd4": 1}
        )
        self.assertIsNotNone(unit)
        self.assertEqual(unit["unit_rel_path"], "hotd4")
        self.assertEqual(unit["unit_dir"], self.game_dir.resolve())
        self.assertEqual(unit["marker_rel_path"], "hotd4/elf/hotd4.game")

    def test_system_not_in_table_is_ignored(self):
        unit = resolve_transfer_unit(
            "c64", "hotd4/hotd4.game", self.marker, self.system_dir.resolve(), {"hotd4": 1}
        )
        self.assertIsNone(unit)

    def test_top_level_file_is_ignored(self):
        top = self.system_dir / "top.game"
        top.write_bytes(b"top")
        unit = resolve_transfer_unit("lindbergh", "top.game", top, self.system_dir.resolve(), {".": 1})
        self.assertIsNone(unit)

    def test_multi_disc_folder_passes_guard(self):
        unit = resolve_transfer_unit(
            "lindbergh", "hotd4/hotd4.game", self.marker, self.system_dir.resolve(), {"hotd4": 2}
        )
        self.assertIsNotNone(unit)

    def test_category_folder_trips_guard(self):
        counts = {"hotd4": FOLDER_UNIT_MAX_ENTRIES + 1}
        unit = resolve_transfer_unit(
            "lindbergh", "hotd4/hotd4.game", self.marker, self.system_dir.resolve(), counts
        )
        self.assertIsNone(unit)

    def test_missing_marker_is_ignored(self):
        missing = self.game_dir / "gone.game"
        unit = resolve_transfer_unit(
            "lindbergh", "hotd4/gone.game", missing, self.system_dir.resolve(), {"hotd4": 1}
        )
        self.assertIsNone(unit)


class GamelistScanFolderUnitTest(unittest.TestCase):
    """list_gamelist_rom_metadata folds marker entries for table systems only."""

    def _scan(self, system_name, entries, layout):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "userdata"
        system_dir = root / "roms" / system_name
        system_dir.mkdir(parents=True)
        for rel, content in layout:
            target = system_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        _write_gamelist(system_dir, entries)
        repo = _repo(root)
        _, items = repo.list_gamelist_rom_metadata(system_name)
        return items

    def test_lindbergh_marker_reports_folder_totals(self):
        items = self._scan(
            "lindbergh",
            [("1", "./hotd4/hotd4.game", "House of the Dead 4")],
            [("hotd4/hotd4.game", b"marker"), ("hotd4/disk0.bin", b"x" * 100)],
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["entry_type"], "folder")
        self.assertEqual(item["relative_path"], "hotd4/hotd4.game")  # identity stays the marker
        self.assertEqual(item["transfer_unit_path"], "hotd4")
        self.assertEqual(item["marker_relative_path"], "hotd4/hotd4.game")
        self.assertEqual(item["file_size"], len(b"marker") + 100)  # folder total

    def test_lindbergh_nested_marker_claims_whole_game_folder(self):
        # Marker nested in elf/ beside the fs/ data dir (the real hotd4a layout): the
        # unit is the top-level game folder, and the size covers EVERYTHING in it.
        items = self._scan(
            "lindbergh",
            [("1", "./hotd4a/elf/hotd4a.game", "House of the Dead 4")],
            [
                ("hotd4a/elf/hotd4a.game", b"marker"),
                ("hotd4a/elf/hod4M.elf", b"e" * 80),
                ("hotd4a/fs/disk0.bin", b"f" * 2700),
            ],
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["entry_type"], "folder")
        self.assertEqual(item["relative_path"], "hotd4a/elf/hotd4a.game")
        self.assertEqual(item["transfer_unit_path"], "hotd4a")
        self.assertEqual(item["marker_relative_path"], "hotd4a/elf/hotd4a.game")
        self.assertEqual(item["file_size"], len(b"marker") + 80 + 2700)

    def test_lindbergh_three_level_marker_claims_top_folder(self):
        # vtennis3/vt3_Lindbergh/vtennis3.game -- marker three levels deep.
        items = self._scan(
            "lindbergh",
            [("1", "./vtennis3/vt3_Lindbergh/vtennis3.game", "Virtua Tennis 3")],
            [
                ("vtennis3/vt3_Lindbergh/vtennis3.game", b"marker"),
                ("vtennis3/vt3_Lindbergh/data/game.bin", b"g" * 500),
            ],
        )
        item = items[0]
        self.assertEqual(item["entry_type"], "folder")
        self.assertEqual(item["transfer_unit_path"], "vtennis3")
        self.assertEqual(item["file_size"], len(b"marker") + 500)

    def test_c64_category_folder_stays_single_file(self):
        items = self._scan(
            "c64",
            [("1", "./1-hit/a.crt", "A"), ("2", "./1-hit/b.crt", "B")],
            [("1-hit/a.crt", b"aaaa"), ("1-hit/b.crt", b"bbbbbb")],
        )
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item["entry_type"], "file")
            self.assertNotIn("transfer_unit_path", item)
        sizes = sorted(item["file_size"] for item in items)
        self.assertEqual(sizes, [4, 6])

    def test_table_system_top_level_file_stays_file(self):
        items = self._scan(
            "psp",
            [("1", "./Game.iso", "Game")],
            [("Game.iso", b"iso bytes")],
        )
        self.assertEqual(items[0]["entry_type"], "file")
        self.assertNotIn("transfer_unit_path", items[0])

    def test_multi_disc_shared_folder_folds_both_entries(self):
        items = self._scan(
            "dreamcast",
            [
                ("d1", "./Game/Game (Disc 1).gdi", "Game Disc 1"),
                ("d2", "./Game/Game (Disc 2).gdi", "Game Disc 2"),
            ],
            [
                ("Game/Game (Disc 1).gdi", b"gdi1"),
                ("Game/Game (Disc 2).gdi", b"gdi2"),
                ("Game/track01.bin", b"t" * 50),
            ],
        )
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual(item["entry_type"], "folder")
            self.assertEqual(item["transfer_unit_path"], "Game")
            self.assertEqual(item["file_size"], 4 + 4 + 50)

    def test_stale_gamelist_entry_skipped(self):
        items = self._scan(
            "dreamcast",
            [("1", "./Game/Game.gdi", "Game"), ("2", "./Other/Other.gdi", "Other")],
            [("Game/Game.gdi", b"gdi")],  # Other/ missing on disk entirely
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["gamelist_game_id"], "1")

    def test_true_directory_entry_unchanged(self):
        # ps3-style: the gamelist points at the directory itself.
        items = self._scan(
            "ps3",
            [("1", "./Game.ps3", "Game")],
            [("Game.ps3/USRDIR/eboot.bin", b"e" * 10)],
        )
        item = items[0]
        self.assertEqual(item["entry_type"], "folder")
        self.assertEqual(item["relative_path"], "Game.ps3")
        self.assertNotIn("transfer_unit_path", item)


class ClassifierVersionReindexTest(unittest.TestCase):
    """A classifier-version bump must re-index systems whose gamelist.xml is unchanged
    (the per-system MD5 gate would otherwise carry stale pre-upgrade rows forever)."""

    def _stale_row(self, current_row: dict) -> dict:
        stale = {key: value for key, value in current_row.items() if key not in {"transfer_unit_path", "marker_relative_path"}}
        stale.update({"entry_type": "file", "file_size": 6, "byte_count": 6, "size": 6})
        return stale

    def test_version_bump_reindexes_unchanged_gamelist(self):
        from app.roms.rom_scanner import ROM_CLASSIFIER_VERSION, _poll_rom_metadata_cache
        from app.storage.rom_metadata_store import (
            _load_rom_metadata_cache,
            _persist_rom_metadata_cache,
            _read_rom_metadata_cache_state,
            _update_rom_metadata_cache_state,
        )

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "userdata"
        system_dir = root / "roms" / "lindbergh"
        (system_dir / "hotd4a" / "elf").mkdir(parents=True)
        (system_dir / "hotd4a" / "elf" / "hotd4a.game").write_bytes(b"marker")
        (system_dir / "hotd4a" / "fs").mkdir()
        (system_dir / "hotd4a" / "fs" / "disk0.bin").write_bytes(b"f" * 100)
        _write_gamelist(system_dir, [("1", "./hotd4a/elf/hotd4a.game", "HOTD4")])
        with mock.patch.dict(
            "os.environ",
            {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios")},
            clear=True,
        ):
            settings = Settings.from_env()
        repo = RomRepository(settings.roms_root, settings.bios_root)

        _poll_rom_metadata_cache(settings, repo)
        cache, _ = _load_rom_metadata_cache(settings)
        key, row = next(iter(cache["entries"].items()))
        self.assertEqual(row["entry_type"], "folder")

        # Simulate a pre-upgrade cache: stale single-file row.
        _persist_rom_metadata_cache(settings, cache, rom_updates={key: self._stale_row(row)})

        # Control: with the CURRENT version stamped, an unchanged gamelist carries the
        # stale row forward (this is exactly why the version gate is needed).
        _update_rom_metadata_cache_state(settings, rom_classifier_version=ROM_CLASSIFIER_VERSION)
        _poll_rom_metadata_cache(settings, repo)
        cache, _ = _load_rom_metadata_cache(settings)
        self.assertEqual(cache["entries"][key]["entry_type"], "file")

        # With an OLD stored version, the same poll re-indexes and re-classifies.
        _update_rom_metadata_cache_state(settings, rom_classifier_version=1)
        _poll_rom_metadata_cache(settings, repo)
        cache, _ = _load_rom_metadata_cache(settings)
        row = cache["entries"][key]
        self.assertEqual(row["entry_type"], "folder")
        self.assertEqual(row["transfer_unit_path"], "hotd4a")
        self.assertEqual(row["file_size"], len(b"marker") + 100)
        state = _read_rom_metadata_cache_state(settings, "rom_classifier_version")
        self.assertEqual(state.get("rom_classifier_version"), ROM_CLASSIFIER_VERSION)


if __name__ == "__main__":
    unittest.main()
