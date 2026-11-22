"""Shared filesystem free-space and ENOSPC helpers for backup asset restore and downloads."""

from __future__ import annotations

import errno
import os
import shutil
from typing import Any, Optional

ASSET_RESERVE_ENV = "DRONE_ASSET_RESTORE_RESERVE_BYTES"
DEFAULT_MIN_RESERVE_BYTES = 1024 ** 3  # 1 GiB
DEFAULT_RESERVE_FRACTION = 0.05
ARTWORK_ALLOWANCE_BYTES = 2 * 1024 * 1024  # Conservative per-item allowance when peer omits artwork size


def reserve_bytes(total_bytes: int = 0) -> int:
    """Safety reserve for a filesystem: ``max(1 GiB, 5%)`` unless ``DRONE_ASSET_RESTORE_RESERVE_BYTES`` is set."""
    raw = os.environ.get(ASSET_RESERVE_ENV)
    if raw is not None and raw.strip() != "":
        try:
            return max(0, int(raw.strip()))
        except ValueError:
            pass
    total = total_bytes if isinstance(total_bytes, int) and not isinstance(total_bytes, bool) and total_bytes > 0 else 0
    return max(DEFAULT_MIN_RESERVE_BYTES, int(total * DEFAULT_RESERVE_FRACTION))


def _valid_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def probe_filesystem(path: Any, *, usage_fn=None) -> Optional[dict]:
    """Return ``{path, mount, free, total, st_dev, group}`` for ``path``'s filesystem, or ``None``."""
    if path is None:
        return None
    probe = os.path.abspath(str(path))
    while probe and not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    if not probe or not os.path.exists(probe):
        return None
    disk_usage = usage_fn or shutil.disk_usage
    try:
        usage = disk_usage(probe)
        st = os.stat(probe)
    except OSError:
        return None
    free = _valid_int(getattr(usage, "free", 0))
    total = _valid_int(getattr(usage, "total", 0))
    dev = st.st_dev
    mount = probe
    cur = probe
    for _ in range(64):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            mount = cur
            break
        try:
            if os.path.ismount(cur) or os.stat(parent).st_dev != dev:
                mount = cur
                break
        except OSError:
            mount = cur
            break
        cur = parent
        mount = cur
    return {
        "path": str(path),
        "mount": mount,
        "free": free,
        "total": total,
        "st_dev": dev,
        "group": str(dev),
    }


def is_enospc(error: Optional[BaseException]) -> bool:
    """True if ``error`` (or its cause/context/reason) is an ``ENOSPC`` disk-full error."""
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, OSError) and getattr(current, "errno", None) == errno.ENOSPC:
            return True
        if "no space left on device" in str(current).lower():
            return True
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and id(reason) not in seen:
            if is_enospc(reason):
                return True
        current = current.__cause__ or current.__context__
    return False
