"""Music metadata/artwork scraper orchestration: MusicBrainz search+match
and Cover Art Archive artwork.

Mirrors ``movies/metadata_manager.py``'s shape closely -- see that file and
the ``drone-movies-feature``/``drone-music-feature`` skills for the shared
design rationale. The one structural difference: there is no API-key
settings state here at all (MusicBrainz + Cover Art Archive are both
keyless), and a scrape applies to a whole **(artist, album)** group rather
than one track -- a release lookup returns an album's whole tracklist in one
call.

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

from pathlib import Path
from typing import Optional

try:
    from ..common.settings import Settings
    from ..storage import music_store as _music_store
    from . import filename_parser as _filename_parser
    from .musicbrainz_client import MusicBrainzClient, MusicBrainzUnavailableError
    from .coverart_client import CoverArtClient, CoverArtUnavailableError
    from . import wikimedia_client as _wikimedia_client
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.settings import Settings  # type: ignore
    from storage import music_store as _music_store  # type: ignore
    from music import filename_parser as _filename_parser  # type: ignore
    from music.musicbrainz_client import MusicBrainzClient, MusicBrainzUnavailableError  # type: ignore
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


def _apply_release_to_group(
    settings: Settings,
    rows: list,
    release: dict,
    *,
    cover_client: Optional[CoverArtClient],
    client: Optional[MusicBrainzClient] = None,
) -> dict:
    """Save one release's art + release-level metadata to every row in
    ``rows`` (an (artist, album) group, or a single-row list for a
    standalone single). Downloads art once and writes it to **one** shared
    file rather than one copy per track (see ``_album_artwork_path``). Also
    attempts an artist photo (``_fetch_artist_art_bytes``) -- best-effort,
    absent for most artists, never fails this call. Deliberately writes ``title=""`` on every row -- see the module
    docstring for why a track's own filename-derived title is never
    overwritten here."""
    music_root = Path(settings.music_root).resolve()
    cover_client = cover_client or _cover_client()
    release_mbid = release.get("release_mbid") or ""

    art_relative_path: Optional[str] = None
    try:
        front_url = cover_client.front_cover_url(release_mbid)
        data = cover_client.download_image(front_url)[0] if front_url else None
    except (CoverArtUnavailableError, ValueError):
        # Missing/unreachable art, or a malformed image URL (download_image's
        # own defensive https-scheme check) -- either way this must never
        # fail the whole apply.
        data = None
    if data and rows:
        target = _album_artwork_path(Path(rows[0]["absolute_path"]).resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        art_relative_path = _relative_to_music_root(music_root, target)

    artist_mbid = release.get("artist_mbid") or ""
    artist_art_relative_path: Optional[str] = None
    if artist_mbid:
        artist_data = _fetch_artist_art_bytes(client or _client(), cover_client, artist_mbid)
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


# ------------------------------------------------------- default-query search

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
