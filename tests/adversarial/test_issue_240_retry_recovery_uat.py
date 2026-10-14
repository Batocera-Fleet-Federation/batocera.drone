"""Issue #240 recovery acceptance, derived from sections 3, 17 and 19.

A retry must recover the participating swarm, not merely change host state.
A reconnect must restore the healthy session state. A failed local launch must
not leave an owned emulator behind. Fakes simulate installed hardware only;
all orchestration, persistence and config restoration are real repository code.
"""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class RetryRecoveryUAT(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub, self.nodes = make_swarm(tmp.name, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        # Make gameplay addresses stable across interpreter hash seeds.
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.before = {name: node.conf_text for name, node in self.nodes.items()}

    def lobby(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                     "emulator_override": {"core": "snes9x"}})["id"]
        self.g.accept_invitation(sid)
        self.ready(sid)
        return sid

    def ready(self, sid):
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)

    def retry_to_running(self, sid):
        self.h.retry_connection(sid)
        self.assertFalse(self.h.get_session(sid)["players"]["guest"]["ready"])
        # Permit implementations that require renewed local acceptance after failure.
        # Neither acceptance nor readiness may launch a game by itself.
        self.g.accept_invitation(sid)
        self.ready(sid)
        self.h.start_session(sid)
        guest = self.g.get_session(sid)
        self.assertFalse(guest["local"]["ended"],
                         "Retry must reopen the consenting guest's lobby, or require and support renewed acceptance")
        self.assertTrue(guest["pending_launch"], "A retried host must deliver a new locally confirmable offer")
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(self.g.get_session(sid)["state"], m.RUNNING)
        self.h.end_session(sid)
        for name, node in self.nodes.items():
            self.assertEqual(node.conf_text, self.before[name])
            self.assertEqual(node.orch.guard.pending(), {})

    def test_retry_after_host_launch_failure_recovers_both_machines(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation temporarily unavailable"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.nodes["host"].conf_text, self.before["host"])
        self.nodes["host"].launcher.fail_launch = None
        self.retry_to_running(sid)

    def test_retry_after_guest_launch_failure_recovers_both_machines(self):
        sid = self.lobby()
        self.nodes["guest"].launcher.fail_launch = "Temporary guest launch rejection"
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.nodes["guest"].conf_text, self.before["guest"])
        self.nodes["guest"].launcher.fail_launch = None
        self.retry_to_running(sid)

    def test_reconnected_running_guest_returns_the_session_to_running(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.hub.offline.add("guest")
        self.h.tick()
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.DEGRADED)
        self.hub.offline.clear()
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(session["state"], m.RUNNING,
                         "No failed or unreachable player remains: the recovered session must cease reporting degraded")
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)

    def test_guest_post_launch_error_does_not_leave_an_owned_orphan(self):
        sid = self.lobby()
        self.h.start_session(sid)
        guest_launcher = self.nodes["guest"].launcher
        # ES accepts and starts the game, then process confirmation fails.
        # This is a local failure of Drone's own launch, not a remote end request.
        with mock.patch.object(guest_launcher, "wait_for_start", side_effect=RuntimeError("process monitor failed")):
            self.g.confirm_launch(sid)
        self.assertEqual(len(guest_launcher.launched), 1)
        self.assertIsNone(guest_launcher.active, "Failure after starting an owned game must clean up that process")
        self.assertEqual(self.nodes["guest"].conf_text, self.before["guest"])
        self.assertEqual(self.g.guard.pending(), {})

    def test_failed_retry_limit_prevents_an_unbounded_launch_loop(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "persistent rejection"
        self.h.start_session(sid)
        # Drive host retry failure without requiring a guest launch offer.
        for _ in range(3):
            self.h.retry_connection(sid)
            self.ready(sid)
            self.h.start_session(sid)
            self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        with self.assertRaises(m.MultiplayerError) as caught:
            self.h.retry_connection(sid)
        self.assertEqual(caught.exception.code, "retry_limit")
        self.assertEqual(self.h.guard.pending(), {})


if __name__ == "__main__":
    unittest.main()
