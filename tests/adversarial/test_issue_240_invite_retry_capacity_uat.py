"""#240 §§2.3-2.4, 3.1, 5.2-5.3, 8, 11, 14, 17: re-invite, retry of a live game, capacity.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- Invite Peer is a new proposal. A guest who left the lobby, or declined, can
  be invited again. The new invitation is reviewable (accept or decline) and
  does not launch a game. A leftover Join of a previously accepted invitation
  is not membership after the guest has left.
- Retry Connection recovers a guest who already launched this session's game.
  Spec 11 warns before replacing a *different* running game; the session ROM
  already in the foreground is participation, not a conflict. Recovery must
  not require killing that game and must not leave the host treating them as
  failed while they are already playing.
- A FAILED start occupies no extra Host Game slot once the user starts a new
  session. Spec 14 limits simultaneous live sessions; spec 17 forbids abandoned
  failed sessions from holding resources. Retry of that failed session remains
  available until it is replaced or ended.
- Mechanism selection never auto-picks a host capability the installed build
  cannot provide: PPSSPP without a built-in ad hoc server cannot host; a
  Dolphin nogui-only install cannot run the GUI Netplay lobby.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch, PPSSPP, or Dolphin
gameplay.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from app.multiplayer import models as m
from app.multiplayer.lifecycle import LAUNCH_DEADLINE
from multiplayer_support import Hub, Node, make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
JOIN_ONLY_PPSSPP_INI = "[Network]\nEnableWlan = False\nProAdhocServer = 127.0.0.1\n"


class Clock:
    def __init__(self):
        self.value = datetime(2032, 5, 10, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def advance(self, delta):
        self.value = (self.value + delta).replace(microsecond=0)


class InviteRetryCapacityUAT(unittest.TestCase):
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

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def lobby(self, peers=("guest",)):
        sid = self.h.create_session({
            "system": "snes", "game_id": "g1", "peers": list(peers),
            "max_players": max(2, len(peers) + 1), **CORE,
        })["id"]
        for name in peers:
            self.nodes[name].orch.accept_invitation(sid)
            self.nodes[name].orch.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def pending_ids(self, orch):
        return [item["session_id"] for item in orch.list_invitations()]

    def test_accept_after_leave_without_a_new_invite_does_not_rejoin(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.leave_session(sid)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LEFT)
        try:
            self.g.accept_invitation(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LEFT,
                         "leaving the lobby spends membership; Join again requires a new invitation")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_invite_after_leave_delivers_a_reviewable_invitation(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.leave_session(sid)
        self.h.invite_peer(sid, "guest")
        self.assertIn(sid, self.pending_ids(self.g),
                      "Invite Peer after a lobby leave must deliver a new pending invitation")
        joined = self.g.accept_invitation(sid)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].launcher.launched, [])

    def test_invite_after_decline_delivers_a_reviewable_invitation(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.assertEqual(self.g.decline_invitation(sid)["status"], "declined")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_DECLINED)
        self.h.invite_peer(sid, "guest")
        self.assertIn(sid, self.pending_ids(self.g),
                      "Invite Peer after a decline must deliver a new pending invitation, not a silent duplicate")
        joined = self.g.accept_invitation(sid)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def three_player_ready(self, label):
        self.hub, self.nodes = make_swarm(str(Path(self.tmp) / label), ("host", "guest", "mallory"),
                                          open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        return self.lobby(peers=("guest", "mallory"))

    def test_housekeeping_recovers_a_confirmed_launch_whose_result_was_dropped(self):
        sid = self.three_player_ready("dropped-result")
        self.h.start_session(sid)
        self.nodes["mallory"].orch.confirm_launch(sid)
        original = self.g._guest_message

        def drop_launch_result(session, action, **extra):
            if action == "launch_result":
                return {"skipped": True}
            return original(session, action, **extra)

        with mock.patch.object(self.g, "_guest_message", side_effect=drop_launch_result):
            self.g.confirm_launch(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)

    def test_retry_of_a_guest_already_running_the_session_game_does_not_require_replace(self):
        sid = self.three_player_ready("retry-live")
        self.h.start_session(sid)
        self.nodes["mallory"].orch.confirm_launch(sid)
        path = self.g.store.require(sid)["local"]["game_path"]
        self.clock.advance(LAUNCH_DEADLINE + timedelta(seconds=1))
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_FAILED)
        self.nodes["guest"].launcher.active = {
            "rom_path": path, "name": "Super Mario Kart", "system": "snes", "emulator": "libretro",
        }
        self.h.retry_connection(sid, "guest")
        host = self.h.get_session(sid)
        if host["players"]["guest"]["status"] == m.P_RUNNING:
            self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        else:
            err = None
            try:
                self.g.confirm_launch(sid)
            except m.MultiplayerError as error:
                err = error
            self.assertIsNone(err, "the session game already running is not a replace conflict: %s"
                              % (err.code if err else ""))
            self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)
            self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.assertEqual(len(self.nodes["mallory"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_retry_still_requires_approval_when_a_different_game_is_running(self):
        sid = self.three_player_ready("retry-other")
        self.h.start_session(sid)
        self.nodes["mallory"].orch.confirm_launch(sid)
        self.clock.advance(LAUNCH_DEADLINE + timedelta(seconds=1))
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_FAILED)
        self.nodes["guest"].launcher.active = {
            "rom_path": "/other.sfc", "name": "other", "system": "snes", "emulator": "libretro",
        }
        self.h.retry_connection(sid, "guest")
        self.assertEqual(self.code(self.g.confirm_launch, sid), "game_running")
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.g.confirm_launch(sid, replace_running_game=True)
        self.assertEqual(self.nodes["guest"].launcher.exited, 1)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)

    def test_failed_session_does_not_block_a_new_host_game(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation temporarily unavailable"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.nodes["host"].launcher.fail_launch = None
        try:
            created = self.h.create_session({
                "system": "snes", "game_id": "g1", "peers": ["guest"],
                "client_request_id": "next-game", **CORE,
            })
        except m.MultiplayerError as error:
            self.fail("a failed start must not occupy the only Host Game slot until the session timeout: %s"
                      % error.code)
        self.assertNotEqual(created["id"], sid)
        self.assertIn(created["state"], m.PRE_START_STATES)
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_failed_session_can_still_be_retried_when_no_new_session_replaced_it(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation temporarily unavailable"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.nodes["host"].launcher.fail_launch = None
        retried = self.h.retry_connection(sid)
        self.assertIn(retried["state"], m.PRE_START_STATES)
        try:
            self.g.accept_invitation(sid)
        except m.MultiplayerError:
            pass
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)


class HostCapabilityHonestyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def test_ppsspp_without_a_builtin_adhoc_server_is_not_an_automatic_host_mechanism(self):
        hub = Hub()
        game = {"id": "p1", "file": "Tekken.iso", "title": "Tekken", "fingerprint": "aa" * 16,
                "players": "1-4", "size": 1000}
        nodes = {}
        for name in ("host", "guest"):
            node = Node(
                hub, name, Path(self.tmp) / "ppsspp",
                games={"psp": [dict(game)]},
                paired=["guest" if name == "host" else "host"],
                binaries={"PPSSPPSDL": "/usr/bin/PPSSPPSDL"},
                versions={"/usr/bin/PPSSPPSDL": "1.18.1"},
            )
            ini = str(node.settings.userdata_root / "system/configs/ppsspp/PSP/SYSTEM/ppsspp.ini")
            Path(ini).parent.mkdir(parents=True, exist_ok=True)
            Path(ini).write_text(JOIN_ONLY_PPSSPP_INI, encoding="utf-8")
            node.probe.files[ini] = JOIN_ONLY_PPSSPP_INI
            node.orch.adapter_map["ppsspp"].refresh()
            nodes[name] = node
        host = nodes["host"].orch
        try:
            session = host.create_session({"system": "psp", "game_id": "p1", "peers": ["guest"]})
        except m.MultiplayerError as error:
            self.assertEqual(error.code, "no_mechanism")
            self.assertEqual(host.list_sessions(), [])
            return
        self.assertNotEqual(
            session["mechanism"], m.PPSSPP_ADHOC,
            "a PPSSPP build that cannot host (no EnableAdhocServer) must not be auto-selected for the host",
        )

    def test_dolphin_nogui_only_is_not_an_automatic_netplay_host(self):
        hub = Hub()
        game = {"id": "gc1", "file": "Melee.iso", "title": "Melee", "fingerprint": "cc" * 16,
                "players": "1-4", "size": 1000}
        nodes = {}
        for name in ("host", "guest"):
            node = Node(
                hub, name, Path(self.tmp) / "dolphin",
                games={"gamecube": [dict(game)]},
                paired=["guest" if name == "host" else "host"],
                binaries={"dolphin-emu-nogui": "/usr/bin/dolphin-emu-nogui"},
                versions={"/usr/bin/dolphin-emu-nogui": "2506a"},
            )
            node.orch.adapter_map["dolphin"].refresh()
            nodes[name] = node
        host = nodes["host"].orch
        self.assertEqual(
            self.code(host.create_session, {"system": "gamecube", "game_id": "gc1", "peers": ["guest"],
                                            "mechanism": m.DOLPHIN_NETPLAY}),
            "mechanism_unusable",
        )
        try:
            session = host.create_session({"system": "gamecube", "game_id": "gc1", "peers": ["guest"]})
        except m.MultiplayerError as error:
            self.assertEqual(error.code, "no_mechanism")
            return
        self.assertNotEqual(session["mechanism"], m.DOLPHIN_NETPLAY)


if __name__ == "__main__":
    unittest.main()
