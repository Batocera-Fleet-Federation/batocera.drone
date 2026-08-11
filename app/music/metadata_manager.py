"""Music metadata/artwork scraper orchestration: MusicBrainz search+match,
Cover Art Archive artwork, and the bulk scrape background job.

Mirrors ``movies/metadata_manager.py``'s shape closely -- see that file and
the ``drone-movies-feature``/``drone-music-feature`` skills for the shared
design rationale. The one structural difference: there is no API-key
settings state here at all (MusicBrainz + Cover Art Archive are both
keyless), and the bulk job is grouped by **(artist, album)** rather than
iterating per track -- a release lookup returns an album's whole tracklist
in one call, which matters given MusicBrainz's stricter self-imposed
throttle (~1 req/sec) versus TMDb's reactive 429-retry. Grouping also means
no per-group result-caching layer is needed the way movies caches show/
season lookups across many episode files: each (artist, album) group is
already unique within one run, so it never repeats a release lookup.

**Album-level only -- deliberately no per-track/per-file matching.** Earlier
versions tried to match each local mp3/flac file to a specific track within
the resolved release's tracklist (title, track/disc number, MusicBrainz
recording id). That was a real source of live bugs: an 11-track folder
matched against a release that turned out to have a single track, silently
collapsing every distinct local file's scraped title onto the same one; more
generally, any mismatch between local file numbering and the release's real
tracklist could assign the wrong title to the wrong file. Since Cover Art
Archive art and the useful metadata (genres, canonical album name, release
date) are release-level, not track-level, anyway, the scraper now only ever
resolves *one release per album* and applies its art + release-level
metadata to every track in the group (``_apply_release_to_group``) -- it
never writes a track's title, track/disc number, or a per-recording
MusicBrainz id. A track's own display title always stays whatever's parsed
from its filename (see ``handlers_music._apply_music_display_titles``).
Artwork is saved **once per group**, not once per track (see
``_album_artwork_path``), both because there's only ever one real file now
and to naturally match the on-disk convention the manual "Upload Cover"
feature already uses (``images/album-cover.<ext>``).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from ..storage import music_store as _music_store
    from ..storage import music_scrape_jobs as _jobs
    from ..storage import music_scrape_job_items as _job_items
    from . import filename_parser as _filename_parser
    from .musicbrainz_client import MusicBrainzClient, MusicBrainzNotFoundError, MusicBrainzUnavailableError
    from .coverart_client import CoverArtClient, CoverArtUnavailableError
    from . import wikimedia_client as _wikimedia_client
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from storage import music_scrape_jobs as _jobs  # type: ignore
    from storage import music_scrape_job_items as _job_items  # type: ignore
    from music import filename_parser as _filename_parser  # type: ignore
    from music.musicbrainz_client import MusicBrainzClient, MusicBrainzNotFoundError, MusicBrainzUnavailableError  # type: ignore
    from music.coverart_client import CoverArtClient, CoverArtUnavailableError  # type: ignore
    from music import wikimedia_client as _wikimedia_client  # type: ignore

# Cover Art Archive can serve PNG or JPEG; the extension is cosmetic only
# (the browser reads Content-Type when serving it back), so a fixed one
# keeps the on-disk naming convention simple -- same call movies makes for
# its always-JPEG TMDb images.
MUSIC_ART_EXTENSION = ".jpg"


class MusicNotFoundError(LookupError):
    """Raised when entry_key doesn't resolve to a known track file."""


def _client() -> MusicBrainzClient:
    return MusicBrainzClient()


def _cover_client() -> CoverArtClient:
    return CoverArtClient()


def search(settings: Settings, query: str, *, client: Optional[MusicBrainzClient] = None) -> list:
    """Plain release search, verbatim query -- what a human-typed search box
    value uses (mirrors ``movies.metadata_manager.search``)."""
    results = (client or _client()).search_release(query)
    for item in results:
        item["kind"] = "release"
    return results


def _album_artwork_path(a_track_absolute_path: Path) -> Path:
    """``<that track's own folder>/images/album-cover.jpg`` -- one shared
    file for the whole group, saved next to whichever track happens to be
    ``rows[0]`` for the group being applied (every row in a group normally
    shares that exact folder; the rare multi-disc case where a group spans
    ``CD1``/``CD2`` subfolders just means the file physically lives under
    one of them -- every row's stored ``art_relative_path`` still points at
    it correctly regardless). Fixed filename, matching the manual "Upload
    Cover" convention (``handlers_music._handle_admin_music_album_art_upload``)
    exactly, so a scraped cover and a manually-uploaded one occupy the same
    slot rather than accumulating as separate orphaned files."""
    return a_track_absolute_path.parent / "images" / f"album-cover{MUSIC_ART_EXTENSION}"


