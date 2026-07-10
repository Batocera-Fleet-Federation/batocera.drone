"""Download manager: the single-active-download queue + transport-tier dispatch.

Extracted from ``drone_api.py``. ``DownloadManager`` owns the queue/worker, progress,
cancel/resume, and drives ``TransportSelector`` over the tiers (LAN-direct →
direct-public → relay). ``_directpublic_fetch`` is the direct-public tier callback
(wraps ``peer_download``'s ``_download_*_from_peer``). The running singleton itself
(``_DOWNLOAD_MANAGER`` / ``_get_download_manager``) stays in ``drone_api``.
"""

import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict, List, Optional

try:
    from ..common.settings import Settings
    from ..storage import saves_store as _saves_store
    from ..transport import DirectPublicTransport, DownloadRequest, TransferContext, TransportSelector
    from ..transport import relay_transfer as _relay_transfer
    from ..transport.lan import LanDirectTransport
    from . import local_network as _local_network
    from .download_errors import DownloadCancelled
    from .edge_relay import _edge_mux_available, _local_network_snapshot, _relay_fetch
    from .peer_download import (
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from transport import DirectPublicTransport, DownloadRequest, TransferContext, TransportSelector  # type: ignore
    from transport import relay_transfer as _relay_transfer  # type: ignore
    from transport.lan import LanDirectTransport  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.download_errors import DownloadCancelled  # type: ignore
    from transfer.edge_relay import _edge_mux_available, _local_network_snapshot, _relay_fetch  # type: ignore
    from transfer.peer_download import (  # type: ignore
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
    )

DOWNLOAD_PROGRESS_PUSH_SECONDS = float(os.environ.get("DOWNLOAD_PROGRESS_PUSH_SECONDS", "5"))
DOWNLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "skipped"}


def _kick_asset_metadata_sync_after_download(*args, **kwargs):
    """Delegate to the drone_api impl (overmind sync orchestration stays there)."""
    try:
        from ..drone_api import _kick_asset_metadata_sync_after_download as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _kick_asset_metadata_sync_after_download as _impl  # type: ignore
    return _impl(*args, **kwargs)


def _directpublic_fetch(request: "DownloadRequest", context: "TransferContext") -> dict:
    """Run the legacy direct mTLS peer download for one asset.

    This is the body of the DirectPublic transport: it dispatches on
    ``asset_type`` to the appropriate ``_download_*_from_peer`` helper (defined
    later in this module) and returns its activity dict unchanged. Injected into
    :class:`DirectPublicTransport` so the transport package needs no dependency
    on this module.
    """
    settings = context.settings
    config = context.config
    peer = context.peer
    progress = context.progress_callback
    cancel_event = context.cancellation_event
    asset_type = request.asset_type
    system = request.system
    rel = request.relative_path
    expected_size = request.expected_size
    expected_fingerprint = request.expected_fingerprint
    if asset_type == "artwork":
        return _download_artwork_from_peer(
            settings,
            context.repository,
            config,
            peer,
            system,
            request.rom_path,
            request.artwork_type,
            progress_callback=progress,
            cancellation_event=cancel_event,
            overwrite=request.overwrite,
            local_rom_path=request.local_rom_path,
        )
    if asset_type == "saves":
        return _download_save_from_peer(
            settings,
            config,
            peer,
            system,
            rel,
            expected_size=expected_size,
            expected_fingerprint=expected_fingerprint,
            cancellation_event=cancel_event,
        )
    if asset_type == "bios":
        return _download_bios_from_peer(
            settings,
            config,
            peer,
            rel,
            expected_size=expected_size,
            expected_md5=expected_fingerprint,
            progress_callback=progress,
            cancellation_event=cancel_event,
        )
    if request.entry_type == "folder":
        return _download_rom_folder_from_peer(
            settings,
            config,
            peer,
            system,
            rel,
            expected_size=expected_size,
            expected_fingerprint=expected_fingerprint,
            marker_relative_path=request.marker_relative_path,
            progress_callback=progress,
            cancellation_event=cancel_event,
        )
    return _download_rom_from_peer(
        settings,
        config,
        peer,
        system,
        rel,
        expected_size=expected_size,
        expected_fingerprint=expected_fingerprint,
        progress_callback=progress,
        cancellation_event=cancel_event,
    )


