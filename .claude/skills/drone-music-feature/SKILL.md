---
name: drone-music-feature
description: Use this when designing, reviewing, or implementing the Drone Music feature — a planned new asset type (parallel to Games/Movies) scanning /userdata/music, with artist/album/song grouping, MusicBrainz+Cover Art Archive scraping, genres, and in-browser audio playback. Also use when working on the Games/Movies-tab-to-Assets-tab navbar consolidation, since that lands as part of this feature.
---

# Drone Music Feature Skill

## Status: shipped (as of 2026-08-10), extended since — this file is historical design rationale

The feature described below **is implemented and live** — Phases 0-3 all
landed (Phase 4 P2P/TheAudioDB is still a real future follow-up). Most of
this file is still an accurate account of *why* things are shaped the way
they are, but it was written as a pre-implementation plan and has **not**
been fully rewritten in shipped-past-tense — treat every file
path/schema/route below as "this is what was planned and is, as far as
anyone has gone back to verify, still true," not as a live source of truth.
**Prefer the real code** (`app/music/`, `app/storage/music_store.py`,
`app/web/handlers_music.py`, the `Music*` sections of `drone.js`) over this
file the moment they disagree, and fix this file when you notice drift
rather than leaving it stale for the next session. See "Shipped deviations /
additions beyond this plan" below for real post-launch changes already known
to exist.

## Goal / origin

The user asked for a Music asset type sourced from `/userdata/music`, with:
songs grouped under Artist (mirroring how Movies groups episodes under a
Show), an Artist's detail page grouping songs under "Albums" (mirroring
Show→Season), artwork/metadata scraping, genres, playback, and a UI change
consolidating the navbar's separate Games/Movies links into one "Assets"
entry with Games/Movies/Music switcher buttons on the search row (not
separate nav tabs). Movies is the direct architectural template for nearly
everything except playback UX (continuous/persistent, not one-video-modal)
and the scraper (keyless, unlike TMDb).

## Design decisions (the three things Movies doesn't answer by itself)

1. **Scraper: MusicBrainz + Cover Art Archive, keyless, TheAudioDB deferred.**
   MusicBrainz (`https://musicbrainz.org/ws/2/`) for canonical artist/album/
   track/genre metadata, Cover Art Archive (`https://coverartarchive.org/`,
   keyed by MusicBrainz release MBID) for album art. Both keyless — **no
   `/admin/music/scraper-settings` route, no API-key entry form, no
   `music_scraper.json` settings namespace** — genuinely simpler than Movies
   here. MusicBrainz requires a fixed, hardcoded, non-configurable
   `User-Agent` per their API etiquette policy (not a secret, just identifies
   the app), and a **proactive** ~1 req/sec self-throttle (unlike TMDb's
   reactive 429-retry) plus 503 retry/backoff mirroring `TmdbClient`'s 429
   handling. TheAudioDB (artist photos) is an explicit follow-up, not v1 —
   its free tier is a shared community test key not meant for
   shipped-feature-default production traffic; if picked up later it should
   get a real user-supplied key, following the TMDb pattern, not the shared
   key.
   - **Bulk scrape must be grouped by (artist, album), not iterated per
     track** — a release lookup (`inc=recordings+artist-credits+tags`)
     returns the whole tracklist in one call. This is load-bearing given
     MusicBrainz's stricter throttle: per-track would make a 2,000-song
     library take 30+ minutes minimum; per-album keeps it to a few minutes
     for a typical library. Cache the resolved release MBID per (artist,
     album) within one bulk-job run, exactly like Movies caches
     `show_id`/`show_details` per show title within a run.

2. **Grouping source: folder structure, primary and required; embedded tags
   (ID3v2/Vorbis/MP4 atoms) are an optional future enrichment, not v1.**
   Top-level folder under `MUSIC_ROOT` = artist, next folder = album, an
   optional `CD1`/`Disc 1`-shaped folder directly under an album is absorbed
   into that album (not a third grouping level) rather than treated as its
   own thing. A file with no album folder (`Artist/Song.mp3`) still groups
   under its artist with an empty album ("singles" bucket); a file with no
   artist folder at all (bare `Song.mp3` at `MUSIC_ROOT`) is an ungrouped
   orphan, same shape as an ungroupable movie extra. **The grouping key is
   always the folder-derived artist/album, never a scraped canonical name**
   — same non-negotiable rule as Movies' show_title, and for the identical
   reason (a partially-scraped album must not visually split into two
   cards).

