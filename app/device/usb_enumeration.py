"""Warn when the kernel gave up enumerating a USB device on a root port.

A Wii U / GameCube adapter (``057e:0337``, USB 1.1 full-speed) can fail to
enumerate when plugged straight into some xHCI root ports (``device
descriptor read/64, error -71`` ... ``unable to enumerate USB device``).  The
kernel never creates a sysfs node, so no udev rule (and no
``wii-u-gc-adapter-daemon``) ever runs, and nothing in userspace explains why
the controllers are missing.  Behind a USB 2.0 hub the same adapter enumerates
fine, so the useful advice is "use a different port or a powered hub".

This module is strictly read-only: it parses ``dmesg`` and reads
``/sys/bus/usb/devices``.  It never writes to USB sysfs (no ``unbind``,
``authorized``, rescan) -- poking a port that the kernel already power-cycled
adds nothing and can disturb working devices.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from . import notifications as _notifications
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from device import notifications as _notifications  # type: ignore

_LOG = logging.getLogger(__name__)

STATE_NAMESPACE = "usb_enumeration_notified.json"
USB_SYSFS_ROOT = Path("/sys/bus/usb/devices")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
# Check this often at most: the poller ticks every few seconds, dmesg is not free.
CHECK_INTERVAL_SECONDS = 300.0

ADAPTER_ID = ("057e", "0337")
ADAPTER_NAME = "Wii U / GameCube adapter"

# "usb usb1-port1: unable to enumerate USB device" is the root port's verdict
# after every retry and power cycle.  Hub ports are named "1-1-port2" and are
# deliberately not matched: those sit behind a hub, which is the working setup.
_ROOT_PORT_FAILURE = re.compile(r"\busb (usb(\d+)-port(\d+)): unable to enumerate USB device")
_TIMESTAMP = re.compile(r"^\s*\[\s*(\d+(?:\.\d+)?)\]")

_last_check_monotonic: Optional[float] = None


def read_kernel_log() -> str:
    try:
        return subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def find_root_port_failures(dmesg_text: str) -> List[Dict[str, Any]]:
    """Return one entry per root port the kernel could not enumerate a device on."""
    failures: Dict[str, Dict[str, Any]] = {}
    for line in dmesg_text.splitlines():
        match = _ROOT_PORT_FAILURE.search(line)
        if not match:
            continue
        port = f"{match.group(2)}-{match.group(3)}"
        stamp = _TIMESTAMP.match(line)
        # Last one wins: it is the most recent failure on that port.
        failures[port] = {
            "port": port,
            "bus": int(match.group(2)),
            "root_port": int(match.group(3)),
            "kernel_port": match.group(1),
            "uptime": float(stamp.group(1)) if stamp else None,
            "line": line.strip(),
        }
    return list(failures.values())


def adapter_present(sysfs_root: Optional[Path] = None) -> bool:
    """True when a ``057e:0337`` device has a sysfs node, i.e. it did enumerate."""
    root = USB_SYSFS_ROOT if sysfs_root is None else sysfs_root
    try:
        entries = list(root.iterdir())
    except OSError:
        return False
    for entry in entries:
        try:
            vendor = (entry / "idVendor").read_text(encoding="ascii", errors="replace").strip().lower()
            product = (entry / "idProduct").read_text(encoding="ascii", errors="replace").strip().lower()
        except OSError:
            continue
        if (vendor, product) == ADAPTER_ID:
            return True
    return False


def _controller_address(bus: int, sysfs_root: Optional[Path] = None) -> str:
    """PCI address of the xHCI controller behind ``usb<bus>``, or '' if unknown."""
    root = USB_SYSFS_ROOT if sysfs_root is None else sysfs_root
    try:
        resolved = str((root / f"usb{bus}").resolve())
    except OSError:
        return ""
    matches = re.findall(r"/([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])(?=/)", resolved)
    return matches[-1] if matches else ""


def _boot_key() -> str:
    try:
        return BOOT_ID_PATH.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        pass
    try:
        for line in Path("/proc/stat").read_text(encoding="ascii", errors="replace").splitlines():
            if line.startswith("btime "):
                return line.split()[1]
    except OSError:
        pass
    return ""


def _describe(failure: Dict[str, Any], controller: str) -> Dict[str, str]:
    where = f"USB port {failure['port']}" + (f" (controller {controller})" if controller else "")
    title = f"A USB device on {where} failed to start"
    message = (
        f"The kernel could not enumerate a device on {where}, and no {ADAPTER_NAME} is detected. "
        f"If that was the {ADAPTER_NAME}, plug it into a different USB port or a powered USB hub "
        "(a hub's full-speed handling often fixes this), then replug it."
    )
    return {"title": title, "message": message}


def check_usb_enumeration(
    settings: Settings,
    dmesg_text: Optional[str] = None,
    sysfs_root: Optional[Path] = None,
) -> int:
    """Record a ``usb_enumeration_failed`` event per newly failed root port.

    Quiet when the adapter is present (it enumerated somewhere, so the leftover
    kernel line is about something else) and when nothing failed.  Each port is
    reported once per boot.  Returns how many events were recorded.
    """
    failures = find_root_port_failures(read_kernel_log() if dmesg_text is None else dmesg_text)
    if not failures or adapter_present(sysfs_root):
        return 0
    database = _state_database_path(settings.userdata_root)
    boot = _boot_key()
    stored = _load_state_payload(database, STATE_NAMESPACE, {})
    notified: List[str] = []
    if isinstance(stored, dict) and stored.get("boot") == boot and isinstance(stored.get("ports"), list):
        notified = [str(port) for port in stored["ports"]]
    recorded = 0
    for failure in failures:
        if failure["port"] in notified:
            continue
        controller = _controller_address(failure["bus"], sysfs_root)
        text = _describe(failure, controller)
        _notifications.record_event(
            settings,
            "usb_enumeration_failed",
            text["title"],
            message=text["message"],
            details={**failure, "controller": controller, "device_id": settings.device_id},
        )
        # Persist per port so a later failure never re-reports an earlier one.
        notified.append(failure["port"])
        _save_state_payload(database, STATE_NAMESPACE, {"boot": boot, "ports": notified})
        recorded += 1
    return recorded


def check_usb_enumeration_throttled(settings: Settings) -> int:
    """``check_usb_enumeration`` at most once per ``CHECK_INTERVAL_SECONDS``."""
    global _last_check_monotonic
    now = time.monotonic()
    if _last_check_monotonic is not None and now - _last_check_monotonic < CHECK_INTERVAL_SECONDS:
        return 0
    _last_check_monotonic = now
    return check_usb_enumeration(settings)
