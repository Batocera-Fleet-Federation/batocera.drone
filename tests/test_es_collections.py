import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app.device.es_collections as es_collections
from app.drone_api import RomRepository, Settings, _execute_overmind_action

ES_SYSTEMS_XML = """<?xml version="1.0"?>
<systemList>
  <system>
    <name>snes</name>
    <fullname>Super Nintendo</fullname>
    <path>/userdata/roms/snes</path>
  </system>
  <system>
    <name>genesis</name>
    <fullname>Sega Genesis</fullname>
    <path>/userdata/roms/genesis</path>
    <group>megadrive</group>
  </system>
  <system>
    <name>megadrive-jp</name>
    <fullname>Mega Drive (Japan)</fullname>
    <path>/userdata/roms/megadrive-jp</path>
    <group>megadrive</group>
  </system>
  <system>
    <name>deprecated-sys</name>
    <fullname>Deprecated</fullname>
    <path>/userdata/roms/deprecated-sys</path>
    <hidden>true</hidden>
  </system>
</systemList>
"""

ES_SETTINGS_XML = """<?xml version="1.0"?>
<map>
  <string name="ThemeSet" value="carbon" />
  <int name="MusicVolume" value="80" />
  <int name="ScreenSaverTime" value="600000" />
  <string name="HiddenSystems" value="snes" />
  <string name="CollectionSystemsAuto" value="favorites,recent" />
  <string name="CollectionSystemsCustom" value="beatemup" />
  <bool name="megadrive-jp.ungroup" value="true" />
</map>
"""


def _build_settings(root: Path, *, with_data: bool = True) -> Settings:
    es_settings = root / "system" / "configs" / "emulationstation" / "es_settings.cfg"
    es_systems = root / "es_systems.cfg"
    collections_dir = root / "system" / "configs" / "emulationstation" / "collections"
    es_settings.parent.mkdir(parents=True, exist_ok=True)
    collections_dir.mkdir(parents=True, exist_ok=True)
    if with_data:
        es_settings.write_text(ES_SETTINGS_XML, encoding="utf-8")
        es_systems.write_text(ES_SYSTEMS_XML, encoding="utf-8")
        (collections_dir / "custom-beatemup.cfg").write_text("", encoding="utf-8")
        (collections_dir / "custom-fighting.cfg").write_text("", encoding="utf-8")
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "ES_SETTINGS_FILE": str(es_settings),
        "ES_SYSTEMS_FILE": str(es_systems),
        "DRONE_SERVICE_CONTROL_DIR": str(root / "system" / "drone-app" / "control"),
        "OVERMIND_DEVICE_ID": "local-test",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


class GetEsCollectionsStateTests(unittest.TestCase):
    def test_reads_music_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            self.assertEqual(state["music_volume"], 80)

    def test_defaults_music_volume_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp), with_data=False)
            state = es_collections.get_es_collections_state(settings)
            self.assertEqual(state["music_volume"], es_collections.DEFAULT_MUSIC_VOLUME)

    def test_reads_screensaver_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            self.assertEqual(state["screensaver_minutes"], 10)  # 600000ms

    def test_defaults_screensaver_minutes_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp), with_data=False)
            state = es_collections.get_es_collections_state(settings)
            self.assertEqual(state["screensaver_minutes"], es_collections.DEFAULT_SCREENSAVER_MINUTES)

    def test_systems_reflect_hidden_systems_and_exclude_definition_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            by_name = {row["name"]: row for row in state["systems"]}
            self.assertEqual(by_name["snes"]["displayed"], False)  # in HiddenSystems
            self.assertEqual(by_name["genesis"]["displayed"], True)
            self.assertNotIn("deprecated-sys", by_name)  # definition-hidden, not a candidate

    def test_groups_reflect_per_system_ungroup_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            self.assertEqual(len(state["groups"]), 1)
            group = state["groups"][0]
            self.assertEqual(group["group"], "megadrive")
            children = {row["name"]: row for row in group["children"]}
            self.assertEqual(children["genesis"]["grouped"], True)  # no ungroup flag set
            self.assertEqual(children["megadrive-jp"]["grouped"], False)  # ungroup=true

    def test_auto_collections_enabled_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            by_name = {row["name"]: row["enabled"] for row in state["auto_collections"]}
            self.assertTrue(by_name["favorites"])
            self.assertTrue(by_name["recent"])
            self.assertFalse(by_name["wheel"])
            self.assertEqual(len(state["auto_collections"]), len(es_collections.AUTO_COLLECTION_DECLS))

    def test_custom_collections_include_discovered_and_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            state = es_collections.get_es_collections_state(settings)
            by_name = {row["name"]: row["enabled"] for row in state["custom_collections"]}
            self.assertTrue(by_name["beatemup"])  # enabled + discovered
            self.assertFalse(by_name["fighting"])  # discovered, not enabled


