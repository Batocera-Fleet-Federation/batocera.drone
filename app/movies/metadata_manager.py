"""Movie metadata/artwork scraper orchestration: TMDb API key storage, search,
and apply (download art + save metadata).

Mirrors ``device/smtp_manager.py``'s settings-state shape (one JSON blob in
the shared ``app_state`` table, sanitized before it ever reaches a browser)
for the API key, and ``roms/rom_artwork_apply.py``'s apply-flow shape
(fetch details, download the chosen images, write them to disk next to the
content they decorate, save metadata) for the actual scrape.

Unlike ROM artwork, movies have no gamelist.xml to write into -- scraped
metadata lives entirely in ``storage/movies_store.py``'s
``movies_metadata_entries`` table, and artwork files land in an ``images/``
folder that is a *sibling of the movie file itself* (not one shared root-level
folder), because movies can be nested arbitrarily deep (season/show
subfolders) and a single shared folder would risk basename collisions between
e.g. two different shows' "S01E01" files. Filenames follow the same
``<stem>-<source>-<field><ext>`` convention ROM scraped art uses (see
``rom_artwork_apply.py``), with ``field`` using the same vocabulary ROMs use
(``image`` for the primary poster, ``fanart`` for the backdrop) rather than
TMDb's own "poster"/"backdrop" terms, so it reads as "just like a ROM" per
the original feature ask.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage import movies_store as _movies_store
    from ..storage import movie_scrape_jobs as _jobs
    from ..storage import movie_scrape_job_items as _job_items
    from . import filename_parser as _filename_parser
    from .tmdb_client import TmdbClient, TmdbUnavailableError
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import movie_scrape_jobs as _jobs  # type: ignore
    from storage import movie_scrape_job_items as _job_items  # type: ignore
    from movies import filename_parser as _filename_parser  # type: ignore
    from movies.tmdb_client import TmdbClient, TmdbUnavailableError  # type: ignore

MOVIES_SCRAPER_STATE_NAMESPACE = "movies_scraper.json"
# TMDb only ever serves posters/backdrops as JPEG -- unlike a generic
# download, there's no need to sniff/guess an extension from Content-Type.
TMDB_IMAGE_EXTENSION = ".jpg"


class MovieNotFoundError(LookupError):
    """Raised when entry_key doesn't resolve to a known movie file."""


def _load_state(settings: Settings) -> dict:
    stored = _load_state_payload(_state_database_path(settings.userdata_root), MOVIES_SCRAPER_STATE_NAMESPACE, {})
    stored = stored if isinstance(stored, dict) else {}
    return {"api_key": str(stored.get("api_key") or "")}


def _save_state(settings: Settings, **updates) -> dict:
    state = _load_state(settings)
    state.update(updates)
    _save_state_payload(_state_database_path(settings.userdata_root), MOVIES_SCRAPER_STATE_NAMESPACE, state)
    return state


def get_settings(settings: Settings) -> dict:
    """Sanitized status -- the key itself never reaches a browser, same rule
    SMTP's password follows (see smtp_manager.py's _sanitized)."""
    state = _load_state(settings)
    return {"has_api_key": bool(state["api_key"])}


def update_settings(settings: Settings, api_key: str) -> dict:
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("TMDb API key is required")
    _save_state(settings, api_key=api_key)
    return {"has_api_key": True}


def _client(settings: Settings) -> TmdbClient:
    state = _load_state(settings)
    return TmdbClient(state["api_key"])


def search(settings: Settings, query: str, *, client: Optional[TmdbClient] = None) -> list:
    return (client or _client(settings)).search(query)


def clean_movie_query(file_name: str) -> str:
    """A reasonable starting search query from a scene/torrent-style release
    filename ("Movie.Title.2026.1080p.BluRay.x264-GROUP.mp4") -- strips the
    extension and replaces separator punctuation with spaces. Not trying to
    strip quality/group tags: TMDb's search is fuzzy enough to usually find
    the right movie anyway, and the per-movie search UI shows results before
    applying one, so a merely-decent default beats a fragile "smart" parser.
    Shared between the per-movie manual search (handlers_movies.py) and the
    bulk auto-scrape job below, which has no human to review a query."""
    name = Path(str(file_name or "")).stem
    name = re.sub(r"[.\-_,;:\[\]()<>]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _safe_movie_stem(movie_path: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", movie_path.stem) or "movie"


