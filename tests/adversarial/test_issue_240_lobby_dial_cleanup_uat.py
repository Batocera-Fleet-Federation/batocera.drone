"""Issue #240 sections 2.3, 7, 9, 10, 11, 13, 16 and 17.

Expected behaviour, derived from the issue before treating the implementation
as correct:

- Assigning a player slot never drops another active player out of the slot
  map. An invited peer who does not have a slot yet can still be placed, and
  the host keeps a distinct slot inside the session capacity.
- Gameplay dials a usable address. A known LAN address is used when one
  exists, even if an earlier mDNS name fails to resolve or resolves to a
  public address. The control-plane route order is not the gameplay address.
- A paired peer's compatibility report is attribution. It must not turn this
  machine's own technical classification into INCOMPATIBLE. A failure counted
  on this machine for that same configuration still does.
- A launch that has already started the session game, and then fails, stops
  that process and restores temporary settings. The join password journaled
  for a generated emulator profile is removed even when that file appears
  only after the first restore looked for it.

The simulated swarm does not start emulators or claim real Batocera connectivity.
""",
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.network import NetworkService
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}
LAN_IP = "192.168.8.20"
PUBLIC_IP = "8.8.8.8"
NAME = "arcade.local"


def _install_real_routes(node) -> None:
    net = node.network
    net.candidates = lambda peer, _net=net: NetworkService.candidates(_net, peer)
    net.host_address = lambda peer, preferred="auto", _net=net: NetworkService.host_address(_net, peer, preferred)


class SlotAssignmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _hub, nodes = make_swarm(temporary.name)
        self.h = nodes["host"].orch

    def _invited(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        session = self.h.get_session(sid)
        self.assertEqual(session["players"]["host"]["slot"], 1)
        self.assertIsNone(session["players"]["guest"]["slot"])
        self.assertEqual(session["players"]["guest"]["status"], m.P_INVITED)
        self.assertGreaterEqual(session["max_players"], 2)
        return sid

    def test_free_slot_assignment_keeps_every_player_placed(self):
        sid = self._invited()
        view = self.h.assign_slot(sid, "guest", 2)
        self.assertEqual(view["players"]["guest"]["slot"], 2)
        self.assertEqual(view["players"]["host"]["slot"], 1)
        self.assertEqual(len({view["players"]["host"]["slot"], view["players"]["guest"]["slot"]}), 2)

    def test_displacing_the_host_keeps_a_unique_slot_for_both_players(self):
        sid = self._invited()
        view = self.h.assign_slot(sid, "guest", 1)
        host_slot = view["players"]["host"]["slot"]
        guest_slot = view["players"]["guest"]["slot"]
        self.assertEqual(guest_slot, 1)
        self.assertIsInstance(host_slot, int)
        self.assertNotEqual(host_slot, guest_slot)
        self.assertGreaterEqual(host_slot, 1)
        self.assertLessEqual(host_slot, view["max_players"])
        occupied = [player["slot"] for player in view["players"].values()
                    if player["status"] not in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED)]
        self.assertNotIn(None, occupied)
        self.assertEqual(len(occupied), len(set(occupied)))


class GameplayDialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch

    def _peer(self, **extra):
        peer = {"drone_id": "host", "name": "Drone host", "api_port": 443, "local_ip": LAN_IP}
        peer.update(extra)
        self.nodes["guest"].peers = [peer]
        return peer

    def _dial(self, resolver):
        _install_real_routes(self.nodes["guest"])
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        session = self.g.store.get(sid)
        context_box = {}

        def resolve(host, *args, **kwargs):
            return resolver(host)

        with mock.patch("app.multiplayer.network.socket.getaddrinfo", side_effect=resolve):
            context = self.g.launch_context(session, m.GUEST_ROLE, port=55435)
            context_box["address"] = context["host_address"]
            edits = self.g.adapter_map["retroarch"].configure_networking(session, m.GUEST_ROLE, context)
        dialed = [edit["value"] for edit in edits if str(edit.get("key", "")).endswith("netplay.server.ip")]
        self.assertEqual(dialed, [context_box["address"]])
        return context_box["address"]

    def test_literal_lan_address_is_dialed_without_dns(self):
        self._peer()
        seen = []

        def resolver(host):
            seen.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]

        self.assertEqual(self._dial(resolver), LAN_IP)
        self.assertEqual(seen, [])

    def test_public_resolution_of_an_mdns_name_does_not_hide_the_lan_address(self):
        peer = self._peer(reachable_url=f"https://{NAME}")
        _install_real_routes(self.nodes["guest"])
        hosts = [urlparse(item).hostname for item in self.nodes["guest"].network.candidates(peer)]
        self.assertIn(NAME, hosts)
        self.assertIn(LAN_IP, hosts)

        def resolver(host):
            if host == NAME:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]
            raise OSError(host)

        address = self._dial(resolver)
        self.assertEqual(address, LAN_IP)
        self.assertNotEqual(address, PUBLIC_IP)
        self.assertNotEqual(address, NAME)

    def test_unresolvable_mdns_name_does_not_hide_the_lan_address(self):
        self._peer(reachable_url=f"https://{NAME}")

        def resolver(host):
            raise OSError(host)

        address = self._dial(resolver)
        self.assertEqual(address, LAN_IP)
        self.assertNotEqual(address, NAME)


class PeerReportClassificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _hub, nodes = make_swarm(temporary.name)
        self.h = nodes["host"].orch
        self.game = self.h.library.get("snes", "g1")
        self.before = self._retroarch()
        self.assertEqual(self.before["classification"], m.LIKELY_COMPATIBLE)
        self.fingerprint = self.before["config_fingerprint"]

    def _retroarch(self):
        # The session path selects a core. A bare library row has none, so the
        # technical class would be REQUIRES_CONFIGURATION before any peer report.
        assessed = self.h.assessor.assess(self.game, core_override="snes9x")
        return next(item for item in assessed if item["mechanism"] == m.RETROARCH_NETPLAY)

    def _import_peer(self, successes, failures):
        imported = self.h.compat.import_shared([{
            "system": "snes",
            "gameId": self.game["title_key"],
            "title": self.game["title"],
            "maximumPlayers": 2,
            "mechanisms": {m.RETROARCH_NETPLAY: {"tested": {self.fingerprint: {
                "successes": successes, "failures": failures}}}},
        }], "guest", "Drone guest")
        self.assertEqual(imported["added"], 1)
        stored = self.h.compat.get("snes", self.game["title_key"])
        self.assertTrue(str(stored.get("source", "")).startswith("peer:"))

    def test_peer_failures_do_not_reclassify_a_local_assessment(self):
        self._import_peer(successes=0, failures=3)
        again = self._retroarch()
        self.assertEqual(again["classification"], self.before["classification"])
        self.assertNotEqual(again["classification"], m.INCOMPATIBLE)
        self.assertNotEqual(again["compatibility_basis"], "failed-sessions")
        peer = {"name": "guest", "device_id": "guest", "reachable": True, "scope": "lan", "level": "exact",
                "latency_ms": 5, "assessments": {m.RETROARCH_NETPLAY: {
                    "classification": again["classification"], "readiness": again["readiness"],
                    "readiness_message": "", "reasons": again["reasons"]}}}
        from app.multiplayer.mechanism import choose, evaluate_candidates
        decision = choose(evaluate_candidates([again], [peer], streaming_enabled=False))
        self.assertEqual(decision["mechanism"], m.RETROARCH_NETPLAY)

    def test_local_failures_still_reclassify_that_configuration(self):
        self.h.compat.record_session_result(
            system="snes", title_key=self.game["title_key"], title=self.game["title"],
            mechanism=m.RETROARCH_NETPLAY, config_fp=self.fingerprint, emulator_version="1.19.1",
            success=False, failure="desync after the join password was accepted")
        again = self._retroarch()
        self.assertEqual(again["classification"], m.INCOMPATIBLE)
        self.assertEqual(again["compatibility_basis"], "failed-sessions")


class PartialLaunchCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": LAN_IP
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.original = {name: node.conf_text for name, node in self.nodes.items()}

    def _ready(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def test_launch_that_starts_the_game_and_then_fails_stops_it_and_restores_settings(self):
        sid = self._ready()
        host = self.nodes["host"]
        path = self.h.store.get(sid)["local"]["game_path"]

        def start_then_fail(game_path):
            host.launcher.launched.append(str(game_path))
            host.launcher.active = {"rom_path": str(game_path), "name": "Super Mario Kart", "system": "snes",
                                     "emulator": "libretro", "pid": 4242}
            raise m.MultiplayerError(
                "es_rejected",
                "EmulationStation started the game, then the launch could not be confirmed.",
                409)

        host.launcher.launch_game = start_then_fail
        self.h.start_session(sid)
        session = self.h.get_session(sid)
        self.assertEqual(session["state"], m.FAILED)
        self.assertGreaterEqual(host.launcher.exited, 1)
        self.assertTrue(host.launcher.active is None or host.launcher.active.get("rom_path") != path)
        self.assertEqual(host.conf_text, self.original["host"])
        self.assertNotIn("netplay.password", host.conf_text)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.g.get_session(sid)["players"]["guest"]["status"], m.P_JOINED)

    def test_generated_password_present_at_restore_is_removed(self):
        host = self.nodes["host"]
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        session = self.h.store.get(sid)
        secret = session["secret"]
        adapter = self.h.adapter_map["retroarch"]
        context = self.h.launch_context(session, m.HOST_ROLE, port=55435, secret=secret)
        self.h.apply_edits(sid, adapter.configure_networking(session, m.HOST_ROLE, context))
        target, lines = self._generated_lines(sid, secret)
        self.assertFalse(target.exists())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        restored = self.h.restore_configuration(sid)
        self.assertEqual(restored["errors"], [])
        self.assertNotIn(secret, target.read_text(encoding="utf-8"))
        self.assertEqual(host.conf_text, self.original["host"])

    def test_generated_password_written_after_a_missed_restore_is_still_removed(self):
        host = self.nodes["host"]
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        session = self.h.store.get(sid)
        secret = session["secret"]
        adapter = self.h.adapter_map["retroarch"]
        context = self.h.launch_context(session, m.HOST_ROLE, port=55435, secret=secret)
        self.h.apply_edits(sid, adapter.configure_networking(session, m.HOST_ROLE, context))
        target, lines = self._generated_lines(sid, secret)
        self.assertFalse(target.exists())
        first = self.h.restore_configuration(sid)
        self.assertEqual(first["errors"], [])
        self.assertEqual(host.conf_text, self.original["host"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertIn(secret, target.read_text(encoding="utf-8"))
        second = self.h.restore_configuration(sid)
        self.assertEqual(second["errors"], [])
        self.assertNotIn(secret, target.read_text(encoding="utf-8"))
        self.assertEqual(host.conf_text, self.original["host"])

    def _generated_lines(self, session_id, secret):
        pending = self.h.guard.pending(session_id)[session_id]
        match = [(path, spec) for path, spec in pending["files"].items() if path.endswith("retroarchcustom.cfg")]
        self.assertEqual(len(match), 1)
        path, spec = match[0]
        lines = [f"{edit['key']}={edit['applied']}" for edit in spec["edits"]]
        self.assertTrue(any(secret in line for line in lines))
        return Path(path), lines


if __name__ == "__main__":
    unittest.main()
