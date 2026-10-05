// Admin -> Integrations and the Stream Deck Setup page.
//
// Loaded after drone.js and reuses its helpers (api, apiPost, escapeHtml,
// showToast, setHash, content/titleNode/subtitleNode). All interaction goes
// through delegated [data-sd-action] / [data-sd-change] handlers so no user or
// library data is ever interpolated into inline JavaScript; every dynamic
// value is escapeHtml'd. Everything here configures only THIS machine.

const SD_API = "/admin/integrations/streamdeck";
const SD_ACTION_TYPES = [
  ["none", "No Action"],
  ["builtin", "Built-In Action"],
  ["game", "Launch Game"],
  ["script", "Custom Script"],
  ["profile", "Profile Navigation"],
];
const SD_TYPE_ICONS = { none: "bi-dash-circle", builtin: "bi-lightning-charge-fill", game: "bi-controller", script: "bi-terminal-fill", profile: "bi-layers-fill" };
const SD_SYMBOL_ICONS = {
  exit: "bi-box-arrow-right", power: "bi-power", reboot: "bi-arrow-clockwise", restart: "bi-arrow-repeat",
  "volume-up": "bi-volume-up-fill", "volume-down": "bi-volume-down-fill", mute: "bi-volume-mute-fill",
  pause: "bi-pause-fill", save: "bi-floppy-fill", load: "bi-folder2-open", play: "bi-play-fill",
  next: "bi-skip-forward-fill", previous: "bi-skip-backward-fill", profile: "bi-grid-fill",
  script: "bi-terminal-fill", game: "bi-controller", star: "bi-star-fill",
};
const SD_ACTION_BACKGROUNDS = {
  "exit-game": "#b91c1c", "reboot-system": "#7f1d1d", "shutdown-system": "#7f1d1d", "restart-emulationstation": "#9a3412",
  "volume-up": "#065f46", "volume-down": "#065f46", "mute-toggle": "#065f46", "pause-toggle": "#1e3a8a",
  "save-state": "#4c1d95", "load-state": "#4c1d95",
};
const SD_ARTWORK_FIELDS = [
  ["auto", "Automatic (best available)"], ["image", "Game image"], ["marquee", "Logo / marquee"], ["wheel", "Wheel logo"],
  ["thumbnail", "Box art / thumbnail"], ["boxart", "Box art (2D)"], ["fanart", "Fan art"],
];

let sdState = {
  tab: "overview", status: null, profilesPayload: null, actions: [], scripts: [], selectedDeviceId: "",
  selectedProfileId: "", showRendered: false, pollTimer: null, jobTimers: {}, systems: null, editor: null,
  scriptEditor: null, scriptRun: null, logSource: "runtime", connection: null,
};
let sdHandlersBound = false;

// ---------------------------------------------------------------- utilities
function sdEsc(value) {
  return escapeHtml(value === undefined || value === null ? "" : value);
}
function sdColor(value, fallback) {
  return /^#[0-9a-fA-F]{6}$/.test(String(value || "")) ? value : fallback;
}
function sdToast(message, type = "success") {
  showToast(escapeHtml(String(message || "")), type, type === "danger" ? 9000 : 5000);
}
function sdError(error) {
  sdToast(error && error.message ? error.message : String(error || "Request failed"), "danger");
}
async function sdGet(path) {
  return api(`${SD_API}${path}`);
}
async function sdPost(path, body = {}) {
  return apiPost(`${SD_API}${path}`, body);
}
function sdTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}
function sdHelpButton(topic, label = "") {
  return `<button type="button" class="btn btn-link btn-sm p-0 ms-1 align-baseline sd-help-link" data-sd-action="help" data-topic="${sdEsc(topic)}" title="Help"><i class="bi bi-question-circle"></i>${label ? ` ${sdEsc(label)}` : ""}</button>`;
}
function sdBadge(text, tone) {
  return `<span class="badge text-bg-${sdEsc(tone)}">${sdEsc(text)}</span>`;
}
function sdHealthTone(health) {
  return { healthy: "success", waiting: "info", installing: "primary", disabled: "secondary", degraded: "warning", error: "danger" }[health] || "secondary";
}
function sdProfiles() {
  return (sdState.profilesPayload && sdState.profilesPayload.profiles) || [];
}
function sdCurrentProfile() {
  const profiles = sdProfiles();
  return profiles.find((row) => row.id === sdState.selectedProfileId) || profiles.find((row) => row.is_default) || profiles[0] || null;
}
function sdDevices() {
  return (sdState.status && sdState.status.devices) || [];
}
function sdSelectedDevice() {
  const devices = sdDevices();
  return devices.find((row) => row.id === sdState.selectedDeviceId) || devices.find((row) => row.source === "runtime") || devices[0] || null;
}
function sdLayout(device) {
  const rows = Number(device && device.rows) || 0;
  const columns = Number(device && device.columns) || 0;
  if (rows > 0 && columns > 0) return { rows, columns, keyCount: Number(device.key_count) || rows * columns, known: true };
  const profile = sdCurrentProfile();
  const maxKey = Math.max(-1, ...((profile && profile.buttons) || []).map((button) => Number(button.key)));
  const keyCount = Math.max(6, maxKey + 1);
  return { rows: Math.ceil(keyCount / 3), columns: 3, keyCount, known: false };
}

// ------------------------------------------------------------ key previews
// Mirrors compiler.py's render specs (the worker draws the same spec with Pillow).
function sdFace(spec, { large = false } = {}) {
  spec = spec || { kind: "blank" };
  const bg = sdColor(spec.background, "#111827");
  const fg = sdColor(spec.text_color, "#ffffff");
  const fallback = spec.fallback ? sdFace(spec.fallback, { large }) : "";
  if (spec.kind === "image" && spec.source_url) {
    const caption = spec.text ? `<span class="sd-face-caption" style="color:${fg}">${sdEsc(spec.text)}</span>` : "";
    return `<div class="sd-face sd-face-image" style="background:${sdColor(spec.background, "#000000")}">
      <div class="sd-face-fallback">${fallback}</div>
      <img src="${sdEsc(spec.source_url)}" alt="" class="sd-fit-${sdEsc(spec.fit || "fill")}" loading="lazy" data-sd-fallback="1">${caption}</div>`;
  }
  if (spec.kind === "generated") {
    const icon = SD_SYMBOL_ICONS[spec.symbol] ? `<i class="bi ${SD_SYMBOL_ICONS[spec.symbol]} sd-face-icon"></i>` : "";
    const text = spec.text ? `<span class="sd-face-text sd-size-${sdEsc(spec.text_size || "medium")}">${sdEsc(spec.text)}</span>` : "";
    const secondary = spec.secondary_text ? `<span class="sd-face-secondary">${sdEsc(spec.secondary_text)}</span>` : "";
    const align = icon ? "middle" : (spec.align || "middle");
    return `<div class="sd-face sd-face-generated sd-align-${sdEsc(align)} ${large ? "sd-face-large" : ""}" style="background:${bg};color:${fg}">${icon}${text}${secondary}</div>`;
  }
  return `<div class="sd-face sd-face-blank"></div>`;
}

function sdBindImageFallbacks() {
  if (sdHandlersBound) return;
  // <img> errors do not bubble; capture them once for every Stream Deck preview.
  document.addEventListener("error", (event) => {
    const img = event.target;
    if (img && img.tagName === "IMG" && img.dataset && img.dataset.sdFallback) img.remove();
  }, true);
}

// --------------------------------------------------------- help content
const SD_HELP = {
  "what-is": ["What is a Stream Deck?", `<p>An Elgato Stream Deck is a USB keypad whose keys are tiny screens. Batocera Drone turns each key into a control for this Batocera machine: exit a game, change volume, launch a specific game, switch pages (profiles), or run your own script.</p>`],
  enable: ["What happens when I enable Stream Deck?", `<ol class="mb-2"><li>Drone checks Python and creates <code>integrations/streamdeck/</code> inside the Drone folder.</li><li>It installs the <code>streamdeck</code> (0.10.0) and <code>Pillow</code> Python libraries into that folder only, using a private virtual environment (or the pip wheel bundled with Python) to run pip.</li><li>It verifies the libraries import, creates the Default profile, and starts a small background runtime that owns the device.</li></ol><p>Nothing is installed into Batocera's own Python, no system files change, and <code>batocera-save-overlay</code> is never needed. The first enable needs internet access to download the libraries; later boots reuse them.</p>`],
  install: ["What software is installed, and where?", `<p>Only two Python packages: <code>streamdeck</code> (the python-elgato-streamdeck library) and <code>Pillow</code> (image processing). They live in <code>&lt;Drone folder&gt;/integrations/streamdeck/lib</code>; pip runs from <code>…/python</code> (a private virtual environment). Removing the integration deletes just those folders. Batocera's global pip and setuptools are never installed, upgraded, or removed.</p>`],
  "modifies-batocera": ["Does it modify Batocera?", `<p>No. All files stay under the Drone folder. Actions use Batocera's own tools (for example <code>batocera-es-swissknife --emukill</code> to exit a game) and EmulationStation's local launch API; nothing is patched or overwritten.</p>`],
  detection: ["How are Stream Decks detected, and which models work?", `<p>Before tooling is installed, Drone lists Elgato USB devices (vendor <code>0fd9</code>) from the kernel. Once installed, the runtime asks the Stream Deck library for every attached deck and reads its real key count, layout, and key image size — nothing is hard-coded to one model. Every model the library supports with key screens works (Mini, Original, MK.2, XL, Neo, +); multiple decks can be attached and each has its own brightness and startup profile. Unplugging and re-plugging is detected automatically.</p>`],
  profiles: ["How do profiles work?", `<p>A profile is a page of key assignments. <strong>Default</strong> is created automatically and is what every deck shows on startup unless you choose a different startup profile for that device. Use <em>Profile Navigation</em> keys (Next, Previous, Go to) to switch pages, and optional <em>automatic rules</em> to switch when a game starts or stops (for example: system <code>switch</code> → "Switch" profile; game stopped → "Default"). Profile IDs never change when you rename a profile.</p>`],
  builtins: ["What are built-in actions, and why are they safer than scripts?", `<p>Built-in actions are reviewed Drone features identified by a stable ID (for example <code>exit-game</code>). Your profile stores only that ID; Drone resolves it to its own handler, checks whether it is available right now (for example Save State needs RetroArch with network commands enabled), logs it, and reports errors. No command text is stored or editable, so a profile can never be turned into arbitrary code.</p><p><strong>Custom scripts</strong> are the opposite: code you write that runs as the Drone service. Use built-ins whenever one exists.</p>`],
  dangerous: ["How are accidental presses prevented?", `<p>Reboot, Shut Down, and Restart EmulationStation are marked dangerous. On the physical deck you must <strong>hold</strong> the key for the configured hold time (1.5 s by default); the key shows “HOLD” while counting and releasing early cancels. You can change the hold time or turn the safeguard off in Overview → Safeguards. Testing them from the browser always asks for confirmation.</p>`],
  "launch-game": ["What is Launch Game, and what happens if another game is running?", `<p><em>Launch Game</em> starts one specific game from your local library with a single press. When pressed:</p><ol><li>Batocera Drone checks for an active game.</li><li>If one is running, Drone exits it using the central <strong>Exit Current Game</strong> action.</li><li>Drone waits until that game has actually closed (it watches the emulator process; it does not just sleep).</li><li>Drone launches the newly selected game.</li></ol><p>If the old game does not close within the timeout (20 s by default), the new game is <strong>not</strong> launched, so two emulators never run at once. While a launch is in progress, further game presses are ignored and reported as “launch already in progress”.</p>`],
  "how-launched": ["How is a game launched?", `<p>Drone asks EmulationStation (its local API on this machine) to launch the ROM — exactly what happens when you pick the game in EmulationStation. That means the same emulator choice, controller configuration, shaders, per-game settings, and hooks. Drone never starts emulator binaries directly. EmulationStation must be running.</p>`],
  "assign-game": ["How do I assign a game, and how does search work?", `<p>Open a key, choose <em>Launch Game</em>, then type part of the title. Search uses Drone's local ROM index (fast even for large libraries); pick a system in the filter to browse just that system. Click <em>Select</em>. You never type ROM paths or emulator commands — Drone stores the game's library ID plus its system and ROM path as a fallback.</p>`],
  "missing-rom": ["What if a configured game is renamed, moved, or deleted?", `<p>Every time profiles load, saved games are re-resolved against the current library. A rescanned game is re-linked by its path automatically. If it cannot be found, the key shows a warning and “Game not found”, the button keeps its configuration, and pressing it does nothing harmful (no launch is attempted). Use <em>Relink Game</em> in the button editor to choose a replacement.</p>`],
  artwork: ["How does game artwork get selected?", `<p>Only artwork already on this machine is used (nothing is downloaded). Automatic order: the game's main image, then its logo (marquee/wheel), then box art, then fan art; if none exist, a button with the game title is generated. You can choose a specific artwork type, a generated button, or upload your own image instead.</p>`],
  images: ["How do uploaded and generated images work?", `<p>Uploads accept PNG, JPEG, and WebP up to 5 MiB and 4096×4096. Drone checks the file type, extension, and actual image data, stores the original in <code>images/</code>, and never executes it. The runtime resizes it to your deck's real key size: <strong>Fill / Crop</strong> (default) covers the key and trims edges, <strong>Fit</strong> shows the whole image with a background border, <strong>Stretch</strong> ignores the aspect ratio. Generated images are drawn from text, an optional symbol, and colors — no upload needed. Resized results are cached in <code>rendered/</code>.</p>`],
  rendering: ["Why might the image look different on the physical key?", `<p>The browser preview approximates the key with web fonts and icons. The physical key is drawn by Pillow at the device's real resolution (for example 80×80 on a Mini) with the model's own rotation and color format, so fonts and icon shapes differ slightly and fine detail is lost on small keys. Turn on <em>Show as rendered</em> above the layout to see the exact images last sent to the device.</p>`],
  scripts: ["What can a custom script do?", `<p>A custom script is a file you write (for example <code>#!/bin/bash</code>) that runs <strong>as the Drone service on this machine, with full administrative rights</strong>. It can do anything that user can: change settings, delete files, or power off. Only trusted administrators should create scripts. Scripts are stored in <code>integrations/streamdeck/scripts/</code>, executed directly (never through a shell command line built by Drone), get a clean environment plus <code>DRONE_STREAMDECK_*</code> variables (key, device, profile, active game), and are stopped after the timeout (30 s by default).</p>`],
  "scripts-vs-builtins": ["Built-in action vs custom script", `<p>Use a built-in action whenever one exists: it is validated, compatibility-checked, logged, and cannot be edited into something else. Scripts are for anything Drone does not provide yet. Launch Game is never implemented as a script.</p>`],
  testing: ["How do I test a button without running its action?", `<p>In the button editor, <strong>Test Selected Button</strong> flashes that key on the device (“TEST”) and proves communication — it never runs the action. <strong>Identify Buttons</strong> (Diagnostics) shows the number of every key for a few seconds. <strong>Test Action</strong> really runs the action after you confirm; testing a Launch Game button may close the game that is currently running.</p>`],
  brightness: ["How do I change brightness?", `<p>Overview → Connected Devices → Brightness. The slider applies when you release it and is saved per device.</p>`],
  apply: ["When do changes reach the device?", `<p>Saving a key stores it; <strong>Apply to Stream Deck</strong> validates the configuration, renders images at the device's key size, and updates the keys and their actions — no reboot needed. Turn on <em>Auto apply</em> to push after every saved key. The device is never updated on each keystroke while you edit.</p>`],
  disable: ["Disable, repair, reinstall, and remove", `<ul><li><strong>Disable</strong> stops the runtime and releases the device. Profiles, images, and scripts are kept.</li><li><strong>Repair Installation</strong> verifies the libraries (reinstalling only if damaged), recovers a malformed configuration, restarts the runtime, and reconnects.</li><li><strong>Reinstall Tooling</strong> deletes and reinstalls only the integration's own libraries; your configuration is kept.</li><li><strong>Remove Tooling Only</strong> deletes the libraries, runtime state, and render cache, keeping configuration, scripts, images, and logs.</li><li><strong>Remove Tooling + Configuration</strong> deletes the whole <code>integrations/streamdeck/</code> folder. Nothing outside it is touched.</li></ul>`],
  "remove-what": ["What gets removed?", `<p><strong>Tooling only:</strong> <code>python/</code>, <code>lib/</code>, <code>rendered/</code>, <code>state/</code>. <strong>Tooling + configuration:</strong> the entire <code>integrations/streamdeck/</code> folder (profiles, scripts, uploaded images, logs too). Batocera, its Python, pip, setuptools, and every other Drone feature are never touched.</p>`],
  reset: ["How do I reset the configuration?", `<p>Use <em>Remove Tooling + Configuration</em> and enable again for a clean start, or delete individual profiles and keys. If the configuration file is ever unreadable, Drone keeps a copy as <code>streamdeck.json.broken-…</code> and starts from defaults automatically.</p>`],
  "trouble-disconnected": ["Troubleshooting: Stream Deck shows as disconnected", `<ol><li>Check the USB cable/port; try another port (avoid unpowered hubs).</li><li>Diagnostics → <strong>Test Connection</strong>. If USB shows the device but the runtime cannot open it, use <strong>Repair Installation</strong>.</li><li>Check the runtime log for “Could not open” or HID errors.</li></ol>`],
  "trouble-blank": ["Troubleshooting: blank keys", `<ol><li>Click <strong>Apply to Stream Deck</strong>.</li><li>Use <strong>Identify Buttons</strong> to prove the device draws images.</li><li>Keys marked with a warning have a missing game, image, or script.</li><li>Check the runtime log for “could not be rendered”.</li></ol>`],
  "trouble-nothing": ["Troubleshooting: a button does nothing", `<ol><li>Look at <em>Last action result</em> on the Overview tab after pressing it.</li><li>Built-ins show availability in the Built-In Actions table (for example Save State needs RetroArch network commands).</li><li>Dangerous actions must be held.</li><li>Make sure you applied the latest changes.</li></ol>`],
  "trouble-game": ["Troubleshooting: a game does not launch", `<ol><li>Make sure EmulationStation is running (the launch goes through it).</li><li>If the key says “Game not found”, use <em>Relink Game</em>.</li><li>If EmulationStation does not list the game, update its gamelists.</li><li>If the previous game did not exit in time, nothing new is launched — check the runtime log.</li></ol>`],
  logs: ["Where are logs and scripts stored?", `<p>Runtime log: <code>integrations/streamdeck/logs/runtime.log</code> (size-rotated). Install log: <code>logs/install.log</code>. Drone-side events (enable, tests) are also in Admin → Debug → System Logs (drone activity). Scripts: <code>integrations/streamdeck/scripts/</code>. All visible on the Diagnostics tab.</p>`],
  remote: ["Does Stream Deck configuration work remotely?", `<p><strong>No.</strong> It configures only the local Batocera machine running this Drone. Other Drones in your swarm cannot see or change it, and nothing here runs commands on another machine.</p>`],
  integrations: ["What are integrations?", `<p>Integrations are optional extensions that connect Batocera Drone with hardware or software on <strong>this machine</strong>. Each one is off until you enable it, keeps its files in its own folder under the Drone folder, and exposes the same things: status, enable/disable, configuration, diagnostics, and documentation. Removing one never affects Batocera or the rest of Drone. Stream Deck is the first integration.</p>`],
};
const SD_HELP_ORDER = ["what-is", "enable", "install", "modifies-batocera", "detection", "profiles", "builtins", "scripts-vs-builtins", "dangerous",
  "launch-game", "how-launched", "assign-game", "missing-rom", "artwork", "images", "rendering", "scripts", "testing", "brightness",
  "apply", "disable", "remove-what", "reset", "trouble-disconnected", "trouble-blank", "trouble-nothing", "trouble-game", "logs", "remote"];

