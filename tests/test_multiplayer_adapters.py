"""Emulator adapters against a scripted (SIMULATED) Batocera machine.

These tests prove the adapters detect honestly and never over-claim; they do not
prove a real emulator session works (see docs/multiplayer.md for real-hardware status).
"""

import json
import tempfile
import unittest
from pathlib import Path

from app.multiplayer import models as m
from app.multiplayer.adapters import build_adapters, adapters_by_id
from app.multiplayer.adapters.base import FULL, MANUAL, TIER_DETECT_ONLY, parse_players
from app.multiplayer.adapters.dolphin import DolphinAdapter, read_game_id
from app.multiplayer.adapters.ppsspp import PPSSPPAdapter, parse_ini_keys
from app.multiplayer.adapters.retroarch import RetroArchAdapter, parse_info
from app.multiplayer.adapters.streaming import StreamingAdapter, build_stream_profile
from app.multiplayer.adapters.switch import SwitchLdnAdapter
from multiplayer_support import FakeProbe, RETRO_FILES
from test_multiplayer_core import make_settings

SNES = {"system": "snes", "rom_name": "Kart.sfc", "players": 2, "title_key": "kart"}


class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = make_settings(tmp.name)

    def probe(self, **kwargs):
        return FakeProbe(self.settings, **kwargs)


