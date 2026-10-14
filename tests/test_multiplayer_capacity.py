"""Admission and restart safeguards for mechanism-specific capacity (simulated swarm)."""
import tempfile
import unittest

from app.multiplayer import models as m
from app.multiplayer.sessions import next_free_slot
from multiplayer_support import make_swarm


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, ("host", "guest", "other"),
            binaries={"retroarch": "/usr/bin/retroarch", "sunshine": "/usr/bin/sunshine",
                      "moonlight": "/usr/bin/moonlight"}, processes={"sunshine"}, open_ports=(55435,))
        for node in self.nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        self.h, self.g = self.nodes["host"].orch, self.nodes["guest"].orch
        self.sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
            "max_players": 4, "emulator_override": {"core": "snes9x"}})["id"]

    def test_refused_switch_preserves_invitation_and_existing_roster(self):
        other = self.nodes["other"].orch
        other.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        other.adapter_map["retroarch"].refresh()
        before = self.h.store.require(self.sid)
        invitation = self.g.store.invitation(self.sid)
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.invite_peer(self.sid, "other")
        self.assertEqual(caught.exception.code, "session_full")
        after = self.h.store.require(self.sid)
        for field in ("mechanism", "emulator", "max_players", "players", "state"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(self.g.store.invitation(self.sid), invitation)
        self.assertIsNone(other.store.invitation(self.sid))
        self.g.accept_invitation(self.sid)
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["slot"], 2)

    def test_accept_refuses_legacy_overfilled_roster_without_joining(self):
        self.h.invite_peer(self.sid, "other")
        self.h.store.mutate(self.sid, lambda s: s.update(max_players=2))
        self.h.invite_peer(self.sid, "guest")  # refresh reviewed capacity
        with self.assertRaises(m.MultiplayerError) as caught:
            self.g.accept_invitation(self.sid)
        self.assertEqual(caught.exception.code, "session_full")
        self.assertIsNone(self.g.store.get(self.sid))
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_INVITED)

    def test_start_rejects_legacy_slot_outside_capacity(self):
        self.g.accept_invitation(self.sid)
        self.g.set_ready(self.sid, True)
        self.h.set_ready(self.sid, True)
        self.h.store.mutate(self.sid, lambda s: s["players"]["guest"].update(slot=5))
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.start_session(self.sid)
        self.assertEqual(caught.exception.code, "session_full")
        self.assertEqual(self.h.launcher.launched, [])
        self.assertEqual(self.h.guard.pending(), {})

    def test_no_slot_is_allocated_above_capacity(self):
        self.g.accept_invitation(self.sid)
        session = self.h.store.require(self.sid)
        session["max_players"] = 2
        with self.assertRaises(m.MultiplayerError) as caught:
            next_free_slot(session)
        self.assertEqual(caught.exception.code, "session_full")