def _artwork_path(movie_absolute_path: Path, field: str) -> Path:
    """``<movie's own folder>/images/<safe-stem>-tmdb-<field>.jpg`` -- a
    sibling of the movie file at whatever depth it lives, mirroring the ROM
    convention of an images/ folder next to the content it decorates."""
    stem = _safe_movie_stem(movie_absolute_path)
    return movie_absolute_path.parent / "images" / f"{stem}-tmdb-{field}{TMDB_IMAGE_EXTENSION}"


def _relative_to_movies_root(movies_root: Path, path: Path) -> str:
    return path.resolve().relative_to(movies_root.resolve()).as_posix()


def apply(settings: Settings, entry_key: str, tmdb_id, *, client: Optional[TmdbClient] = None) -> dict:
    """Fetch details+cast from TMDb, download poster/backdrop art next to the
    movie file, and save the result. Raises MovieNotFoundError for an unknown
    entry_key, ValueError for a bad tmdb_id, TmdbUnavailableError if TMDb
    can't be reached or no API key is configured -- callers map these to the
    right HTTP status (see handlers_movies.py). ``client`` is injectable for
    tests; real callers always resolve it from the stored API key."""
    movie = _movies_store.get_movie_by_key(settings.movies_root, entry_key)
    if not movie:
        raise MovieNotFoundError(entry_key)
    movies_root = Path(settings.movies_root).resolve()
    movie_path = Path(movie["absolute_path"]).resolve()

    client = client or _client(settings)
    details = client.details(tmdb_id)

    poster_relative_path: Optional[str] = None
    if details.get("poster_url"):
        target = _artwork_path(movie_path, "image")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(details["poster_url"])
        target.write_bytes(data)
        poster_relative_path = _relative_to_movies_root(movies_root, target)

    backdrop_relative_path: Optional[str] = None
    if details.get("backdrop_url"):
        target = _artwork_path(movie_path, "fanart")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(details["backdrop_url"])
        target.write_bytes(data)
        backdrop_relative_path = _relative_to_movies_root(movies_root, target)

    extra = {
        "overview": details.get("overview") or "",
        "tagline": details.get("tagline") or "",
        "genres": details.get("genres") or [],
        "cast": details.get("cast") or [],
        "release_date": details.get("release_date"),
        "rating": details.get("rating"),
        "runtime_minutes": details.get("runtime_minutes"),
    }
    return _movies_store.save_movie_metadata(
        settings.movies_root,
        entry_key,
        provider="tmdb",
        provider_id=str(details.get("tmdb_id") or tmdb_id),
        title=details.get("title") or "",
        poster_relative_path=poster_relative_path,
        backdrop_relative_path=backdrop_relative_path,
        extra=extra,
    )


def apply_tv_episode(
    settings: Settings,
    entry_key: str,
    tv_id,
    season_number: int,
    episode_number: int,
    *,
    show_details: Optional[dict] = None,
    season_details: Optional[dict] = None,
    client: Optional[TmdbClient] = None,
) -> dict:
    """TV-episode counterpart of ``apply()``: fetches show-level details
    (backdrop/genres/cast -- show-level in TMDb's data model, see
    ``TmdbClient.tv_details``), this season's own details (TMDb gives each
    season its own poster, often visibly different from the show's general
    poster -- see ``TmdbClient.tv_season_details``), and this specific
    episode's title/overview/air-date/still, downloads artwork next to the
    episode file the same way ``apply()`` does for a movie, and saves it as
    one ``movies_metadata_entries`` row like any other scrape (``provider``
    distinguishes it: ``"tmdb_tv"`` vs plain movies' ``"tmdb"``).
    ``show_details``/``season_details`` let a caller that already fetched
    them for an earlier episode of the same show/season (the bulk job does,
    once each) skip refetching for every subsequent episode."""
    movie = _movies_store.get_movie_by_key(settings.movies_root, entry_key)
    if not movie:
        raise MovieNotFoundError(entry_key)
    movies_root = Path(settings.movies_root).resolve()
    movie_path = Path(movie["absolute_path"]).resolve()

    client = client or _client(settings)
    show = show_details if show_details is not None else client.tv_details(tv_id)
    season = season_details if season_details is not None else client.tv_season_details(tv_id, season_number)
    episode = client.tv_episode_details(tv_id, season_number, episode_number)

    # Season poster first -- it's usually a distinct image from the show's
    # general poster (this is what makes clicking between seasons on the
    # show detail page visibly change the artwork, not just the episode
    # list). Falls back to the show poster for the (uncommon) case where
    # TMDb has no dedicated poster for this season.
    poster_relative_path: Optional[str] = None
    poster_source = season.get("poster_url") or show.get("poster_url")
    if poster_source:
        target = _artwork_path(movie_path, "image")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(poster_source)
        target.write_bytes(data)
        poster_relative_path = _relative_to_movies_root(movies_root, target)

    # The episode's own "still" (a screenshot from that episode) is more
    # useful as this file's backdrop than the show-wide backdrop would be --
    # fall back to the show backdrop only when TMDb has no still for it.
    # (TMDb seasons have no backdrop of their own at all, unlike posters.)
    backdrop_relative_path: Optional[str] = None
    backdrop_source = episode.get("still_url") or show.get("backdrop_url")
    if backdrop_source:
        target = _artwork_path(movie_path, "fanart")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(backdrop_source)
        target.write_bytes(data)
        backdrop_relative_path = _relative_to_movies_root(movies_root, target)

    season_number, episode_number = int(season_number), int(episode_number)
    title = f"{show.get('title') or ''} - S{season_number:02d}E{episode_number:02d}"
    if episode.get("title"):
        title += f" - {episode['title']}"

    extra = {
        "media_type": "tv_episode",
        "show_title": show.get("title") or "",
        "season_number": season_number,
        "episode_number": episode_number,
        "episode_title": episode.get("title") or "",
        "season_name": season.get("title") or "",
        "season_overview": season.get("overview") or "",
        "overview": episode.get("overview") or show.get("overview") or "",
        "tagline": show.get("tagline") or "",
        "genres": show.get("genres") or [],
        "cast": show.get("cast") or [],
        "release_date": episode.get("air_date") or show.get("release_date"),
        "rating": episode.get("rating") if episode.get("rating") is not None else show.get("rating"),
    }
    return _movies_store.save_movie_metadata(
        settings.movies_root,
        entry_key,
        provider="tmdb_tv",
        provider_id=f"{show.get('tmdb_id') or tv_id}-s{season_number}e{episode_number}",
        title=title,
        poster_relative_path=poster_relative_path,
        backdrop_relative_path=backdrop_relative_path,
        extra=extra,
    )


