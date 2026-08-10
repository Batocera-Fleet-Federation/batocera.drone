---
name: drone-movies-feature
description: Use this when designing, reviewing, debugging, or modifying the Drone Movies feature — the movies_cache_entries/movies_metadata_entries SQLite schema, filesystem scanning (movies_store.py), show/season/episode grouping (movies/filename_parser.py), TMDb scraping (movies/tmdb_client.py + movies/metadata_manager.py, including the bulk-scrape background job), genres, in-browser video playback (Range-stream /movies/{key}/stream), Chromecast/AirPlay casting (movies/cast_stream.py), or the Movies Explorer/show-detail frontend in drone.js. Also the reference template when building a parallel media-asset feature (e.g. Music) that's meant to mirror this one.
---

# Drone Movies Feature Skill

## Goal

Be the single accurate source on how the Movies feature actually works end to
end — storage, scanning, scraping, playback, and frontend — verified against
the real code (file:line), not against `ADMIN_FEATURES.md` (frozen, describes
only the original Logs tile) or the Movies section of `drone-admin-features`
(useful for the deep scraper/casting mechanics below, which check out, but
**wrong about navigation** — see "Correction" immediately below). If this
skill and the code disagree, re-verify against the code; this file should be
kept current as the source of truth for Movies specifically.

## Correction to an existing skill

`drone-admin-features` claims the navbar was consolidated into a single
"Assets" link with a tabbed Systems/Movies page (`renderAssetsTabBar`,
`renderMoviesPage`, a filesystem-tree browse mode for movies). **This is
false, verified directly (2026-08-10):** `app/web/templates/index.html:40-41`
still has two separate top-level nav links —
`<a id="systemsMenuBtn" href="#systems">Games</a>` and
`<a id="moviesMenuBtn" href="#movies">Movies</a>` — and `renderAssetsTabBar`/
`renderMoviesPage`/any movies tree-view function do not exist anywhere in
`drone.js` (only the unrelated generic `renderAdminPanelTabs(active, tabs)`
helper at `drone.js:4646`, used for the Debug/Artwork/Swarm admin tab bars,
exists). The real `#movies` route renders straight into the full-bleed
Netflix-style explorer described below — there is no tree view. Treat this
skill's navigation description as authoritative over `drone-admin-features`'s
until that skill is corrected.

## Storage — `app/storage/movies_store.py`

Flat inventory, no parent/child tables for show/season — grouping is always
computed from the file path at read time, never stored relationally (see
"Show/season grouping" below).

- `movies_cache_entries` — `entry_key TEXT PRIMARY KEY` (`sha256(lowercased
  relative_path)[:24]`), `file_path TEXT UNIQUE`, `movie_name`,
  `absolute_path`, `file_size`, `modified_time`, `fingerprint` (sampled hash,
  `sample-fp-v1`, same scheme as ROMs/saves).
- `deleted_movies_cache_entries` — same shape, archive-before-delete.
- `movies_cache_changes` — `entry_key PK, operation` — pending-changes queue,
  same pattern as the ROM/BIOS/saves caches.