class RetroArchTests(AdapterTestCase):
    def adapter(self, files=None, conf=None, binaries=None):
        probe = self.probe(binaries=binaries if binaries is not None else {"retroarch": "/usr/bin/retroarch"},
                           versions={"/usr/bin/retroarch": "1.19.1"}, files={**RETRO_FILES, **(files or {})}, conf=conf or {})
        return RetroArchAdapter(probe)

    def test_not_installed_is_reported_not_assumed(self):
        adapter = self.adapter(binaries={})
        self.assertFalse(adapter.detect_installed())
        self.assertEqual(adapter.readiness()["status"], "not_installed")
        result = adapter.assess_game(SNES)
        self.assertEqual(result["classification"], m.UNSUPPORTED)
        self.assertFalse(result["ready"])

    def test_automation_requires_every_netplay_key_in_the_launcher(self):
        self.assertEqual(self.adapter().effective_automation(), FULL)
        partial = self.adapter(files={"/usr/lib/python3.12/site-packages/configgen/emulatorlauncher.py": "netplay.mode only"})
        self.assertEqual(partial.effective_automation(), MANUAL)
        self.assertIn("does not expose", partial.readiness()["message"])
        missing = self.adapter()
        missing.probe.files.pop("/usr/lib/python3.12/site-packages/configgen/emulatorlauncher.py")
        missing.refresh()
        self.assertEqual(missing.effective_automation(), MANUAL)

    def test_unverified_launcher_refuses_to_configure_or_launch(self):
        adapter = self.adapter(files={"/usr/lib/python3.12/site-packages/configgen/emulatorlauncher.py": "nothing"})
        session = {"game": {"system": "snes", "rom_name": "Kart.sfc"}, "emulator": {"core": "snes9x"}}
        with self.assertRaises(m.CapabilityError) as caught:
            adapter.configure_networking(session, m.HOST_ROLE, {"conf_path": "x", "secret": "s", "port": 55435})
        self.assertEqual(caught.exception.code, "netplay_keys_unverified")
        with self.assertRaises(m.CapabilityError):
            adapter.launch(session, m.HOST_ROLE, {"game_path": "/x"}, launcher=None)

    def test_deterministic_core_is_likely_never_verified(self):
        result = self.adapter(conf={"snes.core": "snes9x"}).assess_game(SNES)
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["emulator"]["core_version"], "1.62.3")
        self.assertTrue(result["ready"])

    def test_unset_core_uses_an_installed_candidate_for_the_session_only(self):
        result = self.adapter().assess_game(SNES)
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(result["candidate_cores"], ["snes9x"])
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["core_source"], "installed_candidate")
        self.assertTrue(result["ready"])
        self.assertTrue(any("session only" in reason for reason in result["reasons"]))

    def test_batocera_default_core_is_used_when_nothing_is_configured(self):
        defaults = "snes:\n  emulator: libretro\n  core:     snes9x  # default\nn64:\n  emulator: mupen64plus\n"
        adapter = self.adapter(files={"/usr/share/batocera/configgen/configgen-defaults.yml": defaults})
        result = adapter.assess_game(SNES)
        self.assertEqual(result["core_source"], "batocera_default")
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)
        # The old "No emulator is set ... assuming" wording never reaches the user.
        self.assertFalse(any("No emulator is set" in reason for reason in result["reasons"]))

    def test_missing_default_core_falls_back_to_an_installed_one(self):
        defaults = "snes:\n  emulator: libretro\n  core: bsnes\n"
        result = self.adapter(files={"/usr/share/batocera/configgen/configgen-defaults.yml": defaults}).assess_game(SNES)
        self.assertEqual(result["emulator"]["core"], "snes9x")
        self.assertIn("bsnes", " ".join(result["reasons"]))

    def test_default_emulator_that_is_not_libretro_is_unsupported(self):
        defaults = "snes:\n  emulator: other\n  core: x\n"
        result = self.adapter(files={"/usr/share/batocera/configgen/configgen-defaults.yml": defaults}).assess_game(SNES)
        self.assertEqual(result["classification"], m.UNSUPPORTED)

    def test_no_installed_core_blocks_with_the_real_reason_and_install_hint_first(self):
        adapter = self.adapter()
        adapter.probe.files.pop("/usr/lib/libretro/snes9x_libretro.so")
        adapter.refresh()
        result = adapter.assess_game(SNES)
        self.assertEqual(result["classification"], m.UNSUPPORTED)
        self.assertIn("Content Downloader", result["reasons"][0])

    def test_downloaded_cores_in_userdata_count_as_installed(self):
        adapter = self.adapter(conf={"snes.core": "bsnes"}, files={"/userdata/cores/bsnes_libretro.so": "binary"})
        self.assertTrue(adapter.core_installed("bsnes"))

    def test_single_player_reason_is_the_first_blocker(self):
        result = self.adapter(conf={"snes.core": "snes9x"}).assess_game({**SNES, "players": 1})
        self.assertIn("single player", result["reasons"][0])

    def test_session_override_supplies_the_core(self):
        result = self.adapter().assess_game(SNES, {"core_overrides": {"snes": "snes9x"}})
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)

    def test_selected_core_not_installed_is_incompatible(self):
        result = self.adapter(conf={"snes.core": "bsnes"}).assess_game(SNES)
        self.assertEqual(result["classification"], m.INCOMPATIBLE)

    def test_other_emulator_configured_is_unsupported(self):
        result = self.adapter(conf={"snes.emulator": "mednafen", "snes.core": "x"}).assess_game(SNES)
        self.assertEqual(result["classification"], m.UNSUPPORTED)

    def test_player_count_gates_the_claim(self):
        adapter = self.adapter(conf={"snes.core": "snes9x"})
        self.assertEqual(adapter.assess_game({**SNES, "players": 1})["classification"], m.UNSUPPORTED)
        self.assertEqual(adapter.assess_game({**SNES, "players": None})["classification"], m.UNKNOWN)

    def test_core_without_deterministic_savestates_is_experimental(self):
        info = {"/usr/share/libretro/info/snes9x_libretro.info": 'savestate = "true"\nsavestate_features = "basic"\n'}
        self.assertEqual(self.adapter(files=info, conf={"snes.core": "snes9x"}).assess_game(SNES)["classification"], m.EXPERIMENTAL)
        nosave = {"/usr/share/libretro/info/snes9x_libretro.info": 'savestate = "false"\n'}
        self.assertEqual(self.adapter(files=nosave, conf={"snes.core": "snes9x"}).assess_game(SNES)["classification"], m.UNSUPPORTED)

    def test_unrelated_systems_are_out_of_scope(self):
        self.assertIsNone(self.adapter().assess_game({"system": "ps3", "rom_name": "x", "players": 2}))

    def test_guest_validation_compares_cores_and_versions(self):
        adapter = self.adapter(conf={})
        base = {"game": {"rom_name": "Kart.sfc"}, "emulator": {"core": "snes9x", "core_version": "1.62.3", "version": "1.19.2"}}
        self.assertEqual(adapter.validate_configuration(base, m.GUEST_ROLE), [])
        differing = {**base, "emulator": {**base["emulator"], "core_version": "1.60"}}
        self.assertIn("versions differ", adapter.validate_configuration(differing, m.GUEST_ROLE)[0])
        missing = {**base, "emulator": {**base["emulator"], "core": "bsnes"}}
        self.assertIn("not installed", adapter.validate_configuration(missing, m.GUEST_ROLE)[0])
        old = {**base, "emulator": {**base["emulator"], "version": "1.17.0"}}
        self.assertIn("incompatible", adapter.validate_configuration(old, m.GUEST_ROLE)[0])

    def test_edits_are_scoped_to_the_one_rom_and_pin_the_host_core(self):
        adapter = self.adapter()
        session = {"game": {"system": "snes", "rom_name": "Kart (USA).sfc"}, "emulator": {"core": "snes9x"}}
        context = {"conf_path": "/c/batocera.conf", "secret": "abc123", "port": 55440, "nickname": "Pi; rm", "host_address": "10.0.0.5"}
        host = {edit["key"]: edit["value"] for edit in adapter.configure_networking(session, m.HOST_ROLE, context)}
        self.assertEqual(host['snes["Kart (USA).sfc"].netplay.mode'], "host")
        self.assertEqual(host['snes["Kart (USA).sfc"].netplay.server.port'], "55440")
        self.assertEqual(host['snes["Kart (USA).sfc"].core'], "snes9x")
        self.assertEqual(host['snes["Kart (USA).sfc"].netplay.nickname'], "Pi rm")
        guest = {edit["key"]: edit["value"] for edit in adapter.configure_networking(session, m.GUEST_ROLE, context)}
        self.assertEqual(guest['snes["Kart (USA).sfc"].netplay.server.ip'], "10.0.0.5")
        self.assertEqual(guest['snes["Kart (USA).sfc"].netplay.mode'], "client")
        self.assertTrue(all(key.startswith('snes["Kart (USA).sfc"].') for key in {**host, **guest}
                            if key not in ("netplay_password", "netplay_spectate_password")))
        watched = [edit for edit in adapter.configure_networking(session, m.HOST_ROLE, context) if edit.get("watch")]
        self.assertEqual({edit["key"] for edit in watched}, {"netplay_password", "netplay_spectate_password"})
        self.assertTrue(all(edit["path"].endswith("retroarchcustom.cfg") for edit in watched))

    def test_parse_info(self):
        self.assertEqual(parse_info('a = "b"\nc=d\n')["a"], "b")

    def test_parse_players(self):
        self.assertEqual((parse_players("1-4"), parse_players("2"), parse_players(""), parse_players("99"), parse_players(None)), (4, 2, None, None, None))


