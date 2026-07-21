"""System / device telemetry collection for the Drone.

Extracted from ``drone_api.py``. Gathers the metrics shown on the admin UI: a
bandwidth speed sample, GPU info, live performance metrics (CPU/memory/temp),
mounted-disk usage, and the combined system-info payload. Read-only probes of
/proc, /sys and standard CLI tools. Lives beside ``device_control`` because the
system-info payload reads the current audio volume from it.
"""

import glob
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from urllib.request import Request, urlopen

try:
    from ..common.http_errors import _format_http_error
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.http_errors import _format_http_error  # type: ignore

SPEED_TEST_DEFAULT_BASE_URL = "https://speed.cloudflare.com"
# Previous CPU-time sample; module-global so the next call computes a delta.
_PERFORMANCE_METRICS_LAST_SAMPLE: Optional[dict] = None


def _speed_test_raw_request(url: str, data: Optional[bytes] = None) -> bytes:
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": "batocera-drone-speed-test/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/octet-stream"
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    timeout = max(1, int(os.environ.get("DRONE_SPEED_TEST_TIMEOUT_SECONDS", "15")))
    with urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        return response.read()


def _sample_speed() -> dict:
    """Measure Internet throughput against Cloudflare's public speed-test edge."""
    sampled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    base_url = (
        os.environ.get("DRONE_SPEED_TEST_BASE_URL", SPEED_TEST_DEFAULT_BASE_URL).strip().rstrip("/")
        or SPEED_TEST_DEFAULT_BASE_URL
    )
    source = "cloudflare-speed-test" if base_url == SPEED_TEST_DEFAULT_BASE_URL else "external-speed-test"
    size = max(1024, min(int(os.environ.get("DRONE_SPEED_TEST_BYTES", "1000000")), 25 * 1000 * 1000))
    sample = {
        "upload_mbps": 0,
        "download_mbps": 0,
        "latency_ms": 0,
        "source": source,
        "sampled_at": sampled_at,
        "bytes": size,
    }
    try:
        latency_url = f"{base_url}/__down?bytes=0"
        started = time.monotonic()
        _speed_test_raw_request(latency_url)
        sample["latency_ms"] = int(max(time.monotonic() - started, 0.001) * 1000)

        download_url = f"{base_url}/__down?bytes={size}"
        started = time.monotonic()
        downloaded = _speed_test_raw_request(download_url)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["download_mbps"] = round((len(downloaded) * 8) / elapsed / 1_000_000, 3)

        upload_url = f"{base_url}/__up"
        payload = b"1" * size
        started = time.monotonic()
        _speed_test_raw_request(upload_url, data=payload)
        elapsed = max(time.monotonic() - started, 0.001)
        sample["upload_mbps"] = round((len(payload) * 8) / elapsed / 1_000_000, 3)
    except Exception as error:
        sample["source"] = f"{source}-failed"
        sample["error"] = _format_http_error(error)
    print(f"Speed sample created: source={sample['source']} down={sample['download_mbps']} up={sample['upload_mbps']}", file=sys.stdout, flush=True)
    return sample


