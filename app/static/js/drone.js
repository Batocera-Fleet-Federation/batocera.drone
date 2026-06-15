const content = document.getElementById("content");
const backBtn = document.getElementById("backBtn") || {
  classList: { add() {}, remove() {} },
  addEventListener() {},
};
const systemsMenuBtn = document.getElementById("systemsMenuBtn");
const biosBtn = document.getElementById("biosBtn");
const brandHomeBtn = document.getElementById("brandHomeBtn");
const emulatorsMenuBtn = document.getElementById("emulatorsMenuBtn");
const themeMenuBtn = document.getElementById("themeMenuBtn");
const systemInfoMenuBtn = document.getElementById("systemInfoMenuBtn");
const adminMenuBtn = document.getElementById("adminMenuBtn");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const clearSearchBtn = document.getElementById("clearSearchBtn");
const droneVersionBadge = document.getElementById("droneVersionBadge");
const titleNode = document.querySelector(".h3.mb-1");
const subtitleNode = document.getElementById("pageSubtitle");
const API_BASE = "/v1/api";
let imageObserver = null;
let activeThemeMeta = null;
let activeGlobalThemeCssNode = null;
let activeSystemThemeCssNode = null;
let activeRandomBackground = null;
let activeRandomLogo = null;
let currentSystemContext = null;
let themeFilterSelectedSystems = [];
let themeFilterQuery = "";
const THEME_GALLERY_PAGE_SIZE = 100;
const BIOS_PAGE_SIZE = 100;
const SYSTEM_ROM_PAGE_SIZE = 200;
const ARTWORK_PAGE_SIZE = 200;
const GAMELIST_EDIT_FIELDS = [
  "name", "sortname", "desc", "genre", "developer", "publisher", "releasedate",
  "players", "rating", "favorite", "hidden", "kidgame", "adult",
  "image", "thumbnail", "marquee", "fanart", "boxart", "video"
];
let biosCurrentPage = 1;
let biosFilterQuery = "";
let biosFilterSelectedSystems = [];
let filterDropdownGlobalCloseBound = false;
let filterDropdownState = {};
let biosFilterInitialized = false;
let themeFilterInitialized = false;
let currentLogSource = null;
let logRefreshTimer = null;
let logRefreshInFlight = false;
let localTransfersTimer = null;
let localTransfersInFlight = false;
let currentConfigSource = null;
let emulatorConfigRows = [];
let selectedEmulatorConfigIndex = 0;
let selectedEmulatorConfigVersionIndex = 0;
let emulatorConfigSelectionRequestId = 0;
let artworkCurrentOffset = 0;
let artworkIncludeFilesystem = false;
let artworkSelectedFields = ["image", "marquee"];
let artworkSelectedSystems = [];
let artworkFilterQuery = "";
let artworkRomStatus = "any";
let artworkFilterDebounceTimer = null;
let systemsCache = null;
let systemRomCache = {};
let systemInfoLoaded = false;
let adminEnabled = true;
let loadingToastEl = null;
const UI_DATA_CACHE_TTL_MS = 5 * 60 * 1000;