class ApplyEsCollectionsTests(unittest.TestCase):
    def test_root_direct_applies_music_volume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                result = es_collections.apply_es_collections(settings, {"music_volume": 55})
            helper.assert_called_once()
            args, kwargs = helper.call_args
            self.assertEqual(args[0], {"music_volume": 55})
            self.assertEqual(kwargs["config"], settings.es_settings_file)
            self.assertIsInstance(result, dict)

    def test_root_direct_computes_full_ungroup_universe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                es_collections.apply_es_collections(settings, {"ungrouped_systems": ["genesis"]})
            args, _ = helper.call_args
            # genesis explicitly ungrouped; megadrive-jp (previously ungrouped) is now
            # re-grouped since it's absent from the new desired list (full-replace semantics).
            self.assertEqual(args[0]["ungroup"], {"genesis": True, "megadrive-jp": False})

    def test_hidden_systems_and_collections_join_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                es_collections.apply_es_collections(settings, {
                    "hidden_systems": ["genesis", "snes"],
                    "auto_collections": ["wheel"],
                    "custom_collections": ["fighting", "beatemup"],
                })
            args, _ = helper.call_args
            self.assertEqual(args[0]["hidden_systems"], "genesis;snes")
            self.assertEqual(args[0]["auto_collections"], "wheel")
            self.assertEqual(args[0]["custom_collections"], "beatemup,fighting")

    def test_invalid_music_volume_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                es_collections.apply_es_collections(settings, {"music_volume": "not-a-number"})

    def test_screensaver_minutes_converts_to_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                es_collections.apply_es_collections(settings, {"screensaver_minutes": 15})
            self.assertEqual(helper.call_args[0][0], {"screensaver_time_ms": 900000})

    def test_screensaver_minutes_clamped_to_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                es_collections.apply_es_collections(settings, {"screensaver_minutes": 999})
            self.assertEqual(helper.call_args[0][0]["screensaver_time_ms"], es_collections.MAX_SCREENSAVER_MINUTES * 60000)

    def test_invalid_screensaver_minutes_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                es_collections.apply_es_collections(settings, {"screensaver_minutes": "forever"})

    def test_empty_updates_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with self.assertRaises(ValueError):
                es_collections.apply_es_collections(settings, {})

    def test_non_root_dispatches_to_privileged_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=999), \
                 mock.patch("app.device.es_collections._request_es_collections_service_control", return_value=True) as dispatch:
                es_collections.apply_es_collections(settings, {"music_volume": 40})
            dispatch.assert_called_once_with({"music_volume": 40})

    def test_non_root_raises_when_worker_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            with mock.patch("app.device.es_collections.os.geteuid", return_value=999), \
                 mock.patch("app.device.es_collections._request_es_collections_service_control", return_value=False):
                with self.assertRaises(OSError):
                    es_collections.apply_es_collections(settings, {"music_volume": 40})