function sdHelpAccordion(topics, id) {
  return `<div class="accordion themed-accordion sd-help" id="${sdEsc(id)}">${topics.map((topic, index) => {
    const entry = SD_HELP[topic];
    if (!entry) return "";
    const target = `${id}-${index}`;
    return `<div class="accordion-item" data-help-text="${sdEsc((entry[0] + " " + entry[1].replace(/<[^>]+>/g, " ")).toLowerCase())}">
      <h2 class="accordion-header"><button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#${sdEsc(target)}">${sdEsc(entry[0])}</button></h2>
      <div id="${sdEsc(target)}" class="accordion-collapse collapse"><div class="accordion-body">${entry[1]}</div></div></div>`;
  }).join("")}</div>`;
}

function sdShowHelp(topic) {
  const entry = SD_HELP[topic];
  if (!entry) return;
  sdShowModal("sdHelpModal", entry[0], entry[1], `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>`);
}

// ----------------------------------------------------------------- modals
function sdShowModal(id, title, bodyHtml, footerHtml, size = "") {
  const alreadyOpen = document.querySelector(".modal.show");
  let modal = document.getElementById(id);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = id;
    modal.tabIndex = -1;
    document.body.appendChild(modal);
  }
  modal.className = `modal fade sd-modal${alreadyOpen ? " sd-modal-nested" : ""}`;
  modal.innerHTML = `<div class="modal-dialog modal-dialog-scrollable ${sdEsc(size)}"><div class="modal-content themed-modal">
    <div class="modal-header"><h5 class="modal-title">${sdEsc(title)}</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body">${bodyHtml}</div><div class="modal-footer">${footerHtml}</div></div></div>`;
  const instance = window.bootstrap.Modal.getOrCreateInstance(modal);
  if (alreadyOpen) {
    modal.addEventListener("shown.bs.modal", () => {
      modal.style.zIndex = "1080";
      const backdrops = document.querySelectorAll(".modal-backdrop");
      const last = backdrops[backdrops.length - 1];
      if (last) {
        last.classList.add("sd-modal-nested-backdrop");
        last.style.zIndex = "1070";
      }
    }, { once: true });
  }
  instance.show();
  return modal;
}

function sdConfirm(title, bodyHtml, confirmLabel = "Continue", tone = "danger") {
  return new Promise((resolve) => {
    const modal = sdShowModal("sdConfirmModal", title, bodyHtml,
      `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
       <button type="button" class="btn btn-${sdEsc(tone)}" data-sd-confirm="yes">${sdEsc(confirmLabel)}</button>`);
    let answered = false;
    modal.querySelector("[data-sd-confirm]").addEventListener("click", () => {
      answered = true;
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
      resolve(true);
    }, { once: true });
    modal.addEventListener("hidden.bs.modal", () => { if (!answered) resolve(false); }, { once: true });
  });
}

// ------------------------------------------------------ integrations page
async function renderIntegrationsPage() {
  sdBindHandlers();
  titleNode.textContent = "Integrations";
  subtitleNode.textContent = "Optional extensions for this Batocera machine";
  content.innerHTML = `<div class="text-center py-5"><div class="spinner-border" role="status"></div></div>`;
  let payload;
  try {
    payload = await api("/admin/integrations");
  } catch (error) {
    content.innerHTML = `<div class="alert alert-danger">${sdEsc(error.message)}</div>`;
    return;
  }
  const cards = (payload.integrations || []).map((card) => {
    const route = String(card.configure_route || "");
    const safeRoute = route.startsWith("#admin/integrations/") ? route : "#admin/integrations";
    const metrics = (card.metrics || []).map((metric) => `<li><span class="text-muted">${sdEsc(metric.label)}:</span> ${sdEsc(metric.value)}</li>`).join("");
    return `<div class="col-md-6 col-xl-4 mb-3"><div class="card h-100 sd-integration-card">
      <div class="card-body d-flex flex-column">
        <div class="d-flex align-items-center gap-3 mb-2"><i class="bi ${sdEsc(card.icon || "bi-puzzle")} sd-integration-icon"></i>
          <div><h4 class="h5 mb-0">${sdEsc(card.name)}</h4>${card.version ? `<small class="text-muted">v${sdEsc(card.version)}</small>` : ""}</div></div>
        <p class="text-muted small">${sdEsc(card.description)}</p>
        <div class="d-flex flex-wrap gap-2 mb-2">
          ${sdBadge(card.installed ? "Installed" : "Not Installed", card.installed ? "success" : "secondary")}
          ${sdBadge(card.enabled ? "Enabled" : "Disabled", card.enabled ? "success" : "secondary")}
          ${sdBadge(`Health: ${card.health}`, sdHealthTone(card.health))}
        </div>
        <div class="small mb-2">${sdEsc(card.health_message)}</div>
        <ul class="list-unstyled small mb-3">${metrics}</ul>
        <div class="mt-auto"><button type="button" class="btn btn-primary" data-sd-action="navigate" data-route="${sdEsc(safeRoute)}"><i class="bi bi-gear me-1"></i>Configure</button></div>
      </div></div></div>`;
  }).join("");
  content.innerHTML = `
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
      <span class="badge text-bg-dark border"><i class="bi bi-house-door me-1"></i>This machine only</span>
      ${sdHelpButton("integrations", "What are integrations?")}
    </div>
    <div class="alert alert-info small"><i class="bi bi-info-circle me-2"></i>Integrations connect Batocera Drone with optional hardware or software on this machine. Each one is off until enabled, keeps its own files inside the Drone folder, and provides status, enable/disable, configuration, diagnostics, and documentation. Configuration is never sent to, or applied on, another Drone.</div>
    <div class="row">${cards || '<div class="text-muted">No integrations are available.</div>'}</div>`;
}

// ------------------------------------------------------- stream deck page
function stopStreamDeckAutoRefresh() {
  if (sdState.pollTimer) clearInterval(sdState.pollTimer);
  sdState.pollTimer = null;
  Object.values(sdState.jobTimers).forEach((timer) => clearTimeout(timer));
  sdState.jobTimers = {};
}

async function sdLoadAll() {
  const [status, profiles, actions, scripts] = await Promise.all([
    sdGet("/status"), sdGet("/profiles"), sdGet("/actions"), sdGet("/scripts"),
  ]);
  sdState.status = status;
  sdState.profilesPayload = profiles;
  sdState.actions = actions.actions || [];
  sdState.scripts = scripts.scripts || [];
  if (!sdProfiles().some((row) => row.id === sdState.selectedProfileId)) sdState.selectedProfileId = profiles.default_profile_id;
  const device = sdSelectedDevice();
  sdState.selectedDeviceId = device ? device.id : "";
}

async function renderStreamDeckPage() {
  sdBindHandlers();
  stopStreamDeckAutoRefresh();
  titleNode.textContent = "Stream Deck Setup";
  subtitleNode.textContent = "Elgato Stream Deck controls for this Batocera machine";
  content.innerHTML = `<div class="text-center py-5"><div class="spinner-border" role="status"></div></div>`;
  try {
    await sdLoadAll();
  } catch (error) {
    content.innerHTML = `<div class="alert alert-danger">${sdEsc(error.message)}</div>`;
    return;
  }
  sdRenderShell();
  const job = sdState.status && sdState.status.job;
  if (job && job.status === "running") sdWatchJob(job.id, { lifecycle: true });
  sdState.pollTimer = setInterval(sdRefreshStatus, 4000);
}