// Toast notification system (appears at top-right)
function ensureToastContainer() {
  let container = document.querySelector(".toast-alert-container");
  if (!container) {
    container = document.createElement("div");
    container.className = "toast-alert-container";
    document.body.appendChild(container);
  }
  return container;
}
function showToast(message, type = "success", durationMs = 5000) {
  const container = ensureToastContainer();
  const icons = { success: "bi-check-circle-fill", danger: "bi-exclamation-triangle-fill", warning: "bi-exclamation-circle-fill", info: "bi-info-circle-fill" };
  const icon = icons[type] || "bi-info-circle-fill";
  const toast = document.createElement("div");
  toast.className = `toast-alert alert-${type}`;
  const iconHtml = type === "loading" ? "" : `<i class="bi ${icon}"></i> `;
  toast.innerHTML = `${iconHtml}${message}`;
  container.appendChild(toast);
  if (durationMs === null) return toast;
  setTimeout(() => dismissToast(toast), durationMs);
  return toast;
}
function dismissToast(toast) {
  if (!toast || !toast.isConnected) return;
  toast.style.transition = "opacity 0.3s, transform 0.3s";
  toast.style.opacity = "0";
  toast.style.transform = "translateX(30px)";
  setTimeout(() => toast.remove(), 300);
}
function showLoadingToast(text = "Loading...") {
  if (!loadingToastEl || !loadingToastEl.isConnected) {
    loadingToastEl = showToast(`<span class="spinner-border spinner-border-sm me-2" role="status"></span><span class="loading-toast-text"></span>`, "loading", null);
  }
  const label = loadingToastEl.querySelector(".loading-toast-text");
  if (label) label.textContent = text;
}
function hideLoadingToast() {
  if (loadingToastEl) {
    dismissToast(loadingToastEl);
    loadingToastEl = null;
  }
}
// Image lightbox viewer
function showImageLightbox(url, title = "") {
  const imageUrl = appendCacheBust(url);
  const overlay = document.createElement("div");
  overlay.className = "image-lightbox-overlay";
  overlay.innerHTML = `<button class="image-lightbox-close" onclick="this.parentElement.remove()">&times;</button><img src="${escapeHtml(imageUrl)}" alt="${escapeHtml(title)}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'text-light p-4',textContent:'Image could not be loaded'}))">`;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.addEventListener("keydown", function escHandler(ev) { if (ev.key === "Escape") { overlay.remove(); document.removeEventListener("keydown", escHandler); } });
  document.body.appendChild(overlay);
}
function appendCacheBust(url) {
  const value = String(url || "");
  if (!value || value.startsWith("data:")) return value;
  return `${value}${value.includes("?") ? "&" : "?"}v=${Date.now()}`;
}
function showError(message) {
  showToast(message, "danger", 8000);
}
function clearError() {
  // Popup notifications dismiss themselves; this keeps older route code harmless.
}
function setLoading(isLoading, text = "Loading...") {
  if (isLoading) {
    showLoadingToast(text);
  } else {
    hideLoadingToast();
  }
}
function setSearchMode(mode, systemName = "") {
  if (mode === "hidden") {
    searchForm.classList.add("d-none");
    return;
  }
  searchForm.classList.remove("d-none");
  if (mode === "system" && systemName) {
    searchInput.placeholder = `Search ROMS in ${systemName} system`;
  } else {
    searchInput.placeholder = "Search ROMs across all systems";
  }
}
function applyAdminVisibility() {
  const adminLinks = [adminMenuBtn, systemInfoMenuBtn, emulatorsMenuBtn].filter(Boolean);
  if (adminEnabled) {
    adminLinks.forEach((link) => link.classList.remove("d-none"));
  } else {
    adminLinks.forEach((link) => link.classList.add("d-none"));
  }
}
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
function formatBytesToMb(byteCount) {
  const value = Number(byteCount || 0);
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}
async function api(url) {
  const absoluteUrl = url.startsWith("http://") || url.startsWith("https://")
    ? url
    : `${API_BASE}${url}`;
  const res = await fetch(absoluteUrl, { credentials: "include" });
  if (res.status === 401) {
    window.location.reload();
    throw new Error("Authentication required");
  }
  if (!res.ok) {
    let msg = `Request failed: ${res.status}`;
    try {
      const data = await res.json();
      if (data.error) msg = data.error;
    } catch (_) {}
    throw new Error(msg);
  }
  return await res.json();
}
async function apiPost(url, payload) {
  const absoluteUrl = url.startsWith("http://") || url.startsWith("https://")
    ? url
    : `${API_BASE}${url}`;
  const res = await fetch(absoluteUrl, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload || {}),
  });
  if (res.status === 401) {
    window.location.reload();
    throw new Error("Authentication required");
  }
  if (!res.ok) {
    let msg = `Request failed: ${res.status}`;
    try {
      const data = await res.json();
      if (data.error) msg = data.error;
    } catch (_) {}
    throw new Error(msg);
  }
  return await res.json();
}
function isUiCacheFresh(entry) {
  return entry && entry.data && (Date.now() - entry.loadedAt) < UI_DATA_CACHE_TTL_MS;
}
async function getSystemsData(forceRefresh = false) {
  if (!forceRefresh && isUiCacheFresh(systemsCache)) return systemsCache.data;
  const data = await api("/systems");
  systemsCache = { data, loadedAt: Date.now() };
  return data;
}
async function getSystemRomData(system, forceRefresh = false) {
  const key = String(system || "");
  const cached = systemRomCache[key];
  if (!forceRefresh && isUiCacheFresh(cached)) return cached.data;
  const data = await api(`/systems/${encodeURIComponent(key)}`);
  systemRomCache[key] = { data, loadedAt: Date.now() };
  return data;
}
function wildcardToRegExp(pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  const wildcard = escaped.replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp(wildcard, "i");
}
function renderFilterDropdown(prefix, options, selected) {
  const selectedSet = new Set(selected || []);
  const label = selectedSet.size ? `${selectedSet.size} selected` : "No systems";
  return `
    <div class="dropdown app-checkbox-dropdown">
      <button class="btn btn-outline-primary dropdown-toggle w-100 text-start" type="button" id="${prefix}FilterToggle" aria-expanded="false">${label}</button>
      <div class="dropdown-menu filter-dropdown-menu app-checkbox-menu" data-prefix="${prefix}" aria-labelledby="${prefix}FilterToggle">
        <input id="${prefix}FilterSearch" type="search" class="form-control form-control-sm mb-2" placeholder="Filter systems...">
        <div class="d-flex gap-2 mb-2">
          <button type="button" class="btn btn-outline-primary btn-sm" id="${prefix}FilterSelectAll">Select all</button>
          <button type="button" class="btn btn-outline-secondary btn-sm" id="${prefix}FilterUnselectAll">Unselect all</button>
        </div>
        <div id="${prefix}FilterOptions" class="filter-options-scroll">
          ${
            options.map((sys) => `
              <div class="form-check m-0 mb-1 ${prefix}-filter-option" data-value="${escapeHtml(sys)}">
                <input class="form-check-input ${prefix}-system-filter" type="checkbox" value="${escapeHtml(sys)}" id="${prefix}-filter-${escapeHtml(sys)}" ${selectedSet.has(sys) ? "checked" : ""}>
                <label class="form-check-label small" for="${prefix}-filter-${escapeHtml(sys)}">${escapeHtml(sys === "_root" ? "root" : sys)}</label>
              </div>
            `).join("")
          }
        </div>
      </div>
    </div>
  `;
}
function setupFilterDropdown(prefix, onSelectionChange) {
  const toggle = document.getElementById(`${prefix}FilterToggle`);
  const menu = toggle ? toggle.parentElement?.querySelector(".dropdown-menu") : null;
  if (toggle && menu) {
    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const isOpen = menu.classList.contains("show");
      document.querySelectorAll(".filter-dropdown-menu.show").forEach((node) => node.classList.remove("show"));
      document.querySelectorAll("[id$='FilterToggle'][aria-expanded='true']").forEach((node) => node.setAttribute("aria-expanded", "false"));
      if (filterDropdownState[prefix] && filterDropdownState[prefix].dirty) {
        filterDropdownState[prefix].dirty = false;
        onSelectionChange();
      }
      if (!isOpen) {
        menu.classList.add("show");
        toggle.setAttribute("aria-expanded", "true");
      } else {
        toggle.setAttribute("aria-expanded", "false");
      }
    });
  }
  if (!filterDropdownState[prefix]) {
    filterDropdownState[prefix] = { dirty: false };
  }
  if (!filterDropdownGlobalCloseBound) {
    document.addEventListener("click", (event) => {
      const target = event.target;
      if (target && target.closest && target.closest(".dropdown")) return;
      document.querySelectorAll(".filter-dropdown-menu.show").forEach((node) => {
        const pfx = node.getAttribute("data-prefix") || "";
        node.classList.remove("show");
        if (pfx && filterDropdownState[pfx] && filterDropdownState[pfx].dirty) {
          filterDropdownState[pfx].dirty = false;
          if (pfx === "bios" || pfx === "theme") {
            document.dispatchEvent(new CustomEvent(`filter-apply-${pfx}`));
          }
        }
      });
      document.querySelectorAll("[id$='FilterToggle'][aria-expanded='true']").forEach((node) => node.setAttribute("aria-expanded", "false"));
    });
    filterDropdownGlobalCloseBound = true;
  }

  const searchEl = document.getElementById(`${prefix}FilterSearch`);
  const selectAllBtn = document.getElementById(`${prefix}FilterSelectAll`);
  const unselectAllBtn = document.getElementById(`${prefix}FilterUnselectAll`);
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      const q = (searchEl.value || "").trim().toLowerCase();
      document.querySelectorAll(`.${prefix}-filter-option`).forEach((node) => {
        const value = (node.getAttribute("data-value") || "").toLowerCase();
        node.style.display = !q || value.includes(q) ? "" : "none";
      });
    });
  }
  document.querySelectorAll(`.${prefix}-system-filter`).forEach((node) => {
    node.addEventListener("change", () => {
      if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = true;
      if (prefix === "bios") {
        if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = false;
        document.dispatchEvent(new CustomEvent(`filter-apply-${prefix}`));
      }
    });
  });
  if (selectAllBtn) {
    selectAllBtn.addEventListener("click", (event) => {
      event.preventDefault();
      document.querySelectorAll(`.${prefix}-system-filter`).forEach((node) => {
        node.checked = true;
      });
      if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = true;
      if (prefix === "bios") {
        if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = false;
        document.dispatchEvent(new CustomEvent(`filter-apply-${prefix}`));
      }
    });
  }
  if (unselectAllBtn) {
    unselectAllBtn.addEventListener("click", (event) => {
      event.preventDefault();
      document.querySelectorAll(`.${prefix}-system-filter`).forEach((node) => {
        node.checked = false;
      });
      if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = true;
      if (prefix === "bios") {
        if (filterDropdownState[prefix]) filterDropdownState[prefix].dirty = false;
        document.dispatchEvent(new CustomEvent(`filter-apply-${prefix}`));
      }
    });
  }
  document.removeEventListener(`filter-apply-${prefix}`, onSelectionChange);
  document.addEventListener(`filter-apply-${prefix}`, onSelectionChange);
}
function setBackground(url) {
  document.body.style.backgroundImage = "";
}
function pickRandomThemeBackground(payload) {
  if (!payload || !payload.enabled || !Array.isArray(payload.backgrounds) || !payload.backgrounds.length) {
    return null;
  }

  const cacheKey = "drone_api_theme_bg_choice_v1";
  const now = Date.now();
  const cacheMs = (payload.cache_seconds || 60) * 1000;
  try {
    const raw = localStorage.getItem(cacheKey);
    if (raw) {
      const cached = JSON.parse(raw);
      if (
        cached &&
        typeof cached.url === "string" &&
        typeof cached.picked_at === "number" &&
        now - cached.picked_at < cacheMs &&
        payload.backgrounds.indexOf(cached.url) >= 0
      ) {
        return cached.url;
      }
    }
  } catch (_) {}

  const idx = Math.floor(Math.random() * payload.backgrounds.length);
  const chosen = payload.backgrounds[idx];
  try {
    localStorage.setItem(cacheKey, JSON.stringify({ url: chosen, picked_at: now }));
  } catch (_) {}
  return chosen;
}
function pickRandomThemeLogo(payload) {
  if (!payload || !payload.enabled || !Array.isArray(payload.logos) || !payload.logos.length) {
    return null;
  }

  const cacheKey = "drone_api_theme_logo_choice_v1";
  const now = Date.now();
  const cacheMs = (payload.cache_seconds || 60) * 1000;
  try {
    const raw = localStorage.getItem(cacheKey);
    if (raw) {
      const cached = JSON.parse(raw);
      if (
        cached &&
        typeof cached.url === "string" &&
        typeof cached.picked_at === "number" &&
        now - cached.picked_at < cacheMs &&
        payload.logos.indexOf(cached.url) >= 0
      ) {
        return cached.url;
      }
    }
  } catch (_) {}

  const idx = Math.floor(Math.random() * payload.logos.length);
  const chosen = payload.logos[idx];
  try {
    localStorage.setItem(cacheKey, JSON.stringify({ url: chosen, picked_at: now }));
  } catch (_) {}
  return chosen;
}
function themeUiValue(theme, key) {
  if (!theme) return null;
  if (theme.ui && theme.ui[key]) return theme.ui[key];
  if (theme[key]) return theme[key];
  return null;
}
function applyThemeBranding(theme) {
  // The shell uses fixed Drone branding; theme art stays in content cards.
}
async function refreshRandomThemeLogo() {
  if (!activeThemeMeta || !activeThemeMeta.enabled) return;
  try {
    const logoPayload = await api("/theme/logos");
    activeRandomLogo = pickRandomThemeLogo(logoPayload);
  } catch (_) {
    // Keep prior logo on failure.
  }
}
async function initializeTheme() {
  try {
    const theme = await api("/theme/meta");
    activeThemeMeta = theme;
    if (!theme || !theme.enabled) return;
    let bgUrl = null;
    let logoUrl = null;
    try {
      const bgPayload = await api("/theme/backgrounds");
      bgUrl = pickRandomThemeBackground(bgPayload);
    } catch (_) {}
    try {
      const logoPayload = await api("/theme/logos");
      logoUrl = pickRandomThemeLogo(logoPayload);
    } catch (_) {}
    activeRandomLogo = logoUrl;
    activeRandomBackground = null;
    setBackground(null);
    if (activeGlobalThemeCssNode) {
      activeGlobalThemeCssNode.remove();
      activeGlobalThemeCssNode = null;
    }
    const globalCssUrl = themeUiValue(theme, "css_url");
    if (globalCssUrl) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = globalCssUrl;
      document.head.appendChild(link);
      activeGlobalThemeCssNode = link;
    }
    applyThemeBranding(theme.ui || theme);
  } catch (_) {
    // Keep default styling when theme metadata is unavailable.
  }
}
async function applySystemTheme(system) {
  if (!activeThemeMeta || !activeThemeMeta.enabled) return;
  try {
    const theme = await api(`/theme/system/${encodeURIComponent(system)}`);
    if (!theme || !theme.enabled) {
      if (activeThemeMeta) {
        setBackground(null);
      }
      if (activeSystemThemeCssNode) {
        activeSystemThemeCssNode.remove();
        activeSystemThemeCssNode = null;
      }
      activeSystemThemeCssNode = null;
      return;
    }
    setBackground(null);
    if (activeSystemThemeCssNode) {
      activeSystemThemeCssNode.remove();
      activeSystemThemeCssNode = null;
    }
    if (theme.css_url) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = theme.css_url;
      document.head.appendChild(link);
      activeSystemThemeCssNode = link;
    }
  } catch (_) {
    // Ignore and keep current theme.
  }
}
function clearSystemTheme() {
  if (activeSystemThemeCssNode) {
    activeSystemThemeCssNode.remove();
    activeSystemThemeCssNode = null;
  }
  if (activeThemeMeta) {
    setBackground(null);
    applyThemeBranding(activeThemeMeta);
  }
}
function setHash(hash) {
  window.location.hash = hash;
}
function stopLogAutoRefresh() {
  if (logRefreshTimer) {
    clearInterval(logRefreshTimer);
    logRefreshTimer = null;
  }
  logRefreshInFlight = false;
}
function stopLocalTransfersAutoRefresh() {
  if (localTransfersTimer) {
    clearInterval(localTransfersTimer);
    localTransfersTimer = null;
  }
  localTransfersInFlight = false;
}
function startLocalTransfersAutoRefresh() {
  // Live-update only the Local Transfers / Transfer History data while a copy is
  // in progress -- never re-render the whole panel, so the user's asset request
  // form, paging, and selections are left untouched.
  stopLocalTransfersAutoRefresh();
  localTransfersTimer = setInterval(async () => {
    if (document.hidden || localTransfersInFlight) return;
    if (!["#admin/integration", "#admin/local-network"].includes(window.location.hash)) return;
    const downloadsBody = document.getElementById("localDownloadsBody");
    const activityBody = document.getElementById("localActivityBody");
    if (!downloadsBody || !activityBody) return;
    localTransfersInFlight = true;
    try {
      const status = await api("/admin/local-network/status");
      if (!downloadsBody.contains(document.activeElement)) {
        downloadsBody.innerHTML = renderLocalTransfersPanel(status.downloads || {});
      }
      activityBody.innerHTML = renderDownloadRows(status.activity || [], false);
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      localTransfersInFlight = false;
    }
  }, 3000);
}
function startLogAutoRefresh() {
  stopLogAutoRefresh();
  logRefreshTimer = setInterval(async () => {
    if (!window.location.hash.startsWith("#admin/logs/") || !currentLogSource || logRefreshInFlight) return;
    logRefreshInFlight = true;
    try {
      const activeSource = document.querySelector("#logSources .list-group-item.active");
      await loadLog(currentLogSource, activeSource, false, true);
    } finally {
      logRefreshInFlight = false;
    }
  }, 5000);
}
function clampLogLines(value) {
  const parsed = Number.parseInt(String(value || "200"), 10);
  if (!Number.isFinite(parsed)) return 200;
  return Math.max(1, Math.min(parsed, 5000));
}
function parseAdminLogsHash(hash) {
  if (!hash.startsWith("#admin/logs/")) return null;
  const raw = hash.substring("#admin/logs/".length);
  const [sourcePart, queryPart = ""] = raw.split("?", 2);
  const source = decodeURIComponent(sourcePart || "").trim();
  if (!source) return null;
  const params = new URLSearchParams(queryPart);
  const lines = clampLogLines(params.get("lines") || "200");
  return { source, lines };
}
function clampMaxBytes(value) {
  const parsed = Number.parseInt(String(value || "131072"), 10);
  if (!Number.isFinite(parsed)) return 131072;
  return Math.max(1024, Math.min(parsed, 1048576));
}
function parseAdminConfigsHash(hash) {
  if (!hash.startsWith("#admin/configs/")) return null;
  const raw = hash.substring("#admin/configs/".length);
  const [sourcePart, queryPart = ""] = raw.split("?", 2);
  const source = decodeURIComponent(sourcePart || "").trim();
  if (!source) return null;
  const params = new URLSearchParams(queryPart);
  const maxBytes = clampMaxBytes(params.get("max_bytes") || "131072");
  return { source, maxBytes };
}
function parseArtworkHash(hash) {
  if (!hash.startsWith("#admin/artwork")) return null;
  const queryIndex = hash.indexOf("?");
  const params = new URLSearchParams(queryIndex >= 0 ? hash.substring(queryIndex + 1) : "");
  const offset = Math.max(0, Number.parseInt(params.get("offset") || "0", 10) || 0);
  const includeFilesystem = ["1", "true", "yes", "on"].includes(String(params.get("include_filesystem") || "0").toLowerCase());
  const fieldsRaw = params.get("fields");
  const fields = fieldsRaw
    ? fieldsRaw.split(",").map((item) => item.trim()).filter(Boolean)
    : ["image", "marquee"];
  const systemsRaw = params.get("systems") || "";
  const systems = systemsRaw.split(",").map((item) => item.trim()).filter(Boolean);
  const q = params.get("q") || "";
  const romStatus = ["any", "exists", "missing"].includes(params.get("rom_status")) ? params.get("rom_status") : "any";
  return { offset, includeFilesystem, fields, systems, q, romStatus };
}
function artworkShowAllSelected(fields = artworkSelectedFields) {
  return (fields || []).includes("show_all");
}
function artworkHash(includeFilesystem = artworkIncludeFilesystem, offset = artworkCurrentOffset, fields = artworkSelectedFields, systems = artworkSelectedSystems, query = artworkFilterQuery, romStatus = artworkRomStatus) {
  const params = new URLSearchParams();
  params.set("offset", String(Math.max(0, Number(offset || 0))));
  params.set("fields", (fields && fields.length ? fields : ["any"]).join(","));
  if (systems && systems.length) params.set("systems", systems.join(","));
  if (query) params.set("q", query);
  if (romStatus && romStatus !== "any") params.set("rom_status", romStatus);
  if (includeFilesystem || artworkShowAllSelected(fields)) params.set("include_filesystem", "1");
  return `#admin/artwork?${params.toString()}`;
}
function setArtworkHash(includeFilesystem = artworkIncludeFilesystem, offset = artworkCurrentOffset, fields = artworkSelectedFields, systems = artworkSelectedSystems, query = artworkFilterQuery, romStatus = artworkRomStatus) {
  setHash(artworkHash(includeFilesystem, offset, fields, systems, query, romStatus));
}
function syncArtworkHash() {
  const nextHash = artworkHash();
  if (window.location.hash !== nextHash) {
    history.replaceState(null, "", nextHash);
  }
}
function romDownloadUrl(system, uniqueId) {
  return `${API_BASE}/systems/${encodeURIComponent(system)}/${encodeURIComponent(uniqueId)}`;
}
function biosDownloadUrl(uniqueId) {
  return `${API_BASE}/bios/${encodeURIComponent(uniqueId)}`;
}
function publicRomImageUrl(system, romName, imageStem, suffix = ".png", withImageSuffix = true) {
  const stem = imageStem || (() => {
    const lastDot = romName.lastIndexOf(".");
    return lastDot >= 0 ? romName.substring(0, lastDot) : romName;
  })();
  const imageFile = withImageSuffix ? `${stem}-image${suffix}` : `${stem}${suffix}`;
  return `${API_BASE}/public/systems/${encodeURIComponent(system)}/images/${encodeURIComponent(imageFile)}`;
}
function romImageByIdUrl(system, uniqueId) {
  return `${API_BASE}/systems/${encodeURIComponent(system)}/images/${encodeURIComponent(uniqueId)}`;
}
function systemThemeImageCandidates(system) {
  const s = system;
  const lower = system.toLowerCase();
  const upper = system.toUpperCase();
  const variants = [s, lower, upper];
  const suffixes = [".png", ".jpg", ".jpeg", ".webp"];
  const names = ["system", "logo", "background"];
  const candidates = [];
  variants.forEach((variant) => {
    names.forEach((name) => {
      suffixes.forEach((ext) => {
        candidates.push(`${API_BASE}/theme/assets/${encodeURIComponent(variant)}/_inc/${name}${ext}`);
      });
    });
  });
  return candidates;
}
function loadRomCardImage(img) {
  if (!img || img.dataset.loaded === "1") return;
  const primarySrc = img.dataset.src;
  let fallbackCandidates = [];
  try {
    fallbackCandidates = JSON.parse(img.dataset.fallbacks || "[]");
  } catch (_) {
    fallbackCandidates = [];
  }
  if (!primarySrc) return;

  img.onerror = function () {
    const next = fallbackCandidates.shift();
    if (next) {
      this.src = next;
      return;
    }
    this.onerror = null;
  };
  img.src = primarySrc;
  img.dataset.loaded = "1";
}
function setupLazyImages() {
  if (imageObserver) {
    imageObserver.disconnect();
    imageObserver = null;
  }

  const lazyImages = Array.from(document.querySelectorAll("img[data-src]"));
  if (!lazyImages.length) return;

  if (!("IntersectionObserver" in window)) {
    lazyImages.forEach(loadRomCardImage);
    return;
  }

  imageObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const img = entry.target;
      loadRomCardImage(img);
      observer.unobserve(img);
    });
  }, { rootMargin: "200px 0px" });

  lazyImages.forEach((img) => imageObserver.observe(img));
}
function renderSystems(data) {
  backBtn.classList.add("d-none");
  setSearchMode("global");
  const systems = data.systems || [];
  content.innerHTML = `
    <div class="row g-3">
      ${systems.map(system => `
        <div class="col-12 col-sm-6 col-lg-4 col-xl-3">
          <div class="card shadow-sm tile pointer" onclick="setHash('#system/${encodeURIComponent(system.name)}')">
            <img
              src=""
              data-src="${systemThemeImageCandidates(system.name)[0]}"
              data-fallbacks='${JSON.stringify(systemThemeImageCandidates(system.name).slice(1))}'
              class="card-img-top"
              alt="${escapeHtml(system.name)} theme artwork"
              style="height: 140px; object-fit: cover; background: #0f172a;"
              loading="lazy"
            >
            <div class="card-body">
              <h2 class="h5 card-title mb-2"><i class="bi bi-controller me-2"></i>${escapeHtml(system.name)}</h2>
              <div class="text-muted"><i class="bi bi-collection-play me-1"></i>ROM Files: ${system.rom_count}</div>
            </div>
          </div>
        </div>
      `).join("")}
    </div>
  `;
  setupLazyImages();
}
function systemHash(system, page = 1) {
  const safePage = Math.max(1, Number(page || 1));
  return `#system/${encodeURIComponent(system)}${safePage > 1 ? `?page=${safePage}` : ""}`;
}
function parseSystemHash(hash) {
  if (!hash.startsWith("#system/") || hash.includes("/rom/")) return null;
  const raw = hash.substring("#system/".length);
  const [systemPart, queryPart = ""] = raw.split("?", 2);
  const params = new URLSearchParams(queryPart);
  return {
    system: decodeURIComponent(systemPart),
    page: Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1),
  };
}
function renderSystemPagination(system, page, totalPages) {
  if (totalPages <= 1) return "";
  const pages = [];
  const start = Math.max(1, page - 3);
  const end = Math.min(totalPages, page + 3);
  for (let item = start; item <= end; item += 1) pages.push(item);
  return `
    <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between mt-3">
      <div class="text-muted small">Page ${page} of ${totalPages}</div>
      <div class="btn-group flex-wrap" role="group" aria-label="ROM pages">
        <button class="btn btn-sm btn-outline-primary" type="button" ${page <= 1 ? "disabled" : ""} onclick="setHash('${systemHash(system, page - 1)}')">Previous</button>
        ${start > 1 ? `<button class="btn btn-sm btn-outline-primary" type="button" onclick="setHash('${systemHash(system, 1)}')">1</button>${start > 2 ? `<button class="btn btn-sm btn-outline-secondary" type="button" disabled>...</button>` : ""}` : ""}
        ${pages.map((item) => `<button class="btn btn-sm ${item === page ? "btn-primary" : "btn-outline-primary"}" type="button" onclick="setHash('${systemHash(system, item)}')">${item}</button>`).join("")}
        ${end < totalPages ? `${end < totalPages - 1 ? `<button class="btn btn-sm btn-outline-secondary" type="button" disabled>...</button>` : ""}<button class="btn btn-sm btn-outline-primary" type="button" onclick="setHash('${systemHash(system, totalPages)}')">${totalPages}</button>` : ""}
        <button class="btn btn-sm btn-outline-primary" type="button" ${page >= totalPages ? "disabled" : ""} onclick="setHash('${systemHash(system, page + 1)}')">Next</button>
      </div>
    </div>
  `;
}
function renderRomGrid(system, items, page = 1, total = items.length) {
  const totalPages = Math.max(1, Math.ceil(total / SYSTEM_ROM_PAGE_SIZE));
  return `
    <div class="mb-4">
      <div class="d-flex flex-wrap gap-2 justify-content-between align-items-center mb-3">
        <h3 class="h5 mb-0">ROM Files <span class="text-muted">(${total})</span></h3>
        <div class="text-muted small">Showing ${total ? ((page - 1) * SYSTEM_ROM_PAGE_SIZE) + 1 : 0}-${Math.min(total, page * SYSTEM_ROM_PAGE_SIZE)} of ${total}</div>
      </div>
      <div class="row g-3">
        ${items.map(item => {
          const existing = item.existing || {};
          const gamelistImage = ["image", "thumbnail", "boxart", "fanart", "marquee"]
            .map((field) => artworkExistingImageUrl({ ...item, system }, existing[field] || ""))
            .find(Boolean) || "";
          const primaryImage = gamelistImage || romImageByIdUrl(system, item.unique_id);
          const fallbacks = [
            publicRomImageUrl(system, item.name, item.image_stem, ".png", true),
            publicRomImageUrl(system, item.name, item.image_stem, ".jpg", true),
            publicRomImageUrl(system, item.name, item.image_stem, ".jpeg", true),
            publicRomImageUrl(system, item.name, item.image_stem, ".webp", true),
            publicRomImageUrl(system, item.name, item.image_stem, ".png", false),
            publicRomImageUrl(system, item.name, item.image_stem, ".jpg", false),
            publicRomImageUrl(system, item.name, item.image_stem, ".jpeg", false),
            publicRomImageUrl(system, item.name, item.image_stem, ".webp", false),
          ];
          return `
          <div class="col-12 col-md-6 col-xl-3">
            <div class="card shadow-sm tile h-100 pointer" onclick="setHash('#system/${encodeURIComponent(system)}/rom/${encodeURIComponent(item.unique_id)}')">
              <img
                src=""
                data-src="${primaryImage}"
                data-fallbacks='${JSON.stringify(fallbacks)}'
                class="card-img-top"
                alt="${escapeHtml(item.name)}"
                style="height: 220px; object-fit: contain; background: #111;"
                loading="lazy"
              >
              <div class="card-body d-flex flex-column">
                <div class="fw-semibold truncate-2 mb-2">${escapeHtml(item.title || item.name)}</div>
                ${item.byte_count !== undefined ? `<div class="text-muted small mono mb-3">${formatBytesToMb(item.byte_count)}</div>` : ""}
                <div class="text-muted small mb-3">${item.has_gamelist_entry ? "gamelist.xml media available" : "no gamelist.xml entry"}</div>
                <div class="mt-auto text-muted small">${item.is_downloadable === false ? "Folder ROM" : "Open details to download"}</div>
              </div>
            </div>
          </div>
        `;
        }).join("") || `<div class="col-12"><div class="text-muted">No roms found.</div></div>`}
      </div>
      ${renderSystemPagination(system, page, totalPages)}
    </div>
  `;
}
function parseSystemRomHash(hash) {
  if (!hash.startsWith("#system/")) return null;
  const rest = hash.substring("#system/".length);
  const marker = "/rom/";
  const markerIndex = rest.indexOf(marker);
  if (markerIndex < 0) return null;
  return {
    system: decodeURIComponent(rest.substring(0, markerIndex)),
    uniqueId: decodeURIComponent(rest.substring(markerIndex + marker.length)),
  };
}
function romMediaItems(system, rom) {
  const labels = {
    image: "Image",
    thumbnail: "Thumbnail",
    marquee: "Marquee",
    fanart: "Fanart",
    boxart: "Boxart",
  };
  return Object.keys(labels).map((field) => {
    const value = rom.existing && rom.existing[field] ? rom.existing[field] : "";
    const url = artworkExistingImageUrl({ ...rom, system }, value);
    return { field, label: labels[field], value, url };
  }).filter((item) => item.url);
}
function romGamelistSummaryHtml(rom) {
  const details = rom.gamelist || {};
  const fields = [
    ["name", "Name"],
    ["desc", "Description"],
    ["genre", "Genre"],
    ["developer", "Developer"],
    ["publisher", "Publisher"],
    ["releasedate", "Release Date"],
    ["players", "Players"],
    ["rating", "Rating"],
  ];
  return fields.map(([field, label]) => {
    const value = artworkGamelistEditValue(details[field]);
    if (!value) return "";
    return `
      <div class="${field === "desc" ? "col-12" : "col-12 col-md-6"}">
        <div class="text-muted small">${escapeHtml(label)}</div>
        <div class="small">${escapeHtml(value)}</div>
      </div>
    `;
  }).filter(Boolean).join("");
}
async function renderRomMediaPage(system, uniqueId) {
  currentSystemContext = system;
  backBtn.classList.remove("d-none");
  setSearchMode("system", system);
  setLoading(true, "Loading ROM media...");
  try {
    const [romsData] = await Promise.all([
      getSystemRomData(system),
      applySystemTheme(system),
    ]);
    const roms = romsData.roms || [];
    const rom = roms.find((item) => String(item.unique_id || "") === String(uniqueId || ""));
    if (!rom) throw new Error("ROM not found");
    rom.system = system;
    const media = romMediaItems(system, rom);
    const primary = media.find((item) => item.field === "image") || media[0];
    titleNode.textContent = rom.title || rom.name || "ROM Media";
    subtitleNode.textContent = `${system} artwork and gamelist.xml metadata`;
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('#system/${encodeURIComponent(system)}')">← Back to ${escapeHtml(system)}</button>
        ${
          rom.is_downloadable === false
            ? `<button class="btn btn-outline-secondary" type="button" disabled><i class="bi bi-folder2-open me-1"></i>Folder ROM</button>`
            : `<a class="btn btn-primary" href="${romDownloadUrl(system, rom.unique_id)}"><i class="bi bi-download me-1"></i>Download</a>`
        }
      </div>
      <div class="card log-card mb-3">
        <div class="card-body">
          <div class="rom-media-hero">
            <div>
              ${primary ? `<button type="button" class="border-0 p-0 bg-transparent w-100" onclick="showImageLightbox('${escapeHtml(primary.url)}', '${escapeHtml(primary.label)}')"><img class="rom-media-primary" src="${escapeHtml(primary.url)}" alt="${escapeHtml(primary.label)}"></button>` : `<div class="rom-media-primary d-flex align-items-center justify-content-center text-muted">No artwork in gamelist.xml</div>`}
            </div>
            <div>
              <div class="d-flex justify-content-between gap-2 align-items-start mb-2">
                <div>
                  <h2 class="h4 mb-1">${escapeHtml(rom.title || rom.name || "")}</h2>
                  <div class="text-muted small mono">${escapeHtml(rom.rom_file || rom.name || "")}</div>
                  <div id="romFingerprint" class="text-muted small mono mt-1">Fingerprint: loading...</div>
                </div>
                <span class="badge ${rom.has_gamelist_entry ? "text-bg-success" : "text-bg-warning"}">${rom.has_gamelist_entry ? "gamelist.xml entry" : "no gamelist.xml entry"}</span>
              </div>
              <div class="row g-2">
                ${romGamelistSummaryHtml(rom) || `<div class="col-12 text-muted small">No gamelist metadata found.</div>`}
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="mb-3">
        <h3 class="h5 mb-2">Gamelist Artwork</h3>
        <div class="rom-media-grid">
          ${media.map((item) => `
            <div class="rom-media-tile">
              <button type="button" class="border-0 p-0 bg-transparent w-100" onclick="showImageLightbox('${escapeHtml(item.url)}', '${escapeHtml(item.label)}')">
                <img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.label)}">
              </button>
              <div class="rom-media-label">
                <div>
                  <div class="fw-semibold">${escapeHtml(item.label)}</div>
                  <div class="text-muted small text-truncate" title="${escapeHtml(item.value)}">${escapeHtml(item.value)}</div>
                </div>
                <a class="btn btn-sm btn-outline-primary" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()"><i class="bi bi-box-arrow-up-right"></i></a>
              </div>
            </div>
          `).join("") || `<div class="text-muted">No image fields are set in gamelist.xml for this ROM.</div>`}
        </div>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header">Artwork Tools</div>
        <div class="card-body">
          <div class="mb-3">${artworkExternalLinksHtml(rom)}</div>
          <div class="mb-3">
            <div class="fw-semibold mb-2">Manual Upload</div>
            ${artworkEditableImageFields(rom)}
          </div>
          <div>
            <div class="fw-semibold mb-2">Marquee Crop</div>
            ${artworkMarqueeCropperHtml(rom)}
          </div>
        </div>
      </div>
    `;
    window.missingArtworkRoms = [rom];
    window.selectedArtworkRomIndex = 0;
    bindArtworkEditButtons(rom, 0);
    api(`/systems/${encodeURIComponent(system)}/roms/${encodeURIComponent(rom.unique_id)}/fingerprint`)
      .then((data) => {
        const node = document.getElementById("romFingerprint");
        if (node) node.textContent = `Fingerprint: ${data.fingerprint || "unavailable"}`;
      })
      .catch(() => {
        const node = document.getElementById("romFingerprint");
        if (node) node.textContent = "Fingerprint: unavailable";
      });
  } catch (err) {
    showToast(`Failed to load ROM media: ${escapeHtml(err.message || "unknown error")}`, "danger");
    setHash(`#system/${encodeURIComponent(system)}`);
  } finally {
    setLoading(false);
  }
}
function renderBiosList(data) {
  backBtn.classList.remove("d-none");
  const files = (data.bios || []).filter((entry) => entry.entry_type === "file");
  const allSystems = (data.systems || []).slice().sort((a, b) => a.localeCompare(b));
  if (!biosFilterInitialized) {
    biosFilterSelectedSystems = [...allSystems];
    biosFilterInitialized = true;
  }
  const total = Number(data.count || 0);
  const offset = Number(data.offset || 0);
  const limit = Number(data.limit || BIOS_PAGE_SIZE);
  const totalPages = Math.max(1, Math.ceil(total / limit));
  biosCurrentPage = Math.floor(offset / limit) + 1;
  if (biosCurrentPage > totalPages) biosCurrentPage = totalPages;
  if (biosCurrentPage < 1) biosCurrentPage = 1;
  const grouped = {};

  files.forEach((item) => {
    const path = item.path || item.name || "";
    const firstSegment = path.includes("/") ? path.split("/")[0] : "_root";
    if (!grouped[firstSegment]) grouped[firstSegment] = [];
    grouped[firstSegment].push(item);
  });

  const systems = Object.keys(grouped).sort((a, b) => a.localeCompare(b));
  content.innerHTML = `
    <div class="mb-3">
      <h2 class="h4 mb-1"><i class="bi bi-cpu me-2"></i>BIOS Files</h2>
      <div class="text-muted">Groups: ${systems.length} · Files: ${total} · Page: ${biosCurrentPage}/${totalPages}</div>
    </div>
    <div class="card shadow-sm mb-3">
      <div class="card-body">
        <div class="row g-3">
          <div class="col-12 col-lg-6">
            <label class="form-label mb-1">Search path/system (supports wildcard <code>*</code> and <code>?</code>)</label>
            <div class="input-group">
              <span class="input-group-text"><i class="bi bi-funnel"></i></span>
              <input id="biosSearchInput" class="form-control" type="search" value="${escapeHtml(biosFilterQuery)}" placeholder="examples: ps2/* , */firmware*">
              <button id="biosSearchBtn" type="button" class="btn btn-primary">Search</button>
              <button id="biosSearchClearBtn" type="button" class="btn btn-outline-secondary">Clear</button>
            </div>
          </div>
          <div class="col-12 col-lg-6">
            <label class="form-label mb-1">System filters</label>
            ${renderFilterDropdown("bios", allSystems, biosFilterSelectedSystems)}
          </div>
        </div>
      </div>
    </div>
    ${
      systems.length
        ? `<div class="card log-card">
          <div class="table-responsive">
            <table class="table table-hover align-middle themed-table bios-table">
              <thead><tr><th>System</th><th>BIOS File</th><th>Size</th><th>MD5</th><th></th></tr></thead>
              <tbody>
                ${systems.map((system) => grouped[system].map((item) => `
                  <tr>
                    <td><span class="badge text-bg-secondary">${escapeHtml(system === "_root" ? "root" : system)}</span></td>
                    <td><div class="fw-semibold">${escapeHtml(item.name)}</div><div class="small text-muted mono">${escapeHtml(item.path || item.name || "")}</div></td>
                    <td class="text-nowrap">${item.byte_count !== undefined ? formatBytes(item.byte_count) : "n/a"}</td>
                    <td class="small mono bios-fingerprint">${escapeHtml(item.bios_md5 || item.md5 || item.fingerprint || "n/a")}</td>
                    <td class="text-end">${
                      item.is_downloadable === false
                        ? `<button class="btn btn-secondary btn-sm" type="button" disabled><i class="bi bi-slash-circle me-1"></i>Disabled</button>`
                        : `<a class="btn btn-primary btn-sm" href="${biosDownloadUrl(item.unique_id)}"><i class="bi bi-download me-1"></i>Download</a>`
                    }</td>
                  </tr>
                `).join("")).join("")}
              </tbody>
            </table>
          </div>
        </div>`
        : `<div class="text-muted">No BIOS files found.</div>`
    }
    <div class="mt-3 d-flex gap-2">
      <button id="biosPrevBtn" type="button" class="btn btn-outline-primary btn-sm" ${biosCurrentPage <= 1 ? "disabled" : ""}>Previous</button>
      <button id="biosNextBtn" type="button" class="btn btn-outline-primary btn-sm" ${!data.has_more ? "disabled" : ""}>Next</button>
    </div>
  `;
  const biosPrevBtn = document.getElementById("biosPrevBtn");
  const biosNextBtn = document.getElementById("biosNextBtn");
  const biosSearchInputEl = document.getElementById("biosSearchInput");
  const biosSearchBtn = document.getElementById("biosSearchBtn");
  const biosSearchClearBtn = document.getElementById("biosSearchClearBtn");
  if (biosSearchInputEl) biosSearchInputEl.style.color = "#eef4ff";
  if (biosSearchBtn && biosSearchInputEl) {
    biosSearchBtn.addEventListener("click", async () => {
      biosFilterQuery = biosSearchInputEl.value || "";
      await loadBiosPage(0);
    });
  }
  if (biosSearchClearBtn && biosSearchInputEl) {
    biosSearchClearBtn.addEventListener("click", async () => {
      biosSearchInputEl.value = "";
      biosFilterQuery = "";
      await loadBiosPage(0);
    });
  }
  setupFilterDropdown("bios", async () => {
      const checked = Array.from(document.querySelectorAll(".bios-system-filter:checked")).map((el) => el.value);
      biosFilterSelectedSystems = checked;
      await loadBiosPage(0);
  });
  if (biosPrevBtn) {
    biosPrevBtn.addEventListener("click", async () => {
      const nextOffset = Math.max(0, offset - BIOS_PAGE_SIZE);
      await loadBiosPage(nextOffset);
    });
  }
  if (biosNextBtn) {
    biosNextBtn.addEventListener("click", async () => {
      const nextOffset = offset + BIOS_PAGE_SIZE;
      await loadBiosPage(nextOffset);
    });
  }
}
function renderThemeGallery(data) {
  backBtn.classList.remove("d-none");
  if (!data || !Array.isArray(data.images)) {
    data = { images: [], count: 0, has_more: false, returned: 0, offset: 0, limit: THEME_GALLERY_PAGE_SIZE, theme_name: "unknown" };
  }
  const systems = (data.systems || []).slice().sort((a, b) => a.localeCompare(b));
  if (!themeFilterInitialized) {
    themeFilterSelectedSystems = [...systems];
    themeFilterInitialized = true;
  }
  const total = Number(data.count || 0);
  const offset = Number(data.offset || 0);
  const limit = Number(data.limit || THEME_GALLERY_PAGE_SIZE);
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));

  content.innerHTML = `
    <div class="mb-3">
      <h2 class="h4 mb-1"><i class="bi bi-image me-2"></i>Theme Gallery</h2>
      <div class="text-muted">Theme: ${escapeHtml((data && data.theme_name) || "unknown")} · Images: ${total} · Page: ${page}/${totalPages}</div>
    </div>
    <div class="card shadow-sm mb-3">
      <div class="card-body">
        <div class="row g-3">
          <div class="col-12 col-lg-6">
            <label class="form-label mb-1">Search path/system (supports wildcard <code>*</code> and <code>?</code>)</label>
            <div class="input-group">
              <span class="input-group-text"><i class="bi bi-funnel"></i></span>
              <input id="themeSearchInput" class="form-control" type="search" value="${escapeHtml(themeFilterQuery)}" placeholder="examples: snes/* , */_inc/*logo*">
              <button id="themeSearchBtn" type="button" class="btn btn-primary">Search</button>
              <button id="themeSearchClearBtn" type="button" class="btn btn-outline-secondary">Clear</button>
            </div>
          </div>
          <div class="col-12 col-lg-6">
            <label class="form-label mb-1">System filters</label>
            ${renderFilterDropdown("theme", systems, themeFilterSelectedSystems)}
          </div>
        </div>
      </div>
    </div>
    <div class="row g-3">
      ${
        (data.images || []).map((item) => `
          <div class="col-12 col-md-6 col-xl-2">
            <div class="card shadow-sm tile h-100">
              <img
                src=""
                data-src="${item.url}"
                data-fallbacks='[]'
                class="card-img-top"
                alt="${escapeHtml(item.name)}"
                style="height: 180px; object-fit: contain; background: rgba(0,0,0,0.25);"
                loading="lazy"
              >
              <div class="card-body">
                <div class="fw-semibold small mb-1">${escapeHtml(item.name)}</div>
                <div class="text-muted small mono">${escapeHtml(item.folder)}</div>
              </div>
            </div>
          </div>
        `).join("") || `<div class="col-12"><div class="text-muted">No theme images found.</div></div>`
      }
    </div>
    <div class="mt-3 d-flex gap-2">
      <button id="themePrevBtn" type="button" class="btn btn-outline-primary btn-sm" ${offset <= 0 ? "disabled" : ""}>Previous</button>
      <button id="themeNextBtn" type="button" class="btn btn-outline-primary btn-sm" ${!data.has_more ? "disabled" : ""}>Next</button>
    </div>
  `;
  const searchInputEl = document.getElementById("themeSearchInput");
  const themeSearchBtn = document.getElementById("themeSearchBtn");
  const themeSearchClearBtn = document.getElementById("themeSearchClearBtn");
  if (searchInputEl) searchInputEl.style.color = "#eef4ff";
  if (themeSearchBtn && searchInputEl) {
    themeSearchBtn.addEventListener("click", async () => {
      themeFilterQuery = searchInputEl.value || "";
      await loadThemePage(0);
    });
  }
  if (themeSearchClearBtn && searchInputEl) {
    themeSearchClearBtn.addEventListener("click", async () => {
      searchInputEl.value = "";
      themeFilterQuery = "";
      await loadThemePage(0);
    });
  }
  setupFilterDropdown("theme", async () => {
      const checked = Array.from(document.querySelectorAll(".theme-system-filter:checked")).map((el) => el.value);
      themeFilterSelectedSystems = checked;
      await loadThemePage(0);
  });
  const themePrevBtn = document.getElementById("themePrevBtn");
  const themeNextBtn = document.getElementById("themeNextBtn");
  if (themePrevBtn) {
    themePrevBtn.addEventListener("click", async () => {
      const nextOffset = Math.max(0, offset - THEME_GALLERY_PAGE_SIZE);
      await loadThemePage(nextOffset);
    });
  }
  if (themeNextBtn) {
    themeNextBtn.addEventListener("click", async () => {
      const nextOffset = offset + THEME_GALLERY_PAGE_SIZE;
      await loadThemePage(nextOffset);
    });
  }
  setupLazyImages();
}
function renderSearchResults(data) {
  backBtn.classList.add("d-none");
  const results = data.results || [];
  const grouped = {};
  results.forEach((item) => {
    const key = item.system || "unknown";
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(item);
  });
  const systems = Object.keys(grouped).sort((a, b) => a.localeCompare(b));
  content.innerHTML = `
    <div class="mb-3">
      <h2 class="h4 mb-1"><i class="bi bi-binoculars me-2"></i>Search Results</h2>
      <div class="text-muted">Query: "${escapeHtml(data.query || "")}" · Scope: ${escapeHtml(data.system || "all systems")} · Matches: ${results.length} · Systems: ${systems.length}</div>
    </div>
    ${
      systems.length
        ? systems.map((system) => `
          <section class="mb-4">
            <h3 class="h5 mb-2"><i class="bi bi-controller me-2"></i>${escapeHtml(system)} <span class="text-muted">(${grouped[system].length})</span></h3>
            <div class="row g-3">
              ${grouped[system].map(item => {
                const fallbacks = [
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".png", true),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".jpg", true),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".jpeg", true),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".webp", true),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".png", false),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".jpg", false),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".jpeg", false),
                  publicRomImageUrl(item.system, item.name, item.image_stem, ".webp", false),
                ];
                return `
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="card shadow-sm tile h-100">
                    <img
                      src=""
                      data-src="${romImageByIdUrl(item.system, item.unique_id)}"
                      data-fallbacks='${JSON.stringify(fallbacks)}'
                      class="card-img-top"
                      alt="${escapeHtml(item.name)}"
                      style="height: 170px; object-fit: contain; background: #111;"
                      loading="lazy"
                    >
                    <div class="card-body">
                      <div class="fw-semibold mb-3">${escapeHtml(item.name)}</div>
                      ${
                        item.is_downloadable === false
                          ? `<button class="btn btn-secondary btn-sm" type="button" disabled><i class="bi bi-folder2-open me-1"></i>Folder ROM (No Download)</button>`
                          : `<a class="btn btn-primary btn-sm" href="${romDownloadUrl(item.system, item.unique_id)}"><i class="bi bi-download me-1"></i>Download</a>`
                      }
                    </div>
                  </div>
                </div>
              `;
              }).join("")}
            </div>
          </section>
        `).join("")
        : `<div class="text-muted">No matches found.</div>`
    }
  `;
  setupLazyImages();
}
async function renderSystem(system, page = 1) {
  currentSystemContext = system;
  backBtn.classList.remove("d-none");
  setSearchMode("system", system);
  setLoading(true, `Loading ${system} ROMs...`);
  const [romsData] = await Promise.all([
    getSystemRomData(system),
    applySystemTheme(system),
  ]);
  const allRoms = romsData.roms || [];
  const totalPages = Math.max(1, Math.ceil(allRoms.length / SYSTEM_ROM_PAGE_SIZE));
  const safePage = Math.max(1, Math.min(Number(page || 1), totalPages));
  const pageRoms = allRoms.slice((safePage - 1) * SYSTEM_ROM_PAGE_SIZE, safePage * SYSTEM_ROM_PAGE_SIZE);

  content.innerHTML = `
    <div class="mb-4">
      <h2 class="h4 mb-1">${escapeHtml(system)}</h2>
      <div class="text-muted">
        ROMs: ${allRoms.length} · Page ${safePage}/${totalPages}
      </div>
    </div>

    ${renderRomGrid(system, pageRoms, safePage, allRoms.length)}
  `;
  setupLazyImages();
  setLoading(false);
}
async function renderSearch(query) {
  setSearchMode("hidden");
  setLoading(true, `Searching for "${query}"...`);
  const systemFilter = currentSystemContext || null;
  if (!systemFilter) {
    clearSystemTheme();
  }
  const url = systemFilter
    ? `/search?q=${encodeURIComponent(query)}&system=${encodeURIComponent(systemFilter)}`
    : `/search?q=${encodeURIComponent(query)}`;
  const data = await api(url);
  renderSearchResults(data);
  setLoading(false);
}
async function renderBios() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading BIOS files...");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  biosCurrentPage = 1;
  biosFilterQuery = "";
  biosFilterSelectedSystems = [];
  biosFilterInitialized = false;
  await loadBiosPage(0);
  setLoading(false);
}
async function loadBiosPage(offset = 0) {
  const selected = biosFilterInitialized && !(biosFilterSelectedSystems || []).length ? ["__none__"] : (biosFilterSelectedSystems || []);
  const systemsParam = encodeURIComponent(selected.join(","));
  const url = `/bios?limit=${BIOS_PAGE_SIZE}&offset=${Math.max(0, offset)}&q=${encodeURIComponent(biosFilterQuery || "")}&systems=${systemsParam}`;
  const data = await api(url);
  renderBiosList(data);
}
async function renderThemeGalleryPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading theme images...");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  themeFilterInitialized = false;
  themeFilterSelectedSystems = [];
  await loadThemePage(0);
  setLoading(false);
}
async function renderSystemsPage() {
  currentSystemContext = null;
  setSearchMode("global");
  setLoading(true, "Loading systems...");
  clearSystemTheme();
  const data = await getSystemsData();
  renderSystems(data);
  setLoading(false);
  refreshRandomThemeLogo().catch(() => {});
}
async function renderHelpPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  titleNode.textContent = "Batocera Drone";
  subtitleNode.textContent = "How this Drone works";
  content.innerHTML = `
    <div class="help-page">
      <div class="help-header mb-4">
        <div>
          <div class="help-kicker">Batocera Drone</div>
          <h2 class="h3 mb-2">Run your whole collection like a fleet — not one machine at a time.</h2>
          <p class="mb-2 text-muted">Drone runs quietly on this Batocera machine and gives you a browser dashboard for everything on it — your library, saves, BIOS, artwork, and live health — from any phone, tablet, or computer on your network. No controller or TV required.</p>
          <p class="mb-0 text-muted">Pair it with Overmind, the optional fleet coordinator at <a href="https://www.batocera-swarm.com" target="_blank" rel="noopener noreferrer">Batocera Swarm <i class="bi bi-box-arrow-up-right ms-1"></i></a>, and you can manage every cabinet from one place: copy content between machines, send remote actions, and watch the health of the whole swarm.</p>
        </div>
      </div>

      <div class="row g-3 mb-4">
        <div class="col-12 col-md-6 col-xl-3">
          <div class="help-metric h-100">
            <i class="bi bi-phone"></i>
            <div>
              <div class="help-metric-title">Browse from anywhere</div>
              <div class="text-muted small">View and search your entire collection from any browser on your network.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-3">
          <div class="help-metric h-100">
            <i class="bi bi-arrow-left-right"></i>
            <div>
              <div class="help-metric-title">Sync between machines</div>
              <div class="text-muted small">Copy games, saves, BIOS, and artwork cabinet-to-cabinet — no re-downloading.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-3">
          <div class="help-metric h-100">
            <i class="bi bi-sliders"></i>
            <div>
              <div class="help-metric-title">Manage remotely</div>
              <div class="text-muted small">Kiosk mode, volume, restarts, and cache refreshes — straight from Overmind.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-3">
          <div class="help-metric h-100">
            <i class="bi bi-shield-lock"></i>
            <div>
              <div class="help-metric-title">Secure by design</div>
              <div class="text-muted small">Encrypted everywhere, certificate-verified peers, and credentials that never leave the machine.</div>
            </div>
          </div>
        </div>
      </div>

      <div class="row g-4">
        <div class="col-12 col-xl-7">
          <div class="help-section">
            <h3 class="h5 mb-3"><i class="bi bi-patch-question me-2"></i>Q&A</h3>
            <div class="accordion help-accordion" id="helpAccordion">
              ${[
                {
                  q: "Why would I use Batocera Drone?",
                  a: "Drone turns this machine into something you can see and control from a browser. Check what's installed, fix missing artwork, watch gameplay history, read logs, and — through Overmind — manage it alongside every other cabinet you own. It's most powerful once you have more than one machine to keep in sync."
                },
                {
                  q: "Can I copy content between machines?",
                  a: "Yes. When connected to Overmind, cabinets copy games, saves, BIOS, and artwork directly from each other over an encrypted peer-to-peer link. Set something up on one machine and let the others pull it — without re-downloading from the internet.",
                  url: "https://www.batocera-swarm.com",
                  linkText: "Open Batocera Swarm"
                },
                {
                  q: "Can I manage the machine remotely?",
                  a: "Yes. From Overmind you can turn Kiosk mode on or off, set the volume, restart the machine or EmulationStation, and rebuild the asset cache — then watch each action's status as it completes."
                },
                {
                  q: "Will my saves stay in sync?",
                  a: "Drone tracks your save files and reports changes, so Overmind can keep them aligned across the fleet. Pick up a game on one cabinet and continue on another."
                },
                {
                  q: "What does the BIOS page do?",
                  a: "Some emulators need firmware before games boot correctly. The BIOS page shows what this machine already has and what's missing, so you can spot gaps before they cause problems."
                },
                {
                  q: "What can Artwork & Metadata fix?",
                  a: "It repairs gamelist data — titles, descriptions, release dates, ratings, box art, marquees, and missing images — so your library looks complete and polished in Batocera."
                },
                {
                  q: "What does the Downloads page track?",
                  a: "Active, queued, and recent transfers heading to this machine, with progress, queue position, and cancellation controls for long file operations."
                },
                {
                  q: "What is the Asset Cache?",
                  a: "Drone's fast snapshot of what this machine holds and what still needs uploading. It's how Overmind understands the cabinet's contents at a glance without rescanning every time."
                },
                {
                  q: "What is Overmind?",
                  a: "The optional central coordinator for your fleet. Connect Drone to it to publish this machine's identity and inventory, run remote actions, and see swarm membership and health from one dashboard.",
                  url: "https://www.batocera-swarm.com",
                  linkText: "Open Batocera Swarm"
                },
                {
                  q: "Why are there Logs and Emulator Config pages?",
                  a: "Logs help you diagnose launch, service, and emulator problems quickly. Emulator Configs surface configuration files from common emulator locations so you can inspect settings without digging through the filesystem."
                },
                {
                  q: "Is it secure?",
                  a: "Drone keeps sensitive credentials local, gates protected tools behind authenticated routes, and uses certificates so trusted cabinets can identify each other without ever sharing private keys."
                },
                {
                  q: "Why do some features disappear?",
                  a: "Admin-only tools are hidden when admin routes are disabled or restricted by configuration. Everyday browsing still works while operational tools stay locked down."
                }
              ].map((item, index) => `
                <div class="accordion-item">
                  <h4 class="accordion-header" id="helpHeading${index}">
                    <button class="accordion-button ${index === 0 ? "" : "collapsed"}" type="button" data-bs-toggle="collapse" data-bs-target="#helpAnswer${index}" aria-expanded="${index === 0 ? "true" : "false"}" aria-controls="helpAnswer${index}">
                      ${escapeHtml(item.q)}
                    </button>
                  </h4>
                  <div id="helpAnswer${index}" class="accordion-collapse collapse ${index === 0 ? "show" : ""}" aria-labelledby="helpHeading${index}" data-bs-parent="#helpAccordion">
                    <div class="accordion-body">
                      ${escapeHtml(item.a)}
                      ${item.url ? `<div class="mt-2 small"><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.linkText || item.url)} <i class="bi bi-box-arrow-up-right ms-1"></i></a></div>` : ""}
                    </div>
                  </div>
                </div>
              `).join("")}
            </div>
          </div>
        </div>

        <div class="col-12 col-xl-5">
          <div class="help-section mb-4">
            <h3 class="h5 mb-3"><i class="bi bi-stars me-2"></i>What Drone does for you</h3>
            <div class="help-link-list">
              <button class="help-link-row" type="button" onclick="setHash('#admin/integration')"><i class="bi bi-arrow-left-right"></i><span><strong>Share across cabinets</strong><small>Copy games, saves, BIOS, and artwork peer-to-peer instead of downloading them everywhere.</small></span></button>
              <button class="help-link-row" type="button" onclick="setHash('#admin/artwork')"><i class="bi bi-images"></i><span><strong>Polish your library</strong><small>Fix titles, descriptions, box art, and marquees so everything looks complete.</small></span></button>
              <button class="help-link-row" type="button" onclick="setHash('#admin/gameplay-logs')"><i class="bi bi-clock-history"></i><span><strong>See what's been played</strong><small>Review detected game launches and recent play sessions.</small></span></button>
              <button class="help-link-row" type="button" onclick="setHash('#admin/system-info')"><i class="bi bi-pc-display"></i><span><strong>Check machine health</strong><small>CPU, memory, storage, network, and connection speed at a glance.</small></span></button>
            </div>
            <div class="mt-2 small">
              <a href="https://www.batocera-swarm.com" target="_blank" rel="noopener noreferrer">Manage your whole fleet with Overmind <i class="bi bi-box-arrow-up-right ms-1"></i></a>
            </div>
          </div>

          <div class="help-section">
            <h3 class="h5 mb-3"><i class="bi bi-lightbulb me-2"></i>Good to know</h3>
            <dl class="help-terms mb-0">
              <dt>Better with more machines</dt>
              <dd>A single Drone is a handy dashboard. A few of them with Overmind become a fleet you keep in sync and manage from one screen.</dd>
              <dt>Peer-to-peer sync</dt>
              <dd>Cabinets copy content directly from each other over encrypted links, so you don't re-download the same files on every machine.</dd>
              <dt>Serious about security</dt>
              <dd>Security was built in from the start, not bolted on. Every connection is encrypted over HTTPS, machine-to-machine transfers use mutually-authenticated TLS with pinned certificates so only trusted cabinets can connect, sensitive credentials never leave this machine, admin tools sit behind authentication, and unauthenticated requests are rate-limited and brute-force protected.</dd>
            </dl>
          </div>
        </div>
      </div>

      <div class="help-section mt-4">
        <h3 class="h5 mb-3"><i class="bi bi-wifi me-2"></i>Open this Drone from another device</h3>
        <p class="text-muted mb-3">When the installer finishes it prints this machine's exact address in a green banner &mdash; it looks like <code>https://batocera.local</code>. Open that from any device on your network. To find it again:</p>
        <ol class="mb-4">
          <li>On a phone, laptop, or computer on the same network, open <code>https://BATOCERA-HOSTNAME.local</code>.</li>
          <li>The default hostname is usually <code>batocera</code>, so try <code>https://batocera.local</code> first.</li>
          <li>Not sure of the name? Check Batocera under <strong>Network Settings</strong> &gt; <strong>Hostname</strong> and use that in place of <code>BATOCERA-HOSTNAME</code>.</li>
          <li>Older bookmarks and router rules can still use <code>https://BATOCERA-HOSTNAME.local:8443</code>.</li>
        </ol>
        <h3 class="h5 mb-3"><i class="bi bi-router me-2"></i>Enable content syncing (port forwarding)</h3>
        <p class="text-muted">Only needed so Overmind and other Drones can pull games, saves, BIOS, artwork, and configs from this machine. The Drone still connects to Overmind for monitoring and remote actions without it.</p>
        <ol class="mb-0">
          <li>Find your router address in <strong>System Info</strong> &gt; <strong>Router IP Address</strong>, then open that IP in a browser to sign in to your router.</li>
          <li>In the router, look for <strong>NAT</strong>, <strong>Port Forwarding</strong>, or <strong>Connected Devices</strong>.</li>
          <li>Forward port <code>443</code> to this machine's IP address (find it under <strong>Network Settings</strong> &gt; <strong>IP Address</strong>).</li>
          <li>Need help? Open <a href="https://www.youtube.com/results?search_query=How+to+enable+Home+Router+NAT+Port+Forwarding" target="_blank" rel="noopener noreferrer">NAT Port Forwarding Help <i class="bi bi-box-arrow-up-right ms-1"></i></a>.</li>
        </ol>
      </div>
    </div>
  `;
  setLoading(false);
}
async function renderAdminPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading admin panel...");
  clearSystemTheme();
  renderAdminMenu();
  setLoading(false);
  refreshRandomThemeLogo().catch(() => {});
}
async function renderAdminMenu() {
  titleNode.textContent = "Admin Panel";
  subtitleNode.textContent = "System administration";
  content.innerHTML = `
    <div class="row">
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/gameplay-logs')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-clock-history me-2"></i>Gameplay Logs</h5>
            <p class="card-text">View detected game launches and recent gameplay sessions.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/logs/es_launch_stdout?lines=200')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-journal-text me-2"></i>System Logs</h5>
            <p class="card-text">View system and emulator logs</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/emulators')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-file-earmark-code me-2"></i>Emulators</h5>
            <p class="card-text">View emulator config files mirrored to Overmind.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/artwork')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-images me-2"></i>Artwork & Metadata</h5>
            <p class="card-text">Manage gamelist artwork, metadata, imports, uploads, and marquee crops.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/integration')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-diagram-3 me-2"></i>Integration</h5>
            <p class="card-text">Enable and configure Overmind and Local Network integrations independently.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/api')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-braces me-2"></i>API Access</h5>
            <p class="card-text">Open Swagger docs, view certificate metadata, and download the public certificate.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile h-100">
          <div class="card-body d-flex flex-column">
            <h5 class="card-title"><i class="bi bi-cloud-download me-2"></i>Drone Update</h5>
            <p class="card-text">Download the latest Drone release and restart the app process without rebooting Batocera.</p>
            <button class="btn btn-primary mt-auto" onclick="updateDroneApp()"><i class="bi bi-arrow-clockwise me-1"></i>Download & Restart</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function updateDroneApp() {
  if (!window.confirm("Download the latest Drone release and restart the Drone app process? Batocera will keep running.")) return;
  const toast = showToast('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Downloading Drone update...', "info", null);
  try {
    const payload = await apiPost("/admin/system/update-drone", {});
    dismissToast(toast);
    showToast(`Drone update downloaded. Restarting app process... (${Math.round((payload.duration_ms || 0) / 1000)}s). Reloading shortly.`, "success", 10000);
    setTimeout(() => {
      window.location.href = `${window.location.pathname}${window.location.search}#home`;
      window.location.reload();
    }, 8000);
  } catch (error) {
    dismissToast(toast);
    showToast(`Drone update request ended unexpectedly: ${escapeHtml(error.message || "unknown error")}. If the service restarted, reload this page in a few seconds.`, "warning", 12000);
  }
}