class PrivilegedEsCollectionsHelperTests(unittest.TestCase):
    def test_writes_all_field_types_and_restarts_emulationstation(self) -> None:
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            config.write_text(ES_SETTINGS_XML, encoding="utf-8")
            with mock.patch("app.set_es_collections.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run:
                set_es_collections.apply_es_collections(
                    {
                        "music_volume": 33,
                        "screensaver_time_ms": 900000,
                        "hidden_systems": "genesis;snes",
                        "auto_collections": "wheel",
                        "custom_collections": "fighting",
                        "ungroup": {"genesis": True, "megadrive-jp": False},
                    },
                    config=config,
                )
            text = config.read_text(encoding="utf-8")
            self.assertIn('name="MusicVolume" value="33"', text)
            self.assertIn('name="ScreenSaverTime" value="900000"', text)
            self.assertIn('name="HiddenSystems" value="genesis;snes"', text)
            self.assertIn('name="CollectionSystemsAuto" value="wheel"', text)
            self.assertIn('name="CollectionSystemsCustom" value="fighting"', text)
            self.assertIn('name="genesis.ungroup" value="true"', text)
            self.assertIn('name="megadrive-jp.ungroup" value="false"', text)
            self.assertIn('name="ThemeSet" value="carbon"', text)  # untouched
            run_commands = [call.args[0] for call in run.call_args_list]
            self.assertIn([set_es_collections.EMULATIONSTATION_SERVICE, "stop"], run_commands)
            self.assertIn(["batocera-save-overlay"], run_commands)
            self.assertIn([set_es_collections.EMULATIONSTATION_SERVICE, "start"], run_commands)

    def test_restarts_even_when_overlay_save_fails(self) -> None:
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                rc = 1 if command and command[0] == "batocera-save-overlay" else 0
                return mock.Mock(returncode=rc, stdout="overlay failure" if rc else "")

            with mock.patch("app.set_es_collections.subprocess.run", side_effect=fake_run):
                set_es_collections.apply_es_collections({"hidden_systems": "snes"}, config=config)
            self.assertIn('name="HiddenSystems" value="snes"', config.read_text(encoding="utf-8"))
            self.assertIn([set_es_collections.EMULATIONSTATION_SERVICE, "start"], calls)

    def test_music_volume_and_screensaver_write_without_restarting(self) -> None:
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return mock.Mock(returncode=0, stdout="")

            with mock.patch("app.set_es_collections.subprocess.run", side_effect=fake_run):
                set_es_collections.apply_es_collections(
                    {"music_volume": 10, "screensaver_time_ms": 300000}, config=config
                )
            written = config.read_text(encoding="utf-8")
            self.assertIn('name="MusicVolume" value="10"', written)
            self.assertIn('name="ScreenSaverTime" value="300000"', written)
            # Overlay save still runs (so the value survives a reboot), but ES is
            # never stopped or started for these two fields.
            self.assertIn(["batocera-save-overlay"], calls)
            self.assertNotIn([set_es_collections.EMULATIONSTATION_SERVICE, "stop"], calls)
            self.assertNotIn([set_es_collections.EMULATIONSTATION_SERVICE, "start"], calls)

    def test_raises_when_emulationstation_does_not_start(self) -> None:
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            with mock.patch("app.set_es_collections.subprocess.run", return_value=mock.Mock(returncode=1, stdout="")), \
                 mock.patch("app.set_es_collections.shutil.which", return_value=None):
                with self.assertRaises(RuntimeError):
                    set_es_collections.apply_es_collections({"hidden_systems": "snes"}, config=config)

    def test_start_does_not_capture_output_via_pipe(self) -> None:
        # Regression test: "start" backgrounds `startx &` without redirecting its
        # own stdout/stderr, so a backgrounded EmulationStation/X process tree
        # keeps the *inherited* pipe write-end open indefinitely. Capturing via
        # stdout=PIPE (as every other step does) would make subprocess.run block
        # until that pipe sees EOF -- which never happens while ES keeps running
        # -- so every "start" would spuriously time out, even a fully successful
        # one, and trigger an unnecessary/conflicting swissknife-restart fallback.
        # Confirmed live: this was wedging the device into a permanent crash loop.
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return mock.Mock(returncode=0, stdout="")

            with mock.patch("app.set_es_collections.subprocess.run", side_effect=fake_run):
                set_es_collections.apply_es_collections({"hidden_systems": "snes"}, config=config)

            start_calls = [kwargs for command, kwargs in calls if command == [set_es_collections.EMULATIONSTATION_SERVICE, "start"]]
            self.assertEqual(len(start_calls), 1)
            self.assertEqual(start_calls[0].get("stdout"), subprocess.DEVNULL)
            self.assertEqual(start_calls[0].get("stderr"), subprocess.DEVNULL)

            stop_calls = [kwargs for command, kwargs in calls if command == [set_es_collections.EMULATIONSTATION_SERVICE, "stop"]]
            self.assertEqual(stop_calls[0].get("stdout"), subprocess.PIPE)

    def test_no_updates_raises(self) -> None:
        from app import set_es_collections

        with self.assertRaises(ValueError):
            set_es_collections.apply_es_collections({})

    def test_cli_reads_json_request_file(self) -> None:
        from app import set_es_collections

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "es_settings.cfg"
            request_file = Path(tmp) / "set-es-collections.request"
            request_file.write_text('{"music_volume": 42}', encoding="utf-8")
            with mock.patch("app.set_es_collections.sys.argv", ["set_es_collections.py", str(request_file)]), \
                 mock.patch.object(set_es_collections, "CONFIG", config), \
                 mock.patch("app.set_es_collections.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")):
                exit_code = set_es_collections.main()
            self.assertEqual(exit_code, 0)
            self.assertIn('name="MusicVolume" value="42"', config.read_text(encoding="utf-8"))


