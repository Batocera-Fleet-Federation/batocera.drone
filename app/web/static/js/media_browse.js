// Browse every Drone's Movies/Shows, Music and Games from this Drone (#network),
// plus the persistent transfer-progress toast used when copying an item here.
//
// Server contract (see handlers_media_browse.py): /v1/api/media-browse/... returns one merged,
// cursor-paged list across this Drone and every paired peer; playback and artwork of a peer's item
// are proxied through this Drone; copying reuses POST /admin/local-network/sync (the same path as
// Swarm > Transfers, so a game arrives with its artwork in the right system folder).
// Relies on drone.js globals: api, apiPost, escapeHtml, jsAttr, formatBytes, showToast,
// ensureToastContainer, dismissToast, setHash, content, adminEnabled.
(function () {
  "use strict";

  const BASE = "/v1/api/media-browse";
  const KINDS = [
    ["movies", "Movies & Shows", "bi-film"],
    ["music", "Music", "bi-music-note-beamed"],
    ["roms", "Games", "bi-grid"],
  ];
  const PAGE = 200;
  const AUTO_LOAD_LIMIT = 1000; // movies/music load this many up front; the rest on demand
  const CLIENT_CACHE_MS = 60 * 1000;

  const state = {
    kind: "movies", q: "", hidden: new Set(), items: [], sources: [], cursor: "", hasMore: false,
    loading: false, token: 0, byKey: new Map(),
  };
  // Re-opening the page (or switching tabs back and forth) must not re-ask every Drone.
  const clientCache = new Map();

  const esc = (v) => escapeHtml(String(v == null ? "" : v));
  const enc = encodeURIComponent;

  // ------------------------------------------------------------------ transfer toast
  const POLL_MS = 1000;
  const STORE_KEY = "droneTransferToastJobs";
  const TERMINAL = new Set(["completed", "failed", "cancelled", "skipped"]);
  const Toast = {
    ids: new Set(), label: "", el: null, timer: null, failedSeen: false, errors: 0,

    track(jobIds, label) {
      (jobIds || []).forEach((id) => id && this.ids.add(String(id)));
      if (label) this.label = label;
      this.persist();
      this.render({ files: 0, done: 0, failed: 0, bytes: 0, total: 0, speed: 0, names: [], state: "running" });
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
      bar.classList.toggle("bg-success", s.state === "done");
      bar.classList.toggle("bg-danger", s.state === "failed");
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

  // ------------------------------------------------------------------ helpers
  const norm = (v) => String(v || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, " ").trim();
  const titleOf = (it) => it.display_title || it.title || it.track_name || it.movie_name || it.rom_name || it.name || it.entry_key || "";
  const keyOf = (it) => `${it.drone_id}|${it.entry_key || it.relative_path || it.file_path || it.rom_path || ""}|${it.system || ""}`;
  const droneLabel = (it) => (it.is_local ? "This Drone" : (it.drone_name || it.drone_id));

  function artUrl(it) {
    if (state.kind === "movies" && it.entry_key) return `${BASE}/movies/${enc(it.drone_id)}/${enc(it.entry_key)}/artwork/poster`;
    if (state.kind === "music" && it.entry_key) return `${BASE}/music/${enc(it.drone_id)}/${enc(it.entry_key)}/artwork/art`;
    if (state.kind === "roms") {
      const image = it.gamelist && it.gamelist.image;
      if (!image) return "";
      if (it.is_local) return `/v1/api/public/systems/${enc(it.system || "")}/images/${enc(String(image).split("/").pop())}`;
      const rom = it.relative_path || it.rom_path || it.file_path || "";
      return `${BASE}/roms/${enc(it.drone_id)}/artwork/${enc(it.system || "")}/image/${rom.split("/").map(enc).join("/")}`;
    }
    return "";
  }
  const streamUrl = (it) => `${BASE}/${state.kind}/${enc(it.drone_id)}/${enc(it.entry_key)}/stream`;

  function badge(it) {
    return `<span class="badge ${it.is_local ? "text-bg-secondary" : "text-bg-info"} nm-drone-badge" title="${esc(droneLabel(it))}"><i class="bi bi-hdd-network me-1"></i>${esc(droneLabel(it))}</span>`;
  }

  // ------------------------------------------------------------------ data
  function cacheKey() { return `${state.kind}|${state.q}`; }

  async function load({ refresh = false, more = false } = {}) {
    const token = ++state.token;
    if (!more) {
      const hit = !refresh && clientCache.get(cacheKey());
      if (hit && Date.now() - hit.at < CLIENT_CACHE_MS) {
        Object.assign(state, { items: hit.items, sources: hit.sources, cursor: hit.cursor, hasMore: hit.hasMore, loading: false });
        paint();
        return;
      }
      state.items = []; state.cursor = ""; state.hasMore = false;
      if (refresh) clientCache.delete(cacheKey());
    }
    state.loading = true;
    paint();
    try {
      let firstPage = !more;
      const target = state.items.length + (state.kind === "roms" ? PAGE : AUTO_LOAD_LIMIT);
      do {
        const params = new URLSearchParams({ limit: String(PAGE) });
        if (state.q) params.set("q", state.q);
        if (state.cursor) params.set("cursor", state.cursor);
        if (refresh && firstPage) params.set("refresh", "1");
        const page = await api(`${BASE}/${state.kind}?${params}`);
        if (token !== state.token) return; // a newer request superseded this one
        firstPage = false;
        state.items = state.items.concat(page.items || []);
        state.sources = page.sources || [];
        state.cursor = page.next_cursor || "";
        state.hasMore = !!page.has_more;
        paint();
      } while (state.hasMore && state.items.length < target);
      clientCache.set(cacheKey(), { at: Date.now(), items: state.items, sources: state.sources, cursor: state.cursor, hasMore: state.hasMore });
    } catch (err) {
      if (token === state.token) showToast(`Could not load the network library: ${esc(err.message || "unknown error")}`, "danger", 8000);
    } finally {
      if (token === state.token) { state.loading = false; paint(); }
    }
  }

  // ------------------------------------------------------------------ view model
  function visibleItems() {
    return state.items.filter((it) => !state.hidden.has(it.drone_id));
  }

  // Movies tab: episodes/extras of the same show collapse into one card across Drones.
  function buildCards(items) {
    if (state.kind !== "movies") return items.map((it) => ({ type: "item", item: it, items: [it] }));
    const cards = [];
    const shows = new Map();
    for (const it of items) {
      const show = it.show_title && (it.kind === "episode" || it.kind === "extra");
      if (!show) { cards.push({ type: "item", item: it, items: [it] }); continue; }
      const key = norm(it.scraped_show_title || it.show_title);
      let card = shows.get(key);
      if (!card) { card = { type: "show", title: it.scraped_show_title || it.show_title, item: it, items: [] }; shows.set(key, card); cards.push(card); }
      card.items.push(it);
    }
    return cards;
  }

  function cardDrones(card) {
    const seen = new Map();
    card.items.forEach((it) => seen.set(it.drone_id, it));
    return [...seen.values()];
  }

  // ------------------------------------------------------------------ rendering
  function paint() {
    const root = document.getElementById("networkMediaRoot");
    if (!root) return;
    const cards = buildCards(visibleItems()).sort((a, b) => norm(a.type === "show" ? a.title : titleOf(a.item)).localeCompare(norm(b.type === "show" ? b.title : titleOf(b.item))));
    state.byKey = new Map();
    cards.forEach((card, i) => state.byKey.set(String(i), card));
    const chips = state.sources.map((s) => {
      const off = state.hidden.has(s.drone_id);
      const tone = s.status === "ok" ? "success" : s.status === "cached" ? "warning" : s.status === "unsupported" ? "secondary" : "danger";
      const note = s.status === "cached" ? ` · saved copy${s.cached_age_seconds != null ? ` (${Math.max(1, Math.round(s.cached_age_seconds / 60))} min old)` : ""}`
        : s.status === "unsupported" ? " · needs update" : s.status === "offline" ? " · offline" : s.status === "timeout" ? " · slow" : s.status === "error" ? " · error" : "";
      return `<button type="button" class="btn btn-sm nm-chip ${off ? "btn-outline-secondary" : "btn-outline-light active"}" data-drone="${esc(s.drone_id)}" title="${esc(s.error || s.status)}">
        <span class="nm-dot bg-${tone}"></span>${esc(s.is_local ? "This Drone" : s.name)} <span class="text-muted small">${Number(s.total || 0)}${esc(note)}</span></button>`;
    }).join("");
    const grid = cards.length ? cards.map((card, i) => cardHtml(card, i)).join("")
      : `<div class="themed-empty">${state.loading ? "Loading…" : "Nothing found on the Drones that answered."}</div>`;
    root.querySelector("#nmChips").innerHTML = chips;
    const gridEl = root.querySelector("#nmGrid");
    gridEl.innerHTML = grid;
    gridEl.querySelectorAll("img").forEach((img) => img.addEventListener("error", () => img.remove(), { once: true }));
    root.querySelector("#nmStatus").textContent = state.loading ? "Loading…" : `${cards.length} shown · ${state.items.length} loaded${state.hasMore ? " · more available" : ""}`;
    root.querySelector("#nmMore").classList.toggle("d-none", !state.hasMore || state.loading);
    root.querySelectorAll(".nm-tab").forEach((b) => b.classList.toggle("active", b.dataset.kind === state.kind));
  }

  function cardHtml(card, i) {
    const it = card.item;
    const art = card.type === "show" ? (it.entry_key ? `${BASE}/movies/${enc(it.drone_id)}/${enc(it.entry_key)}/artwork/poster` : "") : artUrl(it);
    const drones = cardDrones(card);
    const sub = card.type === "show" ? `${card.items.length} episode${card.items.length === 1 ? "" : "s"}`
      : state.kind === "music" ? esc(it.artist || "") : state.kind === "roms" ? esc(it.system || "") : esc(it.kind === "episode" ? "Episode" : "");
    const title = card.type === "show" ? card.title : titleOf(it);
    return `<button type="button" class="nm-card" data-card="${i}" title="${esc(title)}">
      <span class="nm-art">${art ? `<img loading="lazy" src="${esc(art)}" alt="">` : ""}<i class="bi ${card.type === "show" ? "bi-collection-play" : state.kind === "music" ? "bi-music-note-beamed" : state.kind === "roms" ? "bi-controller" : "bi-film"}"></i></span>
      <span class="nm-title">${esc(title)}</span>
      <span class="nm-sub">${sub}</span>
      <span class="nm-badges">${drones.slice(0, 3).map(badge).join("")}${drones.length > 3 ? `<span class="badge text-bg-dark">+${drones.length - 3}</span>` : ""}</span>
    </button>`;
  }

  // ------------------------------------------------------------------ details
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
    modalEl.addEventListener("hidden.bs.modal", () => { modalEl.querySelectorAll("video,audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); }); });
    return modalEl;
  }

  function copyButton(it) {
    if (it.is_local || it.exists_locally || typeof adminEnabled === "undefined" || !adminEnabled) return "";
    return `<button type="button" class="btn btn-sm btn-primary" data-act="copy" data-key="${esc(keyOf(it))}"><i class="bi bi-download me-1"></i>Copy to this Drone</button>`;
  }
  function playButton(it) {
    if (state.kind === "roms" || !it.entry_key) return "";
    return `<button type="button" class="btn btn-sm btn-success" data-act="play" data-key="${esc(keyOf(it))}"><i class="bi bi-play-fill me-1"></i>Play${it.is_local ? "" : " (streams from " + esc(droneLabel(it)) + ")"}</button>`;
  }
  function likeButton(it) {
    if (state.kind !== "music" || !it.entry_key) return "";
    return `<button type="button" class="btn btn-sm btn-outline-light" data-act="like" data-key="${esc(keyOf(it))}"><i class="bi ${it.liked ? "bi-heart-fill text-danger" : "bi-heart"}"></i></button>`;
  }

  function copyRow(it) {
    return `<div class="nm-copy d-flex flex-wrap align-items-center gap-2 py-2 border-top border-secondary-subtle">
      ${badge(it)}<span class="small text-muted flex-grow-1">${it.file_size ? esc(formatBytes(it.file_size)) : ""}${it.exists_locally ? " · already on this Drone" : ""}</span>
      ${playButton(it)}${copyButton(it)}${likeButton(it)}</div>`;
  }

  async function openCard(card) {
    const modal = ensureModal();
    const it = card.item;
    const title = card.type === "show" ? card.title : titleOf(it);
    modal.querySelector(".modal-title").textContent = title;
    const body = modal.querySelector(".modal-body");
    const known = new Map();
    card.items.forEach((x) => known.set(keyOf(x), x));
    modal._items = known;
    if (card.type === "show") {
      const eps = [...card.items].sort((a, b) => (a.season_number || 0) - (b.season_number || 0) || (a.episode_number || 0) - (b.episode_number || 0));
      body.innerHTML = `<div class="d-flex flex-wrap gap-2 mb-2">${cardDrones(card).map(badge).join("")}
        ${typeof adminEnabled !== "undefined" && adminEnabled ? `<button class="btn btn-sm btn-primary ms-auto" data-act="copy-show"><i class="bi bi-download me-1"></i>Copy all remote episodes</button>` : ""}</div>
        <div class="nm-player"></div>
        ${eps.map((ep) => `<div><div class="small fw-semibold mt-2">${ep.season_number != null ? `S${String(ep.season_number).padStart(2, "0")}` : ""}${ep.episode_number != null ? `E${String(ep.episode_number).padStart(2, "0")}` : ""} ${esc(ep.episode_title || titleOf(ep))}</div>${copyRow(ep)}</div>`).join("")}`;
      modal._show = card;
    } else {
      const meta = it.metadata || {};
      body.innerHTML = `<div class="nm-player"></div>
        <div class="mb-2 small text-muted">${esc([it.artist, it.album, it.system, meta.release_date || meta.year].filter(Boolean).join(" · "))}</div>
        ${meta.overview ? `<p class="small">${esc(meta.overview)}</p>` : ""}
        <h6 class="mt-3">On this Drone and others</h6>
        <div class="nm-copies">${copyRow(it)}</div>
        <div class="small text-muted mt-2 nm-also">Looking for other copies on the network…</div>`;
      modal._show = null;
      alsoOn(it, body, known);
    }
    bootstrap.Modal.getOrCreateInstance(modal).show();
  }

  async function alsoOn(it, body, known) {
    const params = new URLSearchParams({ title: titleOf(it), exclude_drone: it.drone_id });
    ["fingerprint", "artist", "system"].forEach((k) => it[k] && params.set(k, it[k]));
    const note = body.querySelector(".nm-also");
    try {
      const res = await api(`${BASE}/${state.kind}/also-on?${params}`);
      const copies = res.copies || [];
      copies.forEach((c) => known.set(keyOf(c), c));
      body.querySelector(".nm-copies").insertAdjacentHTML("beforeend", copies.map(copyRow).join(""));
      if (note) note.textContent = copies.length ? `Also on ${copies.length} other Drone${copies.length === 1 ? "" : "s"}.` : "Only on this Drone.";
    } catch (err) {
      if (note) note.textContent = "Could not check other Drones.";
    }
  }

  function play(it) {
    const modal = ensureModal();
    const host = modal.querySelector(".nm-player");
    if (!host) return;
    const tag = state.kind === "music" ? "audio" : "video";
    host.innerHTML = `<${tag} controls autoplay class="w-100 mb-2" src="${esc(streamUrl(it))}"></${tag}><div class="small text-muted mb-2">Playing from ${esc(droneLabel(it))}</div>`;
    const media = host.firstElementChild;
    media.addEventListener("error", () => showToast("That Drone could not stream this file right now.", "warning", 6000));
  }

  async function copyItems(items) {
    const remote = items.filter((x) => !x.is_local && !x.exists_locally);
    if (!remote.length) { showToast("Everything selected is already on this Drone.", "info"); return; }
    const jobIds = [];
    try {
      for (const it of remote) {
        const res = await apiPost("/admin/local-network/sync", {
          peer_id: it.drone_id, asset_type: state.kind, system: it.system || "", item: it, include_artwork: true,
        });
        (res.jobs || []).forEach((j) => jobIds.push(j.id || j.job_id));
      }
    } catch (err) {
      showToast(`Could not start the transfer: ${esc(err.message || "unknown error")}`, "danger", 8000);
    }
    if (jobIds.length) Toast.track(jobIds, remote.length === 1 ? `Copying ${titleOf(remote[0])}` : `Copying ${remote.length} items`);
    else if (remote.length) showToast("Nothing new to copy — it may already be here or queued.", "info");
  }

  async function toggleLike(it) {
    const liked = !it.liked;
    try {
      await apiPost(`${BASE}/music/like`, { drone_id: it.drone_id, entry_key: it.entry_key, liked });
      it.liked = liked;
      clientCache.clear();
      const btn = modalEl.querySelector(`[data-act="like"][data-key="${CSS.escape(keyOf(it))}"] i`);
      if (btn) btn.className = `bi ${liked ? "bi-heart-fill text-danger" : "bi-heart"}`;
    } catch (err) {
      showToast(`Could not save the like: ${esc(err.message || "unknown error")}`, "danger");
    }
  }

  // ------------------------------------------------------------------ page
  function pageHtml() {
    return `<div class="network-media">
      <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
        <h2 class="h4 mb-0 me-2"><i class="bi bi-diagram-3 me-2"></i>All Drones</h2>
        <div class="btn-group" role="group">${KINDS.map(([k, label, icon]) => `<button type="button" class="btn btn-outline-light btn-sm nm-tab" data-kind="${k}"><i class="bi ${icon} me-1"></i>${label}</button>`).join("")}</div>
        <input id="nmSearch" class="form-control form-control-sm nm-search" type="search" placeholder="Search every Drone" value="${esc(state.q)}">
        <button type="button" id="nmRefresh" class="btn btn-outline-light btn-sm" title="Ask every Drone again"><i class="bi bi-arrow-clockwise"></i></button>
        <span id="nmStatus" class="small text-muted ms-auto"></span>
      </div>
      <div id="nmChips" class="d-flex flex-wrap gap-2 mb-3"></div>
      <div id="nmGrid" class="nm-grid"></div>
      <div class="text-center my-3"><button type="button" id="nmMore" class="btn btn-outline-light btn-sm d-none">Load more</button></div>
    </div>`;
  }

  function wire(root) {
    root.addEventListener("click", (event) => {
      const tab = event.target.closest(".nm-tab");
      if (tab) { state.kind = tab.dataset.kind; setHash(`#network/${state.kind}`); return; }
      const chip = event.target.closest(".nm-chip");
      if (chip) { const id = chip.dataset.drone; state.hidden.has(id) ? state.hidden.delete(id) : state.hidden.add(id); paint(); return; }
      if (event.target.closest("#nmRefresh")) { load({ refresh: true }); return; }
      if (event.target.closest("#nmMore")) { load({ more: true }); return; }
      const card = event.target.closest(".nm-card");
      if (card) { const c = state.byKey.get(card.dataset.card); if (c) openCard(c); }
    });
    let timer = null;
    root.querySelector("#nmSearch").addEventListener("input", (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.q = event.target.value.trim(); load(); }, 350);
    });
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest && event.target.closest("#networkMediaModalHost [data-act], .modal [data-act]");
    if (!btn || !modalEl || !modalEl.contains(btn)) return;
    const act = btn.dataset.act;
    if (act === "copy-show" && modalEl._show) { copyItems(modalEl._show.items); return; }
    const it = modalEl._items && modalEl._items.get(btn.dataset.key);
    if (!it) return;
    if (act === "play") play(it);
    else if (act === "copy") copyItems([it]);
    else if (act === "like") toggleLike(it);
  });

  window.renderNetworkMediaPage = async function renderNetworkMediaPage(hash) {
    const m = /^#network\/(movies|music|roms)/.exec(hash || "");
    if (m && m[1] !== state.kind) { state.kind = m[1]; state.items = []; state.sources = []; }
    content.innerHTML = pageHtml();
    wire(content);
    paint();
    await load();
  };

  Toast.restore();
})();
