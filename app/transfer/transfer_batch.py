"""Batch-level summaries of asset transfers for the notifications bell.

A batch groups the transfers one activity starts: a download selection or bulk
copy (``DownloadManager.transfer_batch``), or one peer's run of uploads
(``UploadBatches``). The bell gets a started entry and a finished entry per
batch instead of one entry per asset. Per-asset rows still go to the audit trail
(``notifications.record_audit_event``), so the email digest is unchanged.

``TransferBatch`` holds no lock of its own: its owner mutates it under the
owner's lock and emits the events it returns outside that lock.
"""

from __future__ import annotations

import uuid
from threading import Lock, Timer
from typing import Any, Optional

try:
    from ..device import notifications as _notifications
except ImportError:  # pragma: no cover - direct script execution fallback
    from device import notifications as _notifications  # type: ignore


# A finished entry lists at most this many failed names; the rest are counted.
MAX_LISTED_FAILURES = 5
# A started entry names at most this many systems; the rest are counted.
MAX_LISTED_SYSTEMS = 3
# An upload batch closes once nothing has been in flight for this long. A peer
# never says how many assets it will pull, so quiet time is the only boundary.
UPLOAD_BATCH_IDLE_SECONDS = 60.0


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _systems_text(systems: set) -> str:
    names = sorted(systems)
    listed = ", ".join(names[:MAX_LISTED_SYSTEMS])
    hidden = len(names) - MAX_LISTED_SYSTEMS
    return f"{listed} and {hidden} more" if hidden > 0 else listed


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class TransferBatch:
    def __init__(self, batch_id: str, direction: str, peer_label: str) -> None:
        self.batch_id = batch_id
        self.direction = direction  # "download" or "upload"
        self.peer_label = peer_label
        self.selected = 0
        self.total_bytes = 0
        self.systems: set = set()
        self.completed = 0
        self.completed_bytes = 0
        self.failed = 0
        self.cancelled = 0
        self.failed_names: list = []
        self.sealed = False
        self.started = False
        self.finished = False
        self._recorded: set = set()

    @property
    def open_count(self) -> int:
        return self.selected - self.completed - self.failed - self.cancelled

    def add_selected(self, *, system: Any = None, total_bytes: Any = None) -> None:
        self.selected += 1
        self.total_bytes += _as_int(total_bytes)
        if system:
            self.systems.add(str(system))

    def record_outcome(self, item_id: str, *, status: Any, name: Any, size_bytes: Any = None, system: Any = None) -> bool:
        """Count one item's terminal status once. Returns False for a repeat."""
        if item_id in self._recorded:
            return False
        self._recorded.add(item_id)
        if system:
            self.systems.add(str(system))
        if status == "completed":
            self.completed += 1
            self.completed_bytes += _as_int(size_bytes)
        elif status == "failed":
            self.failed += 1
            if len(self.failed_names) < MAX_LISTED_FAILURES:
                self.failed_names.append(str(name or "unnamed item"))
        else:
            self.cancelled += 1
        return True

    @property
    def _verb(self) -> str:
        return "Upload" if self.direction == "upload" else "Download"

    def started_event(self) -> Optional[dict]:
        """The started entry, or None when a download selected nothing."""
        if self.direction == "upload":
            return {
                "event_type": "asset_transfer_started",
                "title": "Asset upload started",
                "message": f"Serving assets to {self.peer_label}.",
                "details": {"direction": self.direction, "peer": self.peer_label, "batch_id": self.batch_id},
            }
        if not self.selected:
            return None
        noun = "asset" if self.selected == 1 else "assets"
        parts = []
        if self.systems:
            parts.append(_systems_text(self.systems))
        if self.total_bytes:
            parts.append(format_size(self.total_bytes))
        detail = f" ({', '.join(parts)})" if parts else ""
        return {
            "event_type": "asset_transfer_started",
            "title": "Asset download started",
            "message": f"Downloading {self.selected} {noun} from {self.peer_label}{detail}.",
            "details": {
                "direction": self.direction,
                "peer": self.peer_label,
                "batch_id": self.batch_id,
                "selected": self.selected,
                "total_bytes": self.total_bytes,
                "systems": sorted(self.systems),
            },
        }

    def finished_event(self) -> dict:
        counts = f"{self.completed} completed, {self.failed} failed"
        if self.cancelled:
            counts += f", {self.cancelled} cancelled"
        size = f" ({format_size(self.completed_bytes)})" if self.completed_bytes else ""
        message = f"{self._verb} finished: {counts}{size}."
        if self.failed_names:
            listed = ", ".join(self.failed_names)
            hidden = self.failed - len(self.failed_names)
            if hidden > 0:
                listed += f", and {hidden} more"
            message += f" Failed: {listed}."
        return {
            "event_type": "asset_transfer_finished",
            "title": f"Asset {self._verb.lower()} finished",
            "message": message,
            "details": {
                "direction": self.direction,
                "peer": self.peer_label,
                "batch_id": self.batch_id,
                "selected": self.selected,
                "completed": self.completed,
                "failed": self.failed,
                "cancelled": self.cancelled,
                "completed_bytes": self.completed_bytes,
                "failed_names": list(self.failed_names),
            },
        }