class EsCollectionsOvermindActionTests(unittest.TestCase):
    def test_get_state_action_returns_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            status, message, result = _execute_overmind_action(
                settings, repo, {"action": "get_es_collections_state", "payload": {}}
            )
            self.assertEqual(status, "completed")
            self.assertEqual(result["music_volume"], 80)

    def test_set_music_volume_action_applies_without_restart_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            with mock.patch("app.drone_api.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                status, message, result = _execute_overmind_action(
                    settings, repo, {"action": "set_music_volume", "payload": {"level": 60}}
                )
            self.assertEqual(status, "completed")
            helper.assert_called_once()
            self.assertEqual(helper.call_args[0][0], {"music_volume": 60})
            self.assertNotIn("restart", message.lower())

    def test_set_es_collections_action_message_notes_restart_only_when_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            with mock.patch("app.drone_api.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper"):
                _, no_restart_message, _ = _execute_overmind_action(
                    settings, repo,
                    {"action": "set_es_collections", "payload": {"music_volume": 55, "screensaver_minutes": 5}},
                )
                _, restart_message, _ = _execute_overmind_action(
                    settings, repo,
                    {"action": "set_es_collections", "payload": {"auto_collections": ["all"]}},
                )
            self.assertNotIn("restart", no_restart_message.lower())
            self.assertIn("EmulationStation restarted", restart_message)

    def test_set_music_volume_action_rejects_invalid_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            status, message, result = _execute_overmind_action(
                settings, repo, {"action": "set_music_volume", "payload": {"level": "banana"}}
            )
            self.assertEqual(status, "failed")

    def test_set_es_collections_action_applies_partial_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            with mock.patch("app.drone_api.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                status, message, result = _execute_overmind_action(
                    settings, repo,
                    {"action": "set_es_collections", "payload": {"auto_collections": ["all", "recent"]}},
                )
            self.assertEqual(status, "completed")
            self.assertEqual(helper.call_args[0][0], {"auto_collections": "all,recent"})

    def test_set_es_collections_action_reports_failure_on_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            repo = RomRepository(settings.roms_root, settings.bios_root)
            with mock.patch("app.drone_api.os.geteuid", return_value=999), \
                 mock.patch("app.device.es_collections._request_es_collections_service_control", return_value=False):
                status, message, result = _execute_overmind_action(
                    settings, repo,
                    {"action": "set_es_collections", "payload": {"music_volume": 50}},
                )
            self.assertEqual(status, "failed")


class ScreenModeAdminHandlerTests(unittest.TestCase):
    """The drone's own System Info page reuses the exact same _get_screen_mode /
    _apply_screen_mode functions the Overmind set_screen_mode action already
    calls -- these tests cover the new local admin handlers, not that shared
    logic (already covered by the existing screen-mode action tests)."""

    class _FakeHandler:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.response = None

        def _send_json(self, status_code: int, payload: dict) -> None:
            self.response = (status_code, payload)

    def _handler(self, settings):
        from app.web import handlers_diagnostics

        class FakeHandler(handlers_diagnostics.HandlersDiagnosticsMixin, self._FakeHandler):
            pass

        return FakeHandler(settings)

    def test_get_returns_current_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            with mock.patch("app.web.handlers_diagnostics._get_screen_mode", return_value="kiosk"):
                handler._handle_admin_screen_mode_get()
            self.assertEqual(handler.response, (200, {"screen_mode": "kiosk"}))

    def test_post_applies_valid_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            with mock.patch("app.web.handlers_diagnostics._apply_screen_mode", return_value=(settings.es_settings_file, True)) as apply_mock:
                handler._handle_admin_screen_mode_post({"mode": "Kiosk"})
            apply_mock.assert_called_once_with(settings, "kiosk")
            self.assertEqual(handler.response, (200, {"screen_mode": "kiosk", "emulationstation_restarted": True}))

    def test_post_rejects_invalid_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            handler._handle_admin_screen_mode_post({"mode": "arcade"})
            self.assertEqual(handler.response[0], 400)

    def test_post_reports_apply_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            with mock.patch("app.web.handlers_diagnostics._apply_screen_mode", side_effect=OSError("worker unavailable")):
                handler._handle_admin_screen_mode_post({"mode": "full"})
            self.assertEqual(handler.response[0], 500)


