"""Cleanup fault isolation and bounded recovery on a simulated swarm."""
from datetime import timedelta
from unittest import mock

from app.multiplayer import models as m
from app.multiplayer.config_guard import ConfigGuard, RESTORE_RETRIES
from test_multiplayer_orchestrator import SwarmCase


class CleanupRecoveryTests(SwarmCase):
    def test_all_cleanup_steps_run_when_other_resources_fail(self):
        before = self.nodes["host"].conf_text
        sid = self.running()
        adapter = self.h.adapter_map["retroarch"]
        with mock.patch.object(adapter, "stop_session", side_effect=OSError("stop unavailable")), \
                mock.patch.object(adapter, "cleanup", side_effect=OSError("adapter cleanup unavailable")), \
                mock.patch.object(self.h.network.relay, "close_session", side_effect=OSError("relay unavailable")), \
                mock.patch.object(self.h.launcher, "exit_game", side_effect=OSError("shutdown unavailable")):
            view = self.h.end_session(sid)
        self.assertEqual(view["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].conf_text, before)
        self.assertEqual(self.h.guard.pending(), {})
        self.assertIn("shutdown unavailable", repr(view["events"]))
        self.assertIn("relay unavailable", repr(view["events"]))
        self.assertTrue(self.g.get_session(sid)["local"]["ended"])

    def test_queued_host_start_rechecks_adapter_setting(self):
        sid = self.lobby()
        queued = []
        self.h._spawn = lambda fn, name: queued.append(fn)
        self.h.start_session(sid)
        self.h.update_settings({"emulators": {"disabled_adapters": ["retroarch"]}})
        for fn in queued:
            fn()
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.h.guard.pending(), {})
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)

    def test_recovery_budget_survives_new_config_guard_and_manual_restore_still_works(self):
        sid = self.running()
        with mock.patch.object(self.h.guard, "_atomic_write", side_effect=OSError("storage unavailable")):
            self.h.end_session(sid)
        clock = m.now()
        with mock.patch.object(m, "now", side_effect=lambda: clock):
            for _ in range(RESTORE_RETRIES):
                with mock.patch.object(self.h.guard, "_atomic_write", side_effect=OSError("storage unavailable")):
                    self.h.tick()
                clock += timedelta(minutes=5)
            self.assertEqual(self.h.guard.pending(sid)[sid]["restore_attempts"], RESTORE_RETRIES)
            self.h.guard = ConfigGuard(self.h.settings)
            self.h.tick()
            self.assertIn(sid, self.h.guard.pending(), "automatic budget must survive a new guard")
            self.assertEqual(self.h.restore_configuration(sid)["errors"], [])
        self.assertEqual(self.h.guard.pending(), {})

    def test_recovery_waits_for_backoff(self):
        sid = self.running()
        with mock.patch.object(self.h.guard, "_atomic_write", side_effect=OSError("storage unavailable")):
            self.h.end_session(sid)
            self.h.tick()
        self.h.tick()
        self.assertIn(sid, self.h.guard.pending())
        self.assertEqual(self.h.guard.pending(sid)[sid]["restore_attempts"], 1)

    def test_housekeeping_does_not_restore_live_or_opted_out_configuration(self):
        sid = self.running()
        self.h.tick()
        self.assertIn(sid, self.h.guard.pending())
        self.h.update_settings({"general": {"auto_restore_configuration": False}})
        self.h.end_session(sid)
        self.h.tick()
        self.assertIn(sid, self.h.guard.pending())
        self.assertNotIn("restore_attempts", self.h.guard.pending(sid)[sid])

    def test_disabled_multiplayer_still_restores_abandoned_configuration(self):
        before = self.nodes["host"].conf_text
        sid = self.running()
        with mock.patch.object(self.h.guard, "_atomic_write", side_effect=OSError("storage unavailable")):
            self.h.end_session(sid)
        self.h.update_settings({"general": {"enabled": False}})
        self.h.tick()
        self.assertEqual(self.nodes["host"].conf_text, before)
        self.assertEqual(self.h.guard.pending(), {})


class LaunchEvidenceRecoveryTests(SwarmCase):
    def change_settings(self, name):
        node = self.nodes[name]
        text = node.conf_text + "snes.rewind=1\n"
        node.settings.batocera_conf_file.write_text(text, encoding="utf-8")
        node.probe.files[str(node.settings.batocera_conf_file)] = text
        # Leave the detection memo intact: launch must explicitly reinspect.

    def test_host_rejects_changed_settings_before_start(self):
        sid = self.lobby()
        self.change_settings("host")
        self.assertEqual(self.code(self.h.start_session, sid), "configuration_changed")
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.h.guard.pending(), {})

    def test_queued_worker_rejects_changed_settings(self):
        sid = self.lobby()
        queued = []
        self.h._spawn = lambda fn, name: queued.append(fn)
        self.h.start_session(sid)
        self.change_settings("host")
        for launch in list(queued):
            launch()
        self.assertEqual(self.h.store.require(sid)["state"], m.FAILED)
        self.assertIn("settings changed", self.h.store.require(sid)["error"])
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.h.guard.pending(), {})

    def test_guest_rejects_changed_settings_before_applying_edits(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.change_settings("guest")
        self.g.confirm_launch(sid)
        local = self.g.store.require(sid)["local"]
        self.assertEqual(local["launch_status"], "failed")
        self.assertIn("settings changed", local["launch_error"])
        self.assertIsNone(local["pending_launch"])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})

    def test_failed_guest_launch_preserves_error_and_recovers_journal(self):
        import sqlite3
        before = self.nodes["guest"].conf_text
        sid = self.lobby()
        self.h.start_session(sid)
        self.nodes["guest"].launcher.fail_launch = "EmulationStation unavailable"
        with mock.patch.object(self.g.guard, "restore", side_effect=sqlite3.OperationalError("database busy")):
            self.g.confirm_launch(sid)
        local = self.g.store.require(sid)["local"]
        self.assertEqual(local["launch_status"], "failed")
        self.assertIn("EmulationStation unavailable", local["launch_error"])
        self.assertIsNone(local["pending_launch"])
        self.assertIn("database busy", str(self.g.diagnostics(sid)["local"]["cleanup_errors"]))
        self.assertIn(sid, self.g.guard.pending())
        self.g.tick()
        self.assertEqual(self.nodes["guest"].conf_text, before)
        self.assertEqual(self.g.guard.pending(), {})

    def test_restart_restores_guest_settings_despite_shutdown_failure(self):
        before = self.nodes["guest"].conf_text
        sid = self.running()
        self.g.store.set_local(sid, launch_status="launching")
        with mock.patch.object(self.g.launcher, "exit_game", side_effect=OSError("shutdown unavailable")):
            summary = self.g.recover()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.nodes["guest"].conf_text, before)
        self.assertEqual(self.g.guard.pending(), {})
        local = self.g.store.require(sid)["local"]
        self.assertEqual(local["launch_status"], "failed")
        self.assertIn("shutdown unavailable", str(local["cleanup_errors"]))

    def test_restart_records_guest_failure_when_journal_inventory_is_unavailable(self):
        import sqlite3
        sid = self.running()
        self.g.store.set_local(sid, launch_status="launching")
        with mock.patch.object(self.g.guard, "pending", side_effect=sqlite3.OperationalError("database busy")):
            self.assertEqual(self.g.recover()["failed"], 1)
        self.assertEqual(self.g.store.require(sid)["local"]["launch_status"], "failed")
