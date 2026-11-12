"""Transfer monitoring shows participant roles without loading peer inventories."""
import subprocess
import unittest
from pathlib import Path


class TransferMonitoringTests(unittest.TestCase):
    def test_participants_rows_and_modal_encode_peer_data(self):
        script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('app/web/static/js/drone.js', 'utf8');
const transferPayload = {target_drone_id: 'local'};
const uploadPayload = {target_drone_id: 'local'};
const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const formatBytes = value => String(value || 0);
let modal;
const document = {
  getElementById: () => null,
  createElement: () => (modal = {setAttribute() {}, addEventListener() {}}),
  body: {appendChild() {}}
};
const window = {bootstrap: {Modal: {getOrCreateInstance: () => ({show() {}})}}};
eval(source.slice(source.indexOf('function transferParticipants('), source.indexOf('function renderDownloadsPanel(')) + `
assert.deepEqual(transferParticipants({source_drone_id: 'source'}), {
 'Initiated by': 'local', Downloading: 'local', Uploading: 'source'
});
assert.deepEqual(transferParticipants({_direction: 'upload', peer_device_id: 'peer'}), {
 'Initiated by': 'peer', Downloading: 'peer', Uploading: 'local'
});
assert.equal(transferParticipants({_direction: 'upload'})['Initiated by'], 'Unknown');
const row = {source_drone_id: '<img src=x onerror=alert(1)>', file_path: '<script>bad</script>', error_message: '<svg onload=alert(1)>', percentage: 50};
const html = renderTransferRows([row]);
assert.match(html, /Initiated by/);
assert.match(html, /Downloading/);
assert.match(html, /Uploading/);
assert.match(html, /tabindex="0"/);
assert.doesNotMatch(html, /<img|<script|<svg/);
openTransferDetails(row);
assert.match(modal.innerHTML, /50.0%/);
assert.match(modal.innerHTML, /&lt;svg/);
assert.doesNotMatch(modal.innerHTML, /<img|<script|<svg/);
`);
const panel = source.slice(source.indexOf('async function renderIntegrationTransfersPanel('), source.indexOf('async function renderLocalTransferRequestPanel('));
assert.doesNotMatch(panel, /localTransferRequestPanel|loadSwarmOverview|renderLocalTransferRequestPanel/);
assert.match(panel, /addEventListener\("click"/);
assert.match(panel, /addEventListener\("keydown"/);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
