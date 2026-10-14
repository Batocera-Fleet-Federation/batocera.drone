"""#240 §§7, 8, 11, 17: launch evidence and stale offers after a failed start.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- Compatibility evidence is for actual play of a tested configuration.
  An infrastructure failure (emulator launcher down, Drone restart while the
  session is still STARTING) is not proof the game is INCOMPATIBLE, and must
  not silently retire native Netplay on the next Host Game.
- A user-confirmed failure after the session really ran may still count
  against that configuration.
- A launch offer is a local confirmation, not a command to start after the
  host has already failed or ended. If the guest was unreachable when the
  host recovered a STARTING session, confirming later must not start a game.
- A still-STARTING host keeps a valid unconfirmed offer across a guest
  restart: the local user may still confirm.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch gameplay.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}


class StaleOfferCompatUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (
            mock.patch.object(m, "now", return_value=datetime(2032, 5, 10, 12, tzinfo=timezone.utc)),
            mock.patch("app.multiplayer.core.socket.gethostname", return_value="test-drone"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h, self.g = self.nodes["host"].orch, self.nodes["guest"].orch

    def lobby(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def assessment(self):
        game = self.h.library.get("snes", "g1")
        return next(a for a in self.h.assessor.assess(game, core_override="snes9x")
                    if a["mechanism"] == m.RETROARCH_NETPLAY)

    def test_infrastructure_launch_failure_does_not_make_the_game_unhostable(self):
        sid = self.lobby()
        self.assertNotEqual(self.assessment()["classification"], m.INCOMPATIBLE)
        self.nodes["host"].launcher.fail_launch = "EmulationStation temporarily unavailable"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertIn("EmulationStation", self.h.get_session(sid)["error"])
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.h.end_session(sid)
        verdict = self.assessment()
        self.assertNotEqual(verdict["classification"], m.INCOMPATIBLE,
                            "a launcher outage is not evidence that this game cannot play Netplay")
        self.assertNotEqual(verdict["compatibility_basis"], "failed-sessions")
        self.nodes["host"].launcher.fail_launch = None
        again = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                       "client_request_id": "after-outage", **CORE})
        self.assertEqual(again["mechanism"], m.RETROARCH_NETPLAY)
        self.assertNotEqual(again["state"], m.FAILED)
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_user_confirmed_play_failure_still_counts_for_that_configuration(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.h.end_session(sid)
        self.h.record_result(sid, False, "desync after both machines launched")
        verdict = self.assessment()
        self.assertEqual(verdict["classification"], m.INCOMPATIBLE)
        self.assertEqual(verdict["compatibility_basis"], "failed-sessions")

    def test_confirm_does_not_launch_after_host_failed_while_guest_was_offline(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.assertEqual(self.g.store.require(sid)["local"].get("launch_status"), "awaiting_confirmation")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.hub.offline.add("guest")
        summary = self.h.recover()
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.hub.offline.clear()
        self.g.recover()
        try:
            self.g.confirm_launch(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        self.assertEqual(self.nodes["guest"].launcher.launched, [],
                         "a stale launch offer must not start a game after the host session has failed")
        self.assertTrue(
            self.nodes["guest"].launcher.active is None
            or self.nodes["guest"].launcher.active.get("rom_path") != self.g.store.require(sid)["local"].get("game_path"),
        )
        self.assertNotEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_unconfirmed_offer_survives_guest_recover_while_host_is_still_starting(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        summary = self.g.recover()
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["ended"], 0)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)


if __name__ == "__main__":
    unittest.main()
