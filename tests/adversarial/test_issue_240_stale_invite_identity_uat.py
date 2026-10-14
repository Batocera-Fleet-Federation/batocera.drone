"""#240 §§2.1-2.4, 3.1, 5.1, 6, 8: invitation identity after mechanism change.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- Host Game can create a lobby, then Invite Players one at a time (spec 2.1,
  2.3). Each invitation names the session mechanism and compatibility
  requirements the receiving user reviews (spec 2.4). If adding a peer causes
  the host to change mechanism (spec 8: participating peers; spec 6: streaming
  when native is unsuitable), pending invitees must receive a reviewable
  invitation for the session that actually exists. Accept must succeed for that
  current session and must not launch a game.
- Acceptance is membership on both machines. A guest who rejects the live
  session (identity no longer matches the invitation they reviewed) is not
  joined on the host. Spec 3.1: conflicting operations and stale session
  updates must not leave split-brain membership.
- Invite Peer after a failed accept still delivers a new pending invitation
  when the guest is not actually in the lobby. The host must not be stuck
  waiting for a player who has no local session (spec 2.3, 17).
- View Compatible Games / Host Game evaluates the host's selected core, not
  an invited peer's unrelated default core (spec 5.1, 8). A guest who has the
  host core installed is not blocked solely because batocera.conf names another
  core.

Revision note (dispute adjudication): the earlier setup invited two peers into a
lobby whose live mechanism is REMOTE_STREAMING, i.e. three occupied seats against the
published two-seat streaming capacity. Spec 2.3 ("maximum players based on the game's
actual capabilities") and the capacity suite forbid that, so those four tests now reach
the same stale-invitation conditions with host + one guest: the guest misses a withdrawal
and the re-invite that follows a host RetroArch upgrade. The identity invariants are unchanged.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch or Sunshine gameplay.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

from app.multiplayer import models as m
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
STREAM = {
    "binaries": {
        "retroarch": "/usr/bin/retroarch",
        "sunshine": "/usr/bin/sunshine",
        "moonlight": "/usr/bin/moonlight",
    },
    "processes": {"sunshine"},
    "versions": {"/usr/bin/retroarch": "1.19.1"},
    "open_ports": (55435,),
}
BSNES_INFO = (
    'display_name = "bsnes"\ndisplay_version = "115"\n'
    'savestate = "true"\nsavestate_features = "deterministic"\n'
)


class StaleInviteIdentityUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def _three(self):
        hub, nodes = make_swarm(self.tmp, ("host", "guest", "mallory"), **STREAM)
        for node in nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        nodes["mallory"].orch.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        nodes["mallory"].orch.adapter_map["retroarch"].refresh()
        self.hub, self.nodes = hub, nodes
        self.h = nodes["host"].orch
        self.g = nodes["guest"].orch
        self.mal = nodes["mallory"].orch
        return nodes

    def _pending(self, orch):
        return orch.list_invitations()

    def _stale_pair(self, guest_online_for_reinvite):
        """Two seats only (host + guest), the most a streaming lobby may hold (spec 2.3).

        The guest reviews a native invitation I1. The host withdraws it while the guest is
        unreachable (the guest never hears), then upgrades RetroArch so native Netplay no
        longer fits the pair, and invites the guest again: the live session is now
        REMOTE_STREAMING. Optionally that refresh is not delivered, leaving the guest holding I1.
        """
        hub, nodes = make_swarm(self.tmp, ("host", "guest"), **STREAM)
        for node in nodes.values():
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        self.hub, self.nodes = hub, nodes
        self.h, self.g = nodes["host"].orch, nodes["guest"].orch
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], "max_players": 4, **CORE})["id"]
        stale = self._pending(self.g)
        self.assertEqual([item["mechanism"] for item in stale], [m.RETROARCH_NETPLAY])
        hub.offline.add("guest")
        self.h.remove_player(sid, "guest")
        nodes["host"].probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.h.adapter_map["retroarch"].refresh()
        hub.offline.clear()
        if not guest_online_for_reinvite:
            # The guest still answers compatibility queries but refuses the refreshed invitation,
            # so it keeps the stale native one it reviewed.
            self.g.update_settings({"general": {"invitations_enabled": False}})
        session = self.h.invite_peer(sid, "guest")
        self.g.update_settings({"general": {"invitations_enabled": True}})
        self.assertEqual(session["mechanism"], m.REMOTE_STREAMING, session["mechanism_reason"])
        self.assertEqual(session["max_players"], 2)
        self.assertEqual(session["players"]["guest"]["status"], m.P_INVITED)
        return sid

    def test_pending_invitation_follows_a_pre_join_mechanism_change(self):
        sid = self._stale_pair(guest_online_for_reinvite=True)
        pending = self._pending(self.g)
        self.assertEqual(len(pending), 1, "the guest must hold exactly one reviewable invitation")
        self.assertEqual(
            pending[0]["mechanism"], m.REMOTE_STREAMING,
            "the invitation the guest still holds must name the live mechanism, not the native "
            "session that no longer exists: %s" % pending[0]["mechanism"],
        )
        joined = self.g.accept_invitation(sid)
        self.assertEqual(joined["mechanism"], m.REMOTE_STREAMING)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].launcher.launched, [])

    def test_stale_accept_does_not_join_on_the_host_only(self):
        sid = self._stale_pair(guest_online_for_reinvite=False)
        self.assertEqual([item["mechanism"] for item in self._pending(self.g)], [m.RETROARCH_NETPLAY])
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_INVITED)
        try:
            self.g.accept_invitation(sid)
        except m.MultiplayerError:
            pass
        guest_local = self.g.store.get(sid)
        host_status = self.h.get_session(sid)["players"]["guest"]["status"]
        if guest_local is None:
            self.assertEqual(
                host_status, m.P_INVITED,
                "a guest who did not adopt the replica must not be joined on the host; "
                "split-brain membership leaves Start waiting for a player who cannot ready",
            )
        else:
            self.assertEqual(host_status, m.P_JOINED)
            self.assertEqual(guest_local["players"]["guest"]["status"], m.P_JOINED)
            self.assertEqual(guest_local["mechanism"], m.REMOTE_STREAMING)

    def test_failed_accept_does_not_cancel_invite_or_block_reinvite(self):
        sid = self._stale_pair(guest_online_for_reinvite=False)
        try:
            self.g.accept_invitation(sid)
        except m.MultiplayerError:
            pass
        if self.g.store.get(sid) is not None:
            self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
            return
        self.assertTrue(self._pending(self.g), "a refused accept must not cancel the guest's invitation")
        self.h.invite_peer(sid, "guest")
        pending = [item["mechanism"] for item in self._pending(self.g)]
        self.assertIn(
            m.REMOTE_STREAMING, pending,
            "Invite Peer must deliver a reviewable invitation for the live mechanism after a failed accept; "
            "a host-only join must not make Invite a no-op and must not cancel the guest's invitation",
        )
        joined = self.g.accept_invitation(sid)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)

    def test_host_is_not_stuck_waiting_for_a_guest_with_no_session(self):
        sid = self._stale_pair(guest_online_for_reinvite=False)
        try:
            self.g.accept_invitation(sid)
        except m.MultiplayerError:
            pass
        self.h.set_ready(sid, True)
        if self.g.store.get(sid) is None:
            self.assertNotEqual(
                self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED,
                "Start must not wait for a guest who has no local session",
            )
            self.assertNotEqual(self.h.get_session(sid)["state"], m.READY)
            self.assertEqual(self.code(self.h.start_session, sid), "players_not_ready")
            self.assertEqual(self.nodes["host"].launcher.launched, [], "a refused start launches nothing")
            # Recovery: the host re-invites, the guest reviews the live session and joins.
            self.h.invite_peer(sid, "guest")
            self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        self.h.start_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_create_without_peers_then_invite_incompatible_guest_joins_streaming(self):
        hub, nodes = make_swarm(self.tmp + "-solo", **STREAM)
        host, guest = nodes["host"].orch, nodes["guest"].orch
        guest.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        guest.adapter_map["retroarch"].refresh()
        session = host.create_session({"system": "snes", "game_id": "g1", **CORE})
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        invited = host.invite_peer(session["id"], "guest")
        self.assertEqual(invited["mechanism"], m.REMOTE_STREAMING)
        joined = guest.accept_invitation(session["id"])
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_invite_incompatible_peer_after_join_is_refused(self):
        self._three()
        sid = self.h.create_session({
            "system": "snes", "game_id": "g1", "peers": ["guest"], "max_players": 4, **CORE,
        })["id"]
        self.assertEqual(self.h.get_session(sid)["mechanism"], m.RETROARCH_NETPLAY)
        self.g.accept_invitation(sid)
        self.assertEqual(self.code(self.h.invite_peer, sid, "mallory"), "mechanism_change")
        self.assertEqual(self.h.get_session(sid)["mechanism"], m.RETROARCH_NETPLAY)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)
        self.assertNotIn("mallory", self.h.get_session(sid)["players"])

    def test_create_with_incompatible_peer_selects_streaming(self):
        hub, nodes = make_swarm(self.tmp + "-pair", **STREAM)
        host, guest = nodes["host"].orch, nodes["guest"].orch
        guest.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        guest.adapter_map["retroarch"].refresh()
        session = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["mechanism"], m.REMOTE_STREAMING)
        guest.accept_invitation(session["id"])
        self.assertEqual(host.get_session(session["id"])["players"]["guest"]["status"], m.P_JOINED)


class HostCoreRecommendationUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def test_game_detail_does_not_block_native_for_a_peers_unrelated_default_core(self):
        hub, nodes = make_swarm(
            self.tmp,
            conf={"snes.emulator": "libretro"},
            **STREAM,
        )
        host, guest = nodes["host"].orch, nodes["guest"].orch
        host.probe.conf["snes.core"] = "snes9x"
        guest.probe.conf["snes.core"] = "bsnes"
        guest.probe.files["/usr/lib/libretro/bsnes_libretro.so"] = "binary"
        guest.probe.files["/usr/share/libretro/info/bsnes_libretro.info"] = BSNES_INFO
        host.adapter_map["retroarch"].refresh()
        guest.adapter_map["retroarch"].refresh()
        detail = host.game_detail("snes", "g1")
        native = next(item for item in detail["recommendation"]["candidates"] if item["mechanism"] == m.RETROARCH_NETPLAY)
        self.assertTrue(
            native["usable"],
            "View Compatible Games must ask whether the peer can run the host's snes9x core, which is "
            "installed there; comparing the guest's default bsnes version is not a Netplay incompatibility: %s"
            % native["blockers"],
        )
        self.assertEqual(
            detail["recommendation"]["mechanism"], m.RETROARCH_NETPLAY,
            "native Netplay is usable for this pair; the Host Game recommendation must not prefer streaming "
            "because the guest's batocera.conf names a different core: %s" % detail["recommendation"]["reason"],
        )
        session = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        guest.accept_invitation(session["id"])
        self.assertEqual(host.get_session(session["id"])["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(nodes["guest"].launcher.launched, [])


if __name__ == "__main__":
    unittest.main()
