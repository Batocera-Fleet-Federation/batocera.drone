"""Small filesystem/ID helpers shared by integrations."""

import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple


_HEX_ID = re.compile(r"^[a-f0-9]{32}$")


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600, owner: Optional[Tuple[int, int]] = None) -> None:
    """Atomically replace ``path``; ``owner`` (uid, gid) hands the file over before it appears."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            if owner is not None:
                try:
                    info = os.fstat(handle.fileno())
                    # mkstemp is exclusive; still refuse to give away a linked inode.
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        os.fchown(handle.fileno(), owner[0], owner[1])
                except (AttributeError, OSError):
                    pass  # best effort: the writer may not be allowed to give files away
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600, owner: Optional[Tuple[int, int]] = None) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    atomic_write_bytes(path, data.encode("utf-8"), mode=mode, owner=owner)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.is_symlink():
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def hex_id(value: Any, label: str = "id") -> str:
    normalized = str(value if value is not None else "").strip().lower()
    if not _HEX_ID.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def new_id() -> str:
    return uuid.uuid4().hex


def clean_text(value: Any, limit: int) -> str:
    text = "".join(ch for ch in str(value if value is not None else "") if ch.isprintable())
    return text.strip()[:limit]
