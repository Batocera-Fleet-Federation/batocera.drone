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
from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage import movies_store as _movies_store
    from ..storage import movie_scrape_jobs as _jobs
    from .tmdb_client import TmdbClient, TmdbUnavailableError
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from storage import movie_scrape_jobs as _jobs  # type: ignore
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


# --------------------------------------------------------------- bulk scrape

def _has_artwork(settings: Settings, entry_key: str) -> bool:
    """"Missing artwork" (the bulk job's skip condition when ``rescan_all`` is
    false) means no poster on file yet -- the poster is the primary artwork a
    user actually notices missing; a movie that TMDb had no backdrop for but
    does have a poster doesn't need to be re-queued every run."""
    metadata = _movies_store.get_movie_metadata(settings.movies_root, entry_key)
    return bool(metadata and metadata.get("poster_relative_path"))


def _run_bulk_scrape_job(settings: Settings, job_id: int, candidates: list, client: TmdbClient) -> None:
    matched = skipped = failed = 0
    total = len(candidates)
    for index, movie in enumerate(candidates):
        _jobs.update_progress(
            settings, job_id,
            processed=index, current_movie=movie.get("movie_name") or "",
            matched_count=matched, skipped_count=skipped, failed_count=failed,
        )
        try:
            query = clean_movie_query(movie.get("movie_name") or "")
            if not query:
                # Nothing usable to search TMDb with (e.g. a filename that's
                # all punctuation) -- counted separately from "failed" since
                # no TMDb request was even attempted for this one.
                skipped += 1
                continue
            results = client.search(query)
            if not results:
                failed += 1
                continue
            apply(settings, movie["entry_key"], results[0]["tmdb_id"], client=client)
            matched += 1
        except TmdbUnavailableError:
            # The key was rejected or TMDb is unreachable -- every remaining
            # candidate would fail the same way, so stop early rather than
            # burning through the whole list one doomed request at a time.
            failed += total - index
            break
        except Exception:  # noqa: BLE001 - one bad movie must not kill the whole run
            failed += 1
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
        job = _jobs.create_running(settings, rescan_all=rescan_all, total=len(candidates))
    thread = threading.Thread(
        target=_run_bulk_scrape_job, args=(settings, job["id"], candidates, client),
        name="movie-bulk-scrape", daemon=True,
    )
    thread.start()
    return {"status": "ok", "job": job}


def get_bulk_scrape_status(settings: Settings) -> Optional[dict]:
    return _jobs.latest(settings)
