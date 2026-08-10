"""RomRequestHandler music handlers, as a mixin.

Local music library browsing for the Systems/Assets page: list, stream
(range-aware, for in-browser ``<audio>`` playback/seeking), download, and
scraped-artwork serving. Mirrors ``handlers_movies.py`` closely -- see that
file's module docstring and the ``drone-movies-feature``/``drone-music-feature``
skills for the shared design rationale. Music has no system association
(flat inventory -- see ``storage/music_store.py``); artist/album grouping is
computed from each file's folder path, not stored relationally (see
``music/filename_parser.py``). Routes are plain top-level ``/music`` (not
``/admin/music``), matching the ``/movies``/``/systems``/``/bios``
convention: gated by the same session-cookie login as the rest of the
browsing surface, but not behind the ``admin_enabled`` toggle -- browsing
your own library isn't an admin-only feature. (The scraper *settings/apply*
routes are admin-only -- see the ``/admin/music/*`` methods added alongside
the MusicBrainz/Cover Art Archive client.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    from ..common import http_range as _http_range
    from ..storage import music_store as _music_store
    from ..music import filename_parser as _music_filename_parser
    from ..music import metadata_manager as _music_metadata
    from ..music.musicbrainz_client import MusicBrainzUnavailableError as _MusicBrainzUnavailableError
except ImportError:  # pragma: no cover - direct script execution fallback
    from common import http_range as _http_range  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from music import filename_parser as _music_filename_parser  # type: ignore
    from music import metadata_manager as _music_metadata  # type: ignore
    from music.musicbrainz_client import MusicBrainzUnavailableError as _MusicBrainzUnavailableError  # type: ignore

# Maps the public artwork field name (URL-facing) to the metadata row column
# that holds its stored relative path -- "art" (album/track cover, from Cover
# Art Archive) and "artist" (artist photo -- always empty until a future
# TheAudioDB follow-up; see the drone-music-feature skill).
_ARTWORK_FIELD_COLUMNS = {"art": "art_relative_path", "artist": "artist_art_relative_path"}

# entry_key is always a hex-digest slice computed server-side (see
# music_store._entry_key) -- this just rejects anything that couldn't
# possibly be one before touching the filesystem.
_ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{1,64}$")


class HandlersMusicMixin:
    def _handle_music_list(self, query: Optional[str] = None, limit: Optional[int] = None, offset: int = 0) -> None:
        """``limit=None`` (no ``limit`` query param) returns the whole
        inventory in one shot -- same convention as ``_handle_movies_list``:
        the Music Explorer needs the complete set client-side to group
        tracks into artist/album cards, and a personal music library is
        cheap to return whole. ``limit``/``offset`` stay available for any
        caller that does want a page instead.

        Deliberately no ``cache_key`` here, for the identical reason
        ``_handle_movies_list`` has none -- see that method's docstring.
        """
        if limit is not None:
            safe_limit = max(1, min(int(limit), 2000))
            safe_offset = max(0, int(offset))
            page = _music_store.list_music_page(
                self.settings.music_root, query=str(query or ""), limit=safe_limit, offset=safe_offset
            )
            items = page["items"]
            self._apply_music_display_titles(items)
            self._apply_music_grouping_and_genres(items)
            self._apply_music_likes(items)
            if not self.settings.downloads_enabled:
                for item in items:
                    item["is_downloadable"] = False
            self._send_json(
                200,
                {
                    "music": items,
                    "count": page["total"],
                    "offset": page["offset"],
                    "limit": page["limit"],
                    "returned": len(items),
                    "has_more": (page["offset"] + len(items)) < page["total"],
                },
            )
            return
        items = _music_store.list_music(self.settings.music_root)
        query_value = str(query or "").strip().lower()
        if query_value:
            items = [
                item for item in items
                if query_value in " ".join([str(item.get("track_name") or ""), str(item.get("file_path") or "")]).lower()
            ]
        self._apply_music_display_titles(items)
        self._apply_music_grouping_and_genres(items)
        self._apply_music_likes(items)
        if not self.settings.downloads_enabled:
            for item in items:
                item["is_downloadable"] = False
        self._send_json(
            200,
            {
                "music": items,
                "count": len(items),
                "offset": 0,
                "limit": len(items),
                "returned": len(items),
                "has_more": False,
            },
        )

    def _apply_music_display_titles(self, items: list) -> None:
        """Overlay each track's scraped MusicBrainz title (once it has one)
        in place of its raw filename -- one bulk lookup per list call, not a
        query per row. Falls back to the *cleanly parsed* title (leading
        track number/separator stripped, via ``parse_track_filename``) for
        anything never scraped, not the raw ``track_name`` -- otherwise a
        caller that prepends its own track-number prefix for display (e.g.
        the artist/album track list) would double up on "01 - 01 - Song"
        for any still-unscraped track, since the raw filename usually
        already carries that same leading number itself."""
        titles = _music_store.list_music_display_titles(self.settings.music_root)
        for item in items:
            scraped_title = titles.get(item.get("entry_key"))
            if scraped_title:
                item["display_title"] = scraped_title
                continue
            parsed_title = _music_filename_parser.parse_track_filename(item.get("track_name") or "").title
            item["display_title"] = parsed_title or item.get("track_name") or ""

    def _apply_music_grouping_and_genres(self, items: list) -> None:
        """Overlay ``artist``/``album``/``disc_number`` (from
        ``filename_parser.classify_location`` -- a pure folder-path check, so
        this works immediately for every track, scraped or not) and
        ``track_number``/``title`` (best-effort, from
        ``filename_parser.parse_track_filename``) plus ``genres`` (from
        scraped metadata, one bulk lookup, empty for anything never
        scraped). Feeds the Music Explorer's Artist/Album grouping and Genre
        category sidebar without a per-track request.

        ``artist``/``album`` are deliberately the *folder-parsed* values,
        never a scraped canonical artist/album name, even once scraped: they
        have to stay the stable grouping key regardless of scrape status, or
        an album with only some tracks scraped could split into two cards --
        same rule ``_apply_movie_kind_and_genres`` follows for show_title."""
        genres_by_key = _music_store.list_music_genres(self.settings.music_root)
        for item in items:
            location = _music_filename_parser.classify_location(item.get("file_path") or item.get("relative_path") or "")
            track_info = _music_filename_parser.parse_track_filename(item.get("track_name") or "")
            item["artist"] = location.artist
            item["album"] = location.album
            item["disc_number"] = location.disc_number or track_info.disc_number
            item["track_number"] = track_info.track_number
            item["genres"] = genres_by_key.get(item.get("entry_key")) or []

    def _apply_music_likes(self, items: list) -> None:
        """Overlay ``liked`` (bool) from the liked-keys set -- one bulk
        lookup per list call, same shape as ``_apply_music_display_titles``/
        ``_apply_music_grouping_and_genres``, feeding the Music Explorer's
        Likes filter section without a per-track request."""
        liked_keys = _music_store.list_liked_music_keys(self.settings.music_root)
        for item in items:
            item["liked"] = item.get("entry_key") in liked_keys

    def _resolve_music_path(self, entry_key: str) -> Path:
        """Look up a track by its stable id and validate the resolved path
        stays inside ``music_root`` -- mirrors
        ``handlers_movies._resolve_movie_path``."""
        return _music_store.resolve_music_stream_path(self.settings.music_root, entry_key)

    def _handle_music_download(self, entry_key: str) -> None:
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target = self._resolve_music_path(entry_key)
        self._stream_file(target, "application/octet-stream", as_attachment=True)

    def _handle_music_stream(self, entry_key: str) -> None:
        """Serve a track inline for the persistent <audio> player.

        Range-aware (206 Partial Content), mirroring
        ``handlers_movies._handle_movie_stream`` -- an <audio> element uses
        Range the same way a <video> element does (seeking within a track),
        and reusing the identical parsing/response-writing logic here (via
        ``_stream_music_range``, a near-verbatim copy of
        ``_stream_movie_range``) keeps that security-relevant Range handling
        in one well-tested shape rather than two independent ones.
        """
        if not self.settings.downloads_enabled:
            raise ValueError("downloads are disabled")
        target = self._resolve_music_path(entry_key)
        self._stream_music_range(target, self._guess_content_type(target))

    def _stream_music_range(self, path: Path, content_type: str) -> None:
        file_size = path.stat().st_size
        start, end, status = _http_range.parse_range_header(self.headers.get("Range"), file_size)
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        # Same reasoning as the movie stream cache-control: a track is
        # requested repeatedly within one listening session (seeking issues
        # a fresh Range request per jump; replaying it from the queue re-hits
        # this URL) and never changes underneath a stable entry_key.
        self._send_security_headers(cache_control="public, max-age=300")
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

    def _handle_music_detail(self, entry_key: str) -> None:
        """Full detail payload for the track details page: the file's own
        info plus any scraped MusicBrainz metadata (absent entirely if never
        scraped -- the frontend falls back to showing just the filename)."""
        track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
        if not track:
            raise FileNotFoundError()
        if not self.settings.downloads_enabled:
            track["is_downloadable"] = False
        location = _music_filename_parser.classify_location(track.get("file_path") or "")
        track_info = _music_filename_parser.parse_track_filename(track.get("track_name") or "")
        track["artist"] = location.artist
        track["album"] = location.album
        track["disc_number"] = location.disc_number or track_info.disc_number
        track["track_number"] = track_info.track_number
        metadata = _music_store.get_music_metadata(self.settings.music_root, entry_key)
        # Same cleanly-parsed-title fallback as _apply_music_display_titles
        # (not the raw track_name) -- see that method's docstring for why.
        track["display_title"] = (metadata or {}).get("title") or track_info.title or track.get("track_name") or ""
        track["metadata"] = metadata
        track["liked"] = _music_store.is_music_liked(self.settings.music_root, entry_key)
        self._send_json(200, track)

    def _handle_music_like(self, entry_key: str, payload: dict) -> None:
        """Toggle-or-set a track's liked flag. Not admin-gated -- liking a
        track you can already browse/play isn't an admin action, same as
        every other route in this module (see the module docstring)."""
        track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
        if not track:
            self._send_json(404, {"error": "unknown track"})
            return
        payload = payload if isinstance(payload, dict) else {}
        liked = bool(payload.get("liked", True))
        _music_store.set_music_liked(self.settings.music_root, entry_key, liked)
        self._send_json(200, {"entry_key": entry_key, "liked": liked})

    def _find_album_sibling_art(self, entry_key: str) -> Optional[Path]:
        """If another track in the same on-disk (artist, album) folder has
        scraped art of its own, use it -- every track's ``art`` is really
        just that release's front cover (Cover Art Archive is release-level,
        not track-level) saved individually per file, so a sibling's copy is
        exactly as correct as this track's own would have been; the only
        reason one track has it and another doesn't is a per-download hiccup
        (a transient fetch failure, or a partial/incremental re-scrape),
        never a real difference in what the "right" image is. Scoped to
        tracks under the same artist folder (cheap prefix query, see
        ``list_music_under_artist_folder``) and then filtered to the exact
        matching album via ``classify_location``, so an artist with many
        unrelated albums doesn't cross-contaminate."""
        track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
        if not track:
            return None
        location = _music_filename_parser.classify_location(track.get("file_path") or "")
        if not location.artist or not location.album:
            return None
        music_root = Path(self.settings.music_root).resolve()
        candidates = _music_store.list_music_under_artist_folder(self.settings.music_root, location.artist)
        for candidate in candidates:
            if candidate["entry_key"] == entry_key:
                continue
            candidate_location = _music_filename_parser.classify_location(candidate["file_path"])
            if candidate_location.album != location.album:
                continue
            metadata = _music_store.get_music_metadata(self.settings.music_root, candidate["entry_key"])
            relative_path = (metadata or {}).get("art_relative_path")
            if not relative_path:
                continue
            target = (music_root / relative_path).resolve()
            if target == music_root or music_root not in target.parents or not target.is_file():
                continue
            return target
        return None

    def _handle_music_artwork(self, entry_key: str, field: str) -> None:
        """Serve scraped album/artist art. Session-gated like every other
        music route (not admin-only) -- viewing artwork for a track you can
        already browse isn't an admin action, only scraping it is.

        Falls back, in order, to: another track's scraped art from the same
        album (``_find_album_sibling_art``), then a conventionally-named
        local image file on disk (``cover.jpg``/``folder.jpg``/...) -- for
        the ``art`` field only, whenever there's no scraped art on this
        specific track -- never scraped, scraped but Cover Art Archive had
        nothing, or the scraped file went missing from disk. Only ``art``
        gets this fallback, not ``artist`` -- there's no equivalent "this is
        obviously the artist photo" folder-naming convention to trust the
        way there is for album/track cover art."""
        column = _ARTWORK_FIELD_COLUMNS.get(str(field or ""))
        if not column:
            raise FileNotFoundError()
        if not _ENTRY_KEY_RE.match(str(entry_key or "")):
            raise FileNotFoundError()
        metadata = _music_store.get_music_metadata(self.settings.music_root, entry_key)
        relative_path = (metadata or {}).get(column)
        music_root = Path(self.settings.music_root).resolve()
        target: Optional[Path] = None
        if relative_path:
            candidate = (music_root / relative_path).resolve()
            if candidate != music_root and music_root in candidate.parents and candidate.is_file():
                target = candidate
        if target is None and field == "art":
            target = self._find_album_sibling_art(entry_key)
        if target is None and field == "art":
            track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
            if track and track.get("absolute_path"):
                target = _music_store.find_local_cover_image(Path(track["absolute_path"]), music_root)
        if target is None:
            raise FileNotFoundError()
        # Same helper ROM/movie artwork use (handlers_peer.py): server-side
        # in-memory cache (keyed by mtime) plus a browser-facing
        # Cache-Control: public, max-age=3600.
        self._stream_cached_image(target)

    # -------------------------------------------------------------- delete

    def _handle_admin_music_delete(self, payload: dict) -> None:
        """Permanently deletes one or more tracks (their files plus any
        scraped metadata/artwork) -- the track/artist detail pages' delete
        action (confirmed via a modal client-side). Takes a batch so
        "delete this whole album" is one request instead of one per track.

        Self-contained (does not depend on ``music/metadata_manager.py``,
        unlike ``handlers_movies._handle_admin_movies_delete``'s use of
        ``metadata_manager.delete_movie``) since deleting a file plus its
        optional metadata/artwork row needs nothing scraper-specific."""
        payload = payload if isinstance(payload, dict) else {}
        entry_keys = payload.get("entry_keys")
        if not isinstance(entry_keys, list) or not entry_keys:
            self._send_json(400, {"error": "entry_keys is required"})
            return
        deleted = 0
        for entry_key in entry_keys:
            key = str(entry_key)
            metadata = _music_store.get_music_metadata(self.settings.music_root, key)
            result = _music_store.delete_music_file(self.settings.music_root, key)
            if result.get("deleted"):
                deleted += 1
            _music_store.delete_music_metadata(self.settings.music_root, key)
            if metadata:
                self._unlink_music_artwork(metadata)
        self._send_json(200, {"deleted": deleted, "requested": len(entry_keys)})

    def _unlink_music_artwork(self, metadata: dict) -> None:
        music_root = Path(self.settings.music_root).resolve()
        for column in ("art_relative_path", "artist_art_relative_path"):
            relative = metadata.get(column)
            if not relative:
                continue
            target = (music_root / relative).resolve()
            if target == music_root or music_root not in target.parents:
                continue
            target.unlink(missing_ok=True)

    # ------------------------------------------------------------- scraper
    #
    # No settings/API-key routes here at all, unlike handlers_movies.py --
    # MusicBrainz + Cover Art Archive are both keyless (see
    # music/musicbrainz_client.py's module docstring), so there is nothing
    # to configure before scraping works.

    def _handle_admin_music_scrape_search(self, entry_key: str, query: Optional[str]) -> None:
        """A custom ``query`` is searched verbatim (release search, one plain
        MusicBrainz call). With no custom query, this runs the same
        candidate ladder the bulk scraper trusts
        (``search_track_default_query`` -- release query first, recording
        query as fallback) so a first search finds the right album/track
        without the human needing to hand-clean anything. Mirrors
        ``handlers_movies._handle_admin_movie_scrape_search``."""
        track = _music_store.get_music_by_key(self.settings.music_root, entry_key)
        if not track:
            self._send_json(404, {"error": "unknown track"})
            return
        custom_query = str(query or "").strip()
        try:
            if custom_query:
                search_query = custom_query
                results = _music_metadata.search(self.settings, search_query)
            else:
                location = _music_filename_parser.classify_location(track.get("file_path") or "")
                track_info = _music_filename_parser.parse_track_filename(track.get("track_name") or "")
                outcome = _music_metadata.search_track_default_query(
                    self.settings, location.artist, location.album, track_info.title,
                )
                search_query = outcome["query"]
                results = outcome["results"]
        except _MusicBrainzUnavailableError as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, {"query": search_query, "results": results})

    def _handle_admin_music_scrape_apply(self, entry_key: str, payload: dict) -> None:
        """``release_mbid`` is required; ``recording_mbid`` is an optional
        hint (present when the chosen search result was a specific
        recording, not a whole release) used to pick which track within the
        release corresponds to this file -- see
        ``metadata_manager._match_track_in_release``."""
        payload = payload if isinstance(payload, dict) else {}
        release_mbid = payload.get("release_mbid")
        if not release_mbid:
            self._send_json(400, {"error": "release_mbid is required"})
            return
        try:
            result = _music_metadata.apply(
                self.settings, entry_key, release_mbid, recording_mbid=payload.get("recording_mbid"),
            )
        except _music_metadata.MusicNotFoundError:
            self._send_json(404, {"error": "unknown track"})
            return
        except _music_metadata.MusicMatchError as error:
            self._send_json(400, {"error": str(error)})
            return
        except _MusicBrainzUnavailableError as error:
            self._send_json(502, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_music_scrape_delete(self, entry_key: str) -> None:
        """Clears a scraped track's MusicBrainz metadata + artwork -- for
        when a scrape matched the wrong track/album and a human needs a
        clean slate before retrying."""
        result = _music_metadata.delete_metadata(self.settings, entry_key)
        self._send_json(200, result)

    # ------------------------------------------------------- bulk scrape

    def _handle_admin_music_scrape_bulk_status(self) -> None:
        self._send_json(200, {"job": _music_metadata.get_bulk_scrape_status(self.settings)})

    def _handle_admin_music_scrape_bulk_start(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _music_metadata.start_bulk_scrape(self.settings, rescan_all=bool(payload.get("rescan_all")))
        status_code = 409 if result.get("status") == "already_running" else (
            502 if result.get("status") == "error" else 200
        )
        self._send_json(status_code, result)

    _BULK_ITEM_STATUSES = {"matched", "skipped", "failed"}

    def _handle_admin_music_scrape_bulk_items(self, status: str, limit: Optional[int], offset: int) -> None:
        if status not in self._BULK_ITEM_STATUSES:
            self._send_json(400, {"error": "status must be one of matched, skipped, failed"})
            return
        page = _music_metadata.get_bulk_scrape_items(
            self.settings, status, limit=int(limit) if limit is not None else 200, offset=offset
        )
        self._send_json(200, page)

    def _handle_admin_music_scrape_bulk_retry(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        entry_keys = payload.get("entry_keys")
        status = payload.get("status")
        if not entry_keys and status not in self._BULK_ITEM_STATUSES:
            self._send_json(400, {"error": "either entry_keys or a status (matched, skipped, failed) is required"})
            return
        result = _music_metadata.retry_bulk_scrape_items(
            self.settings,
            status=status if not entry_keys else None,
            entry_keys=entry_keys if isinstance(entry_keys, list) else None,
        )
        status_code = 409 if result.get("status") == "already_running" else (
            502 if result.get("status") == "error" else 200
        )
        self._send_json(status_code, result)
