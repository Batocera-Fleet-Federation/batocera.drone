const content = document.getElementById("content");
const backBtn = document.getElementById("backBtn") || {
  classList: { add() {}, remove() {} },
  addEventListener() {},
};
const assetsMenuBtn = document.getElementById("assetsMenuBtn");
const brandHomeBtn = document.getElementById("brandHomeBtn");
const controlsMenuBtn = document.getElementById("controlsMenuBtn");
const swarmMenuBtn = document.getElementById("swarmMenuBtn");
const adminMenuBtn = document.getElementById("adminMenuBtn");
const apiAccessBtn = document.getElementById("apiAccessBtn");
const notificationsBellWrap = document.getElementById("notificationsBellWrap");
const notificationsBellBtn = document.getElementById("notificationsBellBtn");
const droneVersionBadge = document.getElementById("droneVersionBadge");
const titleNode = document.querySelector(".h3.mb-1");
const subtitleNode = document.getElementById("pageSubtitle");
const API_BASE = "/v1/api";

// Stamp each `table.bff-stack` cell with its column header so the CSS can render a
// label:value stacked card per row on phone widths (see drone.css .bff-stack).
function decorateStackTables(root) {
  const scope = root || document;
  scope.querySelectorAll("table.bff-stack").forEach((table) => {
    const headers = Array.from(table.querySelectorAll("thead th")).map((th) => th.textContent.trim());
    if (!headers.length) return;
    table.querySelectorAll("tbody tr").forEach((tr) => {
      Array.from(tr.children).forEach((td, index) => {
        if (td.colSpan && td.colSpan > 1) return; // full-width/empty-state rows
        if (index < headers.length && !td.hasAttribute("data-label")) {
          td.setAttribute("data-label", headers[index]);
        }
      });
    });
  });
}

function setupStackTables() {
  const target = content || document.body;
  if (!target) return;
  let scheduled = false;
  const observer = new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      decorateStackTables(target);
    });
  });
  observer.observe(target, { childList: true, subtree: true });
  decorateStackTables(target);
}

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
const ARTWORK_PAGE_SIZE = 200;
const GAMELIST_EDIT_FIELDS = [
  "name", "sortname", "desc", "genre", "developer", "publisher", "releasedate",
  "players", "rating", "favorite", "hidden", "kidgame", "adult",
  "image", "thumbnail", "marquee", "fanart", "boxart", "video"
];
// Systems Browse (the games-with-images grid, GET /roms): unlike Movies,
// a ROM library can be far too large to load in one shot (some devices have
// ~300 systems, individual systems can run into the thousands of ROMs), so
// this page is server-paginated (SYSTEMS_EXPLORE_PAGE_SIZE per page) rather
// than fetching everything up front the way moviesAllRows does. The System
// panel narrows to one system at a time (like Movies' Type filter); Category
// (genre) counts are computed server-side, scoped to whatever System/search
// filter is currently active -- see list_rom_genre_counts.
const SYSTEMS_EXPLORE_PAGE_SIZE = 200;
const SYSTEMS_EXPLORE_TOP_SYSTEM_COUNT = 7;
const SYSTEMS_EXPLORE_TOP_CATEGORY_COUNT = 7;
// Category noise reduction: a device's scraped genre data commonly has a
// long tail of 1-4-item variants (typos, alternate scraper phrasings, ...)
// alongside a real set of dominant categories -- hiding the tail only once
// a clearly dominant (>=50) category exists avoids ever hiding anything on
// a library too small/uniform for that split to mean anything. Bypassed
// entirely while the Category search box has text, since that's a
// deliberate lookup, not passive browsing.
const SYSTEMS_EXPLORE_CATEGORY_MIN_COUNT = 5;
const SYSTEMS_EXPLORE_CATEGORY_DOMINANT_THRESHOLD = 50;
let systemsExploreAllSystems = [];
let systemsExploreShowAllSystems = false;
let systemsExploreShowAllCategories = false;
let systemsExploreSystemFilterQuery = "";
let systemsExploreCategoryFilterQuery = "";
let systemsExploreSelectedSystem = "";
let systemsExploreSelectedGenre = "";
let systemsExploreSearchQuery = "";
let systemsExploreBrowserPlayOnly = false;
// Populated once per page load from browserPlaySupportedSystems() (see
// browserPlayUrl) so the System sidebar list can filter synchronously --
// that call itself is async/cached, but list rendering isn't.
let systemsExploreBrowserPlayMap = {};
let systemsExploreRoms = [];
let systemsExploreGenreCounts = [];
let systemsExploreTotal = 0;
let systemsExploreHasMore = false;
let systemsExploreLoadingMore = false;
// BIOS browsing lives inside the Systems Browse sidebar as a pinned pseudo-
// "system" entry (selected the same way a real system is, via
// setSystemsExploreSystem) rather than a separate page -- it has no genre
// facet and its rows have no artwork, so it gets its own item list/render
// path, but reuses the same System-panel selection UI and the same
// total/hasMore/loadingMore paging state as the ROM path (only one of the
// two is ever active at a time).
const SYSTEMS_EXPLORE_BIOS_KEY = "__bios__";
let systemsExploreBiosTotal = 0;
let systemsExploreBiosItems = [];
// Duplicate-game finder: an icon-only toggle next to the search box (see
// renderSystemsExplorePage) that swaps the grid from paginated ROM cards to
// a flat list of duplicate groups within whatever System/Category/search
// filter is currently active -- fetched in full (no "Show more" paging,
// unlike the ROM grid) since a duplicates scan is already scoped down to
// just the games that matched, not the whole library.
let systemsExploreDuplicatesMode = false;
let systemsExploreDuplicateGroups = [];
// Movies Browse (own top-level page, see renderMovieExplorerPage) -- the
// whole set loads once (movie libraries are far smaller than ROM sets) and
// is grouped/filtered client-side.
let moviesAllRows = [];
// Music Browse (own top-level page, see renderMusicExplorerPage) -- same
// whole-set-loads-once-then-groups-client-side shape as moviesAllRows.
let musicAllRows = [];
let musicExplorerGenreFilter = "";
let musicExplorerShowAllGenres = false;
const MUSIC_EXPLORE_TOP_GENRE_COUNT = 7;
let musicExplorerArtistFilter = "";
let musicExplorerShowAllArtists = false;
const MUSIC_EXPLORE_TOP_ARTIST_COUNT = 7;
let musicExplorerLikedFilter = false;
const MUSIC_EXPLORE_PAGE_SIZE = 200;
let musicExploreDisplayLimit = MUSIC_EXPLORE_PAGE_SIZE;
// Scroll position of the Movies/Systems Browse list views, keyed by a fixed
// bucket name (not the literal hash -- systemsExploreHash carries a query
// string that varies with the current search/filter/system selection, so
// bucketing collapses all of those back to one slot per view instead of
// fragmenting by every filter combination). Captured by router() the moment
// either is navigated away from (e.g. into a movie/show/ROM detail page) and
// restored when landing back on it, so "Back" doesn't dump the user at the
// top of a long list. Deliberately an exception to this app's usual
// reset-scroll-on-nav convention, scoped to just these two views.
let movieListScrollPositions = {};
let lastRenderedHash = "";
function movieListScrollBucket(hash) {
  const value = String(hash || "");
  // Browse is the only Movies/Systems view now -- the bare and "/explore"
  // forms render the same page, so they share one scroll-position bucket
  // apiece instead of the four separate ones this used to need back when
  // each also had its own tree view.
  if (value === "#movies" || value.startsWith("#movies/explore")) return "#movies/explore";
  if (value === "#systems" || value.startsWith("#systems/explore") || value.startsWith("#systems?")) return "#systems/explore";
  if (value === "#music" || value.startsWith("#music/explore")) return "#music/explore";
  return null;
}
// Movie Explorer's category sidebar (see renderMovieExplorerPage): "all" |
// "movie" | "episode" for type, "" (no filter) or a genre string for genre --
// both reset to their defaults each time the Explorer page is (re-)opened,
// same as the search box already does.
let movieExplorerTypeFilter = "all";
let movieExplorerGenreFilter = "";
// Same top-N-unless-expanded "Show more"/"Show less" pattern as Systems
// Browse's own System/Category lists (see SYSTEMS_EXPLORE_TOP_*_COUNT).
const MOVIE_EXPLORE_TOP_GENRE_COUNT = 7;
let movieExplorerShowAllGenres = false;
// Duplicate-movie/show finder: an icon-only toggle next to the search box
// (see renderMovieExplorerPage) that swaps the grid from posters to a flat
// list of duplicate groups within whatever Type/Genre/search filter is
// currently active. Unlike the rest of the Explorer (client-side grouping
// over the already-fetched moviesAllRows), duplicate grouping happens
// server-side (GET /admin/movies/duplicates) since it needs its own
// title/quality-tag parsing, not the show/episode grouping this page
// already does.
let movieExplorerDuplicatesMode = false;
let movieExplorerDuplicateGroups = [];
// The Explorer still fetches the whole (small, ~thousands-not-millions)
// movies library in one shot -- unlike Systems Browse it needs the complete
// set client-side anyway to group episodes into show cards and compute
// accurate facet counts (see movieExplorerTypeCount/movieExplorerGenreCount)
// -- but only renders this many cards at a time, growing via "Show more",
// so a large filtered result doesn't dump thousands of DOM nodes/images in
// one paint. Reset to the page size on every filter/search change.
const MOVIE_EXPLORE_PAGE_SIZE = 200;
let movieExploreDisplayLimit = MOVIE_EXPLORE_PAGE_SIZE;
let filterDropdownGlobalCloseBound = false;
let filterDropdownState = {};
let themeFilterInitialized = false;
let currentLogSource = null;
let logRefreshTimer = null;
let logRefreshInFlight = false;
let transfersTimer = null;
let transfersInFlight = false;
let torrentsTimer = null;
let torrentsInFlight = false;
let torrentsLastPayload = null;
let configBackupsTimer = null;
let configBackupsInFlight = false;
let configBackupsLastPayload = [];
let movieBulkScrapeTimer = null;
let movieBulkScrapeInFlight = false;
let movieBulkScrapeWasRunning = false;
// The open matched/skipped/failed breakdown panel on the admin Movies bulk
// scrape card (see toggleMovieBulkScrapeBreakdown) -- null when closed.
let movieBulkScrapeBreakdownStatus = null;
let movieBulkScrapeBreakdownOffset = 0;
const MOVIE_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE = 50;
// True from the moment "Stop" is clicked until the job's status actually
// leaves "running" -- keeps the Stop button showing "Stopping..." across
// the 2s poll ticks that land before the running job reaches its next
// per-candidate stop-check, instead of flickering back to a clickable
// "Stop" state on every tick in between (see patchMovieBulkScrapeLive).
let movieBulkScrapeStopRequested = false;
let musicBulkScrapeTimer = null;
let musicBulkScrapeInFlight = false;
let musicBulkScrapeWasRunning = false;
let musicBulkScrapeBreakdownStatus = null;
let musicBulkScrapeBreakdownOffset = 0;
const MUSIC_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE = 50;
let musicBulkScrapeStopRequested = false;
let swarmDronesById = {};
const SWARM_DATA_CACHE_TTL_MS = 30000;
let swarmOverviewCache = null;
let swarmOverviewCachedAt = 0;
let swarmOverviewPromise = null;
let tailnetDiscoveryCache = null;
let tailnetDiscoveryCachedAt = 0;
let tailnetDiscoveryPromise = null;
// This Drone's own configured peer ROM references (NFS-preferred, SMB fallback),
// keyed by peer_id -- populated by renderSwarmPage(), read by
// renderSwarmDroneCard() so each peer's card can show whether it's currently
// referenced and by the system-info pill in loadSystemInfoBar().
let swarmNetworkSharesByPeer = {};
let vpnTimer = null;
let vpnInFlight = false;
let smtpTimer = null;
let smtpInFlight = false;
let notificationsPollTimer = null;
let notificationsDropdownOpen = false;
let currentConfigSource = null;
let emulatorConfigRows = [];
let selectedEmulatorConfigIndex = 0;
let selectedEmulatorConfigVersionIndex = 0;
let emulatorConfigSelectionRequestId = 0;
let emulatorConfigTreeExpanded = new Set();
let artworkCurrentOffset = 0;
let artworkIncludeFilesystem = false;
let artworkSelectedFields = ["image", "marquee"];
let artworkSelectedSystems = [];
let artworkFilterQuery = "";
let artworkRomStatus = "any";
let artworkFilterDebounceTimer = null;
// Bumped by every renderMissingArtworkPage()/refreshArtworkResults() call --
// the gamelist scan those await can take several seconds on a large ROM
// library, so if the user navigates away (or re-enters the tab, firing a
// second scan) before it resolves, the older call's own eventual completion
// must not overwrite whatever is now on screen. Same local-token idiom as
// emulatorConfigSelectionRequestId, scoped to this one feature rather than
// router()'s page-navigation-wide routerNavToken, since a same-page double
// fetch (e.g. Refresh clicked twice, or leaving and re-entering the tab)
// needs the same guard without an intervening router() navigation.
let artworkRenderRequestId = 0;
let systemsCache = null;
let systemRomCache = {};
let systemInfoLoaded = false;
let adminEnabled = true;
let loadingToastEl = null;
let currentUsername = "";
// Bumped by every router() call, see router()'s staleness self-heal below --
// fast repeat nav clicks fire hashchange faster than the in-flight page's own
// awaited fetch/render can finish, so an older, slower render can otherwise
// finish last and overwrite a newer page's already-rendered content/title.
let routerNavToken = 0;
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
function showVideoLightbox(url, title = "") {
  const videoUrl = appendCacheBust(url);
  const overlay = document.createElement("div");
  overlay.className = "image-lightbox-overlay";
  overlay.innerHTML = `<button class="image-lightbox-close">&times;</button><video src="${escapeHtml(videoUrl)}" class="lightbox-video" controls autoplay aria-label="${escapeHtml(title)}"></video>`;
  const video = overlay.querySelector("video");
  video.addEventListener("error", () => {
    video.replaceWith(Object.assign(document.createElement("div"), { className: "text-light p-4", textContent: "Video could not be loaded" }));
  });
  const close = () => {
    video.pause();
    video.removeAttribute("src");
    video.load();
    overlay.remove();
    document.removeEventListener("keydown", escHandler);
  };
  function escHandler(ev) { if (ev.key === "Escape") close(); }
  overlay.querySelector(".image-lightbox-close").addEventListener("click", close);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", escHandler);
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
function applyAdminVisibility() {
  const adminLinks = [adminMenuBtn, controlsMenuBtn, swarmMenuBtn, apiAccessBtn, notificationsBellWrap].filter(Boolean);
  if (adminEnabled) {
    adminLinks.forEach((link) => link.classList.remove("d-none"));
    startNotificationsPoll();
  } else {
    adminLinks.forEach((link) => link.classList.add("d-none"));
    stopNotificationsPoll();
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
function jsAttr(value) {
  return escapeHtml(JSON.stringify(value));
}
function _apiRequestUrl(url) {
  if (url.startsWith("http://") || url.startsWith("https://")) return url;
  return `${API_BASE}${url}`;
}
let _sessionExpiredToastShown = false;
async function _handleApiUnauthorized(res, retry) {
  // This Drone's own session cookie expired or was never set -- reloading
  // into the login page is the only recovery, but it must not happen
  // silently out from under the user. A background poll (e.g. the
  // notifications badge's 20s timer, which runs on every page) can be the
  // one that first notices an expired session -- on mobile in particular,
  // where a backgrounded/locked tab often has its session cookie evicted by
  // the time it's foregrounded again -- and an unconditional reload() right
  // then would silently wipe out whatever the user was mid-typing (a search
  // box, a form). Surface it and let them reload when they're ready instead.
  if (!_sessionExpiredToastShown) {
    _sessionExpiredToastShown = true;
    showToast(`Your session has expired. <a href="#" onclick="window.location.reload();return false;" class="alert-link">Reload</a> to log in again.`, "danger", null);
  }
  throw new Error("Authentication required");
}
async function api(url) {
  const res = await fetch(_apiRequestUrl(url), { credentials: "include" });
  if (res.status === 401) {
    return _handleApiUnauthorized(res, () => api(url));
  }
  if (!res.ok) {
    let msg = `Request failed: ${res.status}`;
    try {
      const data = await res.json();
      // Most handlers return {"error": "..."}; a few (e.g. VPN connect)
      // return {"errors": [...]} instead -- without this fallback those
      // calls threw a bare "Request failed: 400" and silently discarded the
      // actual reason (e.g. "Use --help for more information." from a
      // failed openvpn invocation), which is exactly what it looked like.
      if (data.error) msg = data.error;
      else if (Array.isArray(data.errors) && data.errors.length) msg = data.errors.join(" ");
    } catch (_) {}
    throw new Error(msg);
  }
  return await res.json();
}
async function apiPost(url, payload) {
  const res = await fetch(_apiRequestUrl(url), {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload || {}),
  });
  if (res.status === 401) {
    return _handleApiUnauthorized(res, () => apiPost(url, payload));
  }
  if (!res.ok) {
    let msg = `Request failed: ${res.status}`;
    try {
      const data = await res.json();
      // Most handlers return {"error": "..."}; a few (e.g. VPN connect)
      // return {"errors": [...]} instead -- without this fallback those
      // calls threw a bare "Request failed: 400" and silently discarded the
      // actual reason (e.g. "Use --help for more information." from a
      // failed openvpn invocation), which is exactly what it looked like.
      if (data.error) msg = data.error;
      else if (Array.isArray(data.errors) && data.errors.length) msg = data.errors.join(" ");
    } catch (_) {}
    throw new Error(msg);
  }
  return await res.json();
}

async function loadSwarmOverview(force = false) {
  const now = Date.now();
  if (!force && swarmOverviewCache && now - swarmOverviewCachedAt < SWARM_DATA_CACHE_TTL_MS) {
    return swarmOverviewCache;
  }
  if (swarmOverviewPromise) return swarmOverviewPromise;
  const request = api("/admin/swarm/overview");
  swarmOverviewPromise = request;
  try {
    const overview = await request;
    swarmOverviewCache = overview;
    swarmOverviewCachedAt = Date.now();
    return overview;
  } finally {
    if (swarmOverviewPromise === request) swarmOverviewPromise = null;
  }
}

async function loadTailnetDiscovery(force = false) {
  const now = Date.now();
  if (!force && tailnetDiscoveryCache && now - tailnetDiscoveryCachedAt < SWARM_DATA_CACHE_TTL_MS) {
    return tailnetDiscoveryCache;
  }
  if (tailnetDiscoveryPromise) return tailnetDiscoveryPromise;
  // A normal page render only reads stored state. The full Tailnet discovery
  // path probes newly seen Drones and is reserved for an explicit Discover /
  // Refresh action, so opening Swarm does not duplicate overview's peer work.
  const request = force
    ? apiPost("/admin/tailnet/discover", {})
    : Promise.all([
        api("/admin/tailnet/status").catch(() => ({ installed: false })),
        api("/admin/local-network/status"),
      ]).then(([tailnet, network]) => ({ tailnet, network }));
  tailnetDiscoveryPromise = request;
  try {
    const discovery = await request;
    tailnetDiscoveryCache = discovery;
    tailnetDiscoveryCachedAt = Date.now();
    if (force) swarmOverviewCachedAt = 0;
    return discovery;
  } finally {
    if (tailnetDiscoveryPromise === request) tailnetDiscoveryPromise = null;
  }
}

function invalidateSwarmDataCache() {
  swarmOverviewCachedAt = 0;
  tailnetDiscoveryCachedAt = 0;
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
function scrollContentToTop() {
  // Reset scroll position on navigation so paging/links/back don't leave the
  // viewport parked at the bottom of the previous page.
  try {
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  } catch (_) {
    window.scrollTo(0, 0);
  }
  const main = document.querySelector("main");
  if (main) main.scrollTop = 0;
}
// Counterpart to the scroll-reset above, for the one deliberate exception to
// it -- see movieListScrollPositions.
function restoreMovieListScroll(hash) {
  const saved = movieListScrollPositions[hash];
  if (!saved) return;
  try {
    window.scrollTo({ top: saved.windowY, left: 0, behavior: "auto" });
  } catch (_) {
    window.scrollTo(0, saved.windowY);
  }
  const main = document.querySelector("main");
  if (main) main.scrollTop = saved.mainTop;
}
function stopLogAutoRefresh() {
  if (logRefreshTimer) {
    clearInterval(logRefreshTimer);
    logRefreshTimer = null;
  }
  logRefreshInFlight = false;
}
function stopTransfersAutoRefresh() {
  if (transfersTimer) {
    clearInterval(transfersTimer);
    transfersTimer = null;
  }
  transfersInFlight = false;
}
function stopTorrentsAutoRefresh() {
  if (torrentsTimer) {
    clearInterval(torrentsTimer);
    torrentsTimer = null;
  }
  torrentsInFlight = false;
}
function startTorrentsAutoRefresh() {
  // Live-update only the torrent list/status region -- never the settings
  // form above it, so in-progress edits and the folder picker are untouched.
  stopTorrentsAutoRefresh();
  torrentsTimer = setInterval(async () => {
    if (document.hidden || torrentsInFlight) return;
    if (window.location.hash !== "#admin/torrents") return;
    const liveNode = document.getElementById("torrentsLive");
    if (!liveNode) return;
    torrentsInFlight = true;
    try {
      const payload = await api("/admin/torrents");
      if (
        window.location.hash === "#admin/torrents" &&
        liveNode.isConnected &&
        document.getElementById("torrentsLive") === liveNode &&
        !liveNode.contains(document.activeElement)
      ) {
        patchTorrentsLive(payload);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      torrentsInFlight = false;
    }
  }, 3000);
}

// -------------------------------------------------------------- Config Backups

function stopConfigBackupsAutoRefresh() {
  if (configBackupsTimer) {
    clearInterval(configBackupsTimer);
    configBackupsTimer = null;
  }
  configBackupsInFlight = false;
}

// Only polls while at least one backup is still "creating" -- this is a rare,
// one-off admin action (not a persistent queue like Torrents), so there's no
// need for an always-on interval; it starts on demand and stops itself once
// nothing is left building.
function startConfigBackupsAutoRefreshIfNeeded(backups) {
  const stillBuilding = (backups || []).some((b) => b.status === "creating");
  if (!stillBuilding) {
    stopConfigBackupsAutoRefresh();
    return;
  }
  if (configBackupsTimer) return;
  configBackupsTimer = setInterval(async () => {
    if (document.hidden || configBackupsInFlight) return;
    if (window.location.hash !== "#admin/config-backups") return;
    const bodyNode = document.getElementById("configBackupsTableBody");
    if (!bodyNode) return;
    configBackupsInFlight = true;
    try {
      const payload = await api("/admin/config-backups");
      if (window.location.hash === "#admin/config-backups" && bodyNode.isConnected) {
        patchConfigBackupsLive(payload.backups || []);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      configBackupsInFlight = false;
    }
  }, 2000);
}

function configBackupStatusBadge(row) {
  const status = String(row.status || "creating");
  const cls = status === "error" ? "danger" : status === "complete" ? "success" : "info";
  const title = status === "error" ? escapeHtml(row.error_message || "") : "";
  return `<span class="badge text-bg-${cls}" title="${title}">${escapeHtml(status)}</span>`;
}

function configBackupRowMarkup(row) {
  const id = row.id;
  const complete = row.status === "complete";
  const skippedNote = row.skipped_file_count
    ? `<div class="small text-muted">${Number(row.skipped_file_count)} file${row.skipped_file_count === 1 ? "" : "s"} skipped (${formatBytes(row.skipped_bytes || 0)}, see MANIFEST.txt in the archive)</div>`
    : "";
  const displayName = row.name || row.file_name || "";
  const sourceBadge = row.is_local === false
    ? `<span class="badge text-bg-info ms-1" title="This backup was pulled from a paired Drone over the swarm -- it was not created on this machine."><i class="bi bi-cloud-download me-1"></i>Downloaded from ${escapeHtml(row.source_drone_name || row.source_drone_id || "peer")}</span>`
    : `<span class="badge text-bg-secondary ms-1" title="Built on this machine"><i class="bi bi-hdd me-1"></i>Created here</span>`;
  const nameCell = `<div><strong>${escapeHtml(displayName)}</strong>${sourceBadge}</div>${row.description ? `<div class="small text-muted">${escapeHtml(row.description)}</div>` : ""}`;
  return `<tr>
    <td class="small">${nameCell}</td>
    <td class="small text-nowrap">${escapeHtml(row.created_at || "")}</td>
    <td>${configBackupStatusBadge(row)}</td>
    <td class="small">${complete ? formatBytes(row.size_bytes || 0) : "--"}</td>
    <td class="small">${complete ? `${Number(row.included_file_count || 0)} files` : "--"}${skippedNote}</td>
    <td class="download-actions">
      <a class="btn btn-sm btn-outline-primary${complete ? "" : " invisible"}" title="Download" aria-label="Download" tabindex="${complete ? "0" : "-1"}" ${complete ? `href="${escapeHtml(_apiRequestUrl(`/admin/config-backups/${id}/download`))}"` : ""}><i class="bi bi-download"></i></a>
      <button class="btn btn-sm btn-outline-secondary${complete ? "" : " invisible"}" title="View contents" aria-label="View contents" tabindex="${complete ? "0" : "-1"}" onclick="${complete ? `openConfigBackupTreeModal(${id})` : ""}"><i class="bi bi-folder2-open"></i></button>
      <button class="btn btn-sm btn-outline-info${complete ? "" : " invisible"}" title="Email this backup" aria-label="Email this backup" tabindex="${complete ? "0" : "-1"}" onclick="${complete ? `emailConfigBackup(${id})` : ""}"><i class="bi bi-envelope"></i></button>
      <button class="btn btn-sm btn-outline-warning${complete ? "" : " invisible"}" title="Apply this backup to this Drone" aria-label="Apply this backup to this Drone" tabindex="${complete ? "0" : "-1"}" onclick="${complete ? `openApplyConfigBackupModal(${id})` : ""}"><i class="bi bi-arrow-repeat"></i></button>
      <button class="btn btn-sm btn-outline-danger" title="Delete backup" aria-label="Delete backup" onclick="deleteConfigBackup(${id})"><i class="bi bi-trash"></i></button>
    </td>
  </tr>`;
}

function renderConfigBackupsTableBody(backups) {
  if (!backups.length) {
    return `<tr><td colspan="6" class="text-center text-muted small py-3">No backups yet. Click "Create Backup" to bundle Batocera + emulator settings, gamelist.xml, saves, and custom scripts into a downloadable archive.</td></tr>`;
  }
  return backups.map(configBackupRowMarkup).join("");
}

function patchConfigBackupsLive(backups) {
  configBackupsLastPayload = backups;
  const bodyNode = document.getElementById("configBackupsTableBody");
  if (bodyNode) bodyNode.innerHTML = renderConfigBackupsTableBody(backups);
  startConfigBackupsAutoRefreshIfNeeded(backups);
}

async function renderConfigBackupsPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Backups";
  subtitleNode.textContent = "Bundle Batocera + emulator settings into a downloadable archive";
  setLoading(true, "Loading backups...");
  let payload;
  try {
    payload = await api("/admin/config-backups");
  } catch (err) {
    setLoading(false);
    content.innerHTML = `<div class="alert alert-danger">Failed to load backups: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  const backups = payload.backups || [];
  configBackupsLastPayload = backups;
  content.innerHTML = `
    <div class="card log-card mb-3">
      <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
        <span><i class="bi bi-archive me-2" aria-hidden="true"></i>Config Backups</span>
        <button class="btn btn-sm btn-primary" id="createConfigBackupBtn" onclick="openCreateConfigBackupModal()"><i class="bi bi-plus-circle me-1"></i>Create Backup</button>
      </div>
      <div class="card-body">
        <p class="text-muted small mb-3">
          Each backup bundles <code>batocera.conf</code>, the text-based settings files under <code>system/configs/**</code> (images, fonts, sound, firmware, and shader/other caches are excluded, whatever their size), every system's <code>gamelist.xml</code>, custom scripts under <code>system/services</code>/<code>custom</code>/<code>custom-scripts</code>/<code>scripts</code>, and everything in <code>saves/</code> except known emulator firmware/OS-partition data and disk images. It does not include ROM/BIOS files or this Drone's own credentials.
        </p>
        <div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table local-assets-table bff-stack">
          <thead><tr><th>Name</th><th>Created</th><th>Status</th><th>Size</th><th>Files</th><th class="download-actions">Actions</th></tr></thead>
          <tbody id="configBackupsTableBody">${renderConfigBackupsTableBody(backups)}</tbody>
        </table></div>
      </div>
    </div>
  `;
  startConfigBackupsAutoRefreshIfNeeded(backups);
}

function openCreateConfigBackupModal() {
  const modalId = "createConfigBackupModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-archive me-2"></i>Create Backup</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <div class="mb-3">
            <label class="form-label small" for="configBackupNameInput">Name <span class="text-muted">(optional)</span></label>
            <input class="form-control" type="text" id="configBackupNameInput" placeholder="e.g. Before RetroArch update" maxlength="120" autofocus>
          </div>
          <div class="mb-1">
            <label class="form-label small" for="configBackupDescriptionInput">Description <span class="text-muted">(optional)</span></label>
            <textarea class="form-control" id="configBackupDescriptionInput" rows="3" maxlength="1000" placeholder="Anything worth remembering about this backup -- helps you (or a swarm peer downloading it) tell it apart from others later."></textarea>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" id="submitCreateConfigBackupBtn" onclick="submitCreateConfigBackup()"><i class="bi bi-plus-circle me-1"></i>Create Backup</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
  document.getElementById("configBackupNameInput")?.focus();
}

async function submitCreateConfigBackup() {
  const name = (document.getElementById("configBackupNameInput")?.value || "").trim();
  const description = (document.getElementById("configBackupDescriptionInput")?.value || "").trim();
  const button = document.getElementById("submitCreateConfigBackupBtn");
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Starting...';
  }
  try {
    await apiPost("/admin/config-backups", { name, description });
    const modal = document.getElementById("createConfigBackupModal");
    if (window.bootstrap?.Modal && modal) {
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    } else if (modal) {
      modal.classList.remove("show");
      modal.style.display = "none";
    }
    showToast("Backup started -- building in the background.", "success");
    await renderConfigBackupsPage();
  } catch (err) {
    showToast(`Failed to start backup: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = '<i class="bi bi-plus-circle me-1"></i>Create Backup';
    }
  }
}

async function deleteConfigBackup(backupId) {
  if (!window.confirm("Delete this backup? This cannot be undone.")) return;
  try {
    await apiPost(`/admin/config-backups/${encodeURIComponent(backupId)}/delete`, {});
    await renderConfigBackupsPage();
  } catch (err) {
    showToast(`Failed to delete backup: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

function showEmailNotConfiguredModal() {
  const modalId = "configBackupEmailNotConfiguredModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-envelope-exclamation me-2"></i>Email isn't set up yet</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p>This Drone doesn't have SMTP configured, so it has no way to send mail yet.</p>
          <p class="mb-0">Open the <strong>Email</strong> tile in Admin, fill in your mail provider's host/port and a from/recipient address, save, and this button will work. If a paired Drone already has email set up, you can also pull its configuration from there instead of entering your own.</p>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
          <button type="button" class="btn btn-primary" onclick="setHash('#admin/smtp')" data-bs-dismiss="modal"><i class="bi bi-envelope me-1"></i>Open Email settings</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

async function emailConfigBackup(backupId) {
  try {
    const result = await apiPost(`/admin/config-backups/${encodeURIComponent(backupId)}/email`, {});
    if (result.status === "not_configured") {
      showEmailNotConfiguredModal();
      return;
    }
    if (result.status === "too_large") {
      const limit = formatBytes(result.limit_bytes || 0);
      const size = formatBytes(result.size_bytes || 0);
      showToast(`This backup is too large to email (${size}, limit ${limit}). Download it directly from this page instead.`, "warning", 8000);
      return;
    }
    if (result.status === "error") {
      showToast(`Failed to queue email: ${escapeHtml(result.error || "unknown error")}`, "danger");
      return;
    }
    if (result.status === "not_found") {
      showToast("That backup no longer exists.", "warning");
      return;
    }
    if (result.status === "queued") {
      showToast("Backup email queued. The backend mail worker will send it even if you close this page.", "success", 7000);
      return;
    }
    showToast("The backup email could not be queued.", "danger");
  } catch (err) {
    showToast(`Failed to queue email: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

// Read-only variant of renderFileTreeNode (buildFileTree is shared -- see the
// Torrents "Move Files" tree) -- no checkboxes, since this is just for
// browsing what's inside the tarball, never for selecting a subset of it.
function renderConfigBackupTreeNode(node) {
  const dirItems = Array.from(node.dirs.entries()).map(([name, child]) => `
    <li class="file-tree-node expanded" data-kind="dir">
      <div class="file-tree-row">
        <span class="file-tree-toggle"><i class="bi bi-chevron-right"></i></span>
        <i class="bi bi-folder2 file-tree-icon"></i>
        <span class="file-tree-label" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
      </div>
      <ul class="file-tree-children">${renderConfigBackupTreeNode(child)}</ul>
    </li>
  `).join("");
  const fileItems = node.files.map((file) => `
    <li class="file-tree-node" data-kind="file">
      <div class="file-tree-row">
        <span class="file-tree-toggle"></span>
        <i class="bi bi-file-earmark file-tree-icon"></i>
        <span class="file-tree-label" title="${escapeHtml(file.relative_path || file.name || "")}">${escapeHtml(file.name || file.relative_path || "")}</span>
        <span class="file-tree-size">${file.size != null ? formatBytes(file.size) : ""}</span>
      </div>
    </li>
  `).join("");
  return dirItems + fileItems;
}

function renderConfigBackupTreeList(files) {
  if (!files.length) {
    return '<div class="text-muted small px-2 py-1">This archive is empty.</div>';
  }
  const tree = buildFileTree(files);
  return `<ul class="file-tree">${renderConfigBackupTreeNode(tree)}</ul>`;
}

// The name's own extension, lowercased with the leading dot kept (e.g.
// "retroarch.cfg" -> ".cfg"); files with no extension (or a leading-dot
// dotfile like ".gitkeep", which has no *real* extension) bucket together.
function configBackupFileExtension(relativePath) {
  const name = String(relativePath || "").split("/").pop() || "";
  const dotIndex = name.lastIndexOf(".");
  if (dotIndex <= 0) return "(no extension)";
  return name.slice(dotIndex).toLowerCase();
}

function summarizeConfigBackupExtensions(files) {
  const byExtension = new Map();
  files.forEach((file) => {
    const ext = configBackupFileExtension(file.relative_path || file.name || "");
    const entry = byExtension.get(ext) || { ext, count: 0, size: 0 };
    entry.count += 1;
    entry.size += Number(file.size || 0);
    byExtension.set(ext, entry);
  });
  return Array.from(byExtension.values());
}

const CONFIG_BACKUP_EXTENSION_SUMMARY_COLLAPSED_ROWS = 10;
let configBackupTreeAllFiles = [];
let configBackupTreeSearchQuery = "";
let configBackupExtensionSummaryExpanded = false;
let configBackupExtensionSummarySort = { key: "size", dir: "desc" };

function sortConfigBackupExtensionRows(rows) {
  const { key, dir } = configBackupExtensionSummarySort;
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => sign * (a[key] - b[key]));
}

// A quick-glance summary at the top of the tree view -- how many of which
// file type, and how much disk space each accounts for -- so it's obvious
// at a glance what a backup is actually made of without expanding the tree.
// Collapsed to the top 10 rows by default (sorted by the current column);
// "Show all" expands it. Files/Size headers are clickable to sort by that
// column, toggling direction on repeat clicks.
function renderConfigBackupExtensionSummary(files, expanded) {
  if (!files.length) return "";
  const rows = sortConfigBackupExtensionRows(summarizeConfigBackupExtensions(files));
  const shown = expanded ? rows : rows.slice(0, CONFIG_BACKUP_EXTENSION_SUMMARY_COLLAPSED_ROWS);
  const toggle = rows.length > CONFIG_BACKUP_EXTENSION_SUMMARY_COLLAPSED_ROWS
    ? `<button type="button" class="btn btn-sm btn-outline-secondary w-100 mt-1" onclick="toggleConfigBackupExtensionSummary()">
         <i class="bi ${expanded ? "bi-chevron-up" : "bi-chevron-down"} me-1"></i>${expanded ? "Show fewer" : `Show all ${rows.length} types`}
       </button>`
    : "";
  const sortIndicator = (key) => configBackupExtensionSummarySort.key === key
    ? `<i class="bi ${configBackupExtensionSummarySort.dir === "asc" ? "bi-caret-up-fill" : "bi-caret-down-fill"} ms-1"></i>`
    : "";
  return `
    <div class="table-responsive mb-3">
      <table class="table table-sm table-hover align-middle themed-table mb-0">
        <thead><tr>
          <th>Type</th>
          <th class="text-end sortable-col" onclick="setConfigBackupExtensionSort('count')">Files${sortIndicator("count")}</th>
          <th class="text-end sortable-col" onclick="setConfigBackupExtensionSort('size')">Size${sortIndicator("size")}</th>
        </tr></thead>
        <tbody>
          ${shown.map((row) => `
            <tr>
              <td class="small"><code>${escapeHtml(row.ext)}</code></td>
              <td class="small text-end">${row.count}</td>
              <td class="small text-end">${formatBytes(row.size)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
      ${toggle}
    </div>
  `;
}

function refreshConfigBackupExtensionSummary() {
  const container = document.getElementById("configBackupExtensionSummaryContainer");
  if (container) container.innerHTML = renderConfigBackupExtensionSummary(configBackupTreeAllFiles, configBackupExtensionSummaryExpanded);
}

function toggleConfigBackupExtensionSummary() {
  configBackupExtensionSummaryExpanded = !configBackupExtensionSummaryExpanded;
  refreshConfigBackupExtensionSummary();
}

function setConfigBackupExtensionSort(key) {
  configBackupExtensionSummarySort = configBackupExtensionSummarySort.key === key
    ? { key, dir: configBackupExtensionSummarySort.dir === "asc" ? "desc" : "asc" }
    : { key, dir: "desc" };
  refreshConfigBackupExtensionSummary();
}

// Delegated on the tree container: clicking a folder row toggles it expanded/
// collapsed; file rows have nothing to do.
function handleConfigBackupTreeClick(event) {
  const row = event.target.closest(".file-tree-row");
  const node = row ? row.closest(".file-tree-node") : null;
  if (node && node.dataset.kind === "dir") {
    node.classList.toggle("expanded");
  }
}

// A real backup can hold 10,000+ files -- rendering every match as a fully
// expanded DOM tree got slow (100-200ms+ per keystroke on a real device's
// worth of files, since replacing the existing large tree in the DOM is what
// actually dominates the cost, not the filtering itself). Search results are
// capped so each render stays fast regardless of how broad the query is; the
// unfiltered full tree (no search box query yet) is intentionally NOT capped
// here, since that's a one-time render at modal-open, not a per-keystroke one.
const CONFIG_BACKUP_TREE_SEARCH_MAX_RESULTS = 500;

function renderConfigBackupTreeListContainer() {
  const query = configBackupTreeSearchQuery.trim().toLowerCase();
  if (!query) return renderConfigBackupTreeList(configBackupTreeAllFiles);
  const filtered = [];
  let truncated = false;
  for (const file of configBackupTreeAllFiles) {
    if (!String(file.relative_path || file.name || "").toLowerCase().includes(query)) continue;
    if (filtered.length >= CONFIG_BACKUP_TREE_SEARCH_MAX_RESULTS) {
      truncated = true;
      break;
    }
    filtered.push(file);
  }
  if (!filtered.length) {
    return `<div class="text-muted small px-2 py-3 text-center">No files or folders match "${escapeHtml(configBackupTreeSearchQuery.trim())}".</div>`;
  }
  const note = truncated
    ? `<div class="small text-muted px-2 py-1">Showing the first ${CONFIG_BACKUP_TREE_SEARCH_MAX_RESULTS} matches -- refine your search to narrow further.</div>`
    : "";
  return note + renderConfigBackupTreeList(filtered);
}

// Only the tree list re-renders as the user types -- the search input itself
// is never touched, so it never loses focus/cursor position mid-keystroke.
// Debounced so rapid typing doesn't trigger a (potentially 100ms+, see above)
// re-render on every single keystroke -- only once typing pauses briefly.
const CONFIG_BACKUP_TREE_FILTER_DEBOUNCE_MS = 200;
let configBackupTreeFilterTimer = null;

function filterConfigBackupTree(value) {
  configBackupTreeSearchQuery = value || "";
  if (configBackupTreeFilterTimer) clearTimeout(configBackupTreeFilterTimer);
  configBackupTreeFilterTimer = setTimeout(() => {
    configBackupTreeFilterTimer = null;
    const container = document.getElementById("configBackupTreeListContainer");
    if (container) container.innerHTML = renderConfigBackupTreeListContainer();
  }, CONFIG_BACKUP_TREE_FILTER_DEBOUNCE_MS);
}

async function openConfigBackupTreeModal(backupId) {
  const modalId = "configBackupTreeModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  configBackupTreeAllFiles = [];
  configBackupTreeSearchQuery = "";
  configBackupExtensionSummaryExpanded = false;
  configBackupExtensionSummarySort = { key: "size", dir: "desc" };
  if (configBackupTreeFilterTimer) {
    clearTimeout(configBackupTreeFilterTimer);
    configBackupTreeFilterTimer = null;
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable modal-lg">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-folder2-open me-2"></i>Backup Contents</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body" id="configBackupTreeModalBody">
          <div class="small text-muted mb-2" id="configBackupTreeMeta"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Loading...</div>
          <div id="configBackupExtensionSummaryContainer"></div>
          <input type="text" class="form-control form-control-sm mb-2" id="configBackupTreeSearchInput" placeholder="Filter files and folders..." oninput="filterConfigBackupTree(this.value)">
          <div id="configBackupTreeListContainer"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
        </div>
      </div>
    </div>`;
  modal.querySelector("#configBackupTreeListContainer").addEventListener("click", handleConfigBackupTreeClick);
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
  const meta = document.getElementById("configBackupTreeMeta");
  try {
    const payload = await api(`/admin/config-backups/${encodeURIComponent(backupId)}/tree`);
    if (!document.body.contains(meta)) return;
    if (payload.status !== "ok") {
      const modalBody = document.getElementById("configBackupTreeModalBody");
      if (modalBody) modalBody.innerHTML = `<div class="text-danger small">Failed to read this archive: ${escapeHtml(payload.error || payload.status || "unknown error")}</div>`;
      return;
    }
    configBackupTreeAllFiles = payload.files || [];
    meta.innerHTML = `${configBackupTreeAllFiles.length} file${configBackupTreeAllFiles.length === 1 ? "" : "s"}, ${formatBytes(payload.size_bytes || 0)} compressed`;
    document.getElementById("configBackupExtensionSummaryContainer").innerHTML =
      renderConfigBackupExtensionSummary(configBackupTreeAllFiles, configBackupExtensionSummaryExpanded);
    document.getElementById("configBackupTreeListContainer").innerHTML = renderConfigBackupTreeListContainer();
  } catch (err) {
    const modalBody = document.getElementById("configBackupTreeModalBody");
    if (modalBody && document.body.contains(modalBody)) {
      modalBody.innerHTML = `<div class="text-danger small">Failed to load contents: ${escapeHtml(err.message || "unknown error")}</div>`;
    }
  }
}

function openApplyConfigBackupModal(backupId) {
  const row = (configBackupsLastPayload || []).find((item) => Number(item.id) === Number(backupId));
  const displayName = row ? (row.name || row.file_name || "") : "";
  const modalId = "configBackupApplyModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-exclamation-triangle me-2"></i>Apply Backup to This Drone</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p>This will extract <strong>${escapeHtml(displayName || "this backup")}</strong> onto this machine. Every file the backup contains overwrites the matching file here (<code>batocera.conf</code>, specific <code>system/configs</code> settings, a system's <code>gamelist.xml</code>, custom scripts, specific saves). Nothing is deleted -- anything on this Drone that isn't part of this particular backup (other emulators' configs, other saves) is left exactly as it is.</p>
          <p>EmulationStation (and any running game) will be stopped during the copy and restarted afterward.</p>
          <div class="alert alert-danger py-2 small mb-3"><strong>This cannot be undone.</strong> Whatever is currently in the files this backup targets will be permanently replaced.</div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="applyConfigBackupAck" onchange="document.getElementById('applyConfigBackupConfirmBtn').disabled = !this.checked">
            <label class="form-check-label small" for="applyConfigBackupAck">I understand this will overwrite the specific files in this backup, and cannot be undone.</label>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-danger" id="applyConfigBackupConfirmBtn" onclick="confirmApplyConfigBackup(${Number(backupId)})" disabled><i class="bi bi-arrow-repeat me-1"></i>Apply Backup</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

async function confirmApplyConfigBackup(backupId) {
  const button = document.getElementById("applyConfigBackupConfirmBtn");
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Applying...';
  }
  try {
    const result = await apiPost(`/admin/config-backups/${encodeURIComponent(backupId)}/apply`, {});
    if (result.status === "not_found") {
      showToast("That backup no longer exists.", "warning");
      return;
    }
    if (result.status === "error") {
      showToast(`Failed to apply backup: ${escapeHtml(result.error || "unknown error")}`, "danger", 10000);
      return;
    }
    const modal = document.getElementById("configBackupApplyModal");
    if (window.bootstrap?.Modal && modal) {
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    } else if (modal) {
      modal.classList.remove("show");
      modal.style.display = "none";
    }
    const restarted = result.restarted_emulationstation ? "EmulationStation was restarted." : "";
    showToast(`Backup applied: ${Number(result.restored_file_count || 0)} file(s) restored. ${restarted}`.trim(), "success", 8000);
  } catch (err) {
    showToast(`Failed to apply backup: ${escapeHtml(err.message || "unknown error")}`, "danger", 10000);
  } finally {
    if (button) {
      button.disabled = false;
      button.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Apply Backup';
    }
  }
}

function startTransfersAutoRefresh() {
  // Live-update only the Transfers data while a copy is in progress -- never
  // re-render the whole page, so the asset-request form, paging, and
  // selections are left untouched.
  stopTransfersAutoRefresh();
  transfersTimer = setInterval(async () => {
    if (document.hidden || transfersInFlight) return;
    if (window.location.hash !== "#admin/transfers") return;
    const transfersBody = document.getElementById("transfersBody");
    if (!transfersBody) return;
    transfersInFlight = true;
    try {
      const [downloads, uploads] = await Promise.all([api("/admin/downloads"), api("/admin/uploads")]);
      if (
        window.location.hash === "#admin/transfers" &&
        transfersBody.isConnected &&
        document.getElementById("transfersBody") === transfersBody &&
        !transfersBody.contains(document.activeElement)
      ) {
        transfersBody.innerHTML = renderTransfersPanel(downloads, uploads);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      transfersInFlight = false;
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
// Shared by every lazy-loaded card grid (Systems/ROMs, Movies, Music) --
// reads data-src (the real URL) and data-fallbacks (a JSON array of
// alternate URLs to try in order, [] when there's only one real source, as
// for Movies/Music) off `img`, tries them in order, and reveals a sibling
// placeholder element once every candidate has failed. Only called once
// `img` is actually near the viewport -- see setupLazyImages.
function loadLazyCardImage(img) {
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
    // Reveal a sibling fallback element (e.g. a placeholder icon) -- a
    // no-op for callers with no such sibling (classList.remove on an
    // element lacking the class is harmless), so this is safe for every
    // caller regardless of whether it has one.
    this.style.display = "none";
    this.nextElementSibling?.classList.remove("d-none");
  };
  img.src = primarySrc;
  img.dataset.loaded = "1";
}
// Explicit IntersectionObserver-driven lazy loading (200px rootMargin) for
// every <img data-src> currently in the DOM -- deliberately not left to the
// native loading="lazy" attribute alone (still set on these <img>s as a
// no-JS fallback). Confirmed live: native lazy-loading's own look-ahead
// distance is connection-speed-adaptive, not a fixed margin -- on a fast
// LAN connection to the Drone, Chrome fetched roughly half of a 200-card
// batch immediately (112 requests) even though only ~30 cards were ever
// on screen, since the browser judges prefetching further ahead as cheap
// on a fast connection. The Systems/ROMs grid already needed this explicit
// mechanism for its own reasons (multi-URL fallback chains); Movies and
// Music now go through the same one so "only load what's actually in the
// viewport" isn't left to a heuristic that varies by network speed.
// Call this after replacing any grid's innerHTML with data-src images.
function setupLazyImages() {
  if (imageObserver) {
    imageObserver.disconnect();
    imageObserver = null;
  }

  const lazyImages = Array.from(document.querySelectorAll("img[data-src]"));
  if (!lazyImages.length) return;

  if (!("IntersectionObserver" in window)) {
    lazyImages.forEach(loadLazyCardImage);
    return;
  }

  imageObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const img = entry.target;
      loadLazyCardImage(img);
      observer.unobserve(img);
    });
  }, { rootMargin: "200px 0px" });

  lazyImages.forEach((img) => imageObserver.observe(img));
}
function movieDownloadUrl(entryKey) {
  return `${API_BASE}/movies/${encodeURIComponent(entryKey)}/download`;
}
function movieStreamUrl(entryKey) {
  return `${API_BASE}/movies/${encodeURIComponent(entryKey)}/stream`;
}
function movieArtworkUrl(entryKey, field) {
  return `${API_BASE}/movies/${encodeURIComponent(entryKey)}/artwork/${encodeURIComponent(field)}`;
}
function movieDetailHash(entryKey) {
  return `#movies/${encodeURIComponent(entryKey)}`;
}
function movieExploreHash() {
  return `#movies/explore`;
}
// "show" is reserved -- an entry_key is always the hex-digest slice
// movies_store._entry_key produces, which can never equal "show".
function showDetailHash(showTitle, seasonNumber) {
  const base = `#movies/show/${encodeURIComponent(showTitle)}`;
  return seasonNumber != null ? `${base}/${seasonNumber}` : base;
}
function parseMoviesHash(hash) {
  if (!hash.startsWith("#movies")) return null;
  const rest = hash.slice("#movies".length).replace(/^\//, "");
  // Browse is the only Movies view now -- bare "#movies" and "#movies/explore"
  // both mean it.
  if (!rest || rest === "explore") return { view: "explore" };
  if (rest.startsWith("show/")) {
    const parts = rest.split("/");
    const showTitle = decodeURIComponent(parts[1] || "");
    if (parts[2] === SHOW_EXTRAS_SEASON_KEY) {
      return { view: "show", showTitle, seasonNumber: SHOW_EXTRAS_SEASON_KEY };
    }
    const seasonNumber = parts[2] ? parseInt(parts[2], 10) : null;
    return { view: "show", showTitle, seasonNumber: Number.isFinite(seasonNumber) ? seasonNumber : null };
  }
  return { view: "detail", entryKey: decodeURIComponent(rest.split("?")[0]) };
}
function musicDownloadUrl(entryKey) {
  return `${API_BASE}/music/${encodeURIComponent(entryKey)}/download`;
}
function musicStreamUrl(entryKey) {
  return `${API_BASE}/music/${encodeURIComponent(entryKey)}/stream`;
}
// Bumped per entry_key right after a successful album-cover upload (see
// uploadMusicAlbumArt) so a re-render in *this* browser session doesn't
// serve a stale image from the artwork endpoint's
// "Cache-Control: public, max-age=3600" -- the upload destination is a
// fixed filename (album-cover.<ext>), so a re-upload overwrites the exact
// URL the browser already cached. Empty until an upload happens, so a
// normal (never-uploaded-to) track's artwork URL is unaffected.
const musicArtCacheBust = new Map();
function musicArtworkUrl(entryKey, field) {
  const bust = musicArtCacheBust.get(entryKey);
  return `${API_BASE}/music/${encodeURIComponent(entryKey)}/artwork/${encodeURIComponent(field)}${bust ? `?v=${bust}` : ""}`;
}
function musicDetailHash(entryKey) {
  return `#music/${encodeURIComponent(entryKey)}`;
}
function musicExploreHash() {
  return `#music/explore`;
}
// "artist" is reserved -- an entry_key is always the hex-digest slice
// music_store._entry_key produces, which can never equal "artist".
//
// `album === ""` (the "Singles" bucket) and `album == null` ("no
// preference, use this artist's default album") must produce *different*
// hashes -- both used to omit the album segment entirely, making them
// indistinguishable once parsed back out by parseMusicHash (which then
// falls back to this artist's first alphabetical *real* album for either
// case). That was a real bug: clicking a "Singles" card in the Explorer
// grid silently landed on whatever album sorts first for that artist
// instead, which looked -- and was reported -- as if two unrelated albums
// "were the same album", since both links led to the identical page.
function artistDetailHash(artist, album) {
  const base = `#music/artist/${encodeURIComponent(artist)}`;
  return album != null ? `${base}/${encodeURIComponent(album)}` : base;
}
// entryKey is any one track in the album (the backend groups every sibling
// on-disk itself, see handlers_music._album_group_entry_keys) -- artist/album
// are only needed here to know which page to re-render afterward, not to
// identify the target server-side.
function openMusicAlbumArtPicker(entryKey, artist, album) {
  let input = document.getElementById("musicAlbumArtUploadInput");
  if (!input) {
    input = document.createElement("input");
    input.type = "file";
    input.id = "musicAlbumArtUploadInput";
    input.accept = "image/jpeg,image/png,image/webp";
    input.className = "d-none";
    document.body.appendChild(input);
  }
  input.onchange = async () => {
    const file = input.files && input.files[0];
    input.value = "";
    if (file) await uploadMusicAlbumArt(entryKey, file, artist, album);
  };
  input.click();
}
async function uploadMusicAlbumArt(entryKey, file, artist, album) {
  const formData = new FormData();
  formData.append("file", file, file.name);
  setLoading(true, "Uploading album cover...");
  try {
    const res = await fetch(_apiRequestUrl(`/admin/music/${encodeURIComponent(entryKey)}/artwork/upload`), { method: "POST", credentials: "include", body: formData });
    let responsePayload = {};
    try { responsePayload = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(responsePayload.error || `Upload failed: ${res.status}`);
    const bustToken = Date.now();
    (responsePayload.entry_keys || []).forEach((key) => musicArtCacheBust.set(key, bustToken));
    musicAllRows = []; // invalidate the client-side inventory cache so every view re-fetches fresh liked/genre/art state
    showToast(`Album cover updated for ${responsePayload.updated} track${responsePayload.updated === 1 ? "" : "s"}.`, "success");
    await renderArtistDetailsPage(artist, album);
  } catch (err) {
    showToast(`Album cover upload failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  } finally {
    setLoading(false);
  }
}
function parseMusicHash(hash) {
  if (!hash.startsWith("#music")) return null;
  const rest = hash.slice("#music".length).replace(/^\//, "");
  if (!rest || rest === "explore") return { view: "explore" };
  if (rest.startsWith("artist/")) {
    const parts = rest.split("/");
    const artist = decodeURIComponent(parts[1] || "");
    const album = parts[2] !== undefined ? decodeURIComponent(parts[2]) : null;
    return { view: "artist", artist, album };
  }
  return { view: "detail", entryKey: decodeURIComponent(rest.split("?")[0]) };
}
// Right-justified Games/Movies/Music switcher, spliced into the end of each
// Explorer page's existing .movie-explorer-topbar row -- the search box's
// own flex-grow-1 already pushes everything after it flush right, so this
// needs no new layout mechanics on desktop widths. Replaces the two separate
// navbar links (index.html's old #systemsMenuBtn "Games"/#moviesMenuBtn
// "Movies") with one always-visible in-page control instead of a nav-level
// tab per type.
//
// On narrow (<=768px) viewports brand+search+switcher-with-labels no longer
// fit on one row -- see the .movie-explorer-topbar/.asset-type-switcher
// rules in drone.css, which wrap the switcher onto its own full-width row
// under the search bar and hide each button's label span (below), leaving
// icon-only buttons.
function renderAssetTypeSwitcher(active) {
  const types = [
    ["systems", "Games", "bi-grid", "#systems"],
    ["movies", "Movies", "bi-film", "#movies"],
    ["music", "Music", "bi-music-note-beamed", "#music"],
  ];
  return `
    <div class="asset-type-switcher">
      ${types.map(([key, label, icon, hash]) => `
        <button type="button" class="btn btn-outline-light btn-sm asset-type-switcher-btn ${active === key ? "active" : ""}" title="${escapeHtml(label)}" onclick="setHash(${jsAttr(hash)})"><i class="bi ${icon} me-1"></i><span class="asset-type-switcher-btn-label">${escapeHtml(label)}</span></button>
      `).join("")}
    </div>
  `;
}
async function renderMovieExplorerPage() {
  currentSystemContext = null;
  clearSystemTheme();
  movieExplorerTypeFilter = "all";
  movieExplorerGenreFilter = "";
  movieExplorerShowAllGenres = false;
  movieExploreDisplayLimit = MOVIE_EXPLORE_PAGE_SIZE;
  movieExplorerDuplicatesMode = false;
  movieExplorerDuplicateGroups = [];
  setLoading(true, "Loading movies...");
  try {
    if (!moviesAllRows.length) {
      const payload = await api("/movies");
      moviesAllRows = payload.movies || [];
    }
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-film me-2"></i>Movies</div>
          <div class="movie-explorer-search flex-grow-1">
            <input id="movieExplorerSearch" type="search" class="form-control" placeholder="Search titles" oninput="filterMovieExplorer(this.value)" autofocus>
          </div>
          <button id="movieExplorerDuplicatesBtn" class="btn btn-outline-light btn-sm" type="button" title="Find duplicate movies/shows" onclick="toggleMovieExplorerDuplicatesMode()"><i class="bi bi-files"></i></button>
          ${renderAssetTypeSwitcher("movies")}
        </div>
        <div class="movie-explorer-body">
          <aside id="movie-explorer-sidebar" class="movie-explorer-sidebar"></aside>
          <div class="movie-explorer-grid-wrap min-width-0">
            <div id="movie-explorer-grid" class="movie-explorer-grid"></div>
            <div id="movie-explorer-more" class="text-center mt-3"></div>
          </div>
        </div>
      </div>
    `;
    renderMovieExplorerSidebar();
    filterMovieExplorer("");
    restoreMovieListScroll(movieExploreHash());
  } catch (err) {
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-film me-2"></i>Movies</div>
          ${renderAssetTypeSwitcher("movies")}
        </div>
        <div class="alert alert-danger m-3">Failed to load movies: ${escapeHtml(err.message || "unknown error")}</div>
      </div>
    `;
  } finally {
    setLoading(false);
  }
}
function movieExplorerGenres() {
  const genres = new Set();
  moviesAllRows.forEach((m) => (m.genres || []).forEach((g) => g && genres.add(g)));
  // Most-to-least by how many movies currently match it (same faceted count
  // shown next to each button), ties broken alphabetically.
  return [...genres].sort((a, b) => movieExplorerGenreCount(b) - movieExplorerGenreCount(a) || a.localeCompare(b));
}
// "Shows" also keeps a groupable extra (a Featurette resolved to a
// show/season) -- otherwise it'd be filtered out before groupMoviesForExplorer
// ever gets a chance to fold it into its show's card, and it would only ever
// show up under "All". Shared by filterMovieExplorer and the sidebar's own
// per-facet counts so both stay in exact agreement.
function movieExplorerRowsMatchingType(rows, value) {
  if (value === "all") return rows;
  if (value === "episode") return rows.filter(isShowGroupableRow);
  return rows.filter((m) => (m.kind || "movie") === value);
}
function movieExplorerRowsMatchingGenre(rows, value) {
  if (!value) return rows;
  return rows.filter((m) => (m.genres || []).includes(value));
}
// Each facet's counts hold the *other* active facet fixed and ask "how many
// would match if this were selected instead" -- standard faceted-search
// convention (picking a facet narrows the other facet's counts, never its
// own) -- rather than a flat unfiltered total for every button.
function movieExplorerTypeCount(value) {
  return movieExplorerRowsMatchingType(movieExplorerRowsMatchingGenre(moviesAllRows, movieExplorerGenreFilter), value).length;
}
function movieExplorerGenreCount(value) {
  return movieExplorerRowsMatchingGenre(movieExplorerRowsMatchingType(moviesAllRows, movieExplorerTypeFilter), value).length;
}
function renderMovieExplorerSidebar() {
  const sidebar = document.getElementById("movie-explorer-sidebar");
  if (!sidebar) return;
  const typeButton = (value, label) => `
    <button type="button" class="movie-explorer-category-btn ${movieExplorerTypeFilter === value ? "active" : ""}" onclick="setMovieExplorerTypeFilter(${jsAttr(value)})">
      <span>${escapeHtml(label)}</span><span class="movie-explorer-category-count">${movieExplorerTypeCount(value).toLocaleString()}</span>
    </button>
  `;
  const genreButton = (value, label) => `
    <button type="button" class="movie-explorer-category-btn ${movieExplorerGenreFilter === value ? "active" : ""}" onclick="setMovieExplorerGenreFilter(${jsAttr(value)})">
      <span>${escapeHtml(label)}</span><span class="movie-explorer-category-count">${movieExplorerGenreCount(value).toLocaleString()}</span>
    </button>
  `;
  const genres = movieExplorerGenres();
  const visibleGenres = movieExplorerShowAllGenres ? genres : genres.slice(0, MOVIE_EXPLORE_TOP_GENRE_COUNT);
  const canExpandGenres = genres.length > MOVIE_EXPLORE_TOP_GENRE_COUNT;
  sidebar.innerHTML = `
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Type</div>
      ${typeButton("all", "All")}
      ${typeButton("movie", "Movies")}
      ${typeButton("episode", "Shows")}
    </div>
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Genres</div>
      ${genreButton("", "All Genres")}
      ${genres.length ? visibleGenres.map((g) => genreButton(g, g)).join("") : `<div class="text-muted small">Scrape movies to see genres.</div>`}
      ${canExpandGenres ? `
        <button type="button" class="movie-explorer-category-btn movie-explorer-sidebar-more-btn" onclick="toggleMovieExplorerShowAllGenres()">
          ${movieExplorerShowAllGenres ? "Show less" : `Show more (${(genres.length - MOVIE_EXPLORE_TOP_GENRE_COUNT).toLocaleString()})`}
        </button>
      ` : ""}
    </div>
  `;
}
function toggleMovieExplorerShowAllGenres() {
  movieExplorerShowAllGenres = !movieExplorerShowAllGenres;
  renderMovieExplorerSidebar();
}
function setMovieExplorerTypeFilter(value) {
  movieExplorerTypeFilter = value;
  renderMovieExplorerSidebar();
  filterMovieExplorer(document.getElementById("movieExplorerSearch")?.value || "");
}
function setMovieExplorerGenreFilter(value) {
  movieExplorerGenreFilter = value;
  renderMovieExplorerSidebar();
  filterMovieExplorer(document.getElementById("movieExplorerSearch")?.value || "");
}
// A row belongs to a show group if it's a real episode, or an "extra"
// (Featurette/deleted-scene/etc.) that classify() managed to resolve a
// show_title for from its directory structure -- its own filename
// essentially never carries that indicator the way a real episode's does
// (see HandlersMoviesMixin._apply_movie_kind_and_genres). An extra with no
// resolvable show at all stays ungrouped (its own orphan card), same as
// before this existed, rather than guessing. season_number is NOT required
// here even though it drives which season tab a row lands under
// (SHOW_EXTRAS_SEASON_KEY below covers a resolved-show-but-no-season
// extra) -- real reported gap: an extras folder sitting directly under the
// show with no season subdivision at all ("Shows/<Show>/Featurettes/.../
// clip.mkv") resolves a show_title fine but has no season folder to infer
// a season from, and requiring one here made the row silently fall out of
// its show's grouping entirely instead of showing up under an "Extras" tab.
function isShowGroupableRow(row) {
  return row.kind === "episode" || (row.kind === "extra" && !!row.show_title);
}
// Sentinel season-tab key for a groupable extra with no resolvable season
// (see isShowGroupableRow) -- deliberately a string, never collides with a
// real (numeric) season number, sorts after every real season in
// renderShowDetailsPage's tab strip, and round-trips through the URL hash
// unlike `null` would (parseMoviesHash needs to keep it, not parseInt it
// away).
const SHOW_EXTRAS_SEASON_KEY = "extras";
function seasonTabLabel(seasonKey) {
  return seasonKey === SHOW_EXTRAS_SEASON_KEY ? "Extras" : `Season ${seasonKey}`;
}
// Sorts a season's members with real episodes first (by episode_number),
// extras afterward (alphabetically, since they have no reliable ordering
// signal of their own) -- used by both the Explorer's season cards and the
// show detail page's episode list so the two stay consistent.
function compareShowGroupMembers(a, b) {
  const aKey = a.kind === "extra" ? Number.POSITIVE_INFINITY : (a.episode_number || 0);
  const bKey = b.kind === "extra" ? Number.POSITIVE_INFINITY : (b.episode_number || 0);
  if (aKey !== bKey) return aKey - bKey;
  return String(a.movie_name || a.name || "").localeCompare(String(b.movie_name || b.name || ""));
}
// "&" and "and" show up interchangeably across different releases of the
// same show -- confirmed live: "Law & Order Special Victims Unit" (seasons
// 1-26, one release group) vs "Law and Order Special Victims Unit" (season
// 27, a different one) are otherwise-identical raw show_title strings that
// would otherwise land in two different groups/two different cards for the
// same show. Normalized only for *matching* episodes together -- never for
// what's actually displayed (movieExplorerCardTitle/the representative's own
// show_title still show the real, unnormalized text).
function movieShowGroupingKey(title) {
  return String(title || "")
    .toLowerCase()
    .replace(/\s*&\s*/g, " and ")
    .replace(/\s+/g, " ")
    .trim();
}
// Groups groupable rows (see isShowGroupableRow) into one synthetic card per
// show -- every season folds into the same card (clicking it lands on the
// show detail page, which owns its own season switcher) -- everything else
// (movies, and an ungroupable "extra" visible under the "All" type filter)
// passes through as its own card, same as before this existed. Grouping key
// is always the filename-parsed show_title (present pre-scrape via
// kind/show_title on every row -- see
// HandlersMoviesMixin._apply_movie_kind_and_genres), never the scraped TMDb
// name, so a show with only some episodes scraped can't split into two
// cards just because the parsed and TMDb names differ slightly.
function groupMoviesForExplorer(rows) {
  const cards = [];
  const showGroups = new Map();
  rows.forEach((row) => {
    if (!isShowGroupableRow(row)) {
      cards.push(row);
      return;
    }
    const key = movieShowGroupingKey(row.show_title);
    if (!showGroups.has(key)) showGroups.set(key, []);
    showGroups.get(key).push(row);
  });
  showGroups.forEach((members) => {
    members.sort(compareShowGroupMembers);
    // Prefer a real episode as the representative (its entry_key drives the
    // show card's poster lookup and the show-detail page's first-ever
    // metadata fetch) -- a real episode's artwork is more specific/reliable
    // than an extra's (apply_tv_extra applies the show's own poster/
    // backdrop as a fallback when an extra's show resolves on TMDb, but an
    // episode's own poster/backdrop cascade is preferred when one exists).
    const representative = members.find((m) => m.scraped_show_title)
      || members.find((m) => m.kind === "episode")
      || members[0];
    cards.push({
      isShowGroup: true,
      showTitle: representative.scraped_show_title || representative.show_title,
      rawShowTitle: representative.show_title,
      entry_key: representative.entry_key,
      genres: representative.genres || [],
      episodeCount: members.length,
    });
  });
  return cards;
}
function movieExplorerCardTitle(entry) {
  return entry.isShowGroup
    ? entry.showTitle
    : entry.display_title || entry.movie_name || entry.name || "";
}
async function toggleMovieExplorerDuplicatesMode() {
  movieExplorerDuplicatesMode = !movieExplorerDuplicatesMode;
  document.getElementById("movieExplorerDuplicatesBtn")?.classList.toggle("active", movieExplorerDuplicatesMode);
  filterMovieExplorer(document.getElementById("movieExplorerSearch")?.value || "");
}
let movieExplorerDuplicatesLoading = false;
async function loadMovieExplorerDuplicates(queryValue) {
  const grid = document.getElementById("movie-explorer-grid");
  if (!grid) return;
  movieExplorerDuplicatesLoading = true;
  renderMovieExplorerDuplicatesGrid(grid);
  try {
    const params = new URLSearchParams();
    if (movieExplorerTypeFilter && movieExplorerTypeFilter !== "all") params.set("kind", movieExplorerTypeFilter);
    if (movieExplorerGenreFilter) params.set("genre", movieExplorerGenreFilter);
    if (String(queryValue || "").trim()) params.set("q", String(queryValue).trim());
    const data = await api(`/admin/movies/duplicates?${params.toString()}`);
    movieExplorerDuplicateGroups = data.groups || [];
  } catch (err) {
    showToast(`Failed to load duplicates: ${escapeHtml(err.message || "unknown error")}`, "danger");
    movieExplorerDuplicateGroups = [];
  } finally {
    movieExplorerDuplicatesLoading = false;
    renderMovieExplorerDuplicatesGrid(grid);
  }
}
function renderMovieExplorerDuplicatesGrid(grid) {
  grid.classList.add("movie-explorer-grid-list");
  const moreWrap = document.getElementById("movie-explorer-more");
  if (moreWrap) moreWrap.innerHTML = "";  // duplicates are fetched all at once -- no paging
  if (movieExplorerDuplicatesLoading) {
    grid.innerHTML = `<div class="text-muted p-4"><span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Scanning for duplicates...</div>`;
    return;
  }
  const groups = movieExplorerDuplicateGroups;
  if (!groups.length) {
    grid.innerHTML = `<div class="text-muted p-4">No duplicate movies or shows found in the current filters.</div>`;
    return;
  }
  const deletableCount = groups.reduce((sum, group) => sum + group.items.filter((item) => !item.recommended_keep).length, 0);
  grid.innerHTML = `
    <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
      <div class="text-muted small">${groups.length.toLocaleString()} duplicate${groups.length === 1 ? "" : "s"} found &middot; ${deletableCount.toLocaleString()} extra cop${deletableCount === 1 ? "y" : "ies"} can be removed.</div>
      <button class="btn btn-danger btn-sm" type="button" onclick="openMovieDuplicatesReviewModal()"><i class="bi bi-trash me-1"></i>Review &amp; Delete Duplicates</button>
    </div>
    <div class="d-flex flex-column gap-3">
      ${groups.map(renderMovieExplorerDuplicateGroup).join("")}
    </div>
  `;
}
function renderMovieExplorerDuplicateGroup(group) {
  return `
    <div class="card log-card">
      <div class="card-header d-flex justify-content-between align-items-center gap-2">
        <span class="fw-semibold text-truncate">${escapeHtml(group.label)}</span>
        <span class="badge text-bg-secondary text-nowrap">${group.kind === "episode" ? "Episode" : "Movie"} &middot; ${group.items.length}</span>
      </div>
      <div class="tree-leaf-list">
        ${group.items.map((item) => `
          <div class="tree-grid-row tree-leaf-row">
            <div class="tree-grid-main">
              <i class="bi bi-film tree-grid-icon"></i>
              <div class="tree-grid-label text-truncate" title="${escapeHtml(item.movie_name)}">
                <span class="fw-semibold">${escapeHtml(item.display_title || item.movie_name)}</span>
              </div>
            </div>
            <div class="tree-grid-meta d-flex align-items-center gap-2">
              ${item.recommended_keep ? `<span class="badge text-bg-success">Keep</span>` : ""}
              <span>${escapeHtml(item.byte_count !== undefined && item.byte_count !== null ? formatBytes(item.byte_count) : "n/a")}</span>
            </div>
            <div class="tree-grid-action">
              <button class="btn btn-outline-danger btn-sm" type="button" title="Delete this copy" onclick="deleteMovieDuplicateItem(${jsAttr(item.entry_key)}, ${jsAttr(item.display_title || item.movie_name)})"><i class="bi bi-trash"></i></button>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}
// Targeted single delete for one duplicate-list row -- distinct from the bulk
// "Review & Delete Duplicates" modal (openMovieDuplicatesReviewModal): confirms
// then deletes just this one copy and refreshes the duplicates list in place
// (no navigation away, unlike deleteMovieFromDetailPage's own-page delete).
function deleteMovieDuplicateItem(entryKey, title) {
  openConfirmDeleteModal({
    title: "Delete movie?",
    body: `<strong>${escapeHtml(title)}</strong> will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      setLoading(true, "Deleting...");
      try {
        await deleteMoviesBatch([entryKey]);
        showToast("Movie deleted.", "success");
        loadMovieExplorerDuplicates(document.getElementById("movieExplorerSearch")?.value || "");
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
}
function openMovieDuplicatesReviewModal() {
  const groups = movieExplorerDuplicateGroups.map((group) => ({ label: group.label, items: group.items }));
  openDuplicatesReviewModal({
    title: "Delete duplicate movies/shows?",
    groups,
    itemLabel: (item) => item.display_title || item.movie_name,
    itemMeta: (item) => (item.byte_count !== undefined && item.byte_count !== null ? formatBytes(item.byte_count) : "n/a"),
    deleteFn: (items) => deleteMoviesBatch(items.map((item) => item.entry_key)),
    onDeleted: () => loadMovieExplorerDuplicates(document.getElementById("movieExplorerSearch")?.value || ""),
  });
}
function filterMovieExplorer(queryValue, opts = {}) {
  const grid = document.getElementById("movie-explorer-grid");
  if (!grid) return;
  if (movieExplorerDuplicatesMode) {
    loadMovieExplorerDuplicates(queryValue || "");
    return;
  }
  grid.classList.remove("movie-explorer-grid-list");
  if (opts.growDisplay) {
    movieExploreDisplayLimit += MOVIE_EXPLORE_PAGE_SIZE;
  } else {
    movieExploreDisplayLimit = MOVIE_EXPLORE_PAGE_SIZE;
  }
  const filter = String(queryValue || "").trim().toLowerCase();
  let rows = movieExplorerRowsMatchingGenre(movieExplorerRowsMatchingType(moviesAllRows, movieExplorerTypeFilter), movieExplorerGenreFilter);
  if (filter) {
    rows = rows.filter((m) => (m.display_title || m.movie_name || m.name || "").toLowerCase().includes(filter));
  }
  const cards = groupMoviesForExplorer(rows);
  const sorted = [...cards].sort((a, b) => movieSortableTitle(movieExplorerCardTitle(a)).localeCompare(movieSortableTitle(movieExplorerCardTitle(b))));
  const visible = sorted.slice(0, movieExploreDisplayLimit);
  grid.innerHTML = visible.length
    ? visible.map(renderMovieExplorerCard).join("")
    : `<div class="text-muted p-4">No movies match the current filters.</div>`;
  renderMovieExplorerMoreButton(visible.length, sorted.length, queryValue);
  setupLazyImages();
}
function renderMovieExplorerMoreButton(shown, total, queryValue) {
  const wrap = document.getElementById("movie-explorer-more");
  if (!wrap) return;
  if (shown >= total) {
    wrap.innerHTML = total ? `<span class="small text-muted">Showing all ${total.toLocaleString()}</span>` : "";
    return;
  }
  wrap.innerHTML = `
    <button type="button" class="btn btn-outline-primary btn-sm" onclick="filterMovieExplorer(${jsAttr(queryValue || "")}, { growDisplay: true })">
      <i class="bi bi-plus-circle me-1"></i>Show more (${shown.toLocaleString()} of ${total.toLocaleString()})
    </button>
  `;
}
function renderMovieExplorerCard(entry) {
  const title = movieExplorerCardTitle(entry);
  const posterUrl = movieArtworkUrl(entry.entry_key, "poster");
  const navigateHash = entry.isShowGroup
    ? showDetailHash(entry.rawShowTitle)
    : movieDetailHash(entry.entry_key);
  return `
    <button type="button" class="movie-explorer-card" title="${escapeHtml(title)}" onclick="setHash(${jsAttr(navigateHash)})">
      <div class="movie-explorer-card-poster">
        <img src="" data-src="${escapeHtml(posterUrl)}" data-fallbacks='[]' alt="" loading="lazy">
        <div class="movie-explorer-card-poster-fallback d-none"><i class="bi bi-film"></i></div>
      </div>
      <div class="movie-explorer-card-title">${escapeHtml(title)}</div>
    </button>
  `;
}
// Show detail page (route #movies/show/<name>[/<season>], reached by
// clicking a season card in the explorer): a season selector plus that
// season's artwork/overview/episode list, mirroring the single-movie detail
// page's hero layout. Switching seasons is just a hash change (each season
// tab links to #movies/show/<name>/<n>) -- the router re-renders this whole
// page on every season click, which both updates the artwork/metadata (the
// actual ask) and keeps the selected season bookmarkable/back-button-able,
// same convention as every other stateful view in this app.
async function renderShowDetailsPage(showTitle, seasonNumber) {
  currentSystemContext = null;
  clearSystemTheme();
  setLoading(true, "Loading show...");
  try {
    if (!moviesAllRows.length) {
      const payload = await api("/movies");
      moviesAllRows = payload.movies || [];
    }
    const showKey = movieShowGroupingKey(showTitle);
    const episodes = moviesAllRows.filter((m) => isShowGroupableRow(m) && movieShowGroupingKey(m.show_title) === showKey);
    if (!episodes.length) {
      content.innerHTML = `
        <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#movies')"><i class="bi bi-arrow-left me-1"></i>Back to Movies</button>
        <div class="alert alert-warning">No episodes found for "${escapeHtml(showTitle)}".</div>
      `;
      return;
    }
    const seasonsMap = new Map();
    episodes.forEach((ep) => {
      // A groupable extra with no resolvable season (isShowGroupableRow)
      // lands in the sentinel "Extras" bucket rather than being dropped --
      // real episodes always have a real season_number (classify() never
      // returns KIND_EPISODE without one).
      const seasonKey = ep.season_number == null ? SHOW_EXTRAS_SEASON_KEY : ep.season_number;
      if (!seasonsMap.has(seasonKey)) seasonsMap.set(seasonKey, []);
      seasonsMap.get(seasonKey).push(ep);
    });
    // Numeric seasons first (ascending), the "Extras" bucket always last --
    // a plain `(a, b) => a - b` breaks the moment a string key is mixed in
    // (NaN from the subtraction), and conceptually "Extras" isn't a season
    // number to interleave with real ones anyway.
    const seasonNumbers = [...seasonsMap.keys()].sort((a, b) => {
      if (a === SHOW_EXTRAS_SEASON_KEY) return 1;
      if (b === SHOW_EXTRAS_SEASON_KEY) return -1;
      return a - b;
    });
    const selectedSeason = seasonNumbers.includes(seasonNumber) ? seasonNumber : seasonNumbers[0];
    const seasonEpisodes = (seasonsMap.get(selectedSeason) || []).slice().sort(compareShowGroupMembers);
    const representative = seasonEpisodes.find((e) => e.scraped_show_title)
      || seasonEpisodes.find((e) => e.kind === "episode")
      || seasonEpisodes[0];
    const displayShowTitle = representative.scraped_show_title || representative.show_title || showTitle;
    const realEpisodeCount = seasonEpisodes.filter((e) => e.kind === "episode").length;
    const extraCount = seasonEpisodes.length - realEpisodeCount;

    let detail = null;
    try {
      detail = await api(`/movies/${encodeURIComponent(representative.entry_key)}`);
    } catch (_) {
      detail = null; // unscraped season -- render with no poster/overview rather than failing the page
    }
    const meta = detail && detail.metadata;
    const posterUrl = meta && meta.poster_relative_path ? movieArtworkUrl(representative.entry_key, "poster") : null;
    const backdropUrl = meta && meta.backdrop_relative_path ? movieArtworkUrl(representative.entry_key, "backdrop") : null;
    const overview = (meta && (meta.season_overview || meta.overview)) || "";
    const genres = (meta && meta.genres) || [];

    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#movies')"><i class="bi bi-arrow-left me-1"></i>Back to Movies</button>
      <div class="movie-detail-hero" ${backdropUrl ? `style="background-image:linear-gradient(180deg, rgba(11,16,32,0.55) 0%, rgba(11,16,32,0.96) 100%), url('${escapeHtml(backdropUrl)}')"` : ""}>
        <div class="movie-detail-hero-body">
          ${
            posterUrl
              ? `<img class="movie-detail-poster" src="${escapeHtml(posterUrl)}" alt="">`
              : `<div class="movie-detail-poster movie-detail-poster-placeholder"><i class="bi bi-film"></i></div>`
          }
          <div class="movie-detail-info min-width-0">
            <div class="small text-muted mb-1"><span class="badge text-bg-info me-2">TV Show</span>${realEpisodeCount} episode${realEpisodeCount === 1 ? "" : "s"}${extraCount ? `, ${extraCount} extra${extraCount === 1 ? "" : "s"}` : ""}</div>
            <h2 class="movie-detail-title" title="${escapeHtml(displayShowTitle)}">${escapeHtml(displayShowTitle)} &middot; ${seasonTabLabel(selectedSeason)}</h2>
            ${genres.length ? `<div class="mb-2">${genres.map((g) => `<span class="badge movie-genre-badge">${escapeHtml(g)}</span>`).join(" ")}</div>` : ""}
            ${
              adminEnabled
                ? `<button class="btn btn-outline-danger btn-sm" type="button" onclick="deleteShowFromDetailPage(${jsAttr(showTitle)}, ${jsAttr(displayShowTitle)}, ${jsAttr(episodes.map((e) => e.entry_key))})"><i class="bi bi-trash me-1"></i>Delete Show</button>`
                : ""
            }
          </div>
        </div>
      </div>
      <div class="movie-detail-body">
        ${overview ? `<p>${escapeHtml(overview)}</p>` : `<p class="text-muted small">No description yet -- scrape an episode of this season to fetch one from TMDb.</p>`}
        <div class="d-flex flex-wrap gap-2 my-3">
          ${seasonNumbers.map((n) => `
            <button type="button" class="btn btn-sm ${n === selectedSeason ? "btn-primary" : "btn-outline-primary"}" onclick="setHash(${jsAttr(showDetailHash(showTitle, n))})">${seasonTabLabel(n)}</button>
          `).join("")}
        </div>
        <div class="list-group">
          ${seasonEpisodes.map(renderShowDetailEpisodeRow).join("")}
        </div>
      </div>
    `;
  } catch (err) {
    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#movies')"><i class="bi bi-arrow-left me-1"></i>Back to Movies</button>
      <div class="alert alert-danger">Failed to load show: ${escapeHtml(err.message || "unknown error")}</div>
    `;
  } finally {
    setLoading(false);
  }
}
function deleteShowFromDetailPage(showTitle, displayShowTitle, entryKeys) {
  openConfirmDeleteModal({
    title: "Delete show?",
    body: `<strong>${escapeHtml(displayShowTitle)}</strong> and all ${entryKeys.length} episode${entryKeys.length === 1 ? "" : "s"}/extras (every season) will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete Show",
    onConfirm: async () => {
      setLoading(true, "Deleting show...");
      try {
        await deleteMoviesBatch(entryKeys);
        showToast("Show deleted.", "success");
        setHash("#movies");
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
}
function renderShowDetailEpisodeRow(ep) {
  const isExtra = ep.kind === "extra";
  const label = isExtra
    ? (ep.movie_name || ep.display_title || ep.name || "")
    : `E${String(ep.episode_number || 0).padStart(2, "0")} - ${ep.episode_title || ep.movie_name || ""}`;
  const badge = isExtra ? `<span class="badge text-bg-secondary me-2">Extra</span>` : "";
  return `
    <div class="list-group-item d-flex align-items-center justify-content-between gap-2 bg-transparent">
      <button type="button" class="btn btn-link btn-sm p-0 text-start text-truncate min-width-0" title="${escapeHtml(ep.file_path || ep.movie_name || "")}" onclick="setHash(movieDetailHash(${jsAttr(ep.entry_key)}))">${badge}${escapeHtml(label)}</button>
      <div class="d-flex gap-2 text-nowrap">
        <button class="btn btn-outline-primary btn-sm" type="button" title="Watch" onclick="openMoviePlayerModal(${jsAttr(ep.entry_key)}, ${jsAttr(label)})"><i class="bi bi-play-circle"></i></button>
        ${
          ep.is_downloadable === false
            ? `<button class="btn btn-secondary btn-sm" type="button" title="Downloads disabled" disabled><i class="bi bi-slash-circle"></i></button>`
            : `<a class="btn btn-primary btn-sm" title="Download" href="${movieDownloadUrl(ep.entry_key)}"><i class="bi bi-download"></i></a>`
        }
      </div>
    </div>
  `;
}
// Ignores a leading "The " (case-insensitive) when alphabetizing a movie/show
// title, so e.g. "The Movie Pt1" sorts next to "Movie Pt2" instead of under
// "T" -- standard library/media-catalog sort convention.
function movieSortableTitle(value) {
  return String(value || "").replace(/^the\s+/i, "").trim();
}

// ============================================================= Music =====
// Music Browse mirrors the Movies Explorer/show-detail pattern closely --
// Artist plays the role Show plays for Movies, Album plays the role Season
// does. See the drone-music-feature skill for the full design rationale.
// The one genuinely new pattern here (vs. Movies' one-video-at-a-time modal)
// is the persistent bottom player bar further below, since music listening
// is continuous while browsing rather than one track at a time in a modal.

function musicArtistAlbumKey(artist, album) {
  return `${String(artist || "").toLowerCase().trim()} ${String(album || "").toLowerCase().trim()}`;
}
// Sorts a group's tracks by disc, then track number, then title -- used by
// both the Explorer's album cards (representative selection) and the
// artist/album detail page's track list, so the two stay consistent (same
// role compareShowGroupMembers plays for Movies).
function compareMusicGroupMembers(a, b) {
  const discA = a.disc_number || 0;
  const discB = b.disc_number || 0;
  if (discA !== discB) return discA - discB;
  const trackA = a.track_number != null ? a.track_number : Number.POSITIVE_INFINITY;
  const trackB = b.track_number != null ? b.track_number : Number.POSITIVE_INFINITY;
  if (trackA !== trackB) return trackA - trackB;
  return String(a.display_title || a.track_name || "").localeCompare(String(b.display_title || b.track_name || ""));
}
// Groups every row that has an artist into one card per (artist, album) pair
// -- a track with no album folder groups under its artist with album="" (a
// "Singles" bucket), same as every other track from that artist with no
// album. A row with no artist at all (an orphan file directly under
// music_root) stays its own individual card, same shape as an ungroupable
// movie extra. Grouping key is always the folder-parsed artist/album (see
// HandlersMusicMixin._apply_music_grouping_and_genres), never a scraped
// canonical name, so a partially-scraped album can't split into two cards.
function groupMusicForExplorer(rows) {
  const cards = [];
  const albumGroups = new Map();
  rows.forEach((row) => {
    if (!row.artist) {
      cards.push(row);
      return;
    }
    const key = musicArtistAlbumKey(row.artist, row.album);
    if (!albumGroups.has(key)) albumGroups.set(key, []);
    albumGroups.get(key).push(row);
  });
  albumGroups.forEach((members) => {
    members.sort(compareMusicGroupMembers);
    const representative = members[0];
    cards.push({
      isAlbumGroup: true,
      artist: representative.artist,
      album: representative.album,
      entry_key: representative.entry_key,
      genres: representative.genres || [],
      trackCount: members.length,
    });
  });
  return cards;
}
function musicExplorerCardTitle(entry) {
  if (entry.isAlbumGroup) return entry.album || "Singles";
  return entry.display_title || entry.track_name || entry.name || "";
}
// Sorts primarily by artist (so the grid reads like an alphabetized record
// shelf), then by album/title -- mirrors movieSortableTitle's "ignore a
// leading The" rule for the artist half.
function musicSortableGroupKey(entry) {
  const artist = movieSortableTitle(entry.isAlbumGroup ? entry.artist : entry.artist || "");
  const secondary = entry.isAlbumGroup ? entry.album || "" : entry.display_title || entry.track_name || "";
  return `${artist} ${secondary}`.toLowerCase();
}
// Each facet's counts hold the *other* active facets fixed and ask "how many
// would match if this were selected instead" -- same faceted-search
// convention the Movies Explorer's Type/Genre sidebar uses (see
// movieExplorerTypeCount/movieExplorerGenreCount) -- rather than a flat
// unfiltered total for every button. musicExplorerFilteredRows composes all
// three facets (Artist/Genre/Likes) so each facet's count function only has
// to exclude itself, not hand-chain the other two.
function musicExplorerRowsMatchingArtist(rows, value) {
  if (!value) return rows;
  return rows.filter((m) => (m.artist || "") === value);
}
function musicExplorerRowsMatchingGenre(rows, value) {
  if (!value) return rows;
  return rows.filter((m) => (m.genres || []).includes(value));
}
function musicExplorerRowsMatchingLiked(rows, value) {
  if (!value) return rows;
  return rows.filter((m) => !!m.liked);
}
function musicExplorerFilteredRows(opts = {}) {
  let rows = musicAllRows;
  if (!opts.excludeArtist) rows = musicExplorerRowsMatchingArtist(rows, musicExplorerArtistFilter);
  if (!opts.excludeGenre) rows = musicExplorerRowsMatchingGenre(rows, musicExplorerGenreFilter);
  if (!opts.excludeLiked) rows = musicExplorerRowsMatchingLiked(rows, musicExplorerLikedFilter);
  return rows;
}
function musicExplorerArtists() {
  const artists = new Set();
  musicAllRows.forEach((m) => { if (m.artist) artists.add(m.artist); });
  return [...artists].sort((a, b) => musicExplorerArtistCount(b) - musicExplorerArtistCount(a) || movieSortableTitle(a).localeCompare(movieSortableTitle(b)));
}
function musicExplorerArtistCount(value) {
  return musicExplorerRowsMatchingArtist(musicExplorerFilteredRows({ excludeArtist: true }), value).length;
}
// Any one track by this artist, to build an artist-photo artwork URL from
// (musicArtworkUrl needs a track entry_key, not an artist name) -- opportunistic,
// same as everywhere else artist photos show up: most artists have none, so
// the caller's onerror just removes the <img> rather than showing a placeholder.
function musicArtistRepresentativeEntryKey(artist) {
  const artistKey = String(artist || "").toLowerCase().trim();
  const row = musicAllRows.find((m) => m.artist && String(m.artist).toLowerCase().trim() === artistKey);
  return row ? row.entry_key : null;
}
function musicExplorerGenres() {
  const genres = new Set();
  musicAllRows.forEach((m) => (m.genres || []).forEach((g) => g && genres.add(g)));
  return [...genres].sort((a, b) => musicExplorerGenreCount(b) - musicExplorerGenreCount(a) || a.localeCompare(b));
}
function musicExplorerGenreCount(value) {
  return musicExplorerRowsMatchingGenre(musicExplorerFilteredRows({ excludeGenre: true }), value).length;
}
function musicExplorerLikedCount(value) {
  return musicExplorerRowsMatchingLiked(musicExplorerFilteredRows({ excludeLiked: true }), value).length;
}
function renderMusicExplorerSidebar() {
  const sidebar = document.getElementById("music-explorer-sidebar");
  if (!sidebar) return;
  const artistButton = (value, label) => {
    const representativeEntryKey = value ? musicArtistRepresentativeEntryKey(value) : null;
    const avatar = representativeEntryKey
      ? `<img class="music-artist-sidebar-avatar" src="${escapeHtml(musicArtworkUrl(representativeEntryKey, "artist"))}" alt="" onerror="this.remove();">`
      : "";
    return `
    <button type="button" class="movie-explorer-category-btn ${musicExplorerArtistFilter === value ? "active" : ""}" onclick="setMusicExplorerArtistFilter(${jsAttr(value)})">
      <span class="d-flex align-items-center gap-2 min-width-0 flex-grow-1">${avatar}<span class="text-truncate">${escapeHtml(label)}</span></span><span class="movie-explorer-category-count">${musicExplorerArtistCount(value).toLocaleString()}</span>
    </button>
  `;
  };
  const genreButton = (value, label) => `
    <button type="button" class="movie-explorer-category-btn ${musicExplorerGenreFilter === value ? "active" : ""}" onclick="setMusicExplorerGenreFilter(${jsAttr(value)})">
      <span>${escapeHtml(label)}</span><span class="movie-explorer-category-count">${musicExplorerGenreCount(value).toLocaleString()}</span>
    </button>
  `;
  const likedButton = (value, label) => `
    <button type="button" class="movie-explorer-category-btn ${musicExplorerLikedFilter === value ? "active" : ""}" onclick="setMusicExplorerLikedFilter(${value ? "true" : "false"})">
      <span>${label}</span><span class="movie-explorer-category-count">${musicExplorerLikedCount(value).toLocaleString()}</span>
    </button>
  `;
  const artists = musicExplorerArtists();
  const visibleArtists = musicExplorerShowAllArtists ? artists : artists.slice(0, MUSIC_EXPLORE_TOP_ARTIST_COUNT);
  const canExpandArtists = artists.length > MUSIC_EXPLORE_TOP_ARTIST_COUNT;
  const genres = musicExplorerGenres();
  const visibleGenres = musicExplorerShowAllGenres ? genres : genres.slice(0, MUSIC_EXPLORE_TOP_GENRE_COUNT);
  const canExpandGenres = genres.length > MUSIC_EXPLORE_TOP_GENRE_COUNT;
  sidebar.innerHTML = `
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Likes</div>
      ${likedButton(false, "All Tracks")}
      ${likedButton(true, `<i class="bi bi-hand-thumbs-up-fill me-1"></i>Liked`)}
    </div>
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Artists</div>
      ${artistButton("", "All Artists")}
      ${artists.length ? visibleArtists.map((a) => artistButton(a, a)).join("") : `<div class="text-muted small">No artists found yet.</div>`}
      ${canExpandArtists ? `
        <button type="button" class="movie-explorer-category-btn movie-explorer-sidebar-more-btn" onclick="toggleMusicExplorerShowAllArtists()">
          ${musicExplorerShowAllArtists ? "Show less" : `Show more (${(artists.length - MUSIC_EXPLORE_TOP_ARTIST_COUNT).toLocaleString()})`}
        </button>
      ` : ""}
    </div>
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Genres</div>
      ${genreButton("", "All Genres")}
      ${genres.length ? visibleGenres.map((g) => genreButton(g, g)).join("") : `<div class="text-muted small">Scrape music to see genres.</div>`}
      ${canExpandGenres ? `
        <button type="button" class="movie-explorer-category-btn movie-explorer-sidebar-more-btn" onclick="toggleMusicExplorerShowAllGenres()">
          ${musicExplorerShowAllGenres ? "Show less" : `Show more (${(genres.length - MUSIC_EXPLORE_TOP_GENRE_COUNT).toLocaleString()})`}
        </button>
      ` : ""}
    </div>
  `;
}
function setMusicExplorerLikedFilter(value) {
  musicExplorerLikedFilter = value;
  renderMusicExplorerSidebar();
  filterMusicExplorer(document.getElementById("musicExplorerSearch")?.value || "");
}
function toggleMusicExplorerShowAllArtists() {
  musicExplorerShowAllArtists = !musicExplorerShowAllArtists;
  renderMusicExplorerSidebar();
}
function setMusicExplorerArtistFilter(value) {
  musicExplorerArtistFilter = value;
  renderMusicExplorerSidebar();
  filterMusicExplorer(document.getElementById("musicExplorerSearch")?.value || "");
}
function toggleMusicExplorerShowAllGenres() {
  musicExplorerShowAllGenres = !musicExplorerShowAllGenres;
  renderMusicExplorerSidebar();
}
function setMusicExplorerGenreFilter(value) {
  musicExplorerGenreFilter = value;
  renderMusicExplorerSidebar();
  filterMusicExplorer(document.getElementById("musicExplorerSearch")?.value || "");
}
function filterMusicExplorer(queryValue, opts = {}) {
  const grid = document.getElementById("music-explorer-grid");
  if (!grid) return;
  if (opts.growDisplay) {
    musicExploreDisplayLimit += MUSIC_EXPLORE_PAGE_SIZE;
  } else {
    musicExploreDisplayLimit = MUSIC_EXPLORE_PAGE_SIZE;
  }
  const filter = String(queryValue || "").trim().toLowerCase();
  let rows = musicExplorerFilteredRows();
  if (filter) {
    rows = rows.filter((m) =>
      (m.display_title || m.track_name || m.name || "").toLowerCase().includes(filter)
      || (m.artist || "").toLowerCase().includes(filter)
      || (m.album || "").toLowerCase().includes(filter)
    );
  }
  // The Liked filter is a flat list of the songs you liked, not the albums
  // they happen to live on -- grouping into album cards here would show a
  // whole album (including tracks you never liked) for every one liked
  // song, which isn't what "Liked" means. Every other filter combination
  // still groups into album cards as usual.
  const cards = musicExplorerLikedFilter ? rows.slice() : groupMusicForExplorer(rows);
  const sorted = [...cards].sort((a, b) => musicSortableGroupKey(a).localeCompare(musicSortableGroupKey(b)));
  const visible = sorted.slice(0, musicExploreDisplayLimit);
  grid.innerHTML = visible.length
    ? visible.map(renderMusicExplorerCard).join("")
    : `<div class="text-muted p-4">No music matches the current filters.</div>`;
  renderMusicExplorerMoreButton(visible.length, sorted.length, queryValue);
  setupLazyImages();
}
function renderMusicExplorerMoreButton(shown, total, queryValue) {
  const wrap = document.getElementById("music-explorer-more");
  if (!wrap) return;
  if (shown >= total) {
    wrap.innerHTML = total ? `<span class="small text-muted">Showing all ${total.toLocaleString()}</span>` : "";
    return;
  }
  wrap.innerHTML = `
    <button type="button" class="btn btn-outline-primary btn-sm" onclick="filterMusicExplorer(${jsAttr(queryValue || "")}, { growDisplay: true })">
      <i class="bi bi-plus-circle me-1"></i>Show more (${shown.toLocaleString()} of ${total.toLocaleString()})
    </button>
  `;
}
function renderMusicExplorerCard(entry) {
  const title = musicExplorerCardTitle(entry);
  const subtitle = entry.isAlbumGroup ? entry.artist : entry.artist || "";
  const artUrl = musicArtworkUrl(entry.entry_key, "art");
  const navigateHash = entry.isAlbumGroup
    ? artistDetailHash(entry.artist, entry.album)
    : musicDetailHash(entry.entry_key);
  return `
    <button type="button" class="movie-explorer-card" title="${escapeHtml(title)}" onclick="setHash(${jsAttr(navigateHash)})">
      <div class="movie-explorer-card-poster music-cover-square">
        <img src="" data-src="${escapeHtml(artUrl)}" data-fallbacks='[]' alt="" loading="lazy">
        <div class="movie-explorer-card-poster-fallback d-none"><i class="bi bi-music-note-beamed"></i></div>
      </div>
      <div class="movie-explorer-card-title">${escapeHtml(title)}</div>
      ${subtitle ? `<div class="movie-explorer-card-subtitle text-truncate">${escapeHtml(subtitle)}</div>` : ""}
    </button>
  `;
}
async function renderMusicExplorerPage() {
  currentSystemContext = null;
  clearSystemTheme();
  musicExplorerGenreFilter = "";
  musicExplorerShowAllGenres = false;
  musicExplorerArtistFilter = "";
  musicExplorerShowAllArtists = false;
  musicExplorerLikedFilter = false;
  musicExploreDisplayLimit = MUSIC_EXPLORE_PAGE_SIZE;
  setLoading(true, "Loading music...");
  try {
    if (!musicAllRows.length) {
      const payload = await api("/music");
      musicAllRows = payload.music || [];
    }
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-music-note-beamed me-2"></i>Music</div>
          <div class="movie-explorer-search flex-grow-1">
            <input id="musicExplorerSearch" type="search" class="form-control" placeholder="Search artists, albums, songs" oninput="filterMusicExplorer(this.value)" autofocus>
          </div>
          ${renderAssetTypeSwitcher("music")}
        </div>
        <div class="movie-explorer-body">
          <aside id="music-explorer-sidebar" class="movie-explorer-sidebar"></aside>
          <div class="movie-explorer-grid-wrap min-width-0">
            <div id="music-explorer-grid" class="movie-explorer-grid"></div>
            <div id="music-explorer-more" class="text-center mt-3"></div>
          </div>
        </div>
      </div>
    `;
    renderMusicExplorerSidebar();
    filterMusicExplorer("");
    restoreMovieListScroll(musicExploreHash());
  } catch (err) {
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-music-note-beamed me-2"></i>Music</div>
          ${renderAssetTypeSwitcher("music")}
        </div>
        <div class="alert alert-danger m-3">Failed to load music: ${escapeHtml(err.message || "unknown error")}</div>
      </div>
    `;
  } finally {
    setLoading(false);
  }
}
// Artist detail page (route #music/artist/<artist>[/<album>], reached by
// clicking an album card in the explorer): an album-switcher tab strip above
// that album's track list, mirroring renderShowDetailsPage's season strip.
// Switching albums is just a hash change -- the router re-renders this whole
// page, which both updates the artwork (an on-demand GET /music/{key} fetch
// of the newly-selected album's first track) and keeps the selection
// bookmarkable/back-button-able, same convention as the Movies show page.
async function renderArtistDetailsPage(artist, albumParam) {
  currentSystemContext = null;
  clearSystemTheme();
  setLoading(true, "Loading artist...");
  try {
    if (!musicAllRows.length) {
      const payload = await api("/music");
      musicAllRows = payload.music || [];
    }
    const artistKey = String(artist || "").toLowerCase().trim();
    const tracks = musicAllRows.filter((m) => m.artist && String(m.artist).toLowerCase().trim() === artistKey);
    if (!tracks.length) {
      content.innerHTML = `
        <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#music')"><i class="bi bi-arrow-left me-1"></i>Back to Music</button>
        <div class="alert alert-warning">No tracks found for "${escapeHtml(artist)}".</div>
      `;
      return;
    }
    const albumsMap = new Map();
    tracks.forEach((t) => {
      const key = t.album || "";
      if (!albumsMap.has(key)) albumsMap.set(key, []);
      albumsMap.get(key).push(t);
    });
    // "Singles" (album === "") always sorts last, after every real album name.
    const albumNames = [...albumsMap.keys()].sort((a, b) => {
      if (a === "" && b !== "") return 1;
      if (b === "" && a !== "") return -1;
      return a.localeCompare(b);
    });
    const selectedAlbum = albumNames.includes(albumParam) ? albumParam : albumNames[0];
    const albumTracks = (albumsMap.get(selectedAlbum) || []).slice().sort(compareMusicGroupMembers);
    const representative = albumTracks[0];
    let detail = null;
    try {
      detail = await api(`/music/${encodeURIComponent(representative.entry_key)}`);
    } catch (_) {
      detail = null; // unscraped album -- render with no art rather than failing the page
    }
    const meta = detail && detail.metadata;
    // Always attempt the artwork URL rather than gating on scraped metadata
    // -- the endpoint itself falls back to a local cover.jpg/folder.jpg on
    // disk when nothing's been scraped (see handlers_music._handle_music_artwork),
    // so an unscraped album can still show real art; onerror below covers
    // the case where neither exists.
    const artUrl = musicArtworkUrl(representative.entry_key, "art");
    const genres = (meta && meta.genres) || [];
    const albumLabel = selectedAlbum || "Singles";
    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#music')"><i class="bi bi-arrow-left me-1"></i>Back to Music</button>
      <div class="movie-detail-hero">
        <div class="movie-detail-hero-body">
          <img class="movie-detail-poster music-cover-square" src="${escapeHtml(artUrl)}" alt="" onerror="this.style.display='none'; this.nextElementSibling.classList.remove('d-none');">
          <div class="movie-detail-poster music-cover-square movie-detail-poster-placeholder d-none"><i class="bi bi-music-note-beamed"></i></div>
          <div class="movie-detail-info min-width-0">
            <div class="small text-muted mb-1"><span class="badge text-bg-info me-2">Artist</span>${albumTracks.length} track${albumTracks.length === 1 ? "" : "s"}</div>
            <div class="d-flex align-items-center gap-2">
              <img class="music-artist-avatar" src="${escapeHtml(musicArtworkUrl(representative.entry_key, "artist"))}" alt="" onerror="this.remove();">
              <h2 class="movie-detail-title mb-0" title="${escapeHtml(artist)}">${escapeHtml(artist)} &middot; ${escapeHtml(albumLabel)}</h2>
            </div>
            ${genres.length ? `<div class="mb-2">${genres.map((g) => `<span class="badge movie-genre-badge">${escapeHtml(g)}</span>`).join(" ")}</div>` : ""}
            <div class="d-flex flex-wrap gap-2 mt-2">
              <button class="btn btn-primary btn-sm" type="button" title="Play Album" onclick="playMusicAlbum(${jsAttr(artist)}, ${jsAttr(selectedAlbum)})"><i class="bi bi-play-circle"></i></button>
              ${
                adminEnabled
                  ? `<button class="btn btn-outline-light btn-sm" type="button" title="Upload a cover image for this album -- applies to every track" onclick="openMusicAlbumArtPicker(${jsAttr(representative.entry_key)}, ${jsAttr(artist)}, ${jsAttr(selectedAlbum)})"><i class="bi bi-image"></i></button>`
                  : ""
              }
            </div>
          </div>
        </div>
      </div>
      <div class="movie-detail-body">
        <div class="list-group">
          ${albumTracks.map((t) => renderArtistDetailTrackRow(t, artist, selectedAlbum)).join("")}
        </div>
        ${renderMusicAlbumScraperCard(representative.entry_key, artist, selectedAlbum, !!meta)}
      </div>
    `;
  } catch (err) {
    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#music')"><i class="bi bi-arrow-left me-1"></i>Back to Music</button>
      <div class="alert alert-danger">Failed to load artist: ${escapeHtml(err.message || "unknown error")}</div>
    `;
  } finally {
    setLoading(false);
  }
}
function renderArtistDetailTrackRow(track, artist, album) {
  const label = track.track_number != null
    ? `${String(track.track_number).padStart(2, "0")} - ${track.display_title || track.track_name || ""}`
    : (track.display_title || track.track_name || "");
  const liked = !!track.liked;
  return `
    <div class="list-group-item d-flex align-items-center justify-content-between gap-2 bg-transparent">
      <button type="button" class="btn btn-link btn-sm p-0 text-start text-truncate min-width-0" title="${escapeHtml(track.file_path || track.track_name || "")}" onclick="setHash(musicDetailHash(${jsAttr(track.entry_key)}))">${escapeHtml(label)}</button>
      <div class="d-flex gap-2 text-nowrap">
        ${musicLikeButtonHtml(track.entry_key, liked, "icon")}
        <button class="btn btn-outline-primary btn-sm" type="button" title="Play" onclick="playMusicTrackFromAlbum(${jsAttr(track.entry_key)}, ${jsAttr(artist)}, ${jsAttr(album)})"><i class="bi bi-play-circle"></i></button>
        ${
          track.is_downloadable === false
            ? `<button class="btn btn-secondary btn-sm" type="button" title="Downloads disabled" disabled><i class="bi bi-slash-circle"></i></button>`
            : `<a class="btn btn-primary btn-sm" title="Download" href="${musicDownloadUrl(track.entry_key)}"><i class="bi bi-download"></i></a>`
        }
      </div>
    </div>
  `;
}
// Shared like-button markup for every entry_key-scoped like control (track
// rows, both detail pages, the player bar), so toggleMusicLike has one
// consistent shape to rebuild after a toggle instead of several. "text" is
// unused today (every call site is icon-only) but kept as a real branch --
// data-variant round-trips through toggleMusicLike's rebuild either way.
function musicLikeButtonHtml(entryKey, liked, variant) {
  const sizeClass = variant === "icon" ? " btn-sm" : "";
  const iconClass = liked ? "bi-hand-thumbs-up-fill" : "bi-hand-thumbs-up";
  const label = variant === "text" ? (liked ? "Liked" : "Like") : "";
  return `<button class="btn${sizeClass} ${liked ? "btn-primary" : "btn-outline-secondary"}" type="button" data-variant="${variant}" data-music-like-key="${escapeHtml(entryKey)}" title="${liked ? "Unlike" : "Like"}" onclick="toggleMusicLike(${jsAttr(entryKey)}, ${liked}, this)"><i class="bi ${iconClass}${label ? " me-1" : ""}"></i>${label}</button>`;
}
// Toggles a track's liked flag and patches the DOM in place (no full
// re-render) -- mirrors the lightweight optimistic-update shape used
// elsewhere in this file (e.g. deleteMusicAlbumScraperMetadata re-renders
// the whole page, but a single icon toggle doesn't need that). Also patches
// musicAllRows in place so the Likes sidebar filter/count reflects the
// change immediately without a re-fetch.
//
// The same track's like button can be visible in more than one place at
// once (a track row on the artist/album page AND the persistent player bar
// showing that same track) -- every matching button, wherever toggled from,
// is kept in sync via the shared data-music-like-key attribute rather than
// only patching the one that was actually clicked.
async function toggleMusicLike(entryKey, likedNow, button) {
  const nextLiked = !likedNow;
  const matches = document.querySelectorAll(`[data-music-like-key="${entryKey}"]`);
  matches.forEach((el) => { el.disabled = true; });
  try {
    await apiPost(`/music/${encodeURIComponent(entryKey)}/like`, { liked: nextLiked });
    const row = musicAllRows.find((m) => m.entry_key === entryKey);
    if (row) row.liked = nextLiked;
    document.querySelectorAll(`[data-music-like-key="${entryKey}"]`).forEach((el) => {
      const variant = el.dataset.variant || "icon";
      el.outerHTML = musicLikeButtonHtml(entryKey, nextLiked, variant);
    });
  } catch (err) {
    showToast(`Failed to update like: ${escapeHtml(err.message || "unknown error")}`, "danger");
    matches.forEach((el) => { el.disabled = false; });
  }
}
async function renderMusicDetailsPage(entryKey) {
  currentSystemContext = null;
  clearSystemTheme();
  setLoading(true, "Loading track...");
  try {
    const track = await api(`/music/${encodeURIComponent(entryKey)}`);
    content.innerHTML = renderMusicDetailShell(track);
  } catch (err) {
    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#music')"><i class="bi bi-arrow-left me-1"></i>Back to Music</button>
      <div class="alert alert-danger">Failed to load track: ${escapeHtml(err.message || "unknown error")}</div>
    `;
  } finally {
    setLoading(false);
  }
}
function renderMusicDetailShell(track) {
  const meta = track.metadata || null;
  const rawName = track.track_name || track.name || "";
  const title = track.display_title || rawName;
  const entryKey = track.entry_key;
  // Always attempt the artwork URL -- see the matching comment in the
  // artist/album detail page for why (local cover.jpg/folder.jpg fallback
  // lives server-side in _handle_music_artwork).
  const artUrl = musicArtworkUrl(entryKey, "art");
  const genres = (meta && meta.genres) || [];
  const artistLabel = (meta && meta.artist) || track.artist || "";
  const albumLabel = (meta && meta.album) || track.album || "";
  const metaBits = [artistLabel, albumLabel, meta && meta.release_date ? String(meta.release_date).slice(0, 4) : ""].filter(Boolean);
  return `
    <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#music')"><i class="bi bi-arrow-left me-1"></i>Back to Music</button>
    <div class="movie-detail-hero">
      <div class="movie-detail-hero-body">
        <img class="movie-detail-poster music-cover-square" src="${escapeHtml(artUrl)}" alt="" onerror="this.style.display='none'; this.nextElementSibling.classList.remove('d-none');">
        <div class="movie-detail-poster music-cover-square movie-detail-poster-placeholder d-none"><i class="bi bi-music-note-beamed"></i></div>
        <div class="movie-detail-info min-width-0">
          <h2 class="movie-detail-title" title="${escapeHtml(title)}">${escapeHtml(title)}</h2>
          ${metaBits.length ? `<div class="text-muted small mb-2">${metaBits.map((bit) => escapeHtml(bit)).join(" &middot; ")}</div>` : ""}
          ${genres.length ? `<div class="mb-3">${genres.map((g) => `<span class="badge movie-genre-badge">${escapeHtml(g)}</span>`).join(" ")}</div>` : ""}
          <div class="d-flex flex-wrap gap-2 mb-2">
            <button class="btn btn-primary btn-sm" type="button" title="Play" onclick="playMusicTrack(${jsAttr(entryKey)}, ${jsAttr(title)}, ${jsAttr(track.artist || artistLabel)}, ${!!track.liked}, ${jsAttr(track.album)})"><i class="bi bi-play-circle"></i></button>
            ${musicLikeButtonHtml(entryKey, !!track.liked, "icon")}
            ${
              track.is_downloadable === false
                ? `<button class="btn btn-outline-secondary btn-sm" type="button" title="Downloads disabled" disabled><i class="bi bi-slash-circle"></i></button>`
                : `<a class="btn btn-outline-primary btn-sm" title="Download" href="${musicDownloadUrl(entryKey)}"><i class="bi bi-download"></i></a>`
            }
            ${
              adminEnabled
                ? `<button class="btn btn-outline-danger btn-sm" type="button" title="Delete" onclick="deleteMusicFromDetailPage(${jsAttr(entryKey)}, ${jsAttr(title)})"><i class="bi bi-trash"></i></button>`
                : ""
            }
          </div>
        </div>
      </div>
    </div>
    <div class="movie-detail-body">
      <div class="text-muted small">${escapeHtml(track.file_path || rawName)} &middot; ${escapeHtml(formatBytes(track.byte_count ?? track.file_size))}</div>
    </div>
  `;
}
// Track files are gone from disk after this -- invalidate the whole
// client-side music cache (not just the deleted row), same reasoning as
// deleteMoviesBatch: every music view reads from the same musicAllRows
// snapshot with no per-row invalidation of its own.
async function deleteMusicBatch(entryKeys) {
  const result = await apiPost("/admin/music/delete", { entry_keys: entryKeys });
  musicAllRows = [];
  return result;
}
function deleteMusicFromDetailPage(entryKey, title) {
  openConfirmDeleteModal({
    title: "Delete track?",
    body: `<strong>${escapeHtml(title)}</strong> will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      setLoading(true, "Deleting...");
      try {
        await deleteMusicBatch([entryKey]);
        showToast("Track deleted.", "success");
        setHash("#music");
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
}
// Lives at the bottom of the artist/album detail page, not the track detail
// page -- scraping is album-level now (one release's art + release-level
// metadata applied to every track in the group, see
// music/metadata_manager.apply_album), so there's no per-track scraper UI
// anymore. entryKey is any one representative track in the album (the
// backend expands it to the whole group server-side, see
// handlers_music._album_group_entry_keys); artist/album are only needed
// here to know which page to re-render afterward. Not shown for the
// "Singles" pseudo-group (album === "") -- there's no one release to search
// for a collection of unrelated standalone tracks. Mirrors the old
// per-track card's shape closely (no API-key gate, same as before --
// MusicBrainz + Cover Art Archive are both keyless).
function renderMusicAlbumScraperCard(entryKey, artist, album, hasMetadata) {
  if (!adminEnabled || album === "") return "";
  return `
    <div class="card mt-4">
      <div class="card-header d-flex align-items-center justify-content-between gap-2">
        <span><i class="bi bi-cloud-download me-1"></i>Artwork &amp; Metadata (MusicBrainz)</span>
        ${
          hasMetadata
            ? `<button class="btn btn-outline-danger btn-sm" type="button" onclick="deleteMusicAlbumScraperMetadata(${jsAttr(entryKey)}, ${jsAttr(artist)}, ${jsAttr(album)})"><i class="bi bi-trash me-1"></i>Remove scraped data</button>`
            : ""
        }
      </div>
      <div class="card-body">
        <div class="input-group mb-3">
          <input id="musicAlbumScraperQuery" type="text" class="form-control" value="" placeholder="Leave blank to search by artist/album">
          <button class="btn btn-primary" type="button" onclick="searchMusicAlbumScraper(${jsAttr(entryKey)}, ${jsAttr(artist)}, ${jsAttr(album)})"><i class="bi bi-search me-1"></i>Search</button>
        </div>
        <div id="music-album-scraper-results" class="mb-3"></div>
      </div>
    </div>
  `;
}
async function deleteMusicAlbumScraperMetadata(entryKey, artist, album) {
  if (!window.confirm("Remove the scraped MusicBrainz metadata and artwork for this album? This cannot be undone, but you can re-scrape it afterward.")) return;
  setLoading(true, "Removing scraped metadata...");
  try {
    await apiPost(`/admin/music/${encodeURIComponent(entryKey)}/scrape/delete`, {});
    musicAllRows = []; // invalidate the client-side inventory cache, same as uploadMusicAlbumArt
    showToast("Scraped metadata removed.", "success");
    await renderArtistDetailsPage(artist, album);
  } catch (err) {
    showToast(`Failed to remove scraped metadata: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function searchMusicAlbumScraper(entryKey, artist, album) {
  const resultsEl = document.getElementById("music-album-scraper-results");
  if (!resultsEl) return;
  const queryInput = document.getElementById("musicAlbumScraperQuery");
  const query = queryInput ? queryInput.value.trim() : "";
  resultsEl.innerHTML = `<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Searching MusicBrainz...</div>`;
  try {
    const data = await api(`/admin/music/${encodeURIComponent(entryKey)}/scrape/search?q=${encodeURIComponent(query)}`);
    if (queryInput && !query && data.query) queryInput.value = data.query;
    const results = data.results || [];
    resultsEl.innerHTML = results.length
      ? `<div class="list-group">${results.map((r) => renderMusicAlbumScraperResult(entryKey, artist, album, r)).join("")}</div>`
      : `<div class="text-muted small">No MusicBrainz matches found${data.query ? ` for "${escapeHtml(data.query)}"` : ""}.</div>`;
  } catch (err) {
    resultsEl.innerHTML = `<div class="alert alert-warning small mb-0">Search failed: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}
// Only ever "release" results now (see
// music/metadata_manager.search_album_default_query) -- no more "recording"
// shape/subtitle branch to handle, since applying always applies a whole
// release to the whole album group.
function renderMusicAlbumScraperResult(entryKey, artist, album, result) {
  const year = result.date ? String(result.date).slice(0, 4) : "";
  const subtitle = [result.artist, result.track_count ? `${result.track_count} tracks` : ""].filter(Boolean).join(" · ");
  return `
    <button type="button" class="list-group-item list-group-item-action" onclick="applyMusicAlbumScraperResult(${jsAttr(entryKey)}, ${jsAttr(artist)}, ${jsAttr(album)}, ${jsAttr(result.release_mbid || "")}, this)">
      <div class="d-flex gap-3 align-items-center">
        <div class="match-thumb-placeholder"><i class="bi bi-disc"></i></div>
        <div class="min-width-0">
          <div class="fw-semibold">${escapeHtml(result.title || "")}${year ? ` <span class="text-muted">(${escapeHtml(year)})</span>` : ""}</div>
          ${subtitle ? `<div class="text-muted small text-truncate-2">${escapeHtml(subtitle)}</div>` : ""}
        </div>
      </div>
    </button>
  `;
}
async function applyMusicAlbumScraperResult(entryKey, artist, album, releaseMbid, button) {
  if (!releaseMbid) {
    showToast("That result has no associated release to scrape from.", "warning");
    return;
  }
  const originalHtml = button ? button.innerHTML : "";
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
  }
  setLoading(true, "Downloading artwork and metadata from MusicBrainz...");
  try {
    const result = await apiPost(`/admin/music/${encodeURIComponent(entryKey)}/scrape/apply`, { release_mbid: releaseMbid });
    const bustToken = Date.now();
    (result.entry_keys || []).forEach((key) => musicArtCacheBust.set(key, bustToken));
    musicAllRows = []; // invalidate the client-side inventory cache, same as uploadMusicAlbumArt
    showToast(`Album artwork and metadata updated for ${result.updated} track${result.updated === 1 ? "" : "s"}.`, "success");
    await renderArtistDetailsPage(artist, album);
  } catch (err) {
    showToast(`Failed to apply MusicBrainz match: ${escapeHtml(err.message || "unknown error")}`, "danger");
    if (button) {
      button.disabled = false;
      button.innerHTML = originalHtml;
    }
  } finally {
    setLoading(false);
  }
}

// ---------------------------------------------------- persistent player bar
// Unlike the Movies player (a one-video-at-a-time Bootstrap modal, closed on
// navigation), music listening is continuous while browsing -- so this bar
// is mounted once at app-shell level (ensureMusicPlayerBar(), called from
// startApp()) and survives every router() content.innerHTML swap, same
// "created once, persists across page renders" shape as ensureToastContainer.
let musicPlayerQueue = [];
let musicPlayerQueueIndex = -1;
let musicPlayerCurrentEntryKey = null;
// The currently playing track's *folder-derived* artist/album (never a
// scraped canonical name -- see the module docstring's grouping-key rule)
// -- openMusicPlayerBarAlbum() navigates with these, so they must match
// the same keys renderArtistDetailsPage filters musicAllRows by, or the
// destination page would come up empty.
let musicPlayerCurrentArtist = null;
let musicPlayerCurrentAlbum = null;
// Shuffle plays a random track from the whole library instead of stepping
// through the current queue (single track/album). It's independent of
// which queue is loaded and persists across track changes until explicitly
// turned off or the bar is closed. musicPlayerShuffleHistory backs the
// Previous button while shuffle is on, since there's no sequential queue
// index to step backward through in that mode.
let musicPlayerShuffle = false;
let musicPlayerShuffleHistory = [];
function ensureMusicPlayerBar() {
  if (document.getElementById("musicPlayerBar")) return;
  const bar = document.createElement("div");
  bar.id = "musicPlayerBar";
  bar.className = "music-player-bar d-none";
  bar.innerHTML = `
    <div class="music-player-bar-art" role="button" tabindex="0" title="Back to album" onclick="openMusicPlayerBarAlbum()" onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openMusicPlayerBarAlbum(); }">
      <img id="musicPlayerBarArt" src="" alt="" onerror="this.style.display='none'; this.nextElementSibling.classList.remove('d-none');">
      <div class="music-player-bar-art-fallback d-none"><i class="bi bi-music-note-beamed"></i></div>
    </div>
    <div class="music-player-bar-info min-width-0">
      <div id="musicPlayerBarTitle" class="text-truncate fw-semibold"></div>
      <div id="musicPlayerBarArtist" class="text-truncate text-muted small"></div>
    </div>
    <div class="music-player-bar-controls d-flex align-items-center gap-2">
      <span id="musicPlayerBarLike"></span>
      <button type="button" id="musicPlayerBarShuffle" class="btn btn-outline-light btn-sm" title="Shuffle" aria-pressed="false" onclick="toggleMusicPlayerShuffle()"><i class="bi bi-shuffle"></i></button>
      <button type="button" class="btn btn-outline-light btn-sm" title="Previous" onclick="playMusicPlayerPrevious()"><i class="bi bi-skip-start-fill"></i></button>
      <audio id="musicPlayerBarAudio" controls></audio>
      <button type="button" class="btn btn-outline-light btn-sm" title="Next" onclick="playMusicPlayerNext()"><i class="bi bi-skip-end-fill"></i></button>
      <button type="button" class="btn btn-outline-light btn-sm" title="Close" onclick="closeMusicPlayerBar()"><i class="bi bi-x-lg"></i></button>
    </div>
  `;
  document.body.appendChild(bar);
  document.getElementById("musicPlayerBarAudio").addEventListener("ended", () => playMusicPlayerNext());
}
function closeMusicPlayerBar() {
  const bar = document.getElementById("musicPlayerBar");
  const audio = document.getElementById("musicPlayerBarAudio");
  if (audio) {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
  }
  bar?.classList.add("d-none");
  document.body.classList.remove("music-player-bar-active");
  musicPlayerQueue = [];
  musicPlayerQueueIndex = -1;
  musicPlayerCurrentEntryKey = null;
  musicPlayerCurrentArtist = null;
  musicPlayerCurrentAlbum = null;
  musicPlayerShuffle = false;
  musicPlayerShuffleHistory = [];
  updateMusicPlayerBarShuffleButton();
}
// Flips shuffle mode and refreshes the toggle button's pressed styling.
// Turning shuffle off drops the shuffle-only history, same as closing the
// bar, since it only makes sense as a Previous stack while shuffle is on.
function toggleMusicPlayerShuffle() {
  musicPlayerShuffle = !musicPlayerShuffle;
  if (!musicPlayerShuffle) musicPlayerShuffleHistory = [];
  updateMusicPlayerBarShuffleButton();
}
function updateMusicPlayerBarShuffleButton() {
  const button = document.getElementById("musicPlayerBarShuffle");
  if (!button) return;
  button.classList.toggle("btn-primary", musicPlayerShuffle);
  button.classList.toggle("btn-outline-light", !musicPlayerShuffle);
  button.setAttribute("aria-pressed", String(musicPlayerShuffle));
}
// Picks a uniformly random track from the whole library (musicAllRows),
// excluding the currently playing one when more than one track exists so
// shuffle doesn't immediately repeat, and records the outgoing track in
// musicPlayerShuffleHistory so Previous can step back through shuffle picks.
function playRandomMusicTrack() {
  const candidates = musicAllRows.filter((m) => m.entry_key !== musicPlayerCurrentEntryKey);
  const pool = candidates.length ? candidates : musicAllRows;
  if (!pool.length) return;
  const next = pool[Math.floor(Math.random() * pool.length)];
  if (musicPlayerCurrentEntryKey) musicPlayerShuffleHistory.push(musicPlayerCurrentEntryKey);
  _playMusicPlayerEntry(next.entry_key, next.display_title || next.track_name, next.artist, !!next.liked, next.album);
}
// Plays one track with no album queue context (e.g. the plain track detail
// page's Play button) -- next/previous are no-ops until a real queue is set
// via playMusicAlbum/playMusicTrackFromAlbum. `liked` is passed in by the
// caller (rather than looked up here) since musicAllRows may not be loaded
// yet if the user navigated straight to a track detail page URL. `artist`/
// `album` must be the folder-derived values (track.artist/track.album from
// the detail payload, not a scraped meta.artist/meta.album) -- see
// musicPlayerCurrentArtist's docstring for why.
function playMusicTrack(entryKey, title, artist, liked, album) {
  musicPlayerQueue = [entryKey];
  musicPlayerQueueIndex = 0;
  _playMusicPlayerEntry(entryKey, title, artist, !!liked, album);
}
// Sets the queue to a whole album's tracks (disc/track-number order) and
// starts playback at one specific track -- what the artist/album detail
// page's per-row Play button calls, so next/previous walk the rest of that
// album.
function playMusicTrackFromAlbum(entryKey, artist, album) {
  const tracks = musicAlbumTracksSorted(artist, album);
  musicPlayerQueue = tracks.map((t) => t.entry_key);
  musicPlayerQueueIndex = Math.max(0, musicPlayerQueue.indexOf(entryKey));
  const track = tracks.find((t) => t.entry_key === entryKey) || tracks[0];
  if (track) _playMusicPlayerEntry(track.entry_key, track.display_title || track.track_name, artist, !!track.liked, album);
}
function playMusicAlbum(artist, album) {
  const tracks = musicAlbumTracksSorted(artist, album);
  if (!tracks.length) return;
  musicPlayerQueue = tracks.map((t) => t.entry_key);
  musicPlayerQueueIndex = 0;
  const first = tracks[0];
  _playMusicPlayerEntry(first.entry_key, first.display_title || first.track_name, artist, !!first.liked, album);
}
function musicAlbumTracksSorted(artist, album) {
  const artistKey = String(artist || "").toLowerCase().trim();
  const albumKey = String(album || "");
  return musicAllRows
    .filter((m) => m.artist && String(m.artist).toLowerCase().trim() === artistKey && (m.album || "") === albumKey)
    .slice()
    .sort(compareMusicGroupMembers);
}
function playMusicPlayerNext() {
  if (musicPlayerShuffle) {
    playRandomMusicTrack();
    return;
  }
  if (musicPlayerQueueIndex < 0 || musicPlayerQueueIndex >= musicPlayerQueue.length - 1) return;
  musicPlayerQueueIndex += 1;
  _playMusicPlayerEntryFromQueue();
}
function playMusicPlayerPrevious() {
  if (musicPlayerShuffle) {
    if (!musicPlayerShuffleHistory.length) return;
    const entryKey = musicPlayerShuffleHistory.pop();
    const row = musicAllRows.find((m) => m.entry_key === entryKey);
    if (row) _playMusicPlayerEntry(row.entry_key, row.display_title || row.track_name, row.artist, !!row.liked, row.album);
    return;
  }
  if (musicPlayerQueueIndex <= 0) return;
  musicPlayerQueueIndex -= 1;
  _playMusicPlayerEntryFromQueue();
}
function _playMusicPlayerEntryFromQueue() {
  const entryKey = musicPlayerQueue[musicPlayerQueueIndex];
  if (!entryKey) return;
  const row = musicAllRows.find((m) => m.entry_key === entryKey);
  _playMusicPlayerEntry(entryKey, row ? (row.display_title || row.track_name) : entryKey, row ? row.artist : "", !!(row && row.liked), row ? row.album : null);
}
function _playMusicPlayerEntry(entryKey, title, artist, liked, album) {
  ensureMusicPlayerBar();
  musicPlayerCurrentEntryKey = entryKey;
  musicPlayerCurrentArtist = artist || null;
  musicPlayerCurrentAlbum = album != null ? album : null;
  const bar = document.getElementById("musicPlayerBar");
  const audio = document.getElementById("musicPlayerBarAudio");
  const art = document.getElementById("musicPlayerBarArt");
  document.getElementById("musicPlayerBarTitle").textContent = title || "";
  document.getElementById("musicPlayerBarArtist").textContent = artist || "";
  if (art) {
    art.style.display = "";
    art.nextElementSibling?.classList.add("d-none");
    art.src = musicArtworkUrl(entryKey, "art");
  }
  updateMusicPlayerBarLikeButton(entryKey, !!liked);
  audio.src = musicStreamUrl(entryKey);
  bar.classList.remove("d-none");
  // Reserves bottom space on every page (see the body.music-player-bar-active
  // rule in drone.css) so the fixed bar never covers the last row of
  // on-screen content -- removed again in closeMusicPlayerBar.
  document.body.classList.add("music-player-bar-active");
  audio.play().catch(() => {});
}
// Clicking the player bar's album art navigates back to that track's
// album/song detail page -- artistDetailHash's null-vs-empty-string
// distinction matters here: an artist with no album grouping at all (never
// happens once something's actually playing, but guarded anyway) leaves
// musicPlayerCurrentAlbum null, which lands on the artist's default album
// rather than a specific one, same fallback renderArtistDetailsPage itself
// uses for an unrecognized album param.
function openMusicPlayerBarAlbum() {
  if (!musicPlayerCurrentArtist) return;
  setHash(artistDetailHash(musicPlayerCurrentArtist, musicPlayerCurrentAlbum));
}
// Rebuilds the player bar's like button for whichever track is now current
// -- called whenever a new track starts (from _playMusicPlayerEntry); the
// data-music-like-key-based sync in toggleMusicLike handles keeping it in
// sync with the rest of the page once a track is already showing.
function updateMusicPlayerBarLikeButton(entryKey, liked) {
  const container = document.getElementById("musicPlayerBarLike");
  if (!container) return;
  container.innerHTML = musicLikeButtonHtml(entryKey, liked, "icon");
}
// Casting (Chromecast/AirPlay) -- both receivers fetch the video file
// themselves, directly, with no browser in the loop, so neither this app's
// session cookie nor its self-signed HTTPS cert works for them. Casting a
// movie mints a short-lived, single-movie-scoped token
// (POST /movies/{entryKey}/cast-token, see handlers_movies.py) good on a
// second, plain-HTTP-only listener (on by default, settings.cast_enabled --
// a Drone with it turned off gets a clear error here, not a silent no-op)
// instead.
let currentPlayerEntryKey = null;
let currentPlayerName = "";
let castApiReady = false;
let castSdkStatus = "loading";
let castDeviceState = "unknown";
let castLoadInProgress = false;
async function mintMovieCastToken(entryKey) {
  try {
    return await apiPost(`/movies/${encodeURIComponent(entryKey)}/cast-token`, {});
  } catch (err) {
    showToast(`Could not prepare casting: ${escapeHtml(err.message || "unknown error")}`, "danger");
    return null;
  }
}
async function castMovieAirPlay(entryKey) {
  // Open synchronously while the click still carries user activation. The
  // HTTP AirPlay controller is navigated into this window after FFmpeg is
  // ready; opening only after that await would be blocked as a popup.
  const airplayWindow = window.open("", "_blank");
  if (!airplayWindow) {
    showToast("Safari blocked the AirPlay window. Allow pop-ups for this Drone and try again.", "warning", 12000);
    return;
  }
  try {
    airplayWindow.document.title = "Preparing Drone AirPlay";
    airplayWindow.document.body.innerHTML = '<p style="font: 1rem system-ui; padding: 2rem">Preparing the TV-compatible stream…</p>';
  } catch (_) {
    // A browser may restrict even the initial about:blank document; the
    // navigation below can still succeed.
  }
  const button = document.getElementById("movieAirPlayButton");
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Preparing…`;
  }
  const castInfo = await mintMovieCastToken(entryKey);
  if (button) {
    button.disabled = false;
    button.innerHTML = `<i class="bi bi-airplay me-1"></i>AirPlay`;
  }
  if (!castInfo) {
    airplayWindow.close();
    return;
  }
  try {
    if (!castInfo.airplay_url) throw new Error("AirPlay controller URL is missing");
    airplayWindow.location.replace(castInfo.airplay_url);
    showToast("AirPlay is prepared in the new window. Tap Choose AirPlay device there.", "info", 10000);
  } catch (error) {
    airplayWindow.close();
    console.warn("Safari AirPlay controller failed", error);
    showToast("Safari could not open the AirPlay controller. Allow pop-ups for this Drone and try again.", "danger", 12000);
  }
}
// Loaded lazily (only once the movie player modal has actually been opened,
// not on every page load) since it fetches an external script from Google.
function loadCastSenderSdk() {
  if (window.cast?.framework && window.chrome?.cast) {
    initCastApi();
    return;
  }
  if (document.getElementById("google-cast-sdk-script")) {
    updateMovieCastButton();
    return;
  }
  window["__onGCastApiAvailable"] = function (isAvailable) {
    if (isAvailable) {
      initCastApi();
    } else {
      castSdkStatus = "unavailable";
      updateMovieCastButton();
    }
  };
  const script = document.createElement("script");
  script.id = "google-cast-sdk-script";
  script.src = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";
  script.onerror = () => {
    castSdkStatus = "failed";
    updateMovieCastButton();
  };
  document.head.appendChild(script);
}
function initCastApi() {
  if (castApiReady || !window.cast?.framework) return;
  castApiReady = true;
  castSdkStatus = "ready";
  const context = cast.framework.CastContext.getInstance();
  context.setOptions({
    receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
    autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
  });
  const syncCastState = () => {
    castDeviceState = String(context.getCastState?.() || "unknown");
    updateMovieCastButton();
  };
  context.addEventListener(cast.framework.CastContextEventType.CAST_STATE_CHANGED, syncCastState);
  context.addEventListener(cast.framework.CastContextEventType.SESSION_STATE_CHANGED, (event) => {
    // Loading is owned by castMovieChromecast(), after requestSession()
    // resolves. Keeping this listener UI-only prevents SESSION_STARTED and
    // the click path from racing and loading the same movie twice.
    syncCastState();
  });
  syncCastState();
}
function updateMovieCastButton() {
  const button = document.getElementById("movieCastButton");
  if (!button) return;
  if (castLoadInProgress) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Preparing…`;
    return;
  }
  button.disabled = false;
  if (castSdkStatus === "loading") {
    button.innerHTML = `<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Cast`;
    button.title = "Loading Google Cast device discovery";
  } else if (castSdkStatus !== "ready") {
    button.innerHTML = `<i class="bi bi-cast me-1"></i>Cast unavailable`;
    button.title = "Google Cast could not start in this browser";
  } else if (castDeviceState === String(cast.framework.CastState.NO_DEVICES_AVAILABLE)) {
    button.innerHTML = `<i class="bi bi-cast me-1"></i>No Chromecast`;
    button.title = "No Chromecast or Google TV receiver was found (Fire TV/AirPlay devices are not Google Cast receivers)";
  } else if (castDeviceState === String(cast.framework.CastState.CONNECTED)) {
    button.innerHTML = `<i class="bi bi-cast me-1"></i>Choose device`;
    button.title = "Choose or confirm the Google Cast device";
  } else {
    button.innerHTML = `<i class="bi bi-cast me-1"></i>Cast`;
    button.title = "Choose a Google Cast device";
  }
}
function castRequestErrorText(error) {
  const code = String((error && (error.code || error.description)) || error || "").toLowerCase();
  if (code.includes("cancel")) return "";
  if (code.includes("receiver_unavailable")) {
    return "No Google Cast receiver is available. Chromecast and Google TV use Google Cast; Fire TV and AirPlay-only TVs do not.";
  }
  if (code.includes("session_error")) {
    return "The selected device could not start Google Cast. Choose a Chromecast or Google TV. For a Fire TV or AirPlay-only TV, open this page in Safari and use AirPlay.";
  }
  if (code.includes("extension_missing") || code.includes("api_not_initialized")) {
    return "Google Cast is unavailable in this browser. Use Chrome or Edge over HTTPS, then reload the page.";
  }
  return code ? `Could not connect to a Cast device (${code}).` : "Could not connect to a Cast device.";
}
async function castMovieChromecast(entryKey) {
  if (!castApiReady || castSdkStatus !== "ready") {
    showToast(
      "Google Cast is not ready. Use a Cast-capable Chrome or Edge browser over HTTPS and allow local-network device access.",
      "warning",
      12000,
    );
    return;
  }
  const context = cast.framework.CastContext.getInstance();
  if (context.getCastState?.() === cast.framework.CastState.NO_DEVICES_AVAILABLE) {
    showToast(
      "No Chromecast or Google TV is visible on this network. Fire TV is not a Google Cast receiver; open this page in Safari and use AirPlay for Fire TV.",
      "warning",
      12000,
    );
    return;
  }
  castLoadInProgress = true;
  updateMovieCastButton();
  try {
    // Official Cast Framework picker: the user chooses (or confirms) the
    // receiver here, even if this origin auto-joined an earlier session. This
    // call stays directly in the click handler to preserve the browser's
    // transient user activation requirement.
    let session;
    try {
      await context.requestSession();
      session = context.getCurrentSession();
    } catch (error) {
      // Auto-join and SESSION_STARTED can complete at the same time as the
      // picker promise rejects. If the framework did establish a usable
      // session, continue with that selected receiver instead of displaying
      // a false session_error and abandoning the cast.
      session = context.getCurrentSession();
      if (!session) throw error;
    }
    if (!session) throw new Error("receiver_unavailable");
    await loadMovieOntoCastSession(entryKey, session);
  } catch (error) {
    console.warn("Google Cast session request failed", error);
    const message = castRequestErrorText(error);
    if (message) showToast(message, "danger", 12000);
  } finally {
    castLoadInProgress = false;
    updateMovieCastButton();
  }
}
// Chromecast's default receiver plays MP4/H.264+AAC and WebM; it does NOT
// play Matroska, and only newer hardware handles HEVC/H.265 -- both very
// common in this library. Used purely to turn an opaque receiver failure
// into an explanation worth acting on, never to block the attempt (some
// Google TV devices do play these).
function likelyUnsupportedOnChromecast(name) {
  const text = String(name || "").toLowerCase();
  if (text.endsWith(".mkv")) return "MKV isn't supported by Chromecast's built-in player";
  if (/(^|[^a-z])(x265|h265|hevc)([^a-z]|$)/.test(text)) return "H.265/HEVC only plays on newer Chromecast hardware";
  return null;
}
function reportCastPlaybackFailure(detail, castInfo = null) {
  // The compatibility HLS path has already normalized the container/codecs,
  // so only attach a format hint when the original file is being sent direct.
  const hint = castInfo?.delivery === "hls" ? null : likelyUnsupportedOnChromecast(currentPlayerName);
  const because = hint ? ` -- ${hint}.` : ".";
  showToast(
    `The TV couldn't play this movie${because}${detail ? ` (${escapeHtml(detail)})` : ""} Try AirPlay, or Download instead.`,
    "danger",
    12000,
  );
}
// How long to let the receiver sit in BUFFERING before calling it stalled.
// Generous: a large file over a slow LAN legitimately takes a while to
// start, and a false "it failed" on a cast that was about to play would be
// worse than waiting.
const CAST_PLAYBACK_START_TIMEOUT_MS = 25000;
// loadMedia() resolving only means the receiver ACCEPTED the request -- it
// can still fail afterwards, in two different ways that need catching
// separately:
//   * it reports an error (IDLE + IdleReason.ERROR), or
//   * it never reports anything at all and just buffers forever.
// The second is what an unsupported container actually does in practice
// (confirmed on a real device with an MKV: cast connects, TV shows a
// permanent loading spinner, no error event is ever emitted) -- so an
// error-only listener would leave the UI claiming a cheerful "Casting
// started" indefinitely. Hence the timeout as well as the error hook.
function watchCastSessionForPlaybackFailure(session, castInfo) {
  const media = session.getMediaSession();
  if (!media || typeof media.addUpdateListener !== "function") return;
  let settled = false;
  let listener = null;
  let stallTimer = null;
  const settle = (report) => {
    if (settled) return;
    settled = true;
    if (stallTimer) clearTimeout(stallTimer);
    try {
      if (listener) media.removeUpdateListener(listener);
    } catch (_) {
      // Listener already detached by the SDK -- nothing to undo.
    }
    if (report) report();
  };
  listener = (isAlive) => {
    if (media.playerState === chrome.cast.media.PlayerState.PLAYING) {
      settle(null); // actually playing on the TV -- stop watching
      return;
    }
    if (media.playerState === chrome.cast.media.PlayerState.IDLE && media.idleReason === chrome.cast.media.IdleReason.ERROR) {
      settle(() => reportCastPlaybackFailure("receiver reported a playback error", castInfo));
      return;
    }
    if (!isAlive) settle(null);
  };
  stallTimer = setTimeout(
    () => settle(() => reportCastPlaybackFailure("the TV never started playing", castInfo)),
    CAST_PLAYBACK_START_TIMEOUT_MS,
  );
  media.addUpdateListener(listener);
}
async function loadMovieOntoCastSession(entryKey, selectedSession = null) {
  const session = selectedSession || cast.framework.CastContext.getInstance().getCurrentSession();
  if (!session) return;
  const castInfo = await mintMovieCastToken(entryKey);
  if (!castInfo) return;
  const mediaInfo = new chrome.cast.media.MediaInfo(castInfo.cast_url, castInfo.content_type || "video/mp4");
  mediaInfo.streamType = chrome.cast.media.StreamType.BUFFERED;
  if (castInfo.delivery === "hls") {
    mediaInfo.hlsSegmentFormat = chrome.cast.media.HlsSegmentFormat.TS;
    mediaInfo.hlsVideoSegmentFormat = chrome.cast.media.HlsVideoSegmentFormat.MPEG2_TS;
  }
  mediaInfo.metadata = new chrome.cast.media.MovieMediaMetadata();
  mediaInfo.metadata.title = currentPlayerName || "Movie";
  const request = new chrome.cast.media.LoadRequest(mediaInfo);
  try {
    await session.loadMedia(request);
    // The receiver has it now -- stop decoding/playing locally too, or the
    // phone keeps burning battery (and, on some devices, audio) on a movie
    // that's supposed to have handed off to the TV.
    const video = document.getElementById("moviePlayerVideo");
    if (video) video.pause();
    watchCastSessionForPlaybackFailure(session, castInfo);
    // Say upfront when the format is one Chromecast's built-in player is
    // known not to handle, rather than letting the user watch a spinner for
    // 25s first -- the attempt still goes ahead (newer Google TV hardware
    // does play some of these), this just sets the expectation honestly.
    const hint = castInfo.delivery === "hls" ? null : likelyUnsupportedOnChromecast(currentPlayerName);
    if (castInfo.delivery === "hls") {
      showToast(
        castInfo.transcoded
          ? "Casting started with compatibility streaming."
          : "Casting started with a cast-compatible stream.",
        "success",
      );
    } else if (hint) {
      showToast(`Casting started, but ${hint} -- if the TV just shows a spinner, use AirPlay or Download instead.`, "warning", 12000);
    } else {
      showToast("Casting started.", "success");
    }
  } catch (err) {
    // Rejected outright -- most often the receiver refusing the container/
    // codec before it even fetches (see likelyUnsupportedOnChromecast).
    reportCastPlaybackFailure(String((err && (err.description || err.code)) || err || ""), castInfo);
  }
}
function openMoviePlayerModal(entryKey, movieName) {
  currentPlayerEntryKey = entryKey;
  currentPlayerName = movieName || "";
  loadCastSenderSdk();
  const modalId = "moviePlayerModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  const airplaySupported = typeof HTMLVideoElement !== "undefined" && !!HTMLVideoElement.prototype.webkitShowPlaybackTargetPicker;
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-lg">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0 movie-player-modal-title" title="${escapeHtml(movieName || "Movie")}"><i class="bi bi-film me-2"></i><span class="movie-player-modal-title-text">${escapeHtml(movieName || "Movie")}</span></h5>
          <button type="button" class="btn-close btn-close-white flex-shrink-0" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <video id="moviePlayerVideo" class="w-100" style="max-height: 70vh; background: #000;" controls autoplay ${airplaySupported ? 'x-webkit-airplay="allow"' : "disableRemotePlayback"} src="${movieStreamUrl(entryKey)}">
            Your browser can't play this video format. <a href="${movieDownloadUrl(entryKey)}">Download it</a> instead.
          </video>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-primary" id="movieCastButton" onclick="castMovieChromecast(${jsAttr(entryKey)})" title="Choose a Chromecast or Google TV"><span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Google Cast</button>
          ${
            airplaySupported
              ? `<button type="button" class="btn btn-outline-primary" id="movieAirPlayButton" title="Prepare AirPlay" onclick="castMovieAirPlay(${jsAttr(entryKey)})"><i class="bi bi-airplay me-1"></i>AirPlay</button>`
              : ""
          }
          <a class="btn btn-outline-primary" href="${movieDownloadUrl(entryKey)}"><i class="bi bi-download me-1"></i>Download</a>
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
        </div>
      </div>
    </div>`;
  // Stop playback (and the in-flight stream) once the modal closes -- otherwise
  // a movie keeps decoding/downloading in the background after it's dismissed.
  // Deliberately does NOT touch an active cast session -- casting is meant to
  // keep playing on the receiver after you close this page, same as any other
  // Chromecast/AirPlay app.
  modal.addEventListener("hidden.bs.modal", () => {
    const video = document.getElementById("moviePlayerVideo");
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    currentPlayerEntryKey = null;
    currentPlayerName = "";
  }, { once: true });
  updateMovieCastButton();
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}
async function renderMovieDetailsPage(entryKey) {
  currentSystemContext = null;
  clearSystemTheme();
  setLoading(true, "Loading movie...");
  try {
    const movie = await api(`/movies/${encodeURIComponent(entryKey)}`);
    content.innerHTML = renderMovieDetailShell(movie);
    await renderMovieScraperCard(entryKey, movie);
  } catch (err) {
    content.innerHTML = `
      <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#movies')"><i class="bi bi-arrow-left me-1"></i>Back to Movies</button>
      <div class="alert alert-danger">Failed to load movie: ${escapeHtml(err.message || "unknown error")}</div>
    `;
  } finally {
    setLoading(false);
  }
}
function renderMovieDetailShell(movie) {
  const meta = movie.metadata || null;
  const rawName = movie.movie_name || movie.name || "";
  const title = movie.display_title || rawName;
  const entryKey = movie.entry_key;
  const posterUrl = meta && meta.poster_relative_path ? movieArtworkUrl(entryKey, "poster") : null;
  const backdropUrl = meta && meta.backdrop_relative_path ? movieArtworkUrl(entryKey, "backdrop") : null;
  const year = meta && meta.release_date ? String(meta.release_date).slice(0, 4) : "";
  const metaBits = [
    year,
    meta && meta.runtime_minutes ? `${meta.runtime_minutes} min` : "",
    meta && meta.rating != null ? `<i class="bi bi-star-fill text-warning"></i> ${Number(meta.rating).toFixed(1)}` : "",
  ].filter(Boolean);
  const genres = (meta && meta.genres) || [];
  const cast = (meta && meta.cast) || [];
  const isTvEpisode = !!(meta && meta.media_type === "tv_episode");
  const episodeBadge = isTvEpisode
    ? `<span class="badge text-bg-info me-2">TV &middot; S${String(meta.season_number).padStart(2, "0")}E${String(meta.episode_number).padStart(2, "0")}</span>`
    : "";
  return `
    <button class="btn btn-outline-secondary btn-sm mb-3" type="button" onclick="setHash('#movies')"><i class="bi bi-arrow-left me-1"></i>Back to Movies</button>
    <div class="movie-detail-hero" ${backdropUrl ? `style="background-image:linear-gradient(180deg, rgba(11,16,32,0.55) 0%, rgba(11,16,32,0.96) 100%), url('${escapeHtml(backdropUrl)}')"` : ""}>
      <div class="movie-detail-hero-body">
        ${
          posterUrl
            ? `<img class="movie-detail-poster" src="${escapeHtml(posterUrl)}" alt="">`
            : `<div class="movie-detail-poster movie-detail-poster-placeholder"><i class="bi bi-film"></i></div>`
        }
        <div class="movie-detail-info min-width-0">
          ${
            isTvEpisode
              ? `<div class="small text-muted mb-1">${episodeBadge}${escapeHtml(meta.show_title || "")}</div>`
              : ""
          }
          <h2 class="movie-detail-title" title="${escapeHtml(title)}">${escapeHtml(title)}</h2>
          ${meta && meta.tagline ? `<div class="movie-detail-tagline fst-italic text-muted mb-2">${escapeHtml(meta.tagline)}</div>` : ""}
          ${metaBits.length ? `<div class="text-muted small mb-2">${metaBits.join(" &middot; ")}</div>` : ""}
          ${genres.length ? `<div class="mb-3">${genres.map((g) => `<span class="badge movie-genre-badge">${escapeHtml(g)}</span>`).join(" ")}</div>` : ""}
          <div class="d-flex flex-wrap gap-2 mb-2">
            <button class="btn btn-primary" type="button" onclick="openMoviePlayerModal(${jsAttr(entryKey)}, ${jsAttr(title)})"><i class="bi bi-play-circle me-1"></i>Watch</button>
            ${
              movie.is_downloadable === false
                ? `<button class="btn btn-outline-secondary" type="button" disabled><i class="bi bi-slash-circle me-1"></i>Downloads disabled</button>`
                : `<a class="btn btn-outline-primary" href="${movieDownloadUrl(entryKey)}"><i class="bi bi-download me-1"></i>Download</a>`
            }
            ${
              adminEnabled
                ? `<button class="btn btn-outline-danger" type="button" onclick="deleteMovieFromDetailPage(${jsAttr(entryKey)}, ${jsAttr(title)})"><i class="bi bi-trash me-1"></i>Delete</button>`
                : ""
            }
          </div>
        </div>
      </div>
    </div>
    <div class="movie-detail-body">
      ${
        meta && meta.overview
          ? `<h6>Overview</h6><p>${escapeHtml(meta.overview)}</p>`
          : `<p class="text-muted small">No description yet -- scrape this movie below to fetch one from TMDb.</p>`
      }
      ${
        meta && meta.youtube_trailer_key
          ? `<h6>Trailer</h6><div class="ratio ratio-16x9 movie-detail-trailer mb-3"><iframe src="https://www.youtube.com/embed/${escapeHtml(meta.youtube_trailer_key)}" title="Trailer" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen loading="lazy" referrerpolicy="strict-origin-when-cross-origin"></iframe></div>`
          : ""
      }
      ${
        cast.length
          ? `<h6>Cast</h6><div class="mb-3">${cast.map((c) => `<span class="movie-cast-chip">${escapeHtml(c.name || "")}${c.character ? `<span class="text-muted"> as ${escapeHtml(c.character)}</span>` : ""}</span>`).join("")}</div>`
          : ""
      }
      <div class="text-muted small">${escapeHtml(movie.file_path || rawName)} &middot; ${escapeHtml(formatBytes(movie.byte_count ?? movie.file_size))}</div>
    </div>
    <div id="movie-scraper-card" class="mt-4"></div>
  `;
}
// Movie/episode files are gone from disk after any of these -- the whole
// client-side movies cache is invalidated (not just the deleted row) since
// every movies view (tree, explorer, show pages) reads from the same
// moviesAllRows snapshot with no per-row invalidation of its own; the next
// view that needs it refetches via its existing "if (!moviesAllRows.length)"
// lazy-load guard.
async function deleteMoviesBatch(entryKeys) {
  const result = await apiPost("/admin/movies/delete", { entry_keys: entryKeys });
  moviesAllRows = [];
  return result;
}
function deleteMovieFromDetailPage(entryKey, title) {
  openConfirmDeleteModal({
    title: "Delete movie?",
    body: `<strong>${escapeHtml(title)}</strong> will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      setLoading(true, "Deleting...");
      try {
        await deleteMoviesBatch([entryKey]);
        showToast("Movie deleted.", "success");
        setHash("#movies");
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
}
async function renderMovieScraperCard(entryKey, movie) {
  const container = document.getElementById("movie-scraper-card");
  if (!container) return;
  if (!adminEnabled) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = `<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Loading scraper status...</div>`;
  try {
    const settings = await api("/admin/movies/scraper-settings");
    container.innerHTML = settings.has_api_key
      ? renderMovieScraperSearchUi(entryKey, movie)
      : renderMovieScraperApiKeyForm(entryKey);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-warning small mb-0">Scraper unavailable: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}
function renderMovieScraperApiKeyForm(entryKey) {
  return `
    <div class="card">
      <div class="card-header"><i class="bi bi-cloud-download me-1"></i>Artwork &amp; Metadata (TMDb)</div>
      <div class="card-body">
        <p class="text-muted small">Set a TMDb API key (v3 auth) to search for this movie and download its poster, backdrop, and details.</p>
        <div class="input-group">
          <input id="movieScraperApiKeyInput" type="password" class="form-control" placeholder="TMDb API key">
          <button class="btn btn-primary" type="button" onclick="saveMovieScraperApiKey(${jsAttr(entryKey)})"><i class="bi bi-check-lg me-1"></i>Save</button>
        </div>
      </div>
    </div>
  `;
}
async function saveMovieScraperApiKey(entryKey) {
  const input = document.getElementById("movieScraperApiKeyInput");
  const apiKey = (input && input.value || "").trim();
  if (!apiKey) {
    showToast("Enter a TMDb API key first.", "warning");
    return;
  }
  setLoading(true, "Saving TMDb API key...");
  try {
    await apiPost("/admin/movies/scraper-settings", { api_key: apiKey });
    showToast("TMDb API key saved.", "success");
    await renderMovieScraperCard(entryKey, null);
    await searchMovieScraper(entryKey);
  } catch (err) {
    showToast(`Failed to save TMDb API key: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
function renderMovieScraperSearchUi(entryKey, movie) {
  // Deliberately blank, not the raw filename: an empty query tells the
  // backend to search using its own cleaned-up-title candidate ladder
  // (same one the bulk scraper uses) rather than the messy filename
  // verbatim -- see searchMovieScraper, which fills this box in with
  // whichever title that ladder actually searched once results come back.
  const hasMetadata = !!(movie && movie.metadata);
  return `
    <div class="card">
      <div class="card-header d-flex align-items-center justify-content-between gap-2">
        <span><i class="bi bi-cloud-download me-1"></i>Artwork &amp; Metadata (TMDb)</span>
        ${
          hasMetadata
            ? `<button class="btn btn-outline-danger btn-sm" type="button" onclick="deleteMovieScraperMetadata(${jsAttr(entryKey)})"><i class="bi bi-trash me-1"></i>Remove scraped data</button>`
            : ""
        }
      </div>
      <div class="card-body">
        <div class="input-group mb-3">
          <input id="movieScraperQuery" type="text" class="form-control" value="" placeholder="Leave blank to search using a cleaned-up title">
          <button class="btn btn-primary" type="button" onclick="searchMovieScraper(${jsAttr(entryKey)})"><i class="bi bi-search me-1"></i>Search</button>
        </div>
        <div id="movie-scraper-results" class="mb-3"></div>
        <div class="small text-muted mb-1">Can't find it? Paste a TMDb page link or ID instead (e.g. a movie that only matches under an alternate title):</div>
        <div class="input-group">
          <input id="movieScraperUrlInput" type="text" class="form-control" placeholder="https://www.themoviedb.org/movie/21380-virus or 21380">
          <button class="btn btn-outline-primary" type="button" onclick="applyMovieScraperUrl(${jsAttr(entryKey)})"><i class="bi bi-link-45deg me-1"></i>Apply</button>
        </div>
      </div>
    </div>
  `;
}
async function deleteMovieScraperMetadata(entryKey) {
  if (!window.confirm("Remove the scraped TMDb metadata and artwork for this entry? This cannot be undone, but you can re-scrape it afterward.")) return;
  setLoading(true, "Removing scraped metadata...");
  try {
    await apiPost(`/admin/movies/${encodeURIComponent(entryKey)}/scrape/delete`, {});
    showToast("Scraped metadata removed.", "success");
    await renderMovieDetailsPage(entryKey);
  } catch (err) {
    showToast(`Failed to remove scraped metadata: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function applyMovieScraperUrl(entryKey) {
  const input = document.getElementById("movieScraperUrlInput");
  const reference = (input && input.value || "").trim();
  if (!reference) {
    showToast("Paste a TMDb link or ID first.", "warning");
    return;
  }
  setLoading(true, "Downloading artwork and metadata from TMDb...");
  try {
    await apiPost(`/admin/movies/${encodeURIComponent(entryKey)}/scrape/apply`, { tmdb_url: reference });
    showToast("Movie artwork and metadata updated.", "success");
    await renderMovieDetailsPage(entryKey);
  } catch (err) {
    showToast(`Failed to apply that TMDb link: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function searchMovieScraper(entryKey) {
  const resultsEl = document.getElementById("movie-scraper-results");
  if (!resultsEl) return;
  const queryInput = document.getElementById("movieScraperQuery");
  const query = queryInput ? queryInput.value.trim() : "";
  resultsEl.innerHTML = `<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Searching TMDb...</div>`;
  try {
    const data = await api(`/admin/movies/${encodeURIComponent(entryKey)}/scrape/search?q=${encodeURIComponent(query)}`);
    // The box started blank (or the user left it blank): show what the
    // backend's candidate ladder actually searched, so it's both visible
    // and ready to hand-edit for a follow-up retry.
    if (queryInput && !query && data.query) queryInput.value = data.query;
    const results = data.results || [];
    resultsEl.innerHTML = results.length
      ? `<div class="list-group">${results.map((r) => renderMovieScraperResult(entryKey, r)).join("")}</div>`
      : `<div class="text-muted small">No TMDb matches found${data.query ? ` for "${escapeHtml(data.query)}"` : ""}.</div>`;
  } catch (err) {
    resultsEl.innerHTML = `<div class="alert alert-warning small mb-0">Search failed: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}
function renderMovieScraperResult(entryKey, result) {
  const year = result.release_date ? String(result.release_date).slice(0, 4) : "";
  return `
    <button type="button" class="list-group-item list-group-item-action" onclick="applyMovieScraperResult(${jsAttr(entryKey)}, ${Number(result.tmdb_id)}, this)">
      <div class="d-flex gap-3 align-items-center">
        ${result.thumbnail_url ? `<img class="match-thumb" src="${escapeHtml(result.thumbnail_url)}" alt="">` : `<div class="match-thumb-placeholder"></div>`}
        <div class="min-width-0">
          <div class="fw-semibold">${escapeHtml(result.title || "")}${year ? ` <span class="text-muted">(${escapeHtml(year)})</span>` : ""}</div>
          ${result.overview ? `<div class="text-muted small text-truncate-2">${escapeHtml(result.overview)}</div>` : ""}
        </div>
      </div>
    </button>
  `;
}
async function applyMovieScraperResult(entryKey, tmdbId, button) {
  const originalHtml = button ? button.innerHTML : "";
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spinner-border spinner-border-sm"></span>`;
  }
  setLoading(true, "Downloading artwork and metadata from TMDb...");
  try {
    await apiPost(`/admin/movies/${encodeURIComponent(entryKey)}/scrape/apply`, { tmdb_id: tmdbId });
    showToast("Movie artwork and metadata updated.", "success");
    await renderMovieDetailsPage(entryKey);
  } catch (err) {
    showToast(`Failed to apply TMDb match: ${escapeHtml(err.message || "unknown error")}`, "danger");
    if (button) {
      button.disabled = false;
      button.innerHTML = originalHtml;
    }
  } finally {
    setLoading(false);
  }
}

// ---------------------------------------------------- Admin Movies (bulk scrape)

function stopMovieBulkScrapeAutoRefresh() {
  if (movieBulkScrapeTimer) {
    clearInterval(movieBulkScrapeTimer);
    movieBulkScrapeTimer = null;
  }
  movieBulkScrapeInFlight = false;
}
// Only polls while a job is "running" -- a rare, one-off admin action (not a
// persistent queue like Torrents), same reasoning as Config Backups' own
// auto-refresh: starts on demand, stops itself once nothing is left running.
function startMovieBulkScrapeAutoRefreshIfNeeded(job) {
  if (!job || job.status !== "running") {
    stopMovieBulkScrapeAutoRefresh();
    return;
  }
  if (movieBulkScrapeTimer) return;
  movieBulkScrapeTimer = setInterval(async () => {
    if (document.hidden || movieBulkScrapeInFlight) return;
    if (window.location.hash !== "#admin/movies") return;
    const statusEl = document.getElementById("movieBulkScrapeStatus");
    if (!statusEl) return;
    movieBulkScrapeInFlight = true;
    try {
      const payload = await api("/admin/movies/scrape/bulk");
      if (window.location.hash === "#admin/movies" && statusEl.isConnected) {
        patchMovieBulkScrapeLive(payload.job || null);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      movieBulkScrapeInFlight = false;
    }
  }, 2000);
}
function movieBulkScrapeStatusBadge(job) {
  const status = String(job.status || "running");
  const cls = status === "error" ? "danger" : status === "complete" ? "success" : status === "stopped" ? "secondary" : "info";
  const title = status === "error" ? escapeHtml(job.error_message || "") : "";
  return `<span class="badge text-bg-${cls}" title="${title}">${escapeHtml(status)}</span>`;
}
function renderMovieBulkScrapeStatus(job) {
  if (!job) {
    return `<div class="text-muted small">No scrape has been run yet.</div>`;
  }
  const total = Number(job.total || 0);
  const processed = Number(job.processed || 0);
  const pct = total > 0 ? Math.round((processed / total) * 100) : 100;
  const running = job.status === "running";
  return `
    <div class="d-flex align-items-center justify-content-between gap-2 mb-1">
      <div>${movieBulkScrapeStatusBadge(job)} <span class="small text-muted">${job.rescan_all ? "Rescanning all movies" : "Scraping movies missing artwork"}</span></div>
      <div class="small text-muted">${processed.toLocaleString()} / ${total.toLocaleString()}</div>
    </div>
    ${total > 0 ? `<div class="progress mb-2" style="height:0.5rem;"><div class="progress-bar${running ? " progress-bar-striped progress-bar-animated" : ""} bg-${job.status === "error" ? "danger" : job.status === "stopped" ? "secondary" : "primary"}" style="width:${pct}%"></div></div>` : ""}
    ${running && job.current_movie ? `<div class="small text-muted mb-2"><span class="spinner-border spinner-border-sm me-1"></span>Scraping: ${escapeHtml(job.current_movie)}</div>` : ""}
    <div class="small text-muted">
      ${movieBulkScrapeCountLink("matched", job.matched_count)}
      &middot; ${movieBulkScrapeCountLink("skipped", job.skipped_count)}
      &middot; ${movieBulkScrapeCountLink("failed", job.failed_count)}
    </div>
    ${job.status === "error" && job.error_message ? `<div class="alert alert-warning small mt-2 mb-0">${escapeHtml(job.error_message)}</div>` : ""}
  `;
}
function movieBulkScrapeCountLink(status, count) {
  const n = Number(count || 0);
  if (!n) return `${n.toLocaleString()} ${escapeHtml(status)}`;
  return `<button type="button" class="btn btn-link btn-sm p-0 align-baseline" onclick="toggleMovieBulkScrapeBreakdown(${jsAttr(status)})">${n.toLocaleString()} ${escapeHtml(status)}</button>`;
}
function patchMovieBulkScrapeLive(job) {
  const statusEl = document.getElementById("movieBulkScrapeStatus");
  if (statusEl) statusEl.innerHTML = renderMovieBulkScrapeStatus(job);
  const running = job && job.status === "running";
  const startBtn = document.getElementById("movieBulkScrapeStartBtn");
  if (startBtn) {
    startBtn.disabled = running;
    startBtn.innerHTML = running
      ? `<span class="spinner-border spinner-border-sm me-1"></span>Scraping...`
      : `<i class="bi bi-play-fill me-1"></i>Start Scraping`;
  }
  const stopBtn = document.getElementById("movieBulkScrapeStopBtn");
  if (stopBtn) {
    stopBtn.classList.toggle("d-none", !running);
    if (!running) movieBulkScrapeStopRequested = false;
    if (!movieBulkScrapeStopRequested) {
      stopBtn.disabled = false;
      stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
    }
  }
  const checkbox = document.getElementById("movieBulkScrapeRescanAll");
  if (checkbox) checkbox.disabled = running;
  // A job that just finished (running -> anything else) can have changed
  // every bucket's contents -- refresh whichever breakdown panel is open
  // (e.g. after "Retry failed", the failed list should reflect what's
  // *still* failing, not what was failing before the retry ran).
  if (movieBulkScrapeWasRunning && !running && movieBulkScrapeBreakdownStatus) {
    loadMovieBulkScrapeBreakdown(movieBulkScrapeBreakdownStatus, 0);
  }
  movieBulkScrapeWasRunning = running;
  startMovieBulkScrapeAutoRefreshIfNeeded(job);
}
function renderMovieAdminApiKeyForm() {
  return `
    <div class="card">
      <div class="card-header"><i class="bi bi-cloud-download me-1"></i>Artwork &amp; Metadata (TMDb)</div>
      <div class="card-body">
        <p class="text-muted small">Set a TMDb API key (v3 auth) to enable scraping movie posters, backdrops, and metadata. This can also be set from any movie's own details page.</p>
        <div class="input-group">
          <input id="movieAdminApiKeyInput" type="password" class="form-control" placeholder="TMDb API key">
          <button class="btn btn-primary" type="button" onclick="saveMovieAdminApiKey()"><i class="bi bi-check-lg me-1"></i>Save</button>
        </div>
      </div>
    </div>
  `;
}
async function saveMovieAdminApiKey() {
  const input = document.getElementById("movieAdminApiKeyInput");
  const apiKey = (input && input.value || "").trim();
  if (!apiKey) {
    showToast("Enter a TMDb API key first.", "warning");
    return;
  }
  setLoading(true, "Saving TMDb API key...");
  try {
    await apiPost("/admin/movies/scraper-settings", { api_key: apiKey });
    showToast("TMDb API key saved.", "success");
    await renderAdminMoviesArtworkPage();
  } catch (err) {
    showToast(`Failed to save TMDb API key: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
function renderMovieAdminBulkScrapeCard(job) {
  const running = job && job.status === "running";
  return `
    <div class="card">
      <div class="card-header"><i class="bi bi-collection-play me-1"></i>Bulk Scrape</div>
      <div class="card-body">
        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" id="movieBulkScrapeRescanAll" ${running ? "disabled" : ""}>
          <label class="form-check-label" for="movieBulkScrapeRescanAll">Rescan all movies (unchecked: only scrape movies missing artwork)</label>
        </div>
        <button id="movieBulkScrapeStartBtn" class="btn btn-primary" type="button" onclick="startMovieBulkScrape()" ${running ? "disabled" : ""}>
          ${running ? `<span class="spinner-border spinner-border-sm me-1"></span>Scraping...` : `<i class="bi bi-play-fill me-1"></i>Start Scraping`}
        </button>
        <button id="movieBulkScrapeStopBtn" class="btn btn-outline-danger ${running ? "" : "d-none"}" type="button" onclick="cancelMovieBulkScrape()">
          <i class="bi bi-stop-fill me-1"></i>Stop
        </button>
        <div id="movieBulkScrapeStatus" class="mt-3">${renderMovieBulkScrapeStatus(job)}</div>
        <div id="movieBulkScrapeBreakdown" class="mt-3"></div>
      </div>
    </div>
  `;
}
async function startMovieBulkScrape() {
  const checkbox = document.getElementById("movieBulkScrapeRescanAll");
  const rescanAll = checkbox ? checkbox.checked : false;
  setLoading(true, "Starting bulk scrape...");
  try {
    const result = await apiPost("/admin/movies/scrape/bulk", { rescan_all: rescanAll });
    if (result.status === "already_running") {
      showToast("A bulk scrape is already running.", "warning");
    } else if (result.status === "error") {
      showToast(`Could not start scraping: ${escapeHtml(result.error || "unknown error")}`, "danger");
    } else {
      showToast("Bulk scrape started.", "success");
    }
  } catch (err) {
    showToast(`Could not start scraping: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
  if (window.location.hash === "#admin/movies") {
    await renderAdminMoviesArtworkPage();
  }
}
// Not a full re-render (unlike startMovieBulkScrape) -- the running job's
// own poll loop (startMovieBulkScrapeAutoRefreshIfNeeded) is already
// patching the status card live every 2s, so this just needs to flip the
// button into a "Stopping..." state and let the next poll tick reflect the
// real outcome once the job actually reaches its next stop-check.
async function cancelMovieBulkScrape() {
  movieBulkScrapeStopRequested = true;
  const stopBtn = document.getElementById("movieBulkScrapeStopBtn");
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Stopping...`;
  }
  try {
    const result = await apiPost("/admin/movies/scrape/bulk/stop", {});
    if (result.status === "not_running") {
      showToast("No bulk scrape is currently running.", "warning");
      movieBulkScrapeStopRequested = false;
      if (stopBtn) {
        stopBtn.disabled = false;
        stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
      }
    } else {
      showToast("Stopping bulk scrape...", "success");
    }
  } catch (err) {
    showToast(`Could not stop scraping: ${escapeHtml(err.message || "unknown error")}`, "danger");
    movieBulkScrapeStopRequested = false;
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
    }
  }
}
async function toggleMovieBulkScrapeBreakdown(status) {
  const container = document.getElementById("movieBulkScrapeBreakdown");
  if (movieBulkScrapeBreakdownStatus === status) {
    movieBulkScrapeBreakdownStatus = null;
    if (container) container.innerHTML = "";
    return;
  }
  await loadMovieBulkScrapeBreakdown(status, 0);
}
async function loadMovieBulkScrapeBreakdown(status, offset) {
  const container = document.getElementById("movieBulkScrapeBreakdown");
  if (!container) return;
  movieBulkScrapeBreakdownStatus = status;
  movieBulkScrapeBreakdownOffset = Math.max(0, offset || 0);
  container.innerHTML = `<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Loading ${escapeHtml(status)} movies...</div>`;
  try {
    const page = await api(`/admin/movies/scrape/bulk/items/${encodeURIComponent(status)}?limit=${MOVIE_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE}&offset=${movieBulkScrapeBreakdownOffset}`);
    if (movieBulkScrapeBreakdownStatus === status) container.innerHTML = renderMovieBulkScrapeBreakdownPanel(status, page);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-warning small mb-0">Could not load ${escapeHtml(status)} list: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}
function renderMovieBulkScrapeBreakdownPanel(status, page) {
  const items = page.items || [];
  const total = Number(page.total || 0);
  const offset = Number(page.offset || 0);
  const limit = Number(page.limit || MOVIE_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE);
  const showing = items.length ? `${(offset + 1).toLocaleString()}-${(offset + items.length).toLocaleString()} of ${total.toLocaleString()}` : "0 of 0";
  const retryAllBtn = status === "failed" && total
    ? `<button class="btn btn-outline-primary btn-sm text-nowrap" type="button" onclick="retryAllMovieBulkScrapeFailed()"><i class="bi bi-arrow-repeat me-1"></i>Retry all ${total.toLocaleString()}</button>`
    : "";
  const rows = items.length
    ? items.map((item) => `
      <div class="d-flex align-items-start justify-content-between gap-2 py-2 border-bottom movie-bulk-scrape-item-row">
        <div class="min-width-0">
          <button type="button" class="btn btn-link btn-sm p-0 text-start text-truncate d-block" style="max-width: 100%;" title="${escapeHtml(item.file_path || item.movie_name || "")}" onclick="setHash(movieDetailHash(${jsAttr(item.entry_key)}))">${escapeHtml(item.movie_name || item.file_path || "")}</button>
          ${item.reason ? `<div class="text-muted small">${escapeHtml(item.reason)}</div>` : ""}
        </div>
        ${
          status === "failed"
            ? `<button class="btn btn-outline-secondary btn-sm text-nowrap" type="button" onclick="retryMovieBulkScrapeItem(${jsAttr(item.entry_key)})"><i class="bi bi-arrow-repeat me-1"></i>Retry</button>`
            : ""
        }
      </div>
    `).join("")
    : `<div class="text-muted small py-2">Nothing here.</div>`;
  return `
    <div class="card log-card">
      <div class="card-header d-flex flex-wrap align-items-center justify-content-between gap-2">
        <span class="text-capitalize">${escapeHtml(status)} movies</span>
        <div class="d-flex align-items-center gap-2">
          ${retryAllBtn}
          <button class="btn btn-outline-secondary btn-sm" type="button" title="Close" onclick="toggleMovieBulkScrapeBreakdown(${jsAttr(status)})"><i class="bi bi-x-lg"></i></button>
        </div>
      </div>
      <div class="card-body p-0" style="max-height: 360px; overflow-y: auto;">
        <div class="px-3">${rows}</div>
      </div>
      <div class="card-footer d-flex align-items-center justify-content-between gap-2">
        <span class="small text-muted">${showing}</span>
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-outline-primary" type="button" ${offset <= 0 ? "disabled" : ""} onclick="loadMovieBulkScrapeBreakdown(${jsAttr(status)}, ${Math.max(0, offset - limit)})">Previous</button>
          <button class="btn btn-sm btn-outline-primary" type="button" ${offset + items.length >= total ? "disabled" : ""} onclick="loadMovieBulkScrapeBreakdown(${jsAttr(status)}, ${offset + limit})">Next</button>
        </div>
      </div>
    </div>
  `;
}
async function startMovieBulkScrapeRetry(body, loadingText) {
  setLoading(true, loadingText);
  try {
    const result = await apiPost("/admin/movies/scrape/bulk/retry", body);
    if (result.status === "already_running") {
      showToast("A scrape is already running -- try again once it finishes.", "warning");
    } else if (result.status === "error") {
      showToast(`Could not start retry: ${escapeHtml(result.error || "unknown error")}`, "danger");
    } else {
      showToast("Retry started.", "success");
      const statusPayload = await api("/admin/movies/scrape/bulk");
      patchMovieBulkScrapeLive(statusPayload.job || null);
    }
  } catch (err) {
    showToast(`Could not start retry: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function retryMovieBulkScrapeItem(entryKey) {
  await startMovieBulkScrapeRetry({ entry_keys: [entryKey] }, "Retrying...");
}
async function retryAllMovieBulkScrapeFailed() {
  await startMovieBulkScrapeRetry({ status: "failed" }, "Retrying all failed movies...");
}
async function renderAdminMoviesArtworkPage() {
  currentSystemContext = null;
  clearSystemTheme();
  // A fresh visit to this page always starts with the breakdown panel
  // closed (its container is rebuilt below) -- reset the tracked state to
  // match, so a stale "failed" from a previous visit can't cause
  // patchMovieBulkScrapeLive to silently reopen it on the next poll tick.
  movieBulkScrapeBreakdownStatus = null;
  setLoading(true, "Loading movie scraper settings...");
  try {
    const [settingsPayload, statusPayload] = await Promise.all([
      api("/admin/movies/scraper-settings"),
      api("/admin/movies/scrape/bulk"),
    ]);
    content.innerHTML = `
      ${renderArtworkTabBar("movies")}
      <div class="text-muted small mb-3">Scrape TMDb for movie poster/backdrop art and metadata (overview, cast, genres) -- the same scraper available on each movie's own details page, run here in bulk across your whole library.</div>
      <div id="movieAdminScraperCard">${settingsPayload.has_api_key ? renderMovieAdminBulkScrapeCard(statusPayload.job) : renderMovieAdminApiKeyForm()}</div>
    `;
    startMovieBulkScrapeAutoRefreshIfNeeded(statusPayload.job);
  } catch (err) {
    content.innerHTML = `${renderArtworkTabBar("movies")}<div class="alert alert-danger">Failed to load movie scraper settings: ${escapeHtml(err.message || "unknown error")}</div>`;
  } finally {
    setLoading(false);
  }
}

// ---------------------------------------------------- Admin Music (bulk scrape)
//
// Mirrors the Admin Movies bulk-scrape block above closely -- the one
// structural difference is there is no API-key gate at all (MusicBrainz +
// Cover Art Archive are both keyless), so renderAdminMusicArtworkPage goes
// straight to the bulk-scrape card with no has_api_key branch, and there is
// no equivalent of renderMovieAdminApiKeyForm/saveMovieAdminApiKey here.

function stopMusicBulkScrapeAutoRefresh() {
  if (musicBulkScrapeTimer) {
    clearInterval(musicBulkScrapeTimer);
    musicBulkScrapeTimer = null;
  }
  musicBulkScrapeInFlight = false;
}
function startMusicBulkScrapeAutoRefreshIfNeeded(job) {
  if (!job || job.status !== "running") {
    stopMusicBulkScrapeAutoRefresh();
    return;
  }
  if (musicBulkScrapeTimer) return;
  musicBulkScrapeTimer = setInterval(async () => {
    if (document.hidden || musicBulkScrapeInFlight) return;
    if (window.location.hash !== "#admin/music") return;
    const statusEl = document.getElementById("musicBulkScrapeStatus");
    if (!statusEl) return;
    musicBulkScrapeInFlight = true;
    try {
      const payload = await api("/admin/music/scrape/bulk");
      if (window.location.hash === "#admin/music" && statusEl.isConnected) {
        patchMusicBulkScrapeLive(payload.job || null);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      musicBulkScrapeInFlight = false;
    }
  }, 2000);
}
function musicBulkScrapeStatusBadge(job) {
  const status = String(job.status || "running");
  const cls = status === "error" ? "danger" : status === "complete" ? "success" : status === "stopped" ? "secondary" : "info";
  const title = status === "error" ? escapeHtml(job.error_message || "") : "";
  return `<span class="badge text-bg-${cls}" title="${title}">${escapeHtml(status)}</span>`;
}
function renderMusicBulkScrapeStatus(job) {
  if (!job) {
    return `<div class="text-muted small">No scrape has been run yet.</div>`;
  }
  const total = Number(job.total || 0);
  const processed = Number(job.processed || 0);
  const pct = total > 0 ? Math.round((processed / total) * 100) : 100;
  const running = job.status === "running";
  return `
    <div class="d-flex align-items-center justify-content-between gap-2 mb-1">
      <div>${musicBulkScrapeStatusBadge(job)} <span class="small text-muted">${job.rescan_all ? "Rescanning all music" : "Scraping tracks missing artwork"}</span></div>
      <div class="small text-muted">${processed.toLocaleString()} / ${total.toLocaleString()}</div>
    </div>
    ${total > 0 ? `<div class="progress mb-2" style="height:0.5rem;"><div class="progress-bar${running ? " progress-bar-striped progress-bar-animated" : ""} bg-${job.status === "error" ? "danger" : job.status === "stopped" ? "secondary" : "primary"}" style="width:${pct}%"></div></div>` : ""}
    ${running && job.current_music ? `<div class="small text-muted mb-2"><span class="spinner-border spinner-border-sm me-1"></span>Scraping: ${escapeHtml(job.current_music)}</div>` : ""}
    <div class="small text-muted">
      ${musicBulkScrapeCountLink("matched", job.matched_count)}
      &middot; ${musicBulkScrapeCountLink("skipped", job.skipped_count)}
      &middot; ${musicBulkScrapeCountLink("failed", job.failed_count)}
    </div>
    ${job.status === "error" && job.error_message ? `<div class="alert alert-warning small mt-2 mb-0">${escapeHtml(job.error_message)}</div>` : ""}
  `;
}
function musicBulkScrapeCountLink(status, count) {
  const n = Number(count || 0);
  if (!n) return `${n.toLocaleString()} ${escapeHtml(status)}`;
  return `<button type="button" class="btn btn-link btn-sm p-0 align-baseline" onclick="toggleMusicBulkScrapeBreakdown(${jsAttr(status)})">${n.toLocaleString()} ${escapeHtml(status)}</button>`;
}
function patchMusicBulkScrapeLive(job) {
  const statusEl = document.getElementById("musicBulkScrapeStatus");
  if (statusEl) statusEl.innerHTML = renderMusicBulkScrapeStatus(job);
  const running = job && job.status === "running";
  const startBtn = document.getElementById("musicBulkScrapeStartBtn");
  if (startBtn) {
    startBtn.disabled = running;
    startBtn.innerHTML = running
      ? `<span class="spinner-border spinner-border-sm me-1"></span>Scraping...`
      : `<i class="bi bi-play-fill me-1"></i>Start Scraping`;
  }
  const stopBtn = document.getElementById("musicBulkScrapeStopBtn");
  if (stopBtn) {
    stopBtn.classList.toggle("d-none", !running);
    if (!running) musicBulkScrapeStopRequested = false;
    if (!musicBulkScrapeStopRequested) {
      stopBtn.disabled = false;
      stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
    }
  }
  const checkbox = document.getElementById("musicBulkScrapeRescanAll");
  if (checkbox) checkbox.disabled = running;
  if (musicBulkScrapeWasRunning && !running && musicBulkScrapeBreakdownStatus) {
    loadMusicBulkScrapeBreakdown(musicBulkScrapeBreakdownStatus, 0);
  }
  musicBulkScrapeWasRunning = running;
  startMusicBulkScrapeAutoRefreshIfNeeded(job);
}
function renderMusicAdminBulkScrapeCard(job) {
  const running = job && job.status === "running";
  return `
    <div class="card">
      <div class="card-header"><i class="bi bi-collection-play me-1"></i>Bulk Scrape</div>
      <div class="card-body">
        <div class="form-check mb-3">
          <input class="form-check-input" type="checkbox" id="musicBulkScrapeRescanAll" ${running ? "disabled" : ""}>
          <label class="form-check-label" for="musicBulkScrapeRescanAll">Rescan all music (unchecked: only scrape tracks missing artwork)</label>
        </div>
        <button id="musicBulkScrapeStartBtn" class="btn btn-primary" type="button" onclick="startMusicBulkScrape()" ${running ? "disabled" : ""}>
          ${running ? `<span class="spinner-border spinner-border-sm me-1"></span>Scraping...` : `<i class="bi bi-play-fill me-1"></i>Start Scraping`}
        </button>
        <button id="musicBulkScrapeStopBtn" class="btn btn-outline-danger ${running ? "" : "d-none"}" type="button" onclick="cancelMusicBulkScrape()">
          <i class="bi bi-stop-fill me-1"></i>Stop
        </button>
        <div id="musicBulkScrapeStatus" class="mt-3">${renderMusicBulkScrapeStatus(job)}</div>
        <div id="musicBulkScrapeBreakdown" class="mt-3"></div>
      </div>
    </div>
  `;
}
async function startMusicBulkScrape() {
  const checkbox = document.getElementById("musicBulkScrapeRescanAll");
  const rescanAll = checkbox ? checkbox.checked : false;
  setLoading(true, "Starting bulk scrape...");
  try {
    const result = await apiPost("/admin/music/scrape/bulk", { rescan_all: rescanAll });
    if (result.status === "already_running") {
      showToast("A bulk scrape is already running.", "warning");
    } else if (result.status === "error") {
      showToast(`Could not start scraping: ${escapeHtml(result.error || "unknown error")}`, "danger");
    } else {
      showToast("Bulk scrape started.", "success");
    }
  } catch (err) {
    showToast(`Could not start scraping: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
  if (window.location.hash === "#admin/music") {
    await renderAdminMusicArtworkPage();
  }
}
// See cancelMovieBulkScrape's comment -- same shape, the running job's own
// poll loop patches the status card live, this just flips the button state.
async function cancelMusicBulkScrape() {
  musicBulkScrapeStopRequested = true;
  const stopBtn = document.getElementById("musicBulkScrapeStopBtn");
  if (stopBtn) {
    stopBtn.disabled = true;
    stopBtn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>Stopping...`;
  }
  try {
    const result = await apiPost("/admin/music/scrape/bulk/stop", {});
    if (result.status === "not_running") {
      showToast("No bulk scrape is currently running.", "warning");
      musicBulkScrapeStopRequested = false;
      if (stopBtn) {
        stopBtn.disabled = false;
        stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
      }
    } else {
      showToast("Stopping bulk scrape...", "success");
    }
  } catch (err) {
    showToast(`Could not stop scraping: ${escapeHtml(err.message || "unknown error")}`, "danger");
    musicBulkScrapeStopRequested = false;
    if (stopBtn) {
      stopBtn.disabled = false;
      stopBtn.innerHTML = `<i class="bi bi-stop-fill me-1"></i>Stop`;
    }
  }
}
async function toggleMusicBulkScrapeBreakdown(status) {
  const container = document.getElementById("musicBulkScrapeBreakdown");
  if (musicBulkScrapeBreakdownStatus === status) {
    musicBulkScrapeBreakdownStatus = null;
    if (container) container.innerHTML = "";
    return;
  }
  await loadMusicBulkScrapeBreakdown(status, 0);
}
async function loadMusicBulkScrapeBreakdown(status, offset) {
  const container = document.getElementById("musicBulkScrapeBreakdown");
  if (!container) return;
  musicBulkScrapeBreakdownStatus = status;
  musicBulkScrapeBreakdownOffset = Math.max(0, offset || 0);
  container.innerHTML = `<div class="text-muted small"><span class="spinner-border spinner-border-sm me-2"></span>Loading ${escapeHtml(status)} music...</div>`;
  try {
    const page = await api(`/admin/music/scrape/bulk/items/${encodeURIComponent(status)}?limit=${MUSIC_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE}&offset=${musicBulkScrapeBreakdownOffset}`);
    if (musicBulkScrapeBreakdownStatus === status) container.innerHTML = renderMusicBulkScrapeBreakdownPanel(status, page);
  } catch (err) {
    container.innerHTML = `<div class="alert alert-warning small mb-0">Could not load ${escapeHtml(status)} list: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}
function renderMusicBulkScrapeBreakdownPanel(status, page) {
  const items = page.items || [];
  const total = Number(page.total || 0);
  const offset = Number(page.offset || 0);
  const limit = Number(page.limit || MUSIC_BULK_SCRAPE_BREAKDOWN_PAGE_SIZE);
  const showing = items.length ? `${(offset + 1).toLocaleString()}-${(offset + items.length).toLocaleString()} of ${total.toLocaleString()}` : "0 of 0";
  const retryAllBtn = status === "failed" && total
    ? `<button class="btn btn-outline-primary btn-sm text-nowrap" type="button" onclick="retryAllMusicBulkScrapeFailed()"><i class="bi bi-arrow-repeat me-1"></i>Retry all ${total.toLocaleString()}</button>`
    : "";
  const rows = items.length
    ? items.map((item) => `
      <div class="d-flex align-items-start justify-content-between gap-2 py-2 border-bottom movie-bulk-scrape-item-row">
        <div class="min-width-0">
          <button type="button" class="btn btn-link btn-sm p-0 text-start text-truncate d-block" style="max-width: 100%;" title="${escapeHtml(item.file_path || item.track_name || "")}" onclick="setHash(musicDetailHash(${jsAttr(item.entry_key)}))">${escapeHtml(item.track_name || item.file_path || "")}</button>
          ${item.reason ? `<div class="text-muted small">${escapeHtml(item.reason)}</div>` : ""}
        </div>
        ${
          status === "failed"
            ? `<button class="btn btn-outline-secondary btn-sm text-nowrap" type="button" onclick="retryMusicBulkScrapeItem(${jsAttr(item.entry_key)})"><i class="bi bi-arrow-repeat me-1"></i>Retry</button>`
            : ""
        }
      </div>
    `).join("")
    : `<div class="text-muted small py-2">Nothing here.</div>`;
  return `
    <div class="card log-card">
      <div class="card-header d-flex flex-wrap align-items-center justify-content-between gap-2">
        <span class="text-capitalize">${escapeHtml(status)} music</span>
        <div class="d-flex align-items-center gap-2">
          ${retryAllBtn}
          <button class="btn btn-outline-secondary btn-sm" type="button" title="Close" onclick="toggleMusicBulkScrapeBreakdown(${jsAttr(status)})"><i class="bi bi-x-lg"></i></button>
        </div>
      </div>
      <div class="card-body p-0" style="max-height: 360px; overflow-y: auto;">
        <div class="px-3">${rows}</div>
      </div>
      <div class="card-footer d-flex align-items-center justify-content-between gap-2">
        <span class="small text-muted">${showing}</span>
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-outline-primary" type="button" ${offset <= 0 ? "disabled" : ""} onclick="loadMusicBulkScrapeBreakdown(${jsAttr(status)}, ${Math.max(0, offset - limit)})">Previous</button>
          <button class="btn btn-sm btn-outline-primary" type="button" ${offset + items.length >= total ? "disabled" : ""} onclick="loadMusicBulkScrapeBreakdown(${jsAttr(status)}, ${offset + limit})">Next</button>
        </div>
      </div>
    </div>
  `;
}
async function startMusicBulkScrapeRetry(body, loadingText) {
  setLoading(true, loadingText);
  try {
    const result = await apiPost("/admin/music/scrape/bulk/retry", body);
    if (result.status === "already_running") {
      showToast("A scrape is already running -- try again once it finishes.", "warning");
    } else if (result.status === "error") {
      showToast(`Could not start retry: ${escapeHtml(result.error || "unknown error")}`, "danger");
    } else {
      showToast("Retry started.", "success");
      const statusPayload = await api("/admin/music/scrape/bulk");
      patchMusicBulkScrapeLive(statusPayload.job || null);
    }
  } catch (err) {
    showToast(`Could not start retry: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function retryMusicBulkScrapeItem(entryKey) {
  await startMusicBulkScrapeRetry({ entry_keys: [entryKey] }, "Retrying...");
}
async function retryAllMusicBulkScrapeFailed() {
  await startMusicBulkScrapeRetry({ status: "failed" }, "Retrying all failed tracks...");
}
async function renderAdminMusicArtworkPage() {
  currentSystemContext = null;
  clearSystemTheme();
  musicBulkScrapeBreakdownStatus = null;
  setLoading(true, "Loading music scraper status...");
  try {
    const statusPayload = await api("/admin/music/scrape/bulk");
    content.innerHTML = `
      ${renderArtworkTabBar("music")}
      <div class="text-muted small mb-3">Scrape MusicBrainz + Cover Art Archive for album cover art and release-level metadata (artist, album, genres) -- the same album-level scraper available at the bottom of each album's own details page, run here in bulk across your whole library. Track titles are always taken from your own filenames, never overwritten. No account or API key needed.</div>
      <div id="musicAdminScraperCard">${renderMusicAdminBulkScrapeCard(statusPayload.job)}</div>
    `;
    startMusicBulkScrapeAutoRefreshIfNeeded(statusPayload.job);
  } catch (err) {
    content.innerHTML = `${renderArtworkTabBar("music")}<div class="alert alert-danger">Failed to load music scraper status: ${escapeHtml(err.message || "unknown error")}</div>`;
  } finally {
    setLoading(false);
  }
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
function romMediaHash(system, uniqueId, page = 1) {
  const safePage = Math.max(1, Number(page || 1));
  return `#system/${encodeURIComponent(system)}/rom/${encodeURIComponent(uniqueId)}${safePage > 1 ? `?page=${safePage}` : ""}`;
}
function parseSystemRomHash(hash) {
  if (!hash.startsWith("#system/")) return null;
  const rest = hash.substring("#system/".length);
  const marker = "/rom/";
  const markerIndex = rest.indexOf(marker);
  if (markerIndex < 0) return null;
  const tail = rest.substring(markerIndex + marker.length);
  const [idPart, queryPart = ""] = tail.split("?", 2);
  const params = new URLSearchParams(queryPart);
  return {
    system: decodeURIComponent(rest.substring(0, markerIndex)),
    uniqueId: decodeURIComponent(idPart),
    page: Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1),
  };
}
let browserPlaySupportedSystemsPromise = null;
// Resolves to { systems: {batocera_system: ejs_core_id}, romsetSensitive: Set<system> }.
// romsetSensitive (mame/fba/fbneo) flags systems where a vendored core existing
// doesn't mean a given ROM will actually boot -- see browser_play.py's
// ROMSET_SENSITIVE_SYSTEMS -- so callers can show a compatibility caveat
// instead of the plain "will work" promise every other system gets.
function browserPlaySupportedSystems() {
  if (!browserPlaySupportedSystemsPromise) {
    browserPlaySupportedSystemsPromise = api("/browser-play/supported-systems")
      .then((data) => ({ systems: data.systems || {}, romsetSensitive: new Set(data.romset_sensitive || []) }))
      .catch(() => ({ systems: {}, romsetSensitive: new Set() }));
  }
  return browserPlaySupportedSystemsPromise;
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
async function renderRomMediaPage(system, uniqueId, page = 1) {
  currentSystemContext = system;
  backBtn.classList.remove("d-none");
  setLoading(true, "Loading ROM media...");
  try {
    const [romsData, browserPlayInfo] = await Promise.all([
      getSystemRomData(system),
      browserPlaySupportedSystems(),
      applySystemTheme(system),
    ]);
    const roms = romsData.roms || [];
    const rom = roms.find((item) => String(item.unique_id || "") === String(uniqueId || ""));
    if (!rom) throw new Error("ROM not found");
    rom.system = system;
    const media = romMediaItems(system, rom);
    const primary = media.find((item) => item.field === "image") || media[0];
    const videoUrl = romVideoUrl(rom);
    const systemLower = String(system || "").toLowerCase();
    const browserPlayCore = browserPlayInfo.systems[systemLower];
    const canPlayInBrowser = Boolean(browserPlayCore) && rom.is_downloadable !== false;
    const browserPlayRomsetSensitive = canPlayInBrowser && browserPlayInfo.romsetSensitive.has(systemLower);
    titleNode.textContent = rom.title || rom.name || "ROM Media";
    subtitleNode.textContent = `${system} artwork and gamelist.xml metadata`;
    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap gap-2">
        <button class="btn btn-outline-secondary" onclick="setHash('${systemsExploreHash(system)}')">← Back to ${escapeHtml(system)}</button>
        ${
          canPlayInBrowser
            ? `<a class="btn btn-success" target="_blank" rel="noopener noreferrer" href="${escapeHtml(browserPlayUrl(system, rom.unique_id, rom.title || rom.name || "", browserPlayCore))}"><i class="bi bi-play-fill me-1"></i>Play in Browser</a>`
            : ""
        }
        ${
          rom.is_downloadable === false
            ? `<button class="btn btn-outline-secondary" type="button" disabled><i class="bi bi-folder2-open me-1"></i>Folder ROM</button>`
            : `<a class="btn btn-primary" href="${romDownloadUrl(system, rom.unique_id)}"><i class="bi bi-download me-1"></i>Download</a>`
        }
        ${
          adminEnabled
            ? `<button class="btn btn-outline-danger" type="button" onclick="deleteRomFromDetailPage(${jsAttr(system)}, ${jsAttr(rom.unique_id)}, ${jsAttr(rom.title || rom.name || "")})"><i class="bi bi-trash me-1"></i>Delete</button>`
            : ""
        }
      </div>
      ${
        browserPlayRomsetSensitive
          ? `<div class="alert alert-warning py-2 px-3 small mb-3"><i class="bi bi-exclamation-triangle me-1"></i>Arcade romset compatibility isn't guaranteed in the browser -- this file needs to exactly match the version the browser emulator core expects, which may differ from what runs on the device itself.</div>`
          : ""
      }
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
      <div class="card log-card mb-3">
        <div class="card-header">Preview Video</div>
        <div class="card-body" id="romMediaVideoBody">
          ${videoUrl
            ? `<video class="rom-media-video" id="romMediaVideoPlayer" src="${escapeHtml(videoUrl)}" controls preload="metadata"></video>`
            : `<div class="text-muted">No preview video set for this ROM. Add one below under Metadata &amp; Artwork Tools → Manual Upload.</div>`
          }
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
        <div class="card-header">Metadata &amp; Artwork Tools</div>
        <div class="card-body">
          <div class="mb-3">
            <div class="fw-semibold mb-2">Metadata</div>
            ${romMetadataEditFormHtml(rom)}
          </div>
          <div class="mb-3">${artworkExternalLinksHtml(rom)}</div>
          <div class="mb-3">
            <div class="fw-semibold mb-2">Manual Upload (images &amp; video)</div>
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
    const videoPlayer = document.getElementById("romMediaVideoPlayer");
    if (videoPlayer) {
      videoPlayer.addEventListener("error", () => {
        const body = document.getElementById("romMediaVideoBody");
        if (body) body.innerHTML = `<div class="text-muted">Video could not be loaded.</div>`;
      });
    }
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
    setHash(systemsExploreHash(system));
  } finally {
    setLoading(false);
  }
}
// Opens EmulatorJS in a new tab rather than an <iframe> or the SPA's own DOM.
// EmulatorJS wants crossOriginIsolated (COOP/COEP -- see ui_routes.py's
// _EMULATORJS_PLAYER_CSP) for its pthread-enabled core build, which only takes
// effect if every ancestor frame has those headers; retrofitting that onto the
// whole SPA page risked breaking its existing cross-origin assets (Bootstrap/
// fonts/Cast SDK). A new top-level tab is its own browsing context with
// independent headers, sidestepping that entirely -- and, as a bonus, leaves no
// EmulatorJS globals/WASM/audio-context state behind in the SPA's own realm.
function browserPlayUrl(system, uniqueId, gameName, core) {
  const params = new URLSearchParams({
    core,
    gameUrl: romDownloadUrl(system, uniqueId),
    system,
    uniqueId,
    gameName: gameName || "",
  });
  return `/static/emulatorjs/player.html?${params.toString()}`;
}
// ROM files are gone from disk after this -- the client-side systems/ROM
// caches are invalidated wholesale (not just the deleted row) since every
// Systems view reads from these same snapshots with no per-row
// invalidation of their own; the next view that needs them refetches via
// their existing forceRefresh-less lazy-load guards.
async function deleteRomsBatch(items) {
  const result = await apiPost("/admin/roms/delete", { items });
  systemsCache = null;
  systemRomCache = {};
  return result;
}
function deleteRomFromDetailPage(system, uniqueId, title) {
  openConfirmDeleteModal({
    title: "Delete ROM?",
    body: `<strong>${escapeHtml(title)}</strong> will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      setLoading(true, "Deleting...");
      try {
        await deleteRomsBatch([{ system, unique_id: uniqueId }]);
        showToast("ROM deleted.", "success");
        setHash(systemsExploreHash(system));
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
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
    ${renderArtworkTabBar("theme")}
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
async function renderThemeGalleryPage() {
  currentSystemContext = null;
  setLoading(true, "Loading theme images...");
  clearSystemTheme();
  await refreshRandomThemeLogo();
  themeFilterInitialized = false;
  themeFilterSelectedSystems = [];
  await loadThemePage(0);
  setLoading(false);
}
// "show" is reserved on the movies side for the same reason noted at
// showDetailHash -- not relevant here, but kept as a plain query-string hash
// (not a path segment) for the same reason: system/genre names can contain
// characters (spaces, slashes in some scraped genre strings) that are easier
// to carry as encoded query values than path segments.
function systemsExploreHash(system = systemsExploreSelectedSystem, genre = systemsExploreSelectedGenre) {
  const params = new URLSearchParams();
  if (system) params.set("system", system);
  if (genre) params.set("genre", genre);
  const qs = params.toString();
  return `#systems/explore${qs ? `?${qs}` : ""}`;
}
function parseSystemsExploreHash(hash) {
  if (!hash.startsWith("#systems/explore")) return null;
  const queryIndex = hash.indexOf("?");
  const params = new URLSearchParams(queryIndex >= 0 ? hash.substring(queryIndex + 1) : "");
  return { system: params.get("system") || "", genre: params.get("genre") || "" };
}
async function renderSystemsExplorePage() {
  currentSystemContext = null;
  clearSystemTheme();
  const parsed = parseSystemsExploreHash(window.location.hash) || { system: "", genre: "" };
  systemsExploreSelectedSystem = parsed.system;
  systemsExploreSelectedGenre = parsed.genre;
  systemsExploreSearchQuery = "";
  systemsExploreShowAllSystems = false;
  systemsExploreShowAllCategories = false;
  systemsExploreSystemFilterQuery = "";
  systemsExploreCategoryFilterQuery = "";
  systemsExploreBiosItems = [];
  systemsExploreDuplicatesMode = false;
  systemsExploreDuplicateGroups = [];
  systemsExploreBrowserPlayOnly = false;
  setLoading(true, "Loading systems...");
  try {
    const [data, biosSummary, browserPlayInfo] = await Promise.all([
      getSystemsData(),
      api("/bios?limit=1&offset=0").catch(() => ({ count: 0 })),
      browserPlaySupportedSystems(),
    ]);
    systemsExploreAllSystems = (data.systems || []).slice().sort((a, b) => Number(b.rom_count || 0) - Number(a.rom_count || 0));
    systemsExploreBiosTotal = Number(biosSummary.count || 0);
    systemsExploreBrowserPlayMap = browserPlayInfo.systems || {};
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-controller me-2"></i>Systems</div>
          <div class="movie-explorer-search flex-grow-1">
            <input id="systemsExploreSearch" type="search" class="form-control" placeholder="Search games" oninput="filterSystemsExplore(this.value)" autofocus>
          </div>
          <button id="systemsExploreBrowserPlayBtn" class="btn btn-outline-light btn-sm" type="button" title="Show only games playable in Chrome (Play in Browser -- other browsers aren't supported yet)" onclick="toggleSystemsExploreBrowserPlayOnly()"><i class="bi bi-play-circle"></i></button>
          <button id="systemsExploreDuplicatesBtn" class="btn btn-outline-light btn-sm" type="button" title="Find duplicate games" onclick="toggleSystemsExploreDuplicatesMode()"><i class="bi bi-files"></i></button>
          ${renderAssetTypeSwitcher("systems")}
        </div>
        <div class="movie-explorer-body">
          <aside id="systems-explore-sidebar" class="movie-explorer-sidebar"></aside>
          <div id="systems-explore-grid-wrap" class="movie-explorer-grid-wrap min-width-0">
            <div id="systems-explore-grid" class="movie-explorer-grid"></div>
            <div id="systems-explore-more" class="text-center mt-3"></div>
          </div>
        </div>
      </div>
    `;
    renderSystemsExploreSidebarShell();
    await loadSystemsExploreCurrentMode({ reset: true });
    restoreMovieListScroll("#systems/explore");
  } catch (err) {
    content.innerHTML = `
      <div class="movie-explorer-overlay">
        <div class="movie-explorer-topbar">
          <div class="movie-explorer-brand"><i class="bi bi-controller me-2"></i>Systems</div>
          ${renderAssetTypeSwitcher("systems")}
        </div>
        <div class="alert alert-danger m-3">Failed to load systems: ${escapeHtml(err.message || "unknown error")}</div>
      </div>
    `;
  } finally {
    setLoading(false);
  }
}
// The sidebar is a stable shell (rendered once, holds the two search boxes)
// wrapping two independently-refreshed list sections -- re-rendering the
// whole sidebar's innerHTML on every keystroke would tear down and recreate
// the search <input> itself, losing focus/cursor position mid-type. Only
// the list containers get replaced; the boxes stay put.
function renderSystemsExploreSidebarShell() {
  const sidebar = document.getElementById("systems-explore-sidebar");
  if (!sidebar) return;
  sidebar.innerHTML = `
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">System</div>
      <input type="search" class="form-control form-control-sm movie-explorer-sidebar-search" placeholder="Search systems" oninput="filterSystemsExploreSystemList(this.value)">
      <div id="systems-explore-system-list"></div>
    </div>
    <div class="movie-explorer-sidebar-section">
      <div class="movie-explorer-sidebar-title">Category</div>
      <input type="search" class="form-control form-control-sm movie-explorer-sidebar-search" placeholder="Search categories" oninput="filterSystemsExploreCategoryList(this.value)">
      <div id="systems-explore-category-list"></div>
    </div>
  `;
  renderSystemsExploreSystemList();
  renderSystemsExploreCategoryList();
}
function systemsExploreVisibleSystems() {
  const base = systemsExploreBrowserPlayOnly
    ? systemsExploreAllSystems.filter((s) => systemsExploreBrowserPlayMap[String(s.name || "").toLowerCase()])
    : systemsExploreAllSystems;
  const search = systemsExploreSystemFilterQuery.trim().toLowerCase();
  if (search) return base.filter((s) => String(s.name || "").toLowerCase().includes(search));
  return systemsExploreShowAllSystems ? base : base.slice(0, SYSTEMS_EXPLORE_TOP_SYSTEM_COUNT);
}
// The threshold trim (see the constants above) and the top-N-unless-expanded
// cap are independent steps -- trim first, then cap what's left, so a
// "Show more" click reveals the rest of the *signal* categories, never the
// long tail the trim already decided wasn't worth surfacing.
function systemsExploreCategoriesAfterThreshold() {
  const hasDominantCategory = systemsExploreGenreCounts.some((g) => Number(g.count || 0) >= SYSTEMS_EXPLORE_CATEGORY_DOMINANT_THRESHOLD);
  return hasDominantCategory
    ? systemsExploreGenreCounts.filter((g) => Number(g.count || 0) >= SYSTEMS_EXPLORE_CATEGORY_MIN_COUNT)
    : systemsExploreGenreCounts;
}
function systemsExploreVisibleCategories() {
  const search = systemsExploreCategoryFilterQuery.trim().toLowerCase();
  if (search) return systemsExploreGenreCounts.filter((g) => String(g.name || "").toLowerCase().includes(search));
  const afterThreshold = systemsExploreCategoriesAfterThreshold();
  return systemsExploreShowAllCategories ? afterThreshold : afterThreshold.slice(0, SYSTEMS_EXPLORE_TOP_CATEGORY_COUNT);
}
function renderSystemsExploreSystemList() {
  const list = document.getElementById("systems-explore-system-list");
  if (!list) return;
  const systemButton = (value, label, count) => `
    <button type="button" class="movie-explorer-category-btn ${systemsExploreSelectedSystem === value ? "active" : ""}" onclick="setSystemsExploreSystem(${jsAttr(value)})">
      <span>${escapeHtml(label)}</span><span class="movie-explorer-category-count">${Number(count || 0).toLocaleString()}</span>
    </button>
  `;
  const searching = Boolean(systemsExploreSystemFilterQuery.trim());
  const visibleSystems = systemsExploreVisibleSystems();
  // "All Systems" here means "all systems currently in scope" -- every system
  // normally, but only the browser-playable ones while that filter is on --
  // so its count badge and the "Show more" threshold both match what's
  // actually reachable instead of counting systems the toggle has hidden.
  const scopedSystems = systemsExploreBrowserPlayOnly
    ? systemsExploreAllSystems.filter((s) => systemsExploreBrowserPlayMap[String(s.name || "").toLowerCase()])
    : systemsExploreAllSystems;
  const totalRoms = scopedSystems.reduce((sum, s) => sum + Number(s.rom_count || 0), 0);
  // "Show more" only makes sense against the passive top-5 default -- an
  // active search already shows every match, uncapped.
  const canExpand = !searching && scopedSystems.length > SYSTEMS_EXPLORE_TOP_SYSTEM_COUNT;
  list.innerHTML = `
    ${searching ? "" : systemButton("", "All Systems", totalRoms)}
    ${searching || systemsExploreBrowserPlayOnly ? "" : systemButton(SYSTEMS_EXPLORE_BIOS_KEY, "BIOS", systemsExploreBiosTotal)}
    ${
      visibleSystems.length
        ? visibleSystems.map((s) => systemButton(s.name, s.name, s.rom_count)).join("")
        : `<div class="text-muted small">No systems match "${escapeHtml(systemsExploreSystemFilterQuery)}".</div>`
    }
    ${canExpand ? `
      <button type="button" class="movie-explorer-category-btn movie-explorer-sidebar-more-btn" onclick="toggleSystemsExploreShowAllSystems()">
        ${systemsExploreShowAllSystems ? "Show less" : `Show more (${(systemsExploreAllSystems.length - SYSTEMS_EXPLORE_TOP_SYSTEM_COUNT).toLocaleString()})`}
      </button>
    ` : ""}
  `;
}
function renderSystemsExploreCategoryList() {
  const list = document.getElementById("systems-explore-category-list");
  if (!list) return;
  if (systemsExploreSelectedSystem === SYSTEMS_EXPLORE_BIOS_KEY) {
    list.innerHTML = `<div class="text-muted small">Not applicable for BIOS files.</div>`;
    return;
  }
  const genreButton = (value, label, count) => `
    <button type="button" class="movie-explorer-category-btn ${systemsExploreSelectedGenre === value ? "active" : ""}" onclick="setSystemsExploreGenre(${jsAttr(value)})">
      <span>${escapeHtml(label)}</span><span class="movie-explorer-category-count">${Number(count || 0).toLocaleString()}</span>
    </button>
  `;
  const searching = Boolean(systemsExploreCategoryFilterQuery.trim());
  const afterThreshold = systemsExploreCategoriesAfterThreshold();
  const visible = systemsExploreVisibleCategories();
  const canExpand = !searching && afterThreshold.length > SYSTEMS_EXPLORE_TOP_CATEGORY_COUNT;
  let emptyMessage = "Scrape games to see categories.";
  if (systemsExploreGenreCounts.length && searching) {
    emptyMessage = `No categories match "${escapeHtml(systemsExploreCategoryFilterQuery)}".`;
  }
  list.innerHTML = `
    ${searching ? "" : genreButton("", "All Categories", systemsExploreTotal)}
    ${visible.length ? visible.map((g) => genreButton(g.name, g.name, g.count)).join("") : `<div class="text-muted small">${emptyMessage}</div>`}
    ${canExpand ? `
      <button type="button" class="movie-explorer-category-btn movie-explorer-sidebar-more-btn" onclick="toggleSystemsExploreShowAllCategories()">
        ${systemsExploreShowAllCategories ? "Show less" : `Show more (${(afterThreshold.length - SYSTEMS_EXPLORE_TOP_CATEGORY_COUNT).toLocaleString()})`}
      </button>
    ` : ""}
  `;
}
function filterSystemsExploreSystemList(value) {
  systemsExploreSystemFilterQuery = value || "";
  renderSystemsExploreSystemList();
}
function filterSystemsExploreCategoryList(value) {
  systemsExploreCategoryFilterQuery = value || "";
  renderSystemsExploreCategoryList();
}
async function setSystemsExploreSystem(value) {
  systemsExploreSelectedSystem = value;
  systemsExploreSelectedGenre = "";
  updateSystemsExploreHash();
  renderSystemsExploreSystemList();
  renderSystemsExploreCategoryList();
  await loadSystemsExploreCurrentMode({ reset: true });
}
async function setSystemsExploreGenre(value) {
  systemsExploreSelectedGenre = value;
  updateSystemsExploreHash();
  renderSystemsExploreSystemList();
  renderSystemsExploreCategoryList();
  await loadSystemsExploreCurrentMode({ reset: true });
}
function toggleSystemsExploreShowAllSystems() {
  systemsExploreShowAllSystems = !systemsExploreShowAllSystems;
  renderSystemsExploreSystemList();
}
function toggleSystemsExploreShowAllCategories() {
  systemsExploreShowAllCategories = !systemsExploreShowAllCategories;
  renderSystemsExploreCategoryList();
}
function updateSystemsExploreHash() {
  const nextHash = systemsExploreHash();
  if (window.location.hash !== nextHash) history.replaceState(null, "", nextHash);
}
let systemsExploreSearchDebounce = null;
function filterSystemsExplore(value) {
  systemsExploreSearchQuery = value || "";
  clearTimeout(systemsExploreSearchDebounce);
  systemsExploreSearchDebounce = setTimeout(() => loadSystemsExploreCurrentMode({ reset: true }), 300);
}
function loadSystemsExploreCurrentMode(opts = {}) {
  if (systemsExploreSelectedSystem === SYSTEMS_EXPLORE_BIOS_KEY) return loadSystemsExploreBios(opts);
  if (systemsExploreDuplicatesMode) return loadSystemsExploreDuplicates();
  return loadSystemsExploreRoms(opts);
}
async function toggleSystemsExploreDuplicatesMode() {
  systemsExploreDuplicatesMode = !systemsExploreDuplicatesMode;
  // BIOS has no "duplicate" concept of its own -- turning duplicates mode
  // on while it's selected falls back to "All Systems" instead.
  if (systemsExploreDuplicatesMode && systemsExploreSelectedSystem === SYSTEMS_EXPLORE_BIOS_KEY) {
    systemsExploreSelectedSystem = "";
    updateSystemsExploreHash();
    renderSystemsExploreSystemList();
    renderSystemsExploreCategoryList();
  }
  document.getElementById("systemsExploreDuplicatesBtn")?.classList.toggle("active", systemsExploreDuplicatesMode);
  await loadSystemsExploreCurrentMode({ reset: true });
}
async function toggleSystemsExploreBrowserPlayOnly() {
  systemsExploreBrowserPlayOnly = !systemsExploreBrowserPlayOnly;
  // BIOS isn't a game system and no system outside SYSTEM_CORE_MAP has any
  // results left once filtered -- both fall back to "All Systems" (playable
  // systems only) rather than leaving the selection pointed at something
  // that can only ever show zero results while the toggle is on.
  const selected = systemsExploreSelectedSystem.toLowerCase();
  if (systemsExploreBrowserPlayOnly && (selected === SYSTEMS_EXPLORE_BIOS_KEY.toLowerCase() || (selected && !systemsExploreBrowserPlayMap[selected]))) {
    systemsExploreSelectedSystem = "";
    updateSystemsExploreHash();
  }
  renderSystemsExploreSystemList();
  renderSystemsExploreCategoryList();
  document.getElementById("systemsExploreBrowserPlayBtn")?.classList.toggle("active", systemsExploreBrowserPlayOnly);
  await loadSystemsExploreCurrentMode({ reset: true });
}
async function loadSystemsExploreDuplicates() {
  systemsExploreLoadingMore = true;
  renderSystemsExploreGrid();
  try {
    const params = new URLSearchParams();
    if (systemsExploreSelectedSystem) params.set("system", systemsExploreSelectedSystem);
    if (systemsExploreSelectedGenre) params.set("genre", systemsExploreSelectedGenre);
    if (systemsExploreSearchQuery.trim()) params.set("q", systemsExploreSearchQuery.trim());
    const data = await api(`/admin/roms/duplicates?${params.toString()}`);
    systemsExploreDuplicateGroups = data.groups || [];
  } catch (err) {
    showToast(`Failed to load duplicates: ${escapeHtml(err.message || "unknown error")}`, "danger");
    systemsExploreDuplicateGroups = [];
  } finally {
    systemsExploreLoadingMore = false;
    renderSystemsExploreGrid();
  }
}
async function loadSystemsExploreBios(opts = {}) {
  const reset = Boolean(opts.reset);
  const offset = reset ? 0 : systemsExploreBiosItems.length;
  if (systemsExploreLoadingMore) return;
  systemsExploreLoadingMore = true;
  renderSystemsExploreMoreButton();
  try {
    const params = new URLSearchParams({ limit: String(SYSTEMS_EXPLORE_PAGE_SIZE), offset: String(offset) });
    if (systemsExploreSearchQuery.trim()) params.set("q", systemsExploreSearchQuery.trim());
    const data = await api(`/bios?${params.toString()}`);
    systemsExploreBiosItems = reset ? (data.bios || []) : [...systemsExploreBiosItems, ...(data.bios || [])];
    systemsExploreTotal = Number(data.count || 0);
    systemsExploreHasMore = Boolean(data.has_more);
  } catch (err) {
    showToast(`Failed to load BIOS files: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    systemsExploreLoadingMore = false;
    renderSystemsExploreGrid();
  }
}
async function loadSystemsExploreRoms(opts = {}) {
  const reset = Boolean(opts.reset);
  const offset = reset ? 0 : systemsExploreRoms.length;
  if (systemsExploreLoadingMore) return;
  systemsExploreLoadingMore = true;
  renderSystemsExploreMoreButton();
  try {
    const params = new URLSearchParams({ limit: String(SYSTEMS_EXPLORE_PAGE_SIZE), offset: String(offset) });
    if (systemsExploreSelectedSystem) params.set("system", systemsExploreSelectedSystem);
    if (systemsExploreSelectedGenre) params.set("genre", systemsExploreSelectedGenre);
    if (systemsExploreSearchQuery.trim()) params.set("q", systemsExploreSearchQuery.trim());
    if (systemsExploreBrowserPlayOnly) params.set("browser_playable", "1");
    const data = await api(`/roms?${params.toString()}`);
    systemsExploreRoms = reset ? (data.roms || []) : [...systemsExploreRoms, ...(data.roms || [])];
    systemsExploreTotal = Number(data.count || 0);
    systemsExploreHasMore = Boolean(data.has_more);
    systemsExploreGenreCounts = data.genres || [];
    // Only the Category list depends on the /roms response -- System counts
    // come from the separate /systems fetch at page load and don't change
    // per query.
    if (reset) renderSystemsExploreCategoryList();
  } catch (err) {
    showToast(`Failed to load games: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    systemsExploreLoadingMore = false;
    renderSystemsExploreGrid();
  }
}
function renderSystemsExploreGrid() {
  const grid = document.getElementById("systems-explore-grid");
  if (!grid) return;
  const isBios = systemsExploreSelectedSystem === SYSTEMS_EXPLORE_BIOS_KEY;
  // #systems-explore-grid is normally a poster-card CSS grid (150px min
  // columns, see .movie-explorer-grid) -- BIOS rows and duplicate groups are
  // both plain lists with no artwork, so without this override each row
  // gets squeezed into a single ~150px card column and its label truncates
  // to nothing (confirmed live: the row rendered with icon/size/button
  // visible but an empty-looking label, even though the DOM text was there).
  grid.classList.toggle("movie-explorer-grid-list", isBios || systemsExploreDuplicatesMode);
  if (isBios) {
    grid.innerHTML = systemsExploreBiosItems.length
      ? `<div class="tree-leaf-list">${systemsExploreBiosItems.map(renderSystemsExploreBiosRow).join("")}</div>`
      : `<div class="text-muted p-4">No BIOS files match the current filters.</div>`;
    renderSystemsExploreMoreButton();
    return;
  }
  if (systemsExploreDuplicatesMode) {
    renderSystemsExploreDuplicatesGrid(grid);
    renderSystemsExploreMoreButton();
    return;
  }
  grid.innerHTML = systemsExploreRoms.length
    ? systemsExploreRoms.map(renderSystemsExploreCard).join("")
    : `<div class="text-muted p-4">No games match the current filters.</div>`;
  renderSystemsExploreMoreButton();
  setupLazyImages();
}
function renderSystemsExploreDuplicatesGrid(grid) {
  if (systemsExploreLoadingMore) {
    grid.innerHTML = `<div class="text-muted p-4"><span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Scanning for duplicates...</div>`;
    return;
  }
  const groups = systemsExploreDuplicateGroups;
  if (!groups.length) {
    grid.innerHTML = `<div class="text-muted p-4">No duplicate games found in the current filters.</div>`;
    return;
  }
  const deletableCount = groups.reduce((sum, group) => sum + group.items.filter((item) => !item.recommended_keep).length, 0);
  grid.innerHTML = `
    <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-3">
      <div class="text-muted small">${groups.length.toLocaleString()} duplicate game${groups.length === 1 ? "" : "s"} found &middot; ${deletableCount.toLocaleString()} extra cop${deletableCount === 1 ? "y" : "ies"} can be removed.</div>
      <button class="btn btn-danger btn-sm" type="button" onclick="openRomDuplicatesReviewModal()"><i class="bi bi-trash me-1"></i>Review &amp; Delete Duplicates</button>
    </div>
    <div class="d-flex flex-column gap-3">
      ${groups.map(renderSystemsExploreDuplicateGroup).join("")}
    </div>
  `;
}
function renderSystemsExploreDuplicateGroup(group) {
  return `
    <div class="card log-card">
      <div class="card-header d-flex justify-content-between align-items-center gap-2">
        <span class="fw-semibold text-capitalize text-truncate">${escapeHtml(group.normalized_title)}</span>
        <span class="badge text-bg-secondary text-nowrap">${escapeHtml(group.system)} &middot; ${group.items.length}</span>
      </div>
      <div class="tree-leaf-list">
        ${group.items.map((item) => `
          <div class="tree-grid-row tree-leaf-row">
            <div class="tree-grid-main">
              <i class="bi bi-controller tree-grid-icon"></i>
              <div class="tree-grid-label text-truncate" title="${escapeHtml(item.rom_name)}">
                <span class="fw-semibold">${escapeHtml(item.rom_name)}</span>
              </div>
            </div>
            <div class="tree-grid-meta d-flex align-items-center gap-2">
              ${item.recommended_keep ? `<span class="badge text-bg-success">Keep</span>` : ""}
              <span>${escapeHtml(item.byte_count !== undefined ? formatBytes(item.byte_count) : "n/a")}</span>
            </div>
            <div class="tree-grid-action">
              <button class="btn btn-outline-danger btn-sm" type="button" title="Delete this copy" onclick="deleteRomDuplicateItem(${jsAttr(item.system)}, ${jsAttr(item.unique_id)}, ${jsAttr(item.rom_name)})"><i class="bi bi-trash"></i></button>
            </div>
          </div>
        `).join("")}
      </div>
    </div>
  `;
}
// Targeted single delete for one duplicate-list row -- distinct from the bulk
// "Review & Delete Duplicates" modal (openRomDuplicatesReviewModal): confirms
// then deletes just this one copy and refreshes the duplicates list in place
// (no navigation away, unlike deleteRomFromDetailPage's own-page delete).
function deleteRomDuplicateItem(system, uniqueId, title) {
  openConfirmDeleteModal({
    title: "Delete game?",
    body: `<strong>${escapeHtml(title)}</strong> will be permanently deleted from disk. This cannot be undone.`,
    confirmLabel: "Delete",
    onConfirm: async () => {
      setLoading(true, "Deleting...");
      try {
        await deleteRomsBatch([{ system, unique_id: uniqueId }]);
        showToast("Game deleted.", "success");
        loadSystemsExploreDuplicates();
      } catch (err) {
        showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      } finally {
        setLoading(false);
      }
    },
  });
}
function openRomDuplicatesReviewModal() {
  const groups = systemsExploreDuplicateGroups.map((group) => ({
    label: `${group.normalized_title} (${group.system})`,
    items: group.items,
  }));
  openDuplicatesReviewModal({
    title: "Delete duplicate games?",
    groups,
    itemLabel: (item) => item.rom_name,
    itemMeta: (item) => (item.byte_count !== undefined ? formatBytes(item.byte_count) : "n/a"),
    deleteFn: (items) => deleteRomsBatch(items.map((item) => ({ system: item.system, unique_id: item.unique_id }))),
    onDeleted: () => loadSystemsExploreDuplicates(),
  });
}
function renderSystemsExploreBiosRow(item) {
  const path = item.path || item.name || "";
  const label = item.name || path;
  const fingerprint = item.bios_md5 || item.md5 || item.fingerprint || "";
  const tooltip = fingerprint ? `${path} · ${fingerprint}` : path;
  const size = item.byte_count !== undefined ? formatBytes(item.byte_count) : "n/a";
  // The accurate, per-file system association resolved at scan time against
  // the vendored BIOS-md5 reference table (see rom_metadata_store.py's
  // BiosCacheRow) -- distinct from the coarse folder-path grouping used for
  // the sidebar's own System facet. Zero or multiple systems both land in
  // the Unassigned/shared bucket, same convention as the "unassigned"
  // filter elsewhere. Kept in the always-visible meta column (not appended
  // to the truncating filename label) so it never gets clipped for a long
  // filename.
  const systems = Array.isArray(item.systems) ? item.systems.filter(Boolean) : [];
  const systemsLabel = systems.length ? systems.join(", ") : "Unassigned";
  return `
    <div class="tree-grid-row tree-leaf-row">
      <div class="tree-grid-main">
        <i class="bi bi-cpu tree-grid-icon"></i>
        <div class="tree-grid-label text-truncate" title="${escapeHtml(tooltip)}">
          <span class="fw-semibold">${escapeHtml(label)}</span>
        </div>
      </div>
      <div class="tree-grid-meta d-flex align-items-center gap-2">
        <span class="badge text-bg-secondary text-truncate${systems.length ? "" : " opacity-50"}" style="max-width:160px" title="${escapeHtml(systemsLabel)}">${escapeHtml(systemsLabel)}</span>
        <span>${escapeHtml(size)}</span>
      </div>
      <div class="tree-grid-action">
        ${
          item.is_downloadable === false
            ? `<button class="btn btn-secondary btn-sm" type="button" title="Downloads disabled" disabled><i class="bi bi-slash-circle"></i></button>`
            : `<a class="btn btn-primary btn-sm" title="Download" href="${biosDownloadUrl(item.unique_id)}"><i class="bi bi-download"></i></a>`
        }
      </div>
    </div>
  `;
}
function renderSystemsExploreMoreButton() {
  const wrap = document.getElementById("systems-explore-more");
  if (!wrap) return;
  if (systemsExploreDuplicatesMode) {
    wrap.innerHTML = "";  // duplicates are fetched all at once -- no paging
    return;
  }
  const isBios = systemsExploreSelectedSystem === SYSTEMS_EXPLORE_BIOS_KEY;
  const loadedCount = isBios ? systemsExploreBiosItems.length : systemsExploreRoms.length;
  if (!systemsExploreHasMore && !systemsExploreLoadingMore) {
    wrap.innerHTML = loadedCount
      ? `<span class="small text-muted">Showing ${loadedCount.toLocaleString()} of ${systemsExploreTotal.toLocaleString()}</span>`
      : "";
    return;
  }
  wrap.innerHTML = `
    <button type="button" class="btn btn-outline-primary btn-sm" ${systemsExploreLoadingMore ? "disabled" : ""} onclick="loadSystemsExploreCurrentMode({ reset: false })">
      ${systemsExploreLoadingMore ? '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>' : '<i class="bi bi-plus-circle me-1"></i>'}
      Show more (${loadedCount.toLocaleString()} of ${systemsExploreTotal.toLocaleString()})
    </button>
  `;
}
function renderSystemsExploreCard(rom) {
  const title = rom.rom_name || rom.name || rom.rom_file || "";
  const guessedSrc = publicRomImageUrl(rom.system, rom.rom_file || title, rom.image_stem);
  const idSrc = romImageByIdUrl(rom.system, rom.unique_id);
  // Prefer the real gamelist-referenced image (roms/gamelist.py's
  // image_relative_path) over the filename guess below -- Browse skips the
  // live per-row gamelist re-attach the ROM detail page uses (see
  // list_rom_browse_page's docstring), so without this a card only found an
  // image when the actual scraped filename happened to match the ROM's own
  // filename stem, which many scrapers don't follow.
  const primarySrc = rom.image_relative_path ? artworkExistingImageUrl(rom, rom.image_relative_path) : guessedSrc;
  const fallbacks = JSON.stringify(rom.image_relative_path ? [guessedSrc, idSrc] : [idSrc]);
  const navigateHash = romMediaHash(rom.system, rom.unique_id);
  return `
    <button type="button" class="movie-explorer-card" title="${escapeHtml(title)}" onclick="setHash(${jsAttr(navigateHash)})">
      <div class="movie-explorer-card-poster">
        <img src="" data-src="${escapeHtml(primarySrc)}" data-fallbacks='${escapeHtml(fallbacks)}' alt="" loading="lazy">
        <div class="movie-explorer-card-poster-fallback d-none"><i class="bi bi-controller"></i></div>
      </div>
      <div class="movie-explorer-card-title">${escapeHtml(title)}</div>
      <div class="movie-explorer-card-subtitle">${escapeHtml(rom.system || "")}</div>
    </button>
  `;
}
// Plain-language explainers for the security/technology terms referenced on
// the home page, opened by showTechInfo() -- keeps that copy skimmable while
// still letting anyone tap a term for the real explanation + a place to read
// more, instead of writing a full paragraph inline for each one.
const HELP_TECH_GLOSSARY = {
  e2ee: {
    title: "End-to-end encryption",
    icon: "bi-lock-fill",
    body: "When two of your Drones exchange files directly, the connection is encrypted for the whole trip, from one machine straight to the other. Nothing in between -- including any relay hop a transfer might pass through -- can read what's inside; only the two machines involved hold the keys.",
    link: "https://en.wikipedia.org/wiki/End-to-end_encryption",
    linkText: "Read more about end-to-end encryption",
  },
  mtls: {
    title: "Certificate-verified peers",
    icon: "bi-patch-check-fill",
    body: "Before two Drones will talk to each other, each one proves its identity with a certificate -- more like a passport that's hard to fake than a password that can be guessed. Because both sides check the other's certificate, a machine you haven't paired can't pretend to be one of yours, and yours won't be tricked into sending files to an impostor.",
    link: "https://en.wikipedia.org/wiki/Mutual_authentication",
    linkText: "Read more about mutual authentication",
  },
  vpn: {
    title: "VPN support",
    icon: "bi-incognito",
    body: "A VPN routes this machine's traffic through an encrypted tunnel to a provider you choose, so your local network and internet provider can't see what it's doing. Upload your provider's configuration file on the VPN page, add your login, and Drone connects automatically -- reconnecting on its own if the tunnel ever drops.",
    link: "https://en.wikipedia.org/wiki/Virtual_private_network",
    linkText: "Read more about VPNs",
  },
  localcreds: {
    title: "Credentials stay local",
    icon: "bi-key-fill",
    body: "Passwords, VPN logins, and email credentials are stored only on the machine you entered them on -- never uploaded anywhere, and never sent to another Drone without you asking for it. When you do choose to share a VPN or email setup with a paired machine, it travels over the same encrypted, certificate-verified link as everything else.",
    link: "",
    linkText: "",
  },
  bruteforce: {
    title: "Brute-force protection",
    icon: "bi-shield-exclamation",
    body: "Login and admin routes watch for repeated failed attempts. Once too many happen too quickly from one place, further attempts are automatically slowed down or blocked, so guessing a password by brute force isn't practical.",
    link: "https://en.wikipedia.org/wiki/Brute-force_attack",
    linkText: "Read more about brute-force attacks",
  },
  p2p: {
    title: "Peer-to-peer transfers",
    icon: "bi-arrow-left-right",
    body: "Instead of sending files up to a central server and back down again, your machines send content directly to each other over the same encrypted, certificate-verified link used everywhere else in Drone. It's faster, keeps your library from passing through anyone else's server, and only ever works between machines you've explicitly paired.",
    link: "https://en.wikipedia.org/wiki/Peer-to-peer",
    linkText: "Read more about peer-to-peer networking",
  },
  torrent: {
    title: "BitTorrent",
    icon: "bi-magnet",
    body: "BitTorrent downloads a file in small pieces from multiple sources at once instead of one slow direct link, which is usually much faster for large or popular files. Drone runs a small torrent client so you can pull a .torrent file down on one machine, then let the rest of your fleet grab it from that machine peer-to-peer instead of downloading it all over again.",
    link: "https://en.wikipedia.org/wiki/BitTorrent",
    linkText: "Read more about BitTorrent",
  },
  tailnet: {
    title: "Tailnet",
    icon: "bi-globe2",
    body: "A tailnet is a private mesh network -- from a free service called Tailscale -- that lets your own devices reach each other directly no matter which network they're on, without opening any ports or reconfiguring your router. It's what lets a Drone at a friend's house, or your phone out in the world, reach your machines back home.",
    link: "https://tailscale.com/",
    linkText: "Learn more at tailscale.com",
  },
};
function showTechInfo(key) {
  const entry = HELP_TECH_GLOSSARY[key];
  if (!entry) return;
  const modalId = "techInfoModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi ${entry.icon} me-2"></i>${escapeHtml(entry.title)}</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p class="mb-0">${escapeHtml(entry.body)}</p>
        </div>
        <div class="modal-footer">
          ${entry.link ? `<a href="${escapeHtml(entry.link)}" target="_blank" rel="noopener noreferrer" class="btn btn-outline-primary btn-sm me-auto"><i class="bi bi-box-arrow-up-right me-1"></i>${escapeHtml(entry.linkText || "Learn more")}</a>` : ""}
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
        </div>
      </div>
    </div>`;
  const bsModal = window.bootstrap?.Modal ? window.bootstrap.Modal.getOrCreateInstance(modal) : null;
  bsModal?.show();
}

// Generic destructive-action confirmation modal (Bootstrap, not
// window.confirm()) -- title/confirmLabel are plain text (escaped here);
// body is trusted HTML, so callers building it from user data must
// escapeHtml() their own dynamic bits before interpolating.
function openConfirmDeleteModal({ title, body, confirmLabel = "Delete", onConfirm }) {
  const modalId = "confirmDeleteModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-exclamation-triangle text-danger me-2"></i>${escapeHtml(title)}</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body"><p class="mb-0">${body}</p></div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-danger btn-sm" id="confirmDeleteModalConfirmBtn">${escapeHtml(confirmLabel)}</button>
        </div>
      </div>
    </div>`;
  const bsModal = window.bootstrap?.Modal ? window.bootstrap.Modal.getOrCreateInstance(modal) : null;
  modal.querySelector("#confirmDeleteModalConfirmBtn").addEventListener("click", () => {
    bsModal?.hide();
    onConfirm();
  }, { once: true });
  bsModal?.show();
}

// Duplicate-group review/bulk-delete modal, shared by the Systems and
// Movies Browse duplicate finders -- the interaction (a checkable list,
// pre-checked to everything except each group's recommended_keep item, a
// live selected-count, one batch delete) is identical between the two;
// only the item shape and the actual delete call differ, both supplied by
// the caller. `groups` is [{ label, items: [...] }]; `itemLabel`/
// `itemMeta` format one item's title line / size-and-detail subline;
// `deleteFn(selectedItems)` performs the actual batch delete and should
// throw on failure (caught here and surfaced as a toast, modal stays open
// so the user can retry).
let duplicatesReviewRows = [];
function openDuplicatesReviewModal({ title, groups, itemLabel, itemMeta, deleteFn, onDeleted }) {
  duplicatesReviewRows = [];
  groups.forEach((group) => {
    group.items.forEach((item) => {
      duplicatesReviewRows.push({ groupLabel: group.label, item, checked: !item.recommended_keep });
    });
  });
  const modalId = "duplicatesReviewModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    document.body.appendChild(modal);
  }
  const rowsHtml = duplicatesReviewRows.map((row, index) => `
    <div class="form-check d-flex align-items-start gap-2 py-1 border-bottom">
      <input class="form-check-input mt-1 flex-shrink-0" type="checkbox" id="dupRow${index}" data-row-index="${index}" ${row.checked ? "checked" : ""}>
      <label class="form-check-label min-width-0" for="dupRow${index}">
        <div class="text-truncate">${escapeHtml(itemLabel(row.item))}${row.item.recommended_keep ? ' <span class="badge text-bg-success">Keep</span>' : ""}</div>
        <div class="text-muted small text-truncate">${escapeHtml(row.groupLabel)} &middot; ${escapeHtml(itemMeta(row.item))}</div>
      </label>
    </div>
  `).join("");
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-lg">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-exclamation-triangle text-danger me-2"></i>${escapeHtml(title)}</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <p class="text-muted small">Everything except each group's highest-quality/latest-revision copy is checked by default. Review and adjust before deleting -- this cannot be undone.</p>
          <div id="duplicatesReviewList" class="duplicates-review-list">${rowsHtml}</div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-danger btn-sm" id="duplicatesReviewConfirmBtn"><i class="bi bi-trash me-1"></i>Delete Selected (<span id="duplicatesReviewCount">0</span>)</button>
        </div>
      </div>
    </div>`;
  const listEl = modal.querySelector("#duplicatesReviewList");
  const countEl = modal.querySelector("#duplicatesReviewCount");
  const confirmBtn = modal.querySelector("#duplicatesReviewConfirmBtn");
  const updateCount = () => {
    const checked = listEl.querySelectorAll("input[type=checkbox]:checked").length;
    countEl.textContent = String(checked);
    confirmBtn.disabled = checked === 0;
  };
  listEl.addEventListener("change", (event) => {
    const index = Number(event.target?.dataset?.rowIndex);
    if (Number.isFinite(index) && duplicatesReviewRows[index]) duplicatesReviewRows[index].checked = event.target.checked;
    updateCount();
  });
  updateCount();
  const bsModal = window.bootstrap?.Modal ? window.bootstrap.Modal.getOrCreateInstance(modal) : null;
  confirmBtn.addEventListener("click", async () => {
    const selected = duplicatesReviewRows.filter((row) => row.checked).map((row) => row.item);
    if (!selected.length) return;
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>Deleting...';
    try {
      await deleteFn(selected);
      bsModal?.hide();
      showToast(`Deleted ${selected.length} duplicate${selected.length === 1 ? "" : "s"}.`, "success");
      await onDeleted();
    } catch (err) {
      showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
      confirmBtn.disabled = false;
      confirmBtn.innerHTML = `<i class="bi bi-trash me-1"></i>Delete Selected (<span id="duplicatesReviewCount">${selected.length}</span>)`;
    }
  });
  bsModal?.show();
}

// First-time-load onboarding tour: a lightweight, dependency-free
// intro.js-style walkthrough. Spotlights one persistent UI element at a
// time (nav bar, notifications bell, status badges) with a step tooltip.
// Auto-runs once per browser (localStorage-gated) and is always skippable --
// via Close, "Skip tour", Escape, or clicking anywhere outside the tooltip.
const ONBOARDING_TOUR_STORAGE_KEY = "droneOnboardingTourDismissedV1";
const ONBOARDING_TOUR_STEPS = [
  {
    selector: "#notificationsBellWrap",
    title: "Notifications",
    body: "Anything that needs your attention -- finished downloads, device alerts -- shows up here.",
  },
  {
    selector: "#assetsMenuBtn",
    title: "Your library",
    body: "Browse and search Games, Movies, and Music on this machine -- switch between them right from the search bar on each page.",
  },
  {
    selector: "#controlsMenuBtn",
    title: "Controls",
    body: "Screen mode, volume, and EmulationStation settings -- managed remotely from any browser.",
  },
  {
    selector: "#swarmMenuBtn",
    title: "Swarm",
    body: "Pair with your other machines here and manage your whole fleet -- no central server required.",
  },
  {
    selector: "#adminMenuBtn",
    title: "Admin",
    body: "Torrents, VPN, Email, Automation, and diagnostic tools all live here.",
  },
  {
    selector: "#systemInfoBar",
    title: "At a glance",
    body: "Batocera version, machine ID, fleet connections, VPN, and email status. Tap any badge to jump straight to that page.",
  },
  {
    selector: ".help-security-band",
    title: "Security, explained",
    body: "Tap any highlighted term on this page any time to see a plain-language explanation and a place to read more.",
  },
];
let onboardingTourState = null;
function isOnboardingTourTargetVisible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0 && el.offsetParent !== null;
}
function startOnboardingTour() {
  const steps = ONBOARDING_TOUR_STEPS.filter((step) => isOnboardingTourTargetVisible(document.querySelector(step.selector)));
  if (!steps.length) return;
  endOnboardingTour(false);
  onboardingTourState = {steps, index: 0};
  ["tourBackdrop", "tourSpotlight", "tourTooltip"].forEach((id) => {
    const el = document.createElement("div");
    el.id = id;
    el.className = id === "tourBackdrop" ? "tour-backdrop" : id === "tourSpotlight" ? "tour-spotlight" : "tour-tooltip";
    document.body.appendChild(el);
  });
  document.getElementById("tourBackdrop").addEventListener("click", () => endOnboardingTour(true));
  window.addEventListener("resize", onboardingTourReposition);
  window.addEventListener("scroll", onboardingTourReposition, true);
  window.addEventListener("hashchange", onboardingTourHashChange);
  document.addEventListener("keydown", onboardingTourKeydown);
  renderOnboardingTourStep();
}
function endOnboardingTour(markDismissed) {
  if (markDismissed) {
    try { localStorage.setItem(ONBOARDING_TOUR_STORAGE_KEY, "1"); } catch (_) {}
  }
  document.getElementById("tourBackdrop")?.remove();
  document.getElementById("tourSpotlight")?.remove();
  document.getElementById("tourTooltip")?.remove();
  document.querySelectorAll(".tour-target-active").forEach((el) => el.classList.remove("tour-target-active"));
  window.removeEventListener("resize", onboardingTourReposition);
  window.removeEventListener("scroll", onboardingTourReposition, true);
  window.removeEventListener("hashchange", onboardingTourHashChange);
  document.removeEventListener("keydown", onboardingTourKeydown);
  onboardingTourState = null;
}
function onboardingTourHashChange() {
  // Safety net: the backdrop blocks clicks on the app underneath, but the
  // hash can still change from outside the tour (browser back/forward) --
  // don't leave a spotlight pointed at a page that's no longer showing.
  endOnboardingTour(false);
}
function onboardingTourKeydown(event) {
  if (!onboardingTourState) return;
  if (event.key === "Escape") endOnboardingTour(true);
  else if (event.key === "ArrowRight" || event.key === "Enter") onboardingTourNext();
  else if (event.key === "ArrowLeft") onboardingTourBack();
}
function onboardingTourNext() {
  if (!onboardingTourState) return;
  if (onboardingTourState.index >= onboardingTourState.steps.length - 1) {
    endOnboardingTour(true);
    return;
  }
  onboardingTourState.index += 1;
  renderOnboardingTourStep();
}
function onboardingTourBack() {
  if (!onboardingTourState || onboardingTourState.index <= 0) return;
  onboardingTourState.index -= 1;
  renderOnboardingTourStep();
}
function onboardingTourReposition() {
  if (onboardingTourState) renderOnboardingTourStep(true);
}
function renderOnboardingTourStep(isReposition = false) {
  const state = onboardingTourState;
  if (!state) return;
  const step = state.steps[state.index];
  const target = document.querySelector(step.selector);
  if (!isOnboardingTourTargetVisible(target)) {
    if (state.index < state.steps.length - 1) {
      state.index += 1;
      renderOnboardingTourStep(isReposition);
    } else {
      endOnboardingTour(false);
    }
    return;
  }
  document.querySelectorAll(".tour-target-active").forEach((el) => el.classList.remove("tour-target-active"));
  target.classList.add("tour-target-active");
  if (isReposition) {
    positionOnboardingTourUI(target, step, state);
  } else {
    target.scrollIntoView({block: "center"});
    requestAnimationFrame(() => requestAnimationFrame(() => positionOnboardingTourUI(target, step, state)));
  }
}
function positionOnboardingTourUI(target, step, state) {
  if (!onboardingTourState) return;
  const rect = target.getBoundingClientRect();
  const pad = 8;
  const spotlight = document.getElementById("tourSpotlight");
  if (!spotlight) return;
  spotlight.style.top = `${Math.max(0, rect.top - pad)}px`;
  spotlight.style.left = `${Math.max(0, rect.left - pad)}px`;
  spotlight.style.width = `${rect.width + pad * 2}px`;
  spotlight.style.height = `${rect.height + pad * 2}px`;
  spotlight.classList.add("is-visible");

  const tooltip = document.getElementById("tourTooltip");
  if (!tooltip) return;
  const isLast = state.index === state.steps.length - 1;
  tooltip.innerHTML = `
    <div class="tour-tooltip-header">
      <span class="tour-tooltip-step">Step ${state.index + 1} of ${state.steps.length}</span>
      <button type="button" class="tour-tooltip-close" onclick="endOnboardingTour(true)" aria-label="Close tour"><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="tour-tooltip-title">${escapeHtml(step.title)}</div>
    <div class="tour-tooltip-body">${escapeHtml(step.body)}</div>
    <div class="tour-tooltip-footer">
      <button type="button" class="btn btn-link btn-sm p-0 tour-skip-btn" onclick="endOnboardingTour(true)">Skip tour</button>
      <div class="d-flex gap-2">
        ${state.index > 0 ? `<button type="button" class="btn btn-outline-light btn-sm" onclick="onboardingTourBack()">Back</button>` : ""}
        <button type="button" class="btn btn-primary btn-sm" onclick="onboardingTourNext()">${isLast ? "Done" : "Next"}</button>
      </div>
    </div>
  `;
  tooltip.classList.add("is-visible");

  const viewportW = window.innerWidth;
  const viewportH = window.innerHeight;
  const tooltipRect = tooltip.getBoundingClientRect();
  const spaceBelow = viewportH - rect.bottom;
  const spaceAbove = rect.top;
  let top = spaceBelow >= tooltipRect.height + 24 || spaceBelow >= spaceAbove
    ? Math.min(rect.bottom + pad + 12, viewportH - tooltipRect.height - 12)
    : Math.max(12, rect.top - pad - 12 - tooltipRect.height);
  let left = Math.max(12, Math.min(rect.left + rect.width / 2 - tooltipRect.width / 2, viewportW - tooltipRect.width - 12));
  tooltip.style.top = `${Math.max(12, top)}px`;
  tooltip.style.left = `${left}px`;
}
function maybeAutoStartOnboardingTour() {
  let dismissed = false;
  try { dismissed = localStorage.getItem(ONBOARDING_TOUR_STORAGE_KEY) === "1"; } catch (_) { dismissed = true; }
  if (dismissed || onboardingTourState) return;
  // Badges/nav visibility settle asynchronously on boot; give them a moment
  // so the tour doesn't measure an empty #systemInfoBar and skip that step.
  setTimeout(() => {
    if (!onboardingTourState && (window.location.hash === "" || window.location.hash === "#" || window.location.hash === "#home" || window.location.hash === "#help")) {
      startOnboardingTour();
    }
  }, 600);
}
async function renderHelpPage() {
  currentSystemContext = null;
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
          <p class="mb-2 text-muted">Drone runs quietly on this Batocera machine and gives you a management dashboard for everything on it — your library, saves, BIOS, artwork, and live health — from any phone, tablet, or computer on your network. No controller or TV required.</p>
          <p class="mb-3 text-muted">Pair your machines together on the Swarm page and they become a fleet: copy content cabinet-to-cabinet over an encrypted peer-to-peer link, watch every Drone's health from any of them, and — with the free tailnet connection — reach it all from your phone anywhere in the world. No central server, no port forwarding, and your library never has to pass through anyone else's server.</p>
          <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-primary" type="button" onclick="setHash('#systems')"><i class="bi bi-grid me-1"></i>Browse your library</button>
            <button class="btn btn-outline-light" type="button" onclick="setHash('#admin/swarm')"><i class="bi bi-diagram-3 me-1"></i>See your fleet</button>
            <button class="btn btn-outline-light" type="button" onclick="startOnboardingTour()"><i class="bi bi-signpost-2 me-1"></i>Take a quick tour</button>
          </div>
        </div>
      </div>

      <div class="help-security-band mb-4">
        <div class="d-flex align-items-center gap-2 mb-2">
          <i class="bi bi-shield-lock-fill fs-5"></i>
          <h3 class="h5 mb-0">Private and secure by default</h3>
        </div>
        <p class="text-muted mb-3">Security isn't an add-on here — every connection between your machines is protected the same way automatically, and it's worth knowing what's actually doing the protecting. Tap any of these to find out:</p>
        <div class="help-term-row">
          <button class="help-term" type="button" onclick="showTechInfo('e2ee')"><i class="bi bi-lock-fill"></i>End-to-end encryption</button>
          <button class="help-term" type="button" onclick="showTechInfo('mtls')"><i class="bi bi-patch-check-fill"></i>Certificate-verified peers</button>
          <button class="help-term" type="button" onclick="showTechInfo('vpn')"><i class="bi bi-incognito"></i>VPN support</button>
          <button class="help-term" type="button" onclick="showTechInfo('localcreds')"><i class="bi bi-key-fill"></i>Credentials stay local</button>
          <button class="help-term" type="button" onclick="showTechInfo('bruteforce')"><i class="bi bi-shield-exclamation"></i>Brute-force protection</button>
        </div>
      </div>

      <div class="row g-3 mb-4">
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-speedometer2"></i>
            <div>
              <div class="help-metric-title">Management dashboard</div>
              <div class="text-muted small">CPU, memory, storage, and network health at a glance, plus screen mode, volume, and EmulationStation controls — all from a browser.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-diagram-3"></i>
            <div>
              <div class="help-metric-title">Fleet management</div>
              <div class="text-muted small">Pair your machines and every one of them shows the whole fleet's health and content — no central server required.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-arrow-left-right"></i>
            <div>
              <div class="help-metric-title">Secure peer-to-peer sharing</div>
              <div class="text-muted small">Copy games, saves, BIOS, and artwork directly between machines over an <button class="help-term-inline" type="button" onclick="event.stopPropagation(); showTechInfo('p2p')">encrypted peer-to-peer link</button> instead of re-downloading everything.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-robot"></i>
            <div>
              <div class="help-metric-title">Automation &amp; remote control</div>
              <div class="text-muted small">Idle volume, stuck-game exit, and Wi-Fi recovery run on their own, or manage kiosk mode and restarts yourself, remotely.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-magnet"></i>
            <div>
              <div class="help-metric-title">Torrent manager</div>
              <div class="text-muted small">Pull new content in with a built-in <button class="help-term-inline" type="button" onclick="event.stopPropagation(); showTechInfo('torrent')">BitTorrent</button> client, then share it across your fleet peer-to-peer.</div>
            </div>
          </div>
        </div>
        <div class="col-12 col-md-6 col-xl-4">
          <div class="help-metric h-100">
            <i class="bi bi-envelope"></i>
            <div>
              <div class="help-metric-title">Notifications &amp; email</div>
              <div class="text-muted small">Get an email digest of fleet activity and pick exactly which events — downloads, offline devices, and more — are worth an alert.</div>
            </div>
          </div>
        </div>
      </div>

      <div class="help-section mb-4">
        <h3 class="h5 mb-3"><i class="bi bi-lightbulb me-2"></i>Good to know</h3>
        <div class="row g-3">
          <div class="col-12 col-md-6">
            <dl class="help-terms mb-0">
              <dt>Better with more machines</dt>
              <dd class="mb-0">A single Drone is a handy dashboard. A few of them paired on the Swarm page become a fleet you keep in sync from any screen — no central server involved.</dd>
            </dl>
          </div>
          <div class="col-12 col-md-6">
            <dl class="help-terms mb-0">
              <dt>No middleman</dt>
              <dd class="mb-0">There's no cloud account or company server in the loop. Your machines talk directly to each other, and only to devices you've explicitly paired.</dd>
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
        <h3 class="h5 mb-3"><i class="bi bi-globe2 me-2"></i>Reach this Drone from anywhere (<button class="help-term-inline" type="button" onclick="showTechInfo('tailnet')">tailnet</button>)</h3>
        <p class="text-muted">No port forwarding or router changes needed. Connect this Drone to your tailnet once, and it gets a private <code>100.x</code> address that works from any network.</p>
        <ol class="mb-0">
          <li>Open the <strong>Swarm</strong> page and follow the <strong>Tailnet</strong> card: create the free account, paste an auth key, done.</li>
          <li>Install the Tailscale app on your phone and sign in to the same account.</li>
          <li>Open <code>https://&lt;tailnet address&gt;</code> from anywhere — the Swarm page shows each Drone's address on its card.</li>
        </ol>
      </div>
    </div>
  `;
  setLoading(false);
  maybeAutoStartOnboardingTour();
}
async function renderAdminPage() {
  currentSystemContext = null;
  setLoading(true, "Loading admin panel...");
  clearSystemTheme();
  renderAdminMenu();
  setLoading(false);
  refreshRandomThemeLogo().catch(() => {});
}
// Shared tab-bar renderer for any admin panel that folds multiple former
// tiles/routes into one: each underlying page still owns its full
// route/render function independently (avoids refactoring several large,
// established pages to share one container), this just prepends the same
// tab bar to each so they present as one tabbed panel instead of separate
// destinations. `tabs` is `[key, label, icon, hash]` tuples.
function renderAdminPanelTabs(active, tabs) {
  return `<ul class="nav nav-tabs admin-panel-tabs mb-3">
    ${tabs.map(([key, label, icon, hash]) => `
      <li class="nav-item">
        <button type="button" class="nav-link ${active === key ? "active" : ""}" onclick="setHash('${hash}')"><i class="bi ${icon} me-1"></i>${label}</button>
      </li>
    `).join("")}
  </ul>`;
}

function renderDebugTabBar(active) {
  return renderAdminPanelTabs(active, [
    ["system-info", "System Info", "bi-pc-display", "#admin/system-info"],
    ["logs", "System Logs", "bi-journal-text", "#admin/logs/es_launch_stdout?lines=200"],
    ["emulators", "Emulators", "bi-file-earmark-code", "#admin/emulators"],
  ]);
}

function renderArtworkTabBar(active) {
  return renderAdminPanelTabs(active, [
    ["metadata", "Artwork & Metadata", "bi-images", "#admin/artwork"],
    ["theme", "Theme Gallery", "bi-brush", "#theme"],
    ["movies", "Movies", "bi-film", "#admin/movies"],
    ["music", "Music", "bi-music-note-beamed", "#admin/music"],
  ]);
}

function renderSwarmTabBar(active) {
  return renderAdminPanelTabs(active, [
    ["swarm", "Swarm", "bi-diagram-3", "#admin/swarm"],
    ["transfers", "Transfers", "bi-arrow-left-right", "#admin/transfers"],
  ]);
}

async function renderAdminMenu() {
  titleNode.textContent = "Admin Panel";
  subtitleNode.textContent = "System administration";
  content.innerHTML = `
    <div class="row">
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/system-info')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-bug me-2"></i>Debug</h5>
            <p class="card-text">System info, logs, and emulator config files -- runtime health, storage, network, Batocera details, and every tracked log source in one tabbed panel.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <!-- Lands on Theme Gallery (#theme, a small paginated image list), not
             Artwork & Metadata (#admin/artwork) -- that tab's gamelist scan can
             take several seconds on a large ROM library, so making it the
             *default* meant every visit to this tile paid that cost even for
             someone who only wanted Movies or Theme Gallery. Same reasoning
             already applied to the root "" hash defaulting to #movies instead
             of a gamelist-scan-involving page -- see router()'s own comment. -->
        <div class="card admin-tile pointer h-100" onclick="setHash('#theme')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-images me-2"></i>Artwork</h5>
            <p class="card-text">Manage gamelist artwork, metadata, imports, uploads, marquee crops, browse installed EmulationStation themes, and scrape movie/TV posters and metadata from TMDb.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/torrents')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-magnet me-2"></i>Torrents</h5>
            <p class="card-text">Watch a folder for .torrent files and download them on this machine with aria2c.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/vpn')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-shield-lock me-2"></i>VPN</h5>
            <p class="card-text">Configure and connect an OpenVPN provider (Proton VPN, NordVPN, and others).</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/smtp')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-envelope me-2"></i>Email</h5>
            <p class="card-text">Configure SMTP, share credentials with the swarm, and choose which activity gets emailed as a digest.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/config-backups')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-archive me-2"></i>Backups</h5>
            <p class="card-text">Bundle Batocera + emulator settings, gamelist.xml, saves, and custom scripts into a downloadable archive.</p>
          </div>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card admin-tile pointer h-100" onclick="setHash('#admin/automation')">
          <div class="card-body">
            <h5 class="card-title"><i class="bi bi-robot me-2"></i>Automation</h5>
            <p class="card-text">Hands-off behaviors for this device: idle volume, exiting a stuck game, and Wi-Fi recovery.</p>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function monitorDroneUpdateWorker() {
  let observedActiveWorker = false;
  for (let attempt = 0; attempt < 180; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
    let job;
    try {
      job = await api("/admin/system/update-drone");
    } catch (error) {
      if (observedActiveWorker) {
        showToast("The Drone API is restarting to load the update. Reloading shortly...", "success", 8000);
        window.setTimeout(() => window.location.reload(), 5000);
        return;
      }
      showToast(`Could not read backend update status: ${escapeHtml(error.message || "unknown error")}`, "warning", 10000);
      return;
    }
    const status = String(job.status || "idle");
    if (["queued", "checking", "downloading"].includes(status)) {
      observedActiveWorker = true;
      continue;
    }
    if (status === "restart_scheduled") {
      showToast("Web/API code, images, and the Ports client are installed. The Drone API is restarting...", "success", 10000);
      window.setTimeout(() => window.location.reload(), 5000);
      return;
    }
    if (status === "current") {
      showToast("The backend worker confirmed that this Drone is already current.", "success", 7000);
      return;
    }
    if (status === "error") {
      showToast(`Backend update failed: ${escapeHtml(job.detail || job.error || "unknown error")}`, "danger", 12000);
      return;
    }
    showToast(`Backend update check finished: ${escapeHtml(job.detail || status)}.`, "info", 8000);
    return;
  }
  showToast("The backend update is still running. It will continue even if this browser closes.", "info", 10000);
}

async function updateDroneApp() {
  if (!window.confirm("Ask the Drone API worker to check for and install the latest complete release? It will update web/API code, images, and the Ports client, then restart only the Drone service.")) return;
  const toast = showToast('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Submitting update check to the Drone API worker...', "info", null);
  try {
    const payload = await apiPost("/admin/system/update-drone", {});
    dismissToast(toast);
    showToast(
      payload.already_running
        ? "The Drone API worker is already checking for an update. This page is only displaying its status."
        : "Update check accepted. The Drone API worker owns the download and install; it continues if this browser closes.",
      "info",
      10000,
    );
    void monitorDroneUpdateWorker();
  } catch (error) {
    dismissToast(toast);
    showToast(`Could not submit the backend update check: ${escapeHtml(error.message || "unknown error")}.`, "warning", 12000);
  }
}

async function setDroneAutoUpdate(checkbox) {
  const requested = checkbox.checked;
  checkbox.disabled = true;
  try {
    const result = await apiPost("/admin/system/auto-update", {enabled: requested});
    checkbox.checked = result.enabled === true;
    showToast(`Automatic Drone updates ${checkbox.checked ? "enabled" : "disabled"}.`, "success");
  } catch (error) {
    checkbox.checked = !requested;
    showToast(`Could not save automatic update setting: ${escapeHtml(error.message || "unknown error")}`, "danger");
  } finally {
    if (checkbox.isConnected) checkbox.disabled = false;
  }
}

async function restartEmulationStation() {
  if (!window.confirm("Restart EmulationStation on this Drone? Any game currently running will be interrupted.")) return;
  const btn = document.getElementById("restartEsBtn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Restarting...';
  }
  try {
    await apiPost("/admin/system/restart-emulationstation", {});
    showToast("EmulationStation restarted.", "success");
  } catch (error) {
    showToast(`EmulationStation restart failed: ${escapeHtml(error.message || "unknown error")}`, "danger", 10000);
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>Restart EmulationStation';
    }
  }
}

async function runPixnUpdate() {
  if (!window.confirm("Run the PixN upgrade script on this Drone?")) return;
  const toast = showToast('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Starting PixN update...', "info", null);
  try {
    const payload = await apiPost("/admin/system/run-pixn-update", {});
    dismissToast(toast);
    showToast(`PixN update started${payload.pid ? ` (pid ${payload.pid})` : ""}.`, "success", 8000);
  } catch (error) {
    dismissToast(toast);
    showToast(`PixN update could not start: ${escapeHtml(error.message || "unknown error")}`, "danger", 10000);
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

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) return `${hours}h ${remainingMinutes}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function renderQueueEta(payload) {
  const pendingCount = (payload.active || []).length + (payload.queued || []).length;
  if (!pendingCount) return "";
  const etaSeconds = payload.queue_eta_seconds == null ? Number.NaN : Number(payload.queue_eta_seconds);
  const remaining = payload.queue_size_estimate_available === false ? "Remaining size is still being discovered" : `${formatBytes(payload.queue_remaining_bytes)} remains`;
  const unknownCount = Number(payload.queue_unknown_size_count) || 0;
  const speed = Number(payload.queue_estimate_speed_bps) || 0;
  const unknownNote = unknownCount ? ` Includes estimated sizes for ${unknownCount} file${unknownCount === 1 ? "" : "s"}.` : "";
  if (payload.queue_eta_state === "paused") {
    return `<div class="alert alert-warning py-2 mb-3"><strong>Queue paused.</strong> ${remaining}.${unknownNote}</div>`;
  }
  if (!Number.isFinite(etaSeconds) || etaSeconds < 0 || !speed) {
    return `<div class="alert alert-info py-2 mb-3"><strong>Queue ETA:</strong> Calculating after transfer speed and file sizes are available. ${remaining}.${unknownNote}</div>`;
  }
  const completion = formatCompactLocalDate(new Date(Date.now() + etaSeconds * 1000).toISOString());
  return `<div class="alert alert-info py-2 mb-3"><strong>Queue ETA:</strong> ${formatDuration(etaSeconds)} remaining, approximately ${escapeHtml(completion)} at ${formatBytes(speed)}/s. ${remaining}.${unknownNote}</div>`;
}

function renderDownloadRows(rows, allowCancel = true, options = {}) {
  if (!rows.length) return `<div class="themed-empty">${escapeHtml(options.emptyText || "No downloads in this group.")}</div>`;
  const includeStarted = options.includeStarted !== false;
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table download-table bff-stack">
    <thead><tr><th>Status</th><th>Source</th><th>File</th><th>System</th><th>Progress</th><th>Speed</th>${includeStarted ? "<th>Started</th>" : ""}<th class="download-actions">Actions</th></tr></thead>
    <tbody>${rows.map(row => {
      const pct = Number(row.percentage || 0);
      const status = String(row.status || "");
      const cancelable = ["queued", "downloading", "pending", "paused"].includes(status);
      const pausable = ["queued", "pending", "downloading"].includes(status);
      const resumable = status === "paused";
      const retryable = ["failed", "cancelled"].includes(status);
      const statusClass = status === "failed" ? "danger" : status === "completed" ? "success" : status === "cancelled" ? "secondary" : status === "downloading" ? "info" : status === "paused" ? "warning" : status === "pending" ? "dark" : "primary";
      const displayStatus = status || "queued";
      const filePath = row.file_path || row.relative_path || row.rom_name || "";
      const errorText = row.error_message || row.failure_reason || "";
      const jobId = escapeHtml(row.job_id || row.id || "");
      // The artwork file can copy successfully while linking it into gamelist.xml
      // fails (e.g. a root-owned, non-writable gamelist). Surface that instead of
      // letting it look like a clean success.
      const gamelistFailed = row.gamelist_update_status === "failed";
      const gamelistError = (row.gamelist_update && row.gamelist_update.error) ? String(row.gamelist_update.error) : "gamelist.xml was not updated";
      const gamelistWarning = gamelistFailed
        ? ` <span class="badge text-bg-warning" title="${escapeHtml(`Artwork copied but not linked: ${gamelistError}`)}"><i class="bi bi-exclamation-triangle me-1"></i>gamelist not linked</span>`
        : "";
      const actions = [
        allowCancel && cancelable && jobId ? `<button class="btn btn-sm btn-outline-danger" title="Cancel download" aria-label="Cancel download" onclick="cancelDroneDownload('${jobId}')"><i class="bi bi-x-circle"></i></button>` : "",
        pausable && jobId ? `<button class="btn btn-sm btn-outline-warning" title="Pause download" aria-label="Pause download" onclick="pauseDroneDownload('${jobId}')"><i class="bi bi-pause-fill"></i></button>` : "",
        resumable && jobId ? `<button class="btn btn-sm btn-outline-success" title="Resume download" aria-label="Resume download" onclick="resumeDroneDownload('${jobId}')"><i class="bi bi-play-fill"></i></button>` : "",
        retryable && jobId ? `<button class="btn btn-sm btn-outline-primary" title="Retry download" aria-label="Retry download" onclick="retryDroneDownload('${jobId}')"><i class="bi bi-arrow-clockwise"></i></button>` : "",
      ].filter(Boolean).join(" ");
      return `<tr>
        <td><span class="badge text-bg-${statusClass}" title="${escapeHtml(errorText)}">${escapeHtml(displayStatus)}${row.queue_position ? ` #${row.queue_position}` : ""}</span>${gamelistWarning}</td>
        <td class="small mono">${escapeHtml(row.source_drone_id || "n/a")}</td>
        <td class="small mono download-file" title="${escapeHtml(errorText || row.rom_fingerprint || "")}">${escapeHtml(filePath)}</td>
        <td class="small">${escapeHtml(row.system || "")}</td>
        <td class="small text-nowrap">${pct.toFixed(1)}% (${formatBytes(row.downloaded_bytes || row.bytes_transferred)} / ${formatBytes(row.total_bytes || row.file_size)})</td>
        <td class="small">${row.transfer_speed_bps ? `${formatBytes(row.transfer_speed_bps)}/s` : ""}</td>
        ${includeStarted ? `<td class="small text-nowrap">${escapeHtml(formatCompactLocalDate(row.started_at || row.download_started_at || row.created_at))}</td>` : ""}
        <td class="download-actions">${actions}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

function renderTransferRows(rows, options = {}) {
  if (!rows.length) return `<div class="themed-empty">${escapeHtml(options.emptyText || "No transfers in this group.")}</div>`;
  const showActions = options.showActions !== false;
  const assetTableText = options.assetTableText === true;
  const tableClass = assetTableText ? "download-table local-assets-table" : "download-table";
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table ${tableClass} bff-stack">
    <thead><tr><th></th><th>Status</th><th>Peer</th><th>File</th><th>System</th><th>Progress</th><th>Speed</th>${showActions ? '<th class="download-actions">Actions</th>' : ""}</tr></thead>
    <tbody>${rows.map(row => {
      const isUpload = row._direction === "upload";
      const pct = Number(row.percentage || 0);
      const status = String(row.status || (isUpload ? "uploading" : "queued"));
      const statusClass = status === "failed" ? "danger" : status === "completed" ? "success" : status === "cancelled" ? "secondary" : status === "downloading" ? "info" : status === "paused" ? "warning" : status === "pending" ? "dark" : "primary";
      const filePath = row.file_path || row.relative_path || row.rom_name || row.file_name || "";
      const errorText = row.error_message || row.failure_reason || "";
      const peerLabel = isUpload ? (row.peer_device_id || "unknown peer") : (row.source_drone_id || "n/a");
      const directionIcon = isUpload
        ? `<i class="bi bi-cloud-arrow-up text-info" title="Upload -- serving to a peer"></i>`
        : `<i class="bi bi-cloud-arrow-down text-primary" title="Download -- pulling from a peer"></i>`;
      const progressText = (row.total_bytes || row.file_size)
        ? `${pct.toFixed(1)}% (${formatBytes(row.downloaded_bytes || row.bytes_transferred)} / ${formatBytes(row.total_bytes || row.file_size)})`
        : formatBytes(row.downloaded_bytes || row.bytes_transferred);
      // The artwork file can copy successfully while linking it into gamelist.xml
      // fails (e.g. a root-owned, non-writable gamelist). Surface that instead of
      // letting it look like a clean success.
      const gamelistFailed = row.gamelist_update_status === "failed";
      const gamelistError = (row.gamelist_update && row.gamelist_update.error) ? String(row.gamelist_update.error) : "gamelist.xml was not updated";
      const gamelistWarning = gamelistFailed
        ? ` <span class="badge text-bg-warning" title="${escapeHtml(`Artwork copied but not linked: ${gamelistError}`)}"><i class="bi bi-exclamation-triangle me-1"></i>gamelist not linked</span>`
        : "";
      let actions = "";
      if (showActions && !isUpload) {
        const jobId = escapeHtml(row.job_id || row.id || "");
        const cancelable = ["queued", "downloading", "pending", "paused"].includes(status);
        const pausable = ["queued", "pending", "downloading"].includes(status);
        const resumable = status === "paused";
        const retryable = ["failed", "cancelled"].includes(status);
        actions = [
          cancelable && jobId ? `<button class="btn btn-sm btn-outline-danger" title="Cancel download" aria-label="Cancel download" onclick="cancelDroneDownload('${jobId}')"><i class="bi bi-x-circle"></i></button>` : "",
          pausable && jobId ? `<button class="btn btn-sm btn-outline-warning" title="Pause download" aria-label="Pause download" onclick="pauseDroneDownload('${jobId}')"><i class="bi bi-pause-fill"></i></button>` : "",
          resumable && jobId ? `<button class="btn btn-sm btn-outline-success" title="Resume download" aria-label="Resume download" onclick="resumeDroneDownload('${jobId}')"><i class="bi bi-play-fill"></i></button>` : "",
          retryable && jobId ? `<button class="btn btn-sm btn-outline-primary" title="Retry download" aria-label="Retry download" onclick="retryDroneDownload('${jobId}')"><i class="bi bi-arrow-clockwise"></i></button>` : "",
        ].filter(Boolean).join(" ");
      }
      return `<tr>
        <td>${directionIcon}</td>
        <td><span class="badge text-bg-${statusClass}" title="${escapeHtml(errorText)}">${escapeHtml(status)}${row.queue_position ? ` #${row.queue_position}` : ""}</span>${gamelistWarning}</td>
        <td class="small mono">${escapeHtml(peerLabel)}</td>
        ${assetTableText
          ? `<td title="${escapeHtml(errorText || row.rom_fingerprint || "")}"><strong>${escapeHtml(filePath)}</strong></td>
        <td>${escapeHtml(row.system || "")}</td>
        <td class="text-nowrap">${progressText}</td>
        <td>${row.transfer_speed_bps ? `${formatBytes(row.transfer_speed_bps)}/s` : ""}</td>`
          : `<td class="small mono download-file" title="${escapeHtml(errorText || row.rom_fingerprint || "")}">${escapeHtml(filePath)}</td>
        <td class="small">${escapeHtml(row.system || "")}</td>
        <td class="small text-nowrap">${progressText}</td>
        <td class="small">${row.transfer_speed_bps ? `${formatBytes(row.transfer_speed_bps)}/s` : ""}</td>`}
        ${showActions ? `<td class="download-actions">${actions}</td>` : ""}
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
      <div><strong>${escapeHtml(payload.target_drone_id || "This Drone")}</strong><div class="small text-muted">${(() => { const n = Number(payload.concurrency && payload.concurrency.active_limit) || 1; return n > 1 ? `Up to ${n} transfers run at a time on this Drone.` : "Transfers run one at a time on this Drone."; })()}</div></div>
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
    ${renderQueueEta(payload)}
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

let transferPayload = {};
let uploadPayload = {};
const transferViews = {
  active: { query: "", limit: 10, page: 1 },
  recent: { query: "", limit: 10, page: 1 },
};

function transferPage(kind, rows) {
  const view = transferViews[kind];
  const query = view.query.trim().toLowerCase();
  const filtered = query ? rows.filter(row => JSON.stringify(row).toLowerCase().includes(query)) : rows;
  const pages = Math.max(1, Math.ceil(filtered.length / view.limit));
  view.page = Math.max(1, Math.min(view.page, pages));
  const start = (view.page - 1) * view.limit;
  return { rows: filtered.slice(start, start + view.limit), total: filtered.length, pages };
}

function renderTransferPager(kind, label, rows) {
  const page = transferPage(kind, rows);
  const view = transferViews[kind];
  return { page, html: `<div class="d-flex flex-wrap gap-2 mb-2">
      <input class="form-control form-control-sm" style="max-width:260px" placeholder="Search ${label.toLowerCase()}" value="${escapeHtml(view.query)}" onchange="setTransferSearch('${kind}', this.value)" onkeydown="if(event.key==='Enter'){event.preventDefault();setTransferSearch('${kind}',this.value)}">
      <select class="form-select form-select-sm" style="width:auto" onchange="setTransferLimit('${kind}', this.value)">${[10, 50, 100, 200].map(size => `<option value="${size}" ${view.limit === size ? "selected" : ""}>${size}</option>`).join("")}</select>
      <button class="btn btn-sm btn-outline-secondary" ${view.page <= 1 ? "disabled" : ""} onclick="setTransferPage('${kind}', ${view.page - 1})">Previous</button>
      <span class="small text-muted align-self-center">Page ${view.page} of ${page.pages}</span>
      <button class="btn btn-sm btn-outline-secondary" ${view.page >= page.pages ? "disabled" : ""} onclick="setTransferPage('${kind}', ${view.page + 1})">Next</button>
    </div>` };
}

function renderTransferControls(payload, active, queued) {
  return `<div class="d-flex flex-wrap align-items-center gap-2 mb-3">
    ${payload.paused
      ? `<span class="badge text-bg-warning"><i class="bi bi-pause-circle me-1"></i>Queue paused</span><button class="btn btn-sm btn-success" type="button" onclick="resumeDroneDownloads()"><i class="bi bi-play-fill me-1"></i>Resume</button>`
      : `<button class="btn btn-sm btn-outline-warning" type="button" ${(active.length || queued.length) ? "" : "disabled"} onclick="pauseDroneDownloads()"><i class="bi bi-pause-fill me-1"></i>Pause</button>`}
    <button class="btn btn-sm btn-outline-danger" type="button" ${queued.length ? "" : "disabled"} onclick="clearDroneDownloads()"><i class="bi bi-x-circle me-1"></i>Clear Queue</button>
    <span class="small text-muted ms-auto">${Number((payload.concurrency && payload.concurrency.active_limit) || 1) > 1 ? `Up to ${Number(payload.concurrency.active_limit)} at a time` : "One at a time"}</span>
  </div>`;
}

function renderTransfersPanel(payload, uploads) {
  transferPayload = payload || {};
  uploadPayload = uploads || {};
  const active = transferPayload.active || [];
  const queued = transferPayload.queued || [];
  const recent = transferPayload.recent || [];
  const uploadActive = uploadPayload.active || [];
  const uploadRecent = uploadPayload.recent || [];
  // Downloads and uploads are consolidated into one set of tables (tagged with
  // a direction icon per row) instead of two separate cards/sections, so both
  // directions of transfer are visible together without doubling the chrome.
  const current = [
    ...active.map(row => ({ ...row, _direction: "download" })),
    ...queued.map(row => ({ ...row, _direction: "download" })),
    ...uploadActive.map(row => ({ ...row, _direction: "upload" })),
  ];
  const allRecent = [
    ...recent.map(row => ({ ...row, _direction: "download" })),
    ...uploadRecent.map(row => ({ ...row, _direction: "upload" })),
  ];
  const currentPager = renderTransferPager("active", "Transfers", current);
  const recentPager = renderTransferPager("recent", "Recent", allRecent);
  return `${renderQueueEta(transferPayload)}${renderTransferControls(transferPayload, active, queued)}
    ${currentPager.html}
    ${renderTransferRows(currentPager.page.rows, { emptyText: "No pending, downloading, or uploading transfers." })}
    <div class="download-section mt-3 mb-0">
      <div class="download-section-title"><span><i class="bi bi-clock-history me-2"></i>Recent</span><span class="badge text-bg-secondary">${recentPager.page.total}</span></div>
      ${recentPager.html}
      ${renderTransferRows(recentPager.page.rows, { showActions: false, assetTableText: true })}
    </div>`;
}

function refreshTransfersPanel() {
  const node = document.getElementById("transfersBody");
  if (node) node.innerHTML = renderTransfersPanel(transferPayload, uploadPayload);
}
function setTransferSearch(kind, value) { transferViews[kind].query = value; transferViews[kind].page = 1; refreshTransfersPanel(); }
function setTransferLimit(kind, value) { transferViews[kind].limit = Number(value) || 10; transferViews[kind].page = 1; refreshTransfersPanel(); }
function setTransferPage(kind, value) { transferViews[kind].page = Number(value) || 1; refreshTransfersPanel(); }

async function renderDownloadsPage() {
  currentSystemContext = null;
  setLoading(true, "Loading downloads...");
  clearSystemTheme();
  titleNode.textContent = "Downloads";
  subtitleNode.textContent = "One active transfer at a time on this Drone";
  try {
    const payload = await api("/admin/downloads");
    content.innerHTML = `
      <div class="mb-3"><button class="btn btn-outline-secondary" onclick="setHash('#admin/transfers')">Back to Transfers</button></div>
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
  await refreshDownloadsView();
}

async function retryDroneDownload(jobId) {
  if (!jobId) return;
  await apiPost(`/admin/downloads/${encodeURIComponent(jobId)}/retry`, {});
  await refreshDownloadsView();
}

async function pauseDroneDownload(jobId) {
  if (!jobId) return;
  await apiPost(`/admin/downloads/${encodeURIComponent(jobId)}/pause`, {});
  await refreshDownloadsView();
}

async function resumeDroneDownload(jobId) {
  if (!jobId) return;
  await apiPost(`/admin/downloads/${encodeURIComponent(jobId)}/resume`, {});
  await refreshDownloadsView();
}

async function refreshDownloadsView() {
  if (window.location.hash === "#admin/transfers" && typeof window.refreshTransfers === "function") {
    await window.refreshTransfers();
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

// -------------------------------------------------------------- Torrents

function torrentStatusBadge(row) {
  const status = String(row.status || "queued");
  const cls = status === "error" ? "danger" : status === "complete" ? "success" : status === "downloading" ? "info" : "primary";
  const seedNote = status === "complete" && row.seeding ? ' <span class="badge text-bg-secondary">seeding</span>' : "";
  return `<span class="badge text-bg-${cls}" title="${escapeHtml(row.message || "")}">${escapeHtml(status)}</span>${seedNote}`;
}

// "Move Downloaded Files" now runs as a background job (see move_files() /
// _move_tick() in torrent_manager.py) tracked entirely through this same 3s
// snapshot poll -- no separate polling loop needed. A compact badge next to
// the status badge is enough; a queued/moving job also disables the row's
// own Move-files button (see canMoveFiles below) so a second click can't
// pile another job on top mid-run.
function torrentMoveJobBadge(row) {
  const job = row.move_job;
  if (!job) return "";
  if (job.status === "queued" || job.status === "moving") {
    const label = `Moving ${Number(job.moved_count) || 0}/${Number(job.total_files) || 0}`;
    const dest = escapeHtml(job.destination || "");
    return ` <span class="badge text-bg-info" title="${dest ? `To ${dest}` : ""}"><span class="spinner-border spinner-border-sm me-1" style="width:0.6rem;height:0.6rem;" role="status" aria-hidden="true"></span>${escapeHtml(label)}</span>`;
  }
  if (job.status === "failed") {
    return ' <span class="badge text-bg-danger" title="See the notifications bell for details">Move failed</span>';
  }
  return "";
}

function torrentMigrationBadge(row) {
  const job = row.migration_job;
  if (!job) return "";
  if (["stopping", "queued", "moving"].includes(job.status)) {
    return ` <span class="badge text-bg-info" title="To ${escapeHtml(job.destination || "")}"><span class="spinner-border spinner-border-sm me-1" style="width:0.6rem;height:0.6rem" role="status" aria-hidden="true"></span>Migrating ${Number(job.moved_count) || 0}/${Number(job.total_files) || 0}</span>`;
  }
  if (job.status === "failed") {
    return ` <span class="badge text-bg-danger" title="${escapeHtml(job.error || "Migration failed")}">Migration failed</span>`;
  }
  return "";
}

// Each row always renders all action slots in the same fixed order,
// hiding inapplicable ones with
// `invisible` (visibility:hidden, not display:none) rather than omitting them
// from the markup. This keeps every button in the exact same physical
// position on every row regardless of status -- the previous
// `.filter(Boolean)` approach collapsed the actions cell to only the
// applicable buttons, so e.g. Delete would sit in a different spot on a
// "downloading" row (2 buttons) vs. a "queued" row (3 buttons), which is
// exactly what made it easy to misclick during the 3s auto-refresh.
function renderTorrentRowMarkup(row) {
  const id = escapeHtml(row.id || "");
  const status = String(row.status || "queued");
  const pct = Number(row.progress_percent || 0);
  let canForceStart = ["queued", "error"].includes(status);
  // "Cancel" now covers two distinct outcomes depending on status: a
  // downloading or errored torrent gets stopped/retried and sent to the back
  // of the queue (see torrent_manager.py's cancel(), which requeues rather
  // than erroring), while a completed-but-still-seeding torrent just stops
  // seeding -- label/icon reflect whichever applies. A torrent already
  // sitting in "queued" has nothing to send to the queue, so it's excluded.
  const canRequeue = ["downloading", "error"].includes(status);
  const canStopSeeding = status === "complete" && row.seeding;
  const canCancel = canRequeue || canStopSeeding;
  const cancelTitle = canStopSeeding ? "Stop seeding" : "Send to queue";
  const cancelIcon = canStopSeeding ? "bi-stop-circle" : "bi-hourglass-split";
  const moveJob = row.move_job;
  const moveJobActive = !!moveJob && (moveJob.status === "queued" || moveJob.status === "moving");
  const migrationJob = row.migration_job;
  const migrationActive = !!migrationJob && ["stopping", "queued", "moving"].includes(migrationJob.status);
  const migrationFailed = !!migrationJob && migrationJob.status === "failed";
  canForceStart = canForceStart && !migrationActive && !migrationFailed;
  const currentDownloadDir = String((torrentsLastPayload || {}).effective_download_directory || "");
  const canMigrate = migrationFailed || (!migrationActive && status !== "complete" && Number(row.completed_bytes || 0) > 0 && String(row.download_dir || "") !== currentDownloadDir);
  const migrateTitle = migrationFailed ? "Retry migration" : migrationActive ? "Migration in progress" : "Move partial download to current Download location";
  const canMoveFiles = status === "complete" && !moveJobActive;
  const moveFilesTitle = moveJobActive ? "Move in progress" : "Move files";
  const progressText = row.total_bytes
    ? `${pct.toFixed(1)}% (${formatBytes(row.completed_bytes)} / ${formatBytes(row.total_bytes)})`
    : (status === "complete" ? "100%" : "0%");
  // The bar's own overlaid label is deliberately more compact than
  // progressText (percentage + total size, not completed/total) -- the
  // column is narrow and this is a glanceable summary; the full
  // completed/total breakdown is still one hover away via the cell's title.
  const progressLabel = row.total_bytes ? `${pct.toFixed(1)}% · ${formatBytes(row.total_bytes)}` : progressText;
  const etaSeconds = Number(row.eta_seconds);
  const etaText = status === "downloading" ? (Number.isFinite(etaSeconds) && etaSeconds > 0 ? formatDuration(etaSeconds) : "--") : "";
  // Every row always shows all 4 action buttons in the same fixed order --
  // ones that don't apply to the current status are grayed out (native
  // `disabled`) rather than hidden, so the row's available actions are
  // visible at a glance instead of guessed from an empty slot, while still
  // being un-clickable (this is what originally fixed the misclick-during-
  // refresh bug; disabled preserves that, invisible was just one way to do it).
  const actionSlot = (enabled, cls, title, onclick, icon) =>
    `<button class="btn btn-sm ${cls}" title="${title}" aria-label="${title}" ${enabled ? "" : "disabled"} onclick="${enabled ? onclick : ""}"><i class="bi ${icon}"></i></button>`;
  const actions = [
    actionSlot(canForceStart, "btn-outline-success", "Force start", `forceStartTorrent('${id}')`, "bi-lightning-charge"),
    actionSlot(canCancel, "btn-outline-warning", cancelTitle, `cancelTorrent('${id}')`, cancelIcon),
    actionSlot(canMigrate, "btn-outline-primary", migrateTitle, `migratePartialTorrent('${id}', ${migrationFailed ? "true" : "false"})`, "bi-device-ssd"),
    actionSlot(canMoveFiles, "btn-outline-info", moveFilesTitle, `openMoveFilesModal('${id}')`, "bi-folder-symlink"),
    actionSlot(!migrationActive, "btn-outline-danger", migrationActive ? "Migration in progress" : "Delete torrent", `deleteTorrent('${id}')`, "bi-trash"),
  ].join(" ");
  return `<tr>
    <td class="download-file" title="${escapeHtml(row.torrent_file || "")}">${escapeHtml(row.name || "")}</td>
    <td>${torrentStatusBadge(row)}${torrentMoveJobBadge(row)}${torrentMigrationBadge(row)}</td>
    <td class="torrent-progress-cell" title="${escapeHtml(progressText)}">
      <div class="torrent-progress-wrap">
        <div class="progress"><div class="progress-bar torrent-progress-bar-${escapeHtml(status)}" style="width:${pct}%"></div></div>
        <span class="torrent-progress-label">${escapeHtml(progressLabel)}</span>
      </div>
    </td>
    <td class="small">${row.download_speed_bps ? `${formatBytes(row.download_speed_bps)}/s` : ""}</td>
    <td class="small">${Number(row.num_seeders || 0)}</td>
    <td class="small">${Number(row.connections || 0)}</td>
    <td class="small">${etaText}</td>
    <td class="download-actions">${actions}</td>
  </tr>`;
}

function renderTorrentTableBody(rows) {
  if (!rows.length) {
    return `<tr><td colspan="8" class="text-center text-muted small py-3">No torrents yet. Drop .torrent files into the watched folder or use Upload Torrents above -- they start automatically.</td></tr>`;
  }
  return rows.map(renderTorrentRowMarkup).join("");
}

// The table/thead is mounted once (renderTorrentsLive) and never replaced;
// only <tbody id="torrentsTableBody"> is patched by patchTorrentsLive, so the
// 3s auto-refresh never flashes the grid.
//
// `torrents-table` (on top of the shared `download-table`/`local-assets-table`
// classes also used by the Transfers and local-peer-browsing tables) carries
// its own `table-layout: fixed` + explicit <colgroup> widths, scoped so it
// doesn't change those other tables' layout. Fixed column widths, combined
// with every cell truncating instead of wrapping (see drone.css), are what
// stop the grid from resizing itself on every 3s poll -- a speed number
// gaining digits, etc. would otherwise reflow every column's auto-fit width
// out from under whatever the user was about to click. The Progress column's
// own label is an absolutely-positioned overlay on top of the (fixed-width)
// bar rather than normal inline text for the same reason -- its length
// growing from "0%" to "45.2% · 4.5 GB" never affects the bar's box size.
function renderTorrentTableShell(rows) {
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table download-table local-assets-table bff-stack torrents-table">
    <colgroup>
      <col style="width:26%"><col style="width:8%"><col style="width:22%"><col style="width:8%">
      <col style="width:5%"><col style="width:5%"><col style="width:8%"><col style="width:18%">
    </colgroup>
    <thead><tr><th>Torrent</th><th>Status</th><th>Progress</th><th>Speed</th><th>SD</th><th>CN</th><th>ETA</th><th class="download-actions">Actions</th></tr></thead>
    <tbody id="torrentsTableBody">${renderTorrentTableBody(rows)}</tbody>
  </table></div>`;
}

function renderTorrentDaemonNote(payload) {
  const aria2 = payload.aria2 || {};
  const dir = (payload.settings || {}).directory || "";
  const daemonBadge = aria2.installed
    ? (aria2.running
      ? '<span class="badge text-bg-success"><i class="bi bi-magnet me-1"></i>aria2c running</span>'
      : '<span class="badge text-bg-secondary"><i class="bi bi-magnet me-1"></i>aria2c idle</span>')
    : '<span class="badge text-bg-warning"><i class="bi bi-exclamation-triangle me-1"></i>aria2c not installed</span>';
  return `${daemonBadge} <span class="small text-muted ms-1">Watching <code>${escapeHtml(dir)}</code> for .torrent files${aria2.version ? ` &middot; aria2 ${escapeHtml(aria2.version)}` : ""}</span>`;
}

function renderTorrentAlerts(payload) {
  const aria2 = payload.aria2 || {};
  const dir = (payload.settings || {}).directory || "";
  const downloadDir = payload.effective_download_directory || dir;
  const daemonError = aria2.installed && !aria2.running && aria2.daemon_error
    ? `<div class="alert alert-danger py-2 mb-3">aria2c problem: ${escapeHtml(aria2.daemon_error)}</div>` : "";
  const dirWarning = payload.directory_exists === false
    ? `<div class="alert alert-warning py-2 mb-3">The torrent folder <code>${escapeHtml(dir)}</code> does not exist yet. Save the settings to create it.</div>` : "";
  // Only a distinct warning when the download location differs from the
  // watch folder -- otherwise the one above already covers it.
  const downloadDirWarning = payload.download_directory_exists === false && downloadDir !== dir
    ? `<div class="alert alert-warning py-2 mb-3">The download location <code>${escapeHtml(downloadDir)}</code> does not exist yet. Save the settings to create it.</div>` : "";
  const vpnWarning = payload.vpn_required && !payload.vpn_ready
    ? `<div class="alert alert-warning py-2 mb-3"><i class="bi bi-shield-lock me-2"></i>VPN-required mode is active. Torrent downloads and uploads are blocked until the VPN reconnects.</div>` : "";
  return `${vpnWarning}${daemonError}${dirWarning}${downloadDirWarning}`;
}

function renderTorrentSummaryCards(counts) {
  counts = counts || {};
  const summary = [
    ["Queued", counts.queued || 0, "bi-hourglass-split", "warning"],
    ["Downloading", counts.downloading || 0, "bi-cloud-arrow-down", "info"],
    ["Complete", counts.complete || 0, "bi-check-circle", "success"],
    ["Error", counts.error || 0, "bi-exclamation-octagon", "danger"],
  ];
  return summary.map(([label, count, icon, tone]) => `<div class="download-summary-card tone-${tone}"><i class="bi ${icon}"></i><div><strong>${count}</strong><span>${label}</span></div></div>`).join("");
}

// First mount only: builds the stable skeleton (card chrome, table + thead).
// Later updates go through patchTorrentsLive, which never recreates any of
// these container nodes -- that's what keeps the 3s auto-refresh flash-free.
function renderTorrentQueueActions(payload) {
  const paused = Boolean(payload.paused);
  const toggleBtn = paused
    ? `<button class="btn btn-sm btn-outline-success" title="Resume downloads" aria-label="Resume downloads" onclick="resumeTorrentDownloads()"><i class="bi bi-play-fill me-1"></i>Resume Downloads</button>`
    : `<button class="btn btn-sm btn-outline-secondary" title="Pause downloads" aria-label="Pause downloads" onclick="pauseTorrentDownloads()"><i class="bi bi-pause-fill me-1"></i>Pause Downloads</button>`;
  const clearBtn = `<button class="btn btn-sm btn-outline-danger" title="Clear torrents" aria-label="Clear torrents" onclick="openTorrentClearModal()"><i class="bi bi-trash3 me-1"></i>Clear</button>`;
  return `${toggleBtn}${clearBtn}`;
}

function renderTorrentsLive(payload) {
  torrentsLastPayload = payload;
  return `
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
      <div id="torrentsDaemonNote">${renderTorrentDaemonNote(payload)}</div>
      <div class="d-flex flex-wrap align-items-center gap-2">
        <div id="torrentsQueueActions" class="d-flex flex-wrap align-items-center gap-2">${renderTorrentQueueActions(payload)}</div>
        <button class="btn btn-sm btn-outline-primary" title="Refresh torrents" aria-label="Refresh torrents" onclick="refreshTorrentsLive()"><i class="bi bi-arrow-repeat"></i></button>
      </div>
    </div>
    <div id="torrentsAlerts">${renderTorrentAlerts(payload)}</div>
    <div id="torrentsSummaryGrid" class="download-summary-grid mb-3">${renderTorrentSummaryCards(payload.counts)}</div>
    ${renderTorrentTableShell(payload.torrents || [])}
  `;
}

// Live-refresh path (3s poll + manual Refresh + post-action reload): patches
// only the leaf content of each region above by id. The card, the table
// element, and its <thead> are never touched, so nothing visibly flashes.
function patchTorrentsLive(payload) {
  torrentsLastPayload = payload;
  const noteNode = document.getElementById("torrentsDaemonNote");
  if (noteNode) noteNode.innerHTML = renderTorrentDaemonNote(payload);
  const actionsNode = document.getElementById("torrentsQueueActions");
  if (actionsNode) actionsNode.innerHTML = renderTorrentQueueActions(payload);
  const alertsNode = document.getElementById("torrentsAlerts");
  if (alertsNode) alertsNode.innerHTML = renderTorrentAlerts(payload);
  const summaryNode = document.getElementById("torrentsSummaryGrid");
  if (summaryNode) summaryNode.innerHTML = renderTorrentSummaryCards(payload.counts);
  const bodyNode = document.getElementById("torrentsTableBody");
  if (bodyNode) bodyNode.innerHTML = renderTorrentTableBody(payload.torrents || []);
}

async function renderTorrentsPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Torrents";
  subtitleNode.textContent = "Watched-folder torrent downloads via aria2c";
  setLoading(true, "Loading torrents...");
  let payload;
  try {
    payload = await api("/admin/torrents");
  } catch (err) {
    setLoading(false);
    content.innerHTML = `<div class="alert alert-danger">Failed to load torrents: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  const settings = payload.settings || {};
  const aria2 = payload.aria2 || {};
  const installCard = aria2.installed ? "" : `
    <div class="card mb-3"><div class="card-body">
      <div class="d-flex flex-wrap align-items-center justify-content-between gap-2">
        <div>
          <h6 class="mb-1"><i class="bi bi-exclamation-triangle text-warning me-2"></i>aria2c is not installed</h6>
          <div class="small text-muted">Torrents are queued but cannot download until aria2c is installed -- a ~6 MB static binary stored inside the Drone app folder.</div>
        </div>
        <button class="btn btn-primary" id="installAria2Btn" onclick="installAria2()"><i class="bi bi-download me-1"></i>Download aria2c</button>
      </div>
    </div></div>`;
  content.innerHTML = `
    ${installCard}
    <div class="card mb-3">
      <div class="card-header d-flex justify-content-between align-items-center gap-2">
        <span><i class="bi bi-gear me-2"></i>Torrent Settings</span>
        <button class="btn btn-sm btn-outline-primary" onclick="openTorrentUploadPicker()"><i class="bi bi-upload me-1"></i>Upload Torrents</button>
      </div>
      <div class="card-body">
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
          <input class="form-control form-control-sm" type="text" id="magnetLinkInput" placeholder="Paste a magnet link (magnet:?xt=urn:btih:...)" style="max-width:420px" onkeydown="if (event.key === 'Enter') addMagnetLink();">
          <button class="btn btn-sm btn-outline-primary" type="button" onclick="addMagnetLink()"><i class="bi bi-magnet me-1"></i>Add Magnet</button>
        </div>
        <div class="small text-muted mb-2">Dropped/uploaded .torrent files are watched from <code>${escapeHtml(settings.directory || "")}</code>.</div>
        <div class="row g-2 mb-2">
          <div class="col-12">
            <label class="form-label mb-1" for="torrentDownloadDir">Download location</label>
            <div class="input-group input-group-sm">
              <input class="form-control" type="text" id="torrentDownloadDir" placeholder="${escapeHtml(payload.effective_download_directory || settings.directory || "")}" value="${escapeHtml(settings.download_directory || "")}">
              <button class="btn btn-outline-secondary" type="button" onclick="openTorrentDirBrowser('torrentDownloadDir', 'Choose download location')"><i class="bi bi-folder2-open me-1"></i>Browse</button>
            </div>
            <div class="form-text">Where downloads land (can differ from the watched folder, e.g. an external drive) -- leave blank to match it. In-progress torrents can be moved here with their per-row migrate button.</div>
          </div>
        </div>
        <div class="row g-2 mb-2">
          <div class="col-6 col-sm-4 col-xl-2">
            <label class="form-label mb-1" for="torrentSeedTime" title="Minutes to keep seeding after a download completes. 0 = stop immediately.">Seed time (min)</label>
            <input class="form-control form-control-sm" type="number" id="torrentSeedTime" min="0" step="1" value="${escapeHtml(String(settings.seed_time ?? 60))}">
          </div>
          <div class="col-6 col-sm-4 col-xl-2">
            <label class="form-label mb-1" for="torrentSeedRatio" title="Stop seeding at this upload/download ratio. 0 = no limit.">Seed ratio</label>
            <input class="form-control form-control-sm" type="number" id="torrentSeedRatio" min="0" step="0.1" value="${escapeHtml(String(settings.seed_ratio ?? 1.0))}">
          </div>
          <div class="col-6 col-sm-4 col-xl-2">
            <label class="form-label mb-1" for="torrentBtStopTimeout" title="Stop a torrent stalled at 0 B/s for this long. 0 = disabled.">Stall timeout (s)</label>
            <input class="form-control form-control-sm" type="number" id="torrentBtStopTimeout" min="0" step="1" value="${escapeHtml(String(settings.bt_stop_timeout ?? 0))}">
          </div>
          <div class="col-6 col-sm-4 col-xl-2">
            <label class="form-label mb-1" for="torrentMaxConcurrent" title="Torrents downloading at once. Force Start bypasses this limit.">Concurrent</label>
            <input class="form-control form-control-sm" type="number" id="torrentMaxConcurrent" min="1" max="16" step="1" value="${escapeHtml(String(settings.max_concurrent_downloads ?? 3))}">
          </div>
          <div class="col-6 col-sm-4 col-xl-2 d-flex align-items-end">
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" role="switch" id="torrentFileAllocation" ${(settings.file_allocation || "prealloc") !== "none" ? "checked" : ""}>
              <label class="form-check-label" for="torrentFileAllocation" title="On pre-allocates file space up front (steadier writes, slower start). Off (file-allocation=none) allocates as data arrives.">File allocation</label>
            </div>
          </div>
          <div class="col-12 col-xl-4 d-flex align-items-end">
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" role="switch" id="torrentVpnRequired" ${settings.vpn_required ? "checked" : ""}>
              <label class="form-check-label" for="torrentVpnRequired" title="Fail closed: bind aria2 to tun0 and block all torrent downloads and uploads whenever the VPN is unavailable.">Require VPN</label>
              <div class="form-text">Prevents torrent download and upload outside the VPN.</div>
            </div>
          </div>
        </div>
        <div class="small text-muted mb-2">Seed and allocation settings apply to torrents added after saving.</div>
        <button class="btn btn-primary btn-sm" id="torrentSettingsSaveBtn" onclick="saveTorrentSettings()"><i class="bi bi-save me-1"></i>Save</button>
      </div>
    </div>
    <div class="card log-card"><div class="card-body">
      <div id="torrentsLive">${renderTorrentsLive(payload)}</div>
    </div></div>
  `;
  startTorrentsAutoRefresh();
}

async function refreshTorrentsLive() {
  try {
    const payload = await api("/admin/torrents");
    if (document.getElementById("torrentsLive")) patchTorrentsLive(payload);
  } catch (err) {
    showToast(`Failed to refresh torrents: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function saveTorrentSettings() {
  const downloadDir = (document.getElementById("torrentDownloadDir").value || "").trim();
  const seedTime = parseInt(document.getElementById("torrentSeedTime").value, 10);
  const seedRatio = parseFloat(document.getElementById("torrentSeedRatio").value);
  const stallTimeout = parseInt(document.getElementById("torrentBtStopTimeout").value, 10);
  const fileAllocation = document.getElementById("torrentFileAllocation").checked ? "prealloc" : "none";
  const maxConcurrent = parseInt(document.getElementById("torrentMaxConcurrent").value, 10);
  const vpnRequired = document.getElementById("torrentVpnRequired").checked;
  if (!Number.isFinite(seedTime) || seedTime < 0) { showToast("Seed time must be 0 or more minutes.", "warning"); return; }
  if (!Number.isFinite(seedRatio) || seedRatio < 0) { showToast("Seed ratio must be 0 or more.", "warning"); return; }
  if (!Number.isFinite(stallTimeout) || stallTimeout < 0) { showToast("Stall timeout must be 0 or more seconds.", "warning"); return; }
  if (!Number.isFinite(maxConcurrent) || maxConcurrent < 1 || maxConcurrent > 16) { showToast("Concurrent downloads must be between 1 and 16.", "warning"); return; }
  setLoading(true, "Saving torrent settings...");
  try {
    await apiPost("/admin/torrents/settings", {
      download_directory: downloadDir,
      seed_time: seedTime,
      seed_ratio: seedRatio,
      bt_stop_timeout: stallTimeout,
      file_allocation: fileAllocation,
      max_concurrent_downloads: maxConcurrent,
      vpn_required: vpnRequired,
    });
    showToast("Torrent settings saved.", "success");
    await renderTorrentsPage();
  } catch (err) {
    showToast(`Failed to save torrent settings: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}

async function installAria2() {
  const button = document.getElementById("installAria2Btn");
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Downloading...';
  }
  const toast = showToast('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Downloading aria2c...', "info", null);
  try {
    const result = await apiPost("/admin/torrents/aria2/install", {});
    dismissToast(toast);
    showToast(`aria2c ${escapeHtml(result.version || "")} installed.`, "success");
    await renderTorrentsPage();
  } catch (err) {
    dismissToast(toast);
    showToast(`aria2c install failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 10000);
    if (button && button.isConnected) {
      button.disabled = false;
      button.innerHTML = '<i class="bi bi-download me-1"></i>Download aria2c';
    }
  }
}

async function forceStartTorrent(torrentId) {
  if (!torrentId) return;
  try {
    await apiPost(`/admin/torrents/${encodeURIComponent(torrentId)}/force-start`, {});
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Force start failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

// Backs both the "Send to queue" and "Stop seeding" buttons (same backend
// route -- torrent_manager.py's cancel() picks the outcome based on status).
// No confirm dialog: sending a torrent back to the queue is no longer a
// destructive, Force-Start-to-undo action -- it resumes on its own -- and
// stopping seeding was already non-destructive.
async function cancelTorrent(torrentId) {
  if (!torrentId) return;
  try {
    await apiPost(`/admin/torrents/${encodeURIComponent(torrentId)}/cancel`, {});
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Action failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function migratePartialTorrent(torrentId, retry = false) {
  if (!torrentId) return;
  const destination = String((torrentsLastPayload || {}).effective_download_directory || "");
  if (!retry && !window.confirm(`Move this torrent's partial download to ${destination} and continue downloading there?`)) return;
  try {
    await apiPost(`/admin/torrents/${encodeURIComponent(torrentId)}/migrate`, { retry });
    showToast(retry ? "Migration retry queued." : "Partial download migration queued.", "success");
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Migration failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 10000);
  }
}

async function deleteTorrent(torrentId) {
  if (!torrentId || !window.confirm("Delete this torrent? It is removed from the list, its .torrent file is deleted, and its downloaded files are deleted too. This cannot be undone.")) return;
  try {
    await apiPost(`/admin/torrents/${encodeURIComponent(torrentId)}/delete`, {});
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Delete failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function pauseTorrentDownloads() {
  try {
    await apiPost("/admin/torrents/pause", {});
    showToast("Torrent downloads paused.", "success");
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Failed to pause downloads: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function resumeTorrentDownloads() {
  try {
    await apiPost("/admin/torrents/resume", {});
    showToast("Torrent downloads resumed.", "success");
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Failed to resume downloads: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

function openTorrentClearModal() {
  const modalId = "torrentClearModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-trash3 me-2"></i>Clear Torrents</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <div class="mb-3">
            <div class="form-label mb-1">Which torrents?</div>
            <div class="form-check">
              <input class="form-check-input" type="radio" name="torrentClearScope" id="torrentClearScopeCompleted" value="completed" checked>
              <label class="form-check-label" for="torrentClearScopeCompleted">Completed only</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="radio" name="torrentClearScope" id="torrentClearScopeAll" value="all">
              <label class="form-check-label" for="torrentClearScopeAll">All (including downloading / pending / error)</label>
            </div>
          </div>
          <div class="mb-1">What should be deleted?</div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="torrentClearFromUi" checked>
            <label class="form-check-label" for="torrentClearFromUi">Remove from this list</label>
          </div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="torrentClearTorrentFile" checked>
            <label class="form-check-label" for="torrentClearTorrentFile">Delete the .torrent file</label>
          </div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="torrentClearDownloadedFiles">
            <label class="form-check-label" for="torrentClearDownloadedFiles">Delete the downloaded files</label>
          </div>
          <div class="alert alert-warning py-2 mt-3 mb-0 small">This cannot be undone.</div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-danger" id="torrentClearConfirmBtn" onclick="confirmTorrentClear()"><i class="bi bi-trash3 me-1"></i>Clear</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

async function confirmTorrentClear() {
  const scope = document.querySelector('input[name="torrentClearScope"]:checked')?.value || "completed";
  const deleteFromUi = Boolean(document.getElementById("torrentClearFromUi")?.checked);
  const deleteTorrentFile = Boolean(document.getElementById("torrentClearTorrentFile")?.checked);
  const deleteDownloadedFiles = Boolean(document.getElementById("torrentClearDownloadedFiles")?.checked);
  if (!deleteFromUi && !deleteTorrentFile && !deleteDownloadedFiles) {
    showToast("Choose at least one action.", "warning");
    return;
  }
  const btn = document.getElementById("torrentClearConfirmBtn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Clearing...';
  }
  try {
    const result = await apiPost("/admin/torrents/clear", {
      scope,
      delete_from_ui: deleteFromUi,
      delete_torrent_file: deleteTorrentFile,
      delete_downloaded_files: deleteDownloadedFiles,
    });
    const cleared = Number(result.cleared) || 0;
    showToast(`Cleared ${cleared} torrent${cleared === 1 ? "" : "s"}.`, "success");
    const modal = document.getElementById("torrentClearModal");
    if (modal && window.bootstrap?.Modal) window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Clear failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-trash3 me-1"></i>Clear';
    }
  }
}

// ------------------------------------------------------ Torrents: move files

function renderMoveFilesLocationChips(recent) {
  const defaults = ["/userdata/roms", "/userdata/bios", "/userdata/saves", "/userdata/movies"];
  const seen = new Set();
  const chips = [];
  (recent || []).forEach((path) => {
    if (path && !seen.has(path)) {
      seen.add(path);
      chips.push({ path, recent: true });
    }
  });
  defaults.forEach((path) => {
    if (!seen.has(path)) {
      seen.add(path);
      chips.push({ path, recent: false });
    }
  });
  if (!chips.length) return "";
  return chips.map((chip) => `<button type="button" class="btn btn-sm btn-outline-secondary" onclick="setMoveFilesDestination('${escapeHtml(chip.path)}')"><i class="bi ${chip.recent ? "bi-clock-history" : "bi-star"} me-1"></i>${escapeHtml(chip.path)}</button>`).join(" ");
}

function setMoveFilesDestination(path) {
  const input = document.getElementById("moveFilesDestination");
  if (input) input.value = path;
}

// Groups the flat file list (each with a `/`-joined relative_path) into a
// folder hierarchy so it can render as a real tree instead of a flat list --
// mirrors the folder-picker's tree so both filesystem-browsing UIs in this
// app look and behave the same way.
function buildFileTree(files) {
  const root = { dirs: new Map(), files: [] };
  files.forEach((file) => {
    const parts = String(file.relative_path || file.name || "").split("/").filter(Boolean);
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    }
    node.files.push(file);
  });
  return root;
}

function renderFileTreeNode(node) {
  const dirItems = Array.from(node.dirs.entries()).map(([name, child]) => `
    <li class="file-tree-node expanded" data-kind="dir">
      <div class="file-tree-row">
        <span class="file-tree-toggle"><i class="bi bi-chevron-right"></i></span>
        <input type="checkbox" class="form-check-input file-tree-checkbox file-tree-folder-checkbox" checked title="Select all files in this folder">
        <i class="bi bi-folder2 file-tree-icon"></i>
        <span class="file-tree-label" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
      </div>
      <ul class="file-tree-children">${renderFileTreeNode(child)}</ul>
    </li>
  `).join("");
  const fileItems = node.files.map((file) => `
    <li class="file-tree-node" data-kind="file">
      <label class="file-tree-row">
        <span class="file-tree-toggle"></span>
        <input type="checkbox" class="form-check-input file-tree-checkbox move-file-checkbox" value="${escapeHtml(file.path)}" ${file.exists ? "checked" : "disabled"}>
        <i class="bi bi-file-earmark file-tree-icon"></i>
        <span class="file-tree-label" title="${escapeHtml(file.relative_path || file.name || "")}">${escapeHtml(file.name || file.relative_path || "")}</span>
        <span class="file-tree-size">${file.size != null ? formatBytes(file.size) : "missing"}</span>
      </label>
    </li>
  `).join("");
  return dirItems + fileItems;
}

function renderMoveFilesList(files) {
  if (!files.length) {
    return '<div class="text-muted small px-2 py-1">No files found for this torrent.</div>';
  }
  const tree = buildFileTree(files);
  return `<ul class="file-tree">${renderFileTreeNode(tree)}</ul>`;
}

// Delegated on #moveFilesBody (a stable container -- only its innerHTML is
// replaced once the file list loads, so attaching this once at modal-open
// time survives that swap). A folder's checkbox bulk-toggles its own
// descendant file checkboxes; clicking the rest of a folder's row expands or
// collapses it; file rows are plain <label>s so clicking anywhere on one
// already toggles its own checkbox natively, no extra handling needed.
function handleMoveFilesTreeClick(event) {
  const folderCheckbox = event.target.closest(".file-tree-folder-checkbox");
  if (folderCheckbox) {
    const node = folderCheckbox.closest(".file-tree-node");
    const checked = folderCheckbox.checked;
    node?.querySelectorAll(".move-file-checkbox:not(:disabled)").forEach((cb) => { cb.checked = checked; });
    return;
  }
  const row = event.target.closest(".file-tree-row");
  const node = row ? row.closest(".file-tree-node") : null;
  if (node && node.dataset.kind === "dir") {
    node.classList.toggle("expanded");
  }
}

async function openMoveFilesModal(torrentId) {
  if (!torrentId) return;
  const modalId = "moveFilesModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable modal-lg">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-folder-symlink me-2"></i>Move Downloaded Files</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <div id="moveFilesBody" class="mb-3"><div class="text-center py-3"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span></div></div>
          <label class="form-label mb-1" for="moveFilesDestination">Move to</label>
          <div class="input-group input-group-sm mb-2">
            <input class="form-control" type="text" id="moveFilesDestination" placeholder="/userdata/roms">
            <button class="btn btn-outline-secondary" type="button" onclick="openTorrentDirBrowser('moveFilesDestination', 'Choose destination')"><i class="bi bi-folder2-open me-1"></i>Browse</button>
          </div>
          <div id="moveFilesSuggestions" class="d-flex flex-wrap gap-2 mb-3">${renderMoveFilesLocationChips((torrentsLastPayload || {}).recent_move_locations)}</div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="moveFilesPreserveStructure" checked>
            <label class="form-check-label small" for="moveFilesPreserveStructure">Preserve folder structure (uncheck to flatten all selected files directly into the destination folder)</label>
          </div>
          <div class="form-check">
            <input class="form-check-input" type="checkbox" id="moveFilesCleanup">
            <label class="form-check-label small" for="moveFilesCleanup">Delete the remaining downloaded files after moving (only if the move succeeds)</label>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" id="moveFilesConfirmBtn" onclick="confirmMoveFiles('${escapeHtml(torrentId)}')"><i class="bi bi-arrow-right-circle me-1"></i>Move</button>
        </div>
      </div>
    </div>`;
  modal.querySelector("#moveFilesBody").addEventListener("click", handleMoveFilesTreeClick);
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
  const body = document.getElementById("moveFilesBody");
  try {
    const result = await api(`/admin/torrents/${encodeURIComponent(torrentId)}/files`);
    if (body) body.innerHTML = renderMoveFilesList(result.files || []);
  } catch (err) {
    if (body) body.innerHTML = `<div class="small text-danger">Failed to list files: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}

async function confirmMoveFiles(torrentId) {
  if (!torrentId) return;
  const checked = Array.from(document.querySelectorAll(".move-file-checkbox:checked")).map((el) => el.value);
  const destination = (document.getElementById("moveFilesDestination")?.value || "").trim();
  const cleanup = Boolean(document.getElementById("moveFilesCleanup")?.checked);
  const preserveStructure = Boolean(document.getElementById("moveFilesPreserveStructure")?.checked);
  if (!checked.length) { showToast("Select at least one file to move.", "warning"); return; }
  if (!destination) { showToast("Choose a destination folder.", "warning"); return; }
  const btn = document.getElementById("moveFilesConfirmBtn");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Moving...';
  }
  try {
    // move_files() now only enqueues a background move_job (see
    // torrent_manager.py) -- the actual shutil.move work happens on its own
    // worker thread and is tracked from here on purely through the normal
    // 3s live-refresh poll (row.move_job), not this response.
    const result = await apiPost(`/admin/torrents/${encodeURIComponent(torrentId)}/move`, { files: checked, destination, cleanup, preserve_structure: preserveStructure });
    if (result.status === "queued") {
      showToast(`Move started in the background (${Number(result.move_job?.total_files) || checked.length} file${checked.length === 1 ? "" : "s"}).`, "success");
      const modal = document.getElementById("moveFilesModal");
      if (modal && window.bootstrap?.Modal) window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    } else if (result.status === "already_in_progress") {
      showToast("A move is already running for this torrent -- wait for it to finish before starting another.", "warning");
    } else {
      showToast(`Move failed to start: ${escapeHtml(result.message || result.status || "unknown error")}`, "danger");
    }
    await refreshTorrentsLive();
  } catch (err) {
    showToast(`Move failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-arrow-right-circle me-1"></i>Move';
    }
  }
}

// Raises a modal (and its freshly-created backdrop) above any other modal
// already open -- Bootstrap doesn't officially support stacked modals, so
// without this a modal opened from within another modal (e.g. Move Files'
// destination Browse button) paints *behind* it at the same default z-index,
// making it unreachable. Call this from a `shown.bs.modal` handler so the
// backdrop this modal just created already exists in the DOM.
function bringModalToFront(modalEl) {
  const openModals = Array.from(document.querySelectorAll(".modal.show"));
  if (openModals.length <= 1) return;
  const z = 1055 + openModals.length * 20;
  modalEl.style.zIndex = String(z);
  const backdrops = document.querySelectorAll(".modal-backdrop");
  const lastBackdrop = backdrops[backdrops.length - 1];
  if (lastBackdrop) lastBackdrop.style.zIndex = String(z - 5);
}

// Shared by both the watch-folder and download-location fields, and the Move
// Files destination picker -- which input gets the chosen path is tracked in
// this module-level var since the modal's own "Use this folder" button calls
// chooseTorrentDir() with no arguments. A lazy-loaded tree (not a drill-down
// list) so the folder hierarchy stays visible while browsing -- each node
// fetches its own children on first expand and caches them (dataset.loaded).
let torrentDirBrowserTargetInputId = "torrentDownloadDir";
let torrentDirBrowserSelectedPath = "";

function renderTorrentDirNode(path, label) {
  return `<li class="dir-tree-node" data-path="${escapeHtml(path)}">
    <div class="dir-tree-row">
      <span class="dir-tree-toggle"><i class="bi bi-chevron-right"></i></span>
      <i class="bi bi-folder2 dir-tree-icon"></i>
      <span class="dir-tree-label" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
    </div>
    <ul class="dir-tree-children"></ul>
  </li>`;
}

function openTorrentDirBrowser(targetInputId = "torrentDownloadDir", title = "Choose download location") {
  torrentDirBrowserTargetInputId = targetInputId;
  torrentDirBrowserSelectedPath = "";
  const modalId = "torrentDirBrowserModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <div class="text-truncate flex-grow-1">
            <h5 class="modal-title mb-0"><i class="bi bi-folder2-open me-2"></i>${escapeHtml(title)}</h5>
            <div class="d-flex align-items-center gap-1 mt-1">
              <button type="button" class="btn btn-sm btn-outline-secondary py-0 px-1" id="torrentDirUpBtn" title="Go up one level" disabled onclick="goUpTorrentDirLevel()"><i class="bi bi-arrow-90deg-up"></i></button>
              <div class="small text-muted text-truncate" id="torrentDirBrowserPath">No folder selected</div>
            </div>
          </div>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body p-2"><ul class="dir-tree" id="torrentDirBrowserTree"><li class="text-center py-3"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span></li></ul></div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" id="torrentDirChooseBtn" onclick="chooseTorrentDir()" disabled>Use this folder</button>
        </div>
      </div>
    </div>`;
  modal.querySelector("#torrentDirBrowserTree").addEventListener("click", handleTorrentDirTreeClick);
  modal.querySelector("#torrentDirBrowserPath").addEventListener("click", handleTorrentDirBreadcrumbClick);
  if (window.bootstrap?.Modal) {
    modal.addEventListener("shown.bs.modal", () => bringModalToFront(modal), { once: true });
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
  const startPath = (document.getElementById(targetInputId)?.value || "").trim();
  loadTorrentDirRoots(startPath);
}

async function loadTorrentDirRoots(startPath) {
  const tree = document.getElementById("torrentDirBrowserTree");
  if (!tree) return;
  tree.innerHTML = '<li class="text-center py-3"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span></li>';
  let payload;
  try {
    payload = await api("/admin/torrents/browse?path=");
  } catch (err) {
    tree.innerHTML = `<li class="p-2 small text-danger">Failed to browse folders: ${escapeHtml(err.message || "unknown error")}</li>`;
    return;
  }
  const target = document.getElementById("torrentDirBrowserTree");
  if (!target) return;
  target.innerHTML = (payload.dirs || []).map((dir) => renderTorrentDirNode(dir.path, dir.name)).join("")
    || '<li class="p-2 small text-muted">No storage roots available.</li>';
  if (startPath) await expandTorrentDirTreeToPath(startPath);
}

async function loadTorrentDirChildren(node) {
  const childrenUl = node.querySelector(":scope > .dir-tree-children");
  if (!childrenUl) return;
  childrenUl.innerHTML = '<li class="text-center py-2"><span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span></li>';
  try {
    const payload = await api(`/admin/torrents/browse?path=${encodeURIComponent(node.dataset.path || "")}`);
    const dirs = payload.dirs || [];
    childrenUl.innerHTML = dirs.length
      ? dirs.map((dir) => renderTorrentDirNode(dir.path, dir.name)).join("")
      : '<li class="px-2 py-1 small text-muted">No subfolders</li>';
    node.dataset.loaded = "1";
  } catch (err) {
    childrenUl.innerHTML = `<li class="px-2 py-1 small text-danger">${escapeHtml(err.message || "failed to load")}</li>`;
  }
}

async function expandTorrentDirNode(node) {
  if (node.dataset.loaded !== "1") await loadTorrentDirChildren(node);
  node.classList.add("expanded");
}

async function toggleTorrentDirNode(node) {
  if (node.classList.contains("expanded")) {
    node.classList.remove("expanded");
    return;
  }
  await expandTorrentDirNode(node);
}

// The tree shows the whole hierarchy at once (not a drill-down list), so
// "going back a directory" means jumping selection to an already-expanded
// ancestor -- walked straight from the DOM (node -> its <ul> -> that ul's
// owning <li>) rather than by re-parsing the path string, so it can't drift
// out of sync with what's actually rendered.
function torrentDirParentNode(node) {
  const parentUl = node.parentElement;
  return parentUl && parentUl.classList.contains("dir-tree-children") ? parentUl.closest(".dir-tree-node") : null;
}

function selectTorrentDirNode(node) {
  const tree = document.getElementById("torrentDirBrowserTree");
  tree?.querySelectorAll(".dir-tree-row.selected").forEach((el) => el.classList.remove("selected"));
  node.querySelector(":scope > .dir-tree-row")?.classList.add("selected");
  torrentDirBrowserSelectedPath = node.dataset.path || "";
  const pathLabel = document.getElementById("torrentDirBrowserPath");
  if (pathLabel) pathLabel.innerHTML = renderTorrentDirBreadcrumb(node);
  const chooseBtn = document.getElementById("torrentDirChooseBtn");
  if (chooseBtn) chooseBtn.disabled = !torrentDirBrowserSelectedPath;
  const upBtn = document.getElementById("torrentDirUpBtn");
  if (upBtn) upBtn.disabled = !torrentDirParentNode(node);
}

// A storage root's own label is its full absolute path (there's no shorter
// name to give it), which would otherwise eat the whole breadcrumb's width
// on its own -- show just its last segment there and keep the full path as
// a tooltip. A no-op for every other (already-short) segment.
function shortenBreadcrumbLabel(label) {
  if (!label.includes("/")) return label;
  const parts = label.split("/").filter(Boolean);
  return parts[parts.length - 1] || label;
}

function renderTorrentDirBreadcrumb(node) {
  const chain = [];
  for (let current = node; current; current = torrentDirParentNode(current)) {
    const label = current.querySelector(":scope > .dir-tree-row .dir-tree-label")?.textContent || current.dataset.path || "";
    chain.unshift({ path: current.dataset.path || "", label });
  }
  if (!chain.length) return "No folder selected";
  return chain
    .map((seg, i) => {
      const btn = `<button type="button" class="dir-tree-crumb" data-path="${escapeHtml(seg.path)}" title="${escapeHtml(seg.label)}">${escapeHtml(shortenBreadcrumbLabel(seg.label))}</button>`;
      return i > 0 ? `<span class="dir-tree-crumb-sep">/</span>${btn}` : btn;
    })
    .join("");
}

function findTorrentDirNodeByPath(path) {
  const tree = document.getElementById("torrentDirBrowserTree");
  if (!tree || !path) return null;
  return Array.from(tree.querySelectorAll(".dir-tree-node")).find((li) => li.dataset.path === path) || null;
}

function handleTorrentDirBreadcrumbClick(event) {
  const crumb = event.target.closest(".dir-tree-crumb");
  if (!crumb) return;
  const node = findTorrentDirNodeByPath(crumb.dataset.path || "");
  if (!node) return;
  selectTorrentDirNode(node);
  node.scrollIntoView({ block: "center" });
}

function goUpTorrentDirLevel() {
  const selectedRow = document.querySelector("#torrentDirBrowserTree .dir-tree-row.selected");
  const selectedNode = selectedRow?.closest(".dir-tree-node");
  const parentNode = selectedNode ? torrentDirParentNode(selectedNode) : null;
  if (!parentNode) return;
  selectTorrentDirNode(parentNode);
  parentNode.scrollIntoView({ block: "center" });
}

async function handleTorrentDirTreeClick(event) {
  const row = event.target.closest(".dir-tree-row");
  if (!row) return;
  const node = row.closest(".dir-tree-node");
  if (!node) return;
  if (event.target.closest(".dir-tree-toggle")) {
    await toggleTorrentDirNode(node);
    return;
  }
  selectTorrentDirNode(node);
  await expandTorrentDirNode(node);
}

// Best-effort: walks the tree down to a previously-saved path, fetching and
// expanding each ancestor level so the user immediately sees where they are
// instead of starting back at the storage roots every time. Silently stops
// (leaving whatever was already expanded) if a segment no longer exists.
async function expandTorrentDirTreeToPath(targetPath) {
  const tree = document.getElementById("torrentDirBrowserTree");
  if (!tree) return;
  const normalizedTarget = targetPath.replace(/\/+$/, "");
  let current = Array.from(tree.children).find((li) => {
    const p = (li.dataset.path || "").replace(/\/+$/, "");
    return p && (normalizedTarget === p || normalizedTarget.startsWith(`${p}/`));
  });
  if (!current) return;
  while (current) {
    await expandTorrentDirNode(current);
    const currentPath = (current.dataset.path || "").replace(/\/+$/, "");
    if (currentPath === normalizedTarget) {
      selectTorrentDirNode(current);
      current.scrollIntoView({ block: "center" });
      return;
    }
    const childrenUl = current.querySelector(":scope > .dir-tree-children");
    const remainder = normalizedTarget.slice(currentPath.length).replace(/^\/+/, "");
    const nextSegment = remainder.split("/")[0];
    if (!nextSegment || !childrenUl) return;
    const nextPath = `${currentPath}/${nextSegment}`;
    current = Array.from(childrenUl.children).find((li) => (li.dataset.path || "") === nextPath);
  }
}

function openTorrentUploadPicker() {
  let input = document.getElementById("torrentUploadInput");
  if (!input) {
    input = document.createElement("input");
    input.type = "file";
    input.id = "torrentUploadInput";
    input.accept = ".torrent,application/x-bittorrent";
    input.multiple = true;
    input.className = "d-none";
    document.body.appendChild(input);
    input.addEventListener("change", async () => {
      const files = Array.from(input.files || []);
      input.value = "";
      if (files.length) await uploadTorrentFiles(files);
    });
  }
  input.click();
}

async function uploadTorrentFiles(files) {
  const formData = new FormData();
  files.forEach((file) => formData.append("torrents", file, file.name));
  const toast = showToast(`<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Uploading ${files.length} torrent file${files.length === 1 ? "" : "s"}...`, "info", null);
  try {
    const res = await fetch(_apiRequestUrl("/admin/torrents/upload"), {
      method: "POST",
      credentials: "include",
      body: formData,
    });
    let payload = {};
    try { payload = await res.json(); } catch (_) {}
    dismissToast(toast);
    if (!res.ok && !(payload.errors || []).length) {
      throw new Error(payload.error || `Upload failed: ${res.status}`);
    }
    const saved = payload.saved || [];
    const errors = payload.errors || [];
    if (saved.length) {
      showToast(`Uploaded ${saved.length} torrent file${saved.length === 1 ? "" : "s"}.`, errors.length ? "warning" : "success");
    }
    errors.forEach((entry) => {
      showToast(`${escapeHtml(entry.file || "file")}: ${escapeHtml(entry.error || "rejected")}`, "danger", 8000);
    });
    setTimeout(refreshTorrentsLive, 700);
  } catch (err) {
    dismissToast(toast);
    showToast(`Torrent upload failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  }
}

async function addMagnetLink() {
  const input = document.getElementById("magnetLinkInput");
  const magnetUri = (input && input.value || "").trim();
  if (!magnetUri) return;
  try {
    const result = await apiPost("/admin/torrents/magnet", { magnet_uri: magnetUri });
    if (result.status === "already_exists") {
      showToast(`${escapeHtml(result.name || "Magnet link")} is already in the torrent list.`, "info");
    } else {
      showToast(`Added ${escapeHtml(result.name || "magnet link")} to the queue.`, "success");
    }
    if (input) input.value = "";
    setTimeout(refreshTorrentsLive, 700);
  } catch (err) {
    showToast(`Failed to add magnet link: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  }
}

function chooseTorrentDir() {
  const input = document.getElementById(torrentDirBrowserTargetInputId);
  const path = torrentDirBrowserSelectedPath;
  if (input && path) input.value = path;
  const modal = document.getElementById("torrentDirBrowserModal");
  if (modal) {
    if (window.bootstrap?.Modal) {
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    } else {
      modal.classList.remove("show");
      modal.style.display = "none";
    }
  }
  if (path) showToast("Folder selected. Click Save to apply it.", "info");
}

// ------------------------------------------------------------------- VPN

function stopVpnAutoRefresh() {
  if (vpnTimer) {
    clearInterval(vpnTimer);
    vpnTimer = null;
  }
  vpnInFlight = false;
}
function startVpnAutoRefresh() {
  // Live-update only the status/log region -- never the upload or
  // credentials forms above it, so in-progress edits are untouched.
  stopVpnAutoRefresh();
  vpnTimer = setInterval(async () => {
    if (document.hidden || vpnInFlight) return;
    if (window.location.hash !== "#admin/vpn") return;
    const liveNode = document.getElementById("vpnLive");
    if (!liveNode) return;
    vpnInFlight = true;
    try {
      const payload = await api("/admin/vpn");
      if (
        window.location.hash === "#admin/vpn" &&
        liveNode.isConnected &&
        document.getElementById("vpnLive") === liveNode &&
        !liveNode.contains(document.activeElement)
      ) {
        patchVpnLive(payload);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      vpnInFlight = false;
    }
  }, 3000);
}

function vpnStatusBadge(payload) {
  const status = String(payload.status || "disconnected");
  const cls = status === "connected" ? "success" : status === "connecting" ? "info" : status === "error" ? "danger" : "secondary";
  const label = status.charAt(0).toUpperCase() + status.slice(1);
  return `<span class="badge text-bg-${cls}" title="${escapeHtml(payload.message || "")}">${escapeHtml(label)}</span>`;
}

function renderVpnStatusCards(payload) {
  const status = String(payload.status || "disconnected");
  const duration = status === "connected" ? formatDuration(payload.connected_duration_seconds || 0) : "--";
  const server = (payload.remotes || [])[0] || "--";
  const cards = [
    ["Status", "", "bi-broadcast", ""],
    ["Connected Since", duration, "bi-clock-history", ""],
    ["VPN Server", server, "bi-hdd-network", "mono"],
    ["Tunnel (tun0)", payload.tunnel_ip || "--", "bi-diagram-2", "mono"],
  ];
  return cards.map(([label, value, icon, valueClass]) => `
    <div class="col-6 col-lg-3">
      <div class="asset-detail-panel h-100">
        <h6><i class="bi ${icon} me-1"></i>${escapeHtml(label)}</h6>
        <div class="${valueClass}">${label === "Status" ? vpnStatusBadge(payload) : escapeHtml(value)}</div>
      </div>
    </div>
  `).join("");
}

function renderVpnValidationErrors(payload) {
  const errors = payload.validation_errors || [];
  if (!errors.length) return "";
  return `<div class="alert alert-warning py-2 mb-3">
    <strong>Not ready to connect:</strong>
    <ul class="mb-0">${errors.map(error => `<li>${escapeHtml(error)}</li>`).join("")}</ul>
  </div>`;
}

function renderVpnLog(payload) {
  const lines = payload.log_tail || [];
  const text = lines.length ? lines.join("\n") : "No log output yet.";
  return `<pre class="local-asset-native-content" style="max-height:280px;overflow:auto;">${escapeHtml(text)}</pre>`;
}

function renderVpnActions(payload) {
  const status = String(payload.status || "disconnected");
  const canConnect = (status === "disconnected" || status === "error") && !(payload.validation_errors || []).length;
  const canDisconnect = status === "connecting" || status === "connected";
  return `<div class="d-flex flex-wrap gap-2 mb-3">
    <button class="btn btn-success" type="button" id="vpnConnectBtn" ${canConnect ? "" : "disabled"} onclick="connectVpn()"><i class="bi bi-play-fill me-1"></i>Connect</button>
    <button class="btn btn-outline-danger" type="button" id="vpnDisconnectBtn" ${canDisconnect ? "" : "disabled"} onclick="disconnectVpn()"><i class="bi bi-stop-fill me-1"></i>Disconnect</button>
    <button class="btn btn-outline-primary" type="button" onclick="verifyVpnPublicIp()"><i class="bi bi-globe me-1"></i>Verify Public IP</button>
    <button class="btn btn-outline-secondary" type="button" onclick="refreshVpnLive()"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
    <a class="btn btn-outline-secondary ${payload.log_available ? "" : "disabled"}" href="${API_BASE}/admin/vpn/log/download" target="_blank" rel="noopener noreferrer"><i class="bi bi-download me-1"></i>Download Log</a>
  </div>`;
}

// Revocation happens on a background poller (see check_sharing_revocation in
// vpn_manager.py), possibly while the user is already sitting on this page --
// so this notice must live in the live-polled region, not the static
// first-mount template, or a revoke that happens mid-visit would go unseen
// until the next manual page load.
function renderVpnRevokedNotice(payload) {
  if (!payload.revoked_reason) return "";
  const when = payload.revoked_at ? ` (${escapeHtml(formatCompactLocalDate(payload.revoked_at))})` : "";
  return `<div class="alert alert-warning py-2 mb-3"><i class="bi bi-shield-exclamation me-1"></i><strong>VPN credentials removed${when}:</strong> ${escapeHtml(payload.revoked_reason)}</div>`;
}

// Same live-polled-region reasoning as renderVpnRevokedNotice above -- the
// self-heal watchdog can reconnect at any time, not just while this page is
// open. Styled as a quiet FYI rather than a warning: a successful self-heal
// is the system working as intended, not a problem needing attention.
function renderVpnSelfHealNote(payload) {
  if (!payload.self_heal_last_at) return "";
  const when = escapeHtml(formatCompactLocalDate(payload.self_heal_last_at));
  const reason = escapeHtml(payload.self_heal_last_reason || "a connection problem");
  const recentNote = (payload.self_heal_recent_count || 0) > 1
    ? ` <span class="text-warning">Reconnected ${payload.self_heal_recent_count} times recently &mdash; if this keeps happening, something's likely still wrong.</span>`
    : "";
  return `<div class="small text-muted mb-3"><i class="bi bi-arrow-repeat me-1"></i>Auto-reconnected ${when} after: ${reason}.${recentNote}</div>`;
}

// First mount only: builds the stable skeleton. Later updates go through
// patchVpnLive, which never recreates these container nodes -- the same
// flash-free pattern as the Torrents grid's 3s auto-refresh.
function renderVpnLive(payload) {
  return `
    <div id="vpnRevokedNotice">${renderVpnRevokedNotice(payload)}</div>
    <div id="vpnSelfHealNote">${renderVpnSelfHealNote(payload)}</div>
    <div id="vpnValidationErrors">${renderVpnValidationErrors(payload)}</div>
    <div id="vpnActions">${renderVpnActions(payload)}</div>
    <div id="vpnStatusCards" class="row g-3 mb-3">${renderVpnStatusCards(payload)}</div>
    <div id="vpnPublicIp" class="small text-muted mb-3">${vpnPublicIpText(payload)}</div>
    <h6>Log</h6>
    <div id="vpnLogBody">${renderVpnLog(payload)}</div>
  `;
}

function patchVpnLive(payload) {
  const revokedNode = document.getElementById("vpnRevokedNotice");
  if (revokedNode) revokedNode.innerHTML = renderVpnRevokedNotice(payload);
  const selfHealNode = document.getElementById("vpnSelfHealNote");
  if (selfHealNode) selfHealNode.innerHTML = renderVpnSelfHealNote(payload);
  const errorsNode = document.getElementById("vpnValidationErrors");
  if (errorsNode) errorsNode.innerHTML = renderVpnValidationErrors(payload);
  const actionsNode = document.getElementById("vpnActions");
  if (actionsNode) actionsNode.innerHTML = renderVpnActions(payload);
  const cardsNode = document.getElementById("vpnStatusCards");
  if (cardsNode) cardsNode.innerHTML = renderVpnStatusCards(payload);
  const logNode = document.getElementById("vpnLogBody");
  if (logNode) logNode.innerHTML = renderVpnLog(payload);
}

let vpnLastPublicIp = null;
function vpnPublicIpText(payload) {
  if (!vpnLastPublicIp) return "";
  if (vpnLastPublicIp.error) return `<i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(vpnLastPublicIp.error)}`;
  return `<i class="bi bi-check-circle text-success me-1"></i>Public IP as of last check: <strong class="mono">${escapeHtml(vpnLastPublicIp.ip)}</strong> (${escapeHtml(formatCompactLocalDate(vpnLastPublicIp.checked_at))})`;
}

async function renderVpnPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "VPN";
  subtitleNode.textContent = "OpenVPN configuration and connection status -- connects automatically whenever the Drone starts up";
  setLoading(true, "Loading VPN status...");
  let payload;
  try {
    payload = await api("/admin/vpn");
  } catch (err) {
    setLoading(false);
    content.innerHTML = `<div class="alert alert-danger">Failed to load VPN status: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  vpnLastPublicIp = null;
  content.innerHTML = `
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-file-earmark-arrow-up me-2"></i>OpenVPN Configuration</div>
      <div class="card-body">
        <p class="text-muted small">Upload the .ovpn file from your VPN provider (Proton VPN, NordVPN, Private Internet Access, or any other OpenVPN provider). It is automatically adjusted to use the credentials saved below.</p>
        <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
          <button class="btn btn-primary" type="button" onclick="openVpnConfigPicker(true)"><i class="bi bi-arrow-repeat me-1"></i>Upload and Reconnect</button>
          <button class="btn btn-outline-primary" type="button" onclick="openVpnConfigPicker(false)"><i class="bi bi-upload me-1"></i>Upload Only</button>
          <span class="small ${payload.has_config ? "text-success" : "text-muted"}">${payload.has_config ? `<i class="bi bi-check-circle me-1"></i>${escapeHtml(payload.config_filename)}${payload.protocol ? ` <span class="badge text-bg-secondary ms-1">${escapeHtml(payload.protocol)}</span>` : ""}` : "No configuration uploaded yet."}</span>
        </div>
        <p class="form-text mb-0">Upload and Reconnect immediately switches the running VPN to the new profile. Upload Only saves it for the next reconnect or restart.</p>
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-key me-2"></i>VPN Credentials</div>
      <div class="card-body">
        <p class="text-muted small">Use the credentials your provider issues for OpenVPN connections (Proton VPN calls these your "OpenVPN / IKEv2 username and password" -- a token, not your account login). Stored in a 600-permission file that openvpn reads directly; this is required by OpenVPN's own <code>auth-user-pass</code> mechanism.</p>
        <div class="row g-2 mb-2">
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="vpnUsername">Username</label>
            <input class="form-control form-control-sm" type="text" id="vpnUsername" autocomplete="off" value="${escapeHtml(payload.username || "")}">
          </div>
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="vpnPassword">Password</label>
            <input class="form-control form-control-sm" type="password" id="vpnPassword" autocomplete="off" placeholder="${payload.has_credentials ? "Saved (leave blank to keep)" : ""}">
          </div>
        </div>
        <button class="btn btn-primary btn-sm" type="button" id="vpnCredentialsSaveBtn" onclick="saveVpnCredentials()"><i class="bi bi-save me-1"></i>Save</button>
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-body">
        <div class="form-check form-switch">
          <input class="form-check-input" type="checkbox" role="switch" id="vpnSelfHeal" ${payload.self_heal_enabled ? "checked" : ""} onchange="setVpnSelfHeal(this.checked)">
          <label class="form-check-label" for="vpnSelfHeal">Automatically reconnect if the VPN connection fails</label>
        </div>
        <p class="text-muted small mb-0 mt-1">Watches for connection errors and reconnects on its own. Replay/decrypt warnings never interrupt a tunnel that is still up. Reconnects are rate-limited so a persistent problem can't loop forever. On by default.</p>
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-people me-2"></i>Share with Swarm</div>
      <div class="card-body">
        ${payload.source_peer_id ? `
        <p class="text-muted small mb-0"><i class="bi bi-info-circle me-1"></i>This configuration was imported from <strong>${escapeHtml(payload.source_peer_name || payload.source_peer_id)}</strong> and cannot be re-shared &mdash; only the drone that originally uploaded a configuration can share it with the swarm.</p>
        ` : `
        <p class="text-muted small">Share this configuration and credentials with drones paired to this one, over the same cert-pinned peer link used for ROM/BIOS transfers -- never through the browser. Only paired drones can pull it, and only while this is turned on.</p>
        <div class="form-check form-switch mb-3">
          <input class="form-check-input" type="checkbox" role="switch" id="vpnSharingEnabled" ${payload.sharing_enabled ? "checked" : ""} onchange="setVpnSharing(this.checked)">
          <label class="form-check-label" for="vpnSharingEnabled">Allow paired drones to pull this VPN configuration</label>
        </div>
        `}
        <hr>
        <p class="text-muted small mb-2">Already sharing on another drone in your swarm? Pull its configuration here instead of uploading your own.</p>
        <div class="d-flex flex-wrap align-items-end gap-2">
          <div>
            <label class="form-label mb-1" for="vpnPullPeer">Paired Drone</label>
            <select id="vpnPullPeer" class="form-select form-select-sm" style="min-width:220px"><option value="">Loading...</option></select>
          </div>
          <button class="btn btn-outline-primary btn-sm" type="button" id="vpnPullBtn" disabled onclick="pullVpnConfigFromPeer()"><i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration</button>
        </div>
      </div>
    </div>
    <div class="card log-card"><div class="card-body">
      <div id="vpnLive">${renderVpnLive(payload)}</div>
    </div></div>
  `;
  startVpnAutoRefresh();
  loadVpnPullPeerOptions();
}

async function refreshVpnLive() {
  try {
    const payload = await api("/admin/vpn");
    if (document.getElementById("vpnLive")) patchVpnLive(payload);
  } catch (err) {
    showToast(`Failed to refresh VPN status: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

function openVpnConfigPicker(reconnectAfterUpload = false) {
  let input = document.getElementById("vpnConfigUploadInput");
  if (!input) {
    input = document.createElement("input");
    input.type = "file";
    input.id = "vpnConfigUploadInput";
    input.accept = ".ovpn";
    input.className = "d-none";
    document.body.appendChild(input);
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      const shouldReconnect = input.dataset.reconnectAfterUpload === "true";
      input.value = "";
      if (file) await uploadVpnConfig(file, shouldReconnect);
    });
  }
  input.dataset.reconnectAfterUpload = reconnectAfterUpload ? "true" : "false";
  input.click();
}

async function uploadVpnConfig(file, reconnectAfterUpload = false) {
  const formData = new FormData();
  formData.append("config", file, file.name);
  setLoading(true, "Uploading OpenVPN configuration...");
  try {
    const res = await fetch(_apiRequestUrl("/admin/vpn/upload"), { method: "POST", credentials: "include", body: formData });
    let responsePayload = {};
    try { responsePayload = await res.json(); } catch (_) {}
    if (!res.ok) throw new Error(responsePayload.error || `Upload failed: ${res.status}`);
    const uploadedName = escapeHtml(responsePayload.config_filename || file.name);
    const protocol = responsePayload.protocol ? ` (${escapeHtml(responsePayload.protocol)})` : "";
    if (reconnectAfterUpload) {
      setLoading(true, `Uploaded ${responsePayload.config_filename || file.name}. Reconnecting VPN...`);
      const reconnectResult = await apiPost("/admin/vpn/reconnect", {});
      if (reconnectResult.status === "error") {
        throw new Error((reconnectResult.errors || []).join(" ") || "The VPN could not reconnect.");
      }
      showToast(`Uploaded ${uploadedName}${protocol}. Reconnecting with the new profile...`, "success", 6000);
    } else {
      showToast(`Uploaded ${uploadedName}${protocol}. It will be used on the next reconnect.`, "success", 6000);
    }
    await renderVpnPage();
  } catch (err) {
    showToast(`OpenVPN config upload failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  } finally {
    setLoading(false);
  }
}

async function saveVpnCredentials() {
  const username = (document.getElementById("vpnUsername").value || "").trim();
  const password = document.getElementById("vpnPassword").value || "";
  if (!username) { showToast("Username is required.", "warning"); return; }
  if (!password) { showToast("Enter the VPN password to save it.", "warning"); return; }
  const button = document.getElementById("vpnCredentialsSaveBtn");
  button.disabled = true;
  try {
    await apiPost("/admin/vpn/credentials", { username, password });
    showToast("VPN credentials saved.", "success");
    document.getElementById("vpnPassword").value = "";
    await refreshVpnLive();
    document.getElementById("vpnPassword").placeholder = "Saved (leave blank to keep)";
  } catch (err) {
    showToast(`Failed to save VPN credentials: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    button.disabled = false;
  }
}

async function connectVpn() {
  const button = document.getElementById("vpnConnectBtn");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Connecting...'; }
  try {
    const result = await apiPost("/admin/vpn/connect", {});
    if (result.status === "error") {
      showToast(`Could not connect: ${escapeHtml((result.errors || []).join(" "))}`, "danger", 8000);
    } else {
      showToast("Connecting to VPN...", "info");
    }
  } catch (err) {
    showToast(`Failed to connect: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    await refreshVpnLive();
  }
}

async function disconnectVpn() {
  const button = document.getElementById("vpnDisconnectBtn");
  if (button) button.disabled = true;
  try {
    await apiPost("/admin/vpn/disconnect", {});
    showToast("VPN disconnected.", "success");
  } catch (err) {
    showToast(`Failed to disconnect: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    await refreshVpnLive();
  }
}

async function verifyVpnPublicIp() {
  const toast = showToast('<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Checking public IP...', "info", null);
  try {
    const res = await fetch(_apiRequestUrl("/admin/vpn/verify-ip"), { method: "POST", credentials: "include" });
    const payload = await res.json();
    vpnLastPublicIp = payload;
    dismissToast(toast);
    if (payload.ip) {
      showToast(`Public IP: ${escapeHtml(payload.ip)}`, "success");
    } else {
      showToast(payload.error || "Could not determine the public IP.", "warning");
    }
  } catch (err) {
    dismissToast(toast);
    vpnLastPublicIp = { error: err.message || "Could not determine the public IP." };
    showToast("Failed to check public IP.", "danger");
  } finally {
    const node = document.getElementById("vpnPublicIp");
    if (node) node.innerHTML = vpnPublicIpText(vpnLastPublicIp);
  }
}

async function setVpnSharing(enabled) {
  const checkbox = document.getElementById("vpnSharingEnabled");
  try {
    await apiPost("/admin/vpn/sharing", { enabled });
    showToast(`VPN sharing with paired drones ${enabled ? "enabled" : "disabled"}.`, "success");
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save sharing setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function setVpnSelfHeal(enabled) {
  const checkbox = document.getElementById("vpnSelfHeal");
  try {
    await apiPost("/admin/vpn/self-heal", { enabled });
    showToast(`VPN auto-reconnect ${enabled ? "enabled" : "disabled"}.`, "success");
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save auto-reconnect setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function loadVpnPullPeerOptions() {
  const select = document.getElementById("vpnPullPeer");
  const button = document.getElementById("vpnPullBtn");
  if (!select) return;
  try {
    const overview = await loadSwarmOverview();
    const onlinePeers = (overview.drones || []).filter(drone => !drone.is_self && drone.online);
    select.innerHTML = onlinePeers.length
      ? onlinePeers.map(drone => `<option value="${escapeHtml(drone.drone_id || "")}">${escapeHtml(drone.name || drone.hostname || drone.drone_id || "Drone")}</option>`).join("")
      : '<option value="">No paired drones online</option>';
    if (button) button.disabled = !onlinePeers.length;
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load drones</option>';
    if (button) button.disabled = true;
  }
}

async function pullVpnConfigFromPeer() {
  const select = document.getElementById("vpnPullPeer");
  const peerId = select ? select.value : "";
  if (!peerId) return;
  const button = document.getElementById("vpnPullBtn");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Pulling...'; }
  try {
    const result = await apiPost("/admin/vpn/pull-from-peer", { peer_id: peerId });
    showToast(
      result.credentials_imported ? "Pulled VPN configuration and credentials from peer." : "Pulled VPN configuration from peer (no credentials were shared).",
      "success",
    );
    await renderVpnPage();
  } catch (err) {
    showToast(`Failed to pull VPN configuration: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
    if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration'; }
  }
}

// -------------------------------------------------------------- Tailnet sharing
// Direct structural copies of setVpnSharing/loadVpnPullPeerOptions/
// pullVpnConfigFromPeer just above -- same single-hop-only sharing model,
// same peer-picker source (GET /admin/swarm/overview via loadSwarmOverview).
async function setTailnetSharing(enabled) {
  const checkbox = document.getElementById("tailnetSharingEnabled");
  try {
    await apiPost("/admin/tailnet/sharing", { enabled });
    showToast(`Tailnet auth-key sharing with paired drones ${enabled ? "enabled" : "disabled"}.`, "success");
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save sharing setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}
async function loadTailnetPullPeerOptions() {
  const select = document.getElementById("tailnetPullPeer");
  const button = document.getElementById("tailnetPullBtn");
  if (!select) return;
  try {
    const overview = await loadSwarmOverview();
    const onlinePeers = (overview.drones || []).filter(drone => !drone.is_self && drone.online);
    select.innerHTML = onlinePeers.length
      ? onlinePeers.map(drone => `<option value="${escapeHtml(drone.drone_id || "")}">${escapeHtml(drone.name || drone.hostname || drone.drone_id || "Drone")}</option>`).join("")
      : '<option value="">No paired drones online</option>';
    if (button) button.disabled = !onlinePeers.length;
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load drones</option>';
    if (button) button.disabled = true;
  }
}
async function pullTailnetConfigFromPeer() {
  const select = document.getElementById("tailnetPullPeer");
  const peerId = select ? select.value : "";
  if (!peerId) return;
  const button = document.getElementById("tailnetPullBtn");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Pulling...'; }
  try {
    await apiPost("/admin/tailnet/pull-from-peer", { peer_id: peerId });
    showToast("Enrolled using the auth key shared by that drone.", "success");
    await renderSwarmPage();
  } catch (err) {
    showToast(`Failed to pull the Tailscale auth key: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
    if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration'; }
  }
}

// ------------------------------------------------------------------ SMTP

function stopSmtpAutoRefresh() {
  if (smtpTimer) {
    clearInterval(smtpTimer);
    smtpTimer = null;
  }
  smtpInFlight = false;
}
function startSmtpAutoRefresh() {
  // Same reasoning as startVpnAutoRefresh: only ever patch the live region,
  // never the settings form above it, so in-progress edits survive a poll.
  stopSmtpAutoRefresh();
  smtpTimer = setInterval(async () => {
    if (document.hidden || smtpInFlight) return;
    if (window.location.hash !== "#admin/smtp") return;
    const liveNode = document.getElementById("smtpLive");
    if (!liveNode) return;
    smtpInFlight = true;
    try {
      const payload = await api("/admin/smtp");
      if (
        window.location.hash === "#admin/smtp" &&
        liveNode.isConnected &&
        document.getElementById("smtpLive") === liveNode &&
        !liveNode.contains(document.activeElement)
      ) {
        patchSmtpLive(payload);
      }
    } catch (err) {
      // Transient poll failure: leave the last good data in place silently.
    } finally {
      smtpInFlight = false;
    }
  }, 5000);
}

// Mirrors device/notifications.py's EVENT_TYPES/EVENT_TYPE_LABELS -- kept in
// sync by hand since the frontend has no shared-schema codegen with the
// stdlib backend.
const SMTP_EVENT_TYPES = [
  ["vpn_connected", "VPN connected"],
  ["vpn_disconnected", "VPN disconnected"],
  ["swarm_peer_connected", "Newly connected to swarm"],
  ["asset_added", "Asset added to a system"],
  ["asset_removed", "Asset removed from a system"],
  ["manual_control_submitted", "Manual control submitted"],
  ["automation_updated", "Automation setting updated"],
  ["asset_uploaded", "Asset uploaded (served to a peer)"],
  ["asset_downloaded", "Asset downloaded (pulled from a peer)"],
  ["torrent_completed", "Torrent download completed"],
  ["drone_updated", "Drone app updated"],
  ["config_backup_applied", "Config backup applied to this machine"],
  ["torrent_move_started", "Moving downloaded torrent files started"],
  ["torrent_move_resuming", "Moving downloaded torrent files resumed after interruption"],
  ["torrent_move_failed", "Moving downloaded torrent files failed"],
  ["torrent_move_finished", "Moving downloaded torrent files finished"],
];

function renderSmtpRevokedNotice(payload) {
  if (!payload.revoked_reason) return "";
  const when = payload.revoked_at ? ` (${escapeHtml(formatCompactLocalDate(payload.revoked_at))})` : "";
  return `<div class="alert alert-warning py-2 mb-3"><i class="bi bi-shield-exclamation me-1"></i><strong>Email credentials removed${when}:</strong> ${escapeHtml(payload.revoked_reason)}</div>`;
}

function renderSmtpTestNote(payload) {
  const result = payload.last_test_result;
  if (!result) return "";
  const when = payload.last_test_at ? escapeHtml(formatCompactLocalDate(payload.last_test_at)) : "";
  if (result.status === "ok") {
    return `<div class="small text-success mb-3"><i class="bi bi-check-circle me-1"></i>Test email sent successfully${when ? ` (${when})` : ""}.</div>`;
  }
  if (result.status === "queued") {
    return `<div class="small text-muted mb-3"><i class="bi bi-hourglass-split me-1"></i>Test email is queued for the backend mail worker${when ? ` (${when})` : ""}.</div>`;
  }
  if (result.status === "relayed") {
    return `<div class="small text-muted mb-3"><i class="bi bi-diagram-3 me-1"></i>Test email was relayed to the SMTP owner's backend worker${when ? ` (${when})` : ""}.</div>`;
  }
  return `<div class="small text-danger mb-3"><i class="bi bi-exclamation-triangle me-1"></i>Last test email failed${when ? ` (${when})` : ""}: ${escapeHtml(result.error || "unknown error")}</div>`;
}

function renderSmtpDigestNote(payload) {
  if (payload.delivery_mode === "relay" || payload.source_peer_id) {
    return `<div class="small text-muted mb-3"><i class="bi bi-diagram-3 me-1"></i>Automatic notifications are relayed by this Drone API worker to <strong>${escapeHtml(payload.source_peer_name || payload.source_peer_id || "the SMTP owner")}</strong>; no digest email is sent locally.</div>`;
  }
  if (payload.last_digest_error) {
    return `<div class="small text-danger mb-3"><i class="bi bi-exclamation-triangle me-1"></i>Last digest email failed: ${escapeHtml(payload.last_digest_error)}</div>`;
  }
  if (!payload.last_digest_sent_at) return `<div class="small text-muted mb-3">No digest email has been sent yet.</div>`;
  return `<div class="small text-muted mb-3"><i class="bi bi-envelope-check me-1"></i>Last digest email sent ${escapeHtml(formatCompactLocalDate(payload.last_digest_sent_at))}.</div>`;
}

// First mount only; patchSmtpLive never recreates these container nodes --
// same flash-free pattern as renderVpnLive/patchVpnLive.
function renderSmtpLive(payload) {
  return `
    <div id="smtpRevokedNotice">${renderSmtpRevokedNotice(payload)}</div>
    <div id="smtpTestNote">${renderSmtpTestNote(payload)}</div>
    <div id="smtpDigestNote">${renderSmtpDigestNote(payload)}</div>
  `;
}

function patchSmtpLive(payload) {
  const revokedNode = document.getElementById("smtpRevokedNotice");
  if (revokedNode) revokedNode.innerHTML = renderSmtpRevokedNotice(payload);
  const testNode = document.getElementById("smtpTestNote");
  if (testNode) testNode.innerHTML = renderSmtpTestNote(payload);
  const digestNode = document.getElementById("smtpDigestNote");
  if (digestNode) digestNode.innerHTML = renderSmtpDigestNote(payload);
}

async function renderSmtpPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Email";
  subtitleNode.textContent = "SMTP configuration, swarm sharing, and activity-digest notifications";
  setLoading(true, "Loading email settings...");
  let payload;
  try {
    payload = await api("/admin/smtp");
  } catch (err) {
    setLoading(false);
    content.innerHTML = `<div class="alert alert-danger">Failed to load email settings: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  const notify = payload.notify || {};
  const isRelay = payload.delivery_mode === "relay" || Boolean(payload.source_peer_id);
  const smtpOwner = escapeHtml(payload.source_peer_name || payload.source_peer_id || "the SMTP owner");
  content.innerHTML = `
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-envelope-gear me-2"></i>SMTP Settings</div>
      <div class="card-body">
        <p class="text-muted small">Used by the backend worker for activity digests, test messages, and backup attachments.</p>
        <div class="row g-2 mb-2">
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="smtpHost">Host</label>
            <input class="form-control form-control-sm" type="text" id="smtpHost" value="${escapeHtml(payload.host || "")}" placeholder="smtp.example.com">
          </div>
          <div class="col-sm-3 col-lg-2">
            <label class="form-label mb-1" for="smtpPort">Port</label>
            <input class="form-control form-control-sm" type="number" id="smtpPort" value="${escapeHtml(payload.port || 587)}">
          </div>
          <div class="col-sm-3 col-lg-2 d-flex align-items-end gap-3">
            <div class="form-check form-switch">
              <input class="form-check-input" type="checkbox" role="switch" id="smtpUseStarttls" ${payload.use_starttls ? "checked" : ""}>
              <label class="form-check-label small" for="smtpUseStarttls">STARTTLS</label>
            </div>
            <div class="form-check form-switch">
              <input class="form-check-input" type="checkbox" role="switch" id="smtpUseSsl" ${payload.use_ssl ? "checked" : ""}>
              <label class="form-check-label small" for="smtpUseSsl">SSL</label>
            </div>
          </div>
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="smtpUsername">Username</label>
            <input class="form-control form-control-sm" type="text" id="smtpUsername" autocomplete="off" value="${escapeHtml(payload.username || "")}">
          </div>
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="smtpPassword">Password</label>
            <input class="form-control form-control-sm" type="password" id="smtpPassword" autocomplete="off" placeholder="${payload.has_password ? "Saved (leave blank to keep)" : ""}">
          </div>
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="smtpFromAddress">From address</label>
            <input class="form-control form-control-sm" type="email" id="smtpFromAddress" value="${escapeHtml(payload.from_address || "")}">
          </div>
          <div class="col-sm-6 col-lg-4">
            <label class="form-label mb-1" for="smtpRecipientEmail">Send all mail to</label>
            <input class="form-control form-control-sm" type="email" id="smtpRecipientEmail" value="${escapeHtml(payload.recipient_email || "")}">
          </div>
        </div>
        <div class="d-flex flex-wrap gap-2 mt-2">
          <button class="btn btn-primary btn-sm" type="button" id="smtpSaveBtn" onclick="saveSmtpSettings()"><i class="bi bi-save me-1"></i>Save</button>
          <button class="btn btn-outline-secondary btn-sm" type="button" id="smtpTestBtn" ${payload.has_config ? "" : "disabled"} onclick="sendSmtpTestEmail()"><i class="bi bi-send me-1"></i>Send Test Email</button>
        </div>
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-body">
        ${isRelay ? `
        <div class="d-flex gap-2 align-items-start">
          <i class="bi bi-diagram-3 text-primary mt-1"></i>
          <div>
            <div class="fw-semibold">Relay-only notification delivery</div>
            <p class="text-muted small mb-0 mt-1">This Drone's API worker forwards notification events to <strong>${smtpOwner}</strong>. It never sends automatic digest mail itself, and the owner controls the delivery interval.</p>
          </div>
        </div>
        ` : `
        <div class="form-check form-switch">
          <input class="form-check-input" type="checkbox" role="switch" id="smtpEnabled" ${payload.smtp_enabled ? "checked" : ""} onchange="setSmtpEnabled(this.checked)">
          <label class="form-check-label" for="smtpEnabled">Send automatic digest mail from this SMTP owner</label>
        </div>
        <p class="text-muted small mb-0 mt-1">Controls the API worker's automatic digest job. Test Email remains an explicit API action.</p>
        `}
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-bell me-2"></i>Email Notifications</div>
      <div class="card-body">
        <div class="alert alert-info py-2 small"><i class="bi bi-hdd-network me-1"></i>All email—including digests, tests, and backup attachments—uses one persistent Drone API queue. The backend worker owns SMTP delivery; Web and Ports UIs only submit actions and display status.</div>
        ${isRelay ? `
        <p class="text-muted small mb-0">Digest frequency and event filters are managed on <strong>${smtpOwner}</strong>. This Drone relays every eligible backend event so the owner can apply those settings once for the combined fleet digest.</p>
        ` : `
        <p class="text-muted small mb-2">How often this SMTP owner's API worker checks for new local or relayed activity and sends one combined digest when anything qualifies.</p>
        <div class="row g-2 align-items-end mb-3">
          <div class="col-sm-4 col-lg-3">
            <label class="form-label mb-1" for="smtpDigestIntervalMinutes">Check every</label>
            <div class="input-group input-group-sm">
              <input class="form-control" type="number" id="smtpDigestIntervalMinutes" min="1" max="1440" step="1" value="${Math.max(1, Math.round((payload.digest_interval_seconds || 300) / 60))}">
              <span class="input-group-text">minutes</span>
            </div>
          </div>
          <div class="col-sm-4 col-lg-3">
            <button class="btn btn-outline-secondary btn-sm" type="button" id="smtpDigestIntervalSaveBtn" onclick="saveSmtpDigestInterval()"><i class="bi bi-save me-1"></i>Save</button>
          </div>
        </div>
        <p class="text-muted small mb-2">From 1 minute to 24 hours (1440 minutes); defaults to 5 minutes.</p>
        <hr>
        <p class="text-muted small">Which activity gets included the next time the digest email sends. Notifications always appear in the bell icon regardless of these toggles -- this only controls what's emailed.</p>
        <div class="row g-2">
          ${SMTP_EVENT_TYPES.map(([key, label]) => `
            <div class="col-sm-6 col-lg-4">
              <div class="form-check form-switch">
                <input class="form-check-input" type="checkbox" role="switch" id="smtpNotify_${escapeHtml(key)}" ${notify[key] ? "checked" : ""} onchange="setSmtpNotificationToggle('${escapeHtml(key)}', this.checked)">
                <label class="form-check-label small" for="smtpNotify_${escapeHtml(key)}">${escapeHtml(label)}</label>
              </div>
            </div>
          `).join("")}
        </div>
        `}
      </div>
    </div>
    <div class="card mb-3">
      <div class="card-header"><i class="bi bi-people me-2"></i>Share with Swarm</div>
      <div class="card-body">
        ${payload.source_peer_id ? `
        <p class="text-muted small mb-0"><i class="bi bi-info-circle me-1"></i>This configuration was imported from <strong>${escapeHtml(payload.source_peer_name || payload.source_peer_id)}</strong> and cannot be re-shared &mdash; only the drone that originally set it up can share it with the swarm.</p>
        ` : `
        <p class="text-muted small">Share these SMTP settings (including the password) with drones paired to this one, over the same cert-pinned peer link used for ROM/BIOS transfers -- never through the browser. Only paired drones can pull it, and only while this is turned on.</p>
        <div class="form-check form-switch mb-3">
          <input class="form-check-input" type="checkbox" role="switch" id="smtpSharingEnabled" ${payload.sharing_enabled ? "checked" : ""} onchange="setSmtpSharing(this.checked)">
          <label class="form-check-label" for="smtpSharingEnabled">Allow paired drones to pull this email configuration</label>
        </div>
        `}
        <hr>
        <p class="text-muted small mb-2">A drone with no email configuration of its own automatically adopts a sharing peer's settings on startup. Already sharing on another drone? Pull it here instead of typing your own.</p>
        <div class="d-flex flex-wrap align-items-end gap-2">
          <div>
            <label class="form-label mb-1" for="smtpPullPeer">Paired Drone</label>
            <select id="smtpPullPeer" class="form-select form-select-sm" style="min-width:220px"><option value="">Loading...</option></select>
          </div>
          <button class="btn btn-outline-primary btn-sm" type="button" id="smtpPullBtn" disabled onclick="pullSmtpConfigFromPeer()"><i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration</button>
        </div>
      </div>
    </div>
    <div class="card"><div class="card-body">
      <div id="smtpLive">${renderSmtpLive(payload)}</div>
    </div></div>
  `;
  startSmtpAutoRefresh();
  loadSmtpPullPeerOptions();
}

async function refreshSmtpLive() {
  try {
    const payload = await api("/admin/smtp");
    if (document.getElementById("smtpLive")) patchSmtpLive(payload);
  } catch (err) {
    showToast(`Failed to refresh email status: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

function _smtpSettingsPayloadFromForm() {
  return {
    host: (document.getElementById("smtpHost").value || "").trim(),
    port: parseInt(document.getElementById("smtpPort").value, 10) || 587,
    use_starttls: document.getElementById("smtpUseStarttls").checked,
    use_ssl: document.getElementById("smtpUseSsl").checked,
    username: (document.getElementById("smtpUsername").value || "").trim(),
    password: document.getElementById("smtpPassword").value || "",
    from_address: (document.getElementById("smtpFromAddress").value || "").trim(),
    recipient_email: (document.getElementById("smtpRecipientEmail").value || "").trim(),
  };
}

async function saveSmtpSettings() {
  const button = document.getElementById("smtpSaveBtn");
  if (button) button.disabled = true;
  try {
    await apiPost("/admin/smtp/settings", _smtpSettingsPayloadFromForm());
    showToast("Email settings saved.", "success");
    await renderSmtpPage();
  } catch (err) {
    showToast(`Failed to save email settings: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  } finally {
    if (button) button.disabled = false;
  }
}

async function sendSmtpTestEmail() {
  const button = document.getElementById("smtpTestBtn");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Queueing...'; }
  try {
    const result = await apiPost("/admin/smtp/test", {});
    if (result.status === "queued") {
      showToast("Test email queued. The backend mail worker will send it even if you leave this page.", "success", 7000);
    } else {
      showToast(`Test email could not be queued: ${escapeHtml(result.error || "unknown error")}`, "danger", 8000);
    }
  } catch (err) {
    showToast(`Test email failed: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
  } finally {
    if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-send me-1"></i>Send Test Email'; }
    await refreshSmtpLive();
  }
}

async function setSmtpEnabled(enabled) {
  const checkbox = document.getElementById("smtpEnabled");
  try {
    await apiPost("/admin/smtp/enabled", { enabled });
    showToast(`Automatic digest mail ${enabled ? "enabled" : "disabled"}.`, "success");
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function saveSmtpDigestInterval() {
  const input = document.getElementById("smtpDigestIntervalMinutes");
  const button = document.getElementById("smtpDigestIntervalSaveBtn");
  const minutes = parseInt(input.value, 10);
  if (!Number.isFinite(minutes) || minutes < 1 || minutes > 1440) {
    showToast("Enter a value between 1 minute and 1440 minutes (24 hours).", "danger");
    return;
  }
  if (button) button.disabled = true;
  try {
    await apiPost("/admin/smtp/digest-interval", { digest_interval_seconds: minutes * 60 });
    showToast("Digest email frequency saved.", "success");
  } catch (err) {
    showToast(`Failed to save digest frequency: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    if (button) button.disabled = false;
  }
}

async function setSmtpNotificationToggle(eventType, enabled) {
  const checkbox = document.getElementById(`smtpNotify_${eventType}`);
  try {
    await apiPost("/admin/smtp/notifications", { [eventType]: enabled });
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save notification setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function setSmtpSharing(enabled) {
  const checkbox = document.getElementById("smtpSharingEnabled");
  try {
    await apiPost("/admin/smtp/sharing", { enabled });
    showToast(`Email sharing with paired drones ${enabled ? "enabled" : "disabled"}.`, "success");
  } catch (err) {
    if (checkbox) checkbox.checked = !enabled;
    showToast(`Failed to save sharing setting: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function loadSmtpPullPeerOptions() {
  const select = document.getElementById("smtpPullPeer");
  const button = document.getElementById("smtpPullBtn");
  if (!select) return;
  try {
    const overview = await loadSwarmOverview();
    const onlinePeers = (overview.drones || []).filter(drone => !drone.is_self && drone.online);
    select.innerHTML = onlinePeers.length
      ? onlinePeers.map(drone => `<option value="${escapeHtml(drone.drone_id || "")}">${escapeHtml(drone.name || drone.hostname || drone.drone_id || "Drone")}</option>`).join("")
      : '<option value="">No paired drones online</option>';
    if (button) button.disabled = !onlinePeers.length;
  } catch (err) {
    select.innerHTML = '<option value="">Failed to load drones</option>';
    if (button) button.disabled = true;
  }
}

async function pullSmtpConfigFromPeer() {
  const select = document.getElementById("smtpPullPeer");
  const peerId = select ? select.value : "";
  if (!peerId) return;
  const button = document.getElementById("smtpPullBtn");
  if (button) { button.disabled = true; button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Pulling...'; }
  try {
    await apiPost("/admin/smtp/pull-from-peer", { peer_id: peerId });
    showToast("Pulled email configuration from peer.", "success");
    await renderSmtpPage();
  } catch (err) {
    showToast(`Failed to pull email configuration: ${escapeHtml(err.message || "unknown error")}`, "danger", 8000);
    if (button) { button.disabled = false; button.innerHTML = '<i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration'; }
  }
}

// ------------------------------------------------------------ Notifications

function stopNotificationsPoll() {
  if (notificationsPollTimer) {
    clearInterval(notificationsPollTimer);
    notificationsPollTimer = null;
  }
}

function startNotificationsPoll() {
  // Lighter cadence than the 3s admin-tile polls above -- this only drives a
  // badge count and runs on every page (not just while one tile is open), so
  // there's no need to match their tighter interval.
  stopNotificationsPoll();
  refreshNotificationsUnreadCount();
  notificationsPollTimer = setInterval(() => {
    if (document.hidden) return;
    refreshNotificationsUnreadCount();
  }, 20000);
}

async function refreshNotificationsUnreadCount() {
  try {
    const payload = await api("/admin/notifications/unread-count");
    const badge = document.getElementById("notificationsUnreadBadge");
    if (!badge) return;
    const count = payload.unread_count || 0;
    if (count > 0) {
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.classList.remove("d-none");
    } else {
      badge.classList.add("d-none");
    }
  } catch (err) {
    // Transient poll failure: leave the last known badge state in place.
  }
}

function renderNotificationItem(item) {
  return `
    <div class="notification-item ${item.read ? "is-read" : "is-unread"}" onclick="markNotificationRead(${item.id})">
      <span class="notification-unread-dot"></span>
      <div class="flex-grow-1">
        <div class="small fw-semibold">${escapeHtml(item.title)}</div>
        ${item.message ? `<div class="small text-muted">${escapeHtml(item.message)}</div>` : ""}
        <div class="small text-muted">${escapeHtml(formatCompactLocalDate(item.created_at))}</div>
      </div>
      <button type="button" class="notification-dismiss-btn" onclick="event.stopPropagation(); dismissNotification(${item.id})" aria-label="Dismiss notification"><i class="bi bi-x-lg"></i></button>
    </div>
  `;
}

// Populated when the dropdown opens (show.bs.dropdown, wired near
// brandHomeBtn's own listener) rather than kept live-polled -- a notification
// inbox doesn't need 3s freshness while closed, only the unread badge does.
async function refreshNotificationsDropdown() {
  const dropdown = document.getElementById("notificationsDropdown");
  if (!dropdown) return;
  try {
    const payload = await api("/admin/notifications?limit=20");
    const items = payload.items || [];
    const header = `
      <div class="notifications-dropdown-header">
        <strong>Notifications</strong>
        <div>
          <button type="button" class="btn btn-sm btn-link p-0 notifications-dismiss-all-btn" onclick="dismissAllNotifications()">Dismiss All</button>
        </div>
      </div>
      <div class="dropdown-divider"></div>
    `;
    dropdown.innerHTML = items.length
      ? header + items.map(renderNotificationItem).join("")
      : header + '<div class="notifications-empty text-muted small px-2 py-3">No notifications yet.</div>';
  } catch (err) {
    dropdown.innerHTML = `<div class="text-danger small px-2 py-3">Failed to load notifications: ${escapeHtml(err.message || "unknown error")}</div>`;
  }
}

async function markNotificationRead(id) {
  try {
    await apiPost(`/admin/notifications/${id}/read`, {});
  } catch (err) {
    // Ignore -- it'll just still show as unread next time the dropdown opens.
  }
  if (notificationsDropdownOpen) await refreshNotificationsDropdown();
  await refreshNotificationsUnreadCount();
}

async function dismissNotification(id) {
  try {
    await apiPost(`/admin/notifications/${id}/dismiss`, {});
  } catch (err) {
    showToast(`Failed to dismiss notification: ${escapeHtml(err.message || "unknown error")}`, "danger");
    return;
  }
  await refreshNotificationsDropdown();
  await refreshNotificationsUnreadCount();
}

// Backend endpoint/store function are still named "clear" (see
// handlers_notifications.py) -- kept as-is, this is just the UI-facing name
// matching the per-item dismissNotification() terminology above.
async function dismissAllNotifications() {
  try {
    await apiPost("/admin/notifications/clear", {});
  } catch (err) {
    showToast(`Failed to dismiss notifications: ${escapeHtml(err.message || "unknown error")}`, "danger");
    return;
  }
  await refreshNotificationsDropdown();
  await refreshNotificationsUnreadCount();
}

async function purgeAssetCache() {
  if (!window.confirm(
    "Purge the asset cache and force a full re-scan?\n\n" +
    "Cached fingerprint values are kept, so ROMs are not re-fingerprinted. This clears stale or " +
    "duplicate entries and rebuilds the ROM list from a fresh full inventory."
  )) {
    return;
  }
  try {
    const result = await apiPost("/admin/asset-cache/purge", {});
    showToast(result.message || "Asset cache purge queued.", "success");
    if (window.location.hash === "#admin/controls" && typeof window.refreshSystemInfoAssetCache === "function") {
      await window.refreshSystemInfoAssetCache();
    } else {
      await renderAssetCachePage();
    }
  } catch (err) {
    showToast(`Failed to purge asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

async function clearPendingAssetChanges() {
  if (!window.confirm(
    "Clear all pending asset changes waiting to be cached?\n\n" +
    "This keeps the local asset cache and files, but discards the unprocessed change queue. " +
    "Discarded changes will reappear only if a later scan detects them again."
  )) {
    return;
  }
  try {
    const result = await apiPost("/admin/asset-cache/clear-pending", {});
    showToast(result.message || "Pending asset changes cleared.", "success");
    if (window.location.hash === "#admin/controls" && typeof window.refreshSystemInfoAssetCache === "function") {
      await window.refreshSystemInfoAssetCache();
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
  const statusText = payload.active ? "Scanning" : payload.needs_upload ? "Processing Pending" : payload.uploaded ? "Current" : "Waiting";
  const stage = payload.active ? 1 : payload.needs_upload ? 2 : payload.uploaded ? 3 : 0;
  const metric = (label, value, icon, tone = "") => `<div class="asset-metric ${tone}"><i class="bi ${icon}"></i><div><strong>${Number(value || 0).toLocaleString()}</strong><span>${escapeHtml(label)}</span></div></div>`;
  const detail = (label, value) => `<div class="asset-detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "n/a")}</strong></div>`;
  const step = (number, label, text) => `<div class="asset-flow-step ${stage === number ? "active" : stage > number ? "complete" : ""}"><span>${stage > number ? '<i class="bi bi-check-lg"></i>' : number}</span><div><strong>${label}</strong><small>${text}</small></div></div>`;
  return `
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
      <div class="asset-cache-status-line">
        <span class="badge ${statusClass}">${escapeHtml(statusText)}</span>
        <span class="small text-muted">${pendingTotal.toLocaleString()} pending change${pendingTotal === 1 ? "" : "s"} waiting to be cached</span>
      </div>
      ${includeActions ? `<div class="d-flex flex-wrap gap-2">
        <button class="btn btn-sm btn-outline-primary" onclick="renderAssetCachePage()"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
        <button class="btn btn-sm btn-outline-warning" onclick="clearPendingAssetChanges()" ${pendingTotal ? "" : "disabled"}><i class="bi bi-x-circle me-1"></i>Clear Pending</button>
        <button class="btn btn-sm btn-outline-danger" onclick="purgeAssetCache()">Purge Cache &amp; Resync</button>
      </div>` : ""}
    </div>
    ${pendingTotal ? `<div class="asset-cache-help mb-3"><strong>What this means:</strong> Drone has local asset changes queued for the next cache pass. Refresh after the next scan completes. If these are stale or duplicated queue entries, use <strong>Clear Pending</strong> to discard the unsent queue without deleting local cache data.</div>` : ""}
    <div class="asset-flow mb-3">
      ${step(1, "Scan", payload.active ? "Reading local assets now" : `Last scan: ${dateText(payload.last_full_scan_at)}`)}
      ${step(2, "Queue", pendingTotal ? `${pendingTotal.toLocaleString()} changes waiting` : "No changes waiting")}
      ${step(3, "Sync", payload.uploaded && !payload.needs_upload ? "Cache is current" : `Last synced: ${dateText(payload.last_successful_upload_at)}`)}
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
  clearSystemTheme();
  titleNode.textContent = "Asset Cache";
  subtitleNode.textContent = "ROM, BIOS, artwork cache and scan state";
  setLoading(true, "Loading asset cache...");
  try {
    const payload = await api("/admin/asset-cache");
    content.innerHTML = `
      <div class="card log-card"><div class="card-body">${renderAssetCachePanel(payload)}</div></div>
    `;
  } catch (err) {
    showToast(`Failed to load asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
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
// Metadata-only field names (GAMELIST_EDIT_FIELDS minus the artwork/video fields,
// which get their own upload widgets in artworkEditableImageFields instead).
const ROM_MEDIA_ARTWORK_FIELDS = new Set(["image", "thumbnail", "marquee", "fanart", "boxart", "video"]);
function romMetadataEditFormHtml(rom) {
  const details = rom && rom.gamelist ? rom.gamelist : {};
  const knownMetadataFields = GAMELIST_EDIT_FIELDS.filter((field) => !ROM_MEDIA_ARTWORK_FIELDS.has(field));
  const editableFields = Array.from(new Set(
    knownMetadataFields.concat(Object.keys(details).filter((key) => key !== "path" && !ROM_MEDIA_ARTWORK_FIELDS.has(key)))
  ));
  const removeBtn = rom.has_gamelist_entry
    ? `<button class="btn btn-sm btn-outline-danger" type="button" onclick="removeRomMediaGamelistEntry()"><i class="bi bi-trash me-1"></i>Remove gamelist entry</button>`
    : "";
  return `
    <form id="romMediaGamelistForm" class="compact-edit">
      <div class="gamelist-edit-grid">
        ${editableFields.map((field) => artworkGamelistFieldControl(field, details[field])).join("")}
      </div>
      <div class="d-flex gap-2 mt-2">
        <button class="btn btn-sm btn-primary" type="button" onclick="saveRomMediaMetadata()">Save Metadata</button>
        ${removeBtn}
      </div>
    </form>
  `;
}
async function saveRomMediaMetadata() {
  const rom = (window.missingArtworkRoms || [])[0];
  if (!rom) return;
  const fields = {};
  document.querySelectorAll("#romMediaGamelistForm .gamelist-edit-field").forEach((node) => {
    const field = node.getAttribute("data-gamelist-field");
    if (field) fields[field] = node.value || "";
  });
  setLoading(true, "Saving metadata...");
  try {
    await apiPost("/admin/artwork/gamelist/update", { system: rom.system, rom_path: rom.rom_path, fields });
    showToast(`Saved metadata for ${escapeHtml(rom.title || rom.name || "ROM")}.`, "success");
    await renderRomMediaPage(rom.system, rom.unique_id);
  } catch (err) {
    showToast(`Metadata update failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
}
async function removeRomMediaGamelistEntry() {
  const rom = (window.missingArtworkRoms || [])[0];
  if (!rom) return;
  const label = rom.title || rom.name || rom.rom_path || "this ROM";
  if (!window.confirm(`Remove "${label}" from gamelist.xml? The ROM file itself will not be deleted, but all of its metadata and artwork references (including any video) will be cleared.`)) return;
  setLoading(true, "Removing gamelist entry...");
  try {
    await apiPost("/admin/artwork/gamelist/remove", { system: rom.system, rom_path: rom.rom_path });
    showToast(`Removed ${escapeHtml(label)} from gamelist.xml.`, "success");
    await renderRomMediaPage(rom.system, rom.unique_id);
  } catch (err) {
    showToast(`Remove failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
  }
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
  const myArtworkRequestId = ++artworkRenderRequestId;
  setLoading(true, "Updating artwork results...");
  try {
    const payload = await api(artworkPayloadUrl(forceRefresh));
    if (myArtworkRequestId !== artworkRenderRequestId) return; // superseded -- see artworkRenderRequestId
    updateArtworkPageFromPayload(payload);
    history.replaceState(null, "", artworkHash());
  } catch (err) {
    if (myArtworkRequestId !== artworkRenderRequestId) return; // superseded -- see artworkRenderRequestId
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
  const myArtworkRequestId = ++artworkRenderRequestId;
  titleNode.textContent = "Artwork & Metadata";
  subtitleNode.textContent = "Manage gamelist.xml artwork, metadata, imports, uploads, and marquee crops";
  artworkIncludeFilesystem = !!includeFilesystem;
  artworkCurrentOffset = Math.max(0, Number(offset || 0));
  artworkSelectedFields = fields && fields.length ? fields : ["any"];
  artworkSelectedSystems = systems || [];
  artworkFilterQuery = query || "";
  artworkRomStatus = ["any", "exists", "missing"].includes(romStatus) ? romStatus : "any";
  syncArtworkHash();
  clearSystemTheme();
  // Paint the shell (tab bar + a scoped spinner) immediately, before the
  // scan below -- previously nothing rendered into `content` until the scan
  // resolved (which can take several seconds on a large ROM library),
  // leaving whatever page you navigated from just sitting there looking
  // stuck, with only a toast to say otherwise, for the whole wait. The tab
  // bar being live right away also means Movies/Theme are one click away
  // without waiting on this page's own scan at all.
  content.innerHTML = `
    ${renderArtworkTabBar("metadata")}
    <div class="text-muted small py-5 text-center">
      <span class="spinner-border spinner-border-sm me-2"></span>${includeFilesystem ? "Scanning ROM directories..." : "Scanning gamelists..."}
    </div>
  `;
  refreshRandomThemeLogo().catch(() => {});
  try {
    const payload = await api(artworkPayloadUrl(forceRefresh));
    if (myArtworkRequestId !== artworkRenderRequestId) return; // superseded -- see artworkRenderRequestId
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
      ${renderArtworkTabBar("metadata")}
      <div class="mb-3 d-flex flex-wrap gap-2">
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
                <table class="table table-sm table-hover align-middle mb-0 bff-stack">
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
    if (myArtworkRequestId !== artworkRenderRequestId) return; // superseded -- see artworkRenderRequestId
    showToast(`Failed to scan artwork: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      ${renderArtworkTabBar("metadata")}
      <div class="alert alert-danger">Failed to load artwork data: ${escapeHtml(err.message || "unknown error")}</div>
    `;
  }
}
const LAUNCHBOX_METADATA_FIELDS = [
  "name", "desc", "genre", "developer", "publisher", "releasedate",
  "players", "rating", "favorite", "hidden", "kidgame", "adult",
  "image", "thumbnail", "marquee", "fanart", "boxart", "video",
  "platform", "esrb", "overview", "playmode", "regional", "favorites"
];
function artworkImageUploadHtml(rom, field) {
  const isVideo = field === "video";
  const existingValue = rom.existing && rom.existing[field] ? rom.existing[field] : null;
  const existingUrl = artworkExistingAssetUrl(rom, field, existingValue);
  const existingDisplay = existingValue ? `<span class="text-muted small artwork-upload-status">has ${escapeHtml(field)}</span>` : `<span class="text-muted small artwork-upload-status">no ${escapeHtml(field)}</span>`;
  const viewBtn = existingUrl
    ? `<button class="btn btn-sm btn-outline-secondary btn-icon artwork-view-btn" type="button" data-image-url="${escapeHtml(existingUrl)}" data-image-title="${escapeHtml(field)}" data-is-video="${isVideo ? "1" : "0"}" title="View existing ${escapeHtml(field)}"><i class="bi bi-eye"></i></button>`
    : `<button class="btn btn-sm btn-outline-secondary btn-icon" type="button" disabled title="No ${escapeHtml(field)} to view"><i class="bi bi-eye-slash"></i></button>`;
  return `
    <div class="artwork-upload-item">
      <span class="artwork-upload-label" title="${escapeHtml(field)}">${escapeHtml(field)}</span>
      <input type="file" class="form-control form-control-sm artwork-upload-file" accept="${isVideo ? "video/*" : "image/*"}" data-field="${escapeHtml(field)}">
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
// Unlike images (looked up by guessed filename -- see artworkExistingImageUrl),
// video files land in either images/ or videos/ depending on how they arrived
// (manual upload vs. P2P peer sync), so the backend resolves the real gamelist
// <video> reference by rom_path instead of a guessed filename.
function artworkExistingVideoUrl(rom, value) {
  if (!String(value || "").trim()) return "";
  const system = rom.system || "";
  const romPath = rom.rom_path || "";
  if (!system || !romPath) return "";
  return `${API_BASE}/public/systems/${encodeURIComponent(system)}/video/${encodeURIComponent(romPath)}`;
}
function artworkExistingAssetUrl(rom, field, value) {
  return field === "video" ? artworkExistingVideoUrl(rom, value) : artworkExistingImageUrl(rom, value);
}
function romVideoUrl(rom) {
  return artworkExistingVideoUrl(rom, rom && rom.existing && rom.existing.video);
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
      const url = button.getAttribute("data-image-url") || "";
      const title = button.getAttribute("data-image-title") || "";
      if (button.getAttribute("data-is-video") === "1") {
        showVideoLightbox(url, title);
      } else {
        showImageLightbox(url, title);
      }
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
  if (!file) { showToast(`Please select a ${field === "video" ? "video" : "image"} file first.`, "warning"); return; }
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
    // This same upload flow is shared by the Artwork admin page and the ROM
    // Details page -- refresh whichever one is actually showing this rom.
    if (document.getElementById("selectedArtworkRom")) {
      await selectArtworkRom(window.selectedArtworkRomIndex, "edit");
    } else if (rom.system && rom.unique_id) {
      await renderRomMediaPage(rom.system, rom.unique_id);
    }
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
    if (data.launchbox_unavailable) {
      matchesEl.innerHTML = `<div class="text-muted">LaunchBox could not be reached from this Drone. You can still use the external LaunchBox link or TheGamesDB matches below.</div>`;
    } else {
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
    }
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
let localPeerAssetContext = {
  peerId: "",
  peerName: "",
  assetType: "roms",
  systems: [],
  availableSystems: [],
  systemCounts: {},
  systemsLoadedPeerId: "",
  items: [],
  query: "",
  limit: 50,
  offset: 0,
  total: 0,
};
function localPeerStatusBadge(peer) {
  if (peer.identity_conflict) return '<span class="badge text-bg-danger">Identity Conflict</span>';
  if (peer.tailnet_forgotten) return '<span class="badge text-bg-secondary">Forgotten</span>';
  if (peer.tailnet_device) return '<span class="badge text-bg-success">Connected</span>';
  if (peer.tailnet_pair_error) return '<span class="badge text-bg-warning">Pairing failed</span>';
  if (!peer.paired) return '<span class="badge text-bg-warning">Discovered</span>';
  const health = peer.health || {};
  if (health.status === "pass") return '<span class="badge text-bg-success">Paired · Online</span>';
  if (health.status === "fail") return '<span class="badge text-bg-danger">Paired · Offline</span>';
  return '<span class="badge text-bg-info">Paired</span>';
}

function renderLocalPeerRows(peers) {
  if (!peers.length) return '<div class="themed-empty">No nearby Drones discovered yet.</div>';
  return `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table bff-stack">
    <thead><tr><th>Drone</th><th>Drone ID</th><th>Source</th><th>Status</th><th>Error</th><th>Address</th><th>Last Seen</th><th>Certificate</th><th></th></tr></thead>
    <tbody>${peers.map(peer => {
      const rawPeerId = String(peer.drone_id || "");
      const peerId = escapeHtml(rawPeerId);
      const peerToken = encodeURIComponent(rawPeerId).replace(/'/g, "%27");
      // A peer advertising a non-HTTPS URL (or no certificate) can't do the
      // certificate-verified mTLS transfer; flag it instead of offering Pair.
      const url = String(peer.reachable_url || "");
      const insecure = !peer.paired && url !== "" && !/^https:/i.test(url);
      let actionCell;
      // A never-paired row can always be manually cleared -- offered
      // alongside whatever the primary action is, everywhere except the two
      // tailnet-lifecycle branches below (their visibility is driven by the
      // live tailnet device/forgotten-peer sync, not this discovered-peers
      // store, so dismissing here wouldn't actually make them go away).
      const dismissBtn = `<button class="btn btn-sm btn-outline-secondary" onclick="dismissLocalPeer(decodeURIComponent('${peerToken}'))" title="Remove from Nearby Drones. It will reappear if this address announces itself again.">Dismiss</button>`;
      if (peer.identity_conflict) {
        actionCell = `<div class="d-flex gap-2 justify-content-end"><button class="btn btn-sm btn-outline-secondary" disabled title="This Drone advertises the same machine id as this device. Reset the Drone id on one machine before pairing.">Resolve ID</button>${dismissBtn}</div>`;
      } else if (peer.paired) {
        actionCell = `<div class="d-flex gap-2 justify-content-end"><button class="btn btn-sm btn-outline-primary" onclick="swarmBrowsePeerAssets(decodeURIComponent('${peerToken}'))">Browse</button><button class="btn btn-sm btn-outline-danger" onclick="forgetLocalPeer(decodeURIComponent('${peerToken}'))">Forget</button></div>`;
      } else if (peer.tailnet_forgotten || peer.tailnet_pair_error) {
        actionCell = `<button class="btn btn-sm btn-outline-primary" onclick="restoreTailnetPeer(decodeURIComponent('${peerToken}'))">${peer.tailnet_forgotten ? "Restore" : "Retry"}</button>`;
      } else if (peer.tailnet_device) {
        actionCell = '<span class="small text-muted">Not a Drone</span>';
      } else if (insecure) {
        actionCell = `<div class="d-flex gap-2 justify-content-end"><button class="btn btn-sm btn-outline-secondary" disabled title="This Drone is advertising ${escapeHtml(url)} (not HTTPS), so it can't be paired for secure transfers. Update/repair the Drone on that machine.">Not secure</button>${dismissBtn}</div>`;
      } else {
        actionCell = `<div class="d-flex gap-2 justify-content-end"><button class="btn btn-sm btn-outline-primary" onclick="pairLocalPeer(decodeURIComponent('${peerToken}'))">Pair</button>${dismissBtn}</div>`;
      }
      return `<tr>
        <td><strong>${escapeHtml(peer.name || peer.hostname || peerId)}</strong>${insecure ? '<span class="badge text-bg-danger ms-2" title="Not running HTTPS — cannot pair">Not secure</span>' : ""}${peer.identity_conflict ? '<span class="badge text-bg-danger ms-2" title="This peer is advertising the same Drone id as this machine">Same ID</span>' : ""}</td>
        <td class="small mono">${peerId}</td>
        <td><span class="badge text-bg-${peer.source === "Local Network" ? "info" : "primary"}">${escapeHtml(peer.source || "Local Network")}</span></td>
        <td>${localPeerStatusBadge(peer)}</td>
        <td class="small text-danger">${escapeHtml(peer.identity_conflict ? `Conflicts with ${peer.conflicting_drone_id || "this Drone id"}` : (peer.health?.failure_reason || peer.tailnet_pair_error || peer.tailnet_probe_error || ""))}</td>
        <td class="small mono">${escapeHtml(peer.reachable_url || peer.tailnet_ip || peer.source_ip || "n/a")}</td>
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

const LOCAL_TRANSFERABLE_TYPES = new Set(["roms", "bios", "saves", "movies", "config_backups"]);
// Movies and config backups have no system or artwork association at all
// (unlike ROMs/BIOS/saves), so the Systems filter is meaningless for them and
// gets grayed out instead of just staying populated-but-inert like it does
// for the other flat types.
const LOCAL_SYSTEMLESS_TYPES = new Set(["movies", "config_backups"]);

function localAssetNativeLabel(key) {
  return String(key || "")
    .replace(/^is_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, char => char.toUpperCase());
}

function localAssetNativeValue(key, value) {
  if (value === null || value === undefined || value === "") return "";
  if (String(key || "").endsWith("_at")) return formatCompactLocalDate(value);
  if (String(key || "").includes("duration") && !Number.isNaN(Number(value))) return formatDuration(value);
  if (String(key || "").includes("size") || String(key || "").includes("byte_count")) return formatBytes(value);
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "object") return Object.entries(value)
    .map(([entryKey, entryValue]) => `${localAssetNativeLabel(entryKey)}: ${localAssetNativeValue(entryKey, entryValue)}`)
    .join("\n");
  return String(value);
}

function localAssetDetailRows(item) {
  const hidden = new Set(["absolute_path", "content", "content_truncated", "is_downloadable"]);
  return Object.entries(item || {})
    .filter(([key, value]) => !hidden.has(key) && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `<tr><th class="text-nowrap">${escapeHtml(localAssetNativeLabel(key))}</th><td class="mono text-wrap">${escapeHtml(localAssetNativeValue(key, value))}</td></tr>`)
    .join("");
}

function showLocalAssetDetails(index) {
  const item = localPeerAssetContext.items[index];
  if (!item) return;
  const modalId = "localAssetDetailsModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  const content = item.content
    ? `<pre class="local-asset-native-content">${escapeHtml(item.content)}</pre>${item.content_truncated ? '<div class="small text-warning mt-2">Content was truncated by the target Drone.</div>' : ""}`
    : `<div class="table-responsive"><table class="table table-sm themed-table local-asset-details-table mb-0"><tbody>${localAssetDetailRows(item) || '<tr><td>No details reported by target Drone.</td></tr>'}</tbody></table></div>`;
  modal.innerHTML = `
    <div class="modal-dialog modal-xl modal-dialog-scrollable">
      <div class="modal-content local-asset-details-modal">
        <div class="modal-header">
          <div>
            <h5 class="modal-title mb-0">${escapeHtml(localAssetDisplayName(item))}</h5>
            <div class="small text-muted">${escapeHtml(localAssetPath(item) || item.relative_path || item.root_name || "")}</div>
          </div>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">${content}</div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

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
  return systemBar + `<div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table local-assets-table bff-stack">
    <thead><tr><th>Name</th><th>Path</th><th>System / Source</th><th>Size</th><th>Details</th><th></th></tr></thead>
    <tbody>${localPeerAssetContext.items.map((item, index) => {
      const exists = isRoms && item.exists_locally === true;
      const statusBadge = isRoms
        ? (exists
            ? '<span class="badge text-bg-success ms-2" title="This ROM is already on this machine (matched by thumbprint)">On this machine</span>'
            : '<span class="badge text-bg-info ms-2">New</span>')
        : "";
      // ROM rows use a compact icon-only button to keep the table tight; an
      // existing ROM is not re-downloaded but the button still copies its artwork.
      const romBtn = `<button class="btn btn-sm ${exists ? "btn-outline-primary" : "btn-primary"}" title="${exists ? "On this machine — copy its artwork" : "Download"}" aria-label="${exists ? "Copy artwork" : "Download"}" onclick="copyLocalPeerAsset(${index})"><i class="bi ${exists ? "bi-images" : "bi-cloud-arrow-down"}"></i></button>`;
      const otherBtn = `<button class="btn btn-sm btn-primary" title="Copy here" aria-label="Copy here" onclick="copyLocalPeerAsset(${index})"><i class="bi bi-cloud-arrow-down"></i></button>`;
      const detailsBtn = `<button class="btn btn-sm btn-outline-primary" title="View details" aria-label="View details" onclick="showLocalAssetDetails(${index})"><i class="bi bi-eye"></i></button>`;
      return `<tr>
      <td><strong>${escapeHtml(localAssetDisplayName(item))}</strong>${statusBadge}</td>
      <td class="small mono">${escapeHtml(localAssetPath(item))}</td>
      <td>${escapeHtml(item.system || item.root_name || localPeerAssetContext.systems.join(", "))}</td>
      <td>${formatBytes(item.byte_count || item.file_size || item.size)}</td>
      <td class="small">${escapeHtml(localAssetDetail(item) || String(item.rom_fingerprint || item.bios_md5 || item.saves_fingerprint || item.fingerprint || item.md5 || "").slice(0, 16))}</td>
      <td>${transferable
        ? (isRoms ? romBtn : otherBtn)
        : detailsBtn}</td>
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

// The Integration page is retired: Overmind integration is disabled (the
// fleet is Overmind-free) and the Local Network configuration moved to the
// Swarm page. #admin/integration redirects there in router().

async function renderTransfersPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Transfers";
  subtitleNode.textContent = "Request and monitor drone-to-drone asset transfers";
  setLoading(true, "Loading transfers...");
  try {
    content.innerHTML = `
      ${renderSwarmTabBar("transfers")}
      <div class="mb-3 d-flex flex-wrap justify-content-end gap-2">
        <button class="btn btn-outline-primary" onclick="setHash('#admin/transfers')"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
      </div>
      <div id="integrationTransfersPanel"></div>`;
    await renderIntegrationTransfersPanel(document.getElementById("integrationTransfersPanel"));
    startTransfersAutoRefresh();
  } catch (err) {
    showToast(`Failed to load transfers: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = '<div class="themed-empty">Transfers could not be loaded.</div>';
  } finally {
    setLoading(false);
  }
}

function _networkShareStatusBadge(share) {
  const status = share.status;
  const transport = String(share.protocol || "").toUpperCase();
  const transportSuffix = transport ? ` via ${transport}` : "";
  const style = status === "mounted"
    ? "background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)"
    : status === "peer_unreachable" || status === "error"
      ? "background:rgba(251,191,36,0.15);color:#fbbf24;border-color:rgba(251,191,36,0.4)"
      : "background:rgba(148,163,184,0.15);color:#94a3b8;border-color:rgba(148,163,184,0.4)";
  const label = status === "mounted"
    ? (share.bios_status === "syncing" || share.bios_status === "pending" ? `Referencing${transportSuffix} (BIOS syncing)` : `Referencing${transportSuffix}`)
    : status === "detaching"
      ? "Detaching..."
      : status === "peer_unreachable"
        ? "Referencing (unreachable)"
        : status === "error"
          ? (share.enabled === false ? "Detach needs retry" : "Referencing (error)")
          : "Referencing...";
  const fallback = share.transport_fallback_detail ? ` NFS fallback reason: ${share.transport_fallback_detail}` : "";
  return `<span class="badge" style="${style}" title="${escapeHtml(share.status_detail || `This Drone is referencing this peer's ROM library${transportSuffix}.${fallback}`)}"><i class="bi bi-hdd-network me-1"></i>${escapeHtml(label)}</span>`;
}

function renderSwarmDroneCard(drone) {
  const summary = drone.summary || {};
  const droneToken = encodeURIComponent(String(drone.drone_id || "")).replace(/'/g, "%27");
  const counts = summary.counts || {};
  const systems = Array.isArray(summary.systems) ? summary.systems : [];
  const share = swarmNetworkSharesByPeer[String(drone.drone_id || "")];
  const badge = drone.is_self
    ? '<span class="badge text-bg-info">This Drone</span>'
    : drone.online
      ? '<span class="badge text-bg-success">Online</span>'
      : '<span class="badge text-bg-secondary">Offline</span>';
  const latency = !drone.is_self && drone.online && drone.latency_ms != null
    ? `<span class="small text-muted">${Number(drone.latency_ms)} ms</span>`
    : "";
  const addressLines = [];
  if (drone.tailnet_ip) {
    const tailnetShort = tailnetShortHostname(drone.dns_name);
    const tailnetLabel = tailnetShort ? `https://${tailnetShort}` : drone.tailnet_ip;
    addressLines.push(`<div class="small text-truncate"><i class="bi bi-diagram-3 me-1" aria-hidden="true"></i><button type="button" class="btn btn-link btn-sm p-0 align-baseline" onclick="openTailnetPeerModal(decodeURIComponent('${droneToken}'))" title="View Tailnet details">${escapeHtml(tailnetLabel)}</button></div>`);
  }
  const lanUrl = drone.advertised_reachable_url || drone.reachable_url;
  if (lanUrl) {
    addressLines.push(`<div class="small text-truncate"><i class="bi bi-house me-1" aria-hidden="true"></i><span class="text-muted">${escapeHtml(lanUrl)}</span></div>`);
  }
  if (share) {
    const skippedCount = Number(share.skipped_count || 0);
    const referencedCount = Number(share.system_count || 0);
    const biosLinkCount = Number(share.bios_link_count || 0);
    const countNote = referencedCount || biosLinkCount ? ` -- ${referencedCount} systems, ${biosLinkCount} BIOS links` : "";
    const skippedNote = skippedCount ? `; ${skippedCount} local item${skippedCount === 1 ? "" : "s"} kept` : "";
    addressLines.push(`<div class="small text-truncate">${_networkShareStatusBadge(share)}<span class="text-muted">${escapeHtml(countNote + skippedNote)}</span></div>`);
  }
  const stats = drone.online && drone.summary
    ? `<div class="d-flex flex-wrap gap-3 small mt-2">
        <span><strong>${Number(counts.roms || 0)}</strong> ROMs</span>
        <span><strong>${Number(counts.bios || 0)}</strong> BIOS</span>
        <span><strong>${Number(counts.artwork || 0)}</strong> artwork</span>
        <span><strong>${systems.length}</strong> systems</span>
      </div>`
    : drone.online && drone.summary_error
      ? `<div class="small text-warning mt-2"><i class="bi bi-hourglass-split me-1" aria-hidden="true"></i>Online; inventory is delayed: ${escapeHtml(drone.summary_error)}</div>`
    : drone.error
      ? `<div class="small text-warning mt-2"><i class="bi bi-exclamation-triangle me-1" aria-hidden="true"></i>${escapeHtml(drone.error)}</div>`
      : "";
  const networkShareButton = share
    ? `<button class="btn btn-sm btn-outline-secondary" onclick="swarmUnreferencePeerRoms(decodeURIComponent('${droneToken}'), ${jsAttr(drone.name || drone.drone_id || "")})" ${share.status === "detaching" ? "disabled" : ""}><i class="bi bi-x-circle me-1"></i>${share.status === "detaching" ? "Detaching..." : share.enabled === false ? "Retry Detach" : "Stop Referencing"}</button>`
    : `<button class="btn btn-sm btn-outline-info" onclick="swarmReferencePeerRoms(decodeURIComponent('${droneToken}'), ${jsAttr(drone.name || drone.drone_id || "")})" ${drone.online && (drone.tailnet_ip || lanUrl) ? "" : "disabled"} title="${drone.tailnet_ip || lanUrl ? "Reference this peer's ROM library and missing BIOS files over read-only NFSv4, with SMB compatibility fallback" : "Requires a known LAN or Tailscale address"}"><i class="bi bi-hdd-network me-1"></i>Reference ROMs</button>`;
  const actions = drone.is_self
    ? ""
    : `<div class="d-flex flex-wrap gap-2 mt-3">
        ${networkShareButton}
        <button class="btn btn-sm btn-outline-success" onclick="swarmBrowsePeerAssets(decodeURIComponent('${droneToken}'))" ${drone.online ? "" : "disabled"}><i class="bi bi-cloud-arrow-down me-1"></i>Request Assets</button>
        <button class="btn btn-sm btn-outline-danger" onclick="forgetLocalPeer(decodeURIComponent('${droneToken}'))"><i class="bi bi-x-circle me-1"></i>Forget</button>
      </div>`;
  return `
    <div class="col"><div class="card log-card h-100">
      <div class="card-header d-flex justify-content-between align-items-center gap-2">
        <span class="text-truncate"><i class="bi bi-hdd-network me-2" aria-hidden="true"></i>${escapeHtml(drone.name || drone.drone_id || "Drone")}</span>
        <span class="d-flex align-items-center gap-2">${latency}${badge}</span>
      </div>
      <div class="card-body">
        ${addressLines.join("") || '<div class="small text-muted">No address recorded.</div>'}
        ${stats}
        ${actions}
      </div>
    </div></div>`;
}

function swarmBrowsePeerAssets(peerId) {
  localPeerAssetContext.peerId = String(peerId || "");
  setHash("#admin/transfers");
}

async function swarmReferencePeerRoms(peerId, peerName) {
  const confirmed = window.confirm(
    `Reference ${peerName}'s whole ROM library and BIOS folder over the network?\n\n` +
    `Every system and BIOS file it has will be symlinked in here -- games and ` +
    `emulators read bytes live from ${peerName}, not from a local copy.\n\n` +
    `If a ROM system already exists locally, it is renamed aside with an ` +
    `".old" suffix (never deleted) and restored when the reference is disabled. ` +
    `Existing local BIOS files stay in place; the network only supplies missing BIOS.`
  );
  if (!confirmed) return;
  try {
    setLoading(true, `Referencing ${peerName}'s ROMs and BIOS...`);
    const result = await apiPost(`/admin/network-shares/${encodeURIComponent(peerId)}/enable`, {});
    if (result.status === "enabling" || result.status === "pending") {
      showToast(
        `Reference accepted for ${escapeHtml(peerName)}. Drone is mounting it in the background; the operation continues if this browser closes.`,
        "info",
        10000,
      );
    } else if (result.status !== "mounted") {
      showToast(`Could not reference ${escapeHtml(peerName)}: ${escapeHtml(result.status_detail || "mount failed")}`, "danger");
    } else {
      const transport = String(result.protocol || "network").toUpperCase();
      showToast(`Now referencing ${escapeHtml(peerName)}'s ROMs and BIOS via ${escapeHtml(transport)}`, "success");
    }
  } catch (err) {
    showToast(`Could not reference ${escapeHtml(peerName)}: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
    await renderSwarmPage();
  }
}

async function swarmUnreferencePeerRoms(peerId, peerName) {
  if (!window.confirm(`Stop referencing ${peerName}'s ROMs? Any local system folders that were renamed aside will be restored.`)) return;
  try {
    setLoading(true, `Removing reference to ${peerName}...`);
    const accepted = await apiPost(`/admin/network-shares/${encodeURIComponent(peerId)}/disable`, {});
    if (accepted.status !== "detaching" && accepted.status !== "disabled") {
      throw new Error(accepted.status_detail || "detach was not accepted");
    }
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await api("/admin/network-shares");
      const current = (response.shares || []).find((share) => String(share.peer_id || "") === String(peerId || ""));
      if (!current) break;
      if (current.status === "error") throw new Error(current.status_detail || "detach cleanup failed");
      setLoading(true, `Restoring local ROMs and BIOS from ${peerName}...`);
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (attempt === 59) throw new Error("detach is still running in the background; check this page again shortly");
    }
    showToast(`Stopped referencing ${escapeHtml(peerName)}'s ROMs`, "success");
    showToast("EmulationStation is refreshing its game list in the background.", "info", 8000);
  } catch (err) {
    showToast(`Failed to remove reference: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    setLoading(false);
    await renderSwarmPage();
  }
}

// Tailnet's MagicDNS FQDN (e.g. "batocera.tailnet-name.ts.net") resolves its
// bare first label (e.g. "batocera") on any device using this tailnet's DNS --
// that's the short, memorable address ("https://batocera") worth showing/
// linking instead of a raw Tailnet IP.
function tailnetShortHostname(dnsName) {
  const trimmed = String(dnsName || "").trim();
  return trimmed ? trimmed.split(".")[0] : "";
}

// Clicking a card's Tailnet address opens this instead of navigating directly
// -- lets you see the full Tailnet identity (and fall back to the raw IP when
// no MagicDNS name is on record yet) before jumping to a new tab.
function openTailnetPeerModal(peerId) {
  const drone = swarmDronesById[String(peerId || "")];
  if (!drone) return;
  const shortName = tailnetShortHostname(drone.dns_name);
  const url = shortName ? `https://${shortName}` : (drone.tailnet_ip ? `https://${drone.tailnet_ip}` : "");
  const modalId = "tailnetPeerModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  const detail = (label, value) => `<div class="asset-detail"><span>${escapeHtml(label)}</span><strong>${value ? escapeHtml(value) : "n/a"}</strong></div>`;
  const noDnsNote = !drone.dns_name
    ? `<div class="small text-muted mt-3"><i class="bi bi-info-circle me-1" aria-hidden="true"></i>No MagicDNS name on record yet for this Drone -- it appears the next time a Tailnet discovery sync sees it online. Falling back to its Tailnet IP address for now.</div>`
    : "";
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-diagram-3 me-2"></i>Tailnet Details -- ${escapeHtml(drone.name || drone.drone_id || "Drone")}</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <div class="asset-detail-panel">
            ${detail("Hostname", shortName || drone.hostname)}
            ${detail("Full DNS name", drone.dns_name)}
            ${detail("Tailnet IP", drone.tailnet_ip)}
            ${detail("Status", drone.is_self ? "This Drone" : (drone.online ? "Online" : "Offline"))}
          </div>
          ${noDnsNote}
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
          ${url ? `<a class="btn btn-primary" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer"><i class="bi bi-box-arrow-up-right me-1"></i>Open ${escapeHtml(url)}</a>` : ""}
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

async function swarmEnrollTailnet() {
  const input = document.getElementById("swarmTailnetKey");
  const button = document.getElementById("swarmTailnetEnrollBtn");
  const authKey = (input.value || "").trim();
  if (!authKey) {
    showToast("Paste an auth key from the Tailscale admin console first.", "warning");
    return;
  }
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Connecting...';
  try {
    const status = await apiPost("/admin/tailnet/enroll", { auth_key: authKey });
    showToast(
      status.tailnet_ip
        ? `Connected to the tailnet as ${escapeHtml(status.tailnet_ip)}.`
        : "Tailnet enrollment accepted; the address will appear shortly.",
      "success",
    );
    invalidateSwarmDataCache();
    await renderSwarmPage();
  } catch (err) {
    showToast(`Tailnet enrollment failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-link-45deg me-1"></i>Connect';
  }
}

function swarmToggleTailnetAuthRotation(show = true) {
  const form = document.getElementById("swarmTailnetRotateForm");
  if (!form) return;
  form.classList.toggle("d-none", !show);
  if (show) document.getElementById("swarmTailnetRotateKey")?.focus();
}

async function swarmRotateTailnetAuthKey() {
  const input = document.getElementById("swarmTailnetRotateKey");
  const button = document.getElementById("swarmTailnetRotateSubmitBtn");
  const authKey = (input?.value || "").trim();
  if (!authKey) {
    showToast("Paste the replacement auth key first.", "warning");
    return;
  }
  if (!window.confirm("Rotate the Tailnet auth token? This Drone will briefly disconnect while it re-enrolls with the replacement key.")) return;
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>Rotating...';
  try {
    const status = await apiPost("/admin/tailnet/rotate-auth-key", { auth_key: authKey });
    showToast(
      status.tailnet_ip
        ? `Tailnet auth token rotated. Connected as ${escapeHtml(status.tailnet_ip)}.`
        : "Tailnet auth token rotated; the address will appear shortly.",
      "success",
    );
    input.value = "";
    invalidateSwarmDataCache();
    await renderSwarmPage();
  } catch (err) {
    showToast(`Tailnet auth token rotation failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Rotate';
  }
}

// The "Share with Swarm" toggle (or, for an imported key, a notice that it
// can't be re-shared -- single-hop-only, exactly like VPN/SMTP: enforced
// both here (hiding the toggle) and independently server-side in
// export_tailnet_payload()) plus a "Pull Configuration" peer picker, always
// available even before this drone is enrolled -- a fresh drone can join by
// pulling a peer's shared key instead of pasting one of its own. Only works
// end-to-end if the key was created as *reusable* in the Tailscale admin
// console (the same guidance already given in the "Connect" steps below);
// a single-use key will fail on the second drone with whatever error
// tailscale itself returns.
function renderTailnetShareSection(state) {
  const sharingControl = !state.enrolled
    ? ""
    : state.source_peer_id
    ? `<p class="text-muted small mb-2"><i class="bi bi-info-circle me-1"></i>This auth key was imported from <strong>${escapeHtml(state.source_peer_name || state.source_peer_id)}</strong> and cannot be re-shared &mdash; only the drone that originally connected with it can share it with the swarm.</p>`
    : `
      <div class="form-check form-switch mb-2">
        <input class="form-check-input" type="checkbox" role="switch" id="tailnetSharingEnabled" ${state.sharing_enabled ? "checked" : ""} onchange="setTailnetSharing(this.checked)">
        <label class="form-check-label" for="tailnetSharingEnabled">Allow paired drones to pull this auth key</label>
      </div>`;
  return `
    <hr>
    <div class="small text-muted mb-2">
      <strong>Share with Swarm.</strong>
      ${state.enrolled
        ? "Share this drone's auth key with paired drones over the same cert-pinned peer link used for ROM/BIOS transfers -- never through the browser."
        : "Already enrolled on another drone in your swarm? Pull its auth key here instead of pasting your own."}
    </div>
    ${sharingControl}
    <div class="d-flex flex-wrap align-items-end gap-2">
      <div>
        <label class="form-label mb-1" for="tailnetPullPeer">Paired Drone</label>
        <select id="tailnetPullPeer" class="form-select form-select-sm" style="min-width:220px"><option value="">Loading...</option></select>
      </div>
      <button class="btn btn-outline-primary btn-sm" type="button" id="tailnetPullBtn" disabled onclick="pullTailnetConfigFromPeer()"><i class="bi bi-cloud-arrow-down me-1"></i>Pull Configuration</button>
    </div>`;
}
function renderSwarmTailnetCard(tailnet) {
  const state = tailnet || {};
  let body;
  if (state.enrolled) {
    const address = state.tailnet_ip ? `https://${escapeHtml(state.tailnet_ip)}` : "";
    body = `
      <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <span class="badge text-bg-success">Connected</span>
        ${state.tailnet_ip ? `<code>${escapeHtml(state.tailnet_ip)}</code>` : ""}
      </div>
      <div class="small text-muted">This Drone is on your tailnet. Your phone (with the Tailscale app signed in to the same account) and Drones in other homes can reach it${address ? ` at <a href="${address}" target="_blank" rel="noopener noreferrer">${address}</a>` : ""} from anywhere -- no port forwarding.</div>
      <div class="d-none mt-3" id="swarmTailnetRotateForm">
        <label class="form-label small" for="swarmTailnetRotateKey">Replacement auth key</label>
        <div class="d-flex flex-column flex-sm-row gap-2">
          <input id="swarmTailnetRotateKey" class="form-control" type="password" placeholder="tskey-auth-..." autocomplete="new-password">
          <button id="swarmTailnetRotateSubmitBtn" class="btn btn-primary text-nowrap" type="button" onclick="swarmRotateTailnetAuthKey()"><i class="bi bi-arrow-repeat me-1"></i>Rotate</button>
          <button class="btn btn-outline-secondary" type="button" onclick="swarmToggleTailnetAuthRotation(false)">Cancel</button>
        </div>
      </div>
      ${renderTailnetShareSection(state)}`;
  } else if (!state.installed) {
    body = `
      <div class="d-flex align-items-center gap-2 mb-2"><span class="badge text-bg-secondary">Not installed</span></div>
      <div class="small text-muted">Tailscale isn't installed on this Drone yet. Re-run the Drone installer once (it now sets the mesh up automatically), then come back here to connect:</div>
      <pre class="small mt-2 mb-0"><code>curl -fsSL https://github.com/Batocera-Fleet-Federation/batocera.drone/releases/latest/download/batocera_install.sh | bash</code></pre>`;
  } else {
    body = `
      <div class="d-flex align-items-center gap-2 mb-2"><span class="badge text-bg-warning text-dark">Not connected</span></div>
      <div class="small text-muted mb-2">
        Drones in different homes -- and your phone when you're away -- can't normally reach each other through home routers.
        A <strong>tailnet</strong> (a free private mesh network from <a href="https://tailscale.com" target="_blank" rel="noopener noreferrer">Tailscale</a>) fixes that:
        every device gets a private <code>100.x</code> address that works from anywhere, encrypted device-to-device, with no router changes or port forwarding.
        The account is only used so your devices can recognize each other; game traffic flows directly between your machines.
      </div>
      <ol class="small text-muted mb-3">
        <li>Create a free Tailscale account: <a href="https://login.tailscale.com/start" target="_blank" rel="noopener noreferrer">login.tailscale.com/start</a></li>
        <li>Generate an auth key (mark it <em>reusable</em> so one key can enroll every Drone): <a href="https://login.tailscale.com/admin/settings/keys" target="_blank" rel="noopener noreferrer">login.tailscale.com/admin/settings/keys</a></li>
        <li>Paste the key below. To reach your Drones from your phone, install the Tailscale app and sign in to the same account.</li>
      </ol>
      <div class="row g-2 align-items-end">
        <div class="col-12 col-md-8"><label class="form-label small" for="swarmTailnetKey">Auth key</label><input id="swarmTailnetKey" class="form-control" type="password" placeholder="tskey-auth-..." autocomplete="off"></div>
        <div class="col-12 col-md-4"><button id="swarmTailnetEnrollBtn" class="btn btn-primary w-100" onclick="swarmEnrollTailnet()"><i class="bi bi-link-45deg me-1"></i>Connect</button></div>
      </div>
      ${renderTailnetShareSection(state)}`;
  }
  return `
    <div class="card log-card h-100">
      <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
        <span><i class="bi bi-globe2 me-2" aria-hidden="true"></i>Tailnet (access from anywhere)</span>
        <div class="d-flex flex-wrap align-items-center gap-2">
          <button class="help-term" type="button" onclick="showTailnetGuideModal()"><i class="bi bi-question-circle-fill"></i>How does this work?</button>
          ${state.enrolled ? '<button class="btn btn-sm btn-outline-primary text-nowrap" type="button" onclick="swarmToggleTailnetAuthRotation(true)"><i class="bi bi-arrow-repeat me-1"></i>Rotate Auth Token</button>' : ""}
        </div>
      </div>
      <div class="card-body">
        <div class="small text-muted mb-2"><i class="bi bi-info-circle me-1"></i>This is required to connect Drones across different networks (different homes, or a phone away from home) -- Drones already on the same local network can find each other without it.</div>
        ${body}
      </div>
    </div>`;
}

// Full-length plain-language explainer for the Tailnet card -- deliberately
// separate from the one-paragraph HELP_TECH_GLOSSARY['tailnet'] entry used
// elsewhere (e.g. the home page): this covers install steps for a machine or
// phone, what joining the mesh actually does, and the access-control model,
// which doesn't fit in a single glossary paragraph.
function showTailnetGuideModal() {
  const modalId = "tailnetGuideModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable modal-lg">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-globe2 me-2"></i>Reach your Drones from anywhere</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <h6 class="text-uppercase small text-muted fw-bold mb-2">What is a tailnet?</h6>
          <p class="small">A tailnet is your own private network of devices, built on top of the ordinary internet by a free service called <a href="https://tailscale.com" target="_blank" rel="noopener noreferrer">Tailscale</a>. Every device you add to it -- a Drone, your laptop, your phone -- gets its own stable private address and can reach every other device on the same tailnet directly, no matter which Wi-Fi or mobile network it happens to be on. Nothing needs opening on your router, and nothing outside your tailnet can see or use it.</p>

          <h6 class="text-uppercase small text-muted fw-bold mb-2 mt-4">Is it secure?</h6>
          <p class="small">Yes. Every connection between two of your devices is encrypted end-to-end with WireGuard, a modern, widely-audited encryption protocol, and traffic goes directly device-to-device wherever possible rather than through Tailscale's own servers -- those servers only help two of your devices find each other and never see what's inside the connection. A device can only become part of your tailnet if it's running the Tailscale app <em>and</em> signed in with your account, so a stranger who somehow learned a Drone's tailnet address still couldn't reach it.</p>

          <h6 class="text-uppercase small text-muted fw-bold mb-2 mt-4">Install it on a machine or phone</h6>
          <ol class="small mb-2">
            <li>Grab the app for your device at <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer">tailscale.com/download</a> (Windows, macOS, Linux, iOS, and Android are all supported, free for personal use).</li>
            <li>Install it and sign in with the same Tailscale account used to connect this Drone above.</li>
            <li>The device appears in your tailnet within seconds -- nothing else to configure.</li>
          </ol>

          <h6 class="text-uppercase small text-muted fw-bold mb-2 mt-4">What installing it actually does</h6>
          <p class="small">Signing in adds that machine or phone to your private mesh. From that moment on it can reach, and be reached by, every other device already on your tailnet -- as if they all shared one network, even if they're actually on opposite sides of the world.</p>

          <h6 class="text-uppercase small text-muted fw-bold mb-2 mt-4">Reaching a Drone from anywhere</h6>
          <p class="small">Once your phone or another computer is signed into the same tailnet as this Drone, open the tailnet address shown on this card in a browser -- from a coffee shop, a friend's house, mobile data, anywhere -- and it opens exactly like it does at home. No port forwarding, no separate VPN toggle, no keeping track of a home IP address that keeps changing.</p>

          <h6 class="text-uppercase small text-muted fw-bold mb-2 mt-4">Who's allowed in</h6>
          <p class="small mb-0">Only devices with Tailscale installed and signed into your Tailscale account are members of your tailnet -- everyone else is invisible to it, including anyone else on the same Wi-Fi. You can see and remove any device from your tailnet at any time from Tailscale's own admin console. And being on the tailnet only gets a device to this Drone's front door: it still needs this Drone's own username and password to actually sign in (see the account button in the top-left of every page to change those).</p>
        </div>
        <div class="modal-footer">
          <a href="https://tailscale.com/download" target="_blank" rel="noopener noreferrer" class="btn btn-outline-primary btn-sm me-auto"><i class="bi bi-box-arrow-up-right me-1"></i>Download Tailscale</a>
          <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Close</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
}

async function renderSwarmPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Swarm";
  subtitleNode.textContent = "Every Drone in your federation -- local and across the tailnet";
  setLoading(true, "Loading swarm...");
  try {
    // Independent calls run concurrently. Default Tailnet/local-network state
    // is read-only here; active discovery is reserved for the page's explicit
    // Discover and Refresh controls.
    const [discovery, overview, networkShares] = await Promise.all([
      loadTailnetDiscovery(),
      loadSwarmOverview(),
      api("/admin/network-shares").catch(() => ({ shares: [] })),
    ]);
    const tailnet = discovery.tailnet || { installed: false };
    const drones = Array.isArray(overview.drones) ? overview.drones : [];
    swarmDronesById = Object.fromEntries(drones.map((drone) => [String(drone.drone_id || ""), drone]));
    swarmNetworkSharesByPeer = Object.fromEntries((networkShares.shares || []).map((share) => [String(share.peer_id || ""), share]));
    content.innerHTML = `
      ${renderSwarmTabBar("swarm")}
      <div class="row row-cols-1 row-cols-md-2 row-cols-xl-3 g-3 mb-3" id="swarmDroneGrid">
        ${drones.map(renderSwarmDroneCard).join("")}
      </div>
      <div class="row g-3 mb-3 align-items-stretch">
        <div class="col-12 col-lg-6">${renderSwarmTailnetCard(tailnet)}</div>
        <div class="col-12 col-lg-6"><div class="card log-card h-100">
          <div class="card-header d-flex justify-content-between align-items-center"><span><i class="bi bi-key me-2" aria-hidden="true"></i>Pairing</span><button class="btn btn-sm btn-outline-primary" id="localPairCodeRotateBtn">Rotate Code</button></div>
          <div class="card-body">
            <div class="small text-muted mb-2"><i class="bi bi-info-circle me-1"></i>This code proves you have access to this Drone. To pair it with another Drone on the same network, pick this Drone from that Drone's Nearby Drones list and enter this code when prompted (or enter that Drone's own code here, if pairing from this side instead). It rotates periodically for security -- once paired, the two Drones stay linked until you unpair them.</div>
            <div id="localPairingBody"><div class="text-muted">Loading pairing...</div></div>
          </div>
        </div></div>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center"><span><i class="bi bi-radar me-2" aria-hidden="true"></i>Nearby Drones</span><div class="d-flex gap-2"><button class="btn btn-sm btn-outline-primary" id="localDiscoverBtn"><i class="bi bi-radar me-1"></i>Discover</button><button class="btn btn-sm btn-outline-secondary" id="localRefreshBtn"><i class="bi bi-arrow-repeat"></i></button></div></div>
        <div class="card-body" id="localPeersBody"><div class="text-muted">Loading peers...</div></div>
      </div>`;
    loadTailnetPullPeerOptions();

    async function refreshPairing(status = null, includeTailnet = false) {
      if (!status) {
        status = includeTailnet
          ? (await loadTailnetDiscovery(true)).network
          : await api("/admin/local-network/status");
      }
      document.getElementById("localPairingBody").innerHTML = status.active
        ? `<div class="d-flex flex-wrap align-items-center gap-3"><div><div class="small text-muted">Pairing code</div><div class="display-6 mono">${escapeHtml(status.pairing?.code || "")}</div></div><div class="small text-muted">Expires ${escapeHtml(status.pairing?.expires_at || "")}.</div></div>`
        : '<div class="themed-empty">Local networking is disabled; enable it above to pair Drones.</div>';
      document.getElementById("localPeersBody").innerHTML = renderLocalPeerRows(status.peers || []);
      document.getElementById("localDiscoverBtn").disabled = !status.active;
      document.getElementById("localPairCodeRotateBtn").disabled = !status.active;
    }
    window.refreshLocalNetwork = refreshPairing;
    document.getElementById("localDiscoverBtn").addEventListener("click", async () => { await apiPost("/admin/local-network/discover", {}); await refreshPairing(null, true); });
    document.getElementById("localRefreshBtn").addEventListener("click", () => refreshPairing(null, true));
    document.getElementById("localPairCodeRotateBtn").addEventListener("click", async () => { await apiPost("/admin/local-network/pairing-code/rotate", {}); await refreshPairing(null, true); });
    await refreshPairing(discovery.network || null);
  } catch (err) {
    showToast(`Failed to load swarm: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = '<div class="themed-empty">Swarm could not be loaded.</div>';
  } finally {
    setLoading(false);
  }
}

async function renderIntegrationTransfersPanel(target) {
  target.innerHTML = `
    <div id="localTransferRequestPanel"></div>
    <div class="card log-card mt-3">
      <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-arrow-left-right me-2"></i>Transfers</span>
        <button id="transfersRefreshBtn" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
      </div>
      <div class="small text-muted px-3 pt-3">All drone-to-drone asset transfers to and from this machine -- downloads and uploads.</div>
      <div class="card-body" id="transfersBody"><div class="text-muted">Loading transfers...</div></div>
    </div>`;

  async function loadTransfers() {
    const [downloads, uploads] = await Promise.all([api("/admin/downloads"), api("/admin/uploads")]);
    const body = document.getElementById("transfersBody");
    if (body) body.innerHTML = renderTransfersPanel(downloads, uploads);
  }
  window.refreshTransfers = loadTransfers;
  document.getElementById("transfersRefreshBtn").addEventListener("click", async () => {
    try {
      await window.refreshTransfers();
    } catch (err) {
      showToast(`Failed to load transfers: ${escapeHtml(err.message || "unknown error")}`, "danger");
    }
  });

  await Promise.allSettled([
    renderLocalTransferRequestPanel(document.getElementById("localTransferRequestPanel")),
    loadTransfers(),
  ]);
}


async function renderLocalTransferRequestPanel(target) {
  target.innerHTML = `
    <div class="card log-card mb-3" id="localAssetsCard"><div class="card-header"><span id="localAssetsTitle">Request Assets from Connected Drone</span></div>
      <div class="card-body">
        <div class="small text-muted mb-3">Request inventories from a paired Drone, then download what you need. ROMs, BIOS, saves, and movies can be copied here; emulator configs and gameplay history are available for inspection.</div>
        <div class="row g-2 mb-2">
          <div class="col-12 col-lg-3"><label class="form-label small" for="localAssetPeer">Connected Drone</label><select id="localAssetPeer" class="form-select"></select></div>
          <div class="col-6 col-lg-2"><label class="form-label small" for="localAssetType">Asset Type</label><select id="localAssetType" class="form-select"><option value="roms">ROMs</option><option value="bios">BIOS</option><option value="saves">Saves</option><option value="movies">Movies</option><option value="config_backups">Config Backups</option><option value="emulator_configs">Emulator Configs</option><option value="gameplay">Gameplay History</option></select></div>
          <div class="col-6 col-lg-2" id="localAssetSystemsWrap"><label class="form-label small">Systems</label><div class="dropdown"><button id="localAssetSystemsToggle" class="btn btn-outline-secondary dropdown-toggle w-100 text-start" type="button" data-bs-toggle="dropdown" data-bs-auto-close="outside">All systems</button><div id="localAssetSystemsMenu" class="dropdown-menu p-2 w-100"><div class="small text-muted">Request assets to load systems.</div></div></div></div>
          <div class="col-8 col-lg-3"><label class="form-label small" for="localAssetQuery">Search</label><input id="localAssetQuery" class="form-control" placeholder="Search assets"></div>
          <div class="col-4 col-lg-2"><label class="form-label small" for="localAssetPageSize">Per Page</label><select id="localAssetPageSize" class="form-select"><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></div>
        </div>
        <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
          <button class="btn btn-sm btn-primary" id="localAssetLoadBtn"><i class="bi bi-search me-1"></i>Request</button>
          <button class="btn btn-sm btn-success" id="localAssetCopyAllBtn" disabled><i class="bi bi-cloud-arrow-down me-1"></i>Download All</button>
          <div class="form-check ms-lg-2 d-none" id="localAssetIncludeArtworkWrap"><input class="form-check-input" type="checkbox" id="localAssetIncludeArtwork" checked><label class="form-check-label small" for="localAssetIncludeArtwork">Include Artwork</label></div>
          <div class="form-check d-none" id="localAssetIncludeRomsWrap"><input class="form-check-input" type="checkbox" id="localAssetIncludeRoms" checked><label class="form-check-label small" for="localAssetIncludeRoms">Include ROMs</label></div>
          <div class="form-check d-none" id="localAssetOverwriteFilesWrap"><input class="form-check-input" type="checkbox" id="localAssetOverwriteFiles"><label class="form-check-label small" for="localAssetOverwriteFiles">Overwrite Files</label></div>
        </div>
        <div id="localAssetsBody"><div class="themed-empty">Select a Drone to browse its assets.</div></div>
        <div id="localAssetsPagination" class="mt-2"></div>
      </div></div>`;

  function updateRequestButtons() {
    const selected = !!(document.getElementById("localAssetPeer") || {}).value;
    document.getElementById("localAssetLoadBtn").disabled = !selected;
    document.getElementById("localAssetCopyAllBtn").disabled = !selected;
  }

  async function refresh() {
    // Only drones that answer the swarm probe are offered -- an unreachable
    // peer in the dropdown is a dead end. Nothing is selected by default; the
    // user picks a drone, its systems load, then Request fetches asset data.
    const peerSelect = document.getElementById("localAssetPeer");
    peerSelect.innerHTML = '<option value="">&lt;Select Drone&gt;</option>';
    updateRequestButtons();
    const overview = await loadSwarmOverview();
    const onlinePeers = (overview.drones || []).filter(drone => !drone.is_self && drone.online);
    const preselect = localPeerAssetContext.peerId;
    peerSelect.innerHTML = [
      '<option value="">&lt;Select Drone&gt;</option>',
      ...onlinePeers.map(drone => `<option value="${escapeHtml(drone.drone_id || "")}">${escapeHtml(drone.name || drone.hostname || drone.drone_id || "Drone")}</option>`),
    ].join("");
    if (preselect && onlinePeers.some(drone => String(drone.drone_id || "") === preselect)) {
      peerSelect.value = preselect;
    } else {
      peerSelect.value = "";
      localPeerAssetContext.peerId = "";
    }
    if (!onlinePeers.length) {
      document.getElementById("localAssetsBody").innerHTML = '<div class="themed-empty">No connected Drones right now. Pair and check Drones on the Swarm page.</div>';
    }
    updateRequestButtons();
  }
  window.refreshLocalNetworkAssets = refresh;

  async function onPeerSelected() {
    localPeerAssetContext.peerId = document.getElementById("localAssetPeer").value || "";
    localPeerAssetContext.systems = [];
    localPeerAssetContext.availableSystems = [];
    localPeerAssetContext.systemCounts = {};
    localPeerAssetContext.systemsLoadedPeerId = "";
    localPeerAssetContext.items = [];
    localPeerAssetContext.total = 0;
    renderLocalAssetSystems();
    document.getElementById("localAssetsPagination").innerHTML = "";
    updateRequestButtons();
    if (!localPeerAssetContext.peerId) {
      document.getElementById("localAssetsBody").innerHTML = '<div class="themed-empty">Select a Drone to browse its assets.</div>';
      return;
    }
    // Selecting a drone retrieves its systems (not its ROMs) so the Systems
    // filter is ready; asset data itself only loads on an explicit Request.
    const toggle = document.getElementById("localAssetSystemsToggle");
    if (toggle) toggle.textContent = "Loading systems...";
    await loadLocalPeerSystems();
    document.getElementById("localAssetsBody").innerHTML = '<div class="themed-empty">Choose systems if you want to narrow things down, then press Request.</div>';
  }

  document.getElementById("localAssetLoadBtn").addEventListener("click", requestLocalPeerAssets);
  document.getElementById("localAssetCopyAllBtn").addEventListener("click", copyAllLocalAssets);
  document.getElementById("localAssetType").addEventListener("change", updateLocalAssetTypeUi);
  document.getElementById("localAssetIncludeArtwork").addEventListener("change", updateLocalAssetTypeUi);
  document.getElementById("localAssetPeer").addEventListener("change", onPeerSelected);
  document.getElementById("localAssetPageSize").addEventListener("change", () => {
    document.getElementById("localAssetsBody").innerHTML = '<div class="themed-empty">Press Request to load assets with the new page size.</div>';
    document.getElementById("localAssetsPagination").innerHTML = "";
  });
  document.getElementById("localAssetQuery").addEventListener("keydown", async (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      await requestLocalPeerAssets();
    }
  });
  updateLocalAssetTypeUi();
  await refresh();
  // Deep link from a Swarm card ("Request Assets") preselects the drone and
  // loads its systems, but never auto-requests asset data -- fetching stays
  // behind an explicit Request click.
  if (localPeerAssetContext.peerId && document.getElementById("localAssetPeer").value === localPeerAssetContext.peerId) {
    await onPeerSelected();
    document.getElementById("localAssetsCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// The Pairing / Nearby Drones panels now live on the Swarm page
// (renderSwarmPage), which replaced the retired Integration page.

function localAssetIncludeArtwork() {
  const checkbox = document.getElementById("localAssetIncludeArtwork");
  return checkbox ? !!checkbox.checked : false;
}

function localAssetIncludeRoms() {
  const checkbox = document.getElementById("localAssetIncludeRoms");
  return checkbox ? !!checkbox.checked : true;
}

function localAssetOverwriteFiles() {
  const checkbox = document.getElementById("localAssetOverwriteFiles");
  return checkbox ? !!checkbox.checked : false;
}

function updateLocalAssetTypeUi() {
  const type = (document.getElementById("localAssetType") || {}).value || "roms";
  const isRoms = type === "roms";
  const transferable = LOCAL_TRANSFERABLE_TYPES.has(type);
  const hasSystems = !LOCAL_SYSTEMLESS_TYPES.has(type);
  document.getElementById("localAssetIncludeArtworkWrap")?.classList.toggle("d-none", !isRoms);
  document.getElementById("localAssetIncludeRomsWrap")?.classList.toggle("d-none", !isRoms);
  document.getElementById("localAssetOverwriteFilesWrap")?.classList.toggle("d-none", !transferable);
  document.getElementById("localAssetSystemsWrap")?.classList.toggle("opacity-50", !hasSystems);
  const systemsToggle = document.getElementById("localAssetSystemsToggle");
  if (systemsToggle) systemsToggle.disabled = !hasSystems;
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
  const counts = localPeerAssetContext.systemCounts || {};
  const selected = new Set(localPeerAssetContext.systems || []);
  if (!systems.length) {
    menu.innerHTML = '<div class="small text-muted px-2 py-1">No systems reported.</div>';
    toggle.textContent = "All systems";
    return;
  }
  // The system list can be very long (250+), so give it a search filter and a
  // scrollable, height-capped list.
  menu.innerHTML = `
    <input type="search" id="localAssetSystemsSearch" class="form-control form-control-sm mb-2" placeholder="Filter systems..." autocomplete="off">
    <div id="localAssetSystemsList" style="max-height: 260px; overflow-y: auto;">
      ${systems.map(system => {
        const safe = escapeHtml(system);
        const count = Number(counts[system] || 0);
        const countBadge = `<span class="badge text-bg-secondary ms-auto">${count}</span>`;
        return `<label class="dropdown-item d-flex gap-2 align-items-center" data-system="${safe.toLowerCase()}"><input class="form-check-input local-asset-system-check" type="checkbox" value="${safe}" ${selected.has(system) ? "checked" : ""}><span>${safe}</span>${countBadge}</label>`;
      }).join("")}
    </div>`;
  toggle.textContent = selected.size ? `${selected.size} selected` : "All systems";
  menu.querySelectorAll(".local-asset-system-check").forEach(input => input.addEventListener("change", () => {
    localPeerAssetContext.systems = selectedLocalAssetSystems();
    toggle.textContent = localPeerAssetContext.systems.length ? `${localPeerAssetContext.systems.length} selected` : "All systems";
  }));
  const search = document.getElementById("localAssetSystemsSearch");
  if (search) {
    search.addEventListener("click", (event) => event.stopPropagation());
    search.addEventListener("input", () => {
      const query = search.value.trim().toLowerCase();
      menu.querySelectorAll("#localAssetSystemsList label").forEach(label => {
        const name = label.getAttribute("data-system") || "";
        label.classList.toggle("d-none", Boolean(query) && !name.includes(query));
      });
    });
  }
}

async function loadLocalPeerSystems() {
  const peerId = (document.getElementById("localAssetPeer") || {}).value || localPeerAssetContext.peerId;
  localPeerAssetContext.peerId = peerId;
  localPeerAssetContext.systems = [];
  localPeerAssetContext.availableSystems = [];
  localPeerAssetContext.systemCounts = {};
  localPeerAssetContext.systemsLoadedPeerId = "";
  renderLocalAssetSystems();
  if (!peerId) return;
  try {
    const summary = await api(`/admin/local-network/peers/${encodeURIComponent(peerId)}/assets?type=summary`);
    const counts = summary.system_counts || {};
    localPeerAssetContext.systemCounts = counts;
    // Hide systems with no items -- an empty system is just noise in the filter.
    localPeerAssetContext.availableSystems = Array.from(new Set(summary.systems || []))
      .filter((name) => Number(counts[name] || 0) > 0)
      .sort((a, b) => String(a).localeCompare(String(b)));
    localPeerAssetContext.systemsLoadedPeerId = peerId;
  } catch (_) {
    localPeerAssetContext.availableSystems = [];
    localPeerAssetContext.systemCounts = {};
  }
  renderLocalAssetSystems();
}

async function requestLocalPeerAssets() {
  const peerId = (document.getElementById("localAssetPeer") || {}).value || localPeerAssetContext.peerId;
  if (!peerId) { showToast("Select a Drone first.", "warning"); return; }
  if (localPeerAssetContext.systemsLoadedPeerId !== peerId) {
    await loadLocalPeerSystems();
  }
  await loadLocalPeerAssets();
}

async function pairLocalPeer(peerId) {
  const code = window.prompt("Enter the 8-digit pairing code shown on the other Drone:");
  if (!code) return;
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/pair`, { pairing_code: code.trim() });
  invalidateSwarmDataCache();
  showToast("Drone paired.", "success");
  if (typeof window.refreshLocalNetwork === "function") await window.refreshLocalNetwork();
  if (typeof window.refreshLocalNetworkAssets === "function") await window.refreshLocalNetworkAssets();
}

async function restoreTailnetPeer(peerId) {
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/restore-tailnet`, {});
  invalidateSwarmDataCache();
  showToast("Tailnet Drone paired.", "success");
  await renderSwarmPage();
}

async function forgetLocalPeer(peerId) {
  if (!window.confirm("Forget this paired Drone? It will need to be paired again before browsing or syncing.")) return;
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/forget`, {});
  invalidateSwarmDataCache();
  if (window.location.hash.startsWith("#admin/swarm")) {
    await renderSwarmPage();
    return;
  }
  if (typeof window.refreshLocalNetwork === "function") await window.refreshLocalNetwork();
  if (typeof window.refreshLocalNetworkAssets === "function") await window.refreshLocalNetworkAssets();
}

async function dismissLocalPeer(peerId) {
  await apiPost(`/admin/local-network/peers/${encodeURIComponent(peerId)}/dismiss`, {});
  invalidateSwarmDataCache();
  if (window.location.hash.startsWith("#admin/swarm")) {
    await renderSwarmPage();
    return;
  }
  if (typeof window.refreshLocalNetwork === "function") await window.refreshLocalNetwork();
  if (typeof window.refreshLocalNetworkAssets === "function") await window.refreshLocalNetworkAssets();
}


async function loadLocalPeerAssets(resetPage = true) {
  const peerId = document.getElementById("localAssetPeer").value;
  const type = document.getElementById("localAssetType").value;
  const systems = selectedLocalAssetSystems();
  const q = document.getElementById("localAssetQuery").value.trim();
  const limit = Math.max(1, Number(document.getElementById("localAssetPageSize").value) || 50);
  if (!peerId) { showToast("Select a Drone first.", "warning"); return; }
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

async function setLocalAssetPage(page) {
  const limit = Math.max(1, Number(localPeerAssetContext.limit) || 50);
  localPeerAssetContext.offset = Math.max(0, (Math.max(1, Number(page) || 1) - 1) * limit);
  await loadLocalPeerAssets(false);
  document.getElementById("localAssetsCard")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function copyLocalPeerAsset(index) {
  const item = localPeerAssetContext.items[index];
  if (!item) return;
  const includeRoms = localPeerAssetContext.assetType !== "roms" || localAssetIncludeRoms();
  if (localPeerAssetContext.assetType === "roms" && !includeRoms && !localAssetIncludeArtwork()) {
    showToast("Select Include Artwork or Include ROMs before downloading.", "warning");
    return;
  }
  const result = await apiPost("/admin/local-network/sync", {
    peer_id: localPeerAssetContext.peerId,
    asset_type: localPeerAssetContext.assetType,
    system: item.system || item.root_name || "",
    include_artwork: localAssetIncludeArtwork(),
    include_roms: includeRoms,
    overwrite_files: localAssetOverwriteFiles(),
    item,
  });
  if (result && result.rom_absent) {
    showToast("That ROM is not on this Drone, so there is no local game to attach artwork to.", "info");
  } else if (result && result.rom_skipped) {
    const artworkJobs = Array.isArray(result.jobs) ? result.jobs.length : 0;
    showToast(artworkJobs
      ? "ROM already on this machine — copying its artwork only."
      : (!includeRoms ? "No missing artwork was found for ROMs on this Drone." : "ROM already on this machine — nothing to download."), "info");
  } else {
    showToast(!includeRoms ? "Artwork queued for local transfer." : "Asset queued for local transfer.", "success");
  }
  if (typeof window.refreshTransfers === "function") await window.refreshTransfers();
}

async function copyAllLocalAssets() {
  const peerId = document.getElementById("localAssetPeer").value;
  const type = document.getElementById("localAssetType").value;
  const systems = selectedLocalAssetSystems();
  const q = document.getElementById("localAssetQuery").value.trim();
  if (!peerId) { showToast("Pair a Drone before copying assets.", "warning"); return; }
  if (!LOCAL_TRANSFERABLE_TYPES.has(type)) { showToast("Bulk download supports ROMs, BIOS, saves, movies, and config backups.", "warning"); return; }
  const includeRoms = type !== "roms" || localAssetIncludeRoms();
  if (type === "roms" && !includeRoms && !localAssetIncludeArtwork()) {
    showToast("Select Include Artwork or Include ROMs before downloading.", "warning");
    return;
  }
  const scopeNoun = !includeRoms ? "artwork for ROMs already here" : type;
  const scope = systems.length ? `all ${scopeNoun} for ${systems.join(", ")}` : (q ? `all ${scopeNoun} matching “${q}”` : `every ${scopeNoun}`);
  if (!window.confirm(`Queue ${scope} from this Drone for download?`)) return;
  await queueLocalBulkCopy({ peer_id: peerId, asset_type: type, systems, q, include_artwork: localAssetIncludeArtwork(), include_roms: includeRoms, overwrite_files: localAssetOverwriteFiles() });
}

async function copyAllRomsForSystem(encodedSystem) {
  const system = decodeURIComponent(encodedSystem);
  const peerId = document.getElementById("localAssetPeer").value || localPeerAssetContext.peerId;
  if (!peerId) { showToast("Pair a Drone before copying assets.", "warning"); return; }
  const includeRoms = localAssetIncludeRoms();
  if (!includeRoms && !localAssetIncludeArtwork()) {
    showToast("Select Include Artwork or Include ROMs before downloading.", "warning");
    return;
  }
  const what = includeRoms ? `all ROMs for ${system}` : `artwork for ${system} ROMs already on this Drone`;
  if (!window.confirm(`Queue ${what} from this Drone for download?`)) return;
  await queueLocalBulkCopy({ peer_id: peerId, asset_type: "roms", system, include_artwork: localAssetIncludeArtwork(), include_roms: includeRoms, overwrite_files: localAssetOverwriteFiles() });
}

async function queueLocalBulkCopy(body) {
  try {
    const result = await apiPost("/admin/local-network/sync-bulk", body);
    const assets = Number(result.queued_assets) || 0;
    const artwork = Number(result.queued_artwork) || 0;
    const skipped = Number(result.skipped_existing) || 0;
    if (body.asset_type === "roms" && !body.include_roms) {
      if (!artwork) {
        showToast("No artwork to copy — either no matching ROMs are on this machine, or their artwork is already present.", "info");
      } else {
        showToast(`Queued ${artwork} artwork files for ROMs already on this Drone.`, "success");
      }
      if (typeof window.refreshTransfers === "function") await window.refreshTransfers();
      return;
    }
    const skippedNote = skipped ? ` ${skipped} already on this machine were skipped.` : "";
    if (!assets && !artwork) {
      showToast(skipped ? `All ${skipped} already on this machine — nothing to download.` : "Nothing matched to download.", skipped ? "info" : "warning");
    } else {
      showToast(`Queued ${assets} ${body.asset_type}${artwork ? ` (+${artwork} artwork files)` : ""} for local transfer.${skippedNote}`, "success");
    }
    if (typeof window.refreshTransfers === "function") await window.refreshTransfers();
  } catch (err) {
    showToast(`Bulk download failed: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

// The Overmind Integration panel was removed with the Integration page
// (Overmind is retired; see renderSwarmPage for the replacement flows).

function formatIdleDuration(seconds) {
  if (seconds === null || seconds === undefined) return "unknown";
  const value = Math.max(0, Math.floor(Number(seconds)));
  if (value < 60) return `${value}s`;
  const minutes = Math.floor(value / 60);
  const remainder = value % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}
async function renderAutomationPage() {
  currentSystemContext = null;
  titleNode.textContent = "Automation";
  subtitleNode.textContent = "Hands-off behaviors for this device";
  clearSystemTheme();
  setLoading(true, "Loading automation settings...");
  let payload;
  try {
    payload = await api("/admin/automation");
  } catch (err) {
    setLoading(false);
    content.innerHTML = `<div class="alert alert-danger">Failed to load automation settings: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  refreshRandomThemeLogo().catch(() => {});
  const idleVolume = payload.idle_volume || {};
  const idleGameExit = payload.idle_game_exit || {};
  const wifiRecovery = payload.wifi_recovery || {};
  const wifiStatus = payload.wifi_status || {};
  const monitor = payload.input_monitor || {};
  const enabled = !!idleVolume.enabled;
  const idleMinutes = Number(idleVolume.idle_minutes ?? 5);
  const targetVolume = Number(idleVolume.target_volume ?? 25);
  const currentVolume = payload.current_volume;
  const gameExitEnabled = !!idleGameExit.enabled;
  const gameExitMinutes = Number(idleGameExit.idle_minutes ?? 15);
  const gameRunning = !!payload.game_running;
  const wifiRecoveryEnabled = !!wifiRecovery.enabled;
  const wifiEnabledLabel = wifiStatus.wifi_enabled === true ? "enabled" : (wifiStatus.wifi_enabled === false ? "disabled" : "unknown");
  const wifiConnectedLabel = wifiStatus.wifi_connected ? "connected" : "not connected";
  const monitorAlert = monitor.available
    ? `<div class="text-muted small mb-3"><i class="bi bi-activity me-1"></i>Input monitor active — last input ${escapeHtml(formatIdleDuration(monitor.idle_seconds))} ago${currentVolume === null || currentVolume === undefined ? "" : ` · current volume ${escapeHtml(String(currentVolume))}%`}.</div>`
    : `<div class="alert alert-warning"><i class="bi bi-exclamation-triangle me-1"></i>The input activity monitor is not reporting yet. This automation only runs once the privileged Drone service is updated and restarted on this machine.</div>`;
  const gameExitStatus = monitor.available
    ? `<div class="text-muted small mb-3"><i class="bi bi-controller me-1"></i>${gameRunning ? "A game is currently running." : "No game is currently running."}</div>`
    : "";
  content.innerHTML = `
    <div class="row row-cols-1 row-cols-lg-2 g-3">
      <div class="col">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-sliders me-2"></i>Set volume when idle</div>
          <div class="card-body">
            ${monitorAlert}
            <p class="card-text text-muted">Automatically set this device's output volume to a target level after it has gone without any controller or keyboard input for a set amount of time -- raising or lowering it, whichever the target requires. The volume stays at the target until the device is used again.</p>
            <div class="form-check form-switch mb-3">
              <input class="form-check-input" type="checkbox" role="switch" id="idleVolumeEnabled" ${enabled ? "checked" : ""}>
              <label class="form-check-label" for="idleVolumeEnabled">Enable idle volume automation</label>
            </div>
            <div class="row g-3 mb-3">
              <div class="col-sm-6">
                <label class="form-label" for="idleVolumeMinutes">Idle time before adjusting (minutes)</label>
                <input class="form-control" type="number" id="idleVolumeMinutes" min="1" max="1440" step="1" value="${escapeHtml(String(idleMinutes))}">
              </div>
              <div class="col-sm-6">
                <label class="form-label" for="idleVolumeTarget">Target volume (%)</label>
                <input class="form-control" type="number" id="idleVolumeTarget" min="0" max="100" step="5" value="${escapeHtml(String(targetVolume))}">
                <div class="form-text">0 = mute.</div>
              </div>
            </div>
            <button class="btn btn-primary" id="idleVolumeSaveBtn"><i class="bi bi-save me-1"></i>Save</button>
          </div>
        </div>
      </div>
      <div class="col">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-power me-2"></i>Exit game when idle</div>
          <div class="card-body">
            ${monitorAlert}
            ${gameExitStatus}
            <p class="card-text text-muted">Automatically exit the running game and return to EmulationStation after it has gone without any controller or keyboard input for a set amount of time. Only applies while a game is actually running.</p>
            <div class="form-check form-switch mb-3">
              <input class="form-check-input" type="checkbox" role="switch" id="idleGameExitEnabled" ${gameExitEnabled ? "checked" : ""}>
              <label class="form-check-label" for="idleGameExitEnabled">Enable idle game exit</label>
            </div>
            <div class="row g-3 mb-3">
              <div class="col-sm-6">
                <label class="form-label" for="idleGameExitMinutes">Idle time before exiting (minutes)</label>
                <input class="form-control" type="number" id="idleGameExitMinutes" min="1" max="1440" step="1" value="${escapeHtml(String(gameExitMinutes))}">
              </div>
            </div>
            <button class="btn btn-primary" id="idleGameExitSaveBtn"><i class="bi bi-save me-1"></i>Save</button>
          </div>
        </div>
      </div>
      <div class="col">
        <div class="card h-100">
          <div class="card-header"><i class="bi bi-wifi me-2"></i>Recover Wi-Fi connection</div>
          <div class="card-body">
            <div class="text-muted small mb-3"><i class="bi bi-router me-1"></i>Wi-Fi is ${escapeHtml(wifiEnabledLabel)} and ${escapeHtml(wifiConnectedLabel)}.</div>
            <p class="card-text text-muted">Check the wireless connection every 60 seconds. When Wi-Fi is disabled or disconnected, Drone turns it off, waits three seconds, and turns it back on.</p>
            <div class="form-check form-switch mb-3">
              <input class="form-check-input" type="checkbox" role="switch" id="wifiRecoveryEnabled" ${wifiRecoveryEnabled ? "checked" : ""}>
              <label class="form-check-label" for="wifiRecoveryEnabled">Enable Wi-Fi recovery</label>
            </div>
            <button class="btn btn-primary" id="wifiRecoverySaveBtn"><i class="bi bi-save me-1"></i>Save</button>
          </div>
        </div>
      </div>
    </div>
  `;
  document.getElementById("idleVolumeSaveBtn").addEventListener("click", async () => {
    const minutesValue = parseInt(document.getElementById("idleVolumeMinutes").value, 10);
    const targetValue = parseInt(document.getElementById("idleVolumeTarget").value, 10);
    if (!Number.isFinite(minutesValue) || minutesValue < 1) {
      showToast("Idle time must be at least 1 minute.", "warning");
      return;
    }
    if (!Number.isFinite(targetValue) || targetValue < 0 || targetValue > 100) {
      showToast("Target volume must be between 0 and 100.", "warning");
      return;
    }
    setLoading(true, "Saving automation settings...");
    try {
      await apiPost("/admin/automation/idle-volume", {
        enabled: document.getElementById("idleVolumeEnabled").checked,
        idle_minutes: minutesValue,
        target_volume: targetValue,
      });
      showToast("Automation settings saved.", "success");
      await renderAutomationPage();
    } catch (err) {
      showToast(`Failed to save automation settings: ${escapeHtml(err.message || "unknown error")}`, "danger");
    } finally {
      setLoading(false);
    }
  });
  document.getElementById("idleGameExitSaveBtn").addEventListener("click", async () => {
    const minutesValue = parseInt(document.getElementById("idleGameExitMinutes").value, 10);
    if (!Number.isFinite(minutesValue) || minutesValue < 1) {
      showToast("Idle time must be at least 1 minute.", "warning");
      return;
    }
    setLoading(true, "Saving automation settings...");
    try {
      await apiPost("/admin/automation/idle-game-exit", {
        enabled: document.getElementById("idleGameExitEnabled").checked,
        idle_minutes: minutesValue,
      });
      showToast("Automation settings saved.", "success");
      await renderAutomationPage();
    } catch (err) {
      showToast(`Failed to save automation settings: ${escapeHtml(err.message || "unknown error")}`, "danger");
    } finally {
      setLoading(false);
    }
  });
  document.getElementById("wifiRecoverySaveBtn").addEventListener("click", async () => {
    setLoading(true, "Saving automation settings...");
    try {
      await apiPost("/admin/automation/wifi-recovery", {
        enabled: document.getElementById("wifiRecoveryEnabled").checked,
      });
      showToast("Automation settings saved.", "success");
      await renderAutomationPage();
    } catch (err) {
      showToast(`Failed to save automation settings: ${escapeHtml(err.message || "unknown error")}`, "danger");
    } finally {
      setLoading(false);
    }
  });
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
      if (!window.confirm("Generate a fresh self-signed Drone certificate now? Paired peers will need to re-pair afterward.")) return;
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
    ["drone_activity", "Drone Activity", "bi-broadcast"],
    ["tailscaled", "Tailscale", "bi-diagram-3"],
    ["es_launch_stdout", "ES Launch Stdout", "bi-terminal"],
    ["es_launch_stderr", "ES Launch Stderr", "bi-exclamation-triangle"],
    ["gameplay", "Gameplay", "bi-clock-history"],
  ];
  const validSources = new Set(logSources.map(([source]) => source));
  const effectiveSource = validSources.has(selectedSource) ? selectedSource : null;
  const effectiveLines = clampLogLines(selectedLines);

  titleNode.textContent = "System Logs";
  subtitleNode.textContent = "View Drone, Tailscale, EmulationStation launch, emulator, and gameplay logs";
  content.innerHTML = `
    ${renderDebugTabBar("logs")}
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
          <div class="card-body" id="logBody">
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
function renderGameplayLogTable(payload) {
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
          <div class="text-muted small mono d-none d-md-block">${escapeHtml(session.rom_path || "")}</div>
        </td>
        <td class="text-nowrap">${escapeHtml(duration)}</td>
      </tr>
    `;
  }).join("");
  return `
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
      <div class="small text-muted">Detected game launches and recent gameplay sessions.</div>
      <span class="badge text-bg-secondary">${sessions.length} session${sessions.length === 1 ? "" : "s"}${payload.pending_spool_events ? ` · ${payload.pending_spool_events} pending event${payload.pending_spool_events === 1 ? "" : "s"}` : ""}</span>
    </div>
    <div class="table-responsive">
      <table class="table table-sm table-hover align-middle themed-table bff-stack">
        <thead><tr><th>Played</th><th>System</th><th>Game</th><th>Duration</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="4" class="text-muted">No gameplay sessions detected yet.</td></tr>'}</tbody>
      </table>
    </div>
  `;
}

function ensureTextLogBody() {
  const logBody = document.getElementById("logBody");
  if (!logBody) return;
  if (document.getElementById("logContent")) return;
  logBody.innerHTML = `
    <div class="small text-muted mb-2">Newest lines are shown first. Automatic updates preserve your reading position.</div>
    <textarea id="logContent" class="mono log-content bg-dark text-light p-3 form-control" readonly spellcheck="false">Select a log source from the left panel to view its contents.</textarea>
  `;
}

async function loadGameplayLog(triggerEl = null, updateHash = true, silent = false) {
  currentLogSource = "gameplay";
  const lines = clampLogLines(document.getElementById("linesInput")?.value || "200");
  const targetHash = `#admin/logs/gameplay?lines=${encodeURIComponent(lines)}`;
  if (updateHash && window.location.hash !== targetHash) {
    setHash(targetHash);
    return;
  }
  if (!silent) setLoading(true, "Loading gameplay logs...");
  try {
    const payload = await api("/admin/gameplay-logs");
    const logTitle = document.getElementById("logTitle");
    const logBody = document.getElementById("logBody");
    if (logTitle) logTitle.textContent = "Gameplay Sessions";
    if (logBody) logBody.innerHTML = renderGameplayLogTable(payload);
    decorateStackTables(logBody || content);
    document.querySelectorAll('#logSources .list-group-item').forEach(el => el.classList.remove('active'));
    const activeEl = triggerEl || document.querySelector('#logSources .list-group-item[data-log-source="gameplay"]');
    if (activeEl) activeEl.classList.add('active');
  } catch (err) {
    if (!silent) showToast(`Failed to load gameplay logs: ${escapeHtml(err.message || "unknown error")}`, "danger");
  } finally {
    if (!silent) setLoading(false);
  }
}

async function renderGameplayLogsPage() {
  setHash("#admin/logs/gameplay?lines=200");
}
async function loadLog(source, triggerEl = null, updateHash = true, silent = false) {
  if (source === "gameplay") {
    await loadGameplayLog(triggerEl, updateHash, silent);
    return;
  }
  currentLogSource = source;
  const lines = clampLogLines(document.getElementById("linesInput")?.value || "200");
  const targetHash = `#admin/logs/${encodeURIComponent(source)}?lines=${encodeURIComponent(lines)}`;
  if (updateHash && window.location.hash !== targetHash) {
    setHash(targetHash);
    return;
  }
  if (!silent) setLoading(true, `Loading ${source} logs...`);
  try {
    ensureTextLogBody();
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
  titleNode.textContent = "Emulators";
  subtitleNode.textContent = "Emulator config files tracked on this Drone";
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
      ${renderDebugTabBar("emulators")}
      <div class="row">
        <div class="col-md-3 mb-3">
          <div class="card log-card">
            <div class="card-header d-flex justify-content-between align-items-center">
              <span>Tracked Configs</span>
              <span class="badge">${emulatorConfigRows.length}</span>
            </div>
            <div class="emulator-config-filter-wrap p-2">
              <input id="emulatorConfigFilter" class="form-control form-control-sm" type="search" placeholder="Filter configs" autocomplete="off" oninput="filterEmulatorConfigs(this.value)">
            </div>
            <div class="emulator-config-source-scroll" id="emulatorConfigSources">
              ${renderEmulatorConfigTree()}
            </div>
            <div id="emulatorConfigFilterEmpty" class="small text-muted px-3 py-2" style="display:none;">No configs match.</div>
            <div class="small text-muted px-3 py-2 border-top" style="border-color:var(--admin-border)!important;">Only recognized emulator/Batocera configuration files are shown${payload.max_configs ? `, up to ${payload.max_configs}` : ""}.</div>
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
              <pre id="emulatorConfigContent" class="mono admin-config-content bg-dark text-light p-3" style="max-height: 640px; overflow-y: auto; white-space: pre-wrap;">Select a config from the left panel to view its contents.</pre>
            </div>
          </div>
        </div>
      </div>
    `;
    if (!emulatorConfigRows.length) {
      document.getElementById("emulatorConfigContent").textContent = "No emulator config files were found.";
    } else {
      expandEmulatorConfigAncestors(selectedEmulatorConfigIndex);
      renderEmulatorConfigTreeIntoContainer();
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
function emulatorConfigPathParts(row) {
  const raw = String((row && row.label) || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const parts = raw.split("/").map(part => part.trim()).filter(Boolean);
  return parts.length ? parts : [raw || "config"];
}
function buildEmulatorConfigTree(rows) {
  const root = { key: "", name: "", dirs: new Map(), files: [] };
  (rows || []).forEach((row, index) => {
    const parts = emulatorConfigPathParts(row);
    let node = root;
    parts.slice(0, -1).forEach((part) => {
      const key = node.key ? `${node.key}/${part}` : part;
      if (!node.dirs.has(part)) {
        node.dirs.set(part, { key, name: part, dirs: new Map(), files: [] });
      }
      node = node.dirs.get(part);
    });
    node.files.push({ name: parts[parts.length - 1] || row.label || `config-${index + 1}`, index, row });
  });
  return root;
}
function sortEmulatorConfigTreeEntries(entries) {
  return entries.sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base" }));
}
function emulatorConfigNodeMatches(node, query) {
  if (!query) return true;
  if (String(node.name || "").toLowerCase().includes(query)) return true;
  for (const file of node.files || []) {
    if (String(file.row?.label || file.name || "").toLowerCase().includes(query)) return true;
  }
  for (const child of node.dirs.values()) {
    if (emulatorConfigNodeMatches(child, query)) return true;
  }
  return false;
}
function emulatorConfigVisibleFiles(files, query) {
  if (!query) return files;
  return files.filter(file => String(file.row?.label || file.name || "").toLowerCase().includes(query));
}
function renderEmulatorConfigTreeNode(node, depth, query) {
  if (!emulatorConfigNodeMatches(node, query)) return "";
  const dirs = sortEmulatorConfigTreeEntries(Array.from(node.dirs.values())).map(child => renderEmulatorConfigTreeNode(child, depth + 1, query)).join("");
  const files = sortEmulatorConfigTreeEntries(emulatorConfigVisibleFiles(node.files || [], query)).map(file => {
    const row = file.row || {};
    const meta = row.size ? `${Number(row.size).toLocaleString()} bytes` : (row.fingerprint ? String(row.fingerprint).slice(0, 8) : "");
    return `<button type="button" class="tree-grid-row tree-leaf-row emulator-tree-row text-start" style="--tree-depth:${depth + 1}" data-config-index="${file.index}" onclick="selectEmulatorConfig(${file.index})">
      <span class="tree-grid-main"><i class="bi bi-file-earmark-code tree-grid-icon"></i><span class="tree-grid-label text-truncate" title="${escapeHtml(row.label || file.name)}">${escapeHtml(file.name)}</span></span>
      <span class="tree-grid-meta">${escapeHtml(meta)}</span>
      <span class="tree-grid-action"></span>
    </button>`;
  }).join("");
  const expanded = query || emulatorConfigTreeExpanded.has(node.key);
  const descendantCount = countEmulatorConfigFiles(node);
  return `<div class="emulator-tree-node" data-folder-key="${escapeHtml(node.key)}">
    <button type="button" class="tree-grid-row tree-category-row emulator-tree-row text-start" style="--tree-depth:${depth}" onclick="toggleEmulatorConfigFolder(this.closest('.emulator-tree-node').dataset.folderKey)">
      <span class="tree-grid-main"><i class="bi ${expanded ? "bi-chevron-down" : "bi-chevron-right"} tree-grid-caret"></i><i class="bi ${expanded ? "bi-folder2-open" : "bi-folder"} tree-grid-icon"></i><span class="tree-grid-label text-truncate" title="${escapeHtml(node.key)}">${escapeHtml(node.name)}</span></span>
      <span class="tree-grid-meta">${descendantCount} file${descendantCount === 1 ? "" : "s"}</span>
      <span class="tree-grid-action"></span>
    </button>
    <div class="tree-branch emulator-tree-children" style="${expanded ? "" : "display:none;"}">${dirs}${files}</div>
  </div>`;
}
function countEmulatorConfigFiles(node) {
  let total = (node.files || []).length;
  for (const child of node.dirs.values()) {
    total += countEmulatorConfigFiles(child);
  }
  return total;
}
function renderEmulatorConfigTree(queryValue = null) {
  const filter = queryValue === null ? document.getElementById("emulatorConfigFilter")?.value : queryValue;
  const query = String(filter || "").trim().toLowerCase();
  const tree = buildEmulatorConfigTree(emulatorConfigRows);
  const roots = sortEmulatorConfigTreeEntries(Array.from(tree.dirs.values())).map(node => renderEmulatorConfigTreeNode(node, 0, query)).join("");
  const rootFiles = sortEmulatorConfigTreeEntries(emulatorConfigVisibleFiles(tree.files, query)).map(file => `
    <button type="button" class="tree-grid-row tree-leaf-row emulator-tree-row text-start" style="--tree-depth:0" data-config-index="${file.index}" onclick="selectEmulatorConfig(${file.index})">
      <span class="tree-grid-main"><i class="bi bi-file-earmark-code tree-grid-icon"></i><span class="tree-grid-label text-truncate" title="${escapeHtml(file.row?.label || file.name)}">${escapeHtml(file.name)}</span></span>
      <span class="tree-grid-meta">${file.row?.size ? `${Number(file.row.size).toLocaleString()} bytes` : ""}</span>
      <span class="tree-grid-action"></span>
    </button>`).join("");
  return `<div class="tree-grid emulator-config-tree">${roots}${rootFiles}</div>`;
}
function renderEmulatorConfigTreeIntoContainer(queryValue = null) {
  const container = document.getElementById("emulatorConfigSources");
  if (!container) return;
  container.innerHTML = renderEmulatorConfigTree(queryValue);
  updateSelectedEmulatorConfigTreeRow();
}
function toggleEmulatorConfigFolder(key) {
  const normalized = String(key || "");
  if (!normalized) return;
  if (emulatorConfigTreeExpanded.has(normalized)) {
    emulatorConfigTreeExpanded.delete(normalized);
  } else {
    emulatorConfigTreeExpanded.add(normalized);
  }
  renderEmulatorConfigTreeIntoContainer();
}
function expandEmulatorConfigAncestors(index) {
  const row = emulatorConfigRows[index];
  if (!row) return;
  const parts = emulatorConfigPathParts(row).slice(0, -1);
  let key = "";
  parts.forEach((part) => {
    key = key ? `${key}/${part}` : part;
    emulatorConfigTreeExpanded.add(key);
  });
}
function updateSelectedEmulatorConfigTreeRow() {
  document.querySelectorAll("#emulatorConfigSources [data-config-index]").forEach((node) => {
    node.classList.toggle("is-active", Number(node.dataset.configIndex) === selectedEmulatorConfigIndex);
  });
}
async function selectEmulatorConfig(index) {
  const row = emulatorConfigRows[index];
  if (!row) return;
  const requestId = ++emulatorConfigSelectionRequestId;
  selectedEmulatorConfigIndex = index;
  selectedEmulatorConfigVersionIndex = 0;
  expandEmulatorConfigAncestors(index);
  renderEmulatorConfigTreeIntoContainer();
  updateSelectedEmulatorConfigTreeRow();
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
  renderEmulatorConfigTreeIntoContainer(query);
  const visible = Array.from(document.querySelectorAll("#emulatorConfigSources [data-config-index]"));
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
            <pre id="configContent" class="mono admin-config-content bg-dark text-light p-3" style="max-height: 600px; overflow-y: auto; white-space: pre-wrap;">Select an emulator from the left panel to view its config.</pre>
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

function syncMusicVolumeControls(musicVolume) {
  const slider = document.getElementById("musicVolumeSlider");
  const value = document.getElementById("musicVolumeValue");
  if (!slider || musicVolume === undefined || musicVolume === null) return;
  slider.value = String(musicVolume);
  slider.disabled = false;
  if (value) value.textContent = `${musicVolume}%`;
}

function syncScreensaverControls(screensaverMinutes) {
  const slider = document.getElementById("screensaverSlider");
  const value = document.getElementById("screensaverValue");
  if (!slider || screensaverMinutes === undefined || screensaverMinutes === null) return;
  slider.value = String(screensaverMinutes);
  slider.disabled = false;
  if (value) value.textContent = Number(screensaverMinutes) === 0 ? "Off" : `${screensaverMinutes} min`;
}

function syncScreenModeControls(mode) {
  const current = document.getElementById("screenModeCurrent");
  if (current) current.textContent = mode ? `Current: ${mode}` : "not yet reported";
  document.querySelectorAll('#screenModeButtons [data-screen-mode]').forEach((btn) => {
    const isActive = btn.dataset.screenMode === mode;
    btn.classList.toggle("btn-primary", isActive);
    btn.classList.toggle("btn-outline-primary", !isActive);
    btn.disabled = isActive;
  });
}

async function loadScreenMode() {
  try {
    const payload = await api("/admin/system-info/screen-mode");
    syncScreenModeControls(payload.screen_mode);
  } catch (err) {
    const current = document.getElementById("screenModeCurrent");
    if (current) current.textContent = "Unavailable";
  }
}

async function applyDroneScreenMode(mode) {
  if (!window.confirm(`Set screen mode to ${mode} and restart EmulationStation now?`)) return;
  try {
    const result = await apiPost("/admin/system-info/screen-mode", {mode});
    syncScreenModeControls(result.screen_mode);
    const outcome = result.emulationstation_restarted
      ? "EmulationStation restarted."
      : "EmulationStation was already in that mode; restart skipped.";
    showToast(`Screen mode set to ${result.screen_mode}; ${outcome}`, "success");
  } catch (err) {
    showToast(`Failed to set screen mode: ${escapeHtml(err.message || "unknown error")}`, "danger");
  }
}

// Sanitizes a system/collection name into a value safe to use in an HTML
// id/for attribute -- system names are normally already id-safe ("nes",
// "arcade"), but custom collection names come from a directory glob
// (custom-*.cfg stems) and could contain spaces or other characters an id
// attribute can't. Real live bug: this function was called from
// renderEsCheckboxGrid below since the Game Collections feature's very
// first commit but was never actually defined anywhere in this file --
// every call threw "cssSafeId is not defined", which broke not just the
// Game Collections section but silently took the Music Volume and
// Screensaver sliders down with it too (their enable-and-populate step,
// syncMusicVolumeControls/syncScreensaverControls, runs later in the same
// renderEsCollectionsBody call and never got reached once this exception
// propagated out of it -- see the outer try/catch in
// renderAdminControlsPage, which only knew to show "Unable to load
// collections" and had no idea two unrelated sliders were casualties).
function cssSafeId(value) {
  return String(value || "").replace(/[^a-zA-Z0-9_-]+/g, "-");
}

function renderEsCheckboxGrid(items, field) {
  if (!items.length) return '<div class="small text-muted">None found.</div>';
  return `<div class="row row-cols-2 row-cols-md-3 row-cols-lg-4 g-1">
    ${items.map((item) => `
      <div class="col">
        <div class="form-check">
          <input class="form-check-input" type="checkbox" data-es-field="${field}" data-es-name="${escapeHtml(item.name)}" id="es-${field}-${cssSafeId(item.name)}" ${item.checked ? "checked" : ""}>
          <label class="form-check-label small" for="es-${field}-${cssSafeId(item.name)}">${escapeHtml(item.label)}</label>
        </div>
      </div>
    `).join("")}
  </div>`;
}

function renderEsCollectionsCard(state) {
  const systems = state.systems || [];
  const groups = state.groups || [];
  const autoCollections = state.auto_collections || [];
  const customCollections = state.custom_collections || [];
  const groupsHtml = groups.length
    ? groups.map((group) => `
      <div class="mb-2 es-collections-group">
        <div class="small fw-semibold text-muted text-uppercase">${escapeHtml(group.group)}</div>
        ${renderEsCheckboxGrid((group.children || []).map((c) => ({name: c.name, label: c.full_name || c.name, checked: c.grouped})), "grouped")}
      </div>
    `).join("")
    : '<div class="small text-muted">No groupable systems found.</div>';
  return `
    <div class="mb-3 es-collections-section">
      <div class="fw-semibold mb-1">Systems Displayed</div>
      ${renderEsCheckboxGrid(systems.map((s) => ({name: s.name, label: s.full_name || s.name, checked: s.displayed})), "displayed")}
    </div>
    <div class="mb-3 es-collections-section">
      <div class="fw-semibold mb-1">Grouped Systems</div>
      <div class="small text-muted mb-2">Checked systems stay folded into their group's shared entry; uncheck to show a system standalone.</div>
      ${groupsHtml}
    </div>
    <div class="mb-3 es-collections-section">
      <div class="fw-semibold mb-1">Automatic Game Collections</div>
      ${renderEsCheckboxGrid(autoCollections.map((a) => ({name: a.name, label: a.label || a.name, checked: a.enabled})), "auto")}
    </div>
    <div class="mb-0 es-collections-section">
      <div class="fw-semibold mb-1">Custom Game Collections</div>
      ${renderEsCheckboxGrid(customCollections.map((c) => ({name: c.name, label: c.name, checked: c.enabled})), "custom")}
    </div>
    <button class="btn btn-primary mt-3" id="esCollectionsSaveBtn"><i class="bi bi-save me-1"></i>Save</button>
  `;
}

// Client-side, filters what's already loaded (no re-fetch) -- toggles
// visibility per checkbox item, folds up now-empty groups/sections, and
// re-runs automatically after any re-render (Save/Refresh) so an active
// search stays applied instead of silently resetting to unfiltered.
function filterEsCollections(rawQuery) {
  const body = document.getElementById("esCollectionsBody");
  if (!body) return;
  const query = String(rawQuery || "").trim().toLowerCase();
  let anyVisible = false;
  body.querySelectorAll(".es-collections-section").forEach((section) => {
    let sectionHasVisible = false;
    section.querySelectorAll(".form-check").forEach((check) => {
      const text = (check.querySelector("label")?.textContent || "").toLowerCase();
      const match = !query || text.includes(query);
      check.closest(".col")?.classList.toggle("d-none", !match);
      if (match) sectionHasVisible = true;
    });
    section.querySelectorAll(".es-collections-group").forEach((group) => {
      const groupHasVisible = Array.from(group.querySelectorAll(".col")).some((col) => !col.classList.contains("d-none"));
      group.classList.toggle("d-none", !groupHasVisible);
    });
    section.classList.toggle("d-none", Boolean(query) && !sectionHasVisible);
    if (sectionHasVisible) anyVisible = true;
  });
  document.getElementById("esCollectionsNoMatches")?.classList.toggle("d-none", !query || anyVisible);
}

function wireEsCollectionsSearch() {
  const input = document.getElementById("esCollectionsSearchInput");
  if (!input || input.dataset.wired === "1") return;
  input.dataset.wired = "1";
  input.addEventListener("input", () => filterEsCollections(input.value));
}

function collectEsCollectionsPayload() {
  const container = document.getElementById("esCollectionsBody");
  if (!container) return {};
  const names = (field, wantChecked) => Array.from(container.querySelectorAll(`input[data-es-field="${field}"]`))
    .filter((el) => el.checked === wantChecked)
    .map((el) => el.dataset.esName);
  return {
    hidden_systems: names("displayed", false),
    ungrouped_systems: names("grouped", false),
    auto_collections: names("auto", true),
    custom_collections: names("custom", true),
  };
}

function wireEsCollectionsSaveButton() {
  const saveBtn = document.getElementById("esCollectionsSaveBtn");
  if (!saveBtn) return;
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const updated = await apiPost("/admin/es-collections", collectEsCollectionsPayload());
      renderEsCollectionsBody(updated);
      showToast("EmulationStation collections updated; EmulationStation restarted.", "success");
    } catch (err) {
      showToast(`Failed to update collections: ${escapeHtml(err.message || "unknown error")}`, "danger");
      const btn = document.getElementById("esCollectionsSaveBtn");
      if (btn) btn.disabled = false;
    }
  });
}

function renderEsCollectionsBody(state) {
  // Music Volume/Screensaver come from the same GET as the systems/
  // collections list (one es_settings.cfg-backed endpoint), but they're
  // otherwise unrelated features -- sync them first, before anything that
  // renders the (much larger, more failure-prone) collections grid, so a
  // bug in that rendering can never again silently take the sliders down
  // with it the way the missing cssSafeId definition just did (that
  // exception aborted this whole function before reaching either sync
  // call, leaving both sliders permanently stuck disabled).
  syncMusicVolumeControls(state.music_volume);
  syncScreensaverControls(state.screensaver_minutes);
  const body = document.getElementById("esCollectionsBody");
  if (!body) return;
  body.innerHTML = renderEsCollectionsCard(state);
  wireEsCollectionsSaveButton();
  wireEsCollectionsSearch();
  filterEsCollections(document.getElementById("esCollectionsSearchInput")?.value || "");
}

async function loadEsCollections() {
  const payload = await api("/admin/es-collections");
  renderEsCollectionsBody(payload);
}
function renderUpdateHistoryEntry(entry) {
  const version = escapeHtml(entry.version || "unknown");
  const previous = entry.previous_version ? `<span class="text-muted">${escapeHtml(entry.previous_version)} &rarr;</span> ` : "";
  const link = entry.release_url
    ? `<a href="${escapeHtml(entry.release_url)}" target="_blank" rel="noopener noreferrer" class="small"><i class="bi bi-box-arrow-up-right me-1"></i>View on GitHub</a>`
    : "";
  const notes = entry.release_notes
    ? `<pre class="update-history-notes small mb-0">${escapeHtml(entry.release_notes)}</pre>`
    : `<div class="small text-muted">No commit notes available for this update.</div>`;
  return `
    <div class="update-history-entry">
      <div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
        <div>${previous}<strong>${version}</strong></div>
        <div class="d-flex align-items-center gap-2">
          <span class="small text-muted">${escapeHtml(formatCompactLocalDate(entry.applied_at) || entry.applied_at || "")}</span>
          ${link}
        </div>
      </div>
      ${notes}
    </div>
  `;
}

function renderUpdateHistorySection(updates) {
  if (!updates.length) {
    return `<div class="text-muted">This Drone hasn't recorded any self-updates yet.</div>`;
  }
  return `<div class="update-history-list">${updates.map(renderUpdateHistoryEntry).join("")}</div>`;
}

async function renderAdminSystemInfoPage() {
  titleNode.textContent = "System Info";
  subtitleNode.textContent = "Runtime, network, and Batocera details";
  setLoading(true, "Loading system information...");
  try {
    const [payload, updateHistoryPayload] = await Promise.all([
      api("/admin/system-info?speed=1"),
      api("/admin/system/update-history").catch(() => ({ updates: [] })),
    ]);
    const updateHistory = Array.isArray(updateHistoryPayload.updates) ? updateHistoryPayload.updates : [];
    const entries = Array.isArray(payload.entries) ? payload.entries : [];
    const fields = payload.fields || {};
    const metrics = payload.runtime_metrics || {};
    const cpu = metrics.cpu || {};
    const memory = metrics.memory || {};
    const disk = metrics.disk || {};
    const disks = Array.isArray(metrics.disks) && metrics.disks.length ? metrics.disks : [disk];
    const process = metrics.process || {};
    const speed = payload.speed_sample || {};
    const tailnet = payload.tailnet_status || {};
    const tailnetPeers = Array.isArray(tailnet.peers) ? tailnet.peers : [];
    const tailnetHealth = Array.isArray(tailnet.health) ? tailnet.health.filter(Boolean) : [];
    const tailnetConnected = tailnet.installed === true && tailnet.running === true && tailnet.backend_state === "Running";
    const tailnetState = !tailnet.installed
      ? "Not installed"
      : (!tailnet.running
        ? "Daemon offline"
        : (tailnetConnected ? "Connected" : (tailnet.backend_state || (tailnet.enrolled ? "Connecting" : "Not connected"))));
    const tailnetTone = tailnetConnected ? "success" : (tailnet.running ? "warning" : "danger");
    const tailnetName = tailnet.tailnet_name || tailnet.magic_dns_suffix || "n/a";
    const tailnetHealthText = tailnetHealth.length
      ? tailnetHealth.join(" · ")
      : (tailnetConnected ? "Healthy" : "No health information reported");
    const pixnInstalled = payload.pixen_installed === true || fields.pixen_installed === true || String(fields.pixen_installed || "").toLowerCase() === "yes";
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
      ${renderDebugTabBar("system-info")}
      <div class="mb-3 d-flex flex-wrap justify-content-end gap-2">
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
      <div class="card log-card mb-3">
        <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
          <span><i class="bi bi-diagram-3 me-2"></i>Tailnet / Tailscale</span>
          <div class="d-flex align-items-center gap-2">
            <span class="badge text-bg-${tailnetTone}">${escapeHtml(tailnetState)}</span>
            <button class="btn btn-sm btn-outline-primary" type="button" onclick="setHash('#admin/logs/tailscaled?lines=200')"><i class="bi bi-journal-text me-1"></i>View Logs</button>
          </div>
        </div>
        <div class="card-body">
          <div class="row g-3">
            <div class="col-12 col-lg-6">
              <div class="asset-detail-panel h-100">
                <h6>Connection</h6>
                ${detail("Status", tailnetState)}
                ${detail("Installed", tailnet.installed ? "Yes" : "No")}
                ${detail("Daemon", tailnet.running ? "Running" : "Stopped")}
                ${detail("Backend state", tailnet.backend_state)}
                ${detail("Tailnet IP", tailnet.tailnet_ip)}
                ${detail("Online peers", String(tailnetPeers.length))}
              </div>
            </div>
            <div class="col-12 col-lg-6">
              <div class="asset-detail-panel h-100">
                <h6>Identity &amp; Health</h6>
                ${detail("Version", tailnet.version)}
                ${detail("Hostname", tailnet.hostname)}
                ${detail("DNS name", tailnet.dns_name)}
                ${detail("Tailnet", tailnetName)}
                ${detail("DERP relay", tailnet.relay)}
                ${detail("Health", tailnetHealthText)}
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
                ${detail("Network IP", fields.network_ip_address)}
                ${detail("Router IP", fields.router_ip_address)}
                ${detail("Batocera", fields.batocera_version)}
                ${detail("PixN", pixnInstalled ? "Installed" : "Not installed")}
              </div>
            </div>
            <div class="col-12 col-lg-6">
              <div class="asset-detail-panel h-100">
                <h6>Hardware</h6>
                ${detail("Model", fields.model)}
                ${detail("Architecture", fields.architecture)}
                ${detail("CPU", fields.cpu_model || fields.cpu_topology)}
                <h6 class="mt-3">GPU</h6>
                ${detail("Vendor", fields.gpu_vendor)}
                ${detail("Model", fields.gpu_model)}
                ${detail("Driver", fields.gpu_driver)}
                ${renderedRows}
              </div>
            </div>
          </div>
        </div>
      </div>
      <div class="card log-card mt-3">
        <div class="card-header"><i class="bi bi-clock-history me-2"></i>Update History</div>
        <div class="card-body">
          ${renderUpdateHistorySection(updateHistory)}
        </div>
      </div>
    `;
  } catch (err) {
    showToast(`Failed to load system information: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="text-muted">System information could not be loaded.</div>
    `;
  } finally {
    setLoading(false);
  }
}

async function renderAdminControlsPage() {
  titleNode.textContent = "Controls";
  subtitleNode.textContent = "Screen mode, volume, screensaver, and EmulationStation configuration";
  setLoading(true, "Loading controls...");
  try {
    const [payload, autoUpdate] = await Promise.all([
      api("/admin/system-info"),
      api("/admin/system/auto-update"),
    ]);
    const fields = payload.fields || {};
    const pixnInstalled = payload.pixen_installed === true || fields.pixen_installed === true || String(fields.pixen_installed || "").toLowerCase() === "yes";
    const rawVolume = payload.audio_volume ?? fields.audio_volume;
    const reportedVolume = Number(rawVolume);
    const volumeAvailable = rawVolume !== null && rawVolume !== undefined && Number.isFinite(reportedVolume);
    const currentVolume = volumeAvailable ? Math.max(0, Math.min(100, Math.round(reportedVolume / 5) * 5)) : 50;

    content.innerHTML = `
      <div class="mb-3 d-flex flex-wrap justify-content-end gap-2">
          <button class="btn btn-outline-primary" onclick="setHash('#admin/controls')"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
          <div class="form-check form-switch mb-0 px-2 d-flex align-items-center">
            <input class="form-check-input ms-0 me-2" type="checkbox" role="switch" id="droneAutoUpdateCheckbox" ${autoUpdate.enabled ? "checked" : ""} onchange="setDroneAutoUpdate(this)">
            <label class="form-check-label text-nowrap" for="droneAutoUpdateCheckbox" title="The Drone API worker checks every 60 seconds and installs both UI bundles in the background">Auto-update Drone</label>
          </div>
          <button class="btn btn-outline-warning" onclick="updateDroneApp()"><i class="bi bi-cloud-download me-1"></i>Update Drone</button>
          <button class="btn btn-outline-danger" id="restartEsBtn" onclick="restartEmulationStation()"><i class="bi bi-arrow-clockwise me-1"></i>Restart EmulationStation</button>
          ${pixnInstalled ? `<button class="btn btn-outline-success" onclick="runPixnUpdate()"><i class="bi bi-play-circle me-1"></i>Run PixN Update</button>` : ""}
      </div>
      <div class="row row-cols-1 row-cols-sm-2 row-cols-xl-4 g-3 mb-3">
        <div class="col">
          <div class="card control-tile h-100">
            <div class="card-header d-flex justify-content-between align-items-center gap-2">
              <span><i class="bi bi-display me-2"></i>Screen Mode</span>
              <span class="small text-muted" id="screenModeCurrent">Loading...</span>
            </div>
            <div class="card-body">
              <div class="btn-group bff-segmented w-100" role="group" aria-label="Screen mode" id="screenModeButtons">
                <button class="btn btn-outline-primary btn-sm" type="button" data-screen-mode="full" onclick="applyDroneScreenMode('full')"><i class="bi bi-unlock me-1"></i>Full</button>
                <button class="btn btn-outline-primary btn-sm" type="button" data-screen-mode="kiosk" onclick="applyDroneScreenMode('kiosk')"><i class="bi bi-lock me-1"></i>Kiosk</button>
                <button class="btn btn-outline-primary btn-sm" type="button" data-screen-mode="kid" onclick="applyDroneScreenMode('kid')"><i class="bi bi-person me-1"></i>Kid</button>
              </div>
              <div class="small text-muted mt-2">Restarts EmulationStation.</div>
            </div>
          </div>
        </div>
        <div class="col">
          <div class="card control-tile h-100">
            <div class="card-header d-flex justify-content-between align-items-center gap-2">
              <span><i class="bi bi-volume-up me-2"></i>Volume</span>
              <output id="systemVolumeValue" for="systemVolumeSlider" class="badge text-bg-primary">${volumeAvailable ? `${currentVolume}%` : "Unavailable"}</output>
            </div>
            <div class="card-body">
              <div class="d-flex align-items-center gap-2">
                <i class="bi bi-volume-mute" aria-hidden="true"></i>
                <input class="form-range flex-grow-1" type="range" id="systemVolumeSlider" min="0" max="100" step="5" value="${currentVolume}" aria-label="System volume" ${volumeAvailable ? "" : "disabled"}>
                <i class="bi bi-volume-up" aria-hidden="true"></i>
              </div>
            </div>
          </div>
        </div>
        <div class="col">
          <div class="card control-tile h-100">
            <div class="card-header d-flex justify-content-between align-items-center gap-2">
              <span><i class="bi bi-music-note-beamed me-2"></i>Music Volume</span>
              <output id="musicVolumeValue" for="musicVolumeSlider" class="badge text-bg-primary">--</output>
            </div>
            <div class="card-body">
              <div class="d-flex align-items-center gap-2">
                <i class="bi bi-volume-mute" aria-hidden="true"></i>
                <input class="form-range flex-grow-1" type="range" id="musicVolumeSlider" min="0" max="100" step="5" value="80" aria-label="Music volume" disabled>
                <i class="bi bi-volume-up" aria-hidden="true"></i>
              </div>
              <div class="small text-muted mt-2">Restarts EmulationStation.</div>
            </div>
          </div>
        </div>
        <div class="col">
          <div class="card control-tile h-100">
            <div class="card-header d-flex justify-content-between align-items-center gap-2">
              <span><i class="bi bi-moon-stars me-2"></i>Screensaver</span>
              <output id="screensaverValue" for="screensaverSlider" class="badge text-bg-primary">--</output>
            </div>
            <div class="card-body">
              <div class="d-flex align-items-center gap-2">
                <i class="bi bi-moon" aria-hidden="true"></i>
                <input class="form-range flex-grow-1" type="range" id="screensaverSlider" min="0" max="120" step="1" value="5" aria-label="Screensaver delay in minutes" disabled>
                <span class="small text-muted text-nowrap">min</span>
              </div>
              <div class="small text-muted mt-2">Restarts EmulationStation.</div>
            </div>
          </div>
        </div>
      </div>
      <div class="card log-card mb-3">
        <div class="card-header d-flex justify-content-between align-items-center gap-2">
          <span><i class="bi bi-collection-play me-2"></i>Game Collections &amp; Systems</span>
          <button id="esCollectionsRefreshBtn" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
        </div>
        <div class="small text-muted px-3 pt-3">Which systems appear, which are grouped together, and which automatic/custom collections are enabled. Saving restarts EmulationStation.</div>
        <div class="px-3 pt-2">
          <input type="search" class="form-control form-control-sm" id="esCollectionsSearchInput" placeholder="Filter systems and collections...">
          <div class="small text-muted mt-2 d-none" id="esCollectionsNoMatches">No systems or collections match that search.</div>
        </div>
        <div class="es-collections-scroll" id="esCollectionsScroll">
          <div class="card-body" id="esCollectionsBody"><div class="text-muted">Loading...</div></div>
        </div>
        <div class="es-collections-toggle">
          <button type="button" class="es-collections-toggle-btn" id="esCollectionsToggleBtn" aria-expanded="false" aria-controls="esCollectionsScroll">
            <i class="bi bi-chevron-down"></i> <span class="es-collections-toggle-label">Show more</span>
          </button>
        </div>
      </div>
      <div class="card log-card">
        <div class="card-header d-flex justify-content-between align-items-center">
          <span><i class="bi bi-database-check me-2"></i>Asset Cache</span>
          <div class="d-flex gap-2">
            <button id="systemInfoAssetCacheRefreshBtn" class="btn btn-sm btn-outline-primary" type="button"><i class="bi bi-arrow-repeat me-1"></i>Refresh</button>
            <button class="btn btn-sm btn-outline-danger" type="button" onclick="purgeAssetCache()">Purge &amp; Resync</button>
          </div>
        </div>
        <div class="card-body" id="systemInfoAssetCacheBody"><div class="text-muted">Loading asset cache...</div></div>
      </div>
    `;

    const volumeSlider = document.getElementById("systemVolumeSlider");
    const volumeValue = document.getElementById("systemVolumeValue");
    let appliedVolume = currentVolume;
    if (volumeSlider && volumeValue && volumeAvailable) {
      volumeSlider.addEventListener("input", () => {
        volumeValue.textContent = `${volumeSlider.value}%`;
      });
      volumeSlider.addEventListener("change", async () => {
        const requestedVolume = Number(volumeSlider.value);
        volumeSlider.disabled = true;
        try {
          const result = await apiPost("/admin/system-info/volume", {level: requestedVolume});
          appliedVolume = Number(result.audio_volume);
          volumeSlider.value = String(appliedVolume);
          volumeValue.textContent = `${appliedVolume}%`;
          showToast(`Volume set to ${appliedVolume}%.`, "success");
        } catch (err) {
          volumeSlider.value = String(appliedVolume);
          volumeValue.textContent = `${appliedVolume}%`;
          showToast(`Failed to set volume: ${escapeHtml(err.message || "unknown error")}`, "danger");
        } finally {
          volumeSlider.disabled = false;
        }
      });
    }

    const musicVolumeSlider = document.getElementById("musicVolumeSlider");
    const musicVolumeValue = document.getElementById("musicVolumeValue");
    if (musicVolumeSlider && musicVolumeValue) {
      musicVolumeSlider.addEventListener("input", () => {
        musicVolumeValue.textContent = `${musicVolumeSlider.value}%`;
      });
      musicVolumeSlider.addEventListener("change", async () => {
        musicVolumeSlider.disabled = true;
        try {
          const result = await apiPost("/admin/system-info/music-volume", {level: Number(musicVolumeSlider.value)});
          syncMusicVolumeControls(result.music_volume);
          showToast(`Music volume set to ${result.music_volume}%; EmulationStation restarted.`, "success");
        } catch (err) {
          showToast(`Failed to set music volume: ${escapeHtml(err.message || "unknown error")}`, "danger");
        } finally {
          musicVolumeSlider.disabled = false;
        }
      });
    }

    const screensaverSlider = document.getElementById("screensaverSlider");
    const screensaverValue = document.getElementById("screensaverValue");
    if (screensaverSlider && screensaverValue) {
      screensaverSlider.addEventListener("input", () => {
        screensaverValue.textContent = Number(screensaverSlider.value) === 0 ? "Off" : `${screensaverSlider.value} min`;
      });
      screensaverSlider.addEventListener("change", async () => {
        screensaverSlider.disabled = true;
        try {
          const result = await apiPost("/admin/es-collections", {screensaver_minutes: Number(screensaverSlider.value)});
          syncScreensaverControls(result.screensaver_minutes);
          showToast(`Screensaver delay set to ${result.screensaver_minutes} min.; EmulationStation restarted.`, "success");
        } catch (err) {
          showToast(`Failed to set screensaver delay: ${escapeHtml(err.message || "unknown error")}`, "danger");
        } finally {
          screensaverSlider.disabled = false;
        }
      });
    }

    loadScreenMode();

    const esCollectionsToggleBtn = document.getElementById("esCollectionsToggleBtn");
    const esCollectionsScroll = document.getElementById("esCollectionsScroll");
    esCollectionsToggleBtn?.addEventListener("click", () => {
      const expanded = esCollectionsScroll.classList.toggle("expanded");
      esCollectionsToggleBtn.classList.toggle("expanded", expanded);
      esCollectionsToggleBtn.setAttribute("aria-expanded", String(expanded));
      esCollectionsToggleBtn.querySelector(".es-collections-toggle-label").textContent = expanded ? "Show less" : "Show more";
      if (!expanded) esCollectionsScroll.scrollIntoView({block: "nearest"});
    });

    document.getElementById("esCollectionsRefreshBtn")?.addEventListener("click", async () => {
      try {
        await loadEsCollections();
      } catch (err) {
        showToast(`Failed to load collections: ${escapeHtml(err.message || "unknown error")}`, "danger");
      }
    });
    try {
      await loadEsCollections();
    } catch (err) {
      const collectionsBody = document.getElementById("esCollectionsBody");
      if (collectionsBody) collectionsBody.innerHTML = '<div class="empty-state">Unable to load collections.</div>';
    }

    // Navigation can remove the Controls DOM while its API requests are still
    // completing. Do not start another request or render into the next page.
    if (window.location.hash !== "#admin/controls") return;

    async function loadAssetCache() {
      const cachePayload = await api("/admin/asset-cache");
      const cacheBody = document.getElementById("systemInfoAssetCacheBody");
      if (!cacheBody || window.location.hash !== "#admin/controls") return;
      cacheBody.innerHTML = renderAssetCachePanel(cachePayload, false);
    }
    window.refreshSystemInfoAssetCache = loadAssetCache;
    document.getElementById("systemInfoAssetCacheRefreshBtn")?.addEventListener("click", async () => {
      try {
        await loadAssetCache();
      } catch (err) {
        showToast(`Failed to load asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
      }
    });
    try {
      await loadAssetCache();
    } catch (err) {
      showToast(`Failed to load asset cache: ${escapeHtml(err.message || "unknown error")}`, "danger");
    }
  } catch (err) {
    showToast(`Failed to load controls: ${escapeHtml(err.message || "unknown error")}`, "danger");
    content.innerHTML = `
      <div class="text-muted">Controls could not be loaded.</div>
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
function _sysInfoBadge(innerHtml, hash, title, style = "") {
  return `<button type="button" class="badge sysinfo-badge-btn" style="${style}" onclick="setHash('${hash}')" title="${escapeHtml(title)}">${innerHtml}</button>`;
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
    const version = fields.batocera_version;
    const droneAppVersion = fields.drone_app_version || payload.drone_app_version || "";
    const machineId = fields.machine_id || "";
    const chips = [];
    if (droneVersionBadge && droneAppVersion) {
      droneVersionBadge.textContent = droneAppVersion;
      droneVersionBadge.classList.remove("d-none");
    }
    if (version) chips.push(_sysInfoBadge(`Batocera: ${escapeHtml(version)}`, "#admin/system-info", "Open System Info"));
    if (machineId) chips.push(_sysInfoBadge(`Machine ID: ${escapeHtml(machineId)}`, "#admin/system-info", "Open System Info"));
    try {
      const network = await api("/admin/local-network/status");
      const pairedCount = Number(network.paired_count || 0);
      chips.push(_sysInfoBadge(`Paired: ${pairedCount}`, "#admin/swarm", "Open Swarm", "background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)"));
    } catch (_) {
      // Paired-device status is best-effort context, not core system info.
    }
    try {
      const vpn = await api("/admin/vpn");
      const vpnConnected = vpn.status === "connected";
      const vpnStyle = vpnConnected
        ? "background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)"
        : "background:rgba(148,163,184,0.15);color:#94a3b8;border-color:rgba(148,163,184,0.4)";
      chips.push(_sysInfoBadge(`<i class="bi bi-shield-lock me-1"></i>VPN: ${vpnConnected ? "Connected" : "Disconnected"}`, "#admin/vpn", "Open VPN", vpnStyle));
    } catch (_) {
      // VPN status is best-effort context, not core system info.
    }
    try {
      const smtp = await api("/admin/smtp");
      const emailOn = !!smtp.smtp_enabled;
      const emailStyle = emailOn
        ? "background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)"
        : "background:rgba(148,163,184,0.15);color:#94a3b8;border-color:rgba(148,163,184,0.4)";
      chips.push(_sysInfoBadge(`<i class="bi bi-envelope me-1"></i>Email: ${emailOn ? "On" : "Off"}`, "#admin/smtp", "Open Email", emailStyle));
    } catch (_) {
      // SMTP status is best-effort context, not core system info.
    }
    try {
      const networkShares = await api("/admin/network-shares");
      const shares = networkShares.shares || [];
      if (shares.length) {
        const allHealthy = shares.every((share) => share.status === "mounted");
        const shareStyle = allHealthy
          ? "background:rgba(52,211,153,0.15);color:#34d399;border-color:rgba(52,211,153,0.4)"
          : "background:rgba(251,191,36,0.15);color:#fbbf24;border-color:rgba(251,191,36,0.4)";
        chips.push(_sysInfoBadge(`<i class="bi bi-hdd-network me-1"></i>Referencing: ${shares.length}`, "#admin/swarm", "Open Swarm", shareStyle));
      }
    } catch (_) {
      // Network share status is best-effort context, not core system info.
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
// Silently updates the URL and re-renders for it immediately, in the same
// async chain -- unlike setHash() (a real hashchange, handled by the
// separately-scheduled "hashchange" listener invocation below), this
// doesn't return until the redirected-to page has actually finished
// rendering. Load-bearing for router()'s internal redirects (empty hash ->
// "#movies", "#bios" -> the Systems Browse BIOS entry, the legacy
// "#system/X" hash): using plain setHash()+return there used to race
// startApp()'s own back-to-back double `await router()` calls (an
// immediate render, then a second one once theme init finishes) -- both
// landing on the same redirected-to hash right on top of each other, so
// neither's async render (e.g. renderMovieExplorerPage's /movies fetch)
// ever got to be the sole "latest" one; each completion kept finding a
// newer call had started meanwhile and retried via router()'s own
// stale-token self-heal, forever (live-reproduced: thousands of req/s
// hammering /movies, CPU pegged, never settling on a fresh login).
async function redirectRouterHash(hash) {
  history.replaceState(null, "", hash);
  await router();
}
// retryDepth: bounds the stale-token self-heal below (see its own comment)
// so two or more router() calls that keep invalidating each other's "am I
// still the latest?" check can never retry one another forever. Confirmed
// live before this cap existed: exactly two overlapping calls landing on
// the same hash right on top of each other was enough to livelock --
// thousands of req/s hammering /movies, CPU pegged, never settling until a
// hard refresh (see redirectRouterHash's docstring for the one specific
// trigger of that shape that's since been fixed at the source; this cap is
// the backstop for any other pair of overlapping triggers hitting the same
// underlying race, not a fix for one specific trigger).
async function router(retryDepth = 0) {
  const myNavToken = ++routerNavToken;
  clearError();
  const outgoingScrollBucket = movieListScrollBucket(lastRenderedHash);
  if (outgoingScrollBucket) {
    const main = document.querySelector("main");
    movieListScrollPositions[outgoingScrollBucket] = { windowY: window.scrollY, mainTop: main ? main.scrollTop : 0 };
  }
  scrollContentToTop();
  try {
    const hash = window.location.hash || "";
    lastRenderedHash = hash;
    if (!hash.startsWith("#admin/logs/")) {
      stopLogAutoRefresh();
      currentLogSource = null;
    }
    if (hash !== "#admin/transfers") {
      stopTransfersAutoRefresh();
    }
    if (hash !== "#admin/torrents") {
      stopTorrentsAutoRefresh();
    }
    if (hash !== "#admin/config-backups") {
      stopConfigBackupsAutoRefresh();
    }
    if (hash !== "#admin/vpn") {
      stopVpnAutoRefresh();
    }
    if (hash !== "#admin/smtp") {
      stopSmtpAutoRefresh();
    }
    if (hash !== "#admin/movies") {
      stopMovieBulkScrapeAutoRefresh();
    }
    if (hash !== "#admin/music") {
      stopMusicBulkScrapeAutoRefresh();
    }
    document.body.classList.toggle("artwork-page", hash.startsWith("#admin/artwork"));
    // The Systems Browse grid reuses the movie-explorer-* full-bleed chrome-
    // takeover CSS wholesale (see renderSystemsExplorePage) rather than a
    // parallel duplicate set -- the class name predates this page but the
    // takeover behavior is identical, so it's shared instead of cloned.
    // Browse is the only Movies/Systems view now, so bare "#movies"/
    // "#systems" get it too, not just their "/explore" spelling -- except a
    // movie/show *detail* page (still "#movies/...") isn't the full-bleed
    // grid, so those are excluded explicitly. "#systems" never collides with
    // the ROM detail page's hash (singular "#system/...", handled by
    // parseSystemRomHash), so no equivalent carve-out is needed there.
    const moviesHashParsedForChrome = parseMoviesHash(hash);
    const isMoviesExplorerRoute = hash.startsWith("#movies")
      && (!moviesHashParsedForChrome || (moviesHashParsedForChrome.view !== "detail" && moviesHashParsedForChrome.view !== "show"));
    // Music reuses the identical movie-explorer-*/movies-page-active chrome
    // classes (not a parallel music-explorer-active set) -- same "shared CSS,
    // not cloned" convention #systems already follows for Movies' classes.
    const musicHashParsedForChrome = parseMusicHash(hash);
    const isMusicExplorerRoute = hash.startsWith("#music")
      && (!musicHashParsedForChrome || (musicHashParsedForChrome.view !== "detail" && musicHashParsedForChrome.view !== "artist"));
    document.body.classList.toggle("movie-explorer-active", isMoviesExplorerRoute || hash.startsWith("#systems") || isMusicExplorerRoute);
    document.body.classList.toggle("movies-page-active", hash.startsWith("#movies") || hash.startsWith("#music"));
    if (hash === "#bios") {
      await redirectRouterHash(systemsExploreHash(SYSTEMS_EXPLORE_BIOS_KEY));
      return;
    } else if (hash === "" || hash === "#") {
      // Movies loads fast (no gamelist scan involved) and is the page
      // people actually want on open -- the help/tour page is still one
      // click away (or #home/#help directly) for anyone who wants it.
      await redirectRouterHash("#movies");
      return;
    } else if (hash === "#theme") {
      await renderThemeGalleryPage();
    } else if (hash === "#home" || hash === "#help") {
      await renderHelpPage();
    } else if (hash.startsWith("#systems")) {
      // Browse is the only Systems view now -- "#systems" and
      // "#systems/explore" render the same page (the plain form is what
      // every nav link/back-button in the app uses).
      await renderSystemsExplorePage();
    } else if (hash.startsWith("#movies")) {
      const parsed = parseMoviesHash(hash);
      if (parsed && parsed.view === "detail") {
        await renderMovieDetailsPage(parsed.entryKey);
      } else if (parsed && parsed.view === "show") {
        await renderShowDetailsPage(parsed.showTitle, parsed.seasonNumber);
      } else {
        // Browse is the only Movies view now -- "#movies" and
        // "#movies/explore" both parse to view === "explore" and render the
        // same page.
        await renderMovieExplorerPage();
      }
    } else if (hash.startsWith("#music")) {
      const parsed = parseMusicHash(hash);
      if (parsed && parsed.view === "detail") {
        await renderMusicDetailsPage(parsed.entryKey);
      } else if (parsed && parsed.view === "artist") {
        await renderArtistDetailsPage(parsed.artist, parsed.album);
      } else {
        await renderMusicExplorerPage();
      }
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
    } else if (hash === "#admin/controls") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAdminControlsPage();
    } else if (hash.startsWith("#admin/artwork")) {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      const parsed = parseArtworkHash(hash) || { offset: 0, includeFilesystem: false, fields: ["image", "marquee"], systems: [], q: "", romStatus: "any" };
      await renderMissingArtworkPage(parsed.includeFilesystem, false, parsed.offset, parsed.fields, parsed.systems, parsed.q, parsed.romStatus);
    } else if (hash === "#admin/movies") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAdminMoviesArtworkPage();
    } else if (hash === "#admin/music") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAdminMusicArtworkPage();
    } else if (hash === "#admin/downloads") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      setHash("#admin/transfers");
      return;
    } else if (hash === "#admin/asset-cache") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAssetCachePage();
    } else if (hash === "#admin/transfers") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderTransfersPage();
    } else if (hash === "#admin/swarm") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderSwarmPage();
    } else if (
      hash.startsWith("#admin/integration")
      || ["#admin/overmind", "#admin/overmind/actions", "#admin/local-network"].includes(hash)
    ) {
      // Integration (and its Overmind panels) is retired; the Swarm page owns
      // pairing, peers, and the tailnet now.
      if (!adminEnabled) {
        setHash("");
        return;
      }
      setHash("#admin/swarm");
      return;
    } else if (hash === "#admin/api") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderApiAdminPage();
    } else if (hash === "#admin/automation") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderAutomationPage();
    } else if (hash === "#admin/torrents") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderTorrentsPage();
    } else if (hash === "#admin/config-backups") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderConfigBackupsPage();
    } else if (hash === "#admin/vpn") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderVpnPage();
    } else if (hash === "#admin/smtp") {
      if (!adminEnabled) {
        setHash("");
        return;
      }
      await renderSmtpPage();
    } else if (parseSystemRomHash(hash)) {
      const parsed = parseSystemRomHash(hash);
      await renderRomMediaPage(parsed.system, parsed.uniqueId, parsed.page);
    } else if (parseSystemHash(hash)) {
      const parsed = parseSystemHash(hash);
      await redirectRouterHash(systemsExploreHash(parsed.system));
      return;
    } else {
      await renderHelpPage();
    }
    // The awaited render call above may have taken long enough that a newer
    // nav click already fired its own router() call and rendered a
    // different, more current page over top of this one -- in which case
    // this call's own content/title write (already done, deep inside the
    // render function above) is now stale and possibly still on screen.
    // Re-run the router for whatever hash is current *now* so the page
    // self-corrects immediately rather than sitting on the wrong content
    // until another nav click or manual refresh. Capped at 3 retries (see
    // the retryDepth comment above router()'s own declaration) -- past that,
    // whatever router() call is still in flight owns settling the page; one
    // more recursive call here would just be another contender in the same
    // race, not a fix for it.
    if (myNavToken !== routerNavToken) {
      if (retryDepth < 3) {
        await router(retryDepth + 1);
        return;
      }
      // Retry budget exhausted while navigation was still actively
      // changing out from under us -- with the hashchange debounce above,
      // this should now be rare in practice, but a stuck "Loading X..."
      // toast forever is a worse outcome than one that closes a beat early
      // on an unusually long burst. Whichever hash is current renders
      // correctly on the next real navigation regardless.
      setLoading(false);
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
notificationsBellBtn?.addEventListener("show.bs.dropdown", () => {
  notificationsDropdownOpen = true;
  refreshNotificationsDropdown();
});
notificationsBellBtn?.addEventListener("hide.bs.dropdown", () => {
  notificationsDropdownOpen = false;
});
assetsMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  setHash("#systems");
});
controlsMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  if (!adminEnabled) return;
  setHash("#admin/controls");
});
swarmMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  if (!adminEnabled) return;
  setHash("#admin/swarm");
});
adminMenuBtn.addEventListener("click", (event) => {
  event.preventDefault();
  if (!adminEnabled) return;
  setHash("#admin");
});
// Not `addEventListener("hashchange", router)` directly -- that would pass
// the hashchange Event object as router()'s first argument, which is now
// retryDepth (see router()'s own declaration). An Event compared with `< 3`
// coerces to NaN, which is never true, silently disabling the bounded
// stale-token retry for every ordinary hashchange-triggered navigation --
// the single most common way router() actually gets called.
// Debounced, not a direct call: the retryDepth cap above only bounds
// router()'s own recursive self-heal *chain* -- it does nothing to stop
// several genuinely separate hashchange events (e.g. a burst of rapid
// clicks/taps) from each independently starting their own full, concurrent
// router() invocation. Those all share the same DOM (content.innerHTML,
// the loading toast, titleNode) with no cancellation of an in-flight one
// when a newer one starts, so whichever happens to finish last "wins" --
// confirmed live: rapid-clicking through several pages left a "Loading
// controls..." toast stuck on screen forever over stale home-page content,
// with none of the ~5 concurrent renders for that hash ever being the one
// to settle it. 50ms is far below normal click-to-click timing (so a
// single real navigation still feels instant) but comfortably coalesces a
// rapid-click burst down to one router() call for whatever hash is current
// once it settles, which is what should have happened as this specific
// symptom was reproduced.
let hashchangeDebounceTimer = null;
window.addEventListener("hashchange", () => {
  if (hashchangeDebounceTimer) clearTimeout(hashchangeDebounceTimer);
  hashchangeDebounceTimer = setTimeout(() => {
    hashchangeDebounceTimer = null;
    router();
  }, 50);
});
async function startApp() {
  document.querySelector(".nav-actions")?.classList.remove("d-none");
  document.getElementById("logoutBtn")?.classList.remove("d-none");
  document.getElementById("accountSettingsBtn")?.classList.remove("d-none");
  ensureMusicPlayerBar();
  try {
    await api("/admin/configs/sources");
    adminEnabled = true;
  } catch (error) {
    const msg = String(error && error.message ? error.message : "").toLowerCase();
    adminEnabled = !(msg.includes("admin disabled") || msg.includes("request failed: 403"));
  }
  applyAdminVisibility();
  setupStackTables();
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

async function submitLogin() {
  const usernameInput = document.getElementById("loginUsername");
  const passwordInput = document.getElementById("loginPassword");
  const errorNode = document.getElementById("loginError");
  const button = document.getElementById("loginSubmitBtn");
  const username = (usernameInput.value || "").trim();
  const password = passwordInput.value || "";
  errorNode.classList.add("d-none");
  if (!username || !password) {
    errorNode.textContent = "Username and password are required.";
    errorNode.classList.remove("d-none");
    return;
  }
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Signing in...';
  try {
    // Always the local gateway's own login, never proxied to a managed peer
    // (there is no managed peer yet at this point in the boot sequence).
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      let message = "Invalid username or password.";
      try {
        const data = await res.json();
        if (data.error) message = data.error;
      } catch (_) {}
      errorNode.textContent = message;
      errorNode.classList.remove("d-none");
      button.disabled = false;
      button.innerHTML = '<i class="bi bi-box-arrow-in-right me-1"></i>Sign in';
      passwordInput.value = "";
      passwordInput.focus();
      return;
    }
    // A full reload re-runs bootstrapApp() from scratch with the new session
    // cookie already set by the browser -- simpler and more robust than
    // reconstructing all the nav/theme/router state this login view skipped.
    window.location.reload();
  } catch (err) {
    errorNode.textContent = "Could not reach the Drone. Check the connection and try again.";
    errorNode.classList.remove("d-none");
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-box-arrow-in-right me-1"></i>Sign in';
  }
}

async function logout() {
  try {
    await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "include" });
  } catch (_) {
    // Best-effort -- reload either way so a stale/unreachable session cannot
    // leave the UI stuck in a half-logged-in state.
  }
  window.location.reload();
}

function openAccountSettingsModal() {
  const modalId = "accountSettingsModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content themed-modal">
        <div class="modal-header">
          <h5 class="modal-title mb-0"><i class="bi bi-person-gear me-2"></i>Account Settings</h5>
          <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
        </div>
        <div class="modal-body">
          <div id="accountSettingsError" class="alert alert-danger py-2 d-none"></div>
          <div class="small text-muted mb-3">Changing your username or password signs out every other device or browser currently logged in -- this one stays signed in.</div>
          <div class="mb-3">
            <label class="form-label" for="accountSettingsUsername">Username</label>
            <input class="form-control" type="text" id="accountSettingsUsername" autocomplete="username" value="${escapeHtml(currentUsername)}">
            <div class="form-text">3-64 characters: letters, numbers, dot, dash, underscore, or @</div>
          </div>
          <div class="mb-3">
            <label class="form-label" for="accountSettingsPassword">New password</label>
            <input class="form-control" type="password" id="accountSettingsPassword" autocomplete="new-password">
            <div class="form-text">At least 8 characters.</div>
          </div>
          <div class="mb-1">
            <label class="form-label" for="accountSettingsPasswordConfirm">Confirm new password</label>
            <input class="form-control" type="password" id="accountSettingsPasswordConfirm" autocomplete="new-password">
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
          <button type="button" class="btn btn-primary" id="accountSettingsSaveBtn" onclick="submitAccountSettings()"><i class="bi bi-check-lg me-1"></i>Save Changes</button>
        </div>
      </div>
    </div>`;
  if (window.bootstrap?.Modal) {
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
  } else {
    modal.classList.add("show");
    modal.style.display = "block";
  }
  document.getElementById("accountSettingsUsername")?.focus();
}

async function submitAccountSettings() {
  const usernameInput = document.getElementById("accountSettingsUsername");
  const passwordInput = document.getElementById("accountSettingsPassword");
  const confirmInput = document.getElementById("accountSettingsPasswordConfirm");
  const errorNode = document.getElementById("accountSettingsError");
  const button = document.getElementById("accountSettingsSaveBtn");
  const username = (usernameInput.value || "").trim();
  const password = passwordInput.value || "";
  const confirm = confirmInput.value || "";
  errorNode.classList.add("d-none");
  if (!/^[A-Za-z0-9._@-]{3,64}$/.test(username)) {
    errorNode.textContent = "Username must be 3-64 characters using letters, numbers, dot, dash, underscore, or @.";
    errorNode.classList.remove("d-none");
    return;
  }
  if (password.length < 8) {
    errorNode.textContent = "Password must be at least 8 characters.";
    errorNode.classList.remove("d-none");
    return;
  }
  if (password !== confirm) {
    errorNode.textContent = "Passwords do not match.";
    errorNode.classList.remove("d-none");
    return;
  }
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Saving...';
  try {
    await apiPost("/admin/credentials/update", { username, password });
    currentUsername = username;
    const modal = document.getElementById("accountSettingsModal");
    if (window.bootstrap?.Modal && modal) {
      window.bootstrap.Modal.getOrCreateInstance(modal).hide();
    } else if (modal) {
      modal.classList.remove("show");
      modal.style.display = "none";
    }
    showToast("Account credentials updated. Other sessions have been signed out.", "success");
  } catch (err) {
    errorNode.textContent = err.message || "Failed to update credentials.";
    errorNode.classList.remove("d-none");
  } finally {
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-check-lg me-1"></i>Save Changes';
  }
}

function renderLoginPage() {
  document.querySelector(".nav-actions")?.classList.add("d-none");
  document.getElementById("logoutBtn")?.classList.add("d-none");
  document.getElementById("accountSettingsBtn")?.classList.add("d-none");
  const systemInfoBar = document.getElementById("systemInfoBar");
  if (systemInfoBar) systemInfoBar.innerHTML = "";
  titleNode.textContent = "";
  subtitleNode.textContent = "";
  content.innerHTML = `
    <div class="row justify-content-center">
      <div class="col-12 col-sm-8 col-md-5 col-lg-4">
        <div class="card mt-5">
          <div class="card-body p-4">
            <div class="text-center mb-4">
              <img src="/content/batocera-swarm-mascot.jpg" alt="" style="width:56px;height:56px;border-radius:50%;">
              <h4 class="mt-3 mb-0">Batocera Drone</h4>
              <div class="small text-muted">Sign in to continue</div>
            </div>
            <div id="loginError" class="alert alert-danger py-2 d-none"></div>
            <div class="mb-3">
              <label class="form-label" for="loginUsername">Username</label>
              <input class="form-control" type="text" id="loginUsername" autocomplete="username">
            </div>
            <div class="mb-3">
              <label class="form-label" for="loginPassword">Password</label>
              <input class="form-control" type="password" id="loginPassword" autocomplete="current-password">
            </div>
            <button class="btn btn-primary w-100" id="loginSubmitBtn" type="button"><i class="bi bi-box-arrow-in-right me-1"></i>Sign in</button>
          </div>
        </div>
      </div>
    </div>
  `;
  const usernameInput = document.getElementById("loginUsername");
  const passwordInput = document.getElementById("loginPassword");
  const onEnter = (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submitLogin();
    }
  };
  usernameInput.addEventListener("keydown", onEnter);
  passwordInput.addEventListener("keydown", onEnter);
  document.getElementById("loginSubmitBtn").addEventListener("click", submitLogin);
  usernameInput.focus();
}

// Named bootstrapApp(), not bootstrap() -- a top-level `function bootstrap`
// declaration is hoisted onto `window.bootstrap`, silently shadowing the
// Bootstrap UI library's own global of the same name (loaded first in
// index.html). That collision broke every data-bs-dismiss="modal" button on
// any modal shown via window.bootstrap.Modal, since window.bootstrap.Modal
// resolved to undefined and callers fell back to a manual show() path that
// Bootstrap's own dismiss handling doesn't know how to hide.
async function bootstrapApp() {
  let authenticated = false;
  try {
    const session = await api("/auth/session");
    authenticated = !!session.authenticated;
    currentUsername = authenticated ? (session.username || "") : "";
  } catch (_) {
    authenticated = false;
  }
  if (!authenticated) {
    renderLoginPage();
    return;
  }
  await startApp();
}
document.getElementById("logoutBtn")?.addEventListener("click", (event) => {
  event.preventDefault();
  logout();
});
document.getElementById("accountSettingsBtn")?.addEventListener("click", (event) => {
  event.preventDefault();
  openAccountSettingsModal();
});
bootstrapApp();