function sdRenderShell() {
  const tabs = [["overview", "Overview", "bi-speedometer2"], ["buttons", "Buttons & Profiles", "bi-grid-3x3-gap"],
    ["scripts", "Custom Scripts", "bi-terminal"], ["diagnostics", "Diagnostics", "bi-activity"], ["help", "Help", "bi-life-preserver"]];
  content.innerHTML = `
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
      <button type="button" class="btn btn-link px-0" data-sd-action="navigate" data-route="#admin/integrations"><i class="bi bi-arrow-left me-1"></i>Integrations</button>
      <span class="badge text-bg-dark border ms-auto"><i class="bi bi-house-door me-1"></i>Configures this machine only</span>
      ${sdHelpButton("remote")}
    </div>
    <div id="sdLifecycleBanner"></div>
    <ul class="nav nav-tabs admin-panel-tabs mb-3" role="tablist">
      ${tabs.map(([key, label, icon]) => `<li class="nav-item"><button type="button" class="nav-link ${sdState.tab === key ? "active" : ""}" data-sd-action="tab" data-tab="${key}"><i class="bi ${icon} me-1"></i>${label}</button></li>`).join("")}
    </ul>
    <div id="sdTabBody"></div>`;
  sdRenderBanner();
  sdRenderTab();
}

function sdRenderTab() {
  const body = document.getElementById("sdTabBody");
  if (!body) return;
  const renderers = { overview: sdOverviewHtml, buttons: sdButtonsHtml, scripts: sdScriptsHtml, diagnostics: sdDiagnosticsHtml, help: sdHelpTabHtml };
  body.innerHTML = (renderers[sdState.tab] || sdOverviewHtml)();
  if (sdState.tab === "diagnostics") sdLoadLog();
  if (sdState.tab === "scripts" && sdState.scriptEditor) sdRenderScriptEditor();
}

async function sdRefreshStatus() {
  if (document.hidden || !window.location.hash.startsWith("#admin/integrations/streamdeck")) return;
  try {
    sdState.status = await sdGet("/status");
  } catch (_) {
    return;
  }
  sdRenderBanner();
  if (sdState.tab === "overview") {
    const panel = document.getElementById("sdStatusPanel");
    if (panel) panel.innerHTML = sdStatusPanelHtml();
    const devicesPanel = document.getElementById("sdDeviceDetails");
    if (devicesPanel) devicesPanel.innerHTML = sdDeviceDetailsHtml();
  }
  if (sdState.tab === "diagnostics") {
    const panel = document.getElementById("sdActivityPanel");
    if (panel) panel.innerHTML = sdActivityHtml();
  }
}

function sdRenderBanner() {
  const banner = document.getElementById("sdLifecycleBanner");
  if (!banner) return;
  const status = sdState.status || {};
  const job = status.job;
  const parts = [];
  if (job && (job.status === "running" || (job.status === "failed" && sdState.lastJobId === job.id))) {
    const steps = (job.steps || []).map((step, index, all) => {
      const done = index < all.length - 1 || job.status !== "running";
      return `<li class="${done ? "text-success" : ""}"><i class="bi ${done ? "bi-check-circle" : "bi-arrow-right-circle"} me-1"></i>${sdEsc(step)}</li>`;
    }).join("");
    const tone = job.status === "failed" ? "danger" : "primary";
    parts.push(`<div class="alert alert-${tone}"><div class="d-flex align-items-center gap-2 mb-1">
      ${job.status === "running" ? '<div class="spinner-border spinner-border-sm"></div>' : '<i class="bi bi-x-octagon"></i>'}
      <strong>${sdEsc(job.description || job.kind)}</strong>: ${sdEsc(job.status === "failed" ? job.error : job.message)}</div>
      <ul class="list-unstyled small mb-0 ms-1">${steps}</ul>
      ${job.status === "failed" ? '<div class="small mt-2">Nothing outside the integration folder was changed. Fix the problem (for example network access) and try again, or open Diagnostics → install log.</div>' : ""}</div>`);
  }
  if (status.config_recovery) {
    parts.push(`<div class="alert alert-warning small"><i class="bi bi-exclamation-triangle me-1"></i>The configuration file was unreadable and was reset to defaults${status.config_recovery.backup ? `; the old file was kept as <code>${sdEsc(status.config_recovery.backup)}</code>` : ""}.</div>`);
  }
  if ((status.config_warnings || []).length) {
    parts.push(`<div class="alert alert-warning small"><i class="bi bi-exclamation-triangle me-1"></i>Some saved settings were invalid and ignored: ${sdEsc(status.config_warnings.slice(0, 3).join("; "))}</div>`);
  }
  banner.innerHTML = parts.join("");
}

// ------------------------------------------------------------ overview tab
function sdField(label, value) {
  return `<tr><th class="text-muted fw-normal">${sdEsc(label)}</th><td>${value}</td></tr>`;
}

function sdEventText(event) {
  if (!event) return "—";
  const label = event.status ? `${event.status}${event.error ? `: ${event.error}` : event.message ? `: ${event.message}` : ""}` : (event.action_type || event.model || "");
  return `${sdEsc(label)} <span class="text-muted small">${sdEsc(sdTime(event.at))}</span>`;
}

function sdStatusPanelHtml() {
  const s = sdState.status || {};
  const device = sdSelectedDevice();
  const versions = s.versions || {};
  const profileId = device && s.active_profiles ? s.active_profiles[device.id] : "";
  const profile = sdProfiles().find((row) => row.id === profileId);
  const game = s.active_game;
  const lastAction = s.last_action ? `${sdEsc(s.last_action.action_type || "")} ${sdEsc(s.last_action.action_id || "")} ${s.last_action.key !== undefined ? `(key ${Number(s.last_action.key) + 1})` : ""} <span class="text-muted small">${sdEsc(sdTime(s.last_action.at))}</span>` : "—";
  return `
    <div class="d-flex flex-wrap gap-2 mb-2">
      ${sdBadge(`Integration: ${s.enabled ? "Enabled" : "Disabled"}`, s.enabled ? "success" : "secondary")}
      ${sdBadge(`Tooling: ${s.tooling || "missing"}`, s.tooling === "installed" ? "success" : s.tooling === "error" ? "danger" : "secondary")}
      ${sdBadge(`Runtime: ${s.runtime || "stopped"}`, s.runtime === "running" ? "success" : s.runtime === "error" ? "danger" : "secondary")}
      ${sdBadge(`Device: ${s.device === "connected" ? "Connected" : "Disconnected"}`, s.device === "connected" ? "success" : "secondary")}
      ${sdBadge(`Health: ${s.health || "unknown"}`, sdHealthTone(s.health))}
    </div>
    <p class="small mb-2">${sdEsc(s.health_message || "")}${s.tooling_reason && s.enabled ? ` <span class="text-warning">${sdEsc(s.tooling_reason)}</span>` : ""}</p>
    <div class="table-responsive"><table class="table table-sm align-middle themed-table sd-status-table mb-0"><tbody>
      ${sdField("Selected device", device ? `${sdEsc(device.model)} ${device.source === "runtime" ? sdBadge("open", "success") : device.connected ? sdBadge("detected", "info") : sdBadge("disconnected", "secondary")}` : "None detected")}
      ${sdField("Model", sdEsc(device ? device.model : "—"))}
      ${sdField("Serial number", sdEsc(device && device.serial ? device.serial : "—"))}
      ${sdField("Firmware", sdEsc(device && device.firmware ? device.firmware : "—"))}
      ${sdField("Keys", device && device.key_count ? `${sdEsc(device.key_count)} (${sdEsc(device.rows)}×${sdEsc(device.columns)})` : "—")}
      ${sdField("Brightness", device ? `${sdEsc(device.brightness)}%` : "—")}
      ${sdField("Active profile", sdEsc(profile ? profile.name : (profileId || "—")))}
      ${sdField("Python / runtime", sdEsc(`${versions.python || "?"}${s.runtime_pid ? ` · pid ${s.runtime_pid}` : ""}`))}
      ${sdField("StreamDeck library", sdEsc(versions.streamdeck || "not installed"))}
      ${sdField("Pillow", sdEsc(versions.pillow || "not installed"))}
      ${sdField("Active game", game ? `${sdEsc(game.name || game.rom_path)} <span class="text-muted">(${sdEsc(game.system)}${game.emulator ? ` · ${sdEsc(game.emulator)}` : ""})</span>` : "None")}
      ${sdField("Last device connection", s.last_device_connection ? `${sdEsc(s.last_device_connection.model || "")} <span class="text-muted small">${sdEsc(sdTime(s.last_device_connection.at))}</span>` : "—")}
      ${sdField("Last button press", s.last_button_press ? `key ${Number(s.last_button_press.key) + 1} <span class="text-muted small">${sdEsc(sdTime(s.last_button_press.at))}</span>` : "—")}
      ${sdField("Last action", lastAction)}
      ${sdField("Last game launch", sdEventText(s.last_game_launch))}
      ${sdField("Last action result", sdEventText(s.last_action_result))}
      ${sdField("Last error", s.last_error ? `<span class="text-danger">${sdEsc(s.last_error)}</span>` : "—")}
    </tbody></table></div>`;
}

function sdLifecycleHtml() {
  const s = sdState.status || {};
  const busy = s.job && s.job.status === "running";
  const disabled = busy ? "disabled" : "";
  return `
    <div class="d-flex flex-wrap gap-2 align-items-center">
      ${s.enabled
        ? `<button type="button" class="btn btn-outline-warning" data-sd-action="disable" ${disabled}><i class="bi bi-pause-circle me-1"></i>Disable Stream Deck</button>`
        : `<button type="button" class="btn btn-success" data-sd-action="enable" ${disabled}><i class="bi bi-play-circle me-1"></i>Enable Stream Deck</button>`}
      ${sdHelpButton("enable", "What happens when I enable this?")}
    </div>
    <hr>
    <div class="d-flex flex-wrap gap-2 align-items-center">
      <button type="button" class="btn btn-sm btn-outline-primary" data-sd-action="repair" ${disabled}><i class="bi bi-wrench me-1"></i>Repair Installation</button>
      <button type="button" class="btn btn-sm btn-outline-primary" data-sd-action="reinstall" ${disabled}><i class="bi bi-arrow-repeat me-1"></i>Reinstall Tooling</button>
      <button type="button" class="btn btn-sm btn-outline-danger" data-sd-action="remove-tooling" ${disabled}><i class="bi bi-trash me-1"></i>Remove Tooling Only</button>
      <button type="button" class="btn btn-sm btn-danger" data-sd-action="remove-all" ${disabled}><i class="bi bi-trash3 me-1"></i>Remove Tooling + Configuration</button>
      ${sdHelpButton("remove-what", "What gets removed?")}
    </div>`;
}

function sdDeviceDetailsHtml() {
  const devices = sdDevices();
  if (!devices.length) {
    return `<div class="text-muted small">No Stream Deck detected. Plug one in — it is picked up automatically (no restart needed). ${sdHelpButton("trouble-disconnected")}</div>`;
  }
  const rows = devices.map((device) => {
    const state = device.source === "runtime" ? sdBadge("Connected", "success") : device.connected ? sdBadge("Detected (not open)", "info") : sdBadge("Disconnected", "secondary");
    const size = Array.isArray(device.key_image_size) ? device.key_image_size.join("×") : "—";
    return `<tr class="${device.id === sdState.selectedDeviceId ? "table-active" : ""}">
      <td><input class="form-check-input" type="radio" name="sdDevice" value="${sdEsc(device.id)}" ${device.id === sdState.selectedDeviceId ? "checked" : ""} data-sd-change="select-device" aria-label="Select device"></td>
      <td>${sdEsc(device.model)}${device.usb_id ? `<div class="small text-muted">USB ${sdEsc(device.usb_id)}</div>` : ""}</td>
      <td class="font-monospace small">${sdEsc(device.serial || device.id)}</td><td>${sdEsc(device.firmware || "—")}</td>
      <td>${device.key_count ? `${sdEsc(device.key_count)} (${sdEsc(device.rows)}×${sdEsc(device.columns)})` : "—"}</td>
      <td>${sdEsc(size)}</td><td>${state}</td></tr>`;
  }).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table mb-0"><thead><tr><th></th><th>Model</th><th>Serial / ID</th><th>Firmware</th><th>Keys</th><th>Key image</th><th>State</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function sdDeviceSettingsHtml() {
  const device = sdSelectedDevice();
  if (!device) return `<div class="text-muted small">Select or connect a device to change its settings.</div>`;
  const options = sdProfiles().map((profile) => `<option value="${sdEsc(profile.id)}" ${device.startup_profile_id === profile.id ? "selected" : ""}>${sdEsc(profile.name)}</option>`).join("");
  return `<div class="row g-3">
    <div class="col-md-7"><label class="form-label" for="sdBrightness">Brightness <span id="sdBrightnessValue" class="badge text-bg-secondary">${sdEsc(device.brightness)}%</span> ${sdHelpButton("brightness")}</label>
      <input type="range" class="form-range" min="0" max="100" step="5" id="sdBrightness" value="${sdEsc(device.brightness)}" data-sd-change="brightness" data-sd-input="brightness-label"></div>
    <div class="col-md-5"><label class="form-label" for="sdStartupProfile">Startup profile</label>
      <select class="form-select" id="sdStartupProfile" data-sd-change="startup-profile"><option value="">Default profile</option>${options}</select></div></div>`;
}

