"""Consent, hostile snapshots and resource races on a simulated swarm."""
import threading
from datetime import timedelta
from unittest import mock

from app.multiplayer import models as m
from app.multiplayer.store import REPLAY_LIMIT, replica_view
from test_multiplayer_orchestrator import SwarmCase


class HardeningTests(SwarmCase):
    def message(self, **values):
        return {"message_id": m.new_message_id(), "issued_at": m.now_iso(), **values}

    def test_a_peer_cannot_reuse_a_locally_hosted_session_id(self):
        sid = self.create()["id"]
        stolen = replica_view(self.h.store.require(sid))
        stolen["host_id"] = "guest"
        stolen["players"]["host"]["status"] = m.P_INVITED
        self.assertEqual(self.code(self.h.peer_invite, "guest", self.message(session=stolen)), "session_exists")
        stolen["version"] += 100
        self.assertEqual(self.code(self.h.store.adopt_replica, stolen, "guest", {"role": m.GUEST_ROLE}), "session_exists")
        self.assertTrue(self.h.get_session(sid)["is_host"])

    def test_accept_response_must_match_the_reviewed_invitation(self):
        sid = self.create()["id"]
        original = self.g.network.post_checked
        def altered(*args, **kwargs):
            result = original(*args, **kwargs)
            result["session"]["emulator"]["name"] = "another-emulator"
            return result
        with mock.patch.object(self.g.network, "post_checked", side_effect=altered):
            self.assertEqual(self.code(self.g.accept_invitation, sid), "session_identity_changed")
        self.assertIsNone(self.g.store.get(sid))
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_replica_cannot_change_emulator_version_or_reopen_completed_session(self):
        sid = self.lobby()
        replica = replica_view(self.h.store.require(sid))
        replica["version"] += 100
        replica["emulator"]["version"] = "99.99"
        self.assertEqual(self.code(self.g.store.apply_replica, replica, "host"), "session_identity_changed")
        self.h.end_session(sid)
        replica = replica_view(self.h.store.require(sid))
        replica.update(state=m.RUNNING, version=replica["version"] + 100)
        self.assertEqual(self.code(self.g.store.apply_replica, replica, "host"), "session_closed")

    def test_queued_host_launch_cannot_outlive_end(self):
        sid = self.lobby()
        queued = []
        self.h._spawn = lambda fn, name: queued.append(fn)
        self.h.start_session(sid)
        self.h.end_session(sid)
        for fn in queued:
            fn()
        self.assertEqual(self.nodes["host"].launcher.launched, [])
        self.assertEqual(self.h.guard.pending(), {})
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)

    def test_queued_guest_launch_cannot_outlive_leave(self):
        sid = self.lobby()
        self.h.start_session(sid)
        queued = []
        self.g._spawn = lambda fn, name: queued.append(fn)
        self.g.confirm_launch(sid)
        self.g.leave_session(sid)
        for fn in queued:
            fn()
        self.assertEqual(self.nodes["guest"].launcher.launched, [])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertEqual(self.code(self.g.peer_launch, "host", self.message(session_id=sid, port=55435)), "not_a_member")

    def test_end_waits_for_in_flight_local_launch_then_cleans_up(self):
        sid = self.lobby()
        entered, release, ended = threading.Event(), threading.Event(), threading.Event()
        errors = []
        launch = self.nodes["host"].launcher.launch_game
        def blocking(path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test launch timed out")
            return launch(path)
        self.h._spawn = lambda fn, name: None  # drive the start worker explicitly
        self.h.start_session(sid)
        def stop():
            try:
                self.h.end_session(sid)
            except Exception as error:
                errors.append(error)
            finally:
                ended.set()
        with mock.patch.object(self.nodes["host"].launcher, "launch_game", side_effect=blocking):
            worker = threading.Thread(target=self.h.run_start, args=(sid,))
            worker.start()
            self.assertTrue(entered.wait(5))
            stopper = threading.Thread(target=stop)
            stopper.start()
            try:
                self.assertFalse(ended.wait(0.05))
            finally:
                release.set()
                worker.join(5)
                stopper.join(5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(self.nodes["host"].launcher.active)
        self.assertEqual(self.h.guard.pending(), {})

    def test_replay_pressure_does_not_evict_an_unexpired_message(self):
        now = m.now_iso()
        for index in range(REPLAY_LIMIT):
            self.h.store.check_replay("guest", str(index), now)
        self.assertEqual(self.code(self.h.store.check_replay, "guest", "overflow", now), "message_rate_limit")
        self.assertEqual(self.h.store.check_replay("guest", "0", now), "duplicate")
        self.assertEqual(self.h.store.check_replay("other", "0", now), "ok")

    def test_overlapping_sessions_cannot_own_the_same_configuration_key(self):
        path = self.h.settings.batocera_conf_file
        first, second = m.new_session_id(), m.new_session_id()
        edit = [{"key": "global.test", "value": "one"}]
        self.h.guard.apply(first, path, "kv", edit)
        self.assertEqual(self.code(self.h.guard.apply, second, path, "kv", edit), "config_in_use")
        self.h.guard.restore(first)
        self.assertNotIn("global.test", path.read_text())

    def test_a_local_result_does_not_promote_imported_reports(self):
        args = dict(system="snes", title_key="kart", title="Kart", mechanism=m.RETROARCH_NETPLAY, emulator_version="1.19")
        self.h.compat.record_session_result(**args, config_fp="aa" * 8, success=True)
        self.g.compat.import_shared(self.h.compat.export_shareable(), "host", "Host")
        self.g.compat.record_session_result(**args, config_fp="bb" * 8, success=True)
        verdict = self.g.compat.evaluate("snes", "kart", m.RETROARCH_NETPLAY, "aa" * 8, m.EXPERIMENTAL)
        self.assertNotEqual(verdict["classification"], m.VERIFIED_COMPATIBLE)

    def test_sampled_matches_do_not_hide_different_rom_content(self):
        sid = self.create()["id"]
        game = self.g.library.get("snes", "g1")
        path = self.g.library.absolute_path(game)
        path.write_bytes(b"different content despite identical sampled metadata")
        self.assertEqual(self.code(self.g.accept_invitation, sid), "cannot_join")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])

    def test_selected_content_hash_cache_invalidates_when_the_file_changes(self):
        game = self.h.library.get("snes", "g1")
        first = self.h.library.content_hash(game)
        self.assertEqual(first, self.h.library.content_hash(game))
        self.h.library.absolute_path(game).write_bytes(b"updated")
        self.assertNotEqual(first, self.h.library.content_hash(game))

    def test_configuration_reports_are_specific_to_rom_revision(self):
        game = self.h.library.get("snes", "g1")
        adapter = self.h.adapter_map["retroarch"]
        emulator = {"name": "libretro", "core": "snes9x", "version": "1.19", "core_version": "1.62"}
        first = adapter.validation_fingerprint(game, emulator)
        self.assertNotEqual(first, adapter.validation_fingerprint({**game, "fingerprint": "different-revision"}, emulator))

    def test_no_guest_launch_removes_the_host_session_credential(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.h.store.set_local(sid, launch_deadline=(m.now() - timedelta(seconds=1)).isoformat())
        self.h.evaluate_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.FAILED)
        self.assertNotIn("secret", self.h.store.require(sid))

    def test_end_never_kills_a_replacement_game_with_the_same_path(self):
        sid = self.running()
        self.h.store.set_local(sid, launched_pid=100)
        self.nodes["host"].launcher.active["pid"] = 200
        self.h.end_session(sid)
        self.assertEqual(self.nodes["host"].launcher.exited, 0)

    def test_unknown_lan_game_is_not_automatically_recommended(self):
        from app.multiplayer.mechanism import choose, evaluate_candidates
        assessment = self.h.adapter_map["lan"].assess_game({"system": "ps2", "players": 4})
        candidates = evaluate_candidates([assessment], [])
        self.assertEqual(choose(candidates)["mechanism"], "")
        self.assertEqual(choose(candidates, forced=m.EMULATED_LAN)["mechanism"], m.EMULATED_LAN)

    def test_ppsspp_cannot_claim_launch_automation_without_inspected_launcher(self):
        self.assertEqual(self.h.adapter_map["ppsspp"].effective_automation(), "manual")

    def test_controller_privacy_blocks_streaming_invitation_acceptance(self):
        snapshot = self.create()["id"]
        game = self.h.store.require(snapshot)
        game["mechanism"] = m.REMOTE_STREAMING
        game["emulator"]["adapter"] = "streaming"
        self.g.cfg.update({"privacy": {"remote_controller_authorization": "disabled"}})
        verdict = self.g.validate_for_join(game)
        self.assertTrue(any("turned off" in item for item in verdict["problems"]))

    def test_guest_cleans_up_if_a_terminal_publish_was_lost(self):
        sid = self.running()
        self.h._spawn = lambda fn, name: None  # lose the host's final snapshot
        self.h.end_session(sid)
        self.assertFalse(self.g.get_session(sid)["local"]["ended"])
        self.g.tick()
        self.assertTrue(self.g.get_session(sid)["local"]["ended"])
        self.assertEqual(self.g.guard.pending(), {})
        self.assertIsNotNone(self.nodes["guest"].launcher.active)

    def test_tailscale_preference_is_used_for_gameplay_address_selection(self):
        self.h.cfg.update({"networking": {"prefer_tailscale": True}})
        self.assertEqual(self.h.preferred_transport(), "tailnet")
        self.h.cfg.update({"networking": {"transport": "lan"}})
        self.assertEqual(self.h.preferred_transport(), "lan")

    def test_generated_retroarch_profile_does_not_retain_the_join_credential(self):
        sid = self.lobby()
        session = self.h.store.require(sid)
        context = self.h.launch_context(session, m.HOST_ROLE, port=55435)
        edits = self.h.adapter_map["retroarch"].configure_networking(session, m.HOST_ROLE, context)
        self.h.apply_edits(sid, edits)
        generated = self.h.settings.userdata_root / "system/configs/retroarch/retroarchcustom.cfg"
        self.assertFalse(generated.exists(), "watching configgen must not create or overwrite its profile")
        generated.parent.mkdir(parents=True)
        generated.write_text('video_driver = "gl"\nnetplay_password = "' + session["secret"] + '"\n')
        self.h.end_session(sid)
        self.assertNotIn(session["secret"], generated.read_text())
        self.assertIn('video_driver = "gl"', generated.read_text())
        self.assertEqual(self.h.guard.pending(), {})

    def test_nonfinite_fractional_and_oversized_numeric_strings_are_structured_errors(self):
        for value in (float("inf"), float("nan"), 2.5, "1" * 10000):
            self.assertEqual(self.code(m.clean_int, value, "port", 1, 65535), "invalid_value")
