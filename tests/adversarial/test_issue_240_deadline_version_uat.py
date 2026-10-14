"""#240 §§3.1, 5.1, 7, 11, 14, 17: launch-deadline offers, version evidence, host loss.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- A launch offer is consent to join the host's *current* start attempt while
  that player is still expected to launch. After the launch deadline the host
  has failed that player. Confirming later must not start a game, and must not
  revive the player, unless the host retries and delivers a fresh offer.
- Completing the session while the guest is unreachable must not leave a
  usable launch offer. Confirmation after the host has ended must not launch.
- RetroArch Netplay joining compares core and emulator versions. A mismatch is
  a capability error the user can act on, not a silent join.
- One confirmed session is evidence for that emulator version only.
- Turning Multiplayer off refuses a new start before any emulator is touched.
- If the host becomes unavailable during a live native session, the guest
  restores temporary settings and does not invent a new launch.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch gameplay.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "tests"):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from app.multiplayer import models as m
from app.multiplayer.lifecycle import HOST_LOST_AFTER, LAUNCH_DEADLINE
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
SNES9X_INFO = (
    'display_name = "Snes9x"\ndisplay_version = "{version}"\n'
    'savestate = "true"\nsavestate_features = "deterministic"\n'
)


class Clock:
    def __init__(self):
        self.value = datetime(2032, 5, 10, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, delta):
        self.value = (self.value + delta).replace(microsecond=0)


class DeadlineVersionUAT(unittest.TestCase):
    names = ("host", "guest")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name
        self.clock = Clock()
        for patcher in (
            mock.patch.object(m, "now", side_effect=self.clock.now),
            mock.patch("app.multiplayer.core.socket.gethostname", return_value="test-drone"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub, self.nodes = make_swarm(self.tmp, self.names, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.before = {name: node.conf_text for name, node in self.nodes.items()}

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def four_player_copies(self):
        for node in self.nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()

    def lobby(self, peers=("guest",), extra=None):
        body = {"system": "snes", "game_id": "g1", "peers": list(peers), **CORE}
        if extra:
            body.update(extra)
        sid = self.h.create_session(body)["id"]
        for name in peers:
            self.nodes[name].orch.accept_invitation(sid)
            self.nodes[name].orch.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def assessment(self, orch=None):
        orch = orch or self.h
        game = orch.library.get("snes", "g1")
        return next(item for item in orch.assessor.assess(game, core_override="snes9x")
                    if item["mechanism"] == m.RETROARCH_NETPLAY)

    def test_late_confirm_after_launch_deadline_does_not_revive_a_failed_guest(self):
        self.hub, self.nodes = make_swarm(str(Path(self.tmp) / "deadline"), ("host", "guest", "mallory"),
                                          open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.four_player_copies()
        sid = self.lobby(peers=("guest", "mallory"), extra={"max_players": 4})
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.assertTrue(self.nodes["mallory"].orch.get_session(sid)["pending_launch"])
        self.hub.offline.add("guest")
        self.nodes["mallory"].orch.confirm_launch(sid)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        self.clock.advance(LAUNCH_DEADLINE + timedelta(seconds=1))
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_FAILED)
        self.assertEqual(session["state"], m.DEGRADED)
        self.hub.offline.clear()
        self.g.recover()
        try:
            self.g.confirm_launch(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        self.assertEqual(self.nodes["guest"].launcher.launched, [],
                         "a launch-deadline failure is not a reconnect; the guest must not start from the old offer")
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertNotEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)

    def test_confirm_before_the_deadline_while_another_guest_is_running_still_joins(self):
        self.hub, self.nodes = make_swarm(str(Path(self.tmp) / "before-deadline"), ("host", "guest", "mallory"),
                                          open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.four_player_copies()
        sid = self.lobby(peers=("guest", "mallory"), extra={"max_players": 4, "client_request_id": "before-deadline"})
        self.h.start_session(sid)
        self.nodes["mallory"].orch.confirm_launch(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_confirm_after_host_completed_while_guest_was_offline_does_not_launch(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.hub.offline.add("guest")
        self.h.end_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)
        self.hub.offline.clear()
        self.g.recover()
        try:
            self.g.confirm_launch(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        self.assertEqual(self.nodes["guest"].launcher.launched, [],
                         "ending the session while the guest is unreachable must not leave a live launch offer")

    def test_different_retroarch_core_version_blocks_join(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        guest_adapter = self.g.adapter_map["retroarch"]
        guest_adapter.probe.files["/usr/share/libretro/info/snes9x_libretro.info"] = SNES9X_INFO.format(version="1.60.0")
        guest_adapter.refresh()
        error = self.code(self.g.accept_invitation, sid)
        self.assertEqual(error, "cannot_join")
        self.assertIn("versions differ", self.g.validate_for_join(self.h.store.require(sid))["problems"][0])
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_INVITED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].launcher.launched, [])

    def test_different_retroarch_emulator_version_blocks_join(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                     "client_request_id": "ra-version", **CORE})["id"]
        self.g.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.g.adapter_map["retroarch"].refresh()
        caught = self.code(self.g.accept_invitation, sid)
        self.assertEqual(caught, "cannot_join")
        message = self.g.validate_for_join(self.h.store.require(sid))["problems"][0]
        self.assertIn("RetroArch versions are incompatible", message)
        self.assertIn("1.19.1", message)
        self.assertIn("1.21.0", message)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_confirmed_success_does_not_verify_a_different_emulator_version(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.h.end_session(sid)
        self.h.record_result(sid, True)
        self.assertEqual(self.assessment()["classification"], m.VERIFIED_COMPATIBLE)
        self.h.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.h.adapter_map["retroarch"].refresh()
        verdict = self.assessment()
        self.assertNotEqual(verdict["classification"], m.VERIFIED_COMPATIBLE,
                            "one successful session is not proof across emulator versions")
        self.assertNotEqual(verdict["compatibility_basis"], "confirmed-session")

    def test_disabling_multiplayer_refuses_start_before_launch(self):
        sid = self.lobby()
        self.h.update_settings({"general": {"enabled": False}})
        self.assertEqual(self.code(self.h.start_session, sid), "multiplayer_disabled")
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].conf_text, self.before["host"])
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)

    def test_host_loss_during_a_running_session_restores_guest_settings(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertNotEqual(self.nodes["guest"].conf_text, self.before["guest"])
        self.hub.offline.add("host")
        self.g.tick()
        self.clock.advance(HOST_LOST_AFTER + timedelta(seconds=1))
        self.g.tick()
        local = self.g.store.require(sid)["local"]
        self.assertTrue(local.get("ended"))
        self.assertEqual(self.nodes["guest"].conf_text, self.before["guest"])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertIsNotNone(self.nodes["guest"].launcher.active)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)

    def test_infrastructure_failure_cannot_be_recorded_as_play_evidence(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation temporarily unavailable"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.code(self.h.record_result, sid, False, "launcher down"), "never_ran")
        self.assertNotEqual(self.assessment()["classification"], m.INCOMPATIBLE)


if __name__ == "__main__":
    unittest.main()
