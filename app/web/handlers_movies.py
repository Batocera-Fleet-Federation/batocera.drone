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
    from ..movies import metadata_manager as _movies_metadata
    from ..movies.tmdb_client import TmdbUnavailableError as _TmdbUnavailableError
except ImportError:  # pragma: no cover - direct script execution fallback
    from storage import movies_store as _movies_store  # type: ignore
    from movies import metadata_manager as _movies_metadata  # type: ignore
    from movies.tmdb_client import TmdbUnavailableError as _TmdbUnavailableError  # type: ignore

# Maps the public artwork field name (URL-facing, movie-flavored) to the
# metadata row column that holds its stored relative path. File-naming on
# disk uses the ROM-flavored "image"/"fanart" vocabulary instead (see
# movies/metadata_manager.py's _artwork_path) so a scraped movie's artwork
# reads as "just like a ROM's" -- the two vocabularies don't need to match,
# this is just the lookup for serving what was already downloaded.
_ARTWORK_FIELD_COLUMNS = {"poster": "poster_relative_path", "backdrop": "backdrop_relative_path"}

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
        any caller that does want a page instead.

        Deliberately **no** ``cache_key`` on either response below, unlike
        most other list endpoints in this app. ``ExpiringLRUCache`` has no
        invalidation hook -- nothing purges a cached entry when
        ``sync_movies_cache()`` changes the underlying data, so a cached
        response can keep serving a stale snapshot (files that were removed
        from the scan, e.g. after the video-extension-allowlist fix, kept
        appearing in the Movies tab for up to an hour after the cache itself
        was already clean) for up to ``json_cache_ttl_seconds`` (default
        3600s). Movies can change often (new downloads/deletes) and this
        endpoint is only called once per Movies-tab visit, so the caching
        trades a real correctness bug for a performance win this call site
        doesn't need -- reading straight from SQLite here is already cheap.
        """
        if limit is not None:
            safe_limit = max(1, min(int(limit), 2000))
            safe_offset = max(0, int(offset))
            page = _movies_store.list_movies_page(
                self.settings.movies_root, query=str(query or ""), limit=safe_limit, offset=safe_offset
            )
            items = page["items"]
            self._apply_movie_display_titles(items)
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
            )
            return
        items = _movies_store.list_movies(self.settings.movies_root)
        query_value = str(query or "").strip().lower()
        if query_value:
            items = [
                item for item in items
                if query_value in " ".join([str(item.get("movie_name") or ""), str(item.get("file_path") or "")]).lower()
            ]
        self._apply_movie_display_titles(items)
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
        )

    def _apply_movie_display_titles(self, items: list) -> None:
        """Overlay each movie's scraped TMDb title (once it has one) in place
        of its raw filename -- one bulk lookup per list call, not a query per
        row. Falls back to movie_name for anything never scraped."""
        titles = _movies_store.list_movie_display_titles(self.settings.movies_root)
        for item in items:
            item["display_title"] = titles.get(item.get("entry_key")) or item.get("movie_name") or ""

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

    # ------------------------------------------------------------- detail

    def _handle_movie_detail(self, entry_key: str) -> None:
        """Full detail payload for the movie details page: the file's own
        info plus any scraped TMDb metadata (absent entirely if never
        scraped -- the frontend falls back to showing just the filename)."""
        movie = _movies_store.get_movie_by_key(self.settings.movies_root, entry_key)
        if not movie:
            raise FileNotFoundError()
        if not self.settings.downloads_enabled:
            movie["is_downloadable"] = False
        metadata = _movies_store.get_movie_metadata(self.settings.movies_root, entry_key)
        movie["display_title"] = (metadata or {}).get("title") or movie.get("movie_name") or ""
        movie["metadata"] = metadata
        self._send_json(200, movie)

    def _handle_movie_artwork(self, entry_key: str, field: str) -> None:
        """Serve a scraped poster/backdrop image. Session-gated like every
        other movies route (not admin-only) -- viewing artwork for a movie
        you can already browse isn't an admin action, only scraping it is."""
        column = _ARTWORK_FIELD_COLUMNS.get(str(field or ""))
        if not column:
            raise FileNotFoundError()
        if not _ENTRY_KEY_RE.match(str(entry_key or "")):
            raise FileNotFoundError()
        metadata = _movies_store.get_movie_metadata(self.settings.movies_root, entry_key)
        relative_path = (metadata or {}).get(column)
        if not relative_path:
            raise FileNotFoundError()
        movies_root = Path(self.settings.movies_root).resolve()
        target = (movies_root / relative_path).resolve()
        if target == movies_root or movies_root not in target.parents or not target.is_file():
            raise FileNotFoundError()
        self._stream_file(target, self._guess_content_type(target))

    # ------------------------------------------------------------- scraper

    def _handle_admin_movie_scraper_settings(self) -> None:
        self._send_json(200, _movies_metadata.get_settings(self.settings))

    def _handle_admin_movie_scraper_settings_update(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            result = _movies_metadata.update_settings(self.settings, payload.get("api_key"))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_movie_scrape_search(self, entry_key: str, query: Optional[str]) -> None:
        movie = _movies_store.get_movie_by_key(self.settings.movies_root, entry_key)
        if not movie:
            self._send_json(404, {"error": "unknown movie"})
            return
        search_query = str(query or "").strip() or _movies_metadata.clean_movie_query(movie.get("movie_name") or "")
        try:
            results = _movies_metadata.search(self.settings, search_query)
        except _TmdbUnavailableError as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, {"query": search_query, "results": results})

    def _handle_admin_movie_scrape_apply(self, entry_key: str, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        tmdb_id = payload.get("tmdb_id")
        if not tmdb_id:
            self._send_json(400, {"error": "tmdb_id is required"})
            return
        try:
            result = _movies_metadata.apply(self.settings, entry_key, tmdb_id)
        except _movies_metadata.MovieNotFoundError:
            self._send_json(404, {"error": "unknown movie"})
            return
        except _TmdbUnavailableError as error:
            self._send_json(502, {"error": str(error)})
            return
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    # ------------------------------------------------------- bulk scrape

    def _handle_admin_movie_scrape_bulk_status(self) -> None:
        self._send_json(200, {"job": _movies_metadata.get_bulk_scrape_status(self.settings)})

    def _handle_admin_movie_scrape_bulk_start(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _movies_metadata.start_bulk_scrape(self.settings, rescan_all=bool(payload.get("rescan_all")))
        status_code = 409 if result.get("status") == "already_running" else (
            502 if result.get("status") == "error" else 200
        )
        self._send_json(status_code, result)
