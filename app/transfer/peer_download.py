"""Direct-peer asset downloads (the direct-public P2P tier).

Extracted from ``drone_api.py``. Given a selected paired peer, pulls a ROM, ROM
folder, BIOS, save, or artwork over cert-pinned mTLS ``GET /peer/*`` (SSL-retry via
the peer cert cache), writes it collision-safely with a sampled-hash guard.
"""

import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Optional, Tuple
from urllib.error import URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from ..common.http_cache import valid_segment
    from ..common.settings import Settings
    from ..device.device_control import _ensure_rom_write_access
    from ..storage.rom_metadata_store import _load_rom_metadata_cache
    from .download_errors import DownloadCancelled
    from .peer_connectivity import (
        PEER_CHECK_TIMEOUT_SECONDS,
        _drone_client_ssl_context,
        _is_ssl_url_error,
        _preferred_peer_address,
        _peer_get_json,
        _peer_ssl_diagnostic,
        _peer_trust_cafile,
    )
    from .transfer_files import (
        collision_safe_target as _collision_safe_target,
        safe_rom_relative_path as _safe_rom_relative_path,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_cache import valid_segment  # type: ignore
    from common.settings import Settings  # type: ignore
    from device.device_control import _ensure_rom_write_access  # type: ignore
    from storage.rom_metadata_store import _load_rom_metadata_cache  # type: ignore
    from transfer.download_errors import DownloadCancelled  # type: ignore
    from transfer.peer_connectivity import (  # type: ignore
        PEER_CHECK_TIMEOUT_SECONDS,
        _drone_client_ssl_context,
        _is_ssl_url_error,
        _preferred_peer_address,
        _peer_get_json,
        _peer_ssl_diagnostic,
        _peer_trust_cafile,
    )
    from transfer.transfer_files import (  # type: ignore
        collision_safe_target as _collision_safe_target,
        safe_rom_relative_path as _safe_rom_relative_path,
    )


def _cached_rom_fingerprint_exists(settings: Settings, expected_fingerprint: Optional[str]) -> bool:
    expected = str(expected_fingerprint or "").strip().lower()
    if not expected:
        return False
    try:
        cache, _ = _load_rom_metadata_cache(settings)
    except Exception:
        return False
    entries = cache.get("entries") if isinstance(cache.get("entries"), dict) else {}
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        fingerprint_value = str(entry.get("rom_fingerprint") or entry.get("fingerprint") or "").strip().lower()
        if fingerprint_value == expected:
            return True
    return False


def _folder_rom_marker_state(
    settings: Settings,
    system: str,
    relative_path: str,
    marker_relative_path: Optional[str],
) -> Optional[Tuple[Path, str]]:
    """For a folder-unit ROM (marker file inside the transferred folder), return the
    resolved local marker path + its folder-relative path. ``None`` for true directory
    entries (marker == the folder itself) or when no marker was provided."""
    marker = str(marker_relative_path or "").strip()
    rel = _safe_rom_relative_path(relative_path)
    if not marker:
        return None
    marker = _safe_rom_relative_path(marker)
    if marker == rel or not marker.startswith(rel + "/"):
        return None
    system_dir = (settings.roms_root / system).resolve()
    marker_target = (system_dir / marker).resolve()
    if system_dir not in marker_target.parents:
        return None
    return marker_target, marker[len(rel) + 1:]


def _download_rom_folder_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    marker_relative_path=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = False,
) -> dict:
    # RomRepository stays in drone_api (Phase 4/6 will move it); lazy-import to avoid a cycle.
    try:
        from ..drone_api import RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import RomRepository  # type: ignore
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    manifest_url = f"{address}/v1/api/peer/rom-manifest/{quote(system, safe='')}/{quote(rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    target_dir = (system_dir / rel).resolve()
    if target_dir == system_dir or system_dir not in target_dir.parents:
        raise ValueError("invalid target path")
    if target_dir.exists() and not target_dir.is_dir():
        raise ValueError("target path exists and is not a directory")
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    expected_fingerprint_clean = str(expected_fingerprint or "").strip().lower()
    marker_state = _folder_rom_marker_state(settings, system, rel, marker_relative_path)

    # Present-check for folder-unit ROMs: the marker file is written LAST (below), so
    # its presence (plus a fingerprint match when one is expected) proves the whole
    # folder arrived. True directory entries keep their historical re-pull behavior.
    if marker_state is not None and not overwrite:
        marker_target, _ = marker_state
        if marker_target.is_file():
            marker_fingerprint = None
            try:
                marker_fingerprint = RomRepository.build_fingerprint(marker_target).lower()
            except Exception:
                pass
            if not expected_fingerprint_clean or marker_fingerprint == expected_fingerprint_clean:
                completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
                duration_ms = int((time.monotonic() - started_mono) * 1000)
                return {
                    "entry_type": "folder",
                    "source_drone_id": peer_id,
                    "target_drone_id": settings.device_id,
                    "system": system,
                    "rom_name": rel,
                    "relative_path": rel,
                    "marker_relative_path": _safe_rom_relative_path(str(marker_relative_path)),
                    "action": "download",
                    "status": "skipped",
                    "skip_reason": "folder ROM marker already exists",
                    "failure_reason": "folder ROM marker already exists",
                    "bytes_transferred": 0,
                    "file_size": expected_size,
                    "fingerprint": marker_fingerprint or expected_fingerprint_clean,
                    "rom_fingerprint": expected_fingerprint_clean or marker_fingerprint,
                    "download_started_at": started,
                    "download_completed_at": completed_dt.isoformat(),
                    "started_at": started,
                    "completed_at": completed_dt.isoformat(),
                    "duration_ms": duration_ms,
                    "duration_seconds": round(duration_ms / 1000, 3),
                    "selected_peer_reason": "local folder ROM already exists",
                }

    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    manifest = _peer_get_json(manifest_url, settings, peer_id=peer_id, config=config)
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    directories = manifest.get("directories") if isinstance(manifest.get("directories"), list) else []
    if not files and not directories:
        raise RuntimeError("folder manifest is empty")
    if marker_state is not None:
        # Write the marker last so the present-check above can't see a half-copied folder.
        marker_folder_rel = marker_state[1].lower()
        files = sorted(files, key=lambda item: str((item or {}).get("relative_path") or "").replace("\\", "/").lower() == marker_folder_rel)

    bytes_written = 0
    total_bytes = None
    try:
        total_bytes = int(manifest.get("file_size") or expected_size or 0) or None
    except Exception:
        total_bytes = None

    def ensure_not_cancelled(partial: Optional[Path] = None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial and partial.exists():
                partial.unlink()
            raise DownloadCancelled("download cancelled")

    target_dir.mkdir(parents=True, exist_ok=True)
    for directory in directories:
        child_dir = (target_dir / _safe_rom_relative_path(str(directory or ""))).resolve()
        if child_dir == target_dir or target_dir not in child_dir.parents:
            raise ValueError("invalid manifest directory path")
        child_dir.mkdir(parents=True, exist_ok=True)

    for item in files:
        if not isinstance(item, dict):
            continue
        child_rel = _safe_rom_relative_path(str(item.get("relative_path") or ""))
        target = (target_dir / child_rel).resolve()
        if target == target_dir or target_dir not in target.parents:
            raise ValueError("invalid manifest file path")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial_target = target.with_name(f"{target.name}.part")
        file_url = f"{address}/v1/api/peer/roms/{quote(system, safe='')}/{quote(rel + '/' + child_rel, safe='/')}"
        request = Request(file_url, headers={"User-Agent": "batocera-drone-rom-folder-sync/1.0"})
        context = _drone_client_ssl_context(settings, file_url, verify=bool(cafile), cafile=cafile)
        expected_file_size = item.get("file_size")
        file_bytes = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                while True:
                    ensure_not_cancelled(partial_target)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    file_bytes += len(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except DownloadCancelled:
            if partial_target.exists():
                partial_target.unlink()
            raise
        except Exception:
            if partial_target.exists():
                partial_target.unlink()
            raise
        if expected_file_size not in (None, ""):
            try:
                if int(expected_file_size) != file_bytes:
                    if partial_target.exists():
                        partial_target.unlink()
                    raise RuntimeError(f"size mismatch for {child_rel} expected={expected_file_size} actual={file_bytes}")
            except ValueError:
                pass
        partial_target.replace(target)
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    activity_fingerprint = None
    if marker_state is not None and expected_fingerprint_clean:
        # Identity check: the marker is the ROM's fingerprint carrier. Remove it on
        # mismatch so the present-check can't accept a wrong/half-written folder.
        marker_target, _ = marker_state
        if not marker_target.is_file():
            raise RuntimeError(f"folder manifest did not include marker file {marker_relative_path}")
        try:
            activity_fingerprint = RomRepository.build_fingerprint(marker_target).lower()
        except Exception as error:
            raise RuntimeError(f"failed to fingerprint marker file {marker_relative_path}: {error}")
        if activity_fingerprint != expected_fingerprint_clean:
            marker_target.unlink()
            raise RuntimeError(
                f"fingerprint mismatch for marker {marker_relative_path} expected={expected_fingerprint_clean} actual={activity_fingerprint}"
            )
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    activity = {
        "entry_type": "folder",
        "source_drone_id": peer_id,
        "target_drone_id": settings.device_id,
        "system": system,
        "rom_name": rel,
        "relative_path": target_dir.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or total_bytes or bytes_written,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested directory ROM and best sampled score",
    }
    if marker_state is not None:
        activity["marker_relative_path"] = _safe_rom_relative_path(str(marker_relative_path))
        if activity_fingerprint:
            activity["fingerprint"] = activity_fingerprint
            activity["rom_fingerprint"] = expected_fingerprint_clean or activity_fingerprint
    return activity


def _download_rom_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = False,
) -> dict:
    # RomRepository stay in drone_api (Phase 4/6 will move them); lazy-import to avoid a cycle.
    try:
        from ..drone_api import RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import RomRepository  # type: ignore
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/roms/{quote(system, safe='')}/{quote(rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    target = (system_dir / rel).resolve()
    if target == system_dir or system_dir not in target.parents:
        raise ValueError("invalid target path")
    partial_target = target.with_name(f"{target.name}.part")
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    expected_fingerprint_clean = str(expected_fingerprint or "").strip().lower()

    def skipped_activity(existing: Path, reason: str) -> dict:
        completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        try:
            size = int(existing.stat().st_size)
        except OSError:
            try:
                size = int(expected_size or 0) if expected_size not in (None, "") else 0
            except (TypeError, ValueError):
                size = 0
        try:
            fingerprint = RomRepository.build_fingerprint(existing) if existing.is_file() else expected_fingerprint_clean
        except Exception:
            fingerprint = expected_fingerprint_clean
        return {
            "source_drone_id": peer_id,
            "target_drone_id": settings.device_id,
            "system": system,
            "rom_name": rel,
            "relative_path": existing.relative_to(system_dir).as_posix(),
            "action": "download",
            "status": "skipped",
            "skip_reason": reason,
            "failure_reason": reason,
            "bytes_transferred": 0,
            "file_size": size or expected_size,
            "fingerprint": fingerprint,
            "rom_fingerprint": expected_fingerprint_clean or fingerprint,
            "download_started_at": started,
            "download_completed_at": completed_dt.isoformat(),
            "started_at": started,
            "completed_at": completed_dt.isoformat(),
            "duration_ms": duration_ms,
            "duration_seconds": round(duration_ms / 1000, 3),
            "selected_peer_reason": "local ROM already exists",
        }

    if target.exists() and not overwrite:
        return skipped_activity(target, "target path already exists")
    if not overwrite and expected_fingerprint_clean and system_dir.exists() and system_dir.is_dir():
        for candidate in sorted(system_dir.rglob("*"), key=lambda path: path.relative_to(system_dir).as_posix().lower()):
            if not candidate.is_file():
                continue
            rel_candidate = candidate.relative_to(system_dir).as_posix()
            if RomRepository.should_ignore_rom_path(Path(rel_candidate)):
                continue
            try:
                if RomRepository.build_fingerprint(candidate).lower() == expected_fingerprint_clean:
                    return skipped_activity(candidate, "matching ROM already exists")
            except Exception:
                continue

    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-rom-sync/1.0"})
    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    def response_total(response) -> Optional[int]:
        if expected_size not in (None, ""):
            try:
                return int(expected_size)
            except Exception:
                pass
        try:
            return int(response.headers.get("Content-Length") or 0) or None
        except Exception:
            return None

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            total_bytes = response_total(response)
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
                if progress_callback:
                    progress_callback(bytes_written, total_bytes)
    except (ssl.SSLError, URLError) as error:
        if isinstance(error, URLError) and not _is_ssl_url_error(error):
            raise
        ssl_error = getattr(error, "reason", error)
        print(f"ROM sync SSL validation failed: {_peer_ssl_diagnostic(url, cafile, ssl_error)}", file=sys.stderr, flush=True)
        if partial_target.exists():
            partial_target.unlink()
        cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=True)
        if address.startswith("https://") and not cafile:
            raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}") from error
        context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
        bytes_written = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                total_bytes = response_total(response)
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except (ssl.SSLError, URLError) as retry_error:
            if isinstance(retry_error, URLError) and not _is_ssl_url_error(retry_error):
                raise
            retry_ssl_error = getattr(retry_error, "reason", retry_error)
            print(f"ROM sync SSL validation retry failed: {_peer_ssl_diagnostic(url, cafile, retry_ssl_error)}", file=sys.stderr, flush=True)
            if partial_target.exists():
                partial_target.unlink()
            raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, retry_ssl_error)) from retry_error
    except DownloadCancelled:
        if partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    actual_fingerprint = RomRepository.build_fingerprint(partial_target)
    if expected_fingerprint_clean and actual_fingerprint.lower() != expected_fingerprint_clean:
        if partial_target.exists():
            partial_target.unlink()
        raise RuntimeError(f"fingerprint mismatch expected={expected_fingerprint_clean} actual={actual_fingerprint}")
    partial_target.replace(target)
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "source_drone_id": peer_id,
        "target_drone_id": settings.device_id,
        "system": system,
        "rom_name": rel,
        "relative_path": target.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "fingerprint": actual_fingerprint,
        "rom_fingerprint": expected_fingerprint_clean or actual_fingerprint,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested system and best sampled score",
    }


