"""#240 §§5.1-5.3, 6, 8, 9: peer-capability mechanism selection.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- Mechanism selection uses installed emulator capabilities and compatibility
  with the invited peers (spec 8). RetroArch Netplay compares core and
  emulator versions (spec 5.1); Dolphin reports incompatible versions
  (spec 5.2). When native networking is unsuitable for a participating peer
  and remote streaming is installed and running, Drone selects streaming
  rather than a native session the guest cannot join (spec 6, 8).
- A forced native mechanism is honoured or refused. It is never silently
  substituted, and it is never accepted when the invited peer cannot use it.
- Existing join-time version checks without streaming remain: create may
  still succeed when streaming is absent, and accept then reports cannot_join.
  Those cases are covered by adversarial-issue-240-deadline-version-uat.
- PPSSPP without EnableAdhocServer cannot host. A join-only guest does not
  block a host that can host. A join-only host with Sunshine falls back to
  streaming. Forced PPSSPP_ADHOC on a join-only host is refused.
- Broadcast-style Switch LDN is unusable over Tailscale; streaming is used
  when it is available (spec 5.4, 8, 9).
- Invite Peer after the host removes a lobby member delivers a new reviewable
  invitation. Confirming a launch after that remove does not start a game.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch, Dolphin, PPSSPP,
Switch, or Sunshine gameplay.
"""
from __future__ import annotations

import json
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
from multiplayer_support import Hub, Node, make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
SNES9X_INFO = (
    'display_name = "Snes9x"\ndisplay_version = "{version}"\n'
    'savestate = "true"\nsavestate_features = "deterministic"\n'
)
JOIN_ONLY_PPSSPP_INI = "[Network]\nEnableWlan = False\nProAdhocServer = 127.0.0.1\n"
HOST_PPSSPP_INI = (
    "[Network]\nEnableWlan = False\nProAdhocServer = 127.0.0.1\nEnableAdhocServer = False\n"
)
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


class MechanismPeerCompatUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def swarm(self, **kwargs):
        options = dict(STREAM)
        options.update(kwargs)
        hub, nodes = make_swarm(self.tmp, **options)
        self.hub, self.nodes = hub, nodes
        self.h = nodes["host"].orch
        self.g = nodes["guest"].orch
        return nodes

    def test_incompatible_retroarch_versions_select_streaming_when_available(self):
        self.swarm()
        self.g.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.g.adapter_map["retroarch"].refresh()
        session = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(
            session["mechanism"], m.REMOTE_STREAMING,
            "native RetroArch is unsuitable when the invited peer's RetroArch major.minor differs; "
            "streaming is installed and running, so Host Game must not pick a doomed Netplay session: %s"
            % session["mechanism_reason"],
        )
        self.assertIn("streaming", session["mechanism_reason"].lower())
        joined = self.g.accept_invitation(session["id"])
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["host"].launcher.launched, [])

    def test_incompatible_retroarch_core_versions_select_streaming_when_available(self):
        self.swarm()
        info = "/usr/share/libretro/info/snes9x_libretro.info"
        self.g.probe.files[info] = SNES9X_INFO.format(version="1.60.0")
        self.g.adapter_map["retroarch"].refresh()
        session = self.h.create_session({
            "system": "snes", "game_id": "g1", "peers": ["guest"],
            "client_request_id": "core-mismatch", **CORE,
        })
        self.assertEqual(
            session["mechanism"], m.REMOTE_STREAMING,
            "native RetroArch is unsuitable when the invited peer's core version differs; "
            "streaming must be selected instead of a session the guest cannot join: %s"
            % session["mechanism_reason"],
        )
        self.g.accept_invitation(session["id"])
        self.assertEqual(self.h.get_session(session["id"])["players"]["guest"]["status"], m.P_JOINED)

    def test_forced_retroarch_with_incompatible_peer_is_refused(self):
        self.swarm()
        self.g.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.g.adapter_map["retroarch"].refresh()
        try:
            session = self.h.create_session({
                "system": "snes", "game_id": "g1", "peers": ["guest"],
                "mechanism": m.RETROARCH_NETPLAY, **CORE,
            })
        except m.MultiplayerError as error:
            self.assertEqual(error.code, "mechanism_unusable")
            self.assertEqual(self.h.list_sessions(), [])
            return
        self.fail(
            "Advanced/forced RetroArch Netplay must be refused when the invited peer cannot join it; "
            "created %s (%s)" % (session["mechanism"], session["mechanism_reason"]),
        )

    def test_game_detail_does_not_recommend_doomed_native_netplay(self):
        self.swarm(conf={"snes.core": "snes9x", "snes.emulator": "libretro"})
        self.h.adapter_map["retroarch"].refresh()
        self.g.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        self.g.adapter_map["retroarch"].refresh()
        detail = self.h.game_detail("snes", "g1")
        recommendation = detail["recommendation"]
        self.assertEqual(
            recommendation["mechanism"], m.REMOTE_STREAMING,
            "View Compatible Games must not propose RetroArch Netplay when the online peer's "
            "RetroArch version is incompatible and streaming is usable: %s"
            % recommendation["reason"],
        )
        native = next(item for item in recommendation["candidates"] if item["mechanism"] == m.RETROARCH_NETPLAY)
        self.assertFalse(native["usable"])
        self.assertTrue(native["blockers"])

    def test_without_streaming_incompatible_versions_still_create_then_block_join(self):
        hub, nodes = make_swarm(self.tmp + "-nostream", open_ports=(55435,))
        host, guest = nodes["host"].orch, nodes["guest"].orch
        guest.probe.versions["/usr/bin/retroarch"] = "1.21.0"
        guest.adapter_map["retroarch"].refresh()
        session = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        self.assertEqual(self.code(guest.accept_invitation, session["id"]), "cannot_join")
        self.assertEqual(nodes["guest"].launcher.launched, [])
        self.assertEqual(nodes["host"].launcher.launched, [])

    def test_incompatible_dolphin_versions_select_streaming_when_available(self):
        game = {"id": "gc1", "file": "Melee.iso", "title": "Melee", "fingerprint": "cc" * 16,
                "players": "1-4", "size": 1000}
        hub = Hub()
        nodes = {}
        for name in ("host", "guest"):
            version = "2506a" if name == "host" else "2509"
            node = Node(
                hub, name, Path(self.tmp) / "dolphin",
                games={"gamecube": [dict(game)]},
                paired=["guest" if name == "host" else "host"],
                binaries={
                    "dolphin-emu": "/usr/bin/dolphin-emu",
                    "sunshine": "/usr/bin/sunshine",
                    "moonlight": "/usr/bin/moonlight",
                },
                versions={"/usr/bin/dolphin-emu": version},
                processes={"sunshine"},
                open_ports=(55435,),
            )
            node.orch.adapter_map["dolphin"].refresh()
            node.orch.adapter_map["streaming"].refresh()
            nodes[name] = node
        host = nodes["host"].orch
        guest = nodes["guest"].orch
        session = host.create_session({"system": "gamecube", "game_id": "gc1", "peers": ["guest"]})
        self.assertEqual(
            session["mechanism"], m.REMOTE_STREAMING,
            "Dolphin versions differ (host 2506a, guest 2509); streaming is available so the host "
            "must not select Dolphin Netplay that the guest cannot join: %s" % session["mechanism_reason"],
        )
        guest.accept_invitation(session["id"])
        self.assertEqual(host.get_session(session["id"])["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_switch_ldn_over_tailscale_falls_back_to_streaming(self):
        game = {"id": "sw1", "file": "Smash.xci", "title": "Smash", "fingerprint": "dd" * 16,
                "players": "1-4", "size": 1000}
        hub = Hub()
        nodes = {}
        config = json.dumps({"multiplayer_mode": 0})
        for name in ("host", "guest"):
            node = Node(
                hub, name, Path(self.tmp) / "switch",
                games={"switch": [dict(game)]},
                paired=["guest" if name == "host" else "host"],
                binaries={
                    "Ryujinx": "/usr/bin/Ryujinx",
                    "sunshine": "/usr/bin/sunshine",
                    "moonlight": "/usr/bin/moonlight",
                },
                processes={"sunshine"},
                open_ports=(55435,),
            )
            path = node.settings.userdata_root / "system" / "configs" / "Ryujinx" / "Config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config, encoding="utf-8")
            node.probe.files[str(path)] = config
            node.orch.adapter_map["switch"].refresh()
            node.orch.adapter_map["streaming"].refresh()
            nodes[name] = node
        nodes["guest"].network.scope = "tailnet"
        host = nodes["host"].orch
        session = host.create_session({"system": "switch", "game_id": "sw1", "peers": ["guest"]})
        self.assertEqual(
            session["mechanism"], m.REMOTE_STREAMING,
            "Switch LDN relies on LAN broadcast, which Tailscale does not carry; streaming is "
            "available and must be selected: %s" % session["mechanism_reason"],
        )
        host.end_session(session["id"])
        self.assertEqual(self.code(host.create_session, {
            "system": "switch", "game_id": "sw1", "peers": ["guest"],
            "mechanism": m.SWITCH_LDN, "client_request_id": "force-ldn",
        }), "mechanism_unusable")


class HostCapabilityAndInviteUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = temporary.name

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def _ppsspp_pair(self, host_ini, guest_ini, *, sunshine=False):
        game = {"id": "p1", "file": "Tekken.iso", "title": "Tekken", "fingerprint": "aa" * 16,
                "players": "1-4", "size": 1000}
        hub = Hub()
        nodes = {}
        binaries = {"PPSSPPSDL": "/usr/bin/PPSSPPSDL"}
        processes = set()
        if sunshine:
            binaries["sunshine"] = "/usr/bin/sunshine"
            binaries["moonlight"] = "/usr/bin/moonlight"
            processes.add("sunshine")
        for name, ini in (("host", host_ini), ("guest", guest_ini)):
            node = Node(
                hub, name, Path(self.tmp) / "ppsspp",
                games={"psp": [dict(game)]},
                paired=["guest" if name == "host" else "host"],
                binaries=binaries,
                versions={"/usr/bin/PPSSPPSDL": "1.18.1"},
                processes=processes,
                open_ports=(55435,),
            )
            path = str(node.settings.userdata_root / "system/configs/ppsspp/PSP/SYSTEM/ppsspp.ini")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(ini, encoding="utf-8")
            node.probe.files[path] = ini
            node.orch.adapter_map["ppsspp"].refresh()
            if sunshine:
                node.orch.adapter_map["streaming"].refresh()
            nodes[name] = node
        return nodes

    def test_join_only_host_with_sunshine_selects_streaming(self):
        nodes = self._ppsspp_pair(JOIN_ONLY_PPSSPP_INI, JOIN_ONLY_PPSSPP_INI, sunshine=True)
        host = nodes["host"].orch
        session = host.create_session({"system": "psp", "game_id": "p1", "peers": ["guest"]})
        self.assertEqual(
            session["mechanism"], m.REMOTE_STREAMING,
            "a PPSSPP build that cannot host must not be auto-selected; Sunshine is running so "
            "streaming is the usable mechanism: %s" % session["mechanism_reason"],
        )
        nodes["guest"].orch.accept_invitation(session["id"])
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_forced_ppsspp_on_join_only_host_is_refused(self):
        nodes = self._ppsspp_pair(JOIN_ONLY_PPSSPP_INI, HOST_PPSSPP_INI, sunshine=True)
        host = nodes["host"].orch
        self.assertEqual(
            self.code(host.create_session, {
                "system": "psp", "game_id": "p1", "peers": ["guest"],
                "mechanism": m.PPSSPP_ADHOC,
            }),
            "mechanism_unusable",
        )
        self.assertEqual(host.list_sessions(), [])

    def test_join_only_guest_does_not_block_a_capable_host(self):
        nodes = self._ppsspp_pair(HOST_PPSSPP_INI, JOIN_ONLY_PPSSPP_INI)
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "psp", "game_id": "p1", "peers": ["guest"]})
        self.assertEqual(session["mechanism"], m.PPSSPP_ADHOC)
        joined = guest.accept_invitation(session["id"])
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_invite_after_host_remove_delivers_a_reviewable_invitation(self):
        hub, nodes = make_swarm(self.tmp, open_ports=(55435,))
        host, guest = nodes["host"].orch, nodes["guest"].orch
        sid = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        guest.accept_invitation(sid)
        host.remove_player(sid, "guest")
        self.assertEqual(host.get_session(sid)["players"]["guest"]["status"], m.P_REMOVED)
        host.invite_peer(sid, "guest")
        pending = [item["session_id"] for item in guest.list_invitations()]
        self.assertIn(sid, pending, "Invite Peer after a host remove must deliver a new pending invitation")
        joined = guest.accept_invitation(sid)
        self.assertEqual(joined["players"]["guest"]["status"], m.P_JOINED)
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_confirm_launch_after_host_remove_does_not_start_the_game(self):
        hub, nodes = make_swarm(self.tmp + "-remove-start", open_ports=(55435,))
        for node in nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
            node.repo.games["snes"][0]["players"] = "1-4"
            node.orch.library.invalidate()
        host, guest = nodes["host"].orch, nodes["guest"].orch
        sid = host.create_session({
            "system": "snes", "game_id": "g1", "peers": ["guest"], "max_players": 4, **CORE,
        })["id"]
        guest.accept_invitation(sid)
        guest.set_ready(sid, True)
        host.set_ready(sid, True)
        host.start_session(sid)
        self.assertTrue(guest.get_session(sid)["pending_launch"])
        host.remove_player(sid, "guest")
        try:
            guest.confirm_launch(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message)
        self.assertEqual(nodes["guest"].launcher.launched, [],
                         "removing a player withdraws membership; their old launch offer must not start a game")


if __name__ == "__main__":
    unittest.main()