def _artist_artwork_path(a_track_absolute_path: Path) -> Path:
    """``<that track's own folder>/images/artist-photo.jpg`` -- same
    shared-file-per-apply-call convention as ``_album_artwork_path``, just a
    different fixed filename so both coexist in the same ``images/``
    folder. Written once per (artist, album) group being applied, same as
    album art -- an artist with several albums in the library ends up with
    one small photo copied into each album's own ``images/`` folder rather
    than one canonical per-artist location, trading a little redundant disk
    space for not having to resolve a true "artist root folder" path (which
    ``classify_location`` does not expose -- only parsed artist/album name
    strings, not filesystem path segments)."""
    return a_track_absolute_path.parent / "images" / f"artist-photo{MUSIC_ART_EXTENSION}"


def _fetch_artist_art_bytes(
    client: MusicBrainzClient, cover_client: CoverArtClient, artist_mbid: str,
) -> Optional[bytes]:
    """Best-effort artist photo lookup: an artist lookup for a MusicBrainz
    "image" relation (most artists have none -- that's the normal case, not
    a failure), then Wikimedia Commons to resolve that relation's file-page
    URL into a real downloadable image. Every failure mode here -- no
    relation, an unrecognized/unresolvable Commons URL, MusicBrainz being
    unavailable, a bad image response -- returns ``None`` rather than
    raising, since this rides along with the album-art apply and must never
    fail it (mirrors the existing Cover Art Archive fetch's own
    never-fail-the-caller convention right above this function)."""
    if not artist_mbid:
        return None
    try:
        artist = client.artist_lookup(artist_mbid)
    except MusicBrainzUnavailableError:
        return None
    image_commons_url = artist.get("image_commons_url")
    if not image_commons_url:
        return None
    resolved_url = _wikimedia_client.resolve_image_url(image_commons_url)
    if not resolved_url:
        return None
    try:
        return cover_client.download_image(resolved_url)[0]
    except (CoverArtUnavailableError, ValueError):
        return None


def _relative_to_music_root(music_root: Path, path: Path) -> str:
    return path.resolve().relative_to(music_root.resolve()).as_posix()


def _select_release_candidate(results: list, local_count: int) -> dict:
    """Prefer the first release search result whose reported ``track_count``
    is plausible for the local group size (>= ``local_count`` -- a local rip
    is rarely more complete than the official release) over blindly trusting
    the top hit. Falls back to ``results[0]`` when no candidate's track count
    is known or plausible, so this never does worse than the old
    always-take-the-top-hit behavior.

    Exists because of a real bug caught live: the top search hit for an
    11-track local compilation folder was a MusicBrainz "Broadcast" release
    (a DJ radio set) with exactly one track -- picking a track-count-plausible
    candidate here avoids using that single-track release's cover art (which
    would still be *wrong*, just no longer capable of corrupting per-track
    titles the way it could back when this module still matched individual
    tracks -- see the module docstring).
    """
    for candidate in results:
        track_count = candidate.get("track_count")
        if isinstance(track_count, int) and track_count >= local_count:
            return candidate
    return results[0]


