"""Admin -> Backups: list existing config backups, create a new one, and
apply (restore) one back onto this machine.

Download/delete/email aren't built here -- downloading needs somewhere on
a *second* device to save the file to (meaningless from the console
itself), and delete/email are lower-value from a gamepad-only console
screen. Apply/restore is included because it's the one destructive action
users actually need from here (recovering this machine's own config), so
it gets its own gamepad-navigable confirmation popup mirroring the browser
UI's ack-checkbox-gated modal (same warning copy: overlay semantics, ES
restart, cannot be undone) rather than a bare button.
"""

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from ..theme import ERROR_COLOR, MUTED_COLOR, SUCCESS_COLOR, WARNING_COLOR
from .base import Screen

_LIST_HEIGHT = 320.0
_STATUS_COLUMN_X = 320.0
_APPLY_POPUP_NAME = "Apply Backup"


class BackupsScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.backups = []
        self.error = None
        self.create_message = None
        self.apply_message = None
        self.pending_apply_id = None
        self.pending_apply_name = ""
        self.apply_ack = False
        self._apply_popup_just_opened = False

    def on_enter(self) -> None:
        self._reload()

    def _reload(self) -> None:
        try:
            result = endpoints.config_backups(self.api_client)
            self.backups = result.get("backups", []) if isinstance(result, dict) else []
            self.error = None
        except DroneApiError as error:
            self.error = str(error)

    def _create_backup(self) -> None:
        try:
            result = endpoints.create_config_backup(self.api_client)
            if result.get("status") == "already_creating":
                self.create_message = "A backup is already being created."
            else:
                self.create_message = "Backup started."
        except DroneApiError as error:
            self.create_message = str(error)
        self._reload()

    def _open_apply_confirmation(self, backup_id, display_name: str) -> None:
        self.pending_apply_id = backup_id
        self.pending_apply_name = display_name
        self.apply_ack = False
        self.apply_message = None
        self._apply_popup_just_opened = True

    def _apply_backup(self, backup_id) -> None:
        try:
            result = endpoints.apply_config_backup(self.api_client, backup_id)
            if result.get("status") == "not_found":
                self.apply_message = "That backup no longer exists."
            elif result.get("status") == "error":
                self.apply_message = f"Failed to apply backup: {result.get('error') or 'unknown error'}"
            else:
                restarted = " EmulationStation was restarted." if result.get("restarted_emulationstation") else ""
                self.apply_message = f"Backup applied: {int(result.get('restored_file_count') or 0)} file(s) restored.{restarted}"
        except DroneApiError as error:
            self.apply_message = f"Failed to apply backup: {error}"
        self._reload()

    def draw(self, navigator) -> None:
        if imgui.button("Create Backup"):
            self.defer_action("Creating backup...", self._create_backup)
        imgui.same_line()
        if imgui.button("Refresh"):
            self.defer_action("Loading backups...", self._reload)
        if self.create_message:
            imgui.same_line()
            imgui.text_disabled(self.create_message)
        if self.apply_message:
            imgui.text_colored(SUCCESS_COLOR if "applied" in self.apply_message.lower() else ERROR_COLOR, self.apply_message)
        imgui.spacing()

        self._draw_apply_confirmation_popup()

        if self.error:
            imgui.text_colored(ERROR_COLOR, self.error)
            return

        imgui.begin_child("backups_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not self.backups:
            imgui.text_disabled("No backups yet.")
        for backup in self.backups:
            self._draw_backup_row(backup)
        imgui.end_child()

    def _draw_backup_row(self, backup: dict) -> None:
        backup_id = backup.get("id")
        label = backup.get("name") or backup.get("file_name") or f"Backup #{backup_id}"
        imgui.text(str(label))

        imgui.same_line(_STATUS_COLUMN_X)
        status = backup.get("status", "")
        if status == "complete":
            imgui.text_colored(SUCCESS_COLOR, status)
        elif status == "error":
            imgui.text_colored(ERROR_COLOR, status)
        else:
            imgui.text_colored(WARNING_COLOR, status or "unknown")

        size_bytes = backup.get("size_bytes")
        created_at = backup.get("created_at")
        detail_bits = []
        if size_bytes:
            detail_bits.append(_format_size(size_bytes))
        if created_at:
            detail_bits.append(str(created_at))
        if detail_bits:
            imgui.text_colored(MUTED_COLOR, "   " + " -- ".join(detail_bits))

        if backup.get("error_message"):
            imgui.text_colored(ERROR_COLOR, f"   {backup['error_message']}")

        if status == "complete":
            if imgui.button(f"Apply this Backup##apply_{backup_id}"):
                self._open_apply_confirmation(backup_id, str(label))

        imgui.separator()

    def _draw_apply_confirmation_popup(self) -> None:
        if self._apply_popup_just_opened:
            imgui.open_popup(_APPLY_POPUP_NAME)
            self._apply_popup_just_opened = False

        if self.pending_apply_id is None:
            return

        opened, _ = imgui.begin_popup_modal(_APPLY_POPUP_NAME, flags=imgui.WindowFlags_.always_auto_resize.value)
        if not opened:
            return

        imgui.text_colored(WARNING_COLOR, f"Apply '{self.pending_apply_name}' to this Drone?")
        imgui.spacing()
        imgui.text_wrapped(
            "Every file this backup contains overwrites the matching file here "
            "(system configs, a system's gamelist.xml, custom scripts, specific "
            "saves). Nothing is deleted -- anything not part of this backup is "
            "left exactly as it is."
        )
        imgui.text_wrapped("EmulationStation (and any running game) will be stopped during the copy and restarted afterward.")
        imgui.spacing()
        imgui.text_colored(ERROR_COLOR, "This cannot be undone.")
        imgui.spacing()
        _, self.apply_ack = imgui.checkbox("I understand this will overwrite files and cannot be undone.", self.apply_ack)
        imgui.spacing()

        cancel_pressed = imgui.button("Cancel") or imgui.is_key_pressed(imgui.Key.gamepad_face_right)
        imgui.same_line()
        imgui.begin_disabled(not self.apply_ack)
        confirm_pressed = imgui.button("Apply Backup")
        imgui.end_disabled()

        if cancel_pressed:
            self.pending_apply_id = None
            imgui.close_current_popup()
        elif confirm_pressed and self.apply_ack:
            backup_id = self.pending_apply_id
            self.pending_apply_id = None
            imgui.close_current_popup()
            self.defer_action(
                "Applying backup... EmulationStation will restart.",
                lambda selected_backup=backup_id: self._apply_backup(selected_backup),
            )

        imgui.end_popup()


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
