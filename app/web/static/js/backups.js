/* Backups: Admin -> Backups (create/manage this Drone's backups) and the Assets
 * "Backups" page (find, inspect, download and restore backups across the swarm).
 *
 * Loaded after drone.js and uses its globals (api, apiPost, escapeHtml, jsAttr,
 * formatBytes, showToast, setLoading, titleNode, subtitleNode, content, setHash,
 * _apiRequestUrl, renderAssetTypeSwitcher, clearSystemTheme). Every value that came
 * from a backup or a peer is escaped before it is put into markup.
 */
"use strict";

const BK = {
  timer: null,
  inFlight: false,
  assets: { sources: [], selected: null, results: {}, query: "", group: "", token: 0 },
  wizard: null,
  missing: null,
  restorePollTimer: null,
  trackedBatchId: null,
};
const BK_ADMIN_HASH = "#admin/backups";
const BK_ASSETS_HASH = "#backups";
const BK_SLOW = { timeoutMs: 120000 };
const BK_ASSET_KIND_LABELS = {
  roms: "Games & artwork",
  bios: "BIOS files (MD5-verified)",
  movies: "Movies & shows",
  music: "Music",
};

// ------------------------------------------------------------------ helpers

function bkModal(id, inner, size = "modal-lg") {
  let modal = document.getElementById(id);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = id;
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    document.body.appendChild(modal);
  }
  modal.innerHTML = `<div class="modal-dialog modal-dialog-centered modal-dialog-scrollable ${size}"><div class="modal-content themed-modal">${inner}</div></div>`;
  if (window.bootstrap?.Modal) window.bootstrap.Modal.getOrCreateInstance(modal).show();
  else { modal.classList.add("show"); modal.style.display = "block"; }
  return modal;
}

function bkHideModal(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  if (window.bootstrap?.Modal) window.bootstrap.Modal.getOrCreateInstance(modal).hide();
  else { modal.classList.remove("show"); modal.style.display = "none"; }
}

function bkDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function bkGroups(categories) {
  const groups = [];
  const index = new Map();
  for (const category of categories || []) {
    if (!index.has(category.group)) {
      index.set(category.group, { group: category.group, label: category.group_label || category.group, items: [] });
      groups.push(index.get(category.group));
    }
    index.get(category.group).items.push(category);
  }
  return groups;
}

function bkCategoryChips(categories, limit = 6) {
  const groups = bkGroups(categories);
  const chips = groups.slice(0, limit).map((group) => {
    const files = group.items.reduce((sum, item) => sum + Number(item.file_count || 0), 0);
    const detail = group.items.length > 1 ? `${group.items.length} parts, ${files} files` : `${files} files`;
    return `<span class="badge rounded-pill text-bg-secondary me-1 mb-1" title="${escapeHtml(group.items.map((i) => i.label).join(", "))}">${escapeHtml(group.label)} <span class="opacity-75">${escapeHtml(detail)}</span></span>`;
  });
  if (groups.length > limit) chips.push(`<span class="badge rounded-pill text-bg-dark mb-1">+${groups.length - limit} more</span>`);
  return chips.join("") || '<span class="text-muted small">No files</span>';
}

function bkSourceLine(backup) {
  const source = backup.source || {};
  const bits = [];
  if (source.drone_name) bits.push(`made on ${escapeHtml(source.drone_name)}`);
  if (source.batocera_version) bits.push(`Batocera ${escapeHtml(source.batocera_version)}`);
  if (source.architecture) bits.push(escapeHtml(source.architecture));
  return bits.join(" · ");
}

function bkCompatBadge(backup) {
  const warnings = backup.compatibility?.warnings || [];
  if (!warnings.length) return "";
  return `<span class="badge text-bg-warning ms-1" title="${escapeHtml(warnings.join(" "))}"><i class="bi bi-exclamation-triangle me-1"></i>Different system</span>`;
}

function bkCategoryChecklist(categories, prefix, checkedIds = null) {
  return bkGroups(categories).map((group, gi) => {
    const items = group.items.map((item) => {
      const checked = checkedIds ? checkedIds.has(item.id) : true;
      return `<div class="form-check ms-3">
        <input class="form-check-input bk-cat" type="checkbox" id="${prefix}-${gi}-${escapeHtml(item.id)}" data-cat="${escapeHtml(item.id)}" data-group="${escapeHtml(group.group)}" ${checked ? "checked" : ""}>
        <label class="form-check-label small" for="${prefix}-${gi}-${escapeHtml(item.id)}">${escapeHtml(item.label)} <span class="text-muted">${Number(item.file_count || 0)} files, ${formatBytes(item.size_bytes || 0)}</span></label>
      </div>`;
    }).join("");
    const groupChecked = group.items.every((item) => (checkedIds ? checkedIds.has(item.id) : true));
    return `<div class="mb-2">
      <div class="form-check">
        <input class="form-check-input bk-group" type="checkbox" id="${prefix}-g-${gi}" data-group="${escapeHtml(group.group)}" ${groupChecked ? "checked" : ""} onchange="bkToggleGroup(this)">
        <label class="form-check-label fw-semibold" for="${prefix}-g-${gi}">${escapeHtml(group.label)}</label>
      </div>
      ${items}
    </div>`;
  }).join("");
}

function bkToggleGroup(input) {
  const container = input.closest(".bk-checklist");
  if (!container) return;
  container.querySelectorAll(".bk-cat").forEach((box) => {
    if (box.dataset.group === input.dataset.group) box.checked = input.checked;
  });
}

function bkCheckedCategories(containerId) {
  return Array.from(document.querySelectorAll(`#${containerId} .bk-cat`)).filter((box) => box.checked).map((box) => box.dataset.cat);
}

function bkSetAll(containerId, value) {
  document.querySelectorAll(`#${containerId} input[type=checkbox]`).forEach((box) => { box.checked = value; });
}

function stopBackupsAutoRefresh() {
  if (BK.timer) clearInterval(BK.timer);
  BK.timer = null;
  BK.inFlight = false;
}

function bkAssetsSummaryLine(assets) {
  if (!assets) return "";
  if (assets.recorded) {
    const counts = assets.counts || {};
    const roms = Number(counts.roms || 0);
    const bios = Number(counts.bios || 0);
    const movies = Number(counts.movies || 0);
    const music = Number(counts.music || 0);
    const mediaNote = assets.media_roms ? ` (${Number(assets.media_roms)} with artwork)` : "";
    return `Records ${roms} game${roms === 1 ? "" : "s"}${mediaNote}, ${bios} BIOS, ${movies} movie${movies === 1 ? "" : "s"}, ${music} music track${music === 1 ? "" : "s"}${assets.truncated ? " (truncated)" : ""}`;
  }
  if (assets.legacy) {
    return "Game list only (older backup)";
  }
  return "";
}

function bkRestoreAssetsBanner(status) {
  const assets = status?.assets;
  if (!assets || !assets.state) return "";
  if (assets.state === "queued" && assets.batch_id && Number(assets.queued || 0) > 0 && BK.trackedBatchId !== assets.batch_id) {
    BK.trackedBatchId = assets.batch_id;
    if (window.DroneTransferToast?.trackBatch) {
      window.DroneTransferToast.trackBatch(assets.batch_id, `Restoring from ${status.backup_name || "backup"}`);
    }
  }
  if (assets.state === "pending" || assets.state === "queueing") {
    return `<div class="small mt-1"><span class="spinner-border spinner-border-sm me-1"></span>Games &amp; media will be queued from the swarm as soon as the settings restore finishes…</div>`;
  }
  if (assets.state === "queued") {
    const queued = Number(assets.queued || 0);
    const skipped = Number(assets.skipped || 0);
    const unavailable = Number(assets.unavailable || 0);
    return `<div class="small mt-1 d-flex flex-wrap align-items-center gap-2">
      <span><i class="bi bi-cloud-arrow-down me-1"></i><strong>Games &amp; media:</strong> ${queued} download(s) queued across paired Drones${skipped ? `, ${skipped} already here/skipped` : ""}${unavailable ? `, <strong>${unavailable} unavailable on online Drones</strong>` : ""}.</span>
      ${assets.batch_id && queued > 0 ? `<button type="button" class="btn btn-sm btn-outline-light" onclick="bkCancelAssetBatch(${jsAttr(assets.batch_id)})">Cancel downloads</button>` : ""}
    </div>`;
  }
  if (assets.state === "skipped" || assets.state === "error") {
    return `<div class="small mt-1 text-warning"><i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(assets.error || "No games or media were queued.")}</div>`;
  }
  return "";
}

async function bkCancelAssetBatch(batchId) {
  if (!batchId) return;
  try {
    const res = await apiPost(`/admin/downloads/batches/${encodeURIComponent(batchId)}/cancel`, {});
    showToast(`Cancelled ${Number(res.cancelled || 0)} remaining download(s).`, "info");
  } catch (err) {
    showToast(escapeHtml(err.message || "Could not cancel the batch."), "danger");
  }
}

function bkRestoreBanner(status) {
  if (!status || status.state === "idle" || !status.state) return "";
  if (status.state === "running") {
    const stage = status.stage === "snapshot" ? "Taking a safety snapshot" : "Restoring files (EmulationStation is restarting)";
    return `<div class="alert alert-info d-flex align-items-center gap-2"><span class="spinner-border spinner-border-sm"></span><div><strong>${escapeHtml(stage)}…</strong> ${escapeHtml(status.backup_name || "")}${bkRestoreAssetsBanner(status)}</div></div>`;
  }
  if (status.state === "error") {
    return `<div class="alert alert-danger"><strong>Restore failed:</strong> ${escapeHtml(status.error || "unknown error")}${status.snapshot_id ? " A safety snapshot was taken first; you can roll back from Safety snapshots below." : ""}${bkRestoreAssetsBanner(status)}</div>`;
  }
  const result = status.result || {};
  const failed = Number(result.failed_count || 0);
  return `<div class="alert ${failed ? "alert-warning" : "alert-success"}"><strong>Restored ${escapeHtml(status.backup_name || "backup")}:</strong>
    ${Number(result.written || 0)} file(s) written, ${Number(result.unchanged || 0)} already identical, ${Number(result.removed || 0)} removed${failed ? `, <strong>${failed} failed</strong>` : ""}.
    ${result.warning ? `<div>${escapeHtml(result.warning)}</div>` : ""}
    ${bkRestoreAssetsBanner(status)}
    ${failed ? `<details class="mt-1"><summary class="small">Show failures</summary><ul class="small mb-0">${(result.failed || []).map((f) => `<li><code>${escapeHtml(f.path)}</code>: ${escapeHtml(f.error)}</li>`).join("")}</ul></details>` : ""}</div>`;
}

// --------------------------------------------------------- admin: this Drone