- `movies_metadata_entries` — `entry_key PK, provider, provider_id, title,
  poster_relative_path, backdrop_relative_path, scraped_at, extra_json`. One
  JSON blob column (`extra_json`) carries every volatile/scraped field
  (overview, tagline, genres, cast, release_date, rating, runtime,
  `show_title`, `season_number`, `episode_number`, `youtube_trailer_key`,
  `media_type`, `season_name`, `season_overview`) — a deliberate, documented
  convention shared with `rom_cache_entries`/`bios_cache_entries` (see
  `drone-db-management`'s explicit carve-out for "loosely-structured scraped
  metadata that isn't core relational state").
- `movie_scrape_jobs.py` / `movie_scrape_job_items.py` — one-row bulk-job
  progress (`status/total/processed/current_movie/matched_count/
  skipped_count/failed_count`) + per-candidate outcome, wiped and rebuilt
  each bulk run.
- `movie_cast_tokens.py` — opaque random token → entry_key, 12h TTL, for the
  unauthenticated Chromecast-receiver fetch (see "Casting" below).

Root env var: `MOVIES_ROOT`, default `/userdata/movies`
(`app/common/settings.py`, alongside `roms_root`/`bios_root`).

**Scanning lives inside `movies_store.py` itself** (`_iter_movie_files`/
`scan_movies`/`sync_movies_cache`) — `os.walk` filtered by a video-extension
**allowlist** (`_VIDEO_SUFFIXES`: mp4/mkv/avi/mov/webm/m4v/wmv/flv/mpg/mpeg/
m2ts/ts/3gp). This must stay an allowlist, not a denylist — a denylist once
let scraper XML/`.nfo`/poster-image sidecar files sitime in `movies_root`
show up as "movies" and get synced/transferred as if they were one (a real
shipped bug: "the movies shown are all xml files").

**Periodic scan wiring**: no dedicated poller thread for movies. Movies rides
the same shared poll cycle as ROM/BIOS metadata (`app/roms/rom_scanner.py`,
inside `_poll_rom_metadata_once`, right after `_saves_store.sync_saves_cache`):

```python
try:
    _movies_store.sync_movies_cache(settings.movies_root)
except Exception as error:
    print(f"Local movies cache scan failed: ...", file=sys.stderr, flush=True)
```

Independently try/excepted so a movies-scan failure never breaks ROM/save
scanning or vice versa.

## Show/season/episode grouping — `app/movies/filename_parser.py`

Pure, no I/O. `classify(file_path, file_name)` → `ParsedEntry(kind,
show_title, year, season, episode, episode_title)`:

- `kind` ∈ `movie` / `episode` / `extra`, decided in priority order: any path
  segment matching a Plex/Kodi local-extras folder name (`Featurettes`,
  `Behind the Scenes`, `Deleted Scenes`, `Interviews`, `Scenes`, `Shorts`,
  `Trailers`, `Other`) → `extra`; else `SxxEyy`/`1x04`-style filename match →
  `episode`; else `movie`.
- **Extras get their show/season from directory structure**
  (`_extra_show_season_from_path`), since a Featurette's filename rarely
  carries a show/season indicator: tries `"<Show> SXX"` combined-folder
  convention first (this app's own `Shows/<Show (Year)>/<Show (Year)> SXX/`
  layout), then a bare `"Season NN"` folder (show name comes from *that*
  folder's own parent instead). Returns `("", None)` — leaving the entry
  ungrouped — when neither convention matches; never guesses.
- **The grouping key is always the filename/folder-parsed `show_title`,
  never the scraped TMDb canonical name.** This is load-bearing: it's what
  stops a partially-scraped show from visually splitting into two Explorer
  cards. The scraped name only ever shows as `scraped_show_title`, a display
  overlay.
- `search_candidates(stem, folder_name=...)` builds an ordered `(title,
  year)` ladder for the scraper: year-cut first (scene-release convention
  puts the year right after the title, which both strips
  quality/codec/group tags in one cut *and* yields a year to pass through
  TMDb's `primary_release_year` disambiguator — this library's own test data
  has a dozen different "Halloween" movies spanning 1978–2022), then the
  same title without the year filter, then an aggressive scene-token/
  bracketed-tag strip, then (added later) a parent-folder `"Title (Year)"`
  candidate (`folder_title_candidate`, requires a parenthesized year — a
  bare year in a folder name is too often unrelated text) for files whose
  own name has nothing usable at all.

## Scraper — `app/movies/tmdb_client.py` + `app/movies/metadata_manager.py`

TMDb (themoviedb.org), stdlib `urllib` only. **Requires a user-supplied API
key** (unlike the keyless ROM scrapers) — stored in the shared `app_state`
SQLite table under a `movies_scraper.json` namespace, sanitized before ever
reaching the browser (`get_settings` returns only `has_api_key: bool`).
`search`/`search_tv`, `details`/`tv_details`/`tv_season_details`/
`tv_episode_details` (poster, backdrop, overview, tagline, genres, cast[:20],
rating, runtime, YouTube trailer key), `download_image`.

**Two distinct error types, and the distinction is load-bearing**:
`TmdbNotFoundError(TmdbUnavailableError)` for a single 404 (one candidate has
no match — skip it, keep the bulk job going) vs. `TmdbUnavailableError` for a
revoked key or exhausted rate-limit retries (abort the whole run). Getting
this wrong is a real production incident that happened twice:
1. A 429 with no retry/backoff used to cascade into "every remaining movie
   fails" — fixed with `Retry-After`-aware backoff (`TMDB_MAX_429_RETRIES`)
   plus a proactive per-call throttle (`_throttle_before_tmdb_call`) so a
   large library doesn't trip rate-limiting in the first place.
2. *Separately*, a plain 404 on one movie/show/season/episode lookup (a
   locally-numbered TV episode/season that doesn't match TMDb's own
   numbering — e.g. a "Season 0" specials folder) used to raise the same
   `TmdbUnavailableError` and **also** mass-fail every untouched remaining
   candidate. A real ~1,250-movie library reproduced this exactly: two
   consecutive runs each reported "2 matched / 88 skipped / 1156 failed" in
   under 15 seconds, with ~1,151 of those "failures" sharing one identical
   reason string. Fixed by giving 404 its own subclass so it can be caught
   *before* the generic `TmdbUnavailableError` clause (subclass-before-parent
   ordering matters) and fail only that one candidate.

**Bulk scraping is a one-shot background job, not a forever-loop poller**
(unlike VPN self-heal or the SMTP digest) — the closest precedent is Config
Backups. State lives in a SQLite row (`movie_scrape_jobs.any_running()`),
guarded against a genuine two-thread race (two POSTs landing on different
handler threads before either has inserted its "running" row) by an
*additional* module-level `threading.Lock()` — the SQLite row alone survives
a process restart but doesn't close that same-process race window.
`threading.Thread(daemon=True)`, no pool, no cancel. Stops early only when
TMDb itself becomes unavailable (bad key, network down); a single
no-match/empty-query movie does not stop the run. "Rescan all" (unchecked
by default) controls the candidate set: unchecked queues only movies with no
poster yet.

**TV episodes get show-level caching**: the resolved TMDb show id and
show-details payload are cached *per bulk-job run*, keyed by show title, so a
season with a dozen episode files costs one TV search + one show-details
fetch total, not one of each per episode. Season-level data
(`tv_season_details`) is fetched too and **its poster is preferred over the
show poster** for what gets downloaded as the episode's own artwork (TMDb has
no season-level backdrop, so backdrops stay show-level) — this is what
actually makes the show-detail page's season switcher change the artwork,
not just re-list episodes.

**Artwork placement**: `<movie's own folder>/images/<safe-stem>-tmdb-
<field>.jpg` — sibling to the specific file, not one shared root folder.
Deliberate: two different shows that both happen to have a same-named
episode file (two `S01E01.mp4`s in different show folders) must not collide.

**Direct TMDb link/ID lookup** (`parse_tmdb_movie_id`, matched via a scoped
`themoviedb\.org/movie/(\d+)` regex, never by stripping non-digit characters
from a pasted string — that would wrongly concatenate unrelated digits from
a slug or query string) is the escape hatch for a title TMDb's own search
ranks outside the default `limit=10` results.

## Genres

Come only from scraped data (`extra_json.genres`), no normalized table.
`movies_store.list_movie_genres()` bulk-reads `{entry_key: [genre,...]}` for
the whole library in one query. `handlers_movies._apply_movie_kind_and_genres`
is the single overlay point adding both `kind` (works pre-scrape, from
filename classification alone) and `genres` (post-scrape only) to every
`/movies` list row. Frontend genre filtering is entirely client-side faceted
counting over the already-fetched row set — no server-side genre query param
on the main list endpoint.

## Playback

**In-browser `<video>` is the primary path, not Chromecast-only.**
`_handle_movie_stream` → `_stream_movie_range` (`handlers_movies.py`) is a
Range-aware (206 Partial Content) authenticated route at
`GET /movies/{entry_key}/stream` — the one route in this app implementing
HTTP Range, needed so a `<video>` can seek without redownloading from byte 0.
Wired into a Bootstrap modal (`openMoviePlayerModal`, drone.js) with an
inline `<video controls autoplay>`.

**Casting (Chromecast/AirPlay)** is real but genuinely complex — a second,
deliberately minimal **unauthenticated** plain-HTTP listener
(`_CastHttpHandler` in `drone_api.py`, gated on `DRONE_CAST_ENABLED`, default
on) serving exactly one route, token-gated (12h TTL, single-movie-scoped,
minted only by an already-authenticated request). Hard-won details worth
not re-learning the hard way:
- **The cast URL's host must be the local socket address
  (`getsockname()[0]`), never the browser's `Host` header** — a Chromecast
  generally can't resolve local mDNS hostnames (`batocera.local`) at all;
  echoing `Host` back looked like a working connection (TV switches to the
  cast screen) that silently died before fetching a single byte.
- **`protocol_version = "HTTP/1.1"` is required** on this listener — every
  other listener in the app is fine on the `BaseHTTPRequestHandler`
  HTTP/1.0 default, but a Chromecast on HTTP/1.0 progressive-streams commonly
  buffers forever without ever starting playback.
- **`disableRemotePlayback` on the `<video>` element is load-bearing, not
  decorative** — Android Chrome's own native Remote Playback UI (a separate
  affordance from the Google Cast Sender SDK button) will otherwise connect
  a session using the session-cookie-gated HTTPS stream URL, which the TV
  can't fetch, producing a "connects but never plays" symptom that looks
  identical to a broken cast flow but has nothing to do with it.
