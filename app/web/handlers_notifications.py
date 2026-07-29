"""RomRequestHandler notifications-inbox handlers, as a mixin.

The bell-icon dropdown's backend: a paginated list + unread count, mark one
(or all) read, dismiss one, and clear (delete). Composed onto
``RomRequestHandler``. See ``storage/audit_store.py`` for the underlying
SQLite tables -- this mixin is a thin pass-through, same shape as
``handlers_torrents.py``/``handlers_vpn.py``.
"""

try:
    from ..storage import audit_store as _audit_store
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import audit_store as _audit_store  # type: ignore


class HandlersNotificationsMixin:
    def _handle_admin_notifications_list(self, query_params: dict) -> None:
        before_id_raw = query_params.get("before_id", [None])[0]
        limit_raw = query_params.get("limit", [str(_audit_store.DEFAULT_PAGE_LIMIT)])[0]
        unread_only = str(query_params.get("unread_only", ["0"])[0]).strip().lower() in ("1", "true", "yes", "on")
        try:
            before_id = int(before_id_raw) if before_id_raw not in (None, "") else None
        except (TypeError, ValueError):
            before_id = None
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = _audit_store.DEFAULT_PAGE_LIMIT
        result = _audit_store.list_notifications_page(
            self.settings, before_id=before_id, limit=limit, unread_only=unread_only
        )
        self._send_json(200, result)

    def _handle_admin_notifications_unread_count(self) -> None:
        self._send_json(200, {"unread_count": _audit_store.unread_notification_count(self.settings)})

    def _handle_admin_notification_read(self, notification_id: str) -> None:
        try:
            found = _audit_store.mark_notification_read(self.settings, int(notification_id))
        except (TypeError, ValueError):
            found = False
        self._send_json(200 if found else 404, {"status": "ok" if found else "not_found"})

    def _handle_admin_notifications_read_all(self) -> None:
        count = _audit_store.mark_all_notifications_read(self.settings)
        self._send_json(200, {"status": "ok", "marked_read": count})

    def _handle_admin_notification_dismiss(self, notification_id: str) -> None:
        try:
            found = _audit_store.delete_notification(self.settings, int(notification_id))
        except (TypeError, ValueError):
            found = False
        self._send_json(200 if found else 404, {"status": "ok" if found else "not_found"})

    def _handle_admin_notifications_clear(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        count = _audit_store.clear_notifications(self.settings, only_read=bool(payload.get("only_read")))
        self._send_json(200, {"status": "ok", "cleared": count})