async function renderBackupsAdminPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Backups";
  subtitleNode.textContent = "Snapshots of Batocera, EmulationStation and emulator settings, saves and save states";
  setLoading(true, "Loading backups...");
  let payload;
  try {
    payload = await api("/admin/backups");
  } catch (err) {
    content.innerHTML = `<div class="alert alert-danger">Failed to load backups: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  content.innerHTML = `
    <div id="bkRestoreBanner">${bkRestoreBanner(payload.restore)}</div>
    <div class="card log-card mb-3">
      <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2">
        <span><i class="bi bi-archive me-2" aria-hidden="true"></i>This Drone's backups</span>
        <div class="d-flex gap-2">
          <button class="btn btn-sm btn-outline-light" onclick="setHash('${BK_ASSETS_HASH}')"><i class="bi bi-diagram-3 me-1"></i>Browse swarm backups</button>
          <button class="btn btn-sm btn-primary" onclick="bkOpenCreate()"><i class="bi bi-plus-circle me-1"></i>Create Backup</button>
        </div>
      </div>
      <div class="card-body">
        <p class="text-muted small mb-3">A backup holds the parts you choose: <code>batocera.conf</code>, EmulationStation settings and collections, controller mappings and RetroArch remaps, RetroArch and each standalone emulator's settings, cheats, game lists and custom scripts, plus per-system saves and save states if you tick them. Passwords, Wi-Fi keys and API tokens are never included. ROMs, BIOS files, movies and music are not stored in the archive, but every backup records which ones this Drone had so missing games and media can be fetched from your paired Drones on restore or at any time with Get missing games. Firmware and caches are never backed up. Paired Drones can see and download your backups from their Backups page.</p>
        <div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table bff-stack">
          <thead><tr><th>Name</th><th>Created</th><th>Contents</th><th>Size</th><th class="download-actions">Actions</th></tr></thead>
          <tbody id="bkAdminBody">${bkAdminRows(payload.backups || [], false)}</tbody>
        </table></div>
      </div>
    </div>
    <div class="card log-card mb-3">
      <div class="card-header"><i class="bi bi-shield-check me-2" aria-hidden="true"></i>Safety snapshots</div>
      <div class="card-body">
        <p class="text-muted small mb-3">Taken automatically before every restore, covering exactly the parts the restore changed. Rolling back restores those parts to how they were, removing files the restore added. The newest five are kept; they stay on this Drone and are never shared.</p>
        <div class="table-responsive"><table class="table table-sm table-hover align-middle themed-table bff-stack">
          <thead><tr><th>Snapshot</th><th>Taken</th><th>Contents</th><th>Size</th><th class="download-actions">Actions</th></tr></thead>
          <tbody id="bkSnapshotBody">${bkAdminRows(payload.snapshots || [], true)}</tbody>
        </table></div>
      </div>
    </div>`;
  bkStartAdminPolling(payload);
}

function bkAdminRows(rows, snapshots) {
  if (!rows.length) {
    return `<tr><td colspan="5" class="text-center text-muted small py-3">${snapshots ? "No snapshots yet." : "No backups yet. Click Create Backup to make one."}</td></tr>`;
  }
  return rows.map((row) => {
    const id = row.backup_id;
    const complete = row.status === "complete";
    const status = row.status === "creating"
      ? '<span class="badge text-bg-info"><span class="spinner-border spinner-border-sm me-1"></span>Creating</span>'
      : row.status === "error" ? `<span class="badge text-bg-danger" title="${escapeHtml(row.error_message || "")}">Failed</span>` : "";
    const origin = snapshots ? ""
      : row.is_local ? '<span class="badge text-bg-secondary ms-1"><i class="bi bi-hdd me-1"></i>Created here</span>'
        : `<span class="badge text-bg-info ms-1"><i class="bi bi-cloud-download me-1"></i>From ${escapeHtml(row.downloaded_from_name || row.downloaded_from_id || "a paired Drone")}</span>`;
    const actions = complete ? `
      ${snapshots
        ? `<button class="btn btn-sm btn-outline-warning" title="Roll back" aria-label="Roll back" onclick="bkOpenRestore(${jsAttr(id)}, true)"><i class="bi bi-arrow-counterclockwise"></i></button>`
        : `<button class="btn btn-sm btn-outline-warning" title="Restore to this Drone" aria-label="Restore to this Drone" onclick="bkOpenRestore(${jsAttr(id)}, false)"><i class="bi bi-arrow-repeat"></i></button>
           <button class="btn btn-sm btn-outline-info" title="Get missing games" aria-label="Get missing games" onclick="bkOpenMissingGames(${jsAttr(id)})"><i class="bi bi-controller me-1"></i>Get missing games</button>`}
      <button class="btn btn-sm btn-outline-secondary" title="View contents" aria-label="View contents" onclick="bkOpenContents(null, ${jsAttr(id)})"><i class="bi bi-folder2-open"></i></button>
      <a class="btn btn-sm btn-outline-primary" title="Download file" aria-label="Download file" href="${escapeHtml(_apiRequestUrl(`/admin/backups/${id}/download`))}"><i class="bi bi-download"></i></a>
      ${snapshots ? "" : `<button class="btn btn-sm btn-outline-light" title="Rename" aria-label="Rename" onclick="bkOpenRename(${jsAttr(id)})"><i class="bi bi-pencil"></i></button>`}` : "";
    const deletable = row.status !== "creating";
    const files = complete ? `${Number(row.file_count || 0)} files` : "";
    return `<tr>
      <td class="small"><div><strong>${escapeHtml(row.name || "Backup")}</strong>${origin}${bkCompatBadge(row)} ${status}</div>
        ${row.description ? `<div class="text-muted">${escapeHtml(row.description)}</div>` : ""}
        ${complete && !snapshots && !row.is_local ? `<div class="text-muted">${bkSourceLine(row)}</div>` : ""}
        ${row.status === "error" ? `<div class="text-danger">${escapeHtml(row.error_message || "")}</div>` : ""}</td>
      <td class="small text-nowrap">${escapeHtml(bkDate(row.created_at))}</td>
      <td class="small">${complete ? bkCategoryChips(row.categories) : ""}<div class="text-muted">${files}</div></td>
      <td class="small text-nowrap">${complete ? formatBytes(row.size_bytes || 0) : "--"}</td>
      <td class="download-actions text-nowrap">${actions}
        ${deletable ? `<button class="btn btn-sm btn-outline-danger" title="Delete" aria-label="Delete" onclick="bkDelete(${jsAttr(id)})"><i class="bi bi-trash"></i></button>` : ""}</td>
    </tr>`;
  }).join("");
}

function bkStartAdminPolling(payload) {
  stopBackupsAutoRefresh();
  const busy = (p) => (p.backups || []).concat(p.snapshots || []).some((row) => row.status === "creating") || p.restore?.state === "running";
  if (!busy(payload)) return;
  BK.timer = setInterval(async () => {
    if (document.hidden || BK.inFlight || window.location.hash !== BK_ADMIN_HASH) return;
    BK.inFlight = true;
    try {
      const next = await api("/admin/backups");
      if (window.location.hash !== BK_ADMIN_HASH) return;
      const body = document.getElementById("bkAdminBody");
      const snaps = document.getElementById("bkSnapshotBody");
      const banner = document.getElementById("bkRestoreBanner");
      if (body) body.innerHTML = bkAdminRows(next.backups || [], false);
      if (snaps) snaps.innerHTML = bkAdminRows(next.snapshots || [], true);
      if (banner) banner.innerHTML = bkRestoreBanner(next.restore);
      if (!busy(next)) stopBackupsAutoRefresh();
    } catch (_) {
      // transient; keep the last good view
    } finally {
      BK.inFlight = false;
    }
  }, 2000);
}

async function bkOpenCreate() {
  bkModal("bkCreateModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi bi-archive me-2"></i>Create Backup</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body"><div class="text-center py-4"><span class="spinner-border"></span><div class="small text-muted mt-2">Looking at what this Drone has…</div></div></div>`);
  let data;
  try {
    data = await api("/admin/backups/categories", BK_SLOW);
  } catch (err) {
    bkModal("bkCreateModal", `<div class="modal-header"><h5 class="modal-title mb-0">Create Backup</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
      <div class="modal-body"><div class="alert alert-danger mb-0">${escapeHtml(err.message || "Could not read this Drone's files.")}</div></div>`);
    return;
  }
  const categories = data.categories || [];
  const total = categories.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
  bkModal("bkCreateModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi bi-archive me-2"></i>Create Backup</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body">
      <div class="mb-3"><label class="form-label small" for="bkName">Name</label>
        <input class="form-control" id="bkName" maxlength="120" placeholder="e.g. Working RetroArch shaders, before update"></div>
      <div class="mb-3"><label class="form-label small" for="bkDescription">Description <span class="text-muted">(optional)</span></label>
        <textarea class="form-control" id="bkDescription" rows="2" maxlength="1000"></textarea></div>
      <div class="d-flex justify-content-between align-items-center mb-2">
        <span class="small fw-semibold">What to include <span class="text-muted fw-normal">(${categories.length} parts, about ${formatBytes(total)} before compression)</span></span>
        <span><button class="btn btn-link btn-sm p-0 me-2" onclick="bkSetAll('bkCreateList', true)">All</button><button class="btn btn-link btn-sm p-0" onclick="bkSetAll('bkCreateList', false)">None</button></span>
      </div>
      <div id="bkCreateList" class="bk-checklist border rounded p-2" style="max-height: 45vh; overflow:auto">${categories.length ? bkCategoryChecklist(categories, "bkc", new Set(categories.filter((c) => !["saves", "states"].includes(c.group)).map((c) => c.id))) : '<div class="text-muted small">Nothing to back up was found.</div>'}</div>
      <div class="small text-muted mt-2"><i class="bi bi-info-circle me-1"></i>Saves and save states are not ticked by default: they are large, and a game's saves can be copied between Drones from its page (Saves &amp; Saved States). Tick them to include them.</div>
      ${data.skipped_count ? `<div class="small text-muted mt-2">${Number(data.skipped_count)} file(s) are always left out (caches, links, firmware and oversized files).</div>` : ""}
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-primary" id="bkCreateSubmit" onclick="bkSubmitCreate()" ${categories.length ? "" : "disabled"}><i class="bi bi-plus-circle me-1"></i>Create Backup</button>
    </div>`);
}

async function bkSubmitCreate() {
  const categories = bkCheckedCategories("bkCreateList");
  if (!categories.length) { showToast("Choose at least one part to back up.", "warning"); return; }
  const button = document.getElementById("bkCreateSubmit");
  if (button) button.disabled = true;
  try {
    await apiPost("/admin/backups", {
      name: document.getElementById("bkName")?.value || "",
      description: document.getElementById("bkDescription")?.value || "",
      categories,
    });
    bkHideModal("bkCreateModal");
    showToast("Backup started. It builds in the background.", "success");
    if (window.location.hash === BK_ADMIN_HASH) await renderBackupsAdminPage();
  } catch (err) {
    showToast(`Could not start the backup: ${escapeHtml(err.message || "unknown error")}`, "danger");
    if (button) button.disabled = false;
  }
}

function bkOpenRename(backupId) {
  bkModal("bkRenameModal", `
    <div class="modal-header"><h5 class="modal-title mb-0">Rename backup</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body"><div class="text-center py-3"><span class="spinner-border"></span></div></div>`, "");
  api(`/admin/backups/${encodeURIComponent(backupId)}`).then((data) => {
    const row = data.backup || {};
    bkModal("bkRenameModal", `
      <div class="modal-header"><h5 class="modal-title mb-0">Rename backup</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
      <div class="modal-body">
        <label class="form-label small" for="bkRenameName">Name</label><input class="form-control mb-2" id="bkRenameName" maxlength="120" value="${escapeHtml(row.name || "")}">
        <label class="form-label small" for="bkRenameDesc">Description</label><textarea class="form-control" id="bkRenameDesc" rows="2" maxlength="1000">${escapeHtml(row.description || "")}</textarea>
      </div>
      <div class="modal-footer"><button class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
        <button class="btn btn-primary" onclick="bkSubmitRename(${jsAttr(backupId)})">Save</button></div>`, "");
  }).catch((err) => showToast(escapeHtml(err.message || "Could not load the backup."), "danger"));
}

async function bkSubmitRename(backupId) {
  try {
    await apiPost(`/admin/backups/${encodeURIComponent(backupId)}/rename`, {
      name: document.getElementById("bkRenameName")?.value || "",
      description: document.getElementById("bkRenameDesc")?.value || "",
    });
    bkHideModal("bkRenameModal");
    await renderBackupsAdminPage();
  } catch (err) {
    showToast(escapeHtml(err.message || "Rename failed."), "danger");
  }
}

async function bkDelete(backupId) {
  if (!window.confirm("Delete this backup from this Drone? Copies on other Drones are not affected.")) return;
  try {
    await apiPost(`/admin/backups/${encodeURIComponent(backupId)}/delete`, {});
    if (window.location.hash === BK_ADMIN_HASH) await renderBackupsAdminPage();
    else await bkLoadAssets();
  } catch (err) {
    showToast(escapeHtml(err.message || "Delete failed."), "danger");
  }
}

// ------------------------------------------------------------------ contents
//
// The Contents view doubles as a picker: tick whole parts or single files, then
// "Restore selected". BK.pick = { whole: Set(categoryId), files: Set(path) } -- a part
// is either whole or narrowed to some of its files, never both.

const BK_MAX_PICKED_FILES = 5000;

