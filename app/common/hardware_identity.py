"""Physical-machine fingerprint used for clone detection.

``HardwareIdentityProvider`` reads the identifiers a machine genuinely owns
(DMI product UUID, DMI board/product/chassis serials, Raspberry Pi / device-tree
serial), rejects placeholder and known non-unique values, and returns a
versioned, hashed ``HardwareSnapshot``. ``compare_hardware`` then turns a stored
snapshot and a current one into one of four verdicts -- same machine, different
machine, insufficient evidence, or contradictory evidence -- so the caller never
mistakes a *missing* identifier for *new* hardware.

Deliberately **not** used as identity: hostname, IP, a lone MAC address, disk or
filesystem UUIDs, ``/etc/machine-id`` and anything stored only on the (cloneable)
disk. Raw serials never leave this module: only per-kind SHA-256 digests are kept
and only short prefixes are ever shown.

Pure stdlib, no Drone-internal dependencies.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

FINGERPRINT_VERSION = 1

# Verdicts from compare_hardware().
SAME = "same"
DIFFERENT = "different"
INSUFFICIENT = "insufficient"
CONTRADICTORY = "contradictory"
BASELINE = "baseline"  # nothing stored yet, current evidence is usable

# "Primary" identifiers are unique per unit by design. "Secondary" serials are
# frequently left at a vendor-wide value, so they only decide when no primary
# identifier can be compared.
PRIMARY_KINDS = ("dmi_product_uuid", "platform_serial")
SECONDARY_KINDS = ("dmi_board_serial", "dmi_product_serial", "dmi_chassis_serial")
ALL_KINDS = PRIMARY_KINDS + SECONDARY_KINDS

_PLACEHOLDER_TEXT = {
    "", "none", "null", "nil", "n/a", "na", "unknown", "undefined", "default", "default string", "empty",
    "invalid", "oem", "o.e.m.", "to be filled by o.e.m.", "to be filled by oem", "not specified",
    "not settable", "notsettable", "not defined", "notdefined", "mb-1234567890",
    "not applicable", "not available", "not present", "not set", "system serial number",
    "system product name", "serial number", "chassis serial number", "base board serial number",
    "type2 - board serial number", "type1 - serial number", "board serial number", "product serial number",
    "123456789", "1234567890", "0123456789", "0123456789abcdef", "123456789abcdef", "0123456789ab",
    "xxxxxxxx", "to be filled by o.e.m", "tobefilledbyo.e.m.", "sn1234567890", "ser1234567890",
    "chassis manufacturer", "chassis version", "all series", "ffffffffffffffff",
}
_PLACEHOLDER_UUIDS = {
    "00000000-0000-0000-0000-000000000000",
    "ffffffff-ffff-ffff-ffff-ffffffffffff",
    "03000200-0400-0500-0006-000700080009",
    "00020003-0004-0005-0006-000700080009",
    "12345678-1234-5678-90ab-cddeff001122",
    "11111111-2222-3333-4444-555555555555",
    "01234567-89ab-cdef-0123-456789abcdef",
    "00000000-0000-0000-0000-000000000001",
}
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_MAX_RAW_LENGTH = 128
_MIN_SERIAL_LENGTH = 6
_VIRTUAL_VENDOR_MARKERS = (
    "qemu", "kvm", "vmware", "virtualbox", "innotek", "xen", "bochs", "parallels", "hyper-v",
    "microsoft corporation", "amazon ec2", "google compute", "openstack", "bhyve",
)


def normalize_identifier(kind: str, raw: object) -> Optional[str]:
    """Return a canonical identifier, or ``None`` for empty/placeholder/non-unique values."""
    if raw is None:
        return None
    text = "".join(ch for ch in str(raw).replace("\x00", " ") if ch.isprintable()).strip()
    if not text or len(text) > _MAX_RAW_LENGTH:
        return None
    value = re.sub(r"\s+", " ", text).lower()
    if value in _PLACEHOLDER_TEXT:
        return None
    if kind == "dmi_product_uuid":
        if not _UUID_PATTERN.match(value) or value in _PLACEHOLDER_UUIDS:
            return None
        compact = value.replace("-", "")
    else:
        compact = re.sub(r"[\s\-_.:/]", "", value)
        if len(compact) < _MIN_SERIAL_LENGTH or value in _PLACEHOLDER_UUIDS:
            return None
    if len(set(compact)) < 3:  # 0000..., ffff..., 1212...
        return None
    if compact in {"0123456789abcdef", "123456789abcdef0", "fedcba9876543210"}:
        return None
    if re.fullmatch(r"(?:0123456789|1234567890|123456789|12345678)+", compact):
        return None
    return value


def _digest(kind: str, value: str) -> str:
    return hashlib.sha256(f"drone-hardware-v{FINGERPRINT_VERSION}|{kind}|{value}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HardwareSnapshot:
    """Hashed hardware evidence. ``components`` maps kind -> sha256 hex digest."""

    components: Dict[str, str] = field(default_factory=dict)
    version: int = FINGERPRINT_VERSION
    ambiguous: bool = False
    metadata: Dict[str, object] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.components) and not self.ambiguous

    @property
    def fingerprint(self) -> Optional[str]:
        """Aggregate SHA-256 over the strong components, or ``None`` without evidence."""
        if not self.components:
            return None
        material = "|".join(f"{kind}={self.components[kind]}" for kind in sorted(self.components))
        return hashlib.sha256(f"drone-hardware-fingerprint-v{self.version}|{material}".encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "version": self.version,
            "components": dict(self.components),
            "metadata": dict(self.metadata),
        }

    def describe(self) -> dict:
        """Diagnostics safe to log/return: kinds and short digest prefixes only."""
        return {
            "fingerprint_prefix": (self.fingerprint or "")[:12] or None,
            "version": self.version,
            "kinds": sorted(self.components),
            "ambiguous": self.ambiguous,
            "virtualized": bool(self.metadata.get("virtualized")),
            "container": bool(self.metadata.get("container")),
            "architecture": self.metadata.get("architecture"),
            "unavailable_reason": self.metadata.get("unavailable_reason"),
        }

    @classmethod
    def from_dict(cls, raw: object) -> Optional["HardwareSnapshot"]:
        if not isinstance(raw, dict):
            return None
        components = raw.get("components")
        if not isinstance(components, dict):
            return None
        clean = {
            str(kind): str(digest).lower()
            for kind, digest in components.items()
            if str(kind) in ALL_KINDS and re.fullmatch(r"[0-9a-fA-F]{64}", str(digest))
        }
        try:
            version = int(raw.get("version") or FINGERPRINT_VERSION)
        except (TypeError, ValueError):
            version = FINGERPRINT_VERSION
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        return cls(components=clean, version=version, metadata=dict(metadata))


class HardwareIdentityProvider:
    """Collect machine-specific identifiers from sysfs/procfs (rooted at ``root``).

    ``root`` defaults to ``/`` and can be pointed at a fixture tree with
    ``DRONE_HARDWARE_ID_ROOT`` (tests, emulated cloned hardware). Containers
    share the *host's* DMI data, so they never yield authoritative evidence.
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        container_probe: Optional[Callable[[], bool]] = None,
        machine: Optional[str] = None,
    ):
        configured = os.environ.get("DRONE_HARDWARE_ID_ROOT")
        self._fixture_root = root is not None or bool(configured)
        self.root = Path(root if root is not None else (configured or "/"))
        self._container_probe = container_probe
        self._machine = machine

    # ---------------------------------------------------------------- reading
    def _read(self, relative: str) -> Optional[str]:
        try:
            with open(self.root / relative, "rb") as handle:
                return handle.read(512).decode("utf-8", errors="ignore")
        except OSError:
            return None

    def _in_container(self) -> bool:
        if self._container_probe is not None:
            return bool(self._container_probe())
        if self._fixture_root:
            return False
        if os.environ.get("DRONE_CONTAINER", "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
            return True
        try:
            cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            return False
        return any(marker in cgroup for marker in ("docker", "containerd", "kubepods", "libpod", "lxc"))

    def _cpuinfo_serial(self) -> Optional[str]:
        try:
            with open(self.root / "proc/cpuinfo", "rb") as handle:
                text = handle.read(65536).decode("utf-8", errors="ignore")
        except OSError:
            return None
        serials = set()
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip().lower() == "serial":
                serials.add(value.strip())
        if len(serials) > 1:
            return "\x00ambiguous"
        return next(iter(serials), None)

    # ---------------------------------------------------------------- collect
    def collect(self, version: int = FINGERPRINT_VERSION) -> HardwareSnapshot:
        if version != FINGERPRINT_VERSION:
            return HardwareSnapshot(version=version, metadata={"unavailable_reason": "unsupported_fingerprint_version"})
        if os.environ.get("DRONE_HARDWARE_IDENTITY_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            return HardwareSnapshot(metadata={"unavailable_reason": "disabled"})
        machine = self._machine or (os.uname().machine if hasattr(os, "uname") else "")
        metadata: Dict[str, object] = {"architecture": machine}
        if self._in_container():
            metadata.update(container=True, unavailable_reason="container_shares_host_hardware")
            return HardwareSnapshot(metadata=metadata)

        sys_vendor = (self._read("sys/class/dmi/id/sys_vendor") or "").strip().lower()
        product_name = (self._read("sys/class/dmi/id/product_name") or "").strip().lower()
        metadata["virtualized"] = any(marker in f"{sys_vendor} {product_name}" for marker in _VIRTUAL_VENDOR_MARKERS)

        raw: Dict[str, Optional[str]] = {
            "dmi_product_uuid": self._read("sys/class/dmi/id/product_uuid"),
            "dmi_board_serial": self._read("sys/class/dmi/id/board_serial"),
            "dmi_product_serial": self._read("sys/class/dmi/id/product_serial"),
            "dmi_chassis_serial": self._read("sys/class/dmi/id/chassis_serial"),
        }
        # Raspberry Pi and most ARM boards expose the SoC/board serial twice;
        # two disagreeing sources are ambiguous, not "the newer one wins".
        devicetree = self._read("sys/firmware/devicetree/base/serial-number")
        cpuinfo = self._cpuinfo_serial()
        ambiguous = False
        platform_candidates = []
        if devicetree is not None:
            platform_candidates.append(normalize_identifier("platform_serial", devicetree.strip("\x00 \n")))
        if cpuinfo == "\x00ambiguous":
            ambiguous = True
        elif cpuinfo is not None:
            platform_candidates.append(normalize_identifier("platform_serial", cpuinfo))
        valid_platform = {value for value in platform_candidates if value}
        if len(valid_platform) > 1:
            ambiguous = True
        elif valid_platform:
            raw["platform_serial"] = next(iter(valid_platform))

        components: Dict[str, str] = {}
        for kind, value in raw.items():
            normalized = normalize_identifier(kind, value)
            if normalized:
                components[kind] = _digest(kind, normalized)
        if not components and not ambiguous:
            metadata["unavailable_reason"] = "no_unique_hardware_identifier"
        elif ambiguous:
            metadata["unavailable_reason"] = "conflicting_hardware_identifiers"
        return HardwareSnapshot(components=components, ambiguous=ambiguous, metadata=metadata)


def compare_hardware(stored: Optional[HardwareSnapshot], current: HardwareSnapshot) -> str:
    """Judge whether ``current`` is the machine ``stored`` was recorded on."""
    if current.ambiguous:
        return CONTRADICTORY
    if stored is None or not stored.components:
        return BASELINE if current.components else INSUFFICIENT
    if not current.components:
        return INSUFFICIENT
    if stored.version != current.version:
        return INSUFFICIENT

    def pairs(kinds):
        return [kind for kind in kinds if kind in stored.components and kind in current.components]

    primary = pairs(PRIMARY_KINDS)
    secondary = pairs(SECONDARY_KINDS)
    if primary:
        equal = [stored.components[kind] == current.components[kind] for kind in primary]
        if all(equal):
            # The unit-unique identifier agrees; a differing secondary serial is odd enough to
            # need a human rather than silently trusting either reading.
            if any(stored.components[kind] != current.components[kind] for kind in secondary):
                return CONTRADICTORY
            return SAME
        if not any(equal):
            return DIFFERENT
        return CONTRADICTORY
    if secondary:
        equal = [stored.components[kind] == current.components[kind] for kind in secondary]
        if all(equal):
            return SAME
        if not any(equal):
            return DIFFERENT
        return CONTRADICTORY
    return INSUFFICIENT
