"""System-info aggregator: hostname/CPU/GPU/perf/disk/network/screen/volume snapshot.

Extracted from ``drone_api.py``. ``_collect_system_info_payload`` assembles the device
status payload the Drone reports to Overmind (and the admin UI). Pulls from system_metrics
(GPU/perf), device_control (screen/volume), network identity, automation, and the ROM cache.
"""

import os
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from ..app_version import drone_app_version as _drone_app_version
    from ..common.settings import Settings
    from ..overmind.overmind_client import _format_overmind_error
    from ..roms.rom_metadata_state import _rom_metadata_cache_status
    from ..transfer.drone_network import _get_local_ip_addresses
    from .automation import _load_automation_config
    from .device_control import _get_audio_volume, _get_screen_mode
    from .pixen import is_pixen_installed, pixen_script_path
    from .system_metrics import _collect_gpu_info, _collect_performance_metrics
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from common.settings import Settings  # type: ignore
    from overmind.overmind_client import _format_overmind_error  # type: ignore
    from roms.rom_metadata_state import _rom_metadata_cache_status  # type: ignore
    from transfer.drone_network import _get_local_ip_addresses  # type: ignore
    from device.automation import _load_automation_config  # type: ignore
    from device.device_control import _get_audio_volume, _get_screen_mode  # type: ignore
    from device.pixen import is_pixen_installed, pixen_script_path  # type: ignore
    from device.system_metrics import _collect_gpu_info, _collect_performance_metrics  # type: ignore
def _collect_system_info_payload(settings: Settings) -> dict:
    hostname = socket.gethostname()
    network = _get_local_ip_addresses()
    memory = {}
    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines()
        parsed = {}
        for line in raw:
            if ":" in line:
                key, value = line.split(":", 1)
                parsed[key.strip()] = value.strip()
        memory = {"total": parsed.get("MemTotal"), "available": parsed.get("MemAvailable")}
    except Exception:
        memory = {}
    disk = {}
    try:
        usage = shutil.disk_usage(settings.userdata_root)
        disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
    except Exception:
        disk = {}
    uptime = None
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except Exception:
        uptime = None
    batocera_version = None
    for candidate in (settings.userdata_root / "system" / "batocera.version", Path("/usr/share/batocera/batocera.version")):
        try:
            if candidate.exists():
                batocera_version = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                break
        except Exception:
            continue
    asset_cache = {}
    try:
        cache_status = _rom_metadata_cache_status(settings)
        counts = cache_status.get("counts") if isinstance(cache_status.get("counts"), dict) else {}
        total_assets = int(counts.get("total") or 0)
        complete = bool(cache_status.get("complete"))
        uploaded = bool(cache_status.get("uploaded"))
        pending_total = int((cache_status.get("pending_changes") or {}).get("total") or 0)
        cached_percent = 100.0 if total_assets and complete else (0.0 if not total_assets else 50.0)
        upload_percent = 100.0 if total_assets and uploaded and not pending_total else (0.0 if total_assets else None)
        health = "green" if complete and (uploaded or not pending_total) else "yellow"
        if cache_status.get("rebuilt") or cache_status.get("scan_in_progress"):
            health = "yellow"
        asset_cache = {
            "health": health,
            "cached_percent": cached_percent,
            "uploaded_percent": upload_percent,
            "counts": counts,
            "pending_changes": cache_status.get("pending_changes") or {},
            "last_full_scan_at": cache_status.get("last_full_scan_at"),
            "last_successful_upload_at": cache_status.get("last_successful_upload_at"),
            "scan_in_progress": bool(cache_status.get("scan_in_progress")),
            "needs_upload": bool(cache_status.get("needs_upload")),
        }
    except Exception as error:
        asset_cache = {"health": "red", "error": _format_overmind_error(error)}
    automation_config = _load_automation_config(settings)
    return {
        "hostname": hostname,
        "device_name": hostname,
        "platform": sys.platform,
        "os": os.uname().sysname if hasattr(os, "uname") else sys.platform,
        "os_release": os.uname().release if hasattr(os, "uname") else "",
        "batocera_version": batocera_version,
        "drone_app_version": _drone_app_version(),
        "architecture": os.uname().machine if hasattr(os, "uname") else "",
        "cpu": {"model": os.environ.get("DRONE_CPU_MODEL", ""), "count": os.cpu_count()},
        "memory": memory,
        "disk": disk,
        "gpu": _collect_gpu_info(),
        "performance": _collect_performance_metrics(settings.userdata_root),
        "pixen_installed": is_pixen_installed(settings),
        "pixen_script_path": str(pixen_script_path(settings)),
        "asset_cache": asset_cache,
        "screen_mode": _get_screen_mode(settings),
        "audio_volume": _get_audio_volume(settings),
        "idle_volume_automation": automation_config["idle_volume"],
        "idle_game_exit_automation": automation_config["idle_game_exit"],
        "wifi_recovery_automation": automation_config["wifi_recovery"],
        "network": network,
        "uptime_seconds": uptime,
        "container": Path("/.dockerenv").exists() or os.environ.get("RUNNING_IN_DOCKER") == "1",
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