async function bkOpenContents(droneId, backupId, preset = null) {
  bkModal("bkContentsModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi bi-folder2-open me-2"></i>Backup contents</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body"><div class="text-center py-4"><span class="spinner-border"></span></div></div>`, "modal-xl");
  const isLocalDrone = !droneId || BK.assets.sources.some((s) => s.is_local && s.drone_id === droneId);
  const url = droneId && !isLocalDrone ? `/admin/backups/swarm/${encodeURIComponent(droneId)}/${encodeURIComponent(backupId)}` : `/admin/backups/${encodeURIComponent(backupId)}`;
  let data;
  try {
    data = await api(url, BK_SLOW);
  } catch (err) {
    document.querySelector("#bkContentsModal .modal-body").innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(err.message || "Could not load the backup.")}</div>`;
    return;
  }
  const backup = data.backup || {};
  // Older backups may hold Batocera's empty-folder markers; restores skip them.
  const files = (data.files || []).filter((f) => !/(^|\/)\.(git)?keep$/i.test(f.path));
  const byCategory = new Map();
  for (const file of files) {
    if (!byCategory.has(file.category)) byCategory.set(file.category, []);
    byCategory.get(file.category).push(file);
  }
  const nodes = new Map();
  const trees = new Map();
  for (const [category, list] of byCategory.entries()) trees.set(category, bkBuildTree(category, list, nodes));
  BK.contents = { data, droneId: isLocalDrone ? null : droneId, backupId, byCategory, trees, nodes, open: new Set(), filter: "" };
  if (trees.size === 1) BK.contents.open.add(trees.values().next().value.id);
  BK.pick = { whole: new Set(preset?.categories || []), files: new Set(preset?.paths || []) };
  const warnings = backup.compatibility?.warnings || [];
  const assetsLine = bkAssetsSummaryLine(data.assets);
  const modal = document.getElementById("bkContentsModal");
  modal.querySelector(".modal-title").innerHTML = `<i class="bi bi-folder2-open me-2"></i>${escapeHtml(backup.name || "Backup")}`;
  modal.querySelector(".modal-body").innerHTML = `
    ${backup.description ? `<p class="mb-1">${escapeHtml(backup.description)}</p>` : ""}
    <p class="small text-muted mb-1">${bkSourceLine(backup)}${backup.created_at ? ` · ${escapeHtml(bkDate(backup.created_at))}` : ""} · ${files.length} files</p>
    ${assetsLine ? `<p class="small text-info mb-2"><i class="bi bi-controller me-1"></i>${escapeHtml(assetsLine)}</p>` : ""}
    ${warnings.map((w) => `<div class="alert alert-warning py-2 small mb-2">${escapeHtml(w)}</div>`).join("")}
    <p class="small text-muted mb-2">Tick whole parts or single files to restore only those.</p>
    <div class="d-flex flex-wrap gap-2 mb-2">
      <input class="form-control form-control-sm flex-grow-1" style="max-width: 28rem" placeholder="Filter files, e.g. Mario or dolphin" oninput="bkFilterContents(this.value)">
      <button class="btn btn-sm btn-outline-info" id="bkPickMatching" onclick="bkPickMatching(true)" disabled>Select matching</button>
      <button class="btn btn-sm btn-outline-secondary" id="bkUnpickMatching" onclick="bkPickMatching(false)" disabled>Clear matching</button>
      <button class="btn btn-sm btn-outline-secondary" onclick="bkPickAll(false)">Clear all</button>
    </div>
    <div id="bkContentsTree" class="bk-tree" role="tree" onchange="bkContentsChanged(event)" onclick="bkTreeClick(event)"></div>
    ${(data.skipped || []).length ? `<details class="mt-2"><summary class="small text-muted">${Number(backup.skipped_count || data.skipped.length)} file(s) were left out</summary><ul class="small">${data.skipped.map((s) => `<li><code>${escapeHtml(s.path)}</code> ${escapeHtml(s.reason)}</li>`).join("")}</ul></details>` : ""}`;
  let footer = modal.querySelector(".modal-footer");
  if (!footer) {
    footer = document.createElement("div");
    footer.className = "modal-footer";
    modal.querySelector(".modal-content").appendChild(footer);
  }
  footer.innerHTML = `
    <span class="small text-muted me-auto" id="bkPickSummary"></span>
    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
    <button type="button" class="btn btn-warning" id="bkPickRestore" onclick="bkRestorePicked()" disabled></button>`;
  bkRenderContentsTree();
}

function bkVisibleFiles(category) {
  const needle = BK.contents.filter;
  const list = BK.contents.byCategory.get(category) || [];
  return needle ? list.filter((f) => f.path.toLowerCase().includes(needle)) : list;
}

// Folder tree per part. Single-child folder chains are merged ("nand/user/save") to keep
// it compact; folders render only when opened, and at most BK_TREE_FILE_LIMIT files
// per folder.
const BK_TREE_FILE_LIMIT = 400;

function bkBuildTree(category, files, nodes) {
  const split = files.map((f) => f.path.split("/"));
  let prefix = split.length ? split[0].slice(0, -1) : [];
  for (const parts of split) {
    let i = 0;
    while (i < prefix.length && i < parts.length - 1 && parts[i] === prefix[i]) i += 1;
    prefix = prefix.slice(0, i);
  }
  const make = (name, path, category, depth) => {
    const node = { id: `n${nodes.size}`, name, path, category, dirs: new Map(), files: [], fileCount: 0, size: 0, depth };
    nodes.set(node.id, node);
    return node;
  };
  const root = make(category, prefix.join("/"), category, 0);
  root.isCategory = true;
  for (const file of files) {
    const parts = file.path.split("/").slice(prefix.length);
    let node = root;
    for (const dir of parts.slice(0, -1)) {
      if (!node.dirs.has(dir)) node.dirs.set(dir, make(dir, node.path ? `${node.path}/${dir}` : dir, category, 0));
      node = node.dirs.get(dir);
    }
    node.files.push(Object.assign({ name: parts[parts.length - 1] }, file));
  }
  const finish = (node, depth) => {
    const merged = new Map();
    for (let child of node.dirs.values()) {
      while (child.dirs.size === 1 && !child.files.length) {
        const only = child.dirs.values().next().value;
        only.name = `${child.name}/${only.name}`;
        nodes.delete(child.id);
        child = only;
      }
      merged.set(child.name, child);
    }
    node.dirs = new Map(Array.from(merged.entries()).sort((x, y) => x[0].localeCompare(y[0], undefined, { numeric: true })));
    node.files.sort((x, y) => x.name.localeCompare(y.name, undefined, { numeric: true }));
    node.depth = depth;
    node.fileCount = node.files.length;
    node.size = node.files.reduce((sum, f) => sum + Number(f.size || 0), 0);
    for (const child of node.dirs.values()) {
      finish(child, depth + 1);
      node.fileCount += child.fileCount;
      node.size += child.size;
    }
  };
  finish(root, 0);
  return root;
}

function bkNodePaths(node) {
  if (!node.allPaths) {
    node.allPaths = node.files.map((f) => f.path);
    for (const child of node.dirs.values()) node.allPaths.push(...bkNodePaths(child));
  }
  return node.allPaths;
}

function bkNodePicked(node) {
  if (BK.pick.whole.has(node.category)) return node.fileCount;
  let count = 0;
  for (const path of bkNodePaths(node)) if (BK.pick.files.has(path)) count += 1;
  return count;
}

function bkNodeMatches(node) {
  const filter = BK.contents.filter;
  if (!filter) return node.fileCount;
  if (node.matchFilter !== filter) {
    node.matchFilter = filter;
    node.matchCount = bkNodePaths(node).filter((p) => p.toLowerCase().includes(filter)).length;
  }
  return node.matchCount;
}

function bkTreeCheckbox(attrs, picked, total, label) {
  const state = picked === 0 ? "" : picked === total ? "checked" : "";
  const partial = picked > 0 && picked < total ? 1 : 0;
  return `<input class="form-check-input" type="checkbox" ${attrs} ${state} data-indeterminate="${partial}" aria-label="${escapeHtml(label)}">`;
}

function bkTreeRows(node, out) {
  const filter = BK.contents.filter;
  const labels = BK.contents.labels;
  const open = filter ? bkNodeMatches(node) > 0 : BK.contents.open.has(node.id);
  const picked = bkNodePicked(node);
  const label = node.isCategory ? (labels.get(node.category) || node.category) : node.name;
  const meta = `${filter ? `${bkNodeMatches(node)} of ` : ""}${node.fileCount} file${node.fileCount === 1 ? "" : "s"} · ${formatBytes(node.size)}${picked && picked < node.fileCount ? ` · ${picked} selected` : ""}`;
  out.push(`<div class="bk-tree-row ${node.isCategory ? "bk-tree-cat" : ""}" role="treeitem" aria-expanded="${open}" style="--depth:${node.depth}">
    <button type="button" class="bk-tree-caret" data-toggle="${node.id}" aria-label="${open ? "Collapse" : "Expand"} ${escapeHtml(label)}"><i class="bi bi-chevron-right"></i></button>
    ${bkTreeCheckbox(`data-node="${node.id}"`, picked, node.fileCount, `Restore ${label}`)}
    <i class="bi ${node.isCategory ? "bi-collection" : open ? "bi-folder2-open" : "bi-folder2"} bk-tree-icon"></i>
    <span class="bk-tree-name" data-toggle="${node.id}" title="${escapeHtml(node.path || label)}">${escapeHtml(label)}${node.isCategory && node.path ? ` <span class="bk-tree-path">${escapeHtml(node.path)}/</span>` : ""}</span>
    <span class="bk-tree-meta">${meta}</span></div>`);
  if (!open) return;
  for (const child of node.dirs.values()) {
    if (!filter || bkNodeMatches(child)) bkTreeRows(child, out);
  }
  const files = filter ? node.files.filter((f) => f.path.toLowerCase().includes(filter)) : node.files;
  const whole = BK.pick.whole.has(node.category);
  for (const file of files.slice(0, BK_TREE_FILE_LIMIT)) {
    out.push(`<div class="bk-tree-row bk-tree-file" role="treeitem" style="--depth:${node.depth + 1}">
      <span class="bk-tree-spacer"></span>
      <input class="form-check-input bk-pick-file" type="checkbox" data-cat="${escapeHtml(file.category)}" data-path="${escapeHtml(file.path)}" ${whole || BK.pick.files.has(file.path) ? "checked" : ""} aria-label="Restore ${escapeHtml(file.path)}">
      <i class="bi bi-file-earmark bk-tree-icon"></i>
      <span class="bk-tree-name" title="${escapeHtml(file.path)}">${escapeHtml(file.name)}${file.redacted ? ' <span class="badge text-bg-dark" title="Passwords and tokens were removed from this file; this Drone\'s own values are kept on restore.">secrets removed</span>' : ""}</span>
      <span class="bk-tree-meta">${formatBytes(file.size || 0)}</span></div>`);
  }
  if (files.length > BK_TREE_FILE_LIMIT) {
    out.push(`<div class="bk-tree-row bk-tree-more" style="--depth:${node.depth + 1}"><span class="bk-tree-spacer"></span>…and ${files.length - BK_TREE_FILE_LIMIT} more files. Tick the folder to include them all, or narrow the filter.</div>`);
  }
}

function bkRenderContentsTree() {
  const tree = document.getElementById("bkContentsTree");
  if (!tree || !BK.contents) return;
  BK.contents.labels = new Map((BK.contents.data.backup?.categories || []).map((c) => [c.id, c.label]));
  const scroller = tree.closest(".modal-body");
  const top = scroller ? scroller.scrollTop : 0;
  const out = [];
  for (const root of BK.contents.trees.values()) {
    if (!BK.contents.filter || bkNodeMatches(root)) bkTreeRows(root, out);
  }
  tree.innerHTML = out.join("") || '<div class="text-muted small p-2">No matching files.</div>';
  tree.querySelectorAll('input[data-indeterminate="1"]').forEach((box) => { box.indeterminate = true; });
  if (scroller) scroller.scrollTop = top;
  bkUpdatePickSummary();
}

function bkTreeClick(event) {
  const target = event.target.closest("[data-toggle]");
  if (!target || !BK.contents || BK.contents.filter) return;
  const id = target.dataset.toggle;
  if (BK.contents.open.has(id)) BK.contents.open.delete(id);
  else BK.contents.open.add(id);
  bkRenderContentsTree();
}

function bkContentsChanged(event) {
  const box = event.target;
  if (!BK.contents || !box?.dataset) return;
  if (box.dataset.node) {
    const node = BK.contents.nodes.get(box.dataset.node);
    if (!node) return;
    const category = node.category;
    if (node.isCategory) {
      for (const path of bkNodePaths(node)) BK.pick.files.delete(path);
      if (box.checked) BK.pick.whole.add(category);
      else BK.pick.whole.delete(category);
    } else {
      bkUnwhole(category);
      for (const path of bkNodePaths(node)) {
        if (box.checked) BK.pick.files.add(path);
        else BK.pick.files.delete(path);
      }
      bkNormalizeCategory(category);
    }
  } else if (box.dataset.path) {
    const category = box.dataset.cat;
    bkUnwhole(category);
    if (box.checked) BK.pick.files.add(box.dataset.path);
    else BK.pick.files.delete(box.dataset.path);
    bkNormalizeCategory(category);
  } else {
    return;
  }
  bkRenderContentsTree();
}

function bkUnwhole(category) {
  // Turn a whole part into "every file of it" before narrowing it.
  if (!BK.pick.whole.has(category)) return;
  BK.pick.whole.delete(category);
  for (const f of BK.contents.byCategory.get(category) || []) BK.pick.files.add(f.path);
}

function bkNormalizeCategory(category) {
  const list = BK.contents.byCategory.get(category) || [];
  if (list.length && list.every((f) => BK.pick.files.has(f.path))) {
    list.forEach((f) => BK.pick.files.delete(f.path));
    BK.pick.whole.add(category);
  }
}

function bkPickMatching(select) {
  if (!BK.contents?.filter) return;
  for (const category of BK.contents.byCategory.keys()) {
    const matches = bkVisibleFiles(category);
    if (!matches.length) continue;
    if (BK.pick.whole.has(category)) {
      if (select) continue;
      BK.pick.whole.delete(category);
      for (const f of BK.contents.byCategory.get(category)) BK.pick.files.add(f.path);
    }
    for (const f of matches) {
      if (select) BK.pick.files.add(f.path);
      else BK.pick.files.delete(f.path);
    }
    bkNormalizeCategory(category);
  }
  bkRenderContentsTree();
}

function bkPickAll(select) {
  BK.pick = { whole: new Set(select ? BK.contents.byCategory.keys() : []), files: new Set() };
  bkRenderContentsTree();
}

function bkPickedSelection() {
  const pick = BK.pick || { whole: new Set(), files: new Set() };
  let fileCount = 0;
  for (const category of pick.whole) fileCount += (BK.contents?.byCategory.get(category) || []).length;
  fileCount += pick.files.size;
  // Send a fully-ticked folder as one folder path rather than every file in it.
  const paths = [];
  const emit = (node) => {
    const picked = bkNodePicked(node);
    if (!picked) return;
    if (picked === node.fileCount && !node.isCategory && node.path) { paths.push(node.path); return; }
    for (const f of node.files) if (pick.files.has(f.path)) paths.push(f.path);
    for (const child of node.dirs.values()) emit(child);
  };
  if (BK.contents?.trees) {
    for (const [category, root] of BK.contents.trees.entries()) if (!pick.whole.has(category)) emit(root);
  } else {
    paths.push(...pick.files);
  }
  return { categories: Array.from(pick.whole), paths: paths.length ? paths : null, fileCount };
}

function bkUpdatePickSummary() {
  const selection = bkPickedSelection();
  const summary = document.getElementById("bkPickSummary");
  const button = document.getElementById("bkPickRestore");
  const tooMany = (selection.paths || []).length > BK_MAX_PICKED_FILES;
  if (summary) {
    summary.innerHTML = selection.fileCount
      ? `${selection.fileCount} file(s) selected${tooMany ? ` <span class="text-warning">– pick at most ${BK_MAX_PICKED_FILES} single files, or tick whole parts</span>` : ""}`
      : "Nothing selected";
  }
  if (button) {
    const remote = Boolean(BK.contents?.droneId) && !bkHaveLocally(BK.contents.backupId);
    button.innerHTML = `<i class="bi bi-arrow-repeat me-1"></i>${remote ? "Download &amp; restore selected" : "Restore selected"}`;
    button.disabled = !selection.fileCount || tooMany;
  }
  const filtered = Boolean(BK.contents?.filter);
  for (const id of ["bkPickMatching", "bkUnpickMatching"]) {
    const el = document.getElementById(id);
    if (el) el.disabled = !filtered;
  }
}

function bkHaveLocally(backupId) {
  for (const result of Object.values(BK.assets.results || {})) {
    for (const b of result.backups || []) if (b.backup_id === backupId && (b.is_local || b.available_locally)) return true;
  }
  return window.location.hash === BK_ADMIN_HASH;
}

function bkFilterContents(value) {
  if (!BK.contents) return;
  BK.contents.filter = String(value || "").trim().toLowerCase();
  bkRenderContentsTree();
}

function bkRestorePicked() {
  const selection = bkPickedSelection();
  if (!selection.fileCount) return;
  const preset = { categories: selection.categories, paths: selection.paths, fileCount: selection.fileCount };
  const { droneId, backupId } = BK.contents;
  bkHideModal("bkContentsModal");
  if (droneId && !bkHaveLocally(backupId)) bkDownloadFromDrone(droneId, backupId, true, preset);
  else bkOpenRestore(backupId, false, preset);
}

// ------------------------------------------------------------------- restore

async function bkOpenRestore(backupId, rollback, preset = null) {
  bkModal("bkRestoreModal", `
    <div class="modal-header"><h5 class="modal-title mb-0">Restore</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body"><div class="text-center py-4"><span class="spinner-border"></span></div></div>`);
  let data;
  try {
    data = await api(`/admin/backups/${encodeURIComponent(backupId)}`, BK_SLOW);
  } catch (err) {
    document.querySelector("#bkRestoreModal .modal-body").innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(err.message || "Could not load the backup.")}</div>`;
    return;
  }
  const backup = data.backup || {};
  let categories = backup.categories || [];
  const snapshotCats = rollback ? (backup.snapshot_of?.categories || []) : [];
  for (const id of snapshotCats) {
    if (!categories.some((c) => c.id === id)) categories = categories.concat([{ id, label: id, group: "other", group_label: "Previously empty", file_count: 0, size_bytes: 0 }]);
  }
  const picked = preset && (preset.paths || []).length ? preset : null;
  const assetsState = rollback ? null : bkInitAssetPlanState("wizard", backupId, backup.name || "backup", data.assets);
  BK.wizard = {
    backupId,
    rollback,
    backup,
    assetsOverview: data.assets || null,
    assetsState,
    paths: picked ? picked.paths : null,
    presetCategories: picked ? picked.categories : null,
  };
  const checkedIds = preset ? new Set(preset.categories || []) : null;
  const title = rollback ? `Roll back: ${backup.snapshot_of?.name || backup.name}` : `Restore ${backup.name || "backup"}`;
  const assetsLine = rollback ? "" : bkAssetsSummaryLine(data.assets);
  bkModal("bkRestoreModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi ${rollback ? "bi-arrow-counterclockwise" : "bi-arrow-repeat"} me-2"></i>${escapeHtml(title)}</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body">
      <p class="small text-muted mb-1">${bkSourceLine(backup)}${backup.created_at ? ` · ${escapeHtml(bkDate(backup.created_at))}` : ""}</p>
      ${assetsLine ? `<p class="small text-info mb-2"><i class="bi bi-controller me-1"></i>${escapeHtml(assetsLine)}</p>` : ""}
      ${picked ? `<div class="alert alert-info py-2 small d-flex flex-wrap align-items-center gap-2 mb-3">
          <span><i class="bi bi-check2-square me-1"></i><strong>${Number(picked.fileCount || 0)} selected file(s)</strong>${(picked.categories || []).length ? ` (${picked.categories.length} whole part(s) plus single files)` : ""}. Only these are restored.</span>
          <button class="btn btn-sm btn-outline-light ms-auto" onclick="bkChangePicked()">Change selection</button>
          <button class="btn btn-sm btn-outline-light" onclick="bkClearPicked()">Restore whole parts instead</button></div>` : `
      <div class="d-flex justify-content-between align-items-center mb-1">
        <span class="small fw-semibold">Parts to restore</span>
        <span>${rollback ? "" : `<button class="btn btn-link btn-sm p-0 me-3" onclick="bkChangePicked()">Choose files…</button>`}<button class="btn btn-link btn-sm p-0 me-2" onclick="bkSetAll('bkRestoreList', true); bkResetPreview()">All</button><button class="btn btn-link btn-sm p-0" onclick="bkSetAll('bkRestoreList', false); bkResetPreview()">None</button></span>
      </div>
      <div id="bkRestoreList" class="bk-checklist border rounded p-2 mb-3" style="max-height: 32vh; overflow:auto" onchange="bkResetPreview()">${bkCategoryChecklist(categories, "bkr", checkedIds)}</div>`}
      <div class="mb-2"><span class="small fw-semibold d-block mb-1">How</span>
        <div class="form-check"><input class="form-check-input" type="radio" name="bkMode" id="bkModeOverlay" value="overlay" ${rollback ? "" : "checked"} onchange="bkResetPreview()">
          <label class="form-check-label small" for="bkModeOverlay"><strong>Overlay</strong> – write the backup's files; leave other files in these parts alone.</label></div>
        <div class="form-check"><input class="form-check-input" type="radio" name="bkMode" id="bkModeExact" value="exact" ${rollback ? "checked" : ""} onchange="bkResetPreview()">
          <label class="form-check-label small" for="bkModeExact"><strong>Exact</strong> – ${picked ? "also remove local files under the selected folders/files that the backup does not have" : "make these parts match the backup, also removing files the backup does not have"}.</label></div>
      </div>
      <div class="form-check mb-3"><input class="form-check-input" type="checkbox" id="bkMachineKeys" onchange="bkResetPreview()">
        <label class="form-check-label small" for="bkMachineKeys">Also restore this machine's display, audio, network and hostname settings from <code>batocera.conf</code></label></div>
      ${rollback ? "" : bkRenderWizardAssetsSection(assetsState)}
      <div id="bkPreview"></div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
      <button type="button" class="btn btn-outline-info" id="bkPreviewBtn" onclick="bkPreview()"><i class="bi bi-eye me-1"></i>Preview changes</button>
      <button type="button" class="btn btn-warning" id="bkRestoreBtn" onclick="bkConfirmRestore()" disabled><i class="bi bi-arrow-repeat me-1"></i>Restore</button>
    </div>`, rollback ? "modal-lg" : "modal-xl");
}

