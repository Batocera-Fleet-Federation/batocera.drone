"""Read the tail of a (log) file — the bytes/lines the log viewer serves.

Extracted from ``drone_api.py``. Reads only the last ``max_bytes`` of a file (seeking
from the end) so multi-MB logs are cheap to preview. Pure stdlib.
"""

import os
from pathlib import Path
from typing import List, Tuple


def _read_file_tail(path: Path, max_bytes: int) -> Tuple[bytes, bool]:
    safe_max = max(1, int(max_bytes))
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(str(path), flags)
    try:
        stat_result = os.fstat(fd)
        size = int(stat_result.st_size)
        start = max(0, size - safe_max)
        if start:
            os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        remaining = min(size, safe_max)
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), size > safe_max
    finally:
        os.close(fd)


def _tail_lines(path: Path, line_count: int, max_bytes: int = 1024 * 1024) -> List[str]:
    raw, truncated = _read_file_tail(path, max_bytes)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    output = lines[-max(1, int(line_count)) :]
    if truncated and output:
        output.insert(0, f"[truncated] showing last {max_bytes} bytes of file")
    return output
