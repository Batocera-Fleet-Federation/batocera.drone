const content = document.getElementById("content");
const backBtn = document.getElementById("backBtn") || {
  classList: { add() {}, remove() {} },
  addEventListener() {},
};
const systemsMenuBtn = document.getElementById("systemsMenuBtn");
const biosBtn = document.getElementById("biosBtn");
const emulatorsMenuBtn = document.getElementById("emulatorsMenuBtn");
const themeMenuBtn = document.getElementById("themeMenuBtn");
const systemInfoMenuBtn = document.getElementById("systemInfoMenuBtn");
const adminMenuBtn = document.getElementById("adminMenuBtn");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const clearSearchBtn = document.getElementById("clearSearchBtn");
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
let currentConfigSource = null;
let emulatorConfigRows = [];
let selectedEmulatorConfigIndex = 0;
let selectedEmulatorConfigVersionIndex = 0;
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
  const label = selectedSet.size ? `${selectedSet.size} selected` : (prefix === "bios" ? "No systems" : "All systems");
  return `
    <div class="dropdown app-checkbox-dropdown">
      <button class="btn btn-outline-primary dropdown-toggle w-100 text-start" type="button" id="${prefix}FilterToggle" data-bs-toggle="dropdown" data-bs-auto-close="outside" aria-expanded="false">${label}</button>
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
              <div class="text-muted"><i class="bi bi-collection-play me-1"></i>ROMs: ${system.rom_count}</div>
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
        <h3 class="h5 mb-0">ROMs <span class="text-muted">(${total})</span></h3>
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
                  <div id="romMd5Hash" class="text-muted small mono mt-1">MD5: loading...</div>
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
    api(`/systems/${encodeURIComponent(system)}/roms/${encodeURIComponent(rom.unique_id)}/md5`)
      .then((data) => {
        const node = document.getElementById("romMd5Hash");
        if (node) node.textContent = `MD5: ${data.md5 || "unavailable"}`;
      })
      .catch(() => {
        const node = document.getElementById("romMd5Hash");
        if (node) node.textContent = "MD5: unavailable";
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
        ? systems.map((system) => `
          <section class="mb-4">
            <h3 class="h5 mb-2"><i class="bi bi-folder2-open me-2"></i>${escapeHtml(system === "_root" ? "root" : system)} <span class="text-muted">(${grouped[system].length})</span></h3>
            <div class="row g-3">
              ${grouped[system].map((item) => `
                <div class="col-12 col-md-6 col-xl-3">
                  <div class="card shadow-sm tile h-100">
                    <div class="card-body">
                      <div class="fw-semibold mb-2">${escapeHtml(item.name)}</div>
                      ${item.byte_count !== undefined ? `<div class="text-muted small mono mb-3">${formatBytesToMb(item.byte_count)}</div>` : ""}
                      ${item.md5 ? `<div class="text-muted small mono mb-3">MD5: ${escapeHtml(item.md5)}</div>` : ""}
                      ${
                        item.is_downloadable === false
                          ? `<button class="btn btn-secondary btn-sm" type="button" disabled><i class="bi bi-slash-circle me-1"></i>Download Disabled</button>`
                          : `<a class="btn btn-primary btn-sm" href="${biosDownloadUrl(item.unique_id)}"><i class="bi bi-download me-1"></i>Download BIOS</a>`
                      }
                    </div>
                  </div>
                </div>
              `).join("")}
            </div>
          </section>
        `).join("")
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
async function renderAdminPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading admin panel...");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  renderAdminMenu();
  setLoading(false);
}
async function renderAdminMenu() {
  titleNode.textContent = "Admin Panel";
  subtitleNode.textContent = "System administration";
  content.innerHTML = `
    <div class="row">
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/logs/es_launch_stdout?lines=200')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-journal-text me-2"></i>Logs</h5>
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
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/downloads')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-cloud-arrow-down me-2"></i>Downloads</h5>
            <p class="card-text">Monitor active and queued ROM/file transfers for this Drone.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/asset-cache')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-database-check me-2"></i>Asset Cache</h5>
            <p class="card-text">Track ROM, BIOS, artwork cache progress and Overmind upload state.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/overmind')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-diagram-3 me-2"></i>Overmind Integration</h5>
            <p class="card-text">Configure batocera.overmind and review processed actions.</p>
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
    showToast(`Drone update downloaded. Restarting app process... (${Math.round((payload.duration_ms || 0) / 1000)}s)`, "success", 10000);
  } catch (error) {
    dismissToast(toast);
    showToast(`Drone update failed: ${escapeHtml(error.message || "unknown error")}`, "danger", 8000);
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

function renderDownloadRows(rows, allowCancel = true) {
  if (!rows.length) return '<div class="themed-empty">No downloads in this group.</div>';
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table download-table">
    <thead><tr><th>Status</th><th>Source</th><th>File</th><th>Progress</th><th>Speed</th><th>Started</th><th></th></tr></thead>
    <tbody>${rows.map(row => {
      const pct = Number(row.percentage || 0);
      const active = ["queued", "downloading"].includes(String(row.status || ""));
      const statusClass = row.status === "failed" ? "danger" : row.status === "completed" ? "success" : row.status === "cancelled" ? "secondary" : row.status === "downloading" ? "info" : "primary";
      const filePath = row.file_path || row.relative_path || row.rom_name || "";
      const fileType = row.file_type || "ROM";
      const errorText = row.error_message || row.failure_reason || "";
      return `<tr>
        <td><span class="badge text-bg-${statusClass}">${escapeHtml(row.status || "queued")}</span>${row.queue_position ? `<div class="download-meta">Queue #${row.queue_position}</div>` : ""}</td>
        <td class="small mono">${escapeHtml(row.source_drone_id || "n/a")}</td>
        <td>
          <div class="download-file">${escapeHtml(filePath)}</div>
          <div class="download-meta">${escapeHtml(fileType)}${row.system ? ` · ${escapeHtml(row.system)}` : ""}${row.rom_md5 ? ` · md5 ${escapeHtml(row.rom_md5)}` : ""}</div>
          ${errorText ? `<div class="text-danger small">${escapeHtml(errorText)}</div>` : ""}
        </td>
        <td style="min-width:190px"><div class="progress"><div class="progress-bar" style="width:${Math.max(0, Math.min(100, pct))}%"></div></div><div class="download-meta">${formatBytes(row.downloaded_bytes || row.bytes_transferred)} / ${formatBytes(row.total_bytes || row.file_size)} · ${pct.toFixed(1)}%</div></td>
        <td class="small">${row.transfer_speed_bps ? `${formatBytes(row.transfer_speed_bps)}/s` : ""}</td>
        <td class="download-meta">${escapeHtml(row.started_at || row.download_started_at || row.created_at || "")}</td>
        <td>${allowCancel && active ? `<button class="btn btn-sm btn-outline-danger" title="Cancel download" aria-label="Cancel download" onclick="cancelDroneDownload('${escapeHtml(row.job_id || row.id)}')"><i class="bi bi-x-circle"></i></button>` : ""}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

async function renderDownloadsPage() {
  currentSystemContext = null;
  setSearchMode("hidden");
  setLoading(true, "Loading downloads...");
  clearSystemTheme();
  titleNode.textContent = "Downloads";
  subtitleNode.textContent = "One active transfer at a time on this Drone";
  try {
    const payload = await api("/admin/downloads");
    const active = payload.active || [];
    const queued = payload.queued || [];
    const recent = payload.recent || [];
    content.innerHTML = `
      <div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin')">Back to Admin</button></div>
      <div class="card log-card mb-3"><div class="card-body py-3">
        <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
          <div><strong>${escapeHtml(payload.target_drone_id || "This Drone")}</strong><div class="small text-muted">Concurrency limit: one active download per target Drone</div></div>
          <button class="btn btn-sm btn-outline-primary" title="Refresh downloads" aria-label="Refresh downloads" onclick="renderDownloadsPage()"><i class="bi bi-arrow-repeat"></i></button>
        </div>
        <h6>Active</h6>${renderDownloadRows(active)}
        <h6 class="mt-3">Queued</h6>${renderDownloadRows(queued)}
        <h6 class="mt-3">Recent</h6>${renderDownloadRows(recent, false)}
        ${active.length || queued.length || recent.length ? "" : '<div class="empty-state">No active or queued downloads.</div>'}
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
  await renderDownloadsPage();
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
    const counts = payload.counts || {};
    const pending = payload.pending_changes || {};
    const boolText = (value) => value ? "yes" : "no";
    const dateText = (value) => value ? new Date(value).toLocaleString() : "n/a";
    const row = (label, value) => `<div class="system-info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value === undefined || value === null || value === "" ? "n/a" : value)}</strong></div>`;
    const pendingTotal = Number(pending.total || 0);
    const statusClass = payload.active ? "text-bg-primary" : payload.needs_upload ? "text-bg-warning" : payload.uploaded ? "text-bg-success" : "text-bg-secondary";
    const statusText = payload.active ? "Scanning" : payload.needs_upload ? "Needs Upload" : payload.uploaded ? "Uploaded" : "Waiting";
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">Back to Admin</button>
        <button class="btn btn-outline-primary" onclick="setHash('#admin/asset-cache')">Refresh</button>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
          <span>Cache Status</span>
          <span class="badge ${statusClass}">${escapeHtml(statusText)}</span>
        </div>
        <div class="card-body">
          <div class="system-info-grid">
            ${row("Poller", payload.poller_enabled ? `enabled (${payload.poll_seconds}s)` : "disabled")}
            ${row("Active", boolText(payload.active))}
            ${row("Complete", boolText(payload.complete))}
            ${row("Dirty", boolText(payload.dirty))}
            ${row("Full refresh", boolText(payload.full_refresh_pending))}
            ${row("Needs upload", boolText(payload.needs_upload))}
            ${row("Last scan", dateText(payload.last_full_scan_at))}
            ${row("Last upload", dateText(payload.last_successful_upload_at))}
            ${row("Checkpoint", dateText(payload.scan_checkpoint_at))}
            ${row("Cache path", payload.path)}
          </div>
        </div>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header">Cached Assets</div>
        <div class="card-body">
          <div class="system-info-grid">
            ${row("Systems", Number(counts.systems || 0).toLocaleString())}
            ${row("ROMs", Number(counts.roms || 0).toLocaleString())}
            ${row("BIOS", Number(counts.bios || 0).toLocaleString())}
            ${row("Artwork", Number(counts.artwork || 0).toLocaleString())}
            ${row("Total", Number(counts.total || 0).toLocaleString())}
            ${row("Pending changes", pendingTotal.toLocaleString())}
          </div>
        </div>
      </div>
      <div class="card log-card">
        <div class="card-header">Pending Upload Details</div>
        <div class="card-body">
          <div class="system-info-grid">
            ${row("ROM upserts", Number(pending.roms || 0).toLocaleString())}
            ${row("BIOS upserts", Number(pending.bios || 0).toLocaleString())}
            ${row("Artwork upserts", Number(pending.artwork || 0).toLocaleString())}
            ${row("ROM deletes", Number(pending.deleted_roms || 0).toLocaleString())}
            ${row("BIOS deletes", Number(pending.deleted_bios || 0).toLocaleString())}
            ${row("Artwork deletes", Number(pending.deleted_artwork || 0).toLocaleString())}
          </div>
        </div>
      </div>
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
      <span class="badge text-bg-secondary">ROMs: ${total}</span>
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
        <span class="badge text-bg-secondary">ROMs: ${total}</span>
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
async function renderOvermindIntegrationPage() {
  titleNode.textContent = "Overmind Integration";
  subtitleNode.textContent = "Configure Overmind action polling and review returned action data";
  content.innerHTML = `
    <div class="mb-3">
      <button class="btn btn-outline-secondary" onclick="setHash('#admin')">← Back to Admin</button>
    </div>
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
    statusEl.innerHTML = `
      <div class="d-flex flex-wrap gap-2 mb-2">
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
      ${swarm.length ? `<div class="mt-3"><strong>Last Swarm Snapshot</strong>${swarm.map((drone) => {
        const checks = peerChecks.filter((item) => String(item.target_drone_id || "") === String(drone.drone_id || drone.device_id || ""));
        const latest = checks[0] || {};
        const passed = latest.status === "pass";
        return `<div class="mt-2 p-2 rounded border" style="border-color:var(--admin-border)!important;background:rgba(31,42,68,.45)">
          <div class="d-flex justify-content-between gap-2"><span>${escapeHtml(drone.name || drone.hostname || drone.device_name || drone.drone_id || "Drone")}</span><span class="badge ${passed ? "text-bg-success" : "text-bg-danger"}">${latest.status ? (passed ? "RESOLVED" : "FAILED") : "unchecked"}</span></div>
          <div class="small text-muted mono">${escapeHtml(drone.drone_id || drone.device_id || "")}</div>
          <div class="small text-muted">Public IP: ${escapeHtml(drone.public_ip || "n/a")}</div>
          <div class="small text-muted">Address: ${escapeHtml(latest.target_address || drone.public_reachable_url || drone.public_ip || drone.local_ip || "n/a")}</div>
          <div class="small text-muted">Checked: ${escapeHtml(latest.checked_at || "n/a")}</div>
          ${latest.failure_reason ? `<div class="small text-danger">${escapeHtml(latest.failure_reason)}</div>` : ""}
        </div>`;
      }).join("")}</div>` : ""}
    `;
    urlInput.value = payload.overmind_url || "https://www.batocera-swarm.com";
    droneNameInput.value = payload.drone_name || "";
    claimEmailInput.value = payload.overmind_email || "";
  }

  async function loadStatus() {
    const payload = await api("/admin/integrations/overmind/status");
    renderStatus(payload);
  }

  function renderActions(actions) {
    actionsBody.innerHTML = actions.length ? `
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
            ${actions.map(action => `
              <tr>
                <td>${escapeHtml(action.processed_at || "n/a")}</td>
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
    ` : `<div class="themed-empty">No processed actions yet.</div>`;
  }

  async function loadActions() {
    const payload = await api("/admin/integrations/overmind/actions");
    renderActions(payload.actions || []);
  }

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

  setLoading(true, "Loading Overmind status...");
  try {
    await loadStatus();
    await loadActions();
  } catch (err) {
    showToast(escapeHtml(err.message || "Failed to load Overmind integration"), "danger");
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
  const logSources = [
    ["drone_stdout", "Drone Stdout", "bi-file-text"],
    ["drone_stderr", "Drone Stderr", "bi-bug"],
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
      <div class="col-md-3">
        <div class="card log-card">
          <div class="card-header">Log Sources</div>
          <div class="list-group list-group-flush" id="logSources">
            ${logSources.map(([source, label, icon]) => `
              <button type="button" class="list-group-item list-group-item-action text-start" data-log-source="${source}" onclick="loadLog('${source}', this)">
                <i class="bi ${icon} me-2"></i>${label}
              </button>
            `).join("")}
          </div>
        </div>
      </div>
      <div class="col-md-9">
        <div class="card log-card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span id="logTitle">Select a log source</span>
            <div>
              <label for="linesInput" class="form-label me-2">Lines:</label>
              <select id="linesInput" class="form-select log-lines-select">
                <option value="100">100</option>
                <option value="200">200</option>
                <option value="500">500</option>
                <option value="1000">1000</option>
                <option value="2000">2000</option>
                <option value="5000">5000</option>
              </select>
              <button class="btn btn-sm btn-outline-primary ms-2" onclick="refreshCurrentLog()">Refresh</button>
            </div>
          </div>
          <div class="card-body">
            <pre id="logContent" class="mono bg-dark text-light p-3" style="max-height: 600px; overflow-y: auto; white-space: pre-wrap;">Select a log source from the left panel to view its contents.</pre>
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
}
async function loadLog(source, triggerEl = null, updateHash = true) {
  currentLogSource = source;
  const lines = clampLogLines(document.getElementById("linesInput")?.value || "200");
  const targetHash = `#admin/logs/${encodeURIComponent(source)}?lines=${encodeURIComponent(lines)}`;
  if (updateHash && window.location.hash !== targetHash) {
    setHash(targetHash);
    return;
  }
  setLoading(true, `Loading ${source} logs...`);
  try {
    const data = await api(`/admin/logs/${source}?lines=${lines}`);
    document.getElementById('logTitle').textContent = `${data.source} Log (${data.path})`;
    document.getElementById('logContent').textContent = data.content.join('\n');
    document.querySelectorAll('#logSources .list-group-item').forEach(el => el.classList.remove('active'));
    const activeEl = triggerEl || document.querySelector(`#logSources .list-group-item[data-log-source="${source}"]`);
    if (activeEl) activeEl.classList.add('active');
  } catch (err) {
    showToast(`Error loading log: ${escapeHtml(err.message || "unknown error")}`, "danger");
    document.getElementById('logContent').textContent = "";
  }
  setLoading(false);
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
              <span>Emulators</span>
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
  try {
    await loadSelectedEmulatorConfigContent(row);
  } catch (err) {
    row.error = err.message || "Failed to load config";
    row.contentLoaded = true;
  }
  if (path) path.textContent = row.root || row.path || "";
  if (versionSelect) {
    versionSelect.innerHTML = (row.versions || []).map((version, versionIndex) => {
      const stamp = version.collected_at ? new Date(version.collected_at).toLocaleString() : `Version ${versionIndex + 1}`;
      const hash = version.fingerprint ? ` ${String(version.fingerprint).slice(0, 8)}` : "";
      return `<option value="${versionIndex}">${escapeHtml(stamp + hash)}</option>`;
    }).join("");
    versionSelect.value = "0";
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
    const process = metrics.process || {};
    const speed = payload.speed_sample || {};
    const row = (label, value) => `<div class="system-info-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "n/a")}</strong></div>`;
    const pct = (value) => value === null || value === undefined || value === "" ? "n/a" : `${Number(value).toFixed(1)}%`;
    const renderedRows = entries.length
      ? entries.slice(0, 18).map((entry) => row(entry.key || "", entry.value || "")).join("")
      : `<div class="text-muted">No system information available.</div>`;

    content.innerHTML = `
      <div class="mb-3">
        <button class="btn btn-outline-secondary" onclick="setHash('#admin')">Back to Admin</button>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
          <span>Runtime Metrics</span>
          <button class="btn btn-sm btn-outline-primary" onclick="setHash('#admin/system-info')">Refresh</button>
        </div>
        <div class="card-body">
          <div class="system-info-grid">
            ${row("CPU host", pct(cpu.host_percent))}
            ${row("Drone CPU", pct(cpu.process_percent))}
            ${row("Load", Array.isArray(cpu.load_average) ? cpu.load_average.map((v) => Number(v).toFixed(2)).join(" / ") : "n/a")}
            ${row("Memory", `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)} (${pct(memory.used_percent)})`)}
            ${row("Process RSS", formatBytes(process.rss_bytes))}
            ${row("Disk", `${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)} (${pct(disk.used_percent)})`)}
            ${row("Disk read", disk.read_bytes_per_second ? `${formatBytes(disk.read_bytes_per_second)}/s` : "n/a")}
            ${row("Disk write", disk.write_bytes_per_second ? `${formatBytes(disk.write_bytes_per_second)}/s` : "n/a")}
            ${row("Download", speed.download_mbps !== undefined ? `${speed.download_mbps} Mbps` : "n/a")}
            ${row("Upload", speed.upload_mbps !== undefined ? `${speed.upload_mbps} Mbps` : "n/a")}
            ${row("Latency", speed.latency_ms !== undefined ? `${speed.latency_ms} ms` : "n/a")}
            ${row("Speed source", speed.source || "n/a")}
          </div>
        </div>
      </div>
      <div class="card log-card">
        <div class="card-header">System Details</div>
        <div class="card-body">
          <div class="system-info-grid">
            ${row("Machine ID", fields.machine_id)}
            ${row("Overmind", fields.overmind_integrated === "yes" ? "linked" : "disconnected")}
            ${row("Batocera", fields.batocera_version || fields.system)}
            ${row("Model", fields.model)}
            ${row("Architecture", fields.architecture)}
            ${row("CPU", fields.cpu_model || fields.cpu_topology)}
            ${row("Network IP", fields.network_ip_address)}
            ${row("Router IP", fields.router_ip_address)}
            ${renderedRows}
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
  const systemsParam = encodeURIComponent((themeFilterSelectedSystems || []).join(","));
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
    const machineId = fields.machine_id || "";
    const overmindIntegrated = fields.overmind_integrated || "no";
    const chips = [];
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
    document.body.classList.toggle("artwork-page", hash.startsWith("#admin/artwork"));
    if (hash === "#bios") {
      await renderBios();
    } else if (hash === "#theme") {
      await renderThemeGalleryPage();
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
    } else if (hash === "#admin/overmind") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderOvermindIntegrationPage();
    } else if (hash === "#admin/overmind/actions") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderOvermindIntegrationPage();
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
      currentSystemContext = null;
      setSearchMode("global");
      setLoading(true, "Loading systems...");
      clearSystemTheme();
      const data = await getSystemsData();
      renderSystems(data);
      setLoading(false);
      refreshRandomThemeLogo().catch(() => {});
    }
  } catch (err) {
    setLoading(false);
    showError(err.message || "Unexpected error");
  }
}
backBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("");
});
biosBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#bios");
});
systemsMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("");
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
  setHash("");
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