function bkChangePicked() {
  const wizard = BK.wizard;
  if (!wizard) return;
  const preset = wizard.paths ? { categories: wizard.presetCategories || [], paths: wizard.paths }
    : { categories: bkCheckedCategories("bkRestoreList"), paths: null };
  bkHideModal("bkRestoreModal");
  bkOpenContents(null, wizard.backupId, preset);
}

function bkClearPicked() {
  if (!BK.wizard) return;
  bkOpenRestore(BK.wizard.backupId, BK.wizard.rollback);
}

function bkRestoreOptions() {
  if (BK.wizard?.paths) {
    return {
      categories: BK.wizard.presetCategories || [],
      paths: BK.wizard.paths,
      mode: document.getElementById("bkModeExact")?.checked ? "exact" : "overlay",
      include_machine_keys: Boolean(document.getElementById("bkMachineKeys")?.checked),
    };
  }
  return {
    categories: bkCheckedCategories("bkRestoreList"),
    mode: document.getElementById("bkModeExact")?.checked ? "exact" : "overlay",
    include_machine_keys: Boolean(document.getElementById("bkMachineKeys")?.checked),
  };
}

function bkResetPreview() {
  const preview = document.getElementById("bkPreview");
  if (preview) preview.innerHTML = "";
  const button = document.getElementById("bkRestoreBtn");
  if (button) button.disabled = true;
}

function bkConfChanges(conf) {
  if (!conf) return "";
  const rows = [];
  for (const row of conf.changed || []) rows.push(`<tr><td><code>${escapeHtml(row.key)}</code></td><td class="text-muted">${escapeHtml(row.old)}</td><td>${escapeHtml(row.new)}</td></tr>`);
  for (const row of conf.added || []) rows.push(`<tr><td><code>${escapeHtml(row.key)}</code></td><td class="text-muted">(not set)</td><td>${escapeHtml(row.value)}</td></tr>`);
  for (const row of conf.removed || []) rows.push(`<tr><td><code>${escapeHtml(row.key)}</code></td><td class="text-muted">${escapeHtml(row.old)}</td><td class="text-danger">(removed)</td></tr>`);
  if (!rows.length) return '<div class="small text-muted mb-2">batocera.conf: no setting changes.</div>';
  return `<details class="mb-2" open><summary class="small fw-semibold">batocera.conf: ${rows.length} setting change(s)</summary>
    <div style="max-height: 25vh; overflow:auto"><table class="table table-sm themed-table small mb-0"><thead><tr><th>Setting</th><th>Now</th><th>After restore</th></tr></thead><tbody>${rows.join("")}</tbody></table></div></details>`;
}

