"""Integration contracts, isolated lifecycle and game orchestration; no hardware required."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.device.game_activity import find_running_emulatorlauncher
from app.integrations.registry import IntegrationDescriptor, IntegrationRegistry, build_integration_registry
from app.integrations.streamdeck.actions import ActionContext, BatoceraControl, BuiltInActionRegistry, RetroArchControl
from app.integrations.streamdeck.config import ConfigStore, DEFAULT_BRIGHTNESS, default_config, normalize_config, validate_button, validate_game
from app.integrations.streamdeck.dependencies import DependencyError, DependencyManager, interpreter_tag
from app.integrations.streamdeck.emulationstation import EmulationStationApi, EmulationStationUnavailable
from app.integrations.streamdeck.game_launcher import GameLauncher
from app.integrations.streamdeck.game_runtime import GameRuntime
from app.integrations.streamdeck.games import GameLibrary
from app.integrations.streamdeck.launch import LaunchGameCoordinator
from app.integrations.streamdeck.manager import StreamDeckIntegration
from app.integrations.streamdeck.paths import OwnershipError, StreamDeckPaths, atomic_write_json, try_file_lock
from app.integrations.streamdeck.process import ProcessResult


class FakeRunner:
    def __init__(self, result=None):
        self.result = result or ProcessResult(0, duration_seconds=.01)
        self.calls = []

    def run(self, executable, arguments=(), **kwargs):
        self.calls.append((str(executable), list(arguments), kwargs))
        return self.result


class FakeRepository:
    def __init__(self, root):
        self.root = Path(root)
        self.rows = {
            'snes': [{'unique_id': 'game-1', 'title': 'Super Game', 'rom_path': 'Super Game.sfc',
                      'gamelist': {'favorite': 'true'}, 'existing': {'image': './images/game.png'}}],
            'nes': [{'unique_id': 'game-2', 'title': 'Other Game', 'rom_path': 'Other.nes'}],
        }
        for system, rows in self.rows.items():
            (self.root / system).mkdir(parents=True)
            for row in rows:
                (self.root / system / row['rom_path']).write_bytes(b'rom')

    def list_assets(self, system, asset_type, include_fingerprint=False):
        return self.root / system, list(self.rows.get(system, []))

    def search_roms(self, query, limit=30, **kwargs):
        return [{'system': system, 'unique_id': row['unique_id'], 'name': row['title']}
                for system, rows in self.rows.items() for row in rows if query.lower() in row['title'].lower()][:limit]

    def list_systems(self):
        return [{'name': system, 'rom_count': len(rows)} for system, rows in self.rows.items()]


class FakeRuntime:
    def __init__(self, active=None, exits=True, starts=True, events=None):
        self.active, self.exits, self.starts = active, exits, starts
        self.events = events if events is not None else []

    def get_active_game(self):
        return self.active

    def is_game_running(self):
        return self.active is not None

    def wait_for_exit(self, timeout):
        self.events.append('wait-exit')
        if self.exits:
            self.active = None
        return self.exits

    def wait_for_start(self, path, timeout):
        self.events.append('wait-start')
        return {'pid': 42, 'rom_path': path} if self.starts else None


class FakeLauncher:
    def __init__(self, events=None, result=None):
        self.events = events if events is not None else []
        self.result = result or {'status': 'accepted'}
        self.calls = []

    def validate(self, game):
        return {'available': True, 'launch_path': '/roms/snes/new.sfc'}

    def launch(self, game):
        self.events.append('launch')
        self.calls.append(game)
        return self.result


class FakeActions:
    def __init__(self, events=None, result=None):
        self.events = events if events is not None else []
        self.result = result or {'status': 'ok'}

    def execute(self, action_id, context):
        self.events.append(action_id)
        return self.result


def wait_job(manager, reply):
    job_id = (reply.get('job') or reply)['id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.jobs.get(job_id)
        if job.get('finished_at'):
            return job
        time.sleep(.01)
    raise AssertionError('job did not finish')


class TemporaryPaths(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = StreamDeckPaths(self.root / 'integrations' / 'streamdeck')


class RegistryTests(unittest.TestCase):
    def test_metadata_disabled_enabled_and_health(self):
        status = {'enabled': False, 'health': 'disabled', 'installed': False}
        registry = IntegrationRegistry()
        descriptor = IntegrationDescriptor('example', 'Example', 'Description', 'icon', '#example', ('devices',))
        provider = SimpleNamespace(card_status=lambda: status)
        registry.register(descriptor, lambda: provider)
        self.assertIs(registry.get('example'), provider)
        self.assertEqual(registry.ids(), ['example'])
        card = registry.cards()[0]
        self.assertFalse(card['enabled'])
        self.assertEqual(card['capabilities'], ['devices'])
        status.update(enabled=True, installed=True, health='healthy')
        self.assertTrue(registry.cards()[0]['enabled'])
        self.assertEqual(registry.cards()[0]['health'], 'healthy')
        with self.assertRaises(ValueError):
            registry.register(descriptor, lambda: provider)
        with self.assertRaises(KeyError):
            registry.get('unknown')

    def test_broken_provider_is_reported_as_error(self):
        registry = IntegrationRegistry()
        registry.register(IntegrationDescriptor('a', 'A', '', '', ''), lambda: 1 / 0)
        self.assertEqual(registry.cards()[0]['health'], 'error')

    def test_shipped_registry_contains_only_streamdeck(self):
        self.assertEqual(build_integration_registry(None, None).ids(), ['streamdeck'])


class ConfigTests(TemporaryPaths):
    def setUp(self):
        super().setUp()
        self.store = ConfigStore(self.paths)

    def test_default_and_enabled_persistence(self):
        self.assertEqual(self.store.load()['default_profile_id'], 'default')
        self.store.set_enabled(True)
        self.assertTrue(ConfigStore(self.paths).load()['enabled'])

    def test_profile_create_rename_duplicate_delete_and_default(self):
        profile = self.store.create_profile('Games')
        self.store.set_button(profile['id'], 0, {'action_type': 'builtin', 'action_id': 'exit-game'})
        duplicate = self.store.create_profile('Copy', profile['id'])
        self.assertEqual(duplicate['buttons'][0]['action_id'], 'exit-game')
        self.store.update_profile(profile['id'], {'name': 'Arcade', 'default': True})
        with self.assertRaises(ValueError):
            self.store.delete_profile(profile['id'])
        self.store.delete_profile(duplicate['id'])
        self.assertEqual(self.store.load()['default_profile_id'], profile['id'])
        self.assertIn('Arcade', [p['name'] for p in self.store.load()['profiles']])

    def test_navigation_references_cleared_on_delete(self):
        target = self.store.create_profile('Extra')
        self.store.set_button('default', 0, {'action_type': 'profile', 'operation': 'go-to', 'profile_id': target['id']})
        self.store.set_rules([{'event': 'game-start', 'profile_id': target['id']}])
        result = self.store.delete_profile(target['id'])
        self.assertEqual(result['navigation_buttons_cleared'], 1)
        self.assertEqual(self.store.load()['context_rules'], [])

    def test_buttons_are_structured_and_bounded(self):
        button = self.store.set_button('default', 0, {'action_type': 'builtin', 'action_id': 'exit-game'})
        self.assertNotIn('command', button)
        for payload in ({'key': 0, 'action_type': 'game', 'command': 'anything'},
                        {'key': -1}, {'key': 64}, {'key': .2}, {'key': True},
                        {'key': 1, 'action_type': 'shell'}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_button(payload)
        with self.assertRaises(ValueError):
            validate_button({'key': 6}, key_count=6)

    def test_game_reference_rejects_absolute_and_traversal_paths(self):
        for path in ('/etc/passwd', '../outside', 'x/../../outside', 'bad\x00path'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_game({'id': 'game', 'system': 'snes', 'rom_path': path})

    def test_brightness_and_settings_limits(self):
        self.assertEqual(self.store.set_device_settings('serial', {'brightness': 999})['brightness'], 100)
        self.assertEqual(self.store.set_device_settings('serial', {'brightness': 0})['brightness'], 0)
        self.assertEqual(self.store.set_settings({'hold_duration_ms': 0})['hold_duration_ms'], 500)

    def test_default_brightness_is_full_and_auto_apply_cannot_be_disabled(self):
        self.assertEqual(default_config()['settings']['auto_apply'], True)
        self.assertEqual(DEFAULT_BRIGHTNESS, 100)
        self.assertEqual(self.store.set_device_settings('new-deck', {})['brightness'], 100)
        self.assertTrue(self.store.load()['settings']['auto_apply'])
        self.assertTrue(self.store.set_settings({'auto_apply': False})['auto_apply'])
        config, _warnings = normalize_config({'settings': {'auto_apply': False}})
        self.assertTrue(config['settings']['auto_apply'])

    def test_invalid_json_is_preserved_then_recovered_disabled(self):
        self.paths.ensure(self.paths.config_dir)
        self.store.path.write_text('{broken')
        self.assertFalse(self.store.load()['enabled'])
        self.assertTrue(list(self.paths.config_dir.glob('*.broken-*')))
        self.assertEqual(json.loads(self.store.path.read_text())['default_profile_id'], 'default')

    def test_malformed_members_do_not_crash_config_load(self):
        for field in ('profiles', 'devices', 'context_rules'):
            for value in (42, 'bad', {'oops': True}):
                with self.subTest(field=field, value=value):
                    config, warnings = normalize_config({field: value})
                    self.assertTrue(warnings)
                    self.assertTrue(config['profiles'])
        config, warnings = normalize_config({'profiles': [{'id': 'a', 'buttons': 42}]})
        self.assertEqual(config['profiles'][0]['buttons'], [])
        self.assertTrue(warnings)

    def test_symlink_is_not_treated_as_corrupt_config(self):
        outside = self.root / 'outside'
        outside.mkdir()
        secret = outside / 'streamdeck.json'
        secret.write_text('{invalid')
        self.paths.ensure(self.paths.root)
        self.paths.config_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OwnershipError):
            self.store.load()
        self.assertEqual(secret.read_text(), '{invalid')
        self.assertEqual(list(outside.iterdir()), [secret])


class ActionTests(unittest.TestCase):
    def registry(self, active=None, code=0):
        runner = FakeRunner(ProcessResult(code))
        control = BatoceraControl(runner, which=lambda name: '/usr/bin/' + name,
                                  restart_frontend=lambda: True)
        retroarch = SimpleNamespace(availability=lambda c: {'available': bool(c.active_game), 'reason': 'No game'},
                                    send=lambda command: {'status': 'ok', 'command': command})
        return BuiltInActionRegistry(control, retroarch, FakeRuntime(active)), runner

    def test_initial_ten_actions_and_implementations(self):
        registry, runner = self.registry({'rom_path': '/old'})
        expected = ['exit-game', 'reboot-system', 'shutdown-system', 'restart-emulationstation',
                    'volume-up', 'volume-down', 'mute-toggle', 'pause-toggle', 'save-state', 'load-state']
        self.assertEqual(registry.ids(), expected)
        for action in expected:
            self.assertEqual(registry.execute(action, registry.context(confirmed=True))['status'], 'ok')
        self.assertEqual([call[1] for call in runner.calls],
                         [['--emukill'], ['--reboot'], ['--shutdown'], ['setSystemVolume', '+5'],
                          ['setSystemVolume', '-5'], ['setSystemVolume', 'mute-toggle']])
        self.assertEqual(registry.execute('save-state')['command'], 'SAVE_STATE')

    def test_availability_unknown_failure_and_confirmation(self):
        registry, runner = self.registry()
        self.assertEqual(registry.execute('exit-game')['status'], 'unavailable')
        self.assertEqual(registry.execute('unknown')['status'], 'error')
        self.assertEqual(registry.execute('reboot-system')['status'], 'confirmation-required')
        self.assertEqual(runner.calls, [])
        registry, _ = self.registry({'rom_path': '/old'}, code=1)
        self.assertEqual(registry.execute('exit-game')['status'], 'error')

    def test_swissknife_informational_exit_codes_are_accepted(self):
        for code in (20, 21, 22, 25):
            registry, _ = self.registry({'rom_path': '/old'}, code=code)
            self.assertEqual(registry.execute('exit-game')['status'], 'ok')

    def test_missing_tools_unavailable(self):
        registry = BuiltInActionRegistry(BatoceraControl(which=lambda _: None), game_runtime=FakeRuntime())
        self.assertFalse(registry.availability('volume-up')['available'])

    def test_retroarch_requires_active_config_and_supported_emulator(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            control = RetroArchControl(root)
            context = ActionContext(active_game={'system': 'snes'})
            self.assertFalse(control.availability(context)['available'])
            config = root / 'retroarch.cfg'
            config.write_text('network_cmd_enable = "false"\n')
            (root / '123').mkdir()
            (root / '123' / 'cmdline').write_bytes(f'/usr/bin/retroarch\0--config\0{config}\0'.encode())
            self.assertIn('disabled', control.availability(context)['reason'])
            config.write_text('network_cmd_enable = "true"\nnetwork_cmd_port = "55355"\n')
            self.assertTrue(control.availability(context)['available'])
            sock = mock.MagicMock()
            sock.__enter__.return_value = sock
            sock.recv.return_value = b'GET_STATUS PLAYING snes,game'
            with mock.patch('app.integrations.streamdeck.actions.socket.socket', return_value=sock):
                self.assertEqual(control.send('SAVE_STATE')['status'], 'ok')
            self.assertEqual(sock.send.call_args_list[-1].args, (b'SAVE_STATE\n',))


class LaunchTests(unittest.TestCase):
    def make(self, active=None, exits=True, starts=True, result=None):
        events = []
        runtime = FakeRuntime(active, exits, starts, events)
        launcher = FakeLauncher(events, result)
        return LaunchGameCoordinator(runtime, launcher, FakeActions(events)), events, launcher

    def test_launch_without_current_game(self):
        coordinator, events, _ = self.make()
        self.assertEqual(coordinator.launch({'id': 'new'})['status'], 'launched')
        self.assertEqual(events, ['launch', 'wait-start'])

    def test_existing_game_must_exit_before_launch(self):
        coordinator, events, _ = self.make({'rom_path': '/roms/old', 'system': 'snes'})
        self.assertEqual(coordinator.launch({'id': 'new'})['status'], 'launched')
        self.assertEqual(events, ['exit-game', 'wait-exit', 'launch', 'wait-start'])

    def test_exit_timeout_never_launches_second_game(self):
        coordinator, events, launcher = self.make({'rom_path': '/old'}, exits=False)
        self.assertEqual(coordinator.launch({'id': 'new'})['stage'], 'exit-timeout')
        self.assertEqual(events, ['exit-game', 'wait-exit'])
        self.assertFalse(launcher.calls)

    def test_exit_failure_prevents_launch(self):
        coordinator, _, launcher = self.make({'rom_path': '/old'})
        coordinator.actions = FakeActions(result={'status': 'error', 'error': 'denied'})
        self.assertEqual(coordinator.launch({'id': 'new'})['stage'], 'exit')
        self.assertFalse(launcher.calls)

    def test_failed_launch_and_unconfirmed_start_are_distinct(self):
        coordinator, _, _ = self.make(result={'status': 'error', 'error': 'ES stopped'})
        self.assertEqual(coordinator.launch({'id': 'new'})['stage'], 'launch')
        coordinator, _, _ = self.make(starts=False)
        self.assertEqual(coordinator.launch({'id': 'new'})['status'], 'launch-unconfirmed')

    def test_competing_launch_is_rejected_until_transition_finishes(self):
        coordinator, _, launcher = self.make()
        entered, release = threading.Event(), threading.Event()
        def launch(game):
            entered.set()
            release.wait(3)
            return {'status': 'accepted'}
        launcher.launch = launch
        thread = threading.Thread(target=coordinator.launch, args=({'id': 'first'},))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(coordinator.launch({'id': 'second'})['status'], 'busy')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_process_lock_is_shared_by_web_tests_and_worker(self):
        with tempfile.TemporaryDirectory() as raw:
            coordinator, _, launcher = self.make()
            coordinator.lock_path = Path(raw) / 'launch.lock'
            with try_file_lock(coordinator.lock_path) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(coordinator.launch({'id': 'new'})['status'], 'busy')
            self.assertFalse(launcher.calls)

    def test_invalid_rom_rejected_before_exit_request(self):
        coordinator, events, _ = self.make({'rom_path': '/old'})
        coordinator.launcher.validate = lambda g: {'available': False, 'reason': 'missing'}
        self.assertEqual(coordinator.launch({'id': 'new'})['stage'], 'validate')
        self.assertFalse(events)


class GameTests(TemporaryPaths):
    def setUp(self):
        super().setUp()
        self.repository = FakeRepository(self.root / 'roms')
        self.library = GameLibrary(self.repository, self.root / 'roms')
        self.game = self.library.search('Super')['items'][0]

    def test_library_search_system_filter_favorites_and_pagination(self):
        self.assertEqual(len(self.library.systems()), 2)
        self.assertEqual(self.game['name'], 'Super Game')
        self.assertTrue(self.game['favorite'])
        self.assertEqual(self.library.search('Game', system='nes')['items'][0]['system'], 'nes')
        self.assertTrue(self.library.search('Game', limit=1)['has_more'])
        self.assertEqual(len(self.library.search(system='snes')['items']), 1)

    def test_game_artwork_resolution_and_confinement(self):
        image = self.root / 'roms/snes/images/game.png'
        image.parent.mkdir()
        image.write_bytes(b'image')
        # Resolved (macOS tmp dirs live behind the /var -> /private/var symlink).
        self.assertEqual(self.library.artwork_path(self.game), image.resolve())
        image.unlink()
        outside = self.root / 'secret.png'
        outside.write_bytes(b'secret')
        image.symlink_to(outside)
        self.assertIsNone(self.library.artwork_path(self.game))

    def test_missing_game_and_path_relink(self):
        self.assertEqual(self.library.resolve(self.game)['resolution'], 'id')
        self.repository.rows['snes'][0]['unique_id'] = 'rescanned'
        self.library.invalidate()
        resolved = self.library.resolve(self.game)
        self.assertEqual((resolved['resolution'], resolved['id']), ('path', 'rescanned'))
        (self.root / 'roms/snes/Super Game.sfc').unlink()
        self.assertFalse(self.library.resolve(self.game)['installed'])

    def test_unindexed_file_is_not_accepted_as_a_game(self):
        (self.root / 'roms/snes/not-a-rom.txt').write_text('x')
        self.assertFalse(self.library.resolve({**self.game, 'id': 'unknown', 'rom_path': 'not-a-rom.txt'})['installed'])

    def test_normal_launch_uses_es_api_with_exact_path_and_no_shell(self):
        api = mock.Mock()
        api.launch.return_value = (200, '')
        launcher = GameLauncher(self.root / 'roms', api)
        self.assertEqual(launcher.launch(self.game)['status'], 'accepted')
        api.launch.assert_called_once_with(str(self.root / 'roms/snes/Super Game.sfc'))

    def test_launcher_missing_escape_and_es_failure(self):
        api = mock.Mock()
        launcher = GameLauncher(self.root / 'roms', api)
        for path in ('absent.sfc', '../outside', '/etc/passwd'):
            self.assertFalse(launcher.validate({**self.game, 'rom_path': path})['available'])
        api.launch.return_value = (404, '')
        self.assertIn('gamelists', launcher.launch(self.game)['error'])
        api.launch.side_effect = EmulationStationUnavailable('offline')
        self.assertEqual(launcher.launch(self.game)['status'], 'error')

    def test_es_api_is_loopback_only(self):
        for url in ('https://example.com', 'http://192.168.1.10:1234', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                EmulationStationApi(url)

    def test_active_game_detection_both_launcher_names_and_spaces(self):
        proc = self.root / 'proc'
        (proc / '123').mkdir(parents=True)
        for name in ('emulatorlauncher.py', 'batocera-launch'):
            (proc / '123/cmdline').write_bytes(f'python3\0/usr/bin/{name}\0-system\0snes\0-rom\0/roms/Game Name.sfc\0'.encode())
            active = find_running_emulatorlauncher(proc)
            self.assertEqual(active['rom_path'], '/roms/Game Name.sfc')
            runtime = GameRuntime(detector=lambda: active)
            self.assertEqual(runtime.get_active_game()['system'], 'snes')
        for argv in ('/usr/bin/python3\0-u\0/usr/bin/emulatorlauncher\0', '/usr/bin/python3\0-m\0configgen.emulatorlauncher\0',
                     'emulatorlauncher\0'):
            (proc / '123/cmdline').write_bytes(f'{argv}-system\0n64\0-rom\0/roms/a.z64\0'.encode())
            self.assertEqual(find_running_emulatorlauncher(proc)['system_name'], 'n64', argv)
        for argv in (b'editor\0-rom\0/roms/emulatorlauncher.txt\0',
                     b'grep\0emulatorlauncher\0-system\0snes\0-rom\0/x\0',
                     b'retroarch\0-L\0core.so\0/roms/emulatorlauncher\0-system\0snes\0-rom\0/x\0'):
            (proc / '123/cmdline').write_bytes(argv)
            self.assertIsNone(find_running_emulatorlauncher(proc), argv)

    def test_runtime_bounded_wait_and_lifecycle_events(self):
        clock = [0.0]
        active = [{'system_name': 'snes', 'rom_path': '/roms/game', 'pid': 123}]
        runtime = GameRuntime(detector=lambda: active[0], clock=lambda: clock[0],
                              sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        events = []
        runtime.subscribe(lambda event, game: events.append(event))
        runtime.poll()
        self.assertIsNotNone(runtime.get_active_game()['started_at'])
        self.assertFalse(runtime.wait_for_exit(.5))
        active[0] = None
        self.assertTrue(runtime.wait_for_exit(.5))
        runtime.poll()
        self.assertEqual(events, ['game-start', 'game-stop'])


class LifecycleTests(TemporaryPaths):
    def setUp(self):
        super().setUp()
        repository = FakeRepository(self.root / 'roms')
        self.manager = StreamDeckIntegration(SimpleNamespace(roms_root=self.root / 'roms'), repository,
                                             paths=self.paths, usb_detector=lambda: [])

    def test_disabled_status_and_compiled_profile(self):
        self.assertFalse(self.manager.get_status()['enabled'])
        self.assertEqual(self.manager.card_status()['health'], 'disabled')
        self.manager.set_button('default', 0, {'action_type': 'builtin', 'action_id': 'exit-game'})
        document = self.manager.compile_runtime()
        self.assertEqual(document['profiles'][0]['buttons'][0]['render']['text'], 'EXIT')
        self.assertEqual(self.manager.apply()['status'], 'saved')

    def test_enable_twice_preserves_profiles_and_jobs_progress(self):
        self.manager.config.create_profile('Keep')
        with mock.patch.object(self.manager.dependencies, 'ensure') as install, \
             mock.patch.object(self.manager, 'start_runtime'), \
             mock.patch.object(self.manager, '_wait_for_runtime', return_value=0):
            for _ in range(2):
                job = wait_job(self.manager, self.manager.enable())
                self.assertEqual(job['status'], 'completed')
                self.assertIn('Connecting...', job['steps'])
        self.assertTrue(self.manager.config.load()['enabled'])
        self.assertIn('Keep', [p['name'] for p in self.manager.config.load()['profiles']])

    def test_install_failure_can_be_retried(self):
        with mock.patch.object(self.manager.dependencies, 'ensure', side_effect=DependencyError('offline')):
            job = wait_job(self.manager, self.manager.enable())
        self.assertEqual(job['status'], 'failed')
        self.assertIn('offline', job['error'])
        self.assertFalse(self.manager.config.load()['enabled'])
        self.assertTrue(self.manager._lifecycle_lock.acquire(False))
        self.manager._lifecycle_lock.release()

    def test_disable_preserves_content_and_cannot_race_install(self):
        self.manager.config.set_enabled(True)
        self.manager.config.create_profile('Keep')
        self.manager.lifecycle_lock_wait = 0.05
        self.manager._lifecycle_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.manager.disable()
            self.assertEqual(self.manager.supervise_once(), 'idle')
        finally:
            self.manager._lifecycle_lock.release()
        self.manager.disable()
        self.assertFalse(self.manager.config.load()['enabled'])
        self.assertEqual(len(self.manager.config.load()['profiles']), 2)

    def test_stop_terminates_a_worker_that_has_not_acquired_its_lock_yet(self):
        process = mock.Mock()
        process.poll.return_value = None
        self.manager._process = process
        self.manager.stop_runtime()
        process.terminate.assert_called_once()
        self.assertIsNone(self.manager._process)

    def test_start_prevents_duplicate_pending_processes(self):
        process = mock.Mock(pid=123)
        process.poll.return_value = None
        self.manager._process = process
        with mock.patch.object(self.manager, 'process_factory') as spawn:
            self.assertEqual(self.manager.start_runtime()['status'], 'starting')
        spawn.assert_not_called()

    def test_supervisor_restores_enabled_worker_and_backs_off(self):
        self.manager.config.set_enabled(True)
        with mock.patch.object(self.manager.dependencies, 'installed', return_value=True), \
             mock.patch.object(self.manager, 'start_runtime') as start:
            self.assertEqual(self.manager.supervise_once(), 'started')
            self.assertEqual(self.manager.supervise_once(), 'backoff')
            start.assert_called_once()
        self.manager.disable()
        self.assertEqual(self.manager.supervise_once(), 'disabled')

    def test_repair_verifies_and_reinstalls_corrupt_libraries(self):
        with mock.patch.object(self.manager.dependencies, 'installed', return_value=True), \
             mock.patch.object(self.manager.dependencies, 'verify', side_effect=DependencyError('broken')), \
             mock.patch.object(self.manager.dependencies, 'ensure') as ensure:
            self.assertEqual(wait_job(self.manager, self.manager.repair())['status'], 'completed')
        self.assertTrue(ensure.call_args.kwargs['force'])

    def test_reinstall_keeps_configuration(self):
        self.manager.config.create_profile('Keep')
        with mock.patch.object(self.manager.dependencies, 'ensure'), \
             mock.patch.object(self.manager.dependencies, 'remove') as remove:
            self.assertEqual(wait_job(self.manager, self.manager.repair(reinstall=True))['status'], 'completed')
        remove.assert_called_once()
        self.assertEqual(len(self.manager.config.load()['profiles']), 2)

    def test_remove_tooling_then_full_removal_is_narrow(self):
        self.manager.config.set_enabled(True)
        self.paths.ensure_layout()
        self.paths.ensure(self.paths.lib_dir)
        (self.paths.lib_dir / 'owned').write_text('x')
        script = self.manager.scripts.create({'name': 'Keep', 'code': '#!/bin/sh\ntrue'})
        unrelated = self.root / 'pip-and-setuptools'
        unrelated.write_text('untouched')
        self.manager.remove()
        self.assertFalse(self.paths.lib_dir.exists())
        self.assertTrue(self.paths.config_file.exists())
        self.assertEqual(self.manager.scripts.get(script['id'])['name'], 'Keep')
        self.manager.remove(include_configuration=True)
        self.assertFalse(self.paths.root.exists())
        self.assertEqual(unrelated.read_text(), 'untouched')

    def test_removal_refuses_redirected_integration_parent(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.paths.root.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OwnershipError):
            self.manager.remove(include_configuration=True)
        self.assertTrue(outside.exists())

    def test_game_assignment_and_web_test_share_dispatcher(self):
        game = self.manager.games.search('Super')['items'][0]
        result = self.manager.set_button('default', 1, {'action_type': 'game', 'game': game})
        self.assertEqual(result['button']['game']['id'], game['id'])
        with self.assertRaises(ValueError):
            self.manager.start_test_action({'action_type': 'game', 'game': game})
        with mock.patch.object(self.manager.dispatcher, 'execute', return_value={'status': 'launched'}) as execute:
            job = wait_job(self.manager, self.manager.start_test_action({'action_type': 'game', 'game': game, 'confirmed': True}))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(execute.call_args.args[0]['game']['id'], game['id'])
        self.assertTrue(execute.call_args.args[1].confirmed)

    def test_test_connection_without_tooling_reports_usb_detection_only(self):
        self.manager.usb_detector = lambda: [{'id': 'AL1', 'model': 'Stream Deck Mini', 'usb_id': '0fd9:0063'}]
        result = self.manager.test_connection()
        self.assertEqual((result['status'], result['via'], len(result['usb'])), ('ok', 'usb', 1))
        self.assertEqual(self.manager.devices()[0]['runtime_state'], 'not-open')

    def test_hid_truncated_serial_is_merged_with_usb_detection(self):
        hid, usb = 'A00DA6261KKZ', 'A00DA6261KKZB0'
        self.manager.usb_detector = lambda: [{
            'id': usb, 'model': 'Stream Deck Mini', 'serial': usb, 'usb_id': '0fd9:0063',
            'usb_path': '7-2', 'rows': 2, 'columns': 3, 'key_count': 6, 'known_model': True,
        }]
        self.manager.config.set_device_settings(usb, {'brightness': 40})
        rows = self.manager.devices(worker={'devices': [{
            'id': hid, 'model': 'Stream Deck Mini', 'serial': hid, 'firmware': '3.03.002',
            'key_count': 6, 'rows': 2, 'columns': 3, 'key_image_size': [80, 80],
        }]})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['id'], row['source'], row['runtime_state']), (hid, 'runtime', 'open'))
        self.assertEqual((row['serial'], row['usb_id'], row['brightness']), (usb, '0fd9:0063', 40))

    def test_remembered_truncated_serial_does_not_duplicate_live_usb_device(self):
        hid, usb = 'A00DA6261KKZ', 'A00DA6261KKZB0'
        atomic_write_json(self.paths.state_dir / 'known-devices.json', {
            hid: {'id': hid, 'model': 'Stream Deck Mini', 'serial': hid, 'key_count': 6, 'rows': 2, 'columns': 3},
        })
        self.manager.usb_detector = lambda: [{
            'id': usb, 'model': 'Stream Deck Mini', 'serial': usb, 'usb_id': '0fd9:0063',
            'usb_path': '7-2', 'rows': 2, 'columns': 3, 'key_count': 6, 'known_model': True,
        }]
        rows = self.manager.devices(worker={})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['id'], rows[0]['source'], rows[0]['serial']), (usb, 'usb', usb))

    def test_apply_and_brightness_reach_a_running_runtime_without_restart(self):
        self.manager.config.set_enabled(True)
        with mock.patch.object(self.manager, 'runtime_info', return_value={'running': True, 'pid': 1}), \
             mock.patch.object(self.manager, 'send_command', return_value={'status': 'ok', 'devices': 1}) as send:
            self.assertEqual(self.manager.apply()['status'], 'applied')
            result = self.manager.set_device_settings('AL1', {'brightness': 25})
        self.assertEqual(result['device']['brightness'], 25)
        self.assertEqual([call.args[0] for call in send.call_args_list], ['reload', 'reload'])
        self.assertEqual(self.manager.config.load()['devices'][0]['brightness'], 25)
        self.assertTrue(self.paths.runtime_document.is_file())

    def test_saving_buttons_settings_and_rules_always_applies(self):
        self.manager.config.set_enabled(True)
        self.manager.config.set_settings({'auto_apply': False})
        self.assertTrue(self.manager.config.load()['settings']['auto_apply'])
        with mock.patch.object(self.manager, 'runtime_info', return_value={'running': True, 'pid': 1}), \
             mock.patch.object(self.manager, 'send_command', return_value={'status': 'ok'}) as send:
            button = self.manager.set_button('default', 0, {'action_type': 'builtin', 'action_id': 'exit-game'})
            settings = self.manager.update_settings({'hold_duration_ms': 2000})
            rules = self.manager.update_rules([{'event': 'game-stop', 'profile_id': 'default'}])
        self.assertTrue(button['applied']['applied'])
        self.assertTrue(settings['settings']['auto_apply'])
        self.assertEqual(settings['settings']['hold_duration_ms'], 2000)
        self.assertTrue(settings['applied']['applied'])
        self.assertEqual(rules['context_rules'][0]['event'], 'game-stop')
        self.assertTrue(rules['applied']['applied'])
        self.assertEqual([call.args[0] for call in send.call_args_list], ['reload', 'reload', 'reload'])

    def test_script_assigned_to_a_button_cannot_be_deleted(self):
        script = self.manager.scripts.create({'name': 'Lights', 'code': '#!/bin/sh\ntrue\n'})
        self.manager.set_button('default', 2, {'action_type': 'script', 'script_id': script['id']})
        references = self.manager.script_references(script['id'])
        self.assertEqual(references[0]['key'], 2)
        with self.assertRaises(ValueError):
            self.manager.scripts.delete(script['id'], references)
        with self.assertRaises(ValueError):
            self.manager.set_button('default', 3, {'action_type': 'script', 'script_id': 'f' * 32})


class DependencyTests(TemporaryPaths):
    def marker(self):
        self.paths.ensure(self.paths.state_dir, self.paths.lib_dir / 'StreamDeck', self.paths.lib_dir / 'PIL')
        for name in ('StreamDeck', 'PIL'):
            (self.paths.lib_dir / name / '__init__.py').write_text('')
        atomic_write_json(self.paths.dependency_marker, {'schema': 1, 'streamdeck': '0.10.0', 'pillow': '11',
                                                        'interpreter': interpreter_tag()})

    def test_installed_dependencies_are_not_reinstalled(self):
        self.marker()
        runner = FakeRunner()
        deps = DependencyManager(self.paths, runner)
        self.assertTrue(deps.ensure()['installed'])
        self.assertFalse(runner.calls)

    def test_missing_package_and_interpreter_change_require_repair(self):
        self.marker()
        deps = DependencyManager(self.paths)
        (self.paths.lib_dir / 'PIL/__init__.py').unlink()
        self.assertFalse(deps.installed())
        self.marker()
        with mock.patch('app.integrations.streamdeck.dependencies.interpreter_tag', return_value='new-python'):
            self.assertFalse(deps.installed())

    def test_failed_staged_install_keeps_previous_libraries(self):
        self.marker()
        (self.paths.lib_dir / 'keep').write_text('old')
        runner = FakeRunner(ProcessResult(1, stderr='offline'))
        deps = DependencyManager(self.paths, runner)
        with mock.patch.object(deps, '_python_available'), mock.patch.object(deps, '_pip', return_value=(['python', '-m', 'pip'], 'venv')):
            with self.assertRaises(DependencyError):
                deps.ensure(force=True)
        self.assertEqual((self.paths.lib_dir / 'keep').read_text(), 'old')
        self.assertIn('--target', runner.calls[0][1])
        self.assertNotIn('uninstall', runner.calls[0][1])

    def test_bundled_pip_is_executed_from_wheel_without_global_ensurepip(self):
        deps = DependencyManager(self.paths, FakeRunner(ProcessResult(0, stdout='/python/ensurepip/_bundled/pip-1.whl\n')))
        with mock.patch.object(deps, '_venv_pip', return_value=None):
            command, method = deps._pip(None)
        self.assertEqual(method, 'bundled-pip')
        self.assertIn('/python/ensurepip/_bundled/pip-1.whl', command)
        self.assertNotIn('--upgrade', command)

    def test_private_venv_symlink_is_rejected(self):
        self.paths.ensure(self.paths.root)
        self.paths.python_dir.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OwnershipError):
            DependencyManager(self.paths)._venv_pip(None)


if __name__ == '__main__':
    unittest.main()
