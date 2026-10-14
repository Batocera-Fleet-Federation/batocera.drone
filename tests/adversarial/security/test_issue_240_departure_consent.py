"""Local-only attacks by an authenticated multiplayer host against guest consent.

The host is attacker-controlled; its roster writes model forged outbound snapshots.
Guest state is touched only through real local actions and peer protocol handlers.
No real emulator, network socket, or external system is used.
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


class DepartureConsentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="issue-240-security-")
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        self.host = self.nodes["host"].orch
        self.guest = self.nodes["guest"].orch
        # Automatic launch does not authorize rejoining after an explicit leave.
        # Auto-accept remains disabled, as it is by default.
        self.guest.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.sid = self.host.create_session({
            "system": "snes", "game_id": "g1", "peers": ["guest"],
            "emulator_override": {"core": "snes9x"},
        })["id"]
        self.guest.accept_invitation(self.sid)

    def host_failure_and_retry(self):
        # A compromised host may lie about its roster and session state. These
        # writes are on the attacker's own machine; publish uses /update and the
        # guest performs its normal identity/version checks.
        def forged_failure(session):
            session["state"] = m.FAILED
            session["players"]["guest"].update(status=m.P_JOINED)
        self.host.store.mutate(self.sid, forged_failure)
        self.host.publish(self.sid)
        self.host.retry_connection(self.sid)

    def test_remote_retry_cannot_erase_an_explicit_local_departure(self):
        self.guest.leave_session(self.sid)
        self.assertTrue(self.guest.store.require(self.sid)["local"]["ended"])
        self.host_failure_and_retry()
        self.assertTrue(
            self.guest.store.require(self.sid)["local"]["ended"],
            "A host's FAILED/lobby snapshots must not revoke a local Leave decision",
        )

    def test_departed_guest_cannot_be_auto_launched_without_fresh_acceptance(self):
        self.guest.leave_session(self.sid)
        original_config = self.nodes["guest"].conf_text
        self.host_failure_and_retry()

        def forged_readiness(session):
            session["players"]["guest"].update(ready=True)
            session["players"]["host"].update(ready=True)
            session["state"] = m.READY
        self.host.store.mutate(self.sid, forged_readiness)
        self.host.publish(self.sid)
        self.host.start_session(self.sid)
        # No invite or accept action occurred after leave. The adversary only
        # sent host-authorized state updates and the normal launch offer.
        self.assertEqual(self.guest.launcher.launched, [],
                         "Leaving must revoke automatic-launch authority for this membership")
        self.assertEqual(self.nodes["guest"].conf_text, original_config)

    def test_still_joined_guest_can_recover_a_host_failure(self):
        self.host_failure_and_retry()
        session = self.guest.store.require(self.sid)
        self.assertFalse(session["local"].get("ended"))
        self.assertEqual(session["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.guest.launcher.launched, [])


if __name__ == "__main__":
    unittest.main()
