"""#240 §§2.3, 3.1, 11, 17: stale launch reports, reconnect, and remaining-player health.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- A launch offer is consent to join the host's current start attempt. After the
  launch deadline that player is failed. A later launch_result, even if the
  same ROM is already in the foreground, is a stale operation. Revival requires
  a host retry that delivers a fresh offer. This is distinct from confirming
  the original offer after the deadline.
- The launch deadline exists so a briefly unreachable guest can still join.
  Missing one /launch POST must not fail that player immediately, must not end
  the host's already-started game, and must not leave the returning guest with
  nothing to confirm.
- Bounded retries recover a returning player. Retrying while that player is
  still unreachable must not spend the budget that would recover them once they
  are reachable again.
- Remove player and Leave session are explicit roster changes. Remaining
  connected players who are still running the session game are a healthy
  session, not a permanently degraded one.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch gameplay.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "tests"):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from app.multiplayer import models as m
from app.multiplayer.lifecycle import LAUNCH_DEADLINE, MAX_RETRIES
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}


class Clock:
    def __init__(self):
        self.value = datetime(2032, 5, 10, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, delta):
        self.value = (self.value + delta).replace(microsecond=0)


class StaleResultReconnectUAT(unittest.TestCase):
    names = ("host", "guest", "mallory")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        self.clock = Clock()
        for patcher in (
            mock.patch.object(m, "now", side_effect=self.clock.now),
            mock.patch("app.multiplayer.core.socket.gethostname", return_value="test-drone"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub, self.nodes = make_swarm(self.tmp, self.names, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def lobby(self, peers=("guest", "mallory")):
        sid = self.h.create_session({
            "system": "snes", "game_id": "g1", "peers": list(peers),
            "max_players": 4, **CORE,
        })["id"]
        for name in peers:
            self.nodes[name].orch.accept_invitation(sid)
            self.nodes[name].orch.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def start_with_mallory_running(self, sid, *, guest_offline_before_start=False):
        if guest_offline_before_start:
            self.hub.offline.add("guest")
        self.h.start_session(sid)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)
        self.nodes["mallory"].orch.confirm_launch(sid)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)

    def fail_guest_for_deadline(self, sid):
        self.hub.offline.add("guest")
        self.clock.advance(LAUNCH_DEADLINE + timedelta(seconds=1))
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_FAILED)
        self.assertEqual(session["state"], m.DEGRADED)
        self.hub.offline.clear()
        self.g.recover()

    def set_guest_session_game_running(self, sid):
        path = self.g.store.require(sid)["local"]["game_path"]
        self.assertTrue(path)
        self.nodes["guest"].launcher.active = {
            "rom_path": path, "name": "Super Mario Kart",
            "system": "snes", "emulator": "libretro",
        }
        return path

    def test_late_launch_result_after_deadline_does_not_revive_a_failed_guest(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.fail_guest_for_deadline(sid)
        self.set_guest_session_game_running(sid)
        try:
            self.g._guest_message(self.g.store.require(sid), "launch_result", ok=True)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        session = self.h.get_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [],
                         "a deadline failure is not consent to start; the guest must not launch through Drone")
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING,
                            "a stale launch_result must not revive a player the host already failed for the deadline")
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(session["players"]["mallory"]["status"], m.P_RUNNING)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)

    def test_launch_result_without_session_game_after_deadline_stays_failed(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.fail_guest_for_deadline(sid)
        self.assertIsNone(self.nodes["guest"].launcher.active)
        try:
            self.g._guest_message(self.g.store.require(sid), "launch_result", ok=True)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(session["state"], m.DEGRADED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_missed_launch_offer_lets_the_guest_join_before_the_deadline(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid, guest_offline_before_start=True)
        host = self.h.get_session(sid)
        self.assertNotEqual(host["players"]["guest"]["status"], m.P_FAILED,
                            "one missed /launch POST is a disconnect, not a launch failure")
        self.assertNotEqual(host["state"], m.FAILED)
        self.hub.offline.clear()
        self.h.tick()
        err = None
        try:
            self.g.confirm_launch(sid)
        except m.MultiplayerError as error:
            err = error.code
        self.assertIsNone(err, "a guest who returns before the launch deadline must still be able to confirm")
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_sole_unreachable_guest_at_start_does_not_immediately_end_the_host_game(self):
        self.hub, self.nodes = make_swarm(str(Path(self.tmp) / "sole-guest"), ("host", "guest"),
                                          open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.hub.offline.add("guest")
        self.h.start_session(sid)
        host = self.h.get_session(sid)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)
        self.assertEqual(self.nodes["host"].launcher.exited, 0,
                         "a momentarily unreachable guest must not abort the host's already-started game")
        self.assertNotEqual(host["state"], m.FAILED)
        self.assertNotEqual(host["players"]["guest"]["status"], m.P_FAILED)
        self.hub.offline.clear()
        self.h.tick()
        self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_retries_while_unreachable_do_not_spend_the_budget_that_recovers_the_guest(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.fail_guest_for_deadline(sid)
        self.hub.offline.add("guest")
        for _ in range(MAX_RETRIES):
            self.h.retry_connection(sid, "guest")
            self.assertNotEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.hub.offline.clear()
        self.g.recover()
        try:
            self.h.retry_connection(sid, "guest")
        except m.MultiplayerError as error:
            self.fail("retries spent while the guest was unreachable must not block recovery once they return: %s"
                      % error.code)
        self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)

    def test_host_retry_while_the_guest_is_reachable_still_recovers(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.fail_guest_for_deadline(sid)
        self.h.retry_connection(sid, "guest")
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_removing_a_player_does_not_leave_remaining_players_degraded(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.h.remove_player(sid, "guest")
        self.h.tick()
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_REMOVED)
        self.assertEqual(session["players"]["mallory"]["status"], m.P_RUNNING)
        self.assertEqual(session["state"], m.RUNNING,
                         "an intentional remove leaves the remaining running players as a healthy session")
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)

    def test_a_guest_leaving_does_not_leave_remaining_players_degraded(self):
        sid = self.lobby()
        self.start_with_mallory_running(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.g.leave_session(sid)
        self.h.tick()
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_LEFT)
        self.assertEqual(session["players"]["mallory"]["status"], m.P_RUNNING)
        self.assertEqual(session["state"], m.RUNNING,
                         "a leave leaves the remaining running players as a healthy session")
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)


if __name__ == "__main__":
    unittest.main()
