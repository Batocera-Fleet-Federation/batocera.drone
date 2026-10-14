// Multiplayer: dashboard, game browser, session lobby and settings.
//
// Defines renderers before drone.js starts the router; calls reuse api helpers (_apiRequestUrl,
// _handleApiUnauthorized), showToast/escapeHtml/setHash, content/titleNode/subtitleNode
// and the themed modal helpers (sdShowModal, sdConfirm). All interaction goes through
// delegated [data-mp-action] / [data-mp-change] handlers so no peer- or library-supplied
// string is ever interpolated into inline JavaScript, and every dynamic value is escaped.

const MP_API = "/admin/multiplayer";
const MP_CLASS_LABELS = {
  VERIFIED_COMPATIBLE: "Verified", LIKELY_COMPATIBLE: "Likely compatible", REQUIRES_CONFIGURATION: "Needs configuration",
  EXPERIMENTAL: "Experimental", INCOMPATIBLE: "Incompatible", UNSUPPORTED: "Unsupported", UNKNOWN: "Untested",
};
const MP_CLASS_TONES = {
  VERIFIED_COMPATIBLE: "success", LIKELY_COMPATIBLE: "info", REQUIRES_CONFIGURATION: "warning", EXPERIMENTAL: "warning",
  INCOMPATIBLE: "danger", UNSUPPORTED: "secondary", UNKNOWN: "secondary",
};
const MP_STATE_TONES = {
  CREATED: "secondary", DISCOVERING: "info", INVITING: "info", CONFIGURING: "info", WAITING_FOR_PLAYERS: "warning", READY: "success",
  STARTING: "primary", RUNNING: "success", DEGRADED: "warning", FAILED: "danger", STOPPING: "secondary", COMPLETED: "secondary",
};
const MP_PLAYER_TONES = {
  invited: "secondary", joined: "info", declined: "secondary", left: "secondary", removed: "secondary",
  launching: "primary", running: "success", failed: "danger", unreachable: "warning",
};
const MP_TIER_LABELS = { implemented: "Implemented", experimental: "Experimental", detect_only: "Detection only" };
const MP_STATUS_LABELS = { ready: "Ready to play", configuration: "Configuration required", untested: "Untested", unsupported: "Unsupported" };

let mpState = { timer: null, games: { system: "", q: "", mechanism: "", players: "", status: "", peer: "", offset: 0, limit: 25 }, systems: null, bound: false };

// ---------------------------------------------------------------- utilities
function mpEsc(value) {
  return escapeHtml(value === undefined || value === null ? "" : value);
}
function mpBadge(text, tone) {
  return `<span class="badge text-bg-${mpEsc(tone)}">${mpEsc(text)}</span>`;
}
function mpClassBadge(classification) {
  return mpBadge(MP_CLASS_LABELS[classification] || classification, MP_CLASS_TONES[classification] || "secondary");
}
function mpToast(message, type = "success") {
  showToast(mpEsc(message), type, type === "danger" ? 9000 : 5000);
}
function mpTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
function mpEncode(value) {
  return encodeURIComponent(String(value));
}

class MpError extends Error {
  constructor(message, status, code, details) {
    super(message);
    this.status = status;
    this.code = code || "";
    this.details = details || {};
  }
}

async function mpRequest(method, path, body) {
  const options = { method, credentials: "include" };
  if (method !== "GET") {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body || {});
  }
  const url = `${MP_API}${path}`;
  const res = await fetch(_apiRequestUrl(url), options);
  if (res.status === 401) return _handleApiUnauthorized(res, () => mpRequest(method, path, body));
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new MpError(data.error || `Request failed: ${res.status}`, res.status, data.code, data.details);
  return data;
}
const mpGet = (path) => mpRequest("GET", path);
const mpPost = (path, body) => mpRequest("POST", path, body || {});

function mpFail(error) {
  mpToast(error && error.message ? error.message : String(error || "Request failed"), "danger");
}

