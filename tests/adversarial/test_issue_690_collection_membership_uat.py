"""Issue #690 collection membership through real scan, SQLite and browse handler.

Expected memberships come from the issue's Matching rules and vert="true"
rule, rather than the classifier's existing fallback/name-attribute behavior.
"""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import drone_api
from app.roms import es_game_traits
from app.roms.rom_scanner import _poll_rom_metadata_cache


class CollectionMembershipUAT(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.resources = root / "resources"
        self.resources.mkdir()
        # A non-arcade system-specific marker must not override arcade lookup.
        (self.resources / "gamesdb.xml").write_text(
            '<systems><system id="arcade"><game id="exact"><gun/></game>'
            '<game id="unflagged"/></system>'
            '<system id="fbneo"><game id="alias"><gun/></game>'
            '<game id="unflagged"><gun/></game></system></systems>')
        (self.resources / "arcaderoms.xml").write_text(
            '<roms><rom id="exact" name="Exact"/>'
            '<rom id="portrait" vert="true"/>'
            '<rom id="landscape" name="Landscape" vert="false"/></roms>')
        config = root / "es_systems.cfg"
        config.write_text('<systemList><system><name>fbneo</name>'
                          '<platform>arcade</platform></system></systemList>')
        env = {"USERDATA_ROOT": str(root), "ROMS_ROOT": str(root / "roms"),
               "BIOS_ROOT": str(root / "bios"), "SAVES_ROOT": str(root / "saves"),
               "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite"),
               "DRONE_DEVICE_ID": "adversarial-690", "ES_SYSTEMS_FILE": str(config)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = drone_api.Settings.from_env()
        patch = mock.patch.object(es_game_traits, "RESOURCE_DIR", self.resources)
        patch.start()
        self.addCleanup(patch.stop)
        self.repo = drone_api.RomRepository(self.settings.roms_root,
                                          self.settings.bios_root, settings=self.settings)
        folder = self.settings.roms_root / "fbneo"
        folder.mkdir(parents=True)
        stems = ["exact", "alias", "alias Deluxe", "unflagged", "portrait", "landscape"]
        for stem in stems:
            (folder / (stem + ".zip")).write_bytes(b"fixture ROM")
        (folder / "gamelist.xml").write_text('<gameList>' + ''.join(
            '<game><path>./' + stem + '.zip</path><name>' + stem + '</name>'
            '<players>1-4</players></game>' for stem in stems) + '</gameList>')
        with contextlib.redirect_stdout(io.StringIO()):
            _poll_rom_metadata_cache(self.settings, self.repo)

    def browse(self, trait):
        handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
        handler.settings, handler.repository = self.settings, self.repo
        responses = []
        handler._send_json = lambda status, payload: responses.append((status, payload))
        handler._handle_rom_browse(system="fbneo", players="4", trait=trait)
        self.assertEqual(responses[-1][0], 200)
        return responses[-1][1]

    def test_arcade_input_filter_uses_only_exact_arcade_ids(self):
        # Issue: "Arcade-platform systems (`<platform>arcade</platform>`): exact
        # lookup of the ROM file's name without its extension against the
        # `<system id="arcade">` ids." Confidence: high.
        result = self.browse("lightgun")
        self.assertEqual({row["file_path"] for row in result["roms"]}, {"exact.zip"},
                         "arcade input matching must not fall back to fbneo substring IDs")
        self.assertEqual(result["count"], 1)

    def test_vertical_marker_does_not_require_a_display_name(self):
        # Issue: "`arcaderoms.xml` lists MAME set names; `vert="true"` marks
        # vertical arcade games." Confidence: high.
        result = self.browse("vertical")
        self.assertEqual({row["file_path"] for row in result["roms"]}, {"portrait.zip"},
                         "vert=true is the issue's stated vertical membership marker")
        self.assertEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main()