3. **Phasing — land in this order:**
   - **Phase 0** — navbar consolidation alone (`index.html`, a new
     `renderAssetTypeSwitcher()` in `drone.js`, minimal CSS). Low-risk,
     independent, immediately visible. Recommended to bundle with Phase 1
     rather than ship standalone, since the Music button in the switcher is
     a dead link until a Music page exists.
   - **Phase 1** — storage + scanning + settings + minimal flat-list
     playback (no grouping UI yet). Gets `<audio>` Range-streaming working
     end to end, verifiable by `curl` alone before any frontend grouping
     work starts.
   - **Phase 2** — Artist/Album grouping UI (mirrors Show/Season) + a
     **persistent bottom player bar** (`#musicPlayerBar`, mounted once at
     app-shell level so it survives router navigation — the one genuinely
     new UI pattern here, since music listening is continuous-across-
     browsing unlike Movies' one-video-at-a-time modal). Simple next/prev
     within the currently-open album's queue; no repeat in v1 (shuffle
     added later, see "Shipped deviations" below).
   - **Phase 3** — scraper client + admin bulk-scrape job + genres.
   - **Phase 4 (explicit follow-up, out of scope for the initial build)** —
     P2P peer-sync route parity (`/peer/music/...`, mirroring the `movies`
     block in `handlers_peer.py`), `music_duplicates.py`, TheAudioDB artist
     photos with a real key.

## Planned schema — `app/storage/music_store.py` (new file)

Structural mirror of `movies_store.py` (see `drone-movies-feature`), same
`entry_key = sha256(lowercased relative_path)[:24]` convention, same
JSON-blob-for-scraped-fields carve-out. **Deliberately no artist/album
tables** — grouping stays computed-not-stored, same as Movies' show/season.

```sql
CREATE TABLE IF NOT EXISTS music_cache_entries (
    entry_key TEXT PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    track_name TEXT NOT NULL,
    absolute_path TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    modified_time INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS deleted_music_cache_entries ( -- same shape );
CREATE TABLE IF NOT EXISTS music_cache_changes (entry_key TEXT PRIMARY KEY, operation TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_music_cache_page ON music_cache_entries(file_path COLLATE NOCASE, entry_key);

CREATE TABLE IF NOT EXISTS music_metadata_entries (
    entry_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL,              -- always "musicbrainz" in v1
    provider_id TEXT NOT NULL,           -- MusicBrainz recording MBID
    title TEXT NOT NULL DEFAULT '',      -- scraped canonical track title
    art_relative_path TEXT,              -- album front cover (Cover Art Archive)
    artist_art_relative_path TEXT,       -- artist photo; always NULL until TheAudioDB (Phase 4)
    scraped_at TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}' -- artist, album, genres, release_date, track_number,
                                           -- disc_number, duration_ms, artist_mbid, release_mbid,
                                           -- release_group_mbid, recording_mbid
);
```

Naming note: `art_relative_path`/`artist_art_relative_path`, not a reused
`poster_relative_path`/`backdrop_relative_path` — same two-artwork-column
shape as Movies, but TMDb vocabulary ("backdrop") doesn't make sense for an
audio file. Artwork still lands sibling-to-content:
`<track's own folder>/images/<safe-stem>-mb-<field>.jpg`, for the same
same-basename-different-folder collision-avoidance reason as Movies.

