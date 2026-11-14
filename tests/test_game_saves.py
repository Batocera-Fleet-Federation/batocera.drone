"""Issue #726: ROM-scoped saves, atomic application, and hostile peer boundaries."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import game_saves as saves
from app.web.handlers_game_saves import HandlersGameSavesMixin


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class GameSavesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.settings = SimpleNamespace(saves_root=self.root / 'saves', userdata_root=self.root, device_id='local')
        self.item = {'system': 'snes', 'rom': 'Mario.sfc', 'stem': 'Mario', 'filename': 'Mario.sfc', 'ids': []}
        self.peer = {'drone_id': 'remote', 'name': 'Living room', 'certificate_fingerprint': 'a' * 64}
        self.addCleanup(saves._CHOICES.clear)

    def write(self, name, data=b'progress', root=None):
        path = (root or self.settings.saves_root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def group(self, name='Mario.srm', content=b'new progress', kind='save', layout='system'):
        path = name if kind != 'directory' else name + '/progress.bin'
        return {'id': hashlib.sha256((layout + ':' + name).encode()).hexdigest(), 'layout': layout,
                'path': name, 'kind': kind,
                'files': [{'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}]}

    def choose(self, group, item=None):
        with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
            saves, '_open_peer', return_value=Response(json.dumps({'saves': [group]}).encode())
        ):
            result = saves.discover(self.settings, item or self.item, 'remote')
        self.assertNotIn('sha256', result['saves'][0])
        return result['saves'][0]['choice']

    def apply(self, token, content=b'new progress', item=None, overwrite=False):
        with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
            saves, '_open_peer', return_value=Response(content)
        ), mock.patch.object(saves.local_network, 'record_activity'):
            return saves.import_choice(self.settings, item or self.item, token, overwrite)

    def test_only_matching_existing_saves_and_states_and_companions(self):
        for name in ['Mario.srm', 'Mario.sfc.state1', 'Mario.sfc.state1.png', 'Mario/state.dat',
                     'Mario2.srm', 'Mario Kart.srm', 'Other.srm', 'Mario.png', 'shared.mcr']:
            self.write('snes/' + name)
        result = saves.inventory(self.settings, self.item)
        self.assertEqual({g['path'] for g in result}, {'Mario.srm', 'Mario.sfc.state1', 'Mario'})
        state = next(g for g in result if g['kind'] == 'state')
        self.assertEqual(len(state['files']), 2)
        for group in result:
            saves.validate_group(self.settings, self.item, group)

    def test_generic_system_names_and_subdirectories(self):
        item = {**self.item, 'system': 'custom-system'}
        self.write('custom-system/retroarch/Mario.srm')
        self.assertEqual(saves.inventory(self.settings, item)[0]['path'], 'retroarch/Mario.srm')

    def test_default_preserves_previous_and_applies_new_save_immediately(self):
        target = self.write('snes/Mario.srm', b'old progress')
        result = self.apply(self.choose(self.group()))
        self.assertEqual(target.read_bytes(), b'new progress')
        self.assertTrue(result['applied'])
        self.assertTrue(result['backup_kept'])
        backups = list(target.parent.glob('.drone-save-*/previous/Mario.srm'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'old progress')
        self.assertEqual(len(saves.inventory(self.settings, self.item)), 1)

    def test_overwrite_replaces_without_leaving_backup(self):
        target = self.write('snes/Mario.srm', b'old progress')
        result = self.apply(self.choose(self.group()), overwrite=True)
        self.assertFalse(result['backup_kept'])
        self.assertEqual(target.read_bytes(), b'new progress')
        self.assertFalse(list(target.parent.glob('.drone-save-*')))

    def test_missing_target_is_created(self):
        self.apply(self.choose(self.group()))
        self.assertEqual((self.settings.saves_root / 'snes/Mario.srm').read_bytes(), b'new progress')

    def test_full_directory_is_applied_as_one_save_set(self):
        target = self.write('snes/Mario/obsolete.bin', b'old')
        result = self.apply(self.choose(self.group('Mario', kind='directory')))
        self.assertFalse(target.exists())
        self.assertEqual((target.parent / 'progress.bin').read_bytes(), b'new progress')
        self.assertTrue(result['backup_kept'])

    def test_corrupt_truncated_or_oversized_download_never_changes_local_save(self):
        for data in (b'bad checksum', b'', b'x' * 30):
            with self.subTest(data=data):
                target = self.write('snes/Mario.srm', b'original')
                with self.assertRaises(saves.SaveError):
                    self.apply(self.choose(self.group()), data)
                self.assertEqual(target.read_bytes(), b'original')
                self.assertFalse(list(target.parent.glob('.drone-save-*')))

    def test_failure_in_second_file_does_not_apply_first(self):
        group = self.group('Mario', kind='directory')
        second = {**group['files'][0], 'path': 'Mario/second.bin'}
        group['files'].append(second)
        self.write('snes/Mario/progress.bin', b'original')
        token = self.choose(group)
        with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
            saves, '_open_peer', side_effect=[Response(b'new progress'), Response(b'bad')]
        ):
            with self.assertRaises(saves.SaveError):
                saves.import_choice(self.settings, self.item, token)
        self.assertEqual((self.settings.saves_root / 'snes/Mario/progress.bin').read_bytes(), b'original')

    def test_install_failure_restores_original(self):
        target = self.write('snes/Mario.srm', b'original')
        real_rename = os.rename
        def fail_incoming(src, dst, **kwargs):
            if '/incoming/' in str(src):
                raise OSError('disk failed')
            return real_rename(src, dst, **kwargs)
        with mock.patch.object(saves.os, 'rename', side_effect=fail_incoming):
            with self.assertRaises(OSError):
                self.apply(self.choose(self.group()))
        self.assertEqual(target.read_bytes(), b'original')

    def test_source_and_target_symlinks_are_not_followed(self):
        outside = self.write('outside/Mario.srm', b'secret', root=self.root)
        folder = self.settings.saves_root / 'snes'
        folder.mkdir(parents=True)
        (folder / 'Mario.srm').symlink_to(outside)
        self.assertFalse(saves.inventory(self.settings, self.item))
        with self.assertRaises(saves.SaveError):
            self.apply(self.choose(self.group()))
        self.assertEqual(outside.read_bytes(), b'secret')
        (folder / 'Mario.srm').unlink()
        folder.rmdir()
        folder.symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises(OSError):
            self.apply(self.choose(self.group()))
        self.assertEqual(outside.read_bytes(), b'secret')

    def test_choice_cannot_be_reused_for_different_rom_or_device_or_after_expiry(self):
        token = self.choose(self.group())
        with self.assertRaises(saves.SaveError):
            self.apply(token, item={**self.item, 'rom': 'Other.sfc'})
        saves._CHOICES[token]['device'] = 'another device'
        with self.assertRaises(saves.SaveError):
            self.apply(token)
        saves._CHOICES[token]['device'] = 'local'
        saves._CHOICES[token]['expires'] = 0
        with self.assertRaises(saves.SaveError):
            self.apply(token)

    def test_unpaired_and_plaintext_sources_are_rejected(self):
        with mock.patch.object(saves.local_network, 'get_paired_peer', return_value=None):
            with self.assertRaises(saves.SaveError):
                saves.tls_peer(self.settings, 'attacker')
        with mock.patch.object(saves.local_network, 'get_paired_peer', return_value=self.peer), mock.patch.object(
            saves, '_peer_address_candidates', return_value=['http://remote:80']
        ):
            with self.assertRaises(saves.SaveError):
                saves.tls_peer(self.settings, 'remote')

    def test_malicious_inventory_cannot_escape_roots_or_write_another_rom(self):
        for path in ('../Mario.srm', '/Mario.srm', 'Other.srm', 'Mario.srm/../../pwn', 'Mario.srm\\pwn'):
            with self.subTest(path=path), self.assertRaises(saves.SaveError):
                self.choose(self.group(path))
        group = self.group()
        group['layout'] = 'system-config'
        with self.assertRaises(saves.SaveError):
            self.choose(group)
        group = self.group()
        group['files'][0]['size'] = saves.MAX_BYTES + 1
        with self.assertRaises(saves.SaveError):
            self.choose(group)

    def test_switch_maps_remote_user_to_local_user_and_preserves_entire_set(self):
        title = '01006A800016E000'
        item = {'system': 'switch', 'rom': f'Smash [{title}].nsp', 'stem': f'Smash [{title}]',
                'filename': f'Smash [{title}].nsp', 'ids': [title]}
        base = 'user/save/0000000000000000/'
        old_user, new_user = 'A' * 32, 'B' * 32
        layout = 'eden-config'
        root = saves.roots(self.settings, item)[layout]
        target = self.write(base + old_user + '/' + title + '/old.bin', b'old', root=root)
        group = self.group(base + new_user + '/' + title, kind='directory', layout=layout)
        result = self.apply(self.choose(group, item), item=item)
        self.assertEqual((target.parent / 'progress.bin').read_bytes(), b'new progress')
        self.assertFalse((root / (base + new_user)).exists())
        self.assertTrue(result['backup_kept'])
        self.assertEqual(saves.capabilities(item)['states'], 'unsupported')
        self.assertEqual(saves.inventory(self.settings, item)[0]['layout'], layout)

    def test_switch_rejects_foreign_title_and_ambiguous_profile(self):
        title = '01006A800016E000'
        item = {**self.item, 'system': 'switch', 'ids': [title]}
        group = self.group('user/save/0000000000000000/' + 'A' * 32 + '/0100000000000000', kind='directory', layout='eden-config')
        with self.assertRaises(saves.SaveError):
            self.choose(group, item)
        group = self.group('user/save/0000000000000000/' + 'A' * 32 + '/' + title, kind='directory', layout='eden-config')
        root = saves.roots(self.settings, item)['eden-config']
        for uid in ('B' * 32, 'C' * 32):
            self.write('user/save/0000000000000000/' + uid + '/' + title + '/old', root=root)
        with self.assertRaises(saves.SaveError):
            self.apply(self.choose(group, item), item=item)

    def test_renamed_remote_rom_save_is_installed_under_local_name(self):
        source = {**self.item, 'rom': 'Remote Mario.sfc', 'stem': 'Remote Mario', 'filename': 'Remote Mario.sfc'}
        group = self.group('Remote Mario.srm')
        with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
            saves, '_open_peer', return_value=Response(json.dumps({'saves': [group], 'game': source}).encode())
        ):
            token = saves.discover(self.settings, self.item, 'remote')['saves'][0]['choice']
        self.apply(token)
        self.assertEqual((self.settings.saves_root / 'snes/Mario.srm').read_bytes(), b'new progress')
        self.assertFalse((self.settings.saves_root / 'snes/Remote Mario.srm').exists())

    def test_remote_rom_is_not_required_for_orphaned_saves(self):
        repo = SimpleNamespace(find_rom_by_path=mock.Mock(side_effect=FileNotFoundError),
                               list_assets=mock.Mock(side_effect=FileNotFoundError))
        source = saves.source_game(repo, 'snes', 'Mario.sfc', 'sample-fingerprint')
        self.assertEqual(source['stem'], 'Mario')
        self.write('snes/Mario.srm')
        self.assertEqual(saves.inventory(self.settings, source)[0]['path'], 'Mario.srm')

    def test_ryujinx_slot_is_matched_by_title_and_mapped_to_local_slot(self):
        title = '01006A800016E000'
        item = {**self.item, 'system': 'switch', 'ids': [title]}
        root = saves.roots(self.settings, item)['ryujinx-config']
        slot = '0000000000000001'
        self.write(slot + '/ExtraData', int(title, 16).to_bytes(8, 'little') + bytes(504), root=root)
        self.write(slot + '/0/progress.bin', b'old', root=root)
        group = self.group('0000000000000009/0', kind='directory', layout='ryujinx-config')
        self.apply(self.choose(group, item), item=item)
        self.assertEqual((root / slot / '0/progress.bin').read_bytes(), b'new progress')
        self.assertFalse((root / '0000000000000009').exists())
        self.assertEqual(saves.inventory(self.settings, item)[0]['path'], slot + '/0')

    def test_switch_source_storage_location_can_differ_from_local(self):
        title = '01006A800016E000'
        item = {**self.item, 'system': 'switch', 'ids': [title]}
        base = 'user/save/0000000000000000/'
        root = saves.roots(self.settings, item)['eden-saves']
        self.write(base + 'A' * 32 + '/' + title + '/old', b'old', root=root)
        group = self.group(base + 'B' * 32 + '/' + title, kind='directory', layout='eden-config')
        self.apply(self.choose(group, item), item=item)
        self.assertEqual((root / (base + 'A' * 32 + '/' + title + '/progress.bin')).read_bytes(), b'new progress')

    def test_running_game_blocks_import_before_network_or_installation(self):
        token = self.choose(self.group())
        with mock.patch.object(saves, 'find_running_emulatorlauncher', return_value={'pid': 42}), mock.patch.object(saves, '_open_peer') as network:
            with self.assertRaises(saves.SaveError):
                saves.import_choice(self.settings, self.item, token)
        network.assert_not_called()

    def test_foreign_switch_ids_and_unknown_manifest_fields_are_not_retained(self):
        source = saves._source_item(self.item, {**self.item, 'ids': ['0100000000000000'], 'untrusted': 'large payload'})
        self.assertFalse(source['ids'])
        self.assertNotIn('untrusted', source)
        group = self.group()
        group['extra'] = 'untrusted payload'
        clean = saves.validate_group(self.settings, self.item, group)
        self.assertNotIn('extra', clean)
        for field in ('layout', 'kind', 'path'):
            with self.subTest(field=field), self.assertRaises(saves.SaveError):
                saves.validate_group(self.settings, self.item, {**group, field: []})

    def test_absence_is_unknown_not_unsupported(self):
        self.assertEqual(saves.capabilities(self.item)['saves'], 'unknown')
        self.assertEqual(saves.capabilities(self.item)['states'], 'unknown')
        self.assertEqual(saves.capabilities({**self.item, 'system': 'imageviewer'})['saves'], 'unsupported')

    def test_redirects_are_refused(self):
        with self.assertRaises(saves.SaveError):
            saves._NoRedirect().redirect_request(None, None, 302, '', {}, 'http://attacker/secret')


class HandlerTests(unittest.TestCase):
    def handler(self):
        handler = HandlersGameSavesMixin()
        handler.auth = SimpleNamespace(authenticate_request=mock.Mock(return_value={'token': 'real-session'}))
        handler.settings = SimpleNamespace(admin_enabled=True, http_only=False)
        handler.headers = {'Content-Type': 'application/json', 'Host': 'local', 'Origin': 'http://local'}
        handler._send_json = mock.Mock()
        handler._send_unauthorized = mock.Mock()
        handler._read_json_body = mock.Mock(return_value={'choice': 'opaque'})
        handler._save_game = mock.Mock(return_value={})
        return handler

    def test_mutation_requires_real_cookie_admin_origin_and_json(self):
        for option in ('cookie', 'admin', 'origin', 'json'):
            with self.subTest(option=option):
                handler = self.handler()
                if option == 'cookie':
                    handler.auth.authenticate_request.return_value = None
                if option == 'admin':
                    handler.settings.admin_enabled = False
                if option == 'origin':
                    handler.headers['Origin'] = 'http://attacker'
                if option == 'json':
                    handler.headers['Content-Type'] = 'text/plain'
                with mock.patch.object(saves, 'import_choice') as imported:
                    handler._handle_game_saves_import('snes', 'id')
                imported.assert_not_called()

    def test_peer_export_requires_tls_and_authorized_paired_certificate(self):
        handler = self.handler()
        handler._peer_request_authorized = mock.Mock(return_value=False)
        with mock.patch.object(saves, 'inventory') as inventory:
            handler._handle_peer_game_saves({})
        inventory.assert_not_called()
        handler.settings.http_only = True
        handler._peer_request_authorized.return_value = True
        with mock.patch.object(saves, 'inventory') as inventory:
            handler._handle_peer_game_saves({})
        inventory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
