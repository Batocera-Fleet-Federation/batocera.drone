"""Fresh launch authorization and diagnostic-only failures on a simulated swarm."""
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