Root env var: `MUSIC_ROOT`, default `/userdata/music`
(`app/common/settings.py`, new `music_root: Path` field next to
`movies_root`). Scan wiring: one more independently-try/excepted
`sync_music_cache()` call in `app/roms/rom_scanner.py`, right next to the
existing `sync_movies_cache()` call — no dedicated poller thread, same as
Movies. Audio extension **allowlist** (not denylist, same reasoning as
Movies' `_VIDEO_SUFFIXES`): `.mp3 .flac .ogg .opus .m4a .wav .wma .aac .aiff
.ape`.

## Planned parsing — `app/music/filename_parser.py` (new file)

Pure, no I/O, mirrors `movies/filename_parser.py`'s contract shape:

- `classify_location(file_path) -> ParsedLocation(artist, album,
  disc_number)` — folder-depth-only (see design decision #2 above); a
  `DISC_FOLDER_RE` (`^(?:cd|disc|disk)\s*\.?\s*(\d{1,2})$`, digits only —
  deliberately not matching spelled-out "One" the way that'd risk
  false-positiving against a real two-word album title) absorbs a disc
  subfolder into its parent album rather than treating it as a grouping
  level.
- `parse_track_filename(file_name) -> ParsedTrackName(track_number,
  disc_number, title)` — best-effort `"01 - Title"`/`"01. Title"`/
  `"1-05 Title"` (disc-track) patterns; purely cosmetic/query-quality, never
  blocks grouping the way Movies' `classify()` kind-detection can — a track
  with no parseable number is still a fully groupable song.
- `search_candidates(artist, album, track_title)` — ordered query candidates
  for the scraper, same "ordered ladder, try each, stop at first hit"
  contract as Movies' `search_candidates`.

## Planned new files

Mirrors the `app/movies/` + `app/storage/movie_*` + `handlers_movies.py`
file set:

- `app/music/filename_parser.py`, `app/music/musicbrainz_client.py`
  (`MusicBrainzClient` + `MusicBrainzUnavailableError`/
  `MusicBrainzNotFoundError(MusicBrainzUnavailableError)` — same
  subclass-before-parent exception shape as `tmdb_client.py`, same reason: a
  single not-found lookup must not abort an entire bulk job), `app/music/
  coverart_client.py` (`CoverArtClient` — checks a release's image list
  before fetching, to avoid a guaranteed-404 request for art-less releases),
  `app/music/metadata_manager.py` (orchestration; no key-storage code path
  at all, unlike movies' `metadata_manager.py`)
- `app/storage/music_scrape_jobs.py`, `app/storage/music_scrape_job_items.py`
  — 1:1 mirrors of the movie equivalents
- `app/web/handlers_music.py` (`HandlersMusicMixin`) — list/detail/stream/
  download/artwork + admin scrape search/apply/delete/bulk + batch delete
- Tests: `tests/test_music_store.py`, `tests/test_music_filename_parser.py`,
  `tests/test_music_metadata_manager.py` (with `FakeMusicBrainzClient`/
  `FakeCoverArtClient`, same injectable-`client=` shape `metadata_manager.py`
  already uses), `tests/test_music_handlers.py`,
  `tests/test_music_scrape_jobs.py`, `tests/test_music_scrape_job_items.py`,
  `tests/test_music_transfer.py` (Phase 4)

## Planned routes (mirrors the Movies route table exactly, minus scraper-settings)

| Method | Path | Handler |
|---|---|---|
| GET | `/music?q=&limit=&offset=` | list |
| GET | `/music/{key}` | detail |
| GET | `/music/{key}/stream` | Range-aware stream |
| GET | `/music/{key}/download` | download |
| GET | `/music/{key}/artwork/{art\|artist}` | artwork |
| GET | `/admin/music/{key}/scrape/search?q=` | manual search |
| POST | `/admin/music/{key}/scrape/apply` | apply a match |
| POST | `/admin/music/{key}/scrape/delete` | remove scraped metadata |
| GET/POST | `/admin/music/scrape/bulk[/items][/retry]` | bulk job status/start/items/retry |
| POST | `/admin/music/delete` | batch delete |
| GET | `/peer/music/...` (Phase 4) | P2P transfer, mirrors `handlers_peer.py`'s `movies` block |

No `/admin/music/scraper-settings` — nothing to configure (see design
decision #1).

## Planned frontend (`drone.js`)

Direct structural mirrors of the Movies functions (see
`drone-movies-feature`'s "Frontend" section for what's being copied):
`renderMusicExplorerPage()`, `renderMusicExplorerSidebar()`,
`groupMusicForExplorer()` (groups by `(artist, album)`),
`renderArtistDetailsPage(artist, album)` (mirrors `renderShowDetailsPage`),
`renderMusicDetailsPage(entryKey)` (mirrors `renderMovieDetailsPage`, but
its scraper card has no API-key-form branch — see decision #1),
`parseMusicHash`, `musicDetailHash`/`musicExploreHash`/`artistDetailHash`,
`musicStreamUrl`/`musicDownloadUrl`/`musicArtworkUrl`. Reuses the
`.movie-explorer-*` CSS classes wholesale (same convention-by-reuse Movies
and the Games/Systems page already share) — no parallel `.music-explorer-*`
class set.

**Navbar consolidation** (Phase 0): replace `index.html:40-41`'s two
`<a>` links with one `<a id="assetsMenuBtn" href="#systems">Assets</a>`. A
new `renderAssetTypeSwitcher(active)` function renders Games/Movies/Music
buttons spliced into the end of each explorer page's existing
`.movie-explorer-topbar` row (the search box's `flex-grow-1` already pushes
everything after it flush right — "right-justified, same row as search"
falls out for free, no new flex mechanics needed). This is **not** a reuse
of `renderAdminPanelTabs` (that helper renders a `<ul class="nav
nav-tabs">` meant to sit *above* a page — semantically an admin sub-tab
bar, not an inline topbar control) — it's a new, simpler function.

