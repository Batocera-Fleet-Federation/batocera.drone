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

Artwork lands in the same sibling-``images/``-folder convention movies uses
(``<track's own folder>/images/<safe-stem>-mb-art.jpg``), for the identical
same-basename-different-folder collision-avoidance reason.
"""

from __future__ import annotations

import re
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
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from storage import music_scrape_jobs as _jobs  # type: ignore
    from storage import music_scrape_job_items as _job_items  # type: ignore
    from music import filename_parser as _filename_parser  # type: ignore
    from music.musicbrainz_client import MusicBrainzClient, MusicBrainzNotFoundError, MusicBrainzUnavailableError  # type: ignore
    from music.coverart_client import CoverArtClient, CoverArtUnavailableError  # type: ignore

# Cover Art Archive can serve PNG or JPEG; the extension is cosmetic only
# (the browser reads Content-Type when serving it back), so a fixed one
# keeps the on-disk naming convention simple -- same call movies makes for
# its always-JPEG TMDb images.
MUSIC_ART_EXTENSION = ".jpg"


class MusicNotFoundError(LookupError):
    """Raised when entry_key doesn't resolve to a known track file."""


class MusicMatchError(ValueError):
    """Raised when a chosen release's tracklist has no track that plausibly
    corresponds to the target file (see ``_match_track_in_release``)."""


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


def _safe_track_stem(track_path: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", track_path.stem) or "track"


def _artwork_path(track_absolute_path: Path) -> Path:
    """``<track's own folder>/images/<safe-stem>-mb-art.jpg`` -- a sibling of
    the track file at whatever depth it lives, mirroring the ROM/Movies
    convention of an images/ folder next to the content it decorates."""
    stem = _safe_track_stem(track_absolute_path)
    return track_absolute_path.parent / "images" / f"{stem}-mb-art{MUSIC_ART_EXTENSION}"


def _relative_to_music_root(music_root: Path, path: Path) -> str:
    return path.resolve().relative_to(music_root.resolve()).as_posix()


def _match_track_in_release(
    release: dict, *, recording_mbid: Optional[str], disc_number: Optional[int],
    track_number: Optional[int], parsed_title: str, allow_single_track_fallback: bool = True,
) -> Optional[dict]:
    """Pick which of a release's tracks corresponds to one on-disk file, in
    priority order:

    1. An explicit ``recording_mbid`` (the human/bulk-job already knows
       exactly which recording -- e.g. picked from a recording search
       result) -- exact, no ambiguity.
    2. (disc_number, track_number) parsed from the file's own folder/name --
       the common case, since most libraries number their files to match
       the album's real tracklist.
    3. The release has exactly one track (a single) -- unambiguous
       regardless of what the file's own name says, **but only when
       ``allow_single_track_fallback`` is true**. The bulk scraper's groups
       loop calls this once per *local* file against the *same* resolved
       release; if the release genuinely (or wrongly) has only one track,
       this fallback would otherwise match every distinct local file in a
       multi-track album to that identical one track -- a real bug caught
       live: an 11-track compilation folder got matched to a MusicBrainz
       release that turned out to be a single-track live-broadcast
       recording, and every track in the folder silently received the
       exact same scraped title. The caller disables this fallback whenever
       there's more than one local file being matched against the same
       release (see ``_run_bulk_scrape_job``); it stays enabled for a
       genuine one-file-one-release match (the singles loop, a manual
       per-track apply, or a group that only has one local file).
    4. Closest case-insensitive title match against the file's parsed
       title.

    Returns ``None`` if nothing plausible matches -- the caller treats that
    as this candidate failing, not a crash.
    """
    tracks = release.get("tracks") or []
    if recording_mbid:
        for track in tracks:
            if track.get("recording_mbid") == recording_mbid:
                return track
        return None
    if disc_number is not None and track_number is not None:
        for track in tracks:
            track_disc = track.get("disc_number") or 1
            if track_disc == disc_number and track.get("track_number") == track_number:
                return track
    if allow_single_track_fallback and len(tracks) == 1:
        return tracks[0]
    if parsed_title:
        needle = parsed_title.strip().lower()
        for track in tracks:
            if str(track.get("title") or "").strip().lower() == needle:
                return track
    return None


def _select_release_candidate(results: list, local_count: int) -> dict:
    """Prefer the first release search result whose reported ``track_count``
    is plausible for the local group size (>= ``local_count`` -- a local rip
    is rarely more complete than the official release) over blindly trusting
    the top hit. Falls back to ``results[0]`` when no candidate's track count
    is known or plausible, so this never does worse than the old
    always-take-the-top-hit behavior.

    Exists because of a real bug caught live: the top search hit for an
    11-track local compilation folder was a MusicBrainz "Broadcast" release
    (a DJ radio set) with exactly one track -- picking it meant every one of
    the 11 distinct local files got matched against that same single track
    (see ``_match_track_in_release``'s ``allow_single_track_fallback``,
    which stops that collision even if a bad candidate is still picked, but
    preferring a track-count-plausible candidate here avoids the bad pick in
    the first place, so the group actually gets real per-track metadata
    instead of just failing safely).
    """
    for candidate in results:
        track_count = candidate.get("track_count")
        if isinstance(track_count, int) and track_count >= local_count:
            return candidate
    return results[0]


def _apply_matched_track(
    settings: Settings,
    entry_key: str,
    track_row: dict,
    release: dict,
    matched_track: dict,
    *,
    cover_client: Optional[CoverArtClient],
    cover_bytes_cache: Optional[dict] = None,
) -> dict:
    """Download art (once per release_mbid if ``cover_bytes_cache`` is
    provided -- the bulk job's optimization, since every track in a group
    shares the same release art; the per-track manual apply path always
    re-fetches, mirroring ``movies.metadata_manager.apply``'s simplicity)
    and save the matched track's metadata."""
    music_root = Path(settings.music_root).resolve()
    track_path = Path(track_row["absolute_path"]).resolve()
    cover_client = cover_client or _cover_client()

    art_relative_path: Optional[str] = None
    release_mbid = release.get("release_mbid") or ""
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
    if data:
        target = _artwork_path(track_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        art_relative_path = _relative_to_music_root(music_root, target)

    extra = {
        "artist": release.get("artist") or "",
        "album": release.get("title") or "",
        "genres": release.get("genres") or [],
        "release_date": release.get("date"),
        "track_number": matched_track.get("track_number"),
        "disc_number": matched_track.get("disc_number"),
        "duration_ms": matched_track.get("length_ms"),
        "release_mbid": release_mbid,
        "release_group_mbid": release.get("release_group_mbid") or "",
        "recording_mbid": matched_track.get("recording_mbid") or "",
    }
    return _music_store.save_music_metadata(
        settings.music_root,
        entry_key,
        provider="musicbrainz",
        provider_id=matched_track.get("recording_mbid") or release_mbid,
        title=matched_track.get("title") or "",
        art_relative_path=art_relative_path,
        artist_art_relative_path=None,  # always empty in v1 -- see the drone-music-feature skill
        extra=extra,
    )


def apply(
    settings: Settings,
    entry_key: str,
    release_mbid: str,
    *,
    recording_mbid: Optional[str] = None,
    client: Optional[MusicBrainzClient] = None,
    cover_client: Optional[CoverArtClient] = None,
) -> dict:
    """Look up a release (its full tracklist in one call), match this
    specific track within it, download art, and save the result. Raises
    ``MusicNotFoundError`` for an unknown entry_key, ``MusicMatchError`` if
    the release's tracklist has nothing plausibly corresponding to this
    file, ``MusicBrainzUnavailableError``/``MusicBrainzNotFoundError`` for
    scraper failures -- callers map these to the right HTTP status (see
    ``handlers_music.py``). ``client``/``cover_client`` are injectable for
    tests."""
    track_row = _music_store.get_music_by_key(settings.music_root, entry_key)
    if not track_row:
        raise MusicNotFoundError(entry_key)
    client = client or _client()
    release = client.release_lookup(release_mbid)

    location = _filename_parser.classify_location(track_row["file_path"])
    track_info = _filename_parser.parse_track_filename(track_row["track_name"])
    matched_track = _match_track_in_release(
        release,
        recording_mbid=recording_mbid,
        disc_number=location.disc_number or track_info.disc_number,
        track_number=track_info.track_number,
        parsed_title=track_info.title,
    )
    if not matched_track:
        raise MusicMatchError("Could not match this track to a specific song on the selected release")
    return _apply_matched_track(settings, entry_key, track_row, release, matched_track, cover_client=cover_client)


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


def delete_music(settings: Settings, entry_key: str) -> dict:
    """Permanently delete a track's underlying file plus any scraped
    metadata/artwork -- mirrors ``movies.metadata_manager.delete_movie``."""
    delete_metadata(settings, entry_key)
    return _music_store.delete_music_file(settings.music_root, entry_key)


# --------------------------------------------------------------- bulk scrape

def _has_artwork(settings: Settings, entry_key: str) -> bool:
    metadata = _music_store.get_music_metadata(settings.music_root, entry_key)
    return bool(metadata and metadata.get("art_relative_path"))


def search_track_default_query(
    settings: Settings, artist: str, album: str, track_title: str, *, client: Optional[MusicBrainzClient] = None,
) -> dict:
    """Per-track manual search's default (no custom query typed yet) case:
    tries ``filename_parser.search_candidates``'s ladder (release queries
    first, recording queries as fallback), stopping at the first rung that
    returns anything. Returns ``{"query", "results"}`` -- mirrors
    ``movies.metadata_manager.search_movie_default_query``."""
    client = client or _client()
    label = track_title or album or artist
    for query_type, query_text in _filename_parser.search_candidates(artist, album, track_title):
        label = query_text
        if query_type == "release":
            results = client.search_release(query_text)
            for item in results:
                item["kind"] = "release"
        else:
            results = client.search_recording(query_text)
            for item in results:
                item["kind"] = "recording"
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

    def _tick(current_label: str) -> None:
        _jobs.update_progress(
            settings, job_id,
            processed=processed, current_music=current_label,
            matched_count=matched, skipped_count=skipped, failed_count=failed,
        )

    for group in groups:
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
            allow_single_track_fallback = len(rows) == 1
            for row in rows:
                location = _filename_parser.classify_location(row["file_path"])
                track_info = _filename_parser.parse_track_filename(row["track_name"])
                matched_track = _match_track_in_release(
                    release, recording_mbid=None,
                    disc_number=location.disc_number or track_info.disc_number,
                    track_number=track_info.track_number, parsed_title=track_info.title,
                    allow_single_track_fallback=allow_single_track_fallback,
                )
                if not matched_track:
                    failed += 1
                    _record_item(settings, row, _job_items.STATUS_FAILED, "could not match this track within the resolved release")
                    continue
                _apply_matched_track(settings, row["entry_key"], row, release, matched_track, cover_client=cover_client, cover_bytes_cache=cover_bytes_cache)
                matched += 1
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
            matched_track = _match_track_in_release(
                release, recording_mbid=recording_result.get("recording_mbid"),
                disc_number=None, track_number=None, parsed_title=track_info.title,
            )
            if not matched_track:
                failed += 1
                _record_item(settings, row, _job_items.STATUS_FAILED, "could not match this track within the resolved release")
                processed += 1
                continue
            _apply_matched_track(settings, row["entry_key"], row, release, matched_track, cover_client=cover_client, cover_bytes_cache=cover_bytes_cache)
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


def get_bulk_scrape_items(settings: Settings, status: str, *, limit: int = 200, offset: int = 0) -> dict:
    return _job_items.list_by_status(settings, status, limit=limit, offset=offset)