class DolphinTests(AdapterTestCase):
    def adapter(self, version="2506", installed=True):
        probe = self.probe(binaries={"dolphin-emu": "/usr/bin/dolphin-emu"} if installed else {},
                           versions={"/usr/bin/dolphin-emu": version})
        return DolphinAdapter(probe)

    def test_never_claims_automated_launch(self):
        adapter = self.adapter()
        self.assertEqual(adapter.effective_automation(), MANUAL)
        with self.assertRaises(m.CapabilityError):
            adapter.launch({}, m.HOST_ROLE, {"game_path": "x"}, None)

    def test_version_mismatch_gives_the_actionable_message(self):
        adapter = self.adapter(version="2506")
        problems = adapter.validate_configuration({"emulator": {"version": "2412"}}, m.GUEST_ROLE)
        self.assertEqual(len(problems), 1)
        self.assertIn("host and client are running incompatible Dolphin versions", problems[0])
        self.assertEqual(adapter.validate_configuration({"emulator": {"version": "2506"}}, m.GUEST_ROLE), [])

    def test_unknown_version_requires_configuration(self):
        result = self.adapter(version="").assess_game({"system": "gamecube", "rom_name": "x.iso", "players": 4})
        self.assertEqual(result["classification"], m.REQUIRES_CONFIGURATION)

    def test_assessment_and_steps(self):
        adapter = self.adapter()
        result = adapter.assess_game({"system": "wii", "rom_name": "x.iso", "players": 4})
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(result["automation"], MANUAL)
        steps = adapter.prepare_session({"game": {"title": "Mario Kart"}}, m.GUEST_ROLE, {"host_address": "10.0.0.2", "port": 2626})[ "manual_steps"]
        self.assertTrue(any("10.0.0.2" in step for step in steps))

    def test_game_id_is_read_only_from_plain_images(self):
        path = self.tmp / "game.iso"
        header = bytearray(0x40)
        header[:6] = b"GALE01"
        header[0x1C:0x20] = bytes.fromhex("C2339F3D")
        path.write_bytes(bytes(header))
        self.assertEqual(read_game_id(path), "GALE01")
        compressed = self.tmp / "game.rvz"
        compressed.write_bytes(bytes(header))
        self.assertEqual(read_game_id(compressed), "")
        junk = self.tmp / "junk.iso"
        junk.write_bytes(b"\x00" * 64)
        self.assertEqual(read_game_id(junk), "")
        self.assertEqual(read_game_id(self.tmp / "nope.iso"), "")


