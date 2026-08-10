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

    * ``Artist/Song.ext``                    -> artist only (a "singles" bucket)
    * ``Artist/Album/Song.ext``               -> the expected, primary case
    * ``Artist/Album/CD1/Song.ext``           -> disc folder absorbed into Album
    * ``Artist/CD1/Song.ext``                 -> rare flat-disc layout, album=""
    * ``Song.ext`` (no folder at all)         -> ungrouped orphan (artist="")

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
    album = second
    disc_number = None
    if len(segments) >= 3:
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


def search_candidates(artist: str, album: str, track_title: str) -> List[Tuple[str, str]]:
    """Ordered ``(query_type, query_string)`` candidates a caller tries in
    turn, most-specific first -- mirrors ``movies.filename_parser
    .search_candidates``'s "ordered ladder, try each, stop at first hit"
    contract. ``query_type`` is ``"release"`` (artist+album -- what the
    bulk-scrape job uses, since a release lookup returns a whole tracklist in
    one call) or ``"recording"`` (a single-track fallback, for files with no
    resolvable album -- the "singles"/orphan case).
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    track_title = (track_title or "").strip()
    candidates: List[Tuple[str, str]] = []
    if artist and album:
        candidates.append(("release", f"{artist} {album}"))
        candidates.append(("release", album))
    elif album:
        candidates.append(("release", album))
    if artist and track_title:
        candidates.append(("recording", f"{artist} {track_title}"))
    if track_title:
        candidates.append(("recording", track_title))
    return candidates
