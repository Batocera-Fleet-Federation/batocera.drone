"""Multiplayer foundations: vocabulary/validators, state machine, session store, config guard, settings."""

import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.multiplayer import models as m
from app.multiplayer.config_guard import ConfigGuard, scoped_key
from app.multiplayer.settings_store import MultiplayerSettings
from app.multiplayer.store import SessionStore, public_view, replica_view, sanitize_session


def make_settings(tmp: str, device_id: str = "node-a") -> Settings:
    root = Path(tmp)
    env = {"USERDATA_ROOT": str(root / "userdata"), "ROMS_ROOT": str(root / "roms"), "BIOS_ROOT": str(root / "bios"),
           "SAVES_ROOT": str(root / "saves"), "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"), "DRONE_DEVICE_ID": device_id,
           "BATOCERA_CONF_FILE": str(root / "userdata" / "system" / "batocera.conf")}
    (root / "userdata" / "system").mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


def sample_session(session_id: str = None, host: str = "node-a", state: str = m.CREATED) -> dict:
    session_id = session_id or m.new_session_id()
    now = m.now_iso()
    return {
        "id": session_id, "version": 1, "state": state, "host_id": host, "host_name": "Host", "created_at": now, "updated_at": now,
        "expires_at": (m.now() + timedelta(hours=1)).isoformat(),
        "game": {"system": "snes", "title": "Kart", "rom_name": "Kart.sfc", "fingerprint": "ab" * 16, "size": 5, "unique_id": "g1"},
        "mechanism": m.RETROARCH_NETPLAY, "mechanism_reason": "r", "emulator": {"adapter": "retroarch", "name": "libretro", "core": "snes9x"},
        "max_players": 2,
        "players": {host: {"device_id": host, "name": "Host", "role": "host", "status": "joined", "ready": False, "slot": 1}},
        "events": [], "error": "", "secret": "topsecret", "local": {"role": "host"},
    }


