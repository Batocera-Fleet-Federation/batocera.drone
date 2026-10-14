"""Issue #240 adversarial UAT: local consent, readiness, and session lifecycle.

Expected behaviour, derived from the issue before inspecting the implementation:

* Opening Multiplayer, inviting a paired peer, and accepting must never launch a
  game. An invitation is a proposal; only this machine's user starts a game here.
* Auto-accept invitations is off by default. Turning it on still only joins the
  lobby.
* Play Together is refused until every joined player is ready.
* A remote end never closes the guest's running game.
* Replacing a game that is already running needs this machine's approval.
* After a successful RetroArch session, emulator settings are restored.
* Host-only actions stay on the host. A guest leave is not a session kill.
* Pending invitees who have not accepted are not launched when the host starts.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import SNES_GAME, make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}


class SwarmCase(unittest.TestCase):
    names = ("host", "guest")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.hub, self.nodes = make_swarm(self.tmp, self.names, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code


class ConsentAndSessionUAT(SwarmCase):
    def test_definition_of_done_invite_accept_ready_play_end_restores(self):
        host_conf, guest_conf = self.nodes["host"].conf_text, self.nodes["guest"].conf_text
        overview = self.h.overview()
        self.assertTrue(overview["enabled"])
        self.assertEqual(overview["online_peers"], 1)
        self.assertTrue(any(peer["device_id"] == "guest" for peer in overview["peers"]))
        self.assertIn(m.RETROARCH_NETPLAY, {item["mechanism"] for item in overview["mechanisms"]})

        games = self.h.list_games({"system": "snes", "include_peers": "1"})["games"]
        self.assertEqual(games[0]["title"], SNES_GAME["title"])
        self.assertEqual(games[0]["peers"][0]["device_id"], "guest")

        session = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["state"], m.INVITING)
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        self.assertTrue(session["mechanism_reason"])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].launcher.launched, [])

        invitation = self.g.list_invitations()[0]
        for field in ("session_id", "host_id", "game", "emulator", "mechanism", "max_players", "expires_at"):
            self.assertIn(field, invitation)
        self.assertEqual(invitation["host_id"], "host")
        self.assertEqual(invitation["session_id"], session["id"])
        self.assertEqual(invitation["game"]["title"], SNES_GAME["title"])
        self.assertNotIn("secret", invitation)

        self.g.accept_invitation(session["id"])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.code(self.h.start_session, session["id"]), "players_not_ready")

        self.g.set_ready(session["id"], True)
        self.assertEqual(self.code(self.h.start_session, session["id"]), "players_not_ready")
        self.h.set_ready(session["id"], True)
        self.assertEqual(self.h.get_session(session["id"])["state"], m.READY)

        started = self.h.start_session(session["id"])
        self.assertEqual(started["state"], m.STARTING)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertTrue(self.g.get_session(session["id"])["pending_launch"])

        self.g.confirm_launch(session["id"])
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(session["id"])["state"], m.RUNNING)

        ended = self.h.end_session(session["id"])
        self.assertEqual(ended["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].conf_text, host_conf)
        self.assertEqual(self.nodes["guest"].conf_text, guest_conf)
        self.assertEqual(self.nodes["host"].launcher.exited, 1)
        self.assertIsNotNone(self.nodes["guest"].launcher.active)
        self.assertEqual(self.h.guard.pending(), {})
        self.assertEqual(self.g.guard.pending(), {})

    def test_auto_accept_is_off_by_default_and_still_never_launches(self):
        settings = self.g.get_settings()
        self.assertFalse(settings["general"]["auto_accept_invitations"])
        self.assertEqual(settings["privacy"]["local_launch_authorization"], "confirm")
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "invited")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.h.end_session(sid)

        self.g.update_settings({"general": {"auto_accept_invitations": True}})
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                     "client_request_id": "auto", **CORE})["id"]
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "joined")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_guest_cannot_perform_host_actions_and_leave_is_not_a_kill(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        for call, args in (
            (self.g.start_session, (sid,)),
            (self.g.invite_peer, (sid, "guest")),
            (self.g.remove_player, (sid, "guest")),
            (self.g.assign_slot, (sid, "guest", 2)),
            (self.g.end_session, (sid,)),
        ):
            with self.subTest(call=call.__name__):
                self.assertEqual(self.code(call, *args), "not_session_host")
        self.g.leave_session(sid)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "left")
        self.assertNotEqual(self.h.get_session(sid)["state"], m.COMPLETED)

    def test_running_game_is_not_replaced_without_local_approval(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        other = {"rom_path": "/other.sfc", "name": "other", "system": "snes", "emulator": "libretro"}
        self.nodes["host"].launcher.active = dict(other)
        self.assertEqual(self.code(self.h.start_session, sid), "game_running")
        self.assertEqual(self.nodes["host"].launcher.exited, 0)
        self.h.start_session(sid, replace_running_game=True)
        self.assertEqual(self.nodes["host"].launcher.exited, 1)

    def test_unaccepted_invitee_is_not_launched_when_the_host_starts(self):
        self.hub, self.nodes = make_swarm(self.tmp, ("host", "guest", "mallory"), open_ports=(55435,))
        self.h, self.g = self.nodes["host"].orch, self.nodes["guest"].orch
        for name in ("host", "guest", "mallory"):
            self.nodes[name].repo.games["snes"][0]["players"] = "1-4"
            self.nodes[name].orch.library.invalidate()
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest", "mallory"],
                                     "max_players": 4, **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.nodes["mallory"].launcher.launched, [])
        self.assertEqual(self.h.get_session(sid)["players"]["mallory"]["status"], "removed")
        self.assertEqual(self.nodes["mallory"].orch.list_invitations(), [])

    def test_confirm_after_the_host_ended_does_not_launch(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.assertTrue(self.g.get_session(sid)["pending_launch"])
        self.h.end_session(sid)
        self.assertIn(self.code(self.g.confirm_launch, sid), ("nothing_to_launch", "session_closed", "not_a_member"))
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_duplicate_accept_is_idempotent_and_does_not_launch(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        first = self.g.accept_invitation(sid)
        again = self.g.accept_invitation(sid)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "joined")

    def test_concurrent_accept_and_decline_never_launch_and_leave_a_terminal_invite(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        errors = []

        def accept():
            try:
                self.g.accept_invitation(sid)
            except m.MultiplayerError as error:
                errors.append(error.code)

        def decline():
            try:
                self.g.decline_invitation(sid)
            except m.MultiplayerError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=accept), threading.Thread(target=decline)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        invitation = self.g.list_invitations(include_closed=True)[0]
        self.assertIn(invitation["status"], ("accepted", "declined"))
        self.assertNotEqual(invitation["status"], "pending")
        guest_status = self.h.get_session(sid)["players"]["guest"]["status"]
        self.assertIn(guest_status, ("joined", "declined", "invited"))


if __name__ == "__main__":
    unittest.main()
