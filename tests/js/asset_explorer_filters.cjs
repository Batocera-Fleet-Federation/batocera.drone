// Execute the actual explorer/upload functions with a small DOM and API fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('app/web/static/js/drone.js', 'utf8');
const slice = (start, end) => source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
const nodes = new Map();
const node = () => ({innerHTML: '', value: '', checked: false, disabled: false, focus() {}, querySelector() { return null; }, classList: {toggle() {}}});
for (const id of ['systems-explore-active-filters', 'systemsExploreSearch', 'systemsExploreBrowserPlayFilter', 'systemsExploreDuplicatesFilter']) nodes.set(id, node());
const document = {getElementById: id => nodes.get(id), querySelectorAll: () => []};
const window = {location: {hash: '#systems/explore', reload() { window.reloaded = true; }}};
const entries = [window.location.hash];
let cursor = 0;
const history = {
  pushState(_, __, hash) { entries.splice(++cursor); entries.push(hash); window.location.hash = hash; },
  replaceState(_, __, hash) { entries[cursor] = hash; window.location.hash = hash; },
};
const escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
const jsAttr = text => escapeHtml(JSON.stringify(text));
let responses = [], calls = [], toasts = [], refreshes = 0;
const api = async url => { calls.push(url); return responses.shift() || {roms: [], groups: [], count: 0, genres: []}; };
const showToast = (...args) => toasts.push(args);
const setLoading = () => {};
const content = {innerHTML: ''};
const clearSystemTheme = () => {};
const getSystemsData = async () => ({systems: [{name: 'snes', rom_count: 10}]});
const browserPlaySupportedSystems = async () => ({systems: {snes: 'snes'}});
const renderAssetTypeSwitcher = () => '';
const restoreMovieListScroll = () => {};
const API_BASE = '/v1/api';
const systemRomCache = {snes: {data: {roms: [{unique_id: 'one', stale: true}]}, loadedAt: 1}};
const renderRomMediaPage = async () => { refreshes++; };
let fetchResult;
const fetch = async (...args) => { calls.push(args); return await fetchResult; };
const FormData = global.FormData;
const run = eval('(function(){' +
  slice('const SYSTEMS_EXPLORE_PAGE_SIZE', '// Movies Browse (own top-level page') +
  slice('function systemsExploreHash(', 'function renderSystemsExploreSidebarShell(') +
  slice('async function setSystemsExploreSystem(', 'function renderSystemsExploreGrid(') +
  slice('const artworkAssetVersions', 'function artworkEditableImageFields(') +
  slice('function bindArtworkEditButtons(', 'function openMarqueeCropper(') + `
return async function test() {
  // Rendering helpers irrelevant to networking; active chips still use real markup.
  renderSystemsExploreSidebarShell = () => {};
  renderSystemsExploreSystemList = () => {};
  renderSystemsExploreCategoryList = () => {};
  renderSystemsExploreMissingArtList = () => {};
  renderSystemsExploreGrid = () => {};
  renderSystemsExploreMoreButton = () => {};
  systemsExploreBrowserPlayMap = {snes: 'snes'};
  systemsExploreSelectedSystem = 'snes'; systemsExploreSelectedGenre = 'Action';
  systemsExploreSearchQuery = '<img src=x onerror=alert(1)> & Sonic';
  systemsExploreMissingArt = ['image', 'video']; systemsExploreBrowserPlayOnly = true; systemsExploreDuplicatesMode = true;
  const original = systemsExploreHash();
  updateSystemsExploreHash();
  const parsed = parseSystemsExploreHash(original);
  assert.equal(parsed.query, systemsExploreSearchQuery);
  assert.deepEqual(parsed.missingArt, ['image', 'video']);
  assert.equal(parsed.duplicates, true); assert.equal(parsed.browserPlayable, true);
  assert(!parseSystemsExploreHash('#systems/explore?missing_art=bogus,image').missingArt.includes('bogus'));
  systemsExploreSelectedSystem = ''; systemsExploreMissingArt = []; systemsExploreDuplicatesMode = false;
  await renderSystemsExplorePage();
  assert.equal(systemsExploreSelectedSystem, 'snes');
  assert.deepEqual(systemsExploreMissingArt, ['image', 'video']);
  assert(systemsExploreDuplicatesMode && systemsExploreBrowserPlayOnly);
  assert(content.innerHTML.includes('value="&lt;img'));
  renderSystemsExploreActiveFilters();
  const chips = document.getElementById('systems-explore-active-filters').innerHTML;
  assert.equal((chips.match(/aria-label=/g) || []).length, 7);
  assert(!chips.includes('<img')); assert(chips.includes('&lt;img'));
  await removeSystemsExploreFilter('system');
  let params = new URLSearchParams(calls.at(-1).split('?')[1]);
  assert(!params.has('system')); assert.equal(params.get('genre'), 'Action');
  assert.equal(params.get('missing_art'), 'image,video'); assert.equal(params.get('browser_playable'), '1');
  assert.equal(parseSystemsExploreHash(window.location.hash).duplicates, true);
  await removeSystemsExploreFilter('missing_art', 'image');
  assert.deepEqual(parseSystemsExploreHash(window.location.hash).missingArt, ['video']);
  assert.equal(systemsExploreSelectedGenre, 'Action');
  // Back navigation/reload consume exactly the state recorded before removal.
  window.location.hash = entries[--cursor];
  await renderSystemsExplorePage();
  assert.deepEqual(systemsExploreMissingArt, ['image', 'video']);
  assert.equal(systemsExploreSelectedSystem, '');
  assert.equal(systemsExploreSelectedGenre, 'Action');
  await removeSystemsExploreFilter('q'); assert.equal(document.getElementById('systemsExploreSearch').value, '');
  assert.equal(systemsExploreSelectedGenre, 'Action');
  systemsExploreSelectedSystem = '__bios__'; systemsExploreDuplicatesMode = false; systemsExploreBrowserPlayOnly = false;
  await toggleSystemsExploreDuplicatesMode(); assert.equal(systemsExploreSelectedSystem, '');
  systemsExploreSelectedSystem = 'ps3'; await toggleSystemsExploreBrowserPlayOnly(); assert.equal(systemsExploreSelectedSystem, '');
  // A filter reset supersedes an earlier request even while it is loading.
  systemsExploreDuplicatesMode = false;
  let resolveOld; responses.push(new Promise(resolve => { resolveOld = resolve; }));
  const old = loadSystemsExploreRoms({reset: true});
  responses.push({roms: [{unique_id: 'new'}], count: 1, genres: []});
  await removeSystemsExploreFilter('genre');
  resolveOld({roms: [{unique_id: 'old'}], count: 1, genres: []}); await old;
  assert.equal(systemsExploreRoms[0].unique_id, 'new'); assert.equal(systemsExploreLoadingMore, false);
  systemsExploreSelectedSystem = ''; systemsExploreSelectedGenre = ''; systemsExploreSearchQuery = '';
  systemsExploreMissingArt = []; systemsExploreBrowserPlayOnly = false; systemsExploreDuplicatesMode = false;
  renderSystemsExploreActiveFilters(); assert.equal(document.getElementById('systems-explore-active-filters').innerHTML, '');

  // Change events submit only explicit selections, lock duplicates and permit retry.
  const status = {textContent: ''};
  const input = {files: [], value: '', disabled: false, getAttribute: () => 'video', closest: () => ({querySelector: () => status}), addEventListener(event, fn) { this[event] = fn; }};
  document.querySelectorAll = selector => selector === '.artwork-upload-file' ? [input] : [];
  const rom = {system: 'snes', unique_id: 'one', rom_path: './one.zip'};
  bindArtworkEditButtons(rom, 0); const before = calls.length;
  input.change(); assert.equal(calls.length, before);
  input.files = [new Blob(['movie'], {type: 'video/mp4'})];
  let resolveUpload; fetchResult = new Promise(resolve => { resolveUpload = resolve; });
  input.change();
  const uploading = new Promise(resolve => setImmediate(resolve));
  assert(input.disabled); assert.equal(status.textContent, 'Uploading video…');
  assert.equal(calls.at(-1)[0], '/v1/api/admin/artwork/upload');
  assert.equal(calls.at(-1)[1].credentials, 'include');
  assert.equal(calls.at(-1)[1].body.get('field'), 'video');
  assert.equal(calls.at(-1)[1].body.get('rom_path'), './one.zip');
  await uploadArtworkImage(rom, 'video', input); assert.equal(calls.length, before + 1);
  resolveUpload({status: 200, ok: true, json: async () => ({rom_name: 'One', existing: {video: './images/one-video.mp4'}})}); await uploading;
  // The refreshed detail page must render the updated ROM, not a list fetched before the upload.
  assert.equal(refreshes, 1); assert.equal(systemRomCache.snes.data.roms[0], rom); assert.equal(systemRomCache.snes.data.roms[0].existing.video, './images/one-video.mp4');
  assert(systemRomCache.snes.loadedAt > 1); assert(!input.disabled); assert.equal(input.value, '');
  // The replaced file is re-requested instead of served from the 1 h browser cache.
  assert(artworkExistingAssetUrl(rom, 'video', 'x.mp4').includes('?v='));
  assert(artworkExistingAssetUrl(rom, 'video', 'x.mp4').startsWith('/v1/api/public/systems/snes/video/'));
  assert(!artworkExistingImageUrl({system: 'snes'}, './images/other.png').includes('?v='));
  // Oversized files are refused up front with a readable message and no request.
  const bigCalls = calls.length;
  await uploadArtworkImage(rom, 'video', {files: [{size: 51 * 1024 * 1024}], value: 'x', closest: () => ({querySelector: () => status})});
  assert.equal(calls.length, bigCalls); assert(status.textContent.includes('too large'));
  fetchResult = {status: 400, ok: false, json: async () => ({error: '<bad file>'})};
  await uploadArtworkImage(rom, 'video', input);
  assert.equal(status.textContent, 'Upload failed: <bad file>'); assert(!input.disabled); assert.equal(refreshes, 1);
  fetchResult = {status: 401, ok: false}; await uploadArtworkImage(rom, 'video', input); assert(window.reloaded);
  let resolveLate; fetchResult = new Promise(resolve => { resolveLate = resolve; });
  const late = uploadArtworkImage(rom, 'video', input); window.location.hash = '#movies';
  resolveLate({status: 200, ok: true, json: async () => ({})}); await late;
  assert.equal(refreshes, 1);
 };` + '\n})');
run()()
  .then(() => console.log('Explorer filter and upload behavior passed.'))
  .catch(error => { console.error(error); process.exitCode = 1; });
