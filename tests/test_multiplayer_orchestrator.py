"""Orchestrator integration over a SIMULATED swarm (in-process Drones, fake machines, fake launcher).

What this proves: session coordination, the state machine, authorization, replay/stale-update
protection, cleanup and recovery behave as designed. What it does NOT prove: that a real emulator
actually connects. That needs two real Batocera machines (see docs/multiplayer.md).
"""

import tempfile
import unittest
from datetime import timedelta

from app.multiplayer import models as m
from app.multiplayer.lifecycle import HOST_LOST_AFTER
from app.multiplayer.orchestrator import MultiplayerOrchestrator
from app.multiplayer.store import replica_view
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

    def create(self, peers=("guest",), **extra):
        return self.h.create_session({"system": "snes", "game_id": "g1", "peers": list(peers), **CORE, **extra})

    def lobby(self):
        """Host session with the guest joined and everyone ready."""
        session = self.create()
        self.g.accept_invitation(session["id"])
        self.g.set_ready(session["id"], True)
        self.h.set_ready(session["id"], True)
        return session["id"]

    def running(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        return sid

    def code(self, call, *args, **kwargs):
        with self.assertRaises(m.MultiplayerError) as caught:
            call(*args, **kwargs)
        return caught.exception.code


class FullFlowTests(SwarmCase):
    def test_host_invites_guest_accepts_and_both_launch_then_everything_is_restored(self):
        host_conf_before, guest_conf_before = self.nodes["host"].conf_text, self.nodes["guest"].conf_text
        session = self.create()
        self.assertEqual(session["state"], m.INVITING)
        self.assertEqual(session["mechanism"], m.RETROARCH_NETPLAY)
        self.assertIn("RetroArch Netplay", session["mechanism_reason"])
        self.assertEqual([i["status"] for i in self.g.list_invitations()], ["pending"])
        self.assertEqual(self.nodes["guest"].launcher.launched, [], "an invitation must never launch anything")

        joined = self.g.accept_invitation(session["id"])
        self.assertEqual(joined["players"]["guest"]["status"], "joined")
        self.assertEqual(self.h.get_session(session["id"])["state"], m.WAITING_FOR_PLAYERS)
        self.assertEqual(self.g.set_ready(session["id"], True)["players"]["guest"]["ready"], True)
        self.assertEqual(self.h.get_session(session["id"])["state"], m.WAITING_FOR_PLAYERS)
        self.assertEqual(self.h.set_ready(session["id"], True)["state"], m.READY)

        started = self.h.start_session(session["id"])
        self.assertIn(started["state"], (m.STARTING,))
        host_launch = self.nodes["host"].launcher.launched
        self.assertEqual(len(host_launch), 1)
        self.assertIn('netplay.mode=host', self.nodes["host"].conf_text)
        self.assertIn('.core=snes9x', self.nodes["host"].conf_text)
        # The guest is offered the launch but nothing runs until its own user confirms.
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        view = self.g.get_session(session["id"])
        self.assertTrue(view["pending_launch"])
        self.assertIn("launch", view["actions"])

        self.g.confirm_launch(session["id"])
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        guest_conf = self.nodes["guest"].conf_text
        self.assertIn("netplay.mode=client", guest_conf)
        self.assertIn("netplay.server.port=55435", guest_conf)
        self.assertEqual(self.h.get_session(session["id"])["state"], m.RUNNING)
        self.assertEqual(self.g.get_session(session["id"])["state"], m.RUNNING)

        ended = self.h.end_session(session["id"])
        self.assertEqual(ended["state"], m.COMPLETED)
        self.assertEqual(self.g.get_session(session["id"])["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].conf_text, host_conf_before)
        self.assertEqual(self.nodes["guest"].conf_text, guest_conf_before)
        self.assertEqual(self.nodes["host"].launcher.exited, 1, "the host closes the game it started")
        self.assertIsNotNone(self.nodes["guest"].launcher.active, "a remote end must not close the guest's game")
        self.assertEqual(self.h.guard.pending(), {})
        self.assertEqual(self.g.guard.pending(), {})

    def test_session_views_and_audit_never_leak_the_join_secret(self):
        sid = self.running()
        secret = self.h.store.get(sid)["secret"]
        self.assertTrue(secret)
        for orchestrator in (self.h, self.g):
            blob = repr(orchestrator.get_session(sid)) + repr(orchestrator.diagnostics(sid)) + repr(orchestrator.overview())
            self.assertNotIn(secret, blob)
        self.assertNotIn(secret, repr(self.nodes["host"].alerts) + repr(self.nodes["guest"].alerts))
        self.assertNotIn("secret", self.g.store.get(sid))
        self.h.end_session(sid)
        self.assertNotIn("secret", self.h.store.get(sid))

    def test_player_slots_can_be_reassigned_and_swap(self):
        sid = self.create()["id"]
        self.g.accept_invitation(sid)
        swapped = self.h.assign_slot(sid, "guest", 1)
        self.assertEqual((swapped["players"]["guest"]["slot"], swapped["players"]["host"]["slot"]), (1, 2))
        self.assertEqual(self.code(self.h.assign_slot, sid, "guest", 3), "invalid_slot")
        self.assertEqual(self.g.get_session(sid)["players"]["guest"]["slot"], 1)

    def test_idempotent_create_and_session_limit(self):
        first = self.create(client_request_id="req-1")
        again = self.create(client_request_id="req-1")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.h.list_sessions()), 1)
        self.assertEqual(self.code(self.create), "too_many_sessions")

    def test_readiness_is_frozen_once_started(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.code(self.g.set_ready, sid, False), "session_started")

    def test_invite_after_creation_and_capacity(self):
        sid = self.create(peers=())["id"]
        self.assertEqual(self.h.get_session(sid)["state"], m.WAITING_FOR_PLAYERS)
        view = self.h.invite_peer(sid, "guest")
        self.assertEqual(view["players"]["guest"]["status"], "invited")
        self.assertEqual(len(self.g.list_invitations()), 1)
        self.assertEqual(self.h.invite_peer(sid, "guest")["players"]["guest"]["status"], "invited", "re-inviting is idempotent")
        self.assertEqual(len(self.g.list_invitations()), 1)

    def test_players_can_be_removed_and_are_told(self):
        sid = self.lobby()
        removed = self.h.remove_player(sid, "guest")
        self.assertEqual(removed["players"]["guest"]["status"], "removed")
        self.assertEqual(self.h.get_session(sid)["state"], m.WAITING_FOR_PLAYERS)
        self.assertTrue(self.g.get_session(sid)["local"]["ended"])
        self.assertEqual(self.code(self.h.remove_player, sid, "host"), "cannot_remove_host")

    def test_games_browser_reports_status_and_peer_copies(self):
        page = self.h.list_games({"system": "snes", "include_peers": "1"})
        game = page["games"][0]
        self.assertEqual(game["players"], 2)
        self.assertEqual(game["status"], "ready")  # no core set: installed snes9x is used for the session only
        self.assertEqual([peer["name"] for peer in game["peers"]], ["Drone guest"])
        self.assertEqual(game["peers"][0]["level"], "exact")
        self.assertEqual(self.h.list_games({"system": "snes", "status": "ready"})["total"], 1)
        self.assertEqual(self.h.list_games({"system": "snes", "status": "configuration"})["total"], 0)
        self.assertEqual(self.h.list_games({"system": "snes", "peer": "guest"})["total"], 1)
        self.assertEqual(self.h.list_games({"system": "snes", "players": "4"})["total"], 0)
        self.assertEqual(self.h.list_games({"system": "snes", "q": "kart"})["total"], 1)
        self.assertEqual(self.code(self.h.list_games, {"system": "snes", "status": "bogus"}), "invalid_filter")
        shared = self.h.shared_games()
        self.assertEqual(shared[0]["title"], "Super Mario Kart")


class AuthorizationTests(SwarmCase):
    names = ("host", "guest", "mallory")

    def test_only_the_host_can_use_host_actions(self):
        sid = self.lobby()
        for call, args in ((self.g.start_session, (sid,)), (self.g.invite_peer, (sid, "mallory")), (self.g.remove_player, (sid, "host")),
                           (self.g.assign_slot, (sid, "guest", 2)), (self.g.retry_connection, (sid,))):
            with self.subTest(call=call.__name__):
                self.assertEqual(self.code(call, *args), "not_session_host")

    def test_guest_end_is_a_leave_not_a_session_kill(self):
        sid = self.lobby()
        self.g.leave_session(sid)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "left")
        self.assertNotEqual(self.h.get_session(sid)["state"], m.COMPLETED)

    def test_unpaired_or_non_member_peers_cannot_touch_a_session(self):
        sid = self.lobby()
        payload = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": sid, "action": "leave"}
        # mallory is paired with the swarm but is not a member of this session
        self.assertEqual(self.code(self.h.peer_respond, "mallory", payload), "not_a_member")
        # a Drone that is not paired at all is refused before anything else
        self.assertEqual(self.code(self.h.peer_respond, "stranger", {**payload, "message_id": m.new_message_id()}), "not_paired")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "joined")

    def test_identity_claims_in_the_body_cannot_impersonate_another_peer(self):
        sid = self.lobby()
        payload = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": sid, "action": "leave", "source_drone_id": "guest"}
        self.assertEqual(self.code(self.h.peer_respond, "mallory", payload), "identity_mismatch")

    def test_only_the_host_may_push_updates_or_invitations(self):
        sid = self.lobby()
        forged = replica_view(self.h.store.get(sid))
        forged.update(version=999, state=m.COMPLETED)
        message = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": forged}
        self.assertEqual(self.code(self.g.peer_update, "mallory", message), "not_session_host")
        invite = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": forged}
        self.assertEqual(self.code(self.g.peer_invite, "mallory", invite), "not_session_host")
        launch = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": sid, "port": 55435, "secret": "x" * 8}
        self.assertEqual(self.code(self.g.peer_launch, "mallory", launch), "session_not_found")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_replayed_messages_do_not_apply_twice(self):
        sid = self.create()["id"]
        self.g.accept_invitation(sid)
        message = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": sid, "action": "ready"}
        first = self.h.peer_respond("guest", message)
        version = self.h.get_session(sid)["version"]
        again = self.h.peer_respond("guest", message)
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(self.h.get_session(sid)["version"], version)
        self.assertNotIn("duplicate", first)

    def test_stale_and_future_dated_messages_are_refused(self):
        sid = self.create()["id"]
        for stamp in ((m.now() - timedelta(hours=2)).isoformat(), (m.now() + timedelta(hours=2)).isoformat()):
            message = {"message_id": m.new_message_id(), "issued_at": stamp, "session_id": sid, "action": "leave"}
            self.assertEqual(self.code(self.h.peer_respond, "guest", message), "stale_message")

    def test_stale_snapshots_cannot_roll_a_guest_back(self):
        sid = self.lobby()
        old = replica_view(self.h.store.get(sid))
        self.h.set_ready(sid, False)
        current = self.g.get_session(sid)["version"]
        message = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": {**old, "version": current - 1}}
        self.assertEqual(self.g.peer_update("host", message), {"applied": False, "reason": "stale"})
        self.assertEqual(self.g.get_session(sid)["version"], current)

    def test_malformed_peer_input_is_rejected_without_side_effects(self):
        for payload in ({}, {"message_id": "zz", "issued_at": m.now_iso()}, {"message_id": m.new_message_id(), "issued_at": "yesterday"},
                        {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": "x"}, []):
            with self.subTest(payload=payload), self.assertRaises(m.MultiplayerError):
                self.g.peer_invite("host", payload)
        self.assertEqual(self.g.list_invitations(), [])

    def test_host_cannot_force_a_launch_on_a_guest(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.g.get_session(sid)["local"]["launch_status"], "awaiting_confirmation")

    def test_invitation_permissions(self):
        self.g.update_settings({"privacy": {"invite_permission": "none"}})
        refused = self.create(client_request_id="refused")
        self.assertIn("Could not deliver", refused["players"]["guest"]["error"])
        self.assertEqual(self.g.list_invitations(), [])
        self.h.end_session(refused["id"])
        self.g.update_settings({"privacy": {"invite_permission": "allowlist", "allowed_inviters": ["mallory"]}})
        session = self.create()
        self.assertEqual(session["players"]["guest"]["error"].startswith("Could not deliver"), True)
        self.assertEqual(self.g.list_invitations(), [])
        self.g.update_settings({"privacy": {"allowed_inviters": ["host"]}})
        self.h.retry_connection(session["id"])
        self.assertEqual(len(self.g.list_invitations()), 1)

    def test_invitations_can_be_switched_off_and_multiplayer_disabled(self):
        self.g.update_settings({"general": {"invitations_enabled": False}})
        self.assertTrue(self.create()["players"]["guest"]["error"])
        self.h.update_settings({"general": {"enabled": False}})
        self.assertEqual(self.code(self.h.list_games, {"system": "snes"}), "multiplayer_disabled")
        self.assertEqual(self.code(self.h.create_session, {"system": "snes", "game_id": "g1"}), "multiplayer_disabled")

    def test_expired_invitations_cannot_be_accepted(self):
        sid = self.create()["id"]
        self.g.store.update_invitation(sid, expires_at=(m.now() - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.code(self.g.accept_invitation, sid), "invitation_expired")
        self.assertEqual(self.g.list_invitations(), [])

    def test_declined_invitations_stay_declined(self):
        sid = self.create()["id"]
        self.assertEqual(self.g.decline_invitation(sid)["status"], "declined")
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "declined")
        self.assertEqual(self.code(self.g.accept_invitation, sid), "invitation_closed")

    def test_auto_accept_joins_the_lobby_but_never_launches(self):
        self.g.update_settings({"general": {"auto_accept_invitations": True}})
        sid = self.create()["id"]
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "joined")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.h.start_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_automatic_launch_is_opt_in_and_never_replaces_a_running_game(self):
        self.g.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(len(self.nodes["guest"].launcher.launched), 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)

    def test_automatic_launch_waits_for_the_user_when_a_game_is_running(self):
        self.g.update_settings({"privacy": {"local_launch_authorization": "auto"}})
        self.nodes["guest"].launcher.active = {"rom_path": "/other.sfc", "name": "other", "system": "snes", "emulator": "libretro"}
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)