function mpStopPoll() {
  if (mpState.timer) clearInterval(mpState.timer);
  mpState.timer = null;
}
function mpPoll(fn, ms) {
  mpStopPoll();
  mpState.timer = setInterval(async () => {
    if (document.hidden || !window.location.hash.startsWith("#multiplayer")) return;
    if (document.querySelector(".modal.show")) return;
    if (document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    try { await fn(); } catch (_) { /* transient */ }
  }, ms);
}

function mpNav(active) {
  const links = [["#multiplayer", "Dashboard", "bi-speedometer2"], ["#multiplayer/games", "Games", "bi-controller"], ["#multiplayer/settings", "Settings", "bi-gear"]];
  return `<ul class="nav nav-tabs admin-panel-tabs mb-3">${links.map(([hash, label, icon]) =>
    `<li class="nav-item"><a class="nav-link ${active === hash ? "active" : ""}" href="${hash}"><i class="bi ${icon} me-1"></i>${label}</a></li>`).join("")}</ul>`;
}
function mpSpinner() {
  return `<div class="text-center py-5"><div class="spinner-border" role="status"></div></div>`;
}
function mpDisabledNotice() {
  return `<div class="alert alert-warning">Multiplayer is turned off. Enable it under <a href="#multiplayer/settings">Settings</a>.</div>`;
}

// -------------------------------------------------------------------- router
async function renderMultiplayerPage() {
  mpBind();
  mpStopPoll();
  const hash = window.location.hash;
  titleNode.textContent = "Multiplayer";
  subtitleNode.textContent = "Play together across your Drone swarm";
  content.innerHTML = mpSpinner();
  try {
    if (hash.startsWith("#multiplayer/session/")) return await mpRenderSession(decodeURIComponent(hash.split("/")[2] || ""));
    if (hash === "#multiplayer/settings") return await mpRenderSettings();
    if (hash.startsWith("#multiplayer/games")) return await mpRenderGames(decodeURIComponent(hash.split("/")[2] || ""));
    return await mpRenderDashboard();
  } catch (error) {
    content.innerHTML = `${mpNav("")}<div class="alert alert-danger">${mpEsc(error.message)}</div>`;
  }
}

// ----------------------------------------------------------------- dashboard
function mpPeersCard(peers) {
  if (!peers.length) return `<p class="text-muted mb-0">No paired Drones yet. Pair from the <a href="#admin/swarm">Swarm</a> page.</p>`;
  const rows = peers.map((peer) => `<tr>
      <td>${mpEsc(peer.name)}</td>
      <td>${peer.online ? mpBadge("Online", "success") : mpBadge("Offline", "secondary")}</td>
      <td>${mpBadge(peer.scope || "unknown", peer.scope === "lan" ? "info" : "secondary")}</td>
      <td>${peer.latency_ms === null || peer.latency_ms === undefined ? "—" : `${mpEsc(peer.latency_ms)} ms`}</td>
      <td class="text-end"><button class="btn btn-sm btn-outline-light" data-mp-action="test-peer" data-peer="${mpEsc(peer.device_id)}">Test</button></td></tr>`).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack mb-0"><thead><tr><th>Drone</th><th>Status</th><th>Route</th><th>Latency</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function mpSessionsCard(sessions) {
  if (!sessions.length) return `<p class="text-muted mb-0">No active sessions.</p>`;
  const rows = sessions.map((session) => `<tr>
      <td><a href="#multiplayer/session/${mpEncode(session.id)}">${mpEsc(session.game.title)}</a></td>
      <td>${mpEsc(session.mechanism_label || "—")}</td>
      <td>${mpBadge(session.state.replace(/_/g, " "), MP_STATE_TONES[session.state] || "secondary")}</td>
      <td>${mpEsc(session.is_host ? "You" : session.host_name)}</td></tr>`).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack mb-0"><thead><tr><th>Game</th><th>Mechanism</th><th>State</th><th>Host</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function mpInvitationsCard(invitations) {
  if (!invitations.length) return `<p class="text-muted mb-0">No pending invitations.</p>`;
  return invitations.map((inv) => `<div class="border rounded p-2 mb-2">
      <div><strong>${mpEsc(inv.host_name)}</strong> invited you to <strong>${mpEsc(inv.game.title)}</strong> <span class="text-muted">(${mpEsc(inv.game.system)})</span></div>
      <div class="small text-muted mb-2">${mpEsc(inv.mechanism_label)} · up to ${mpEsc(inv.max_players)} players · expires ${mpEsc(mpTime(inv.expires_at))}</div>
      <button class="btn btn-sm btn-success me-1" data-mp-action="accept" data-session="${mpEsc(inv.session_id)}">Accept</button>
      <button class="btn btn-sm btn-outline-light" data-mp-action="decline" data-session="${mpEsc(inv.session_id)}">Decline</button></div>`).join("");
}
function mpMechanismsCard(mechanisms) {
  const rows = mechanisms.map((item) => `<tr><td>${mpEsc(item.label)}</td><td>${item.available ? mpBadge("Available", "success") : mpBadge("Not available", "secondary")}</td><td class="small text-muted">${mpEsc(item.adapters.join(", ") || "—")}</td></tr>`).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack mb-0"><thead><tr><th>Mechanism</th><th>On this Drone</th><th>Via</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function mpAdaptersTable(adapters) {
  const rows = adapters.map((adapter) => `<tr>
      <td>${mpEsc(adapter.name)}</td>
      <td>${mpBadge(MP_TIER_LABELS[adapter.tier] || adapter.tier, adapter.tier === "implemented" ? "success" : adapter.tier === "experimental" ? "warning" : "secondary")}</td>
      <td>${adapter.installed ? mpBadge("Installed", "success") : mpBadge("Not found", "secondary")} <span class="small text-muted">${mpEsc(adapter.version || "")}</span></td>
      <td class="small">${mpEsc(adapter.readiness.message)}</td></tr>`).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack mb-0"><thead><tr><th>Emulator</th><th>Support</th><th>Detected</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function mpRecentCard(recent) {
  if (!recent.length) return `<p class="text-muted mb-0">Nothing yet.</p>`;
  return `<ul class="list-unstyled mb-0">${recent.map((item) => `<li class="mb-1">${mpEsc(item.title)} <span class="text-muted small">${mpEsc(item.system)} · ${mpEsc(item.outcome)} · ${mpEsc(mpTime(item.ended_at))}</span></li>`).join("")}</ul>`;
}

async function mpRenderDashboard() {
  const data = await mpGet("");
  const hosted = data.sessions.find((session) => session.is_host);
  content.innerHTML = `${mpNav("#multiplayer")}
    ${data.enabled ? "" : mpDisabledNotice()}
    <div class="d-flex flex-wrap gap-2 mb-3">
      <a class="btn btn-primary" href="#multiplayer/games"><i class="bi bi-plus-circle me-1"></i>Host Game</a>
      <a class="btn btn-outline-light" href="#multiplayer" data-mp-action="scroll-invitations"><i class="bi bi-box-arrow-in-right me-1"></i>Join Game${data.invitations.length ? ` <span class="badge text-bg-danger">${data.invitations.length}</span>` : ""}</a>
      <a class="btn btn-outline-light" href="${hosted ? `#multiplayer/session/${mpEncode(hosted.id)}` : "#multiplayer/games"}"><i class="bi bi-person-plus me-1"></i>Invite Players</a>
      <a class="btn btn-outline-light" href="#multiplayer/games"><i class="bi bi-list-check me-1"></i>View Compatible Games</a>
      <a class="btn btn-outline-light" href="#multiplayer/settings"><i class="bi bi-gear me-1"></i>Multiplayer Settings</a>
    </div>
    <div class="row g-3">
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Swarm members <span class="small text-muted">(${data.online_peers} online)</span></h2>${mpPeersCard(data.peers)}</div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Active sessions</h2>${mpSessionsCard(data.sessions)}</div></section></div>
      <div class="col-12 col-xl-6" id="mpInvitations"><section class="card h-100"><div class="card-body"><h2 class="h5">Pending invitations</h2>${mpInvitationsCard(data.invitations)}</div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Games on several machines</h2><div id="mpShared" class="text-muted">Loading…</div></div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Supported mechanisms</h2>${mpMechanismsCard(data.mechanisms)}</div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Emulator compatibility</h2>${mpAdaptersTable(data.adapters)}</div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Recently played</h2>${mpRecentCard(data.recent)}</div></section></div>
      <div class="col-12 col-xl-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Network</h2>
        <p class="mb-1">Preferred transport: <strong>${mpEsc(data.network.transport)}</strong> · Relay: <strong>${mpEsc(data.network.relay)}</strong> · Controllers detected: <strong>${mpEsc(data.network.controllers)}</strong></p>
        <p class="small text-muted mb-0">Sessions use your existing paired, certificate-pinned connections. Tailscale carries unicast game traffic; games that discover each other by LAN broadcast need the same local network.</p></div></section></div>
    </div>`;
  decorateStackTables(content);
  mpLoadShared();
  mpPoll(async () => {
    if (window.location.hash === "#multiplayer") await mpRenderDashboard();
  }, 15000);
}
function mpSharedHtml(games) {
  if (!games.length) return "No shared multiplayer games found with online peers.";
  return `<ul class="list-unstyled mb-0">${games.map((game) => `<li class="mb-1">
    <a href="#multiplayer/games/${mpEncode(game.system)}" data-mp-action="open-game" data-system="${mpEsc(game.system)}" data-game="${mpEsc(game.id)}">${mpEsc(game.title)}</a>
    <span class="text-muted small">${mpEsc(game.system)} · ${mpEsc(game.peers.map((peer) => peer.name).join(", "))}</span></li>`).join("")}</ul>`;
}
async function mpLoadShared() {
  const node = document.getElementById("mpShared");
  if (!node) return;
  if (mpState.shared) node.innerHTML = mpSharedHtml(mpState.shared);  // show the last result while refreshing
  try {
    mpState.shared = (await mpGet("/games/shared")).games;
    const fresh = document.getElementById("mpShared");
    if (fresh) fresh.innerHTML = mpSharedHtml(mpState.shared);
  } catch (error) {
    node.textContent = error.message;
  }
}

// -------------------------------------------------------------- game browser
async function mpRenderGames(systemFromHash) {
  if (!mpState.systems) mpState.systems = (await mpGet("/games/systems")).systems;
  if (systemFromHash) mpState.games.system = systemFromHash;
  const filters = mpState.games;
  const systemOptions = [`<option value="">Choose a system…</option>`].concat(mpState.systems.map((item) =>
    `<option value="${mpEsc(item.system)}" ${item.system === filters.system ? "selected" : ""}>${mpEsc(item.system)}${item.native ? " ●" : ""}</option>`)).join("");
  const mechanismOptions = [`<option value="">Any mechanism</option>`].concat(Object.keys(MP_MECHANISM_LABELS).map((key) =>
    `<option value="${key}" ${filters.mechanism === key ? "selected" : ""}>${mpEsc(MP_MECHANISM_LABELS[key])}</option>`)).join("");
  const statusOptions = [`<option value="">Any status</option>`].concat(Object.keys(MP_STATUS_LABELS).map((key) =>
    `<option value="${key}" ${filters.status === key ? "selected" : ""}>${mpEsc(MP_STATUS_LABELS[key])}</option>`)).join("");
  const playerOptions = ["", "2", "3", "4", "6", "8"].map((value) => `<option value="${value}" ${filters.players === value ? "selected" : ""}>${value ? `${value}+ players` : "Any players"}</option>`).join("");
  content.innerHTML = `${mpNav("#multiplayer/games")}
    <div class="row g-2 mb-3">
      <div class="col-6 col-md-3"><select class="form-select" data-mp-change="filter" data-filter="system" aria-label="System">${systemOptions}</select></div>
      <div class="col-6 col-md-3"><input class="form-control" type="search" placeholder="Search titles" value="${mpEsc(filters.q)}" data-mp-change="filter" data-filter="q" aria-label="Search"></div>
      <div class="col-6 col-md-2"><select class="form-select" data-mp-change="filter" data-filter="mechanism" aria-label="Mechanism">${mechanismOptions}</select></div>
      <div class="col-6 col-md-2"><select class="form-select" data-mp-change="filter" data-filter="status" aria-label="Status">${statusOptions}</select></div>
      <div class="col-6 col-md-2"><select class="form-select" data-mp-change="filter" data-filter="players" aria-label="Players">${playerOptions}</select></div>
      <div class="col-12 col-md-4"><select class="form-select" id="mpPeerFilter" data-mp-change="filter" data-filter="peer" aria-label="Compatible peer"><option value="">Any peer</option></select></div>
    </div>
    <p class="small text-muted">A game is only marked <strong>Ready</strong> when the mechanism is installed and its core/emulator reports what netplay needs. A console being multiplayer-capable never makes a game “verified”: that needs a session you confirmed worked.</p>
    <div id="mpGames">${filters.system ? mpSpinner() : `<p class="text-muted">Pick a system to see its multiplayer candidates.</p>`}</div>`;
  mpFillPeerFilter();
  if (filters.system) await mpLoadGames();
}
const MP_MECHANISM_LABELS = {
  RETROARCH_NETPLAY: "RetroArch Netplay", DOLPHIN_NETPLAY: "Dolphin Netplay", PPSSPP_ADHOC: "PPSSPP ad hoc", SWITCH_LDN: "Switch LDN / LAN play",
  EMULATED_LAN: "Emulated LAN", EMULATOR_NATIVE_NETWORK: "Emulator-native network", REMOTE_STREAMING: "Remote streaming",
};
async function mpFillPeerFilter() {
  const select = document.getElementById("mpPeerFilter");
  if (!select) return;
  try {
    const peers = (await mpGet("/peers")).peers;
    select.innerHTML = `<option value="">Any peer</option>` + peers.map((peer) => `<option value="${mpEsc(peer.device_id)}" ${peer.device_id === mpState.games.peer ? "selected" : ""}>${mpEsc(peer.name)}${peer.online ? "" : " (offline)"}</option>`).join("");
  } catch (_) { /* optional */ }
}
async function mpLoadGames() {
  const filters = mpState.games;
  const params = new URLSearchParams({ system: filters.system, limit: String(filters.limit), offset: String(filters.offset), include_peers: "1" });
  ["q", "mechanism", "status", "players", "peer"].forEach((key) => { if (filters[key]) params.set(key, filters[key]); });
  const node = document.getElementById("mpGames");
  try {
    const data = await mpGet(`/games?${params.toString()}`);
    if (!data.games.length) { node.innerHTML = `<p class="text-muted">No games match these filters.</p>`; return; }
    const rows = data.games.map((game) => `<tr>
      <td><strong>${mpEsc(game.title)}</strong><div class="small text-muted">${mpEsc(game.rom_name)}</div></td>
      <td>${game.players ? mpEsc(game.players) : "—"}</td>
      <td>${mpBadge(MP_STATUS_LABELS[game.status] || game.status, game.status === "ready" ? "success" : game.status === "configuration" ? "warning" : "secondary")}</td>
      <td>${game.mechanisms.filter((item) => item.classification !== "UNSUPPORTED").map((item) => `<span class="me-1" title="${mpEsc(item.reasons.join(' '))}">${mpEsc(item.label)} ${mpClassBadge(item.classification)}</span>`).join("<br>") || `<span class="text-muted">None usable</span>`}</td>
      <td class="small">${game.peers.map((peer) => `${mpEsc(peer.name)} <span class="text-muted">(${mpEsc(peer.level.replace("_", " "))})</span>`).join("<br>") || "—"}</td>
      <td class="text-end"><button class="btn btn-sm btn-primary" data-mp-action="open-game" data-system="${mpEsc(game.system)}" data-game="${mpEsc(game.id)}">Host…</button></td></tr>`).join("");
    const pager = `<div class="d-flex justify-content-between align-items-center mt-2">
      <button class="btn btn-sm btn-outline-light" data-mp-action="page" data-delta="-1" ${filters.offset <= 0 ? "disabled" : ""}>Previous</button>
      <span class="small text-muted">${filters.offset + 1}–${Math.min(filters.offset + data.games.length, data.total)} of ${data.total}${data.truncated ? " (peer filter checked the first 200)" : ""}</span>
      <button class="btn btn-sm btn-outline-light" data-mp-action="page" data-delta="1" ${filters.offset + filters.limit >= data.total ? "disabled" : ""}>Next</button></div>`;
    node.innerHTML = `<div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack"><thead><tr><th>Game</th><th>Players</th><th>Status</th><th>Mechanisms</th><th>Peers with a copy</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${pager}`;
    decorateStackTables(node);
  } catch (error) {
    node.innerHTML = `<div class="alert alert-danger">${mpEsc(error.message)}</div>`;
  }
}

