---
name: drone-games-feature
description: Use this when designing, reviewing, debugging, or modifying the Drone Games/ROMs feature reached from the navbar's "Games" link (#systems) — the rom_cache_entries/bios_cache_entries/artwork_cache_entries SQLite schema, filesystem scanning (roms/rom_scanner.py, rom_fs_watcher.py), gamelist.xml handling (roms/gamelist.py), the three keyless scrapers (LaunchBox/TheGamesDB/MobyGames in roms/scrapers.py), artwork write/upload/crop, per-system BIOS association, duplicate detection, Browser Play (EmulatorJS), or the Systems Browse explorer frontend in drone.js.
---

# Drone Games Feature Skill

## Goal

Be the accurate source on how the Games/ROMs feature works end to end,
verified against real code (file:line, 2026-08-10), superseding parts of
`drone-admin-features` that describe this area from a stale "ROMs/BIOS
TreeGrid browser" narrative — see "Correction" below.

## Correction to an existing skill

`drone-admin-features`'s "ROMs/BIOS TreeGrid browser" section describes a
`system > games | bios > files` filesystem-tree UI with `buildMoviesTree`-
style expand/collapse functions. **No such tree view exists for
Games/ROMs**, verified directly: there is no `renderSystemsTree`/tree-node-
render function anywhere in `drone.js`. What that skill is likely pointing
at is the `.tree-grid-row` CSS class, which is real but purely cosmetic row
styling reused inside the duplicate-groups view
(`renderSystemsExploreDuplicateGroup`, `drone.js:3942`) and the BIOS row
renderer (`renderSystemsExploreBiosRow`, `drone.js:4008`) — not a
folder/expand-collapse hierarchy. **Games uses the same full-bleed
Netflix-style card-grid explorer pattern as Movies** (see
`drone-movies-feature`); it did not keep a tree view that Movies lost, and
the navbar has not been consolidated into an "Assets" tab (also verified
false directly — see `drone-movies-feature`'s correction for the shared
navbar evidence, `index.html:40-41`).

## Data model — `app/storage/rom_metadata_store.py`

Schema built inline via `_ensure_schema` (~L328-448), `CREATE TABLE IF NOT
EXISTS` + `_ensure_column` (~L513) idempotent-migration pattern (e.g.
`_ensure_column(connection, "asset_gamelists", "gamelist_md5", ...)`).
Independent tables, joined only by `system`/derived filenames — no foreign
keys:

- `asset_systems` (~L333) — system name → rom_count.
- `asset_gamelists` (~L336) — one row per system: gamelist.xml path/size/
  mtime/`gamelist_md5` (the per-system change-detection gate).
- `rom_cache_entries` (~L345) — one row per ROM (`entry_key` PK,
  `UNIQUE(system, file_path)`), `fingerprint` (sampled hash), `gamelist_path`/
  `gamelist_game_id`, `image_stem`, `extra_json`.
