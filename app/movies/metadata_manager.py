"""Movie metadata/artwork scraper orchestration: TMDb API key storage, search,
and apply (download art + save metadata).

Mirrors ``device/smtp_manager.py``'s settings-state shape (one JSON blob in
the shared ``app_state`` table, sanitized before it ever reaches a browser)
for the API key, and ``roms/rom_artwork_apply.py``'s apply-flow shape
(fetch details, download the chosen images, write them to disk next to the
content they decorate, save metadata) for the actual scrape.

Unlike ROM artwork, movies have no gamelist.xml to write into. Scraped
metadata lives in ``storage/movies_store.py`` and artwork is written using
Plex's local-media convention so the same files are immediately usable by
the SWARM media server: movie ``poster.jpg``/``fanart.jpg`` files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

try:
    from ..common.settings import Settings
    from ..storage.state_store import database_path as _state_database_path
    from ..storage.state_store import load_payload as _load_state_payload
    from ..storage.state_store import save_payload as _save_state_payload
    from ..storage import movies_store as _movies_store
    from . import filename_parser as _filename_parser
    from .tmdb_client import TmdbClient, TmdbUnavailableError, parse_tmdb_movie_id
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage.state_store import database_path as _state_database_path  # type: ignore
    from storage.state_store import load_payload as _load_state_payload  # type: ignore
    from storage.state_store import save_payload as _save_state_payload  # type: ignore
    from storage import movies_store as _movies_store  # type: ignore
    from movies import filename_parser as _filename_parser  # type: ignore
    from movies.tmdb_client import TmdbClient, TmdbUnavailableError, parse_tmdb_movie_id  # type: ignore

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


def _movie_artwork_path(movie_absolute_path: Path, movies_root: Path, field: str) -> Path:
    """Return Plex's canonical local artwork path for one movie."""
    movie_absolute_path = movie_absolute_path.resolve()
    movies_root = movies_root.resolve()
    if movie_absolute_path.parent == movies_root:
        suffix = "" if field == "poster" else "-fanart"
        return movie_absolute_path.with_name(f"{movie_absolute_path.stem}{suffix}{TMDB_IMAGE_EXTENSION}")
    return movie_absolute_path.parent / ("poster.jpg" if field == "poster" else "fanart.jpg")