class EsCollectionsAdminHandlerTests(unittest.TestCase):
    class _FakeHandler:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.response = None

        def _send_json(self, status_code: int, payload: dict) -> None:
            self.response = (status_code, payload)

    def _handler(self, settings):
        from app.web import handlers_es_collections

        class FakeHandler(handlers_es_collections.HandlersEsCollectionsMixin, self._FakeHandler):
            pass

        return FakeHandler(settings)

    def test_get_returns_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            handler._handle_admin_es_collections_get()
            status_code, payload = handler.response
            self.assertEqual(status_code, 200)
            self.assertEqual(payload["music_volume"], 80)

    def test_post_applies_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                handler._handle_admin_es_collections_post({"auto_collections": ["all"]})
            status_code, payload = handler.response
            self.assertEqual(status_code, 200)
            helper.assert_called_once()
            self.assertEqual(helper.call_args[0][0], {"auto_collections": "all"})

    def test_post_rejects_empty_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            handler._handle_admin_es_collections_post({})
            self.assertEqual(handler.response[0], 400)

    def test_music_volume_post_applies_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            handler = self._handler(settings)
            with mock.patch("app.device.es_collections.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper") as helper:
                handler._handle_admin_music_volume_post({"level": 45})
            status_code, payload = handler.response
            self.assertEqual(status_code, 200)
            self.assertEqual(helper.call_args[0][0], {"music_volume": 45})

            handler._handle_admin_music_volume_post({"level": 500})
            self.assertEqual(handler.response[0], 400)

            handler._handle_admin_music_volume_post({"level": "nope"})
            self.assertEqual(handler.response[0], 400)


class EsCollectionsUiContentTests(unittest.TestCase):
    def test_system_info_page_wires_screen_mode(self) -> None:
        root = Path(__file__).resolve().parents[1]
        js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

        self.assertIn('id="screenModeButtons"', js)
        self.assertIn("async function loadScreenMode()", js)
        self.assertIn("async function applyDroneScreenMode(mode)", js)
        self.assertIn('api("/admin/system-info/screen-mode")', js)
        self.assertIn('apiPost("/admin/system-info/screen-mode"', js)

    def test_system_info_page_wires_screensaver(self) -> None:
        root = Path(__file__).resolve().parents[1]
        js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

        self.assertIn('id="screensaverSlider"', js)
        self.assertIn("function syncScreensaverControls(screensaverMinutes)", js)
        # Reuses the collections endpoint (one more optional field), not a new route.
        self.assertIn('apiPost("/admin/es-collections", {screensaver_minutes:', js)
        render_start = js.index("function renderEsCollectionsBody(state)")
        render_end = js.index("async function loadEsCollections()")
        self.assertIn("syncScreensaverControls(state.screensaver_minutes)", js[render_start:render_end])

    def test_system_info_page_wires_music_volume_and_collections(self) -> None:
        root = Path(__file__).resolve().parents[1]
        js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

        self.assertIn('id="musicVolumeSlider"', js)
        self.assertIn('apiPost("/admin/system-info/music-volume"', js)

        self.assertIn('id="esCollectionsBody"', js)
        self.assertIn('id="esCollectionsSaveBtn"', js)
        self.assertIn("function renderEsCollectionsCard(state)", js)
        self.assertIn("function collectEsCollectionsPayload()", js)
        self.assertIn('api("/admin/es-collections")', js)
        self.assertIn('apiPost("/admin/es-collections"', js)

        # displayed/grouped are inverted on save: unchecked -> hidden/ungrouped.
        payload_start = js.index("function collectEsCollectionsPayload()")
        payload_end = js.index("function wireEsCollectionsSaveButton()")
        payload_source = js[payload_start:payload_end]
        self.assertIn('names("displayed", false)', payload_source)
        self.assertIn('names("grouped", false)', payload_source)
        self.assertIn('names("auto", true)', payload_source)
        self.assertIn('names("custom", true)', payload_source)