class _OpenUploadBatch:
    def __init__(self, settings: Any, batch: TransferBatch) -> None:
        self.settings = settings
        self.batch = batch
        self.generation = 0
        self.timer: Optional[Timer] = None


class UploadBatches:
    """Groups one peer's uploads into batches.

    A batch opens with the peer's first upload and stays open while the peer
    keeps requesting. It closes once nothing is in flight for ``idle_seconds``.
    Each completed or failed upload is counted in its batch's finished entry.
    """

    def __init__(self, idle_seconds: float = UPLOAD_BATCH_IDLE_SECONDS) -> None:
        self._idle_seconds = idle_seconds
        self._lock = Lock()
        self._open: dict = {}

    def begin(self, settings: Any, *, peer_key: str, peer_label: str, system: Any = None) -> tuple:
        """Count one upload as started. Returns the token ``finish`` needs."""
        item_id = str(uuid.uuid4())
        started = None
        with self._lock:
            entry = self._open.get(peer_key)
            if entry is None:
                entry = _OpenUploadBatch(settings, TransferBatch(str(uuid.uuid4()), "upload", peer_label))
                self._open[peer_key] = entry
                started = entry.batch.started_event()
                entry.batch.started = True
            entry.generation += 1
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None
            entry.batch.add_selected(system=system)
        if started:
            self._emit(settings, started)
        return (peer_key, item_id)

    def finish(self, token: tuple, *, status: str, name: Any, size_bytes: Any = None, system: Any = None) -> None:
        peer_key, item_id = token
        with self._lock:
            entry = self._open.get(peer_key)
            if entry is None or not entry.batch.record_outcome(
                item_id, status=status, name=name, size_bytes=size_bytes, system=system
            ):
                return
            if entry.batch.open_count > 0:
                return
            entry.generation += 1
            timer = Timer(self._idle_seconds, self._close, args=(peer_key, entry, entry.generation))
            timer.daemon = True
            entry.timer = timer
            timer.start()

    def _close(self, peer_key: str, entry: _OpenUploadBatch, generation: int) -> None:
        with self._lock:
            if self._open.get(peer_key) is not entry or entry.generation != generation:
                return
            if entry.batch.open_count > 0:
                return
            self._open.pop(peer_key, None)
            entry.timer = None
            entry.batch.sealed = True
            entry.batch.finished = True
            finished = entry.batch.finished_event()
        self._emit(entry.settings, finished)

    @staticmethod
    def _emit(settings: Any, event: dict) -> None:
        _notifications.record_event(
            settings, event["event_type"], event["title"], event["message"], details=event["details"]
        )


_UPLOAD_BATCHES: Optional[UploadBatches] = None
_UPLOAD_BATCHES_LOCK = Lock()


def get_upload_batches() -> UploadBatches:
    global _UPLOAD_BATCHES
    if _UPLOAD_BATCHES is None:
        with _UPLOAD_BATCHES_LOCK:
            if _UPLOAD_BATCHES is None:
                _UPLOAD_BATCHES = UploadBatches()
    return _UPLOAD_BATCHES