def _download_bios_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    relative_path: str,
    expected_size=None,
    expected_md5=None,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = False,
) -> dict:
    # RomRepository stay in drone_api (Phase 4/6 will move them); lazy-import to avoid a cycle.
    try:
        from ..drone_api import RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import RomRepository  # type: ignore
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
    if not address:
        raise RuntimeError("selected peer has no address")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/bios/{quote(rel, safe='/')}"
    bios_root = settings.bios_root.resolve()
    target = (bios_root / rel).resolve()
    if target == bios_root or bios_root not in target.parents:
        raise ValueError("invalid target path")
    partial_target = target.with_name(f"{target.name}.part")
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    expected_md5_clean = str(expected_md5 or "").strip().lower()

    def skipped_activity(existing: Path, reason: str) -> dict:
        completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
        duration_ms = int((time.monotonic() - started_mono) * 1000)
        return {
            "asset_type": "bios", "file_type": "BIOS", "source_drone_id": peer_id,
            "target_drone_id": settings.device_id, "system": "bios",
            "bios_name": rel, "rom_name": rel,
            "relative_path": existing.relative_to(bios_root).as_posix(),
            "action": "download", "status": "skipped", "skip_reason": reason,
            "failure_reason": reason, "bytes_transferred": 0,
            "file_size": existing.stat().st_size, "bios_md5": expected_md5_clean,
            "download_started_at": started, "download_completed_at": completed_dt.isoformat(),
            "started_at": started, "completed_at": completed_dt.isoformat(),
            "duration_ms": duration_ms, "duration_seconds": round(duration_ms / 1000, 3),
        }

    if not overwrite:
        if target.is_file():
            return skipped_activity(target, "target path already exists")
        if expected_md5_clean and bios_root.is_dir():
            for candidate in bios_root.rglob("*"):
                if not candidate.is_file() or candidate.name.endswith(".part"):
                    continue
                try:
                    if RomRepository.build_md5(candidate).lower() == expected_md5_clean:
                        return skipped_activity(candidate, "matching BIOS already exists")
                except OSError:
                    continue

    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-bios-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    def response_total(response) -> Optional[int]:
        if expected_size not in (None, ""):
            try:
                return int(expected_size)
            except Exception:
                pass
        try:
            return int(response.headers.get("Content-Length") or 0) or None
        except Exception:
            return None

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            total_bytes = response_total(response)
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
                if progress_callback:
                    progress_callback(bytes_written, total_bytes)
    except (ssl.SSLError, URLError) as error:
        if isinstance(error, URLError) and not _is_ssl_url_error(error):
            raise
        ssl_error = getattr(error, "reason", error)
        print(f"BIOS sync SSL validation failed: {_peer_ssl_diagnostic(url, cafile, ssl_error)}", file=sys.stderr, flush=True)
        if partial_target.exists():
            partial_target.unlink()
        cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config, refresh_cert=True)
        if address.startswith("https://") and not cafile:
            raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}") from error
        context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
        bytes_written = 0
        try:
            with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
                total_bytes = response_total(response)
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
        except (ssl.SSLError, URLError) as retry_error:
            if isinstance(retry_error, URLError) and not _is_ssl_url_error(retry_error):
                raise
            retry_ssl_error = getattr(retry_error, "reason", retry_error)
            print(f"BIOS sync SSL validation retry failed: {_peer_ssl_diagnostic(url, cafile, retry_ssl_error)}", file=sys.stderr, flush=True)
            if partial_target.exists():
                partial_target.unlink()
            raise ssl.SSLError(_peer_ssl_diagnostic(url, cafile, retry_ssl_error)) from retry_error
    except DownloadCancelled:
        if partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    if expected_size not in (None, ""):
        try:
            if int(expected_size) != bytes_written:
                raise RuntimeError(f"size mismatch expected={expected_size} actual={bytes_written}")
        except ValueError:
            pass
    # BIOS verifies against a full-file MD5 (exact emulator identity), not the sampled fingerprint.
    actual_md5 = RomRepository.build_md5(partial_target)
    if expected_md5_clean and actual_md5.lower() != expected_md5_clean:
        if partial_target.exists():
            partial_target.unlink()
        raise RuntimeError(f"md5 mismatch expected={expected_md5_clean} actual={actual_md5}")
    partial_target.replace(target)
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "bios",
        "file_type": "BIOS",
        "source_drone_id": peer_id,
        "target_drone_id": settings.device_id,
        "system": "bios",
        "bios_name": rel,
        "rom_name": rel,
        "relative_path": target.relative_to(bios_root).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "md5": actual_md5,
        "bios_md5": expected_md5_clean or actual_md5,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested BIOS and best sampled score",
    }