def _collect_gpu_info() -> dict:
    info = {
        "vendor": None,
        "model": None,
        "driver": None,
        "renderer": None,
        "pci_devices": [],
    }
    try:
        result = subprocess.run(["lspci", "-nnk"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            current = None
            for line in (result.stdout or "").splitlines():
                lower = line.lower()
                if " vga compatible controller" in lower or " 3d controller" in lower or " display controller" in lower:
                    current = {"description": line.strip(), "driver": None}
                    parts = line.split(":", 2)
                    description = parts[-1].strip() if parts else line.strip()
                    if not info["model"]:
                        info["model"] = description
                    if " nvidia " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "NVIDIA"
                    elif " amd " in f" {lower} " or " advanced micro devices" in lower or " ati " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "AMD"
                    elif " intel " in f" {lower} ":
                        info["vendor"] = info["vendor"] or "Intel"
                    info["pci_devices"].append(current)
                    continue
                if current and "kernel driver in use:" in lower:
                    driver = line.split(":", 1)[1].strip()
                    current["driver"] = driver
                    info["driver"] = info["driver"] or driver
    except Exception:
        pass

    for card in sorted(Path("/sys/class/drm").glob("card*/device")):
        try:
            vendor_id = (card / "vendor").read_text(encoding="utf-8", errors="ignore").strip()
            device_id = (card / "device").read_text(encoding="utf-8", errors="ignore").strip()
            driver = card.resolve().parts[-2] if card.exists() else None
            entry = {"path": str(card), "vendor_id": vendor_id, "device_id": device_id}
            if driver:
                entry["driver"] = driver
            info["pci_devices"].append(entry)
        except Exception:
            continue

    try:
        result = subprocess.run(["sh", "-c", "glxinfo -B 2>/dev/null"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            for line in (result.stdout or "").splitlines():
                if ":" not in line:
                    continue
                key, value = [part.strip() for part in line.split(":", 1)]
                lower = key.lower()
                if lower == "opengl vendor string":
                    info["vendor"] = info["vendor"] or value
                elif lower == "opengl renderer string":
                    info["renderer"] = value
                    info["model"] = info["model"] or value
        elif not info["renderer"]:
            info["renderer"] = None
    except Exception:
        pass

    return info


def _collect_performance_metrics(root: Path) -> dict:
    global _PERFORMANCE_METRICS_LAST_SAMPLE
    now = time.monotonic()
    previous = _PERFORMANCE_METRICS_LAST_SAMPLE
    elapsed = max(0.001, now - float(previous.get("monotonic") or now)) if previous else None

    process_seconds = float(os.times().user + os.times().system)
    total_jiffies = None
    idle_jiffies = None
    try:
        values = [int(part) for part in Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()[1:]]
        total_jiffies = sum(values)
        idle_jiffies = values[3] + (values[4] if len(values) > 4 else 0)
    except Exception:
        pass

    memory = {}
    try:
        parsed = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            parsed[key] = int(raw.strip().split()[0]) * 1024
        total = int(parsed.get("MemTotal") or 0)
        available = int(parsed.get("MemAvailable") or 0)
        used = max(0, total - available) if total else 0
        memory = {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_percent": round((used / total) * 100, 2) if total else None,
        }
    except Exception:
        memory = {}

    process_memory = {}
    try:
        values = {}
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(("VmRSS:", "VmSize:")):
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
        process_memory = {"rss_bytes": values.get("VmRSS"), "vms_bytes": values.get("VmSize")}
    except Exception:
        process_memory = {}

    diskstats = {}
    try:
        totals = {"read_bytes": 0, "write_bytes": 0, "weighted_io_ms": 0}
        for line in Path("/proc/diskstats").read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = line.split()
            if len(parts) < 14 or parts[2].startswith(("loop", "ram", "fd")):
                continue
            totals["read_bytes"] += int(parts[5]) * 512
            totals["write_bytes"] += int(parts[9]) * 512
            totals["weighted_io_ms"] += int(parts[13])
        diskstats = totals
    except Exception:
        diskstats = {}

    process_cpu_percent = None
    host_cpu_percent = None
    disk_rates = {}
    if previous and elapsed:
        process_delta = process_seconds - float(previous["cpu"].get("process_seconds") or 0)
        process_cpu_percent = round(max(0.0, process_delta / elapsed * 100 / max(1, os.cpu_count() or 1)), 2)
        if total_jiffies is not None and previous["cpu"].get("total_jiffies") is not None:
            total_delta = int(total_jiffies) - int(previous["cpu"]["total_jiffies"])
            idle_delta = int(idle_jiffies or 0) - int(previous["cpu"]["idle_jiffies"] or 0)
            if total_delta > 0:
                host_cpu_percent = round(max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)), 2)
        if diskstats and previous.get("diskstats"):
            prev_disk = previous["diskstats"]
            read_delta = max(0, diskstats.get("read_bytes", 0) - prev_disk.get("read_bytes", 0))
            write_delta = max(0, diskstats.get("write_bytes", 0) - prev_disk.get("write_bytes", 0))
            weighted_delta = max(0, diskstats.get("weighted_io_ms", 0) - prev_disk.get("weighted_io_ms", 0))
            disk_rates = {
                "read_bytes_per_second": round(read_delta / elapsed, 2),
                "write_bytes_per_second": round(write_delta / elapsed, 2),
                "contention_percent": round(max(0.0, min(100.0, weighted_delta / (elapsed * 1000) * 100)), 2),
            }

    disks = _collect_mounted_disk_metrics(root)
    disk = dict(disks[0]) if disks else {}
    disk.update(disk_rates)

    sample = {
        "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "cpu": {
            "process_percent": process_cpu_percent,
            "host_percent": host_cpu_percent,
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "cpu_count": os.cpu_count(),
            "process_seconds": process_seconds,
            "total_jiffies": total_jiffies,
            "idle_jiffies": idle_jiffies,
        },
        "memory": memory,
        "process": process_memory,
        "disk": disk,
        "disks": disks,
        "diskstats": diskstats,
        "monotonic": now,
    }
    _PERFORMANCE_METRICS_LAST_SAMPLE = sample
    public_cpu = {key: value for key, value in sample["cpu"].items() if key not in {"process_seconds", "total_jiffies", "idle_jiffies"}}
    return {
        "collected_at": sample["collected_at"],
        "cpu": public_cpu,
        "memory": memory,
        "process": process_memory,
        "disk": disk,
        "disks": disks,
    }


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _collect_mounted_disk_metrics(root: Path, mountinfo_path: Path = Path("/proc/self/mountinfo")) -> List[dict]:
    """Collect capacity metrics for the main data filesystem and mounted physical drives."""
    root = root.resolve()
    try:
        main_device = root.stat().st_dev
    except OSError:
        main_device = None

    candidates = []
    try:
        for line in mountinfo_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            before, after = line.split(" - ", 1)
            fields = before.split()
            trailing = after.split()
            if len(fields) < 5 or len(trailing) < 2:
                continue
            mount_path = Path(_decode_mountinfo_path(fields[4]))
            source = _decode_mountinfo_path(trailing[1])
            if not source.startswith("/dev/"):
                continue
            candidates.append((mount_path, trailing[0], source))
    except (OSError, ValueError):
        candidates = []

    # Always include the configured userdata filesystem even when procfs is unavailable
    # or Batocera exposes it through a non-/dev mount source.
    candidates.insert(0, (root, "", ""))
    rows = []
    by_device = {}
    for mount_path, filesystem, source in candidates:
        try:
            stat = mount_path.stat()
            usage = shutil.disk_usage(mount_path)
        except OSError:
            continue
        device_id = stat.st_dev
        existing_index = by_device.get(device_id)
        if existing_index is not None:
            existing = rows[existing_index]
            if source and not existing.get("source"):
                existing["source"] = source
            if filesystem and not existing.get("filesystem"):
                existing["filesystem"] = filesystem
            continue
        by_device[device_id] = len(rows)
        is_main = main_device is not None and device_id == main_device
        label = "Main drive" if is_main else (mount_path.name or source or str(mount_path))
        rows.append({
            "label": label,
            "path": str(mount_path),
            "source": source or None,
            "filesystem": filesystem or None,
            "is_main": is_main,
            "is_external": not is_main,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": round((usage.used / usage.total) * 100, 2) if usage.total else None,
        })
    rows.sort(key=lambda row: (not row["is_main"], str(row["label"]).lower(), str(row["path"]).lower()))
    return rows


def _read_text_file(path: Path, max_bytes: int = 262144) -> dict:
    try:
        raw = path.read_bytes()[:max_bytes + 1]
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        return {
            "path": str(path),
            "size": path.stat().st_size,
            "truncated": truncated,
            "content": raw.decode("utf-8", errors="replace"),
        }
    except Exception as error:
        return {"path": str(path), "error": str(error)}