async function mpOpenHostDialog(system, gameId) {
  let detail;
  try { detail = await mpGet(`/games/${mpEncode(system)}/${mpEncode(gameId)}`); } catch (error) { mpFail(error); return; }
  const online = (await mpGet("/peers")).peers.filter((peer) => peer.online);
  const rec = detail.recommendation;
  const usableMechanisms = rec.candidates.filter((item) => item.usable);
  const cores = (detail.assessments.find((a) => a.mechanism === "RETROARCH_NETPLAY") || {}).candidate_cores || [];
  const candidateRows = rec.candidates.map((item) => `<li class="mb-1">${mpEsc(item.label)} ${mpClassBadge(item.classification)} ${item.usable ? mpBadge("usable", "success") : mpBadge("blocked", "danger")}
      ${item.blockers.length ? `<div class="small text-warning">${item.blockers.map(mpEsc).join("<br>")}</div>` : ""}${item.warnings.length ? `<div class="small text-muted">${item.warnings.map(mpEsc).join("<br>")}</div>` : ""}</li>`).join("");
  const peerBoxes = online.length ? online.map((peer) => {
    const report = detail.peers.find((item) => item.device_id === peer.device_id) || {};
    return `<div class="form-check"><input class="form-check-input" type="checkbox" id="mpInv-${mpEsc(peer.device_id)}" value="${mpEsc(peer.device_id)}" data-mp-invite>
      <label class="form-check-label" for="mpInv-${mpEsc(peer.device_id)}">${mpEsc(peer.name)} <span class="small text-muted">${report.reachable ? `copy: ${mpEsc((report.level || "none").replace("_", " "))}` : "not reachable"}</span></label></div>`;
  }).join("") : `<p class="text-muted mb-0">No paired Drone is online.</p>`;
  const body = `<div class="mb-3"><strong>${mpEsc(detail.game.title)}</strong> <span class="text-muted">${mpEsc(system)}</span></div>
    <div class="alert ${rec.mechanism ? "alert-info" : "alert-warning"} small">${mpEsc(rec.reason)}</div>
    <h3 class="h6">Why each mechanism is (not) usable</h3><ul class="list-unstyled">${candidateRows}</ul>
    <h3 class="h6">Invite players</h3>${peerBoxes}
    <details class="mt-3"><summary>Advanced</summary>
      <label class="form-label mt-2" for="mpMechanism">Mechanism (overrides the automatic choice; unusable ones are refused)</label>
      <select class="form-select" id="mpMechanism"><option value="">Automatic</option>${usableMechanisms.map((item) => `<option value="${mpEsc(item.mechanism)}">${mpEsc(item.label)}</option>`).join("")}</select>
      ${cores.length ? `<label class="form-label mt-2" for="mpCore">RetroArch core for this session</label><select class="form-select" id="mpCore"><option value="">As configured in Batocera</option>${cores.map((core) => `<option value="${mpEsc(core)}">${mpEsc(core)}</option>`).join("")}</select>` : ""}
    </details>`;
  const modal = sdShowModal("mpHostModal", "Host a multiplayer session", body,
    `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button><button type="button" class="btn btn-primary" id="mpCreateBtn">Create session</button>`, "modal-lg");
  modal.querySelector("#mpCreateBtn").addEventListener("click", async (event) => {
    event.target.disabled = true;
    const payload = { system, game_id: gameId, peers: Array.from(modal.querySelectorAll("[data-mp-invite]:checked")).map((box) => box.value),
      client_request_id: `ui-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}` };
    const mechanism = modal.querySelector("#mpMechanism").value;
    if (mechanism) payload.mechanism = mechanism;
    const core = modal.querySelector("#mpCore");
    if (core && core.value) payload.emulator_override = { core: core.value };
    try {
      const session = await mpPost("/sessions", payload);
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
      setHash(`#multiplayer/session/${mpEncode(session.id)}`);
    } catch (error) {
      event.target.disabled = false;
      mpFail(error);
    }
  });
}

