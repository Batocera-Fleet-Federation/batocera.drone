"""RomRequestHandler download-admin handlers, as a mixin.

Extracted from ``drone_api.py``. The admin download/asset-cache endpoints: list/pause/
resume/clear the download queue, cancel/retry a job, and purge/clear the asset-metadata
cache. Composed onto ``RomRequestHandler``.
"""

try:
    from ..common.runtime_state import _ROM_METADATA_WAKE
    from ..roms.rom_metadata_state import _rom_metadata_cache_status
    from ..storage.rom_metadata_store import (
        _clear_pending_rom_metadata_changes,
        _purge_asset_cache_keep_fingerprint,
        _update_rom_metadata_cache_state,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.runtime_state import _ROM_METADATA_WAKE  # type: ignore
    from roms.rom_metadata_state import _rom_metadata_cache_status  # type: ignore
    from storage.rom_metadata_store import (  # type: ignore
        _clear_pending_rom_metadata_changes,
        _purge_asset_cache_keep_fingerprint,
        _update_rom_metadata_cache_state,
    )


def _get_download_manager():
    """Delegate to the drone_api singleton accessor (lazy to avoid a cycle)."""
    try:
        from ..drone_api import _get_download_manager as _impl
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import _get_download_manager as _impl  # type: ignore
    return _impl()


class HandlersDownloadsMixin:
    def _handle_admin_downloads(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(200, {"target_drone_id": self.settings.overmind_device_id, "downloads": [], "active": [], "queued": [], "recent": []})
            return
        self._send_json(200, manager.snapshot())

    def _handle_admin_asset_cache(self) -> None:
        self._send_json(200, _rom_metadata_cache_status(self.settings))

    def _handle_admin_asset_cache_purge(self) -> None:
        """Purge cached asset metadata while keeping fingerprint, forcing a clean resync."""
        result = _purge_asset_cache_keep_fingerprint(self.settings)
        _ROM_METADATA_WAKE.set()
        cleared = result.get("cleared") or {}
        roms = int(cleared.get("roms") or 0)
        kept = int(cleared.get("preserved_fingerprint") or 0)
        self._send_json(200, {
            "status": result.get("status", "queued"),
            "kept_fingerprint": True,
            "cleared": cleared,
            "requested_at": result.get("requested_at"),
            "message": (
                f"Asset cache cleared ({roms} ROMs, {int(cleared.get('bios') or 0)} BIOS, "
                f"{int(cleared.get('artwork') or 0)} artwork). Kept {kept} fingerprint hashes — "
                "rebuilding now without re-hashing, then uploading a full inventory."
            ),
        })

    def _handle_admin_asset_cache_clear_pending(self) -> None:
        """Discard pending asset metadata upload changes without clearing cached assets."""
        before = _rom_metadata_cache_status(self.settings).get("pending_changes") or {}
        cleared_total = int(before.get("total") or 0)
        _clear_pending_rom_metadata_changes(self.settings)
        _update_rom_metadata_cache_state(self.settings, dirty=False, full_refresh_pending=False)
        after = _rom_metadata_cache_status(self.settings)
        self._send_json(200, {
            "status": "cleared",
            "cleared": before,
            "pending_changes": after.get("pending_changes") or {},
            "message": f"Cleared {cleared_total:,} pending asset change{'s' if cleared_total != 1 else ''}.",
        })

    def _handle_admin_download_cancel(self, job_id: str) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        result = manager.cancel(job_id, "cancelled from Drone admin")
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)

    def _handle_admin_download_retry(self, job_id: str) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        result = manager.retry(job_id)
        status_code = 404 if result.get("status") == "not_found" else 409 if result.get("status") == "not_retryable" else 200
        self._send_json(status_code, result)

    def _handle_admin_downloads_pause(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.pause())

    def _handle_admin_downloads_resume(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.resume())

    def _handle_admin_downloads_clear(self) -> None:
        manager = _get_download_manager()
        if manager is None:
            self._send_json(503, {"error": "download manager unavailable"})
            return
        self._send_json(200, manager.clear_queue())