class PPSSPPTests(AdapterTestCase):
    INI = "[General]\nA = 1\n[Network]\nEnableWlan = False\nProAdhocServer = socom.cc\nEnableAdhocServer = False\n"

    def adapter(self, ini=INI):
        path = self.settings.userdata_root / "system" / "configs" / "ppsspp" / "PSP" / "SYSTEM" / "ppsspp.ini"
        files = {str(path): ini} if ini is not None else {}
        return PPSSPPAdapter(self.probe(binaries={"PPSSPPSDL": "/usr/bin/PPSSPPSDL"}, versions={"/usr/bin/PPSSPPSDL": "1.18.1"}, files=files))

    def test_only_offered_when_the_build_has_the_keys(self):
        self.assertTrue(self.adapter().detect_features()[m.PPSSPP_ADHOC]["available"])
        self.assertFalse(self.adapter(ini=None).detect_features()[m.PPSSPP_ADHOC]["available"])
        no_builtin = self.adapter(ini="[Network]\nEnableWlan = False\nProAdhocServer = x\n")
        feature = no_builtin.detect_features()[m.PPSSPP_ADHOC]
        self.assertTrue(feature["available"])
        self.assertFalse(feature["builtin_server"])
        self.assertTrue(no_builtin.validate_configuration({}, m.HOST_ROLE))
        self.assertEqual(no_builtin.validate_configuration({}, m.GUEST_ROLE), [])

    def test_titles_are_experimental_never_verified(self):
        result = self.adapter().assess_game({"system": "psp", "rom_name": "x.iso", "players": 4})
        self.assertEqual(result["classification"], m.EXPERIMENTAL)
        self.assertEqual(self.adapter().assess_game({"system": "psp", "rom_name": "x.iso", "players": None})["classification"], m.UNKNOWN)

    def test_host_and_guest_edits(self):
        adapter = self.adapter()
        host = {e["key"]: e["value"] for e in adapter.configure_networking({}, m.HOST_ROLE, {})}
        self.assertEqual((host["EnableAdhocServer"], host["ProAdhocServer"]), ("True", "127.0.0.1"))
        guest = {e["key"]: e["value"] for e in adapter.configure_networking({}, m.GUEST_ROLE, {"host_address": "10.1.1.9"})}
        self.assertEqual((guest["EnableAdhocServer"], guest["ProAdhocServer"]), ("False", "10.1.1.9"))
        self.assertTrue(all(e["kind"] == "ini" and e["section"] == "Network" for e in adapter.configure_networking({}, m.HOST_ROLE, {})))

    def test_ini_parser(self):
        self.assertEqual(parse_ini_keys(self.INI, "network")["ProAdhocServer"], "socom.cc")