async function bkPreview() {
  const options = bkRestoreOptions();
  const preview = document.getElementById("bkPreview");
  if (!options.categories.length && !(options.paths || []).length) { preview.innerHTML = '<div class="alert alert-warning py-2 small">Choose at least one part.</div>'; return; }
  preview.innerHTML = '<div class="text-center py-3"><span class="spinner-border spinner-border-sm me-2"></span><span class="small">Comparing with this Drone…</span></div>';
  let plan;
  try {
    plan = await apiPost(`/admin/backups/${encodeURIComponent(BK.wizard.backupId)}/plan`, options);
  } catch (err) {
    preview.innerHTML = `<div class="alert alert-danger py-2 small">${escapeHtml(err.message || "Preview failed.")}</div>`;
    return;
  }
  BK.wizard.plan = plan;
  const totals = plan.totals || {};
  const rows = (plan.categories || []).map((c) => `<tr><td>${escapeHtml(c.label)}</td><td class="text-end">${c.new}</td><td class="text-end">${c.changed}</td><td class="text-end text-muted">${c.identical}</td><td class="text-end ${c.remove ? "text-danger" : ""}">${plan.mode === "exact" ? c.remove : "–"}</td></tr>`).join("");
  const warnings = plan.warnings || [];
  preview.innerHTML = `
    <div class="small fw-semibold mb-1">What changes on this Drone</div>
    <div style="max-height: 25vh; overflow:auto"><table class="table table-sm themed-table small mb-2"><thead><tr><th>Part</th><th class="text-end">New</th><th class="text-end">Changed</th><th class="text-end">Same</th><th class="text-end">Removed</th></tr></thead><tbody>${rows}</tbody></table></div>
    ${totals.blocked ? `<div class="alert alert-warning py-2 small">${Number(totals.blocked)} file(s) cannot be replaced because a link or folder is in the way; they will be skipped.</div>` : ""}
    ${bkConfChanges(plan.batocera_conf)}
    ${plan.removal_count ? `<details class="mb-2"><summary class="small text-danger">${Number(plan.removal_count)} file(s) will be removed</summary><ul class="small mb-0">${(plan.removals || []).map((p) => `<li><code>${escapeHtml(p)}</code></li>`).join("")}${plan.removal_count > (plan.removals || []).length ? "<li>…</li>" : ""}</ul></details>` : ""}
    ${warnings.map((w) => `<div class="alert alert-warning py-2 small mb-2"><i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(w)}</div>`).join("")}
    ${warnings.length ? `<div class="form-check mb-2"><input class="form-check-input" type="checkbox" id="bkAckWarnings" onchange="bkUpdateRestoreButton()"><label class="form-check-label small" for="bkAckWarnings">I understand the warnings above and want to restore anyway.</label></div>` : ""}
    <div class="form-check"><input class="form-check-input" type="checkbox" id="bkAckRestore" onchange="bkUpdateRestoreButton()">
      <label class="form-check-label small" for="bkAckRestore">A safety snapshot of these parts is taken first. Any running game is closed and EmulationStation restarts.</label></div>`;
  const assetsState = BK.wizard.assetsState;
  if (assetsState && assetsState.enabled && !assetsState.plan && !assetsState.loading) {
    await bkPlanAssets("wizard");
  }
  bkUpdateRestoreButton();
}

function bkUpdateRestoreButton() {
  const button = document.getElementById("bkRestoreBtn");
  if (!button) return;
  const needsWarnings = (BK.wizard?.plan?.warnings || []).length > 0;
  let ok = Boolean(document.getElementById("bkAckRestore")?.checked) && (!needsWarnings || Boolean(document.getElementById("bkAckWarnings")?.checked));
  const assetsState = BK.wizard?.assetsState;
  if (ok && assetsState && assetsState.enabled) {
    if (assetsState.loading) ok = false;
    else if (assetsState.plan) {
      const stats = bkComputeAssetSelectionStats(assetsState);
      if (stats.selectedCount > 0 && !stats.fits) ok = false;
    }
  }
  button.disabled = !ok;
}

async function bkConfirmRestore() {
  const options = bkRestoreOptions();
  options.acknowledge_warnings = Boolean(document.getElementById("bkAckWarnings")?.checked);
  const assetsState = BK.wizard?.assetsState;
  if (assetsState && assetsState.enabled) {
    if (!assetsState.plan) {
      await bkPlanAssets("wizard");
      if (!assetsState.plan) return;
    }
    const stats = bkComputeAssetSelectionStats(assetsState);
    if (stats.selectedCount > 0 && !stats.fits) {
      showToast("Free disk space is too low for the selected downloads. Untick some items first.", "warning");
      bkUpdateRestoreButton();
      return;
    }
    const selection = bkBuildAssetSelection(assetsState);
    if (selection && selection._selectedCount > 0) {
      options.assets = {
        plan_id: selection.plan_id,
        selected: selection.selected,
        deselected: selection.deselected,
        deselected_groups: selection.deselected_groups,
      };
    }
  }
  const button = document.getElementById("bkRestoreBtn");
  if (button) button.disabled = true;
  try {
    await apiPost(`/admin/backups/${encodeURIComponent(BK.wizard.backupId)}/restore`, options);
  } catch (err) {
    showToast(escapeHtml(err.message || "Restore could not start."), "danger");
    if (button) button.disabled = false;
    return;
  }
  bkHideModal("bkRestoreModal");
  showToast("Restore started. EmulationStation will restart when the files are in place.", "info");
  if (window.location.hash === BK_ADMIN_HASH) await renderBackupsAdminPage();
  else bkWatchRestore();
}

function bkWatchRestore() {
  if (BK.restorePollTimer) clearInterval(BK.restorePollTimer);
  BK.restorePollTimer = setInterval(async () => {
    try {
      const status = await api("/admin/backups/restore");
      const banner = document.getElementById("bkAssetsRestoreBanner");
      if (banner) banner.innerHTML = bkRestoreBanner(status);
      if (status.state !== "running") {
        clearInterval(BK.restorePollTimer);
        BK.restorePollTimer = null;
        showToast(status.state === "complete" ? "Restore finished." : `Restore failed: ${escapeHtml(status.error || "unknown error")}`, status.state === "complete" ? "success" : "danger");
      }
    } catch (_) { /* the Drone may be busy restarting EmulationStation */ }
  }, 2500);
}

// ------------------------------------------------------- assets: swarm view

async function renderBackupsAssetsPage() {
  currentSystemContext = null;
  clearSystemTheme();
  titleNode.textContent = "Backups";
  subtitleNode.textContent = "Find, inspect, download and restore backups from every Drone in your swarm";
  setLoading(true, "Finding Drones...");
  let sources = [];
  let restore = {};
  try {
    [sources, restore] = await Promise.all([
      api("/admin/backups/sources").then((d) => d.sources || []),
      api("/admin/backups/restore").catch(() => ({})),
    ]);
  } catch (err) {
    content.innerHTML = `<div class="alert alert-danger">Could not load Drones: ${escapeHtml(err.message || "unknown error")}</div>`;
    return;
  } finally {
    setLoading(false);
  }
  const state = BK.assets;
  state.sources = sources;
  if (!state.selected) state.selected = new Set(sources.filter((s) => s.online).map((s) => s.drone_id));
  for (const id of Array.from(state.selected)) if (!sources.some((s) => s.drone_id === id && s.online)) state.selected.delete(id);
  content.innerHTML = `
    <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
      <input type="search" class="form-control flex-grow-1" style="max-width: 32rem" id="bkSearch" placeholder="Search backups by name, Drone, Batocera version or contents" value="${escapeHtml(state.query)}" oninput="bkSearchChanged(this.value)">
      <select class="form-select" style="max-width: 14rem" id="bkGroupFilter" onchange="bkGroupChanged(this.value)">
        <option value="">Any contents</option>
        ${["batocera", "emulationstation", "controllers", "retroarch", "emulators", "scripts", "gamelists", "cheats", "saves", "states"].map((g) => `<option value="${g}" ${state.group === g ? "selected" : ""}>${escapeHtml({ batocera: "Batocera", emulationstation: "EmulationStation", controllers: "Controllers", retroarch: "RetroArch", emulators: "Standalone emulators", scripts: "Custom scripts", gamelists: "Game lists", cheats: "Cheats", saves: "Saves", states: "Save states" }[g])}</option>`).join("")}
      </select>
      <button class="btn btn-outline-light" onclick="bkLoadAssets(true)" title="Refresh"><i class="bi bi-arrow-clockwise"></i></button>
      <button class="btn btn-outline-light" onclick="setHash('${BK_ADMIN_HASH}')"><i class="bi bi-gear me-1"></i>Manage this Drone's backups</button>
      ${typeof renderAssetTypeSwitcher === "function" ? renderAssetTypeSwitcher("backups") : ""}
    </div>
    <div id="bkAssetsRestoreBanner">${bkRestoreBanner(restore)}</div>
    <div class="mb-3 d-flex flex-wrap gap-2" id="bkDroneChips">${bkDroneChips()}</div>
    <div id="bkAssetsList"></div>`;
  if (restore.state === "running") bkWatchRestore();
  await bkLoadAssets();
}

function bkDroneChips() {
  const state = BK.assets;
  return state.sources.map((source) => {
    const result = state.results[source.drone_id];
    const checked = state.selected.has(source.drone_id);
    const status = !source.online ? '<span class="text-muted">offline</span>'
      : !checked ? "" : !result ? '<span class="spinner-border spinner-border-sm"></span>'
        : result.status === "ok" ? `<span class="badge text-bg-dark">${result.backups.length}</span>`
          : `<i class="bi bi-exclamation-circle text-warning" title="${escapeHtml(result.error || "")}"></i>`;
    return `<label class="btn btn-sm ${checked ? "btn-outline-info active" : "btn-outline-secondary"} d-flex align-items-center gap-2 ${source.online ? "" : "disabled"}">
      <input type="checkbox" class="form-check-input m-0" ${checked ? "checked" : ""} ${source.online ? "" : "disabled"} onchange="bkToggleDrone(${jsAttr(source.drone_id)}, this.checked)">
      <i class="bi ${source.is_local ? "bi-hdd" : "bi-hdd-network"}"></i>${escapeHtml(source.is_local ? `${source.name} (this Drone)` : source.name)} ${status}</label>`;
  }).join("");
}

function bkToggleDrone(droneId, checked) {
  if (checked) BK.assets.selected.add(droneId);
  else BK.assets.selected.delete(droneId);
  bkLoadAssets();
}

let bkSearchTimer = null;
function bkSearchChanged(value) {
  BK.assets.query = value;
  if (bkSearchTimer) clearTimeout(bkSearchTimer);
  bkSearchTimer = setTimeout(() => bkRenderAssets(), 150);
}

function bkGroupChanged(value) {
  BK.assets.group = value;
  bkRenderAssets();
}

async function bkLoadAssets(force = false) {
  const state = BK.assets;
  const token = ++state.token;
  if (force) state.results = {};
  const targets = state.sources.filter((s) => s.online && state.selected.has(s.drone_id));
  for (const source of targets) if (force || !state.results[source.drone_id]) delete state.results[source.drone_id];
  bkRenderAssets();
  await Promise.all(targets.map(async (source) => {
    if (state.results[source.drone_id]) return;
    try {
      const data = await api(`/admin/backups/swarm?drones=${encodeURIComponent(source.drone_id)}`, BK_SLOW);
      const entry = (data.sources || [])[0] || { status: "error", error: "No answer", backups: [] };
      if (token === state.token) state.results[source.drone_id] = entry;
    } catch (err) {
      if (token === state.token) state.results[source.drone_id] = { status: "error", error: err.message, backups: [] };
    }
    if (token === state.token) bkRenderAssets();
  }));
}

function bkMatches(backup) {
  const state = BK.assets;
  if (state.group && !(backup.categories || []).some((c) => c.group === state.group)) return false;
  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  const hay = [backup.name, backup.description, backup.drone_name, backup.source?.drone_name, backup.source?.batocera_version, backup.source?.architecture]
    .concat((backup.categories || []).map((c) => c.label)).join(" ").toLowerCase();
  return query.split(/\s+/).every((word) => hay.includes(word));
}

function bkRenderAssets() {
  const chips = document.getElementById("bkDroneChips");
  if (chips) chips.innerHTML = bkDroneChips();
  const list = document.getElementById("bkAssetsList");
  if (!list) return;
  const state = BK.assets;
  const sections = [];
  for (const source of state.sources) {
    if (!state.selected.has(source.drone_id) || !source.online) continue;
    const result = state.results[source.drone_id];
    let body;
    if (!result) body = '<div class="text-muted small py-2"><span class="spinner-border spinner-border-sm me-2"></span>Asking this Drone…</div>';
    else if (result.status !== "ok") body = `<div class="alert alert-warning py-2 small mb-0">${escapeHtml(result.error || "Could not list this Drone's backups.")} <button class="btn btn-link btn-sm p-0 align-baseline" onclick="bkRetryDrone(${jsAttr(source.drone_id)})">Retry</button></div>`;
    else {
      const items = result.backups.filter(bkMatches);
      body = items.length ? `<div class="row g-3">${items.map((b) => bkAssetCard(b)).join("")}</div>`
        : `<div class="text-muted small py-2">${result.backups.length ? "No backups match the filters." : "No backups on this Drone."}</div>`;
    }
    sections.push(`<section class="mb-4"><h6 class="text-uppercase small text-muted mb-2"><i class="bi ${source.is_local ? "bi-hdd" : "bi-hdd-network"} me-1"></i>${escapeHtml(source.is_local ? `${source.name} (this Drone)` : source.name)}</h6>${body}</section>`);
  }
  list.innerHTML = sections.join("") || '<div class="text-muted">Select at least one online Drone above.</div>';
}