# --------------------------------------------------------------- bulk scrape

def _has_artwork(settings: Settings, entry_key: str) -> bool:
    """"Missing artwork" (the bulk job's skip condition when ``rescan_all`` is
    false) means no poster on file yet -- the poster is the primary artwork a
    user actually notices missing; a movie that TMDb had no backdrop for but
    does have a poster doesn't need to be re-queued every run."""
    metadata = _movies_store.get_movie_metadata(settings.movies_root, entry_key)
    return bool(metadata and metadata.get("poster_relative_path"))


# A brief pause before each TMDb-touching candidate in a bulk run -- the
# search ladder alone can issue up to 4 requests for one movie
# (filename_parser.search_candidates), and without any pacing a large
# library can trip TMDb's rate limiting well before the run finishes (see
# TmdbClient's TMDB_MAX_429_RETRIES docstring for what an untreated 429
# used to cascade into: a whole contiguous block of "failed" results that
# had nothing to do with those movies' filenames). Module-level so tests
# can zero it out via mock.patch instead of eating this delay for real.
_REQUEST_THROTTLE_SECONDS = 0.2


def _throttle_before_tmdb_call() -> None:
    if _REQUEST_THROTTLE_SECONDS > 0:
        time.sleep(_REQUEST_THROTTLE_SECONDS)


def _search_movie_with_ladder(client: TmdbClient, movie_name: str) -> list:
    """Try each ``(title, year)`` rung of ``filename_parser.search_candidates``
    against TMDb movie search, stopping at the first hit -- see that
    function's docstring for why the year-filtered title comes first. A
    ``TmdbUnavailableError`` on any rung propagates immediately (the caller's
    "stop the whole job" handling applies the same as a single-candidate
    search would)."""
    stem = Path(movie_name or "").stem
    for title, year in _filename_parser.search_candidates(stem):
        results = client.search(title, year=year)
        if results:
            return results
    return []


def _search_show_with_ladder(client: TmdbClient, show_title: str, year: Optional[str]) -> list:
    """TV counterpart of ``_search_movie_with_ladder`` -- just the
    year-filtered-then-unfiltered pair, since ``classify()`` already parsed a
    clean show title straight from the episode's own filename (no scene-tag
    stripping needed the way a movie filename needs it)."""
    attempts = [(show_title, year)] if year else []
    attempts.append((show_title, None))
    for title, attempt_year in attempts:
        if not title:
            continue
        results = client.search_tv(title, year=attempt_year)
        if results:
            return results
    return []


