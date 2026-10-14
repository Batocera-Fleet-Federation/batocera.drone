"""Execution consent and policy-write ordering on the simulated swarm."""
import threading
from unittest import mock

from app.multiplayer import models as m
from app.multiplayer.settings_store import MultiplayerSettings
from test_multiplayer_orchestrator import SwarmCase


class ExecutionAuthorityTests(SwarmCase):
    def queued_launch(self, automatic=True):
        sid = self.lobby()
        self.g.update_settings({"privacy": {"local_launch_authorization": "auto" if automatic else "confirm"}})
        queued = []
        self.g._spawn = lambda fn, name: queued.append(fn)
        self.h.start_session(sid)
        if automatic:
            queued.pop(0)()  # confirmation and execution are distinct scheduling points
        else:
            self.g.confirm_launch(sid)
        return sid, queued.pop(0)

    def test_auto_revoke_and_restore_does_not_restore_old_consent(self):
        sid, execute = self.queued_launch()
        self.g.update_settings({"privacy": {"local_launch_authorization": "confirm"}})
        self.g.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.g.cfg = MultiplayerSettings(self.g.settings)  # generations survive reconstruction
        execute()
        self.assertEqual(self.g.launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        fresh = []
        self.g._spawn = lambda fn, name: fresh.append(fn)
        self.g.confirm_launch(sid)
        execute()  # a revoked callback cannot use the new explicit token
        self.assertEqual(self.g.launcher.launched, [])
        fresh.pop(0)()
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_disable_and_reenable_revokes_explicit_consent(self):
        sid, execute = self.queued_launch(automatic=False)
        self.g.update_settings({"general": {"enabled": False}})
        self.g.update_settings({"general": {"enabled": True}})
        execute()
        self.assertEqual(self.g.launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertTrue(self.g.get_session(sid)["pending_launch"])

    def test_auto_policy_change_preserves_explicit_consent(self):
        sid, execute = self.queued_launch(automatic=False)
        self.g.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.g.update_settings({"privacy": {"local_launch_authorization": "confirm"}})
        execute()
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_settings_write_serializes_with_resource_acquisition(self):
        sid, execute = self.queued_launch()
        entered, release, updating, updated = (threading.Event() for _ in range(4))
        errors = []
        original = self.g.apply_edits

        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test resource wait timed out")
            return original(*args)

        def revoke():
            updating.set()
            try:
                self.g.update_settings({"general": {"enabled": False}})
            except Exception as error:
                errors.append(error)
            finally:
                updated.set()

        with mock.patch.object(self.g, "apply_edits", side_effect=blocked):
            worker = threading.Thread(target=execute)
            writer = threading.Thread(target=revoke)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                writer.start()
                self.assertTrue(updating.wait(5))
                self.assertFalse(updated.wait(0.05), "policy cannot change between authorization and launch")
            finally:
                release.set()
                worker.join(5)
                if writer.ident is not None:
                    writer.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(updated.is_set())
        self.assertFalse(self.g.cfg.get()["general"]["enabled"])
        self.assertEqual(len(self.g.launcher.launched), 1)  # execution acquired authority before revocation
