"""Authorization revocation between confirmation and execution, using local fakes only."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class ExecutionRevocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue-240-execution-security-")
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        self.host = self.nodes["host"].orch
        self.guest = self.nodes["guest"].orch
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

    def queue_automatic_execution(self):
        self.guest.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.host.start_session(self.sid)
        self.assertEqual(len(self.queued), 1)
        callback, name = self.queued.pop(0)
        self.assertIn("auto-launch", name)
        callback()  # auto-confirm finishes; actual execution is still queued
        self.assertEqual(len(self.queued), 1)
        self.assertIn("multiplayer-launch-", self.queued[0][1])
        self.assertEqual(self.guest.launcher.launched, [])

    def execute_one(self):
        callback, _name = self.queued.pop(0)
        callback()

    def assert_no_execution(self):
        # Subtests expose both effects even when the first security invariant fails.
        with self.subTest(effect="launch"):
            self.assertEqual(self.guest.launcher.launched, [],
                             "Revoked authorization must block queued emulator execution")
        with self.subTest(effect="configuration"):
            self.assertEqual(self.nodes["guest"].conf_text, self.original_config,
                             "Revoked authorization must block temporary configuration writes")
        with self.subTest(effect="journal"):
            self.assertEqual(self.guest.guard.pending(), {})

    def test_confirmation_policy_revokes_already_auto_confirmed_execution(self):
        self.queue_automatic_execution()
        self.guest.update_settings({"privacy": {"local_launch_authorization": "confirm"}})
        self.execute_one()
        self.assert_no_execution()

    def test_disabling_multiplayer_revokes_already_auto_confirmed_execution(self):
        self.queue_automatic_execution()
        self.guest.update_settings({"general": {"enabled": False}})
        self.execute_one()
        self.assert_no_execution()

    def test_disabling_multiplayer_revokes_queued_explicit_execution(self):
        self.host.start_session(self.sid)
        self.guest.confirm_launch(self.sid)
        self.assertEqual(len(self.queued), 1)
        self.guest.update_settings({"general": {"enabled": False}})
        self.execute_one()
        self.assert_no_execution()

    def test_explicit_confirmation_under_confirm_policy_remains_authorized(self):
        self.host.start_session(self.sid)
        self.guest.confirm_launch(self.sid)
        self.execute_one()
        self.assertEqual(len(self.guest.launcher.launched), 1)
        self.assertEqual(self.host.get_session(self.sid)["state"], m.RUNNING)

    def test_unrevoked_automatic_execution_remains_authorized(self):
        self.queue_automatic_execution()
        self.execute_one()
        self.assertEqual(len(self.guest.launcher.launched), 1)
        self.assertEqual(self.host.get_session(self.sid)["state"], m.RUNNING)


if __name__ == "__main__":
    unittest.main()
