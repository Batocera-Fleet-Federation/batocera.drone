"""Issue #240 adversarial UAT: matching honesty, capacity, and mechanism choice.

Expected behaviour, derived from the issue:

* A multiplayer-capable console does not make a title VERIFIED_COMPATIBLE.
* Detect-only emulators never advertise working native networking.
* RetroArch/Dolphin joining needs identical ROM content. Streaming does not:
  only the host needs the game. PPSSPP may join on title match.
* Forced mechanisms are honoured or refused, never silently substituted.
* Broadcast LAN mechanisms are unusable over Tailscale.
* Maximum players follow the game's recorded capability, not an arbitrary 8.
* Unpaired peers cannot be invited. Library listing never hashes the catalogue.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.adapters.detect_only import EXPERIMENTAL_EMULATORS
from multiplayer_support import Node, Hub, make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
PSP_INI = "[Network]\nEnableWlan = False\nProAdhocServer = x\nEnableAdhocServer = False\n"


class SwarmCase(unittest.TestCase):
    names = ("host", "guest")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.hub, self.nodes = make_swarm(self.tmp, self.names, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code


class MatchingHonestyTests(SwarmCase):
    def test_snes_is_not_verified_just_because_the_console_has_netplay(self):
        page = self.h.list_games({"system": "snes"})
        game = page["games"][0]
        self.assertNotEqual(game["classification"], m.VERIFIED_COMPATIBLE)
        self.assertNotEqual(game["status"], "verified")
        for item in game["mechanisms"]:
            self.assertNotEqual(item["classification"], m.VERIFIED_COMPATIBLE)
        with mock.patch.object(self.h.library, "content_hash", side_effect=AssertionError("browser hashed")):
            again = self.h.list_games({"system": "snes"})
        self.assertEqual(again["total"], 1)

    def test_detect_only_emulators_are_unsupported_native_networking(self):
        adapters = {adapter.id: adapter for adapter in self.h.adapters}
        for adapter_id, _name, systems, _binaries, _note in EXPERIMENTAL_EMULATORS:
            adapter = adapters[adapter_id]
            self.assertEqual(adapter.tier, "detect_only")
            self.assertEqual(adapter.effective_automation(), "manual")
            game = {"system": systems[0], "rom_name": "x", "players": 4, "title": "X"}
            assessment = adapter.assess_game(game)
            self.assertIsNotNone(assessment)
            self.assertEqual(assessment["classification"], m.UNSUPPORTED)
            self.assertFalse(assessment["ready"])
            readiness = adapter.readiness()
            if adapter.detect_installed():
                self.assertEqual(readiness["status"], "unsupported_build")

    def test_different_rom_bytes_block_retroarch_join(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        path = self.g.library.absolute_path(self.g.library.get("snes", "g1"))
        path.write_bytes(b"different content despite identical sampled metadata")
        self.assertEqual(self.code(self.g.accept_invitation, sid), "cannot_join")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "invited")

    def test_hash_refuses_a_rom_that_changes_while_it_is_being_read(self):
        game = self.h.library.get("snes", "g1")
        path = self.h.library.absolute_path(game)
        original_open = Path.open

        def mutate_during_read(self_path, *args, **kwargs):
            handle = original_open(self_path, *args, **kwargs)
            if Path(self_path) == path:
                fd = os.open(str(path), os.O_WRONLY | os.O_TRUNC)
                try:
                    os.write(fd, b"mutated-during-hash")
                finally:
                    os.close(fd)
            return handle

        with mock.patch.object(Path, "open", mutate_during_read):
            self.assertEqual(self.code(self.h.library.content_hash, game), "content_changed")

    def test_max_players_follows_the_game_and_unpaired_peers_cannot_join(self):
        session = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                         "max_players": 8, **CORE})
        self.assertEqual(session["max_players"], 2)
        self.assertEqual(self.code(self.h.invite_peer, session["id"], "stranger"), "not_paired")
        self.hub, extra = make_swarm(self.tmp + "-cap", ("host", "guest", "mallory"), open_ports=(55435,))
        host = extra["host"].orch
        self.assertEqual(
            self._code(host.create_session, {"system": "snes", "game_id": "g1",
                                             "peers": ["guest", "mallory"], **CORE}),
            "too_many_players",
        )

    def _code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def test_forced_unusable_mechanism_is_refused_not_substituted(self):
        self.assertEqual(
            self.code(self.h.create_session, {"system": "snes", "game_id": "g1", "peers": ["guest"],
                                              "mechanism": m.REMOTE_STREAMING, **CORE}),
            "mechanism_unusable",
        )
        self.assertEqual(self.h.list_sessions(), [])
        self.assertEqual(
            self.code(self.h.create_session, {"system": "snes", "game_id": "g1", "peers": ["guest"],
                                              "mechanism": "ROOT_SHELL", **CORE}),
            "invalid_mechanism",
        )

    def test_broadcast_lan_over_tailscale_is_unusable(self):
        self.nodes["guest"].network.scope = "tailnet"
        game = {"id": "ps1", "file": "LAN.iso", "title": "LAN Title", "fingerprint": "11" * 16,
                "players": "1-4", "size": 1000}
        for node in self.nodes.values():
            node.repo.games["ps2"] = [dict(game)]
            (node.root / "roms" / "ps2").mkdir(parents=True, exist_ok=True)
            (node.root / "roms" / "ps2" / game["file"]).write_bytes(b"iso")
            node.orch.library.invalidate()
        self.assertEqual(
            self.code(self.h.create_session, {"system": "ps2", "game_id": "ps1", "peers": ["guest"],
                                              "mechanism": m.EMULATED_LAN}),
            "mechanism_unusable",
        )


class StreamingAndTitleMatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def test_streaming_lets_a_guest_without_the_rom_join_and_does_not_launch_there(self):
        hub, nodes = make_swarm(
            self.tmp, games_for=("host",),
            binaries={"retroarch": "/usr/bin/retroarch", "sunshine": "/usr/bin/sunshine",
                      "moonlight": "/usr/bin/moonlight"},
            processes={"sunshine"}, versions={"/usr/bin/retroarch": "1.19.1"}, open_ports=(55435,),
        )
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["mechanism"], m.REMOTE_STREAMING)
        self.assertIn("no native mechanism", session["mechanism_reason"].lower())
        guest.accept_invitation(session["id"])
        guest.set_ready(session["id"], True)
        host.set_ready(session["id"], True)
        host.start_session(session["id"])
        self.assertEqual(len(nodes["host"].launcher.launched), 1)
        self.assertEqual(nodes["guest"].launcher.launched, [])
        self.assertTrue(guest.get_session(session["id"])["my_steps"])

    def test_ppsspp_title_match_allows_different_file_bytes(self):
        hub = Hub()
        games = {
            "host": {"id": "p1", "file": "Tekken.iso", "title": "Tekken", "fingerprint": "aa" * 16,
                     "players": "1-4", "size": 1000},
            "guest": {"id": "p1", "file": "Tekken (EUR).iso", "title": "Tekken", "fingerprint": "bb" * 16,
                      "players": "1-4", "size": 2000},
        }
        nodes = {}
        for name in ("host", "guest"):
            node = Node(
                hub, name, Path(self.tmp),
                games={"psp": [games[name]]},
                paired=["guest" if name == "host" else "host"],
                binaries={"PPSSPPSDL": "/usr/bin/PPSSPPSDL"},
                versions={"/usr/bin/PPSSPPSDL": "1.18.1"},
            )
            ini = str(node.settings.userdata_root / "system/configs/ppsspp/PSP/SYSTEM/ppsspp.ini")
            Path(ini).parent.mkdir(parents=True, exist_ok=True)
            Path(ini).write_text(PSP_INI, encoding="utf-8")
            node.probe.files[ini] = PSP_INI
            node.orch.adapter_map["ppsspp"].refresh()
            (node.root / "roms" / "psp" / games[name]["file"]).write_bytes(b"host-rom" if name == "host" else b"guest-rom")
            nodes[name] = node
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "psp", "game_id": "p1", "peers": ["guest"]})
        self.assertEqual(session["mechanism"], m.PPSSPP_ADHOC)
        joined = guest.accept_invitation(session["id"])
        self.assertEqual(joined["players"]["guest"]["status"], "joined")
        self.assertEqual(nodes["guest"].launcher.launched, [])


if __name__ == "__main__":
    unittest.main()
