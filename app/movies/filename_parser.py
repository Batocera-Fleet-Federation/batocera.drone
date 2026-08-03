"""Classify a movies-library file (movie / TV episode / bonus extra) and turn
its filename into a ladder of TMDb search candidates, purely from the path --
no I/O, no TMDb calls, so this is cheap to unit-test against a large corpus of
real-world scene-release names.

Real movie libraries (this module was written against a 257-file real one)
are dominated by two release-naming styles: dot-separated scene/torrent names
("28.Days.Later.2002.1080p.BluRay.DDP5.1.x265.10bit-GalaxyRG265.mkv") and
plain names with a quality tag in parens ("Ant-Man (1080p).mkv"). TV
libraries organized the Sonarr/TRaSH way name episodes
"Show (Year) - SxxEyy - Episode Title (quality info)" and put bonus content
(cast interviews, behind-the-scenes, deleted scenes) in Plex/Kodi/Jellyfin's
standard "local extras" subfolders alongside the real episodes -- both are
strong, reliable signals worth matching on directly rather than trying to
infer them from content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

KIND_MOVIE = "movie"
KIND_EPISODE = "episode"
KIND_EXTRA = "extra"

# Plex/Kodi/Jellyfin's standard "local extras" folder names -- content in one
# of these is bonus material (interviews, behind-the-scenes, deleted scenes),
# never a movie or episode in its own right, so it should never burn a TMDb
# search call. Matched case-insensitively against any path segment.
EXTRAS_FOLDER_NAMES = frozenset(
    {
        "featurettes", "behind the scenes", "deleted scenes", "interviews",
        "scenes", "shorts", "trailers", "other",
    }
)

# "Show (2006) - S01E04 - Episode Title (1080p BluRay x265 Silence).mkv" --
# the TRaSH/Sonarr convention. Year is optional (some libraries omit it).
# The tail after the episode number is deliberately unconstrained (``.*$``,
# not e.g. requiring a "- Title" separator): real libraries also use
# "Show S01E04 [1080p][x265][group].mkv" -- tags jammed on with no separator
# at all -- and an earlier, stricter version of this regex simply failed to
# match those (falling through to KIND_MOVIE, which then got searched
# against TMDb's *movie* endpoint and never matched). The tail is cleaned up
# separately below into ``episode_title``, which is cosmetic only -- TMDb
# search uses ``show``/``year`` alone, so a messy or empty tail here never
# hurts match quality, only the display string.
_EPISODE_RE = re.compile(
    r"^(?P<show>.+?)\s*(?:\(?(?P<year>(?:19|20)\d{2})\)?)?\s*[-_. ]+"
    r"[Ss](?P<season>\d{1,2})[.\s_-]?[Ee](?P<episode>\d{1,3})"
    r"(?P<tail>.*)$"
)
# "Show.2006.1x04.Episode.Title.mkv" -- the older "1x04" style.
_ALT_EPISODE_RE = re.compile(
    r"^(?P<show>.+?)\s*(?:\(?(?P<year>(?:19|20)\d{2})\)?)?\s*[-_. ]+"
    r"(?P<season>\d{1,2})x(?P<episode>\d{2,3})"
    r"(?P<tail>.*)$"
)

# Scene-release tooling substitutes filesystem-illegal characters for
# lookalikes (seen in the wild: "Face⁄Off" for "Face/Off") -- put back the
# real character before any other cleaning so it doesn't read as junk.
_UNICODE_SUBSTITUTES = {
    "⁄": "/",  # fraction slash standing in for "/"
    "’": "'", "‘": "'",  # curly quotes
    "“": '"', "”": '"',
}

# Resolution/source/codec/audio/edition/release-language vocabulary that
# shows up between the title and the release group but carries no title
# information -- stripped only in the aggressive fallback candidate, since
# TMDb's fuzzy search usually shrugs this noise off on its own.
_SCENE_TOKENS_RE = re.compile(
    r"\b("
    r"2160p|1080p|720p|480p|4k|uhd|bluray|brrip|bdrip|dvdrip|webrip|web-?dl|"
    r"hdtv|hdrip|dvdscr|camrip|"
    r"x264|x265|h264|h265|hevc|avc|"
    r"aac(?:5\.?1)?|dd5\.?1|ddp5\.?1|ddp|dts(?:-?hd)?|"
    r"10bit|8bit|hdr10?|dovi|dolby(?:\.?vision)?|"
    r"proper|repack|remastered|extended|unrated|theatrical|directors\.?cut|imax|"
    r"nordic|multi|dual\.?audio|"
    r"yify|yts(?:\.\w+)?"
    r")\b",
    re.IGNORECASE,
)
# A trailing "-GROUPNAME" release-group tag: single token (no spaces), so
# this can't accidentally eat a legitimately hyphenated title like
# "Ant-Man" or "Re-Animator" (those are always followed by more words, i.e.
# a space, before the string ends).
_TRAILING_GROUP_RE = re.compile(r"-[A-Za-z0-9.]{2,20}$")
_BRACKETED_RE = re.compile(r"[\[\(][^\[\]()]*[\])]")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_PUNCTUATION_RE = re.compile(r"[.\-_,;:\[\]()<>/]+")


@dataclass(frozen=True)
class ParsedEntry:
    kind: str  # KIND_MOVIE | KIND_EPISODE | KIND_EXTRA
    show_title: str = ""
    year: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_title: str = ""


def _normalize_unicode(text: str) -> str:
    for src, dst in _UNICODE_SUBSTITUTES.items():
        text = text.replace(src, dst)
    return text


def _collapse(text: str) -> str:
    text = _PUNCTUATION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify(file_path: str, file_name: str) -> ParsedEntry:
    """Decide whether ``file_name`` (at ``file_path`` within movies_root) is a
    movie, a TV episode, or bonus/extra content -- in that priority order:
    the extras-folder check runs first since a stray episode-shaped name
    inside a "Featurettes" folder (unlikely, but possible) is still extra
    content, not something to search TMDb for."""
    path_segments = [segment.lower() for segment in Path(file_path).parts[:-1]]
    if any(segment in EXTRAS_FOLDER_NAMES for segment in path_segments):
        return ParsedEntry(kind=KIND_EXTRA)

    stem = _normalize_unicode(Path(file_name).stem)
    match = _EPISODE_RE.match(stem) or _ALT_EPISODE_RE.match(stem)
    if not match:
        return ParsedEntry(kind=KIND_MOVIE)

    fields = match.groupdict()
    tail = _BRACKETED_RE.sub("", fields.get("tail") or "")
    tail = _SCENE_TOKENS_RE.sub("", tail)
    episode_title = tail.strip(" -_.:")
    return ParsedEntry(
        kind=KIND_EPISODE,
        show_title=_collapse(fields["show"]),
        year=fields.get("year"),
        season=int(fields["season"]),
        episode=int(fields["episode"]),
        episode_title=episode_title,
    )


def search_candidates(stem: str) -> List[Tuple[str, Optional[str]]]:
    """Build an ordered ladder of ``(title, year)`` search candidates from a
    filename stem (extension already stripped) or a parsed show title --
    most-precise first. Callers try each in turn and stop at the first one
    that gets a TMDb hit.

    The single most valuable move is candidate #1: truncate the title at its
    year token and pass the year as a TMDb filter. Scene-release convention
    always places the year immediately after the title, so this one cut
    reliably drops every trailing quality/codec/group tag *and* disambiguates
    remakes/reboots that share a title (this library alone has a dozen
    different "Halloween" movies spanning 1978-2022). Candidate #2 repeats
    the same title without the year filter, in case the filename's year is
    wrong (a re-release date, a typo'd release-group tag). The rest are
    fallbacks for names with no year at all (the "Ant-Man (1080p).mkv" style)
    or where TMDb's fuzzy search still needs the noise stripped by hand.
    """
    stem = _normalize_unicode(stem)
    candidates: List[Tuple[str, Optional[str]]] = []

    def _add(title: str, year: Optional[str]) -> None:
        if title and (title, year) not in candidates:
            candidates.append((title, year))

    year_match = _YEAR_RE.search(stem)
    if year_match:
        title = _collapse(stem[: year_match.start()])
        _add(title, year_match.group(1))
        _add(title, None)

    tag_stripped = _BRACKETED_RE.sub(" ", stem)
    tag_stripped = _SCENE_TOKENS_RE.sub(" ", tag_stripped)
    tag_stripped = _TRAILING_GROUP_RE.sub("", tag_stripped)
    _add(_collapse(tag_stripped), None)

    _add(_collapse(stem), None)
    return candidates