def _record_item(settings: Settings, movie: dict, status: str, reason: str = "") -> None:
    """Log one candidate's outcome to ``movie_scrape_job_items`` -- backs the
    admin UI's clickable matched/skipped/failed breakdown and "retry failed"
    action. Best-effort: a logging failure here must never take down the
    scrape itself, so it's swallowed rather than propagated."""
    try:
        _job_items.record(
            settings, movie.get("entry_key") or "", movie.get("movie_name") or "",
            movie.get("file_path") or movie.get("relative_path") or "", status, reason,
        )
    except Exception:  # noqa: BLE001 - diagnostics must not break the scrape
        pass


def _run_bulk_scrape_job(settings: Settings, job_id: int, candidates: list, client: TmdbClient) -> None:
    matched = skipped = failed = 0
    total = len(candidates)
    # Per-job caches so a show with a dozen episodes only costs one TV search
    # and one show-details fetch (and a season's worth of episodes only one
    # season-details fetch), not one per episode file.
    show_id_by_title: dict = {}
    show_details_by_id: dict = {}
    season_details_by_key: dict = {}
    for index, movie in enumerate(candidates):
        _jobs.update_progress(
            settings, job_id,
            processed=index, current_movie=movie.get("movie_name") or "",
            matched_count=matched, skipped_count=skipped, failed_count=failed,
        )
        try:
            parsed = _filename_parser.classify(
                movie.get("file_path") or movie.get("relative_path") or "", movie.get("movie_name") or ""
            )
            if parsed.kind == _filename_parser.KIND_EXTRA:
                # Bonus/behind-the-scenes content living in a Plex/Kodi
                # extras folder (Featurettes, Interviews, ...) -- never a
                # real movie or episode, so never worth a TMDb call. Counted
                # with the other "nothing to search for" cases, not failed.
                skipped += 1
                _record_item(settings, movie, _job_items.STATUS_SKIPPED, "bonus/extra content (Featurettes-style folder)")
                continue

            if parsed.kind == _filename_parser.KIND_EPISODE:
                _throttle_before_tmdb_call()
                show_key = parsed.show_title.lower()
                tv_id = show_id_by_title.get(show_key)
                if tv_id is None:
                    show_results = _search_show_with_ladder(client, parsed.show_title, parsed.year)
                    if not show_results:
                        failed += 1
                        _record_item(
                            settings, movie, _job_items.STATUS_FAILED,
                            f"no TMDb show match for \"{parsed.show_title}\"",
                        )
                        continue
                    tv_id = show_results[0]["tmdb_id"]
                    show_id_by_title[show_key] = tv_id
                show_details = show_details_by_id.get(tv_id)
                if show_details is None:
                    show_details = client.tv_details(tv_id)
                    show_details_by_id[tv_id] = show_details
                season_key = (tv_id, parsed.season)
                season_details = season_details_by_key.get(season_key)
                if season_details is None:
                    season_details = client.tv_season_details(tv_id, parsed.season)
                    season_details_by_key[season_key] = season_details
                apply_tv_episode(
                    settings, movie["entry_key"], tv_id, parsed.season, parsed.episode,
                    show_details=show_details, season_details=season_details, client=client,
                )
                matched += 1
                _record_item(settings, movie, _job_items.STATUS_MATCHED)
                continue

            stem = Path(movie.get("movie_name") or "").stem
            if not _filename_parser.search_candidates(stem):
                # Nothing usable to search TMDb with (e.g. a filename that's
                # all punctuation) -- counted separately from "failed" since
                # no TMDb request was even attempted for this one.
                skipped += 1
                _record_item(settings, movie, _job_items.STATUS_SKIPPED, "filename has no usable title text")
                continue
            _throttle_before_tmdb_call()
            results = _search_movie_with_ladder(client, movie.get("movie_name") or "")
            if not results:
                failed += 1
                _record_item(settings, movie, _job_items.STATUS_FAILED, "no TMDb results for any tried title/year")
                continue
            apply(settings, movie["entry_key"], results[0]["tmdb_id"], client=client)
            matched += 1
            _record_item(settings, movie, _job_items.STATUS_MATCHED)
        except TmdbUnavailableError as error:
            # The key was rejected or TMDb is unreachable/still rate-limited
            # after TmdbClient's own retries -- every remaining candidate
            # would fail the same way, so stop early rather than burning
            # through the whole list one doomed request at a time. Still
            # record each remaining candidate individually (not just the
            # aggregate count) so "retry failed" has exact entry_keys to
            # re-queue once the underlying problem (usually transient
            # rate-limiting) has cleared.
            reason = str(error)
            for remaining in candidates[index:]:
                _record_item(settings, remaining, _job_items.STATUS_FAILED, reason)
            failed += total - index
            break
        except Exception as error:  # noqa: BLE001 - one bad movie must not kill the whole run
            failed += 1
            _record_item(settings, movie, _job_items.STATUS_FAILED, f"error: {error}")
    _jobs.update_progress(
        settings, job_id,
        processed=total, current_movie="",
        matched_count=matched, skipped_count=skipped, failed_count=failed,
    )
    _jobs.mark_complete(settings, job_id)