class AdminControlsPageSplitTests(unittest.TestCase):
    """System Info and Controls are separate pages: screen mode, volume, music
    volume, screensaver, collections, and asset cache all live on Controls;
    System Info keeps only health + details (with GPU added)."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        cls.html = root.joinpath("app/web/templates/index.html").read_text(encoding="utf-8")

    def _function_body(self, name: str) -> str:
        start = self.js.index(f"async function {name}(")
        brace_start = self.js.index("{", start)
        depth = 0
        i = brace_start
        while i < len(self.js):
            char = self.js[i]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[start:i + 1]
            i += 1
        raise AssertionError(f"unbalanced braces scanning function {name}")

    def test_navbar_has_controls_link_after_system_info(self) -> None:
        self.assertIn('id="controlsMenuBtn" href="#admin/controls"', self.html)
        system_info_pos = self.html.index('id="systemInfoMenuBtn"')
        controls_pos = self.html.index('id="controlsMenuBtn"')
        admin_pos = self.html.index('id="adminMenuBtn"')
        self.assertTrue(system_info_pos < controls_pos < admin_pos)

    def test_router_dispatches_controls_hash(self) -> None:
        self.assertIn('hash === "#admin/controls"', self.js)
        router_start = self.js.index('hash === "#admin/system-info"')
        router_end = self.js.index('hash.startsWith("#admin/artwork")', router_start)
        router_section = self.js[router_start:router_end]
        self.assertIn("await renderAdminSystemInfoPage();", router_section)
        self.assertIn("await renderAdminControlsPage();", router_section)

    def test_system_info_page_keeps_only_health_and_details(self) -> None:
        body = self._function_body("renderAdminSystemInfoPage")
        self.assertIn("System Health", body)
        self.assertIn("System Details", body)
        self.assertIn(">GPU<", body)
        self.assertIn("fields.gpu_vendor", body)
        self.assertIn("fields.gpu_model", body)
        self.assertIn("fields.gpu_driver", body)

        for marker in (
            "Screen Mode</span>",
            "id=\"systemVolumeSlider\"",
            "Music Volume</span>",
            "Screensaver</span>",
            "Game Collections",
            "Asset Cache</span>",
        ):
            self.assertNotIn(marker, body, f"{marker!r} should have moved off System Info")

    def test_controls_page_has_all_moved_cards(self) -> None:
        body = self._function_body("renderAdminControlsPage")
        self.assertIn('titleNode.textContent = "Controls";', body)

        for marker in (
            'id="screenModeButtons"',
            'id="systemVolumeSlider"',
            'id="musicVolumeSlider"',
            'id="screensaverSlider"',
            'id="esCollectionsBody"',
            'id="systemInfoAssetCacheBody"',
        ):
            self.assertIn(marker, body)

        self.assertNotIn("System Health", body)
        self.assertNotIn("System Details", body)

    def test_controls_nav_link_is_admin_gated(self) -> None:
        self.assertIn('const controlsMenuBtn = document.getElementById("controlsMenuBtn");', self.js)
        self.assertIn("const adminLinks = [adminMenuBtn, systemInfoMenuBtn, controlsMenuBtn, transfersMenuBtn, apiAccessBtn]", self.js)
        self.assertIn('controlsMenuBtn.addEventListener("click"', self.js)
        click_start = self.js.index('controlsMenuBtn.addEventListener("click"')
        click_end = self.js.index("});", click_start)
        click_body = self.js[click_start:click_end]
        self.assertIn("if (!adminEnabled) return;", click_body)
        self.assertIn('setHash("#admin/controls");', click_body)

    def test_asset_cache_refresh_hooks_check_controls_hash(self) -> None:
        purge_start = self.js.index("async function purgeAssetCache()")
        purge_end = self.js.index("async function clearPendingAssetChanges()")
        clear_start = purge_end
        clear_end = self.js.index("function renderAssetCachePanel(")
        for section in (self.js[purge_start:purge_end], self.js[clear_start:clear_end]):
            self.assertIn('window.location.hash === "#admin/controls"', section)
            self.assertNotIn('window.location.hash === "#admin/system-info"', section)


class HeartbeatEsCollectionsTests(unittest.TestCase):
    """The Drone proactively reports ES-collections state in its heartbeat so
    Overmind can store it, without touching the existing live remote-control
    (get_es_collections_state/set_es_collections) actions."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.source = root.joinpath("app/overmind/action_poller.py").read_text(encoding="utf-8")

    def test_heartbeat_builder_imports_and_calls_get_es_collections_state(self) -> None:
        self.assertIn("from ..device.es_collections import (\n        get_es_collections_state,", self.source)
        self.assertIn("from device.es_collections import (\n        get_es_collections_state,", self.source)
        self.assertIn(
            'system_info_payload["es_collections"] = get_es_collections_state(settings)',
            self.source,
        )

    def test_es_collections_assigned_alongside_screen_mode_and_audio_volume(self) -> None:
        # Same per-tick refresh cadence as the other "current device configuration"
        # fields it's conceptually grouped with (not the rarer full system_info
        # refresh), so it has the same staleness characteristics users already see
        # for screen_mode/audio_volume today.
        screen_mode_pos = self.source.index('system_info_payload["screen_mode"]')
        audio_volume_pos = self.source.index('system_info_payload["audio_volume"]')
        es_collections_pos = self.source.index('system_info_payload["es_collections"]')
        self.assertTrue(screen_mode_pos < audio_volume_pos < es_collections_pos)


