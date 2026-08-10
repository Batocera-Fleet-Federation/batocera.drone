"""Classify a music-library file's Artist/Album grouping and best-effort track
number/title, purely from the path -- no I/O, no MusicBrainz calls, so this
is cheap to unit-test.

Unlike ``movies/filename_parser.py`` (which has to infer a TV show/season
from scene-release filename conventions because that's the only signal
available), a personal music library is overwhelmingly organized by real
folder structure -- ``Artist/Album/Track.ext``, sometimes with a disc-number
subfolder for multi-disc releases. So grouping here is folder-depth-only:
the top-level folder under ``music_root`` is the artist, the next is the
album, and an optional ``CD1``/``Disc 1``-shaped folder directly under the
album is absorbed into that album rather than treated as a grouping level of
its own. This is deliberately simpler than the movies module and does not
read embedded ID3v2/Vorbis/MP4-atom tags -- see the ``drone-music-feature``
skill for why that's an intentional v1 scope cut, not an oversight.

The grouping key this module returns must always win over any scraped
canonical artist/album name for the *same* reason ``movies/filename_parser``'s
``show_title`` does: a partially-scraped album must not visually split into
two cards just because the grouping key lives in two places that can drift
apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

KIND_TRACK = "track"  # only one kind exists today -- kept for symmetry with
# movies/filename_parser.classify()'s kind field, and in case a future kind
# (podcast, audiobook chapter) is ever added without reshaping every caller.

# "CD1", "CD 1", "Disc 1", "Disc.1", "Disk 02" -- digits only, deliberately
# not matching a spelled-out "One"/"Two": a low count of spelled-out discs is
# vanishingly rare in real libraries, and being strict here avoids a false
# positive against a real two-word album title that happens to start with
# "Disc" or "CD" (unlikely, but the digit requirement costs nothing).
_DISC_FOLDER_RE = re.compile(r"^(?:cd|disc|disk)\s*\.?\s*(\d{1,2})$", re.IGNORECASE)

# A release-type "category" folder some libraries insert between Artist and
# the real album folder -- e.g. Artist/Album/<Real Album Name>/track.mp3,
# Artist/Compilation/<Real Release Name>/track.mp3. This is a real,
# widely-used tagging convention (MusicBrainz Picard and similar tools sort
# by release-group type), confirmed live against a real ~2,400-track library
# where every ATB release was collapsed into one fake "ATB / Album" bucket
# and every compilation into one fake "ATB / Compilation" bucket -- tracks
# from genuinely different real albums that happened to share a track title
# then looked like duplicates within that one merged group, and the bulk
# scraper searched MusicBrainz for a release literally titled "Album"/
# "Compilation" and could never find a real match. The vocabulary below
# mirrors MusicBrainz's own release-group primary/secondary type list, since
# that's almost certainly where this folder-naming convention comes from.
_CATEGORY_FOLDER_NAMES = frozenset(
    {
        "album", "albums", "single", "singles", "ep", "eps", "broadcast", "broadcasts",
        "other", "others", "compilation", "compilations", "soundtrack", "soundtracks",
        "spokenword", "interview", "interviews", "audiobook", "audiobooks",
        "audio drama", "live", "live album", "live albums", "remix", "remixes",
        "dj-mix", "dj mix", "mixtape", "mixtapes", "street", "demo", "demos",
        "field recording", "field recordings", "bootleg", "bootlegs",
    }
)

# "01 - Title", "01. Title", "01_Title", "1-05 Title" (disc-track form: the
# leading "1-" is a disc number, "05" is the track). The separator class
# deliberately includes '.', whitespace, '_', and '-' together (so "01 - "
# and "01. " both consume as one run) but the *disc* prefix requires an
# immediate '-' or '.' with no space, so "01 - Title" is never
# misread as disc=01. A title with no leading number at all (the common case
# for a file that's already just "Song Title.mp3") simply doesn't match --
# not an error, see parse_track_filename's fallback.
_TRACK_RE = re.compile(r"^(?:(?P<disc>\d{1,2})[-.])?(?P<track>\d{1,3})[.\s_-]+(?P<title>.+)$")

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse(value: str) -> str:
    cleaned = value.replace("_", " ").replace(".", " ")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


@dataclass(frozen=True)
class ParsedLocation:
    kind: str = KIND_TRACK
    artist: str = ""
    album: str = ""
    disc_number: Optional[int] = None


@dataclass(frozen=True)
class ParsedTrackName:
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    title: str = ""


def classify_location(file_path: str) -> ParsedLocation:
    """Artist/album/disc purely from folder depth under ``music_root``:

    * ``Artist/Song.ext``                       -> artist only (a "singles" bucket)
    * ``Artist/Album/Song.ext``                  -> the expected, primary case
    * ``Artist/Category/Album/Song.ext``         -> a release-type category folder
      (Album/Compilation/EP/Live/...) is transparently skipped; ``Album`` here
      is the real album name, not the category label -- see
      ``_CATEGORY_FOLDER_NAMES``.
    * ``Artist/Album/CD1/Song.ext``               -> disc folder absorbed into Album
    * ``Artist/Category/Album/CD1/Song.ext``      -> both of the above together
    * ``Artist/CD1/Song.ext``                     -> rare flat-disc layout, album=""
    * ``Song.ext`` (no folder at all)             -> ungrouped orphan (artist="")

    Never raises; an unrecognized/empty path just yields an all-empty
    ``ParsedLocation()``, same "leave it ungrouped rather than guess"
    contract as ``movies.filename_parser``'s directory-derived show/season.
    """
    segments = [segment.strip() for segment in Path(str(file_path or "")).parts[:-1] if segment.strip()]
    if not segments:
        return ParsedLocation()
    artist = segments[0]
    if len(segments) == 1:
        return ParsedLocation(artist=artist)
    second = segments[1]
    disc_only_match = _DISC_FOLDER_RE.match(second)
    if disc_only_match and len(segments) == 2:
        return ParsedLocation(artist=artist, album="", disc_number=int(disc_only_match.group(1)))
    # A category folder only counts as a wrapper when there's a real album
    # segment beneath it to skip to -- Artist/Album/track.ext (no third
    # segment) keeps "Album" as a literal (if unfortunate) album name rather
    # than being swallowed with nothing left to replace it. Also declines the
    # wrapper reading when the very next segment looks like a disc folder
    # (Artist/Album/CD1/track.ext, a real album that happens to be named
    # "Album") -- there'd be nothing sensible left to use as the album name.
    album_index = 1
    if (
        second.lower() in _CATEGORY_FOLDER_NAMES
        and len(segments) >= 3
        and not _DISC_FOLDER_RE.match(segments[2])
    ):
        album_index = 2
    album = segments[album_index]
    disc_number = None
    if len(segments) > album_index + 1:
        last_disc_match = _DISC_FOLDER_RE.match(segments[-1])
        if last_disc_match:
            disc_number = int(last_disc_match.group(1))
    return ParsedLocation(artist=artist, album=album, disc_number=disc_number)


def parse_track_filename(file_name: str) -> ParsedTrackName:
    """Best-effort (disc_number, track_number, title) from a filename stem.

    Purely cosmetic/query-quality -- unlike ``movies.filename_parser.classify``,
    a track with no parseable leading number is still a perfectly valid,
    fully groupable song; it just sorts by filename and searches by its
    whole (cleaned) stem instead of a cleaner extracted title.
    """
    stem = Path(str(file_name or "")).stem.strip()
    match = _TRACK_RE.match(stem)
    if not match:
        return ParsedTrackName(title=_collapse(stem))
    disc_value = match.group("disc")
    return ParsedTrackName(
        disc_number=int(disc_value) if disc_value is not None else None,
        track_number=int(match.group("track")),
        title=_collapse(match.group("title")),
    )


# A real album folder name is often "YYYY - Title (Catalog/Edition Info)" --
# e.g. "2011 - Distant Earth (Deluxe Fanbox) (1061391KON)" -- great for a
# human browsing a file tree, but the year prefix and catalog/edition
# parentheticals actively hurt a literal MusicBrainz release-title search.
# Mirrors movies.filename_parser's scene-tag stripping: clean up before
# searching, but keep the raw folder name as a fallback rung in case the
# cleanup was too aggressive for an unusual name.
_LEADING_YEAR_RE = re.compile(r"^(?:19|20)\d{2}\s*[-:]?\s*")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _clean_album_name(album: str) -> str:
    cleaned = _LEADING_YEAR_RE.sub("", album.strip())
    while True:
        stripped = _TRAILING_PAREN_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    return cleaned.strip() or album.strip()


def search_candidates(artist: str, album: str, track_title: str) -> List[Tuple[str, str]]:
    """Ordered ``(query_type, query_string)`` candidates a caller tries in
    turn, most-specific first -- mirrors ``movies.filename_parser
    .search_candidates``'s "ordered ladder, try each, stop at first hit"
    contract. ``query_type`` is ``"release"`` (artist+album -- what the
    bulk-scrape job uses, since a release lookup returns a whole tracklist in
    one call) or ``"recording"`` (a single-track fallback, for files with no
    resolvable album -- the "singles"/orphan case). The cleaned album name
    (see ``_clean_album_name``) is tried before the raw folder name.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    track_title = (track_title or "").strip()
    candidates: List[Tuple[str, str]] = []
    if artist and album:
        cleaned_album = _clean_album_name(album)
        candidates.append(("release", f"{artist} {cleaned_album}"))
        if cleaned_album != album:
            candidates.append(("release", f"{artist} {album}"))
        candidates.append(("release", cleaned_album))
    elif album:
        candidates.append(("release", _clean_album_name(album)))
    if artist and track_title:
        candidates.append(("recording", f"{artist} {track_title}"))
    if track_title:
        candidates.append(("recording", track_title))
    return candidates
