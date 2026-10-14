"""Issue #240 sections 2.2, 3, 7, 8, 9, 11, 13, 14, 16 and 17.

Expected behaviour, derived from the issue before treating the current
implementation as correct:

- Emulator clients dial an address chosen for gameplay, not the control-plane
  route order. With the default transport (auto) and Tailscale preference off,
  a known LAN address is the dial address. A public address is not written
  into the session when a LAN address exists. Tailscale is used when it is
  the only route, or when the user explicitly prefers it.
- An imported peer compatibility report keeps its peer attribution and does
  not turn an untested (UNKNOWN) game into Ready to Play or into an automatic
  mechanism. VERIFIED_COMPATIBLE still requires a confirmed session on this
  machine. Advanced/forced selection remains available.
- A transient failure while checking a launch-success report must not consume
  that report and then fail a guest who is actually running the session game.
  Housekeeping has to observe the guest's real session-game status.
- A status body that omits session-game truth cannot be filled in from
  "some game is running". Peer status is untrusted until the session game
  itself is confirmed.
- A Drone restart during a guest launch restores temporary settings and stops
  the session game this machine had started. It does not close a different
  foreground game.

The simulated swarm does not start emulators or claim real Batocera connectivity.
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
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.matching import browser_status
from app.multiplayer.mechanism import choose, evaluate_candidates
from app.multiplayer.network import NetworkService, route_scope
from app.multiplayer.orchestrator import MultiplayerOrchestrator
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
PUBLIC_IP = "8.8.8.8"
LAN_IP = "192.168.8.20"
TAILNET_IP = "100.64.0.8"


def _install_real_routes(node) -> None:
    """Dial addresses come from NetworkService, not the swarm fake's fixed 10.0.0.x."""
    net = node.network
    net.candidates = lambda peer, _net=net: NetworkService.candidates(_net, peer)
    net.host_address = lambda peer, preferred="auto", _net=net: NetworkService.host_address(_net, peer, preferred)


class GameplayAddressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.assertEqual(route_scope(PUBLIC_IP), "public")
        self.assertEqual(route_scope(LAN_IP), "lan")
        self.assertEqual(route_scope(TAILNET_IP), "tailnet")
        networking = self.g.get_settings()["networking"]
        self.assertEqual(networking["transport"], "auto")
        self.assertFalse(networking["prefer_tailscale"])
        self.assertEqual(self.g.preferred_transport(), "auto")

    def _host_peer(self, **extra):
        peer = {"drone_id": "host", "name": "Drone host", "api_port": 443}
        peer.update(extra)
        self.nodes["guest"].peers = [peer]
        return peer

    def _guest_dial(self):
        _install_real_routes(self.nodes["guest"])
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        session = self.g.store.get(sid)
        context = self.g.launch_context(session, m.GUEST_ROLE, port=55435)
        adapter = self.g.adapter_map["retroarch"]
        self.assertEqual(adapter.effective_automation(), "full")
        edits = adapter.configure_networking(session, m.GUEST_ROLE, context)
        dialed = [edit["value"] for edit in edits if str(edit.get("key", "")).endswith("netplay.server.ip")]
        self.assertEqual(dialed, [context["host_address"]])
        return context["host_address"]

    def test_default_transport_dials_lan_not_the_public_address(self):
        self._host_peer(public_ip=PUBLIC_IP, public_resolvable=True, local_ip=LAN_IP)
        address = self._guest_dial()
        self.assertEqual(address, LAN_IP)
        self.assertNotEqual(address, PUBLIC_IP)

    def test_default_transport_dials_lan_when_tailscale_is_not_preferred(self):
        self._host_peer(public_ip=PUBLIC_IP, public_resolvable=True, local_ip=LAN_IP, tailnet_ip=TAILNET_IP)
        address = self._guest_dial()
        self.assertEqual(address, LAN_IP)
        self.assertNotEqual(address, TAILNET_IP)
        self.assertNotEqual(address, PUBLIC_IP)

    def test_tailnet_is_used_when_it_is_the_only_route(self):
        self._host_peer(tailnet_ip=TAILNET_IP)
        self.assertEqual(self._guest_dial(), TAILNET_IP)

    def test_explicit_tailscale_preference_dials_the_tailnet(self):
        self.g.update_settings({"networking": {"transport": "tailnet"}})
        self.assertEqual(self.g.preferred_transport(), "tailnet")
        self._host_peer(public_ip=PUBLIC_IP, public_resolvable=True, local_ip=LAN_IP, tailnet_ip=TAILNET_IP)
        self.assertEqual(self._guest_dial(), TAILNET_IP)

    def test_explicit_lan_transport_dials_lan(self):
        self.g.update_settings({"networking": {"transport": "lan"}})
        self._host_peer(public_ip=PUBLIC_IP, public_resolvable=True, local_ip=LAN_IP, tailnet_ip=TAILNET_IP)
        self.assertEqual(self._guest_dial(), LAN_IP)

    def test_public_address_is_used_only_when_nothing_else_exists(self):
        self._host_peer(public_ip=PUBLIC_IP, public_resolvable=True)
        self.assertEqual(self._guest_dial(), PUBLIC_IP)


class UnverifiedCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name)
        self.h = self.nodes["host"].orch
        game = {"id": "dos1", "file": "Commander Keen.zip", "title": "Commander Keen",
                "fingerprint": "cd" * 16, "size": 2000}
        host = self.nodes["host"]
        host.repo.games["dos"] = [game]
        rom_dir = host.root / "roms" / "dos"
        rom_dir.mkdir(parents=True, exist_ok=True)
        (rom_dir / game["file"]).write_bytes(b"rom")
        self.h.library.invalidate()

    def _lan(self):
        page = self.h.list_games({"system": "dos"})
        self.assertEqual(page["total"], 1)
        row = page["games"][0]
        return row, next(item for item in row["mechanisms"] if item["mechanism"] == m.EMULATED_LAN)

    def test_peer_success_does_not_make_an_untested_game_ready(self):
        row, lan = self._lan()
        self.assertEqual(lan["classification"], m.UNKNOWN)
        self.assertEqual(row["status"], "untested")
        self.assertEqual(self.h.list_games({"system": "dos", "status": "ready"})["total"], 0)
        game = self.h.library.get("dos", "dos1")
        assessment = next(item for item in self.h.assessor.assess(game) if item["mechanism"] == m.EMULATED_LAN)
        self.assertEqual(assessment["classification"], m.UNKNOWN)
        fingerprint = assessment["config_fingerprint"]
        imported = self.h.compat.import_shared([{
            "system": "dos",
            "gameId": game["title_key"],
            "title": game["title"],
            "maximumPlayers": 2,
            "mechanisms": {m.EMULATED_LAN: {"tested": {fingerprint: {"successes": 1, "failures": 0}}}},
        }], "guest", "Drone guest")
        self.assertEqual(imported["added"], 1)
        stored = self.h.compat.get("dos", game["title_key"])
        self.assertTrue(str(stored.get("source", "")).startswith("peer:"))
        again = next(item for item in self.h.assessor.assess(game) if item["mechanism"] == m.EMULATED_LAN)
        self.assertNotEqual(again["classification"], m.VERIFIED_COMPATIBLE)
        self.assertNotEqual(again["classification"], m.LIKELY_COMPATIBLE)
        self.assertEqual(again["classification"], m.UNKNOWN)
        self.assertEqual(browser_status([again]), "untested")
        peer = {"name": "guest", "device_id": "guest", "reachable": True, "scope": "lan", "level": "title",
                "latency_ms": 5, "assessments": {m.EMULATED_LAN: {
                    "classification": m.UNKNOWN, "readiness": "manual_steps_required",
                    "readiness_message": "", "reasons": ["This game's LAN requirements have not been recorded."]}}}
        decision = choose(evaluate_candidates([again], [peer], streaming_enabled=True))
        self.assertEqual(decision["mechanism"], "")
        self.assertEqual(choose(evaluate_candidates([again], [peer]), forced=m.EMULATED_LAN)["mechanism"], m.EMULATED_LAN)
        listed, listed_lan = self._lan()
        self.assertEqual(listed["status"], "untested")
        self.assertEqual(listed_lan["classification"], m.UNKNOWN)
        self.assertEqual(self.h.list_games({"system": "dos", "status": "ready"})["total"], 0)