class ControlsDashboardLayoutTests(unittest.TestCase):
    """Screen Mode/Volume/Music Volume/Screensaver are compact tiles in a
    responsive grid on the Controls page, not full-width stacked cards."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")
        cls.css = root.joinpath("app/web/static/css/drone.css").read_text(encoding="utf-8")

    def test_controls_page_wraps_simple_controls_in_a_tile_grid(self) -> None:
        start = self.js.index("async function renderAdminControlsPage(")
        end = self.js.index("Game Collections", start)
        body = self.js[start:end]
        self.assertIn("row row-cols-1 row-cols-sm-2 row-cols-xl-4 g-3", body)
        self.assertEqual(body.count('class="col">'), 4)
        self.assertEqual(body.count("control-tile"), 4)
        for marker in ("Screen Mode", "Volume</span>", "Music Volume", "Screensaver"):
            self.assertIn(marker, body)

    def test_control_tile_css_defined(self) -> None:
        self.assertIn(".control-tile .card-body", self.css)


class FrictionlessSaveTests(unittest.TestCase):
    """Music Volume, Screensaver, and Collections apply on click/slider-change
    with no confirm dialog. Music Volume/Screensaver take effect live (no
    EmulationStation restart, confirmed on a real device); Collections still
    restarts ES since it changes which systems/collections are shown. Screen
    Mode intentionally keeps its confirm."""

    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.js = root.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def test_no_save_and_restart_button_labels_remain(self) -> None:
        self.assertNotIn("Save &amp; Restart EmulationStation", self.js)

    def test_music_volume_and_screensaver_apply_on_slider_change_with_no_confirm(self) -> None:
        # No separate Save button: moving the slider applies immediately.
        self.assertNotIn('id="musicVolumeSaveBtn"', self.js)
        self.assertNotIn('id="screensaverSaveBtn"', self.js)

        music_start = self.js.index('musicVolumeSlider.addEventListener("change"')
        music_end = self.js.index("});", music_start)
        music_body = self.js[music_start:music_end]
        self.assertNotIn("window.confirm", music_body)
        self.assertIn('apiPost("/admin/system-info/music-volume"', music_body)

        saver_start = self.js.index('screensaverSlider.addEventListener("change"')
        saver_end = self.js.index("});", saver_start)
        saver_body = self.js[saver_start:saver_end]
        self.assertNotIn("window.confirm", saver_body)
        self.assertIn('apiPost("/admin/es-collections", {screensaver_minutes:', saver_body)

    def test_es_collections_save_has_no_confirm(self) -> None:
        save_start = self.js.index("function wireEsCollectionsSaveButton()")
        save_end = self.js.index("\nfunction ", save_start + 1)
        self.assertNotIn("window.confirm", self.js[save_start:save_end])

    def test_screen_mode_confirm_is_unchanged(self) -> None:
        # Deliberately out of scope: switching to kiosk/kid can lock out admin
        # access until switched back, a more consequential action than a volume
        # or screensaver tweak.
        self.assertIn("restart EmulationStation now?", self.js)

    def test_music_volume_and_screensaver_success_toasts_do_not_claim_a_restart(self) -> None:
        music_start = self.js.index('musicVolumeSlider.addEventListener("change"')
        music_end = self.js.index("const screensaverSlider", music_start)
        music_body = self.js[music_start:music_end]
        self.assertIn("Music volume set to ${result.music_volume}%.", music_body)
        self.assertNotIn("restart", music_body.lower())

        saver_start = self.js.index('screensaverSlider.addEventListener("change"')
        saver_end = self.js.index("loadScreenMode();", saver_start)
        saver_body = self.js[saver_start:saver_end]
        self.assertIn("Screensaver delay set to ${result.screensaver_minutes} min.", saver_body)
        self.assertNotIn("restart", saver_body.lower())

    def test_es_collections_save_toast_still_mentions_restart(self) -> None:
        save_start = self.js.index("function wireEsCollectionsSaveButton()")
        save_end = self.js.index("\nfunction ", save_start + 1)
        self.assertIn("EmulationStation restarted", self.js[save_start:save_end])


class EsLifecycleLockTests(unittest.TestCase):
    """Two overlapping root-direct calls that stop/write/start EmulationStation
    (screen mode, ES collections) must never run concurrently. Confirmed live:
    without serialization, two overlapping restarts race the same
    EmulationStation/X session and wedge it into a permanent crash-restart loop
    (black screen that never recovers on its own)."""

    def test_concurrent_es_collections_calls_are_serialized(self) -> None:
        import app.device.es_collections as es_collections_module

        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            active = 0
            max_active = 0
            tracking_lock = threading.Lock()

            def fake_helper(low_level, config=None):
                nonlocal active, max_active
                with tracking_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with tracking_lock:
                    active -= 1

            with mock.patch("app.device.es_collections._apply_es_collections_helper", side_effect=fake_helper), \
                 mock.patch("app.device.es_collections.os.geteuid", return_value=0):
                threads = [
                    threading.Thread(target=es_collections_module.apply_es_collections, args=(settings, {"music_volume": 10})),
                    threading.Thread(target=es_collections_module.apply_es_collections, args=(settings, {"screensaver_minutes": 5})),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)
                    self.assertFalse(t.is_alive(), "a thread did not finish -- the lock may have deadlocked")

            self.assertEqual(max_active, 1, "two ES-restart calls ran concurrently -- the lock did not serialize them")

    def test_screen_mode_and_es_collections_share_the_same_lock(self) -> None:
        from app.device import device_control
        import app.device.es_collections as es_collections_module

        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(Path(tmp))
            active = 0
            max_active = 0
            tracking_lock = threading.Lock()

            def fake_screen_mode_helper(mode, config=None):
                nonlocal active, max_active
                with tracking_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with tracking_lock:
                    active -= 1

            def fake_collections_helper(low_level, config=None):
                nonlocal active, max_active
                with tracking_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.05)
                with tracking_lock:
                    active -= 1

            with mock.patch("app.device.device_control._set_screen_mode_helper", side_effect=fake_screen_mode_helper), \
                 mock.patch("app.device.device_control.os.geteuid", return_value=0), \
                 mock.patch("app.device.es_collections._apply_es_collections_helper", side_effect=fake_collections_helper), \
                 mock.patch("app.device.es_collections.os.geteuid", return_value=0):
                threads = [
                    threading.Thread(target=device_control._apply_screen_mode, args=(settings, "kiosk")),
                    threading.Thread(target=es_collections_module.apply_es_collections, args=(settings, {"hidden_systems": ["snes"]})),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)
                    self.assertFalse(t.is_alive(), "a thread did not finish -- the lock may have deadlocked")

            self.assertEqual(
                max_active, 1,
                "screen mode and ES collections restarts ran concurrently -- they must share one lock",
            )


if __name__ == "__main__":
    unittest.main()