- **Chromecast's default receiver doesn't support Matroska at all**, and only
  handles HEVC/H.265 on newer hardware — no server-side fix short of
  transcoding (not implemented). AirPlay is considerably more forgiving for
  `.mkv`.
- A resolved `loadMedia()` promise does **not** mean playback started — an
  unsupported container buffers forever with no error event at all, so the
  frontend pairs an error listener with a 25s stall timeout
  (`watchCastSessionForPlaybackFailure`) and settles on whichever comes
  first.

## Frontend (`drone.js`)

No shared "Explorer" base component exists — Movies and the Games/Systems
browse page share CSS/JS chrome **by convention** (same CSS classes, same
copy-pasted render-function shape), not by a real abstraction.
`document.body.classList.toggle("movie-explorer-active", ...)` is set for
*both* the `#movies` and `#systems` routes.

- `renderMovieExplorerPage()` (`drone.js:1699`) — full-bleed Netflix-style
  card grid, `document.body.classList` chrome-takeover (hides sidebar/nav,
  strips app-shell padding so it reads as its own page). Fetches the whole
  `/movies` list once into `moviesAllRows`.
- `renderMovieExplorerSidebar()` (`drone.js:1779`) — Type (All/Movies/Shows)
  + Genre filter buttons (top N by count + "Show more"), client-side faceted.
