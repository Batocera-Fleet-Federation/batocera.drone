"""#240 §§3, 6, 8, 10: lobby capacity after a mechanism switch, and lossless temporary configuration.

Expected invariants, derived from the issue before reading the implementation:

- Spec 2.3: "Support configurable maximum players based on the game's actual
  capabilities." Spec 6 / adapter docs: Drone limits a streaming lobby to the
  host plus one remote guest. Whatever path adds a player (create, Invite
  Peer, accept) the number of occupied seats must never exceed the session's
  published ``max_players``, including when adding a peer makes the host fall
  back from a native mechanism (4 players) to streaming (2 players).
- Spec 10: "Never corrupt the user's permanent settings", "Preserve the
  original values", "Existing emulator settings remain intact". Applying and
  restoring temporary multiplayer keys must leave every unrelated line of
  batocera.conf / ppsspp.ini exactly as the user wrote it, including lines
  that contain Unicode line separators (U+2028, NEL, form feed) that ordinary
  ROM names or host names can legitimately hold, and must restore the file
  byte-for-byte when nothing else changed.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch or Sunshine gameplay.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.config_guard import ConfigGuard
from multiplayer_support import make_swarm
from test_multiplayer_core import make_settings

CORE = {"emulator_override": {"core": "snes9x"}}
STREAM = {
    "binaries": {
        "retroarch": "/usr/bin/retroarch",
        "sunshine": "/usr/bin/sunshine",
        "moonlight": "/usr/bin/moonlight",
    },
    "processes": {"sunshine"},
    "versions": {"/usr/bin/retroarch": "1.19.1"},
    "open_ports": (55435,),
}
DEPARTED = (m.P_DECLINED, m.P_LEFT, m.P_REMOVED)


def occupied(view):
    return [pid for pid, player in view["players"].items() if player["status"] not in DEPARTED]


class CapacityAfterMechanismSwitchUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, ("host", "amy", "bob", "cat"), **STREAM)
        for node in self.nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        # cat runs an incompatible RetroArch, so a native session cannot include
        # cat: the host's only option for a group containing cat is streaming.
        cat = self.nodes["cat"].orch
        cat.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        cat.adapter_map["retroarch"].refresh()
        self.h = self.nodes["host"].orch

    def native_lobby(self):
        return self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["amy", "bob"],
                                      "max_players": 4, **CORE})

    def test_streaming_lobby_cannot_be_created_over_its_cap(self):
        # Guard for the create path: streaming hosts + one guest only.
        hub, nodes = make_swarm(tempfile.mkdtemp(), ("host", "amy", "bob"), games_for=("host",), **STREAM)
        with self.assertRaises(m.MultiplayerError) as caught:
            nodes["host"].orch.create_session({"system": "snes", "game_id": "g1", "peers": ["amy", "bob"], **CORE})
        self.assertEqual(caught.exception.code, "too_many_players")

    def test_invite_that_forces_streaming_cannot_exceed_streaming_capacity(self):
        session = self.native_lobby()
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        try:
            view = self.h.invite_peer(session["id"], "cat")
        except m.MultiplayerError:
            view = self.h.get_session(session["id"])  # refusing the invite is a valid outcome
        self.assertLessEqual(
            len(occupied(view)), view["max_players"],
            f"{view['mechanism']} lobby allows {view['max_players']} players but holds {sorted(occupied(view))}")

    def test_invitees_cannot_all_join_a_lobby_that_shrank_below_their_number(self):
        session = self.native_lobby()
        try:
            self.h.invite_peer(session["id"], "cat")
        except m.MultiplayerError:
            pass
        for name in ("amy", "bob", "cat"):
            try:
                self.nodes[name].orch.accept_invitation(session["id"])
            except m.MultiplayerError:
                pass  # a stale or refused invitation is acceptable; overfilling is not
        view = self.h.get_session(session["id"])
        joined = [pid for pid, player in view["players"].items() if player["status"] == m.P_JOINED]
        self.assertLessEqual(
            len(joined), view["max_players"],
            f"{len(joined)} players joined a {view['mechanism']} lobby capped at {view['max_players']}")

    def test_slots_never_exceed_the_published_capacity(self):
        session = self.native_lobby()
        try:
            self.h.invite_peer(session["id"], "cat")
        except m.MultiplayerError:
            pass
        for name in ("amy", "bob"):
            try:
                self.nodes[name].orch.accept_invitation(session["id"])
            except m.MultiplayerError:
                pass
        view = self.h.get_session(session["id"])
        for pid, player in view["players"].items():
            if player["status"] not in DEPARTED and player.get("slot"):
                self.assertLessEqual(player["slot"], view["max_players"], f"{pid} holds slot {player['slot']}")


class ConfigurationFidelityUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = make_settings(temporary.name)
        self.guard = ConfigGuard(self.settings)
        self.conf = self.root / "userdata" / "system" / "batocera.conf"
        self.edits = [{"key": "snes.core", "value": "snes9x"}, {"key": "snes.netplay.mode", "value": "host"}]

    def round_trip(self, original: str):
        self.conf.write_bytes(original.encode("utf-8"))
        session = m.new_session_id()
        self.guard.apply(session, self.conf, "kv", self.edits)
        applied = self.conf.read_bytes().decode("utf-8")
        self.guard.restore(session)
        return applied, self.conf.read_bytes().decode("utf-8")

    def assert_lossless(self, original: str, line: str):
        applied, restored = self.round_trip(original)
        self.assertIn(line, applied, "an unrelated setting was rewritten while the session was applied")
        self.assertEqual(restored, original, "restore changed content the session did not own")

    def test_rom_key_with_unicode_line_separator_survives(self):
        line = 'snes["Café Kart.sfc"].core=bsnes\n'
        self.assert_lossless(line + "system.language=en_US\n", line)

    def test_hostname_with_next_line_character_survives(self):
        line = "system.hostname=BA\x85TOCERA\n"
        self.assert_lossless(line + "snes.core=bsnes\n", line)

    def test_value_with_form_feed_survives(self):
        line = "global.note=a\x0cb\n"
        self.assert_lossless("# user\n" + line, line)

    def test_ini_value_with_line_separator_survives(self):
        ini = self.root / "userdata" / "system" / "ppsspp.ini"
        original = ("[General]\nRecent0 = /roms/Café Quest.iso\n"
                    "[Network]\nEnableWlan = False\nProAdhocServer = socom.cc\n")
        ini.write_bytes(original.encode("utf-8"))
        session = m.new_session_id()
        self.guard.apply(session, ini, "ini", [{"section": "Network", "key": "EnableWlan", "value": "True"}])
        self.assertIn("Recent0 = /roms/Café Quest.iso\n", ini.read_bytes().decode("utf-8"))
        self.guard.restore(session)
        self.assertEqual(ini.read_bytes().decode("utf-8"), original)

    def test_crlf_files_keep_their_line_endings(self):
        original = "system.language=en_US\r\nsnes.core=bsnes\r\n"
        _applied, restored = self.round_trip(original)
        self.assertEqual(restored, original)

    def test_file_without_trailing_newline_is_restored_exactly(self):
        original = "system.language=en_US\nsnes.core=bsnes"
        _applied, restored = self.round_trip(original)
        self.assertEqual(restored, original)


if __name__ == "__main__":
    unittest.main()
