"""Execute the multiplayer UI renderers with Node; no browser/emulator required."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is needed for JavaScript renderer checks")
class MultiplayerFrontendTests(unittest.TestCase):
    def evaluate(self, code):
        drone = (ROOT / "app/web/static/js/drone.js").read_text()
        helper = drone[drone.index("function escapeHtml(value)"):drone.index("function jsAttr(value)")]
        script = "const vm = require('vm'); const context = vm.createContext({});\n"
        script += "vm.runInContext(" + json.dumps(helper) + ", context);\n"
        script += "vm.runInContext(require('fs').readFileSync('app/web/static/js/multiplayer.js', 'utf8'), context);\n"
        script += "console.log(JSON.stringify(vm.runInContext(" + json.dumps(code) + ", context)));"
        result = subprocess.run([shutil.which("node"), "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)

    def test_untrusted_peer_fields_are_escaped_in_text_and_attributes(self):
        html = self.evaluate('mpPeersCard([{ name: \'<img src=x onerror="alert(1)">\', device_id: \'x" onclick="alert(1)\', online: true, scope: "lan" }])')
        self.assertNotIn("<img", html)
        self.assertNotIn('onclick="', html)
        self.assertIn("&lt;img", html)
        self.assertIn("&quot;", html)

    def test_guest_controls_follow_the_authorized_actions(self):
        html = self.evaluate('mpLobbyControls({ id: "a", actions: ["set_ready", "leave"], my_player: { status: "joined", ready: false } }, [])')
        self.assertIn('data-mp-action="ready"', html)
        self.assertIn('data-mp-action="leave"', html)
        for action in ("start", "end", "retry", "launch"):
            self.assertNotIn('data-mp-action="' + action + '"', html)

    def test_invitation_offers_acceptance_without_game_launch(self):
        html = self.evaluate('mpInvitationsCard([{session_id: "a", host_name: "<script>alert(1)</script>", game: {title: "Game", system: "snes"}, max_players: 2}])')
        self.assertIn('data-mp-action="accept"', html)
        self.assertIn('data-mp-action="decline"', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('data-mp-action="launch"', html)

    def test_sunshine_card_offers_only_the_actions_the_machine_allows(self):
        html = self.evaluate('mpDependencyCard({ installed: false, can_install: true, supported: true, job: { state: "idle" }, source: "<b>x</b>" })')
        self.assertIn('data-mp-action="dep-install"', html)
        self.assertNotIn('data-mp-action="dep-uninstall"', html)
        self.assertNotIn("<b>x</b>", html)
        html = self.evaluate('mpDependencyCard({ installed: true, managed: true, running: false, can_start: true, can_uninstall: true, job: { state: "running", percent: 40, message: "<i>x</i>" } })')
        self.assertIn('data-mp-action="dep-start"', html)
        self.assertIn('data-mp-action="dep-uninstall"', html)
        self.assertIn("Installing", html)
        self.assertNotIn("<i>x</i>", html)

    def test_unmanaged_sunshine_cannot_be_uninstalled_from_the_ui(self):
        html = self.evaluate('mpDependencyCard({ installed: true, managed: false, running: true, can_uninstall: false, job: { state: "idle" } })')
        self.assertNotIn('data-mp-action="dep-uninstall"', html)
        self.assertIn("will not remove it", html)

    def test_setup_hint_links_to_settings_only_when_sunshine_is_the_blocker(self):
        blocked = self.evaluate('mpSetupHint({ reason: "No multiplayer mechanism is usable.", candidates: [{ blockers: ["Sunshine is not installed on this (host) machine."] }] })')
        self.assertIn("#multiplayer/settings", blocked)
        self.assertEqual(self.evaluate('mpSetupHint({ reason: "ok", candidates: [{ blockers: ["Other"] }] })'), "")

    def test_multiplayer_renderer_is_loaded_before_initial_router_can_run(self):
        shell = (ROOT / "app/web/templates/index.html").read_text()
        self.assertLess(shell.index("/static/js/multiplayer.js"), shell.index("/static/js/drone.js"))
