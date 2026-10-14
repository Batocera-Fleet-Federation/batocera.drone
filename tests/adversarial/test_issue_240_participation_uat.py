"""Issue #240 sections 2.4, 3.1, 17: participation survives real failure boundaries.

Expected before examining implementation: ending a host session withdraws its
pending invitations; explicitly renewing an expired invitation makes it reviewable
again; an online process with no session/game is not a reconnected running player.
These are simulated emulator/network tests using real orchestration and SQLite.
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
from multiplayer_support import make_swarm


class ParticipationUAT(unittest.TestCase):
    def setUp(self):
        clock = mock.patch.object(m, "now", return_value=datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc))
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

    def running(self):
        sid = self.create()
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        return sid

    def disconnect(self, sid):
        self.hub.offline.add("guest")
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.DEGRADED)
        self.hub.offline.clear()

    def test_ending_host_withdraws_delivered_pending_invitation(self):
        sid = self.create()
        self.assertEqual(len(self.g.list_invitations()), 1)
        self.h.end_session(sid)
        # Exercise normal guest housekeeping as well as the immediate host push.
        self.g.tick()
        self.assertEqual(self.g.list_invitations(), [],
                         "A reachable host ended this session: Join Game must not still offer its invitation")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_explicit_renewal_of_expired_invitation_can_be_accepted(self):
        sid = self.create()
        self.g.store.update_invitation(sid, expires_at=(m.now() - timedelta(seconds=1)).isoformat())
        self.g.tick()
        self.assertEqual(self.g.list_invitations(), [])
        # The original session remains open; this is a new host action, not a
        # replay and not a request to reverse a user's explicit decline.
        self.h.invite_peer(sid, "guest")
        pending = self.g.list_invitations()
        self.assertEqual([item["session_id"] for item in pending], [sid],
                         "Renewal reported as delivered must yield a fresh reviewable invitation")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.g.accept_invitation(sid)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_online_guest_that_lost_replica_is_not_recovered_as_running(self):
        sid = self.running()
        self.disconnect(sid)
        # A restored/cleared guest DB can answer mTLS but no longer knows the
        # session; its emulator also exited. No network response is mocked here.
        self.g.store.delete(sid)
        self.nodes["guest"].launcher.active = None
        status = self.g.peer_status("host", sid)
        self.assertIsNone(status["session"])
        self.assertFalse(status["game_running"])
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING,
                            "Drone reachability alone cannot establish session participation")
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)

    def test_online_guest_whose_game_exited_is_not_recovered_as_running(self):
        sid = self.running()
        self.disconnect(sid)
        self.nodes["guest"].launcher.active = None
        status = self.g.peer_status("host", sid)
        self.assertTrue(status["session"]["known"])
        self.assertFalse(status["game_running"])
        self.h.tick()
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING,
                            "A known replica without its native emulator is not a recovered running player")

    def test_reachable_guest_with_live_game_does_recover_without_relaunch(self):
        sid = self.running()
        self.disconnect(sid)
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)


if __name__ == "__main__":
    unittest.main()
