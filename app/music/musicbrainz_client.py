"""MusicBrainz client: release/recording search, release lookup (with its
full tracklist), no API key required.

Used only by the Music tab's metadata scraper -- see
``music/metadata_manager.py`` for orchestration and ``web/handlers_music.py``
for the admin routes that call this. Same "stdlib urllib client class" shape
as ``movies/tmdb_client.py``, but MusicBrainz is keyless (unlike TMDb) --
there is no user-supplied credential anywhere in this module, and no
settings-storage counterpart to ``metadata_manager.get_settings``/
``update_settings`` exists for Music at all.

MusicBrainz's own API etiquette policy (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)
requires a descriptive ``User-Agent`` identifying the application (not a
browser UA the way TMDb's scraper client uses -- MusicBrainz explicitly asks
for the opposite) and a *proactive* self-throttle to roughly one request per
second, unlike TMDb's purely reactive 429-retry-with-backoff. Both matter
here: skipping the User-Agent gets a client blocked outright, and a bulk
scrape run that doesn't self-throttle will trip MusicBrainz's own rate
limiting well before a modest-sized library finishes.
"""

from __future__ import annotations

import json
import threading
import time
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2"
# Identifies this application per MusicBrainz's API etiquette policy -- not a
# secret, just required so MusicBrainz can contact a maintainer if a client
# misbehaves. Deliberately fixed/non-configurable (there's no per-user
# account or key to attach it to, unlike TMDb).
MUSICBRAINZ_USER_AGENT = (
    "BatoceraDroneMusicScraper/1.0 "
    "(+https://github.com/Batocera-Fleet-Federation/batocera.drone)"
)
# MusicBrainz's own guidance: no more than ~1 request/second for an
# unauthenticated client. Enforced unconditionally on every request (not just
# inside the bulk job), via a module-level "time of last call" gate shared by
# every MusicBrainzClient instance in this process.
MUSICBRAINZ_MIN_REQUEST_INTERVAL_SECONDS = 1.05
MUSICBRAINZ_MAX_503_RETRIES = 3
MUSICBRAINZ_RETRY_BASE_DELAY_SECONDS = 1.0
# A server-sent Retry-After is honored (see _retry_delay_seconds), but never
# past this cap. Real live bug: MusicBrainz rate-limited this Drone and sent
# a large Retry-After; with no cap, _run_bulk_scrape_job's background thread
# slept synchronously for hours -- Stop correctly set stop_requested in
# SQLite, but the thread never returned to the top of its loop to see it,
# and there was no way to start a fresh scrape (any_running() still saw
# "running") short of hand-editing the job row on the live device.
MUSICBRAINZ_MAX_RETRY_DELAY_SECONDS = 60.0

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait_seconds = MUSICBRAINZ_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        _last_request_at = time.monotonic()


class MusicBrainzUnavailableError(RuntimeError):
    """Raised when MusicBrainz can't be reached, rejects the request, or is rate-limiting this Drone."""


class MusicBrainzNotFoundError(MusicBrainzUnavailableError):
    """A specific MusicBrainz id (release/recording) doesn't exist -- unlike
    MusicBrainzUnavailableError's other cases (unreachable, rate-limited),
    which mean every subsequent call will fail the same way, this is scoped
    to one lookup. Subclasses MusicBrainzUnavailableError so any existing
    ``except MusicBrainzUnavailableError`` catch still catches this
    unchanged; only the bulk scrape job's per-(artist,album) loop needs to
    tell the two apart, so a single 404 fails just that one album instead of
    aborting the whole run -- the exact TMDb-incident-shaped bug
    ``tmdb_client.TmdbNotFoundError`` exists to avoid, applied here
    preemptively rather than waited-for."""