class DownloadManager:
    """Per-target Drone download queue with a small pool of worker threads.

    Running a few transfers concurrently markedly improves aggregate throughput
    over Wi-Fi (where a single TCP stream rarely fills the link) and hides the
    per-file TLS-handshake latency when copying many small files (artwork). The
    pool size is ``DRONE_DOWNLOAD_CONCURRENCY`` (default 3, clamped 1-8)."""

    def __init__(self, settings: Settings, repository: "RomRepository") -> None:
        self.settings = settings
        self.repository = repository
        # Transport seam, in priority order: LAN-direct (same-network peers) ->
        # public-direct (the legacy _download_*_from_peer path) -> relay (when the
        # Edge mux is enabled). The selector tries each usable tier and falls back
        # to the next on failure.
        transports = [
            LanDirectTransport(_directpublic_fetch, local_network=_local_network_snapshot),
            DirectPublicTransport(_directpublic_fetch),
        ]
        if settings.edge_enabled:
            transports.append(
                _relay_transfer.RelayReceiverTransport(_relay_fetch, is_available=_edge_mux_available)
            )
        self._selector = TransportSelector(transports)
        self._lock = Lock()
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._cancel_events: Dict[str, Event] = {}
        self._wake = Event()
        self._paused = False
        self._last_download_state_push_at = 0.0
        self._concurrency = self._resolve_concurrency()
        # Job selection + the queued->downloading transition happen atomically under
        # self._lock, so multiple workers never claim the same job. Per-job state is
        # likewise mutated under the lock, so running _run_job concurrently is safe.
        self._threads = []
        for index in range(self._concurrency):
            thread = Thread(target=self._worker, name=f"drone-download-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    @staticmethod
    def _resolve_concurrency() -> int:
        try:
            value = int(os.environ.get("DRONE_DOWNLOAD_CONCURRENCY", "3"))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(value, 8))

    def enqueue_rom(self, config: dict, peer: dict, system: str, relative_path: str, expected_size=None, expected_fingerprint=None, source_action_id: Optional[str] = None, entry_type: str = "file", sync_id: Optional[str] = None, artwork_types=None, marker_relative_path: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": sync_id or job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "ROM",
            "entry_type": entry_type,
            "system": system,
            "rom_name": relative_path,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_fingerprint,
            "_entry_type": entry_type,
            "_marker_relative_path": str(marker_relative_path or "").strip() or None,
            "_artwork_types": [str(value).strip() for value in (artwork_types or []) if str(value).strip()],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_bios(self, config: dict, peer: dict, relative_path: str, expected_size=None, expected_md5=None, source_action_id: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "bios",
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "BIOS",
            "system": "bios",
            "bios_name": relative_path,
            "rom_name": relative_path,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "bios_md5": expected_md5,
            "_asset_type": "bios",
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_md5,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_artwork(self, config: dict, peer: dict, system: str, rom_path: str, artwork_type: str, source_action_id: Optional[str] = None, overwrite: bool = False, local_rom_path: Optional[str] = None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        label = f"{rom_path}:{artwork_type}"
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_action_id": source_action_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "artwork",
            "file_path": label,
            "file_name": Path(rom_path).name,
            "file_type": "ARTWORK",
            "system": system,
            "rom_name": rom_path,
            "rom_path": rom_path,
            "artwork_type": artwork_type,
            "relative_path": label,
            "total_bytes": None,
            "file_size": None,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_asset_type": "artwork",
            "_config": config,
            "_peer": peer,
            "_overwrite": bool(overwrite),
            "_local_rom_path": local_rom_path,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_save(self, config: dict, peer: dict, system: str, relative_path: str, expected_size=None, expected_fingerprint=None) -> dict:
        job_id = str(uuid.uuid4())
        peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": job_id,
            "source_drone_id": peer_id,
            "target_drone_id": self.settings.overmind_device_id,
            "asset_type": "saves",
            "file_path": relative_path,
            "file_name": Path(relative_path).name,
            "file_type": "Save",
            "system": system,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "queued",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": None,
            "cancellation_requested": False,
            "created_at": now,
            "_asset_type": "saves",
            "_config": config,
            "_peer": peer,
            "_expected_fingerprint": expected_fingerprint,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def cancel(self, job_id: str, reason: str = "cancelled by user") -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            if job.get("status") in DOWNLOAD_TERMINAL_STATUSES:
                return {"status": job.get("status"), "job": self._public_job_locked(job)}
            job["cancellation_requested"] = True
            job["cancel_reason"] = reason
            event = self._cancel_events.get(job_id)
            if event:
                event.set()
            if job.get("status") == "queued":
                job["status"] = "cancelled"
                job["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                job["download_completed_at"] = job["completed_at"]
                job["failure_reason"] = reason
                job["error_message"] = reason
                self._update_queue_positions_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return {"status": snapshot.get("status"), "job": snapshot}

    def retry(self, job_id: str) -> dict:
        with self._lock:
            original = self._jobs.get(job_id)
            if not original:
                return {"status": "not_found", "job_id": job_id}
            if original.get("status") not in {"failed", "cancelled"}:
                return {"status": "not_retryable", "job_id": job_id, "job": self._public_job_locked(original)}
            retry_job = {key: value for key, value in original.items() if key not in {"id", "job_id", "sync_id"}}
            retry_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            retry_job.update({
                "id": retry_id,
                "job_id": retry_id,
                "sync_id": retry_id,
                "status": "queued",
                "queue_position": None,
                "downloaded_bytes": 0,
                "bytes_transferred": 0,
                "percentage": 0,
                "transfer_speed_bps": 0,
                "started_at": None,
                "download_started_at": None,
                "completed_at": None,
                "download_completed_at": None,
                "error_message": None,
                "failure_reason": None,
                "cancellation_requested": False,
                "cancel_reason": None,
                "retried_from_job_id": job_id,
                "created_at": now,
            })
            retry_job.pop("_started_mono", None)
            self._jobs[retry_id] = retry_job
            self._cancel_events[retry_id] = Event()
            self._update_queue_positions_locked()
            snapshot = self._public_job_locked(retry_job)
        self._wake.set()
        return {"status": "queued", "job": snapshot, "retried_from_job_id": job_id}

    def find_pending_rom(self, system: str, relative_path: str, expected_fingerprint: Optional[str] = None) -> Optional[dict]:
        """Return an active/queued ROM job for the same local target or fingerprint."""
        system_norm = str(system or "").strip().lower()
        path_norm = str(relative_path or "").replace("\\", "/").strip().lstrip("./").lower()
        fp_norm = str(expected_fingerprint or "").strip().lower()
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") not in {"queued", "downloading"}:
                    continue
                if str(job.get("file_type") or "").upper() != "ROM":
                    continue
                if str(job.get("system") or "").strip().lower() != system_norm:
                    continue
                job_path = str(job.get("relative_path") or job.get("file_path") or "").replace("\\", "/").strip().lstrip("./").lower()
                job_fp = str(job.get("_expected_fingerprint") or "").strip().lower()
                if path_norm and job_path == path_norm:
                    return self._public_job_locked(job)
                if fp_norm and job_fp == fp_norm:
                    return self._public_job_locked(job)
        return None

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [self._public_job_locked(job) for job in self._jobs.values()]
            paused = self._paused
        active = [job for job in jobs if job.get("status") == "downloading"]
        queued = [job for job in jobs if job.get("status") == "queued"]
        recent = [job for job in jobs if job.get("status") in DOWNLOAD_TERMINAL_STATUSES][-25:]
        estimate = self._queue_estimate(active, queued, recent, paused, self._concurrency)
        return {
            "target_drone_id": self.settings.overmind_device_id,
            "concurrency": {"scope": "target_drone", "active_limit": self._concurrency},
            "paused": paused,
            "active": active,
            "queued": queued,
            "recent": list(reversed(recent)),
            "downloads": active + queued + list(reversed(recent)),
            **estimate,
        }

    @staticmethod
    def _queue_estimate(active: List[dict], queued: List[dict], recent: List[dict], paused: bool, concurrency: int = 1) -> dict:
        def safe_int(value: object) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        pending = active + queued
        known_remaining = 0
        unknown_size_count = 0
        known_sizes = []
        for job in pending:
            total = safe_int(job.get("total_bytes") or job.get("file_size"))
            downloaded = safe_int(job.get("downloaded_bytes") or job.get("bytes_transferred"))
            if total > 0:
                known_sizes.append(total)
                known_remaining += max(0, total - downloaded)
            else:
                unknown_size_count += 1

        if not known_sizes:
            known_sizes = [
                safe_int(job.get("total_bytes") or job.get("file_size"))
                for job in recent
                if safe_int(job.get("total_bytes") or job.get("file_size")) > 0
            ]
        average_size = int(sum(known_sizes) / len(known_sizes)) if known_sizes else 0
        estimated_unknown_bytes = average_size * unknown_size_count
        size_estimate_available = unknown_size_count == 0 or average_size > 0
        remaining_bytes = known_remaining + estimated_unknown_bytes if size_estimate_available else None

        parallel = max(1, int(concurrency or 1))
        speeds = [safe_int(job.get("transfer_speed_bps")) for job in active if safe_int(job.get("transfer_speed_bps")) > 0]
        speed_source = "active"
        if speeds:
            # Aggregate throughput across the concurrently-running streams is what
            # actually drains the queue.
            speed_bps = sum(speeds)
        else:
            recent_speeds = [
                safe_int(job.get("transfer_speed_bps"))
                for job in recent
                if job.get("status") == "completed" and safe_int(job.get("transfer_speed_bps")) > 0
            ]
            speed_source = "recent"
            # Project a single stream's typical speed across however many transfers
            # will run in parallel (bounded by the pool size and the work pending).
            per_stream = int(sum(recent_speeds) / len(recent_speeds)) if recent_speeds else 0
            speed_bps = per_stream * (min(parallel, len(pending)) if pending else 1)
        eta_seconds = int(remaining_bytes / speed_bps) if remaining_bytes is not None and remaining_bytes > 0 and speed_bps > 0 else None
        return {
            "queue_eta_seconds": eta_seconds,
            "queue_remaining_bytes": remaining_bytes,
            "queue_known_remaining_bytes": known_remaining,
            "queue_estimated_unknown_bytes": estimated_unknown_bytes,
            "queue_unknown_size_count": unknown_size_count,
            "queue_size_estimate_available": size_estimate_available,
            "queue_estimate_speed_bps": speed_bps,
            "queue_estimate_speed_source": speed_source if speed_bps else None,
            "queue_eta_state": "paused" if paused and pending else ("calculating" if pending and eta_seconds is None else "ready"),
        }

    def pause(self) -> dict:
        """Stop the worker from starting any further queued downloads. A job that
        is already downloading runs to completion (cancel it individually to stop
        it sooner)."""
        with self._lock:
            self._paused = True
        return self.snapshot()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
        self._wake.set()
        return self.snapshot()

    def clear_queue(self) -> dict:
        """Cancel every still-queued job so nothing further downloads. The active
        job (if any) is left running."""
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        cleared = 0
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") == "queued":
                    job["status"] = "cancelled"
                    job["failure_reason"] = "queue cleared by user"
                    job["error_message"] = job["failure_reason"]
                    job["cancellation_requested"] = True
                    job["completed_at"] = now
                    job["download_completed_at"] = now
                    event = self._cancel_events.get(job.get("job_id"))
                    if event:
                        event.set()
                    cleared += 1
            self._update_queue_positions_locked()
        result = self.snapshot()
        result["cleared"] = cleared
        return result

    def _public_job_locked(self, job: dict) -> dict:
        public = {key: value for key, value in job.items() if not key.startswith("_")}
        downloaded = int(public.get("downloaded_bytes") or 0)
        total = public.get("total_bytes") or public.get("file_size")
        try:
            total_int = int(total)
        except Exception:
            total_int = 0
        public["total_bytes"] = total_int or None
        public["file_size"] = total_int or public.get("file_size")
        public["downloaded_bytes"] = downloaded
        public["bytes_transferred"] = downloaded
        public["percentage"] = round((downloaded / total_int) * 100, 1) if total_int else 0
        return public

    def _update_queue_positions_locked(self) -> None:
        position = 1
        for job in self._jobs.values():
            if job.get("status") == "queued":
                job["queue_position"] = position
                position += 1
            else:
                job["queue_position"] = None

    def _worker(self) -> None:
        while True:
            job_id = None
            with self._lock:
                if not self._paused:
                    for candidate_id, candidate in self._jobs.items():
                        if candidate.get("status") == "queued":
                            job_id = candidate_id
                            break
                if job_id:
                    job = self._jobs[job_id]
                    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    job["status"] = "downloading"
                    job["started_at"] = now
                    job["download_started_at"] = now
                    job["_started_mono"] = time.monotonic()
                    self._update_queue_positions_locked()
            if not job_id:
                self._wake.wait(1)
                self._wake.clear()
                continue
            self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            config = job.get("_config") or {}
            peer = job.get("_peer") or {}
            system = str(job.get("system") or "")
            rel = str(job.get("relative_path") or job.get("file_path") or "")
            rom_path = str(job.get("rom_path") or job.get("rom_name") or rel)
            artwork_type = str(job.get("artwork_type") or "")
            artwork_overwrite = bool(job.get("_overwrite"))
            artwork_local_rom_path = job.get("_local_rom_path")
            expected_size = job.get("file_size") or job.get("total_bytes")
            expected_fingerprint = job.get("_expected_fingerprint")
            entry_type = str(job.get("_entry_type") or job.get("entry_type") or "file").lower()
            marker_relative_path = job.get("_marker_relative_path")
            cancel_event = self._cancel_events.get(job_id) or Event()
            asset_type = str(job.get("_asset_type") or "rom").lower()
            rom_artwork_types = list(job.get("_artwork_types") or [])
        self._push_download_state(config, "started", force=True)

        def progress(downloaded: int, total: Optional[int]) -> None:
            should_push = False
            with self._lock:
                current = self._jobs.get(job_id)
                if not current:
                    return
                current["downloaded_bytes"] = downloaded
                current["bytes_transferred"] = downloaded
                if total:
                    current["total_bytes"] = total
                    current["file_size"] = total
                started = float(current.get("_started_mono") or time.monotonic())
                elapsed = max(0.001, time.monotonic() - started)
                current["transfer_speed_bps"] = int(downloaded / elapsed)
                now = time.monotonic()
                if now - self._last_download_state_push_at >= DOWNLOAD_PROGRESS_PUSH_SECONDS:
                    self._last_download_state_push_at = now
                    should_push = True
            if should_push:
                self._push_download_state(config, "progress", force=True)

        try:
            request = DownloadRequest(
                asset_type=asset_type,
                system=system,
                relative_path=rel,
                rom_path=rom_path,
                artwork_type=artwork_type,
                entry_type=entry_type,
                expected_size=expected_size,
                expected_fingerprint=expected_fingerprint,
                overwrite=artwork_overwrite,
                local_rom_path=artwork_local_rom_path,
                marker_relative_path=marker_relative_path,
            )
            context = TransferContext(
                settings=self.settings,
                repository=self.repository,
                config=config,
                peer=peer,
                progress_callback=progress,
                cancellation_event=cancel_event,
            )
            activity = self._selector.fetch(request, context)
            refresh_started = time.monotonic()
            try:
                refreshed = (
                    self.repository.list_artwork_metadata()
                    if asset_type == "artwork"
                    else (
                        self.repository.list_bios_entries()
                        if asset_type == "bios"
                        else (_saves_store.list_saves(self.settings.saves_root, system=system or None) if asset_type == "saves" else self.repository.list_assets(system, "roms")[1])
                    )
                )
                activity["inventory_refresh_status"] = "succeeded"
                activity["inventory_refresh_count"] = len(refreshed)
            except Exception as refresh_error:
                activity["inventory_refresh_status"] = "failed"
                activity["inventory_refresh_error"] = str(refresh_error)
            activity["inventory_refresh_duration_ms"] = int((time.monotonic() - refresh_started) * 1000)
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current.update(activity)
                    current["id"] = job_id
                    current["job_id"] = job_id
                    current["sync_id"] = job_id
        except DownloadCancelled as error:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["status"] = "cancelled"
                    current["failure_reason"] = str(error) or current.get("cancel_reason") or "cancelled"
                    current["error_message"] = current["failure_reason"]
                    current["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["download_completed_at"] = current["completed_at"]
        except Exception as error:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    current["status"] = "failed"
                    current["failure_reason"] = str(error)
                    current["error_message"] = str(error)
                    current["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["download_completed_at"] = current["completed_at"]
        finally:
            terminal_activity = None
            with self._lock:
                current = self._jobs.get(job_id)
                if current and current.get("status") in DOWNLOAD_TERMINAL_STATUSES:
                    terminal_activity = self._public_job_locked(current)
                self._update_queue_positions_locked()
            self._push_download_state(config, "completed", force=True)
            if terminal_activity:
                if _local_network.is_local_mode(self.settings):
                    _local_network.record_activity(self.settings, terminal_activity)
                _post_rom_sync_activity(self.settings, config, terminal_activity)
                if asset_type == "rom" and terminal_activity.get("status") == "completed":
                    _kick_asset_metadata_sync_after_download(self.settings, self.repository, config, "rom_download_completed")
                    # Receiver-driven artwork: pull the game's artwork from the same
                    # peer (Overmind no longer carries artwork to queue sync_artwork).
                    # Artwork is keyed by the gamelist <path>, so folder-unit ROMs
                    # look it up by the marker file, not the transferred folder.
                    completed_rel = str(marker_relative_path or terminal_activity.get("relative_path") or rel or "")
                    if completed_rel and rom_artwork_types:
                        for field in rom_artwork_types:
                            try:
                                self.enqueue_artwork(
                                    config, peer, system, completed_rel, field,
                                    source_action_id=terminal_activity.get("source_action_id"),
                                    local_rom_path=completed_rel,
                                )
                            except Exception:
                                # Best-effort: artwork is a nice-to-have on top of the ROM.
                                pass

    def _push_download_state(self, config: dict, reason: str, force: bool = False) -> None:
        if not force:
            now = time.monotonic()
            with self._lock:
                if now - self._last_download_state_push_at < DOWNLOAD_PROGRESS_PUSH_SECONDS:
                    return
                self._last_download_state_push_at = now
        _post_download_state(self.settings, config, self.snapshot(), reason=reason)
