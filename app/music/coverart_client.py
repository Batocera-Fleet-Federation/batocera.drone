"""Cover Art Archive client: front-cover lookup + image download, no API key
required.

Companion to ``musicbrainz_client.py`` -- Cover Art Archive (run by the
Internet Archive) keys its images by MusicBrainz release MBID, so this is
only ever called with a release id already resolved via
``MusicBrainzClient.release_lookup``/``search_release``. Same
"stdlib urllib client class" shape as ``movies/tmdb_client.py``'s image
download, but simpler: there's no separate poster/backdrop distinction, just
one front-cover image per release.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COVER_ART_ARCHIVE_BASE = "https://coverartarchive.org"
# Reuses the same identifying User-Agent as the MusicBrainz client -- Cover
# Art Archive is operated by the same MetaBrainz project and expects the
# same etiquette.
try:
    from .musicbrainz_client import MUSICBRAINZ_USER_AGENT as _USER_AGENT
except ImportError:  # pragma: no cover - direct script execution fallback
    from musicbrainz_client import MUSICBRAINZ_USER_AGENT as _USER_AGENT  # type: ignore


class CoverArtUnavailableError(RuntimeError):
    """Raised when Cover Art Archive can't be reached or rejects the request."""


class CoverArtClient:
    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def front_cover_url(self, release_mbid: str) -> Optional[str]:
        """The full-size front-cover image URL for a release, or ``None`` if
        this release has no cover art on file at all -- checked via the
        JSON image list first (rather than requesting the image directly and
        treating a 404 as "no art") so a release with genuinely no art on
        file costs one small JSON fetch instead of a guaranteed-404 image
        request."""
        safe_id = str(release_mbid or "").strip()
        if not safe_id:
            return None
        url = f"{COVER_ART_ARCHIVE_BASE}/release/{safe_id}"
        request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                return None
            raise CoverArtUnavailableError(f"Cover Art Archive returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise CoverArtUnavailableError("Cover Art Archive could not be reached from this Drone") from error
        images = payload.get("images") if isinstance(payload, dict) else None
        if not isinstance(images, list):
            return None
        for image in images:
            if isinstance(image, dict) and image.get("front") and image.get("image"):
                return str(image["image"])
        return None

    def download_image(self, url: str) -> Tuple[bytes, str]:
        parsed_scheme_ok = str(url or "").startswith("https://")
        if not parsed_scheme_ok:
            raise ValueError("image_url must be an https URL")
        request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "image/*"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                content_type = response.headers.get_content_type()
                if not str(content_type or "").startswith("image/"):
                    raise ValueError("image_url did not return an image")
                max_bytes = 20 * 1024 * 1024
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("image is too large")
                return data, content_type
        except HTTPError as error:
            raise CoverArtUnavailableError(f"Cover Art Archive image fetch returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise CoverArtUnavailableError("Cover Art Archive image could not be reached from this Drone") from error