def _relative_to_media_root(settings: Settings, path: Path) -> str:
    return _movies_store.media_relative_path(settings.movies_root, path, settings.shows_root)


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
        target = _movie_artwork_path(movie_path, movies_root, "poster")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(details["poster_url"])
        target.write_bytes(data)
        poster_relative_path = _relative_to_media_root(settings, target)

    backdrop_relative_path: Optional[str] = None
    if details.get("backdrop_url"):
        target = _movie_artwork_path(movie_path, movies_root, "backdrop")
        target.parent.mkdir(parents=True, exist_ok=True)
        data, _content_type = client.download_image(details["backdrop_url"])
        target.write_bytes(data)
        backdrop_relative_path = _relative_to_media_root(settings, target)

    extra = {
        "overview": details.get("overview") or "",
        "tagline": details.get("tagline") or "",
        "genres": details.get("genres") or [],
        "cast": details.get("cast") or [],
        "release_date": details.get("release_date"),
        "rating": details.get("rating"),
        "runtime_minutes": details.get("runtime_minutes"),
        "youtube_trailer_key": details.get("youtube_trailer_key"),
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


def apply_by_reference(settings: Settings, entry_key: str, tmdb_reference: str, *, client: Optional[TmdbClient] = None) -> dict:
    """``apply()``'s direct-lookup counterpart: takes whatever a human pasted
    (a bare TMDb id, or a full themoviedb.org movie URL -- see
    ``tmdb_client.parse_tmdb_movie_id``) instead of a tmdb_id already chosen
    from this app's own search results. This is the escape hatch for a movie
    whose title search doesn't reliably surface it -- e.g. it's catalogued
    under an AKA/alternate title that TMDb's search only weakly (or never)
    matches against, so no amount of retyping the search box finds it, even
    though the human already found the right page by searching TMDb's own
    site directly. Raises ``ValueError`` (via parse_tmdb_movie_id) for a
    reference that isn't a recognizable id or movie URL, the same way a bad
    tmdb_id already does -- callers need no separate error handling."""
    tmdb_id = parse_tmdb_movie_id(tmdb_reference)
    return apply(settings, entry_key, tmdb_id, client=client)


def delete_metadata(settings: Settings, entry_key: str) -> dict:
    """Remove a movie/show entry's scraped TMDb metadata and artwork -- for
    when a scrape matched the wrong movie/show (a title that doesn't
    disambiguate well, e.g.) and a human needs to clear it before retrying,
    rather than being stuck with a permanently-wrong result. Deleting an
    entry with no metadata is a no-op, not an error (see
    ``movies_store.delete_movie_metadata``'s docstring) -- this mirrors how
    ``get_movie_metadata`` already treats "never scraped" as a normal state,
    not a missing-resource error."""
    deleted = _movies_store.delete_movie_metadata(settings.movies_root, entry_key)
    if not deleted:
        return {"deleted": False}
    for column in ("poster_relative_path", "backdrop_relative_path"):
        relative_path = deleted.get(column)
        if (
            deleted.get("provider") != "local"
            and relative_path
            and _movies_store.artwork_reference_count(settings.movies_root, relative_path) == 0
        ):
            try:
                target = _movies_store.resolve_media_relative_path(
                    settings.movies_root, relative_path, settings.shows_root
                )
            except FileNotFoundError:
                continue
            target.unlink(missing_ok=True)
    return {"deleted": True}


def delete_movie(settings: Settings, entry_key: str) -> dict:
    """Permanently delete a movie/episode's underlying file, plus any scraped
    metadata and artwork for it -- the Movies UI detail page's delete action.
    Unlike ``delete_metadata`` (which only clears a scrape gone wrong, leaving
    the file itself alone), this removes the file too."""
    delete_metadata(settings, entry_key)
    return _movies_store.delete_movie_file(settings.movies_root, entry_key, settings.shows_root)


# ------------------------------------------------------- default-query search

def movie_folder_name(movie: dict) -> Optional[str]:
    """The movie's immediate parent directory name, or ``None`` for a file
    sitting directly under ``movies_root`` (or any other path with no
    parent segment) -- fed to ``filename_parser.search_candidates``'s
    ``folder_name`` so a "Title (Year)" folder (a curated Radarr/Filebot-
    style movie folder, or this app's own "Shows/<Show (Year)>/" layout) can
    rescue a search even when the file's own name doesn't parse well."""
    parent = Path(movie.get("file_path") or movie.get("relative_path") or "").parent.name
    return parent or None


def _search_movie_ladder_with_label(client: TmdbClient, movie_name: str, folder_name: Optional[str] = None) -> Tuple[str, list]:
    """Try each ``(title, year)`` rung of ``filename_parser.search_candidates``
    against TMDb movie search, stopping at the first hit -- see that
    function's docstring for why the year-filtered title comes first (and,
    when ``folder_name`` is given, why a "Title (Year)" parent folder is
    tried before any filename-derived rung at all). A ``TmdbUnavailableError``
    on any rung propagates immediately.

    Also returns a human-readable label ("title (year)", or the bare title
    when there's no year) for whichever rung actually matched -- or the last
    rung tried, if none did -- so a caller that shows the query back to a
    human (the per-movie manual search's default, see
    ``search_movie_default_query`` below) can display what was actually
    searched. A plain punctuation-only cleanup (no scene-tag stripping, no
    year filter) is why a release like
    "10.Cloverfield.Lane.2016.1080p.BluRay.x264-[YTS.AG].mp4" used to come up
    empty from the movie's own details page unless the file was renamed by
    hand first."""
    stem = Path(movie_name or "").stem
    label = movie_name or ""
    for title, year in _filename_parser.search_candidates(stem, folder_name=folder_name):
        label = f"{title} ({year})" if year else title
        results = client.search(title, year=year)
        if results:
            return label, results
    return label, []


def search_movie_default_query(
    settings: Settings, movie_name: str, folder_name: Optional[str] = None, *, client: Optional[TmdbClient] = None,
) -> dict:
    """Per-movie manual search's default (no custom query typed yet) case:
    runs the ``_search_movie_ladder_with_label`` candidate ladder, so a
    first-time search on a movie's own details page finds the right match
    without the human first having to rename the file or hand-clean the
    query. Returns
    ``{"query", "results"}``: ``query`` is a human-readable label of whichever
    rung actually matched (or the last one tried, if none did), for the
    caller to show back to the user and reuse as the search box's value for
    a follow-up manual retry. ``client`` is injectable for tests, like
    ``search``/``apply`` above; real callers always resolve it from the
    stored API key."""
    client = client or _client(settings)
    query, results = _search_movie_ladder_with_label(client, movie_name, folder_name)
    return {"query": query, "results": results}