class PeerDataIsUntrustedTests(SwarmCase):
    names = ("host", "guest", "mallory")

    def test_hostile_emulator_identifiers_in_a_snapshot_are_rejected(self):
        sid = self.create()["id"]
        snapshot = replica_view(self.h.store.get(sid))
        for field, value in (("core", "../../etc/passwd"), ("adapter", "retroarch\nglobal.x=1"), ("name", "a b;c")):
            forged = {**snapshot, "emulator": {**snapshot["emulator"], field: value}}
            message = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "invite_expires_at": m.now_iso(), "session": forged}
            with self.subTest(field=field), self.assertRaises(m.MultiplayerError) as caught:
                self.g.peer_invite("host", message)
            self.assertEqual(caught.exception.code, "invalid_identifier")

    def test_guest_keys_use_its_own_file_name_when_content_matches(self):
        sid = self.create()["id"]
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

    def test_a_host_cannot_change_the_game_or_emulator_under_a_joined_guest(self):
        sid = self.create()["id"]
        self.g.accept_invitation(sid)
        current = replica_view(self.h.store.get(sid))
        for patch in ({"game": {**current["game"], "rom_name": '../../../etc/shadow".x=1'}}, {"game": {**current["game"], "fingerprint": "ff" * 16}},
                      {"emulator": {**current["emulator"], "core": "evilcore"}}, {"mechanism": m.REMOTE_STREAMING}):
            forged = {**current, **patch, "version": current["version"] + 10}
            message = {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session": forged}
            with self.subTest(patch=list(patch)), self.assertRaises(m.MultiplayerError) as caught:
                self.g.peer_update("host", message)
            self.assertIn(caught.exception.code, ("session_identity_changed", "invalid_game"))
        self.assertEqual(self.g.get_session(sid)["game"]["rom_name"], SNES_GAME["file"])

    def test_adding_a_player_cannot_silently_switch_the_mechanism_of_a_joined_group(self):
        for name in ("host", "guest"):
            self.nodes[name].repo.games["snes"][0]["players"] = "1-4"
        self.g.library.invalidate()
        self.h.library.invalidate()
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], "max_players": 4, **CORE})["id"]
        self.g.accept_invitation(sid)
        # Everyone could now stream, but mallory lacks the game: native play is ruled out for the enlarged group.
        self.nodes["host"].probe.binaries["sunshine"] = "/usr/bin/sunshine"
        self.nodes["host"].probe.processes.add("sunshine")
        self.nodes["guest"].probe.binaries["moonlight"] = "/usr/bin/moonlight"
        self.nodes["mallory"].probe.binaries["moonlight"] = "/usr/bin/moonlight"
        self.nodes["mallory"].repo.games["snes"] = []
        for node in self.nodes.values():
            node.orch.library.invalidate()
            for adapter in node.orch.adapters:
                adapter.refresh()
        self.assertEqual(self.code(self.h.invite_peer, sid, "mallory"), "mechanism_change")
        self.assertEqual(self.h.get_session(sid)["mechanism"], m.RETROARCH_NETPLAY)
        self.assertNotIn("mallory", self.h.get_session(sid)["players"])


