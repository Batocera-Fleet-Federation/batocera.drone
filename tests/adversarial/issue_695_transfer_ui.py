"""Shared harness for the Issue #695 adversarial UAT suites.

The real Transfers panel (`renderIntegrationTransfersPanel` in
app/web/static/js/drone.js) is loaded into a Node vm with a stub DOM and a fake
`fetch` that answers the two Transfers endpoints. The rows come from the real
backend (`DownloadManager.enqueue_rom` / `snapshot` and `UploadTracker`), so the
row JSON the panel embeds is the same shape the browser receives.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
DRONE_JS = ROOT / "app" / "web" / "static" / "js" / "drone.js"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.common.settings import Settings  # noqa: E402
from app.drone_api import RomRepository  # noqa: E402
from app.transfer.download_manager import DownloadManager  # noqa: E402
from app.transfer.upload_tracker import UploadTracker  # noqa: E402

RUN_SCRIPT = r'''
const fs = require("node:fs");
const vm = require("node:vm");
const src = fs.readFileSync(process.env.DRONE_JS, "utf8");
const input = JSON.parse(process.env.TRANSFER_INPUT);
const fetched = [];
const json = body => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
async function fakeFetch(url, opts = {}) {
  const u = String(url);
  fetched.push(`${opts.method || "GET"} ${u}`);
  if (u.includes("/admin/downloads")) return json(input.downloads);
  if (u.includes("/admin/uploads")) return json(input.uploads);
  return json({});
}
const makeEl = id => ({
  id, _h: "", _l: {}, dataset: {}, style: {}, value: "", checked: false, disabled: false,
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  addEventListener(type, fn) { (this._l[type] ||= []).push(fn); },
  removeEventListener() {}, appendChild() {}, setAttribute() {}, getAttribute() { return null; },
  removeAttribute() {}, querySelector() { return makeEl(""); }, querySelectorAll() { return []; },
  closest() { return null; }, matches() { return false; }, contains() { return false; },
  focus() {}, click() {}, remove() {},
  get innerHTML() { return this._h; }, set innerHTML(v) { this._h = String(v); },
  get textContent() { return this._t || ""; }, set textContent(v) { this._t = String(v); },
});
const els = new Map();
const document = {
  activeElement: null,
  getElementById(id) { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); },
  querySelector() { return makeEl(""); }, querySelectorAll() { return []; },
  addEventListener() {}, removeEventListener() {}, createElement() { return makeEl(""); },
  body: makeEl("body"), head: makeEl("head"), documentElement: makeEl("html"),
  readyState: "complete", title: "", cookie: "",
};
const loc = { hash: "#admin/transfers", origin: "https://drone.test", pathname: "/", search: "", href: "https://drone.test/" };
const storage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const opened = [];
const win = {
  location: loc, addEventListener() {}, removeEventListener() {}, confirm() { return true; },
  matchMedia() { return { matches: false, addEventListener() {}, addListener() {} }; },
  localStorage: storage, sessionStorage: storage, innerWidth: 1200, innerHeight: 800,
  history: { pushState() {}, replaceState() {} }, open() {}, scrollTo() {},
  bootstrap: { Modal: { getOrCreateInstance(modal) { opened.push(modal.innerHTML); return { show() {} }; } } },
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
const decode = s => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const dlPairs = html => [...String(html).matchAll(/<dt>([\s\S]*?)<\/dt><dd[^>]*>([\s\S]*?)<\/dd>/g)].map(m => [decode(m[1]), decode(m[2])]);
const result = { error: null, fetched, rows: [], clicks: [], panelHtml: "", bodyHtml: "" };
(async () => {
  try {
    vm.runInContext(src, ctx, { filename: "drone.js" });
    const panel = document.getElementById("integrationTransfersPanel");
    await vm.runInContext("renderIntegrationTransfersPanel(document.getElementById('integrationTransfersPanel'))", ctx);
    const body = document.getElementById("transfersBody");
    result.panelHtml = panel.innerHTML;
    result.bodyHtml = body.innerHTML;
    const re = /<tr([^>]*)data-transfer-details="([^"]*)"([^>]*)>/g;
    for (const m of body.innerHTML.matchAll(re)) {
      const raw = decode(m[2]);
      result.rows.push({ raw, details: JSON.parse(raw), attrs: m[1] + m[3] });
    }
    const rowTarget = index => ({
      closest(sel) { return sel.includes("data-transfer-details") ? { dataset: { transferDetails: result.rows[index].raw } } : null; },
      matches(sel) { return sel === "[data-transfer-details]"; },
    });
    const buttonTarget = { closest(sel) { return sel.includes("button") ? {} : null; }, matches() { return false; } };
    const fire = (kind, target, key) => {
      const before = opened.length;
      if (kind === "click") for (const fn of body._l.click || []) fn({ target });
      else for (const fn of body._l.keydown || []) fn({ key, target, preventDefault() {} });
      return opened.length > before ? dlPairs(opened[opened.length - 1]) : null;
    };
    const steps = [];
    result.rows.forEach((_, i) => {
      steps.push({ label: `click row ${i}`, pairs: fire("click", rowTarget(i)) });
      steps.push({ label: `Enter row ${i}`, pairs: fire("keydown", rowTarget(i), "Enter") });
      steps.push({ label: `Space row ${i}`, pairs: fire("keydown", rowTarget(i), " ") });
      steps.push({ label: `key a row ${i}`, pairs: fire("keydown", rowTarget(i), "a") });
    });
    steps.push({ label: "click button", pairs: fire("click", buttonTarget) });
    steps.push({ label: "Enter button", pairs: fire("keydown", buttonTarget, "Enter") });
    result.clicks = steps;
  } catch (e) {
    result.error = String(e && e.stack || e);
  }
  process.stdout.write(JSON.stringify(result));
})();
'''


def build_backend_snapshots(tmp: str):
    """Real Transfers payloads: one queued download and one upload from a peer."""
    root = Path(tmp) / "userdata"
    with mock.patch.dict(
        "os.environ",
        {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "OVERMIND_DEVICE_ID": "target-a",
        },
        clear=True,
    ):
        settings = Settings.from_env()
    # Thread.start is patched so no worker claims the queued job during the test.
    with mock.patch("app.transfer.download_manager.Thread.start"):
        manager = DownloadManager(settings, RomRepository(root / "roms", root / "bios"))
    manager.enqueue_rom(
        {}, {"drone_id": "source-a"}, "snes", "Game0.zip", expected_size=1000,
        sync_id="sync-42", source_action_id="action-7",
    )
    downloads = manager.snapshot(compact=True, queued_limit=500)
    tracker = UploadTracker()
    tracker.start(peer_device_id="peer-b", asset_type="roms", relative_path="snes/Game1.zip",
                  system="snes", total_bytes=2000)
    # Mirrors _handle_admin_uploads, which adds the local target_drone_id to the snapshot.
    uploads = {"target_drone_id": settings.device_id, **tracker.snapshot()}
    return downloads, uploads


def run_transfers_panel(downloads: dict, uploads: dict) -> dict:
    env = dict(os.environ)
    env["DRONE_JS"] = str(DRONE_JS)
    env["TRANSFER_INPUT"] = json.dumps({"downloads": downloads, "uploads": uploads})
    completed = subprocess.run(
        ["node", "-e", RUN_SCRIPT], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    result = json.loads(completed.stdout)
    if result["error"]:
        raise AssertionError(result["error"])
    return result


def modal_pairs(step: dict) -> dict:
    """Label -> value for the modal opened by a step (empty dict when none opened)."""
    return dict(step["pairs"] or [])

