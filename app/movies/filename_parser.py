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
        "scenes", "shorts", "trailers", "other", "hidden extras", "extras",
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
# A "(Year)" anywhere in a *folder* name -- deliberately not anchored to the
# end, since a well-organized folder can still carry trailing quality tags
# ("Alien Resurrection (1997) [1080p] [BluRay]"), but unlike a filename's
# year token this one must be parenthesized: a bare year is common enough in
# an unrelated folder name (e.g. a release-group or torrent-site folder) that
# treating it the same as a real "Title (Year)" folder would too often be
# wrong.
_FOLDER_YEAR_RE = re.compile(r"\((?P<year>(?:19|20)\d{2})\)")
# A season folder that carries the show name in the same segment, e.g. this
# app's own "Shows/<Show (Year)>/<Show (Year)> SXX/" layout
# ("Lost (2004) S02"). No "E<nn>" suffix allowed -- a folder never
# legitimately carries an episode number too, only a filename does.
_SEASON_FOLDER_WITH_SHOW_RE = re.compile(r"^(?P<show>.+?)\s+S(?P<season>\d{1,2})$", re.IGNORECASE)
# A bare "Season NN" folder (Plex/Kodi/Jellyfin convention) -- carries no
# show name of its own; the show has to come from this folder's own parent.
_BARE_SEASON_FOLDER_RE = re.compile(r"^season\s+(?P<season>\d{1,3})$", re.IGNORECASE)
# Last-resort season-folder fallback for a real-world "everything dumped in
# one folder" release/reorg name that doesn't cleanly end in " SNN" or match
# bare "Season NN" exactly -- e.g. "WandaVision (2021) Season 1 S01 (1080p
# BluRay x265 HEVC 10bit EAC3 5.1 Silence)" or
# "Secret.Invasion.S01.COMPLETE.1080p.DSNP.WEB-DL.DDP5.1.H.264-NTb[TGx]".
# Takes the *first* season-shaped token (spelled-out "Season NN" or a bare
# "SNN" abbreviation), word-boundaried so this can't fire on an actual
# "S01E04" episode marker (never legitimate in a folder name) or match
# mid-word. Tried only after the two stricter patterns above fail.
_SEASON_TOKEN_RE = re.compile(r"\bseason\s+(?P<season_word>\d{1,3})\b|\bs(?P<season_abbr>\d{1,3})\b(?!e\d)", re.IGNORECASE)


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


def folder_title_candidate(folder_name: str) -> Optional[Tuple[str, str]]:
    """``(title, year)`` from a "Title (Year)" *folder* name (the common
    Radarr/Filebot/Plex movie-folder convention -- and, in this app's own
    library, the same shape a movies-download folder or a "Shows/<Show
    (Year)>/" tree already uses), or ``None`` if the folder doesn't carry a
    parenthesized year at all.

    A folder someone (or something) organized things into is often a
    cleaner, more trustworthy title than the release filename inside it --
    especially once a human has renamed the folder by hand, or for a show
    folder whose *episode* filenames are the messy part. Deliberately
    conservative: requires the year to be parenthesized (unlike a filename's
    year token, a bare year in a folder name is too often unrelated -- a
    release-group or site-branding folder, not the title) and, unlike the
    filename year-cut, doesn't also try the un-stripped variant -- a folder
    name is far less likely to carry genuine title text past the tag-strip
    than a full release filename is."""
    match = _FOLDER_YEAR_RE.search(folder_name or "")
    if not match:
        return None
    raw_title_part = folder_name[: match.start()]
    title = _collapse(_SCENE_TOKENS_RE.sub(" ", _BRACKETED_RE.sub(" ", raw_title_part)))
    if not title:
        return None
    return title, match.group("year")


def _season_and_show_from_combined_folder(segment: str) -> Tuple[str, Optional[int]]:
    """Last-resort single-segment fallback -- see ``_SEASON_TOKEN_RE``.
    Everything before the first season-shaped token is the show candidate,
    run through the same year-aware folder cleanup ``folder_title_candidate``
    already does for a proper "Title (Year)" folder, falling back to plain
    tag-stripped collapse when there's no parenthesized year to anchor on."""
    match = _SEASON_TOKEN_RE.search(segment)
    if not match:
        return "", None
    season = int(match.group("season_word") or match.group("season_abbr"))
    raw_show = segment[: match.start()]
    folder_candidate = folder_title_candidate(raw_show)
    show_title = folder_candidate[0] if folder_candidate else _collapse(_SCENE_TOKENS_RE.sub(" ", _BRACKETED_RE.sub(" ", raw_show)))
    return (show_title, season) if show_title else ("", None)


