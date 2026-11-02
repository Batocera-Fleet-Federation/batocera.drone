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
  const PAGE = 200;
  const CAPS = { roms: 400, movies: 1000, music: 600 }; // auto-loaded per Drone; more on demand
  const RENDER_STEP = 300;
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

  // ================================================================== loading toast
  // One toast per page load of other Drones' assets.  Created only if the load takes longer than
  // a blink (cache hits never flash a toast) and stays until every Drone has answered or failed.
  const LoadToast = {
    el: null, delay: null, active: false, state: null,

    begin(kind, total) {
      this.cancel();
      this.active = true;
      this.state = { kind, total, done: 0, found: 0, issues: [] };
      this.delay = setTimeout(() => { this.delay = null; if (this.active) this.render(); }, 350);
    },
    update(patch) {
      if (!this.active) return;
      Object.assign(this.state, patch);
      if (this.el) this.render();
    },
    issue(text) {
      if (!this.active) return;
      this.state.issues.push(text);
      if (this.el) this.render();
    },
    end() {
      if (!this.active) return;
      const s = this.state;
      this.active = false;
      if (this.delay) { clearTimeout(this.delay); this.delay = null; if (!s.issues.length) return; this.render(); }
      if (!this.el) return;
      this.render(true);
      const el = this.el;
      setTimeout(() => { if (this.el === el) this.cancel(); }, s.issues.length ? 7000 : 1800);
    },
    cancel() {
      this.active = false;
      if (this.delay) { clearTimeout(this.delay); this.delay = null; }
      if (this.el) { dismissToast(this.el); this.el = null; }
    },
    render(finished = false) {
      const s = this.state;
      if (!this.el || !this.el.isConnected) {
        const el = document.createElement("div");
        el.className = "toast-alert transfer-toast alert-info load-toast";
        el.setAttribute("role", "status");
        el.setAttribute("aria-live", "polite");
        el.innerHTML = `<div class="tt-head"><span class="spinner-border spinner-border-sm tt-spin" aria-hidden="true"></span>
          <span class="tt-title"></span></div>
          <div class="progress tt-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><div class="progress-bar"></div></div>
          <div class="tt-meta"><span class="tt-files"></span><span class="tt-speed"></span></div>
          <div class="tt-names"></div>`;
        ensureToastContainer().appendChild(el);
        this.el = el;
      }
      const el = this.el;
      const pct = s.total ? Math.min(100, (s.done / s.total) * 100) : 0;
      el.classList.toggle("alert-warning", finished && s.issues.length > 0);
      el.classList.toggle("alert-success", finished && s.issues.length === 0);
      el.classList.toggle("alert-info", !finished);
      el.querySelector(".tt-spin").classList.toggle("d-none", finished);
      el.querySelector(".tt-title").textContent = finished
        ? (s.issues.length ? `Loaded ${NOUN[s.kind]} from ${s.total - s.issues.length} of ${s.total} Drones` : `Loaded ${NOUN[s.kind]} from other Drones`)
        : `Loading ${NOUN[s.kind]} from other Drones…`;
      el.querySelector(".progress-bar").style.width = `${finished ? 100 : pct}%`;
      el.querySelector(".tt-bar").setAttribute("aria-valuenow", String(Math.round(finished ? 100 : pct)));
      el.querySelector(".tt-files").textContent = `${s.done} of ${s.total} Drone${s.total === 1 ? "" : "s"}`;
      el.querySelector(".tt-speed").textContent = `${s.found.toLocaleString()} found`;
      el.querySelector(".tt-names").textContent = s.issues.join(" · ");
    },
  };

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
    roms: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null, cap: CAPS.roms },
    movies: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null, cap: CAPS.movies },
    music: { sig: "", token: 0, peers: new Map(), limit: RENDER_STEP, pending: null, cap: CAPS.music },
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
    return { id: p.drone_id, name: p.name, items: [], total: 0, status: "loading", cursor: "", hasMore: false, error: "", age: null };
  }

  async function loadPeer(kind, entry, f, token, cap) {
    const ls = L[kind];
    entry.status = "loading";
    let errored = false;
    let first = !entry.cursor && entry.items.length === 0;
    try {
      while (token === ls.token && entry.items.length < cap && (first || entry.hasMore)) {
        first = false;
        const params = new URLSearchParams({ limit: String(PAGE), drones: entry.id, exclude_local: "1" });
        if (f.q) params.set("q", f.q);
        if (f.system) params.set("system", f.system);
        if (f.genre) params.set("genre", f.genre);
        if (entry.cursor) params.set("cursor", entry.cursor);
        const page = await api(`${API}/${kind}?${params}`, { timeoutMs: 45000 });
        if (token !== ls.token) return;
        const src = (page.sources || [])[0] || { status: "ok" };
        entry.status = src.status;
        entry.total = Number(src.total || 0);
        entry.age = src.cached_age_seconds == null ? null : src.cached_age_seconds;
        entry.error = src.error || "";
        entry.items.push(...(page.items || []));
        entry.cursor = page.next_cursor || "";
        entry.hasMore = !!page.has_more;
        LoadToast.update({ found: totalFound(kind) });
        schedulePaint(kind);
        if ((src.status !== "ok" && src.status !== "cached") || !(page.items || []).length) break;
      }
    } catch (err) {
      errored = true;
      if (token !== ls.token) return;
      entry.status = "error";
      entry.error = String(err && err.message || "failed");
    }
    if (token !== ls.token) return;
    if (entry.status === "offline" || entry.status === "timeout" || entry.status === "error") {
      LoadToast.issue(`${entry.name}: ${entry.status === "timeout" ? "slow" : entry.status === "offline" ? "offline" : (entry.error === "not found" ? "this Drone needs an update" : "error")}`);
    } else if (entry.status === "unsupported") {
      LoadToast.issue(`${entry.name}: needs an update`);
    } else if (entry.status === "cached") {
      LoadToast.issue(`${entry.name}: showing saved copy`);
    }
    if (errored) entry.hasMore = false;
    LoadToast.update({ done: (LoadToast.state ? LoadToast.state.done : 0) + 1 });
    schedulePaint(kind);
  }

  const totalFound = (kind) => [...L[kind].peers.values()].reduce((n, e) => n + e.items.length, 0);

  async function start(kind, sig, f) {
    const ls = L[kind];
    const token = ++ls.token;
    ls.sig = sig;
    ls.limit = RENDER_STEP;
    ls.cap = CAPS[kind];
    for (const other of Object.keys(L)) if (other !== kind) { L[other].token += 1; }
    const hit = cache.get(`${kind}|${sig}`);
    const fresh = hit && Date.now() - hit.at < FRESH_MS;
    if (hit) {
      ls.peers = new Map([...hit.peers].map(([id, e]) => [id, { ...e, items: e.items.slice() }]));
      paint(kind);
      if (fresh) { LoadToast.cancel(); return; }
    }
    let list;
    try {
      list = (await peers()).filter((p) => !p.is_local);
    } catch (err) {
      if (token === ls.token) {
        LoadToast.begin(kind, 1);
        LoadToast.issue(err && err.message === "not found" ? "This Drone needs an update to browse other Drones" : `Could not list Drones: ${err.message || "error"}`);
        LoadToast.end();
      }
      return;
    }
    if (token !== ls.token) return;
    if (!list.length) { ls.peers = new Map(); paint(kind); LoadToast.cancel(); return; }
    // Keep whatever was already shown while a refresh runs (no flicker); swap in each Drone's
    // fresh result as soon as it answers.
    const staging = new Map(list.map((p) => [p.drone_id, newEntry(p)]));
    LoadToast.begin(kind, list.length);
    if (!hit) ls.peers = staging;
    await Promise.all([...staging.values()].map(async (entry) => {
      await loadPeer(kind, entry, f, token, ls.cap);
      if (hit && token === ls.token) { ls.peers.set(entry.id, entry); schedulePaint(kind); }
    }));
    if (token !== ls.token) return;
    cache.set(`${kind}|${sig}`, { at: Date.now(), peers: new Map([...ls.peers].map(([id, e]) => [id, { ...e, items: e.items.slice() }])) });
    LoadToast.end();
  }

  async function loadMore(kind) {
    const ls = L[kind];
    const f = filtersFor(kind);
    if (!f) return;
    ls.limit += RENDER_STEP;
    const loaded = totalFound(kind);
    const pending = [...ls.peers.values()].filter((e) => e.hasMore);
    if (loaded < ls.limit && pending.length) {
      const token = ++ls.token; // supersede nothing else; same filters
      ls.cap += CAPS[kind];
      LoadToast.begin(kind, pending.length);
      await Promise.all(pending.map((e) => loadPeer(kind, e, f, token, ls.cap)));
      if (token === ls.token) LoadToast.end();
    }
    paint(kind);
  }

  // ================================================================== layer rendering
  const paintQueued = new Set();
  function schedulePaint(kind) {
    if (paintQueued.has(kind)) return;
    paintQueued.add(kind);
    requestAnimationFrame(() => { paintQueued.delete(kind); paint(kind); });
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
    if (kind === "roms") return items.map((it) => ({ group: false, title: titleOf(it), sub: it.system || "", items: [it] }));
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
      if (!key) { cards.push({ group: false, title: titleOf(it), sub: kind === "music" ? (it.artist || "") : (it.kind === "episode" ? "Episode" : ""), items: [it] }); continue; }
      let card = groups.get(key);
      if (!card) { card = { group: true, title, sub, items: [] }; groups.set(key, card); cards.push(card); }
      card.items.push(it);
    }
    cards.forEach((c) => { if (c.group) c.sub = `${c.sub ? c.sub + " · " : ""}${c.items.length} ${c.items.length === 1 ? "item" : "items"}`; });
    return cards;
  }

  function cardEl(kind, card, index) {
    const it = card.items[0];
    const drones = [...new Map(card.items.map((x) => [x.drone_id, x])).values()];
    const art = artUrl(kind, it);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "movie-explorer-card remote-card";
    btn.dataset.rmIndex = String(index);
    btn.title = `${card.title} — ${drones.map(droneLabel).join(", ")}`;
    btn.innerHTML = `
      <div class="movie-explorer-card-poster">
        ${art ? `<img loading="lazy" decoding="async" src="${esc(art)}" alt="">` : ""}
        <div class="movie-explorer-card-poster-fallback ${art ? "d-none" : ""}"><i class="bi ${card.group && kind === "movies" ? "bi-collection-play" : card.group ? "bi-disc" : ICON[kind]}"></i></div>
        <span class="remote-drone-badge"><i class="bi bi-hdd-network me-1"></i>${esc(droneLabel(drones[0]))}${drones.length > 1 ? ` +${drones.length - 1}` : ""}</span>
      </div>
      <div class="movie-explorer-card-title">${esc(card.title)}</div>
      <div class="movie-explorer-card-subtitle">${esc(card.sub)}</div>`;
    return btn;
  }

  function statusBadges(kind) {
    const out = [];
    for (const e of L[kind].peers.values()) {
      if (e.status === "loading") out.push(`<span class="badge text-bg-secondary"><span class="spinner-border spinner-border-sm me-1" style="width:.7rem;height:.7rem"></span>${esc(e.name)}</span>`);
      else if (e.status === "cached") out.push(`<span class="badge text-bg-warning" title="Unreachable — showing the last copy${e.age != null ? ` (${Math.max(1, Math.round(e.age / 60))} min old)` : ""}">${esc(e.name)}: saved copy</span>`);
      else if (e.status === "offline" || e.status === "timeout" || e.status === "error") out.push(`<span class="badge text-bg-danger" title="${esc(e.error || e.status)}">${esc(e.name)}: ${e.status === "timeout" ? "slow" : e.status === "offline" ? "offline" : "unavailable"}</span>`);
      else if (e.status === "unsupported") out.push(`<span class="badge text-bg-secondary">${esc(e.name)}: needs update</span>`);
      else out.push(`<span class="badge text-bg-info">${esc(e.name)} · ${e.total.toLocaleString()}</span>`);
    }
    return out.join(" ");
  }

  function paint(kind) {
    const grid = document.getElementById(GRIDS[kind]);
    if (!grid) return;
    const f = filtersFor(kind);
    let layer = grid.querySelector(":scope > .remote-media-layer");
    if (!f || (!L[kind].peers.size && !LoadToast.active)) { if (layer) layer.remove(); return; }
    const ls = L[kind];
    const items = [];
    for (const e of ls.peers.values()) for (const it of e.items) if (passes(kind, it, f)) items.push(it);
    // Client-side text match for movies/music: remote pages were fetched with the same q, but a
    // stale cached page may predate a keystroke.
    const needle = f.q.toLowerCase();
    const cards = buildCards(kind, needle && kind !== "roms"
      ? items.filter((it) => `${titleOf(it)} ${it.artist || ""} ${it.album || ""} ${it.file_path || ""}`.toLowerCase().includes(needle))
      : items);
    cards.sort((a, b) => norm(a.title).localeCompare(norm(b.title)));
    if (!layer) {
      layer = document.createElement("div");
      layer.className = "remote-media-layer";
      layer.addEventListener("click", onLayerClick);
      layer.addEventListener("error", (event) => {
        const img = event.target;
        if (img && img.tagName === "IMG") {
          const fb = img.parentElement && img.parentElement.querySelector(".movie-explorer-card-poster-fallback");
          img.remove();
          if (fb) fb.classList.remove("d-none");
        }
      }, true);
      grid.appendChild(layer);
    }
    layer._kind = kind;
    layer._cards = cards;
    const shown = cards.slice(0, ls.limit);
    const frag = document.createDocumentFragment();
    const head = document.createElement("div");
    head.className = "remote-media-divider";
    head.innerHTML = `<span class="fw-semibold"><i class="bi bi-diagram-3 me-2"></i>On other Drones${cards.length ? ` · ${cards.length.toLocaleString()}` : ""}</span><span class="remote-media-status">${statusBadges(kind)}</span>`;
    frag.appendChild(head);
    shown.forEach((card, i) => frag.appendChild(cardEl(kind, card, i)));
    const more = cards.length > shown.length || [...ls.peers.values()].some((e) => e.hasMore);
    if (more) {
      const wrap = document.createElement("div");
      wrap.className = "remote-media-more";
      wrap.innerHTML = `<button type="button" class="btn btn-outline-primary btn-sm" data-rm-more="1"><i class="bi bi-plus-circle me-1"></i>Show more from other Drones</button>`;
      frag.appendChild(wrap);
    }
    layer.replaceChildren(frag);
  }

  function onLayerClick(event) {
    const layer = event.currentTarget;
    if (event.target.closest("[data-rm-more]")) { loadMore(layer._kind); return; }
    const card = event.target.closest(".remote-card");
    if (!card) return;
    const entry = layer._cards && layer._cards[Number(card.dataset.rmIndex)];
    if (entry) openDetails(layer._kind, entry);
  }

  // ================================================================== activation (no edits to the big renderers)
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
        // First load starts immediately; filter changes wait for the user to stop typing.
        ls.pending = setTimeout(() => start(kind, sig, f), ls.sig === "" ? 0 : 400);
        if (layer) paint(kind);
      } else if (!layer) {
        paint(kind); // local grid re-rendered: put the remote cards back (from memory, no network)
      }
    }
    if (found !== activeKind) {
      if (!found) {
        for (const kind of Object.keys(L)) { L[kind].token += 1; L[kind].sig = ""; clearTimeout(L[kind].pending); }
        LoadToast.cancel();
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

  function openDetails(kind, card) {
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
        const detail = await api(`/${kind}/${enc(parsed.entryKey)}`, { timeoutMs: 15000 });
        item = { title: detail.display_title || detail.movie_name || detail.track_name, fingerprint: detail.fingerprint || "", artist: detail.artist || "" };
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