class SwitchTests(AdapterTestCase):
    def adapter(self, binary="/usr/ryujinx/Ryujinx", config=None):
        files = {}
        if config is not None:
            files[str(self.settings.userdata_root / "system" / "configs" / "Ryujinx" / "Config.json")] = json.dumps(config)
        return SwitchLdnAdapter(self.probe(binaries={"Ryujinx": binary} if binary else {}, files=files))

    def test_requires_a_multiplayer_mode_in_the_emulator_settings(self):
        self.assertFalse(self.adapter(config={"x": 1}).detect_features()[m.SWITCH_LDN]["available"])
        self.assertTrue(self.adapter(config={"multiplayer_mode": "LdnMitm"}).detect_features()[m.SWITCH_LDN]["available"])

    def test_eden_is_detected_but_not_automated(self):
        adapter = SwitchLdnAdapter(self.probe(binaries={"eden": "/usr/bin/eden"}))
        feature = adapter.detect_features()[m.SWITCH_LDN]
        self.assertFalse(feature["available"])
        self.assertIn("room servers", feature["reason"])

    def test_lan_discovery_is_flagged_and_never_verified(self):
        result = self.adapter(config={"multiplayer_mode": "LdnMitm"}).assess_game({"system": "switch", "rom_name": "x.nsp", "players": 4})
        self.assertEqual(result["classification"], m.EXPERIMENTAL)
        self.assertTrue(result["needs_same_lan"])
        self.assertEqual(result["automation"], MANUAL)

    def test_not_installed(self):
        self.assertEqual(self.adapter(binary="").assess_game({"system": "switch", "rom_name": "x", "players": 2})["classification"], m.UNSUPPORTED)


