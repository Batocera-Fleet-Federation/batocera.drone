"""Admin -> Backups: list existing config backups + trigger a new one.

Download/delete/apply/email aren't built here -- downloading needs
somewhere on a *second* device to save the file to (meaningless from the
console itself), and delete/apply are destructive actions better left to
the browser UI's confirmation flow. Read-only visibility plus "make a new
one" is the useful subset from a gamepad-only console screen.
"""

from imgui_bundle import imgui

from client import endpoints
from client.errors import DroneApiError

from ..theme import ERROR_COLOR, MUTED_COLOR, SUCCESS_COLOR, WARNING_COLOR
from .base import Screen

_LIST_HEIGHT = 320.0
_STATUS_COLUMN_X = 320.0


class BackupsScreen(Screen):
    def __init__(self, api_client):
        self.api_client = api_client
        self.backups = []
        self.error = None
        self.create_message = None

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

    def draw(self, navigator) -> None:
        if imgui.button("Create Backup"):
            self._create_backup()
        imgui.same_line()
        if imgui.button("Refresh"):
            self._reload()
        if self.create_message:
            imgui.same_line()
            imgui.text_disabled(self.create_message)
        imgui.spacing()

        if self.error:
            imgui.text_colored(ERROR_COLOR, self.error)
            return

        imgui.begin_child("backups_list", imgui.ImVec2(0, _LIST_HEIGHT), True)
        if not self.backups:
            imgui.text_disabled("No backups yet.")
        for backup in self.backups:
            self._draw_backup_row(backup)
        imgui.end_child()

    @staticmethod
    def _draw_backup_row(backup: dict) -> None:
        label = backup.get("name") or backup.get("file_name") or f"Backup #{backup.get('id')}"
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

        imgui.separator()


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"