function bkRetryDrone(droneId) {
  delete BK.assets.results[droneId];
  bkLoadAssets();
}

function bkAssetCard(backup) {
  const local = backup.is_local;
  const id = backup.backup_id;
  const drone = backup.drone_id;
  const onThisDrone = local || backup.available_locally;
  const actions = [
    `<button class="btn btn-sm btn-outline-secondary" onclick="bkOpenContents(${jsAttr(drone)}, ${jsAttr(id)})"><i class="bi bi-folder2-open me-1"></i>Contents</button>`,
    onThisDrone
      ? `<button class="btn btn-sm btn-warning" onclick="bkOpenRestore(${jsAttr(id)}, false)"><i class="bi bi-arrow-repeat me-1"></i>Restore here</button>
         <button class="btn btn-sm btn-outline-info" onclick="bkOpenMissingGames(${jsAttr(id)})"><i class="bi bi-controller me-1"></i>Get missing games</button>`
      : `<button class="btn btn-sm btn-warning" onclick="bkDownloadFromDrone(${jsAttr(drone)}, ${jsAttr(id)}, true)"><i class="bi bi-arrow-repeat me-1"></i>Download &amp; restore</button>`,
    onThisDrone
      ? (local ? `<a class="btn btn-sm btn-outline-primary" href="${escapeHtml(_apiRequestUrl(`/admin/backups/${id}/download`))}" title="Save the archive file"><i class="bi bi-download"></i></a>` : "")
      : `<button class="btn btn-sm btn-outline-primary" onclick="bkDownloadFromDrone(${jsAttr(drone)}, ${jsAttr(id)}, false)"><i class="bi bi-cloud-download me-1"></i>Copy to this Drone</button>`,
  ].join("");
  return `<div class="col-12 col-lg-6 col-xxl-4"><div class="card h-100 log-card" id="bk-card-${escapeHtml(drone)}-${escapeHtml(id)}">
    <div class="card-body d-flex flex-column">
      <div class="d-flex justify-content-between align-items-start gap-2">
        <h6 class="mb-1">${escapeHtml(backup.name || "Backup")}</h6>
        <span class="small text-muted text-nowrap">${formatBytes(backup.size_bytes || 0)}</span>
      </div>
      <div class="small text-muted mb-1">${escapeHtml(bkDate(backup.created_at))}${bkSourceLine(backup) ? ` · ${bkSourceLine(backup)}` : ""}</div>
      <div class="mb-1">${!local && backup.available_locally ? '<span class="badge text-bg-success me-1"><i class="bi bi-check2 me-1"></i>Also on this Drone</span>' : ""}${bkCompatBadge(backup)}</div>
      ${backup.description ? `<p class="small mb-2">${escapeHtml(backup.description)}</p>` : ""}
      <div class="mb-2">${bkCategoryChips(backup.categories, 8)}</div>
      <div class="bk-progress small text-muted mb-2"></div>
      <div class="mt-auto d-flex flex-wrap gap-2">${actions}</div>
    </div></div></div>`;
}

async function bkDownloadFromDrone(droneId, backupId, thenRestore, preset = null) {
  const card = document.getElementById(`bk-card-${droneId}-${backupId}`);
  const progress = card?.querySelector(".bk-progress");
  const setProgress = (html) => { if (progress) progress.innerHTML = html; };
  setProgress('<span class="spinner-border spinner-border-sm me-1"></span>Queuing download…');
  let result;
  try {
    result = await apiPost(`/admin/backups/swarm/${encodeURIComponent(droneId)}/${encodeURIComponent(backupId)}/download`, {});
  } catch (err) {
    setProgress(`<span class="text-danger">${escapeHtml(err.message || "Download failed.")}</span>`);
    return;
  }
  if (result.status !== "queued") {
    setProgress('<span class="text-success">Already on this Drone.</span>');
    if (thenRestore) bkOpenRestore(backupId, false, preset);
    return;
  }
  const jobId = result.job?.id;
  const started = Date.now();
  while (Date.now() - started < 6 * 60 * 60 * 1000) {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    let snapshot;
    try { snapshot = await api("/admin/downloads?compact=1&queued_limit=200"); } catch (_) { continue; }
    const jobs = [].concat(snapshot.active || [], snapshot.queued || [], snapshot.recent || []);
    const job = jobs.find((j) => j.id === jobId || j.job_id === jobId);
    if (!job) continue;
    if (job.status === "completed") {
      setProgress('<span class="text-success"><i class="bi bi-check2 me-1"></i>Copied to this Drone and verified.</span>');
      delete BK.assets.results[droneId];
      delete BK.assets.results[(BK.assets.sources.find((s) => s.is_local) || {}).drone_id];
      if (thenRestore) bkOpenRestore(backupId, false, preset);
      else bkLoadAssets();
      return;
    }
    if (["failed", "error", "cancelled"].includes(job.status)) {
      setProgress(`<span class="text-danger">${escapeHtml(job.error_message || job.failure_reason || "Download failed.")}</span>`);
      return;
    }
    const pct = Number(job.percentage || 0);
    setProgress(job.status === "queued" ? "Waiting in the transfer queue…"
      : `<div class="progress" style="height: 6px"><div class="progress-bar" style="width:${pct}%"></div></div><span>${pct}% · ${formatBytes(job.downloaded_bytes || 0)} of ${formatBytes(job.total_bytes || 0)}</span>`);
  }
}

// -------------------------------------------------- asset restore & missing games

function bkDefaultAssetKinds(overview) {
  const kinds = new Set();
  if (overview?.recorded) {
    const counts = overview.counts || {};
    if (Number(counts.roms || 0) > 0) kinds.add("roms");
    if (Number(counts.movies || 0) > 0) kinds.add("movies");
    if (Number(counts.music || 0) > 0) kinds.add("music");
    if (!kinds.size) kinds.add("roms");
  } else {
    kinds.add("roms");
    kinds.add("movies");
    kinds.add("music");
  }
  // BIOS is always opt-in at restore time (MD5-verified).
  return kinds;
}

function bkInitAssetPlanState(scope, backupId, backupName, overview) {
  return {
    scope,
    backupId,
    backupName: backupName || "backup",
    overview: overview || null,
    enabled: scope === "missing",
    kinds: bkDefaultAssetKinds(overview),
    loading: false,
    error: "",
    plan: null,
    selected: new Set(),
    deselected: new Set(),
    deselectedGroups: new Set(),
  };
}

function bkGetAssetPlanState(scope) {
  return scope === "wizard" ? BK.wizard?.assetsState : BK.missing;
}

function bkAssetKindCheckboxesHtml(state) {
  const scope = state.scope;
  const counts = state.overview?.recorded ? (state.overview.counts || {}) : null;
  return ["roms", "movies", "music", "bios"].map((kind) => {
    const checked = state.kinds.has(kind);
    const countNote = counts ? ` <span class="text-muted">(${Number(counts[kind] || 0)})</span>` : "";
    const id = `bkAssetKind-${scope}-${kind}`;
    return `<div class="form-check form-check-inline">
      <input class="form-check-input" type="checkbox" id="${id}" ${checked ? "checked" : ""} onchange="bkToggleAssetKind(${jsAttr(scope)}, ${jsAttr(kind)}, this.checked)">
      <label class="form-check-label small" for="${id}">${escapeHtml(BK_ASSET_KIND_LABELS[kind] || kind)}${countNote}</label>
    </div>`;
  }).join("");
}

function bkRenderWizardAssetsSection(state) {
  if (!state) return "";
  const summary = bkAssetsSummaryLine(state.overview);
  return `<div class="card log-card mb-3">
    <div class="card-header d-flex flex-wrap justify-content-between align-items-center gap-2 py-2">
      <div class="form-check mb-0">
        <input class="form-check-input" type="checkbox" id="bkFetchAssetsToggle" ${state.enabled ? "checked" : ""} onchange="bkToggleWizardFetchAssets(this.checked)">
        <label class="form-check-label fw-semibold small" for="bkFetchAssetsToggle"><i class="bi bi-controller me-1"></i>Also fetch games &amp; media from the swarm</label>
      </div>
      ${summary ? `<span class="small text-muted">${escapeHtml(summary)}</span>` : ""}
    </div>
    <div class="card-body py-2" id="bkWizardAssetsBody" ${state.enabled ? "" : "hidden"}>
      <p class="small text-muted mb-2">Settings and game lists are restored first; once EmulationStation restarts, missing games, artwork and media are queued from your online paired Drones (spread across peers, never overwriting existing files or restored game list descriptions).</p>
      <div class="d-flex flex-wrap align-items-center gap-2 mb-2">
        <div id="bkAssetKinds-wizard">${bkAssetKindCheckboxesHtml(state)}</div>
        <button type="button" class="btn btn-sm btn-outline-info ms-auto" onclick="bkPlanAssets('wizard')"><i class="bi bi-search me-1"></i>Search swarm</button>
      </div>
      <div id="bkAssetPlan-wizard">${bkRenderAssetPlanHtml(state)}</div>
    </div>
  </div>`;
}

function bkToggleWizardFetchAssets(checked) {
  const state = bkGetAssetPlanState("wizard");
  if (!state) return;
  state.enabled = Boolean(checked);
  const body = document.getElementById("bkWizardAssetsBody");
  if (body) body.hidden = !state.enabled;
  if (state.enabled && !state.plan && !state.loading) {
    bkPlanAssets("wizard");
  } else {
    bkUpdateRestoreButton();
  }
}

function bkToggleAssetKind(scope, kind, checked) {
  const state = bkGetAssetPlanState(scope);
  if (!state) return;
  if (checked) state.kinds.add(kind);
  else state.kinds.delete(kind);
  state.plan = null;
  bkRefreshAssetPlanContainer(scope);
  if (state.kinds.size && (scope === "missing" || state.enabled)) {
    bkPlanAssets(scope);
  }
}

async function bkPlanAssets(scope) {
  const state = bkGetAssetPlanState(scope);
  if (!state) return;
  const kinds = Array.from(state.kinds);
  if (!kinds.length) {
    state.error = "Choose at least one asset type to search for.";
    state.plan = null;
    bkRefreshAssetPlanContainer(scope);
    return;
  }
  state.loading = true;
  state.error = "";
  bkRefreshAssetPlanContainer(scope);
  try {
    const plan = await apiPost(`/admin/backups/${encodeURIComponent(state.backupId)}/assets/plan`, {
      kinds,
      include_bios: state.kinds.has("bios"),
    }, BK_SLOW);
    state.plan = plan;
    state.selected = new Set();
    state.deselected = new Set();
    state.deselectedGroups = new Set();
  } catch (err) {
    state.plan = null;
    state.error = err.message || "Could not search the swarm for missing games and media.";
  } finally {
    state.loading = false;
    bkRefreshAssetPlanContainer(scope);
  }
}

function bkComputeAssetSelectionStats(state) {
  const plan = state?.plan;
  if (!plan) return { selectedCount: 0, selectedBytes: 0, probableSelected: 0, pools: [], fits: true };
  let selectedCount = 0;
  let selectedBytes = 0;
  let probableSelected = 0;
  const byKindBytes = { roms: 0, bios: 0, movies: 0, music: 0 };
  for (const group of plan.groups || []) {
    if (state.deselectedGroups.has(group.key)) continue;
    let groupCount = group.default_count !== undefined ? Number(group.default_count) : Number(group.count || 0);
    let groupBytes = Number(group.bytes || 0);
    for (const item of group.items || []) {
      const itemBytes = (item.action === "download" ? Number(item.size || 0) : 0) + Number(item.artwork_bytes || 0);
      if (item.default_selected === false) {
        if (state.selected.has(item.key)) {
          groupCount += 1;
          groupBytes += itemBytes;
        }
      } else if (state.deselected.has(item.key)) {
        groupCount = Math.max(0, groupCount - 1);
        groupBytes = Math.max(0, groupBytes - itemBytes);
      }
    }
    selectedCount += groupCount;
    selectedBytes += groupBytes;
    byKindBytes[group.kind] = (byKindBytes[group.kind] || 0) + groupBytes;
  }
  for (const item of plan.probable || []) {
    if (!state.selected.has(item.key)) continue;
    selectedCount += 1;
    probableSelected += 1;
    const itemBytes = (item.action === "download" ? Number(item.size || 0) : 0) + Number(item.artwork_bytes || 0);
    selectedBytes += itemBytes;
    byKindBytes[item.kind] = (byKindBytes[item.kind] || 0) + itemBytes;
  }
  const poolMap = new Map();
  const space = plan.space || {};
  for (const kind of Object.keys(space)) {
    const info = space[kind];
    if (!info) continue;
    const key = info.group || kind;
    if (!poolMap.has(key)) {
      poolMap.set(key, {
        group: key,
        mount: info.mount || info.path || kind,
        path: info.path || kind,
        free: Number(info.free || 0),
        selected: 0,
        queued: Number(info.queued || 0),
        staging: Number(info.staging || 0),
        reserve: Number(info.reserve || 0),
        kinds: [],
      });
    }
    const pool = poolMap.get(key);
    pool.selected += Number(byKindBytes[kind] || 0);
    pool.kinds.push(kind);
  }
  const pools = Array.from(poolMap.values()).map((pool) => {
    const required = pool.selected + pool.queued + pool.staging + pool.reserve;
    const fits = pool.selected === 0 ? true : pool.free >= required;
    return Object.assign({}, pool, { required, needed: required, fits, short: !fits });
  });
  return { selectedCount, selectedBytes, probableSelected, pools, fits: pools.every((p) => p.fits) };
}