class GameReplacementTests(SwarmCase):
    other = {"rom_path": "/other.sfc", "name": "other", "system": "snes", "emulator": "libretro"}

    def test_host_must_approve_replacing_a_running_game(self):
        sid = self.lobby()
        self.nodes["host"].launcher.active = dict(self.other)
        self.assertEqual(self.code(self.h.start_session, sid), "game_running")
        self.assertEqual(self.nodes["host"].launcher.exited, 0)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        self.h.start_session(sid, replace_running_game=True)
        self.assertEqual(self.nodes["host"].launcher.exited, 1)
        self.assertEqual(len(self.nodes["host"].launcher.launched), 1)

    def test_guest_must_approve_replacing_a_running_game(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.nodes["guest"].launcher.active = dict(self.other)
        self.assertEqual(self.code(self.g.confirm_launch, sid), "game_running")
        self.assertEqual(self.nodes["guest"].launcher.exited, 0)
        self.g.confirm_launch(sid, replace_running_game=True)
        self.assertEqual(self.nodes["guest"].launcher.exited, 1)

    def test_an_existing_copy_requires_consent_and_relaunch_with_network_settings(self):
        sid = self.lobby()
        path = self.h.store.get(sid)["local"]["game_path"]
        self.nodes["host"].launcher.active = {"rom_path": path, "name": "k", "system": "snes", "emulator": "libretro"}
        self.assertEqual(self.code(self.h.start_session, sid), "game_running")
        self.assertEqual(self.nodes["host"].launcher.exited, 0)
        self.h.start_session(sid, replace_running_game=True)
        self.assertEqual(self.nodes["host"].launcher.exited, 1)


class FailureAndRecoveryTests(SwarmCase):
    def test_a_host_launch_failure_fails_the_session_and_cleans_up(self):
        before = self.nodes["host"].conf_text
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "EmulationStation is not running"
        view = self.h.start_session(sid)
        final = self.h.get_session(sid)
        self.assertEqual(final["state"], m.FAILED)
        self.assertIn("EmulationStation", final["error"])
        self.assertEqual(self.nodes["host"].conf_text, before)
        self.assertEqual(self.g.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.h.guard.pending(), {})
        issues = self.h.compat.get("snes", m.normalize_title(SNES_GAME["file"]))["knownIssues"]
        self.assertIn("EmulationStation", issues[0]["detail"])
        self.assertNotIn("secret", self.h.store.get(sid))

    def test_a_game_that_never_starts_times_out_with_a_clear_error(self):
        sid = self.lobby()
        self.nodes["host"].launcher.start_never_happens = True
        self.h.start_session(sid)
        self.assertIn("did not start", self.h.get_session(sid)["error"])

    def test_host_not_listening_fails_before_any_guest_is_asked(self):
        self.nodes["host"].probe.open_ports.clear()
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.g.get_session(sid)["local"]["launch_status"], "")

    def test_a_guest_that_cannot_launch_degrades_not_kills_the_session(self):
        sid = self.lobby()
        self.nodes["guest"].launcher.fail_launch = "boom"
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "failed")
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)  # the only guest failed: nobody to play with
        guest_view = self.g.get_session(sid)
        self.assertTrue(guest_view["local"]["ended"])
        self.assertIn("boom", guest_view["local"]["launch_error"])
        self.assertNotIn("netplay", self.nodes["guest"].conf_text, "a failed guest launch restores its settings")

    def test_retry_resets_a_failed_session_with_a_new_credential_and_is_bounded(self):
        sid = self.lobby()
        self.nodes["host"].launcher.fail_launch = "boom"
        self.h.start_session(sid)
        self.nodes["host"].launcher.fail_launch = None
        old_secret = self.h.store.get(sid).get("secret")
        view = self.h.retry_connection(sid)
        self.assertEqual(view["state"], m.WAITING_FOR_PLAYERS)
        self.assertFalse(view["players"]["guest"]["ready"])
        self.assertFalse(view["players"]["host"]["ready"])
        self.assertTrue(self.h.store.get(sid)["secret"])
        self.assertNotEqual(self.h.store.get(sid)["secret"], old_secret)
        for _ in range(m.MAX_PLAYERS_LIMIT):
            try:
                self.h.retry_connection(sid)
            except m.MultiplayerError as error:
                self.assertEqual(error.code in ("retry_limit", "nothing_to_retry"), True)
                break

    def test_a_vanished_host_ends_the_guests_session_and_restores_its_settings(self):
        before = self.nodes["guest"].conf_text
        sid = self.running()
        self.hub.offline.add("host")
        self.g.tick()
        self.assertTrue(self.g.get_session(sid)["local"]["host_unreachable"])
        self.g.store.set_local(sid, host_unreachable_since=(m.now() - HOST_LOST_AFTER - timedelta(seconds=5)).isoformat())
        self.g.tick()
        view = self.g.get_session(sid)
        self.assertTrue(view["local"]["ended"])
        self.assertIn("host became unavailable", view["local"]["ended_reason"])
        self.assertEqual(self.nodes["guest"].conf_text, before)
        self.assertIsNotNone(self.nodes["guest"].launcher.active, "the guest's game is left for the user to close")

    def test_a_vanished_guest_degrades_the_session_and_can_recover(self):
        sid = self.running()
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.hub.offline.add("guest")
        self.h.tick()
        self.h.tick()
        view = self.h.get_session(sid)
        self.assertEqual(view["players"]["guest"]["status"], "unreachable")
        self.assertEqual(view["state"], m.DEGRADED)
        self.hub.offline.clear()
        self.h.store.set_local(sid, last_seen={"guest": (m.now() - timedelta(minutes=5)).isoformat()})
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "running")

    def test_the_hosts_game_exiting_completes_the_session(self):
        before = self.nodes["host"].conf_text
        sid = self.running()
        self.nodes["host"].launcher.active = None
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING, "one missed check is not enough")
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].conf_text, before)

    def test_a_guests_game_exiting_makes_the_guest_leave(self):
        sid = self.running()
        self.nodes["guest"].launcher.active = None
        self.g.tick()
        self.g.tick()
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], "left")
        self.assertTrue(self.g.get_session(sid)["local"]["ended"])

    def test_expired_sessions_and_invitations_are_cleaned_up(self):
        sid = self.create()["id"]
        self.h.store.mutate(sid, lambda s: s.update(expires_at=(m.now() - timedelta(minutes=1)).isoformat()))
        self.g.store.update_invitation(sid, expires_at=(m.now() - timedelta(minutes=1)).isoformat())
        self.g.tick()
        self.assertEqual(self.g.list_invitations(include_closed=True)[0]["status"], "expired")
        self.h.tick()
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)
        self.assertEqual(self.h.guard.pending(), {})

    def test_restart_while_starting_fails_the_session_and_restores_settings(self):
        before = self.nodes["host"].conf_text
        sid = self.lobby()
        self.nodes["host"].launcher.start_never_happens = True
        self.nodes["host"].launcher.fail_launch = None
        # Simulate a crash mid-start: configuration applied, state STARTING, process gone.
        session = self.h.store.get(sid)
        adapter = self.h.adapter_map["retroarch"]
        context = self.h.launch_context(session, m.HOST_ROLE, port=55435)
        self.h.apply_edits(sid, adapter.configure_networking(session, m.HOST_ROLE, context))
        self.h.store.mutate(sid, lambda s: s.update(state=m.STARTING))
        self.assertNotEqual(self.nodes["host"].conf_text, before)
        reborn = MultiplayerOrchestrator(self.nodes["host"].settings, self.nodes["host"].repo, probe=self.nodes["host"].probe,
                                         launcher=self.nodes["host"].launcher, network=self.nodes["host"].network,
                                         paired_peers=lambda: list(self.nodes["host"].peers), spawn=lambda f, n: f(), sleep=lambda s: None,
                                         audit=lambda *a, **k: None, bell=lambda *a, **k: None)
        summary = reborn.recover()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(reborn.get_session(sid)["state"], m.FAILED)
        self.assertEqual(self.nodes["host"].conf_text, before)

    def test_restart_with_orphaned_settings_for_a_finished_session(self):
        before = self.nodes["host"].conf_text
        sid = self.running()
        self.h.store.mutate(sid, lambda s: s.update(state=m.STOPPING))
        self.h.store.mutate(sid, lambda s: s.update(state=m.COMPLETED))
        self.assertNotEqual(self.nodes["host"].conf_text, before)
        summary = self.h.recover()
        self.assertEqual(summary["restored"], 1)
        self.assertEqual(self.nodes["host"].conf_text, before)

    def test_restart_resumes_a_live_session_whose_game_is_still_running(self):
        sid = self.running()
        self.assertEqual(self.h.recover()["resumed"], 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.nodes["host"].launcher.active = None
        self.assertEqual(self.h.recover()["ended"], 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)

    def test_ending_twice_is_harmless(self):
        sid = self.running()
        self.h.end_session(sid)
        self.assertEqual(self.h.end_session(sid)["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].launcher.exited, 1)

    def test_turning_off_auto_restore_leaves_a_manual_restore(self):
        before = self.nodes["host"].conf_text
        self.h.update_settings({"general": {"auto_restore_configuration": False}})
        sid = self.running()
        self.h.end_session(sid)
        self.assertNotEqual(self.nodes["host"].conf_text, before)
        self.assertTrue(self.h.diagnostics(sid)["configuration"]["pending_restore"])
        self.h.restore_configuration(sid)
        self.assertEqual(self.nodes["host"].conf_text, before)


class MechanismFlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_no_usable_mechanism_creates_nothing_and_says_why(self):
        hub, nodes = make_swarm(self.tmp, games_for=("host",), open_ports=(55435,))
        host = nodes["host"].orch
        with self.assertRaises(m.MultiplayerError) as caught:
            host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(caught.exception.code, "no_mechanism")
        self.assertIn("guest", caught.exception.message.lower())
        self.assertEqual(host.store.all(), [])

    def test_streaming_is_used_when_only_the_host_has_the_game(self):
        hub, nodes = make_swarm(self.tmp, games_for=("host",), binaries={"retroarch": "/usr/bin/retroarch", "sunshine": "/usr/bin/sunshine",
                                                                         "moonlight": "/usr/bin/moonlight"}, processes={"sunshine"},
                                versions={"/usr/bin/retroarch": "1.19.1"}, open_ports=(55435,))
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})
        self.assertEqual(session["mechanism"], m.REMOTE_STREAMING)
        self.assertEqual(session["max_players"], 2)
        self.assertIn("no native mechanism", session["mechanism_reason"])
        self.assertEqual(session["stream_profile"]["resolution"], "1280x720")
        guest.accept_invitation(session["id"])
        guest.set_ready(session["id"], True)
        host.set_ready(session["id"], True)
        started = host.start_session(session["id"])
        self.assertEqual(host.get_session(session["id"])["state"], m.RUNNING)
        self.assertEqual(len(nodes["host"].launcher.launched), 1)
        self.assertEqual(nodes["guest"].launcher.launched, [], "streaming guests run Moonlight, Drone launches nothing for them")
        self.assertTrue(guest.get_session(session["id"])["my_steps"])
        self.assertNotIn("netplay", nodes["host"].conf_text)

    def test_manual_mechanisms_validate_but_never_launch(self):
        hub, nodes = make_swarm(self.tmp, names=("host", "guest"), games_for=(), binaries={"dolphin-emu": "/usr/bin/dolphin-emu"},
                                versions={"/usr/bin/dolphin-emu": "2506"})
        for name, node in nodes.items():
            node.repo.games["gamecube"] = [{"id": "gc1", "file": "Mario Kart Double Dash.iso", "title": "Mario Kart", "fingerprint": "cd" * 16, "players": "1-4"}]
            (node.root / "roms" / "gamecube").mkdir(parents=True, exist_ok=True)
            (node.root / "roms" / "gamecube" / "Mario Kart Double Dash.iso").write_bytes(b"x")
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "gamecube", "game_id": "gc1", "peers": ["guest"]})
        self.assertEqual(session["mechanism"], m.DOLPHIN_NETPLAY)
        self.assertEqual(session["max_players"], 4)
        joined = guest.accept_invitation(session["id"])
        self.assertTrue(joined["my_steps"])
        guest.set_ready(session["id"], True)
        host.set_ready(session["id"], True)
        host.start_session(session["id"])
        self.assertEqual(host.get_session(session["id"])["state"], m.RUNNING)
        self.assertEqual(nodes["host"].launcher.launched, [])
        self.assertEqual(nodes["guest"].launcher.launched, [])

    def test_dolphin_version_mismatch_blocks_joining_with_the_actionable_message(self):
        hub, nodes = make_swarm(self.tmp, names=("host", "guest"), games_for=(), binaries={"dolphin-emu": "/usr/bin/dolphin-emu"},
                                versions={"/usr/bin/dolphin-emu": "2506"})
        for node in nodes.values():
            node.repo.games["gamecube"] = [{"id": "gc1", "file": "Game.iso", "title": "Game", "fingerprint": "cd" * 16, "players": "1-4"}]
            (node.root / "roms" / "gamecube").mkdir(parents=True, exist_ok=True)
            (node.root / "roms" / "gamecube" / "Game.iso").write_bytes(b"x")
        host, guest = nodes["host"].orch, nodes["guest"].orch
        session = host.create_session({"system": "gamecube", "game_id": "gc1", "peers": ["guest"]})
        nodes["guest"].probe.versions["/usr/bin/dolphin-emu"] = "2412"
        nodes["guest"].orch.adapter_map["dolphin"].refresh()
        with self.assertRaises(m.MultiplayerError) as caught:
            guest.accept_invitation(session["id"])
        self.assertEqual(caught.exception.code, "cannot_join")
        self.assertIn("incompatible Dolphin versions", caught.exception.message)
        self.assertEqual(host.get_session(session["id"])["players"]["guest"]["status"], "invited")

    def test_forcing_an_unusable_mechanism_is_refused(self):
        hub, nodes = make_swarm(self.tmp, open_ports=(55435,))
        with self.assertRaises(m.MultiplayerError) as caught:
            nodes["host"].orch.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], "mechanism": "REMOTE_STREAMING", **CORE})
        self.assertEqual(caught.exception.code, "mechanism_unusable")


class ResultRecordingTests(SwarmCase):
    def test_a_confirmed_session_verifies_only_that_configuration(self):
        sid = self.running()
        self.h.end_session(sid)
        self.h.record_result(sid, True)
        game = self.h.list_games({"system": "snes"})["games"][0]
        # Verified needs this exact emulator/core/version: the Batocera-selected core path has none recorded.
        record = self.h.compat.get("snes", m.normalize_title(SNES_GAME["file"]))
        self.assertEqual(record["source"], "local")
        self.assertEqual(self.code(self.h.record_result, sid, True), "already_recorded")
        assessments = self.h.assessor.assess(self.h.library.games("snes")[0], core_override="snes9x")
        self.assertEqual(assessments[0]["classification"], m.VERIFIED_COMPATIBLE)
        self.assertEqual(assessments[0]["compatibility_basis"], "confirmed-session")

    def test_a_session_that_never_ran_has_no_result_to_record(self):
        sid = self.create()["id"]
        self.assertEqual(self.code(self.h.record_result, sid, True), "never_ran")


if __name__ == "__main__":
    unittest.main()
