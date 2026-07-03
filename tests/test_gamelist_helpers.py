"""Tests for the pure gamelist.xml / artwork helpers (``roms/gamelist.py``).

The high-level gamelist read/write paths are covered by endpoint tests, but a cluster
of pure helpers underneath them had no direct coverage despite real edge-case logic:
metadata precedence, XML child create/update/remove, artwork-path normalization and
identity (the key to dedup — differently-spelled paths must collapse to one identity),
placeholder-image detection, and stable game-id lookup. These lock those contracts.
"""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app.roms.gamelist import (
    _artwork_identity,
    _find_gamelist_entry_by_game_id,
    _first_metadata_value,
    _gamelist_details,
    _gamelist_game_id,
    _looks_like_placeholder_image,
    _normalize_gamelist_rom_path,
    _relative_artwork_path,
    _remove_child,
    _set_child_text,
    _text_or_empty,
)


class MetadataAndXmlPrimitiveTests(unittest.TestCase):
    def test_text_or_empty(self):
        game = ET.fromstring("<game><name>  Zelda  </name><blank></blank></game>")
        self.assertEqual(_text_or_empty(game, "name"), "Zelda")   # stripped
        self.assertEqual(_text_or_empty(game, "blank"), "")       # empty element
        self.assertEqual(_text_or_empty(game, "missing"), "")     # absent tag

    def test_set_child_text_creates_then_updates_without_duplicating(self):
        game = ET.fromstring("<game/>")
        _set_child_text(game, "desc", "first")
        self.assertEqual(_text_or_empty(game, "desc"), "first")
        _set_child_text(game, "desc", "second")
        self.assertEqual(_text_or_empty(game, "desc"), "second")
        self.assertEqual(len(game.findall("desc")), 1)  # updated in place, not appended

    def test_remove_child_is_idempotent(self):
        game = ET.fromstring("<game><desc>x</desc></game>")
        _remove_child(game, "desc")
        self.assertIsNone(game.find("desc"))
        _remove_child(game, "desc")  # no-op, no error on second removal

    def test_first_metadata_value_precedence_and_skipping(self):
        self.assertEqual(_first_metadata_value("", "  ", None, "Hello"), "Hello")
        self.assertEqual(_first_metadata_value(None, "", []), "")

    def test_first_metadata_value_unwraps_dicts_by_key_priority(self):
        self.assertEqual(_first_metadata_value({"name": "", "title": "T"}), "T")
        self.assertEqual(_first_metadata_value({"value": "V", "displayName": "D"}), "V")
        # a fully-empty dict is skipped so the next arg is consulted
        self.assertEqual(_first_metadata_value({"name": "", "title": ""}, "fallback"), "fallback")

    def test_first_metadata_value_joins_collections(self):
        self.assertEqual(_first_metadata_value(["a", "", "b"]), "a, b")
        self.assertEqual(_first_metadata_value((None, {"name": "X"})), "X")

    def test_gamelist_details_shapes(self):
        self.assertEqual(_gamelist_details(None), {})
        game = ET.fromstring(
            "<game><name>N</name><genre>A</genre><genre>B</genre>"
            "<rating type='x'>0.5</rating></game>"
        )
        details = _gamelist_details(game)
        self.assertEqual(details["name"], "N")
        self.assertEqual(details["genre"], ["A", "B"])  # repeated tag -> list
        self.assertEqual(details["rating"], {"text": "0.5", "attributes": {"type": "x"}})


class PathAndIdentityNormalizationTests(unittest.TestCase):
    def test_normalize_gamelist_rom_path(self):
        self.assertEqual(_normalize_gamelist_rom_path("./snes/Game.zip"), "snes/Game.zip")
        self.assertEqual(_normalize_gamelist_rom_path(".\\.\\snes\\Game.zip"), "snes/Game.zip")
        self.assertEqual(_normalize_gamelist_rom_path("/roms/x"), "roms/x")
        self.assertEqual(_normalize_gamelist_rom_path("  ./a/b  "), "a/b")
        self.assertEqual(_normalize_gamelist_rom_path(None), "")

    def test_artwork_identity_relative_forms_collapse(self):
        canonical = "images/box.png"
        for variant in ("./images/box.png", "/images/box.png", "images\\Box.PNG", "./Images/Box.png"):
            with self.subTest(variant=variant):
                self.assertEqual(_artwork_identity(variant), canonical)
        self.assertEqual(_artwork_identity(""), "")
        self.assertEqual(_artwork_identity(None), "")

    def test_artwork_identity_urls_use_host_and_path(self):
        self.assertEqual(
            _artwork_identity("https://cdn.example.com/art/Box.png/"),
            "cdn.example.com/art/box.png",
        )
        self.assertEqual(_artwork_identity("http://Host.com/A"), "host.com/a")

    def test_relative_artwork_path_inside_and_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            system_dir = Path(tmp) / "roms" / "snes"
            system_dir.mkdir(parents=True)
            inside = system_dir / "images" / "art.png"
            self.assertEqual(_relative_artwork_path(system_dir, inside), "./images/art.png")
            outside = Path(tmp) / "elsewhere" / "art.png"
            # not under system_dir -> returns the original path string, unmodified
            self.assertEqual(_relative_artwork_path(system_dir, outside), str(outside))


class PlaceholderImageTests(unittest.TestCase):
    def test_empty_or_tiny_is_placeholder(self):
        self.assertTrue(_looks_like_placeholder_image(b""))
        self.assertTrue(_looks_like_placeholder_image(b"x" * 127))  # below 128 bytes

    def test_flat_image_is_placeholder(self):
        self.assertTrue(_looks_like_placeholder_image(bytes(200)))          # 1 distinct byte
        self.assertTrue(_looks_like_placeholder_image((b"\x00\x01\x02" * 100)))  # 3 distinct

    def test_varied_real_image_is_not_placeholder(self):
        self.assertFalse(_looks_like_placeholder_image(bytes(range(256)) * 2))  # 256 distinct, 512 bytes


class GameIdLookupTests(unittest.TestCase):
    def test_gamelist_game_id_sources_and_precedence(self):
        self.assertEqual(_gamelist_game_id(None, "rel"), "rel")
        self.assertEqual(_gamelist_game_id(ET.fromstring("<game id='42'/>"), "rel"), "42")
        self.assertEqual(_gamelist_game_id(ET.fromstring("<game><id>7</id></game>"), "rel"), "7")
        self.assertEqual(_gamelist_game_id(ET.fromstring("<game/>"), "rel"), "rel")
        # id attribute wins over an <id> child
        self.assertEqual(_gamelist_game_id(ET.fromstring("<game id='42'><id>7</id></game>"), "rel"), "42")

    def test_find_entry_by_id_attr_child_or_path(self):
        root = ET.fromstring(
            "<gameList>"
            "<game id='a1'><name>One</name></game>"
            "<game><path>./snes/Zelda.zip</path></game>"
            "</gameList>"
        )
        self.assertEqual(_find_gamelist_entry_by_game_id(root, "a1").find("name").text, "One")
        # path match is normalization- and case-insensitive
        matched = _find_gamelist_entry_by_game_id(root, "snes/zelda.zip")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.find("path").text, "./snes/Zelda.zip")
        self.assertIsNone(_find_gamelist_entry_by_game_id(root, "nope"))
        self.assertIsNone(_find_gamelist_entry_by_game_id(root, ""))


if __name__ == "__main__":
    unittest.main()