class ModelTests(unittest.TestCase):
    def test_every_state_has_an_entry_and_completed_is_final(self):
        self.assertEqual(set(m.TRANSITIONS), set(m.SESSION_STATES))
        self.assertEqual(m.TRANSITIONS[m.COMPLETED], frozenset())

    def test_illegal_transitions_are_refused(self):
        illegal = [(m.CREATED, m.RUNNING), (m.CREATED, m.STARTING), (m.WAITING_FOR_PLAYERS, m.STARTING), (m.INVITING, m.RUNNING),
                   (m.RUNNING, m.READY), (m.RUNNING, m.CREATED), (m.COMPLETED, m.RUNNING), (m.COMPLETED, m.STOPPING),
                   (m.STOPPING, m.RUNNING), (m.STARTING, m.READY), (m.READY, m.RUNNING), (m.FAILED, m.RUNNING)]
        for current, target in illegal:
            with self.subTest(current=current, target=target):
                self.assertFalse(m.can_transition(current, target))

    def test_legal_transitions(self):
        legal = [(m.CREATED, m.DISCOVERING), (m.DISCOVERING, m.INVITING), (m.INVITING, m.WAITING_FOR_PLAYERS),
                 (m.WAITING_FOR_PLAYERS, m.READY), (m.READY, m.STARTING), (m.STARTING, m.RUNNING), (m.RUNNING, m.DEGRADED),
                 (m.DEGRADED, m.RUNNING), (m.RUNNING, m.STOPPING), (m.STOPPING, m.COMPLETED), (m.STARTING, m.FAILED), (m.FAILED, m.STOPPING)]
        for current, target in legal:
            with self.subTest(current=current, target=target):
                self.assertTrue(m.can_transition(current, target))

    def test_identifier_validation(self):
        self.assertEqual(m.clean_session_id("A" * 32), "a" * 32)
        for bad in ("", "../etc", "z" * 32, "a" * 31, None):
            with self.assertRaises(m.MultiplayerError):
                m.clean_session_id(bad)
        for bad in ("a b", "x;rm", "a" * 200, "", "../x"):
            with self.assertRaises(m.MultiplayerError):
                m.clean_peer_id(bad)
        with self.assertRaises(m.MultiplayerError):
            m.clean_system("snes/../x")
        with self.assertRaises(m.MultiplayerError):
            m.clean_mechanism("ROOT_SHELL")
        self.assertEqual(m.clean_mechanism("remote_streaming"), m.REMOTE_STREAMING)

    def test_bounded_text_and_numbers(self):
        self.assertNotIn("\n", m.clean_text("a\nb\x00c"))
        self.assertEqual(len(m.clean_text("x" * 1000, 50)), 50)
        self.assertEqual(m.clean_nickname("Bob; rm -rf /"), "Bob rm -rf")
        for bad in (True, "x", 99, -1):
            with self.assertRaises(m.MultiplayerError):
                m.clean_int(bad, "n", 0, 10)

    def test_title_key_matches_regional_variants(self):
        self.assertEqual(m.normalize_title("Super Mario Kart (USA).sfc"), m.normalize_title("Super Mario Kart (Europe) [!].zip"))
        self.assertNotEqual(m.normalize_title("Super Mario Kart.sfc"), m.normalize_title("Super Mario World.sfc"))
        self.assertTrue(m.normalize_title("(Demo).sfc"))

    def test_config_fingerprint_is_stable_and_version_sensitive(self):
        a = m.config_fingerprint("X", "libretro", "snes9x", "1.19", "1.62")
        self.assertEqual(a, m.config_fingerprint("x", "LIBRETRO", "snes9x", "1.19", "1.62"))
        self.assertNotEqual(a, m.config_fingerprint("X", "libretro", "snes9x", "1.20", "1.62"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = make_settings(tmp.name)
        self.store = SessionStore(self.settings)

    def test_mutate_bumps_version_only_when_something_changed(self):
        session = self.store.create(sample_session())
        same = self.store.mutate(session["id"], lambda s: None)
        self.assertEqual(same["version"], 1)
        changed = self.store.mutate(session["id"], lambda s: s.update(state=m.DISCOVERING))
        self.assertEqual(changed["version"], 2)

    def test_mutate_enforces_the_state_machine(self):
        session = self.store.create(sample_session())
        with self.assertRaises(m.MultiplayerError) as caught:
            self.store.mutate(session["id"], lambda s: s.update(state=m.RUNNING))
        self.assertEqual(caught.exception.code, "illegal_transition")
        self.assertEqual(self.store.get(session["id"])["state"], m.CREATED)

    def test_expected_version_detects_conflicts(self):
        session = self.store.create(sample_session())
        self.store.mutate(session["id"], lambda s: s.update(state=m.DISCOVERING))
        with self.assertRaises(m.MultiplayerError) as caught:
            self.store.mutate(session["id"], lambda s: s.update(state=m.INVITING), expected_version=1)
        self.assertEqual(caught.exception.code, "version_conflict")

    def test_concurrent_mutations_never_lose_an_update(self):
        session = self.store.create(sample_session())

        def bump(n):
            self.store.mutate(session["id"], lambda s: s["events"].append({"n": n}))
        threads = [threading.Thread(target=bump, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        final = self.store.get(session["id"])
        self.assertEqual(final["version"], 21)
        self.assertEqual(len(final["events"]), 20)

    def test_views_never_expose_the_join_secret(self):
        session = self.store.create(sample_session())
        self.assertNotIn("secret", public_view(session, viewer_id="node-a"))
        self.assertNotIn("secret", replica_view(session))
        self.assertNotIn("local", replica_view(session))

    def test_replica_only_moves_forward_and_only_from_the_host(self):
        guest_store = SessionStore(make_settings(tempfile.mkdtemp(), "guest"))
        base = sample_session(host="host")
        base["players"]["guest"] = {"device_id": "guest", "name": "G", "status": "invited"}
        guest_store.adopt_replica(replica_view(base), "host", {"role": "guest"})
        newer = dict(replica_view(base), version=5, state=m.INVITING)
        self.assertIsNotNone(guest_store.apply_replica(newer, "host"))
        stale = dict(replica_view(base), version=3, state=m.CREATED)
        self.assertIsNone(guest_store.apply_replica(stale, "host"))
        self.assertIsNone(guest_store.apply_replica(newer, "host"))  # duplicate
        self.assertEqual(guest_store.get(base["id"])["version"], 5)
        with self.assertRaises(m.MultiplayerError) as caught:
            guest_store.apply_replica(dict(newer, version=9), "someone-else")
        self.assertEqual(caught.exception.code, "not_session_host")

    def test_replica_for_an_unknown_session_is_rejected(self):
        with self.assertRaises(m.MultiplayerError) as caught:
            self.store.apply_replica(replica_view(sample_session(host="host")), "host")
        self.assertEqual(caught.exception.code, "session_not_found")

    def test_replica_cannot_overwrite_a_session_this_drone_hosts(self):
        own = self.store.create(sample_session(host="node-a"))
        with self.assertRaises(m.MultiplayerError):
            self.store.apply_replica(dict(replica_view(own), version=99), "node-a")

    def test_sanitize_rebuilds_snapshots_field_by_field(self):
        raw = replica_view(sample_session(host="host"))
        raw["injected"] = {"$ne": 1}
        raw["game"]["rom_name"] = "x\n.sfc"
        raw["players"]["host"]["name"] = "<b>" + "A" * 500
        clean = sanitize_session(raw)
        self.assertNotIn("injected", clean)
        self.assertNotIn("\n", clean["game"]["rom_name"])
        self.assertLessEqual(len(clean["players"]["host"]["name"]), 80)

    def test_stream_profile_is_reduced_to_a_fixed_shape(self):
        raw = replica_view(sample_session(host="host"))
        raw["stream_profile"] = {"resolution": "1280x720; rm", "fps": "60", "bitrate_kbps": 8000, "encoder": "<x>", "evil": {"deep": ["x"] * 1000}}
        profile = sanitize_session(raw)["stream_profile"]
        self.assertEqual(set(profile), {"resolution", "fps", "bitrate_kbps", "encoder", "hardware_encoder_available", "note"})
        self.assertEqual((profile["resolution"], profile["fps"], profile["bitrate_kbps"], profile["encoder"]), ("1280x720", 0, 8000, "software"))

    def test_sanitize_rejects_malformed_snapshots(self):
        base = replica_view(sample_session(host="host"))
        for patch in ({"state": "HACKED"}, {"version": "x"}, {"version": -1}, {"id": "../etc"}, {"host_id": "a b"}, {"mechanism": "ROOT"},
                      {"max_players": 99}, {"game": {"system": "../x", "rom_name": "a"}}):
            with self.subTest(patch=patch), self.assertRaises(m.MultiplayerError):
                sanitize_session({**base, **patch})
        with self.assertRaises(m.MultiplayerError):
            sanitize_session("not a dict")

    def test_replay_window(self):
        now = m.now_iso()
        self.assertEqual(self.store.check_replay("p", "a" * 16, now), "ok")
        self.assertEqual(self.store.check_replay("p", "a" * 16, now), "duplicate")
        self.assertEqual(self.store.check_replay("q", "a" * 16, now), "ok")  # keyed per caller
        old = (m.now() - timedelta(hours=1)).isoformat()
        future = (m.now() + timedelta(hours=1)).isoformat()
        for stamp in (old, future, "not-a-time", None):
            with self.subTest(stamp=stamp), self.assertRaises(m.MultiplayerError):
                self.store.check_replay("p", "b" * 16, stamp)

    def test_history_is_bounded_and_deduplicated(self):
        session = sample_session()
        for _ in range(3):
            self.store.archive(session, "completed")
        self.assertEqual(len(self.store.history()), 1)


class ConfigGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = make_settings(tmp.name)
        self.guard = ConfigGuard(self.settings)
        self.conf = self.root / "userdata" / "system" / "batocera.conf"
        self.conf.write_text("# user config\nsystem.language=en_US\nsnes.core=bsnes\n", encoding="utf-8")
        self.sid = m.new_session_id()

    def edits(self, **values):
        return [{"key": key, "value": value} for key, value in values.items()]

    def test_apply_and_restore_round_trip_keeps_unrelated_lines(self):
        original = self.conf.read_text()
        self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": "snes9x", "snes.netplay.mode": "host"}))
        text = self.conf.read_text()
        self.assertIn("snes.core=snes9x", text)
        self.assertIn("snes.netplay.mode=host", text)
        self.assertIn("system.language=en_US", text)
        result = self.guard.restore(self.sid)
        self.assertEqual((result["restored"], result["errors"]), (2, []))
        self.assertEqual(self.conf.read_text(), original)
        self.assertEqual(self.guard.pending(), {})

    def test_a_key_the_user_changed_afterwards_is_left_alone(self):
        self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": "snes9x"}))
        self.conf.write_text(self.conf.read_text().replace("snes.core=snes9x", "snes.core=mesen-s"), encoding="utf-8")
        result = self.guard.restore(self.sid)
        self.assertEqual(result["left_alone"], 1)
        self.assertIn("snes.core=mesen-s", self.conf.read_text())

    def test_restore_survives_a_simulated_crash_between_journal_and_cleanup(self):
        self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.netplay.mode": "client"}))
        recovered = ConfigGuard(self.settings)  # a fresh process only has the persisted journal
        self.assertIn(self.sid, recovered.pending())
        recovered.restore(self.sid)
        self.assertNotIn("netplay", self.conf.read_text())

    def test_failed_verification_rolls_back(self):
        original = self.conf.read_text()
        # The write is silently dropped (e.g. the file is replaced underneath us): verification must catch it.
        with mock.patch.object(ConfigGuard, "_atomic_write", staticmethod(lambda path, text: None)):
            with self.assertRaises(m.MultiplayerError) as caught:
                self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": "snes9x"}))
        self.assertEqual(caught.exception.code, "config_verify_failed")
        self.assertEqual(self.conf.read_text(), original)
        self.assertEqual(self.guard.pending(), {})

    def test_values_cannot_inject_extra_lines_or_keys(self):
        for value in ("a\nglobal.audio.volume=0", "a\r\nb", "x" * 600):
            with self.subTest(value=value), self.assertRaises(m.MultiplayerError):
                self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": value}))
        for key in ("a=b", "bad key", "x\ny", "../../x", ""):
            with self.subTest(key=key), self.assertRaises(m.MultiplayerError):
                self.guard.apply(self.sid, self.conf, "kv", [{"key": key, "value": "1"}])
        self.assertEqual(self.conf.read_text().count("\n"), 3)

    def test_scoped_keys_reject_hostile_rom_names(self):
        self.assertEqual(scoped_key("snes", "Game (USA).sfc", "netplay.mode"), 'snes["Game (USA).sfc"].netplay.mode')
        for name in ('a".b', "a\nb", "a\\b", "a=b", ""):
            with self.subTest(name=name), self.assertRaises(m.MultiplayerError):
                scoped_key("snes", name, "netplay.mode")

    def test_targets_outside_userdata_or_through_symlinks_are_refused(self):
        outside = self.root / "outside.conf"
        outside.write_text("a=1\n")
        with self.assertRaises(m.MultiplayerError):
            self.guard.apply(self.sid, outside, "kv", self.edits(**{"snes.core": "x"}))
        link = self.root / "userdata" / "system" / "link.conf"
        link.symlink_to(outside)
        with self.assertRaises(m.MultiplayerError):
            self.guard.apply(self.sid, link, "kv", self.edits(**{"snes.core": "x"}))
        self.assertEqual(outside.read_text(), "a=1\n")

    def test_missing_file_is_not_created(self):
        with self.assertRaises(m.MultiplayerError) as caught:
            self.guard.apply(self.sid, self.root / "userdata" / "system" / "missing.conf", "kv", self.edits(**{"snes.core": "x"}))
        self.assertEqual(caught.exception.code, "config_missing")

    def test_ini_sections_are_edited_in_place_and_restored(self):
        ini = self.root / "userdata" / "system" / "ppsspp.ini"
        ini.write_text("[General]\nFirstRun = False\n[Network]\nEnableWlan = False\nProAdhocServer = socom.cc\n[Graphics]\nX = 1\n", encoding="utf-8")
        original = ini.read_text()
        self.guard.apply(self.sid, ini, "ini", [{"section": "Network", "key": "EnableWlan", "value": "True"},
                                                {"section": "Network", "key": "ProAdhocServer", "value": "10.0.0.2"}])
        text = ini.read_text()
        self.assertIn("EnableWlan = True", text)
        self.assertIn("ProAdhocServer = 10.0.0.2", text)
        self.assertIn("[Graphics]\nX = 1", text)
        self.guard.restore(self.sid)
        self.assertEqual(ini.read_text(), original)

    def test_ini_missing_section_is_refused(self):
        ini = self.root / "userdata" / "system" / "x.ini"
        ini.write_text("[General]\nA = 1\n")
        with self.assertRaises(m.MultiplayerError):
            self.guard.apply(self.sid, ini, "ini", [{"section": "Network", "key": "EnableWlan", "value": "True"}])
        self.assertEqual(ini.read_text(), "[General]\nA = 1\n")

    def test_second_apply_to_the_same_key_keeps_the_true_original(self):
        self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": "first"}))
        self.guard.apply(self.sid, self.conf, "kv", self.edits(**{"snes.core": "second"}))
        self.guard.restore(self.sid)
        self.assertIn("snes.core=bsnes", self.conf.read_text())


class SettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = MultiplayerSettings(make_settings(tmp.name))

    def test_safe_defaults(self):
        config = self.store.get()
        self.assertFalse(config["general"]["auto_accept_invitations"])
        self.assertEqual(config["privacy"]["local_launch_authorization"], "confirm")
        self.assertTrue(config["general"]["auto_restore_configuration"])
        self.assertEqual(config["networking"]["relay_provider"], "none")

    def test_update_validates_and_persists(self):
        updated = self.store.update({"general": {"max_simultaneous_sessions": 2}, "streaming": {"max_resolution": "1920x1080"}})
        self.assertEqual(updated["general"]["max_simultaneous_sessions"], 2)
        self.assertEqual(self.store.get()["streaming"]["max_resolution"], "1920x1080")

    def test_rejects_unknown_and_out_of_range_values(self):
        for patch in ({"nope": {}}, {"general": {"nope": 1}}, {"general": {"enabled": "yes"}}, {"general": {"max_simultaneous_sessions": 99}},
                      {"general": {"preferred_mechanism": "ROOT"}}, {"networking": {"relay_provider": "evil"}}, {"networking": {"port_range_start": 80}},
                      {"streaming": {"max_resolution": "9999x9999"}}, {"privacy": {"invite_permission": "everyone"}},
                      {"privacy": {"allowed_inviters": ["a b"]}}, {"emulators": {"core_overrides": {"snes": "a b"}}}, "text"):
            with self.subTest(patch=patch), self.assertRaises(m.MultiplayerError):
                self.store.update(patch)

    def test_port_range_must_be_ordered(self):
        with self.assertRaises(m.MultiplayerError):
            self.store.update({"networking": {"port_range_start": 60000, "port_range_end": 55000}})


if __name__ == "__main__":
    unittest.main()
