"""Wikimedia Commons client: resolves a Commons file-page URL (as returned
by a MusicBrainz artist "image" relationship) to an actual downloadable
image URL, no API key required.

MusicBrainz itself hosts no images at all -- an artist's "image" relation
(see ``musicbrainz_client.MusicBrainzClient.artist_lookup``; most artists
have none) points at a Commons *file page*
(e.g. ``https://commons.wikimedia.org/wiki/File:Some_Artist.jpg``), not a
raw image. This client turns that into the real ``upload.wikimedia.org``
URL via Wikimedia's own public MediaWiki Action API -- keyless and free,
same "no third-party account" fit as MusicBrainz/Cover Art Archive.
Downloading the resolved URL's bytes reuses
``coverart_client.CoverArtClient.download_image`` (same https-only,
image/*-content-type, size-capped logic) rather than a second copy of it.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from .musicbrainz_client import MUSICBRAINZ_USER_AGENT as _USER_AGENT
except ImportError:  # pragma: no cover - direct script execution fallback
    from musicbrainz_client import MUSICBRAINZ_USER_AGENT as _USER_AGENT  # type: ignore

WIKIMEDIA_COMMONS_API_BASE = "https://commons.wikimedia.org/w/api.php"


def _commons_file_title(commons_page_url: str) -> Optional[str]:
    """Extract ``File:Whatever.jpg`` from a Commons file-page URL. Only the
    plain ``/wiki/File:X.jpg`` path form is recognized (what MusicBrainz
    relations use in practice) -- anything else, or a URL that isn't even
    on a wikimedia.org host, returns ``None`` rather than guessing."""
    parsed = urlparse(str(commons_page_url or ""))
    if not parsed.netloc.endswith("wikimedia.org"):
        return None
    path = parsed.path
    if "/wiki/" not in path:
        return None
    title = path.split("/wiki/", 1)[1]
    return title if title.startswith("File:") else None


def resolve_image_url(commons_page_url: str, *, timeout_seconds: int = 15) -> Optional[str]:
    """The direct, downloadable image URL for a Commons file page, or
    ``None`` if the page URL isn't a recognizable Commons file link or the
    lookup fails for any reason -- always best-effort, never raises, since
    an artist photo is optional enrichment and must never fail the
    album-art apply/bulk job it rides along with (mirrors the existing
    Cover Art Archive fetch's own never-fail-the-caller convention)."""
    title = _commons_file_title(commons_page_url)
    if not title:
        return None
    query = f"action=query&titles={quote(title)}&prop=imageinfo&iiprop=url&format=json"
    url = f"{WIKIMEDIA_COMMONS_API_BASE}?{query}"
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    pages = (payload.get("query") or {}).get("pages") or {}
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        image_info = page.get("imageinfo")
        if isinstance(image_info, list) and image_info and isinstance(image_info[0], dict) and image_info[0].get("url"):
            return str(image_info[0]["url"])
    return None
