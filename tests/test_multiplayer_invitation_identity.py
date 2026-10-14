"""Invitation refresh and acceptance races over the simulated pinned-peer channel."""
import tempfile
import unittest

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class InvitationIdentityTests(unittest.TestCase):
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
        self.sid = self.h.create_session({"system": "snes", "game_id": "g1", "max_players": 4,
                                         "emulator_override": {"core": "snes9x"}})["id"]
        self.h.invite_peer(self.sid, "guest")
        self.original = self.g.store.invitation(self.sid)["session"]

    def switch_to_streaming(self):
        other = self.nodes["other"]
        other.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        other.orch.adapter_map["retroarch"].refresh()
        self.h.invite_peer(self.sid, "other")
        self.assertEqual(self.h.get_session(self.sid)["mechanism"], m.REMOTE_STREAMING)

    def test_lost_refresh_refuses_stale_accept_without_joining_and_can_retry(self):
        self.hub.offline.add("guest")
        self.switch_to_streaming()
        self.hub.offline.clear()
        with self.assertRaises(m.MultiplayerError) as caught:
            self.g.accept_invitation(self.sid)
        self.assertEqual(caught.exception.code, "session_identity_changed")
        self.assertIsNone(self.g.store.get(self.sid))
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_INVITED)
        self.assertEqual(self.g.store.invitation(self.sid)["status"], "pending")
        self.h.invite_peer(self.sid, "guest")
        self.assertEqual(self.g.accept_invitation(self.sid)["mechanism"], m.REMOTE_STREAMING)
        self.assertEqual(self.g.launcher.launched, [])

    def test_delayed_old_invite_and_withdrawal_do_not_overwrite_refreshed_invitation(self):
        self.switch_to_streaming()
        for endpoint, snapshot in ((self.g.peer_invite, self.original),
                                   (self.g.peer_update, {**self.original, "state": m.STOPPING})):
            endpoint("host", {"message_id": m.new_message_id(), "issued_at": m.now_iso(),
                              "session": snapshot})
        invitation = self.g.store.invitation(self.sid)
        self.assertEqual(invitation["mechanism"], m.REMOTE_STREAMING)
        self.assertEqual(invitation["status"], "pending")
        self.assertEqual(self.g.accept_invitation(self.sid)["mechanism"], m.REMOTE_STREAMING)

    def test_roster_only_version_change_does_not_invalidate_reviewed_identity(self):
        self.h.invite_peer(self.sid, "other")
        self.assertGreater(self.h.get_session(self.sid)["version"], self.original["version"])
        joined = self.g.accept_invitation(self.sid)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(joined["mechanism"], m.RETROARCH_NETPLAY)

    def test_accept_without_reviewed_identity_fails_closed(self):
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.peer_respond("guest", {"message_id": m.new_message_id(), "issued_at": m.now_iso(),
                                         "session_id": self.sid, "action": "accept"})
        self.assertEqual(caught.exception.code, "invitation_review_required")
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_INVITED)

    def test_changed_mechanism_refreshes_host_evidence_before_launch(self):
        self.switch_to_streaming()
        session = self.h.store.require(self.sid)
        game = self.h.library.get("snes", "g1")
        expected = next(a["config_fingerprint"] for a in self.h.assessor.assess(game)
                        if a["mechanism"] == m.REMOTE_STREAMING)
        self.assertEqual(session["local"]["tested_config_fingerprint"], expected)
        self.g.accept_invitation(self.sid)
        self.g.set_ready(self.sid, True)
        self.h.set_ready(self.sid, True)
        self.h.start_session(self.sid)
        self.assertEqual(self.h.get_session(self.sid)["state"], m.RUNNING)
        self.assertEqual(len(self.h.launcher.launched), 1)
        self.assertEqual(self.g.launcher.launched, [])

    def test_return_to_native_recomputes_content_and_reviewed_configuration(self):
        self.switch_to_streaming()
        self.nodes["other"].orch.decline_invitation(self.sid)
        self.g.decline_invitation(self.sid)
        self.h.invite_peer(self.sid, "guest")
        session = self.h.store.require(self.sid)
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        self.assertEqual(len(session["game"]["sha256"]), 64)
        self.g.accept_invitation(self.sid)
        self.g.set_ready(self.sid, True)
        self.h.set_ready(self.sid, True)
        self.h.start_session(self.sid)
        self.g.confirm_launch(self.sid)
        self.assertEqual(self.h.get_session(self.sid)["state"], m.RUNNING)