function sdSettingsHtml() {
  const settings = (sdState.status && sdState.status.settings) || {};
  return `<div class="row g-3">
    <div class="col-md-6"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" id="sdConfirmDangerous" ${settings.confirm_dangerous_actions ? "checked" : ""}>
      <label class="form-check-label" for="sdConfirmDangerous">Require a hold for dangerous actions ${sdHelpButton("dangerous")}</label></div></div>
    <div class="col-md-6"><label class="form-label small" for="sdHoldMs">Hold time (ms)</label><input type="number" class="form-control form-control-sm" id="sdHoldMs" min="500" max="5000" step="100" value="${sdEsc(settings.hold_duration_ms || 1500)}"></div>
    <div class="col-md-6"><div class="form-check form-switch"><input class="form-check-input" type="checkbox" id="sdAutoApply" ${settings.auto_apply ? "checked" : ""}>
      <label class="form-check-label" for="sdAutoApply">Auto apply saved keys to the device ${sdHelpButton("apply")}</label></div></div>
    <div class="col-md-6"><label class="form-label small" for="sdExitTimeout">Wait for the running game to exit (seconds)</label><input type="number" class="form-control form-control-sm" id="sdExitTimeout" min="5" max="120" value="${sdEsc(settings.exit_timeout_seconds || 20)}"></div>
    <div class="col-md-6"><label class="form-label small" for="sdLaunchTimeout">Wait for the new game to start (seconds)</label><input type="number" class="form-control form-control-sm" id="sdLaunchTimeout" min="5" max="180" value="${sdEsc(settings.launch_confirm_timeout_seconds || 45)}"></div>
    <div class="col-md-6"><label class="form-label small" for="sdScriptTimeout">Custom script timeout (seconds)</label><input type="number" class="form-control form-control-sm" id="sdScriptTimeout" min="1" max="300" value="${sdEsc(settings.script_timeout_seconds || 30)}"></div>
    <div class="col-12"><button type="button" class="btn btn-sm btn-primary" data-sd-action="save-settings"><i class="bi bi-save me-1"></i>Save settings</button></div></div>`;
}

function sdOverviewHtml() {
  const s = sdState.status || {};
  const paths = s.paths || {};
  return `<div class="row g-3">
    <div class="col-lg-7"><section class="card h-100"><div class="card-body"><h2 class="h5">Status</h2><div id="sdStatusPanel">${sdStatusPanelHtml()}</div></div></section></div>
    <div class="col-lg-5"><section class="card mb-3"><div class="card-body"><h2 class="h5">Stream Deck support</h2>
      <p class="small text-muted">Enabling installs two Python libraries into the Drone folder only and starts a background runtime that reconnects automatically after reboots and re-plugging.</p>
      ${sdLifecycleHtml()}</div></section>
      <section class="card"><div class="card-body"><h2 class="h5">Tooling / Runtime</h2><div class="table-responsive"><table class="table table-sm themed-table mb-0"><tbody>
        ${sdField("Install method", sdEsc(s.tooling_method || "—"))}
        ${sdField("HID transport", sdEsc(s.hid_transport || "—"))}
        ${sdField("Started", sdEsc(sdTime(s.runtime_started_at)))}
        ${sdField("Folder", `<code class="small">${sdEsc(paths.root || "")}</code>`)}
        ${sdField("Scripts", `<code class="small">${sdEsc(paths.scripts || "")}</code>`)}
        ${sdField("Logs", `<code class="small">${sdEsc(paths.logs || "")}</code>`)}
      </tbody></table></div></div></section></div>
    <div class="col-12"><section class="card"><div class="card-body"><h2 class="h5">Connected Devices ${sdHelpButton("detection")}</h2><div id="sdDeviceDetails">${sdDeviceDetailsHtml()}</div></div></section></div>
    <div class="col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Device Settings</h2>${sdDeviceSettingsHtml()}</div></section></div>
    <div class="col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Safeguards &amp; timing</h2>${sdSettingsHtml()}</div></section></div>
  </div>`;
}

// -------------------------------------------------------- buttons/profiles
function sdButtonsHtml() {
  const profile = sdCurrentProfile();
  const device = sdSelectedDevice();
  const layout = sdLayout(device);
  const buttons = new Map(((profile && profile.buttons) || []).map((button) => [Number(button.key), button]));
  const pending = sdState.status && sdState.status.pending_apply;
  const autoApply = sdState.status && sdState.status.settings && sdState.status.settings.auto_apply;
  const deviceOptions = sdDevices().map((row) => `<option value="${sdEsc(row.id)}" ${row.id === (device && device.id) ? "selected" : ""}>${sdEsc(row.model)} — ${sdEsc(row.serial || row.id)}</option>`).join("");
  const keys = [];
  for (let key = 0; key < layout.keyCount; key += 1) {
    const button = buttons.get(key);
    const summary = (button && button.summary) || {};
    const type = (button && button.action_type) || "none";
    const problem = summary.problem ? `<span class="sd-key-problem" title="${sdEsc(summary.problem)}"><i class="bi bi-exclamation-triangle-fill"></i></span>` : "";
    const face = sdState.showRendered && device && device.source === "runtime"
      ? `<div class="sd-face sd-face-image"><div class="sd-face-fallback">${sdFace(button && button.render)}</div><img src="${sdEsc(`${API_BASE}${SD_API}/preview/${encodeURIComponent(device.id)}/${key}?v=${Date.now()}`)}" alt="" class="sd-fit-stretch" data-sd-fallback="1"></div>`
      : sdFace(button && button.render);
    const caption = type === "game" && summary.name ? `${summary.name}${summary.system ? ` · ${summary.system}` : ""}` : (summary.name || (button && button.label) || "Unconfigured");
    keys.push(`<button type="button" class="sd-key ${button ? "sd-key-configured" : ""} ${summary.problem ? "sd-key-has-problem" : ""}" data-sd-action="edit-key" data-key="${key}" aria-label="Edit key ${key + 1}">
      <span class="sd-key-screen">${face}</span>
      <span class="sd-key-number">${key + 1}</span>${problem}
      <span class="sd-key-meta"><i class="bi ${SD_TYPE_ICONS[type] || "bi-dash-circle"}"></i> ${sdEsc(caption)}</span></button>`);
  }
  const profileOptions = sdProfiles().map((row) => `<option value="${sdEsc(row.id)}" ${profile && row.id === profile.id ? "selected" : ""}>${sdEsc(row.name)}${row.is_default ? " (default)" : ""}</option>`).join("");
  return `
    <section class="card mb-3"><div class="card-body">
      <div class="d-flex flex-wrap align-items-end gap-2">
        <div class="sd-profile-select"><label class="form-label small mb-1" for="sdProfileSelect">Profile ${sdHelpButton("profiles")}</label>
          <select class="form-select" id="sdProfileSelect" data-sd-change="select-profile">${profileOptions}</select></div>
        <button type="button" class="btn btn-outline-primary" data-sd-action="profile-add"><i class="bi bi-plus-lg me-1"></i>Add</button>
        <button type="button" class="btn btn-outline-secondary" data-sd-action="profile-rename"><i class="bi bi-pencil me-1"></i>Rename</button>
        <button type="button" class="btn btn-outline-secondary" data-sd-action="profile-duplicate"><i class="bi bi-copy me-1"></i>Duplicate</button>
        <button type="button" class="btn btn-outline-secondary" data-sd-action="profile-default" ${profile && profile.is_default ? "disabled" : ""}><i class="bi bi-star me-1"></i>Set Default</button>
        <button type="button" class="btn btn-outline-danger" data-sd-action="profile-delete" ${profile && profile.is_default ? "disabled" : ""}><i class="bi bi-trash me-1"></i>Delete</button>
      </div></div></section>
    <section class="card mb-3"><div class="card-body">
      <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
        <h2 class="h5 mb-0">Visual Button Layout</h2>
        ${sdDevices().length > 1 ? `<select class="form-select form-select-sm w-auto" data-sd-change="select-device-layout" aria-label="Device">${deviceOptions}</select>` : ""}
        <div class="form-check form-switch ms-lg-3"><input class="form-check-input" type="checkbox" id="sdShowRendered" data-sd-change="show-rendered" ${sdState.showRendered ? "checked" : ""} ${device && device.source === "runtime" ? "" : "disabled"}>
          <label class="form-check-label small" for="sdShowRendered">Show as rendered on the device ${sdHelpButton("rendering")}</label></div>
        <div class="ms-auto d-flex align-items-center gap-2">
          ${pending ? sdBadge("Unapplied changes", "warning") : sdBadge("Device up to date", "success")}
          ${autoApply ? sdBadge("Auto apply on", "info") : ""}
          <button type="button" class="btn btn-primary" data-sd-action="apply"><i class="bi bi-upload me-1"></i>Apply to Stream Deck</button>
        </div>
      </div>
      ${layout.known ? "" : `<div class="alert alert-secondary small py-2">No device layout is known yet; showing a ${layout.columns}-column preview. Connect and enable a Stream Deck to see its real layout.</div>`}
      <div class="sd-deck" style="--sd-columns:${Number(layout.columns)}">${keys.join("")}</div>
      <p class="small text-muted mt-2 mb-0">Click a key to edit it. Keys show their current artwork, number, action, and warnings for missing games, images, or scripts.</p>
    </div></section>
    <div class="row g-3">
      <div class="col-xl-7"><section class="card h-100"><div class="card-body"><h2 class="h5">Built-In Actions ${sdHelpButton("builtins")}</h2>${sdActionsTableHtml()}</div></section></div>
      <div class="col-xl-5"><section class="card h-100"><div class="card-body"><h2 class="h5">Automatic profile switching ${sdHelpButton("profiles")}</h2>${sdRulesHtml()}</div></section></div>
    </div>`;
}