function bkBuildAssetSelection(state) {
  if (!state?.plan) return null;
  const stats = bkComputeAssetSelectionStats(state);
  return {
    plan_id: state.plan.plan_id,
    selected: Array.from(state.selected),
    deselected: Array.from(state.deselected),
    deselected_groups: Array.from(state.deselectedGroups),
    _selectedCount: stats.selectedCount,
  };
}

function bkSourceStatusChip(source) {
  const status = source.status || "ok";
  const badgeClass = status === "ok" ? "text-bg-success" : status === "timeout" ? "text-bg-warning" : status === "unsupported" ? "text-bg-secondary" : "text-bg-danger";
  const detail = status === "ok" ? `${Number(source.matched || 0)} matched` : status;
  return `<span class="badge ${badgeClass} me-1 mb-1" title="${escapeHtml(source.error || "")}"><i class="bi bi-hdd-network me-1"></i>${escapeHtml(source.name || source.drone_id)}: ${escapeHtml(detail)}</span>`;
}

function bkRenderAssetSpaceHtml(stats, scope) {
  const activePools = (stats.pools || []).filter((p) => p.selected > 0 || !p.fits);
  if (!activePools.length) return "";
  return activePools.map((pool) => {
    const free = Math.max(0, pool.free);
    const required = Math.max(0, pool.required);
    const pct = free > 0 ? Math.min(100, Math.round((required / free) * 100)) : (required > 0 ? 100 : 0);
    const parts = [`${formatBytes(pool.selected)} selected`];
    if (pool.queued) parts.push(`${formatBytes(pool.queued)} queued`);
    if (pool.reserve) parts.push(`${formatBytes(pool.reserve)} reserve`);
    return `<div class="mb-2">
      <div class="d-flex justify-content-between small mb-1">
        <span>Disk <code>${escapeHtml(pool.mount || pool.path)}</code> (${escapeHtml(pool.kinds.join(", "))})</span>
        <span class="${!pool.fits ? "text-danger fw-semibold" : "text-muted"}">${parts.join(" + ")} = ${formatBytes(required)} / ${formatBytes(free)} free</span>
      </div>
      <div class="progress" style="height: 6px"><div class="progress-bar ${!pool.fits ? "bg-danger" : "bg-info"}" style="width: ${pct}%"></div></div>
      ${!pool.fits ? `<div class="alert alert-danger py-1 px-2 small mt-1 mb-0 d-flex flex-wrap align-items-center gap-2"><span><i class="bi bi-exclamation-octagon me-1"></i>Not enough free space (${formatBytes(required)} required, ${formatBytes(free)} free). Untick items until it fits.</span><button type="button" class="btn btn-sm btn-outline-light ms-auto" onclick="bkUntickUntilFits(${jsAttr(scope)})">Untick until it fits</button></div>` : ""}
    </div>`;
  }).join("");
}

function bkRenderAssetPlanSummaryHtml(state) {
  const plan = state.plan;
  if (!plan) return "";
  const totals = plan.totals || {};
  const stats = bkComputeAssetSelectionStats(state);
  const sources = (plan.sources || []).map(bkSourceStatusChip).join("") || '<span class="text-muted small">No online paired Drones responded.</span>';
  return `<div class="d-flex flex-wrap align-items-center gap-2 mb-2">
      <span class="badge text-bg-secondary">Already here: ${Number(totals.here || 0)}</span>
      <span class="badge text-bg-success">Available: ${Number(totals.available || 0)}</span>
      <span class="badge text-bg-warning">Probable (unticked): ${Number(totals.probable || 0)}</span>
      <span class="badge text-bg-dark">Unavailable: ${Number(totals.unavailable || 0)}</span>
      ${plan.queued_already ? `<span class="badge text-bg-info">${Number(plan.queued_already)} already queued</span>` : ""}
      <span class="small fw-semibold ms-auto">Selected to fetch: ${stats.selectedCount} item(s) · ${formatBytes(stats.selectedBytes)}</span>
    </div>
    ${plan.legacy ? '<div class="alert alert-info py-1 px-2 small mb-2"><i class="bi bi-info-circle me-1"></i>Game list only (older backup): matches are based on system and relative path, so they appear under Probable matches below and need a tick.</div>' : ""}
    ${plan.truncated ? '<div class="alert alert-warning py-1 px-2 small mb-2">This backup reached the 200,000-item cap for at least one category.</div>' : ""}
    <div class="mb-2"><span class="small text-muted me-1">Swarm sources:</span>${sources}</div>
    ${bkRenderAssetSpaceHtml(stats, state.scope)}`;
}

function bkRenderAssetPlanHtml(state) {
  if (!state) return "";
  if (state.loading) {
    return '<div class="text-center py-3"><span class="spinner-border spinner-border-sm me-2"></span><span class="small">Searching online paired Drones for missing games &amp; media…</span></div>';
  }
  if (state.error) {
    return `<div class="alert alert-warning py-2 small mb-0"><i class="bi bi-exclamation-triangle me-1"></i>${escapeHtml(state.error)}</div>`;
  }
  const plan = state.plan;
  if (!plan) {
    return '<div class="small text-muted">Click <strong>Search swarm</strong> to check which missing games and media your online paired Drones have.</div>';
  }
  const scope = state.scope;
  const accId = `bkAssetAcc-${scope}`;
  const accordionItems = [];

  (plan.groups || []).forEach((group, idx) => {
    const groupDefault = group.default_count !== undefined ? Number(group.default_count) : Number(group.count || 0);
    const groupChecked = !state.deselectedGroups.has(group.key) && (groupDefault > 0 || (group.items || []).some((i) => state.selected.has(i.key)));
    const collapseId = `${accId}-g-${idx}`;
    const kindLabel = BK_ASSET_KIND_LABELS[group.kind] || group.kind;
    const title = group.system ? `${kindLabel} · ${group.system}` : kindLabel;
    const rows = (group.items || []).map((item) => {
      const checked = item.default_selected === false ? state.selected.has(item.key) : (!state.deselectedGroups.has(group.key) && !state.deselected.has(item.key));
      const artBadge = item.action === "artwork"
        ? `<span class="badge text-bg-info ms-1">artwork only (${escapeHtml((item.artwork || []).join(", "))})</span>`
        : (item.artwork || []).length ? `<span class="badge text-bg-secondary ms-1">+ artwork</span>` : "";
      const unknownBadge = item.size_unknown ? `<span class="badge text-bg-secondary ms-1">size unknown</span>` : "";
      const peers = (item.peers || []).map((p) => `<span class="badge text-bg-dark me-1">${escapeHtml(p.name || p.drone_id)}</span>`).join("");
      const sizeCell = item.action === "artwork" ? "media only" : (item.size_unknown ? escapeHtml(item.size_display || "size unknown") : formatBytes(item.size || 0));
      return `<tr>
        <td style="width: 2rem"><input class="form-check-input bk-asset-exact" type="checkbox" data-group="${escapeHtml(group.key)}" data-key="${escapeHtml(item.key)}" ${checked ? "checked" : ""} onchange="bkToggleExactAssetItem(${jsAttr(scope)}, ${jsAttr(group.key)}, ${jsAttr(item.key)}, this.checked)"></td>
        <td class="small"><div><strong>${escapeHtml(item.name || item.path)}</strong>${artBadge}${unknownBadge}</div><div class="text-muted"><code>${escapeHtml(item.path)}</code></div></td>
        <td class="small">${peers}</td>
        <td class="small text-end text-nowrap">${sizeCell}</td>
      </tr>`;
    }).join("");
    accordionItems.push(`<div class="accordion-item">
      <h2 class="accordion-header d-flex align-items-center">
        <div class="form-check ms-3 me-1 my-0">
          <input class="form-check-input bk-asset-group" type="checkbox" data-group="${escapeHtml(group.key)}" ${groupChecked ? "checked" : ""} aria-label="Select ${escapeHtml(title)}" onchange="bkToggleAssetGroup(${jsAttr(scope)}, ${jsAttr(group.key)}, this.checked)">
        </div>
        <button class="accordion-button collapsed py-2 small" type="button" data-bs-toggle="collapse" data-bs-target="#${collapseId}">
          <span class="fw-semibold me-2">${escapeHtml(title)}</span>
          <span class="badge text-bg-success me-2">${Number(group.count || 0)} available</span>
          <span class="text-muted small">${formatBytes(group.bytes || 0)}</span>
        </button>
      </h2>
      <div id="${collapseId}" class="accordion-collapse collapse" data-bs-parent="#${accId}">
        <div class="accordion-body p-2">
          <div class="table-responsive" style="max-height: 28vh; overflow:auto">
            <table class="table table-sm align-middle themed-table mb-0">
              <thead><tr><th></th><th>Item</th><th>Available on</th><th class="text-end">Size</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          ${group.omitted ? `<div class="small text-muted mt-1">…and ${Number(group.omitted)} more item(s) in this group (use the group checkbox to include or exclude them all).</div>` : ""}
        </div>
      </div>
    </div>`);
  });

  if ((plan.probable || []).length) {
    const collapseId = `${accId}-probable`;
    const rows = plan.probable.map((item) => {
      const checked = state.selected.has(item.key);
      const artBadge = (item.artwork || []).length ? `<span class="badge text-bg-secondary ms-1">+ artwork</span>` : "";
      const peers = (item.peers || []).map((p) => `<span class="badge text-bg-dark me-1">${escapeHtml(p.name || p.drone_id)}</span>`).join("");
      return `<tr>
        <td style="width: 2rem"><input class="form-check-input bk-asset-probable" type="checkbox" data-key="${escapeHtml(item.key)}" ${checked ? "checked" : ""} onchange="bkToggleProbableAssetItem(${jsAttr(scope)}, ${jsAttr(item.key)}, this.checked)"></td>
        <td class="small"><div><span class="badge text-bg-warning me-1">Probable</span><strong>${escapeHtml(item.name || item.path)}</strong>${artBadge}</div>
          <div class="text-muted">${item.system ? `<span class="me-1">${escapeHtml(item.system)}</span>· ` : ""}<code>${escapeHtml(item.path)}</code></div></td>
        <td class="small">${peers}</td>
        <td class="small text-end text-nowrap">${formatBytes(item.size || 0)}</td>
      </tr>`;
    }).join("");
    accordionItems.push(`<div class="accordion-item">
      <h2 class="accordion-header">
        <button class="accordion-button collapsed py-2 small" type="button" data-bs-toggle="collapse" data-bs-target="#${collapseId}">
          <span class="badge text-bg-warning me-2">Probable</span>
          <span class="fw-semibold me-2">Probable matches (${plan.probable.length})</span>
          <span class="text-muted small">Same system, path &amp; size — unticked by default, tick to include</span>
        </button>
      </h2>
      <div id="${collapseId}" class="accordion-collapse collapse" data-bs-parent="#${accId}">
        <div class="accordion-body p-2">
          <div class="d-flex justify-content-end gap-2 mb-1">
            <button type="button" class="btn btn-link btn-sm p-0" onclick="bkSetAllProbable(${jsAttr(scope)}, true)">Tick all probable</button>
            <button type="button" class="btn btn-link btn-sm p-0" onclick="bkSetAllProbable(${jsAttr(scope)}, false)">Untick all</button>
          </div>
          <div class="table-responsive" style="max-height: 28vh; overflow:auto">
            <table class="table table-sm align-middle themed-table mb-0">
              <thead><tr><th></th><th>Item</th><th>Available on</th><th class="text-end">Size</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          ${plan.probable_omitted ? `<div class="small text-muted mt-1">…and ${Number(plan.probable_omitted)} more probable match(es).</div>` : ""}
        </div>
      </div>
    </div>`);
  }

  if ((plan.unavailable || []).length) {
    const collapseId = `${accId}-unavailable`;
    const rows = plan.unavailable.map((item) => `<tr>
      <td class="small"><div><strong>${escapeHtml(item.name || item.path)}</strong></div>
        <div class="text-muted">${item.system ? `<span class="me-1">${escapeHtml(item.system)}</span>· ` : `<span>${escapeHtml(item.kind)}</span> · `}<code>${escapeHtml(item.path)}</code></div></td>
      <td class="small text-end text-nowrap text-muted">${item.size ? formatBytes(item.size) : "–"}</td>
    </tr>`).join("");
    accordionItems.push(`<div class="accordion-item">
      <h2 class="accordion-header">
        <button class="accordion-button collapsed py-2 small" type="button" data-bs-toggle="collapse" data-bs-target="#${collapseId}">
          <span class="badge text-bg-dark me-2">${Number(plan.totals?.unavailable || plan.unavailable.length)}</span>
          <span class="fw-semibold me-2">Unavailable on online Drones</span>
          <span class="text-muted small">Read-only — re-run Get missing games when more Drones are online</span>
        </button>
      </h2>
      <div id="${collapseId}" class="accordion-collapse collapse" data-bs-parent="#${accId}">
        <div class="accordion-body p-2">
          <div class="table-responsive" style="max-height: 25vh; overflow:auto">
            <table class="table table-sm align-middle themed-table mb-0">
              <thead><tr><th>Item</th><th class="text-end">Size</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          ${plan.unavailable_omitted ? `<div class="small text-muted mt-1">…and ${Number(plan.unavailable_omitted)} more unavailable item(s).</div>` : ""}
        </div>
      </div>
    </div>`);
  }

  return `<div id="bkAssetPlanSummary-${scope}">${bkRenderAssetPlanSummaryHtml(state)}</div>
    ${accordionItems.length ? `<div class="accordion themed-accordion" id="${accId}">${accordionItems.join("")}</div>` : '<div class="small text-muted">Everything recorded in this backup for the selected categories is already on this Drone.</div>'}`;
}