class StreamingTests(AdapterTestCase):
    def adapter(self, sunshine=True, moonlight=True, running=True, hardware=None):
        binaries = {}
        if sunshine:
            binaries["sunshine"] = "/usr/bin/sunshine"
        if moonlight:
            binaries["moonlight"] = "/usr/bin/moonlight"
        return StreamingAdapter(self.probe(binaries=binaries, processes={"sunshine"} if running else (), hardware=hardware))

    def test_requires_sunshine_on_the_host(self):
        result = self.adapter(sunshine=False).assess_game({"system": "n64", "rom_name": "x.z64", "players": 4})
        self.assertEqual(result["classification"], m.UNSUPPORTED)
        self.assertTrue(result["guest_ready"])

    def test_sunshine_not_running_needs_configuration(self):
        result = self.adapter(running=False).assess_game({"system": "n64", "rom_name": "x", "players": 4})
        self.assertEqual(result["classification"], m.REQUIRES_CONFIGURATION)

    def test_weak_host_without_hardware_encoder_is_unsuitable(self):
        weak = self.adapter(hardware={"cpu_count": 2, "vaapi": False, "nvenc": False})
        self.assertEqual(weak.assess_game({"system": "n64", "rom_name": "x", "players": 4})["classification"], m.UNSUPPORTED)

    def test_works_for_any_system_but_is_not_called_verified(self):
        result = self.adapter().assess_game({"system": "n64", "rom_name": "x", "players": 4})
        self.assertEqual(result["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(result["max_players"], 2)
        self.assertEqual(result["content_match"], "host_only")
        self.assertEqual(self.adapter().assess_game({"system": "n64", "rom_name": "x", "players": None})["classification"], m.UNKNOWN)

    def test_guest_needs_moonlight_and_host_needs_sunshine(self):
        adapter = self.adapter(moonlight=False)
        self.assertEqual(adapter.validate_configuration({}, m.HOST_ROLE), [])
        self.assertTrue(adapter.validate_configuration({}, m.GUEST_ROLE))
        self.assertTrue(self.adapter(sunshine=False).validate_configuration({}, m.HOST_ROLE))

    def test_profile_never_exceeds_the_configured_ceilings(self):
        profile = build_stream_profile({"quality": "quality", "max_resolution": "1280x720", "max_fps": 30, "max_bitrate_kbps": 5000, "encoding": "auto"},
                                       {"vaapi": True, "nvenc": False})
        self.assertEqual((profile["resolution"], profile["fps"], profile["bitrate_kbps"], profile["encoder"]), ("1280x720", 30, 5000, "hardware"))
        software = build_stream_profile({"quality": "balanced", "max_resolution": "1920x1080", "max_fps": 60, "max_bitrate_kbps": 50000, "encoding": "software"},
                                        {"vaapi": True, "nvenc": False})
        self.assertEqual(software["encoder"], "software")
        self.assertLessEqual(software["bitrate_kbps"], 15000)

    def test_guests_launch_nothing(self):
        self.assertFalse(self.adapter().guest_launches)


class RegistryTests(AdapterTestCase):
    def test_experimental_emulators_are_detect_only_and_never_pretend(self):
        adapters = adapters_by_id(build_adapters(self.probe(binaries={"rpcs3": "/x/rpcs3"})))
        for name in ("rpcs3", "pcsx2", "xemu", "cemu", "flycast", "melonds", "azahar", "vita3k", "xenia", "shadps4"):
            self.assertEqual(adapters[name].tier, TIER_DETECT_ONLY, name)
            self.assertEqual(adapters[name].mechanisms, (), name)
        result = adapters["rpcs3"].assess_game({"system": "ps3", "rom_name": "x", "players": 4})
        self.assertEqual(result["classification"], m.UNSUPPORTED)
        self.assertEqual(result["mechanism"], m.EMULATOR_NATIVE_NETWORK)
        self.assertEqual(adapters["rpcs3"].capabilities()["installed"], True)
        self.assertFalse(adapters["xemu"].capabilities()["installed"])
        self.assertIn("not installed", adapters["xemu"].assess_game({"system": "xbox", "rom_name": "x", "players": 2})["reasons"][0])

    def test_generic_lan_flags_broadcast_and_stays_untested(self):
        lan = adapters_by_id(build_adapters(self.probe()))["lan"]
        result = lan.assess_game({"system": "ps2", "rom_name": "x", "players": 4})
        self.assertEqual(result["classification"], m.UNKNOWN)
        self.assertTrue(any("broadcast" in reason.lower() for reason in result["reasons"]))
        self.assertIsNone(lan.assess_game({"system": "snes", "rom_name": "x", "players": 2}))

    def test_every_adapter_reports_a_complete_capability_document(self):
        for adapter in build_adapters(self.probe()):
            with self.subTest(adapter=adapter.id):
                document = adapter.capabilities()
                for key in ("id", "name", "tier", "automation", "installed", "systems", "mechanisms", "protocols", "requirements",
                            "permissions", "limitations", "readiness", "supported_versions"):
                    self.assertIn(key, document)
                json.dumps(document)

    def test_unsupported_operations_raise_capability_errors(self):
        lan = adapters_by_id(build_adapters(self.probe()))["lan"]
        with self.assertRaises(m.CapabilityError) as caught:
            lan.launch({}, m.HOST_ROLE, {"game_path": "x"}, None)
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