def _extra_show_season_from_path(file_path: str) -> Tuple[str, Optional[int]]:
    """Best-effort ``(show_title, season)`` for bonus/extra content, inferred
    from the *directory structure* rather than the file's own name -- a
    Featurette clip's filename ("01. Adrift.mkv") almost never carries a
    show or season indicator the way a real episode's does, but the folder
    tree around it usually does: this app's own "Shows/<Show (Year)>/<Show
    (Year)> SXX/" layout, the Plex/Kodi/Jellyfin bare "Season NN" folder
    convention (show name one level further up), or -- last resort -- a
    messier combined folder that buries a season token in the middle of a
    longer release/reorg name (see ``_season_and_show_from_combined_folder``).

    Searches from the segment closest to the file outward to the root,
    stopping at the first match, so a deeper, unrelated "... S3" style
    sub-featurette folder can't be mistaken for the real season folder
    above it. Returns ``("", None)`` if nothing matches anywhere in the
    path -- callers should leave the entry ungrouped rather than guess."""
    segments = Path(file_path).parts[:-1]
    for index in range(len(segments) - 1, -1, -1):
        segment = segments[index]
        with_show = _SEASON_FOLDER_WITH_SHOW_RE.match(segment)
        if with_show:
            folder_candidate = folder_title_candidate(with_show.group("show"))
            show_title = folder_candidate[0] if folder_candidate else _collapse(with_show.group("show"))
            if show_title:
                return show_title, int(with_show.group("season"))
            continue
        bare = _BARE_SEASON_FOLDER_RE.match(segment)
        if bare and index > 0:
            parent = segments[index - 1]
            folder_candidate = folder_title_candidate(parent)
            show_title = folder_candidate[0] if folder_candidate else _collapse(parent)
            if show_title:
                return show_title, int(bare.group("season"))
            continue
        combined_show, combined_season = _season_and_show_from_combined_folder(segment)
        if combined_show:
            return combined_show, combined_season
    return "", None


def classify(file_path: str, file_name: str) -> ParsedEntry:
    """Decide whether ``file_name`` (at ``file_path`` within movies_root) is a
    movie, a TV episode, or bonus/extra content -- in that priority order:
    the extras-folder check runs first since a stray episode-shaped name
    inside a "Featurettes" folder (unlikely, but possible) is still extra
    content, not something to search TMDb for.

    Extra content also gets a best-effort ``show_title``/``season`` from the
    directory structure (see ``_extra_show_season_from_path``) so the Movies
    UI can list it under the right show/season instead of leaving it as an
    orphan card with no artwork and no context -- ``episode``/
    ``episode_title`` stay empty, since a Featurette isn't a numbered
    episode."""
    path_segments = [segment.lower() for segment in Path(file_path).parts[:-1]]
    if any(segment in EXTRAS_FOLDER_NAMES for segment in path_segments):
        show_title, season = _extra_show_season_from_path(file_path)
        return ParsedEntry(kind=KIND_EXTRA, show_title=show_title, season=season)

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


def search_candidates(stem: str, *, folder_name: Optional[str] = None) -> List[Tuple[str, Optional[str]]]:
    """Build an ordered ladder of ``(title, year)`` search candidates from a
    filename stem (extension already stripped) or a parsed show title --
    most-precise first. Callers try each in turn and stop at the first one
    that gets a TMDb hit.

    ``folder_name`` (optional -- the file's immediate parent directory, if
    any) is tried *first* when it carries a "Title (Year)" pattern
    (``folder_title_candidate``): a folder someone organized things into is
    often cleaner and more trustworthy than the release filename inside it.
    Every rung below this is filename-only, unaffected by whether a folder
    candidate was found.

    The single most valuable filename-based move is the next candidate:
    truncate the title at its year token and pass the year as a TMDb filter.
    Scene-release convention always places the year immediately after the
    title, so this one cut reliably drops every trailing quality/codec/group
    tag *and* disambiguates remakes/reboots that share a title (this library
    alone has a dozen different "Halloween" movies spanning 1978-2022). The
    next rung repeats the same title without the year filter, in case the
    filename's year is wrong (a re-release date, a typo'd release-group
    tag). The rest are fallbacks for names with no year at all (the
    "Ant-Man (1080p).mkv" style) or where TMDb's fuzzy search still needs the
    noise stripped by hand.

    The year-cut candidate also gets the *same* scene-token stripping as the
    tag-stripped fallback below, not just a punctuation collapse -- some
    releases put an edition tag *before* the year instead of after
    ("Alien.Resurrection.Directors.Cut.1997...", rather than the more common
    "Movie.1997.Directors.Cut..."), and TMDb's own title is never "Alien
    Resurrection Directors Cut", so leaving that text in reliably broke the
    match. The un-stripped year-cut title is still added as a further
    fallback rung afterward, in case the vocabulary ever over-matches part of
    a legitimate title.
    """
    stem = _normalize_unicode(stem)
    candidates: List[Tuple[str, Optional[str]]] = []

    def _add(title: str, year: Optional[str]) -> None:
        if title and (title, year) not in candidates:
            candidates.append((title, year))

    folder_candidate = folder_title_candidate(folder_name) if folder_name else None
    if folder_candidate:
        folder_title, folder_year = folder_candidate
        _add(folder_title, folder_year)
        _add(folder_title, None)

    year_match = _YEAR_RE.search(stem)
    if year_match:
        raw_title_part = stem[: year_match.start()]
        cleaned_title_part = _SCENE_TOKENS_RE.sub(" ", _BRACKETED_RE.sub(" ", raw_title_part))
        title = _collapse(cleaned_title_part)
        _add(title, year_match.group(1))
        _add(title, None)
        raw_title = _collapse(raw_title_part)
        if raw_title != title:
            _add(raw_title, year_match.group(1))
            _add(raw_title, None)

    tag_stripped = _BRACKETED_RE.sub(" ", stem)
    tag_stripped = _SCENE_TOKENS_RE.sub(" ", tag_stripped)
    tag_stripped = _TRAILING_GROUP_RE.sub("", tag_stripped)
    _add(_collapse(tag_stripped), None)

    _add(_collapse(stem), None)
    return candidates