// --------------------------------------------------------------------- lobby
function mpPlayerRows(session) {
  const players = Object.values(session.players).sort((a, b) => (a.slot || 99) - (b.slot || 99));
  const slotOptions = (player) => Array.from({ length: session.max_players }, (_, index) => index + 1)
    .map((slot) => `<option value="${slot}" ${player.slot === slot ? "selected" : ""}>P${slot}</option>`).join("");
  const canEdit = session.actions.includes("assign_slot");
  return players.map((player) => {
    const isMe = player.device_id === (session.my_player || {}).device_id;
    const slotCell = canEdit && player.slot && !["declined", "left", "removed"].includes(player.status)
      ? `<select class="form-select form-select-sm" data-mp-change="slot" data-session="${mpEsc(session.id)}" data-peer="${mpEsc(player.device_id)}" aria-label="Player slot">${slotOptions(player)}</select>`
      : (player.slot ? `P${mpEsc(player.slot)}` : "—");
    const actions = [];
    if (session.actions.includes("remove_player") && player.role !== "host" && !["declined", "left", "removed"].includes(player.status)) {
      actions.push(`<button class="btn btn-sm btn-outline-danger" data-mp-action="remove" data-session="${mpEsc(session.id)}" data-peer="${mpEsc(player.device_id)}">Remove</button>`);
    }
    if (session.actions.includes("retry") && ["failed", "unreachable"].includes(player.status)) {
      actions.push(`<button class="btn btn-sm btn-outline-warning" data-mp-action="retry" data-session="${mpEsc(session.id)}" data-peer="${mpEsc(player.device_id)}">Retry</button>`);
    }
    return `<tr>
      <td>${slotCell}</td>
      <td>${mpEsc(player.name)} ${player.role === "host" ? mpBadge("Host", "primary") : ""} ${isMe ? mpBadge("You", "info") : ""}${player.error ? `<div class="small text-danger">${mpEsc(player.error)}</div>` : ""}</td>
      <td>${mpBadge(player.status, MP_PLAYER_TONES[player.status] || "secondary")}</td>
      <td>${player.ready ? mpBadge("Ready", "success") : mpBadge("Not ready", "secondary")}</td>
      <td>${player.controllers === null || player.controllers === undefined ? "—" : mpEsc(player.controllers)}</td>
      <td>${player.latency_ms === null || player.latency_ms === undefined ? "—" : `${mpEsc(player.latency_ms)} ms`} ${player.route ? `<span class="small text-muted">${mpEsc(player.route)}</span>` : ""}</td>
      <td>${mpClassBadge((player.compat || {}).classification || "UNKNOWN")}</td>
      <td class="text-end">${actions.join(" ")}</td></tr>`;
  }).join("");
}

