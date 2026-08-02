"""RomRequestHandler movies handlers, as a mixin.

Local movie library browsing for the Systems/Assets page: list, stream (range-
aware, for in-browser ``<video>`` playback/seeking), and download. Movies have
no system/artwork association (flat inventory -- see ``storage/movies_store.py``),
so unlike ROMs/BIOS there is no per-system grouping. Routes are plain top-level
``/movies`` (not ``/admin/movies``), matching the ``/systems``/``/bios``
convention: gated by the same session-cookie login as the rest of the browsing
surface, but not behind the ``admin_enabled`` toggle -- browsing your own
library isn't an admin-only feature.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    from ..storage import movies_store as _movies_store
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import movies_store as _movies_store  # type: ignore

# entry_key is always a hex-digest slice computed server-side (see
# movies_store._entry_key) -- this just rejects anything that couldn't
# possibly be one before touching the filesystem.
_ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{1,64}$")


class HandlersMoviesMixin:
    def _handle_movies_list(self, query: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> None:
        """``limit=None`` (no ``limit`` query param) returns the whole
        inventory in one shot -- same convention as ``_handle_rom_list``.
        The Movies tab's folder tree needs the complete set client-side to
        build the on-disk hierarchy (a movie library is far smaller than a
        ROM set, so this is cheap); ``limit``/``offset`` stay available for
        any caller that does want a page instead."""
        if limit is not None:
            safe_limit = max(1, min(int(limit), 2000))
            safe_offset = max(0, int(offset))
            page = _movies_store.list_movies_page(
                self.settings.movies_root, query=str(query or ""), limit=safe_limit, offset=safe_offset
            )
            items = page["items"]
            if not self.settings.downloads_enabled:
                for item in items:
                    item["is_downloadable"] = False
            self._send_json(
                200,
                {
                    "movies": items,
                    "count": page["total"],
                    "offset": page["offset"],
                    "limit": page["limit"],
                    "returned": len(items),
                    "has_more": (page["offset"] + len(items)) < page["total"],
                },
                cache_key=f"json:/movies?limit={safe_limit}&offset={safe_offset}&q={str(query or '').strip().lower()}",
            )
            return
        items = _movies_store.list_movies(self.settings.movies_root)
        query_value = str(query or "").strip().lower()
        if query_value:
            items = [
                item for item in items
                if query_value in " ".join([str(item.get("movie_name") or ""), str(item.get("file_path") or "")]).lower()
            ]
        if not self.settings.downloads_enabled:
            for item in items:
                item["is_downloadable"] = False
        self._send_json(
            200,
            {
                "movies": items,
                "count": len(items),
                "offset": 0,
                "limit": len(items),
                "returned": len(items),
                "has_more": False,
            },
            cache_key=f"json:/movies?q={query_value}",
        )

    def _resolve_movie_path(self, entry_key: str) -> Path:
        """Look up a movie by its stable id and validate the resolved path
        stays inside ``movies_root`` -- same path-traversal discipline as
        every other file-serving handler in this app."""
        if not _ENTRY_KEY_RE.match(str(entry_key or "")):
            raise FileNotFoundError()
        row = _movies_store.get_movie_by_key(self.settings.movies_root, entry_key)
        if not row:
            raise FileNotFoundError()
        movies_root = Path(self.settings.movies_root).resolve()
        target = (movies_root / row["file_path"]).resolve()
        if target == movies_root or movies_root not in target.parents or not target.is_file():
            raise FileNotFoundError()
        return target

    def _handle_movie_download(self, entry_key: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target = self._resolve_movie_path(entry_key)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_movie_stream(self, entry_key: str) -> None:
        """Serve a movie inline for the Systems/Assets page's <video> player.

        Unlike ``_handle_movie_download``/the plain ``_stream_file`` used
        elsewhere, this is Range-aware (206 Partial Content): a movie can be
        gigabytes, and a browser's <video> element needs Range support to
        seek/scrub without re-downloading everything from byte 0. Nothing
        else in this app currently needs that (per-game preview clips served
        via ``_handle_public_video`` are short enough that a plain full-body
        response is fine).
        """
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target = self._resolve_movie_path(entry_key)
        self._stream_movie_range(target, self._guess_content_type(target))

    def _stream_movie_range(self, path: Path, content_type: str) -> None:
        file_size = path.stat().st_size
        start, end, status = 0, file_size - 1, 200
        range_header = self.headers.get("Range")
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
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self._send_security_headers()
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