def _download_save_from_peer(
    settings: Settings,
    config: dict,
    peer: dict,
    system: str,
    relative_path: str,
    expected_size=None,
    expected_fingerprint=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = True,
) -> dict:
    """Fetch a single game-save file from a peer and write it under saves_root.

    The fetched copy replaces the local one only when ``overwrite`` is enabled.
    The sampled fingerprint is verified when the caller supplies the expected value.
    ``relative_path`` is the path WITHIN the system directory.
    """
    # RomRepository stay in drone_api (Phase 4/6 will move them); lazy-import to avoid a cycle.
    try:
        from ..drone_api import RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import RomRepository  # type: ignore
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
    if not address:
        raise RuntimeError("selected peer has no address")
    system_clean = _safe_rom_relative_path(system).strip("/")
    rel = _safe_rom_relative_path(relative_path)
    url = f"{address}/v1/api/peer/saves/{quote(system_clean, safe='/')}/{quote(rel, safe='/')}"
    saves_root = Path(settings.saves_root).resolve()
    target = (saves_root / system_clean / rel).resolve()
    if saves_root not in target.parents:
        raise ValueError("invalid save target path")
    if target.is_file() and not overwrite:
        return {
            "asset_type": "saves", "file_type": "Save", "source_drone_id": peer_id,
            "target_drone_id": settings.device_id, "system": system_clean,
            "save_name": rel, "relative_path": f"{system_clean}/{rel}",
            "action": "download", "status": "skipped",
            "skip_reason": "target path already exists", "failure_reason": "target path already exists",
            "bytes_transferred": 0, "file_size": target.stat().st_size,
            "fingerprint": str(expected_fingerprint or ""), "duration_ms": 0, "duration_seconds": 0,
        }
    partial_target = target.with_name(f"{target.name}.part")
    target.parent.mkdir(parents=True, exist_ok=True)
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    started_mono = time.monotonic()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-saves-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            if partial_target.exists():
                partial_target.unlink()
            raise DownloadCancelled("download cancelled")

    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response, partial_target.open("wb") as handle:
            while True:
                ensure_not_cancelled()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                bytes_written += len(chunk)
    except DownloadCancelled:
        raise
    except Exception:
        if partial_target.exists():
            partial_target.unlink()
        raise
    expected_fp = str(expected_fingerprint or "").strip().lower()
    if expected_fp:
        actual_fp = RomRepository.build_fingerprint(partial_target).lower()
        if actual_fp != expected_fp:
            if partial_target.exists():
                partial_target.unlink()
            raise RuntimeError(f"fingerprint mismatch expected={expected_fp} actual={actual_fp}")
    partial_target.replace(target)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "saves",
        "file_type": "Save",
        "source_drone_id": peer_id,
        "target_drone_id": settings.device_id,
        "system": system_clean,
        "save_name": rel,
        "relative_path": f"{system_clean}/{rel}",
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": expected_size or bytes_written,
        "fingerprint": expected_fp or RomRepository.build_fingerprint(target),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
    }


