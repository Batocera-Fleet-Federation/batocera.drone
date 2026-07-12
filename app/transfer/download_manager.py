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
from urllib.error import HTTPError

try:
    from ..common.settings import Settings
    from ..storage import saves_store as _saves_store
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..transport import DirectPublicTransport, DownloadRequest, TransferContext, TransportSelector
    from ..transport import relay_transfer as _relay_transfer
    from ..transport.lan import LanDirectTransport
    from . import local_network as _local_network
    from .download_errors import DownloadCancelled
    from .edge_relay import _edge_mux_available, _local_network_snapshot, _relay_fetch
    from .peer_download import (
        _best_peer_for_bios,
        _best_peer_for_rom,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
        _resolve_rom_by_gamelist_id_from_peer,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage import saves_store as _saves_store  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from transport import DirectPublicTransport, DownloadRequest, TransferContext, TransportSelector  # type: ignore
    from transport import relay_transfer as _relay_transfer  # type: ignore
    from transport.lan import LanDirectTransport  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.download_errors import DownloadCancelled  # type: ignore
    from transfer.edge_relay import _edge_mux_available, _local_network_snapshot, _relay_fetch  # type: ignore
    from transfer.peer_download import (  # type: ignore
        _best_peer_for_bios,
        _best_peer_for_rom,
        _download_artwork_from_peer,
        _download_bios_from_peer,
        _download_rom_folder_from_peer,
        _download_rom_from_peer,
        _download_save_from_peer,
        _post_download_state,
        _post_rom_sync_activity,
        _resolve_rom_by_gamelist_id_from_peer,
    )

DOWNLOAD_PROGRESS_PUSH_SECONDS = float(os.environ.get("DOWNLOAD_PROGRESS_PUSH_SECONDS", "5"))
DOWNLOAD_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "skipped"}
DOWNLOAD_QUEUE_STATE_NAMESPACE = "download_manager.json"
DOWNLOAD_RECONNECT_INITIAL_SECONDS = 5
DOWNLOAD_RECONNECT_MAX_SECONDS = 60
DOWNLOAD_PERSISTED_RECENT_LIMIT = 25


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
        self._restore_state()
        # Job selection + the queued->downloading transition happen atomically under
        # self._lock, so multiple workers never claim the same job. Per-job state is
        # likewise mutated under the lock, so running _run_job concurrently is safe.
        self._threads = []
        for index in range(self._concurrency):
            thread = Thread(target=self._worker, name=f"drone-download-worker-{index + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _persistent_jobs_locked(self) -> list[dict]:
        pending = [
            dict(job)
            for job in self._jobs.values()
            if job.get("status") not in DOWNLOAD_TERMINAL_STATUSES
        ]
        recent = [
            dict(job)
            for job in self._jobs.values()
            if job.get("status") in DOWNLOAD_TERMINAL_STATUSES
        ][-DOWNLOAD_PERSISTED_RECENT_LIMIT:]
        return pending + recent

    def _persist_state_locked(self) -> None:
        _save_state_payload(
            _state_database_path(self.settings.userdata_root),
            DOWNLOAD_QUEUE_STATE_NAMESPACE,
            {
                "version": 1,
                "paused": self._paused,
                "jobs": self._persistent_jobs_locked(),
            },
        )

    def _restore_state(self) -> None:
        stored = _load_state_payload(
            _state_database_path(self.settings.userdata_root),
            DOWNLOAD_QUEUE_STATE_NAMESPACE,
            {},
        )
        if not isinstance(stored, dict):
            return
        self._paused = bool(stored.get("paused", False))
        jobs = stored.get("jobs") if isinstance(stored.get("jobs"), list) else []
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        restored_pending = False
        for raw_job in jobs:
            if not isinstance(raw_job, dict):
                continue
            job = dict(raw_job)
            job_id = str(job.get("job_id") or job.get("id") or "").strip()
            if not job_id or job_id in self._jobs:
                continue
            job["id"] = job_id
            job["job_id"] = job_id
            status = str(job.get("status") or "queued").lower()
            job["_resolving"] = False
            if status == "paused":
                # A user-parked job stays parked across a restart -- it must not
                # silently resume on its own.
                job["status"] = "paused"
            elif status == "pending":
                # Still no source peer as of the last run; re-check right away
                # instead of waiting out a pre-restart backoff timer.
                job["status"] = "pending"
                job["reconnect_after_epoch"] = 0
            elif status not in DOWNLOAD_TERMINAL_STATUSES:
                if job.get("cancellation_requested"):
                    job["status"] = "cancelled"
                    job["completed_at"] = now
                    job["download_completed_at"] = now
                    job["failure_reason"] = job.get("cancel_reason") or "cancelled before restart"
                    job["error_message"] = job["failure_reason"]
                else:
                    job["status"] = "queued"
                    job["started_at"] = None
                    job["download_started_at"] = None
                    job["completed_at"] = None
                    job["download_completed_at"] = None
                    job["downloaded_bytes"] = 0
                    job["bytes_transferred"] = 0
                    job["percentage"] = 0
                    job["transfer_speed_bps"] = 0
                    job["cancellation_requested"] = False
                    job["resume_reason"] = "Drone restarted before the transfer completed"
                    job.pop("_started_mono", None)
                    restored_pending = True
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
        self._update_queue_positions_locked()
        if jobs:
            self._persist_state_locked()
        if restored_pending and not self._paused:
            self._wake.set()

    @staticmethod
    def _is_reconnectable_error(error: Exception) -> bool:
        if isinstance(error, ValueError):
            return False
        if isinstance(error, HTTPError):
            return error.code in {408, 425, 429} or error.code >= 500
        message = str(error or "").strip().lower()
        permanent_markers = (
            "fingerprint mismatch",
            "md5 mismatch",
            "invalid target path",
            "invalid save target path",
            "invalid artwork type",
            "unsafe artwork target path",
            "folder manifest is empty",
            "folder manifest did not include marker",
            "does not support asset type",
        )
        return not any(marker in message for marker in permanent_markers)

    @staticmethod
    def _reconnect_delay(attempt: int) -> int:
        try:
            initial = max(1, int(os.environ.get("DOWNLOAD_RECONNECT_INITIAL_SECONDS", str(DOWNLOAD_RECONNECT_INITIAL_SECONDS))))
        except (TypeError, ValueError):
            initial = DOWNLOAD_RECONNECT_INITIAL_SECONDS
        try:
            maximum = max(initial, int(os.environ.get("DOWNLOAD_RECONNECT_MAX_SECONDS", str(DOWNLOAD_RECONNECT_MAX_SECONDS))))
        except (TypeError, ValueError):
            maximum = max(initial, DOWNLOAD_RECONNECT_MAX_SECONDS)
        exponent = min(16, max(0, attempt - 1))
        return min(maximum, initial * (2 ** exponent))

    def _refresh_connection_metadata_locked(self, job: dict) -> None:
        try:
            from ..overmind.overmind_config import _load_overmind_config_for_settings
        except ImportError:  # pragma: no cover - flat execution
            from overmind.overmind_config import _load_overmind_config_for_settings  # type: ignore

        stored_config = job.get("_config") if isinstance(job.get("_config"), dict) else {}
        overmind_job = bool(stored_config.get("overmind_url") or stored_config.get("overmind_token"))
        use_overmind = overmind_job and _local_network.is_overmind_mode(self.settings)
        if use_overmind:
            current_config = _load_overmind_config_for_settings(self.settings)
            if isinstance(current_config, dict):
                job["_config"] = {**stored_config, **current_config}
        source_id = str(job.get("source_drone_id") or "").strip()
        if not source_id:
            return
        current_peer = None if use_overmind else _local_network.get_paired_peer(self.settings, source_id)
        if use_overmind:
            swarm = _load_state_payload(
                _state_database_path(self.settings.userdata_root),
                "overmind_swarm.json",
                [],
            )
            if isinstance(swarm, list):
                current_peer = next(
                    (
                        row
                        for row in swarm
                        if isinstance(row, dict)
                        and str(row.get("drone_id") or row.get("device_id") or "").strip() == source_id
                    ),
                    None,
                )
        if isinstance(current_peer, dict):
            job["_peer"] = {**(job.get("_peer") or {}), **current_peer}

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
            self._persist_state_locked()
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
            self._persist_state_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_pending_rom(
        self,
        config: dict,
        system: str,
        gamelist_id: str = "",
        relative_path: str = "",
        expected_size=None,
        expected_fingerprint=None,
        source_action_id: Optional[str] = None,
        entry_type: str = "file",
        sync_id: Optional[str] = None,
        source_device_ids: Optional[set] = None,
    ) -> dict:
        """Queue a ROM sync with no source peer resolved yet.

        Used when Overmind named candidate Drones for this ROM but none is
        currently peer-resolvable. The job sits in 'pending' -- invisible to the
        normal queued-job scan -- while the worker periodically retries peer (and,
        if only a gamelist_id was given, path) resolution against
        ``source_device_ids`` and promotes it to 'queued' once a source answers.
        """
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": sync_id or job_id,
            "source_action_id": source_action_id,
            "source_drone_id": None,
            "target_drone_id": self.settings.overmind_device_id,
            "file_path": relative_path or gamelist_id,
            "file_name": Path(relative_path).name if relative_path else gamelist_id,
            "file_type": "ROM",
            "entry_type": entry_type,
            "system": system,
            "rom_name": relative_path or gamelist_id,
            "relative_path": relative_path,
            "total_bytes": expected_size,
            "file_size": expected_size,
            "downloaded_bytes": 0,
            "bytes_transferred": 0,
            "percentage": 0,
            "transfer_speed_bps": 0,
            "status": "pending",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": "Waiting for a source Drone with this ROM to become reachable",
            "cancellation_requested": False,
            "created_at": now,
            "reconnect_after_epoch": None,
            "pending_attempts": 0,
            "_resolving": False,
            "_config": config,
            "_gamelist_id": gamelist_id,
            "_expected_fingerprint": expected_fingerprint,
            "_entry_type": entry_type,
            "_pending_kind": "rom",
            "_source_device_ids": sorted(str(v) for v in (source_device_ids or set()) if str(v).strip()),
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            self._persist_state_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return snapshot

    def enqueue_pending_bios(
        self,
        config: dict,
        relative_path: str,
        expected_size=None,
        expected_md5=None,
        source_action_id: Optional[str] = None,
        sync_id: Optional[str] = None,
        source_device_ids: Optional[set] = None,
    ) -> dict:
        """BIOS counterpart to :meth:`enqueue_pending_rom` -- see its docstring."""
        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        job = {
            "id": job_id,
            "job_id": job_id,
            "sync_id": sync_id or job_id,
            "source_action_id": source_action_id,
            "source_drone_id": None,
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
            "status": "pending",
            "queue_position": None,
            "started_at": None,
            "download_started_at": None,
            "completed_at": None,
            "download_completed_at": None,
            "error_message": None,
            "failure_reason": "Waiting for a source Drone with this BIOS to become reachable",
            "cancellation_requested": False,
            "created_at": now,
            "bios_md5": expected_md5,
            "reconnect_after_epoch": None,
            "pending_attempts": 0,
            "_resolving": False,
            "_asset_type": "bios",
            "_config": config,
            "_expected_fingerprint": expected_md5,
            "_pending_kind": "bios",
            "_source_device_ids": sorted(str(v) for v in (source_device_ids or set()) if str(v).strip()),
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = Event()
            self._update_queue_positions_locked()
            self._persist_state_locked()
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
            self._persist_state_locked()
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
            self._persist_state_locked()
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
            # queued/paused/pending all have no worker thread actively running them
            # right now, so cancellation lands immediately (same as the pre-existing
            # queued case) instead of waiting for _run_job to observe the event.
            if job.get("status") in {"queued", "paused", "pending"}:
                job["status"] = "cancelled"
                job["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                job["download_completed_at"] = job["completed_at"]
                job["failure_reason"] = reason
                job["error_message"] = reason
                job["pause_requested"] = False
                self._update_queue_positions_locked()
            self._persist_state_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return {"status": snapshot.get("status"), "job": snapshot}

    def pause_job(self, job_id: str) -> dict:
        """Pause a single job. A queued/pending job is parked immediately (nothing
        is running for it); an in-flight download is asked to stop -- like cancel,
        reusing the same cancellation event -- but lands in 'paused' instead of
        'cancelled' so resume_job can re-run it."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            current_status = job.get("status")
            if current_status in DOWNLOAD_TERMINAL_STATUSES:
                return {"status": "not_pausable", "job_id": job_id, "job": self._public_job_locked(job)}
            if current_status == "paused":
                return {"status": "paused", "job": self._public_job_locked(job)}
            if current_status == "downloading":
                job["pause_requested"] = True
                event = self._cancel_events.get(job_id)
                if event:
                    event.set()
                self._persist_state_locked()
                snapshot = self._public_job_locked(job)
                self._wake.set()
                return {"status": "pausing", "job": snapshot}
            job["status"] = "paused"
            job["queue_position"] = None
            job["reconnect_after_epoch"] = None
            self._update_queue_positions_locked()
            self._persist_state_locked()
            snapshot = self._public_job_locked(job)
        return {"status": "paused", "job": snapshot}

    def resume_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return {"status": "not_found", "job_id": job_id}
            if job.get("status") != "paused":
                return {"status": "not_resumable", "job_id": job_id, "job": self._public_job_locked(job)}
            job["status"] = "queued"
            job["pause_requested"] = False
            job["resume_reason"] = "Resumed by user"
            self._update_queue_positions_locked()
            self._persist_state_locked()
            snapshot = self._public_job_locked(job)
        self._wake.set()
        return {"status": "queued", "job": snapshot}

    @staticmethod
    def _land_paused_locked(job: dict) -> None:
        """Land an in-flight job that was asked to pause. Must be called under
        self._lock. Mirrors the reconnect-reset shape (progress zeroed) since the
        underlying transfer was aborted mid-flight, not resumed from an offset."""
        job["status"] = "paused"
        job["pause_requested"] = False
        job["resume_reason"] = None
        job["failure_reason"] = None
        job["error_message"] = None
        job["started_at"] = None
        job["download_started_at"] = None
        job["downloaded_bytes"] = 0
        job["bytes_transferred"] = 0
        job["percentage"] = 0
        job["transfer_speed_bps"] = 0
        job.pop("_started_mono", None)

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
            self._persist_state_locked()
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
        # 'pending' (no source peer yet) and 'paused' jobs are bucketed with
        # 'queued' for transport/storage purposes (the relational schema's
        # state_bucket only allows active/queued/recent) -- each row's own
        # 'status' field still distinguishes them for display.
        queued = [job for job in jobs if job.get("status") in {"queued", "pending", "paused"}]
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
            self._persist_state_locked()
        return self.snapshot()

    def resume(self) -> dict:
        with self._lock:
            self._paused = False
            self._persist_state_locked()
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
            self._persist_state_locked()
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
            pending_id = None
            with self._lock:
                if not self._paused:
                    for candidate_id, candidate in self._jobs.items():
                        reconnect_after = float(candidate.get("reconnect_after_epoch") or 0)
                        if candidate.get("status") == "queued" and reconnect_after <= time.time():
                            job_id = candidate_id
                            break
                    if job_id is None:
                        # No real download ready to run -- use the spare capacity to
                        # retry peer resolution for a 'pending' (no source yet) job.
                        # _resolving guards against two worker threads claiming the
                        # same pending job.
                        for candidate_id, candidate in self._jobs.items():
                            reconnect_after = float(candidate.get("reconnect_after_epoch") or 0)
                            if (
                                candidate.get("status") == "pending"
                                and not candidate.get("_resolving")
                                and reconnect_after <= time.time()
                            ):
                                pending_id = candidate_id
                                candidate["_resolving"] = True
                                break
                if job_id:
                    job = self._jobs[job_id]
                    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    job["status"] = "downloading"
                    job["started_at"] = now
                    job["download_started_at"] = now
                    job["_started_mono"] = time.monotonic()
                    job["reconnect_after_epoch"] = None
                    job["failure_reason"] = None
                    job["error_message"] = None
                    job["resume_reason"] = None
                    self._refresh_connection_metadata_locked(job)
                    self._update_queue_positions_locked()
                    self._persist_state_locked()
            if job_id:
                self._run_job(job_id)
                continue
            if pending_id:
                self._retry_pending_job(pending_id)
                continue
            self._wake.wait(1)
            self._wake.clear()

    def _retry_pending_job(self, job_id: str) -> None:
        """Attempt to resolve a source peer (and, for a gamelist-id-only ROM, its
        path) for one 'pending' job. Promotes it to 'queued' on success, or bumps
        its backoff and leaves it 'pending' on failure. Peer selection reads only
        locally-cached state (no network I/O); gamelist-id resolution makes one
        HTTP call to the chosen peer -- both run unlocked, matching _run_job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.get("status") != "pending":
                if job is not None:
                    job["_resolving"] = False
                return
            kind = str(job.get("_pending_kind") or "rom")
            system = str(job.get("system") or "")
            gamelist_id = str(job.get("_gamelist_id") or "")
            rel = str(job.get("relative_path") or "")
            config = job.get("_config") or {}
            source_device_ids = set(job.get("_source_device_ids") or [])
            expected_fingerprint = job.get("_expected_fingerprint")
            attempt = int(job.get("pending_attempts") or 0) + 1

        def _back_off() -> None:
            with self._lock:
                current = self._jobs.get(job_id)
                if current and current.get("status") == "pending":
                    current["pending_attempts"] = attempt
                    current["reconnect_after_epoch"] = time.time() + self._reconnect_delay(attempt)
                if current is not None:
                    current["_resolving"] = False
                self._persist_state_locked()

        try:
            peer = (
                _best_peer_for_bios(self.settings, config, rel, source_device_ids=source_device_ids)
                if kind == "bios"
                else _best_peer_for_rom(self.settings, self.repository, config, system, rel, source_device_ids=source_device_ids)
            )
            if not peer:
                _back_off()
                return
            marker_relative_path = None
            artwork_types: list = []
            if kind == "rom" and not rel and gamelist_id:
                resolved = _resolve_rom_by_gamelist_id_from_peer(self.settings, config, peer, system, gamelist_id)
                if not resolved or not resolved.get("relative_path"):
                    _back_off()
                    return
                rel = str(resolved.get("relative_path") or "").strip()
                marker_relative_path = str(resolved.get("marker_relative_path") or "").strip() or None
                if not expected_fingerprint:
                    expected_fingerprint = resolved.get("rom_fingerprint")
                artwork_types = resolved.get("artwork_types") if isinstance(resolved.get("artwork_types"), list) else []
            with self._lock:
                job = self._jobs.get(job_id)
                if not job or job.get("status") != "pending":
                    if job is not None:
                        job["_resolving"] = False
                    return
                peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
                job["status"] = "queued"
                job["source_drone_id"] = peer_id
                job["relative_path"] = rel
                job["file_path"] = rel or job.get("file_path")
                job["file_name"] = Path(rel).name if rel else job.get("file_name")
                job["rom_name"] = rel or job.get("rom_name")
                job["_peer"] = peer
                job["_expected_fingerprint"] = expected_fingerprint
                if kind == "rom":
                    job["_marker_relative_path"] = marker_relative_path
                    job["_artwork_types"] = [str(v).strip() for v in (artwork_types or []) if str(v).strip()]
                job["reconnect_after_epoch"] = None
                job["failure_reason"] = None
                job["resume_reason"] = "A source Drone became reachable"
                job["_resolving"] = False
                self._update_queue_positions_locked()
                self._persist_state_locked()
            self._wake.set()
        except Exception:
            _back_off()

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
                with self._lock:
                    self._persist_state_locked()
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
                if current and current.get("pause_requested"):
                    self._land_paused_locked(current)
                elif current:
                    current["status"] = "cancelled"
                    current["failure_reason"] = str(error) or current.get("cancel_reason") or "cancelled"
                    current["error_message"] = current["failure_reason"]
                    current["completed_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                    current["download_completed_at"] = current["completed_at"]
        except Exception as error:
            with self._lock:
                current = self._jobs.get(job_id)
                if current and current.get("pause_requested"):
                    self._land_paused_locked(current)
                elif current:
                    if self._is_reconnectable_error(error) and not current.get("cancellation_requested"):
                        attempt = int(current.get("reconnect_attempts") or 0) + 1
                        delay = self._reconnect_delay(attempt)
                        current["status"] = "queued"
                        current["reconnect_attempts"] = attempt
                        current["reconnect_after_epoch"] = time.time() + delay
                        current["resume_reason"] = f"Source unavailable; reconnecting in {delay} seconds"
                        current["failure_reason"] = str(error)
                        current["error_message"] = str(error)
                        current["started_at"] = None
                        current["download_started_at"] = None
                        current["downloaded_bytes"] = 0
                        current["bytes_transferred"] = 0
                        current["percentage"] = 0
                        current["transfer_speed_bps"] = 0
                        current.pop("_started_mono", None)
                    else:
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
                self._persist_state_locked()
                reconnecting = bool(current and current.get("status") == "queued" and current.get("reconnect_attempts"))
                paused = bool(current and current.get("status") == "paused")
            push_reason = "paused" if paused else ("reconnecting" if reconnecting else "completed")
            self._push_download_state(config, push_reason, force=True)
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
