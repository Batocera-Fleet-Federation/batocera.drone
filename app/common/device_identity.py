"""Stable per-device machine identity for the Drone.

Extracted from ``drone_api.py``. This Drone's own device id is a random, locally
administered, MAC-shaped value generated on first boot (never the NIC's MAC, so two
installs on one host do not share it), then persisted under ``USERDATA_ROOT`` so it stays
stable across reboots. Installations that already persisted an id keep it. If the id
cannot be persisted, the physical NIC MAC (or ``uuid.getnode()``) is used as before.

Pure stdlib, no Drone-internal dependencies.
"""

import os
import re
import secrets
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
    override = os.environ.get("DRONE_DEVICE_ID_FILE")
    if override is not None:
        _require_real_path(override, "DRONE_DEVICE_ID_FILE")
        if not os.path.isabs(os.fspath(override)):
            raise TypeError("DRONE_DEVICE_ID_FILE must be an absolute filesystem path")
        _require_real_path(userdata_root, "userdata_root")
        # An explicit absolute file override is independent of userdata_root;
        # the identity file cannot resolve through cwd in that case.
        return Path(override)
    _require_real_path(userdata_root, "userdata_root")
    # Without an override, a relative root would read or persist identity under cwd.
    if not os.path.isabs(os.fspath(userdata_root)):
        raise TypeError("userdata_root must be an absolute filesystem path")
    return Path(userdata_root) / "system" / "drone-app" / "device-id"


def _is_mock_path(value: object) -> bool:
    """Return whether *value* is a unittest mock, including spec masquerades."""
    try:
        from unittest import mock
    except ImportError:  # pragma: no cover - unittest is part of supported Python
        return type(value).__module__ == "unittest.mock"
    mock_types = tuple(
        candidate
        for name in ("Mock", "MagicMock", "NonCallableMock", "NonCallableMagicMock")
        if isinstance((candidate := getattr(mock, name, None)), type)
    )
    return type(value).__module__ == "unittest.mock" or isinstance(value, mock_types)


def _require_real_path(value: object, label: str) -> None:
    """Reject unittest mocks before pathlib can turn their repr into a cwd path."""
    if _is_mock_path(value):
        raise TypeError(f"{label} must be a filesystem path")
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"{label} must be a filesystem path")


def _read_persisted_machine_id(userdata_root: Path) -> Optional[str]:
    try:
        return _normalize_device_id(_device_id_path(userdata_root).read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_persisted_machine_id(userdata_root: Path, value: str) -> bool:
    normalized = _normalize_device_id(value)
    if not normalized:
        return False
    try:
        path = _device_id_path(userdata_root)
        if not path.parent.exists():
            return False
        path.write_text(f"{normalized}\n", encoding="utf-8")
    except OSError:
        return False
    return True


def random_device_id() -> str:
    """A random, locally administered, MAC-shaped logical id (not derived from the NIC)."""
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] | 0x02) & 0xFE
    return ":".join(f"{octet:02x}" for octet in octets)


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
    path = _device_id_path(userdata_root)
    persisted = _normalize_device_id(_read_persisted_machine_id(userdata_root))
    if persisted:
        return persisted
    # The first boot must not adopt the NIC's MAC: installs on one host would share it and
    # refuse each other as "this Drone itself". The random id is persisted before use so
    # every later start agrees. If it cannot be persisted, fall back to the NIC-derived id
    # instead of one that would change on every start.
    generated = random_device_id()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if _write_persisted_machine_id(userdata_root, generated):
        return generated
    return next(iter(_physical_mac_candidates()), None) or _runtime_machine_id()


def _fake_machine_id(userdata_root: Path) -> str:
    # Fake-data mode gets the same persisted random id as a real install, scoped to its
    # USERDATA_ROOT, so two fake-data processes on one host do not share a host-derived id.
    # The root is required: _machine_id(None) returns the NIC-derived id, which is exactly
    # what this helper must never hand out, so reject a missing root instead of defaulting.
    _require_real_path(userdata_root, "userdata_root")
    # This helper's public internal contract is Path (Settings.from_env supplies
    # one); retain that boundary instead of accepting str/PathLike objects that
    # cannot participate in the root path composition used by the identity API.
    userdata_root / "system"
    _device_id_path(userdata_root)
    return _machine_id(userdata_root)