function sdActionsTableHtml() {
  const rows = sdState.actions.map((action) => `<tr>
    <td><i class="bi ${SD_SYMBOL_ICONS[action.default_icon] || "bi-lightning"} me-1"></i>${sdEsc(action.display_name)}<div class="small text-muted font-monospace">${sdEsc(action.id)}</div></td>
    <td>${sdEsc(action.category)}</td>
    <td>${action.availability && action.availability.available ? sdBadge("Available", "success") : `${sdBadge("Unavailable now", "secondary")}<div class="small text-muted">${sdEsc(action.availability ? action.availability.reason : "")}</div>`}</td>
    <td>${action.dangerous ? `${sdBadge("Hold to confirm", "warning")}` : ""}<div class="small text-muted">${sdEsc(action.compatibility)}</div></td></tr>`).join("");
  return `<div class="table-responsive"><table class="table table-sm align-middle themed-table mb-0"><thead><tr><th>Action</th><th>Category</th><th>Right now</th><th>Notes</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function sdRulesHtml() {
  const rules = (sdState.profilesPayload && sdState.profilesPayload.context_rules) || [];
  const profiles = sdProfiles();
  const rows = rules.map((rule, index) => `<tr data-rule-index="${index}">
    <td><input class="form-check-input" type="checkbox" data-rule-field="enabled" ${rule.enabled ? "checked" : ""} aria-label="Enabled"></td>
    <td><select class="form-select form-select-sm" data-rule-field="event"><option value="game-start" ${rule.event === "game-start" ? "selected" : ""}>Game starts</option><option value="game-stop" ${rule.event === "game-stop" ? "selected" : ""}>Game stops</option></select></td>
    <td><input class="form-control form-control-sm" data-rule-field="system" placeholder="any system" value="${sdEsc(rule.system)}"></td>
    <td><input class="form-control form-control-sm" data-rule-field="emulator" placeholder="any emulator" value="${sdEsc(rule.emulator)}"></td>
    <td><select class="form-select form-select-sm" data-rule-field="profile_id">${profiles.map((profile) => `<option value="${sdEsc(profile.id)}" ${profile.id === rule.profile_id ? "selected" : ""}>${sdEsc(profile.name)}</option>`).join("")}</select></td>
    <td><button type="button" class="btn btn-sm btn-outline-danger" data-sd-action="rule-remove" data-index="${index}" aria-label="Remove rule"><i class="bi bi-x-lg"></i></button><input type="hidden" data-rule-field="id" value="${sdEsc(rule.id)}"></td></tr>`).join("");
  return `<p class="small text-muted">Optional. The first matching rule switches every connected deck. Example: when a <code>switch</code> game starts → “Switch”; when a game stops → “Default”.</p>
    <div class="table-responsive"><table class="table table-sm align-middle themed-table" id="sdRulesTable"><thead><tr><th>On</th><th>When</th><th>System</th><th>Emulator</th><th>Profile</th><th></th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="text-muted small">No rules — the startup profile stays active.</td></tr>'}</tbody></table></div>
    <div class="d-flex gap-2"><button type="button" class="btn btn-sm btn-outline-primary" data-sd-action="rule-add"><i class="bi bi-plus-lg me-1"></i>Add rule</button>
    <button type="button" class="btn btn-sm btn-primary" data-sd-action="rules-save"><i class="bi bi-save me-1"></i>Save rules</button></div>`;
}

function sdCollectRules() {
  return Array.from(document.querySelectorAll("#sdRulesTable tbody tr[data-rule-index]")).map((row) => {
    const value = (field) => row.querySelector(`[data-rule-field="${field}"]`);
    return {
      id: value("id").value || undefined, enabled: value("enabled").checked, event: value("event").value,
      system: value("system").value.trim(), emulator: value("emulator").value.trim(), profile_id: value("profile_id").value,
    };
  });
}

// ------------------------------------------------------------ button editor
function sdOpenEditor(key) {
  const profile = sdCurrentProfile();
  if (!profile) return;
  const saved = (profile.buttons || []).find((button) => Number(button.key) === key);
  const button = saved ? JSON.parse(JSON.stringify(saved)) : { key, action_type: "none", label: "", image: { type: "default", fit: "fill", background: "#111827", text_color: "#ffffff", text_size: "medium", align: "middle", symbol: "" } };
  sdState.editor = {
    key, profileId: profile.id, button,
    game: saved && saved.resolved_game ? { ...saved.resolved_game } : (button.game ? { ...button.game } : null),
    results: [], search: "", system: "", searchSeq: 0, uploaded: button.image && button.image.type === "uploaded" ? { id: button.image.image_id } : null,
  };
  const device = sdSelectedDevice();
  const title = `Key ${key + 1} — ${profile.name}`;
  sdShowModal("sdEditorModal", title, `<div id="sdEditorBody"></div>`, `
    <button type="button" class="btn btn-outline-secondary me-auto" data-sd-action="editor-test-key" ${device && device.source === "runtime" ? "" : "disabled"} title="Flash this key on the device without running its action"><i class="bi bi-lightbulb me-1"></i>Test Selected Button</button>
    <button type="button" class="btn btn-outline-warning" data-sd-action="editor-test-action"><i class="bi bi-play me-1"></i>Test Action</button>
    <button type="button" class="btn btn-outline-danger" data-sd-action="editor-clear"><i class="bi bi-eraser me-1"></i>Clear Key</button>
    <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
    <button type="button" class="btn btn-primary" data-sd-action="editor-save"><i class="bi bi-save me-1"></i>Save</button>`, "modal-xl");
  sdRenderEditor();
  if (button.action_type === "game") sdEnsureSystems();
}

function sdEnsureSystems() {
  if (sdState.systems) return;
  sdGet("/games/systems").then((payload) => {
    sdState.systems = payload.systems || [];
    const target = document.getElementById("sdEditorAction");
    if (target && sdState.editor && sdState.editor.button.action_type === "game") target.innerHTML = sdEditorActionHtml();
  }).catch(sdError);
}

function sdEditorImage() {
  const editor = sdState.editor;
  editor.button.image = editor.button.image || { type: "default" };
  return editor.button.image;
}

function sdRenderEditor() {
  const body = document.getElementById("sdEditorBody");
  const editor = sdState.editor;
  if (!body || !editor) return;
  const button = editor.button;
  const typeOptions = SD_ACTION_TYPES.map(([value, label]) => `<option value="${value}" ${button.action_type === value ? "selected" : ""}>${label}</option>`).join("");
  body.innerHTML = `<div class="row g-4">
    <div class="col-lg-7">
      <label class="form-label" for="sdEditorType">Action Type ${sdHelpButton("scripts-vs-builtins")}</label>
      <select class="form-select mb-3" id="sdEditorType" data-sd-change="editor-type">${typeOptions}</select>
      <div id="sdEditorAction">${sdEditorActionHtml()}</div>
      <hr>
      <label class="form-label" for="sdEditorLabel">Display label <span class="text-muted small">(optional; shown on generated art)</span></label>
      <input class="form-control mb-3" id="sdEditorLabel" maxlength="40" value="${sdEsc(button.label || "")}" data-sd-input="editor-label">
      <div id="sdEditorArtwork">${sdEditorArtworkHtml()}</div>
    </div>
    <div class="col-lg-5"><div class="sd-editor-preview-wrap">
      <div class="small text-muted mb-2">Preview ${sdHelpButton("rendering")}</div>
      <div class="sd-editor-preview" id="sdEditorPreview">${sdFace(sdEditorPreviewSpec(), { large: true })}</div>
      <div class="small text-muted mt-2" id="sdEditorPreviewNote">${sdEsc(sdPreviewNote())}</div>
    </div></div></div>`;
}

function sdPreviewNote() {
  const device = sdSelectedDevice();
  const size = device && Array.isArray(device.key_image_size) && device.key_image_size[0] ? device.key_image_size.join("×") : "";
  return size ? `Rendered at ${size} pixels for ${device.model}.` : "Rendered at the connected device's key size.";
}

function sdUpdatePreview() {
  const preview = document.getElementById("sdEditorPreview");
  if (preview) preview.innerHTML = sdFace(sdEditorPreviewSpec(), { large: true });
}

function sdEditorActionHtml() {
  const editor = sdState.editor;
  const button = editor.button;
  if (button.action_type === "builtin") {
    const groups = {};
    sdState.actions.forEach((action) => { (groups[action.category] = groups[action.category] || []).push(action); });
    const options = Object.entries(groups).map(([category, actions]) => `<optgroup label="${sdEsc(category)}">${actions.map((action) => `<option value="${sdEsc(action.id)}" ${button.action_id === action.id ? "selected" : ""}>${sdEsc(action.display_name)}${action.availability && !action.availability.available ? " (unavailable right now)" : ""}</option>`).join("")}</optgroup>`).join("");
    const action = sdState.actions.find((row) => row.id === button.action_id) || null;
    return `<label class="form-label" for="sdEditorBuiltin">Built-In Action ${sdHelpButton("builtins")}</label>
      <select class="form-select" id="sdEditorBuiltin" data-sd-change="editor-builtin"><option value="">Choose an action…</option>${options}</select>
      ${action ? `<div class="small mt-2">${sdEsc(action.description)} <span class="text-muted">${sdEsc(action.compatibility)}</span></div>` : ""}
      ${action && action.availability && !action.availability.available ? `<div class="alert alert-secondary small mt-2 mb-0"><i class="bi bi-info-circle me-1"></i>Not available right now: ${sdEsc(action.availability.reason)} Pressing it will report this instead of doing nothing.</div>` : ""}
      ${action && action.dangerous ? `<div class="alert alert-warning small mt-2 mb-0"><i class="bi bi-hand-index-thumb me-1"></i>Dangerous action: on the device you must hold this key to confirm. ${sdHelpButton("dangerous")}</div>` : ""}`;
  }
  if (button.action_type === "game") return sdGamePickerHtml();
  if (button.action_type === "script") {
    const options = sdState.scripts.map((script) => `<option value="${sdEsc(script.id)}" ${button.script_id === script.id ? "selected" : ""}>${sdEsc(script.name)}</option>`).join("");
    return `<label class="form-label" for="sdEditorScript">Custom Script ${sdHelpButton("scripts", "What can this script do?")}</label>
      ${sdState.scripts.length ? `<select class="form-select" id="sdEditorScript" data-sd-change="editor-script"><option value="">Choose a script…</option>${options}</select>` : `<div class="alert alert-secondary small">No scripts yet. Create one on the <strong>Custom Scripts</strong> tab first.</div>`}
      <div class="alert alert-warning small mt-2 mb-0"><i class="bi bi-shield-exclamation me-1"></i>Scripts run as the Drone service on this machine with full rights.</div>`;
  }
  if (button.action_type === "profile") {
    const operation = button.operation || "next";
    const profiles = sdProfiles().map((profile) => `<option value="${sdEsc(profile.id)}" ${button.profile_id === profile.id ? "selected" : ""}>${sdEsc(profile.name)}</option>`).join("");
    return `<label class="form-label" for="sdEditorOperation">Navigation ${sdHelpButton("profiles")}</label>
      <select class="form-select mb-2" id="sdEditorOperation" data-sd-change="editor-operation">
        <option value="next" ${operation === "next" ? "selected" : ""}>Next profile / page</option>
        <option value="previous" ${operation === "previous" ? "selected" : ""}>Previous profile / page</option>
        <option value="go-to" ${operation === "go-to" ? "selected" : ""}>Go to a specific profile</option></select>
      ${operation === "go-to" ? `<select class="form-select" id="sdEditorTargetProfile" data-sd-change="editor-target-profile"><option value="">Choose a profile…</option>${profiles}</select>` : ""}`;
  }
  return `<div class="text-muted small">This key will do nothing when pressed. Choose an action type above.</div>`;
}

function sdGamePickerHtml() {
  const editor = sdState.editor;
  const game = editor.game;
  const missing = game && game.installed === false;
  const systems = (sdState.systems || []).map((system) => `<option value="${sdEsc(system.name)}" ${editor.system === system.name ? "selected" : ""}>${sdEsc(system.name)}${system.rom_count ? ` (${sdEsc(system.rom_count)})` : ""}</option>`).join("");
  const selected = game ? `<div class="sd-selected-game ${missing ? "border-danger" : ""}">
      ${missing ? "" : `<img src="${sdEsc(`${API_BASE}${SD_API}/games/artwork?system=${encodeURIComponent(game.system)}&rom_path=${encodeURIComponent(game.rom_path || "")}&field=auto`)}" alt="" data-sd-fallback="1" class="sd-game-thumb">`}
      <div class="flex-grow-1"><div class="small text-muted">Selected</div><strong>${sdEsc(game.name)}</strong> <span class="badge text-bg-secondary">${sdEsc(game.system)}</span>
        ${missing ? `<div class="text-danger small mt-1"><i class="bi bi-exclamation-triangle me-1"></i>Game not found — search below to relink this button. ${sdHelpButton("missing-rom")}</div>` : ""}
        <details class="small mt-1"><summary class="text-muted">Advanced details</summary><div class="font-monospace text-break">${sdEsc(game.rom_path || "")}</div><div class="text-muted">Library ID: ${sdEsc(game.id)}</div></details></div></div>` : "";
  return `<div class="d-flex align-items-center gap-1 mb-2"><strong>Launch Game</strong> ${sdHelpButton("launch-game", "What happens if another game is running?")} ${sdHelpButton("assign-game")}</div>
    ${selected}
    <div class="row g-2 mt-1">
      <div class="col-md-7"><input class="form-control" id="sdGameSearch" placeholder="${missing ? "Relink Game: search…" : "Search games…"}" value="${sdEsc(editor.search)}" data-sd-input="game-search" autocomplete="off"></div>
      <div class="col-md-5"><select class="form-select" id="sdGameSystem" data-sd-change="game-system"><option value="">All systems</option>${systems}</select></div>
    </div>
    <div id="sdGameResults" class="list-group mt-2 sd-game-results">${sdGameResultsHtml()}</div>
    <div class="form-check mt-3"><input class="form-check-input" type="checkbox" checked disabled id="sdExitFirst">
      <label class="form-check-label small" for="sdExitFirst">Exit the current game before launching (always on: two games never run at once)</label></div>`;
}

function sdGameResultsHtml() {
  const editor = sdState.editor;
  if (!editor) return "";
  if (editor.loading) return `<div class="list-group-item text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Searching…</div>`;
  if (!editor.results.length) {
    return `<div class="list-group-item text-muted small">${editor.search || editor.system ? "No local games match." : "Type part of a title, or choose a system to browse."}</div>`;
  }
  return editor.results.map((game, index) => `<div class="list-group-item d-flex align-items-center gap-3">
      <img src="${sdEsc(`${API_BASE}/systems/${encodeURIComponent(game.system)}/images/${encodeURIComponent(game.id)}`)}" alt="" loading="lazy" class="sd-game-thumb" data-sd-fallback="1">
      <div class="flex-grow-1 min-w-0"><div class="text-truncate"><strong>${sdEsc(game.name)}</strong> ${game.favorite ? '<i class="bi bi-star-fill text-warning" title="Favorite"></i>' : ""}</div>
        <div class="small text-muted">${sdEsc(game.system)} · ${sdBadge("Installed", "success")}</div>
        <details class="small"><summary class="text-muted">Advanced</summary><span class="font-monospace text-break">${sdEsc(game.rom_path)}</span></details></div>
      <button type="button" class="btn btn-sm btn-outline-primary" data-sd-action="game-select" data-index="${index}">Select</button></div>`).join("")
    + (editor.hasMore ? `<button type="button" class="list-group-item list-group-item-action text-center small" data-sd-action="game-more">Show more</button>` : "");
}

let sdSearchTimer = null;
function sdScheduleGameSearch() {
  clearTimeout(sdSearchTimer);
  sdSearchTimer = setTimeout(() => sdRunGameSearch(false), 300);
}

async function sdRunGameSearch(append) {
  const editor = sdState.editor;
  if (!editor) return;
  if (!editor.search && !editor.system) {
    editor.results = [];
    editor.hasMore = false;
    const target = document.getElementById("sdGameResults");
    if (target) target.innerHTML = sdGameResultsHtml();
    return;
  }
  const seq = ++editor.searchSeq;
  const offset = append ? editor.results.length : 0;
  editor.loading = !append;
  const target = document.getElementById("sdGameResults");
  if (target && !append) target.innerHTML = sdGameResultsHtml();
  try {
    const params = new URLSearchParams({ q: editor.search, system: editor.system, limit: "30", offset: String(offset) });
    const result = await sdGet(`/games?${params.toString()}`);
    if (seq !== editor.searchSeq || sdState.editor !== editor) return;
    editor.results = append ? editor.results.concat(result.items || []) : (result.items || []);
    editor.hasMore = Boolean(result.has_more);
  } catch (error) {
    sdError(error);
  } finally {
    editor.loading = false;
  }
  const refreshed = document.getElementById("sdGameResults");
  if (refreshed) refreshed.innerHTML = sdGameResultsHtml();
}

