"""Authenticated host attacks against queued, locally confirmed launch offers.

Only local temporary fixtures and the repository's simulated swarm are used.
Holding the spawn callback deterministically models a delayed daemon worker.
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


class LaunchOfferBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue-240-offer-security-")
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
        self.host.start_session(self.sid)
        self.queued = []
        self.guest._spawn = lambda fn, name: self.queued.append(fn)
        self.original_config = self.nodes["guest"].conf_text

    def assert_no_local_execution(self):
        self.assertEqual(self.guest.launcher.launched, [],
                         "A worker for an old consent must not execute a fresh unconfirmed offer")
        self.assertEqual(self.nodes["guest"].conf_text, self.original_config)
        self.assertEqual(self.guest.guard.pending(), {})

    def test_host_cannot_replace_consumed_offer_under_queued_worker(self):
        self.guest.confirm_launch(self.sid)
        self.assertEqual(len(self.queued), 1)
        approved = dict(self.guest.store.require(self.sid)["local"]["pending_launch"])
        # A different message ID passes replay protection. It replaces the
        # consumed plan with a new, explicitly unconfirmed launch offer.
        try:
            self.guest.peer_launch("host", {
                "message_id": m.new_message_id(), "issued_at": m.now_iso(),
                "session_id": self.sid, "port": 55435, "secret": "replacement123",
                "wave": 99,
            })
        except m.MultiplayerError:
            self.assert_no_local_execution()
            return  # Rejecting replacement of an in-flight offer is safe.
        current = self.guest.store.require(self.sid)["local"].get("pending_launch")
        if current == approved:
            return  # Preserving the exact already-confirmed offer is safe too.
        self.queued[0]()
        self.assert_no_local_execution()

    def test_host_retry_cannot_reuse_worker_for_voided_confirmation(self):
        self.guest.confirm_launch(self.sid)
        old_wave = self.guest.store.require(self.sid)["local"]["pending_launch"]["wave"]
        self.host.fail_session(self.sid, "Host reset before guest launch")
        self.assertIsNone(self.guest.store.require(self.sid)["local"]["pending_launch"])
        self.host.retry_connection(self.sid)
        # The attacker controls only its own roster. The guest receives the
        # same authenticated snapshots it would receive from a real host.
        def fake_ready(session):
            session["players"]["host"]["ready"] = True
            session["players"]["guest"]["ready"] = True
            session["state"] = m.READY
        self.host.store.mutate(self.sid, fake_ready)
        self.host.publish(self.sid)
        self.host.start_session(self.sid)
        offer = self.guest.store.require(self.sid)["local"]["pending_launch"]
        self.assertGreater(offer["wave"], old_wave)
        self.assertFalse(offer["consumed"])
        self.assertEqual(self.guest.get_settings()["privacy"]["local_launch_authorization"], "confirm")
        # No local confirmation or readiness action occurred after the reset.
        self.queued[0]()
        self.assert_no_local_execution()

    def test_original_confirmed_offer_still_launches(self):
        self.guest.confirm_launch(self.sid)
        self.queued[0]()
        self.assertEqual(len(self.guest.launcher.launched), 1)


if __name__ == "__main__":
    unittest.main()