**Persistent player bar**: mounted once from the app's init function (named
`bootstrapApp()`, **never** `bootstrap()` — see the `window.bootstrap`
collision gotcha in `drone-admin-features`) alongside the toast container,
so it survives `content.innerHTML` swaps across router navigation. Play
buttons throughout call `playMusicTrack(entryKey, title, artist)` instead of
opening a modal; `<audio>`'s native `ended` event advances a simple
`musicPlayerQueue` array (set whenever an album/artist page's tracklist is
opened) to the next track.

## Shipped deviations / additions beyond this plan

Real changes made after initial launch, not reflected in the sections above:

- **`classify_location` also skips a release-type "category" folder.** Real
  libraries (MusicBrainz Picard-tagged) often insert a wrapper folder between
  Artist and the real album name — `Artist/Album/<Real Album Name>/track.ext`,
  `Artist/Compilation/<Real Release>/track.ext`, etc. (a `_CATEGORY_FOLDER_NAMES`
  allowlist mirroring MusicBrainz's own release-group type vocabulary).
  Without this, every release of that type collapsed into one fake bucket —
  found live against a real ~2,400-track library where "ATB / Album" held 22
  different real albums' tracks, with same-titled tracks across them looking
  like duplicates. `search_candidates` also gained `_clean_album_name` (strips
  a leading year + trailing catalog/edition parentheticals) so a messy real
  folder name like `2011 - Distant Earth (Deluxe Fanbox) (1061391KON)`
  searches MusicBrainz as `ATB Distant Earth` first, raw name kept as a
  fallback rung.
- **Music Explorer sidebar gained Artist and Likes filter sections**,
  alongside the originally-planned Genre section — all three are
  cross-faceted (each facet's button counts hold the *other two* fixed, via
  `musicExplorerFilteredRows({excludeX: true})` in `drone.js`), not
  independent single-facet filters.
- **Liked tracks (thumbs up).** A new `music_likes` table
  (`music_store.py` — presence-only, no boolean column) + `POST
  /music/{key}/like` (not admin-gated, same "browsing your own library isn't
  an admin action" reasoning as every other `/music/*` route) +
  `_apply_music_likes` overlay on list/detail responses. Toggled from a
  shared `musicLikeButtonHtml(entryKey, liked, variant)` button (icon-only on
  track rows, icon+text on the track detail page) via `toggleMusicLike`,
  which patches `musicAllRows` in place so the Likes sidebar count/filter
  update without a re-fetch.
- **Local image files as scraper-fallback artwork.** `find_local_cover_image`
  (`music_store.py`) recognizes conventionally-named local cover art
  (`cover`/`folder`/`album`/`front`/`art`, any of `.jpg/.jpeg/.png/.webp`,
  matched case-insensitively — the same convention Plex/Kodi/etc. use)
  sitting beside a track, or one folder up (the artist root, for a library
  that keeps one image per artist rather than per album). `_handle_music_artwork`
  falls back to it for the `art` field only (not `artist`) whenever there's
  no *scraped* art — never scraped, or scraped but Cover Art Archive had
  nothing for that release. Scraped art always wins when both exist. The
  frontend had to stop gating the `<img src>` on `meta && meta.art_relative_path`
  before requesting it (`renderMusicDetailShell`, the artist/album detail
  page) — that client-side gate would otherwise skip the request entirely
  and never see the server-side fallback; now every artwork `<img>` always
  attempts the URL and uses `onerror` to swap to the placeholder icon, same
  pattern the Explorer card already used.

- **Scraping went from per-track matching to album-only, and the manual
  scraper UI moved from the track page to the bottom of the album page.**
  The bulk scraper (and the manual "search and apply" card) used to match
  each local mp3/flac file to a specific track within a resolved release's
  tracklist and write that track's title/track-number/disc-number/recording
  MBID -- real bugs kept coming from exactly that per-file matching (an
  11-track folder collapsing onto a single-track "Broadcast" release;
  numbering mismatches assigning the wrong title to the wrong file). Since
  Cover Art Archive art and the useful MusicBrainz metadata (genres,
  canonical album name, release date) are release-level anyway, the scraper
  now resolves **one release per album** and applies its art + release-level
  metadata to every track in the group -- it never writes a track's title
  or any per-recording id; a track's own display title always stays
  whatever's parsed from its filename. `metadata_manager.py`:
  `_match_track_in_release`/`_apply_matched_track`/`apply()`/
  `search_track_default_query`/`MusicMatchError` are gone, replaced by
  `_apply_release_to_group`/`apply_album`/`search_album_default_query`/
  `delete_album_metadata`. Artwork is saved **once per group** now, at a
  fixed `images/album-cover.jpg` filename (matching the manual-upload
  convention exactly, so a scraped and a manually-uploaded cover share one
  slot) rather than once per track at a per-stem filename. The frontend's
  `renderMusicScraperCard`/`renderMusicScraperSearchUi`/friends (track-page,
  per-track) were replaced by `renderMusicAlbumScraperCard`/friends
  (`renderArtistDetailsPage`, bottom of the album page, hidden for the
  "Singles" pseudo-group since there's no one release to search for a
  collection of unrelated standalone tracks) -- `entry_key` in every
  `/admin/music/{key}/scrape/*` route is now just an anchor track used to
  resolve the album group server-side (`_album_group_entry_keys`), not the
  target of the write. The manual "Upload Cover" button was removed from the
  track detail page for the same reason (tracks inherit art from their
  album, per `_find_album_sibling_art`) -- it now only exists on the album
  page, where it already lived alongside the new scraper card.

- **Artist photos, keyless (not the deferred TheAudioDB follow-up from
  design decision #1 -- a different, real mechanism instead).** MusicBrainz
  itself hosts no images, but an artist lookup with `inc=url-rels`
  (`MusicBrainzClient.artist_lookup`) can surface a relation of type
  `"image"` pointing at a Wikimedia Commons file page -- most artists have
  none, which is the normal case, not a failure. `app/music/
  wikimedia_client.py` (new file) resolves that Commons page URL to a real
  downloadable image via Wikimedia's own public MediaWiki Action API (also
  keyless). `_apply_release_to_group` fetches this best-effort, cached per
  `artist_mbid` across a bulk run the same way album art is cached per
  `release_mbid` (`artist_bytes_cache`, parallel to `cover_bytes_cache`),
  and writes it to `images/artist-photo.jpg` alongside the album cover
  (same "one shared file per apply call" reasoning as
  `_album_artwork_path`, just a different fixed filename) -- populating
  `artist_art_relative_path`, which `handlers_music._handle_music_artwork`
  already served (`field=artist`) since the schema was first designed, just
  never had data before now. Shown as a small round avatar next to the
  artist name on the album detail page (`.music-artist-avatar`,
  `renderArtistDetailsPage`) -- absent for most artists, so its `onerror`
  just removes the `<img>` entirely rather than falling back to a
  placeholder icon the way the big album-cover poster does.

- **The album detail page no longer shows a row of buttons for every other
  album by the same artist.** `renderArtistDetailsPage(artist, album)`
  still groups the whole artist's tracks internally (to validate/fall back
  the requested `album` param), but the page itself only ever renders the
  one selected album's tracklist now -- no artist-wide album switcher.
  Navigating between an artist's different albums goes back through the
  Music Explorer grid (each album already has its own card there) rather
  than a tab row on this page.

- **`CoverArtClient.front_cover_url` upgrades a plain `http://` image URL to
  `https://` before returning it.** Real live bug caught manually testing
  the album-only redesign above against a real release (Radiohead's "OK
  Computer"): Cover Art Archive's own JSON sometimes reports a front-cover
  image URL as `http://`, not `https://`, even though the same path is
  equally servable over https. `download_image`'s own https-only check (a
  genuine safety guard, not relaxed) then rejected it with a `ValueError`,
  which `_apply_release_to_group`'s must-never-fail-the-apply handling
  swallowed into a silent "no art" -- so a real, popular album with real
  cover art on file ended up with no artwork and no error anywhere. Also
  exposed that `coverart_client.py` had **zero** dedicated unit tests before
  this (only ever exercised through `FakeCoverArtClient` in
  `metadata_manager` tests) -- see the new `tests/test_coverart_client.py`.

- **Artist photos now show up in two more places, and album art has one
  more fallback tier.** The Music Explorer sidebar's Artists filter list
  shows a small circular avatar (`.music-artist-sidebar-avatar`,
  `musicArtistRepresentativeEntryKey` finds any one track by that artist to
  build the artwork URL from) beside each artist name -- absent for most
  artists, same opportunistic `onerror`-removes-the-`<img>` pattern as the
  album-page avatar. `handlers_music._handle_music_artwork`'s `art`-field
  fallback chain gained a fourth and final tier: this artist's own scraped
  photo (`_find_artist_photo`, a prefix-scoped `list_music_under_artist_folder`
  query across the *whole* artist, not just one album group), tried only
  after a track's own art, sibling album art, and a local cover image file
  have all failed -- so an album with no cover of its own shows the artist's
  photo instead of a bare placeholder icon, wherever art is requested.

- **Shuffle, added after the "no shuffle/repeat in v1" call above.** A
  toggle button (`#musicPlayerBarShuffle`, bi-shuffle icon) sits in the
  persistent player bar between the like button and Previous -- so it's
  only visible while a track is actually playing, same as the rest of the
  bar. It's a global-library shuffle, not a within-queue shuffle: `let
  musicPlayerShuffle` + `let musicPlayerShuffleHistory` (`drone.js`) are
  independent of `musicPlayerQueue`/`musicPlayerQueueIndex` -- toggling it
  on doesn't touch the underlying album queue at all, it just makes
  `playMusicPlayerNext()` (and therefore the `<audio>` `ended` handler,
  since that already called `playMusicPlayerNext()`) branch to
  `playRandomMusicTrack()`, which picks uniformly from all of `musicAllRows`
  (excluding the currently-playing track when more than one exists) instead
  of stepping the queue index. `playMusicPlayerPrevious()` branches the same
  way, popping `musicPlayerShuffleHistory` (pushed to on every shuffle pick)
  since there's no sequential index to step backward through in that mode.
  Turning shuffle off (or closing the bar, which also resets shuffle)
  leaves the original queue/index untouched, so sequential next/prev
  resumes exactly where it would have if shuffle had never been toggled on
  -- verified live against the mock server (`scripts/run_mock_server.py`,
  `ROM_METADATA_INITIAL_DELAY_SECONDS=0` to skip its 60s startup poll delay
  since the mock userdata seeder doesn't seed any `music/` fixtures itself).

## Common failure patterns to avoid (learned from Movies, apply here too)

- Using a scraped artist/album name as the grouping key instead of the
  folder-derived one — splits a partially-scraped album into two cards.
- Iterating the bulk-scrape job per track instead of per (artist, album) —
  blows through MusicBrainz's throttle budget for no reason.
- Treating a single MusicBrainz not-found lookup the same as a genuine
  service-unavailable failure — mass-fails an entire bulk run.
- Reusing `renderAdminPanelTabs` for the asset-type switcher instead of a
  dedicated inline component — wrong semantics, wrong DOM position.
- Naming the music playback bar's mount function `bootstrap` — see the
  `window.bootstrap` collision gotcha.
- Shipping the audio-extension list as a denylist instead of an allowlist.

## Default bias

Copy the Movies pattern by default for anything not explicitly called out
above as different (keyless scraper, folder-primary grouping, persistent
player bar, no Chromecast/AirPlay casting in v1, no P2P sync in v1). Where
this skill and `drone-movies-feature` disagree on a shared convention
(schema shape, scan wiring, allowlist discipline, error-subclass ordering),
treat `drone-movies-feature` as authoritative — it describes shipped code,
this file describes a plan.