function mpLobbyControls(session, invitable) {
  const mine = session.my_player || {};
  const buttons = [];
  if (session.actions.includes("set_ready") && mine.status === "joined") {
    buttons.push(`<button class="btn ${mine.ready ? "btn-outline-light" : "btn-success"}" data-mp-action="ready" data-session="${mpEsc(session.id)}" data-ready="${mine.ready ? "false" : "true"}">${mine.ready ? "Not ready" : "I'm ready"}</button>`);
  }
  if (session.actions.includes("start")) buttons.push(`<button class="btn btn-primary" data-mp-action="start" data-session="${mpEsc(session.id)}"><i class="bi bi-play-fill me-1"></i>Play Together</button>`);
  if (session.actions.includes("launch")) buttons.push(`<button class="btn btn-primary" data-mp-action="launch" data-session="${mpEsc(session.id)}"><i class="bi bi-play-fill me-1"></i>Launch on this Drone</button>`);
  if (session.actions.includes("retry")) buttons.push(`<button class="btn btn-outline-warning" data-mp-action="retry" data-session="${mpEsc(session.id)}">Retry</button>`);
  if (session.actions.includes("leave")) buttons.push(`<button class="btn btn-outline-light" data-mp-action="leave" data-session="${mpEsc(session.id)}">Leave session</button>`);
  if (session.actions.includes("end")) buttons.push(`<button class="btn btn-outline-danger" data-mp-action="end" data-session="${mpEsc(session.id)}">End session</button>`);
  return buttons.join(" ");
}

async function mpRenderSession(sessionId) {
  const [session, peers] = await Promise.all([mpGet(`/sessions/${mpEncode(sessionId)}`), mpGet("/peers")]);
  const taken = new Set(Object.values(session.players).filter((p) => !["declined", "left", "removed"].includes(p.status)).map((p) => p.device_id));
  const invitable = peers.peers.filter((peer) => !taken.has(peer.device_id));
  const game = session.game;
  const ended = session.state === "COMPLETED" || Boolean(session.local && session.local.ended);
  content.innerHTML = `${mpNav("")}
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
      <h2 class="h4 mb-0">${mpEsc(game.title)}</h2>
      ${mpBadge(session.state.replace(/_/g, " "), MP_STATE_TONES[session.state] || "secondary")}
      <span class="text-muted">${mpEsc(game.system)} · hosted by ${mpEsc(session.is_host ? "you" : session.host_name)}</span>
    </div>
    ${session.error ? `<div class="alert alert-danger">${mpEsc(session.error)}</div>` : ""}
    ${session.local && session.local.ended ? `<div class="alert alert-secondary">This session is over${session.local.ended_reason ? `: ${mpEsc(session.local.ended_reason)}` : ""}. A game that is still running on this Drone is left open for you to close.</div>` : ""}
    ${session.local && session.local.host_unreachable ? `<div class="alert alert-warning">The host is not answering. This session ends automatically if it stays unreachable.</div>` : ""}
    <div class="row g-3">
      <div class="col-12 col-xl-8">
        <section class="card mb-3"><div class="card-body">
          <h3 class="h6">Players <span class="text-muted">(${Object.values(session.players).filter((p) => ["joined", "launching", "running", "invited"].includes(p.status)).length}/${mpEsc(session.max_players)})</span></h3>
          <div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack mb-2"><thead><tr><th>Requested slot</th><th>Player</th><th>Status</th><th>Ready</th><th>Controllers</th><th>Latency</th><th>Compatibility</th><th></th></tr></thead><tbody>${mpPlayerRows(session)}</tbody></table></div>
          <div class="d-flex flex-wrap gap-2">${mpLobbyControls(session)}</div>
          ${session.actions.includes("invite") ? `<div class="input-group mt-3"><select class="form-select" id="mpInviteSelect" aria-label="Invite a peer"><option value="">Invite a peer…</option>${invitable.map((peer) => `<option value="${mpEsc(peer.device_id)}">${mpEsc(peer.name)}${peer.online ? "" : " (offline)"}</option>`).join("")}</select><button class="btn btn-outline-light" data-mp-action="invite" data-session="${mpEsc(session.id)}">Invite</button></div>` : ""}
        </div></section>
        ${session.my_steps.length ? `<section class="card mb-3"><div class="card-body"><h3 class="h6">Your steps</h3><ol class="mb-0">${session.my_steps.map((step) => `<li>${mpEsc(step)}</li>`).join("")}</ol></div></section>` : ""}
        ${session.state === "COMPLETED" ? `<section class="card mb-3"><div class="card-body"><h3 class="h6">Did it work?</h3><p class="small text-muted">Your answer updates this machine's compatibility record for this exact emulator version and configuration only.</p>
          <button class="btn btn-sm btn-success me-1" data-mp-action="result" data-session="${mpEsc(session.id)}" data-success="true">It worked</button>
          <button class="btn btn-sm btn-outline-danger" data-mp-action="result" data-session="${mpEsc(session.id)}" data-success="false">It didn't</button></div></section>` : ""}
      </div>
      <div class="col-12 col-xl-4">
        <section class="card mb-3"><div class="card-body">
          <h3 class="h6">Mechanism</h3>
          <p class="mb-1"><strong>${mpEsc(session.mechanism_label || "Not chosen")}</strong></p>
          <p class="small text-muted">${mpEsc(session.mechanism_reason)}</p>
          <p class="small mb-0">Emulator: ${mpEsc(session.emulator.name || "—")} ${session.emulator.core ? `· core ${mpEsc(session.emulator.core)}` : ""} ${session.emulator.version ? `· ${mpEsc(session.emulator.version)}` : ""}</p>
          ${Object.keys(session.stream_profile || {}).length ? `<p class="small mb-0">Stream profile: ${mpEsc(session.stream_profile.resolution)} @ ${mpEsc(session.stream_profile.fps)} FPS, ≤ ${mpEsc(session.stream_profile.bitrate_kbps)} kbit/s, ${mpEsc(session.stream_profile.encoder)} encoding</p>` : ""}
        </div></section>
        <section class="card mb-3"><div class="card-body"><h3 class="h6">Session log</h3>
          <ul class="list-unstyled small mb-0">${(session.events || []).slice(-8).reverse().map((event) => `<li class="mb-1"><span class="text-muted">${mpEsc(mpTime(event.at))}</span> ${mpEsc(event.message)}</li>`).join("") || "<li>No events yet.</li>"}</ul></div></section>
        <section class="card"><div class="card-body"><h3 class="h6">Diagnostics</h3>
          <button class="btn btn-sm btn-outline-light" data-mp-action="diagnostics" data-session="${mpEsc(session.id)}">Run diagnostics</button>
          <div id="mpDiagnostics" class="small mt-2"></div></div></section>
      </div>
    </div>`;
  decorateStackTables(content);
  if (!ended) {
    mpPoll(async () => {
      if (!window.location.hash.startsWith(`#multiplayer/session/`)) return;
      await mpRenderSession(sessionId);
    }, 3000);
  }
}

