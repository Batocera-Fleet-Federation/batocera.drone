"""Exercise browser-side Swarm preload races and table markup with Node."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for browser JavaScript checks")
class SwarmUiPreloadTests(unittest.TestCase):
    def run_js(self, script):
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_downloads_reuses_probes_across_navigation_and_rejects_stale_cache(self):
        self.run_js(r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("app/web/static/js/drone.js", "utf8");
const functions = source.slice(source.indexOf("async function loadSwarmOverview("), source.indexOf("function swarmSetProbeProgress("));
const invalidate = source.slice(source.indexOf("function invalidateSwarmDataCache("), source.indexOf("function isUiCacheFresh("));
let swarmOverviewCache = null, swarmOverviewCachedAt = 0, swarmOverviewPromise = null;
let tailnetDiscoveryCachedAt = 0, swarmPageGeneration = 1, swarmDronesById = {};
const SWARM_DATA_CACHE_TTL_MS = 30000;
const window = { location: { hash: "#admin/swarm" } };
const document = { getElementById: () => ({ isConnected: true, dataset: { swarmGeneration: "1" } }) };
const calls = [], pending = [], patches = [];
const api = path => {
  calls.push(path);
  return new Promise((resolve, reject) => pending.push({ resolve, reject }));
};
function swarmSetProbeProgress() {}
function patchSwarmDroneCard(drone) { patches.push(drone); }
eval(functions + invalidate + `
(async () => {
  const self = { drone_id: "self", is_self: true, online: true };
  const overview = { drones: [self, { drone_id: "online" }, { drone_id: "offline" }] };
  preloadSwarmConnectedDrones(overview, 1);
  assert.equal(calls.length, 2, "checks start on Swarm before Downloads opens");
  window.location.hash = "#admin/transfers";
  const downloads = loadSwarmOverview();
  assert.equal(calls.length, 2, "Downloads must reuse the existing checks");
  pending[0].resolve({ drone: { drone_id: "online", online: true, pending: false } });
  pending[1].reject(new Error("unreachable"));
  const result = await downloads;
  assert.deepEqual(result.drones.map(d => [d.drone_id, d.online]), [["self", true], ["online", true], ["offline", false]]);
  assert.equal(patches.length, 0, "late probes must not patch another page");
  assert.equal(await loadSwarmOverview(), result, "completed results are immediately cached");
  assert.equal(calls.length, 2);

  preloadSwarmConnectedDrones({ drones: [{ drone_id: "removed" }] }, 1);
  const stale = swarmOverviewPromise;
  invalidateSwarmDataCache();
  pending[2].resolve({ drone: { drone_id: "removed", online: true } });
  await stale;
  assert.equal(swarmOverviewCache, null, "removal must not republish stale peers");

  preloadSwarmConnectedDrones({ drones: [self] }, 1);
  await swarmOverviewPromise;
  assert.deepEqual(swarmOverviewCache.drones, [self], "self-only swarms also finish preloading");
})().catch(err => { console.error(err); process.exitCode = 1; });
`);
''')

    def test_drone_rows_have_inventory_columns_and_accessible_icon_actions(self):
        self.run_js(r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("app/web/static/js/drone.js", "utf8");
const functions = source.slice(source.indexOf("function swarmDroneCardId("), source.indexOf("// Swap only the regions", source.indexOf("function swarmDroneCardId(")));
const cssSafeId = value => value;
const escapeHtml = value => String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");
const swarmNetworkSharesByPeer = {};
const REFERENCE_ROMS_UI_ENABLED = true;
eval(functions + `
const peer = { drone_id: "peer", name: "<Drone>", online: true, reachable_url: "https://peer", summary: { counts: { roms: 12, bios: 3, artwork: 9 }, systems: ["nes", "snes"] } };
const row = renderSwarmDroneCard(peer);
assert.match(row, /<tr id=/);
assert.match(row, /&lt;Drone>/);
assert.equal((row.match(/<td /g) || []).length, 5);
assert.equal((row.match(/<td>/g) || []).length, 2);
assert.deepEqual([...row.matchAll(/data-swarm-count>(.*?)<\\/td>/g)].map(m => m[1]), ["12", "3", "9", "2"]);
const actions = row.slice(row.indexOf("data-swarm-actions"));
for (const button of actions.matchAll(/<button([^>]*)>(.*?)<\\/button>/gs)) {
  assert.match(button[1], /aria-label=/);
  assert.match(button[1], /title=/);
  assert.equal(button[2].replace(/<[^>]+>/g, "").trim(), "");
}
const pending = renderSwarmDroneCard({ ...peer, online: null, pending: true });
assert.match(pending, /Checking\\.\\.\\./);
assert.equal((pending.match(/data-swarm-count>&mdash;/g) || []).length, 4);
const self = renderSwarmDroneCard({ ...peer, is_self: true });
assert.match(self, /aria-label="Leave Swarm"/);
assert.doesNotMatch(self, /aria-label="Forget Drone"/);
`);
''')

    def test_transfer_progress_updates_while_search_or_actions_have_focus(self):
        self.run_js(r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("app/web/static/js/drone.js", "utf8");
const updater = source.slice(source.indexOf("function updateTransfersLive("), source.indexOf("function refreshTransfersPanel("));
const cell = (html, focus = null) => ({ innerHTML: html, contains: element => focus !== null && element === focus });
const row = (key, cells) => ({ dataset: { transferKey: key }, cells });
const region = (name, html, rows = [], focus = null) => ({
  dataset: { transferRegion: name }, innerHTML: html,
  contains: element => focus !== null && element === focus,
  querySelectorAll: () => rows,
});
const action = {}, search = {};
const oldRow = row("download:job-1", [cell("0.0%"), cell(""), cell("pause button", action)]);
// An unrelated job appears first in the next snapshot: patch by identity.
const newRow = row("download:job-1", [cell("42.5%"), cell("3 MB/s"), cell("new pause button")]);
const nextRegions = [region("active-pager", "new pager"), region("active-rows", "new rows", [row("download:other", [cell("99%")]), newRow]), region("eta", "2m remaining")];
const document = {
  activeElement: action,
  createElement: () => ({ content: { querySelectorAll: () => nextRegions } }),
};
const renderTransfersPanel = () => "new snapshot";
eval(updater + `
const pager = region("active-pager", "old pager");
const rows = region("active-rows", "old rows", [oldRow], action);
const eta = region("eta", "4m remaining");
const target = {
  contains: () => true,
  querySelector: () => rows,
  querySelectorAll: () => [pager, rows, eta],
};
updateTransfersLive(target, {}, {});
assert.equal(oldRow.cells[0].innerHTML, "42.5%");
assert.equal(oldRow.cells[1].innerHTML, "3 MB/s");
assert.equal(oldRow.cells[2].innerHTML, "pause button", "the focused action stays intact");
assert.equal(eta.innerHTML, "2m remaining");
assert.equal(rows.innerHTML, "old rows", "the focused row is not reordered");

document.activeElement = search;
pager.innerHTML = "typed search";
pager.contains = element => element === search;
updateTransfersLive(target, {}, {});
assert.equal(pager.innerHTML, "typed search", "typing is preserved");
assert.equal(rows.innerHTML, "new rows", "the table still updates during typing");

document.activeElement = null;
target.contains = () => false;
updateTransfersLive(target, {}, {});
assert.equal(target.innerHTML, "new snapshot");
`);
// The actual poll must use the updater without skipping focused controls.
const poll = source.slice(source.indexOf("function startTransfersAutoRefresh("), source.indexOf("function startLogAutoRefresh("));
assert.match(poll, /updateTransfersLive\(transfersBody, downloads, uploads\)/);
assert.doesNotMatch(poll, /contains\(document.activeElement\)/);
''')
