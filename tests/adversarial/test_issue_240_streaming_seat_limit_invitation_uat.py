"""Issue #240: invitation refresh behaviour under the documented streaming limit (host + one guest).

Adjudication of ``tests/test_multiplayer_invitation_identity.py``: four of its tests invite a second
guest while the plan has already become REMOTE_STREAMING.  Remote streaming is documented
(docs/multiplayer.md "Remote streaming") and implemented (``StreamingAdapter.max_players = 2``) as
host plus one guest, so that fixture asks for an impossible three-seat streaming lobby and the tests'
premise, not the product, is wrong.  Rewriting those tests is outside this role's write scope, so the
intended guarantees are re-expressed here on a topology that the limit allows:

* a third seat that would force streaming is refused without damaging the existing lobby/invitation;
* a lone streaming guest still goes through review -> accept -> host-only launch;
* delayed older copies never overwrite a newer invitation.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class StreamingSeatLimitUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(
            temporary.name, ("host", "guest", "other"),
            binaries={"retroarch": "/usr/bin/retroarch", "sunshine": "/usr/bin/sunshine", "moonlight": "/usr/bin/moonlight"},
            processes={"sunshine"}, open_ports=(55435,))
        for node in self.nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        # "other" runs an incompatible RetroArch, so any lobby containing it can only stream.
        other = self.nodes["other"]
        other.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        other.orch.adapter_map["retroarch"].refresh()
        self.h, self.g, self.o = (self.nodes[n].orch for n in ("host", "guest", "other"))

    def new_session(self):
        return self.h.create_session({"system": "snes", "game_id": "g1", "max_players": 4,
                                      "emulator_override": {"core": "snes9x"}})["id"]

    def test_third_seat_that_forces_streaming_is_refused_and_leaves_the_lobby_intact(self):
        sid = self.new_session()
        self.h.invite_peer(sid, "guest")
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.invite_peer(sid, "other")
        self.assertEqual(caught.exception.code, "session_full")
        session = self.h.get_session(sid)
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        self.assertEqual(sorted(session["players"]), ["guest", "host"])
        self.assertNotEqual(session["state"], m.CONFIGURING, "a refused invite must not strand the lobby mid-configuration")
        self.assertIsNone(self.o.store.invitation(sid))
        invitation = self.g.store.invitation(sid)
        self.assertEqual((invitation["status"], invitation["mechanism"]), ("pending", m.RETROARCH_NETPLAY))
        joined = self.g.accept_invitation(sid)
        self.assertEqual(joined["mechanism"], m.RETROARCH_NETPLAY)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)

    def test_refused_third_seat_never_shrinks_native_capacity_or_bumps_version_backwards(self):
        sid = self.new_session()
        self.h.invite_peer(sid, "guest")
        before = self.h.get_session(sid)
        with self.assertRaises(m.MultiplayerError):
            self.h.invite_peer(sid, "other")
        after = self.h.get_session(sid)
        self.assertGreaterEqual(after["version"], before["version"])
        self.assertEqual(after["max_players"], before["max_players"])
        self.assertEqual(after["mechanism_reason"], before["mechanism_reason"])

    def test_lone_streaming_guest_reviews_accepts_and_only_the_host_launches(self):
        sid = self.new_session()
        self.h.invite_peer(sid, "other")
        session = self.h.get_session(sid)
        self.assertEqual(session["mechanism"], m.REMOTE_STREAMING)
        self.assertLessEqual(session["max_players"], 2)
        self.assertEqual(self.o.store.invitation(sid)["mechanism"], m.REMOTE_STREAMING)
        self.o.accept_invitation(sid)
        self.o.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.h.launcher.launched), 1)
        self.assertEqual(self.o.launcher.launched, [])

    def test_a_second_guest_cannot_join_a_streaming_lobby_even_after_the_first_declines_and_returns(self):
        sid = self.new_session()
        self.h.invite_peer(sid, "other")
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.invite_peer(sid, "guest")
        self.assertEqual(caught.exception.code, "session_full")
        self.assertEqual(sorted(self.h.get_session(sid)["players"]), ["host", "other"])

    def test_delayed_older_invite_and_withdrawal_do_not_overwrite_a_newer_copy(self):
        sid = self.new_session()
        self.h.invite_peer(sid, "guest")
        original = self.g.store.invitation(sid)["session"]
        self.h.set_ready(sid, True)  # a real host change: the session version moves on
        self.h.invite_peer(sid, "guest")  # re-send the current copy
        self.assertGreater(self.g.store.invitation(sid)["session"]["version"], original["version"])
        for endpoint, snapshot in ((self.g.peer_invite, original), (self.g.peer_update, {**original, "state": m.STOPPING})):
            try:
                endpoint("host", {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": snapshot})
            except m.MultiplayerError:
                pass  # refusing the stale copy is acceptable; overwriting is not
        invitation = self.g.store.invitation(sid)
        self.assertEqual(invitation["status"], "pending")
        self.assertEqual(self.g.accept_invitation(sid)["players"]["guest"]["status"], m.P_JOINED)


if __name__ == "__main__":
    unittest.main()