function bkRefreshAssetPlanContainer(scope) {
  const state = bkGetAssetPlanState(scope);
  const host = document.getElementById(`bkAssetPlan-${scope}`);
  if (host && state) host.innerHTML = bkRenderAssetPlanHtml(state);
  bkUpdateMissingFetchButton();
  bkUpdateRestoreButton();
}

function bkRefreshAssetPlanSummaryOnly(scope) {
  const state = bkGetAssetPlanState(scope);
  const host = document.getElementById(`bkAssetPlanSummary-${scope}`);
  if (host && state) host.innerHTML = bkRenderAssetPlanSummaryHtml(state);
  bkUpdateMissingFetchButton();
  bkUpdateRestoreButton();
}

function bkUntickUntilFits(scope) {
  const state = bkGetAssetPlanState(scope);
  if (!state?.plan) return;
  let stats = bkComputeAssetSelectionStats(state);
  const badKinds = () => new Set((stats.pools || []).filter((p) => !p.fits).flatMap((p) => p.kinds || []));
  while (!stats.fits) {
    const targetKinds = badKinds();
    const candidates = [];
    for (const item of state.plan.probable || []) {
      if (state.selected.has(item.key) && targetKinds.has(item.kind)) {
        candidates.push({ type: "probable", key: item.key, bytes: (item.action === "download" ? Number(item.size || 0) : 0) + Number(item.artwork_bytes || 0) });
      }
    }
    for (const group of state.plan.groups || []) {
      if (state.deselectedGroups.has(group.key) || !targetKinds.has(group.kind)) continue;
      for (const item of group.items || []) {
        if (item.default_selected === false) {
          if (state.selected.has(item.key)) {
            candidates.push({ type: "probable", key: item.key, bytes: (item.action === "download" ? Number(item.size || 0) : 0) + Number(item.artwork_bytes || 0) });
          }
        } else if (!state.deselected.has(item.key)) {
          candidates.push({ type: "exact", key: item.key, bytes: (item.action === "download" ? Number(item.size || 0) : 0) + Number(item.artwork_bytes || 0) });
        }
      }
    }
    if (candidates.length) {
      candidates.sort((a, b) => b.bytes - a.bytes);
      const pick = candidates[0];
      if (pick.type === "probable") state.selected.delete(pick.key);
      else state.deselected.add(pick.key);
      stats = bkComputeAssetSelectionStats(state);
      continue;
    }
    const groups = (state.plan.groups || []).filter((g) => !state.deselectedGroups.has(g.key) && targetKinds.has(g.kind));
    if (!groups.length) break;
    groups.sort((a, b) => Number(b.bytes || 0) - Number(a.bytes || 0));
    state.deselectedGroups.add(groups[0].key);
    stats = bkComputeAssetSelectionStats(state);
  }
  bkRefreshAssetPlanContainer(scope);
}

function bkToggleAssetGroup(scope, groupKey, checked) {
  const state = bkGetAssetPlanState(scope);
  if (!state?.plan) return;
  const group = (state.plan.groups || []).find((g) => g.key === groupKey);
  if (checked) {
    state.deselectedGroups.delete(groupKey);
    for (const item of group?.items || []) state.deselected.delete(item.key);
  } else {
    state.deselectedGroups.add(groupKey);
    for (const item of group?.items || []) {
      state.deselected.delete(item.key);
      state.selected.delete(item.key);
    }
  }
  const container = document.getElementById(`bkAssetPlan-${scope}`);
  if (container) {
    container.querySelectorAll(`.bk-asset-exact[data-group="${CSS.escape(groupKey)}"]`).forEach((box) => {
      const item = (group?.items || []).find((i) => i.key === box.dataset.key);
      box.checked = item?.default_selected === false ? state.selected.has(box.dataset.key) : Boolean(checked);
    });
  }
  bkRefreshAssetPlanSummaryOnly(scope);
}

function bkToggleExactAssetItem(scope, groupKey, itemKey, checked) {
  const state = bkGetAssetPlanState(scope);
  if (!state?.plan) return;
  const group = (state.plan.groups || []).find((g) => g.key === groupKey);
  const item = (group?.items || []).find((i) => i.key === itemKey);
  if (item && item.default_selected === false) {
    if (checked) {
      state.deselectedGroups.delete(groupKey);
      state.selected.add(itemKey);
    } else {
      state.selected.delete(itemKey);
    }
    bkRefreshAssetPlanSummaryOnly(scope);
    return;
  }
  if (state.deselectedGroups.has(groupKey)) {
    state.deselectedGroups.delete(groupKey);
    for (const entry of group?.items || []) {
      if (entry.default_selected !== false && entry.key !== itemKey) state.deselected.add(entry.key);
    }
  }
  if (checked) state.deselected.delete(itemKey);
  else state.deselected.add(itemKey);
  const container = document.getElementById(`bkAssetPlan-${scope}`);
  if (container && group) {
    const groupBox = container.querySelector(`.bk-asset-group[data-group="${CSS.escape(groupKey)}"]`);
    if (groupBox) groupBox.checked = !state.deselectedGroups.has(groupKey) && !(group.items || []).filter((i) => i.default_selected !== false).every((i) => state.deselected.has(i.key));
  }
  bkRefreshAssetPlanSummaryOnly(scope);
}

function bkToggleProbableAssetItem(scope, itemKey, checked) {
  const state = bkGetAssetPlanState(scope);
  if (!state?.plan) return;
  if (checked) state.selected.add(itemKey);
  else state.selected.delete(itemKey);
  bkRefreshAssetPlanSummaryOnly(scope);
}

function bkSetAllProbable(scope, checked) {
  const state = bkGetAssetPlanState(scope);
  if (!state?.plan) return;
  for (const item of state.plan.probable || []) {
    if (checked) state.selected.add(item.key);
    else state.selected.delete(item.key);
  }
  const container = document.getElementById(`bkAssetPlan-${scope}`);
  if (container) {
    container.querySelectorAll(".bk-asset-probable").forEach((box) => { box.checked = Boolean(checked); });
  }
  bkRefreshAssetPlanSummaryOnly(scope);
}

function bkUpdateMissingFetchButton() {
  const button = document.getElementById("bkMissingFetchBtn");
  if (!button) return;
  const state = BK.missing;
  if (!state || state.loading || !state.plan) {
    button.disabled = true;
    return;
  }
  const stats = bkComputeAssetSelectionStats(state);
  button.disabled = stats.selectedCount <= 0 || !stats.fits;
  button.innerHTML = `<i class="bi bi-cloud-arrow-down me-1"></i>Fetch ${stats.selectedCount} item(s)${stats.selectedBytes ? ` (${formatBytes(stats.selectedBytes)})` : ""}`;
}

async function bkOpenMissingGames(backupId) {
  bkModal("bkMissingGamesModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi bi-controller me-2"></i>Get missing games &amp; media</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body"><div class="text-center py-4"><span class="spinner-border"></span></div></div>`, "modal-xl");
  let data;
  try {
    data = await api(`/admin/backups/${encodeURIComponent(backupId)}`, BK_SLOW);
  } catch (err) {
    document.querySelector("#bkMissingGamesModal .modal-body").innerHTML = `<div class="alert alert-danger mb-0">${escapeHtml(err.message || "Could not load the backup.")}</div>`;
    return;
  }
  const backup = data.backup || {};
  const state = bkInitAssetPlanState("missing", backupId, backup.name || "backup", data.assets);
  BK.missing = state;
  const assetsLine = bkAssetsSummaryLine(data.assets);
  bkModal("bkMissingGamesModal", `
    <div class="modal-header"><h5 class="modal-title mb-0"><i class="bi bi-controller me-2"></i>Get missing games: ${escapeHtml(backup.name || "Backup")}</h5>
      <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button></div>
    <div class="modal-body">
      <p class="small text-muted mb-1">${bkSourceLine(backup)}${backup.created_at ? ` · ${escapeHtml(bkDate(backup.created_at))}` : ""}</p>
      ${assetsLine ? `<p class="small text-info mb-2"><i class="bi bi-controller me-1"></i>${escapeHtml(assetsLine)}</p>` : ""}
      <p class="small text-muted mb-2">Searches your online paired Drones for games, artwork and media recorded in this backup that are not on this Drone yet. Downloads are spread across peers and never overwrite existing files.</p>
      <div class="d-flex flex-wrap align-items-center gap-2 mb-3">
        <div id="bkAssetKinds-missing">${bkAssetKindCheckboxesHtml(state)}</div>
        <button type="button" class="btn btn-sm btn-outline-info ms-auto" onclick="bkPlanAssets('missing')"><i class="bi bi-arrow-clockwise me-1"></i>Search swarm again</button>
      </div>
      <div id="bkAssetPlan-missing">${bkRenderAssetPlanHtml(state)}</div>
    </div>
    <div class="modal-footer">
      <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Close</button>
      <button type="button" class="btn btn-primary" id="bkMissingFetchBtn" onclick="bkSubmitMissingGames()" disabled><i class="bi bi-cloud-arrow-down me-1"></i>Fetch selected</button>
    </div>`, "modal-xl");
  await bkPlanAssets("missing");
}

async function bkSubmitMissingGames() {
  const state = BK.missing;
  if (!state?.plan) return;
  const stats = bkComputeAssetSelectionStats(state);
  const selection = bkBuildAssetSelection(state);
  if (!selection || selection._selectedCount <= 0) {
    showToast("Select at least one item to fetch.", "warning");
    return;
  }
  if (!stats.fits) {
    showToast("Free disk space is too low for the selected downloads. Untick items until it fits.", "warning");
    bkUpdateMissingFetchButton();
    return;
  }
  const button = document.getElementById("bkMissingFetchBtn");
  if (button) button.disabled = true;
  try {
    const res = await apiPost(`/admin/backups/${encodeURIComponent(state.backupId)}/assets/fetch`, {
      plan_id: selection.plan_id,
      selected: selection.selected,
      deselected: selection.deselected,
      deselected_groups: selection.deselected_groups,
    }, BK_SLOW);
    bkHideModal("bkMissingGamesModal");
    if (res.batch_id && Number(res.queued || 0) > 0 && window.DroneTransferToast?.trackBatch) {
      BK.trackedBatchId = res.batch_id;
      window.DroneTransferToast.trackBatch(res.batch_id, `Restoring from ${res.label || state.backupName}`);
    }
    const unavailableNote = res.unavailable ? ` (${Number(res.unavailable)} unavailable on online Drones)` : "";
    showToast(`Queued ${Number(res.queued || 0)} download(s) from the swarm${unavailableNote}.`, res.queued ? "success" : "info");
  } catch (err) {
    showToast(escapeHtml(err.message || "Could not queue the downloads."), "danger");
    if (button) button.disabled = false;
  }
}
