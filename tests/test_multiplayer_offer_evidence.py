"""Fresh launch authorization and diagnostic-only failures on a simulated swarm."""
from datetime import timedelta
from unittest import mock

from app.multiplayer import models as m
from test_multiplayer_orchestrator import SwarmCase


class OfferEvidenceTests(SwarmCase):
    def test_offline_host_blocks_confirmation_without_consuming_offer(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.hub.offline.add("host")
        self.assertEqual(self.code(self.g.confirm_launch, sid), "host_unreachable")
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertEqual(self.g.launcher.launched, [])
        self.hub.offline.clear()
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_queued_guest_rechecks_host_after_lost_terminal_publish(self):
        sid = self.lobby()
        self.h.start_session(sid)
        queued = []
        self.g._spawn = lambda fn, name: queued.append(fn)
        self.g.confirm_launch(sid)
        self.h._spawn = lambda fn, name: None  # terminal snapshot is lost
        self.h.end_session(sid)
        for fn in queued:
            fn()
        self.assertEqual(self.g.launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertIsNone(self.g.store.require(sid)["local"]["pending_launch"])

    def test_removed_membership_blocks_offer_despite_lost_publish(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.h._spawn = lambda fn, name: None
        self.h.remove_player(sid, "guest")
        self.assertEqual(self.code(self.g.confirm_launch, sid), "launch_expired")
        self.assertEqual(self.g.launcher.launched, [])

    def test_orchestration_diagnostic_does_not_erase_confirmed_success(self):
        sid = self.running()
        self.h.end_session(sid)
        self.h.record_result(sid, True)
        fingerprint = self.h.store.require(sid)["local"]["tested_config_fingerprint"]
        sid = self.lobby()
        self.h.launcher.fail_launch = "Launcher unavailable"
        self.h.start_session(sid)
        game = self.h.store.require(sid)["game"]
        title_key = m.normalize_title(game["rom_name"])
        verdict = self.h.compat.evaluate("snes", title_key, m.RETROARCH_NETPLAY, fingerprint, m.LIKELY_COMPATIBLE)
        self.assertEqual(verdict["classification"], m.VERIFIED_COMPATIBLE)
        self.assertEqual(verdict["evidence"]["failures"], 0)
        self.assertEqual(self.h.compat.get("snes", title_key)["knownIssues"][-1]["kind"], "orchestration")


class DeadlineOfferTests(SwarmCase):
    names = ("host", "guest", "other")

    def setUp(self):
        super().setUp()
        for node in self.nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        self.sid = self.create(peers=("guest", "other"), max_players=4)["id"]
        for name in ("guest", "other"):
            orch = self.nodes[name].orch
            orch.accept_invitation(self.sid)
            orch.set_ready(self.sid, True)
        self.h.set_ready(self.sid, True)
        self.h.start_session(self.sid)
        self.nodes["other"].orch.confirm_launch(self.sid)
        self.after_deadline = m.parse_time(self.h.store.require(self.sid)["local"]["launch_deadline"]) + timedelta(seconds=1)

    def test_host_deadline_blocks_confirmation_before_housekeeping(self):
        before = self.nodes["guest"].conf_text
        with mock.patch.object(m, "now", return_value=self.after_deadline):
            self.assertEqual(self.code(self.g.confirm_launch, self.sid), "launch_expired")
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        self.assertEqual(self.g.launcher.launched, [])
        self.assertEqual(self.nodes["guest"].conf_text, before)
        self.assertEqual(self.g.guard.pending(), {})

    def test_queued_launch_cannot_outlive_host_deadline(self):
        queued = []
        self.g._spawn = lambda fn, name: queued.append(fn)
        self.g.confirm_launch(self.sid)
        with mock.patch.object(m, "now", return_value=self.after_deadline):
            queued[0]()
        self.assertEqual(self.g.launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertIsNone(self.g.store.require(self.sid)["local"]["pending_launch"])

    def test_expired_player_can_launch_from_explicit_host_retry(self):
        with mock.patch.object(m, "now", return_value=self.after_deadline):
            self.h.evaluate_launch(self.sid)
            self.assertEqual(self.h.get_session(self.sid)["state"], m.DEGRADED)
            self.assertEqual(self.code(self.g.confirm_launch, self.sid), "launch_expired")
            self.h.retry_connection(self.sid, "guest")
            self.g.confirm_launch(self.sid)
        self.assertEqual(len(self.g.launcher.launched), 1)
        self.assertEqual(self.h.get_session(self.sid)["state"], m.RUNNING)

    def test_late_success_and_health_probe_cannot_reopen_expired_start(self):
        self.g.launcher.active = {"rom_path": self.g.store.require(self.sid)["local"]["game_path"]}
        with mock.patch.object(m, "now", return_value=self.after_deadline):
            self.g._guest_message(self.g.store.require(self.sid), "launch_result", ok=True)
            self.h.evaluate_launch(self.sid, probe=True)
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_FAILED)

    def test_roster_departure_keeps_remaining_running_player_healthy(self):
        self.g.confirm_launch(self.sid)
        self.g.leave_session(self.sid)
        self.h.tick()
        self.assertEqual(self.h.get_session(self.sid)["state"], m.RUNNING)
        self.assertEqual(self.h.get_session(self.sid)["players"]["guest"]["status"], m.P_LEFT)

    def test_offline_retries_preserve_budget_until_delivery(self):
        with mock.patch.object(m, "now", return_value=self.after_deadline):
            self.h.evaluate_launch(self.sid)
            self.hub.offline.add("guest")
            for _ in range(4):
                self.h.retry_connection(self.sid, "guest")
            self.assertEqual(self.h.store.require(self.sid)["local"]["retries"].get("guest", 0), 0)
            self.hub.offline.clear()
            self.h.retry_connection(self.sid, "guest")
            self.assertEqual(self.h.store.require(self.sid)["local"]["retries"]["guest"], 1)
            self.g.confirm_launch(self.sid)
        self.assertEqual(self.h.get_session(self.sid)["state"], m.RUNNING)


class LaunchDeliveryTests(SwarmCase):
    def test_missed_offer_is_delivered_when_guest_returns_without_restart(self):
        sid = self.lobby()
        self.hub.offline.add("guest")
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertEqual(self.h.launcher.exited, 0)
        self.hub.offline.clear()
        self.h.tick()
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.h.tick()
        self.assertFalse(self.g.get_session(sid)["pending_launch"])
        self.assertEqual(len(self.g.launcher.launched), 1)

    def test_unreachable_pending_guest_resumes_launching_without_claiming_game_running(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.hub.offline.add("guest")
        self.h._probe_player(sid, "guest")
        self.h._probe_player(sid, "guest")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_UNREACHABLE)
        self.hub.offline.clear()
        self.h._probe_player(sid, "guest")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        self.assertEqual(self.g.launcher.launched, [])
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
