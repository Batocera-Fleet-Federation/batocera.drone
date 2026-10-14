"""#240 §§7,10,13,17: evidence describes the launched config; recovery isolates faults.

Expected invariants are from the issue: confirming a session cannot verify settings
that were never played, and an emulator shutdown/configuration backend failure
cannot skip independent recovery or leave a launch permanently pending. Emulators
and peer transport are simulated; configuration files and SQLite are real temp data.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import make_swarm


class LaunchEvidenceRecoveryUAT(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (
            mock.patch.object(m, "now", return_value=datetime(2032, 5, 10, 12, tzinfo=timezone.utc)),
            mock.patch("app.multiplayer.core.socket.gethostname", return_value="test-drone"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub, self.nodes = make_swarm(temporary.name, open_ports=(55435,))
        for node in self.nodes.values():
            node.network.host_address = lambda peer, preferred="auto": "192.168.50.10"
        self.h, self.g = self.nodes["host"].orch, self.nodes["guest"].orch
        self.original_guest = self.nodes["guest"].conf_text
        self.set_host_settings("snes.rewind=0\n")

    def set_host_settings(self, text):
        node = self.nodes["host"]
        node.settings.batocera_conf_file.write_text(text, encoding="utf-8")
        # Keep read-only machine introspection faithful to the real fixture file.
        node.probe.files[str(node.settings.batocera_conf_file)] = text
        node.orch.adapter_map["retroarch"].refresh()

    def lobby(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"],
                                     "emulator_override": {"core": "snes9x"}})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def finish_and_record(self, sid):
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.h.end_session(sid)
        self.h.record_result(sid, True)

    def assessment(self):
        game = self.h.library.get("snes", "g1")
        return next(a for a in self.h.assessor.assess(game, core_override="snes9x")
                    if a["mechanism"] == m.RETROARCH_NETPLAY)

    def test_unchanged_launched_settings_can_retain_confirmed_evidence(self):
        sid = self.lobby()
        self.finish_and_record(sid)
        self.assertEqual(self.assessment()["classification"], m.VERIFIED_COMPATIBLE)

    def test_settings_changed_before_launch_do_not_verify_the_unplayed_lobby_config(self):
        sid = self.lobby()
        lobby_fp = self.h.store.require(sid)["local"]["tested_config_fingerprint"]
        # User changes a relevant emulation setting after accepting invitations.
        self.set_host_settings("snes.rewind=1\n")
        launch_fp = self.assessment()["config_fingerprint"]
        self.assertNotEqual(lobby_fp, launch_fp, "fixture must describe distinct emulation settings")
        try:
            self.h.start_session(sid)
        except m.MultiplayerError as error:
            self.assertTrue(error.message, "changed-settings rejection needs an actionable reason")
            self.assertEqual(self.nodes["host"].launcher.launched, [])
            self.assertNotEqual(self.assessment()["classification"], m.VERIFIED_COMPATIBLE)
            return
        if self.h.get_session(sid)["state"] == m.FAILED:
            self.assertTrue(self.h.get_session(sid)["error"])
            self.assertEqual(self.nodes["host"].launcher.launched, [])
            return
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.h.end_session(sid)
        self.h.record_result(sid, True)
        # A worker may reject changed settings, or bind successful evidence to the
        # actual launch settings. It must never verify the old unplayed settings.
        self.set_host_settings("snes.rewind=0\n")
        verdict = self.assessment()
        self.assertEqual(verdict["config_fingerprint"], lobby_fp)
        self.assertNotEqual(verdict["classification"], m.VERIFIED_COMPATIBLE,
                            "lobby settings were not the settings used by the successful game")

    def test_guest_failed_launch_records_failure_even_if_restore_backend_raises(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.nodes["guest"].launcher.fail_launch = "EmulationStation unavailable"
        # ConfigGuard normally returns file errors, but its SQLite journal can
        # raise (busy disk/database). No raw backend error may bypass launch bookkeeping.
        with mock.patch.object(self.g.guard, "restore", side_effect=sqlite3.OperationalError("journal database unavailable")):
            try:
                self.g.confirm_launch(sid)
            except (sqlite3.Error, m.MultiplayerError):
                pass
        local = self.g.store.require(sid)["local"]
        self.assertEqual(local.get("launch_status"), "failed",
                         "restoration failure must not leave a consumed launch stuck as launching")
        self.assertIn("EmulationStation unavailable", local.get("launch_error", ""))
        self.assertIn(sid, self.g.guard.pending(), "retain original settings for later recovery")
        self.g.tick()
        self.assertEqual(self.nodes["guest"].conf_text, self.original_guest)
        self.assertEqual(self.g.guard.pending(), {})

    def test_guest_restart_shutdown_failure_still_restores_configuration(self):
        sid = self.lobby()
        self.h.start_session(sid)
        session = self.g.store.require(sid)
        plan = session["local"]["pending_launch"]
        context = self.g.launch_context(session, m.GUEST_ROLE, port=plan["port"], secret=plan["secret"])
        self.g.apply_edits(sid, self.g.adapter_map["retroarch"].configure_networking(session, m.GUEST_ROLE, context))
        self.g.store.set_local(sid, launch_status="launching")
        self.nodes["guest"].launcher.active = {"rom_path": session["local"]["game_path"], "pid": 42}
        self.assertNotEqual(self.nodes["guest"].conf_text, self.original_guest)
        with mock.patch.object(self.nodes["guest"].launcher, "exit_game", side_effect=OSError("emulator shutdown unavailable")):
            self.g.recover()
        self.assertEqual(self.nodes["guest"].conf_text, self.original_guest,
                         "guest restart recovery must isolate process shutdown from config restoration")
        self.assertEqual(self.g.guard.pending(), {})
        self.assertEqual(self.g.store.require(sid)["local"]["launch_status"], "failed")


if __name__ == "__main__":
    unittest.main()
