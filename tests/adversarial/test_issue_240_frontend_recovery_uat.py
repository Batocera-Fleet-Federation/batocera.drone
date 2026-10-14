"""Issue #240 adversarial UAT: UI contract, clone reset, worker generations, privacy.

Expected behaviour, derived from the issue:

* There is a top-level Multiplayer section with dashboard actions Host Game,
  Join Game, Invite Players, View Compatible Games, and Multiplayer Settings.
* Guest lobby controls never include host-only start/end/retry.
* Invitations offer accept/decline and never a launch.
* Untrusted strings are escaped in the UI.
* Clone identity reset restores session emulator settings, drops peer-bound
  multiplayer state, and does not announce to the old swarm.
* Each multiplayer worker generation has its own stop event.
* Multiplayer events are local-only: no SMTP toggle, never in the digest.
* Auto-accept and auto-launch stay off unless the user opts in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / "tests"
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from app.device import notifications
from app.multiplayer import models as m
from app.multiplayer.identity_reset import reset_multiplayer_identity_state
from app.multiplayer.worker import start_multiplayer_worker, stop_multiplayer_worker
from app.multiplayer import worker as worker_mod
from multiplayer_support import make_swarm
from test_multiplayer_core import make_settings

CORE = {"emulator_override": {"core": "snes9x"}}


class FrontendContractTests(unittest.TestCase):
    def test_shell_exposes_a_top_level_multiplayer_section(self):
        shell = (REPO_ROOT / "app/web/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="multiplayerMenuBtn"', shell)
        self.assertIn('href="#multiplayer"', shell)
        self.assertIn("Multiplayer", shell)
        self.assertLess(shell.index("/static/js/multiplayer.js"), shell.index("/static/js/drone.js"))
        drone = (REPO_ROOT / "app/web/static/js/drone.js").read_text(encoding="utf-8")
        self.assertIn("multiplayerMenuBtn", drone)
        self.assertIn('hash === "#multiplayer" || hash.startsWith("#multiplayer/")', drone)

    def test_dashboard_actions_and_guest_controls(self):
        script = (REPO_ROOT / "app/web/static/js/multiplayer.js").read_text(encoding="utf-8")
        for label in ("Host Game", "Join Game", "Invite Players", "View Compatible Games", "Multiplayer Settings"):
            self.assertIn(label, script)
        for action in ("accept", "decline", "ready", "leave", "start", "end", "retry", "launch"):
            self.assertIn(f'data-mp-action="{action}"', script)
        self.assertNotIn("onclick=", script)
        self.assertNotIn("eval(", script)
        self.assertIn("escapeHtml", script)

    @unittest.skipUnless(shutil.which("node"), "Node is needed for JavaScript renderer checks")
    def test_untrusted_fields_and_guest_actions_in_rendered_html(self):
        html = self._evaluate(
            'mpPeersCard([{ name: \'<img src=x onerror="alert(1)">\', '
            'device_id: \'x" onclick="alert(1)\', online: true, scope: "lan" }])'
        )
        self.assertNotIn("<img", html)
        self.assertNotIn('onclick="', html)
        self.assertIn("&lt;img", html)
        guest = self._evaluate(
            'mpLobbyControls({ id: "a", actions: ["set_ready", "leave"], '
            'my_player: { status: "joined", ready: false } }, [])'
        )
        self.assertIn('data-mp-action="ready"', guest)
        self.assertIn('data-mp-action="leave"', guest)
        for action in ("start", "end", "retry", "launch"):
            self.assertNotIn('data-mp-action="' + action + '"', guest)
        invite = self._evaluate(
            'mpInvitationsCard([{session_id: "a", host_name: "<script>alert(1)</script>", '
            'game: {title: "Game", system: "snes"}, max_players: 2, mechanism_label: "RetroArch Netplay", '
            'expires_at: "2099-01-01T00:00:00+00:00"}])'
        )
        self.assertIn('data-mp-action="accept"', invite)
        self.assertIn('data-mp-action="decline"', invite)
        self.assertNotIn("<script>", invite)
        self.assertNotIn('data-mp-action="launch"', invite)

    def _evaluate(self, code):
        drone = (REPO_ROOT / "app/web/static/js/drone.js").read_text(encoding="utf-8")
        helper = drone[drone.index("function escapeHtml(value)"):drone.index("function jsAttr(value)")]
        script = "const vm = require('vm'); const context = vm.createContext({});\n"
        script += "vm.runInContext(" + json.dumps(helper) + ", context);\n"
        script += "vm.runInContext(require('fs').readFileSync('app/web/static/js/multiplayer.js', 'utf8'), context);\n"
        script += "console.log(JSON.stringify(vm.runInContext(" + json.dumps(code) + ", context)));"
        result = subprocess.run(
            [shutil.which("node"), "-e", script], cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=15, check=True,
        )
        return json.loads(result.stdout)


class RecoveryAndPrivacyTests(unittest.TestCase):
    def test_clone_reset_restores_config_drops_peer_state_and_stays_silent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub, nodes = make_swarm(tmp.name, open_ports=(55435,))
        host = nodes["host"]
        before = host.conf_text
        sid = host.orch.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        host.orch.update_settings({"privacy": {"invite_permission": "allowlist", "allowed_inviters": ["guest"]}})
        host.orch.compat.record_session_result(
            system="snes", title_key="local-title", title="Local", mechanism=m.RETROARCH_NETPLAY,
            config_fp="aa", emulator_version="1", success=True,
        )
        host.orch.compat.import_shared(
            [{"system": "snes", "gameId": "imported", "mechanisms": {m.RETROARCH_NETPLAY: {"tested": {"bb": {"successes": 1}}}}}],
            "guest",
        )
        adapter = host.orch.adapter_map["retroarch"]
        session = host.orch.store.get(sid)
        host.orch.apply_edits(sid, adapter.configure_networking(
            session, m.HOST_ROLE, host.orch.launch_context(session, m.HOST_ROLE, port=55435)))
        self.assertNotEqual(host.conf_text, before)
        log_before_reset = list(hub.log)
        result = reset_multiplayer_identity_state(host.settings)
        self.assertEqual(result["restored_sessions"], 1)
        self.assertEqual(host.conf_text, before)
        self.assertEqual(host.orch.store.all(), [])
        self.assertEqual(host.orch.store.invitations(), [])
        self.assertEqual(host.orch.get_settings()["privacy"]["allowed_inviters"], [])
        self.assertIsNotNone(host.orch.compat.get("snes", "local-title"))
        self.assertIsNone(host.orch.compat.get("snes", "imported"))
        self.assertEqual(hub.log, log_before_reset)

    def test_worker_generations_do_not_reuse_a_stop_event(self):
        stop_multiplayer_worker()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = make_settings(tmp.name)
        with mock.patch.object(worker_mod, "SWEEP_SECONDS", 0.05), mock.patch.object(worker_mod, "STARTUP_DELAY_SECONDS", 0.0):
            first = start_multiplayer_worker(settings, mock.Mock())
            self.assertIsNotNone(first)
            first_stop = worker_mod._STOP
            self.assertIsNone(start_multiplayer_worker(settings, mock.Mock()))
            self.assertIs(worker_mod._STOP, first_stop)
            stop_multiplayer_worker()
            first.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertTrue(first_stop.is_set())
            second = start_multiplayer_worker(settings, mock.Mock())
            self.assertIsNotNone(second)
            self.assertIsNot(worker_mod._STOP, first_stop)
            self.assertFalse(worker_mod._STOP.is_set())
            stop_multiplayer_worker()
            second.join(timeout=2)

    def test_multiplayer_events_are_local_only_and_have_no_smtp_toggle(self):
        for event_type in notifications.MULTIPLAYER_EVENT_TYPES:
            self.assertNotIn(event_type, notifications.EVENT_TYPES)
            self.assertIn(event_type, notifications.LOCAL_ONLY_EVENT_TYPES)
        captured = []

        def fake_insert(settings, event_type, title, message="", details=None, notify=True, digest=True):
            captured.append({"digest": digest, "notify": notify, "event_type": event_type})
            return {"id": 1}

        with mock.patch.object(notifications, "_insert", fake_insert):
            notifications.record_event(mock.Mock(), "multiplayer_invitation", "Invite", "from host")
            notifications.record_audit_event(mock.Mock(), "multiplayer_session", "Session", "ended")
        self.assertEqual(len(captured), 2)
        self.assertFalse(any(item["digest"] for item in captured))
        self.assertTrue(captured[0]["notify"])
        self.assertFalse(captured[1]["notify"])

    def test_settings_defaults_keep_invitations_and_launches_manual(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub, nodes = make_swarm(tmp.name, open_ports=(55435,))
        config = nodes["host"].orch.get_settings()
        self.assertTrue(config["general"]["enabled"])
        self.assertFalse(config["general"]["auto_accept_invitations"])
        self.assertEqual(config["privacy"]["local_launch_authorization"], "confirm")
        self.assertTrue(config["general"]["auto_restore_configuration"])
        self.assertEqual(config["networking"]["relay_provider"], "none")

    def test_host_restart_mid_start_fails_the_session_and_restores_settings(self):
        from app.multiplayer.orchestrator import MultiplayerOrchestrator
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub, nodes = make_swarm(tmp.name, open_ports=(55435,))
        host, guest = nodes["host"], nodes["guest"]
        before = host.conf_text
        sid = host.orch.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], **CORE})["id"]
        guest.orch.accept_invitation(sid)
        guest.orch.set_ready(sid, True)
        host.orch.set_ready(sid, True)
        host.launcher.start_never_happens = True
        session = host.orch.store.get(sid)
        adapter = host.orch.adapter_map["retroarch"]
        context = host.orch.launch_context(session, m.HOST_ROLE, port=55435)
        host.orch.apply_edits(sid, adapter.configure_networking(session, m.HOST_ROLE, context))
        host.orch.store.mutate(sid, lambda s: s.update(state=m.STARTING))
        self.assertNotEqual(host.conf_text, before)
        reborn = MultiplayerOrchestrator(
            host.settings, host.repo, probe=host.probe, launcher=host.launcher, network=host.network,
            paired_peers=lambda: list(host.peers), spawn=lambda f, n: f(), sleep=lambda s: None,
            audit=lambda *a, **k: None, bell=lambda *a, **k: None,
        )
        summary = reborn.recover()
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(reborn.get_session(sid)["state"], m.FAILED)
        self.assertEqual(host.conf_text, before)
        self.assertIn("restarted", reborn.get_session(sid)["error"].lower())


if __name__ == "__main__":
    unittest.main()