def _apply_release_to_group(
    settings: Settings,
    rows: list,
    release: dict,
    *,
    cover_client: Optional[CoverArtClient],
    client: Optional[MusicBrainzClient] = None,
    cover_bytes_cache: Optional[dict] = None,
    artist_bytes_cache: Optional[dict] = None,
) -> dict:
    """Save one release's art + release-level metadata to every row in
    ``rows`` (an (artist, album) group, or a single-row list for a
    standalone single). Downloads art once per ``release_mbid`` (shared via
    ``cover_bytes_cache`` across the whole bulk run, same optimization the
    old per-track path used) and writes it to **one** shared file rather
    than one copy per track (see ``_album_artwork_path``). Also attempts an
    artist photo (``_fetch_artist_art_bytes``, cached per ``artist_mbid``
    the same way) -- best-effort, absent for most artists, never fails this
    call. Deliberately writes ``title=""`` on every row -- see the module
    docstring for why a track's own filename-derived title is never
    overwritten here."""
    music_root = Path(settings.music_root).resolve()
    cover_client = cover_client or _cover_client()
    release_mbid = release.get("release_mbid") or ""

    art_relative_path: Optional[str] = None
    cached = cover_bytes_cache.get(release_mbid) if cover_bytes_cache is not None else None
    if cached is None:
        try:
            front_url = cover_client.front_cover_url(release_mbid)
            data = cover_client.download_image(front_url)[0] if front_url else None
        except (CoverArtUnavailableError, ValueError):
            # Missing/unreachable art, or a malformed image URL (download_image's
            # own defensive https-scheme check) -- either way this must never
            # fail the whole apply/bulk-job candidate. Real bug caught live:
            # an uncaught ValueError here crashed the bulk-scrape background
            # thread entirely, silently leaving the job stuck at "running"
            # forever with no error surfaced to the admin UI.
            data = None
        if cover_bytes_cache is not None:
            cover_bytes_cache[release_mbid] = data
    else:
        data = cached
    if data and rows:
        target = _album_artwork_path(Path(rows[0]["absolute_path"]).resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        art_relative_path = _relative_to_music_root(music_root, target)

    artist_mbid = release.get("artist_mbid") or ""
    artist_art_relative_path: Optional[str] = None
    if artist_mbid:
        cached_artist = artist_bytes_cache.get(artist_mbid) if artist_bytes_cache is not None else None
        if cached_artist is None:
            artist_data = _fetch_artist_art_bytes(client or _client(), cover_client, artist_mbid)
            if artist_bytes_cache is not None:
                artist_bytes_cache[artist_mbid] = artist_data
        else:
            artist_data = cached_artist
        if artist_data and rows:
            artist_target = _artist_artwork_path(Path(rows[0]["absolute_path"]).resolve())
            artist_target.parent.mkdir(parents=True, exist_ok=True)
            artist_target.write_bytes(artist_data)
            artist_art_relative_path = _relative_to_music_root(music_root, artist_target)

    extra = {
        "artist": release.get("artist") or "",
        "album": release.get("title") or "",
        "genres": release.get("genres") or [],
        "release_date": release.get("date"),
        "release_mbid": release_mbid,
        "release_group_mbid": release.get("release_group_mbid") or "",
        "artist_mbid": artist_mbid,
    }
    saved = {}
    for row in rows:
        saved = _music_store.save_music_metadata(
            settings.music_root,
            row["entry_key"],
            provider="musicbrainz",
            provider_id=release_mbid,
            title="",
            art_relative_path=art_relative_path,
            artist_art_relative_path=artist_art_relative_path,
            extra=extra,
        )
    return {
        "updated": len(rows),
        "has_art": art_relative_path is not None,
        "has_artist_art": artist_art_relative_path is not None,
        "metadata": saved,
    }


def apply_album(
    settings: Settings,
    entry_keys: list,
    release_mbid: str,
    *,
    client: Optional[MusicBrainzClient] = None,
    cover_client: Optional[CoverArtClient] = None,
) -> dict:
    """Look up one release and apply its art + release-level metadata to
    every track in ``entry_keys`` -- the album detail page's manual "search
    and apply" action (see ``handlers_music._handle_admin_music_scrape_apply``,
    which expands a single clicked track to its whole album group before
    calling this). Raises ``MusicNotFoundError`` if none of ``entry_keys``
    resolve to a known track. ``client``/``cover_client`` are injectable for
    tests."""
    rows = [row for row in (_music_store.get_music_by_key(settings.music_root, key) for key in entry_keys) if row]
    if not rows:
        raise MusicNotFoundError(",".join(entry_keys))
    client = client or _client()
    release = client.release_lookup(release_mbid)
    result = _apply_release_to_group(settings, rows, release, cover_client=cover_client, client=client)
    result["entry_keys"] = [row["entry_key"] for row in rows]
    return result


def delete_metadata(settings: Settings, entry_key: str) -> dict:
    """Remove a track's scraped MusicBrainz metadata and artwork -- mirrors
    ``movies.metadata_manager.delete_metadata``."""
    deleted = _music_store.delete_music_metadata(settings.music_root, entry_key)
    if not deleted:
        return {"deleted": False}
    music_root = Path(settings.music_root).resolve()
    for column in ("art_relative_path", "artist_art_relative_path"):
        relative_path = deleted.get(column)
        if relative_path:
            (music_root / relative_path).unlink(missing_ok=True)
    return {"deleted": True}


def delete_album_metadata(settings: Settings, entry_keys: list) -> dict:
    """Batch version of ``delete_metadata`` for the album page's "Remove
    scraped data" action -- every track in a group shares the exact same
    ``art_relative_path`` (see ``_apply_release_to_group``), so the art file
    is only ever unlinked once even though every row references it."""
    music_root = Path(settings.music_root).resolve()
    deleted = 0
    art_paths_to_unlink: set = set()
    for entry_key in entry_keys:
        removed = _music_store.delete_music_metadata(settings.music_root, entry_key)
        if not removed:
            continue
        deleted += 1
        for column in ("art_relative_path", "artist_art_relative_path"):
            relative_path = removed.get(column)
            if relative_path:
                art_paths_to_unlink.add(relative_path)
    for relative_path in art_paths_to_unlink:
        (music_root / relative_path).unlink(missing_ok=True)
    return {"deleted": deleted}


def delete_music(settings: Settings, entry_key: str) -> dict:
    """Permanently delete a track's underlying file plus any scraped
    metadata/artwork -- mirrors ``movies.metadata_manager.delete_movie``."""
    delete_metadata(settings, entry_key)
    return _music_store.delete_music_file(settings.music_root, entry_key)


# --------------------------------------------------------------- bulk scrape

def _has_artwork(settings: Settings, entry_key: str) -> bool:
    metadata = _music_store.get_music_metadata(settings.music_root, entry_key)
    return bool(metadata and metadata.get("art_relative_path"))


def search_album_default_query(
    settings: Settings, artist: str, album: str, *, client: Optional[MusicBrainzClient] = None,
) -> dict:
    """The album page's manual search's default (no custom query typed yet)
    case: release-only candidates from ``filename_parser.search_candidates``
    (an empty ``track_title`` means its recording-query rungs are never
    generated -- see that function), stopping at the first rung that returns
    anything. Returns ``{"query", "results"}`` -- mirrors
    ``movies.metadata_manager.search_movie_default_query``. Deliberately no
    recording-search fallback (unlike the old per-track version this
    replaces): a "recording" result identifies one song, not an album, and
    ``apply_album`` only ever applies a whole release."""
    client = client or _client()
    label = album or artist
    for query_type, query_text in _filename_parser.search_candidates(artist, album, ""):
        if query_type != "release":
            continue
        label = query_text
        results = client.search_release(query_text)
        for item in results:
            item["kind"] = "release"
        if results:
            return {"query": label, "results": results}
    return {"query": label, "results": []}


def _group_bulk_candidates(rows: list) -> tuple:
    """Split the whole music inventory into (artist, album)-grouped release
    candidates and individual singles/recording candidates, per the module
    docstring. A file with no artist at all (an orphan directly under
    music_root) is skipped entirely -- nothing usable to search with, same
    "skipped, not failed" treatment movies gives an all-punctuation filename."""
    groups: dict = {}
    singles: list = []
    orphans: list = []
    for row in rows:
        location = _filename_parser.classify_location(row["file_path"])
        if not location.artist:
            orphans.append(row)
            continue
        if location.album:
            key = (location.artist.strip().lower(), location.album.strip().lower())
            groups.setdefault(key, {"artist": location.artist, "album": location.album, "rows": []})
            groups[key]["rows"].append(row)
        else:
            singles.append(row)
    return list(groups.values()), singles, orphans


def _run_bulk_scrape_job(settings: Settings, job_id: int, groups: list, singles: list, client: MusicBrainzClient, cover_client: CoverArtClient) -> None:
    matched = skipped = failed = 0
    total = len(groups) + len(singles)
    processed = 0
    cover_bytes_cache: dict = {}
    artist_bytes_cache: dict = {}

    def _tick(current_label: str) -> None:
        _jobs.update_progress(
            settings, job_id,
            processed=processed, current_music=current_label,
            matched_count=matched, skipped_count=skipped, failed_count=failed,
        )

    def _stop_if_requested() -> bool:
        # A user-requested stop -- not a failure, so nothing from here on is
        # recorded in job_items (they were simply never reached).
        if not _jobs.is_stop_requested(settings, job_id):
            return False
        _tick("")
        _jobs.mark_stopped(settings, job_id)
        return True

    for group in groups:
        if _stop_if_requested():
            return
        _tick(f"{group['artist']} – {group['album']}")
        rows = group["rows"]
        try:
            candidates = _filename_parser.search_candidates(group["artist"], group["album"], "")
            release_candidates = [c for c in candidates if c[0] == "release"]
            results = []
            for _query_type, query_text in release_candidates:
                results = client.search_release(query_text)
                if results:
                    break
            if not results:
                skipped += len(rows)
                for row in rows:
                    _record_item(settings, row, _job_items.STATUS_SKIPPED, "no MusicBrainz release results")
                processed += 1
                continue
            release = client.release_lookup(_select_release_candidate(results, len(rows))["release_mbid"])
            _apply_release_to_group(
                settings, rows, release, cover_client=cover_client, client=client,
                cover_bytes_cache=cover_bytes_cache, artist_bytes_cache=artist_bytes_cache,
            )
            matched += len(rows)
            for row in rows:
                _record_item(settings, row, _job_items.STATUS_MATCHED)
        except MusicBrainzNotFoundError as error:
            failed += len(rows)
            for row in rows:
                _record_item(settings, row, _job_items.STATUS_FAILED, str(error))
        except MusicBrainzUnavailableError as error:
            reason = str(error)
            for row in rows:
                _record_item(settings, row, _job_items.STATUS_FAILED, reason)
            remaining_groups = groups[groups.index(group) + 1:]
            for remaining in remaining_groups:
                for row in remaining["rows"]:
                    _record_item(settings, row, _job_items.STATUS_FAILED, reason)
            for row in singles:
                _record_item(settings, row, _job_items.STATUS_FAILED, reason)
            # len(rows) for *this* group (its release_lookup/search just
            # raised) plus every group/single after it in the queue.
            failed += len(rows) + sum(len(g["rows"]) for g in remaining_groups) + len(singles)
            _jobs.update_progress(settings, job_id, processed=total, current_music="", matched_count=matched, skipped_count=skipped, failed_count=failed)
            _jobs.mark_complete(settings, job_id)
            return
        except Exception as error:  # noqa: BLE001 - one bad group must not kill the whole run,
            # and must never leave the job stuck at "running" forever either
            # -- a real live bug: an uncaught exception here used to crash
            # this background thread outright, with nothing left to ever
            # call mark_complete/mark_error, so the job (and any_running()'s
            # guard against starting a second one) stayed "running" forever.
            failed += len(rows)
            for row in rows:
                _record_item(settings, row, _job_items.STATUS_FAILED, f"error: {error}")
        processed += 1

    for row in singles:
        if _stop_if_requested():
            return
        _tick(row.get("track_name") or "")
        try:
            location = _filename_parser.classify_location(row["file_path"])
            track_info = _filename_parser.parse_track_filename(row["track_name"])
            candidates = _filename_parser.search_candidates(location.artist, "", track_info.title)
            recording_result = None
            for query_type, query_text in candidates:
                if query_type != "recording":
                    continue
                results = client.search_recording(query_text)
                if results:
                    recording_result = results[0]
                    break
            if not recording_result or not recording_result.get("release_mbid"):
                skipped += 1
                _record_item(settings, row, _job_items.STATUS_SKIPPED, "no MusicBrainz recording match")
                processed += 1
                continue
            release = client.release_lookup(recording_result["release_mbid"])
            _apply_release_to_group(
                settings, [row], release, cover_client=cover_client, client=client,
                cover_bytes_cache=cover_bytes_cache, artist_bytes_cache=artist_bytes_cache,
            )
            matched += 1
            _record_item(settings, row, _job_items.STATUS_MATCHED)
        except MusicBrainzNotFoundError as error:
            failed += 1
            _record_item(settings, row, _job_items.STATUS_FAILED, str(error))
        except MusicBrainzUnavailableError as error:
            reason = str(error)
            remaining_singles = singles[singles.index(row):]
            for remaining in remaining_singles:
                _record_item(settings, remaining, _job_items.STATUS_FAILED, reason)
            failed += len(remaining_singles)
            _jobs.update_progress(settings, job_id, processed=total, current_music="", matched_count=matched, skipped_count=skipped, failed_count=failed)
            _jobs.mark_complete(settings, job_id)
            return
        except Exception as error:  # noqa: BLE001 - one bad track must not kill the whole
            # run or leave the job stuck at "running" forever -- see the
            # matching catch-all in the groups loop above for why.
            failed += 1
            _record_item(settings, row, _job_items.STATUS_FAILED, f"error: {error}")
        processed += 1

    _jobs.update_progress(settings, job_id, processed=total, current_music="", matched_count=matched, skipped_count=skipped, failed_count=failed)
    _jobs.mark_complete(settings, job_id)


def _record_item(settings: Settings, row: dict, status: str, reason: str = "") -> None:
    try:
        _job_items.record(settings, row.get("entry_key") or "", row.get("track_name") or "", row.get("file_path") or "", status, reason)
    except Exception:  # noqa: BLE001 - diagnostics must not break the scrape
        pass


_BULK_SCRAPE_START_LOCK = threading.Lock()


def start_bulk_scrape(
    settings: Settings, *, rescan_all: bool = False,
    client: Optional[MusicBrainzClient] = None, cover_client: Optional[CoverArtClient] = None,
) -> dict:
    """Kick off a background job that scrapes every (artist, album) group
    (``rescan_all``) or only tracks still missing art (the default). Only
    one job runs at a time; ``music_scrape_jobs`` (not an in-process flag)
    is the guard -- mirrors ``movies.metadata_manager.start_bulk_scrape``.
    Always resolvable (no API key to be missing), unlike the movies
    equivalent's ``TmdbUnavailableError`` guard at start time."""
    with _BULK_SCRAPE_START_LOCK:
        if _jobs.any_running(settings):
            return {"status": "already_running"}
        client = client or _client()
        cover_client = cover_client or _cover_client()
        rows = _music_store.list_music(settings.music_root)
        if not rescan_all:
            rows = [row for row in rows if not _has_artwork(settings, row["entry_key"])]
        groups, singles, _orphans = _group_bulk_candidates(rows)
        _job_items.clear(settings)
        job = _jobs.create_running(settings, rescan_all=rescan_all, total=len(groups) + len(singles))
    thread = threading.Thread(
        target=_run_bulk_scrape_job, args=(settings, job["id"], groups, singles, client, cover_client),
        name="music-bulk-scrape", daemon=True,
    )
    thread.start()
    return {"status": "ok", "job": job}


def retry_bulk_scrape_items(
    settings: Settings, *, status: Optional[str] = None, entry_keys: Optional[list] = None,
    client: Optional[MusicBrainzClient] = None, cover_client: Optional[CoverArtClient] = None,
) -> dict:
    """Re-run the bulk scraper over a specific subset of the *last* run's
    results -- mirrors ``movies.metadata_manager.retry_bulk_scrape_items``.
    Retried tracks are re-grouped by (artist, album) same as a fresh run
    (a retry set can span multiple albums), and does not clear
    ``music_scrape_job_items`` first -- each retried item's row is upserted
    in place."""
    with _BULK_SCRAPE_START_LOCK:
        if _jobs.any_running(settings):
            return {"status": "already_running"}
        client = client or _client()
        cover_client = cover_client or _cover_client()
        keys = list(entry_keys) if entry_keys else _job_items.entry_keys_by_status(settings, status or _job_items.STATUS_FAILED)
        rows = []
        for key in keys:
            row = _music_store.get_music_by_key(settings.music_root, key)
            if row:
                rows.append(row)
        groups, singles, _orphans = _group_bulk_candidates(rows)
        job = _jobs.create_running(settings, rescan_all=False, total=len(groups) + len(singles))
    thread = threading.Thread(
        target=_run_bulk_scrape_job, args=(settings, job["id"], groups, singles, client, cover_client),
        name="music-bulk-scrape-retry", daemon=True,
    )
    thread.start()
    return {"status": "ok", "job": job}


def get_bulk_scrape_status(settings: Settings) -> Optional[dict]:
    return _jobs.latest(settings)


def stop_bulk_scrape(settings: Settings) -> dict:
    """Request the currently-running bulk scrape job stop at its next
    per-(group/single) check. A no-op (not an error) if nothing is running
    -- same "idempotent, already in the desired state" convention the rest
    of this module uses rather than treating a late/duplicate stop click as
    a failure."""
    job = _jobs.latest(settings)
    if not job or job.get("status") != _jobs.STATUS_RUNNING:
        return {"status": "not_running"}
    _jobs.request_stop(settings, job["id"])
    return {"status": "ok", "job": job}


def get_bulk_scrape_items(settings: Settings, status: str, *, limit: int = 200, offset: int = 0) -> dict:
    return _job_items.list_by_status(settings, status, limit=limit, offset=offset)