function sdEditorArtworkHtml() {
  const editor = sdState.editor;
  const image = sdEditorImage();
  const isGame = editor.button.action_type === "game";
  const sources = [["default", isGame ? "Game artwork (automatic)" : "Default artwork"], ...(isGame ? [["game-artwork", "Specific game artwork"]] : []),
    ["generated", "Generated"], ["uploaded", "Upload"], ["blank", "Blank"]];
  const radios = sources.map(([value, label]) => `<div class="form-check form-check-inline"><input class="form-check-input" type="radio" name="sdImageType" id="sdImageType-${value}" value="${value}" ${image.type === value ? "checked" : ""} data-sd-change="image-type"><label class="form-check-label" for="sdImageType-${value}">${label}</label></div>`).join("");
  const fit = `<label class="form-label small mt-2" for="sdImageFit">Fit</label><select class="form-select form-select-sm" id="sdImageFit" data-sd-change="image-field" data-field="fit">
      <option value="fill" ${image.fit !== "fit" && image.fit !== "stretch" ? "selected" : ""}>Fill / Crop (default)</option><option value="fit" ${image.fit === "fit" ? "selected" : ""}>Fit</option><option value="stretch" ${image.fit === "stretch" ? "selected" : ""}>Stretch</option></select>`;
  const colors = `<div class="row g-2 mt-1"><div class="col-6"><label class="form-label small" for="sdImageBg">Background</label><input type="color" class="form-control form-control-color w-100" id="sdImageBg" value="${sdEsc(sdColor(image.background, "#111827"))}" data-sd-input="image-field" data-field="background"></div>
      <div class="col-6"><label class="form-label small" for="sdImageFg">Text color</label><input type="color" class="form-control form-control-color w-100" id="sdImageFg" value="${sdEsc(sdColor(image.text_color, "#ffffff"))}" data-sd-input="image-field" data-field="text_color"></div></div>`;
  let details = "";
  if (image.type === "generated") {
    const symbols = [["", "No symbol"], ...Object.keys(SD_SYMBOL_ICONS).map((key) => [key, key.replace("-", " ")])];
    details = `<div class="row g-2 mt-1">
      <div class="col-md-6"><label class="form-label small" for="sdImageText">Text</label><input class="form-control form-control-sm" id="sdImageText" maxlength="24" value="${sdEsc(image.text || "")}" placeholder="e.g. EXIT, SMASH" data-sd-input="image-field" data-field="text"></div>
      <div class="col-md-6"><label class="form-label small" for="sdImageSecondary">Secondary text</label><input class="form-control form-control-sm" id="sdImageSecondary" maxlength="24" value="${sdEsc(image.secondary_text || "")}" data-sd-input="image-field" data-field="secondary_text"></div>
      <div class="col-md-4"><label class="form-label small" for="sdImageSymbol">Symbol</label><select class="form-select form-select-sm" id="sdImageSymbol" data-sd-change="image-field" data-field="symbol">${symbols.map(([value, label]) => `<option value="${value}" ${image.symbol === value ? "selected" : ""}>${sdEsc(label)}</option>`).join("")}</select></div>
      <div class="col-md-4"><label class="form-label small" for="sdImageSize">Text size</label><select class="form-select form-select-sm" id="sdImageSize" data-sd-change="image-field" data-field="text_size">${["small", "medium", "large"].map((value) => `<option value="${value}" ${(image.text_size || "medium") === value ? "selected" : ""}>${value}</option>`).join("")}</select></div>
      <div class="col-md-4"><label class="form-label small" for="sdImageAlign">Alignment</label><select class="form-select form-select-sm" id="sdImageAlign" data-sd-change="image-field" data-field="align">${["top", "middle", "bottom"].map((value) => `<option value="${value}" ${(image.align || "middle") === value ? "selected" : ""}>${value}</option>`).join("")}</select></div>
      </div>${colors}`;
  } else if (image.type === "uploaded") {
    details = `<label class="form-label small mt-2" for="sdImageFile">PNG, JPEG, or WebP (max 5 MiB) ${sdHelpButton("images")}</label>
      <input type="file" class="form-control form-control-sm" id="sdImageFile" accept=".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" data-sd-change="image-upload">
      <div class="small mt-1" id="sdUploadState">${editor.uploaded ? '<i class="bi bi-check-circle text-success me-1"></i>Image uploaded.' : "Choose a file to upload."}</div>${fit}${colors}`;
  } else if (image.type === "game-artwork" && isGame) {
    details = `<label class="form-label small mt-2" for="sdArtworkField">Artwork ${sdHelpButton("artwork")}</label><select class="form-select form-select-sm" id="sdArtworkField" data-sd-change="image-field" data-field="artwork_field">${SD_ARTWORK_FIELDS.map(([value, label]) => `<option value="${value}" ${(image.artwork_field || "auto") === value ? "selected" : ""}>${label}</option>`).join("")}</select>${fit}${colors}`;
  } else if (image.type === "default") {
    details = isGame ? `<div class="small text-muted mt-2">Uses the best local artwork for the game, or a generated title button if none exists. ${sdHelpButton("artwork")}</div>${fit}`
      : `<div class="small text-muted mt-2">Uses the action's built-in icon and colors.</div>`;
  }
  return `<label class="form-label d-block">Image source ${sdHelpButton("images")}</label>${radios}${details}`;
}

function sdEditorPreviewSpec() {
  const editor = sdState.editor;
  const button = editor.button;
  const image = sdEditorImage();
  const label = button.label || "";
  const generated = (text, symbol, background, secondary = "") => ({
    kind: "generated", text: String(text || "").slice(0, 24), secondary_text: secondary, symbol: symbol || "",
    background: background || sdColor(image.background, "#111827"), text_color: sdColor(image.text_color, "#ffffff"),
    text_size: image.text_size || "medium", align: image.align || "middle",
  });
  let fallback = { kind: "blank" };
  if (button.action_type === "builtin") {
    const action = sdState.actions.find((row) => row.id === button.action_id);
    fallback = action ? generated(label || action.default_label, action.default_icon, SD_ACTION_BACKGROUNDS[action.id]) : generated(label || "?", "", "#374151");
  } else if (button.action_type === "game") {
    const game = editor.game;
    fallback = generated(label || (game ? game.name : "GAME"), "game", "#1f2937", game ? game.system : "");
  } else if (button.action_type === "script") {
    const script = sdState.scripts.find((row) => row.id === button.script_id);
    fallback = generated(label || (script ? script.name : "SCRIPT"), "script", "#374151");
  } else if (button.action_type === "profile") {
    const target = sdProfiles().find((row) => row.id === button.profile_id);
    const text = { next: "NEXT", previous: "PREV" }[button.operation || "next"] || (target ? target.name : "PROFILE");
    fallback = generated(label || text, { next: "next", previous: "previous" }[button.operation || "next"] || "profile", "#0f766e");
  } else if (label) {
    fallback = generated(label);
  }
  if (image.type === "blank") return { kind: "blank" };
  if (image.type === "generated") return generated(image.text || label || fallback.text || "", image.symbol || "", sdColor(image.background, "#111827"), image.secondary_text || "");
  if (image.type === "uploaded") {
    if (!editor.uploaded) return generated(label || "UPLOAD", "", "#374151");
    return { kind: "image", source_url: editor.uploaded.preview || `${API_BASE}${SD_API}/images/${editor.uploaded.id}`, fit: image.fit || "fill",
      background: sdColor(image.background, "#000000"), text: label, text_color: sdColor(image.text_color, "#ffffff"), fallback };
  }
  if ((image.type === "default" || image.type === "game-artwork") && button.action_type === "game" && editor.game && editor.game.installed !== false) {
    const field = image.type === "game-artwork" ? (image.artwork_field || "auto") : "auto";
    return { kind: "image", fit: image.fit || "fill", background: sdColor(image.background, "#000000"), text: label,
      text_color: sdColor(image.text_color, "#ffffff"), fallback,
      source_url: `${API_BASE}${SD_API}/games/artwork?system=${encodeURIComponent(editor.game.system)}&rom_path=${encodeURIComponent(editor.game.rom_path || "")}&field=${encodeURIComponent(field)}` };
  }
  return fallback;
}

function sdEditorPayload() {
  const editor = sdState.editor;
  const button = editor.button;
  const image = { ...sdEditorImage() };
  if (image.type === "uploaded") {
    if (!editor.uploaded) throw new Error("Upload an image first, or choose another image source.");
    image.image_id = editor.uploaded.id;
  } else {
    delete image.image_id;
  }
  delete image.preview;
  const payload = { action_type: button.action_type, label: button.label || "", image };
  if (button.action_type === "builtin") {
    if (!button.action_id) throw new Error("Choose a built-in action.");
    payload.action_id = button.action_id;
  } else if (button.action_type === "game") {
    if (!editor.game || editor.game.installed === false) throw new Error("Search for and select a game.");
    payload.game = { id: editor.game.id, name: editor.game.name, system: editor.game.system, rom_path: editor.game.rom_path };
  } else if (button.action_type === "script") {
    if (!button.script_id) throw new Error("Choose a script.");
    payload.script_id = button.script_id;
  } else if (button.action_type === "profile") {
    payload.operation = button.operation || "next";
    if (payload.operation === "go-to") {
      if (!button.profile_id) throw new Error("Choose the profile to go to.");
      payload.profile_id = button.profile_id;
    }
  }
  return payload;
}

async function sdSaveEditor(clear = false) {
  const editor = sdState.editor;
  if (!editor) return;
  let payload;
  try {
    payload = clear ? { action_type: "none", label: "", image: { type: "default" } } : sdEditorPayload();
  } catch (error) {
    sdError(error);
    return;
  }
  try {
    const result = await sdPost(`/profiles/${encodeURIComponent(editor.profileId)}/buttons/${editor.key}`, payload);
    window.bootstrap.Modal.getOrCreateInstance(document.getElementById("sdEditorModal")).hide();
    sdState.editor = null;
    if (result.applied) sdToast(result.applied.message || "Saved and applied.");
    else sdToast(clear ? `Key ${editor.key + 1} cleared. Apply to update the device.` : `Key ${editor.key + 1} saved. Click “Apply to Stream Deck” to update the device.`);
    await sdReloadConfig();
  } catch (error) {
    sdError(error);
  }
}

async function sdReloadConfig() {
  const [profiles, status] = await Promise.all([sdGet("/profiles"), sdGet("/status")]);
  sdState.profilesPayload = profiles;
  sdState.status = status;
  if (!sdProfiles().some((row) => row.id === sdState.selectedProfileId)) sdState.selectedProfileId = profiles.default_profile_id;
  sdRenderBanner();
  sdRenderTab();
}

