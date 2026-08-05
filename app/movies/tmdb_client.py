"""TMDb (The Movie Database) client: search, details+credits, image download.

Used only by the Movies tab's metadata/artwork scraper -- see
``movies/metadata_manager.py`` for where the API key is stored and
``web/handlers_movies.py`` for the admin routes that call this. Same shape as
``app/roms/scrapers.py``'s ``LaunchBoxClient`` -- stdlib ``urllib`` only, no
third-party deps -- but movies have no per-system/per-platform matching
concern LaunchBox has, so this is considerably simpler.

Unlike LaunchBox/TheGamesDB (both keyless), TMDb requires a free API key --
there is no keyless equivalent with comparable cast/description/genre/poster
data for movies. The key is supplied by the user via the admin UI (see
``metadata_manager.py``'s state storage), never hardcoded or read from an env
var here -- this module only ever receives it as a parameter.
"""

from __future__ import annotations

import json
import re
import time
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"
TMDB_IMAGE_HOST = "image.tmdb.org"
# w500/w1280 balance quality against a locked-down device's limited storage --
# TMDb also serves "original", but that's far larger than this needs.
TMDB_POSTER_SIZE = "w500"
TMDB_POSTER_THUMB_SIZE = "w154"
TMDB_BACKDROP_SIZE = "w1280"
# Cast lists can run to 100+ credited people for a large production; the
# `credits` payload is already ordered by billing, so the top 20 is the
# meaningful cast, not an arbitrary cut.
TMDB_CAST_LIMIT = 20
SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# A bulk scrape can fire thousands of requests in a short span (the search
# ladder alone can issue up to 4 per movie -- see filename_parser.py); a 429
# there used to be treated exactly like a revoked API key ("stop the whole
# job, mark every remaining candidate failed") by metadata_manager's bulk
# job, which is almost certainly why a real 2,667-movie run once came back
# "1,054 matched, 1,576 failed" -- a contiguous block of failures starting
# right where TMDb started throttling, not 1,576 genuinely unmatchable
# movies. Retrying a 429 with backoff here (honoring Retry-After when TMDb
# sends one) turns that transient condition back into what it actually is.
TMDB_MAX_429_RETRIES = 3
TMDB_RETRY_BASE_DELAY_SECONDS = 1.0


class TmdbUnavailableError(RuntimeError):
    """Raised when TMDb can't be reached, rejects the request, or no API key is configured."""


class TmdbNotFoundError(TmdbUnavailableError):
    """A specific TMDb id (movie/show/season/episode) doesn't exist -- unlike
    TmdbUnavailableError's other cases (invalid API key, unreachable,
    rate-limited), which mean every subsequent call will fail the same way,
    this is scoped to one lookup: the id was valid a moment ago (usually just
    returned by a search call), but the specific resource it points at
    doesn't exist. Most commonly a locally-numbered TV episode or season that
    doesn't match TMDb's own numbering -- a season finale split into a
    different number of parts than TMDb's, a "Season 0" specials folder TMDb
    has no matching season for, etc. -- real libraries are full of these.

    Subclasses TmdbUnavailableError so any existing ``except
    TmdbUnavailableError`` catch (e.g. the per-movie manual scrape's HTTP 502
    mapping) still catches this unchanged; only the bulk scrape job's
    per-candidate loop needs to tell the two apart, so a single 404 fails
    just that one candidate instead of aborting the whole run. Real incident
    this fixed: two consecutive bulk runs against a ~1,250-movie library each
    reported roughly "2 matched / 88 skipped / 1156 failed" in under 15
    seconds -- 1,151 of those "failures" shared one identical reason string
    ("TMDb has no movie with that id"), because a single 404 partway through
    the run (a mis-numbered episode) used to abort the whole job and stamp
    every untouched remaining candidate -- including obviously-unrelated
    movies and Featurettes-folder extras -- with that one error, rather than
    just failing the one candidate that actually hit it."""


def tmdb_image_url(path: Optional[str], size: str) -> Optional[str]:
    if not path:
        return None
    return f"{TMDB_IMAGE_BASE}/{size}{path}"


def _digits_only(value) -> str:
    """String-of-digits form of an id/number parameter, treating ``None``
    (not a falsy ``0``) as "missing". Season/episode numbers can legitimately
    be ``0`` (Plex/Kodi/Jellyfin's "Season 0" convention for standalone TV
    specials), and the tempting ``str(value or "")`` idiom is wrong here --
    ``0 or ""`` evaluates to ``""``, silently treating a real season-0 episode
    as if the parameter were never passed at all and raising a spurious
    "required" error instead of ever reaching TMDb."""
    return re.sub(r"[^0-9]", "", "" if value is None else str(value))


