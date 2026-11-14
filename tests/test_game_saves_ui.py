"""Exercise the actual chooser JavaScript without a browser dependency."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'node is required for UI tests')
class GameSavesUiTests(unittest.TestCase):
    def test_choose_remote_save_and_both_overwrite_modes(self):
        script = r'''
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync('app/web/static/js/drone.js', 'utf8');
const code = source.slice(source.indexOf('async function loadRomSwarmSaves('), source.indexOf('async function renderRomMediaPage('));
class Element {
  constructor(tag = '') { this.tag = tag; this.children = []; this.listeners = {}; this.isConnected = true; this.checked = false; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.append(node); }
  replaceChildren() { this.children = []; }
  addEventListener(kind, action) { this.listeners[kind] = action; }
  remove() { this.removed = true; }
  querySelector(id) { return elements[id]; }
  querySelectorAll() { return Object.values(elements); }
}
const body = new Element();
const elements = {'#romSaveDrones': new Element(), '#romSaveResult': new Element(), '#romSaveOverwrite': new Element()};
global.document = {getElementById: () => body, createElement: (tag) => new Element(tag)};
global.escapeHtml = (text) => String(text).replaceAll('<', '&lt;');
global.formatBytes = (value) => `${value} bytes`;
let confirmCount = 0;
global.window = {confirm: () => { confirmCount++; return true; }};
const requests = [], posts = [];
let empty = false;
global.api = async (url) => {
  requests.push(url);
  if (url.includes('?drone=')) return {saves: empty ? [] : [{choice: 'opaque', name: '<img onerror=attack()>', kind: 'state', layout: 'system', file_count: 2, file_size: 99}]};
  return {capabilities: {saves: 'unknown', states: 'available', message: 'Support'}, drones: [{drone_id: 'living-room', name: 'Living Room'}]};
};
global.apiPost = async (url, data) => { posts.push({url, data}); return {backup_kept: !data.overwrite, applied: true}; };
const load = new Function(code + '; return loadRomSwarmSaves;')();
(async () => {
  for (const overwrite of [false, true]) {
    elements['#romSaveDrones'].children = [];
    await load('snes', 'rom id');
    assert(body.innerHTML.includes('role="switch"'));
    const section = elements['#romSaveDrones'].children[0];
    await section.children[0].listeners.click();
    const row = section.children[1].children[0];
    assert(row.children[0].textContent.includes('<img onerror=attack()>'));
    assert.strictEqual(row.children[0].innerHTML, undefined);
    elements['#romSaveOverwrite'].checked = overwrite;
    await row.children[1].listeners.click();
    assert.strictEqual(posts.at(-1).data.choice, 'opaque');
    assert.strictEqual(posts.at(-1).data.overwrite, overwrite);
    assert(posts.at(-1).url.includes('/admin/systems/snes/roms/rom%20id/saves'));
    assert(elements['#romSaveResult'].textContent.includes('Save applied'));
  }
  assert.strictEqual(confirmCount, 1);
  empty = true;
  elements['#romSaveDrones'].children = [];
  await load('snes', 'rom id');
  const section = elements['#romSaveDrones'].children[0];
  await section.children[0].listeners.click();
  assert(section.children[1].textContent.includes('No saves or saved states'));
  assert.strictEqual(section.children[1].children.length, 0);
  assert(requests.some(url => url.endsWith('?drone=living-room')));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
