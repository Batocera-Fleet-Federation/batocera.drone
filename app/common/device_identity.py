"""Stable per-device machine identity for the Drone.

Extracted from ``drone_api.py``. This Drone's own device id is derived from the first
physical NIC MAC (virtual interfaces such as docker/veth/wg are skipped and
locally-administered MACs de-prioritised), then persisted under ``USERDATA_ROOT``
so it stays stable across reboots and NIC enumeration order; if no physical NIC is
found it falls back to ``uuid.getnode()``.

Pure stdlib, no Drone-internal dependencies.
"""

import os
import re
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{3,128}$")
_VIRTUAL_INTERFACE_PREFIXES = (
    "br-",
    "docker",
    "dummy",
    "ip6tnl",
    "sit",
    "tap",
    "tun",
    "veth",
    "virbr",
    "wg",
    "zt",
)
_VIRTUAL_INTERFACE_NAMES = {"lo", "bonding_masters"}
_PHYSICAL_INTERFACE_PRIORITIES = (
    "eth",
    "en",
    "wlan",
    "wl",
)


def _normalize_device_id(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip()
    if not normalized or not _DEVICE_ID_PATTERN.match(normalized):
        return None
    return normalized


def _device_id_path(userdata_root: Path) -> Path:
    return Path(os.environ.get("DRONE_DEVICE_ID_FILE", str(userdata_root / "system" / "drone-app" / "device-id")))


def _read_persisted_machine_id(userdata_root: Path) -> Optional[str]:
    try:
        return _normalize_device_id(_device_id_path(userdata_root).read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_persisted_machine_id(userdata_root: Path, value: str) -> None:
    normalized = _normalize_device_id(value)
    if not normalized:
        return
    try:
        path = _device_id_path(userdata_root)
        if not path.parent.exists():
            return
        path.write_text(f"{normalized}\n", encoding="utf-8")
    except OSError:
        return


def _interface_priority(name: str, has_device: bool, mac: str) -> Tuple[int, int, str]:
    prefix_index = next((index for index, prefix in enumerate(_PHYSICAL_INTERFACE_PRIORITIES) if name.startswith(prefix)), len(_PHYSICAL_INTERFACE_PRIORITIES))
    first_octet = int(mac.split(":", 1)[0], 16)
    locally_administered = 1 if first_octet & 0x02 else 0
    return (0 if has_device else 1, prefix_index + locally_administered, name)


def _physical_mac_candidates(sys_class_net: Path = Path("/sys/class/net")) -> List[str]:
    candidates: List[Tuple[Tuple[int, int, str], str]] = []
    try:
        interfaces = list(sys_class_net.iterdir())
    except OSError:
        return []
    for interface in interfaces:
        name = interface.name
        if name in _VIRTUAL_INTERFACE_NAMES or name.startswith(_VIRTUAL_INTERFACE_PREFIXES):
            continue
        try:
            mac = (interface / "address").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if not re.match(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", mac) or mac == "00:00:00:00:00:00":
            continue
        candidates.append((_interface_priority(name, (interface / "device").exists(), mac), mac))
    candidates.sort(key=lambda row: row[0])
    return [mac for _, mac in candidates]


def _runtime_machine_id() -> str:
    node = uuid.getnode()
    return ":".join(f"{(node >> shift) & 0xff:02x}" for shift in range(40, -1, -8))


def _machine_id(userdata_root: Optional[Path] = None) -> str:
    if userdata_root is None:
        return _runtime_machine_id()
    persisted = _read_persisted_machine_id(userdata_root)
    if persisted:
        return persisted
    generated = next(iter(_physical_mac_candidates()), None) or _runtime_machine_id()
    _write_persisted_machine_id(userdata_root, generated)
    return generated


def _fake_machine_id() -> str:
    return _machine_id()