def _download_artwork_from_peer(
    settings: Settings,
    repository: "RomRepository",
    config: dict,
    peer: dict,
    system: str,
    rom_path: str,
    artwork_type: str,
    progress_callback=None,
    cancellation_event: Optional[Event] = None,
    overwrite: bool = False,
    local_rom_path: Optional[str] = None,
) -> dict:
    # ARTWORK_FIELDS + RomRepository stay in drone_api (Phase 4/6 will move them); lazy-import to avoid a cycle.
    try:
        from ..drone_api import ARTWORK_FIELDS, RomRepository
    except ImportError:  # pragma: no cover - flat execution
        from drone_api import ARTWORK_FIELDS, RomRepository  # type: ignore
    peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
    address = _preferred_peer_address(peer, settings=settings, peer_id=peer_id)
    if not address:
        raise RuntimeError("selected peer has no address")
    system = valid_segment(system)
    field = str(artwork_type or "").strip()
    if field not in ARTWORK_FIELDS:
        raise ValueError("invalid artwork type")
    # The peer resolves its artwork from its own gamelist using the *peer's* ROM
    # path; the file is written locally and linked in the local gamelist against
    # the *local* ROM path (which differs only when the same ROM is present under
    # a different filename).
    rom_rel = _safe_rom_relative_path(rom_path)
    local_rom_rel = _safe_rom_relative_path(local_rom_path) if local_rom_path else rom_rel
    url = f"{address}/v1/api/peer/artwork/{quote(system, safe='')}/{quote(field, safe='')}/{quote(rom_rel, safe='/')}"
    system_dir = (settings.roms_root / system).resolve()
    cafile = _peer_trust_cafile(settings, peer_id=peer_id, config=config)
    if address.startswith("https://") and not cafile:
        raise ssl.SSLError(f"no trusted certificate cached for peer {peer_id}")
    context = _drone_client_ssl_context(settings, url, verify=bool(cafile), cafile=cafile)
    started_dt = datetime.now(timezone.utc).replace(microsecond=0)
    started = started_dt.isoformat()
    started_mono = time.monotonic()
    bytes_written = 0
    request = Request(url, headers={"User-Agent": "batocera-drone-artwork-sync/1.0"})

    def ensure_not_cancelled() -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            raise DownloadCancelled("download cancelled")

    partial_target = None
    target = None
    artwork_relative_path = ""
    try:
        with urlopen(request, timeout=max(10, PEER_CHECK_TIMEOUT_SECONDS * 4), context=context) as response:
            header_name = response.headers.get("X-Asset-Relative-Path") or ""
            ext = Path(header_name).suffix or Path(urlparse(response.geturl()).path).suffix or ".bin"
            if overwrite:
                # Deterministic name keyed to the *local* ROM so a re-copy overwrites
                # the same file instead of accumulating "-1" duplicates. Keep the
                # peer's media subdir (videos/ for video, manuals/ for manual, etc.)
                # so the file lands where EmulationStation expects it; default to
                # images/ when the peer did not provide one.
                media_subdir = Path(header_name).parent.as_posix() if header_name else ""
                if not media_subdir or media_subdir in (".", "/"):
                    media_subdir = "images"
                artwork_relative_path = _safe_rom_relative_path(f"{media_subdir}/{Path(local_rom_rel).stem}-{field}{ext}")
                target = (system_dir / artwork_relative_path).resolve()
                if target != system_dir and system_dir not in target.parents:
                    raise ValueError("unsafe artwork target path")
            else:
                artwork_relative_path = _safe_rom_relative_path(header_name or f"images/{Path(local_rom_rel).stem}-{field}{ext}")
                target = _collision_safe_target(system_dir, artwork_relative_path)
            partial_target = target.with_name(f"{target.name}.part")

            def _open_partial():
                target.parent.mkdir(parents=True, exist_ok=True)
                return partial_target.open("wb")

            try:
                handle = _open_partial()
            except PermissionError:
                # The media dir isn't yet writable by the unprivileged Drone (a
                # freshly-scraped, root-owned images/ or videos/). Ask the privileged
                # worker to fix perms, then retry once before giving up.
                _ensure_rom_write_access(settings, system)
                handle = _open_partial()
            try:
                total_bytes = int(response.headers.get("Content-Length") or 0) or None
            except Exception:
                total_bytes = None
            with handle:
                while True:
                    ensure_not_cancelled()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_written += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_written, total_bytes)
    except DownloadCancelled:
        if partial_target and partial_target.exists():
            partial_target.unlink()
        raise
    except Exception:
        if partial_target and partial_target.exists():
            partial_target.unlink()
        raise
    if not partial_target or not target:
        raise RuntimeError("artwork download failed")
    actual_fingerprint = RomRepository.build_fingerprint(partial_target)
    partial_target.replace(target)
    gamelist_update = None
    gamelist_update_status = "succeeded"
    artwork_rel = target.relative_to(system_dir).as_posix()
    try:
        try:
            gamelist_update = repository.update_gamelist_artwork_reference(system, local_rom_rel, field, artwork_rel)
        except Exception:
            # gamelist.xml is typically root-owned / not yet writable by the Drone
            # (a freshly-scraped, root:644 file), surfacing as PermissionError; other
            # transient errors are possible too. Ask the privileged worker to make it
            # group-writable, then retry once before reporting failure.
            _ensure_rom_write_access(settings, system)
            gamelist_update = repository.update_gamelist_artwork_reference(system, local_rom_rel, field, artwork_rel)
    except Exception as error:
        gamelist_update_status = "failed"
        gamelist_update = {"error": str(error), "path": str(system_dir / "gamelist.xml")}
        print(
            f"Artwork download completed but gamelist update failed: system={system} rom={local_rom_rel} artwork_type={field} error={error}",
            file=sys.stderr,
            flush=True,
        )
    completed_dt = datetime.now(timezone.utc).replace(microsecond=0)
    duration_ms = int((time.monotonic() - started_mono) * 1000)
    return {
        "asset_type": "artwork",
        "file_type": "ARTWORK",
        "source_drone_id": peer_id,
        "target_drone_id": settings.device_id,
        "system": system,
        "rom_name": local_rom_rel,
        "rom_path": local_rom_rel,
        "artwork_type": field,
        "relative_path": target.relative_to(system_dir).as_posix(),
        "action": "download",
        "status": "completed",
        "bytes_transferred": bytes_written,
        "file_size": bytes_written,
        "fingerprint": actual_fingerprint,
        "download_started_at": started,
        "download_completed_at": completed_dt.isoformat(),
        "started_at": started,
        "completed_at": completed_dt.isoformat(),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
        "selected_peer_reason": "healthy peer with requested artwork and best sampled score",
        "gamelist_update_status": gamelist_update_status,
        "gamelist_update": gamelist_update,
    }

