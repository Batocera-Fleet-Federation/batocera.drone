"""Local-only attacks against delayed automatic launch authorization.

The real protocol and configuration guard run on the simulated swarm. Holding
spawn callbacks models daemon scheduling without clocks, sockets or emulators.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class AutoLaunchRevocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue-240-auto-security-")
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        self.host = self.nodes["host"].orch
        self.guest = self.nodes["guest"].orch
        self.guest.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.sid = self.host.create_session({
            "system": "snes", "game_id": "g1", "peers": ["guest"],
            "emulator_override": {"core": "snes9x"},
        })["id"]
        self.guest.accept_invitation(self.sid)
        self.guest.set_ready(self.sid, True)
        self.host.set_ready(self.sid, True)
        self.queued = []
        self.guest._spawn = lambda fn, name: self.queued.append((fn, name))
        self.original_config = self.nodes["guest"].conf_text
        self.host.start_session(self.sid)
        self.assertEqual(len(self.queued), 1)
        self.assertIn("auto-launch", self.queued[0][1])
        self.assertEqual(self.guest.launcher.launched, [])

    def drain_workers(self):
        # Auto-confirm may enqueue a second launch worker. Bound the drain so
        # an implementation regression cannot cause uncontrolled execution.
        for _ in range(10):
            if not self.queued:
                return
            callback, _name = self.queued.pop(0)
            callback()
        self.fail("Unexpected unbounded worker chain")

    def assert_no_execution(self):
        self.assertEqual(self.guest.launcher.launched, [],
                         "Queued automatic consent must respect the current local launch policy")
        self.assertEqual(self.nodes["guest"].conf_text, self.original_config)
        self.assertEqual(self.guest.guard.pending(), {})
        self.assertTrue(self.guest.get_session(self.sid)["pending_launch"],
                        "The offer must remain available for explicit local confirmation")

    def test_switching_to_confirmation_revokes_queued_automatic_consent(self):
        self.guest.update_settings({"privacy": {"local_launch_authorization": "confirm"}})
        self.drain_workers()
        self.assert_no_execution()

    def test_host_replacement_after_revocation_cannot_borrow_old_auto_worker(self):
        self.guest.update_settings({"privacy": {"local_launch_authorization": "confirm"}})
        host_session = self.host.store.require(self.sid)
        response = self.guest.peer_launch("host", {
            "message_id": m.new_message_id(), "issued_at": m.now_iso(),
            "session_id": self.sid, "port": host_session["local"]["port"],
            "secret": "attackerreplacement123", "wave": host_session["local"]["launch_wave"],
        })
        self.assertEqual(response["status"], "awaiting_confirmation")
        self.assertEqual(len(self.queued), 1, "The replacement itself cannot schedule auto consent")
        self.drain_workers()
        self.assert_no_execution()

    def test_current_automatic_policy_still_launches(self):
        self.drain_workers()
        self.assertEqual(len(self.guest.launcher.launched), 1)
        self.assertEqual(self.host.get_session(self.sid)["state"], m.RUNNING)


if __name__ == "__main__":
    unittest.main()