class TmdbClient:
    def __init__(self, api_key: str, timeout_seconds: int = 15):
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise TmdbUnavailableError("No TMDb API key is configured")

    def _retry_delay_seconds(self, error: HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return max(0.5, float(retry_after))
            except (TypeError, ValueError):
                pass  # not a plain-seconds value (could be an HTTP-date) -- fall back to backoff
        return TMDB_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)

    def _get_json(self, path: str, params: Optional[dict] = None) -> dict:
        query = dict(params or {})
        query["api_key"] = self.api_key
        url = f"{TMDB_API_BASE}{path}?{urlencode(query)}"
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT, "Accept": "application/json"})
        attempt = 0
        while True:
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                if error.code == 401:
                    raise TmdbUnavailableError("TMDb rejected the configured API key") from error
                if error.code == 404:
                    raise TmdbNotFoundError("TMDb has no result for that id") from error
                if error.code == 429 and attempt < TMDB_MAX_429_RETRIES:
                    time.sleep(self._retry_delay_seconds(error, attempt))
                    attempt += 1
                    continue
                if error.code == 429:
                    raise TmdbUnavailableError("TMDb is rate-limiting this Drone (429), retries exhausted") from error
                raise TmdbUnavailableError(f"TMDb returned HTTP {error.code}") from error
            except (URLError, TimeoutError, OSError) as error:
                raise TmdbUnavailableError("TMDb could not be reached from this Drone") from error

    def search(self, query: str, limit: int = 10, *, year: Optional[str] = None) -> List[dict]:
        """``year`` (when given) is passed as TMDb's ``primary_release_year``
        filter -- the single biggest disambiguator for a scene-release
        filename, since remakes/reboots sharing a title are common (this
        matters even for something as ordinary as "Halloween", which spans a
        dozen movies from 1978 to 2022). Callers that already parsed a year
        out of the filename should always pass it; a caller unsure whether
        the parsed year is trustworthy can retry once with ``year=None``."""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        params = {"query": normalized_query, "include_adult": "false"}
        if year:
            params["primary_release_year"] = str(year)
        payload = self._get_json("/search/movie", params)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        output = []
        for item in results:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "tmdb_id": item.get("id"),
                    "title": item.get("title") or item.get("original_title") or "",
                    "release_date": item.get("release_date") or None,
                    "overview": item.get("overview") or "",
                    "thumbnail_url": tmdb_image_url(item.get("poster_path"), TMDB_POSTER_THUMB_SIZE),
                }
            )
        return output[: max(1, min(int(limit), 20))]

    def search_tv(self, query: str, limit: int = 10, *, year: Optional[str] = None) -> List[dict]:
        """Same shape as ``search`` but against ``/search/tv`` -- TMDb's TV
        equivalent of ``primary_release_year`` is ``first_air_date_year``."""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        params = {"query": normalized_query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = str(year)
        payload = self._get_json("/search/tv", params)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        output = []
        for item in results:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "tmdb_id": item.get("id"),
                    "title": item.get("name") or item.get("original_name") or "",
                    "release_date": item.get("first_air_date") or None,
                    "overview": item.get("overview") or "",
                    "thumbnail_url": tmdb_image_url(item.get("poster_path"), TMDB_POSTER_THUMB_SIZE),
                }
            )
        return output[: max(1, min(int(limit), 20))]

    def details(self, tmdb_id) -> dict:
        safe_id = _digits_only(tmdb_id)
        if not safe_id:
            raise ValueError("tmdb_id is required")
        # append_to_response=credits gets cast/crew in the same request instead
        # of a separate /credits call -- one HTTP round trip per apply.
        payload = self._get_json(f"/movie/{safe_id}", {"append_to_response": "credits"})
        genres = [genre.get("name") for genre in (payload.get("genres") or []) if isinstance(genre, dict) and genre.get("name")]
        credits_payload = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
        cast = []
        for member in (credits_payload.get("cast") or [])[:TMDB_CAST_LIMIT]:
            if not isinstance(member, dict) or not member.get("name"):
                continue
            cast.append({"name": member.get("name"), "character": member.get("character") or ""})
        return {
            "tmdb_id": payload.get("id"),
            "title": payload.get("title") or payload.get("original_title") or "",
            "overview": payload.get("overview") or "",
            "tagline": payload.get("tagline") or "",
            "genres": genres,
            "cast": cast,
            "release_date": payload.get("release_date") or None,
            "rating": payload.get("vote_average"),
            "runtime_minutes": payload.get("runtime"),
            "poster_url": tmdb_image_url(payload.get("poster_path"), TMDB_POSTER_SIZE),
            "backdrop_url": tmdb_image_url(payload.get("backdrop_path"), TMDB_BACKDROP_SIZE),
        }

    def tv_details(self, tv_id) -> dict:
        """Show-level details -- poster/backdrop/genres/cast are all
        show-level in TMDb's data model (there's no per-episode poster, only
        a per-episode "still" screenshot -- see ``tv_episode_details``), so
        this is fetched once per show and reused across every episode file
        that show has on disk."""
        safe_id = _digits_only(tv_id)
        if not safe_id:
            raise ValueError("tv_id is required")
        payload = self._get_json(f"/tv/{safe_id}", {"append_to_response": "credits"})
        genres = [genre.get("name") for genre in (payload.get("genres") or []) if isinstance(genre, dict) and genre.get("name")]
        credits_payload = payload.get("credits") if isinstance(payload.get("credits"), dict) else {}
        cast = []
        for member in (credits_payload.get("cast") or [])[:TMDB_CAST_LIMIT]:
            if not isinstance(member, dict) or not member.get("name"):
                continue
            cast.append({"name": member.get("name"), "character": member.get("character") or ""})
        return {
            "tmdb_id": payload.get("id"),
            "title": payload.get("name") or payload.get("original_name") or "",
            "overview": payload.get("overview") or "",
            "tagline": payload.get("tagline") or "",
            "genres": genres,
            "cast": cast,
            "release_date": payload.get("first_air_date") or None,
            "rating": payload.get("vote_average"),
            "poster_url": tmdb_image_url(payload.get("poster_path"), TMDB_POSTER_SIZE),
            "backdrop_url": tmdb_image_url(payload.get("backdrop_path"), TMDB_BACKDROP_SIZE),
        }

    def tv_season_details(self, tv_id, season_number) -> dict:
        """Season-level details: TMDb gives each season its own poster
        (often a distinct image from the show's own poster -- see
        ``metadata_manager.apply_tv_episode``, which prefers this over the
        show poster) and occasionally its own overview, but *not* its own
        backdrop -- there's no ``backdrop_path`` on a TMDb season object, so
        that stays show-level. Raises ``TmdbUnavailableError`` (404 mapped
        the same way ``tv_episode_details`` does) if the show has no such
        season."""
        safe_tv_id = _digits_only(tv_id)
        safe_season = _digits_only(season_number)
        if not safe_tv_id or not safe_season:
            raise ValueError("tv_id and season_number are required")
        payload = self._get_json(f"/tv/{safe_tv_id}/season/{safe_season}")
        return {
            "title": payload.get("name") or "",
            "overview": payload.get("overview") or "",
            "air_date": payload.get("air_date") or None,
            "poster_url": tmdb_image_url(payload.get("poster_path"), TMDB_POSTER_SIZE),
        }

    def tv_episode_details(self, tv_id, season_number, episode_number) -> dict:
        """Episode-level details: title, overview, air date, rating, and a
        "still" (a screenshot from the episode -- the closest TMDb has to
        per-episode artwork). Raises ``TmdbUnavailableError`` (404 mapped the
        same way ``details`` does) if the show has no such season/episode,
        e.g. a locally-numbered episode that doesn't match TMDb's numbering."""
        safe_tv_id = _digits_only(tv_id)
        safe_season = _digits_only(season_number)
        safe_episode = _digits_only(episode_number)
        if not safe_tv_id or not safe_season or not safe_episode:
            raise ValueError("tv_id, season_number, and episode_number are required")
        payload = self._get_json(f"/tv/{safe_tv_id}/season/{safe_season}/episode/{safe_episode}")
        return {
            "title": payload.get("name") or "",
            "overview": payload.get("overview") or "",
            "air_date": payload.get("air_date") or None,
            "rating": payload.get("vote_average"),
            "still_url": tmdb_image_url(payload.get("still_path"), TMDB_BACKDROP_SIZE),
        }

    def download_image(self, url: str) -> Tuple[bytes, str]:
        parsed = urlparse(str(url or ""))
        if parsed.scheme != "https" or parsed.netloc != TMDB_IMAGE_HOST:
            raise ValueError("image_url must be a TMDb image URL")
        request = Request(url, headers={"User-Agent": SCRAPER_USER_AGENT, "Accept": "image/*"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            content_type = response.headers.get_content_type()
            if not str(content_type or "").startswith("image/"):
                raise ValueError("image_url did not return an image")
            max_bytes = 20 * 1024 * 1024
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("image is too large")
            return data, content_type