async function sdUploadImage(input) {
  const file = input.files && input.files[0];
  const state = document.getElementById("sdUploadState");
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    sdError(new Error("Images must be 5 MiB or smaller."));
    return;
  }
  if (state) state.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Uploading…';
  const form = new FormData();
  form.append("image", file);
  try {
    const response = await fetch(_apiRequestUrl(`${SD_API}/images/upload`), { method: "POST", credentials: "include", body: form });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Upload failed (${response.status})`);
    sdState.editor.uploaded = { id: payload.id, preview: `${API_BASE}${SD_API}/images/${payload.id}` };
    if (state) state.innerHTML = `<i class="bi bi-check-circle text-success me-1"></i>Uploaded ${sdEsc(payload.width)}×${sdEsc(payload.height)} ${sdEsc(String(payload.type || "").toUpperCase())}.`;
    sdUpdatePreview();
  } catch (error) {
    if (state) state.textContent = "Upload failed.";
    input.value = "";
    sdError(error);
  }
}

// ---------------------------------------------------------- jobs / actions
function sdWatchJob(jobId, { lifecycle = false, onDone = null } = {}) {
  if (sdState.jobTimers[jobId]) return;
  if (lifecycle) sdState.lastJobId = jobId;
  const tick = async () => {
    let job;
    try {
      job = await sdGet(`/jobs/${encodeURIComponent(jobId)}`);
    } catch (error) {
      delete sdState.jobTimers[jobId];
      return;
    }
    if (lifecycle && sdState.status) {
      sdState.status.job = job;
      sdRenderBanner();
    }
    if (job.status === "running") {
      sdState.jobTimers[jobId] = setTimeout(tick, 800);
      if (onDone && onDone.progress) onDone.progress(job);
      return;
    }
    delete sdState.jobTimers[jobId];
    if (lifecycle) {
      if (job.status === "completed") sdToast((job.result && job.result.message) || "Done.");
      else sdToast(job.error || "The operation failed.", "danger");
      await sdLoadAll().catch(() => {});
      sdRenderBanner();
      sdRenderTab();
    }
    if (onDone) (onDone.done || onDone)(job);
  };
  sdState.jobTimers[jobId] = setTimeout(tick, 300);
}

async function sdLifecycle(path, body = {}) {
  try {
    const result = await sdPost(path, body);
    if (result.job) {
      sdWatchJob(result.job.id, { lifecycle: true });
      if (sdState.status) sdState.status.job = result.job;
      sdRenderBanner();
      sdRenderTab();
    } else {
      sdToast(result.message || "Done.");
      await sdLoadAll();
      sdRenderBanner();
      sdRenderTab();
    }
  } catch (error) {
    sdError(error);
  }
}

function sdResultHtml(job) {
  const result = job.result || {};
  const status = job.status === "running" ? "Running" : result.status || job.status;
  const tone = { completed: "success", ok: "success", launched: "success", "already-running": "info", running: "primary", Running: "primary", "timed-out": "warning", cancelled: "secondary", "launch-unconfirmed": "warning" }[status] || (job.status === "failed" ? "danger" : "secondary");
  const lines = [`<div class="mb-2">${sdBadge(status, tone)} ${result.duration_seconds !== undefined ? `<span class="small text-muted">${sdEsc(result.duration_seconds)} s</span>` : ""} ${result.exit_code !== undefined && result.exit_code !== null ? `<span class="small">exit code ${sdEsc(result.exit_code)}</span>` : ""}</div>`];
  if (job.error || result.error) lines.push(`<div class="text-danger small mb-2">${sdEsc(job.error || result.error)}</div>`);
  if (result.message) lines.push(`<div class="small mb-2">${sdEsc(result.message)}</div>`);
  if (result.stage) lines.push(`<div class="small text-muted">Stage: ${sdEsc(result.stage)}</div>`);
  if (result.previous_game) lines.push(`<div class="small text-muted">Previous game: ${sdEsc(result.previous_game.name || result.previous_game.rom_path)}</div>`);
  if (result.stdout !== undefined) lines.push(`<div class="small text-muted mt-2">stdout${result.truncated ? " (truncated)" : ""}</div><pre class="sd-output">${sdEsc(result.stdout || "")}</pre>`);
  if (result.stderr !== undefined) lines.push(`<div class="small text-muted">stderr</div><pre class="sd-output">${sdEsc(result.stderr || "")}</pre>`);
  return lines.join("");
}

async function sdTestAction() {
  const editor = sdState.editor;
  if (!editor) return;
  let payload;
  try {
    payload = sdEditorPayload();
  } catch (error) {
    sdError(error);
    return;
  }
  if (payload.action_type === "none") {
    sdToast("This key has no action to test.", "info");
    return;
  }
  if (payload.action_type === "profile") {
    sdToast("Profile navigation switches pages on the physical deck; press the key on the device to test it.", "info");
    return;
  }
  const request = { action_type: payload.action_type, action_id: payload.action_id, game: payload.game, script_id: payload.script_id, confirmed: false };
  const action = sdState.actions.find((row) => row.id === payload.action_id);
  if (payload.action_type === "game") {
    const ok = await sdConfirm("Test Game Launch", `<p>This runs the real sequence: <strong>if a game is running, it will be closed</strong>, Drone waits for it to exit, then launches <strong>${sdEsc(payload.game.name)}</strong> through EmulationStation.</p>`, "Close current game and launch", "warning");
    if (!ok) return;
    request.confirmed = true;
  } else if (action && action.dangerous) {
    const ok = await sdConfirm(`Run “${action.display_name}” now?`, `<p>${sdEsc(action.description)} This really happens on this Batocera machine.</p>`, `Run ${action.display_name}`);
    if (!ok) return;
    request.confirmed = true;
  } else if (payload.action_type === "script") {
    const ok = await sdConfirm("Run this script now?", `<p>The script runs on this machine as the Drone service, exactly as a key press would.</p>`, "Run script", "warning");
    if (!ok) return;
  }
  try {
    const job = await sdPost("/actions/test", request);
    const modal = sdShowModal("sdResultModal", "Test result", `<div id="sdTestResult">${sdResultHtml(job)}</div>`, `<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>`);
    sdWatchJob(job.id, { onDone: (final) => { const target = modal.querySelector("#sdTestResult"); if (target) target.innerHTML = sdResultHtml(final); sdRefreshStatus(); } });
  } catch (error) {
    sdError(error);
  }
}

async function sdDeviceCommand(operation, body = {}) {
  const device = sdSelectedDevice();
  if (!device || device.source !== "runtime") {
    sdToast("Connect a Stream Deck and enable the integration first.", "warning");
    return;
  }
  try {
    const result = await sdPost(`/devices/${encodeURIComponent(device.id)}/${operation}`, body);
    sdToast(operation === "identify" ? "Key numbers are shown on the device for a few seconds." : `Key ${Number(body.key) + 1} is flashing “TEST” (its action was not run).`, "info");
    return result;
  } catch (error) {
    sdError(error);
  }
}

// ---------------------------------------------------------------- scripts
function sdScriptsHtml() {
  const rows = sdState.scripts.map((script) => `<tr class="${sdState.scriptEditor && sdState.scriptEditor.id === script.id ? "table-active" : ""}">
    <td><strong>${sdEsc(script.name)}</strong><div class="small text-muted">${sdEsc(script.description || "")}</div></td>
    <td class="small text-muted">${sdEsc(sdTime(script.updated_at))}</td>
    <td class="text-end text-nowrap">
      <button type="button" class="btn btn-sm btn-outline-primary" data-sd-action="script-edit" data-id="${sdEsc(script.id)}"><i class="bi bi-pencil"></i> Edit</button>
      <button type="button" class="btn btn-sm btn-outline-secondary" data-sd-action="script-duplicate" data-id="${sdEsc(script.id)}"><i class="bi bi-copy"></i></button>
      <button type="button" class="btn btn-sm btn-outline-warning" data-sd-action="script-test" data-id="${sdEsc(script.id)}"><i class="bi bi-play"></i> Run/Test</button>
      <button type="button" class="btn btn-sm btn-outline-danger" data-sd-action="script-delete" data-id="${sdEsc(script.id)}"><i class="bi bi-trash"></i></button></td></tr>`).join("");
  return `<div class="alert alert-warning small"><i class="bi bi-shield-exclamation me-1"></i><strong>Administrative code execution.</strong> Scripts run on this machine as the Drone service with full rights. Prefer a built-in action when one exists. ${sdHelpButton("scripts", "What can a script do?")}</div>
    <div class="row g-3"><div class="col-xl-5"><section class="card h-100"><div class="card-body">
      <div class="d-flex align-items-center mb-2"><h2 class="h5 mb-0">Scripts</h2><button type="button" class="btn btn-sm btn-primary ms-auto" data-sd-action="script-new"><i class="bi bi-plus-lg me-1"></i>New script</button></div>
      <div class="table-responsive"><table class="table table-sm align-middle themed-table mb-0"><tbody>${rows || '<tr><td class="text-muted small">No custom scripts yet.</td></tr>'}</tbody></table></div>
      <p class="small text-muted mt-2 mb-0">Assign a script to a key from the button editor (Action Type → Custom Script). One script can be assigned to many keys; a script cannot be deleted while assigned.</p>
    </div></section></div>
    <div class="col-xl-7"><section class="card h-100"><div class="card-body" id="sdScriptEditor"><div class="text-muted small">Select a script to edit, or create a new one.</div></div></section></div></div>`;
}

async function sdEditScript(scriptId) {
  try {
    sdState.scriptEditor = scriptId ? await sdGet(`/scripts/${encodeURIComponent(scriptId)}`)
      : { id: "", name: "New script", description: "", code: "#!/bin/bash\n# Runs on this Batocera machine as the Drone service.\necho \"Hello from Stream Deck\"\n", references: [] };
    sdState.scriptRun = null;
    sdRenderTab();
  } catch (error) {
    sdError(error);
  }
}

function sdRenderScriptEditor() {
  const target = document.getElementById("sdScriptEditor");
  const script = sdState.scriptEditor;
  if (!target || !script) return;
  const refs = (script.references || []).map((ref) => `${sdEsc(ref.profile_name)} key ${Number(ref.key) + 1}`).join(", ");
  target.innerHTML = `<div class="row g-2">
      <div class="col-md-5"><label class="form-label small" for="sdScriptName">Name</label><input class="form-control" id="sdScriptName" maxlength="80" value="${sdEsc(script.name)}"></div>
      <div class="col-md-7"><label class="form-label small" for="sdScriptDescription">Description</label><input class="form-control" id="sdScriptDescription" maxlength="300" value="${sdEsc(script.description)}"></div>
      <div class="col-12"><label class="form-label small" for="sdScriptCode">Code <span class="text-muted">(must start with an interpreter line such as <code>#!/bin/bash</code>; max 64 KiB)</span></label>
        <textarea class="form-control font-monospace sd-code" id="sdScriptCode" rows="14" spellcheck="false">${sdEsc(script.code)}</textarea></div></div>
    <div class="small text-muted mt-1">${refs ? `Assigned to: ${refs}` : "Not assigned to any key."} Available variables: <code>DRONE_STREAMDECK_KEY</code>, <code>_DEVICE_ID</code>, <code>_PROFILE_ID</code>, <code>_TRIGGER</code>, <code>_GAME_SYSTEM</code>, <code>_GAME_ROM</code>.</div>
    <div class="d-flex flex-wrap gap-2 mt-2">
      <button type="button" class="btn btn-primary" data-sd-action="script-save"><i class="bi bi-save me-1"></i>Save</button>
      ${script.id ? `<button type="button" class="btn btn-outline-warning" data-sd-action="script-test" data-id="${sdEsc(script.id)}"><i class="bi bi-play me-1"></i>Run/Test (saved version)</button>` : ""}
      ${sdState.scriptRun && sdState.scriptRun.status === "running" ? `<button type="button" class="btn btn-outline-danger" data-sd-action="script-cancel"><i class="bi bi-stop-circle me-1"></i>Cancel run</button>` : ""}
    </div>
    <div id="sdScriptRun" class="mt-3">${sdState.scriptRun ? sdResultHtml(sdState.scriptRun) : ""}</div>`;
}

async function sdSaveScript() {
  const script = sdState.scriptEditor;
  const payload = {
    name: document.getElementById("sdScriptName").value, description: document.getElementById("sdScriptDescription").value,
    code: document.getElementById("sdScriptCode").value,
  };
  try {
    const saved = script.id ? await sdPost(`/scripts/${encodeURIComponent(script.id)}/update`, payload) : await sdPost("/scripts", payload);
    sdState.scriptEditor = { ...saved, references: script.references || [] };
    sdState.scripts = (await sdGet("/scripts")).scripts || [];
    sdToast("Script saved.");
    sdRenderTab();
  } catch (error) {
    sdError(error);
  }
}

async function sdRunScript(scriptId) {
  const ok = await sdConfirm("Run this script now?", `<p>The saved script runs on this machine as the Drone service. Output, exit code, and duration are shown when it finishes (timeout ${sdEsc((sdState.status && sdState.status.settings && sdState.status.settings.script_timeout_seconds) || 30)} s).</p>`, "Run script", "warning");
  if (!ok) return;
  try {
    if (!sdState.scriptEditor || sdState.scriptEditor.id !== scriptId) await sdEditScript(scriptId);
    const job = await sdPost(`/scripts/${encodeURIComponent(scriptId)}/test`, {});
    sdState.scriptRun = job;
    sdRenderTab();
    sdWatchJob(job.id, { onDone: (final) => { sdState.scriptRun = final; if (sdState.tab === "scripts") sdRenderTab(); } });
  } catch (error) {
    sdError(error);
  }
}

// ------------------------------------------------------------ diagnostics
function sdActivityHtml() {
  const s = sdState.status || {};
  return `<div class="table-responsive"><table class="table table-sm themed-table mb-0"><tbody>
    ${sdField("Runtime", `${sdEsc(s.runtime || "stopped")}${s.runtime_pid ? ` (pid ${sdEsc(s.runtime_pid)})` : ""}`)}
    ${sdField("Launch state", sdEsc(s.launch_state || "IDLE"))}
    ${sdField("Last button press", s.last_button_press ? `key ${Number(s.last_button_press.key) + 1} <span class="text-muted small">${sdEsc(sdTime(s.last_button_press.at))}</span>` : "—")}
    ${sdField("Last action result", sdEventText(s.last_action_result))}
    ${sdField("Last game launch", sdEventText(s.last_game_launch))}
    ${sdField("Last runtime exit", s.last_runtime_exit ? `exit code ${sdEsc(s.last_runtime_exit.exit_code)} <span class="text-muted small">${sdEsc(sdTime(s.last_runtime_exit.at))}</span>` : "—")}
    ${sdField("Last error", s.last_error ? `<span class="text-danger">${sdEsc(s.last_error)}</span>` : "—")}
  </tbody></table></div>`;
}

function sdConnectionHtml() {
  const result = sdState.connection;
  if (!result) return `<div class="small text-muted">Not run yet.</div>`;
  const devices = (result.devices || []).map((entry) => {
    const caps = entry.capabilities || entry;
    return `<li>${sdEsc(caps.model || entry.model || "Device")} ${sdEsc(caps.serial || "")} — ${caps.key_count ? `${sdEsc(caps.key_count)} keys (${sdEsc(caps.rows)}×${sdEsc(caps.columns)}), ` : ""}communication: <strong>${sdEsc(entry.communication || "—")}</strong></li>`;
  }).join("");
  const usb = (result.usb || []).map((entry) => `<li>${sdEsc(entry.model)} <span class="text-muted">USB ${sdEsc(entry.usb_id)}</span></li>`).join("");
  return `<div class="mb-1">${sdBadge(result.status || "unknown", result.status === "ok" ? "success" : result.status === "no-device" ? "secondary" : "danger")} <span class="small text-muted">via ${sdEsc(result.via || "")}</span></div>
    ${result.error ? `<div class="text-danger small">${sdEsc(result.error)}</div>` : ""}${result.message ? `<div class="small">${sdEsc(result.message)}</div>` : ""}
    ${devices ? `<div class="small mt-1">Opened devices:</div><ul class="small mb-1">${devices}</ul>` : ""}
    <div class="small">USB (kernel) devices:</div><ul class="small mb-0">${usb || "<li class='text-muted'>none</li>"}</ul>`;
}

function sdDiagnosticsHtml() {
  const device = sdSelectedDevice();
  const layout = sdLayout(device);
  const keyOptions = Array.from({ length: layout.keyCount }, (_, key) => `<option value="${key}">Key ${key + 1}</option>`).join("");
  const sources = [["runtime", "Runtime log"], ["install", "Install log"], ["console", "Runtime console"]];
  return `<div class="row g-3">
    <div class="col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Device tests ${sdHelpButton("testing")}</h2>
      <div class="d-flex flex-wrap gap-2 mb-3">
        <button type="button" class="btn btn-outline-primary" data-sd-action="test-connection"><i class="bi bi-usb-plug me-1"></i>Test Connection</button>
        <button type="button" class="btn btn-outline-primary" data-sd-action="identify"><i class="bi bi-123 me-1"></i>Identify Buttons</button></div>
      <div class="input-group mb-3"><select class="form-select" id="sdTestKey" aria-label="Key">${keyOptions}</select>
        <button type="button" class="btn btn-outline-secondary" data-sd-action="test-key"><i class="bi bi-lightbulb me-1"></i>Test Selected Button</button></div>
      <p class="small text-muted">Test Selected Button flashes the key only — its action is never run. To run an action, use <em>Test Action</em> in the button editor.</p>
      <div id="sdConnectionResult">${sdConnectionHtml()}</div></div></section></div>
    <div class="col-lg-6"><section class="card h-100"><div class="card-body"><h2 class="h5">Runtime activity</h2><div id="sdActivityPanel">${sdActivityHtml()}</div></div></section></div>
    <div class="col-12"><section class="card"><div class="card-body">
      <div class="d-flex flex-wrap align-items-center gap-2 mb-2"><h2 class="h5 mb-0">Logs ${sdHelpButton("logs")}</h2>
        <select class="form-select form-select-sm w-auto ms-auto" data-sd-change="log-source">${sources.map(([value, label]) => `<option value="${value}" ${sdState.logSource === value ? "selected" : ""}>${label}</option>`).join("")}</select>
        <button type="button" class="btn btn-sm btn-outline-secondary" data-sd-action="log-refresh"><i class="bi bi-arrow-clockwise"></i> Refresh</button></div>
      <pre class="sd-output sd-log" id="sdLogView">Loading…</pre></div></section></div>
    <div class="col-12"><section class="card"><div class="card-body"><h2 class="h5">Troubleshooting</h2>${sdHelpAccordion(["trouble-disconnected", "trouble-blank", "trouble-nothing", "trouble-game"], "sdTrouble")}</div></section></div>
  </div>`;
}

async function sdLoadLog() {
  const view = document.getElementById("sdLogView");
  if (!view) return;
  try {
    const result = await sdGet(`/logs?source=${encodeURIComponent(sdState.logSource)}&lines=300`);
    view.textContent = (result.lines || []).join("\n") || `(empty) ${result.path || ""}`;
    view.scrollTop = view.scrollHeight;
  } catch (error) {
    view.textContent = error.message;
  }
}

// -------------------------------------------------------------- help tab
function sdHelpTabHtml() {
  return `<section class="card"><div class="card-body">
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3"><h2 class="h5 mb-0">Stream Deck documentation</h2>
      <input type="search" class="form-control form-control-sm ms-auto sd-help-search" placeholder="Search help…" data-sd-input="help-search" aria-label="Search help"></div>
    <div class="alert alert-info small"><strong>Quick start:</strong> plug in the deck → Overview → <em>Enable Stream Deck</em> → Buttons &amp; Profiles → click a key → choose an action and artwork → Save → <em>Apply to Stream Deck</em>.</div>
    ${sdHelpAccordion(SD_HELP_ORDER, "sdHelpDocs")}</div></section>`;
}

// ------------------------------------------------------------- event wiring
async function sdOnAction(action, element) {
  const profile = sdCurrentProfile();
  switch (action) {
    case "navigate": setHash(element.dataset.route); return;
    case "tab": sdState.tab = element.dataset.tab; sdRenderShell(); return;
    case "help": sdShowHelp(element.dataset.topic); return;
    case "enable": await sdLifecycle("/enable"); return;
    case "disable":
      if (await sdConfirm("Disable Stream Deck?", "<p>The runtime stops and releases the device. Profiles, images, and scripts are kept; enable again any time.</p>", "Disable", "warning")) await sdLifecycle("/disable");
      return;
    case "repair": await sdLifecycle("/repair", { reinstall: false }); return;
    case "reinstall":
      if (await sdConfirm("Reinstall Stream Deck tooling?", "<p>Deletes and re-downloads only the integration's own Python libraries. Your profiles, images, and scripts are kept. Internet access is required.</p>", "Reinstall", "warning")) await sdLifecycle("/repair", { reinstall: true });
      return;
    case "remove-tooling":
      if (await sdConfirm("Remove tooling only?", `${SD_HELP["remove-what"][1]}<p>This disables the integration; configuration, scripts, images, and logs are kept.</p>`, "Remove tooling")) await sdLifecycle("/remove", { include_configuration: false });
      return;
    case "remove-all":
      if (await sdConfirm("Remove tooling and all configuration?", `<p>This permanently deletes <code>${sdEsc((sdState.status && sdState.status.paths && sdState.status.paths.root) || "integrations/streamdeck")}</code>: libraries, profiles, scripts, uploaded images, and logs. Nothing outside that folder is touched.</p>`, "Remove everything")) await sdLifecycle("/remove", { include_configuration: true });
      return;
    case "save-settings":
      try {
        await sdPost("/settings", {
          confirm_dangerous_actions: document.getElementById("sdConfirmDangerous").checked,
          hold_duration_ms: Number(document.getElementById("sdHoldMs").value), auto_apply: document.getElementById("sdAutoApply").checked,
          exit_timeout_seconds: Number(document.getElementById("sdExitTimeout").value),
          launch_confirm_timeout_seconds: Number(document.getElementById("sdLaunchTimeout").value),
          script_timeout_seconds: Number(document.getElementById("sdScriptTimeout").value),
        });
        sdToast("Settings saved. Apply to update the device.");
        await sdReloadConfig();
      } catch (error) { sdError(error); }
      return;
    case "apply":
      try {
        const result = await sdPost("/apply", {});
        sdToast(result.message || "Applied.", result.applied ? "success" : "info");
        if ((result.problems || []).length) sdToast(`${result.problems.length} key(s) need attention (missing game, image, or script).`, "warning");
        await sdReloadConfig();
      } catch (error) { sdError(error); }
      return;
    case "edit-key": sdOpenEditor(Number(element.dataset.key)); return;
    case "profile-add":
    case "profile-duplicate":
    case "profile-rename": {
      const isRename = action === "profile-rename";
      const name = window.prompt(isRename ? "Profile name" : "New profile name", isRename ? (profile ? profile.name : "") : action === "profile-duplicate" ? `${profile ? profile.name : "Profile"} copy` : "New Profile");
      if (!name) return;
      try {
        if (isRename) await sdPost(`/profiles/${encodeURIComponent(profile.id)}/update`, { name });
        else {
          const created = action === "profile-add" ? await sdPost("/profiles", { name }) : await sdPost(`/profiles/${encodeURIComponent(profile.id)}/duplicate`, { name });
          sdState.selectedProfileId = created.id;
        }
        await sdReloadConfig();
      } catch (error) { sdError(error); }
      return;
    }
    case "profile-default":
      try { await sdPost(`/profiles/${encodeURIComponent(profile.id)}/update`, { default: true }); await sdReloadConfig(); } catch (error) { sdError(error); }
      return;
    case "profile-delete":
      if (!profile || !(await sdConfirm(`Delete profile “${profile.name}”?`, "<p>Its key assignments are deleted. Keys elsewhere that navigate to it are cleared, and rules that target it are removed.</p>", "Delete profile"))) return;
      try { await sdPost(`/profiles/${encodeURIComponent(profile.id)}/delete`, {}); sdState.selectedProfileId = ""; await sdReloadConfig(); } catch (error) { sdError(error); }
      return;
    case "rule-add": {
      const rules = sdCollectRules();
      rules.push({ enabled: true, event: "game-start", system: "", emulator: "", profile_id: (profile && profile.id) || "" });
      sdState.profilesPayload.context_rules = rules;
      sdRenderTab();
      return;
    }
    case "rule-remove": {
      const rules = sdCollectRules();
      rules.splice(Number(element.dataset.index), 1);
      sdState.profilesPayload.context_rules = rules;
      sdRenderTab();
      return;
    }
    case "rules-save":
      try { await sdPost("/rules", { rules: sdCollectRules() }); sdToast("Rules saved. Apply to update the runtime."); await sdReloadConfig(); } catch (error) { sdError(error); }
      return;
    case "game-select": {
      const game = sdState.editor && sdState.editor.results[Number(element.dataset.index)];
      if (!game) return;
      sdState.editor.game = { ...game, installed: true };
      const image = sdEditorImage();
      if (image.type === "generated" || image.type === "blank") image.type = "default";
      document.getElementById("sdEditorAction").innerHTML = sdEditorActionHtml();
      document.getElementById("sdEditorArtwork").innerHTML = sdEditorArtworkHtml();
      sdUpdatePreview();
      return;
    }
    case "game-more": await sdRunGameSearch(true); return;
    case "editor-save": await sdSaveEditor(false); return;
    case "editor-clear":
      if (await sdConfirm("Clear this key?", "<p>The key will have no action and a blank image after you apply.</p>", "Clear key", "warning")) await sdSaveEditor(true);
      return;
    case "editor-test-key": await sdDeviceCommand("test-button", { key: sdState.editor.key }); return;
    case "editor-test-action": await sdTestAction(); return;
    case "test-connection":
      try {
        sdState.connection = { status: "running", via: "" };
        document.getElementById("sdConnectionResult").innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
        sdState.connection = await sdPost("/test-connection", {});
      } catch (error) { sdState.connection = { status: "error", error: error.message }; }
      document.getElementById("sdConnectionResult").innerHTML = sdConnectionHtml();
      return;
    case "identify": await sdDeviceCommand("identify"); return;
    case "test-key": await sdDeviceCommand("test-button", { key: Number(document.getElementById("sdTestKey").value) }); return;
    case "log-refresh": await sdLoadLog(); return;
    case "script-new": await sdEditScript(""); return;
    case "script-edit": await sdEditScript(element.dataset.id); return;
    case "script-save": await sdSaveScript(); return;
    case "script-duplicate":
      try { const copy = await sdPost(`/scripts/${encodeURIComponent(element.dataset.id)}/duplicate`, {}); sdState.scripts = (await sdGet("/scripts")).scripts || []; await sdEditScript(copy.id); } catch (error) { sdError(error); }
      return;
    case "script-delete":
      if (!(await sdConfirm("Delete this script?", "<p>Scripts that are assigned to keys cannot be deleted; reassign those keys first.</p>", "Delete script"))) return;
      try {
        await sdPost(`/scripts/${encodeURIComponent(element.dataset.id)}/delete`, {});
        if (sdState.scriptEditor && sdState.scriptEditor.id === element.dataset.id) sdState.scriptEditor = null;
        sdState.scripts = (await sdGet("/scripts")).scripts || [];
        sdRenderTab();
      } catch (error) { sdError(error); }
      return;
    case "script-test": await sdRunScript(element.dataset.id); return;
    case "script-cancel":
      try { if (sdState.scriptRun) await sdPost(`/jobs/${encodeURIComponent(sdState.scriptRun.id)}/cancel`, {}); } catch (error) { sdError(error); }
      return;
    default:
  }
}

async function sdOnChange(kind, element) {
  const editor = sdState.editor;
  switch (kind) {
    case "select-device":
    case "select-device-layout":
      sdState.selectedDeviceId = element.value;
      sdRenderTab();
      return;
    case "select-profile": sdState.selectedProfileId = element.value; sdRenderTab(); return;
    case "show-rendered": sdState.showRendered = element.checked; sdRenderTab(); return;
    case "brightness":
    case "startup-profile": {
      const device = sdSelectedDevice();
      if (!device) return;
      const body = kind === "brightness" ? { brightness: Number(element.value) } : { startup_profile_id: element.value };
      try {
        const result = await sdPost(`/devices/${encodeURIComponent(device.id)}/settings`, body);
        sdToast(result.applied && result.applied.status === "ok" ? "Applied to the device." : "Saved.");
        sdState.status = await sdGet("/status");
      } catch (error) { sdError(error); }
      return;
    }
    case "log-source": sdState.logSource = element.value; await sdLoadLog(); return;
    case "editor-type":
      editor.button.action_type = element.value;
      if (element.value === "game") sdEnsureSystems();
      if (element.value !== "game" && sdEditorImage().type === "game-artwork") sdEditorImage().type = "default";
      document.getElementById("sdEditorAction").innerHTML = sdEditorActionHtml();
      document.getElementById("sdEditorArtwork").innerHTML = sdEditorArtworkHtml();
      sdUpdatePreview();
      return;
    case "editor-builtin": editor.button.action_id = element.value; document.getElementById("sdEditorAction").innerHTML = sdEditorActionHtml(); sdUpdatePreview(); return;
    case "editor-script": editor.button.script_id = element.value; sdUpdatePreview(); return;
    case "editor-operation": editor.button.operation = element.value; document.getElementById("sdEditorAction").innerHTML = sdEditorActionHtml(); sdUpdatePreview(); return;
    case "editor-target-profile": editor.button.profile_id = element.value; sdUpdatePreview(); return;
    case "game-system": editor.system = element.value; await sdRunGameSearch(false); return;
    case "image-type":
      sdEditorImage().type = element.value;
      document.getElementById("sdEditorArtwork").innerHTML = sdEditorArtworkHtml();
      sdUpdatePreview();
      return;
    case "image-field": sdEditorImage()[element.dataset.field] = element.value; sdUpdatePreview(); return;
    case "image-upload": await sdUploadImage(element); return;
    default:
  }
}

function sdOnInput(kind, element) {
  const editor = sdState.editor;
  switch (kind) {
    case "brightness-label": {
      const label = document.getElementById("sdBrightnessValue");
      if (label) label.textContent = `${element.value}%`;
      return;
    }
    case "editor-label": editor.button.label = element.value; sdUpdatePreview(); return;
    case "image-field": sdEditorImage()[element.dataset.field] = element.value; sdUpdatePreview(); return;
    case "game-search": editor.search = element.value.trim(); sdScheduleGameSearch(); return;
    case "help-search": {
      const needle = element.value.trim().toLowerCase();
      document.querySelectorAll("#sdHelpDocs .accordion-item").forEach((item) => {
        item.classList.toggle("d-none", Boolean(needle) && !String(item.dataset.helpText || "").includes(needle));
      });
      return;
    }
    default:
  }
}

function sdBindHandlers() {
  if (sdHandlersBound) return;
  sdBindImageFallbacks();
  sdHandlersBound = true;
  document.addEventListener("click", (event) => {
    const element = event.target.closest("[data-sd-action]");
    if (!element || element.disabled) return;
    event.preventDefault();
    sdOnAction(element.dataset.sdAction, element).catch(sdError);
  });
  document.addEventListener("change", (event) => {
    const element = event.target.closest("[data-sd-change]");
    if (element) sdOnChange(element.dataset.sdChange, element).catch(sdError);
  });
  document.addEventListener("input", (event) => {
    const element = event.target.closest("[data-sd-input]");
    if (element && sdState.editor !== undefined) {
      try { sdOnInput(element.dataset.sdInput, element); } catch (error) { sdError(error); }
    }
  });
}