function formatBytes(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = n;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function formatCompactLocalDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = number => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function renderDownloadRows(rows, allowCancel = true) {
  if (!rows.length) return '<div class="themed-empty">No downloads in this group.</div>';
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table download-table">
    <thead><tr><th>Status</th><th>Source</th><th>File</th><th>System</th><th>Progress</th><th>Speed</th><th>Started</th><th></th></tr></thead>
    <tbody>${rows.map(row => {
      const pct = Number(row.percentage || 0);
      const active = ["queued", "downloading"].includes(String(row.status || ""));
      const retryable = ["failed", "cancelled"].includes(String(row.status || ""));
      const statusClass = row.status === "failed" ? "danger" : row.status === "completed" ? "success" : row.status === "cancelled" ? "secondary" : row.status === "downloading" ? "info" : "primary";
      const filePath = row.file_path || row.relative_path || row.rom_name || "";
      const errorText = row.error_message || row.failure_reason || "";
      const jobId = escapeHtml(row.job_id || row.id || "");
      const actions = [
        allowCancel && active && jobId ? `<button class="btn btn-sm btn-outline-danger" title="Cancel download" aria-label="Cancel download" onclick="cancelDroneDownload('${jobId}')"><i class="bi bi-x-circle"></i></button>` : "",
        retryable && jobId ? `<button class="btn btn-sm btn-outline-primary" title="Retry download" aria-label="Retry download" onclick="retryDroneDownload('${jobId}')"><i class="bi bi-arrow-clockwise"></i></button>` : "",
      ].filter(Boolean).join(" ");
      return `<tr>
        <td><span class="badge text-bg-${statusClass}" title="${escapeHtml(errorText)}">${escapeHtml(row.status || "queued")}${row.queue_position ? ` #${row.queue_position}` : ""}</span></td>
        <td class="small mono">${escapeHtml(row.source_drone_id || "n/a")}</td>
        <td class="small mono" title="${escapeHtml(errorText || row.rom_fingerprint || "")}">${escapeHtml(filePath)}</td>
        <td class="small">${escapeHtml(row.system || "")}</td>
        <td class="small text-nowrap">${pct.toFixed(1)}% (${formatBytes(row.downloaded_bytes || row.bytes_transferred)} / ${formatBytes(row.total_bytes || row.file_size)})</td>
        <td class="small">${row.transfer_speed_bps ? `${formatBytes(row.transfer_speed_bps)}/s` : ""}</td>
        <td class="small text-nowrap">${escapeHtml(formatCompactLocalDate(row.started_at || row.download_started_at || row.created_at))}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

function renderDownloadsPanel(payload, includeHeader = true) {
  const active = payload.active || [];
  const queued = payload.queued || [];
  const recent = payload.recent || [];
  const summary = [
    ["Active", active.length, "bi-cloud-arrow-down", "info"],
    ["Queued", queued.length, "bi-hourglass-split", "warning"],
    ["Recent", recent.length, "bi-clock-history", "success"],
  ];
  return `
    ${includeHeader ? `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
      <div><strong>${escapeHtml(payload.target_drone_id || "This Drone")}</strong><div class="small text-muted">Transfers run one at a time on this Drone.</div></div>
      <button class="btn btn-sm btn-outline-primary" title="Refresh downloads" aria-label="Refresh downloads" onclick="renderDownloadsPage()"><i class="bi bi-arrow-repeat"></i></button>
    </div>` : ""}
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
      ${payload.paused
        ? `<span class="badge text-bg-warning"><i class="bi bi-pause-circle me-1"></i>Queue paused</span><button class="btn btn-sm btn-success" type="button" onclick="resumeDroneDownloads()"><i class="bi bi-play-fill me-1"></i>Resume</button>`
        : `<button class="btn btn-sm btn-outline-warning" type="button" ${(active.length || queued.length) ? "" : "disabled"} onclick="pauseDroneDownloads()"><i class="bi bi-pause-fill me-1"></i>Pause</button>`}
      <button class="btn btn-sm btn-outline-danger" type="button" ${queued.length ? "" : "disabled"} onclick="clearDroneDownloads()"><i class="bi bi-x-circle me-1"></i>Clear Queue</button>
    </div>
    <div class="download-summary-grid mb-3">
      ${summary.map(([label, count, icon, tone]) => `<div class="download-summary-card tone-${tone}"><i class="bi ${icon}"></i><div><strong>${count}</strong><span>${label}</span></div></div>`).join("")}
    </div>
    <div class="download-section">
      <div class="download-section-title"><span><i class="bi bi-lightning-charge me-2"></i>Active</span><span class="badge text-bg-info">${active.length}</span></div>
      ${renderDownloadRows(active)}
    </div>
    <div class="download-section">
      <div class="download-section-title"><span><i class="bi bi-hourglass-split me-2"></i>Queued</span><span class="badge text-bg-warning">${queued.length}</span></div>
      ${renderDownloadRows(queued)}
    </div>
    <div class="download-section mb-0">
      <div class="download-section-title"><span><i class="bi bi-clock-history me-2"></i>Recent</span><span class="badge text-bg-secondary">${recent.length}</span></div>
      ${renderDownloadRows(recent, false)}
    </div>
  `;
}

let localTransferPayload = {};
const localTransferViews = {
  queued: { query: "", limit: 10, page: 1 },
  recent: { query: "", limit: 10, page: 1 },
};

function localTransferPage(kind, rows) {
  const view = localTransferViews[kind];
  const query = view.query.trim().toLowerCase();
  const filtered = query ? rows.filter(row => JSON.stringify(row).toLowerCase().includes(query)) : rows;
  const pages = Math.max(1, Math.ceil(filtered.length / view.limit));
  view.page = Math.max(1, Math.min(view.page, pages));
  const start = (view.page - 1) * view.limit;
  return { rows: filtered.slice(start, start + view.limit), total: filtered.length, pages };
}

function renderLocalTransferGroup(kind, label, icon, tone, rows, allowCancel) {
  const page = localTransferPage(kind, rows);
  const view = localTransferViews[kind];
  return `<div class="download-section">
    <div class="download-section-title"><span><i class="bi ${icon} me-2"></i>${label}</span><span class="badge text-bg-${tone}">${page.total}</span></div>
    <div class="d-flex flex-wrap gap-2 mb-2">
      <input class="form-control form-control-sm" style="max-width:260px" placeholder="Search ${label.toLowerCase()}" value="${escapeHtml(view.query)}" onchange="setLocalTransferSearch('${kind}', this.value)" onkeydown="if(event.key==='Enter'){event.preventDefault();setLocalTransferSearch('${kind}',this.value)}">
      <select class="form-select form-select-sm" style="width:auto" onchange="setLocalTransferLimit('${kind}', this.value)">${[10, 50, 100, 200].map(size => `<option value="${size}" ${view.limit === size ? "selected" : ""}>${size}</option>`).join("")}</select>
      <button class="btn btn-sm btn-outline-secondary" ${view.page <= 1 ? "disabled" : ""} onclick="setLocalTransferPage('${kind}', ${view.page - 1})">Previous</button>
      <span class="small text-muted align-self-center">Page ${view.page} of ${page.pages}</span>
      <button class="btn btn-sm btn-outline-secondary" ${view.page >= page.pages ? "disabled" : ""} onclick="setLocalTransferPage('${kind}', ${view.page + 1})">Next</button>
    </div>
    ${renderDownloadRows(page.rows, allowCancel)}
  </div>`;
}

function renderLocalTransfersPanel(payload) {
  localTransferPayload = payload || {};
  const active = localTransferPayload.active || [];
  const queued = localTransferPayload.queued || [];
  const recent = localTransferPayload.recent || [];
  return `<div class="download-section">
      <div class="download-section-title"><span><i class="bi bi-lightning-charge me-2"></i>Active</span><span class="badge text-bg-info">${active.length}</span></div>
      ${renderDownloadRows(active)}
    </div>
    ${renderLocalTransferGroup("queued", "Queued", "bi-hourglass-split", "warning", queued, true)}
    ${renderLocalTransferGroup("recent", "Recent", "bi-clock-history", "secondary", recent, false)}`;
}

function refreshLocalTransfersPanel() {
  const node = document.getElementById("localDownloadsBody");
  if (node) node.innerHTML = renderLocalTransfersPanel(localTransferPayload);
}
function setLocalTransferSearch(kind, value) { localTransferViews[kind].query = value; localTransferViews[kind].page = 1; refreshLocalTransfersPanel(); }
function setLocalTransferLimit(kind, value) { localTransferViews[kind].limit = Number(value) || 10; localTransferViews[kind].page = 1; refreshLocalTransfersPanel(); }
function setLocalTransferPage(kind, value) { localTransferViews[kind].page = Number(value) || 1; refreshLocalTransfersPanel(); }

async function renderDownloadsPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading downloads...");
  clearSystemTheme();
  titleNode.textContent = "Downloads";
  subtitleNode.textContent = "One active transfer at a time on this Drone";
  try {
    const payload = await api("/admin/downloads");
    content.innerHTML = `
      <div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin/integration')">Back to Integration</button></div>
      <div class="card log-card mb-3"><div class="card-body py-3">
        ${renderDownloadsPanel(payload)}
      </div></div>`;
  } catch (error) {
    content.innerHTML = '<div class="empty-state">Unable to load downloads.</div>';
  } finally {
    setLoading(false);
  }
}

async function cancelDroneDownload(jobId) {
  if (!jobId || !window.confirm("Cancel this download?")) return;
  await apiPost(`/admin/downloads/${encodeURIComponent(jobId)}/cancel`, {});
  if (["#admin/integration", "#admin/overmind", "#admin/local-network"].includes(window.location.hash)) {
    const refresh = window.refreshLocalNetwork || window.refreshOvermindDownloads;
    if (typeof refresh === "function") await refresh();
    else await renderIntegrationPage();
  } else {
    await renderDownloadsPage();
  }
}

async function retryDroneDownload(jobId) {
  if (!jobId) return;
  await apiPost(`/admin/downloads/${encodeURIComponent(jobId)}/retry`, {});
  if (["#admin/integration", "#admin/overmind", "#admin/local-network"].includes(window.location.hash)) {
    const refresh = window.refreshLocalNetwork || window.refreshOvermindDownloads;
    if (typeof refresh === "function") await refresh();
    else await renderIntegrationPage();
  } else {
    await renderDownloadsPage();
  }
}

async function refreshDownloadsView() {
  if (["#admin/integration", "#admin/overmind", "#admin/local-network"].includes(window.location.hash)) {
    const refresh = window.refreshLocalNetwork || window.refreshOvermindDownloads;
    if (typeof refresh === "function") await refresh();
    else await renderIntegrationPage();
  } else {
    await renderDownloadsPage();
  }
}

async function pauseDroneDownloads() {
  try {
    await apiPost("/admin/downloads/pause", {});
    showToast("Downloads paused. The active transfer finishes; nothing new starts until you resume.", "info");
    await refreshDownloadsView();
  } catch (err) {
    showToast(`Failed to pause downloads: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function resumeDroneDownloads() {
  try {
    await apiPost("/admin/downloads/resume", {});
    showToast("Downloads resumed.", "success");
    await refreshDownloadsView();
  } catch (err) {
    showToast(`Failed to resume downloads: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function clearDroneDownloads() {
  if (!window.confirm("Clear the download queue? Queued items are cancelled so nothing else downloads. Any active transfer keeps running.")) return;
  try {
    const result = await apiPost("/admin/downloads/clear", {});
    showToast(`Cleared ${Number(result.cleared) || 0} queued download${(Number(result.cleared) || 0) === 1 ? "" : "s"}.`, "success");
    await refreshDownloadsView();
  } catch (err) {
    showToast(`Failed to clear queue: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function purgeAssetCache() {
  if (!window.confirm(
    "Purge the asset cache and force a full re-scan and Overmind re-upload?\n\n" +
    "Cached fingerprint values are kept, so ROMs are not re-fingerprinted. This clears stale or " +
    "duplicate entries and rebuilds Overmind's ROM list from a fresh full inventory."
  )) {
    return;
  }
  try {
    const result = await apiPost("/admin/asset-cache/purge", {});
    showToast(result.message || "Asset cache purge queued.", "success");
    if (["#admin/integration", "#admin/overmind"].includes(window.location.hash) && typeof window.refreshOvermindAssetCache === "function") {
      await window.refreshOvermindAssetCache();
    } else {
      await renderAssetCachePage();
    }
  } catch (err) {
    showToast(`Failed to purge asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function clearPendingAssetChanges() {
  if (!window.confirm(
    "Clear all pending asset changes waiting to upload to Overmind?\n\n" +
    "This keeps the local asset cache and files, but discards the unsent upload queue. " +
    "Discarded changes will not reach Overmind unless a later scan detects them again."
  )) {
    return;
  }
  try {
    const result = await apiPost("/admin/asset-cache/clear-pending", {});
    showToast(result.message || "Pending asset changes cleared.", "success");
    if (["#admin/integration", "#admin/overmind"].includes(window.location.hash) && typeof window.refreshOvermindAssetCache === "function") {
      await window.refreshOvermindAssetCache();
    } else {
      await renderAssetCachePage();
    }
  } catch (err) {
    showToast(`Failed to clear pending asset changes: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

function renderAssetCachePanel(payload, includeActions = true) {
  const counts = payload.counts || {};
  const pending = payload.pending_changes || {};
  const dateText = (value) => value ? new Date(value).toLocaleString() : "Not yet";
  const pendingTotal = Number(pending.total || 0);
  const statusClass = payload.active ? "text-bg-primary" : payload.needs_upload ? "text-bg-warning" : payload.uploaded ? "text-bg-success" : "text-bg-secondary";
  const statusText = payload.active ? "Scanning" : payload.needs_upload ? "Upload Pending" : payload.uploaded ? "Synced" : "Waiting";
  const stage = payload.active ? 1 : payload.needs_upload ? 2 : payload.uploaded ? 3 : 0;
  const metric = (label, value, icon, tone = "") => `<div class="asset-metric ${tone}"><i class="bi ${icon}"></i><div><strong>${Number(value || 0).toLocaleString()}</strong><span>${escapeHtml(label)}</span></div></div>`;
  const detail = (label, value) => `<div class="asset-detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "n/a")}</strong></div>`;
  const step = (number, label, text) => `<div class="asset-flow-step ${stage === number ? "active" : stage > number ? "complete" : ""}"><span>${stage > number ? '<i class="bi bi-check-lg"></i>' : number}</span><div><strong>${label}</strong><small>${text}</small></div></div>`;
  return `
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
      <div class="asset-cache-status-line">
        <span class="badge ${statusClass}">${escapeHtml(statusText)}</span>
        <span class="small text-muted">${pendingTotal.toLocaleString()} pending change${pendingTotal === 1 ? "" : "s"} waiting for Overmind upload</span>
      </div>
      ${includeActions ? `<div class="d-flex flex-wrap gap-2">
        <button class="btn btn-sm btn-outline-primary" onclick="renderAssetCachePage()"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
        <button class="btn btn-sm btn-outline-warning" onclick="clearPendingAssetChanges()" ${pendingTotal ? "" : "disabled"}><i class="bi bi-x-circle me-1"></i>Clear Pending</button>
        <button class="btn btn-sm btn-outline-danger" onclick="purgeAssetCache()">Purge Cache &amp; Resync</button>
      </div>` : ""}
    </div>
    ${pendingTotal ? `<div class="asset-cache-help mb-3"><strong>What this means:</strong> Drone has local asset changes queued for Overmind. If Overmind is connected, refresh after the next upload cycle. If these are stale or duplicated queue entries, use <strong>Clear Pending</strong> to discard the unsent queue without deleting local cache data.</div>` : ""}
    <div class="asset-flow mb-3">
      ${step(1, "Scan", payload.active ? "Reading local assets now" : `Last scan: ${dateText(payload.last_full_scan_at)}`)}
      ${step(2, "Queue", pendingTotal ? `${pendingTotal.toLocaleString()} changes waiting` : "No changes waiting")}
      ${step(3, "Sync", payload.uploaded && !payload.needs_upload ? "Overmind is current" : `Last upload: ${dateText(payload.last_successful_upload_at)}`)}
    </div>
    <div class="asset-metric-grid mb-3">
      ${metric("Systems", counts.systems, "bi-grid")}
      ${metric("ROMs", counts.roms, "bi-controller", "accent")}
      ${metric("BIOS", counts.bios, "bi-cpu")}
      ${metric("Artwork", counts.artwork, "bi-images")}
      ${metric("Pending", pendingTotal, "bi-cloud-arrow-up", pendingTotal ? "warning" : "")}
    </div>
    <div class="row g-3">
      <div class="col-12 col-xl-6">
        <div class="asset-detail-panel h-100">
          <h6>Cache Health</h6>
          ${detail("Poller", payload.poller_enabled ? `Every ${payload.poll_seconds}s` : "Disabled")}
          ${detail("Real-time watch", payload.watch_enabled ? (payload.watch_active ? "Active" : "Enabled, inactive") : "Disabled")}
          ${detail("Cache state", payload.complete ? (payload.dirty ? "Complete, changes pending" : "Complete") : "Building")}
          ${detail("Full refresh", payload.full_refresh_pending ? "Pending" : "Not required")}
          ${detail("Cache path", payload.path)}
        </div>
      </div>
      <div class="col-12 col-xl-6">
        <div class="asset-detail-panel h-100">
          <h6>Pending Upload Details</h6>
          ${detail("ROM changes", `${Number(pending.roms || 0).toLocaleString()} upserts · ${Number(pending.deleted_roms || 0).toLocaleString()} deletes`)}
          ${detail("BIOS changes", `${Number(pending.bios || 0).toLocaleString()} upserts · ${Number(pending.deleted_bios || 0).toLocaleString()} deletes`)}
          ${detail("Artwork changes", `${Number(pending.artwork || 0).toLocaleString()} upserts · ${Number(pending.deleted_artwork || 0).toLocaleString()} deletes`)}
          ${detail("Checkpoint", dateText(payload.scan_checkpoint_at))}
          ${detail("Last successful upload", dateText(payload.last_successful_upload_at))}
        </div>
      </div>
    </div>
  `;
}

async function renderAssetCachePage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  clearSystemTheme();
  titleNode.textContent = "Asset Cache";
  subtitleNode.textContent = "ROM, BIOS, artwork cache and Overmind upload state";
  setLoading(true, "Loading asset cache...");
  try {
    const payload = await api("/admin/asset-cache");
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin/integration')">Back to Integration</button>
      </div>
      <div class="card log-card"><div class="card-body">${renderAssetCachePanel(payload)}</div></div>
    `;
  } catch (err) {
    showToast(`Failed to load asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="mb-3">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">Back to Admin</button>
      </div>
      <div class="text-muted">Asset cache status could not be loaded.</div>
    `;
  } finally {
    setLoading(false);
  }
}
function renderArtworkCheckboxDropdown(kind, label, options, selected, allLabel = "Any", enableTools = false) {
  const selectedSet = new Set(selected || []);
  const buttonLabel = selectedSet.has("__none__")
    ? "None"
    : selectedSet.has("show_all")
    ? "Show All"
    : selectedSet.has("any")
    ? allLabel
    : selectedSet.size
      ? `${selectedSet.size} selected`
      : allLabel;
  return `
    <div class="dropdown app-checkbox-dropdown">
      <button class="btn btn-outline-primary dropdown-toggle w-100 text-start" type="button" id="${kind}ArtworkFilterToggle" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">${escapeHtml(label)}: ${escapeHtml(buttonLabel)}</button>
      <div class="dropdown-menu filter-dropdown-menu app-checkbox-menu">
        ${enableTools ? `
          <input id="artwork-${kind}-search" type="search" class="form-control form-control-sm mb-2" placeholder="Search ${escapeHtml(label.toLowerCase())}...">
          <div class="d-flex gap-2 mb-2">
            <button type="button" class="btn btn-outline-primary btn-sm" id="artwork-${kind}-select-all">Select all</button>
            <button type="button" class="btn btn-outline-secondary btn-sm" id="artwork-${kind}-unselect-all">Unselect all</button>
          </div>
        ` : ""}
        <div class="filter-options-scroll">
          ${options.map((option) => `
            <div class="form-check m-0 mb-1 artwork-${kind}-option" data-value="${escapeHtml(String(option.label || option.value).toLowerCase())}">
              <input class="form-check-input artwork-${kind}-filter" type="checkbox" value="${escapeHtml(option.value)}" id="artwork-${kind}-${escapeHtml(option.value)}" ${selectedSet.has(option.value) ? "checked" : ""}>
              <label class="form-check-label small" for="artwork-${kind}-${escapeHtml(option.value)}">${escapeHtml(option.label)}</label>
            </div>
          `).join("")}
        </div>
      </div>
    </div>
  `;
}
function selectedArtworkCheckboxValues(selector) {
  return Array.from(document.querySelectorAll(`${selector}:checked`)).map((el) => el.value);
}
function artworkMissingRowsHtml(roms) {
  return (roms || []).map((rom, idx) => `
    <tr id="artwork-row-${idx}" data-filter="${escapeHtml(`${rom.system} ${rom.name} ${(rom.missing || []).join(" ")}`.toLowerCase())}" onclick="selectArtworkRom(${idx})" style="cursor: pointer;">
      <td class="mono small">${escapeHtml(rom.system || "")}</td>
      <td>
        <div class="fw-semibold">${escapeHtml(rom.title || rom.name || "")}</div>
        <div class="text-muted small">${escapeHtml(rom.rom_name || rom.name || "")}</div>
        <div class="mt-1">
          <span class="badge ${rom.rom_exists ? "text-bg-success" : "text-bg-danger"}">${rom.rom_exists ? "ROM exists" : "ROM missing"}</span>
          ${rom.has_gamelist_entry ? "" : `<span class="badge text-bg-warning ms-1">new gamelist entry needed</span>`}
        </div>
      </td>
      <td>${(rom.missing || []).length ? (rom.missing || []).map((field) => `<span class="badge text-bg-danger me-1">${escapeHtml(field)}</span>`).join("") : `<span class="badge text-bg-success">complete</span>`}</td>
    </tr>
  `).join("") || `<tr><td colspan="3" class="text-muted p-3">No artwork or metadata results found.</td></tr>`;
}
function refreshArtworkTableRows() {
  const rows = document.getElementById("artworkRows");
  if (!rows) return;
  rows.innerHTML = artworkMissingRowsHtml(window.missingArtworkRoms || []);
  if (window.selectedArtworkRomIndex !== undefined) {
    const row = document.getElementById(`artwork-row-${window.selectedArtworkRomIndex}`);
    if (row) row.classList.add("artwork-selected-row");
  }
}
function artworkGamelistValueHtml(value) {
  if (Array.isArray(value)) {
    return value.map((item) => artworkGamelistValueHtml(item)).join(`<div class="border-top my-1"></div>`);
  }
  if (value && typeof value === "object") {
    const text = value.text ? `<div>${escapeHtml(value.text)}</div>` : "";
    const attrs = value.attributes
      ? Object.entries(value.attributes).map(([key, attrValue]) => `<div class="text-muted small">${escapeHtml(key)}: ${escapeHtml(attrValue)}</div>`).join("")
      : "";
    return text + attrs;
  }
  const normalized = String(value || "").trim();
  return normalized ? escapeHtml(normalized) : `<span class="text-muted">empty</span>`;
}
function artworkGamelistEditValue(value) {
  if (Array.isArray(value)) return value.map((item) => artworkGamelistEditValue(item)).filter(Boolean).join("\n");
  if (value && typeof value === "object") return String(value.text || "");
  return String(value || "");
}
function artworkGamelistFieldControl(field, value) {
  const normalized = artworkGamelistEditValue(value);
  const label = field === "desc" ? "Description" : field;
  if (field === "desc") {
    return `
      <div class="gamelist-edit-field-row gamelist-edit-field-wide">
        <label class="gamelist-edit-label">${escapeHtml(label)}</label>
        <textarea class="form-control form-control-sm gamelist-edit-field" data-gamelist-field="${escapeHtml(field)}" rows="3">${escapeHtml(normalized)}</textarea>
      </div>
    `;
  }
  return `
    <div class="gamelist-edit-field-row">
      <label class="gamelist-edit-label" title="${escapeHtml(label)}">${escapeHtml(label)}</label>
      <input class="form-control form-control-sm gamelist-edit-field" data-gamelist-field="${escapeHtml(field)}" value="${escapeHtml(normalized)}">
    </div>
  `;
}
function artworkGamelistDetailsHtml(rom) {
  const details = rom && rom.gamelist ? rom.gamelist : {};
  const entries = Object.entries(details);
  const statusBadge = `<span class="badge ${rom.rom_exists ? "text-bg-success" : "text-bg-danger"}">${rom.rom_exists ? "ROM exists" : "ROM missing"}</span>`;
  if (!rom.has_gamelist_entry) {
    return `
      <div class="fw-semibold">${escapeHtml(rom.title || rom.name || "")}</div>
      <div class="text-muted small mb-2">${escapeHtml(rom.system || "")} · ${escapeHtml(rom.rom_path || rom.rom_name || "")}</div>
      <div class="mb-3">${statusBadge}</div>
      <div class="text-warning small fw-semibold">No gamelist.xml entry exists for this ROM.</div>
      <div class="text-muted small">Use the <strong>Edit</strong> tab to create one.</div>
    `;
  }
  return `
    <div class="d-flex justify-content-between align-items-start gap-2 mb-2">
      <div>
        <div class="fw-semibold">${escapeHtml(rom.title || rom.name || "")}</div>
        <div class="text-muted small">${escapeHtml(rom.system || "")} · Missing ${(rom.missing || []).map(escapeHtml).join(", ")}</div>
      </div>
      ${statusBadge}
    </div>
    <dl class="gamelist-details mb-0">
      ${entries.map(([key, value]) => `
        <dt>${escapeHtml(key)}</dt>
        <dd class="small mb-2">${artworkGamelistValueHtml(value)}</dd>
      `).join("") || `<dd class="text-muted small mb-0">No gamelist details found.</dd>`}
    </dl>
  `;
}
function artworkGamelistEditFormHtml(rom) {
  const details = rom && rom.gamelist ? rom.gamelist : {};
  const editableFields = Array.from(new Set(GAMELIST_EDIT_FIELDS.concat(Object.keys(details).filter((key) => key !== "path"))));
  if (!rom.has_gamelist_entry) {
    return `
      <form id="gamelistEditForm" class="compact-edit">
        <div class="gamelist-edit-grid">
          ${artworkGamelistFieldControl("name", rom.title || rom.name || "")}
          ${artworkGamelistFieldControl("desc", "")}
        </div>
        <div class="d-flex gap-2 mt-2">
          <button class="btn btn-sm btn-primary" type="button" onclick="saveSelectedArtworkGamelist()">Save Gamelist Data</button>
        </div>
      </form>
    `;
  }
  return `
    <form id="gamelistEditForm" class="compact-edit">
      <div class="gamelist-edit-grid">
        ${editableFields.map((field) => artworkGamelistFieldControl(field, details[field])).join("")}
      </div>
      <div class="d-flex gap-2 mt-2">
        <button class="btn btn-sm btn-primary" type="button" onclick="saveSelectedArtworkGamelist()">Save Metadata</button>
      </div>
    </form>
  `;
}
function artworkPayloadUrl(forceRefresh = false) {
  const fieldsParam = encodeURIComponent((artworkSelectedFields || []).join(","));
  const systemsParam = encodeURIComponent((artworkSelectedSystems || []).join(","));
  const includeFilesystem = artworkIncludeFilesystem || artworkShowAllSelected();
  return `/admin/artwork/missing?limit=${ARTWORK_PAGE_SIZE}&offset=${artworkCurrentOffset}&refresh=${forceRefresh ? "1" : "0"}&fields=${fieldsParam}&systems=${systemsParam}&q=${encodeURIComponent(artworkFilterQuery)}&rom_status=${encodeURIComponent(artworkRomStatus)}${includeFilesystem ? "&include_filesystem=1" : ""}`;
}
function updateArtworkPageFromPayload(payload) {
  const roms = payload.roms || [];
  const fieldCounts = payload.field_counts || {};
  const total = Number(payload.count || 0);
  const limit = Number(payload.limit || ARTWORK_PAGE_SIZE);
  const pageOffset = Number(payload.offset || 0);
  const page = Math.floor(pageOffset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const firstItem = total ? pageOffset + 1 : 0;
  const lastItem = pageOffset + roms.length;
  artworkCurrentOffset = pageOffset;
  window.missingArtworkRoms = roms;

  const rows = document.getElementById("artworkRows");
  if (rows) {
    rows.innerHTML = artworkMissingRowsHtml(roms);
    const row = document.getElementById(`artwork-row-${window.selectedArtworkRomIndex}`);
    if (row) row.classList.add("artwork-selected-row");
  }
  const summary = document.getElementById("artworkSummary");
  if (summary) {
    summary.innerHTML = `
      <span class="badge text-bg-secondary">ROM Files: ${total}</span>
      <span class="badge text-bg-light border">Showing: ${firstItem}-${lastItem}</span>
      <span class="badge text-bg-light border">Page: ${page}/${totalPages}</span>
      <span class="badge text-bg-light border">Mode: ${escapeHtml(payload.mode || "gamelist")}</span>
      <span class="badge text-bg-light border">Scan: ${Number(payload.elapsed_ms || 0)} ms</span>
      ${(payload.fields || []).map((field) => `<span class="badge text-bg-light border">${escapeHtml(field)}: ${Number(fieldCounts[field] || 0)}</span>`).join("")}
    `;
  }
  const prevBtn = document.getElementById("artworkPrevBtn");
  const nextBtn = document.getElementById("artworkNextBtn");
  if (prevBtn) prevBtn.disabled = pageOffset <= 0;
  if (nextBtn) nextBtn.disabled = !payload.has_more;
  const cleanupBtn = document.getElementById("removeMissingGamelistBtn");
  if (cleanupBtn) cleanupBtn.disabled = total <= 0;
}
async function refreshArtworkResults(forceRefresh = false) {
  setLoading(true, "Updating artwork results...");
  try {
    const payload = await api(artworkPayloadUrl(forceRefresh));
    updateArtworkPageFromPayload(payload);
    history.replaceState(null, "", artworkHash());
  } catch (err) {
    showToast(`Failed to update artwork results: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
function setupArtworkDropdownTools(kind, onApply) {
  const search = document.getElementById(`artwork-${kind}-search`);
  const selectAll = document.getElementById(`artwork-${kind}-select-all`);
  const unselectAll = document.getElementById(`artwork-${kind}-unselect-all`);
  if (search) {
    search.addEventListener("input", () => {
      const q = (search.value || "").trim().toLowerCase();
      document.querySelectorAll(`.artwork-${kind}-option`).forEach((node) => {
        node.style.display = !q || (node.getAttribute("data-value") || "").includes(q) ? "" : "none";
      });
    });
  }
  if (selectAll) {
    selectAll.addEventListener("click", (event) => {
      event.preventDefault();
      document.querySelectorAll(`.artwork-${kind}-filter`).forEach((node) => {
        node.checked = true;
      });
      onApply();
    });
  }
  if (unselectAll) {
    unselectAll.addEventListener("click", (event) => {
      event.preventDefault();
      document.querySelectorAll(`.artwork-${kind}-filter`).forEach((node) => {
        node.checked = false;
      });
      onApply();
    });
  }
}
async function renderMissingArtworkPage(includeFilesystem = false, forceRefresh = false, offset = 0, fields = artworkSelectedFields, systems = artworkSelectedSystems, query = artworkFilterQuery, romStatus = artworkRomStatus) {
  titleNode.textContent = "Artwork & Metadata";
  subtitleNode.textContent = "Manage gamelist.xml artwork, metadata, imports, uploads, and marquee crops";
  artworkIncludeFilesystem = !!includeFilesystem;
  artworkCurrentOffset = Math.max(0, Number(offset || 0));
  artworkSelectedFields = fields && fields.length ? fields : ["any"];
  artworkSelectedSystems = systems || [];
  artworkFilterQuery = query || "";
  artworkRomStatus = ["any", "exists", "missing"].includes(romStatus) ? romStatus : "any";
  syncArtworkHash();
  setSearchMode("hidden");
  setLoading(true, includeFilesystem ? "Scanning ROM directories..." : "Scanning gamelists...");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  try {
    const payload = await api(artworkPayloadUrl(forceRefresh));
    const roms = payload.roms || [];
    const fieldCounts = payload.field_counts || {};
    const availableFields = [{ value: "any", label: "Any" }, { value: "show_all", label: "Show All" }].concat((payload.fields || []).map((field) => ({ value: field, label: field === "duplicate_artwork" ? "Duplicate Artwork" : field })));
    const availableSystems = (payload.systems || []).map((system) => ({ value: system, label: system }));
    const total = Number(payload.count || 0);
    const limit = Number(payload.limit || ARTWORK_PAGE_SIZE);
    const pageOffset = Number(payload.offset || 0);
    const page = Math.floor(pageOffset / limit) + 1;
    const totalPages = Math.max(1, Math.ceil(total / limit));
    const firstItem = total ? pageOffset + 1 : 0;
    const lastItem = pageOffset + roms.length;
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button>
        <button class="btn btn-outline-primary" onclick="renderMissingArtworkPage(false, true, 0, artworkSelectedFields, artworkSelectedSystems, artworkFilterQuery, artworkRomStatus)">Refresh</button>
        <button id="removeMissingGamelistBtn" class="btn btn-outline-danger" type="button">Remove Missing ROM Entries</button>
      </div>
      <div id="artworkSummary" class="mb-3 d-flex flex-wrap gap-2">
        <span class="badge text-bg-secondary">ROM Files: ${total}</span>
        <span class="badge text-bg-light border">Showing: ${firstItem}-${lastItem}</span>
        <span class="badge text-bg-light border">Page: ${page}/${totalPages}</span>
        <span class="badge text-bg-light border">Mode: ${escapeHtml(payload.mode || "gamelist")}</span>
        <span class="badge text-bg-light border">Scan: ${Number(payload.elapsed_ms || 0)} ms</span>
        ${(payload.fields || []).map((field) => `<span class="badge text-bg-light border">${escapeHtml(field)}: ${Number(fieldCounts[field] || 0)}</span>`).join("")}
      </div>
      <div class="card log-card artwork-filter-panel mb-3">
        <div class="card-body">
          <div class="row g-3">
            <div class="col-12 col-lg-4">
              ${renderArtworkCheckboxDropdown("field", "Missing Type", availableFields, artworkSelectedFields, "Any")}
            </div>
            <div class="col-12 col-lg-4">
              ${renderArtworkCheckboxDropdown("system", "System", availableSystems, artworkSelectedSystems.length ? artworkSelectedSystems : availableSystems.map((item) => item.value), "All systems", true)}
            </div>
            <div class="col-12 col-lg-4">
              ${renderArtworkCheckboxDropdown("status", "ROM Status", [
                { value: "any", label: "Any" },
                { value: "exists", label: "Exists" },
                { value: "missing", label: "Missing" },
              ], [artworkRomStatus || "any"], "Any")}
            </div>
          </div>
        </div>
      </div>
      <div class="row g-3 artwork-results-row">
        <div class="col-12 col-xl-7">
          <div class="card log-card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span>Artwork & Metadata</span>
              <input id="artworkFilter" class="form-control form-control-sm" style="max-width: 260px;" type="search" value="${escapeHtml(artworkFilterQuery)}" placeholder="Filter all results">
            </div>
            <div class="card-header d-flex justify-content-between align-items-center gap-2">
              <button id="artworkPrevBtn" class="btn btn-sm btn-outline-primary" type="button" ${pageOffset <= 0 ? "disabled" : ""}>Previous</button>
              <span class="text-muted small">Search and dropdown filters apply before paging.</span>
              <button id="artworkNextBtn" class="btn btn-sm btn-outline-primary" type="button" ${!payload.has_more ? "disabled" : ""}>Next</button>
            </div>
            <div class="card-body p-0">
              <div class="table-responsive" style="max-height: 620px;">
                <table class="table table-sm table-hover align-middle mb-0">
                  <thead class="table-light">
                    <tr>
                      <th>System</th>
                      <th>ROM</th>
                      <th>Missing</th>
                    </tr>
                  </thead>
                  <tbody id="artworkRows">${artworkMissingRowsHtml(roms)}</tbody>
                </table>
              </div>
            </div>
          </div>
	        </div>
	        <div class="col-12 col-xl-5">
	          <div class="card log-card">
	            <div class="card-body">
	              <div id="selectedArtworkRom" class="text-muted">Select a ROM to view gamelist details and search LaunchBox.</div>
	            </div>
	          </div>
	        </div>
      </div>
    `;
    window.missingArtworkRoms = roms;
    const applyArtworkFilters = () => {
      let selectedFields = selectedArtworkCheckboxValues(".artwork-field-filter");
      if (selectedFields.includes("show_all")) selectedFields = ["show_all"];
      else if (selectedFields.includes("any") || !selectedFields.length) selectedFields = ["any"];
      const selectedSystems = selectedArtworkCheckboxValues(".artwork-system-filter");
      const allSystems = availableSystems.map((item) => item.value);
      const normalizedSystems = selectedSystems.length === allSystems.length ? [] : (selectedSystems.length ? selectedSystems : ["__none__"]);
      artworkSelectedFields = selectedFields;
      artworkSelectedSystems = normalizedSystems;
      artworkCurrentOffset = 0;
      refreshArtworkResults(false);
    };
    const applyRomStatusFilter = () => {
      const checked = selectedArtworkCheckboxValues(".artwork-status-filter");
      let nextStatus = checked.find((value) => value !== "any") || "any";
      artworkRomStatus = ["any", "exists", "missing"].includes(nextStatus) ? nextStatus : "any";
      document.querySelectorAll(".artwork-status-filter").forEach((node) => {
        node.checked = node.value === artworkRomStatus;
      });
      artworkCurrentOffset = 0;
      refreshArtworkResults(false);
    };
    document.querySelectorAll(".artwork-status-filter").forEach((node) => {
      node.addEventListener("change", () => {
        if (!node.checked && selectedArtworkCheckboxValues(".artwork-status-filter").length === 0) {
          const anyNode = document.querySelector('.artwork-status-filter[value="any"]');
          if (anyNode) anyNode.checked = true;
        }
        if (node.checked) {
          document.querySelectorAll(".artwork-status-filter").forEach((item) => {
            if (item !== node) item.checked = false;
          });
        }
        applyRomStatusFilter();
      });
    });
    const removeMissingBtn = document.getElementById("removeMissingGamelistBtn");
    if (removeMissingBtn) {
      removeMissingBtn.disabled = total <= 0;
      removeMissingBtn.addEventListener("click", removeMissingGamelistEntriesForCurrentFilters);
    }
    document.querySelectorAll(".artwork-field-filter").forEach((node) => {
      node.addEventListener("change", () => {
        if ((node.value === "any" || node.value === "show_all") && node.checked) {
          document.querySelectorAll(".artwork-field-filter").forEach((item) => {
            if (item !== node) item.checked = false;
          });
        } else if (node.value !== "any" && node.value !== "show_all" && node.checked) {
          const anyNode = document.querySelector('.artwork-field-filter[value="any"]');
          if (anyNode) anyNode.checked = false;
          const showAllNode = document.querySelector('.artwork-field-filter[value="show_all"]');
          if (showAllNode) showAllNode.checked = false;
        }
        applyArtworkFilters();
      });
    });
    document.querySelectorAll(".artwork-system-filter").forEach((node) => {
      node.addEventListener("change", applyArtworkFilters);
    });
    setupArtworkDropdownTools("system", applyArtworkFilters);
    const prevBtn = document.getElementById("artworkPrevBtn");
    const nextBtn = document.getElementById("artworkNextBtn");
    if (prevBtn) {
      prevBtn.addEventListener("click", async () => {
        artworkCurrentOffset = Math.max(0, artworkCurrentOffset - ARTWORK_PAGE_SIZE);
        await refreshArtworkResults(false);
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", async () => {
        artworkCurrentOffset = artworkCurrentOffset + ARTWORK_PAGE_SIZE;
        await refreshArtworkResults(false);
      });
    }
    const filter = document.getElementById("artworkFilter");
    if (filter) {
      filter.addEventListener("input", () => {
        artworkFilterQuery = (filter.value || "").trim();
        if (artworkFilterDebounceTimer) window.clearTimeout(artworkFilterDebounceTimer);
        artworkFilterDebounceTimer = window.setTimeout(() => {
          artworkCurrentOffset = 0;
          refreshArtworkResults(false);
        }, 300);
      });
    }
  } catch (err) {
    showToast(`Failed to scan artwork: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="mb-3">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button>
      </div>
      <div class="text-muted">Artwork results could not be loaded.</div>
    `;
  } finally {
    setLoading(false);
  }
}
const LAUNCHBOX_METADATA_FIELDS = [
  "name", "desc", "genre", "developer", "publisher", "releasedate",
  "players", "rating", "favorite", "hidden", "kidgame", "adult",
  "image", "thumbnail", "marquee", "fanart", "boxart", "video",
  "platform", "esrb", "overview", "playmode", "regional", "favorites"
];
function artworkImageUploadHtml(rom, field) {
  const existingValue = rom.existing && rom.existing[field] ? rom.existing[field] : null;
  const existingUrl = artworkExistingImageUrl(rom, existingValue);
  const existingDisplay = existingValue ? `<span class="text-muted small artwork-upload-status">has ${escapeHtml(field)}</span>` : `<span class="text-muted small artwork-upload-status">no ${escapeHtml(field)}</span>`;
  const viewBtn = existingUrl
    ? `<button class="btn btn-sm btn-outline-secondary btn-icon artwork-view-btn" type="button" data-image-url="${escapeHtml(existingUrl)}" data-image-title="${escapeHtml(field)}" title="View existing ${escapeHtml(field)}"><i class="bi bi-eye"></i></button>`
    : `<button class="btn btn-sm btn-outline-secondary btn-icon" type="button" disabled title="No ${escapeHtml(field)} to view"><i class="bi bi-eye-slash"></i></button>`;
  return `
    <div class="artwork-upload-item">
      <span class="artwork-upload-label" title="${escapeHtml(field)}">${escapeHtml(field)}</span>
      <input type="file" class="form-control form-control-sm artwork-upload-file" accept="image/*" data-field="${escapeHtml(field)}">
      <button class="btn btn-sm btn-primary btn-icon artwork-upload-btn" type="button" data-field="${escapeHtml(field)}" title="Upload"><i class="bi bi-upload"></i></button>
      ${viewBtn}
      ${existingDisplay}
    </div>
  `;
}
function artworkExistingImageUrl(rom, value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (/^(https?:)?\/\//i.test(raw) || raw.startsWith("data:")) return raw;
  if (raw.startsWith(API_BASE) || raw.startsWith("/public/")) return raw;
  const normalized = raw.replace(/\\/g, "/").replace(/^\.\//, "");
  const imagePrefix = "images/";
  const imageFile = normalized.toLowerCase().startsWith(imagePrefix)
    ? normalized.substring(imagePrefix.length)
    : normalized.split("/").pop();
  if (!imageFile) return raw;
  return `${API_BASE}/public/systems/${encodeURIComponent(rom.system || "")}/images/${encodeURIComponent(imageFile)}`;
}
function artworkEditableImageFields(rom) {
  const fieldSet = new Set(rom.missing || []);
  const withExisting = new Set(GAMELIST_EDIT_FIELDS.filter(f => ["image","thumbnail","marquee","fanart","boxart","video"].includes(f)));
  return `<div class="artwork-upload-grid">${Array.from(withExisting).map(f => artworkImageUploadHtml(rom, f)).join("")}</div>`;
}
function artworkMarqueeCropperHtml(rom) {
  const fields = ["image", "thumbnail", "fanart", "boxart", "marquee"];
  const buttons = fields.map((field) => {
    const existingValue = rom.existing && rom.existing[field] ? rom.existing[field] : "";
    const url = artworkExistingImageUrl(rom, existingValue);
    if (!url) return "";
    return `
      <button class="btn btn-sm btn-outline-primary marquee-crop-source-btn" type="button" data-source-field="${escapeHtml(field)}" data-image-url="${escapeHtml(url)}">
        <i class="bi bi-crop me-1"></i>${escapeHtml(field)}
      </button>
    `;
  }).filter(Boolean).join("");
  if (!buttons) return `<div class="text-muted small">Add or import an image, fanart, boxart, or thumbnail first.</div>`;
  return `<div class="marquee-source-grid">${buttons}</div>`;
}
function artworkMetadataEditFields(rom) {
  const details = rom.gamelist || {};
  const metaFields = ["name","desc","genre","developer","publisher","releasedate","players","rating"];
  return metaFields.map(field => `
    <div class="mb-2">
      <label class="form-label text-muted mb-1">${escapeHtml(field === "desc" ? "Description" : field)}</label>
      ${field === "desc"
        ? `<textarea class="form-control form-control-sm gamelist-edit-field" data-gamelist-field="${escapeHtml(field)}" rows="3">${escapeHtml(artworkGamelistEditValue(details[field]))}</textarea>`
        : `<input class="form-control form-control-sm gamelist-edit-field" data-gamelist-field="${escapeHtml(field)}" value="${escapeHtml(artworkGamelistEditValue(details[field]))}">`
      }
    </div>
  `).join("");
}
function googleImageSearchUrl(rom) {
  const query = `${artworkRomSearchTitle(rom)} ${rom.system || ""} images`.trim();
  return `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(query)}`;
}
function artworkRomSearchTitle(rom) {
  const gamelistName = rom && rom.gamelist && typeof rom.gamelist.name === "string" ? rom.gamelist.name : "";
  return (gamelistName || rom.search_title || rom.title || rom.name || "").trim();
}
function scraperSearchQuery(rom, includeSystem = true) {
  return `${artworkRomSearchTitle(rom)} ${includeSystem ? (rom.system || "") : ""}`.trim();
}
function launchBoxSearchUrl(rom) {
  return `https://gamesdb.launchbox-app.com/games/results/${encodeURIComponent(scraperSearchQuery(rom, false))}`;
}
function theGamesDBSearchUrl(rom) {
  return `https://thegamesdb.net/search.php?name=${encodeURIComponent(scraperSearchQuery(rom, false))}`;
}
function mobyGamesSearchUrl(rom) {
  return `https://www.mobygames.com/search/?q=${encodeURIComponent(scraperSearchQuery(rom, false))}`;
}
function artworkExternalLinksHtml(rom) {
  return `
    <div class="d-flex flex-wrap gap-2 align-items-center mb-3">
      <a class="btn btn-sm btn-outline-primary" href="${escapeHtml(googleImageSearchUrl(rom))}" target="_blank" rel="noopener noreferrer" title="Search Google Images"><i class="bi bi-google me-1"></i>Google</a>
      <a class="btn btn-sm btn-outline-primary" href="${escapeHtml(launchBoxSearchUrl(rom))}" target="_blank" rel="noopener noreferrer" title="Open LaunchBox search"><i class="bi bi-box-arrow-up-right me-1"></i>LaunchBox</a>
      <a class="btn btn-sm btn-outline-primary" href="${escapeHtml(theGamesDBSearchUrl(rom))}" target="_blank" rel="noopener noreferrer" title="Open TheGamesDB search"><i class="bi bi-box-arrow-up-right me-1"></i>TheGamesDB</a>
      <a class="btn btn-sm btn-outline-primary" href="${escapeHtml(mobyGamesSearchUrl(rom))}" target="_blank" rel="noopener noreferrer" title="Open MobyGames search"><i class="bi bi-box-arrow-up-right me-1"></i>MobyGames</a>
    </div>
  `;
}
// Update URL when a rom is selected in artwork page
function setArtworkSelectedRomHash(index) {
  const rom = (window.missingArtworkRoms || [])[index];
  if (!rom) return;
  const params = new URLSearchParams(window.location.hash.split("?")[1] || "");
  params.set("selected", String(index));
  const newHash = `#admin/artwork?${params.toString()}`;
  if (window.location.hash !== newHash) {
    history.replaceState(null, "", newHash);
  }
}
// Upload image for a given field
function bindArtworkEditButtons(rom, index) {
  document.querySelectorAll(".artwork-upload-btn").forEach((button) => {
    button.addEventListener("click", () => {
      uploadArtworkImage(rom, button.getAttribute("data-field") || "", button);
    });
  });
  document.querySelectorAll(".artwork-view-btn").forEach((button) => {
    button.addEventListener("click", () => {
      showImageLightbox(button.getAttribute("data-image-url") || "", button.getAttribute("data-image-title") || "");
    });
  });
  document.querySelectorAll(".marquee-crop-source-btn").forEach((button) => {
    button.addEventListener("click", () => {
      openMarqueeCropper(index, button.getAttribute("data-image-url") || "", button.getAttribute("data-source-field") || "image");
    });
  });
}
async function uploadArtworkImage(rom, field, btnEl) {
  const fileInput = btnEl.closest(".artwork-upload-item").querySelector(".artwork-upload-file");
  const file = fileInput && fileInput.files[0];
  if (!file) { showToast("Please select an image file first.", "warning"); return; }
  const system = rom.system || "";
  btnEl.disabled = true;
  btnEl.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
  setLoading(true, `Uploading ${field}...`);
  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("field", field);
    formData.append("system", system);
    formData.append("rom_id", rom.unique_id || "");
    formData.append("rom_path", rom.rom_path || "");
    const res = await fetch(`${API_BASE}/admin/artwork/upload`, {
      method: "POST",
      credentials: "include",
      body: formData,
    });
    if (res.status === 401) {
      window.location.reload();
      throw new Error("Authentication required");
    }
    if (!res.ok) {
      let msg = `Upload failed: ${res.status}`;
      try { const d = await res.json(); if (d.error) msg = d.error; } catch(_) {}
      throw new Error(msg);
    }
    const result = await res.json();
    if (result.existing) rom.existing = result.existing;
    if (result.gamelist) rom.gamelist = result.gamelist;
    if (result.missing) rom.missing = result.missing;
    if (result.has_gamelist_entry !== undefined) rom.has_gamelist_entry = !!result.has_gamelist_entry;
    refreshArtworkTableRows();
    showToast(`Uploaded ${escapeHtml(field)} for ${escapeHtml(result.rom_name || "ROM")}.`, "success");
    // Refresh the selected rom view
    await selectArtworkRom(window.selectedArtworkRomIndex, "edit");
  } catch (err) {
    showToast(`Upload failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
    btnEl.disabled = false;
    btnEl.innerHTML = `<i class="bi bi-upload"></i>`;
  }
}
function openMarqueeCropper(rowIndex, sourceUrl, sourceLabel) {
  const rom = (window.missingArtworkRoms || [])[rowIndex];
  if (!rom || !sourceUrl) return;
  const existing = document.getElementById("marqueeCropperOverlay");
  if (existing) existing.remove();
  const overlay = document.createElement("div");
  overlay.className = "cropper-overlay";
  overlay.id = "marqueeCropperOverlay";
  overlay.innerHTML = `
    <div class="cropper-panel">
      <div class="d-flex justify-content-between align-items-center gap-2 mb-2">
        <div>
          <div class="fw-semibold">Crop Marquee</div>
          <div class="text-muted small">${escapeHtml(rom.title || rom.name || "Selected ROM")} · ${escapeHtml(sourceLabel)}</div>
        </div>
        <button class="btn btn-sm btn-outline-secondary" type="button" id="marqueeCropClose"><i class="bi bi-x-lg"></i></button>
      </div>
      <canvas id="marqueeCropCanvas" class="marquee-crop-canvas" width="860" height="520"></canvas>
      <div class="row g-2 align-items-center mt-2">
        <div class="col-12 col-md-8">
          <label class="form-label text-muted small mb-1" for="marqueeCropSize">Crop width</label>
          <input id="marqueeCropSize" class="form-range" type="range" min="160" max="860" value="720">
        </div>
        <div class="col-12 col-md-4">
          <img id="marqueeCropPreview" class="cropper-preview" alt="">
        </div>
      </div>
      <div class="d-flex justify-content-end gap-2 mt-3">
        <button class="btn btn-sm btn-outline-secondary" type="button" id="marqueeCropCancel">Cancel</button>
        <button class="btn btn-sm btn-primary" type="button" id="marqueeCropSave"><i class="bi bi-save me-1"></i>Save Marquee</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const canvas = overlay.querySelector("#marqueeCropCanvas");
  const preview = overlay.querySelector("#marqueeCropPreview");
  const sizeInput = overlay.querySelector("#marqueeCropSize");
  const saveBtn = overlay.querySelector("#marqueeCropSave");
  const close = () => overlay.remove();
  overlay.querySelector("#marqueeCropClose").addEventListener("click", close);
  overlay.querySelector("#marqueeCropCancel").addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });

  const ctx = canvas.getContext("2d");
  const image = new Image();
  image.crossOrigin = "anonymous";
  const state = { scale: 1, offsetX: 0, offsetY: 0, crop: { x: 70, y: 180, w: 720, h: 180 }, drag: null };

  function fitImage() {
    const scale = Math.min(canvas.width / image.naturalWidth, canvas.height / image.naturalHeight);
    state.scale = scale;
    state.offsetX = (canvas.width - image.naturalWidth * scale) / 2;
    state.offsetY = (canvas.height - image.naturalHeight * scale) / 2;
    const maxW = Math.min(canvas.width - 40, image.naturalWidth * scale);
    const cropW = Math.max(160, Math.min(maxW, Number(sizeInput.value || 720)));
    state.crop.w = cropW;
    state.crop.h = cropW / 4;
    state.crop.x = (canvas.width - state.crop.w) / 2;
    state.crop.y = (canvas.height - state.crop.h) / 2;
    sizeInput.max = String(Math.floor(Math.min(canvas.width - 20, image.naturalWidth * scale)));
    sizeInput.value = String(Math.floor(state.crop.w));
  }
  function clampCrop() {
    state.crop.w = Math.max(160, Math.min(Number(sizeInput.max || 860), state.crop.w));
    state.crop.h = state.crop.w / 4;
    state.crop.x = Math.max(0, Math.min(canvas.width - state.crop.w, state.crop.x));
    state.crop.y = Math.max(0, Math.min(canvas.height - state.crop.h, state.crop.y));
  }
  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#050814";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, state.offsetX, state.offsetY, image.naturalWidth * state.scale, image.naturalHeight * state.scale);
    ctx.fillStyle = "rgba(0,0,0,0.54)";
    ctx.fillRect(0, 0, canvas.width, state.crop.y);
    ctx.fillRect(0, state.crop.y + state.crop.h, canvas.width, canvas.height - state.crop.y - state.crop.h);
    ctx.fillRect(0, state.crop.y, state.crop.x, state.crop.h);
    ctx.fillRect(state.crop.x + state.crop.w, state.crop.y, canvas.width - state.crop.x - state.crop.w, state.crop.h);
    ctx.strokeStyle = "#00c2ff";
    ctx.lineWidth = 3;
    ctx.strokeRect(state.crop.x, state.crop.y, state.crop.w, state.crop.h);
    updatePreview();
  }
  function cropToSourceRect() {
    return {
      sx: Math.max(0, (state.crop.x - state.offsetX) / state.scale),
      sy: Math.max(0, (state.crop.y - state.offsetY) / state.scale),
      sw: Math.min(image.naturalWidth, state.crop.w / state.scale),
      sh: Math.min(image.naturalHeight, state.crop.h / state.scale),
    };
  }
  function updatePreview() {
    const output = document.createElement("canvas");
    output.width = 640;
    output.height = 160;
    const outCtx = output.getContext("2d");
    const rect = cropToSourceRect();
    outCtx.fillStyle = "#000";
    outCtx.fillRect(0, 0, output.width, output.height);
    outCtx.drawImage(image, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, output.width, output.height);
    preview.src = output.toDataURL("image/png");
  }
  function pointerPosition(event) {
    const bounds = canvas.getBoundingClientRect();
    return {
      x: (event.clientX - bounds.left) * (canvas.width / bounds.width),
      y: (event.clientY - bounds.top) * (canvas.height / bounds.height),
    };
  }
  canvas.addEventListener("pointerdown", (event) => {
    const pos = pointerPosition(event);
    if (pos.x < state.crop.x || pos.x > state.crop.x + state.crop.w || pos.y < state.crop.y || pos.y > state.crop.y + state.crop.h) return;
    state.drag = { startX: pos.x, startY: pos.y, cropX: state.crop.x, cropY: state.crop.y };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!state.drag) return;
    const pos = pointerPosition(event);
    state.crop.x = state.drag.cropX + pos.x - state.drag.startX;
    state.crop.y = state.drag.cropY + pos.y - state.drag.startY;
    clampCrop();
    draw();
  });
  canvas.addEventListener("pointerup", () => { state.drag = null; });
  sizeInput.addEventListener("input", () => {
    const centerX = state.crop.x + state.crop.w / 2;
    const centerY = state.crop.y + state.crop.h / 2;
    state.crop.w = Number(sizeInput.value || 720);
    state.crop.h = state.crop.w / 4;
    state.crop.x = centerX - state.crop.w / 2;
    state.crop.y = centerY - state.crop.h / 2;
    clampCrop();
    draw();
  });
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
    setLoading(true, "Saving marquee crop...");
    try {
      const output = document.createElement("canvas");
      output.width = 1280;
      output.height = 320;
      const outCtx = output.getContext("2d");
      const rect = cropToSourceRect();
      outCtx.fillStyle = "#000";
      outCtx.fillRect(0, 0, output.width, output.height);
      outCtx.drawImage(image, rect.sx, rect.sy, rect.sw, rect.sh, 0, 0, output.width, output.height);
      const blob = await new Promise((resolve) => output.toBlob(resolve, "image/png"));
      if (!blob) throw new Error("Could not render crop");
      const formData = new FormData();
      formData.append("file", new File([blob], "marquee.png", { type: "image/png" }));
      formData.append("field", "marquee");
      formData.append("system", rom.system || "");
      formData.append("rom_id", rom.unique_id || "");
      formData.append("rom_path", rom.rom_path || "");
      const res = await fetch(`${API_BASE}/admin/artwork/upload`, { method: "POST", credentials: "include", body: formData });
      if (res.status === 401) {
        window.location.reload();
        throw new Error("Authentication required");
      }
      if (!res.ok) {
        let msg = `Upload failed: ${res.status}`;
        try { const d = await res.json(); if (d.error) msg = d.error; } catch(_) {}
        throw new Error(msg);
      }
      const result = await res.json();
      rom.existing = result.existing || rom.existing || {};
      rom.gamelist = result.gamelist || rom.gamelist || {};
      rom.missing = result.missing || rom.missing || [];
      rom.has_gamelist_entry = result.has_gamelist_entry !== undefined ? !!result.has_gamelist_entry : rom.has_gamelist_entry;
      refreshArtworkTableRows();
      showToast(`Saved marquee for ${escapeHtml(result.rom_name || "ROM")}.`, "success");
      close();
      await selectArtworkRom(rowIndex, "edit");
    } catch (err) {
      showToast(`Marquee crop failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      saveBtn.disabled = false;
      saveBtn.innerHTML = `<i class="bi bi-save me-1"></i>Save Marquee`;
    } finally {
      setLoading(false);
    }
  });
  image.onload = () => {
    fitImage();
    draw();
  };
  image.onerror = () => {
    showToast("Could not load image for cropping.", "danger");
    close();
  };
  image.src = sourceUrl;
}
async function selectArtworkRom(index, activeTab = "matches") {
  const roms = window.missingArtworkRoms || [];
  const rom = roms[index];
  if (!rom) return;
  document.querySelectorAll("#artworkRows tr").forEach((row) => row.classList.remove("artwork-selected-row"));
  const selectedRow = document.getElementById(`artwork-row-${index}`);
  if (selectedRow) selectedRow.classList.add("artwork-selected-row");
  window.selectedArtworkRomIndex = index;
  // Update URL for bookmarking
  setArtworkSelectedRomHash(index);
  const selected = document.getElementById("selectedArtworkRom");
  // Render tabbed panel with Matches (default) and Edit tabs
  selected.innerHTML = `
    <ul class="nav nav-tabs meta-panel-tabs mb-2" id="metaPanelTabs" role="tablist">
      <li class="nav-item" role="presentation">
        <button class="nav-link active" id="matches-tab" data-bs-toggle="tab" data-bs-target="#matches-panel" type="button" role="tab" aria-controls="matches-panel" aria-selected="true">Matches</button>
      </li>
      <li class="nav-item" role="presentation">
        <button class="nav-link" id="edit-tab" data-bs-toggle="tab" data-bs-target="#edit-panel" type="button" role="tab" aria-controls="edit-panel" aria-selected="false">Edit</button>
      </li>
    </ul>
    <div class="tab-content meta-panel-content">
      <div class="tab-pane fade show active compact-edit" id="matches-panel" role="tabpanel" aria-labelledby="matches-tab">
        ${artworkGamelistDetailsHtml(rom)}
        <div class="border-top mt-3 pt-3">
          ${artworkExternalLinksHtml(rom)}
        </div>
        <div class="form-check">
          <input class="form-check-input" type="checkbox" id="artworkMatchOverrideExisting">
          <label class="form-check-label small" for="artworkMatchOverrideExisting">Override existing data (re-imports all gamelist metadata)</label>
        </div>
        <div class="border-top mt-3 pt-3">
          <div class="fw-semibold mb-2">LaunchBox Matches</div>
          <div id="launchboxMatches" class="mt-2"></div>
        </div>
        <div class="border-top mt-3 pt-3">
          <div class="mb-2">
            <div class="fw-semibold">TheGamesDB Matches</div>
            <div class="text-muted small" id="theGamesDBImageQuery"></div>
          </div>
          <div id="theGamesDBImageMatches" class="mt-2"></div>
        </div>
      </div>
      <div class="tab-pane fade compact-edit" id="edit-panel" role="tabpanel" aria-labelledby="edit-tab">
        <div class="mb-2">
          <div><span class="fw-semibold">${escapeHtml(rom.title || rom.name || "")}</span> <span class="text-muted small">${escapeHtml(rom.system || "")}</span></div>
          ${rom.has_gamelist_entry ? `<button class="btn btn-sm btn-outline-danger mt-2" type="button" onclick="removeArtworkGamelistEntry(${index})"><i class="bi bi-trash me-1"></i>Remove from gamelist</button>` : ""}
        </div>
        <div class="mb-2">
          <div class="compact-edit-section-title">Metadata</div>
          ${artworkGamelistEditFormHtml(rom)}
        </div>
        <div class="mb-2">
          <div class="compact-edit-section-title">Artwork Uploads</div>
          ${artworkEditableImageFields(rom)}
        </div>
        <div class="mb-2">
          <div class="compact-edit-section-title">Marquee Cropper</div>
          ${artworkMarqueeCropperHtml(rom)}
        </div>
      </div>
    </div>
  `;
  // Initialize Bootstrap tabs
  const tabEls = document.querySelectorAll('#metaPanelTabs .nav-link');
  tabEls.forEach(el => {
    el.addEventListener('shown.bs.tab', () => { /* no-op */ });
  });
  bindArtworkEditButtons(rom, index);
  // Switch to matches tab by default
  const tabToShow = activeTab === "edit"
    ? document.getElementById("edit-tab")
    : document.getElementById("matches-tab");
  if (tabToShow) tabToShow.click();
  // Search LaunchBox for matches - query fresh from DOM since it was just created
  const matchesEl = document.getElementById("launchboxMatches");
  if (!matchesEl) return;
  setLoading(true, "Searching LaunchBox matches...");
  try {
    const data = await api(`/admin/artwork/launchbox/search?system=${encodeURIComponent(rom.system || "")}&rom_id=${encodeURIComponent(rom.unique_id || "")}&rom_path=${encodeURIComponent(rom.rom_path || "")}&q=${encodeURIComponent(artworkRomSearchTitle(rom))}`);
    const matches = data.matches || [];
    const platformNote = data.launchbox_platform
      ? `<div class="text-muted small mb-2">Filtered by LaunchBox platform: ${escapeHtml(data.launchbox_platform)}</div>`
      : `<div class="text-muted small mb-2">No LaunchBox platform mapping found for this system; showing title matches.</div>`;
    matchesEl.innerHTML = platformNote + (matches.length ? `
      <div class="list-group">
        ${matches.map((match) => `
          <button type="button" class="list-group-item list-group-item-action launchbox-match-btn" data-launchbox-game-key="${escapeHtml(String(match.game_key || ""))}">
            <div class="d-flex gap-3 align-items-center">
              ${match.thumbnail_url ? `<img src="${match.thumbnail_url}" alt="" style="width: 56px; height: 56px; object-fit: cover; background:#111;">` : `<div style="width:56px;height:56px;background:#111;"></div>`}
              <div>
                <div class="fw-semibold">${escapeHtml(match.name || "")}</div>
                <div class="text-muted small">${escapeHtml(match.platform || "unknown platform")}</div>
              </div>
            </div>
          </button>
        `).join("")}
      </div>
    ` : `<div class="text-muted">No LaunchBox matches found.</div>`);
    matchesEl.querySelectorAll(".launchbox-match-btn").forEach((button) => {
      button.addEventListener("click", () => {
        applyLaunchboxArtwork(
          index,
          rom.system || "",
          rom.unique_id || "",
          rom.rom_path || "",
          button.getAttribute("data-launchbox-game-key") || ""
        );
      });
    });
  } catch (err) {
    showToast(`LaunchBox search failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
    matchesEl.innerHTML = `<div class="text-muted">LaunchBox matches could not be loaded.</div>`;
  } finally {
    setLoading(false);
  }
  await searchTheGamesDBImagesForRom(index, false);
}
async function searchTheGamesDBImagesForRom(index, forceRefresh = false) {
  const rom = (window.missingArtworkRoms || [])[index];
  const matchesEl = document.getElementById("theGamesDBImageMatches");
  if (!rom || !matchesEl) return;
  if (matchesEl.getAttribute("data-loaded") === "1" && !forceRefresh) return;
  const queryEl = document.getElementById("theGamesDBImageQuery");
  matchesEl.innerHTML = "";
  setLoading(true, "Searching TheGamesDB matches...");
  try {
    const data = await api(`/admin/artwork/thegamesdb/search?system=${encodeURIComponent(rom.system || "")}&rom_id=${encodeURIComponent(rom.unique_id || "")}&rom_path=${encodeURIComponent(rom.rom_path || "")}&q=${encodeURIComponent(artworkRomSearchTitle(rom))}`);
    const matches = (data.matches || []).slice(0, 5);
    if (queryEl) queryEl.textContent = data.query || "";
    matchesEl.setAttribute("data-loaded", "1");
    matchesEl.innerHTML = matches.length ? `
      <div class="text-muted small mb-2">Imports artwork and fills empty metadata from the selected TheGamesDB page. Marquee uses clear logo first, then banner.</div>
      <div class="list-group">
        ${matches.map((match) => `
          <button type="button" class="list-group-item list-group-item-action thegamesdb-match-btn" data-thegamesdb-game-id="${escapeHtml(String(match.game_id || ""))}">
            <div class="d-flex gap-3 align-items-center">
              ${match.thumbnail_url ? `<img class="match-thumb" src="${escapeHtml(match.thumbnail_url)}" alt="">` : `<div class="match-thumb-placeholder"></div>`}
              <div>
                <div class="fw-semibold">${escapeHtml(match.name || match.title || "")}</div>
                <div class="text-muted small">${escapeHtml(match.platform || "unknown platform")}</div>
              </div>
            </div>
          </button>
        `).join("")}
      </div>
    ` : `<div class="text-muted">No TheGamesDB matches found.</div>`;
    matchesEl.querySelectorAll(".thegamesdb-match-btn").forEach((button) => {
      button.addEventListener("click", () => {
        applyTheGamesDBArtwork(index, button.getAttribute("data-thegamesdb-game-id") || "", button);
      });
    });
  } catch (err) {
    showToast(`TheGamesDB search failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
    matchesEl.innerHTML = `<div class="text-muted">TheGamesDB matches could not be loaded.</div>`;
  } finally {
    setLoading(false);
  }
}
async function applyTheGamesDBArtwork(rowIndex, gameId, button) {
  const rom = (window.missingArtworkRoms || [])[rowIndex];
  if (!rom || !gameId) return;
  const overrideCheckbox = document.getElementById("artworkMatchOverrideExisting") || document.getElementById("launchboxOverrideExisting");
  const overrideExisting = overrideCheckbox ? overrideCheckbox.checked : false;
  const originalHtml = button ? button.innerHTML : "";
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
  }
  setLoading(true, "Downloading TheGamesDB artwork and metadata...");
  try {
    const result = await apiPost("/admin/artwork/thegamesdb/apply", {
      system: rom.system || "",
      rom_id: rom.unique_id || "",
      rom_path: rom.rom_path || "",
      game_id: gameId,
      override_existing: overrideExisting,
      import_metadata: true,
    });
    rom.existing = result.existing || rom.existing || {};
    rom.gamelist = result.gamelist || rom.gamelist || {};
    rom.missing = result.missing || rom.missing || [];
    rom.has_gamelist_entry = result.has_gamelist_entry !== undefined ? !!result.has_gamelist_entry : rom.has_gamelist_entry;
    refreshArtworkTableRows();
    const updated = result.updated || [];
    const artCount = updated.filter((item) => item.path).length;
    const metaCount = Number(result.metadata_imported || 0);
    showToast(`Imported ${artCount} artwork field${artCount === 1 ? "" : "s"}${metaCount ? ` and ${metaCount} metadata field${metaCount === 1 ? "" : "s"}` : ""} for ${escapeHtml(result.rom_name || "ROM")}.`, "success");
    const row = document.getElementById(`artwork-row-${rowIndex}`);
    if (row) {
      if (!rom.missing.length) {
        row.remove();
      } else {
        const missingCell = row.children[2];
        if (missingCell) missingCell.innerHTML = rom.missing.map((item) => `<span class="badge text-bg-danger me-1">${escapeHtml(item)}</span>`).join("");
      }
    }
    await selectArtworkRom(rowIndex, "matches");
  } catch (err) {
    showToast(`TheGamesDB import failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
    if (button) {
      button.disabled = false;
      button.innerHTML = originalHtml;
    }
  }
}
async function searchMobyGamesImagesForRom(index, forceRefresh = false) {
  const rom = (window.missingArtworkRoms || [])[index];
  const matchesEl = document.getElementById("mobyGamesImageMatches");
  if (!rom || !matchesEl) return;
  if (matchesEl.getAttribute("data-loaded") === "1" && !forceRefresh) return;
  const queryEl = document.getElementById("mobyGamesImageQuery");
  matchesEl.innerHTML = "";
  setLoading(true, "Searching MobyGames matches...");
  try {
    const data = await api(`/admin/artwork/mobygames/search?system=${encodeURIComponent(rom.system || "")}&rom_id=${encodeURIComponent(rom.unique_id || "")}&rom_path=${encodeURIComponent(rom.rom_path || "")}&q=${encodeURIComponent(rom.search_title || rom.title || rom.name || "")}`);
    const matches = (data.matches || []).slice(0, 5);
    if (queryEl) {
      const platform = data.mobygames_platform ? ` · ${data.mobygames_platform}` : "";
      queryEl.textContent = `${data.query || ""}${platform}`;
    }
    matchesEl.setAttribute("data-loaded", "1");
    if (data.configured === false || data.message) {
      matchesEl.innerHTML = `<div class="text-muted">${escapeHtml(data.message || "MobyGames scraper is not available right now.")}</div>`;
      return;
    }
    matchesEl.innerHTML = matches.length ? `
      <div class="text-muted small mb-2">Imports MobyGames cover scans, screenshots, and available metadata from the selected match.</div>
      <div class="list-group">
        ${matches.map((match) => `
          <button type="button" class="list-group-item list-group-item-action mobygames-match-btn" data-mobygames-game-id="${escapeHtml(String(match.game_id || ""))}">
            <div class="d-flex gap-3 align-items-center">
              ${match.thumbnail_url ? `<img class="match-thumb" src="${escapeHtml(match.thumbnail_url)}" alt="">` : `<div class="match-thumb-placeholder"></div>`}
              <div>
                <div class="fw-semibold">${escapeHtml(match.name || match.title || "")}</div>
                <div class="text-muted small">${escapeHtml(match.platform || "unknown platform")}</div>
              </div>
            </div>
          </button>
        `).join("")}
      </div>
    ` : `<div class="text-muted">No MobyGames matches found.</div>`;
    matchesEl.querySelectorAll(".mobygames-match-btn").forEach((button) => {
      button.addEventListener("click", () => {
        applyMobyGamesArtwork(index, button.getAttribute("data-mobygames-game-id") || "", button);
      });
    });
  } catch (err) {
    showToast(`MobyGames search failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
    matchesEl.innerHTML = `<div class="text-muted">MobyGames matches could not be loaded.</div>`;
  } finally {
    setLoading(false);
  }
}
async function applyMobyGamesArtwork(rowIndex, gameId, button) {
  const rom = (window.missingArtworkRoms || [])[rowIndex];
  if (!rom || !gameId) return;
  const overrideCheckbox = document.getElementById("artworkMatchOverrideExisting") || document.getElementById("launchboxOverrideExisting");
  const overrideExisting = overrideCheckbox ? overrideCheckbox.checked : false;
  const originalHtml = button ? button.innerHTML : "";
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
  }
  setLoading(true, "Downloading MobyGames artwork and metadata...");
  try {
    const result = await apiPost("/admin/artwork/mobygames/apply", {
      system: rom.system || "",
      rom_id: rom.unique_id || "",
      rom_path: rom.rom_path || "",
      game_id: gameId,
      override_existing: overrideExisting,
      import_metadata: true,
    });
    rom.existing = result.existing || rom.existing || {};
    rom.gamelist = result.gamelist || rom.gamelist || {};
    rom.missing = result.missing || rom.missing || [];
    rom.has_gamelist_entry = result.has_gamelist_entry !== undefined ? !!result.has_gamelist_entry : rom.has_gamelist_entry;
    refreshArtworkTableRows();
    const updated = result.updated || [];
    const artCount = updated.filter((item) => item.path).length;
    const metaCount = Number(result.metadata_imported || 0);
    showToast(`Imported ${artCount} artwork field${artCount === 1 ? "" : "s"}${metaCount ? ` and ${metaCount} metadata field${metaCount === 1 ? "" : "s"}` : ""} for ${escapeHtml(result.rom_name || "ROM")}.`, "success");
    await selectArtworkRom(rowIndex, "matches");
  } catch (err) {
    showToast(`MobyGames import failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
    if (button) {
      button.disabled = false;
      button.innerHTML = originalHtml;
    }
  }
}
async function applyLaunchboxArtwork(rowIndex, system, romId, romPath, gameKey) {
  // Check override existing checkbox
  const overrideCheckbox = document.getElementById("artworkMatchOverrideExisting") || document.getElementById("launchboxOverrideExisting");
  const overrideExisting = overrideCheckbox ? overrideCheckbox.checked : false;
  setLoading(true, "Downloading artwork and metadata...");
  try {
    const result = await apiPost("/admin/artwork/launchbox/apply", {
      system,
      rom_id: romId,
      rom_path: romPath,
      game_key: gameKey,
      override_existing: overrideExisting,
      import_metadata: true,
    });
    const updated = result.updated || [];
    const artCount = updated.filter((item) => item.path).length;
    const metaCount = Number(result.metadata_imported || 0);
    let successMsg = `Updated ${artCount} artwork field${artCount === 1 ? "" : "s"} for ${escapeHtml(result.rom_name || "ROM")}.`;
    if (metaCount) {
      successMsg += ` Also imported ${metaCount} metadata field${metaCount === 1 ? "" : "s"}.`;
    }
    showToast(successMsg, "success");
    const rom = (window.missingArtworkRoms || [])[rowIndex];
    if (rom) {
      const updatedFields = new Set(updated.map((item) => item.field));
      rom.existing = result.existing || rom.existing || {};
      rom.gamelist = result.gamelist || rom.gamelist || {};
      rom.missing = (rom.missing || []).filter((field) => !updatedFields.has(field));
      if (Array.isArray(result.missing)) rom.missing = result.missing;
      rom.has_gamelist_entry = result.has_gamelist_entry !== undefined ? !!result.has_gamelist_entry : rom.has_gamelist_entry;
      refreshArtworkTableRows();
      const row = document.getElementById(`artwork-row-${rowIndex}`);
      if (row) {
        if (!rom.missing.length) {
          row.remove();
        } else {
          const missingCell = row.children[2];
          if (missingCell) {
            missingCell.innerHTML = rom.missing.map((field) => `<span class="badge text-bg-danger me-1">${escapeHtml(field)}</span>`).join("");
          }
        }
      }
    }
  } catch (err) {
    showToast(`Artwork update failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function saveSelectedArtworkGamelist() {
  const index = Number(window.selectedArtworkRomIndex);
  const rom = (window.missingArtworkRoms || [])[index];
  if (!rom) return;
  const fields = {};
  document.querySelectorAll("#gamelistEditForm .gamelist-edit-field").forEach((node) => {
    const field = node.getAttribute("data-gamelist-field");
    if (field) fields[field] = node.value || "";
  });
  setLoading(true, "Saving gamelist data...");
  try {
    const result = await apiPost("/admin/artwork/gamelist/update", {
      system: rom.system,
      rom_path: rom.rom_path,
      fields,
    });
    rom.has_gamelist_entry = true;
    rom.gamelist = result.gamelist || {};
    rom.existing = result.existing || rom.existing || {};
    rom.missing = result.missing || rom.missing || [];
    rom.title = result.title || rom.title || rom.name;
    rom.name = rom.title || rom.name;
    rom.search_title = result.search_title || rom.title || rom.name || rom.search_title || "";
    await selectArtworkRom(index, "edit");
    const rows = document.getElementById("artworkRows");
    if (rows) rows.innerHTML = artworkMissingRowsHtml(window.missingArtworkRoms || []);
    showToast(`Saved gamelist data for ${escapeHtml(rom.title || rom.name || "ROM")}.`, "success");
  } catch (err) {
    showToast(`Gamelist update failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function removeArtworkGamelistEntry(index) {
  const rom = (window.missingArtworkRoms || [])[index];
  if (!rom) return;
  const label = rom.title || rom.name || rom.rom_path || "this ROM";
  if (!window.confirm(`Remove "${label}" from gamelist.xml? The ROM file will not be deleted.`)) return;
  setLoading(true, "Removing gamelist entry...");
  try {
    await apiPost("/admin/artwork/gamelist/remove", { system: rom.system, rom_path: rom.rom_path });
    const row = document.getElementById(`artwork-row-${index}`);
    if (row) row.remove();
    window.missingArtworkRoms[index] = null;
    showToast(`Removed ${escapeHtml(label)} from gamelist.xml.`, "success");
  } catch (err) {
    showToast(`Remove failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function removeMissingGamelistEntriesForCurrentFilters() {
  const confirmed = window.confirm(
    "Remove all gamelist.xml entries matching the current filters where the ROM file is missing on disk? ROM files are not deleted."
  );
  if (!confirmed) return;
  setLoading(true, "Removing missing-ROM gamelist entries...");
  try {
    const result = await apiPost("/admin/artwork/gamelist/remove-missing", {
      confirm: "DELETE_MISSING_GAMELIST_ENTRIES",
      include_filesystem: artworkIncludeFilesystem,
      fields: artworkSelectedFields || ["any"],
      systems: artworkSelectedSystems || [],
      q: artworkFilterQuery || "",
    });
    showToast(`Removed ${Number(result.removed_count || 0)} missing-ROM gamelist entr${Number(result.removed_count || 0) === 1 ? "y" : "ies"}.`, "success");
    artworkCurrentOffset = 0;
    await refreshArtworkResults(true);
    if (result.failed_count) {
      showToast(`Skipped ${Number(result.failed_count || 0)} entr${Number(result.failed_count || 0) === 1 ? "y" : "ies"} because their gamelist.xml could not be written.`, "warning", 8000);
    }
  } catch (err) {
    showToast(`Bulk remove failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
let localPeerAssetContext = { peerId: "", peerName: "", assetType: "summary", systems: [], availableSystems: [], items: [], query: "", limit: 50, offset: 0, total: 0 };

function localPeerStatusBadge(peer) {
  if (!peer.paired) return '<span class="badge text-bg-warning">Discovered</span>';
  const health = peer.health || {};
  if (health.status === "pass") return '<span class="badge text-bg-success">Paired · Online</span>';
  if (health.status === "fail") return '<span class="badge text-bg-danger">Paired · Offline</span>';
  return '<span class="badge text-bg-info">Paired</span>';
}

function renderLocalPeerRows(peers) {
  if (!peers.length) return '<div class="themed-empty">No nearby Drones discovered yet.</div>';
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table">
    <thead><tr><th>Drone</th><th>Drone ID</th><th>Status</th><th>Error</th><th>Address</th><th>Last Seen</th><th>Certificate</th><th></th></tr></thead>
    <tbody>${peers.map(peer => {
      const rawPeerId = String(peer.drone_id || "");
      const peerId = escapeHtml(rawPeerId);
      const peerToken = encodeURIComponent(rawPeerId).replace(/'/g, "%27");
      // A peer advertising a non-HTTPS URL (or no certificate) can't do the
      // certificate-verified mTLS transfer; flag it instead of offering Pair.
      const url = String(peer.reachable_url || "");
      const insecure = !peer.paired && url !== "" && !/^https:/i.test(url);
      let actionCell;
      if (peer.paired) {
        actionCell = `<button class="btn btn-sm btn-primary me-1" onclick="browseLocalPeer(decodeURIComponent('${peerToken}'))">Request Assets</button><button class="btn btn-sm btn-outline-danger" onclick="forgetLocalPeer(decodeURIComponent('${peerToken}'))">Forget</button>`;
      } else if (insecure) {
        actionCell = `<button class="btn btn-sm btn-outline-secondary" disabled title="This Drone is advertising ${escapeHtml(url)} (not HTTPS), so it can't be paired for secure transfers. Update/repair the Drone on that machine.">Not secure</button>`;
      } else {
        actionCell = `<button class="btn btn-sm btn-outline-primary" onclick="pairLocalPeer(decodeURIComponent('${peerToken}'))">Pair</button>`;
      }
      return `<tr>
        <td><strong>${escapeHtml(peer.name || peer.hostname || peerId)}</strong>${insecure ? '<span class="badge text-bg-danger ms-2" title="Not running HTTPS — cannot pair">Not secure</span>' : ""}</td>
        <td class="small mono">${peerId}</td>
        <td>${localPeerStatusBadge(peer)}</td>
        <td class="small text-danger">${escapeHtml(peer.health?.failure_reason || "")}</td>
        <td class="small mono">${escapeHtml(peer.reachable_url || peer.source_ip || "n/a")}</td>
        <td class="small text-nowrap">${escapeHtml(formatCompactLocalDate(peer.last_seen) || "n/a")}</td>
        <td class="small mono">${escapeHtml(String(peer.certificate_fingerprint || "").slice(0, 16) || "pending")}</td>
        <td class="text-nowrap">${actionCell}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

function localAssetPath(item) {
  return item.relative_path || item.rom_path || item.file_path || item.path || item.name || "";
}

function localAssetDisplayName(item) {
  return item.name || item.rom_name || item.save_name || item.game_name || item.title || localAssetPath(item) || "Peer record";
}

function localAssetDetail(item) {
  const date = item.played_at || item.started_at || item.modified_at;
  return date ? formatCompactLocalDate(date) : (item.duration || item.emulator || "");
}

const LOCAL_TRANSFERABLE_TYPES = new Set(["roms", "bios", "artwork", "saves"]);

function localAssetSystemEntries() {
  // Distinct systems present in the currently loaded page (for per-system bulk copy).
  const systems = [];
  const seen = new Set();
  (localPeerAssetContext.items || []).forEach(item => {
    const system = String(item.system || item.root_name || "").trim();
    if (system && !seen.has(system)) { seen.add(system); systems.push(system); }
  });
  return systems.sort((a, b) => a.localeCompare(b));
}

function renderLocalAssetRows(payload) {
  localPeerAssetContext.items = payload.items || [];
  if (!localPeerAssetContext.items.length) return '<div class="themed-empty">No assets match this view.</div>';
  const isRoms = localPeerAssetContext.assetType === "roms";
  const transferable = LOCAL_TRANSFERABLE_TYPES.has(localPeerAssetContext.assetType);
  const downloadLabel = isRoms ? "Download" : "Copy Here";
  // When browsing ROMs across multiple systems, expose a quick per-system "download all".
  let systemBar = "";
  if (isRoms) {
    const systems = localAssetSystemEntries();
    if (systems.length) {
      systemBar = `<div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <span class="small text-muted">Download all ROMs for a system:</span>
        ${systems.map(system => `<button class="btn btn-sm btn-outline-success" type="button" onclick="copyAllRomsForSystem('${encodeURIComponent(system).replace(/'/g, "%27")}')"><i class="bi bi-cloud-arrow-down me-1"></i>${escapeHtml(system)}</button>`).join("")}
      </div>`;
    }
  }
  return systemBar + `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table">
    <thead><tr><th>Name</th><th>Path</th><th>System / Source</th><th>Size</th><th>Details</th><th></th></tr></thead>
    <tbody>${localPeerAssetContext.items.map((item, index) => {
      const exists = isRoms && item.exists_locally === true;
      const statusBadge = isRoms
        ? (exists
            ? '<span class="badge text-bg-success ms-2" title="This ROM is already on this machine (matched by thumbprint)">On this machine</span>'
            : '<span class="badge text-bg-info ms-2">New</span>')
        : "";
      // An existing ROM is not re-downloaded; the button still copies artwork
      // (when Include artwork is checked) and links it in the gamelist.
      const romBtnLabel = exists ? "Get Artwork" : downloadLabel;
      return `<tr>
      <td><strong>${escapeHtml(localAssetDisplayName(item))}</strong>${statusBadge}</td>
      <td class="small mono">${escapeHtml(localAssetPath(item))}</td>
      <td>${escapeHtml(item.system || item.root_name || localPeerAssetContext.systems.join(", "))}</td>
      <td>${formatBytes(item.byte_count || item.file_size || item.size)}</td>
      <td class="small">${escapeHtml(localAssetDetail(item) || String(item.rom_fingerprint || item.bios_md5 || item.saves_fingerprint || item.fingerprint || item.md5 || "").slice(0, 16))}</td>
      <td>${transferable
        ? `<button class="btn btn-sm ${exists ? "btn-outline-primary" : "btn-primary"}" onclick="copyLocalPeerAsset(${index})"><i class="bi bi-cloud-arrow-down me-1"></i>${isRoms ? romBtnLabel : downloadLabel}</button>`
        : `<details><summary class="btn btn-sm btn-outline-primary">View Details</summary><pre class="mono small mt-2 mb-0 text-wrap">${escapeHtml(JSON.stringify(item, null, 2))}</pre></details>`}</td>
    </tr>`;
    }).join("")}</tbody></table></div>`;
}

function renderLocalAssetsPagination() {
  const node = document.getElementById("localAssetsPagination");
  if (!node) return;
  const limit = Math.max(1, Number(localPeerAssetContext.limit) || 50);
  const total = Math.max(0, Number(localPeerAssetContext.total) || 0);
  const offset = Math.max(0, Number(localPeerAssetContext.offset) || 0);
  if (!total) { node.innerHTML = ""; return; }
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const page = Math.min(totalPages, Math.floor(offset / limit) + 1);
  const start = Math.max(1, page - 3);
  const end = Math.min(totalPages, page + 3);
  const pages = [];
  for (let item = start; item <= end; item += 1) pages.push(item);
  const showingFrom = total ? offset + 1 : 0;
  const showingTo = Math.min(total, offset + limit);
  node.innerHTML = `<div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
      <div class="text-muted small">Showing ${showingFrom}-${showingTo} of ${total}</div>
      <div class="btn-group flex-wrap" role="group" aria-label="Asset pages">
        <button class="btn btn-sm btn-outline-primary" type="button" ${page <= 1 ? "disabled" : ""} onclick="setLocalAssetPage(${page - 1})">Previous</button>
        ${start > 1 ? `<button class="btn btn-sm btn-outline-primary" type="button" onclick="setLocalAssetPage(1)">1</button>` : ""}
        ${pages.map(item => `<button class="btn btn-sm ${item === page ? "btn-primary" : "btn-outline-primary"}" type="button" onclick="setLocalAssetPage(${item})">${item}</button>`).join("")}
        ${end < totalPages ? `<button class="btn btn-sm btn-outline-primary" type="button" onclick="setLocalAssetPage(${totalPages})">${totalPages}</button>` : ""}
        <button class="btn btn-sm btn-outline-primary" type="button" ${page >= totalPages ? "disabled" : ""} onclick="setLocalAssetPage(${page + 1})">Next</button>
      </div>
    </div>`;
}

async function renderIntegrationPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  clearSystemTheme();
  titleNode.textContent = "Integration";
  subtitleNode.textContent = "Manage this Drone through Overmind or directly on the local network";
  setLoading(true, "Loading integration...");
  try {
    const modeStatus = await api("/admin/network-mode");
    const overmindActive = !!modeStatus.overmind_enabled;
    const localActive = !!modeStatus.local_network_enabled;
    content.innerHTML = `
      <div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button></div>
      <div class="card log-card mb-3">
        <div class="card-header">Integration Enablement</div>
        <div class="card-body">
          <div class="small text-muted mb-3">Enable either integration independently, or run both at the same time.</div>
          <div class="row g-3">
            <div class="col-12 col-lg-6"><div class="p-3 rounded border h-100" style="border-color:var(--admin-border)!important">
              <div class="form-check form-switch"><input id="integrationOvermindToggle" class="form-check-input" type="checkbox" ${overmindActive ? "checked" : ""}><label class="form-check-label fw-semibold" for="integrationOvermindToggle">Overmind Integration</label></div>
              <div class="small text-muted mt-2">Heartbeats, actions, fleet reporting, and Overmind-managed syncing.</div>
              <button class="btn btn-sm btn-outline-primary mt-3" type="button" onclick="setHash('#admin/overmind')">Open Overmind Integration</button>
            </div></div>
            <div class="col-12 col-lg-6"><div class="p-3 rounded border h-100" style="border-color:var(--admin-border)!important">
              <div class="form-check form-switch"><input id="integrationLocalToggle" class="form-check-input" type="checkbox" ${localActive ? "checked" : ""}><label class="form-check-label fw-semibold" for="integrationLocalToggle">Local Network</label></div>
              <div class="small text-muted mt-2">Discover, pair, browse, and sync directly with nearby Drones.</div>
              <button class="btn btn-sm btn-outline-primary mt-3" type="button" onclick="setHash('#admin/local-network')">Open Local Network</button>
            </div></div>
          </div>
        </div>
      </div>
      <div id="integrationOvermindConfiguration"></div>`;

    document.getElementById("integrationOvermindToggle").addEventListener("change", (event) => setIntegrationEnabled("overmind", event.target.checked));
    document.getElementById("integrationLocalToggle").addEventListener("change", (event) => setIntegrationEnabled("local_network", event.target.checked));
    await renderIntegrationOvermindConfiguration(document.getElementById("integrationOvermindConfiguration"));
    window.refreshLocalNetwork = null;
    window.refreshOvermindDownloads = null;
    window.refreshOvermindAssetCache = null;
    stopLocalTransfersAutoRefresh();
  } catch (err) {
    showToast(`Failed to load integration: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = '<div class="themed-empty">Integration status could not be loaded.</div>';
  } finally {
    setLoading(false);
  }
}

async function renderIntegrationOvermindConfiguration(target) {
  target.innerHTML = `<div class="card log-card mb-3"><div class="card-header">Overmind Configuration</div><div class="card-body">
    <div class="row g-2">
      <div class="col-12 col-lg-4"><label class="form-label small">Overmind URL</label><input id="integrationOvermindUrl" class="form-control" type="url"></div>
      <div class="col-12 col-lg-4"><label class="form-label small">Authorization Token</label><input id="integrationOvermindToken" class="form-control" type="password" placeholder="Leave blank to keep current token"></div>
      <div class="col-12 col-lg-4"><label class="form-label small">Drone Name</label><input id="integrationOvermindName" class="form-control"></div>
      <div class="col-12 col-lg-6"><label class="form-label small">Claim Email <span class="text-muted">(optional)</span></label><input id="integrationOvermindEmail" class="form-control" type="email"></div>
      <div class="col-12 col-lg-6"><label class="form-label small">Claim Password <span class="text-muted">(optional)</span></label><input id="integrationOvermindPassword" class="form-control" type="password"></div>
    </div>
    <button id="integrationOvermindSave" class="btn btn-primary mt-3" type="button">Save Overmind Configuration</button>
    <span id="integrationOvermindConfigStatus" class="small text-muted ms-2"></span>
  </div></div>`;
  const payload = await api("/admin/integrations/overmind/status");
  document.getElementById("integrationOvermindUrl").value = payload.overmind_url || "https://www.batocera-swarm.com";
  document.getElementById("integrationOvermindName").value = payload.drone_name || "";
  document.getElementById("integrationOvermindEmail").value = payload.overmind_email || "";
  document.getElementById("integrationOvermindConfigStatus").textContent = `Status: ${(payload.status || {}).integration_state || "not configured"}`;
  document.getElementById("integrationOvermindSave").addEventListener("click", async () => {
    const body = {
      overmind_url: document.getElementById("integrationOvermindUrl").value.trim(),
      drone_name: document.getElementById("integrationOvermindName").value.trim(),
    };
    const token = document.getElementById("integrationOvermindToken").value;
    const email = document.getElementById("integrationOvermindEmail").value.trim();
    const password = document.getElementById("integrationOvermindPassword").value;
    if (token) body.overmind_auth_token = token;
    if (email) body.overmind_email = email;
    if (password) body.overmind_password = password;
    try {
      await apiPost("/admin/integrations/overmind/config", body);
      document.getElementById("integrationOvermindToken").value = "";
      document.getElementById("integrationOvermindPassword").value = "";
      showToast("Overmind configuration saved.", "success");
    } catch (err) {
      showToast(`Failed to save Overmind configuration: ${escapeHtml(err.message || "unknown error")}`, "danger");
    }
  });
}

async function setIntegrationEnabled(integration, enabled) {
  const label = integration === "local_network" ? "Local Network" : "Overmind";
  setLoading(true, `${enabled ? "Enabling" : "Disabling"} ${label}...`);
  try {
    const current = await api("/admin/network-mode");
    await apiPost("/admin/network-mode", {
      overmind_enabled: integration === "overmind" ? enabled : !!current.overmind_enabled,
      local_network_enabled: integration === "local_network" ? enabled : !!current.local_network_enabled,
    });
    showToast(`${label} integration ${enabled ? "enabled" : "disabled"}.`, "success");
    await renderIntegrationPage();
  } catch (err) {
    showToast(`Failed to update ${label}: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}

async function renderLocalNetworkPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  clearSystemTheme();
  titleNode.textContent = "Local Network";
  subtitleNode.textContent = "Pair nearby Drones and manage direct asset transfers";
  window.refreshOvermindDownloads = null;
  window.refreshOvermindAssetCache = null;
  content.innerHTML = `<div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin/integration')">← Back to Integration</button></div><div id="localNetworkPagePanel"></div>`;
  await renderLocalNetworkIntegrationPanel(document.getElementById("localNetworkPagePanel"));
}

async function renderOvermindIntegrationPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  clearSystemTheme();
  titleNode.textContent = "Overmind Integration";
  subtitleNode.textContent = "Monitor Overmind activity, downloads, cache, and actions";
  window.refreshLocalNetwork = null;
  content.innerHTML = `<div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin/integration')">← Back to Integration</button></div><div id="overmindPagePanel"></div>`;
  const panel = document.getElementById("overmindPagePanel");
  await renderOvermindIntegrationPanel(panel);
  const configurationCard = panel.querySelector(".card.log-card");
  if (configurationCard) configurationCard.remove();
}

async function renderLocalNetworkIntegrationPanel(target) {
  target.innerHTML = `
    <div class="card log-card mb-3"><div class="card-header d-flex justify-content-between align-items-center"><span>Pairing</span><button class="btn btn-sm btn-outline-primary" id="localPairCodeRotateBtn">Rotate Code</button></div>
      <div class="card-body" id="localPairingBody"></div></div>
    <div class="card log-card mb-3"><div class="card-header d-flex justify-content-between align-items-center"><span>Nearby Drones</span><div class="d-flex gap-2"><button class="btn btn-sm btn-outline-primary" id="localDiscoverBtn"><i class="bi bi-radar me-1"></i>Discover</button><button class="btn btn-sm btn-outline-secondary" id="localRefreshBtn"><i class="bi bi-arrow-repeat"></i></button></div></div>
      <div class="card-body" id="localPeersBody"><div class="text-muted">Loading peers...</div></div></div>
    <div class="card log-card mb-3" id="localAssetsCard"><div class="card-header"><span id="localAssetsTitle">Request Assets from Connected Drone</span></div>
      <div class="card-body">
        <div class="small text-muted mb-3">Request inventories from a paired Drone, then download what you need. ROMs, BIOS, artwork, and saves can be copied here; emulator configs and gameplay history are available for inspection.</div>
        <div class="row g-2 mb-2">
          <div class="col-12 col-lg-3"><label class="form-label small" for="localAssetPeer">Connected Drone</label><select id="localAssetPeer" class="form-select"></select></div>
          <div class="col-6 col-lg-2"><label class="form-label small" for="localAssetType">Asset Type</label><select id="localAssetType" class="form-select"><option value="roms">ROMs</option><option value="bios">BIOS</option><option value="saves">Saves</option><option value="emulator_configs">Emulator Configs</option><option value="gameplay">Gameplay History</option><option value="artwork">Artwork</option></select></div>
          <div class="col-6 col-lg-2"><label class="form-label small">Systems</label><div class="dropdown"><button id="localAssetSystemsToggle" class="btn btn-outline-secondary dropdown-toggle w-100 text-start" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">All systems</button><div id="localAssetSystemsMenu" class="dropdown-menu p-2 w-100"><div class="small text-muted">Select a connected Drone to load systems.</div></div></div></div>
          <div class="col-8 col-lg-3"><label class="form-label small" for="localAssetQuery">Search</label><input id="localAssetQuery" class="form-control" placeholder="Search assets"></div>
          <div class="col-4 col-lg-2"><label class="form-label small" for="localAssetPageSize">Per Page</label><select id="localAssetPageSize" class="form-select"><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></div>
        </div>
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
          <button class="btn btn-primary" id="localAssetLoadBtn"><i class="bi bi-search me-1"></i>Request</button>
          <button class="btn btn-success" id="localAssetCopyAllBtn" disabled><i class="bi bi-cloud-arrow-down me-1"></i>Download All</button>
          <div class="form-check ms-lg-2 d-none" id="localAssetArtworkWrap"><input class="form-check-input" type="checkbox" id="localAssetIncludeArtwork" checked><label class="form-check-label small" for="localAssetIncludeArtwork">Include artwork (places art &amp; updates gamelist.xml)</label></div>
        </div>
        <div id="localAssetsBody"><div class="themed-empty">Pair a nearby Drone, then request its assets here.</div></div>
        <div id="localAssetsPagination" class="mt-2"></div>
      </div></div>
    <div class="card log-card mb-3"><div class="card-header">Local Transfers</div><div class="card-body" id="localDownloadsBody"></div></div>
    <div class="card log-card"><div class="card-header">Local Transfer History</div><div class="card-body" id="localActivityBody"></div></div>`;

  async function refresh() {
    const status = await api("/admin/local-network/status");
    document.getElementById("localPairingBody").innerHTML = status.active
      ? `<div class="d-flex flex-wrap align-items-center gap-3"><div><div class="small text-muted">Pairing code</div><div class="display-6 mono">${escapeHtml(status.pairing?.code || "")}</div></div><div class="small text-muted">Expires ${escapeHtml(status.pairing?.expires_at || "")}. Enter this code on the other Drone to approve it.</div></div>`
      : '<div class="themed-empty">Enable Local Network integration to discover and pair nearby Drones.</div>';
    document.getElementById("localPeersBody").innerHTML = renderLocalPeerRows(status.peers || []);
    document.getElementById("localDownloadsBody").innerHTML = renderLocalTransfersPanel(status.downloads || {});
    document.getElementById("localActivityBody").innerHTML = renderDownloadRows(status.activity || [], false);
    document.getElementById("localDiscoverBtn").disabled = !status.active;
    document.getElementById("localPairCodeRotateBtn").disabled = !status.active;
    const pairedPeers = (status.peers || []).filter(peer => peer.paired);
    const peerSelect = document.getElementById("localAssetPeer");
    const selectedPeerId = peerSelect.value || localPeerAssetContext.peerId;
    peerSelect.innerHTML = pairedPeers.length
      ? pairedPeers.map(peer => `<option value="${escapeHtml(peer.drone_id || "")}">${escapeHtml(peer.name || peer.hostname || peer.drone_id || "Drone")}</option>`).join("")
      : '<option value="">No paired Drones</option>';
    if (pairedPeers.some(peer => String(peer.drone_id || "") === selectedPeerId)) peerSelect.value = selectedPeerId;
    if (peerSelect.value && peerSelect.value !== localPeerAssetContext.peerId) {
      localPeerAssetContext.peerId = peerSelect.value;
      await loadLocalPeerSystems();
    }
    document.getElementById("localAssetLoadBtn").disabled = !pairedPeers.length;
    document.getElementById("localAssetCopyAllBtn").disabled = !pairedPeers.length;
  }
  window.refreshLocalNetwork = refresh;
  document.getElementById("localDiscoverBtn").addEventListener("click", async () => { await apiPost("/admin/local-network/discover", {}); await refresh(); });
  document.getElementById("localRefreshBtn").addEventListener("click", refresh);
  document.getElementById("localPairCodeRotateBtn").addEventListener("click", async () => { await apiPost("/admin/local-network/pairing-code/rotate", {}); await refresh(); });
  document.getElementById("localAssetLoadBtn").addEventListener("click", () => loadLocalPeerAssets());
  document.getElementById("localAssetCopyAllBtn").addEventListener("click", copyAllLocalAssets);
  document.getElementById("localAssetType").addEventListener("change", updateLocalAssetTypeUi);
  document.getElementById("localAssetPeer").addEventListener("change", loadLocalPeerSystems);
  document.getElementById("localAssetPageSize").addEventListener("change", () => loadLocalPeerAssets());
  document.getElementById("localAssetQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadLocalPeerAssets(); } });
  updateLocalAssetTypeUi();
  await refresh();
  startLocalTransfersAutoRefresh();
}

function localAssetIncludeArtwork() {
  const checkbox = document.getElementById("localAssetIncludeArtwork");
  return checkbox ? !!checkbox.checked : false;
}

function updateLocalAssetTypeUi() {
  const type = (document.getElementById("localAssetType") || {}).value || "roms";
  // The "Include artwork" option only applies when copying ROMs.
  const wrap = document.getElementById("localAssetArtworkWrap");
  if (wrap) wrap.classList.toggle("d-none", type !== "roms");
}

function selectedLocalAssetSystems() {
  return Array.from(document.querySelectorAll(".local-asset-system-check:checked"))
    .map(input => input.value)
    .sort((a, b) => a.localeCompare(b));
}

function renderLocalAssetSystems() {
  const menu = document.getElementById("localAssetSystemsMenu");
  const toggle = document.getElementById("localAssetSystemsToggle");
  if (!menu || !toggle) return;
  const systems = localPeerAssetContext.availableSystems || [];
  const selected = new Set(localPeerAssetContext.systems || []);
  menu.innerHTML = systems.length
    ? systems.map(system => `<label class="dropdown-item d-flex gap-2 align-items-center"><input class="form-check-input local-asset-system-check" type="checkbox" value="${escapeHtml(system)}" ${selected.has(system) ? "checked" : ""}>${escapeHtml(system)}</label>`).join("")
    : '<div class="small text-muted px-2 py-1">No systems reported.</div>';
  toggle.textContent = selected.size ? `${selected.size} selected` : "All systems";
  menu.querySelectorAll(".local-asset-system-check").forEach(input => input.addEventListener("change", () => {
    localPeerAssetContext.systems = selectedLocalAssetSystems();
    toggle.textContent = localPeerAssetContext.systems.length ? `${localPeerAssetContext.systems.length} selected` : "All systems";
  }));
}

async function loadLocalPeerSystems() {
  const peerId = (document.getElementById("localAssetPeer") || {}).value || localPeerAssetContext.peerId;
  localPeerAssetContext.peerId = peerId;
  localPeerAssetContext.systems = [];
  localPeerAssetContext.availableSystems = [];
  renderLocalAssetSystems();
  if (!peerId) return;
  try {
    const summary = await api(`/admin/local-network/peers/${encodeURIComponent(peerId)}/assets?type=summary`);
    localPeerAssetContext.availableSystems = Array.from(new Set(summary.systems || [])).sort((a, b) => String(a).localeCompare(String(b)));
  } catch (_) {
    localPeerAssetContext.availableSystems = [];
  }
  renderLocalAssetSystems();
}

async function pairLocalPeer(peerId) {
  const code = window.prompt("Enter the 8-digit pairing code shown on the other Drone:");
  if (!code) return;
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/pair`, { pairing_code: code.trim() });
  showToast("Drone paired.", "success");
  await window.refreshLocalNetwork();
}

async function forgetLocalPeer(peerId) {
  if (!window.confirm("Forget this paired Drone? It will need to be paired again before browsing or syncing.")) return;
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/forget`, {});
  await window.refreshLocalNetwork();
}

async function browseLocalPeer(peerId) {
  localPeerAssetContext = { peerId, peerName: peerId, assetType: "roms", systems: [], availableSystems: [], items: [], query: "", limit: 50, offset: 0, total: 0 };
  document.getElementById("localAssetPeer").value = peerId;
  document.getElementById("localAssetType").value = "roms";
  document.getElementById("localAssetQuery").value = "";
  await loadLocalPeerSystems();
  updateLocalAssetTypeUi();
  document.getElementById("localAssetsBody").innerHTML = '<div class="themed-empty">Choose an asset type and request assets from this Drone.</div>';
  document.getElementById("localAssetsPagination").innerHTML = "";
  document.getElementById("localAssetsCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadLocalPeerAssets(resetPage = true) {
  const peerId = document.getElementById("localAssetPeer").value;
  const type = document.getElementById("localAssetType").value;
  const systems = selectedLocalAssetSystems();
  const q = document.getElementById("localAssetQuery").value.trim();
  const limit = Math.max(1, Number(document.getElementById("localAssetPageSize").value) || 50);
  if (!peerId) { showToast("Pair a Drone before requesting assets.", "warning"); return; }
  if (resetPage || type !== localPeerAssetContext.assetType || systems.join(",") !== localPeerAssetContext.systems.join(",") || q !== localPeerAssetContext.query || limit !== localPeerAssetContext.limit) {
    localPeerAssetContext.offset = 0;
  }
  localPeerAssetContext.peerId = peerId;
  localPeerAssetContext.assetType = type;
  localPeerAssetContext.systems = systems;
  localPeerAssetContext.query = q;
  localPeerAssetContext.limit = limit;
  const params = new URLSearchParams({ type, limit: String(limit), offset: String(localPeerAssetContext.offset) });
  if (systems.length) params.set("systems", systems.join(","));
  if (q) params.set("q", q);
  const body = document.getElementById("localAssetsBody");
  body.innerHTML = '<div class="text-muted">Requesting peer assets...</div>';
  try {
    const payload = await api(`/admin/local-network/peers/${encodeURIComponent(localPeerAssetContext.peerId)}/assets?${params.toString()}`);
    localPeerAssetContext.total = Number(payload.total) || 0;
    if (typeof payload.limit === "number") localPeerAssetContext.limit = payload.limit;
    if (typeof payload.offset === "number") localPeerAssetContext.offset = payload.offset;
    body.innerHTML = renderLocalAssetRows(payload);
    renderLocalAssetsPagination();
  } catch (err) {
    body.innerHTML = `<div class="themed-empty text-danger">${escapeHtml(err.message || "Failed to request assets")}</div>`;
    document.getElementById("localAssetsPagination").innerHTML = "";
  }
}

function setLocalAssetPage(page) {
  const limit = Math.max(1, Number(localPeerAssetContext.limit) || 50);
  localPeerAssetContext.offset = Math.max(0, (Math.max(1, Number(page) || 1) - 1) * limit);
  loadLocalPeerAssets(false);
}

async function copyLocalPeerAsset(index) {
  const item = localPeerAssetContext.items[index];
  if (!item) return;
  const result = await apiPost("/admin/local-network/sync", {
    peer_id: localPeerAssetContext.peerId,
    asset_type: localPeerAssetContext.assetType,
    system: item.system || item.root_name || "",
    include_artwork: localAssetIncludeArtwork(),
    item,
  });
  if (result && result.rom_skipped) {
    const artworkJobs = Array.isArray(result.jobs) ? result.jobs.length : 0;
    showToast(artworkJobs
      ? "ROM already on this machine — copying its artwork only."
      : "ROM already on this machine — nothing to download.", "info");
  } else {
    showToast("Asset queued for local transfer.", "success");
  }
  await window.refreshLocalNetwork();
}

async function copyAllLocalAssets() {
  const peerId = document.getElementById("localAssetPeer").value;
  const type = document.getElementById("localAssetType").value;
  const systems = selectedLocalAssetSystems();
  const q = document.getElementById("localAssetQuery").value.trim();
  if (!peerId) { showToast("Pair a Drone before copying assets.", "warning"); return; }
  if (!LOCAL_TRANSFERABLE_TYPES.has(type)) { showToast("Bulk download supports ROMs, BIOS, saves, and artwork.", "warning"); return; }
  const scope = systems.length ? `all ${type} for ${systems.join(", ")}` : (q ? `all ${type} matching “${q}”` : `every ${type} asset`);
  if (!window.confirm(`Queue ${scope} from this Drone for download?`)) return;
  await queueLocalBulkCopy({ peer_id: peerId, asset_type: type, systems, q, include_artwork: localAssetIncludeArtwork() });
}

async function copyAllRomsForSystem(encodedSystem) {
  const system = decodeURIComponent(encodedSystem);
  const peerId = document.getElementById("localAssetPeer").value || localPeerAssetContext.peerId;
  if (!peerId) { showToast("Pair a Drone before copying assets.", "warning"); return; }
  if (!window.confirm(`Queue all ROMs for ${system} from this Drone for download?`)) return;
  await queueLocalBulkCopy({ peer_id: peerId, asset_type: "roms", system, include_artwork: localAssetIncludeArtwork() });
}

async function queueLocalBulkCopy(body) {
  try {
    const result = await apiPost("/admin/local-network/sync-bulk", body);
    const assets = Number(result.queued_assets) || 0;
    const artwork = Number(result.queued_artwork) || 0;
    const skipped = Number(result.skipped_existing) || 0;
    const skippedNote = skipped ? ` ${skipped} already on this machine were skipped.` : "";
    if (!assets && !artwork) {
      showToast(skipped ? `All ${skipped} already on this machine — nothing to download.` : "Nothing matched to download.", skipped ? "info" : "warning");
    } else {
      showToast(`Queued ${assets} ${body.asset_type}${artwork ? ` (+${artwork} artwork files)` : ""} for local transfer.${skippedNote}`, "success");
    }
    await window.refreshLocalNetwork();
  } catch (err) {
    showToast(`Bulk download failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function renderOvermindIntegrationPanel(target) {
  target.innerHTML = `
    <div class="card log-card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Configuration</span>
        <button id="overmindRefreshBtn" class="btn btn-sm btn-outline-primary" type="button">Refresh</button>
      </div>
      <div class="card-body">
        <div class="row g-3">
          <div class="col-12 col-lg-6">
            <div class="mb-3">
              <label class="form-label">Overmind URL <span class="text-danger">*</span> <span class="text-muted small">Required</span></label>
              <input id="overmindUrlInput" class="form-control" type="url" placeholder="https://www.batocera-swarm.com">
              <div class="text-muted small mt-1"><a href="https://www.batocera-swarm.com" target="_blank" rel="noopener noreferrer">Open Batocera Swarm <i class="bi bi-box-arrow-up-right ms-1"></i></a></div>
            </div>
            <div class="mb-3">
              <label class="form-label">Authorization Token <span class="text-danger">*</span> <span class="text-muted small">Required</span></label>
              <input id="overmindAuthTokenInput" class="form-control" type="password" placeholder="Token generated in Overmind">
              <div class="text-muted small mt-1">Paste an authorization token from Overmind. This token is required to connect this Drone.</div>
            </div>
            <div class="mb-3">
              <label class="form-label">Drone Name  <span class="text-muted small">Optional</span></label>
              <input id="droneNameInput" class="form-control" type="text" placeholder="Arcade Cabinet">
            </div>
          </div>
          <div class="col-12 col-lg-6">
            <div class="p-3 rounded border h-100" style="border-color:var(--admin-border)!important;background:rgba(31,42,68,.35)">
              <div class="fw-semibold mb-1">Claim Ownership <span class="text-muted small">Optional</span></div>
              <div class="text-muted small mb-3">Use your Overmind account to identify this Drone as yours. This grants your Overmind account admin access to this Drone even when it belongs to another swarm.</div>
              <div class="mb-3">
                <label class="form-label">Overmind Email</label>
                <input id="claimEmailInput" class="form-control" type="email" autocomplete="username">
              </div>
              <div class="mb-0">
                <label class="form-label">Overmind Password</label>
                <input id="claimPasswordInput" class="form-control" type="password" autocomplete="current-password">
              </div>
            </div>
          </div>
        </div>
        <div class="d-flex gap-2">
          <button id="overmindSaveBtn" class="btn btn-primary" type="button">Save Configuration</button>
          <button id="overmindDisconnectBtn" class="btn btn-outline-danger" type="button">Disconnect Swarm</button>
        </div>
        <hr>
        <div class="small" id="overmindStatus"></div>
      </div>
    </div>
    <div class="card log-card mt-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-cloud-arrow-down me-2"></i>Downloads</span>
        <button id="overmindDownloadsRefreshBtn" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
      </div>
      <div class="card-body" id="overmindDownloadsBody"><div class="text-muted">Loading downloads...</div></div>
    </div>
    <div class="card log-card mt-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-database-check me-2"></i>Asset Cache</span>
        <div class="d-flex gap-2">
          <button id="overmindAssetCacheRefreshBtn" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
          <button class="btn btn-sm btn-outline-warning" type="button" onclick="clearPendingAssetChanges()"><i class="bi bi-x-circle me-1"></i>Clear Pending</button>
          <button class="btn btn-sm btn-outline-danger" type="button" onclick="purgeAssetCache()">Purge &amp; Resync</button>
        </div>
      </div>
      <div class="card-body" id="overmindAssetCacheBody"><div class="text-muted">Loading asset cache...</div></div>
    </div>
    <div class="card log-card mt-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span>Processed Overmind Actions</span>
        <button id="overmindActionsRefreshBtn" class="btn btn-sm btn-outline-primary" type="button">Refresh</button>
      </div>
      <div class="card-body" id="overmindActionsBody">
        <div class="text-muted">Loading processed actions...</div>
      </div>
    </div>
  `;

  const statusEl = document.getElementById("overmindStatus");
  const urlInput = document.getElementById("overmindUrlInput");
  const droneNameInput = document.getElementById("droneNameInput");
  const authTokenInput = document.getElementById("overmindAuthTokenInput");
  const saveBtn = document.getElementById("overmindSaveBtn");
  const disconnectBtn = document.getElementById("overmindDisconnectBtn");
  const claimEmailInput = document.getElementById("claimEmailInput");
  const claimPasswordInput = document.getElementById("claimPasswordInput");
  const refreshBtn = document.getElementById("overmindRefreshBtn");
  const actionsRefreshBtn = document.getElementById("overmindActionsRefreshBtn");
  const actionsBody = document.getElementById("overmindActionsBody");
  const downloadsRefreshBtn = document.getElementById("overmindDownloadsRefreshBtn");
  const downloadsBody = document.getElementById("overmindDownloadsBody");
  const assetCacheRefreshBtn = document.getElementById("overmindAssetCacheRefreshBtn");
  const assetCacheBody = document.getElementById("overmindAssetCacheBody");
  const ACTIONS_PER_PAGE = 10;
  let allActions = [];
  let actionsPage = 0;

  function renderActionsPage() {
    const total = allActions.length;
    const totalPages = Math.max(1, Math.ceil(total / ACTIONS_PER_PAGE));
    actionsPage = Math.max(0, Math.min(actionsPage, totalPages - 1));
    const start = actionsPage * ACTIONS_PER_PAGE;
    const pageItems = allActions.slice(start, start + ACTIONS_PER_PAGE);
    const showPrev = actionsPage > 0;
    const showNext = actionsPage < totalPages - 1;
    const paginationHtml = totalPages > 1 ? `
      <div class="d-flex align-items-center gap-2 mt-2 flex-wrap">
        <button class="btn btn-sm btn-outline-secondary" onclick="overmindActionsPrev()" ${showPrev ? "" : "disabled"}>&#8249; Prev</button>
        <span class="small text-muted">Page ${actionsPage + 1} of ${totalPages} &nbsp;(${total} total)</span>
        <button class="btn btn-sm btn-outline-secondary" onclick="overmindActionsNext()" ${showNext ? "" : "disabled"}>Next &#8250;</button>
      </div>` : "";
    actionsBody.innerHTML = pageItems.length ? `
      <div class="table-responsive">
        <table class="table table-sm align-middle themed-table">
          <thead>
            <tr>
              <th>Processed</th>
              <th>Action</th>
              <th>Status</th>
              <th>Device</th>
              <th>Message</th>
              <th>Returned Data</th>
            </tr>
          </thead>
          <tbody>
            ${pageItems.map(action => `
              <tr>
                <td class="text-nowrap">${escapeHtml(formatCompactLocalDate(action.processed_at) || "n/a")}</td>
                <td>${escapeHtml(action.action || "n/a")}</td>
                <td><span class="badge text-bg-secondary">${escapeHtml(action.status || "n/a")}</span></td>
                <td class="mono small">${escapeHtml(action.device_id || "n/a")}</td>
                <td>${escapeHtml(action.message || "")}${action.fake_data ? ' <span class="badge text-bg-info ms-1">fake data</span>' : ''}</td>
                <td>${escapeHtml(action.result_summary || "")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
      ${paginationHtml}
    ` : `<div class="themed-empty">No processed actions yet.</div>`;
  }

  window.overmindActionsPrev = function() { actionsPage--; renderActionsPage(); };
  window.overmindActionsNext = function() { actionsPage++; renderActionsPage(); };

  function renderStatus(payload) {
    const status = payload.status || {};
    const configured = status.configured ? "yes" : "no";
    const enabled = status.integration_enabled ? "yes" : "no";
    const state = status.integration_state || "not_started";
    const swarmStatus = status.swarm_connection_status || "disconnected";
    const requestedAt = status.requested_at || "n/a";
    const startedAt = status.last_started_at || "n/a";
    const errorMsg = status.last_error || "none";
    const authTokenMask = payload.auth_token_masked || "(not set)";
    const droneName = payload.drone_name || "(not set)";
    const machineId = payload.machine_id || "n/a";
    const cert = payload.certificate || {};
    const swarm = payload.swarm || [];
    const peerChecks = payload.peer_checks || [];
    const overmindActive = payload.overmind_active !== false;
    statusEl.innerHTML = `
      <div class="d-flex flex-wrap gap-2 mb-2">
        <span class="badge ${overmindActive ? "text-bg-success" : "text-bg-secondary"}">Overmind: ${overmindActive ? "enabled" : "disabled"}</span>
        <span class="badge ${status.configured ? "text-bg-success" : "text-bg-secondary"}">Overmind: ${status.configured ? "linked" : "disconnected"}</span>
        <span class="badge ${swarmStatus === "connected" ? "text-bg-success" : swarmStatus.includes("pending") || swarmStatus.includes("requested") ? "text-bg-warning" : "text-bg-secondary"}">Connected to Swarm: ${escapeHtml(swarmStatus)}</span>
      </div>
      <div><strong>Configured:</strong> ${escapeHtml(configured)}</div>
      <div><strong>Integration Enabled:</strong> ${escapeHtml(enabled)}</div>
      <div><strong>Machine ID:</strong> ${escapeHtml(machineId)}</div>
      <div><strong>Action Polling:</strong> Drone sends a heartbeat request and receives one pending action per poll.</div>
      <div><strong>State:</strong> ${escapeHtml(state)}</div>
      <div><strong>Drone Name:</strong> ${escapeHtml(droneName)}</div>
      <div><strong>Authorization Token:</strong> ${escapeHtml(authTokenMask)}</div>
      <div><strong>Requested At:</strong> ${escapeHtml(requestedAt)}</div>
      <div><strong>Last Started At:</strong> ${escapeHtml(startedAt)}</div>
      <div><strong>Last Error:</strong> ${escapeHtml(errorMsg)}</div>
      <div><strong>Certificate:</strong> ${escapeHtml(cert.status || "unknown")}${cert.fingerprint ? ` · ${escapeHtml(String(cert.fingerprint).slice(0, 16))}` : ""}</div>
      <div><strong>Swarm Drones:</strong> ${swarm.length}</div>
      <div class="mt-2"><strong>Notes:</strong> ${escapeHtml(status.notes || "")}</div>
      ${swarm.filter(d => String(d.drone_id || d.device_id || "") !== machineId).length ? `<div class="mt-3"><strong>Last Swarm Snapshot (P2P Health via Public IP)</strong>${swarm.filter(d => String(d.drone_id || d.device_id || "") !== machineId).map((drone) => {
        const dronePeerId = String(drone.drone_id || drone.device_id || "");
        const checks = peerChecks.filter((item) => String(item.target_drone_id || "") === dronePeerId);
        const latest = checks[0] || {};
        const passed = latest.status === "pass";
        const hasPublicIp = !!(drone.public_ip || "").trim();
        return `<div class="mt-2 p-2 rounded border" style="border-color:var(--admin-border)!important;background:rgba(31,42,68,.45)">
          <div class="d-flex justify-content-between gap-2"><span>${escapeHtml(drone.name || drone.hostname || drone.device_name || dronePeerId || "Drone")}</span><span class="badge ${!hasPublicIp ? "text-bg-secondary" : (passed ? "text-bg-success" : (latest.status ? "text-bg-danger" : "text-bg-warning"))}">${!hasPublicIp ? "no public IP" : (latest.status ? (passed ? "RESOLVED" : "FAILED") : "pending")}</span></div>
          <div class="small text-muted mono">${escapeHtml(dronePeerId)}</div>
          <div class="small text-muted">Public IP: ${escapeHtml(drone.public_ip || "n/a")}</div>
          <div class="small text-muted">Checked: ${escapeHtml(latest.checked_at || "n/a")}${latest.latency_ms != null ? ` · ${latest.latency_ms} ms` : ""}</div>
          ${latest.failure_reason ? `<div class="small text-danger">${escapeHtml(latest.failure_reason)}</div>` : ""}
        </div>`;
      }).join("")}</div>` : ""}
      ${!overmindActive ? '<div class="alert alert-warning mt-3 mb-0">Overmind communication is disabled. Enable it on the Integration page to resume it.</div>' : ""}
    `;
    urlInput.value = payload.overmind_url || "https://www.batocera-swarm.com";
    droneNameInput.value = payload.drone_name || "";
    claimEmailInput.value = payload.overmind_email || "";
  }

  async function loadStatus() {
    const payload = await api("/admin/integrations/overmind/status");
    renderStatus(payload);
  }

  async function loadActions() {
    const payload = await api("/admin/integrations/overmind/actions");
    allActions = payload.actions || [];
    actionsPage = 0;
    renderActionsPage();
  }

  async function loadDownloads() {
    const payload = await api("/admin/downloads");
    downloadsBody.innerHTML = renderDownloadsPanel(payload, false);
  }

  async function loadAssetCache() {
    const payload = await api("/admin/asset-cache");
    assetCacheBody.innerHTML = renderAssetCachePanel(payload, false);
  }

  window.refreshOvermindDownloads = loadDownloads;
  window.refreshOvermindAssetCache = loadAssetCache;

  async function saveConfig() {
    const overmindUrl = (urlInput.value || "").trim();
    const droneName = (droneNameInput.value || "").trim();
    const overmindAuthToken = authTokenInput.value || "";
    const claimEmail = (claimEmailInput.value || "").trim();
    const claimPassword = claimPasswordInput.value || "";
    const body = { overmind_url: overmindUrl, drone_name: droneName };
    if (overmindAuthToken) {
      body.overmind_auth_token = overmindAuthToken;
    }
    if (claimEmail) {
      body.overmind_email = claimEmail;
    }
    if (claimPassword) {
      body.overmind_email = claimEmail;
      body.overmind_password = claimPassword;
    }
    const payload = await apiPost("/admin/integrations/overmind/config", body);
    authTokenInput.value = "";
    claimPasswordInput.value = "";
    const state = payload.status?.integration_state || "";
    const isActive = payload.status?.integration_enabled && state !== "pending_failed";
    if (state === "pending_failed") {
      console.error("Overmind authorization failed", payload.status?.last_error || "authorization token was rejected", payload.status?.last_onboarding_attempt || {});
      showToast(`Overmind authorization failed: ${escapeHtml(payload.status?.last_error || "authorization token was rejected")}`, "danger");
    } else {
      showToast(isActive ? "Overmind registered and polling is active." : "Overmind configuration saved. Check status for registration details.", isActive ? "success" : "warning");
    }
    renderStatus(payload);
  }

  saveBtn.addEventListener("click", async () => {
    setLoading(true, "Saving Overmind configuration...");
    try {
      await saveConfig();
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to save config"), "danger");
      claimPasswordInput.value = "";
    } finally {
      setLoading(false);
    }
  });
  disconnectBtn.addEventListener("click", async () => {
    if (!window.confirm("Disconnect this Drone from its Overmind swarm?")) return;
    setLoading(true, "Disconnecting from swarm...");
    try {
      const payload = await apiPost("/admin/integrations/overmind/swarm/disconnect", {});
      renderStatus(payload);
      showToast("Disconnected from swarm.", "success");
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to disconnect swarm"), "danger");
    } finally {
      setLoading(false);
    }
  });
  refreshBtn.addEventListener("click", async () => {
    setLoading(true, "Loading Overmind status...");
    try {
      await loadStatus();
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to load status"), "danger");
    } finally {
      setLoading(false);
    }
  });
  actionsRefreshBtn.addEventListener("click", async () => {
    setLoading(true, "Loading processed actions...");
    try {
      await loadActions();
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to load processed actions"), "danger");
    } finally {
      setLoading(false);
    }
  });
  downloadsRefreshBtn.addEventListener("click", async () => {
    try {
      await loadDownloads();
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to load downloads"), "danger");
    }
  });
  assetCacheRefreshBtn.addEventListener("click", async () => {
    try {
      await loadAssetCache();
    } catch (err) {
      showToast(escapeHtml(err.message || "Failed to load asset cache"), "danger");
    }
  });

  setLoading(true, "Loading Overmind status...");
  try {
    const loaders = [
      ["status", loadStatus],
      ["actions", loadActions],
      ["downloads", loadDownloads],
      ["asset cache", loadAssetCache],
    ];
    const results = await Promise.allSettled(loaders.map(([, loader]) => loader()));
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        const label = loaders[index][0];
        showToast(`Failed to load Overmind ${label}: ${escapeHtml(result.reason?.message || "unknown error")}`, "danger");
      }
    });
  } finally {
    setLoading(false);
  }
}
async function renderApiAdminPage() {
  titleNode.textContent = "API Access";
  subtitleNode.textContent = "Swagger documentation and mTLS certificate guidance";
  setLoading(true, "Loading API status...");
  try {
    const payload = await api("/admin/api/status");
    const cert = payload.certificate || {};
    const rows = [
      ["Fingerprint", cert.fingerprint],
      ["Subject", cert.subject],
      ["Issuer", cert.issuer],
      ["Serial Number", cert.serial_number],
      ["SAN", (cert.san || []).join(", ")],
      ["Valid From", cert.valid_from],
      ["Valid Until", cert.valid_until],
      ["Renewal", cert.renewal_status],
      ["Source", cert.source],
    ];
    content.innerHTML = `
      <div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button></div>
      <div class="card log-card mb-3">
        <div class="card-header">API Documentation</div>
        <div class="card-body">
          <div class="d-flex flex-wrap gap-2 mb-3">
            <a class="btn btn-primary" href="${escapeHtml(payload.swagger_url || `${API_BASE}/swagger`)}" target="_blank" rel="noopener noreferrer"><i class="bi bi-braces me-1"></i>Open Swagger</a>
            <a class="btn btn-outline-primary" href="${escapeHtml(payload.openapi_url || `${API_BASE}/openapi.json`)}" target="_blank" rel="noopener noreferrer">Open OpenAPI JSON</a>
            <a class="btn btn-outline-primary" href="${escapeHtml(payload.certificate_download_url || `${API_BASE}/admin/api/certificate`)}"><i class="bi bi-download me-1"></i>Download Public Certificate</a>
            <button class="btn btn-outline-warning" type="button" id="rotateDroneCertBtn"><i class="bi bi-arrow-repeat me-1"></i>Rotate Drone Certificate</button>
          </div>
          <div class="alert alert-warning mb-0">Do not share Drone private key material. Store certificates safely, rotate them if exposed, and only call protected peer APIs from trusted systems.</div>
        </div>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header">Certificate Metadata</div>
        <div class="card-body">
          <div class="row g-2">
            ${rows.map(([label, value]) => `
              <div class="col-12 col-md-6">
                <div class="text-muted small">${escapeHtml(label)}</div>
                <div class="mono small text-break">${escapeHtml(String(value || "n/a"))}</div>
              </div>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="card log-card">
        <div class="card-header">mTLS Example</div>
        <div class="card-body">
          <p class="text-muted small">Peer API routes can require a client certificate. The public certificate download does not include the private key.</p>
          <pre class="mono small p-3 rounded" style="background:rgba(0,0,0,.25);white-space:pre-wrap">${escapeHtml((payload.guidance || {}).curl || "")}</pre>
          <div class="text-muted small">${escapeHtml((payload.guidance || {}).lifecycle || "")}</div>
        </div>
      </div>
    `;
    document.getElementById("rotateDroneCertBtn")?.addEventListener("click", async () => {
      if (!window.confirm("Rotate this Drone mTLS certificate through Overmind signing?")) return;
      setLoading(true, "Rotating Drone certificate...");
      try {
        await apiPost("/admin/api/certificate/rotate", {});
        showToast("Drone certificate rotated.", "success");
        await renderApiAdminPage();
      } catch (err) {
        showToast(`Certificate rotation failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    });
  } catch (err) {
    showToast(`Failed to load API status: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function renderLogsPage(selectedSource = null, selectedLines = 200) {
  stopLogAutoRefresh();
  const logSources = [
    ["drone_stdout", "Drone Stdout", "bi-file-text"],
    ["drone_stderr", "Drone Stderr", "bi-bug"],
    ["drone_overmind", "Overmind", "bi-broadcast"],
    ["es_launch_stdout", "ES Launch Stdout", "bi-terminal"],
    ["es_launch_stderr", "ES Launch Stderr", "bi-exclamation-triangle"],
  ];
  const validSources = new Set(logSources.map(([source]) => source));
  const effectiveSource = validSources.has(selectedSource) ? selectedSource : null;
  const effectiveLines = clampLogLines(selectedLines);

  titleNode.textContent = "System Logs";
  subtitleNode.textContent = "View Drone application and EmulationStation launch logs";
  content.innerHTML = `
    <div class="mb-3">
      <button class="btn btn-outline-secondary" onclick="renderAdminPage()">← Back to Admin</button>
    </div>
    <div class="row">
      <div class="col-md-3 col-xl-2">
        <div class="card log-card">
          <div class="card-header">Log Sources</div>
          <div class="list-group list-group-flush log-source-list" id="logSources">
            ${logSources.map(([source, label, icon]) => `
              <button type="button" class="list-group-item list-group-item-action text-start" data-log-source="${source}" onclick="loadLog('${source}', this)">
                <i class="bi ${icon} me-2"></i>${label}
              </button>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="col-md-9 col-xl-10">
        <div class="card log-card">
          <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
            <div><span id="logTitle">Select a log source</span><span class="badge text-bg-success ms-2"><i class="bi bi-broadcast-pin me-1"></i>Live · 5s</span></div>
            <div class="d-flex align-items-center flex-wrap gap-2">
              <label for="linesInput" class="form-label me-2">Lines:</label>
              <select id="linesInput" class="form-select log-lines-select">
                <option value="100">100</option>
                <option value="200">200</option>
                <option value="500">500</option>
                <option value="1000">1000</option>
                <option value="2000">2000</option>
                <option value="5000">5000</option>
              </select>
              <button class="btn btn-sm btn-outline-primary" onclick="refreshCurrentLog()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <div class="small text-muted mb-2">Newest lines are shown first. Automatic updates preserve your reading position.</div>
            <textarea id="logContent" class="mono log-content bg-dark text-light p-3 form-control" readonly spellcheck="false">Select a log source from the left panel to view its contents.</textarea>
          </div>
        </div>
      </div>
    </div>
  `;
  const linesInput = document.getElementById("linesInput");
  if (linesInput) {
    linesInput.value = String(effectiveLines);
  }
  if (effectiveSource) {
    const sourceBtn = document.querySelector(`#logSources .list-group-item[data-log-source="${effectiveSource}"]`);
    await loadLog(effectiveSource, sourceBtn, false);
  }
  startLogAutoRefresh();
}
async function renderGameplayLogsPage() {
  stopLogAutoRefresh();
  currentLogSource = null;
  titleNode.textContent = "Gameplay Logs";
  subtitleNode.textContent = "Detected game launches from EmulationStation";
  setLoading(true, "Loading gameplay logs...");
  try {
    const payload = await api("/admin/gameplay-logs");
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    const rows = sessions.map((session) => {
      const duration = session.duration_seconds !== undefined && session.duration_seconds !== null
        ? `${Math.round(Number(session.duration_seconds) || 0)}s`
        : "n/a";
      return `
        <tr>
          <td class="text-nowrap">${escapeHtml(session.played_at || "n/a")}</td>
          <td>${escapeHtml(session.system_name || "n/a")}</td>
          <td>
            <div class="fw-semibold">${escapeHtml(session.game_name || session.name || "Unknown game")}</div>
            <div class="text-muted small mono">${escapeHtml(session.rom_path || "")}</div>
          </td>
          <td class="text-nowrap">${escapeHtml(duration)}</td>
        </tr>
      `;
    }).join("");
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap justify-content-between gap-2">
        <button class="btn btn-outline-secondary" onclick="renderAdminPage()">Back to Admin</button>
        <button class="btn btn-outline-primary" onclick="renderGameplayLogsPage()"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
          <span>Gameplay Sessions</span>
          <span class="badge text-bg-secondary">${sessions.length} session${sessions.length === 1 ? "" : "s"}${payload.pending_spool_events ? ` · ${payload.pending_spool_events} pending event${payload.pending_spool_events === 1 ? "" : "s"}` : ""}</span>
        </div>
        <div class="card-body">
          <div class="table-responsive">
            <table class="table table-sm table-hover align-middle themed-table">
              <thead><tr><th>Played</th><th>System</th><th>Game</th><th>Duration</th></tr></thead>
              <tbody>${rows || '<tr><td colspan="4" class="text-muted">No gameplay sessions detected yet.</td></tr>'}</tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  } catch (err) {
    showToast(`Failed to load gameplay logs: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="mb-3">
        <button class="btn btn-outline-secondary" onclick="renderAdminPage()">Back to Admin</button>
      </div>
      <div class="text-muted">Gameplay logs could not be loaded.</div>
    `;
  } finally {
    setLoading(false);
  }
}
async function loadLog(source, triggerEl = null, updateHash = true, silent = false) {
  currentLogSource = source;
  const lines = clampLogLines(document.getElementById("linesInput")?.value || "200");
  const targetHash = `#admin/logs/${encodeURIComponent(source)}?lines=${encodeURIComponent(lines)}`;
  if (updateHash && window.location.hash !== targetHash) {
    setHash(targetHash);
    return;
  }
  if (!silent) setLoading(true, `Loading ${source} logs...`);
  try {
    const data = await api(`/admin/logs/${source}?lines=${lines}`);
    const logTitle = document.getElementById("logTitle");
    const logContent = document.getElementById("logContent");
    if (!logTitle || !logContent) throw new Error("Log viewer is not available");
    const previousHeight = logContent.scrollHeight;
    const previousTop = logContent.scrollTop;
    const wasAtTop = previousTop <= 2;
    logTitle.textContent = `${data.source} Log (${data.path})`;
    logContent.value = [...data.content].reverse().join("\n");
    if (!wasAtTop) {
      logContent.scrollTop = previousTop + Math.max(0, logContent.scrollHeight - previousHeight);
    } else {
      logContent.scrollTop = previousTop;
    }
    document.querySelectorAll('#logSources .list-group-item').forEach(el => el.classList.remove('active'));
    const activeEl = triggerEl || document.querySelector(`#logSources .list-group-item[data-log-source="${source}"]`);
    if (activeEl) activeEl.classList.add('active');
  } catch (err) {
    if (!silent) {
      showToast(`Error loading log: ${escapeHtml(err.message || "unknown error")}`, "danger");
      const logContent = document.getElementById("logContent");
      if (logContent) logContent.value = "";
    }
  }
  if (!silent) setLoading(false);
}
async function refreshCurrentLog() {
  if (!currentLogSource) return;
  const activeSource = document.querySelector('#logSources .list-group-item.active');
  await loadLog(currentLogSource, activeSource);
}
async function renderEmulatorsPage() {
  setSearchMode("hidden");
  titleNode.textContent = "Emulators";
  subtitleNode.textContent = "Emulator config files mirrored to Overmind";
  clearSystemTheme();
  setLoading(true, "Loading emulator configs...");
  try {
    const payload = await api("/admin/emulators");
    const configs = Array.isArray(payload.configs) ? payload.configs : [];
    emulatorConfigRows = configs.map((item, index) => {
      const label = item.relative_path || item.path || item.name || `config-${index + 1}`;
      const content = item.content || item.text || JSON.stringify(item, null, 2);
      const versions = Array.isArray(item.versions) && item.versions.length
        ? item.versions
        : [{ collected_at: item.collected_at || "", fingerprint: item.fingerprint || item.md5 || "", content }];
      return {
        label,
        rootName: item.root_name || "configs",
        root: item.root || "",
        path: item.path || "",
        content: item.content || "",
        contentLoaded: Boolean(item.content || item.error),
        fingerprint: item.fingerprint || item.md5 || "",
        md5: item.md5 || "",
        size: item.size,
        truncated: Boolean(item.truncated),
        error: item.error || "",
        versions,
      };
    });
    selectedEmulatorConfigIndex = Math.min(selectedEmulatorConfigIndex || 0, Math.max(0, emulatorConfigRows.length - 1));
    content.innerHTML = `
      <div class="row">
        <div class="col-md-3 mb-3">
          <div class="card log-card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span>Overmind Config Set</span>
              <span class="badge">${emulatorConfigRows.length}</span>
            </div>
            <div class="emulator-config-filter-wrap p-2">
              <input id="emulatorConfigFilter" class="form-control form-control-sm" type="search" placeholder="Filter configs" autocomplete="off" oninput="filterEmulatorConfigs(this.value)">
            </div>
            <div class="list-group list-group-flush emulator-config-source-scroll" id="emulatorConfigSources">
              ${emulatorConfigRows.map((row, index) => `
                <button type="button" class="list-group-item list-group-item-action text-start" data-config-index="${index}" onclick="selectEmulatorConfig(${index})">
                  <i class="bi bi-file-earmark-code me-2"></i>${escapeHtml(row.label)}
                </button>
              `).join("")}
            </div>
            <div id="emulatorConfigFilterEmpty" class="small text-muted px-3 py-2" style="display:none;">No configs match.</div>
            <div class="small text-muted px-3 py-2 border-top" style="border-color:var(--admin-border)!important;">Only configuration files selected for Overmind synchronization are shown${payload.max_configs ? `, up to ${payload.max_configs}` : ""}.</div>
          </div>
        </div>
        <div class="col-md-9">
          <div class="card log-card">
            <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
              <span id="emulatorConfigTitle">Select a config</span>
              <div class="d-flex flex-wrap align-items-end gap-2">
                <div>
                  <label class="form-label small mb-1" for="emulatorConfigVersion">Version</label>
                  <select id="emulatorConfigVersion" class="form-select form-select-sm" onchange="selectEmulatorConfigVersion(this.value)"></select>
                </div>
                <button class="btn btn-sm btn-outline-primary" onclick="renderEmulatorsPage()">Refresh</button>
              </div>
            </div>
            <div class="card-body">
              <div class="mb-2">
                <div id="emulatorConfigPath" class="small text-muted"></div>
                <div id="emulatorConfigFingerprint" class="small text-muted mono"></div>
              </div>
              <pre id="emulatorConfigContent" class="mono bg-dark text-light p-3" style="max-height: 640px; overflow-y: auto; white-space: pre-wrap;">Select a config from the left panel to view its contents.</pre>
            </div>
          </div>
        </div>
      </div>
    `;
    if (!emulatorConfigRows.length) {
      document.getElementById("emulatorConfigContent").textContent = "No emulator config files were found in the Overmind reporting set.";
    } else {
      setTimeout(() => selectEmulatorConfig(selectedEmulatorConfigIndex), 0);
    }
  } catch (err) {
    content.innerHTML = `<div class="alert alert-danger">Failed to load emulator configs: ${escapeHtml(err.message || "unknown error")}</div>`;
  } finally {
    setLoading(false);
  }
}
async function loadSelectedEmulatorConfigContent(row) {
  if (!row || row.contentLoaded) return row;
  const params = new URLSearchParams({
    root: row.rootName || "configs",
    relative_path: row.label,
    max_bytes: "131072",
  });
  const data = await api(`/admin/emulators/file?${params.toString()}`);
  row.root = data.root || row.root;
  row.path = data.path || row.path;
  row.content = data.content || "";
  row.error = data.error || "";
  row.fingerprint = data.fingerprint || data.md5 || row.fingerprint;
  row.md5 = data.md5 || row.md5;
  row.truncated = Boolean(data.truncated);
  row.contentLoaded = true;
  row.versions = [{ collected_at: data.collected_at || "", fingerprint: row.fingerprint, content: row.content }];
  return row;
}
async function selectEmulatorConfig(index) {
  const row = emulatorConfigRows[index];
  if (!row) return;
  const requestId = ++emulatorConfigSelectionRequestId;
  selectedEmulatorConfigIndex = index;
  selectedEmulatorConfigVersionIndex = 0;
  document.querySelectorAll("#emulatorConfigSources .list-group-item").forEach((node) => {
    node.classList.toggle("active", Number(node.dataset.configIndex) === index);
  });
  const title = document.getElementById("emulatorConfigTitle");
  const path = document.getElementById("emulatorConfigPath");
  const fingerprint = document.getElementById("emulatorConfigFingerprint");
  const versionSelect = document.getElementById("emulatorConfigVersion");
  const contentNode = document.getElementById("emulatorConfigContent");
  if (title) title.textContent = row.label;
  if (path) path.textContent = row.root || row.path || "";
  if (contentNode && !row.contentLoaded) contentNode.textContent = "Loading config...";
  if (versionSelect) versionSelect.disabled = !row.contentLoaded;
  try {
    await loadSelectedEmulatorConfigContent(row);
  } catch (err) {
    row.error = err.message || "Failed to load config";
    row.contentLoaded = true;
  }
  if (requestId !== emulatorConfigSelectionRequestId || selectedEmulatorConfigIndex !== index) return;
  if (path) path.textContent = row.root || row.path || "";
  if (versionSelect) {
    const optionsHtml = (row.versions || []).map((version, versionIndex) => {
      const stamp = version.collected_at ? new Date(version.collected_at).toLocaleString() : `Version ${versionIndex + 1}`;
      const hash = version.fingerprint ? ` ${String(version.fingerprint).slice(0, 8)}` : "";
      return `<option value="${versionIndex}">${escapeHtml(stamp + hash)}</option>`;
    }).join("");
    if (document.activeElement !== versionSelect && versionSelect.innerHTML !== optionsHtml) {
      versionSelect.innerHTML = optionsHtml;
      versionSelect.value = String(selectedEmulatorConfigVersionIndex);
    }
    versionSelect.disabled = false;
  }
  const version = (row.versions || [])[0] || row;
  if (fingerprint) fingerprint.textContent = version.fingerprint || row.fingerprint ? `fingerprint: ${version.fingerprint || row.fingerprint}` : "";
  if (contentNode) contentNode.textContent = row.error ? `[Config read error] ${row.error}` : (version.content || row.content || "");
}
function selectEmulatorConfigVersion(value) {
  const row = emulatorConfigRows[selectedEmulatorConfigIndex || 0];
  if (!row) return;
  const versionIndex = Math.max(0, Math.min((row.versions || []).length - 1, Number(value) || 0));
  selectedEmulatorConfigVersionIndex = versionIndex;
  const version = (row.versions || [])[versionIndex] || row;
  const fingerprint = document.getElementById("emulatorConfigFingerprint");
  const contentNode = document.getElementById("emulatorConfigContent");
  if (fingerprint) fingerprint.textContent = version.fingerprint || row.fingerprint ? `fingerprint: ${version.fingerprint || row.fingerprint}` : "";
  if (contentNode) contentNode.textContent = row.error ? `[Config read error] ${row.error}` : (version.content || row.content || "");
}
function filterEmulatorConfigs(value) {
  const query = String(value || "").trim().toLowerCase();
  const buttons = Array.from(document.querySelectorAll("#emulatorConfigSources .list-group-item"));
  const visible = [];
  buttons.forEach((button) => {
    const matched = !query || button.textContent.toLowerCase().includes(query);
    button.style.display = matched ? "" : "none";
    if (matched) visible.push(button);
  });
  const empty = document.getElementById("emulatorConfigFilterEmpty");
  if (empty) empty.style.display = visible.length ? "none" : "block";
  const selectedVisible = visible.some((button) => Number(button.dataset.configIndex) === selectedEmulatorConfigIndex);
  if (!selectedVisible && visible.length) {
    selectEmulatorConfig(Number(visible[0].dataset.configIndex));
  }
}
async function renderConfigsPage(selectedSource = null, selectedMaxBytes = 131072) {
  setLoading(true, "Loading emulator config sources...");
  const configSourceCatalog = [
    ["batocera", "Batocera Config", "bi-sliders"],
    ["es_systems", "ES Systems", "bi-diagram-3"],
    ["emulationstation", "EmulationStation", "bi-window-stack"],
    ["es_input", "ES Controller Input", "bi-controller"],
    ["retroarch", "RetroArch", "bi-controller"],
    ["mame", "MAME", "bi-joystick"],
    ["dolphin", "Dolphin", "bi-water"],
    ["pcsx2", "PCSX2", "bi-disc"],
    ["rpcs3", "RPCS3", "bi-hdd-stack"],
    ["ppsspp", "PPSSPP", "bi-phone"],
    ["duckstation", "DuckStation", "bi-disc"],
    ["citra", "Citra", "bi-nintendo-switch"],
    ["yuzu", "Yuzu", "bi-controller"],
    ["ryujinx", "Ryujinx", "bi-nintendo-switch"],
    ["cemu", "Cemu", "bi-controller"],
    ["xemu", "Xemu", "bi-xbox"],
    ["xenia", "Xenia", "bi-xbox"],
    ["flycast", "Flycast", "bi-cloud"],
    ["dosbox", "DOSBox", "bi-terminal"],
    ["scummvm", "ScummVM", "bi-compass"],
    ["snes9x", "Snes9x", "bi-controller"],
    ["bsnes", "bsnes", "bi-controller"],
    ["fceux", "FCEUX", "bi-cassette"],
    ["mednafen", "Mednafen", "bi-cassette"],
    ["mgba", "mGBA", "bi-controller"],
    ["wine", "Wine", "bi-cup-straw"],
    ["shadps4", "shadPS4", "bi-playstation"],
    ["themes", "Themes Directory", "bi-palette"],
    ["controllers", "Controllers Config", "bi-usb-symbol"],
  ];
  const catalogMap = new Map(configSourceCatalog.map((item) => [item[0], item]));
  let allowedSourceKeys = configSourceCatalog.map((item) => item[0]);
  try {
    const sourcePayload = await api("/admin/configs/sources");
    if (sourcePayload && Array.isArray(sourcePayload.sources) && sourcePayload.sources.length > 0) {
      allowedSourceKeys = sourcePayload.sources.filter((key) => catalogMap.has(key));
    }
  } catch (_) {
    // Fall back to full list if source scan endpoint is unavailable.
  }
  const configSources = allowedSourceKeys.map((key) => catalogMap.get(key)).filter(Boolean);
  const validSources = new Set(configSources.map(([source]) => source));
  const effectiveSource = validSources.has(selectedSource) ? selectedSource : null;
  const effectiveMaxBytes = clampMaxBytes(selectedMaxBytes);

  titleNode.textContent = "Emulators";
  subtitleNode.textContent = "View emulator config files and detected versions";
  content.innerHTML = `
    <div class="mb-3">
      <button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button>
    </div>
    <div class="row">
      <div class="col-md-3">
        <div class="card log-card">
          <div class="card-header">Emulators</div>
          <div class="list-group list-group-flush" id="configSources">
            ${configSources.map(([source, label, icon]) => `
              <button type="button" class="list-group-item list-group-item-action text-start" data-config-source="${source}" onclick="loadConfig('${source}', this)">
                <i class="bi ${icon} me-2"></i>${label}
              </button>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="col-md-9">
        <div class="card log-card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span id="configTitle">Select an emulator</span>
            <div>
              <label for="maxBytesInput" class="form-label me-2">Max Bytes:</label>
              <select id="maxBytesInput" class="form-select log-lines-select">
                <option value="16384">16 KB</option>
                <option value="65536">64 KB</option>
                <option value="131072">128 KB</option>
                <option value="262144">256 KB</option>
                <option value="524288">512 KB</option>
                <option value="1048576">1 MB</option>
              </select>
              <button class="btn btn-sm btn-outline-primary ms-2" onclick="refreshCurrentConfig()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <pre id="configContent" class="mono bg-dark text-light p-3" style="max-height: 600px; overflow-y: auto; white-space: pre-wrap;">Select an emulator from the left panel to view its config.</pre>
          </div>
        </div>
      </div>
    </div>
  `;
  const maxBytesInput = document.getElementById("maxBytesInput");
  if (maxBytesInput) {
    maxBytesInput.value = String(effectiveMaxBytes);
  }
  if (effectiveSource) {
    const sourceBtn = document.querySelector(`#configSources .list-group-item[data-config-source="${effectiveSource}"]`);
    await loadConfig(effectiveSource, sourceBtn, false);
  } else {
    setLoading(false);
  }
}
async function loadConfig(source, triggerEl = null, updateHash = true) {
  currentConfigSource = source;
  const maxBytes = clampMaxBytes(document.getElementById("maxBytesInput")?.value || "131072");
  const targetHash = `#admin/configs/${encodeURIComponent(source)}?max_bytes=${encodeURIComponent(maxBytes)}`;
  setLoading(true, `Loading ${source} config...`);
  if (updateHash && window.location.hash !== targetHash) {
    setHash(targetHash);
    return;
  }
  try {
    const formatParam = source === "es_systems" ? "&format=xml" : "";
    const data = await api(`/admin/configs/${source}?max_bytes=${maxBytes}${formatParam}`);
    document.getElementById("configTitle").textContent = `${data.source} Config (${data.path})`;
    document.getElementById("configContent").textContent = (data.content || []).join("\n");
    document.querySelectorAll("#configSources .list-group-item").forEach(el => el.classList.remove("active"));
    const activeEl = triggerEl || document.querySelector(`#configSources .list-group-item[data-config-source="${source}"]`);
    if (activeEl) activeEl.classList.add("active");
  } catch (err) {
    showToast(`Error loading config: ${escapeHtml(err.message || "unknown error")}`, "danger");
    document.getElementById("configContent").textContent = "";
  }
  setLoading(false);
}
async function refreshCurrentConfig() {
  if (!currentConfigSource) return;
  const activeSource = document.querySelector("#configSources .list-group-item.active");
  await loadConfig(currentConfigSource, activeSource);
}
async function renderAdminSystemInfoPage() {
  setSearchMode("hidden");
  titleNode.textContent = "System Info";
  subtitleNode.textContent = "Runtime, network, and Batocera details";
  setLoading(true, "Loading system information...");
  try {
    const payload = await api("/admin/system-info?speed=1");
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    const fields = payload.fields || {};
    const metrics = payload.runtime_metrics || {};
    const cpu = metrics.cpu || {};
    const memory = metrics.memory || {};
    const disk = metrics.disk || {};
    const disks = Array.isArray(metrics.disks) && metrics.disks.length ? metrics.disks : [disk];
    const process = metrics.process || {};
    const speed = payload.speed_sample || {};
    const detail = (label, value) => `<div class="asset-detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "n/a")}</strong></div>`;
    const pct = (value) => value === null || value === undefined || value === "" ? "n/a" : `${Number(value).toFixed(1)}%`;
    const numericPct = (value) => Math.max(0, Math.min(100, Number(value || 0)));
    const health = (label, value, display, tone = "info") => `<div class="system-health-row">
      <div class="d-flex justify-content-between gap-2"><span>${escapeHtml(label)}</span><strong>${escapeHtml(display)}</strong></div>
      <div class="progress"><div class="progress-bar bg-${tone}" style="width:${numericPct(value)}%"></div></div>
    </div>`;
    const renderedRows = entries.length
      ? entries.slice(0, 18).map((entry) => detail(entry.key || "", entry.value || "")).join("")
      : `<div class="text-muted">No system information available.</div>`;
    const renderedDisks = disks.map((drive, index) => {
      const label = drive.label || (drive.is_main ? "Main drive" : `Drive ${index + 1}`);
      const tone = numericPct(drive.used_percent) >= 90 ? "danger" : (drive.is_external ? "info" : "primary");
      return health(label, drive.used_percent, `${formatBytes(drive.used_bytes)} / ${formatBytes(drive.total_bytes)} (${pct(drive.used_percent)})`, tone);
    }).join("");
    const diskDetails = disks.map((drive, index) => {
      const label = drive.label || (drive.is_main ? "Main drive" : `Drive ${index + 1}`);
      const location = [drive.path, drive.source, drive.filesystem].filter(Boolean).join(" · ");
      return detail(label, location || "n/a");
    }).join("");

    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap justify-content-between gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">Back to Admin</button>
        <button class="btn btn-outline-primary" onclick="setHash('#admin/system-info')"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header">System Health</div>
        <div class="card-body">
          <div class="row g-3">
            <div class="col-12 col-lg-7">
              ${health("Host CPU", cpu.host_percent, pct(cpu.host_percent), numericPct(cpu.host_percent) >= 85 ? "danger" : "info")}
              ${health("Drone CPU", cpu.process_percent, pct(cpu.process_percent), numericPct(cpu.process_percent) >= 85 ? "danger" : "success")}
              ${health("Memory", memory.used_percent, `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)} (${pct(memory.used_percent)})`, numericPct(memory.used_percent) >= 90 ? "danger" : "warning")}
              ${renderedDisks}
            </div>
            <div class="col-12 col-lg-5">
              <div class="asset-detail-panel h-100">
                <h6>Runtime &amp; Network</h6>
                ${detail("Load average", Array.isArray(cpu.load_average) ? cpu.load_average.map((v) => Number(v).toFixed(2)).join(" / ") : "n/a")}
                ${detail("Process RSS", formatBytes(process.rss_bytes))}
                ${detail("Disk I/O", `${disk.read_bytes_per_second ? `${formatBytes(disk.read_bytes_per_second)}/s read` : "n/a"} · ${disk.write_bytes_per_second ? `${formatBytes(disk.write_bytes_per_second)}/s write` : "n/a"}`)}
                <h6 class="mt-3">Mounted Drives</h6>
                ${diskDetails}
                ${detail("Internet", `${speed.download_mbps ?? "n/a"} Mbps down · ${speed.upload_mbps ?? "n/a"} Mbps up`)}
                ${detail("Latency", speed.latency_ms !== undefined ? `${speed.latency_ms} ms` : "n/a")}
                ${detail("Speed source", speed.source || "n/a")}
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="card log-card">
        <div class="card-header">System Details</div>
        <div class="card-body">
          <div class="row g-3">
            <div class="col-12 col-lg-6">
              <div class="asset-detail-panel h-100">
                <h6>Identity &amp; Network</h6>
                ${detail("Machine ID", fields.machine_id)}
                ${detail("Overmind", fields.overmind_integrated === "yes" ? "Linked" : "Disconnected")}
                ${detail("Network IP", fields.network_ip_address)}
                ${detail("Router IP", fields.router_ip_address)}
                ${detail("Batocera", fields.batocera_version || fields.system)}
              </div>
            </div>
            <div class="col-12 col-lg-6">
              <div class="asset-detail-panel h-100">
                <h6>Hardware</h6>
                ${detail("Model", fields.model)}
                ${detail("Architecture", fields.architecture)}
                ${detail("CPU", fields.cpu_model || fields.cpu_topology)}
                ${renderedRows}
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  } catch (err) {
    showToast(`Failed to load system information: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="mb-3">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button>
      </div>
      <div class="text-muted">System information could not be loaded.</div>
    `;
  } finally {
    setLoading(false);
  }
}
async function loadThemePage(offset = 0) {
  const selected = themeFilterInitialized && !(themeFilterSelectedSystems || []).length ? ["__none__"] : (themeFilterSelectedSystems || []);
  const systemsParam = encodeURIComponent(selected.join(","));
  const url = `/theme/images?limit=${THEME_GALLERY_PAGE_SIZE}&offset=${Math.max(0, offset)}&q=${encodeURIComponent(themeFilterQuery || "")}&systems=${systemsParam}`;
  const data = await api(url);
  renderThemeGallery(data);
}
function _extractInfoField(lines, keys) {
  const lowered = (keys || []).map((k) => String(k).toLowerCase());
  for (const line of lines || []) {
    const s = String(line || "");
    const ls = s.toLowerCase();
    for (const key of lowered) {
      if (ls.includes(key) && s.includes(":")) {
        const value = s.split(":", 2)[1].trim();
        if (value) return value;
      }
    }
  }
  return null;
}
async function loadSystemInfoBar() {
  if (systemInfoLoaded) return;
  const bar = document.getElementById("systemInfoBar");
  const machineNav = document.getElementById("machineIdNav");
  if (!bar) return;
  if (!adminEnabled) {
    bar.innerHTML = "";
    if (machineNav) machineNav.textContent = "Machine ID unavailable";
    systemInfoLoaded = true;
    return;
  }
  try {
    const payload = await api("/admin/system-info");
    const fields = payload.fields || {};
    const lines = payload.lines || [];
    const version = fields.batocera_version || fields.system || _extractInfoField(lines, ["version", "batocera version", "system"]);
    const droneAppVersion = fields.drone_app_version || payload.drone_app_version || "";
    const machineId = fields.machine_id || "";
    const overmindIntegrated = fields.overmind_integrated || "no";
    const chips = [];
    if (droneVersionBadge && droneAppVersion) {
      droneVersionBadge.textContent = droneAppVersion;
      droneVersionBadge.classList.remove("d-none");
    }
    if (version) chips.push(`<span class="badge">Batocera: ${escapeHtml(version)}</span>`);
    if (machineId) chips.push(`<span class="badge">Machine ID: ${escapeHtml(machineId)}</span>`);
    if (overmindIntegrated === "yes") {
      chips.push(`<span class="badge" style="background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)">Overmind: linked</span>`);
    } else {
      chips.push(`<span class="badge" style="background:rgba(239,68,68,0.15);color:#ef4444;border-color:rgba(239,68,68,0.4)">Overmind: disconnected</span>`);
    }
    try {
      const overmindPayload = await api("/admin/integrations/overmind/status");
      const swarmStatus = (overmindPayload.status || {}).swarm_connection_status || "disconnected";
      const swarmConnected = String(swarmStatus).toLowerCase() === "connected";
      chips.push(`<span class="badge" style="background:${swarmConnected ? "rgba(52,211,153,0.15)" : "rgba(239,68,68,0.15)"};color:${swarmConnected ? "#34d399" : "#ef4444"};border-color:${swarmConnected ? "rgba(52,211,153,0.4)" : "rgba(239,68,68,0.4)"}">Swarm: ${swarmConnected ? "Connected" : "Disconnected"}</span>`);
    } catch (_) {
      chips.push(`<span class="badge" style="background:rgba(239,68,68,0.15);color:#ef4444;border-color:rgba(239,68,68,0.4)">Swarm: Disconnected</span>`);
    }
    if (machineNav && machineId) machineNav.textContent = `Machine ID: ${machineId}`;
    if (!chips.length && lines.length) {
      chips.push(`<span class="badge">${escapeHtml(lines[0])}</span>`);
    }
    bar.innerHTML = chips.join("");
  } catch (_) {
    bar.innerHTML = `<span class="badge">System Info Unavailable</span>`;
    if (machineNav) machineNav.textContent = "Machine ID unavailable";
  } finally {
    systemInfoLoaded = true;
  }
}
async function router() {
  clearError();
  try {
    const hash = window.location.hash || "";
    if (!hash.startsWith("#admin/logs/")) {
      stopLogAutoRefresh();
      currentLogSource = null;
    }
    if (!["#admin/integration", "#admin/local-network", "#admin/overmind"].includes(hash)) {
      stopLocalTransfersAutoRefresh();
    }
    document.body.classList.toggle("artwork-page", hash.startsWith("#admin/artwork"));
    if (hash === "#bios") {
      await renderBios();
    } else if (hash === "#theme") {
      await renderThemeGalleryPage();
    } else if (hash === "" || hash === "#" || hash === "#home" || hash === "#help") {
      await renderHelpPage();
    } else if (hash === "#systems") {
      await renderSystemsPage();
    } else if (hash === "#admin") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAdminPage();
    } else if (hash === "#admin/emulators") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderEmulatorsPage();
    } else if (hash.startsWith("#admin/logs/")) {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      const parsed = parseAdminLogsHash(hash);
      if (!parsed) {
        setHash("#admin");
        return;
      }
      await renderLogsPage(parsed.source, parsed.lines);
    } else if (hash === "#admin/gameplay-logs") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderGameplayLogsPage();
    } else if (hash.startsWith("#admin/configs/")) {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      const parsed = parseAdminConfigsHash(hash);
      if (!parsed) {
        setHash("#admin");
        return;
      }
      await renderConfigsPage(parsed.source, parsed.maxBytes);
    } else if (hash === "#admin/system-info") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAdminSystemInfoPage();
    } else if (hash.startsWith("#admin/artwork")) {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      const parsed = parseArtworkHash(hash) || { offset: 0, includeFilesystem: false, fields: ["image", "marquee"], systems: [], q: "", romStatus: "any" };
      await renderMissingArtworkPage(parsed.includeFilesystem, false, parsed.offset, parsed.fields, parsed.systems, parsed.q, parsed.romStatus);
    } else if (hash === "#admin/downloads") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderDownloadsPage();
    } else if (hash === "#admin/asset-cache") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAssetCachePage();
    } else if (hash === "#admin/integration") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderIntegrationPage();
    } else if (["#admin/overmind", "#admin/overmind/actions"].includes(hash)) {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderOvermindIntegrationPage();
    } else if (hash === "#admin/local-network") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderLocalNetworkPage();
    } else if (hash === "#admin/api") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderApiAdminPage();
    } else if (parseSystemRomHash(hash)) {
      const parsed = parseSystemRomHash(hash);
      await renderRomMediaPage(parsed.system, parsed.uniqueId);
    } else if (hash.startsWith("#search-system/")) {
      const rest = hash.substring("#search-system/".length);
      const slashIndex = rest.indexOf("/");
      if (slashIndex > 0) {
        const system = decodeURIComponent(rest.substring(0, slashIndex));
        const q = decodeURIComponent(rest.substring(slashIndex + 1));
        currentSystemContext = system;
        searchInput.value = q;
        await renderSearch(q);
      } else {
        currentSystemContext = null;
        setHash("");
      }
    } else if (hash.startsWith("#search/")) {
      const q = decodeURIComponent(hash.substring("#search/".length));
      currentSystemContext = null;
      await refreshRandomThemeLogo();
      searchInput.value = q;
      await renderSearch(q);
    } else if (parseSystemHash(hash)) {
      const parsed = parseSystemHash(hash);
      await renderSystem(parsed.system, parsed.page);
    } else {
      await renderHelpPage();
    }
  } catch (err) {
    setLoading(false);
    showError(err.message || "Unexpected error");
  }
}
backBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#systems");
});
brandHomeBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#home");
});
biosBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#bios");
});
systemsMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#systems");
});
themeMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#theme");
});
systemInfoMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  if (!adminEnabled) return;
  setHash("#admin/system-info");
});
adminMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  if (!adminEnabled) return;
  setHash("#admin");
});
searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const q = (searchInput.value || "").trim();
  if (!q) return;
  if (currentSystemContext) {
    setHash(`#search-system/${encodeURIComponent(currentSystemContext)}/${encodeURIComponent(q)}`);
  } else {
    setHash(`#search/${encodeURIComponent(q)}`);
  }
});
clearSearchBtn.addEventListener("click", () => {
  searchInput.value = "";
  currentSystemContext = null;
  setHash("#systems");
});
window.addEventListener("hashchange", router);
async function bootstrap() {
  try {
    await api("/admin/configs/sources");
    adminEnabled = true;
  } catch (error) {
    const msg = String(error && error.message ? error.message : "").toLowerCase();
    adminEnabled = !(msg.includes("admin disabled") || msg.includes("request failed: 403"));
  }
  applyAdminVisibility();
  loadSystemInfoBar();
  // Render immediately so UI/menu works even if theme discovery is slow.
  await router();
  try {
    await initializeTheme();
  } catch (_) {
    // Ignore theme failures and continue rendering app.
  }
  // Re-render after theme init so branding/background can apply.
  await router();
}
bootstrap();
