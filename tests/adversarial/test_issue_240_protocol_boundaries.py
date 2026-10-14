"""Issue #240 adversarial boundaries: untrusted peer input, membership, replay.

Expected behaviour, derived from the issue:

* Coordination uses the existing authenticated swarm channel. The caller is the
  certificate identity; a body field cannot impersonate another Drone.
* Unpaired machines cannot touch sessions. Session membership is required.
* Guests dial the host address they already use for that peer, never an address
  from a launch message. Peer-supplied paths, commands, and ROM names never
  reach a subprocess or config key.
* Replays, stale timestamps, and completed sessions cannot be reapplied.
* Session secrets never appear in views, invitations, diagnostics, or audit.
* Malformed identifiers, ports, and secrets are structured errors with no
  side effects.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from app.multiplayer.store import replica_view
from multiplayer_support import SNES_GAME, make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}


class SwarmCase(unittest.TestCase):
    names = ("host", "guest", "mallory")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.hub, self.nodes = make_swarm(self.tmp, self.names, open_ports=(55435,))
        self.h = self.nodes["host"].orch
        self.g = self.nodes["guest"].orch
        self.m = self.nodes["mallory"].orch if "mallory" in self.nodes else None

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code

    def message(self, **values):
        return {"message_id": m.new_message_id(), "issued_at": m.now_iso(), **values}

    def lobby(self):
        session = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.g.accept_invitation(session["id"])
        self.g.set_ready(session["id"], True)
        self.h.set_ready(session["id"], True)
        return session["id"]


class ProtocolBoundaryTests(SwarmCase):
    def test_launch_payload_host_address_path_and_command_are_ignored(self):
        sid = self.lobby()
        self.g.network.host_address = lambda peer, preferred="auto": "192.0.2.55"
        self.h.start_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        hostile = self.message(
            session_id=sid,
            port=55435,
            secret="abc;rm -rf /",
            host_address="198.51.100.9",
            game_path="/etc/passwd",
            rom_name='evil"; id',
            command="id",
            emulator="bash -c id",
            core="../../etc/passwd",
        )
        self.g.peer_launch("host", hostile)
        self.g.confirm_launch(sid)
        launched = self.nodes["guest"].launcher.launched
        self.assertEqual(len(launched), 1)
        self.assertTrue(launched[0].endswith(SNES_GAME["file"]))
        self.assertNotIn("/etc/passwd", launched[0])
        conf = self.nodes["guest"].conf_text
        self.assertIn("192.0.2.55", conf)
        self.assertNotIn("198.51.100.9", conf)
        self.assertNotIn("/etc/passwd", conf)
        self.assertNotIn("bash", conf)
        self.assertNotIn("rm -rf", conf)
        self.assertNotIn("abc;rm", conf)

    def test_non_host_cannot_offer_a_launch_or_push_a_snapshot(self):
        sid = self.lobby()
        snapshot = replica_view(self.h.store.require(sid))
        snapshot.update(version=snapshot["version"] + 50, state=m.RUNNING)
        self.assertEqual(self.code(self.g.peer_launch, "mallory", self.message(session_id=sid, port=55435)),
                         "session_not_found")
        self.assertEqual(self.code(self.g.peer_update, "mallory", self.message(session=snapshot)), "not_session_host")
        self.assertEqual(self.code(self.g.peer_invite, "mallory", self.message(session=snapshot)), "not_session_host")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_body_identity_cannot_impersonate_a_member(self):
        sid = self.lobby()
        payload = self.message(session_id=sid, action="leave", source_drone_id="guest")
        self.assertEqual(self.code(self.h.peer_respond, "mallory", payload), "identity_mismatch")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "joined")

    def test_unpaired_and_non_member_cannot_mutate(self):
        sid = self.lobby()
        payload = self.message(session_id=sid, action="leave")
        self.assertEqual(self.code(self.h.peer_respond, "mallory", payload), "not_a_member")
        self.assertEqual(self.code(self.h.peer_respond, "stranger", payload), "not_paired")
        self.assertEqual(self.code(self.h.invite_peer, sid, "stranger"), "not_paired")
        self.h.end_session(sid)
        self.assertEqual(self.code(self.h.create_session, {"system": "snes", "game_id": "g1", "peers": ["host"], **CORE}),
                         "invalid_peer_id")

    def test_replay_and_stale_messages_do_not_reapply(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        message = self.message(session_id=sid, action="ready")
        self.h.peer_respond("guest", message)
        version = self.h.get_session(sid)["version"]
        again = self.h.peer_respond("guest", message)
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(self.h.get_session(sid)["version"], version)
        from datetime import timedelta
        stale = self.message(session_id=sid, action="leave", issued_at=(m.now() - timedelta(hours=3)).isoformat())
        self.assertEqual(self.code(self.h.peer_respond, "guest", stale), "stale_message")

    def test_completed_session_cannot_be_resurrected(self):
        sid = self.lobby()
        self.h.end_session(sid)
        replica = replica_view(self.h.store.require(sid))
        replica.update(state=m.RUNNING, version=replica["version"] + 99)
        self.assertEqual(self.code(self.g.store.apply_replica, replica, "host"), "session_closed")
        self.assertEqual(self.code(self.h.start_session, sid), "session_started")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_malformed_launch_ports_and_session_ids_are_refused(self):
        sid = self.lobby()
        self.h.start_session(sid)
        for port in (0, -1, 65536, "22;id", True, 22.5, "1" * 40):
            with self.subTest(port=port):
                self.assertEqual(self.code(self.g.peer_launch, "host", self.message(session_id=sid, port=port)),
                                 "invalid_value")
        self.assertEqual(self.code(self.g.peer_launch, "host", self.message(session_id="../etc", port=55435)),
                         "invalid_session_id")
        self.assertEqual(self.code(self.g.confirm_launch, "not-an-id"), "invalid_session_id")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_views_invitations_diagnostics_and_audit_never_contain_the_secret(self):
        sid = self.lobby()
        self.h.start_session(sid)
        secret = self.h.store.get(sid)["secret"]
        self.assertTrue(secret)
        blobs = [
            repr(self.h.get_session(sid)),
            repr(self.g.get_session(sid)),
            repr(self.h.diagnostics(sid)),
            repr(self.h.overview()),
            repr(self.g.list_invitations(include_closed=True)),
            repr(self.nodes["host"].alerts),
            repr(self.nodes["guest"].alerts),
        ]
        for blob in blobs:
            self.assertNotIn(secret, blob)
        self.assertNotIn("secret", self.g.store.get(sid))
        self.h.end_session(sid)
        self.assertNotIn("secret", self.h.store.get(sid))

    def test_guest_uses_its_own_rom_name_not_the_hosts(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        guest = self.nodes["guest"]
        guest.repo.games["snes"][0]["file"] = "Mario Kart (Europe).sfc"
        (guest.root / "roms" / "snes" / "Mario Kart (Europe).sfc").write_bytes(b"rom")
        self.g.library.invalidate()
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertIn('snes["Mario Kart (Europe).sfc"].netplay.mode=client', guest.conf_text)
        self.assertNotIn("Super Mario Kart (USA)", guest.conf_text)
        self.assertTrue(guest.launcher.launched[0].endswith("Mario Kart (Europe).sfc"))

    def test_hostile_emulator_tokens_in_an_invitation_are_rejected(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        snapshot = replica_view(self.h.store.get(sid))
        for field, value in (("core", "snes9x;id"), ("adapter", "retroarch\nglobal.x=1"), ("name", "a b")):
            forged = {**snapshot, "emulator": {**snapshot["emulator"], field: value}}
            with self.subTest(field=field):
                self.assertEqual(
                    self.code(self.g.peer_invite, "host",
                              self.message(invite_expires_at=m.now_iso(), session=forged)),
                    "invalid_identifier",
                )
        invitations = self.g.list_invitations()
        self.assertEqual(len(invitations), 1)
        self.assertEqual(invitations[0]["session_id"], sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_create_rejects_path_traversal_and_command_shaped_input(self):
        for body in (
            {"system": "../etc", "game_id": "g1"},
            {"system": "snes", "game_id": "g1;rm"},
            {"system": "snes", "game_id": "g1", "mechanism": "ROOT_SHELL"},
            {"system": "snes", "game_id": "g1", "peers": ["guest;id"]},
            {"system": "snes", "game_id": "g1", "emulator_override": {"core": "snes9x;rm"}},
        ):
            with self.subTest(body=body):
                with self.assertRaises(m.MultiplayerError):
                    self.h.create_session(body)

    def test_joined_guest_rejects_a_host_swap_of_game_or_mechanism(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        current = replica_view(self.h.store.get(sid))
        for patch in (
            {"game": {**current["game"], "rom_name": "../../../etc/shadow"}},
            {"game": {**current["game"], "fingerprint": "ff" * 16}},
            {"emulator": {**current["emulator"], "core": "evilcore"}},
            {"mechanism": m.REMOTE_STREAMING},
        ):
            forged = {**current, **patch, "version": current["version"] + 10}
            with self.subTest(patch=list(patch)):
                self.assertIn(
                    self.code(self.g.peer_update, "host", self.message(session=forged)),
                    ("session_identity_changed", "invalid_game", "invalid_identifier"),
                )


class IllegalTransitionTests(SwarmCase):
    names = ("host", "guest")

    def test_start_is_refused_from_inviting_and_waiting(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.assertEqual(self.code(self.h.start_session, sid), "players_not_ready")
        self.g.accept_invitation(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.WAITING_FOR_PLAYERS)
        self.assertEqual(self.code(self.h.start_session, sid), "players_not_ready")
        self.assertEqual(self.nodes["host"].launcher.launched, [])

    def test_failed_cannot_jump_to_running(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation is not running"
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertFalse(m.can_transition(m.FAILED, m.RUNNING))
        self.assertEqual(self.code(self.h.start_session, sid), "session_started")


if __name__ == "__main__":
    unittest.main()