# Guards the any_running()-check-then-create_running()-insert sequence below
# against two nearly-simultaneous POSTs (the admin app is a threaded HTTP
# server -- one request per thread) both reading "nothing running yet"
# before either has inserted its row, which would start two jobs at once.
# The SQLite row is still the cross-restart source of truth (matching
# config_backup.py); this lock only closes the same-process race window
# between that read and the write.
_BULK_SCRAPE_START_LOCK = threading.Lock()


def start_bulk_scrape(settings: Settings, *, rescan_all: bool = False, client: Optional[TmdbClient] = None) -> dict:
    """Kick off a background job that scrapes every movie (``rescan_all``) or
    only those still missing a poster (the default) -- searching TMDb by a
    cleaned-up filename and auto-applying the top match, with no per-movie
    confirmation (unlike the manual search-and-apply flow on a movie's own
    details page). Only one job runs at a time; ``movie_scrape_jobs`` (not an
    in-process flag) is the guard, same reasoning as config_backup.py's
    ``any_creating`` check. ``client`` is injectable for tests, like
    ``search``/``apply`` above; real callers always resolve it from the
    stored API key."""
    with _BULK_SCRAPE_START_LOCK:
        if _jobs.any_running(settings):
            return {"status": "already_running"}
        try:
            client = client or _client(settings)
        except TmdbUnavailableError as error:
            return {"status": "error", "error": str(error)}
        movies = _movies_store.list_movies(settings.movies_root)
        candidates = movies if rescan_all else [m for m in movies if not _has_artwork(settings, m["entry_key"])]
        # A fresh full/default run supersedes the previous run's breakdown
        # entirely -- clear it before scraping starts so a status/items
        # check made right after this returns never sees stale rows from an
        # earlier run mixed in with the new one.
        _job_items.clear(settings)
        job = _jobs.create_running(settings, rescan_all=rescan_all, total=len(candidates))
    thread = threading.Thread(
        target=_run_bulk_scrape_job, args=(settings, job["id"], candidates, client),
        name="movie-bulk-scrape", daemon=True,
    )
    thread.start()
    return {"status": "ok", "job": job}


def retry_bulk_scrape_items(
    settings: Settings, *, status: Optional[str] = None, entry_keys: Optional[list] = None,
    client: Optional[TmdbClient] = None,
) -> dict:
    """Re-run the bulk scraper over a specific subset of the *last* run's
    results: either every item still at ``status`` (e.g. ``"failed"`` -- the
    common case, since a whole block of "failed" is usually TMDb rate-
    limiting that's since cleared, not 1,500 genuinely unmatchable movies)
    or an explicit ``entry_keys`` list for retrying one/a few by hand. Same
    one-job-at-a-time guard and background-thread shape as
    ``start_bulk_scrape``, but deliberately does **not** clear
    ``movie_scrape_job_items`` first -- each retried item's row is upserted
    in place by ``_run_bulk_scrape_job`` (moving it out of whichever bucket
    it was retried from), while every other item from the last run is left
    exactly as it was."""
    with _BULK_SCRAPE_START_LOCK:
        if _jobs.any_running(settings):
            return {"status": "already_running"}
        try:
            client = client or _client(settings)
        except TmdbUnavailableError as error:
            return {"status": "error", "error": str(error)}
        keys = list(entry_keys) if entry_keys else _job_items.entry_keys_by_status(settings, status or _job_items.STATUS_FAILED)
        candidates = []
        for key in keys:
            movie = _movies_store.get_movie_by_key(settings.movies_root, key)
            if movie:  # skip anything removed from disk since the last run
                candidates.append(movie)
        job = _jobs.create_running(settings, rescan_all=False, total=len(candidates))
    thread = threading.Thread(
        target=_run_bulk_scrape_job, args=(settings, job["id"], candidates, client),
        name="movie-bulk-scrape-retry", daemon=True,
    )
    thread.start()
    return {"status": "ok", "job": job}


def get_bulk_scrape_status(settings: Settings) -> Optional[dict]:
    return _jobs.latest(settings)


def get_bulk_scrape_items(settings: Settings, status: str, *, limit: int = 200, offset: int = 0) -> dict:
    return _job_items.list_by_status(settings, status, limit=limit, offset=offset)
