"""Backups: find backups on every online Drone, copy them here, and restore them.

Two tabs:

* **Find Backups** -- search backups on this Drone and every online paired Drone
  (``GET /admin/backups/swarm``). A peer's backup can be copied to this Drone
  through the normal transfer queue (verified on arrival), or copied and then
  restored in one go.
* **This Drone** -- create a settings backup (saves are left out by default), restore one of this Drone's
  backups, or roll back a restore from its automatic safety snapshot.

Restores open a gamepad-navigable popup: pick the parts (grouped checkboxes) --
or, after "Choose files...", folders and single files in a per-part tree --
Overlay or Exact, optionally include this machine's display/audio/network keys,
Preview (the Drone compares against local files), acknowledge any compatibility
warnings, then Restore. The Drone takes a safety snapshot first and then closes
running games -- including this Ports app -- and restarts EmulationStation, so the
popup says so before anything starts.

Network calls go through ``Screen.defer_action``; transfer progress is polled on its
own daemon thread so the render loop never blocks.
"""

import threading
import time
from typing import Dict, List, Optional

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from ..theme import ERROR_COLOR, MUTED_COLOR, SUCCESS_COLOR, WARNING_COLOR
from ..widgets import button_hint_footer, search_box, tab_button
from .base import Screen

_LIST_HEIGHT = 380.0
_RESTORE_POPUP = "Restore Backup"
_POLL_SECONDS = 1.5
_TERMINAL_JOB_STATES = ("completed", "failed", "error", "cancelled")


class BackupsScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.tab = "swarm"
        self.query = ""
        self.sources: List[dict] = []
        self.local_backups: List[dict] = []
        self.snapshots: List[dict] = []
        self.restore_state: dict = {}
        self.error: Optional[str] = None
        self.message: Optional[str] = None
        self.copies: Dict[str, dict] = {}
        self.dialog: Optional[dict] = None
        self._dialog_just_opened = False
        self._open_after_copy: Optional[str] = None
        self._poller: Optional[threading.Thread] = None

    # ------------------------------------------------------------- data

    def on_enter(self) -> None:
        self._reload()

    def _reload(self) -> None:
        self._load_local()
        self._search()

    def _load_local(self) -> None:
        try:
            result = endpoints.backups(self.api_client) or {}
            self.local_backups = list(result.get("backups") or [])
            self.snapshots = list(result.get("snapshots") or [])
            self.restore_state = dict(result.get("restore") or {})
            self.error = None
        except DroneApiError as error:
            self.error = str(error)

    def _search(self) -> None:
        try:
            result = endpoints.backups_swarm(self.api_client, query=self.query) or {}
            self.sources = list(result.get("sources") or [])
            self.error = None
        except DroneApiError as error:
            self.error = str(error)

    def _find_local(self, backup_id: str) -> Optional[dict]:
        return next((row for row in self.local_backups + self.snapshots if row.get("backup_id") == backup_id), None)

    def _create_backup(self) -> None:
        try:
            endpoints.create_backup(self.api_client)
            self.message = "Backup started. It builds in the background; refresh to see it."
        except DroneApiError as error:
            self.message = f"Could not start a backup: {error}"
        self._load_local()

    # ------------------------------------------------------------ copies

    def _request_copy(self, drone_id: str, backup_id: str, restore_after: bool = False) -> None:
        try:
            result = endpoints.copy_backup_from_drone(self.api_client, drone_id, backup_id) or {}
        except DroneApiError as error:
            self.copies[backup_id] = {"status": "failed", "error": str(error)}
            return
        if result.get("status") == "queued":
            job = result.get("job") or {}
            self.copies[backup_id] = {"job_id": job.get("id") or job.get("job_id"), "status": "queued", "percentage": 0,
                                      "restore_after": restore_after}
            self._ensure_poller()
        else:
            self.copies[backup_id] = {"status": "completed"}
            self._load_local()
            if restore_after:
                self._open_after_copy = backup_id

    def _ensure_poller(self) -> None:
        if self._poller is not None and self._poller.is_alive():
            return
        self._poller = threading.Thread(target=self._poll_loop, name="drone-ports-backup-copies", daemon=True)
        self._poller.start()

    def _poll_loop(self) -> None:
        while self._poll_downloads_once():
            time.sleep(_POLL_SECONDS)

    def _poll_downloads_once(self) -> bool:
        """Update copy progress; returns whether any copy is still running."""
        pending = {backup_id: copy for backup_id, copy in self.copies.items()
                   if copy.get("job_id") and copy.get("status") not in _TERMINAL_JOB_STATES}
        if not pending:
            return False
        try:
            snapshot = endpoints.downloads(self.api_client) or {}
        except DroneApiError:
            return True
        jobs = {}
        for key in ("active", "queued", "recent", "downloads"):
            for job in snapshot.get(key) or []:
                if isinstance(job, dict):
                    jobs[job.get("id") or job.get("job_id")] = job
        finished = False
        for backup_id, copy in pending.items():
            job = jobs.get(copy["job_id"])
            if not job:
                continue
            status = str(job.get("status") or "queued")
            copy.update(status=status, percentage=float(job.get("percentage") or 0),
                        error=job.get("error_message") or job.get("failure_reason"))
            if status == "completed":
                finished = True
                if copy.get("restore_after"):
                    self._open_after_copy = backup_id
        if finished:
            self._load_local()
            self._search()
        return any(copy.get("status") not in _TERMINAL_JOB_STATES for copy in pending.values())

    # ----------------------------------------------------------- restore

    def _open_restore(self, backup: dict, rollback: bool = False) -> None:
        categories = list(backup.get("categories") or [])
        if rollback:
            known = {item.get("id") for item in categories}
            for category_id in (backup.get("snapshot_of") or {}).get("categories") or []:
                if category_id not in known:
                    categories.append({"id": category_id, "label": category_id, "group": "other",
                                       "group_label": "Previously empty", "file_count": 0})
        self.dialog = {
            "backup": backup,
            "categories": categories,
            "selected": {item.get("id") for item in categories},
            "mode": "exact" if rollback else "overlay",
            "machine_keys": False,
            "plan": None,
            "plan_error": None,
            "ack": False,
            "ack_warnings": False,
            "rollback": rollback,
            # Per-file choice, filled by _load_files: trees per category, and the picked
            # file paths of categories that are not restored whole.
            "trees": None,
            "picked": set(),
            "files_error": None,
        }
        self._dialog_just_opened = True

    def _load_files(self) -> None:
        dialog = self.dialog
        if not dialog:
            return
        try:
            detail = endpoints.backup_detail(self.api_client, dialog["backup"]["backup_id"]) or {}
        except DroneApiError as error:
            dialog["files_error"] = str(error)
            return
        dialog["trees"] = build_file_trees(detail.get("files") or [])
        dialog["files_error"] = None

    def _dialog_options(self) -> dict:
        dialog = self.dialog or {}
        ordered = [item.get("id") for item in dialog.get("categories") or [] if item.get("id") in dialog.get("selected", set())]
        options = {"categories": ordered, "mode": dialog.get("mode", "overlay"),
                   "include_machine_keys": bool(dialog.get("machine_keys"))}
        paths = selection_paths(dialog)
        if paths:
            options["paths"] = paths
        return options

    def _preview(self) -> None:
        dialog = self.dialog
        if not dialog:
            return
        options = self._dialog_options()
        try:
            dialog["plan"] = endpoints.plan_backup_restore(
                self.api_client, dialog["backup"]["backup_id"], options["categories"],
                mode=options["mode"], include_machine_keys=options["include_machine_keys"],
                paths=options.get("paths"))
            dialog["plan_error"] = None
        except DroneApiError as error:
            dialog["plan"] = None
            dialog["plan_error"] = str(error)

    def _restore(self) -> None:
        dialog = self.dialog
        if not dialog:
            return
        options = self._dialog_options()
        try:
            endpoints.restore_backup(self.api_client, dialog["backup"]["backup_id"], options["categories"],
                                     mode=options["mode"], include_machine_keys=options["include_machine_keys"],
                                     acknowledge_warnings=bool(dialog.get("ack_warnings")),
                                     paths=options.get("paths"))
            self.message = ("Restore started. A safety snapshot is taken first, then running games (including this app) "
                            "close and EmulationStation restarts.")
            self.dialog = None
        except DroneApiError as error:
            dialog["plan_error"] = f"Restore could not start: {error}"
        self._load_local()

    def _reset_plan(self) -> None:
        if self.dialog:
            self.dialog["plan"] = None
            self.dialog["ack"] = False
            self.dialog["ack_warnings"] = False

    # -------------------------------------------------------------- draw

    def draw(self, navigator) -> None:
        if self._open_after_copy:
            backup = self._find_local(self._open_after_copy)
            self._open_after_copy = None
            if backup:
                self._open_restore(backup)
        for key, label in (("swarm", "Find Backups"), ("local", "This Drone")):
            tab_button(f"{label}##backups_tab_{key}", active=self.tab == key, on_click=lambda k=key: setattr(self, "tab", k))
            imgui.same_line()
        if imgui.button("Refresh##backups_refresh"):
            self.defer_action("Loading backups...", self._reload)
        imgui.spacing()
        if self.restore_state.get("state") == "running":
            imgui.text_colored(WARNING_COLOR, f"Restoring {self.restore_state.get('backup_name') or 'a backup'}...")
        elif self.restore_state.get("state") == "error":
            imgui.text_colored(ERROR_COLOR, f"Last restore failed: {self.restore_state.get('error') or 'unknown error'}")
        if self.message:
            imgui.text_wrapped(self.message)
        if self.error:
            imgui.text_colored(ERROR_COLOR, self.error)
        self._draw_restore_popup()
        if self.tab == "swarm":
            self._draw_swarm()
        else:
            self._draw_local()
        button_hint_footer("A Select   B Back   L1/R1 Section")

    def _draw_swarm(self) -> None:
        self.query, triggered = search_box("backups_search", self.query)
        if triggered:
            self.defer_action("Searching backups...", self._search)
        imgui.begin_child("backups_swarm_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not self.sources:
            imgui.text_disabled("No Drones answered yet. Choose Refresh.")
        for source in self.sources:
            name = source.get("name") or source.get("drone_id")
            imgui.text_colored(MUTED_COLOR, str(name).upper())
            if source.get("status") != "ok":
                imgui.text_colored(WARNING_COLOR, f"   {source.get('error') or 'Could not list backups.'}")
                imgui.separator()
                continue
            if not source.get("backups"):
                imgui.text_disabled("   No backups.")
            for backup in source.get("backups") or []:
                self._draw_swarm_row(backup)
            imgui.separator()
        imgui.end_child()

    def _draw_swarm_row(self, backup: dict) -> None:
        backup_id = str(backup.get("backup_id") or "")
        drone_id = str(backup.get("drone_id") or "")
        imgui.text(str(backup.get("name") or "Backup"))
        imgui.text_colored(MUTED_COLOR, "   " + _details(backup))
        imgui.text_colored(MUTED_COLOR, "   " + _category_summary(backup.get("categories")))
        for warning in (backup.get("compatibility") or {}).get("warnings") or []:
            imgui.text_colored(WARNING_COLOR, f"   ! {warning}")
        copy = self.copies.get(backup_id)
        if copy:
            status = copy.get("status")
            if status == "completed":
                imgui.text_colored(SUCCESS_COLOR, "   Copied to this Drone and verified.")
            elif status in _TERMINAL_JOB_STATES:
                imgui.text_colored(ERROR_COLOR, f"   Copy failed: {copy.get('error') or status}")
            else:
                imgui.text_colored(WARNING_COLOR, f"   Copying... {copy.get('percentage', 0):.0f}%")
        here = bool(backup.get("is_local") or backup.get("available_locally"))
        busy = bool(copy) and copy.get("status") not in _TERMINAL_JOB_STATES
        if here:
            local = self._find_local(backup_id) or backup
            if imgui.button(f"Restore here##restore_{drone_id}_{backup_id}"):
                self._open_restore(local)
        else:
            imgui.begin_disabled(busy)
            if imgui.button(f"Copy to this Drone##copy_{drone_id}_{backup_id}"):
                self.defer_action("Queuing copy...", lambda d=drone_id, b=backup_id: self._request_copy(d, b))
            imgui.same_line()
            if imgui.button(f"Copy & Restore##copyrestore_{drone_id}_{backup_id}"):
                self.defer_action("Queuing copy...", lambda d=drone_id, b=backup_id: self._request_copy(d, b, True))
            imgui.end_disabled()
        imgui.spacing()

    def _draw_local(self) -> None:
        if imgui.button("Create Backup (settings, no saves)##backups_create"):
            self.defer_action("Starting backup...", self._create_backup)
        imgui.begin_child("backups_local_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        imgui.text_colored(MUTED_COLOR, "BACKUPS")
        if not self.local_backups:
            imgui.text_disabled("   No backups yet.")
        for backup in self.local_backups:
            self._draw_local_row(backup, rollback=False)
        imgui.spacing()
        imgui.text_colored(MUTED_COLOR, "SAFETY SNAPSHOTS (taken before each restore)")
        if not self.snapshots:
            imgui.text_disabled("   None yet.")
        for snapshot in self.snapshots:
            self._draw_local_row(snapshot, rollback=True)
        imgui.end_child()

    def _draw_local_row(self, backup: dict, *, rollback: bool) -> None:
        backup_id = str(backup.get("backup_id") or "")
        imgui.text(str(backup.get("name") or "Backup"))
        status = backup.get("status")
        if status == "creating":
            imgui.same_line()
            imgui.text_colored(WARNING_COLOR, "creating...")
        elif status == "error":
            imgui.text_colored(ERROR_COLOR, f"   {backup.get('error_message') or 'failed'}")
            return
        imgui.text_colored(MUTED_COLOR, "   " + _details(backup))
        if status == "complete":
            imgui.text_colored(MUTED_COLOR, "   " + _category_summary(backup.get("categories")))
            label = "Roll back" if rollback else "Restore"
            if imgui.button(f"{label}##local_{backup_id}"):
                self._open_restore(backup, rollback=rollback)
        imgui.spacing()

    def _draw_restore_popup(self) -> None:
        if self._dialog_just_opened:
            imgui.open_popup(_RESTORE_POPUP)
            self._dialog_just_opened = False
        dialog = self.dialog
        if dialog is None:
            return
        opened, _ = imgui.begin_popup_modal(_RESTORE_POPUP, flags=imgui.WindowFlags_.always_auto_resize.value)
        if not opened:
            return
        backup = dialog["backup"]
        title = f"Roll back: {(backup.get('snapshot_of') or {}).get('name') or backup.get('name')}" if dialog["rollback"] \
            else f"Restore '{backup.get('name') or 'backup'}' to this Drone"
        imgui.text_colored(WARNING_COLOR, title)
        imgui.text_colored(MUTED_COLOR, _details(backup))
        trees = dialog.get("trees")
        imgui.begin_child("restore_parts", imgui.ImVec2(640, 300 if trees else 220), True)
        for group in _groups(dialog["categories"]):
            imgui.text_colored(MUTED_COLOR, group["label"])
            for item in group["items"]:
                if trees and item["id"] in trees:
                    self._draw_tree_node(dialog, trees[item["id"]], item.get("label") or item["id"])
                    continue
                checked = item["id"] in dialog["selected"]
                changed, value = imgui.checkbox(f"{item.get('label') or item['id']} ({int(item.get('file_count') or 0)} files)##cat_{item['id']}", checked)
                if changed:
                    (dialog["selected"].add if value else dialog["selected"].discard)(item["id"])
                    self._reset_plan()
        imgui.end_child()
        picked, total = picked_file_count(dialog)
        if trees:
            imgui.text_colored(MUTED_COLOR, f"{picked} of {total} files selected. Open a part to pick folders or single files.")
        elif not dialog["rollback"]:
            if imgui.button("Choose files...##restore_choose_files"):
                self.defer_action("Loading the backup's files...", self._load_files)
            imgui.same_line()
            imgui.text_colored(MUTED_COLOR, "Pick folders or single files instead of whole parts.")
        if dialog.get("files_error"):
            imgui.text_colored(ERROR_COLOR, dialog["files_error"])
        if imgui.radio_button("Overlay: write the backup's files only", dialog["mode"] == "overlay"):
            dialog["mode"] = "overlay"
            self._reset_plan()
        if imgui.radio_button("Exact: also remove files the backup does not have", dialog["mode"] == "exact"):
            dialog["mode"] = "exact"
            self._reset_plan()
        changed, dialog["machine_keys"] = imgui.checkbox("Also restore display/audio/network/hostname settings", dialog["machine_keys"])
        if changed:
            self._reset_plan()
        imgui.spacing()
        if imgui.button("Preview changes##restore_preview"):
            self.defer_action("Comparing with this Drone...", self._preview)
        if dialog.get("plan_error"):
            imgui.text_colored(ERROR_COLOR, dialog["plan_error"])
        plan = dialog.get("plan")
        if plan:
            for line in _plan_lines(plan):
                imgui.text_wrapped(line)
            for warning in plan.get("warnings") or []:
                imgui.text_colored(WARNING_COLOR, f"! {warning}")
            if plan.get("warnings"):
                _, dialog["ack_warnings"] = imgui.checkbox("I understand the warnings above", dialog["ack_warnings"])
            imgui.text_wrapped("A safety snapshot is taken first. Running games, including this Ports app, are closed and "
                               "EmulationStation restarts.")
            _, dialog["ack"] = imgui.checkbox("Restore these parts now", dialog["ack"])
        imgui.spacing()
        imgui.set_item_default_focus()
        cancel = imgui.button("Cancel##restore_cancel") or imgui.is_key_pressed(imgui.Key.gamepad_face_right)
        imgui.same_line()
        ready = bool(plan) and dialog["ack"] and (dialog["ack_warnings"] or not plan.get("warnings"))
        imgui.begin_disabled(not ready)
        confirm = imgui.button("Restore##restore_confirm")
        imgui.end_disabled()
        if cancel:
            self.dialog = None
            imgui.close_current_popup()
        elif confirm and ready:
            imgui.close_current_popup()
            self.defer_action("Starting restore...", self._restore)
        imgui.end_popup()


    def _draw_tree_node(self, dialog: dict, node: "FileNode", label: Optional[str] = None) -> None:
        """One part or folder: a checkbox (ticks everything under it) beside an expandable node."""
        picked = node_picked(dialog, node)
        name = label or node.name
        partial = f"  [{picked}/{node.file_count}]" if 0 < picked < node.file_count else ""
        changed, value = imgui.checkbox(f"##pick_{node.key}", picked == node.file_count and node.file_count > 0)
        if changed:
            toggle_node(dialog, node, value)
            self._reset_plan()
        imgui.same_line()
        opened = imgui.tree_node(f"{name}{partial}  -- {node.file_count} files, {_format_size(node.size)}##node_{node.key}")
        if not opened:
            return
        for child in node.dirs.values():
            self._draw_tree_node(dialog, child)
        for entry in node.files[:_TREE_FILE_LIMIT]:
            checked = node.category in dialog["selected"] or entry["path"] in dialog["picked"]
            changed, value = imgui.checkbox(f"{entry['name']}  ({_format_size(int(entry.get('size') or 0))})##file_{entry['path']}", checked)
            if changed:
                toggle_file(dialog, node.category, entry["path"], value)
                self._reset_plan()
        if len(node.files) > _TREE_FILE_LIMIT:
            imgui.text_colored(MUTED_COLOR, f"...and {len(node.files) - _TREE_FILE_LIMIT} more; tick the folder to include them all.")
        imgui.tree_pop()


# ------------------------------------------------------------- file tree logic
#
# Same model as the browser picker: a category in dialog["selected"] is restored
# whole; otherwise only the paths in dialog["picked"]. A fully ticked folder is
# sent as one folder path (the Drone treats a path as "this file or everything
# under this folder").

_TREE_FILE_LIMIT = 200


class FileNode:
    def __init__(self, key: str, name: str, path: str, category: str):
        self.key = key
        self.name = name
        self.path = path
        self.category = category
        self.dirs: Dict[str, "FileNode"] = {}
        self.files: List[dict] = []
        self.file_count = 0
        self.size = 0
        self.is_category = False
        self._paths: Optional[List[str]] = None

    def paths(self) -> List[str]:
        if self._paths is None:
            self._paths = [entry["path"] for entry in self.files]
            for child in self.dirs.values():
                self._paths.extend(child.paths())
        return self._paths


def build_file_trees(files) -> Dict[str, FileNode]:
    """One folder tree per category, rooted at the category's common folder, with
    single-child folder chains merged (``nand/user/save``) to keep it compact."""
    by_category: Dict[str, List[dict]] = {}
    for entry in files or []:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry.get("category"):
            by_category.setdefault(str(entry["category"]), []).append(entry)
    counter = [0]

    def make(name: str, path: str, category: str) -> FileNode:
        counter[0] += 1
        return FileNode(f"n{counter[0]}", name, path, category)

    trees: Dict[str, FileNode] = {}
    for category, entries in by_category.items():
        split = [entry["path"].split("/") for entry in entries]
        prefix = split[0][:-1]
        for parts in split:
            i = 0
            while i < len(prefix) and i < len(parts) - 1 and parts[i] == prefix[i]:
                i += 1
            prefix = prefix[:i]
        root = make(category, "/".join(prefix), category)
        root.is_category = True
        for entry, parts in zip(entries, split):
            node = root
            for folder in parts[len(prefix):-1]:
                if folder not in node.dirs:
                    node.dirs[folder] = make(folder, f"{node.path}/{folder}" if node.path else folder, category)
                node = node.dirs[folder]
            node.files.append(dict(entry, name=parts[-1]))
        _finish(root)
        trees[category] = root
    return trees


def _finish(node: FileNode) -> None:
    merged: Dict[str, FileNode] = {}
    for child in node.dirs.values():
        while len(child.dirs) == 1 and not child.files:
            only = next(iter(child.dirs.values()))
            only.name = f"{child.name}/{only.name}"
            child = only
        merged[child.name] = child
    node.dirs = dict(sorted(merged.items(), key=lambda item: item[0].lower()))
    node.files.sort(key=lambda entry: entry["name"].lower())
    node.file_count = len(node.files)
    node.size = sum(int(entry.get("size") or 0) for entry in node.files)
    for child in node.dirs.values():
        _finish(child)
        node.file_count += child.file_count
        node.size += child.size


def node_picked(dialog: dict, node: FileNode) -> int:
    if node.category in dialog["selected"]:
        return node.file_count
    picked = dialog["picked"]
    return sum(1 for path in node.paths() if path in picked)


def _unwhole(dialog: dict, category: str) -> None:
    if category in dialog["selected"]:
        dialog["selected"].discard(category)
        dialog["picked"].update(dialog["trees"][category].paths())


def _normalize(dialog: dict, category: str) -> None:
    root = dialog["trees"][category]
    paths = root.paths()
    if paths and all(path in dialog["picked"] for path in paths):
        dialog["picked"].difference_update(paths)
        dialog["selected"].add(category)


def toggle_node(dialog: dict, node: FileNode, value: bool) -> None:
    if node.is_category:
        dialog["picked"].difference_update(node.paths())
        (dialog["selected"].add if value else dialog["selected"].discard)(node.category)
        return
    _unwhole(dialog, node.category)
    if value:
        dialog["picked"].update(node.paths())
    else:
        dialog["picked"].difference_update(node.paths())
    _normalize(dialog, node.category)


def toggle_file(dialog: dict, category: str, path: str, value: bool) -> None:
    _unwhole(dialog, category)
    (dialog["picked"].add if value else dialog["picked"].discard)(path)
    _normalize(dialog, category)


def selection_paths(dialog: dict) -> List[str]:
    """Picked files of partly chosen categories, with fully ticked folders folded into one path."""
    trees = dialog.get("trees") or {}
    picked = dialog.get("picked") or set()
    if not picked:
        return []
    out: List[str] = []

    def emit(node: FileNode) -> None:
        count = node_picked(dialog, node)
        if not count:
            return
        if count == node.file_count and not node.is_category and node.path:
            out.append(node.path)
            return
        out.extend(entry["path"] for entry in node.files if entry["path"] in picked)
        for child in node.dirs.values():
            emit(child)

    for category, root in trees.items():
        if category not in dialog["selected"]:
            emit(root)
    return out


def picked_file_count(dialog: dict):
    trees = dialog.get("trees") or {}
    total = sum(root.file_count for root in trees.values())
    picked = sum(node_picked(dialog, root) for root in trees.values())
    return picked, total


def _groups(categories) -> List[dict]:
    groups: List[dict] = []
    index: Dict[str, dict] = {}
    for item in categories or []:
        key = str(item.get("group") or "other")
        if key not in index:
            index[key] = {"label": str(item.get("group_label") or key), "items": []}
            groups.append(index[key])
        index[key]["items"].append(item)
    return groups


def _category_summary(categories) -> str:
    groups = _groups(categories)
    if not groups:
        return "No files"
    return ", ".join(group["label"] for group in groups[:6]) + (f" +{len(groups) - 6} more" if len(groups) > 6 else "")


def _details(backup: dict) -> str:
    source = backup.get("source") or {}
    bits = []
    if backup.get("drone_name") and not backup.get("is_local"):
        bits.append(f"on {backup['drone_name']}")
    if source.get("batocera_version"):
        bits.append(f"Batocera {source['batocera_version']}")
    if backup.get("size_bytes"):
        bits.append(_format_size(int(backup["size_bytes"])))
    if backup.get("created_at"):
        bits.append(str(backup["created_at"]).replace("T", " ")[:16])
    return " -- ".join(bits)


def _plan_lines(plan: dict) -> List[str]:
    totals = plan.get("totals") or {}
    lines = [f"{int(totals.get('new') or 0)} new, {int(totals.get('changed') or 0)} changed, "
             f"{int(totals.get('identical') or 0)} already the same"
             + (f", {int(totals.get('remove') or 0)} removed" if plan.get("mode") == "exact" else "") + "."]
    conf = plan.get("batocera_conf")
    if conf:
        count = len(conf.get("changed") or []) + len(conf.get("added") or []) + len(conf.get("removed") or [])
        lines.append(f"batocera.conf: {count} setting change(s).")
    if totals.get("blocked"):
        lines.append(f"{int(totals['blocked'])} file(s) are blocked by a link or folder and will be skipped.")
    return lines


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