- `groupMoviesForExplorer()` (`drone.js:1873`) — groups episode/extra rows by
  `(show_title, season_number)` into synthetic `{isShowGroup:true,
  episodeCount}` cards; movie rows and ungroupable extras stay individual
  cards.
- `renderShowDetailsPage()` (`drone.js:2086`) — route
  `#movies/show/<show-title>[/<season-number>]`: a season-tab strip above an
  episode list for the selected season; switching seasons is a hash change
  that re-renders the whole page (also how the header artwork updates to the
  newly-selected season).
- `router()` (`drone.js:11246`) — a big `if/else if` chain on
  `window.location.hash`, not a declarative route table.

## Routes (`app/web/api_routes.py`, dispatch on `parts[0]`)

| Method | Path | Handler |
|---|---|---|
| GET | `/movies?q=&limit=&offset=` | list |
| GET | `/movies/{key}` | detail |
| GET | `/movies/{key}/stream` | Range stream |
| GET | `/movies/{key}/download` | download |
| GET | `/movies/{key}/artwork/{poster\|backdrop}` | artwork |
| POST | `/movies/{key}/cast-token` | mint a cast token |
| GET/POST | `/admin/movies/scraper-settings` | TMDb key get/update |
| GET | `/admin/movies/{key}/scrape/search?q=` | manual search |
| POST | `/admin/movies/{key}/scrape/apply` | apply a match (`tmdb_id` or `tmdb_url`) |
| POST | `/admin/movies/{key}/scrape/delete` | remove scraped metadata (idempotent) |
| GET/POST | `/admin/movies/scrape/bulk[/items][/retry]` | bulk job status/start/items/retry |
| GET | `/admin/movies/duplicates?kind=&genre=&q=` | duplicate detection (`app/movies/movie_duplicates.py`) |
| POST | `/admin/movies/delete` | batch delete |
| GET | `/peer/movies/...` | inter-drone P2P transfer (mTLS-gated) |

Dispatch is a large `if len(parts) == N and parts[0] == "movies" and ...`
chain, not a declarative table — actual handler logic lives in
`app/web/handlers_movies.py` (`HandlersMoviesMixin`).

## Common failure patterns

- Assuming the navbar/Movies UI has been consolidated into an "Assets" tab
  or has a tree-view browse mode — it hasn't; see "Correction" above.
- Using the show/season grouping key from scraped data instead of the
  filename/folder-parsed value — splits a partially-scraped show into two
  cards.
- Treating a single-candidate TMDb 404 the same as a revoked-key/
  rate-limit-exhausted failure — mass-fails an entire bulk run over one
  unmatchable title.
- Building a cast URL from the browser's `Host` header instead of the local
  socket address — looks like it connects, never plays.
- Reverting the movie-file allowlist to a denylist — lets sidecar/scraper
  files get treated as movies.

## Default bias

When building a new feature meant to mirror this one (e.g. Music), reuse this
shape directly: flat cache-entries + metadata-entries tables with a JSON
`extra_json` blob for scraped fields, folder/filename-derived grouping key
that's independent of and takes priority over scraped display names, a
SQLite-row-backed one-shot bulk-scrape job (not a forever poller), and
Range-aware authenticated streaming as the primary playback path with casting
treated as optional, separate, and genuinely complex.
