"""Issue #694 UAT: the Controls page renders without the RGS Download Manager
card or the Run PixN Update button.

Issue sentence verified: "In the controls UI there are co trols for RGS Download
Manager and Run PixN update. Let's remove these from the UI for now as they are
mostly never used and have some issues we need to work through later."

The real Controls renderer (`renderAdminControlsPage` in app/web/static/js/drone.js)
is executed in a Node vm with a stub DOM and a recording fetch, so the assertions
check the markup and the network requests the page actually produces rather than
source text.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRONE_JS = ROOT / "app" / "web" / "static" / "js" / "drone.js"

RENDER_SCRIPT = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const src = fs.readFileSync(process.env.DRONE_JS, "utf8");
const pixenInstalled = process.env.PIXEN_INSTALLED === "1";
const fetched = [];
const json = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) });
async function fakeFetch(url, opts = {}) {
  const u = String(url);
  fetched.push(`${opts.method || "GET"} ${u}`);
  if (u.includes("/admin/system-info/screen-mode")) return json({ mode: "full" });
  if (u.includes("/admin/system-info")) return json({ fields: { pixen_installed: pixenInstalled ? "yes" : "no", audio_volume: 50 }, pixen_installed: pixenInstalled, audio_volume: 50 });
  if (u.includes("/admin/system/auto-update")) return json({ enabled: false });
  if (u.includes("/admin/tailnet/status")) return json({ installed: false });
  if (u.includes("/admin/rgs")) return json({ installed: true, available: true, running: true, enabled: true });
  return json({});
}
const makeEl = () => ({
  _h: "", dataset: {}, style: {}, value: "0", checked: false, disabled: false,
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  addEventListener() {}, removeEventListener() {}, appendChild() {}, setAttribute() {},
  getAttribute() { return null; }, removeAttribute() {}, querySelector() { return makeEl(); },
  querySelectorAll() { return []; }, closest() { return null; }, contains() { return false; },
  focus() {}, click() {}, remove() {},
  get innerHTML() { return this._h; }, set innerHTML(v) { this._h = String(v); },
  get textContent() { return this._t || ""; }, set textContent(v) { this._t = String(v); },
});
const els = new Map();
const document = {
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl()); return els.get(id); },
  querySelector() { return makeEl(); }, querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {}, createElement() { return makeEl(); },
  body: makeEl(), head: makeEl(), documentElement: makeEl(), readyState: "complete", title: "", cookie: "",
};
const loc = { hash: "#admin/controls", origin: "https://drone.test", pathname: "/", search: "", href: "https://drone.test/" };
const storage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const win = {
  location: loc, addEventListener() {}, removeEventListener() {}, confirm() { return true; },
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  localStorage: storage, sessionStorage: storage, innerWidth: 1200, innerHeight: 800,
  history: { pushState() {}, replaceState() {} }, open() {}, scrollTo() {},
};
const ctx = vm.createContext({
  document, window: win, console, setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {},
  fetch: fakeFetch, Promise, JSON, Math, Date, Number, String, Array, Object, Set, Map, RegExp, Error, URL,
  URLSearchParams, Intl, navigator: { userAgent: "node" }, location: loc, localStorage: storage,
  sessionStorage: storage, history: win.history, requestAnimationFrame() { return 0; },
  Event: class {}, CustomEvent: class {},
  MutationObserver: class { observe() {} disconnect() {} },
  ResizeObserver: class { observe() {} disconnect() {} },
  IntersectionObserver: class { observe() {} disconnect() {} },
});
const result = { html: "", fetched, error: null };
(async () => {
  try {
    vm.runInContext(src, ctx, { filename: "drone.js" });
    await vm.runInContext("renderAdminControlsPage()", ctx);
    result.html = vm.runInContext("content.innerHTML", ctx);
  } catch (e) {
    result.error = String(e && e.stack || e);
  }
  process.stdout.write(JSON.stringify(result));
})();
'''


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the Controls renderer")
class ControlsRgsPixnRemovalUat(unittest.TestCase):
    def render_controls(self, pixen_installed: bool) -> dict:
        env = dict(os.environ)
        env["DRONE_JS"] = str(DRONE_JS)
        env["PIXEN_INSTALLED"] = "1" if pixen_installed else "0"
        completed = subprocess.run(
            ["node", "-e", RENDER_SCRIPT],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIsNone(result["error"], result["error"])
        return result

    def test_controls_page_omits_rgs_card_and_pixn_update_when_pixn_installed(self):
        # Issue #694: "...controls for RGS Download Manager and Run PixN update. Let's remove these from the UI..."
        # The Run PixN Update button was conditional on PixN being installed, so the installed case is the regression case.
        result = self.render_controls(pixen_installed=True)
        html = result["html"]
        self.assertIn("Screen Mode", html, "Controls page did not render")
        self.assertNotIn("RGS Download Manager", html)
        self.assertNotIn("rgsEnabledCheckbox", html)
        self.assertNotIn("Enable RGS", html)
        self.assertNotIn("Run PixN Update", html)
        self.assertNotIn("runPixnUpdate", html)
        self.assertNotIn("setRgsEnabled", html)

    def test_controls_page_does_not_request_rgs_status(self):
        # Issue #694: "...Let's remove these from the UI..." - the RGS status request only fed the removed RGS card.
        result = self.render_controls(pixen_installed=True)
        rgs_requests = [call for call in result["fetched"] if "/admin/rgs" in call]
        self.assertEqual(rgs_requests, [], result["fetched"])

    def test_controls_page_keeps_update_and_restart_controls(self):
        # Issue #694 names only the RGS Download Manager and Run PixN update controls for removal, so Update Drone,
        # Restart EmulationStation and Auto-update must still render.
        result = self.render_controls(pixen_installed=True)
        html = result["html"]
        self.assertIn("Update Drone", html)
        self.assertIn("Restart EmulationStation", html)
        self.assertIn('id="droneAutoUpdateCheckbox"', html)


if __name__ == "__main__":
    unittest.main()
