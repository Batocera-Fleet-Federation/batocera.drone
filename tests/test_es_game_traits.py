"""ES resource fixtures, relational filters, and unchanged-gamelist refresh."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from app import drone_api
from app.roms import es_game_traits as traits
from app.roms.rom_scanner import _poll_rom_metadata_cache
from app.storage.rom_metadata_store import (
    _empty_rom_metadata_cache, _persist_rom_metadata_cache,
    list_rom_cache_page, list_rom_trait_counts,
)

FIXTURES = Path(__file__).parent / 'fixtures' / 'es_traits'


class EsGameTraitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / 'resources'
        shutil.copytree(FIXTURES, self.resources)
        env = {'USERDATA_ROOT': str(self.root), 'ROMS_ROOT': str(self.root / 'roms'),
               'BIOS_ROOT': str(self.root / 'bios'), 'DRONE_DEVICE_ID': 'traits-test',
               'DRONE_STATE_DATABASE_FILE': str(self.root / 'state.sqlite')}
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = drone_api.Settings.from_env()
        self.patch = mock.patch.object(traits, 'RESOURCE_DIR', self.resources)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.platform_patch = mock.patch('app.device.device_control._resolve_es_systems_effective',
                                        return_value=(None, [{'name': 'fbneo', 'platform': 'arcade'}, {'name': 'mame', 'platform': 'arcade'}]))
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)

    def test_players_ranges(self):
        for value, expected in [('1', (1, 1)), ('1-4', (1, 4)), ('2', (1, 2)),
                                ('1+', (1, 99999)), ('4+', (4, 99999)), ('', (-1, -1)),
                                ('junk', (1, 0)), ('3-4', (3, 4))]:
            with self.subTest(value=value):
                self.assertEqual(traits.parse_players_range(value), expected)
        self.assertEqual(traits.indexed_name('Revision (USA 2) [Rev 3]'), 'revision23')

    def test_resources_match_es(self):
        classifier = traits.GameTraits(self.settings)
        for system, name, expected in [
            ('snes', 'Tin Star (USA).zip', ['lightgun']),
            ('nes', 'Super Racer [v2].zip', ['wheel']),
            ('snes', 'Marble Madness.zip', ['trackball']),
            ('snes', 'Arkanoid.zip', ['spinner']),
            ('snes', 'Revision (2).zip', ['spinner']),
            ('mame', 'gunset.zip', ['lightgun']),
            ('mame', 'gunset (USA).zip', []),
            ('mame', 'balls.zip', ['spinner', 'trackball']),
            ('mame', 'verticalset.zip', ['vertical']),
            ('snes', 'verticalset.zip', []),
            ('fbneo', 'knownset.zip', []),  # known unflagged arcade must not fall back
            ('fbneo', 'Unknown Deluxe.zip', []),  # arcade platforms never use substring matching
            ('mame', 'bios.zip', []), ('snes', 'default.zip', []),
        ]:
            with self.subTest(system=system, name=name):
                self.assertEqual(classifier.classify(system, name), expected)
        for file in self.resources.iterdir():
            file.unlink()
        self.assertEqual(traits.GameTraits(self.settings).classify('mame', 'gunset.zip'), [])
        (self.resources / 'gamesdb.xml').write_text('<broken>')
        self.assertEqual(traits.GameTraits(self.settings).classify('snes', 'Tin Star.zip'), [])

    def test_index_filters_counts_roundtrip_and_deletion(self):
        entries = {str(i): {'system': 'snes', 'file_path': f'{i}.zip', 'rom_name': f'Game {i}',
                            'players': players, 'traits': flags, 'genre': 'Action', 'missing_artwork': ['image']}
                   for i, (players, flags) in enumerate([('1', []), ('1-4', ['lightgun']),
                                                        ('4+', ['wheel']), ('', ['lightgun'])])}
        cache = {**_empty_rom_metadata_cache(), 'entries': entries,
                 'last_full_scan_at': '2026-11-12T00:00:00Z', 'scan_in_progress': False}
        _persist_rom_metadata_cache(self.settings, cache, rom_updates=entries)
        def page(**filters):
            return list_rom_cache_page(self.settings, **filters)
        self.assertEqual(page(players='2')['total'], 1)
        self.assertEqual(page(players='4')['total'], 2)
        self.assertEqual(page(players='4', trait='lightgun', genre='Action', missing_artwork=['image'])['total'], 1)
        self.assertEqual(page(trait='lightgun')['total'], 2)
        self.assertEqual(page(trait="' OR 1=1 --")['total'], 0)
        self.assertEqual(page(players='3')['total'], 0)
        self.assertEqual(page(trait='lightgun', limit=1, offset=1)['total'], 2)
        self.assertEqual(len(page(trait='lightgun', limit=1, offset=1)['items']), 1)
        counts = {r['name']: r['count'] for r in list_rom_trait_counts(self.settings, query='Game', genre='Action')}
        self.assertEqual(counts['4players'], 2)
        self.assertEqual(counts['lightgun'], 2)
        item = page(players='2')['items'][0]
        self.assertEqual((item['players_min'], item['players_max']), (1, 4))
        _persist_rom_metadata_cache(self.settings, cache, rom_updates={'1': item})
        self.assertEqual(page(players='2', trait='lightgun')['total'], 1)
        _persist_rom_metadata_cache(self.settings, cache, rom_deletes=['1'])
        self.assertEqual(page(players='2')['total'], 0)

    def test_handler_combines_players_traits_and_existing_filters(self):
        entries = {region: {'system': 'snes', 'file_path': f'{region}.zip',
                            'rom_name': f'Tin Star ({region})', 'unique_id': region,
                            'players': players, 'traits': ['lightgun'],
                            'genre': 'Action', 'missing_artwork': ['image']}
                   for region, players in [('USA', '1-4'), ('Europe', '2'), ('Japan', '1')]}
        cache = {**_empty_rom_metadata_cache(), 'entries': entries,
                 'last_full_scan_at': '2026-11-12T00:00:00Z', 'scan_in_progress': False}
        _persist_rom_metadata_cache(self.settings, cache, rom_updates=entries)
        repo = drone_api.RomRepository(self.settings.roms_root, self.settings.bios_root, settings=self.settings)
        handler = drone_api.RomRequestHandler.__new__(drone_api.RomRequestHandler)
        handler.settings, handler.repository = self.settings, repo
        responses = []
        handler._send_json = lambda status, payload: responses.append(payload)
        scope = dict(players='2', trait='lightgun', genre='Action', query='Tin Star',
                     missing_art=['image'], browser_playable=True)
        handler._handle_rom_browse(**scope)
        self.assertEqual(responses[-1]['count'], 2)
        self.assertEqual({r['name']: r['count'] for r in responses[-1]['traits']}['4players'], 1)
        handler._handle_admin_rom_duplicates(**scope)
        self.assertEqual(responses[-1]['count'], 2)
        handler._handle_rom_browse(**{**scope, 'players': '4'})
        self.assertEqual(responses[-1]['count'], 1)
        handler._handle_rom_browse(**{**scope, 'trait': "' OR 1=1 --"})
        self.assertEqual(responses[-1]['count'], 0)

    def test_resource_change_reindexes_unchanged_gamelist(self):
        system_dir = self.settings.roms_root / 'snes'
        system_dir.mkdir(parents=True)
        (system_dir / 'Tin Star.zip').write_bytes(b'rom')
        (system_dir / 'gamelist.xml').write_text('<gameList><game><path>./Tin Star.zip</path><name>Tin Star</name><players>1-4</players></game></gameList>')
        repo = drone_api.RomRepository(self.settings.roms_root, self.settings.bios_root, settings=self.settings)
        _poll_rom_metadata_cache(self.settings, repo)
        self.assertEqual(list_rom_cache_page(self.settings, players='4', trait='lightgun')['total'], 1)
        resource = self.resources / 'gamesdb.xml'
        resource.write_text(resource.read_text().replace('<gun/>', '<wheel/>'))
        _poll_rom_metadata_cache(self.settings, repo)
        self.assertEqual(list_rom_cache_page(self.settings, trait='lightgun')['total'], 0)
        self.assertEqual(list_rom_cache_page(self.settings, trait='wheel')['total'], 1)
        resource.unlink()
        _poll_rom_metadata_cache(self.settings, repo)
        self.assertEqual(list_rom_cache_page(self.settings, trait='wheel')['total'], 0)
        self.assertEqual(list_rom_cache_page(self.settings, players='4')['total'], 1)
