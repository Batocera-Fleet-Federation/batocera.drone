// Other Drones' Games, Movies/Shows and Music, shown in place on the normal pages (#systems,
// #movies, #music) -- there is no separate page.  This file is a *layer* on top of the existing
// explorers: it never replaces their rendering, it appends remote cards to the end of their grid
// and adds an "Available on" card to local detail pages.
//
// * Loading is asynchronous, one request per paired Drone, so a slow or offline Drone never
//   blocks the page or the others, and nothing the user is doing (typing, scrolling, a modal)
//   is interrupted: cards are appended in a display:contents wrapper in animation-frame batches.
// * Every load is reported in a persistent toast (Drones done, items found, problems).
// * Results are cached client-side (and server-side, see handlers_media_browse.py), so revisiting a
//   page or re-filtering does not ask every Batocera machine again.
// * Playback/artwork of a peer's item is proxied through this Drone; Copy uses the same sync path
//   as Swarm > Transfers (a game arrives with its artwork).  Also owns the persistent
//   transfer-progress toast: DroneTransferToast.track(jobIds).
// Relies on drone.js globals: api, apiPost, escapeHtml, formatBytes, showToast, ensureToastContainer,
// dismissToast, content, adminEnabled.
(function () {
  "use strict";

  // api()/apiPost() prepend /v1/api themselves; BASE is for <img>/<video> URLs, which are not routed through them.
  const API = "/media-browse";
  const BASE = "/v1/api/media-browse";
  const PAGE = 20; // items fetched per Drone per request; further pages load only as the user scrolls
  const RENDER_STEP = 20;
  const OFFLINE_STATES = new Set(["offline", "timeout", "cached", "error", "unsupported"]);
  const ONLINE_KEY = "droneMediaOnlineOnly";
  let onlineOnly = false;
  try { onlineOnly = window.localStorage.getItem(ONLINE_KEY) === "1"; } catch (e) { /* storage unavailable */ }
  const FRESH_MS = 60 * 1000;
  const SOURCES_MS = 5 * 60 * 1000;
  const GRIDS = { roms: "systems-explore-grid", movies: "movie-explorer-grid", music: "music-explorer-grid" };
  const NOUN = { roms: "games", movies: "movies & shows", music: "music" };
  const ICON = { roms: "bi-controller", movies: "bi-film", music: "bi-music-note-beamed" };

  const esc = (v) => escapeHtml(String(v == null ? "" : v));
  const enc = encodeURIComponent;
  const norm = (v) => String(v || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, " ").trim();
  const titleOf = (it) => it.display_title || it.title || it.track_name || it.movie_name || it.rom_name || it.name || it.entry_key || "";
  const keyOf = (it) => `${it.drone_id}|${it.entry_key || it.relative_path || it.file_path || it.rom_path || ""}|${it.system || ""}`;
  const droneLabel = (it) => (it.is_local ? "This Drone" : (it.drone_name || it.drone_id));
  const canCopy = () => typeof adminEnabled !== "undefined" && !!adminEnabled;

  // ================================================================== transfer toast
  // Persistent while a copy runs: progress bar, files done, bytes, speed, current names.
  const POLL_MS = 1000;
  const STORE_KEY = "droneTransferToastJobs";
  const TERMINAL = new Set(["completed", "failed", "cancelled", "skipped"]);
  const Toast = {
    ids: new Set(), label: "", el: null, timer: null, errors: 0,

    track(jobIds, label) {
      (jobIds || []).forEach((id) => id && this.ids.add(String(id)));
      if (label) this.label = label;
      this.persist();
      this.render({ files: this.ids.size, done: 0, failed: 0, bytes: 0, total: 0, speed: 0, names: [], state: "running" });
      this.start();
    },
    persist() {
      try { sessionStorage.setItem(STORE_KEY, JSON.stringify({ ids: [...this.ids], label: this.label })); } catch (e) { /* private mode */ }
    },
    restore() {
      try {
        const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
        if (saved && Array.isArray(saved.ids) && saved.ids.length) this.track(saved.ids, saved.label);
      } catch (e) { /* ignore */ }
    },
    start() {
      if (this.timer) return;
      const tick = async () => {
        this.timer = null;
        if (!this.ids.size) return;
        await this.poll();
        if (this.ids.size) this.timer = setTimeout(tick, POLL_MS);
      };
      this.timer = setTimeout(tick, 300);
    },
    async poll() {
      let snap;
      try {
        snap = await api("/admin/downloads?compact=1", { timeoutMs: 8000 });
        this.errors = 0;
      } catch (e) {
        this.errors += 1; // keep the toast; the Drone may just be busy
        if (this.el) this.el.querySelector(".tt-sub").textContent = "Reconnecting…";
        return;
      }
      const all = [...(snap.active || []), ...(snap.queued || []), ...(snap.recent || [])];
      const byId = new Map(all.map((j) => [String(j.id || j.job_id), j]));
      const stats = { files: this.ids.size, done: 0, failed: 0, bytes: 0, total: 0, speed: 0, names: [], state: "running" };
      for (const id of this.ids) {
        const job = byId.get(id);
        if (!job) { stats.done += 1; continue; } // aged out of the recent list: finished
        const status = String(job.status || "");
        const size = Number(job.total_bytes || job.file_size || 0);
        const got = Number(job.downloaded_bytes || 0);
        stats.total += size;
        if (TERMINAL.has(status)) {
          stats.bytes += status === "completed" ? (size || got) : got;
          if (status === "failed" || status === "cancelled") stats.failed += 1; else stats.done += 1;
        } else {
          stats.bytes += got;
          if (status === "downloading") {
            stats.speed += Number(job.transfer_speed_bps || 0);
            stats.names.push(job.file_name || job.relative_path || "file");
          }
        }
      }
      if (stats.done + stats.failed >= stats.files) stats.state = stats.failed ? "failed" : "done";
      this.render(stats);
      if (stats.state !== "running") this.finish(stats);
    },
    finish(stats) {
      this.ids.clear();
      try { sessionStorage.removeItem(STORE_KEY); } catch (e) { /* ignore */ }
      if (stats.state === "done") setTimeout(() => this.close(), 6000);
      window.dispatchEvent(new CustomEvent("drone-transfer-finished", { detail: stats }));
    },
    close() {
      if (this.el) { dismissToast(this.el); this.el = null; }
    },
    ensure() {
      if (this.el && this.el.isConnected) return this.el;
      const el = document.createElement("div");
      el.className = "toast-alert transfer-toast alert-info";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = `
        <div class="tt-head"><i class="bi bi-cloud-arrow-down-fill tt-icon"></i>
          <span class="tt-title"></span>
          <button type="button" class="tt-close d-none" aria-label="Dismiss"><i class="bi bi-x-lg"></i></button></div>
        <div class="tt-sub"></div>
        <div class="progress tt-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar"></div></div>
        <div class="tt-meta"><span class="tt-files"></span><span class="tt-speed"></span></div>
        <div class="tt-names"></div>`;
      el.querySelector(".tt-close").addEventListener("click", () => this.close());
      ensureToastContainer().appendChild(el);
      this.el = el;
      return el;
    },
    render(s) {
      const el = this.ensure();
      const finished = s.state !== "running";
      const pct = s.total > 0 ? Math.min(100, (s.bytes / s.total) * 100)
        : (s.files ? Math.min(100, ((s.done + s.failed) / s.files) * 100) : 0);
      const shown = finished ? 100 : pct;
      el.classList.toggle("alert-success", s.state === "done");
      el.classList.toggle("alert-danger", s.state === "failed");
      el.classList.toggle("alert-info", s.state === "running");
      el.querySelector(".tt-icon").className = `bi tt-icon ${s.state === "done" ? "bi-check-circle-fill" : s.state === "failed" ? "bi-exclamation-triangle-fill" : "bi-cloud-arrow-down-fill"}`;
      el.querySelector(".tt-title").textContent = s.state === "done" ? "Transfer complete"
        : s.state === "failed" ? "Transfer finished with errors" : (this.label || "Copying to this Drone");
      el.querySelector(".tt-sub").textContent = s.total > 0 ? `${formatBytes(s.bytes)} of ${formatBytes(s.total)}` : (finished ? "" : "Starting…");
      const bar = el.querySelector(".progress-bar");
      bar.style.width = `${shown.toFixed(1)}%`;
      el.querySelector(".tt-bar").setAttribute("aria-valuenow", String(Math.round(shown)));
      const completed = s.done + s.failed;
      el.querySelector(".tt-files").textContent = `${completed} of ${s.files} file${s.files === 1 ? "" : "s"}${s.failed ? ` (${s.failed} failed)` : ""}`;
      el.querySelector(".tt-speed").textContent = !finished && s.speed > 0 ? `${formatBytes(s.speed)}/s` : "";
      const names = s.names.slice(0, 2).join(", ");
      el.querySelector(".tt-names").textContent = !finished && names ? `${names}${s.names.length > 2 ? ` +${s.names.length - 2} more` : ""}` : "";
      // "Stays until it's done": only offer a dismiss control once the transfer has ended.
      el.querySelector(".tt-close").classList.toggle("d-none", !finished);
    },
  };
  window.DroneTransferToast = Toast;

  // ================================================================== sources + data
  let sourcesCache = { at: 0, promise: null };
  function peers() {
    const now = Date.now();
    if (!sourcesCache.promise || now - sourcesCache.at > SOURCES_MS) {
      sourcesCache = {
        at: now,
        promise: api(`${API}/sources`, { timeoutMs: 15000 })
          .then((r) => r.sources || [])
          .catch((err) => { sourcesCache = { at: 0, promise: null }; throw err; }),
      };
    }
    return sourcesCache.promise;
  }

  const L = {
    roms: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null },
    movies: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null },
    music: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null },
  };
  const cache = new Map(); // kind|sig -> { at, peers: Map }

  // What the page's own filters currently are.  null = the page is in a mode the remote layer
  // does not apply to (BIOS list, duplicate finder, Chrome-playable-only), so no remote cards.
  function filtersFor(kind) {
    try {
      if (kind === "roms") {
        if (systemsExploreDuplicatesMode || systemsExploreBrowserPlayOnly) return null;
        const system = systemsExploreSelectedSystem || "";
        if (system === SYSTEMS_EXPLORE_BIOS_KEY) return null;
        return { q: (systemsExploreSearchQuery || "").trim(), system, genre: systemsExploreSelectedGenre || "" };
      }
      if (kind === "movies") {
        if (movieExplorerDuplicatesMode) return null;
        const box = document.getElementById("movieExplorerSearch");
        return { q: box ? box.value.trim() : "", system: "", genre: "", type: movieExplorerTypeFilter, tag: movieExplorerGenreFilter || "" };
      }
      const box = document.getElementById("musicExplorerSearch");
      return { q: box ? box.value.trim() : "", system: "", genre: "", tag: musicExplorerGenreFilter || "", artist: musicExplorerArtistFilter || "", liked: !!musicExplorerLikedFilter };
    } catch (e) {
      return null; // explorer globals not ready yet
    }
  }
  const sigOf = (f) => JSON.stringify([f.q, f.system, f.genre]);

  function newEntry(p) {
    return { id: p.drone_id, name: p.name, items: [], total: 0, status: "loading", cursor: "", hasMore: false, busy: false, error: "", age: null };
  }

  // Loads ONE page (PAGE items) of one Drone; the next page is requested by loadMore() when the
  // user scrolls to the end.  Progress and problems show on that Drone's chip next to the
  // "On other Drones" divider -- no toast, nothing modal.
  async function loadPeer(kind, entry, f, token) {
    const ls = L[kind];
    entry.status = "loading";
    entry.hasMore = false;
    entry.busy = true;
    try {
      const params = new URLSearchParams({ limit: String(PAGE), drones: entry.id, exclude_local: "1" });
      if (f.q) params.set("q", f.q);
      if (f.system) params.set("system", f.system);
      if (f.genre) params.set("genre", f.genre);
      if (entry.cursor) params.set("cursor", entry.cursor);
      const page = await api(`${API}/${kind}?${params}`, { timeoutMs: 60000 });
      if (token !== ls.token) return;
      const src = (page.sources || [])[0] || { status: "ok" };
      entry.total = Number(src.total || 0);
      entry.age = src.cached_age_seconds == null ? null : src.cached_age_seconds;
      entry.error = src.error || "";
      entry.items.push(...(page.items || []));
      entry.cursor = page.next_cursor || "";
      entry.hasMore = !!page.has_more;
      const ok = src.status === "ok" || src.status === "cached";
      // A Drone is "loading" until its last page is in; a failure/stale status shows at once.
      entry.status = src.status;
      if (!ok || !(page.items || []).length) entry.hasMore = false;
    } catch (err) {
      if (token !== ls.token) return;
      entry.status = "error";
      entry.error = String(err && err.message || "failed");
      entry.hasMore = false;
    } finally {
      entry.busy = false;
    }
    if (token !== ls.token) return;
    if (entry.status === "loading") entry.status = "ok";
    schedulePaint(kind, true);
  }

  // Fetch the next page for every Drone that still has more and is not already fetching.
  function loadMore(kind) {
    const ls = L[kind];
    const f = filtersFor(kind);
    if (!f) return;
    for (const e of ls.peers.values()) {
      if (e.hasMore && !e.busy && e.cursor && (e.status === "ok" || e.status === "cached")) {
        cache.delete(`${kind}|${ls.sig}`);
        loadPeer(kind, e, f, ls.token);
      }
    }
  }

  async function start(kind, sig, f) {
    const ls = L[kind];
    const token = ++ls.token;
    ls.sig = sig;
    ls.limit = RENDER_STEP;
    for (const other of Object.keys(L)) if (other !== kind) { L[other].token += 1; }
    const grid = document.getElementById(GRIDS[kind]);
    let layer = grid ? grid.querySelector(":scope > .remote-media-layer") : null;
    if (layer) layer._deferred = false;
    const hit = cache.get(`${kind}|${sig}`);
    const fresh = hit && Date.now() - hit.at < FRESH_MS;
    if (hit) {
      ls.peers = new Map([...hit.peers].map(([id, e]) => [id, { ...e, items: e.items.slice() }]));
      paint(kind);
      if (fresh) return;
    }
    let list;
    try {
      list = (await peers()).filter((p) => !p.is_local);
    } catch (err) {
      if (token === ls.token) {
        const msg = err && err.message === "not found" ? "needs an update" : (err && err.message) || "error";
        ls.peers = new Map([["_list", { id: "_list", name: "Other Drones", items: [], total: 0, status: "error", error: msg }]]);
        paint(kind);
      }
      return;
    }
    if (token !== ls.token) return;
    if (!list.length) { ls.peers = new Map(); if (layer) layer.remove(); return; }
    // Keep whatever was already shown while a refresh runs (no flicker); swap in each Drone's
    // fresh result as soon as it answers.
    const staging = new Map(list.map((p) => [p.drone_id, newEntry(p)]));
    if (!hit) { ls.peers = staging; paint(kind); }
    await Promise.all([...staging.values()].map(async (entry) => {
      await loadPeer(kind, entry, f, token);
      if (hit && token === ls.token) { ls.peers.set(entry.id, entry); schedulePaint(kind); }
    }));
    if (token !== ls.token) return;
    cache.set(`${kind}|${sig}`, { at: Date.now(), peers: new Map([...ls.peers].map(([id, e]) => [id, { ...e, items: e.items.slice() }])) });
  }

  function retry(kind, id) {
    const ls = L[kind];
    const f = filtersFor(kind);
    if (!f) return;
    if (id === "_list") { sourcesCache = { at: 0, promise: null }; cache.clear(); ls.sig = ""; queueScan(); return; }
    const entry = ls.peers.get(id);
    if (!entry) return;
    entry.items = []; entry.cursor = ""; entry.error = ""; entry.busy = false;
    cache.delete(`${kind}|${ls.sig}`);
    loadPeer(kind, entry, f, ls.token);
    paint(kind);
  }

  // ================================================================== layer rendering
  // Coalesced: with thousands of items arriving, repainting every page would stutter the page.
  const paintQueued = new Set();
  function schedulePaint(kind, now) {
    if (paintQueued.has(kind)) return;
    paintQueued.add(kind);
    const run = () => { paintQueued.delete(kind); paint(kind); };
    if (now) requestAnimationFrame(run); else setTimeout(() => requestAnimationFrame(run), 250);
  }

  function artUrl(kind, it) {
    if (kind === "movies" && it.entry_key) return `${BASE}/movies/${enc(it.drone_id)}/${enc(it.entry_key)}/artwork/poster`;
    if (kind === "music" && it.entry_key) return `${BASE}/music/${enc(it.drone_id)}/${enc(it.entry_key)}/artwork/art`;
    if (kind === "roms") {
      const image = it.gamelist && it.gamelist.image;
      if (!image) return "";
      if (it.is_local) return `/v1/api/public/systems/${enc(it.system || "")}/images/${enc(String(image).split("/").pop())}`;
      const rom = it.relative_path || it.rom_path || it.file_path || "";
      return `${BASE}/roms/${enc(it.drone_id)}/artwork/${enc(it.system || "")}/image/${rom.split("/").map(enc).join("/")}`;
    }
    return "";
  }

  // Apply the page's own client-side filters (type / category / artist / liked) to remote rows.
  function passes(kind, it, f) {
    if (it.exists_locally) return false; // already shown as a normal local card
    if (onlineOnly) {
      const owner = L[kind].peers.get(it.drone_id);
      if (owner && OFFLINE_STATES.has(owner.status)) return false;
    }
    if (kind === "movies") {
      if (f.type && f.type !== "all") {
        const k = it.kind || "movie";
        if (f.type === "episode" ? !(it.show_title && (k === "episode" || k === "extra")) : k !== f.type) return false;
      }
      if (f.tag && !(it.genres || []).includes(f.tag)) return false;
    } else if (kind === "music") {
      if (f.tag && !(it.genres || []).includes(f.tag)) return false;
      if (f.artist && (it.artist || "") !== f.artist) return false;
      if (f.liked && !it.liked) return false;
    }
    return true;
  }

  // Episodes of one show / tracks of one album collapse into one card, across Drones.
  function buildCards(kind, items) {
    if (kind === "roms") return items.map((it) => ({ key: keyOf(it), sortKey: norm(titleOf(it)), group: false, title: titleOf(it), sub: it.system || "", items: [it] }));
    const cards = [];
    const groups = new Map();
    for (const it of items) {
      let key = "";
      let title = "";
      let sub = "";
      if (kind === "movies" && it.show_title && (it.kind === "episode" || it.kind === "extra")) {
        key = `s|${norm(it.scraped_show_title || it.show_title)}`; title = it.scraped_show_title || it.show_title; sub = "Show";
      } else if (kind === "music" && it.album) {
        key = `a|${norm(it.artist)}|${norm(it.album)}`; title = it.album; sub = it.artist || "";
      }
      if (!key) { cards.push({ key: keyOf(it), sortKey: norm(titleOf(it)), group: false, title: titleOf(it), sub: kind === "music" ? (it.artist || "") : (it.kind === "episode" ? "Episode" : ""), items: [it] }); continue; }
      let card = groups.get(key);
      if (!card) { card = { key, sortKey: norm(title), group: true, title, sub, items: [] }; groups.set(key, card); cards.push(card); }
      card.items.push(it);
    }
    cards.forEach((c) => { if (c.group) c.sub = `${c.sub ? c.sub + " · " : ""}${c.items.length} ${c.items.length === 1 ? "item" : "items"}`; });
    return cards;
  }

  // ---- cards: each is created once and reconciled in place, so a repaint never rebuilds (or
  // re-requests the image of) a card that is already on the page -- that rebuild was the flicker.
  const cardEls = { roms: new Map(), movies: new Map(), music: new Map() };

  // Covers stream in as cards scroll into view, a few at a time, instead of all at once.
  const ART_MAX = 6;
  const artQueue = [];
  let artActive = 0;
  const visibility = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const el = e.target;
      el._visible = e.isIntersecting;
      if (e.isIntersecting) requestArt(el);
      else cancelArt(el);
    }
  }, { rootMargin: "500px 0px" });

  function requestArt(el) {
    if (el._artState || !el._art) return;
    el._artState = "queued";
    artQueue.push(el);
    pumpArt();
  }
  function cancelArt(el) {
    if (el._artState !== "queued") return;
    const at = artQueue.indexOf(el);
    if (at >= 0) artQueue.splice(at, 1);
    el._artState = "";
  }
  function pumpArt() {
    while (artActive < ART_MAX && artQueue.length) {
      const el = artQueue.pop(); // newest-visible first: what the user is looking at now
      if (!el._visible) { el._artState = ""; continue; }
      startArt(el);
    }
  }
  function startArt(el) {
    artActive += 1;
    el._artState = "loading";
    const poster = el.querySelector(".rm-poster");
    const skel = el.querySelector(".rm-skel");
    const img = new Image();
    img.decoding = "async";
    img.alt = "";
    img.className = "rm-art";
    const settle = () => { artActive -= 1; pumpArt(); };
    img.onload = () => {
      el._artState = "done";
      poster.insertBefore(img, skel);
      requestAnimationFrame(() => { img.classList.add("loaded"); skel.classList.add("rm-done"); });
      settle();
    };
    img.onerror = () => {
      settle();
      if ((el._tries = (el._tries || 0) + 1) <= 1) {
        // One quiet retry while the card is still on screen; then a calm, final placeholder.
        el._artState = "";
        setTimeout(() => { if (el._visible) requestArt(el); }, 2500);
      } else {
        el._artState = "failed";
        skel.classList.add("rm-final");
      }
    };
    img.src = el._art;
  }

  function cardEl(kind, card) {
    const it = card.items[0];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "movie-explorer-card remote-card";
    btn.dataset.rmKey = card.key;
    btn._art = artUrl(kind, it);
    btn.innerHTML = `
      <div class="movie-explorer-card-poster rm-poster">
        <div class="rm-skel${btn._art ? "" : " rm-final"}"><i class="bi ${card.group && kind === "movies" ? "bi-collection-play" : card.group ? "bi-disc" : ICON[kind]}"></i></div>
        <span class="remote-drone-badge"></span>
      </div>
      <div class="movie-explorer-card-title"></div>
      <div class="movie-explorer-card-subtitle"></div>`;
    visibility.observe(btn);
    return btn;
  }

  function updateCard(el, card) {
    el._card = card;
    const drones = [...new Map(card.items.map((x) => [x.drone_id, x])).values()];
    const badge = `${droneLabel(drones[0])}${drones.length > 1 ? ` +${drones.length - 1}` : ""}`;
    const set = (sel, text) => { const n = el.querySelector(sel); if (n.textContent !== text) n.textContent = text; };
    set(".remote-drone-badge", badge);
    set(".movie-explorer-card-title", card.title);
    set(".movie-explorer-card-subtitle", card.sub);
    const title = `${card.title} — ${drones.map(droneLabel).join(", ")}`;
    if (el.title !== title) el.title = title;
  }

  // One chip per Drone: this is where loading, progress and problems are shown.
  function statusBadges(kind) {
    const out = [];
    const n = (v) => Number(v || 0).toLocaleString();
    for (const e of L[kind].peers.values()) {
      if (onlineOnly && OFFLINE_STATES.has(e.status)) continue;
      const name = esc(e.name);
      const retryBtn = (cls, text, title) => `<button type="button" class="badge border-0 ${cls}" data-rm-retry="${esc(e.id)}" title="${esc(title)}">${text} <i class="bi bi-arrow-clockwise"></i></button>`;
      if (e.status === "loading" || e.busy) {
        out.push(`<span class="badge text-bg-secondary rm-chip-loading" title="Loading from ${name}"><span class="spinner-border spinner-border-sm me-1" style="width:.7rem;height:.7rem"></span>${name}</span>`);
      } else if (e.status === "cached") {
        out.push(`<span class="badge text-bg-warning" title="Unreachable — showing the last copy${e.age != null ? ` (${Math.max(1, Math.round(e.age / 60))} min old)` : ""}">${name}: saved copy · ${n(e.items.length)}</span>`);
      } else if (e.status === "timeout") {
        out.push(retryBtn("text-bg-danger", `${name}: slow`, "Did not answer in time — click to try again"));
      } else if (e.status === "offline") {
        out.push(retryBtn("text-bg-danger", `${name}: offline`, e.error || "Unreachable — click to try again"));
      } else if (e.status === "error") {
        out.push(retryBtn("text-bg-danger", `${name}: ${e.error === "needs an update" || e.error === "not found" ? "needs an update" : "unavailable"}`, e.error || "Click to try again"));
      } else if (e.status === "unsupported") {
        out.push(`<span class="badge text-bg-secondary" title="This Drone's software is too old to share this">${name}: needs update</span>`);
      } else {
        out.push(`<span class="badge text-bg-info">${name}${e.items.length ? ` · ${n(e.items.length)}` : ""}</span>`);
      }
    }
    return out.join(" ");
  }

  // The divider and the end-of-list marker persist for the life of the layer; only their text changes.
  function makeLayer(grid) {
    const layer = document.createElement("div");
    layer.className = "remote-media-layer";
    layer.addEventListener("click", onLayerClick);
    layer._head = document.createElement("div");
    layer._head.className = "remote-media-divider";
    layer._head.innerHTML = `<span class="fw-semibold"><i class="bi bi-diagram-3 me-2"></i>On other Drones<span class="rm-count"></span></span><span class="remote-media-status"></span><label class="form-check form-switch m-0 small rm-online-toggle"><input class="form-check-input" type="checkbox" data-rm-online ${onlineOnly ? "checked" : ""}><span class="form-check-label ms-1">Online only</span></label>`;
    layer._tail = document.createElement("div");
    layer._tail.className = "remote-media-tail";
    layer.append(layer._head, layer._tail);
    tailWatch.observe(layer._tail);
    grid.appendChild(layer);
    return layer;
  }

  // Reaching the end of what is drawn draws more (data in memory or next page from peers): infinite scroll.
  const tailWatch = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const layer = e.target.parentElement;
      if (!layer) continue;
      layer._tailVisible = e.isIntersecting;
      if (e.isIntersecting) {
        const kind = layer._kind;
        if (layer._more) {
          revealMore(kind);
        } else {
          const f = filtersFor(kind);
          if (f && L[kind]) {
            for (const entry of L[kind].peers.values()) {
              if (entry.hasMore && !entry.loadingMore && entry.status !== "error") {
                loadPeer(kind, entry, f, L[kind].token, true);
              }
            }
          }
        }
      }
    }
  }, { rootMargin: "800px 0px" });
  function revealMore(kind) {
    const ls = L[kind];
    if (ls.drawable > ls.limit) {
      ls.limit += RENDER_STEP;
      schedulePaint(kind, true);
    } else {
      loadMore(kind); // everything fetched is already drawn: ask the Drones for the next page
    }
  }

  function paint(kind) {
    const grid = document.getElementById(GRIDS[kind]);
    if (!grid) return;
    const f = filtersFor(kind);
    let layer = grid.querySelector(":scope > .remote-media-layer");
    if (!f || !L[kind].peers.size) { if (layer) layer.remove(); return; }
    const ls = L[kind];
    const items = [];
    for (const e of ls.peers.values()) for (const it of e.items) if (passes(kind, it, f)) items.push(it);
    // Client-side text match for movies/music: remote pages were fetched with the same q, but a
    // stale cached page may predate a keystroke.
    const needle = f.q.toLowerCase();
    const cards = buildCards(kind, needle && kind !== "roms"
      ? items.filter((it) => `${titleOf(it)} ${it.artist || ""} ${it.album || ""} ${it.file_path || ""}`.toLowerCase().includes(needle))
      : items);
    cards.sort((a, b) => (a.sortKey < b.sortKey ? -1 : a.sortKey > b.sortKey ? 1 : 0));

    const isAnyLoading = [...ls.peers.values()].some((e) => e.status === "loading" || e.busy);
    const hasProblems = [...ls.peers.values()].some((e) => ["timeout", "offline", "error"].includes(e.status));
    if (!cards.length && !isAnyLoading && !hasProblems) {
      if (layer) layer.remove();
      return;
    }

    if (!layer) layer = makeLayer(grid);
    layer._kind = kind;
    layer._deferred = false;

    const count = cards.length ? ` · ${cards.length.toLocaleString()}` : "";
    const countEl = layer._head.querySelector(".rm-count");
    if (countEl.textContent !== count) countEl.textContent = count;
    const html = statusBadges(kind);
    const statusEl = layer._head.querySelector(".remote-media-status");
    if (layer._statusHtml !== html) { statusEl.innerHTML = html; layer._statusHtml = html; }

    ls.drawable = cards.length;
    const shown = cards.slice(0, ls.limit);
    const els = cardEls[kind];
    const want = new Set();
    let cursor = layer._head.nextSibling;
    for (const card of shown) {
      let el = els.get(card.key);
      if (!el) { el = cardEl(kind, card); els.set(card.key, el); }
      updateCard(el, card);
      want.add(card.key);
      if (el === cursor) cursor = cursor.nextSibling; // already in place: do not touch it
      else layer.insertBefore(el, cursor);
    }
    for (const [key, el] of els) {
      if (want.has(key)) continue;
      visibility.unobserve(el);
      cancelArt(el);
      el.remove();
      els.delete(key);
    }
    if (layer.lastChild !== layer._tail) layer.appendChild(layer._tail);
    layer._more = cards.length > shown.length || [...ls.peers.values()].some((e) => e.hasMore);
    if (layer._more && layer._tailVisible) revealMore(kind); // still at the end after drawing: keep going
  }

  function onLayerClick(event) {
    const layer = event.currentTarget;
    const toggle = event.target.closest("[data-rm-online]");
    if (toggle) {
      onlineOnly = !!toggle.checked;
      try { window.localStorage.setItem(ONLINE_KEY, onlineOnly ? "1" : "0"); } catch (e) { /* ignore */ }
      for (const k of Object.keys(L)) { L[k].limit = RENDER_STEP; schedulePaint(k, true); }
      return;
    }
    const trigger = event.target.closest("[data-rm-trigger]");
    if (trigger) {
      const kind = layer._kind;
      const f = filtersFor(kind);
      if (f) start(kind, sigOf(f), f);
      return;
    }
    const again = event.target.closest("[data-rm-retry]");
    if (again) { retry(layer._kind, again.dataset.rmRetry); return; }
    const el = event.target.closest(".remote-card");
    if (el && el._card) openDetails(layer._kind, el._card);
  }

  // ================================================================== activation (no edits to the big renderers)
  function isDeferred(kind, f) {
    // ROMs with no query, no system, and no genre is "All Systems" overview:
    // With tens of thousands of ROMs, don't query peers until the remote section
    // is scrolled into view or the user narrows the scope with a filter/search.
    return kind === "roms" && !f.q && !f.system && !f.genre;
  }

  const dividerWatch = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const layer = e.target.closest(".remote-media-layer");
      if (!layer || !layer._deferred) continue;
      dividerWatch.unobserve(e.target);
      const kind = layer._kind;
      const f = filtersFor(kind);
      if (f) start(kind, sigOf(f), f);
    }
  }, { rootMargin: "600px 0px" });

  async function ensureDeferredDivider(grid, kind, sig, f) {
    let list = [];
    try {
      list = (await peers()).filter((p) => !p.is_local);
    } catch (e) { list = []; }
    if (!list.length) {
      const existing = grid.querySelector(":scope > .remote-media-layer");
      if (existing) existing.remove();
      return;
    }
    const currentF = filtersFor(kind);
    if (!currentF || sigOf(currentF) !== sig) return;
    let layer = grid.querySelector(":scope > .remote-media-layer");
    if (layer && !layer._deferred) layer.remove();
    if (!layer || !layer.isConnected) {
      layer = document.createElement("div");
      layer.className = "remote-media-layer";
      layer.addEventListener("click", onLayerClick);
      layer._head = document.createElement("div");
      layer._head.className = "remote-media-divider";
      layer._head.innerHTML = `<span class="fw-semibold"><i class="bi bi-diagram-3 me-2"></i>On other Drones</span><span class="remote-media-status"><button type="button" class="btn btn-outline-light btn-sm py-0 px-2" data-rm-trigger="1"><i class="bi bi-cloud-arrow-down me-1"></i>Check other Drones</button></span>`;
      layer.appendChild(layer._head);
      grid.appendChild(layer);
    }
    layer._kind = kind;
    layer._deferred = true;
    dividerWatch.observe(layer._head);
  }

  let activeKind = "";
  let scanQueued = false;
  function scan() {
    scanQueued = false;
    let found = "";
    for (const kind of Object.keys(GRIDS)) {
      const grid = document.getElementById(GRIDS[kind]);
      if (!grid) continue;
      found = kind;
      const f = filtersFor(kind);
      const layer = grid.querySelector(":scope > .remote-media-layer");
      if (!f) { if (layer) layer.remove(); continue; }
      const sig = sigOf(f);
      const ls = L[kind];
      if (sig !== ls.sig) {
        clearTimeout(ls.pending);
        if (isDeferred(kind, f)) {
          ls.sig = sig;
          ls.token += 1;
          ls.peers = new Map();
          if (layer) layer.remove();
          ensureDeferredDivider(grid, kind, sig, f);
        } else {
          // Filter changes wait for the user to stop typing
          ls.pending = setTimeout(() => start(kind, sig, f), ls.sig === "" ? 150 : 350);
          if (layer) paint(kind);
        }
      } else if (!layer) {
        if (isDeferred(kind, f) && !ls.peers.size) {
          ensureDeferredDivider(grid, kind, sig, f);
        } else {
          paint(kind); // local grid re-rendered: put the remote cards back (from memory, no network)
        }
      }
    }
    if (found !== activeKind) {
      if (!found) {
        for (const kind of Object.keys(L)) { L[kind].token += 1; L[kind].sig = ""; clearTimeout(L[kind].pending); }
      }
      activeKind = found;
    }
  }
  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    requestAnimationFrame(scan);
  }
  function observe() {
    const host = document.getElementById("content");
    if (!host) return;
    new MutationObserver((records) => {
      // Our own layer updates (and image loads inside it) must not retrigger a scan.
      if (records.every((r) => r.target.closest && r.target.closest(".remote-media-layer"))) return;
      queueScan();
    }).observe(host, { childList: true, subtree: true });
    queueScan();
  }

  // ================================================================== details
  let modalEl = null;
  function ensureModal() {
    if (modalEl) return modalEl;
    modalEl = document.createElement("div");
    modalEl.className = "modal fade";
    modalEl.tabIndex = -1;
    modalEl.innerHTML = `<div class="modal-dialog modal-lg modal-dialog-scrollable"><div class="modal-content themed-modal">
      <div class="modal-header"><h5 class="modal-title"></h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
      <div class="modal-body"></div></div></div>`;
    document.body.appendChild(modalEl);
    modalEl.addEventListener("hidden.bs.modal", () => {
      modalEl.querySelectorAll("video,audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
    });
    modalEl.addEventListener("click", (event) => onAction(event, modalEl));
    return modalEl;
  }

  const badge = (it) => `<span class="badge ${it.is_local ? "text-bg-secondary" : "text-bg-info"} nm-drone-badge" title="${esc(droneLabel(it))}"><i class="bi bi-hdd-network me-1"></i>${esc(droneLabel(it))}</span>`;

  function copyRow(kind, it) {
    const play = kind !== "roms" && it.entry_key
      ? `<button type="button" class="btn btn-sm btn-success" data-act="play" data-key="${esc(keyOf(it))}"><i class="bi bi-play-fill me-1"></i>Play${it.is_local ? "" : " from " + esc(droneLabel(it))}</button>` : "";
    const copy = !it.is_local && !it.exists_locally && canCopy()
      ? `<button type="button" class="btn btn-sm btn-primary" data-act="copy" data-key="${esc(keyOf(it))}"><i class="bi bi-download me-1"></i>Copy to this Drone</button>` : "";
    const like = kind === "music" && it.entry_key
      ? `<button type="button" class="btn btn-sm btn-outline-light" data-act="like" data-key="${esc(keyOf(it))}" title="Like (saved on this Drone only)"><i class="bi ${it.liked ? "bi-heart-fill text-danger" : "bi-heart"}"></i></button>` : "";
    return `<div class="nm-copy d-flex flex-wrap align-items-center gap-2 py-2 border-top border-secondary-subtle">
      ${badge(it)}<span class="small text-muted flex-grow-1">${it.file_size ? esc(formatBytes(it.file_size)) : ""}${it.exists_locally ? " · already on this Drone" : ""}</span>${play}${copy}${like}</div>`;
  }

  // Which Drones hold this title?  Fills `host` asynchronously; never blocks the page.
  async function fillAvailability(kind, it, host, registry, includeSelf) {
    host.innerHTML = `<div class="small text-muted"><span class="spinner-border spinner-border-sm me-2"></span>Checking which Drones have this…</div>`;
    const params = new URLSearchParams({ title: titleOf(it) });
    ["fingerprint", "artist", "system"].forEach((k) => it[k] && params.set(k, it[k]));
    if (it.drone_id) params.set("exclude_drone", it.drone_id);
    try {
      const res = await api(`${API}/${kind}/also-on?${params}`, { timeoutMs: 45000 });
      const copies = res.copies || [];
      if (includeSelf && copies.some((c) => c.is_local)) it.exists_locally = true;
      copies.forEach((c) => registry.set(keyOf(c), c));
      const rows = (includeSelf ? [it] : []).concat(copies);
      rows.forEach((r) => registry.set(keyOf(r), r));
      const down = (res.sources || []).filter((s) => !["ok", "cached"].includes(s.status));
      host.innerHTML = `${rows.map((r) => copyRow(kind, r)).join("") || '<div class="small text-muted">Only on this Drone.</div>'}
        ${down.length ? `<div class="small text-muted mt-2">Could not check: ${esc(down.map((s) => s.name).join(", "))}</div>` : ""}`;
    } catch (err) {
      host.innerHTML = `<div class="small text-muted">Could not check other Drones${err && err.message ? ` (${esc(err.message)})` : ""}.</div>`;
    }
  }

  // Movies and music open the same full detail pages as local items (key "r~<drone>~<key>", see
  // remoteEntryKey in drone.js); a show/album card opens its first episode/track.  Games keep the
  // details dialog.
  function openDetails(kind, card) {
    if (kind !== "roms") {
      const first = card.items.slice().sort((a, b) => (a.season_number || 0) - (b.season_number || 0)
        || (a.episode_number || a.track_number || 0) - (b.episode_number || b.track_number || 0))[0];
      const key = remoteEntryKey(first.drone_id, first.entry_key);
      setHash(kind === "movies" ? movieDetailHash(key) : musicDetailHash(key));
      return;
    }
    const modal = ensureModal();
    const it = card.items[0];
    modal.querySelector(".modal-title").textContent = card.title;
    const body = modal.querySelector(".modal-body");
    const registry = new Map();
    modal._kind = kind;
    modal._registry = registry;
    modal._group = card.group ? card : null;
    if (card.group) {
      const eps = card.items.slice().sort((a, b) => (a.season_number || 0) - (b.season_number || 0) || (a.episode_number || a.track_number || 0) - (b.episode_number || b.track_number || 0));
      eps.forEach((e) => registry.set(keyOf(e), e));
      const drones = [...new Map(card.items.map((x) => [x.drone_id, x])).values()];
      body.innerHTML = `<div class="d-flex flex-wrap gap-2 mb-2">${drones.map(badge).join("")}
        ${canCopy() ? `<button class="btn btn-sm btn-primary ms-auto" data-act="copy-all"><i class="bi bi-download me-1"></i>Copy all to this Drone</button>` : ""}</div>
        <div class="nm-player"></div>
        ${eps.map((e) => `<div class="small fw-semibold mt-2">${e.season_number != null ? `S${String(e.season_number).padStart(2, "0")}` : ""}${e.episode_number != null ? `E${String(e.episode_number).padStart(2, "0")} ` : ""}${esc(e.episode_title || titleOf(e))}</div>${copyRow(kind, e)}`).join("")}`;
    } else {
      const meta = it.metadata || {};
      registry.set(keyOf(it), it);
      body.innerHTML = `<div class="nm-player"></div>
        <div class="mb-2 small text-muted">${esc([it.artist, it.album, it.system, meta.release_date || meta.year].filter(Boolean).join(" · "))}</div>
        ${meta.overview ? `<p class="small">${esc(meta.overview)}</p>` : ""}
        <h6 class="mt-3">Available on</h6><div class="nm-avail"></div>`;
      fillAvailability(kind, it, body.querySelector(".nm-avail"), registry, true);
    }
    bootstrap.Modal.getOrCreateInstance(modal).show();
  }

  function play(kind, it, scope) {
    const host = scope.querySelector(".nm-player");
    if (!host) return;
    const tag = kind === "music" ? "audio" : "video";
    const url = `${BASE}/${kind}/${enc(it.drone_id)}/${enc(it.entry_key)}/stream`;
    host.innerHTML = `<${tag} controls autoplay class="w-100 mb-2" src="${esc(url)}"></${tag}><div class="small text-muted mb-2">Playing from ${esc(droneLabel(it))}</div>`;
    host.firstElementChild.addEventListener("error", () => showToast("That Drone could not stream this file right now.", "warning", 6000));
    host.scrollIntoView({ block: "nearest" });
  }

  async function copyItems(kind, items) {
    const remote = items.filter((x) => !x.is_local && !x.exists_locally);
    if (!remote.length) { showToast("Everything selected is already on this Drone.", "info"); return; }
    const jobIds = [];
    try {
      for (const it of remote) {
        const res = await apiPost("/admin/local-network/sync", {
          peer_id: it.drone_id, asset_type: kind, system: it.system || "", item: it, include_artwork: true,
        });
        (res.jobs || []).forEach((j) => jobIds.push(j.id || j.job_id));
      }
    } catch (err) {
      showToast(`Could not start the transfer: ${esc(err.message || "unknown error")}`, "danger", 8000);
    }
    if (jobIds.length) Toast.track(jobIds, remote.length === 1 ? `Copying ${titleOf(remote[0])}` : `Copying ${remote.length} items`);
    else if (remote.length) showToast("Nothing new to copy — it may already be here or queued.", "info");
  }

  async function toggleLike(scope, it) {
    const liked = !it.liked;
    try {
      await apiPost(`${API}/music/like`, { drone_id: it.drone_id, entry_key: it.entry_key, liked });
      it.liked = liked;
      const icon = scope.querySelector(`[data-act="like"][data-key="${CSS.escape(keyOf(it))}"] i`);
      if (icon) icon.className = `bi ${liked ? "bi-heart-fill text-danger" : "bi-heart"}`;
    } catch (err) {
      showToast(`Could not save the like: ${esc(err.message || "unknown error")}`, "danger");
    }
  }

  // Shared click handling for the modal and the on-page "Available on" card.
  function onAction(event, scope) {
    const btn = event.target.closest("[data-act]");
    if (!btn || !scope.contains(btn)) return;
    const kind = scope._kind;
    const act = btn.dataset.act;
    if (act === "copy-all" && scope._group) { copyItems(kind, scope._group.items); return; }
    const it = scope._registry && scope._registry.get(btn.dataset.key);
    if (!it) return;
    if (act === "play") play(kind, it, scope);
    else if (act === "copy") copyItems(kind, [it]);
    else if (act === "like") toggleLike(scope, it);
  }

  // ================================================================== local detail pages
  // A local game/movie/track page gets an "Available on" card showing every Drone that has it.
  async function onRouteRendered(hash) {
    document.querySelectorAll(".drone-availability").forEach((n) => n.remove());
    let kind = "";
    let item = null;
    let remoteItem = false;
    try {
      if (hash.startsWith("#system/")) {
        const rom = (window.missingArtworkRoms || [])[0];
        if (!rom) return;
        kind = "roms";
        item = { title: rom.title || rom.name || rom.rom_name, system: rom.system, fingerprint: rom.fingerprint || rom.rom_fingerprint || "" };
      } else if (hash.startsWith("#movies/") || hash.startsWith("#music/")) {
        const parsed = hash.startsWith("#movies/") ? parseMoviesHash(hash) : parseMusicHash(hash);
        if (!parsed || parsed.view !== "detail" || !parsed.entryKey) return;
        kind = hash.startsWith("#movies/") ? "movies" : "music";
        const remote = remoteKeyParts(parsed.entryKey);
        const detail = await fetchMediaDetail(kind, parsed.entryKey);
        item = { title: detail.display_title || detail.movie_name || detail.track_name, fingerprint: detail.fingerprint || "", artist: detail.artist || "" };
        if (remote) {
          // A page for another Drone's item: that Drone is the first row, then every other holder.
          Object.assign(item, detail, { title: item.title, drone_id: remote.droneId, entry_key: remote.key, is_local: false });
          remoteItem = true;
        }
      } else {
        return;
      }
    } catch (e) { return; }
    if (!item || !item.title || window.location.hash !== hash) return;
    const first = content.firstElementChild;
    if (!first) return;
    const card = document.createElement("div");
    card.className = "card log-card mb-3 drone-availability";
    card.innerHTML = `<div class="card-header"><i class="bi bi-diagram-3 me-2"></i>Available on</div><div class="card-body"><div class="nm-avail"></div></div>`;
    first.insertAdjacentElement("afterend", card);
    card._kind = kind;
    card._registry = new Map();
    card.addEventListener("click", (event) => onAction(event, card));
    if (remoteItem) { await fillAvailability(kind, item, card.querySelector(".nm-avail"), card._registry, true); return; }
    let self = null;
    try { self = (await peers()).find((p) => p.is_local); } catch (e) { /* offline listing is fine */ }
    const here = { ...item, drone_id: self ? self.drone_id : "", drone_name: "This Drone", is_local: true };
    await fillAvailability(kind, here, card.querySelector(".nm-avail"), card._registry, false);
    const host = card.querySelector(".nm-avail");
    if (host && card.isConnected) host.insertAdjacentHTML("afterbegin", `<div class="nm-copy d-flex align-items-center gap-2 py-2">${badge(here)}<span class="small text-muted">This page</span></div>`);
  }
  window.DroneRemoteMedia = { onRouteRendered, refresh() { cache.clear(); for (const k of Object.keys(L)) L[k].sig = ""; queueScan(); } };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", observe);
  else observe();
  Toast.restore();
})();
