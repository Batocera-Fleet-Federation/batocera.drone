"""#240 §§3.1,10,17: restart recovery must isolate guests and notify the host.

Expected invariants, derived from the issue before treating the current
implementation as correct:

- A Drone restart is a recovery event. Temporary emulator settings cannot wait
  for later housekeeping when the session game is already gone.
- Host restart after the host game has exited ends the session and restores
  settings. Guest restart after the guest's session game has exited must restore
  that guest's settings with the same immediacy. The host's still-running game
  is left alone.
- Guest restart while the session game is still in the foreground must not
  tear down a healthy session.
- Guest restart during an in-flight launch is a failed launch. The host must
  observe that failure inside the launch window, not remain STARTING with the
  guest still launching until the 120s deadline.

Emulators and peer transport are simulated. Configuration files and SQLite are
real temporary data. This does not validate real RetroArch gameplay.
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.multiplayer import models as m
from multiplayer_support import make_swarm

CORE = {"emulator_override": {"core": "snes9x"}}


class RestartParticipationUAT(unittest.TestCase):
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
        self.original_host = self.nodes["host"].conf_text
        self.original_guest = self.nodes["guest"].conf_text

    def lobby(self):
        sid = self.h.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        self.g.accept_invitation(sid)
        self.g.set_ready(sid, True)
        self.h.set_ready(sid, True)
        self.assertEqual(self.h.get_session(sid)["state"], m.READY)
        return sid

    def running(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.g.confirm_launch(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertNotEqual(self.nodes["guest"].conf_text, self.original_guest)
        self.assertIn(sid, self.g.guard.pending())
        return sid

    def test_host_restart_after_session_game_exits_ends_and_restores(self):
        sid = self.running()
        self.assertNotEqual(self.nodes["host"].conf_text, self.original_host)
        self.nodes["host"].launcher.active = None
        summary = self.h.recover()
        self.assertGreaterEqual(summary["ended"], 1)
        self.assertEqual(self.h.get_session(sid)["state"], m.COMPLETED)
        self.assertEqual(self.nodes["host"].conf_text, self.original_host)
        self.assertEqual(self.h.guard.pending(), {})

    def test_guest_restart_while_session_game_is_running_keeps_the_session(self):
        sid = self.running()
        guest_conf = self.nodes["guest"].conf_text
        path = self.g.store.require(sid)["local"]["game_path"]
        summary = self.g.recover()
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["ended"], 0)
        self.assertEqual(self.nodes["guest"].conf_text, guest_conf)
        self.assertEqual(self.nodes["guest"].launcher.active["rom_path"], path)
        self.assertEqual(self.g.store.require(sid)["local"].get("launch_status"), "running")
        self.assertEqual(self.h.get_session(sid)["state"], m.RUNNING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING)

    def test_guest_restart_after_session_game_exits_restores_configuration(self):
        sid = self.running()
        host_path = self.h.store.require(sid)["local"]["game_path"]
        self.nodes["guest"].launcher.active = None
        self.g.recover()
        local = self.g.store.require(sid)["local"]
        self.assertEqual(self.nodes["guest"].conf_text, self.original_guest,
                         "guest restart recovery must restore temporary netplay settings when the session game is gone")
        self.assertEqual(self.g.guard.pending(), {})
        self.assertNotEqual(local.get("launch_status"), "running")
        self.assertEqual(self.nodes["host"].launcher.active["rom_path"], host_path,
                         "a guest restart must not close the host's still-running game")
        self.g.tick()
        self.h.tick()
        self.assertNotEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_RUNNING,
                            "the host must not keep a guest as running after that guest's session game is gone")

    def test_guest_restart_during_launch_fails_on_the_host_inside_the_launch_window(self):
        sid = self.lobby()
        self.h.start_session(sid)
        self.assertEqual(self.h.get_session(sid)["state"], m.STARTING)
        self.assertEqual(self.h.get_session(sid)["players"]["guest"]["status"], m.P_LAUNCHING)
        session = self.g.store.require(sid)
        plan = session["local"]["pending_launch"]
        context = self.g.launch_context(session, m.GUEST_ROLE, port=plan["port"], secret=plan["secret"])
        self.g.apply_edits(sid, self.g.adapter_map["retroarch"].configure_networking(session, m.GUEST_ROLE, context))
        self.g.store.set_local(sid, launch_status="launching")
        self.nodes["guest"].launcher.active = {"rom_path": session["local"]["game_path"], "pid": 42}
        self.assertNotEqual(self.nodes["guest"].conf_text, self.original_guest)
        self.g.recover()
        self.assertEqual(self.g.store.require(sid)["local"].get("launch_status"), "failed")
        self.assertEqual(self.nodes["guest"].conf_text, self.original_guest)
        self.g.tick()
        self.h.tick()
        host = self.h.get_session(sid)
        self.assertNotEqual(host["players"]["guest"]["status"], m.P_LAUNCHING,
                            "a guest restart during launch is a failed launch, not an in-flight one")
        self.assertNotEqual(host["state"], m.STARTING,
                            "the host must not remain STARTING until the 120s launch deadline")
        self.assertEqual(self.nodes["guest"].launcher.launched, [])


if __name__ == "__main__":
    unittest.main()
