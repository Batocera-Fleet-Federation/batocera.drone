"""Lexical helpers for Drone-owned network-share symlinks.

These helpers deliberately never call :meth:`Path.resolve`.  Resolving a
symlink into a dead CIFS mount can block for a long time or raise before the
caller gets a chance to remove the link.  Ownership checks only need the
stored link text and a trusted local root, so normalising those strings is
both faster and safer.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def network_reference_root() -> Path:
    """Return the stable local root used for Drone-managed network mounts."""
    configured = os.environ.get("DRONE_NETWORK_SHARE_DIR")
    if configured:
        return Path(os.path.abspath(os.path.normpath(configured)))
    try:
        from .install_paths import drone_install_root
    except ImportError:  # pragma: no cover - direct script execution fallback
        from common.install_paths import drone_install_root  # type: ignore
    return drone_install_root() / "network-shares"


def lexical_symlink_target(path: Path) -> Optional[Path]:
    """Return an absolute, normalised target without touching the target."""
    path = Path(path)
    try:
        raw_target = os.readlink(path)
    except OSError:
        return None
    target = Path(raw_target)
    if not target.is_absolute():
        target = path.parent / target
    return Path(os.path.abspath(os.path.normpath(str(target))))


def path_is_within(path: Path, root: Path) -> bool:
    """Lexically test containment without filesystem resolution."""
    path_text = os.path.abspath(os.path.normpath(str(path)))
    root_text = os.path.abspath(os.path.normpath(str(root)))
    try:
        return os.path.commonpath((path_text, root_text)) == root_text
    except ValueError:
        return False


def is_network_reference(path: Path, network_root: Path) -> bool:
    """Whether ``path`` is a symlink owned by Drone's network-share root."""
    target = lexical_symlink_target(path)
    return target is not None and path_is_within(target, network_root)


def symlink_points_to(path: Path, target: Path) -> bool:
    """Compare a symlink target lexically, including dangling symlinks."""
    current = lexical_symlink_target(path)
    if current is None:
        return False
    expected = Path(os.path.abspath(os.path.normpath(str(target))))
    return current == expected
