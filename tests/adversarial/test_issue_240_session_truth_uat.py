"""Issue #240 sections 2.2, 2.4, 3, 7, 8, 11 and 17: session truth boundaries.

Expected from the issue, before treating the current code as correct:

- An invitation's own lifetime is enforceable by the host. After that lifetime
  a peer cannot join, and housekeeping must not leave them invited.
- A launch-success report is untrusted until the reporting machine is actually
  running that session's game. Another title, or no title, is not success.
- Reconnect recovery has the same rule: answering the coordination API while a
  different game is in the foreground is not a recovered multiplayer session.
- When the session game exits and some other game is running, this machine
  leaves the session and does not close the other game.
- A saved mechanism preference must not automatically select a mechanism whose
  game capability is UNKNOWN. That remains an explicit advanced (forced) choice.
  Preference still applies to a mechanism whose capability is known.

These tests use the simulated swarm and fixed clock. They do not start
emulators, open sockets, or claim real Batocera connectivity.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.mechanism import choose
from multiplayer_support import make_swarm


def _candidate(mechanism, classification, *, usable=True):
    return {
        "mechanism": mechanism,
        "label": m.MECHANISM_LABELS[mechanism],
        "usable": usable,
        "classification": classification,
        "blockers": [] if usable else ["blocked"],
        "warnings": [],
        "assessment": {},
        "adapter": "x",
        "adapter_name": "X",
        "automation": "manual",
        "max_players": 2,
    }


class SessionTruthUAT(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2031, 6, 4, 15, 0, tzinfo=timezone.utc)
        clock = mock.patch.object(m, "now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def create(self):
        return self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                      "emulator_override": {"core": "snes9x"}})["id"]

    def started(self):
        sid = self.create()
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)
        return sid

    def running(self):
        sid = self.started()
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        return sid

    def disconnect(self, sid):
        self.hub.offline.add("guest")
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.DEGRADED)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_UNREACHABLE)
        self.hub.offline.clear()

    def msg(self, sid, **extra):
        body = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": sid}
        body.update(extra)
        return body

    def replace_guest_game(self):
        active = dict(self.nodes["guest"].launcher.active or {})
        active["rom_path"] = str(active.get("rom_path") or "/userdata/roms/snes/session.sfc") + ".other-title"
        active["name"] = "Some Other Game"
        active["pid"] = 9090
        active.setdefault("system", "snes")
        active.setdefault("emulator", "libretro")
        self.nodes["guest"].launcher.active = active
        return active["rom_path"]

    def test_host_rejects_accept_after_the_invitation_lifetime(self):
        sid = self.create()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_INVITED)
        ttl = self.h.cfg.get()["general"]["invitation_ttl_minutes"]
        self.clock += timedelta(minutes=ttl + 1)
        with self.assertRaises(m.MultiplayerError):
            self.h.peer_respond("guest", self.msg(sid, action="accept", controllers=1))
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.h.tick()
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_INVITED,
                            "An invitation past its lifetime must leave the host lobby")
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertNotIn(self.h.get_session(sid)["state"], (m.STARTING, m.RUNNING, m.COMPLETED))

    def test_launch_report_without_the_session_game_does_not_complete(self):
        sid = self.started()
        self.nodes["guest"].launcher.active = None
        self.h.peer_respond("guest", self.msg(sid, action="launch_result", ok=True))
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING)
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_launch_report_for_a_different_game_does_not_complete(self):
        sid = self.started()
        other = self.replace_guest_game()
        self.h.peer_respond("guest", self.msg(sid, action="launch_result", ok=True))
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING,
                            "A different foreground game is not this session's launch")
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], other)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)

    def test_confirmed_launch_of_the_session_game_still_runs(self):
        sid = self.running()
        self.assertEqual(self.g.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_other_foreground_game_is_not_a_recovered_participant(self):
        sid = self.running()
        self.disconnect(sid)
        other = self.replace_guest_game()
        status = self.g.peer_status("host", sid)
        self.assertTrue(status["session"]["known"])
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING,
                            "Reachability plus some other running game is not session participation")
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], other)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)

    def test_same_session_game_still_recovers_without_relaunch(self):
        sid = self.running()
        self.disconnect(sid)
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_replaced_session_game_makes_the_guest_leave_without_killing_it(self):
        sid = self.running()
        other = self.replace_guest_game()
        self.g.tick()
        self.g.tick()
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertTrue((self.g.store.get(sid).get("local") or {}).get("ended"))
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], other)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)


class UnknownMechanismPreferenceTests(unittest.TestCase):
    def test_preference_does_not_auto_select_unknown_capability(self):
        candidates = [
            _candidate(m.EMULATED_LAN, m.UNKNOWN),
            _candidate(m.REMOTE_STREAMING, m.LIKELY_COMPATIBLE),
        ]
        decision = choose(candidates, preferred=m.EMULATED_LAN)
        self.assertEqual(decision["mechanism"], m.REMOTE_STREAMING)
        self.assertNotEqual(decision["mechanism"], m.EMULATED_LAN)
        forced = choose(candidates, forced=m.EMULATED_LAN)
        self.assertEqual(forced["mechanism"], m.EMULATED_LAN)
        self.assertTrue(forced["forced"])

    def test_preference_still_selects_a_known_usable_mechanism(self):
        candidates = [
            _candidate(m.RETROARCH_NETPLAY, m.LIKELY_COMPATIBLE),
            _candidate(m.REMOTE_STREAMING, m.LIKELY_COMPATIBLE),
        ]
        decision = choose(candidates, preferred=m.REMOTE_STREAMING)
        self.assertEqual(decision["mechanism"], m.REMOTE_STREAMING)

    def test_unknown_only_stays_unselected_until_advanced(self):
        candidates = [_candidate(m.EMULATED_LAN, m.UNKNOWN)]
        self.assertEqual(choose(candidates, preferred=m.EMULATED_LAN)["mechanism"], "")
        self.assertEqual(choose(candidates, forced=m.EMULATED_LAN)["mechanism"], m.EMULATED_LAN)


if __name__ == "__main__":
    unittest.main()