class MusicBrainzClient:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def _retry_delay_seconds(self, error: HTTPError, attempt: int) -> float:
        retry_after = error.headers.get("Retry-After") if error.headers else None
        if retry_after:
            try:
                return min(MUSICBRAINZ_MAX_RETRY_DELAY_SECONDS, max(0.5, float(retry_after)))
            except (TypeError, ValueError):
                pass
        return MUSICBRAINZ_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)

    def _get_json(self, path: str, params: Optional[dict] = None) -> dict:
        query = dict(params or {})
        query["fmt"] = "json"
        url = f"{MUSICBRAINZ_API_BASE}{path}?{urlencode(query)}"
        request = Request(url, headers={"User-Agent": MUSICBRAINZ_USER_AGENT, "Accept": "application/json"})
        attempt = 0
        while True:
            _throttle()
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                if error.code == 404:
                    raise MusicBrainzNotFoundError("MusicBrainz has no result for that id") from error
                if error.code in (503, 429) and attempt < MUSICBRAINZ_MAX_503_RETRIES:
                    time.sleep(self._retry_delay_seconds(error, attempt))
                    attempt += 1
                    continue
                if error.code in (503, 429):
                    raise MusicBrainzUnavailableError(
                        "MusicBrainz is rate-limiting this Drone, retries exhausted"
                    ) from error
                raise MusicBrainzUnavailableError(f"MusicBrainz returned HTTP {error.code}") from error
            except (URLError, TimeoutError, OSError) as error:
                raise MusicBrainzUnavailableError("MusicBrainz could not be reached from this Drone") from error

    @staticmethod
    def _artist_credit_name(payload: dict) -> str:
        credits_list = payload.get("artist-credit") if isinstance(payload, dict) else None
        if not isinstance(credits_list, list):
            return ""
        names = []
        for credit in credits_list:
            if isinstance(credit, dict) and credit.get("name"):
                names.append(str(credit["name"]))
        return " ".join(names)

    def search_release(self, query: str, limit: int = 10) -> List[dict]:
        """Release (album) search -- the primary entry point for the bulk
        scraper, which resolves one release per on-disk album rather than
        one recording per track (see the module docstring and
        ``metadata_manager``'s bulk job for why: a release lookup returns
        the whole tracklist in one call, which matters a lot given
        MusicBrainz's stricter throttle than TMDb's)."""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        payload = self._get_json("/release/", {"query": normalized_query})
        releases = payload.get("releases") if isinstance(payload, dict) else None
        if not isinstance(releases, list):
            return []
        output = []
        for item in releases:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            output.append(
                {
                    "release_mbid": item.get("id"),
                    "title": item.get("title") or "",
                    "artist": self._artist_credit_name(item),
                    "date": item.get("date") or None,
                    "track_count": item.get("track-count"),
                }
            )
        return output[: max(1, min(int(limit), 25))]

    def search_recording(self, query: str, limit: int = 10) -> List[dict]:
        """Recording (track) search -- the fallback for a file with no
        resolvable album (a "singles" track with no parent-folder grouping),
        where there's no whole-release tracklist worth fetching."""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        payload = self._get_json("/recording/", {"query": normalized_query})
        recordings = payload.get("recordings") if isinstance(payload, dict) else None
        if not isinstance(recordings, list):
            return []
        output = []
        for item in recordings:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            releases = item.get("releases") if isinstance(item.get("releases"), list) else []
            first_release = releases[0] if releases and isinstance(releases[0], dict) else {}
            output.append(
                {
                    "recording_mbid": item.get("id"),
                    "title": item.get("title") or "",
                    "artist": self._artist_credit_name(item),
                    "release_mbid": first_release.get("id"),
                    "release_title": first_release.get("title") or "",
                }
            )
        return output[: max(1, min(int(limit), 25))]

    def release_lookup(self, release_mbid: str) -> dict:
        """Full release detail including the complete tracklist (every medium
        -- a "Double Album"'s two discs are two media, matching this app's
        own disc_number concept) and genres. One call serves every track on
        the release, which is the whole reason the bulk scraper is grouped
        by (artist, album) rather than iterating per track."""
        safe_id = str(release_mbid or "").strip()
        if not safe_id:
            raise ValueError("release_mbid is required")
        payload = self._get_json(
            f"/release/{safe_id}", {"inc": "recordings+artist-credits+genres+release-groups"}
        )
        genres = [
            genre.get("name") for genre in (payload.get("genres") or [])
            if isinstance(genre, dict) and genre.get("name")
        ]
        tracks = []
        for medium in payload.get("media") or []:
            if not isinstance(medium, dict):
                continue
            disc_number = medium.get("position") or 1
            for track in medium.get("tracks") or []:
                if not isinstance(track, dict):
                    continue
                recording = track.get("recording") if isinstance(track.get("recording"), dict) else {}
                try:
                    track_number = int(track.get("position") or 0) or None
                except (TypeError, ValueError):
                    track_number = None
                tracks.append(
                    {
                        "disc_number": int(disc_number) if str(disc_number).isdigit() else 1,
                        "track_number": track_number,
                        "title": track.get("title") or recording.get("title") or "",
                        "recording_mbid": recording.get("id") or "",
                        "length_ms": track.get("length") or recording.get("length"),
                    }
                )
        release_group = payload.get("release-group") if isinstance(payload.get("release-group"), dict) else {}
        return {
            "release_mbid": payload.get("id"),
            "title": payload.get("title") or "",
            "artist": self._artist_credit_name(payload),
            "date": payload.get("date") or None,
            "genres": genres,
            "release_group_mbid": release_group.get("id") or "",
            "tracks": tracks,
        }