- `rom_genres` (~L365) — **normalized** one-row-per-(rom,genre) side table
  (unlike Movies' JSON-blob genres) — deliberately split out for indexed
  facet counts; a multi-valued `<genre>` tag gets split into separate rows.
- `bios_cache_entries` (~L374) — separate table, **full-file `md5`**, not the
  sampled fingerprint ROMs use.
- `artwork_cache_entries` (~L379-384) — filesystem-discovered artwork,
  `artwork_types` JSON list.
- `deleted_*_cache_entries` (~L385-405) tombstones + `cache_changes`
  (~L407) pending-change queue, same pattern as Movies/saves.

Heavy indexing for paging/search (~L412-440), plus `_ensure_rom_search_index`
(FTS5, falling back to LIKE). See `drone-db-management` for the general
schema philosophy this follows.

## Filesystem scanning

`app/roms/rom_scanner.py`:
- `_poll_rom_metadata_cache` (~L84) — loads the cache, diffs against the
  live tree, batches new/changed files.
- `_hash_rom_metadata_batches` (~L466) — fills sampled fingerprints in
  time-budgeted batches (`ROM_METADATA_HASH_BUDGET_SECONDS`), not all at
  once.
- `_poll_rom_metadata_once` (~L609) — drives the whole cycle, then syncs
  saves and Movies (see `drone-movies-feature`'s scan-wiring section — this
  is the same function that call chains into `sync_movies_cache`).
- `ROM_CLASSIFIER_VERSION = 3` (~L79) — a manual version bump forces a full
  reindex when field-derivation rules change; not tied to schema migrations.

Fingerprint: `app/common/fingerprint.py`'s `sample-fp-v1`
(`FINGERPRINT_ALGORITHM`) for ROMs; BIOS uses full MD5
(`RomRepository.build_md5`, delegating to `drone_api.py`).

`app/roms/rom_fs_watcher.py` — raw `ctypes`/libc inotify, no third-party
deps, debounced (`debounce_seconds=10.0` default) with a max-delay cap so a
continuously-changing directory still eventually flushes
(`min(last_event+debounce, first_event+max_delay)`).

`app/roms/rom_systems.py` (`RomSystemsSearchMixin`) — **there is no static
systems allowlist**. `list_system_names` just iterates directories under
`roms_root`, filtered only by `should_include_system` (a static method in
`drone_api.py` excluding names ending `.old`/containing `.old.` — the
network-share-referencing convention, see `drone-admin-features`). Any
subfolder of the ROMs root is effectively a recognized "system."

## gamelist.xml — `app/roms/gamelist.py`

Pure stdlib helpers, no Drone state of its own (state lives in
`rom_metadata_store.py`; the read/lookup logic sits in
`RomSystemsSearchMixin`/`RomArtworkGamelistMixin`).

- `ARTWORK_FIELDS = ("image","thumbnail","marquee","fanart","boxart",
  "video","wheel","manual")`.
- `_gamelist_details` — flattens a `<game>` element into a dict
  (multi-occurrence tags become a list).
- `_database_rom_metadata_fields` — what the scanner actually persists per
  ROM: pulls `genre` and the **real gamelist-referenced**
  `image_relative_path` — this replaced an earlier filename-guessing
  heuristic (worth knowing if you see old comments/tests referencing
  guessed paths).
- `_looks_like_placeholder_image` — rejects tiny/flat scraper placeholder
  images by size **and** a known-bad SHA-256 hash, so a failed/blank scrape
  doesn't get treated as real art.

## Scraping — `app/roms/scrapers.py` + `app/web/handlers_artwork.py`

**Three keyless clients**, all in one file — LaunchBox and TheGamesDB, plus
**MobyGames**, which the top-level CLAUDE.md doesn't mention but is real:

- `LaunchBoxClient` — `gamesdb-api.launchbox-app.com` with base-URL
  fallbacks, `.search(query, system, limit)`, `LAUNCHBOX_PLATFORM_ALIASES`
  maps ~80 Drone system names → LaunchBox platform strings.
- `TheGamesDBScraper` — `.search(title, system, limit)`.
- `MobyGamesClient` — **scrapes HTML**, not a real API (a source comment
  notes MobyGames' actual API "blocked the request with a browser
  challenge") — `.search(title, system, limit)`.

No API keys anywhere for any of the three — a genuine contrast with Movies'
TMDb, which requires one.

Routes (`app/web/api_routes.py`):
```
GET  /admin/artwork/missing
GET  /admin/artwork/{launchbox,thegamesdb,mobygames}/search
POST /admin/artwork/{launchbox,thegamesdb,mobygames}/apply
POST /admin/artwork/gamelist/{remove,update,remove-missing}
POST /admin/artwork/upload
```
Handlers: `_handle_admin_launchbox_search`/`apply` etc. in
`handlers_artwork.py`.

**No bulk-scrape job exists for ROMs** — confirmed by repo-wide grep for
`bulk_scrape`/`ScrapeJob`; only Movies has that pattern
(`movie_scrape_jobs.py`/`MovieBulkScrapeJob`). ROM scraping is **purely
per-item, manual, admin-page-driven** — there is no "rescan whole library"
button/job for Games the way there is for Movies. Worth knowing before
assuming parity: a request to "add bulk scraping for ROMs" is new work, not
a wiring gap.

## Artwork handling

`app/roms/rom_artwork_apply.py` — `RomArtworkApplyMixin.apply_remote_artwork`
is the shared write path all three scrapers funnel into: finds/creates the
`<game>` gamelist entry under `_GAMELIST_WRITE_LOCK`, writes the image to a
**sibling `images/` folder next to the system's gamelist**
(`images_dir = system_dir / "images"`) as
`{stem}-{source_label}-{field}{ext}`, sets the gamelist `<field>` tag to the
relative path, rewrites gamelist.xml. `apply_launchbox_artwork`/
`apply_thegamesdb_artwork`/`apply_mobygames_artwork` are thin
provider-specific wrappers around it.

`app/roms/rom_artwork_gamelist.py` — `RomArtworkGamelistMixin`:
`_entry_missing_artwork`/`_entry_has_duplicate_artwork`, `list_missing_artwork`
(filesystem+gamelist union), `update_gamelist_entry`/`remove_gamelist_entry(ies)`
for manual metadata edits, `resolve_artwork_file`.

**Manual upload + marquee crop are real, separate features**: the frontend
(`artworkImageUploadHtml`, `artworkMarqueeCropperHtml`) POSTs to
`/admin/artwork/upload` (`_handle_admin_artwork_upload`).

## Per-system BIOS association — `app/roms/rom_asset_bios.py`

Vendored MD5→system map at `app/roms/data/bios_system_map.json`, loaded by
`_load_bios_system_map` (cached global, degrades to `{}` on missing/corrupt
file) and queried by `bios_systems_for_md5` — used by the scanner to file a
BIOS entry under the right system(s), or leave it unassigned/shared when it
matches zero or multiple systems. `RomAssetBiosMixin` also has
`list_bios_page`/`list_bios_entries`/`find_bios_file_by_unique_id`. A
sibling vendored file, `folder_unit_systems.json`, backs multi-file/folder-
ROM classification (`rom_transfer_unit.py`) — a separate concern (e.g.
lindbergh/dreamcast marker-file games that transfer as a whole folder).

## Duplicate detection — `app/roms/rom_duplicates.py`

`RomDuplicatesMixin.find_duplicate_roms`: strips No-Intro/TOSEC-style
bracketed release tags via `normalize_rom_title` to group releases of the
same game, then ranks copies with `rom_version_rank` — an explicit
`(Rev N)`/`v#.#` regex signal first, falling back to a hardcoded
`_REGION_PRIORITY` list (World > USA > Europe > ...) as a tiebreak.

## Browser Play (EmulatorJS) — `app/roms/browser_play.py`

`SYSTEM_CORE_MAP` hardcodes ~26 systems → literal libretro core name (e.g.
`"nes": "fceumm"`, `"psx": "mednafen_psx_hw"`) — deliberately excludes psp
(needs cross-origin isolation) and saturn/segacd (BIOS/multi-disc heavy).
`ROMSET_SENSITIVE_SYSTEMS = {"mame","fba","fbneo"}` flags arcade cores where
a device-working ROM may not boot in-browser (romset-compatibility caveat
shown in the UI). Route: `GET /browser-play/supported-systems`
(`handlers_content.py`). `browser_playable` is a **query-side list filter**
on `/roms` (`_handle_rom_browse` narrows `systems_filter` to
`SYSTEM_CORE_MAP.keys()`), not a per-ROM boolean returned in list payloads.

## API routes

```
GET /systems/{system}                       -> _handle_rom_list (per-system paged list)
GET /systems/{system}/images|videos          -> asset list
GET /systems/{system}/{file}                 -> download
GET /systems/{system}/roms/{id}               -> download
GET /systems/{system}/roms/{id}/fingerprint   -> _handle_rom_fingerprint
GET /systems/{system}/images|videos/{file}    -> image/video serve
GET /bios/{id}                                -> _handle_bios_download
GET /roms                                     -> _handle_rom_browse (cross-system, Systems Browse backend)
GET /browser-play/supported-systems
```

`_handle_rom_list` (`handlers_content.py`) — per-system paged list,
`limit`/`offset`/`q`, capped at 5000, returns
`count/offset/limit/returned/has_more` when paginated. `_handle_rom_browse`
— the cross-system "Systems Browse" card-grid backend: `system`/`genre`/
`q`/`browser_playable` filters, same pagination envelope plus a
server-computed `genres` facet list (mirrors the Movies Explorer sidebar
facet pattern, but server-side here vs. Movies' fully client-side
faceting — a real difference worth preserving if you're comparing the two).

## Frontend (`drone.js`)

- `renderSystemsExplorePage` — full-bleed `movie-explorer-overlay`/
  `movie-explorer-sidebar`/`movie-explorer-grid` DOM, the same Netflix-style
  card-grid pattern Movies uses (see "Correction" above) — System/Category
  sidebar facets, search, a "Play in Browser only" toggle, and a "Find
  duplicate games" toggle (`toggleSystemsExploreDuplicatesMode`).
- ROM detail page is `renderRomMediaPage(system, uniqueId, page)`, titled
  "ROM Media": hero image, gamelist summary badges, preview video, a
  metadata edit form (`romMetadataEditFormHtml`, directly edits gamelist.xml
  title/desc/genre/rating), `artworkExternalLinksHtml` (Google/LaunchBox/
  TheGamesDB/MobyGames — **external search-page links only**, opens a new
  tab, does not itself call the scraper). Manual image/video upload + a
  marquee cropper live here too.
- **The real scraper search+apply UI is a separate page**,
  `renderMissingArtworkPage` (`#admin/artwork`, the Admin panel's "Artwork &
  Metadata" tab — see `drone-admin-features`) — a "Matches"/"Edit" tabbed
  panel (`selectArtworkRom`) with `#launchboxMatches`/
  `#theGamesDBImageMatches` result lists calling `applyLaunchboxArtwork`/
  `applyTheGamesDBArtwork`/`applyMobyGamesArtwork`. **Not directly reachable
  from the Games navbar link** — per-ROM scraping is a two-hop flow: browse
  in Games, notice missing art, then separately go to Admin → Artwork →
  find that row → search/apply. (Compare Movies, where the scraper card is
  embedded directly in the movie's own detail page.)

## Common failure patterns

- Assuming a tree/folder-browse UI exists for ROMs/BIOS — it doesn't; see
  "Correction" above.
- Assuming ROM scraping has a bulk/background-job mode like Movies — it
  doesn't; it's manual per-item only.
- Assuming `browser_playable` is a field on each ROM row — it's a
  list-narrowing query filter, not a per-row flag.
- Forgetting MobyGames when reasoning about "the ROM scrapers" — it's a
  real third provider (HTML-scraped, not an API), easy to miss since it's
  less documented at the top level than LaunchBox/TheGamesDB.
- Treating `rom_genres` and Movies' `extra_json.genres` as the same shape —
  ROMs normalize genre into its own indexed side table; Movies keeps it in
  a JSON blob. Don't copy one pattern into the other's codepath.

## Default bias

When comparing Games to Movies (e.g. while building Music — see
`drone-music-feature`), Games is the **less feature-complete** precedent for
scraping (no bulk job, no in-detail-page scraper card, no API-key
management) — prefer Movies as the template for a new bulk-scrape-capable
media feature, not Games, unless the per-item-only, admin-page-driven shape
is specifically what's wanted.
