"""Pure HTTP ``Range`` header parsing -- no I/O, so it's shared by every
Range-aware file-serving handler in this app (the authenticated movie
stream route and the cast listener's plain-HTTP route alike) without either
one owning the logic.
"""

from __future__ import annotations

from typing import Optional, Tuple


def parse_range_header(range_header: Optional[str], file_size: int) -> Tuple[int, int, int]:
    """Returns ``(start, end, status)`` -- an inclusive byte range plus the
    HTTP status to respond with: ``206`` for a valid partial-content
    request, ``200`` (the whole file, ``start=0``) for no ``Range`` header
    or one that doesn't parse. A malformed ``Range`` header is treated as
    absent rather than an error -- matches every real browser/media
    player's own tolerance for a server that just serves the full body
    instead of honoring a range it didn't understand.
    """
    start, end, status = 0, file_size - 1, 200
    if range_header and range_header.startswith("bytes="):
        try:
            spec = range_header.split("=", 1)[1].split(",")[0].strip()
            range_start, _, range_end = spec.partition("-")
            if range_start:
                candidate_start = int(range_start)
                candidate_end = int(range_end) if range_end else file_size - 1
            elif range_end:
                # Suffix form ("bytes=-500" -- last 500 bytes).
                candidate_start = max(0, file_size - int(range_end))
                candidate_end = file_size - 1
            else:
                raise ValueError("empty range")
            candidate_end = min(candidate_end, file_size - 1)
            if 0 <= candidate_start <= candidate_end:
                start, end, status = candidate_start, candidate_end, 206
        except (ValueError, IndexError):
            pass  # malformed Range -- fall back to the full 200 response
    return start, end, status
