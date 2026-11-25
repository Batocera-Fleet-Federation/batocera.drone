"""Controller Assignments: classification, ordering, best effort, overlay, lifecycle, routes."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.integrations.controller_assignments import es_overlay, launch_order as lo
from app.integrations.controller_assignments.config import (ConfigStore, PRESETS, compile_runtime, normalize,
                                                            preset_systems, validate_order)
from app.integrations.controller_assignments.devices import connected_controllers
from app.integrations.controller_assignments.manager import ControllerAssignmentsIntegration, WRAPPER_SOURCE
from app.integrations.controller_assignments.paths import ControllerAssignmentsPaths
from app.integrations.registry import build_integration_registry

ROOT = Path(__file__).resolve().parents[1]

GC = "030000007e0500003703000000000000"
DRAGON = "03000000790000000600000010010000"
DS4 = "030000004c050000cc09000011810000"
XBOX = "030000005e0400008e02000014010000"
SNES_PAD = "03000000830500006020000010010000"
N64_NSO = "030000007e0500001920000011810000"


def pad(slot, name, guid, event, buttons=12, axes=6):
    return {"index": str(slot - 1), "guid": guid, "name": name, "devicepath": f"/dev/input/{event}",
            "nbbuttons": str(buttons), "nbhats": "1", "nbaxes": str(axes)}


def argv_for(players, system="gamecube", rom="/userdata/roms/gamecube/Game.iso"):
    argv = ["emulatorlauncher"]
    for slot, values in players.items():
        for key in lo.PLAYER_KEYS:
            argv += [f"-p{slot}{key}", values[key]]
    return argv + ["-system", system, "-rom", rom, "-gameinfoxml", "/dev/null", "-systemname", "X"]


def order_names(argv):
    _, _, players = lo.parse_players(argv)
    return [players[slot]["name"] for slot in sorted(players)]


class FakeSysfs:
    """A /sys + /run/udev tree with input devices at USB ports."""

    def __init__(self, root: Path):
        self.sys = root / "sys"
        self.udev = root / "udev"
        self.udev.mkdir(parents=True)

    def add(self, event, vendor, product, port="", uniq="", gun=False, minor=None):
        device = self.sys / "devices" / "pci0000:00" / "usb1"
        if port:
            for part in ("1-" + port).split("."):
                pass
            device = device / f"1-{port}" / f"1-{port}:1.0"
        device = device / f"input-{event}"
        (device / "id").mkdir(parents=True)
        (device / "id" / "vendor").write_text(vendor)
        (device / "id" / "product").write_text(product)
        if uniq:
            (device / "uniq").write_text(uniq)
        event_dir = self.sys / "class" / "input" / event
        event_dir.mkdir(parents=True)
        (event_dir / "device").symlink_to(device)
        minor = minor if minor is not None else 64 + int(event[5:])
        (event_dir / "dev").write_text(f"13:{minor}")
        if gun:
            (self.udev / f"c13:{minor}").write_text("E:ID_INPUT_GUN=1\n")


class ClassificationTests(unittest.TestCase):
    def test_families_by_usb_identity_name_and_vendor(self):
        cases = [
            ("Wii U GameCube Adapter Port 1", "057e", "0337", "gamecube"),
            ("MAYFLASH GameCube Controller Adapter", "0079", "1844", "gamecube"),
            ("N64 Controller", "057e", "2019", "n64"),
            ("raphnet.net GC/N64_USB v3.6", "289b", "0040", "n64"),
            ("8BitDo 64 Controller", "2dc8", "3019", "n64"),
            ("SNES Controller", "057e", "2017", "snes"),
            ("USB,2-axis 8-button gamepad  ", "0583", "2060", "snes"),
            ("USB Gamepad ", "0079", "0011", "snes"),
            ("8BitDo SN30 Pro", "2dc8", "6101", "snes"),
            ("NES Controller (R)", "057e", "2007", "nes"),
            ("8BitDo N30 gamepad", "2dc8", "9001", "nes"),
            ("Retro-bit Sega Saturn", "", "", "sega"),
            ("SEGA Genesis Control Pad", "057e", "201e", "sega"),
            ("8BitDo M30 gamepad", "2dc8", "5006", "sega"),
            ("Mega Drive Mini 6B", "0ca3", "0024", "sega"),
            ("Sony Interactive Entertainment Wireless Controller", "054c", "09cc", "playstation"),
            ("Twin USB Joystick", "0810", "0001", "playstation"),
            ("Microsoft X-Box 360 pad", "045e", "028e", "xbox"),
            ("PowerA Xbox Series pad", "20d6", "2001", "xbox"),
            ("Nintendo Switch Pro Controller", "057e", "2009", "nintendo"),
            ("Nintendo Switch Left Joy-Con", "057e", "2006", "nintendo"),
            ("DragonRise Inc.   Generic   USB  Joystick  ", "0079", "0006", "arcade"),
            ("Xin-Mo Xin-Mo Dual Arcade", "16c0", "05e1", "arcade"),
            ("Generic USB gamepad", "1234", "5678", "generic"),
        ]
        for name, vendor, product, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(lo.classify(name, vendor, product), expected)

    def test_snes_is_not_nes_and_guid_ids_decode(self):
        self.assertEqual(lo.classify("SNES30 pad"), "snes")
        self.assertEqual(lo.guid_ids(GC), ("057e", "0337"))
        self.assertEqual(lo.guid_ids("nonsense"), ("", ""))

    def test_every_profile_family_is_a_known_type_and_presets_cover_requested_consoles(self):
        for profile in lo.SYSTEM_PROFILES.values():
            self.assertTrue(set(profile["families"]) <= set(lo.TYPE_IDS))
        for system in ("gamecube", "n64", "snes", "nes", "megadrive", "genesis", "saturn", "dreamcast", "psx",
                       "ps2", "xbox", "mame", "switch"):
            self.assertIn(system, lo.SYSTEM_PROFILE_BY_SYSTEM)
        self.assertIn("snes", preset_systems("snes"))
        self.assertIn("nes", preset_systems("nes"))
        self.assertIn("n64", preset_systems("n64"))
        self.assertIn("megadrive", preset_systems("sega"))


class DeviceFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FakeSysfs(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_usb_port_bluetooth_and_udev_gun_flag(self):
        self.fs.add("event24", "0079", "0006", port="2.2.1")
        self.fs.add("event3", "054c", "09cc", uniq="AA:BB:CC:DD:EE:FF")
        self.fs.add("event9", "16c0", "0f01", port="4", gun=True)
        facts = lo.device_facts("/dev/input/event24", "", str(self.fs.sys), str(self.fs.udev))
        self.assertEqual((facts["vendor"], facts["product"], facts["port"]), ("0079", "0006", "usb:1-2.2.1"))
        self.assertEqual(lo.device_facts("/dev/input/event3", "", str(self.fs.sys), str(self.fs.udev))["port"],
                         "bt:aa:bb:cc:dd:ee:ff")
        self.assertTrue(lo.device_facts("/dev/input/event9", "", str(self.fs.sys), str(self.fs.udev))["gun"])
        self.assertFalse(facts["gun"])

    def test_connected_controllers_lists_joysticks_without_guns(self):
        proc = Path(self.tmp.name) / "proc" / "bus" / "input"
        proc.mkdir(parents=True)
        proc.joinpath("devices").write_text(
            'I: Bus=0003 Vendor=057e Product=0337 Version=0000\nN: Name="Wii U GameCube Adapter Port 1"\n'
            'H: Handlers=event19 js2 \n\n'
            'I: Bus=0003 Vendor=16c0 Product=0f01 Version=0100\nN: Name="Sinden lightgun"\n'
            'H: Handlers=kbd event23 js6 \n\n'
            'I: Bus=0003 Vendor=413d Product=2114 Version=0110\nN: Name="SONiX Keyboard"\nH: Handlers=kbd event10 \n')
        self.fs.add("event19", "057e", "0337")
        rows = connected_controllers(Path(self.tmp.name) / "proc", str(self.fs.sys), str(self.fs.udev))
        self.assertEqual([row["name"] for row in rows], ["Wii U GameCube Adapter Port 1"])
        self.assertEqual(rows[0]["guid"], GC)
        self.assertEqual(rows[0]["type"], "gamecube")


class OrderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.fs = FakeSysfs(base)
        # The user's real layout: 4 arcade sticks first, a DS4, then GC adapter ports.
        for number, port in ((24, "2.1"), (25, "2.2"), (26, "2.3"), (27, "2.4")):
            self.fs.add(f"event{number}", "0079", "0006", port=port)
        self.fs.add("event3", "054c", "09cc", uniq="aa:bb")
        for number in (19, 20, 21):
            self.fs.add(f"event{number}", "057e", "0337")
        self.players = {
            1: pad(1, "DragonRise Inc.   Generic   USB  Joystick  ", DRAGON, "event24", 12, 7),
            2: pad(2, "DragonRise Inc.   Generic   USB  Joystick  ", DRAGON, "event25", 12, 7),
            3: pad(3, "DragonRise Inc.   Generic   USB  Joystick  ", DRAGON, "event26", 12, 7),
            4: pad(4, "DragonRise Inc.   Generic   USB  Joystick  ", DRAGON, "event27", 12, 7),
            5: pad(5, "Sony Interactive Entertainment Wireless Controller", DS4, "event3", 13, 6),
            6: pad(6, "Wii U GameCube Adapter Port 1", GC, "event19", 12, 6),
            7: pad(7, "Wii U GameCube Adapter Port 2", GC, "event20", 12, 6),
            8: pad(8, "Wii U GameCube Adapter Port 3", GC, "event21", 12, 6),
        }
        self.es_settings = base / "es_settings.cfg"
        self.es_settings.write_text("<config></config>")
        self.runtime = {"enabled": True, "es_settings_file": str(self.es_settings), "roms_root": "/userdata/roms",
                        "default": [], "systems": {}, "games": {}}

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, argv, runtime=None):
        return lo.plan(argv, runtime or self.runtime, sys_root=str(self.fs.sys), udev_root=str(self.fs.udev))

    def test_argv_round_trip_keeps_everything_else(self):
        argv = argv_for(self.players)
        rest, insert_at, players = lo.parse_players(argv)
        self.assertEqual(lo.build_argv(rest, insert_at, players), argv)
        self.assertEqual(lo.option_value(argv, "-system"), "gamecube")

    def test_system_rule_moves_gamecube_ports_into_first_slots(self):
        self.runtime["systems"] = {"gamecube": {"order": [{"kind": "type", "type": "gamecube"}], "source": "system"}}
        new_argv, report = self.plan(argv_for(self.players))
        self.assertEqual(order_names(new_argv)[:3], ["Wii U GameCube Adapter Port 1", "Wii U GameCube Adapter Port 2",
                                                     "Wii U GameCube Adapter Port 3"])
        self.assertEqual(len(order_names(new_argv)), 8)  # nobody is dropped
        self.assertTrue(report["changed"])
        self.assertEqual(report["rule"], "system")
        # Each player's arguments move as one group, with their own index.
        _, _, players = lo.parse_players(new_argv)
        self.assertEqual(players[1]["devicepath"], "/dev/input/event19")
        self.assertEqual(players[1]["index"], "5")

    def test_batocera_pins_keep_their_slot_and_rules_fill_open_slots(self):
        self.es_settings.write_text(
            '<config><string name="INPUT P1GUID" value="%s"/><string name="INPUT P1NAME" '
            'value="DragonRise Inc.   Generic   USB  Joystick  "/><string name="INPUT P1PATH" '
            'value="/sys/devices/pci0000:00/usb1/1-2.1/1-2.1:1.0/input/input24/event24"/></config>' % DRAGON)
        self.runtime["systems"] = {"gamecube": {"order": [{"kind": "type", "type": "gamecube"}], "source": "system"}}
        new_argv, report = self.plan(argv_for(self.players))
        names = order_names(new_argv)
        self.assertEqual(names[0], "DragonRise Inc.   Generic   USB  Joystick  ")
        self.assertEqual(names[1:4], ["Wii U GameCube Adapter Port 1", "Wii U GameCube Adapter Port 2",
                                      "Wii U GameCube Adapter Port 3"])
        self.assertEqual(report["players"][0]["lock"], "Batocera P1")
        _, _, players = lo.parse_players(new_argv)
        self.assertEqual(players[1]["devicepath"], "/dev/input/event24")  # the pinned stick, not another one

    def test_pin_for_a_missing_controller_leaves_the_slot_open(self):
        self.es_settings.write_text('<config><string name="INPUT P1GUID" value="%s"/>'
                                    '<string name="INPUT P1NAME" value="Not Connected"/></config>' % XBOX)
        self.runtime["systems"] = {"gamecube": {"order": [{"kind": "type", "type": "gamecube"}], "source": "system"}}
        new_argv, _ = self.plan(argv_for(self.players))
        self.assertEqual(order_names(new_argv)[0], "Wii U GameCube Adapter Port 1")

    def test_light_guns_are_never_moved(self):
        self.fs.add("event40", "16c0", "0f01", port="9", gun=True, minor=300)
        players = dict(self.players)
        players[2] = pad(2, "Sinden lightgun", "03000000c0160000010f000000010000", "event40")
        self.runtime["systems"] = {"gamecube": {"order": [{"kind": "type", "type": "gamecube"}], "source": "system"}}
        new_argv, report = self.plan(argv_for(players))
        self.assertEqual(order_names(new_argv)[1], "Sinden lightgun")
        self.assertEqual(report["players"][1]["lock"], "light gun")

    def test_device_selector_distinguishes_identical_sticks_by_port(self):
        self.runtime["systems"] = {"mame": {"source": "system", "order": [
            {"kind": "device", "name": "DragonRise Inc.   Generic   USB  Joystick", "guid": DRAGON, "port": "usb:1-2.4"},
            {"kind": "device", "name": "DragonRise Inc.   Generic   USB  Joystick", "guid": DRAGON, "port": "usb:1-2.3"}]}}
        new_argv, _ = self.plan(argv_for(self.players, "mame", "/userdata/roms/mame/sf2.zip"))
        _, _, players = lo.parse_players(new_argv)
        self.assertEqual([players[1]["devicepath"], players[2]["devicepath"]], ["/dev/input/event27", "/dev/input/event26"])

    def test_precedence_game_system_preset_best_effort_default(self):
        runtime = dict(self.runtime, auto=True, default=[{"kind": "type", "type": "playstation"}],
                       systems={"snes": {"mode": "best-effort", "source": "preset:snes"},
                                "nes": {"order": [{"kind": "type", "type": "arcade"}], "source": "system"}},
                       games={"gamecube/Game.iso": {"order": [{"kind": "type", "type": "playstation"}]}})
        self.assertEqual(lo.resolve_rule(runtime, "gamecube", "gamecube/Game.iso")[0], "game")
        self.assertEqual(lo.resolve_rule(runtime, "nes", "nes/x.nes")[0], "system")
        self.assertEqual(lo.resolve_rule(runtime, "snes", "snes/x.sfc")[:3:2], ("preset:snes", True))
        self.assertEqual(lo.resolve_rule(runtime, "n64", "n64/x.z64")[0], "best-effort")
        self.assertEqual(lo.resolve_rule(runtime, "atari2600", "atari2600/x.a26")[0], "default")
        self.assertEqual(lo.resolve_rule(dict(runtime, auto=False), "n64", "n64/x.z64")[0], "default")
        self.assertEqual(lo.resolve_rule(dict(self.runtime), "n64", "n64/x.z64")[0], "none")

    def test_best_effort_gamecube_prefers_gamecube_then_analog_pads_over_arcade_sticks(self):
        new_argv, report = self.plan(argv_for(self.players), dict(self.runtime, auto=True))
        names = order_names(new_argv)
        self.assertEqual(names[:3], ["Wii U GameCube Adapter Port 1", "Wii U GameCube Adapter Port 2",
                                     "Wii U GameCube Adapter Port 3"])
        self.assertEqual(names[3], "Sony Interactive Entertainment Wireless Controller")
        self.assertEqual(report["rule"], "best-effort")
        self.assertEqual(report["profile"], "gamecube")

    def test_best_effort_for_snes_n64_nes_and_sega(self):
        self.fs.add("event50", "0583", "2060", port="5")
        self.fs.add("event51", "057e", "2019", uniq="cc:dd")
        self.fs.add("event52", "045e", "028e", port="6")
        players = {
            1: pad(1, "Microsoft X-Box 360 pad", XBOX, "event52", 11, 8),
            2: pad(2, "DragonRise Inc.   Generic   USB  Joystick  ", DRAGON, "event24", 12, 7),
            3: pad(3, "USB,2-axis 8-button gamepad  ", SNES_PAD, "event50", 8, 2),
            4: pad(4, "N64 Controller", N64_NSO, "event51", 14, 4),
            5: pad(5, "Wii U GameCube Adapter Port 1", GC, "event19", 12, 6),
        }
        runtime = dict(self.runtime, auto=True)
        expectations = {
            "snes": "USB,2-axis 8-button gamepad  ",
            "nes": "USB,2-axis 8-button gamepad  ",
            "n64": "N64 Controller",
            "megadrive": "USB,2-axis 8-button gamepad  ",
            "psx": "Microsoft X-Box 360 pad",
            "mame": "DragonRise Inc.   Generic   USB  Joystick  ",
        }
        for system, first in expectations.items():
            with self.subTest(system=system):
                new_argv, _ = self.plan(argv_for(players, system, f"/userdata/roms/{system}/g.zip"), runtime)
                self.assertEqual(order_names(new_argv)[0], first)
        # N64 falls back to a GameCube pad before an Xbox pad, and never puts a digital pad first.
        new_argv, _ = self.plan(argv_for(players, "n64", "/userdata/roms/n64/g.z64"), runtime)
        self.assertEqual(order_names(new_argv)[1], "Wii U GameCube Adapter Port 1")
        self.assertNotEqual(order_names(new_argv)[2], "USB,2-axis 8-button gamepad  ")

    def test_best_effort_ties_follow_the_default_order(self):
        runtime = dict(self.runtime, auto=True, default=[
            {"kind": "device", "name": "Wii U GameCube Adapter Port 3", "guid": GC, "port": ""}])
        new_argv, _ = self.plan(argv_for(self.players), runtime)
        self.assertEqual(order_names(new_argv)[0], "Wii U GameCube Adapter Port 3")

    def test_disabled_or_unknown_rules_leave_argv_untouched(self):
        argv = argv_for(self.players)
        self.assertEqual(self.plan(argv, dict(self.runtime, enabled=False))[0], argv)
        self.assertEqual(self.plan(argv)[0], argv)

    def test_rom_key_relative_to_system_folder(self):
        self.assertEqual(lo.rom_key("gamecube", "/userdata/roms/gamecube/sub/Game.iso", "/userdata/roms"),
                         "gamecube/sub/Game.iso")
        self.assertEqual(lo.rom_key("gamecube", "/mnt/net/roms/gamecube/Game.iso", "/userdata/roms"),
                         "gamecube/Game.iso")


class WrapperMainTests(unittest.TestCase):
    def test_main_execs_reordered_command_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime.json"
            report = Path(tmp) / "last.json"
            runtime.write_text(json.dumps({"enabled": True, "report_file": str(report),
                                           "es_settings_file": "/nonexistent", "default": [], "systems": {}, "games": {}}))
            argv = ["--runtime", str(runtime), "--", "emulatorlauncher", "-system", "nes", "-rom", "/r/nes/a.nes"]
            with mock.patch.object(lo.os, "execvp") as execvp:
                lo.main(argv)
            execvp.assert_called_once_with("emulatorlauncher", argv[3:])
            self.assertEqual(json.loads(report.read_text())["rule"], "none")

    def test_main_falls_back_to_the_shell_when_exec_fails(self):
        argv = ["--runtime", "/nope", "--", "VAR=1", "emulatorlauncher", "-system", "nes"]
        with mock.patch.object(lo.os, "execvp", side_effect=FileNotFoundError("VAR=1")), \
                mock.patch.object(lo.os, "execv") as execv:
            lo.main(argv)
        execv.assert_called_once_with("/bin/sh", ["/bin/sh", "-c", "VAR=1 emulatorlauncher -system nes"])

    def test_main_never_blocks_a_launch_on_bad_runtime(self):
        argv = ["--runtime", "/does/not/exist.json", "--", "emulatorlauncher", "-system", "nes"]
        with mock.patch.object(lo.os, "execvp") as execvp:
            lo.main(argv)
        execvp.assert_called_once_with("emulatorlauncher", argv[3:])

    def test_installed_wrapper_runs_standalone_without_the_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "drone-controller-order"
            wrapper.write_bytes(WRAPPER_SOURCE.read_bytes())
            wrapper.chmod(0o755)
            result = subprocess.run([sys.executable, "-I", str(wrapper), "--runtime", "/nope", "--",
                                     sys.executable, "-c", "import sys; print(sys.argv[1:])", "-system", "nes"],
                                    capture_output=True, text=True, cwd=tmp, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("['-system', 'nes']", result.stdout)


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        self.assertEqual(validate_order([{"kind": "type", "type": "snes"}, {"kind": "type", "type": "snes"}]),
                         [{"kind": "type", "type": "snes"}])
        for bad in ([{"kind": "type", "type": "nope"}], [{"kind": "device", "name": ""}], "x",
                    [{"kind": "device", "name": "a", "port": "../../etc"}], [{"kind": "shell", "cmd": "rm"}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_order(bad)

    def test_normalize_drops_invalid_and_compile_folds_presets_under_system_rules(self):
        config = normalize({"enabled": True, "auto": True, "presets": {"snes": True, "bogus": True},
                            "systems": {"snes": {"order": [{"kind": "type", "type": "xbox"}]}, "../x": {"order": []}},
                            "games": {"n64/../../x": {"order": []}, "n64/Mario.z64": {"order": [], "name": "Mario"}}})
        self.assertEqual(list(config["presets"]), ["snes"])
        self.assertEqual(list(config["systems"]), ["snes"])
        self.assertEqual(list(config["games"]), ["n64/Mario.z64"])
        runtime = compile_runtime(config, es_settings_file=Path("/e"), roms_root=Path("/r"), report_file=Path("/s"))
        self.assertTrue(runtime["auto"])
        self.assertEqual(runtime["systems"]["snes"]["source"], "system")
        self.assertEqual(runtime["systems"]["sfc"], {"mode": "best-effort", "source": "preset:snes"})
        self.assertEqual(set(PRESETS), {"gamecube", "n64", "snes", "nes", "sega", "playstation", "xbox", "arcade", "switch"})


def make_settings(base: Path):
    userdata = base / "userdata"
    es_dir = userdata / "system" / "configs" / "emulationstation"
    es_dir.mkdir(parents=True)
    stock = base / "es_systems.cfg"
    stock.write_text(
        "<systemList>"
        "<system><name>gamecube</name><fullname>GameCube</fullname><path>/userdata/roms/gamecube</path>"
        "<command>emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%</command></system>"
        "<system><name>switch</name><fullname>Switch</fullname><path>/userdata/roms/switch</path>"
        "<command>emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%</command></system>"
        "<system><name>dos</name><fullname>DOS</fullname><path>/userdata/roms/dos</path>"
        "<command>emulatorlauncher -system dos -rom %ROM%</command></system>"
        "</systemList>")
    # RGS-style overlay replacing one command, plus a partial (hardware-only) override.
    (es_dir / "es_systems_switch.cfg").write_text(
        "<systemList><system><name>switch</name><fullname>Switch</fullname><path>/userdata/roms/switch</path>"
        "<command>/userdata/system/rgs/emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM%</command>"
        "</system></systemList>")
    (es_dir / "es_systems_override.cfg").write_text(
        "<systemList><system><name>dos</name><hardware>pc</hardware></system></systemList>")
    return SimpleNamespace(userdata_root=userdata, es_systems_file=stock, es_settings_file=es_dir / "es_settings.cfg",
                           roms_root=userdata / "roms", admin_enabled=True)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.settings = make_settings(self.base)
        self.wrapper = Path("/userdata/system/drone-app/integrations/controller-assignments/bin/drone-controller-order")
        self.runtime = Path("/userdata/system/drone-app/integrations/controller-assignments/state/runtime.json")
        self.marker = self.base / "marker.json"
        es_overlay._CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_command_nodes_chain_to_the_effective_command(self):
        result = es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)
        self.assertTrue(result["changed"])
        root = ET.parse(es_overlay.overlay_path(self.settings)).getroot()
        nodes = {node.findtext("name"): node for node in root.findall("system")}
        # dos has no %CONTROLLERSCONFIG%, so it is left alone.
        self.assertEqual(set(nodes), {"gamecube", "switch"})
        for node in nodes.values():
            self.assertEqual([child.tag for child in node], ["name", "command"])  # partial: nothing else
        prefix = f"{self.wrapper} --runtime {self.runtime} --"
        self.assertEqual(nodes["switch"].findtext("command"),
                         f"{prefix} /userdata/system/rgs/emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM%")
        # Idempotent, and never wraps the wrapper.
        es_overlay._CACHE.clear()
        self.assertFalse(es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)["changed"])
        self.assertEqual(es_overlay.strip_wrapper(nodes["gamecube"].findtext("command")),
                         "emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%")
        self.assertTrue(es_overlay.in_sync(self.settings, self.wrapper, self.runtime))

    def test_refuses_foreign_file_and_removes_only_its_own(self):
        es_overlay.overlay_path(self.settings).write_text("<systemList/>")
        with self.assertRaises(ValueError):
            es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)
        self.assertEqual(es_overlay.remove(self.settings, self.marker), {"removed": False})
        self.assertTrue(es_overlay.overlay_path(self.settings).exists())
        es_overlay.overlay_path(self.settings).unlink()
        es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)
        self.assertEqual(es_overlay.remove(self.settings, self.marker), {"removed": True})
        self.assertFalse(es_overlay.overlay_path(self.settings).exists())

    def test_only_plain_player_commands_are_wrapped(self):
        self.assertTrue(es_overlay.wrappable("emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM%"))
        for command in ("SDL_X=1 emulatorlauncher %CONTROLLERSCONFIG%", "a %CONTROLLERSCONFIG%; b",
                        "emulatorlauncher -system dos", "x $(y) %CONTROLLERSCONFIG%"):
            with self.subTest(command=command):
                self.assertFalse(es_overlay.wrappable(command))

    def test_unsafe_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            es_overlay.wrapper_prefix(Path("/tmp/a b/x"), self.runtime)

    def test_drone_system_resolver_merges_partial_overlays(self):
        from app.device.device_control import _resolve_es_systems_effective
        es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)
        _, systems = _resolve_es_systems_effective(self.settings)
        by_name = {item["name"]: item for item in systems}
        self.assertEqual(by_name["dos"]["path"], "/userdata/roms/dos")
        self.assertEqual(by_name["dos"]["fullname"], "DOS")
        self.assertFalse(by_name["dos"]["hidden"])
        self.assertTrue(by_name["gamecube"]["command"].startswith(str(self.wrapper)))
        self.assertEqual(by_name["gamecube"]["path"], "/userdata/roms/gamecube")

    def test_network_overlay_ignores_the_command_overlay_and_merges_partials(self):
        from app.device import network_es_overlay
        self.assertIn(es_overlay.OVERLAY_FILENAME, network_es_overlay.COMMAND_ONLY_OVERLAYS)
        es_overlay.apply(self.settings, self.wrapper, self.runtime, self.marker)
        nodes = network_es_overlay._effective_system_nodes(self.settings)
        self.assertEqual(nodes["gamecube"].findtext("command"), "emulatorlauncher %CONTROLLERSCONFIG% -system %SYSTEM% -rom %ROM%")
        self.assertEqual(nodes["dos"].findtext("path"), "/userdata/roms/dos")
        self.assertEqual(nodes["dos"].findtext("hardware"), "pc")

    def test_restart_needed_compares_emulationstation_start(self):
        proc = self.base / "proc"
        (proc / "123").mkdir(parents=True)
        (proc / "123" / "comm").write_text("emulationstatio\n")
        started = os.stat(proc / "123").st_ctime
        self.marker.write_text(json.dumps({"changed_at": started + 10}))
        self.assertTrue(es_overlay.restart_needed(self.marker, proc))
        self.marker.write_text(json.dumps({"changed_at": started - 10}))
        self.assertFalse(es_overlay.restart_needed(self.marker, proc))
        self.assertFalse(es_overlay.restart_needed(self.marker, self.base / "noproc"))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.settings = make_settings(self.base)
        es_overlay._CACHE.clear()
        self.paths = ControllerAssignmentsPaths(self.base / "drone" / "integrations" / "controller-assignments")
        self.proc = self.base / "proc"
        self.proc.mkdir()
        self.manager = ControllerAssignmentsIntegration(self.settings, None, paths=self.paths,
                                                        controller_detector=lambda: [], proc_root=self.proc)

    def tearDown(self):
        self.tmp.cleanup()

    def _start_es(self):
        (self.proc / "77").mkdir(exist_ok=True)
        (self.proc / "77" / "comm").write_text("emulationstatio\n")

    def test_enable_disable_lifecycle(self):
        self.assertEqual(self.manager.card_status()["health"], "disabled")
        self._start_es()
        time.sleep(0.05)
        result = self.manager.enable(requested_by="admin")
        self.assertTrue(result["restart_needed"])
        self.assertEqual(self.paths.wrapper.read_bytes(), WRAPPER_SOURCE.read_bytes())
        self.assertTrue(os.access(self.paths.wrapper, os.X_OK))
        runtime = json.loads(self.paths.runtime_file.read_text())
        self.assertTrue(runtime["enabled"])
        self.assertEqual(runtime["report_file"], str(self.paths.report_file))
        status = self.manager.get_status()
        self.assertEqual(status["health"], "waiting")
        self.assertEqual(status["wrapped_systems"], 2)
        self.assertIn("gamecube", status["systems"])

        self.manager.set_auto(True)
        self.manager.set_preset("n64", True)
        self.manager.set_system("gamecube", [{"kind": "type", "type": "gamecube"}])
        self.manager.set_game("gamecube/Mario Kart.iso", [{"kind": "type", "type": "gamecube"}], "Mario Kart")
        runtime = json.loads(self.paths.runtime_file.read_text())
        self.assertTrue(runtime["auto"])
        self.assertEqual(runtime["systems"]["n64"]["source"], "preset:n64")
        self.assertIn("gamecube/Mario Kart.iso", runtime["games"])

        self.manager.disable()
        self.assertFalse(es_overlay.overlay_path(self.settings).exists())
        self.assertFalse(json.loads(self.paths.runtime_file.read_text())["enabled"])  # pass-through until restart
        self.assertTrue(self.paths.wrapper.exists())
        self.assertEqual(self.manager.config.load()["systems"]["gamecube"]["order"][0]["type"], "gamecube")

    def test_remove_keeps_wrapper_until_emulationstation_restarts(self):
        self.manager.enable()
        self._start_es()
        time.sleep(0.05)
        result = self.manager.remove(include_configuration=True)
        self.assertEqual(result["status"], "pending-restart")
        self.assertTrue(self.paths.wrapper.exists())
        # After EmulationStation restarts, Remove deletes everything.
        self.paths.state_dir.joinpath("overlay.json").write_text(json.dumps({"changed_at": 0}))
        result = self.manager.remove(include_configuration=True)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(self.paths.root.exists())

    def test_repair_resyncs_after_another_overlay_changes(self):
        self.manager.enable()
        es_dir = self.settings.userdata_root / "system" / "configs" / "emulationstation"
        (es_dir / "es_systems_gamecube.cfg").write_text(
            "<systemList><system><name>gamecube</name><command>/opt/other %CONTROLLERSCONFIG%</command></system></systemList>")
        self.assertEqual(self.manager.get_status()["health"], "degraded")
        self.manager.repair()
        self.assertEqual(self.manager.get_status()["health"], "healthy")

    def test_registry_lists_the_integration(self):
        with mock.patch("app.integrations.controller_assignments.manager.ControllerAssignmentsPaths.default",
                        return_value=self.paths):
            ids = build_integration_registry(self.settings, None).ids()
        self.assertIn("controller-assignments", ids)


class HandlerTests(unittest.TestCase):
    def _handler(self, body):
        from app.web.handlers_integrations import HandlersIntegrationsMixin
        handler = HandlersIntegrationsMixin()
        handler.sent = []
        handler._send_json = lambda code, payload: handler.sent.append((code, payload))
        handler._read_json_body = lambda: body
        handler._integration_actor = lambda: "admin"
        handler.manager = mock.Mock()
        handler._controller_assignments = lambda: handler.manager
        return handler

    def test_post_routes_dispatch_to_the_manager(self):
        cases = [
            (["auto"], {"enabled": True}, "set_auto"),
            (["default"], {"order": []}, "set_default"),
            (["presets", "snes"], {"enabled": True}, "set_preset"),
            (["systems"], {"system": "snes", "order": []}, "set_system"),
            (["systems", "delete"], {"system": "snes"}, "delete_system"),
            (["games"], {"game": "snes/a.sfc", "order": []}, "set_game"),
            (["games", "delete"], {"game": "snes/a.sfc"}, "delete_game"),
            (["enable"], {}, "enable"),
            (["disable"], {}, "disable"),
            (["repair"], {}, "repair"),
            (["remove"], {}, "remove"),
        ]
        for sub, body, method in cases:
            with self.subTest(sub=sub):
                handler = self._handler(body)
                getattr(handler.manager, method).return_value = {"status": "ok"}
                handler._dispatch_controller_assignments_post(sub)
                self.assertTrue(getattr(handler.manager, method).called)
                self.assertEqual(handler.sent[-1][0], 200)
        with self.assertRaises(KeyError):
            self._handler({})._dispatch_controller_assignments_post(["nope"])

    def test_ui_route_and_page_are_shipped(self):
        js = (ROOT / "app/web/static/js/integrations.js").read_text()
        router = (ROOT / "app/web/static/js/drone.js").read_text()
        self.assertIn("async function renderControllerAssignmentsPage()", js)
        self.assertIn('hash === "#admin/integrations/controller-assignments"', router)
        self.assertIn("themed-table", js[js.index("controller assignments page"):])


if __name__ == "__main__":
    unittest.main()
