"""Upload activity tracker: what this Drone is currently serving to peers.

A small counterpart to DownloadManager, kept separate because serving is purely
reactive -- there is no queue, retry, or worker pool here, just a record of
in-flight and recently-finished sends. Instrumented into the mTLS ``/peer/*``
handlers (``web/handlers_peer.py``). Lazily-created process-wide singleton via
:func:`get_upload_tracker` -- unlike DownloadManager it needs no settings or
repository, so it doesn't need bootstrap wiring in ``drone_api.py``.
"""

import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Optional

UPLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
UPLOAD_RECENT_LIMIT = 25


class UploadTracker:
    def __init__(self) -> None:
        self._lock = Lock()
        self._uploads: "OrderedDict[str, dict]" = OrderedDict()

    def start(
        self,
        *,
        peer_device_id: Optional[str],
        asset_type: str,
        relative_path: str,
        system: Optional[str] = None,
        transport: str = "direct",
        total_bytes: Optional[int] = None,
    ) -> str:
        upload_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            self._uploads[upload_id] = {
                "id": upload_id,
                "upload_id": upload_id,
                "peer_device_id": peer_device_id or None,
                "asset_type": asset_type,
                "system": system,
                "relative_path": relative_path,
                "file_path": relative_path,
                "file_name": Path(relative_path).name if relative_path else "",
                "transport": transport,
                "status": "uploading",
                "total_bytes": total_bytes,
                "file_size": total_bytes,
                "bytes_transferred": 0,
                "percentage": 0,
                "transfer_speed_bps": 0,
                "started_at": now,
                "completed_at": None,
                "error_message": None,
                "_started_mono": time.monotonic(),
            }
            self._trim_locked()
        return upload_id

    def progress(self, upload_id: str, bytes_sent: int, total_bytes: Optional[int] = None) -> None:
        with self._lock:
            entry = self._uploads.get(upload_id)
            if not entry:
                return
            entry["bytes_transferred"] = bytes_sent
            if total_bytes:
                entry["total_bytes"] = total_bytes
                entry["file_size"] = total_bytes
            total = entry.get("total_bytes")
            entry["percentage"] = round((bytes_sent / total) * 100, 1) if total else 0
            elapsed = max(0.001, time.monotonic() - float(entry.get("_started_mono") or time.monotonic()))
            entry["transfer_speed_bps"] = int(bytes_sent / elapsed)

    def finish(self, upload_id: str, status: str = "completed", error: Optional[str] = None) -> None:
        with self._lock:
            entry = self._uploads.get(upload_id)
            if not entry:
                return
            entry["status"] = status if status in UPLOAD_TERMINAL_STATUSES else "completed"
            entry["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            entry["error_message"] = error
            entry.pop("_started_mono", None)
            self._trim_locked()

    def _trim_locked(self) -> None:
        terminal_ids = [uid for uid, entry in self._uploads.items() if entry.get("status") in UPLOAD_TERMINAL_STATUSES]
        overflow = len(terminal_ids) - UPLOAD_RECENT_LIMIT
        for uid in terminal_ids[:max(0, overflow)]:
            self._uploads.pop(uid, None)

    def snapshot(self) -> dict:
        with self._lock:
            entries = [
                {key: value for key, value in entry.items() if not key.startswith("_")}
                for entry in self._uploads.values()
            ]
        active = [entry for entry in entries if entry.get("status") == "uploading"]
        recent = [entry for entry in entries if entry.get("status") in UPLOAD_TERMINAL_STATUSES][-UPLOAD_RECENT_LIMIT:]
        return {"active": active, "recent": list(reversed(recent))}


_UPLOAD_TRACKER: Optional[UploadTracker] = None
_UPLOAD_TRACKER_LOCK = Lock()


def get_upload_tracker() -> UploadTracker:
    global _UPLOAD_TRACKER
    if _UPLOAD_TRACKER is None:
        with _UPLOAD_TRACKER_LOCK:
            if _UPLOAD_TRACKER is None:
                _UPLOAD_TRACKER = UploadTracker()
    return _UPLOAD_TRACKER