async function mpDiagnostics(sessionId) {
  const node = document.getElementById("mpDiagnostics");
  node.textContent = "Checking…";
  try {
    const data = await mpGet(`/sessions/${mpEncode(sessionId)}/diagnostics`);
    const rows = data.connectivity.map((peer) => `<tr><td>${mpEsc(peer.name)}</td><td>${peer.reachable ? mpBadge("reachable", "success") : mpBadge("unreachable", "danger")}</td><td>${mpEsc(peer.scope)}</td><td>${peer.latency_ms === null || peer.latency_ms === undefined ? "—" : `${mpEsc(peer.latency_ms)} ms`}</td><td>${mpEsc((peer.advice || []).join(" ") || peer.error || "")}</td></tr>`).join("");
    node.innerHTML = `${data.problems.length ? `<div class="text-warning mb-2">${data.problems.map(mpEsc).join("<br>")}</div>` : `<div class="text-success mb-2">No blocking problems on this machine.</div>`}
      ${rows ? `<div class="table-responsive"><table class="table table-sm themed-table bff-stack"><thead><tr><th>Peer</th><th>Link</th><th>Route</th><th>RTT</th><th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
      <div>Settings changed for this session: ${data.configuration.pending_restore ? mpEsc(data.configuration.files.map((f) => `${f.file} (${f.keys.length})`).join(", ")) + " — not yet restored" : "none (or already restored)"}</div>
      ${data.active_game ? `<div>Game running here: ${mpEsc(data.active_game.name)}</div>` : ""}`;
    decorateStackTables(node);
  } catch (error) {
    node.textContent = error.message;
  }
}

// ------------------------------------------------------------------ settings
async function mpRenderSettings() {
  const [settings, adapters, peers] = await Promise.all([mpGet("/settings"), mpGet("/adapters"), mpGet("/peers")]);
  const check = (section, key, label, help = "") => `<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="mp-${section}-${key}" data-mp-setting="${section}.${key}" ${settings[section][key] ? "checked" : ""}><label class="form-check-label" for="mp-${section}-${key}">${mpEsc(label)}</label>${help ? `<div class="small text-muted">${mpEsc(help)}</div>` : ""}</div>`;
  const select = (section, key, label, options) => `<div class="mb-2"><label class="form-label" for="mp-${section}-${key}">${mpEsc(label)}</label><select class="form-select" id="mp-${section}-${key}" data-mp-setting="${section}.${key}">${options.map(([value, text]) => `<option value="${mpEsc(value)}" ${String(settings[section][key]) === String(value) ? "selected" : ""}>${mpEsc(text)}</option>`).join("")}</select></div>`;
  const number = (section, key, label, min, max) => `<div class="mb-2"><label class="form-label" for="mp-${section}-${key}">${mpEsc(label)}</label><input class="form-control" type="number" min="${min}" max="${max}" id="mp-${section}-${key}" data-mp-setting="${section}.${key}" value="${mpEsc(settings[section][key])}"></div>`;
  const inviterBoxes = peers.peers.map((peer) => `<div class="form-check"><input class="form-check-input" type="checkbox" data-mp-inviter="${mpEsc(peer.device_id)}" id="mp-inv-${mpEsc(peer.device_id)}" ${settings.privacy.allowed_inviters.includes(peer.device_id) ? "checked" : ""}><label class="form-check-label" for="mp-inv-${mpEsc(peer.device_id)}">${mpEsc(peer.name)}</label></div>`).join("") || `<p class="text-muted small">No paired peers.</p>`;
  const adapterRows = adapters.adapters.map((adapter) => `<tr>
      <td>${mpEsc(adapter.name)}<div class="small text-muted">${mpEsc(adapter.limitations.slice(0, 2).join(" · "))}</div></td>
      <td>${mpBadge(MP_TIER_LABELS[adapter.tier] || adapter.tier, adapter.tier === "implemented" ? "success" : adapter.tier === "experimental" ? "warning" : "secondary")}</td>
      <td>${adapter.installed ? mpBadge("Installed", "success") : mpBadge("Not found", "secondary")} <span class="small">${mpEsc(adapter.version || "")}</span><div class="small text-muted">${mpEsc(adapter.readiness.message)}</div></td>
      <td><div class="form-check form-switch"><input class="form-check-input" type="checkbox" data-mp-adapter="${mpEsc(adapter.id)}" id="mp-ad-${mpEsc(adapter.id)}" ${adapter.enabled ? "checked" : ""} aria-label="Enable ${mpEsc(adapter.name)}"></div></td></tr>`).join("");
  const overrides = Object.entries(settings.emulators.core_overrides).map(([system, core]) => `${system}=${core}`).join("\n");
  content.innerHTML = `${mpNav("#multiplayer/settings")}
    <div class="row g-3">
      <div class="col-12 col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">General</h2>
        ${check("general", "enabled", "Enable multiplayer")}
        ${check("general", "invitations_enabled", "Accept invitations from peers")}
        ${check("general", "auto_accept_invitations", "Automatically accept invitations", "Only joins the lobby. A game is never launched without your confirmation (unless you choose Automatic under Privacy).")}
        ${check("general", "auto_restore_configuration", "Restore emulator settings automatically", "Turn off only to inspect changes; use “Restore” on a session afterwards.")}
        ${select("general", "preferred_mechanism", "Preferred mechanism", [["", "Automatic"]].concat(Object.entries(MP_MECHANISM_LABELS)))}
        ${number("general", "max_simultaneous_sessions", "Maximum simultaneous sessions", 1, 4)}
        ${number("general", "session_timeout_minutes", "Session timeout (minutes)", 5, 1440)}
        ${number("general", "invitation_ttl_minutes", "Invitation lifetime (minutes)", 1, 240)}
        <button class="btn btn-primary" data-mp-action="save-settings" data-section="general">Save general</button></div></section></div>
      <div class="col-12 col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Networking</h2>
        ${select("networking", "transport", "Preferred connection transport", [["auto", "Automatic"], ["lan", "Local network"], ["tailnet", "Tailscale"]])}
        ${number("networking", "port_range_start", "Session port range start", 1024, 65535)}
        ${number("networking", "port_range_end", "Session port range end", 1024, 65535)}
        <p class="small text-muted">Relay: none. Drone ships no broadcast relay; LAN-broadcast games need one local network. Game ports are opened per session by the emulator and never by Drone's firewall settings.</p>
        <div class="input-group mb-2"><select class="form-select" id="mpTestPeer" aria-label="Peer to test">${peers.peers.map((peer) => `<option value="${mpEsc(peer.device_id)}">${mpEsc(peer.name)}</option>`).join("")}</select><button class="btn btn-outline-light" data-mp-action="test-peer-select">Test connectivity</button></div>
        <div id="mpConnectivity" class="small mb-2"></div>
        <button class="btn btn-primary" data-mp-action="save-settings" data-section="networking">Save networking</button></div></section></div>
      <div class="col-12"><section class="card"><div class="card-body"><h2 class="h5">Emulators</h2>
        <div class="table-responsive"><table class="table table-sm align-middle themed-table bff-stack"><thead><tr><th>Adapter</th><th>Support</th><th>Detected</th><th>Enabled</th></tr></thead><tbody>${adapterRows}</tbody></table></div>
        <label class="form-label" for="mpCoreOverrides">Core overrides (one <code>system=core</code> per line; applied to this session only, your Batocera choice is untouched)</label>
        <textarea class="form-control mb-2" id="mpCoreOverrides" rows="3" spellcheck="false">${mpEsc(overrides)}</textarea>
        <button class="btn btn-primary me-1" data-mp-action="save-emulators">Save emulators</button>
        <button class="btn btn-outline-light" data-mp-action="redetect">Re-detect emulators</button></div></section></div>
      <div class="col-12 col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Remote streaming</h2>
        <p class="small text-muted">Uses Sunshine (host) and Moonlight (guest). Drone does not stream video itself.</p>
        ${check("streaming", "enabled", "Enable remote streaming")}
        ${select("streaming", "quality", "Preferred quality", [["low_latency", "Low latency"], ["balanced", "Balanced"], ["quality", "Best quality"]])}
        ${select("streaming", "max_resolution", "Maximum resolution", ["854x480", "1280x720", "1920x1080", "2560x1440", "3840x2160"].map((value) => [value, value]))}
        ${number("streaming", "max_fps", "Maximum frame rate", 15, 120)}
        ${number("streaming", "max_bitrate_kbps", "Bitrate limit (kbit/s)", 500, 150000)}
        ${select("streaming", "encoding", "Encoding", [["auto", "Automatic"], ["hardware", "Prefer hardware"], ["software", "Prefer software"]])}
        <button class="btn btn-primary" data-mp-action="save-settings" data-section="streaming">Save streaming</button></div></section></div>
      <div class="col-12 col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Privacy and security</h2>
        ${select("privacy", "invite_permission", "Who can invite this Drone", [["all_paired", "Any paired peer"], ["allowlist", "Only the peers below"], ["none", "Nobody"]])}
        <div class="mb-2"><div class="form-label">Trusted peers allowed to invite</div>${inviterBoxes}</div>
        ${select("privacy", "local_launch_authorization", "Launching a game here", [["confirm", "Ask me every time (recommended)"], ["auto", "Automatic when no other game is running"]])}
        ${select("privacy", "remote_controller_authorization", "Remote controllers", [["host_assigned", "Allow streaming invitations (Sunshine manages input)"], ["disabled", "Disable streaming invitations"]])}
        <p class="small text-muted">Drone does not revoke existing Sunshine pairings or enforce its controller slots. Configure Sunshine for controller-only input; disable keyboard/mouse access there. Disconnect Moonlight when leaving a session.</p>
        ${check("privacy", "session_logging", "Keep an audit log of session events")}
        <button class="btn btn-primary" data-mp-action="save-privacy">Save privacy</button></div></section></div>
    </div>`;
  decorateStackTables(content);
}

function mpReadSection(section) {
  const values = {};
  document.querySelectorAll(`[data-mp-setting^="${section}."]`).forEach((input) => {
    const key = input.dataset.mpSetting.split(".")[1];
    if (input.type === "checkbox") values[key] = input.checked;
    else if (input.type === "number") values[key] = Number(input.value);
    else values[key] = input.value;
  });
  return values;
}
async function mpSaveSettings(patch, message = "Settings saved") {
  await mpPost("/settings", patch);
  mpToast(message);
  mpState.systems = null;
  await mpRenderSettings();
}

// -------------------------------------------------------------------- actions
async function mpWithReplaceConfirm(request, runningTitle) {
  try {
    return await request(false);
  } catch (error) {
    if (error.code !== "game_running") throw error;
    const running = (error.details && error.details.running) || {};
    const ok = await sdConfirm(runningTitle, `<p>${mpEsc(running.name || "A game")} is running on this Drone${running.system ? ` (${mpEsc(running.system)})` : ""}. Continuing will close it. Unsaved progress will be lost.</p>`, "Close it and continue", "danger");
    if (!ok) return null;
    return request(true);
  }
}

async function mpOnAction(action, element) {
  const session = element.dataset.session;
  switch (action) {
    case "test-peer": {
      const result = await mpPost("/connectivity/test", { peer_id: element.dataset.peer });
      mpToast(result.reachable ? `Reachable over ${result.scope}: ${result.latency_ms} ms` : `Unreachable: ${result.error}`, result.reachable ? "success" : "danger");
      return;
    }
    case "test-peer-select": {
      const peer = document.getElementById("mpTestPeer").value;
      const node = document.getElementById("mpConnectivity");
      node.textContent = "Testing…";
      const result = await mpPost("/connectivity/test", { peer_id: peer });
      node.innerHTML = `${result.reachable ? mpBadge("reachable", "success") : mpBadge("unreachable", "danger")} route ${mpEsc(result.scope)} · ${result.latency_ms === null ? "—" : `${mpEsc(result.latency_ms)} ms`}<div class="text-muted">${mpEsc((result.advice || []).join(" "))}</div>`;
      return;
    }
    case "scroll-invitations": {
      const node = document.getElementById("mpInvitations");
      if (node) node.scrollIntoView({ behavior: "smooth" });
      return;
    }
    case "open-game": return mpOpenHostDialog(element.dataset.system, element.dataset.game);
    case "page": {
      mpState.games.offset = Math.max(0, mpState.games.offset + Number(element.dataset.delta) * mpState.games.limit);
      return mpLoadGames();
    }
    case "accept": {
      const result = await mpPost(`/invitations/${mpEncode(session)}/accept`);
      mpToast("Joined the session");
      setHash(`#multiplayer/session/${mpEncode(result.id)}`);
      return;
    }
    case "decline":
      await mpPost(`/invitations/${mpEncode(session)}/decline`);
      mpToast("Invitation declined");
      return mpRenderDashboard();
    case "invite": {
      const peer = document.getElementById("mpInviteSelect").value;
      if (!peer) return;
      await mpPost(`/sessions/${mpEncode(session)}/invite`, { peer_id: peer });
      mpToast("Invitation sent");
      return mpRenderSession(session);
    }
    case "remove":
      if (!await sdConfirm("Remove player", "<p>Remove this player from the session?</p>", "Remove")) return;
      await mpPost(`/sessions/${mpEncode(session)}/remove-player`, { peer_id: element.dataset.peer });
      return mpRenderSession(session);
    case "ready":
      await mpPost(`/sessions/${mpEncode(session)}/ready`, { ready: element.dataset.ready === "true" });
      return mpRenderSession(session);
    case "start": {
      const result = await mpWithReplaceConfirm((replace) => mpPost(`/sessions/${mpEncode(session)}/start`, { replace_running_game: replace }), "Replace the running game?");
      if (result) mpToast("Starting the session…", "info");
      return mpRenderSession(session);
    }
    case "launch": {
      const result = await mpWithReplaceConfirm((replace) => mpPost(`/sessions/${mpEncode(session)}/launch`, { replace_running_game: replace }), "Replace the running game?");
      if (result) mpToast("Launching…", "info");
      return mpRenderSession(session);
    }
    case "retry":
      await mpPost(`/sessions/${mpEncode(session)}/retry`, { peer_id: element.dataset.peer || "" });
      mpToast("Retrying…", "info");
      return mpRenderSession(session);
    case "leave":
      if (!await sdConfirm("Leave session", "<p>Leave this session? Your emulator settings are restored; a running game is not closed.</p>", "Leave", "warning")) return;
      await mpPost(`/sessions/${mpEncode(session)}/leave`);
      setHash("#multiplayer");
      return;
    case "end":
      if (!await sdConfirm("End session", "<p>End the session for everyone? The game on this Drone is closed and emulator settings are restored. Other players' games keep running until they close them.</p>", "End session")) return;
      await mpPost(`/sessions/${mpEncode(session)}/end`);
      setHash("#multiplayer");
      return;
    case "result":
      await mpPost(`/sessions/${mpEncode(session)}/result`, { success: element.dataset.success === "true" });
      mpToast("Thanks — recorded for this configuration");
      return mpRenderSession(session);
    case "diagnostics": return mpDiagnostics(session);
    case "save-settings": return mpSaveSettings({ [element.dataset.section]: mpReadSection(element.dataset.section) });
    case "save-emulators": {
      const disabled = Array.from(document.querySelectorAll("[data-mp-adapter]")).filter((input) => !input.checked).map((input) => input.dataset.mpAdapter);
      const overrides = {};
      document.getElementById("mpCoreOverrides").value.split("\n").map((line) => line.trim()).filter(Boolean).forEach((line) => {
        const [system, core] = line.split("=").map((part) => (part || "").trim());
        if (system && core) overrides[system] = core;
      });
      return mpSaveSettings({ emulators: { disabled_adapters: disabled, core_overrides: overrides } });
    }
    case "save-privacy": {
      const values = mpReadSection("privacy");
      values.allowed_inviters = Array.from(document.querySelectorAll("[data-mp-inviter]:checked")).map((box) => box.dataset.mpInviter);
      return mpSaveSettings({ privacy: values });
    }
    case "redetect":
      await mpPost("/adapters/refresh");
      mpToast("Emulator detection refreshed");
      return mpRenderSettings();
    default:
  }
}

async function mpOnChange(kind, element) {
  if (kind === "filter") {
    const filters = mpState.games;
    filters[element.dataset.filter] = element.value;
    filters.offset = 0;
    if (element.dataset.filter === "system") {
      setHash(element.value ? `#multiplayer/games/${mpEncode(element.value)}` : "#multiplayer/games");
      return;
    }
    return mpLoadGames();
  }
  if (kind === "slot") {
    await mpPost(`/sessions/${mpEncode(element.dataset.session)}/assign-slot`, { peer_id: element.dataset.peer, slot: Number(element.value) });
    return mpRenderSession(element.dataset.session);
  }
}

function mpBind() {
  if (mpState.bound) return;
  mpState.bound = true;
  document.addEventListener("click", (event) => {
    const element = event.target.closest("[data-mp-action]");
    if (!element || element.disabled) return;
    if (element.tagName !== "A" || element.dataset.mpAction !== "scroll-invitations") event.preventDefault();
    mpOnAction(element.dataset.mpAction, element).catch(mpFail);
  });
  document.addEventListener("change", (event) => {
    const element = event.target.closest("[data-mp-change]");
    if (!element) return;
    mpOnChange(element.dataset.mpChange, element).catch(mpFail);
  });
}