class LaunchVerificationTests(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2031, 6, 4, 16, 0, tzinfo=timezone.utc)
        clock = mock.patch.object(m, "now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def _started(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        return sid

    def test_transient_verification_failure_does_not_fail_a_running_guest(self):
        sid = self._started()
        real_get = self.h.network.get
        calls = {"status": 0}

        def flaky(peer, endpoint, timeout=5.0):
            if str(endpoint).split("?", 1)[0] == "/status" and calls["status"] == 0:
                calls["status"] += 1
                raise OSError("status check timed out")
            return real_get(peer, endpoint, timeout=timeout)

        self.h.network.get = flaky
        self.g.confirm_launch(sid)
        self.assertGreaterEqual(calls["status"], 1)
        guest_path = self.nodes["guest"].launcher.active["rom_path"]
        self.assertTrue(self.g.session_game_running(self.g.store.get(sid)))
        status = self.g.peer_status("host", sid)
        self.assertTrue(status["session_game_running"])
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        self.clock += timedelta(seconds=121)
        self.h.tick()
        session = self.h.get_session(sid)
        player = session["players"]["guest"]
        self.assertNotEqual(player["status"], m.P_FAILED)
        self.assertNotIn("did not confirm", (player.get("error") or "").lower())
        self.assertEqual(player["status"], m.P_RUNNING)
        self.assertEqual(session["state"], m.RUNNING)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], guest_path)

    def test_omitted_session_game_flag_is_not_treated_as_participation(self):
        sid = self._started()
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.hub.offline.add("guest")
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_UNREACHABLE)
        self.hub.offline.clear()
        active = dict(self.nodes["guest"].launcher.active)
        active["rom_path"] = str(active["rom_path"]) + ".other-title"
        active["name"] = "Some Other Game"
        active["pid"] = 9091
        self.nodes["guest"].launcher.active = active
        real_get = self.h.network.get

        def omit_session_game(peer, endpoint, timeout=5.0):
            if str(endpoint).split("?", 1)[0] == "/status":
                return {"device_id": "guest", "enabled": True, "game_running": True,
                        "session": {"known": True, "state": m.RUNNING, "version": 1}}
            return real_get(peer, endpoint, timeout=timeout)

        self.h.network.get = omit_session_game
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertNotEqual(session["players"]["guest"]["status"], m.P_RUNNING)
        self.assertNotEqual(session["state"], m.RUNNING)
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], active["rom_path"])

    def test_explicit_false_session_game_flag_does_not_restore_participation(self):
        sid = self._started()
        self.g.confirm_launch(sid)
        self.hub.offline.add("guest")
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_UNREACHABLE)
        self.hub.offline.clear()
        real_get = self.h.network.get

        def deny_session_game(peer, endpoint, timeout=5.0):
            if str(endpoint).split("?", 1)[0] == "/status":
                return {"device_id": "guest", "enabled": True, "game_running": True, "session_game_running": False,
                        "session": {"known": True, "state": m.DEGRADED, "version": 1}}
            return real_get(peer, endpoint, timeout=timeout)

        self.h.network.get = deny_session_game
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["guest"]["status"], m.P_UNREACHABLE)
        self.assertNotEqual(session["state"], m.RUNNING)


class GuestRestartCleanupTests(unittest.TestCase):
    def _prepared(self, *, session_game_in_foreground):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _hub, nodes = make_swarm(temporary.name, open_ports=(55435,))
        host, guest = nodes["host"], nodes["guest"]
        for node in nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        original = guest.conf_text
        sid = host.orch.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        guest.orch.accept_invitation(sid)
        session = guest.orch.store.get(sid)
        path = session["local"]["game_path"]
        self.assertTrue(path)
        adapter = guest.orch.adapter_map["retroarch"]
        context = guest.orch.launch_context(session, m.GUEST_ROLE, port=55435, secret="join-secret")
        guest.orch.apply_edits(sid, adapter.configure_networking(session, m.GUEST_ROLE, context))
        self.assertNotEqual(guest.conf_text, original)
        guest.orch.store.set_local(sid, launch_status="launching")
        foreground = path if session_game_in_foreground else str(Path(path).with_name("Other Game.sfc"))
        guest.launcher.active = {"rom_path": foreground, "name": Path(foreground).stem, "system": "snes",
                                  "emulator": "libretro", "pid": 4242}
        return guest, sid, original, path, foreground

    def _recover(self, guest):
        reborn = MultiplayerOrchestrator(
            guest.settings, guest.repo, probe=guest.probe, launcher=guest.launcher, network=guest.network,
            paired_peers=lambda: list(guest.peers), spawn=lambda function, _name: function(), sleep=lambda _seconds: None,
            audit=lambda *_args, **_kwargs: None, bell=lambda *_args, **_kwargs: None,
        )
        return reborn.recover()

    def test_restart_during_guest_launch_stops_the_session_game_and_restores_settings(self):
        guest, _sid, original, path, foreground = self._prepared(session_game_in_foreground=True)
        self.assertEqual(foreground, path)
        summary = self._recover(guest)
        self.assertEqual(summary["failed"], 1)
        self.assertGreaterEqual(guest.launcher.exited, 1)
        self.assertTrue(guest.launcher.active is None or guest.launcher.active.get("rom_path") != path)
        self.assertEqual(guest.conf_text, original)

    def test_restart_during_guest_launch_does_not_close_a_different_game(self):
        guest, _sid, original, _path, foreground = self._prepared(session_game_in_foreground=False)
        summary = self._recover(guest)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(guest.launcher.exited, 0)
        self.assertEqual(guest.launcher.active["rom_path"], foreground)
        self.assertEqual(guest.conf_text, original)


if __name__ == "__main__":
    unittest.main()
