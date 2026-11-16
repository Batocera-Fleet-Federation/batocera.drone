"""RomRequestHandler backup routes, as a mixin (logic lives in ``backups/service.py``).

Admin (session; loopback stays pre-authenticated so the Ports client works):

* ``GET  /admin/backups``                                  -- this Drone's backups, snapshots, restore status
* ``GET  /admin/backups/categories``                       -- what a new backup could contain right now
* ``GET  /admin/backups/restore``                          -- restore job status
* ``GET  /admin/backups/sources``                          -- this Drone + paired Drones (online flag)
* ``GET  /admin/backups/swarm?drones=a,b&q=``              -- backups on this and the chosen online Drones
* ``GET  /admin/backups/swarm/<drone>/<backup>``           -- one backup's file list (any Drone)
* ``GET  /admin/backups/<id>`` / ``<id>/download``          -- local detail / archive download
* ``POST /admin/backups``                                  -- create {name, description, categories}
* ``POST /admin/backups/<id>/{rename,delete,plan,restore}``
* ``POST /admin/backups/swarm/<drone>/<backup>/download``  -- queue a pull from a paired Drone

Peer (pinned mTLS, HTTPS only): ``GET /peer/backups``, ``/peer/backups/<id>``,
``/peer/backups/<id>/archive``. Peers can list and download; nothing a peer sends can
start a restore.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote

try:
    from ..backups import service as _backups
except ImportError:  # pragma: no cover - direct script execution fallback
    from backups import service as _backups  # type: ignore

_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _get_download_manager():
    try:
        from ..drone_api import _get_download_manager as _impl
    except ImportError:  # pragma: no cover - direct script execution fallback
        from drone_api import _get_download_manager as _impl  # type: ignore
    return _impl()


def _segments(parts: list) -> list:
    # The router passes path segments still percent-encoded; the browser and the
    # Ports client encode Drone ids with encodeURIComponent/quote.
    return [unquote(str(part)) for part in parts]


def _first(query: dict, name: str) -> str:
    return str((query.get(name) or [""])[0]).strip()[:500]


class HandlersBackupsMixin:
    # ------------------------------------------------------------------ helpers

    def _backup_error(self, error: "_backups.BackupError") -> None:
        body = {"error": str(error)}
        body.update(getattr(error, "details", None) or {})
        self._send_json(int(getattr(error, "status", 400) or 400), body)

    def _backup_sources(self) -> list:
        online = self._media_online_peer_ids()
        return [
            {"drone_id": source.drone_id, "name": source.name, "is_local": source.is_local,
             "online": source.is_local or source.drone_id in online}
            for source in self._media_sources()
        ]

    def _backup_mutation_allowed(self) -> bool:
        if not self.settings.admin_enabled:
            self._send_json(403, {"error": "admin disabled"})
            return False
        origin_ok = getattr(self, "_multiplayer_origin_ok", None)
        if origin_ok is not None and not origin_ok():
            self._send_json(403, {"error": "Cross-origin backup requests are not allowed."})
            return False
        return True

    # -------------------------------------------------------------------- admin

    def _handle_admin_backups_get(self, parts: list, query: dict) -> None:
        settings = self.settings
        parts = _segments(parts)
        try:
            if not parts:
                self._send_json(200, _backups.list_local(settings))
            elif parts == ["categories"]:
                self._send_json(200, _backups.available_categories(settings))
            elif parts == ["restore"]:
                self._send_json(200, _backups.restore_status())
            elif parts == ["sources"]:
                self._send_json(200, {"sources": self._backup_sources()})
            elif parts == ["swarm"]:
                requested = {value for value in _first(query, "drones").split(",") if value}
                sources = [source for source in self._backup_sources()
                           if source["online"] and (not requested or source["drone_id"] in requested)]
                self._send_json(200, _backups.swarm_listing(settings, sources, _first(query, "q")))
            elif len(parts) == 3 and parts[0] == "swarm":
                drone_id, backup_id = parts[1], parts[2]
                if drone_id == str(settings.device_id):
                    self._send_json(200, _backups.detail(settings, backup_id))
                else:
                    self._send_json(200, _backups.peer_backup_detail(settings, drone_id, backup_id))
            elif len(parts) == 1:
                self._send_json(200, _backups.detail(settings, parts[0]))
            elif len(parts) == 2 and parts[1] == "download":
                row = _backups.get_local(settings, parts[0])
                path = _backups.archive_path(settings, parts[0])
                if not path.is_file():
                    raise _backups.BackupError("This backup's files are missing from disk.", 410)
                stem = _FILENAME_UNSAFE.sub("-", row.get("name") or "backup").strip("-")[:80] or "backup"
                self._stream_file(path, "application/gzip", extra_headers={
                    "Content-Disposition": f'attachment; filename="{stem}-{parts[0][:8]}.tar.gz"'})
            else:
                self._send_json(404, {"error": "not found"})
        except _backups.BackupError as error:
            self._backup_error(error)

    def _handle_admin_backups_post(self, parts: list, payload: Optional[dict]) -> None:
        if not self._backup_mutation_allowed():
            return
        payload = payload if isinstance(payload, dict) else {}
        settings = self.settings
        parts = _segments(parts)
        try:
            if not parts:
                row = _backups.create_backup(settings, name=payload.get("name") or "",
                                             description=payload.get("description") or "",
                                             categories=payload.get("categories"))
                self._send_json(202, {"status": "started", "backup": row})
            elif len(parts) == 4 and parts[0] == "swarm" and parts[3] == "download":
                if parts[1] == str(settings.device_id):
                    raise _backups.BackupError("This backup is already on this Drone.", 409)
                result = _backups.enqueue_download(settings, _get_download_manager(), parts[1], parts[2])
                self._send_json(202 if result["status"] == "queued" else 200, result)
            elif len(parts) == 2 and parts[1] == "delete":
                self._send_json(200, _backups.delete(settings, parts[0]))
            elif len(parts) == 2 and parts[1] == "rename":
                self._send_json(200, {"backup": _backups.rename(settings, parts[0], name=payload.get("name"),
                                                                description=payload.get("description"))})
            elif len(parts) == 2 and parts[1] in ("plan", "restore"):
                options = dict(categories=payload.get("categories") if isinstance(payload.get("categories"), list) else [],
                               mode=str(payload.get("mode") or "overlay"),
                               include_machine_keys=payload.get("include_machine_keys") is True,
                               paths=payload.get("paths") if isinstance(payload.get("paths"), list) else None)
                if parts[1] == "plan":
                    self._send_json(200, _backups.plan(settings, parts[0], **options))
                else:
                    status = _backups.start_restore(settings, parts[0], acknowledge_warnings=payload.get("acknowledge_warnings") is True,
                                                    **options)
                    self._send_json(202, status)
            else:
                self._send_json(404, {"error": "not found"})
        except _backups.BackupError as error:
            self._backup_error(error)

    # --------------------------------------------------------------------- peer

    def _handle_peer_backups(self, parts: list) -> None:
        parts = _segments(parts)
        if self.settings.http_only:
            self._send_json(403, {"error": "Backup sharing requires paired HTTPS."})
            return
        if not self._peer_request_authorized():
            return
        try:
            if not parts:
                self._send_json(200, _backups.peer_listing(self.settings))
            elif len(parts) == 1:
                self._send_json(200, _backups.peer_manifest(self.settings, parts[0]))
            elif len(parts) == 2 and parts[1] == "archive":
                path = _backups.peer_archive(self.settings, parts[0])
                self._stream_file(path, "application/gzip", as_attachment=True,
                                  upload_meta={"asset_type": "backups", "relative_path": parts[0]})
            else:
                self._send_json(404, {"error": "not found"})
        except _backups.BackupError as error:
            self._backup_error(error)
