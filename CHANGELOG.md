# Changelog

## [v0.0.29] - 2026-05-30

- Updating install scripts to fix a root vs drone-user issue on new machines.  Found when testing on pi5.

## [v0.0.28] - 2026-05-27

- Fixing auth token connection between drone and overmind after database refactor.
- Updating tests.

## [v0.0.27] - 2026-05-27

- Updating drone api to fix bug where heartbeat stops.

## [v0.0.26] - 2026-05-27

- Added refresh_emulator_list in Overmind and Drone. Drone executes it through batocera-es-swissknife --restart, refreshing EmulationStation’s list. Added a Delete Actions button and API endpoint to clear pending/in-progress queued actions for a Drone.
- Drone now keeps a durable SQLite change queue in rom_metadata_store.py (line 61). Added/updated/deleted ROM, BIOS, and artwork rows remain queued until Overmind accepts them. Failed uploads retry only pending changes, not the entire catalog.

## [v0.0.25] - 2026-05-27

- Updated Drone’s emulator config reporting to use an explicit allowlist instead of recursively reading all config-like files under /userdata/system/configs and /userdata/system/.config: overmind_reporting.py (line 18). It now collects only the specified emulator, Batocera, desktop/UI, and patch metadata paths, while excluding scraper data, RPCS3 runtime/history content, shadPS4 game/download/UI data, backups, dolphin-emu/TimePlayed.ini, and dolphin-emu/Logger.ini. I treated Logger.ini as excluded because it is runtime logging configuration and was listed as questionable. I also fixed the existing 250-file report limit behavior: if more than 250 allowed configs change, unsent files remain pending and are reported on a subsequent pass instead of being incorrectly fingerprinted as already delivered.
- Background ROM and BIOS MD5 reads now honor ROM_METADATA_HASH_IO_YIELD_SECONDS, defaulting to 0.05 seconds after each 1 MB read. Metadata polling now derives system counts from its required inventory pass instead of recursively walking ROM directories twice. Redundant per-ROM file checks during inventory assembly were removed. Emulator config collection is deferred while metadata processing is active. Cache logging now reports separate SQLite cache-load, SQLite cache-write, and total poll durations. In rom_metadata_store.py (line 119): Successful upload acknowledgement now updates only compact SQLite state fields (dirty and last_successful_upload_at) instead of loading and decoding all cached ROM, BIOS, and artwork rows. Documentation was updated in README.md (line 217), and regression coverage was added in test_unit.py (line 1222).

## [v0.0.24] - 2026-05-27

- Updating drone to only receive subset of information returned from overmind to reduce non-required information from going back and forth.

## [v0.0.23] - 2026-05-26

- Updating drone fix bug with database updates.

## [v0.0.22] - 2026-05-26

- Reduced default upload chunks from 1000 to 250. Reduced default MD5 patch batches from 1000 to 250. Drone metadata collection now prefers the local SQLite metadata cache/database when present. The metadata scan no longer reads/sends gamelist.xml payloads for ROM/system inventory; it uses cached DB data when available and filesystem rows when rebuilding. Added force_upload=True support so a clean Drone cache can still be resent to Overmind after an EC2/Overmind restart. Added Drone support for a new Overmind action: rebuild_asset_metadata.

## [v0.0.21] - 2026-05-26

- Fixing issue where drone was overwhelming overmind when pushing asset metadata.

## [v0.0.20] - 2026-05-26

- Drone now uploads large asset inventory in chunks instead of one huge POST. Metadata upload timeout is now configurable and defaults to 60s: OVERMIND_UPLOAD_TIMEOUT_SECONDS=60 Inventory chunk size is configurable and defaults to 1000 asset rows: ROM_METADATA_UPLOAD_CHUNK_SIZE=1000 Drone logs each inventory chunk start/success and logs upload failures with phase, mode, chunk, and error. Drone keeps the local metadata cache dirty=True until uploads are confirmed. If a chunk fails, the next poll retries instead of assuming Overmind has the data. If Drone restarts midway, the SQLite cache still has the metadata and dirty state, so the next poll resumes by re-sending the inventory. Overmind now understands update_mode: inventory_chunk and appends chunks without wiping earlier chunks.

## [v0.0.19] - 2026-05-26

- Refactoring out sqlite into its own python files.
- Implemented the Drone-owned persistence migration to SQLite. The shared storage layer is in state_store.py (line 1), using the existing /userdata/system/drone-app/rom_metadata_cache.sqlite3 database so the large ROM cache is not copied or rebuilt just to rename the database. Updated drone_api.py (line 1213) and overmind_reporting.py (line 1) so SQLite now stores: Drone credential hashes Overmind integration configuration Swarm snapshots and peer-check results Log delivery cursors and emulator config fingerprints Small per-ROM MD5 lookup results Peer certificate metadata Processed Overmind action history Mock-mode marker state Incremental ROM/BIOS/artwork metadata cache Existing JSON and action-history files are imported on first access and removed after successful migration. Completed action history remains bounded in SQLite instead of growing indefinitely. I intentionally did not move these into SQLite: ROM, BIOS, artwork, videos, and gamelist.xml, because they are Batocera content Certificate and private key PEM files, because TLS APIs require file paths Drone stdout/stderr and Batocera logs, because they are tailed as streaming log files I also fixed an initialization edge case: if credentials or another state item creates the shared database before ROM metadata runs, the first metadata poll initializes its tables without discarding the other stored state. Documentation and changelog entries were updated
- Updating logic so that it caches rom metadata in a much better way without taking machine down.

## [v0.0.18] - 2026-05-26

- Updating how logs are streamed to overmind so that overmind does not get delayed.
- Replaced the monolithic ROM metadata JSON checkpoint rewrites with incremental SQLite row updates, including automatic migration of existing caches. Metadata hashing now yields disk time between chunks, starts after a startup delay, and suspends overlapping filesystem telemetry walks while active.
- Consolidated Drone-owned persistent state into the local SQLite database, migrating credentials, Overmind connection/swarm/peer state, reporting cursors, ROM MD5 lookups, peer certificate metadata, mock-mode marker state, and processed action history from legacy files on first use.
- Updating algorithm to only download from drones that are reachable via published IP (i.e. resolvable over internet).
- Drone peer health checks and ROM/BIOS/artwork transfers now prefer the public endpoint over local/private addresses
- Removing ability to Update remote machine for now.  Adding ability to toggle kiosk mode on or off remotely.
- Updating speed sampling to use cloudflare instead of overmind.
- now checkpoints discovery and BIOS hash progress while a scan is in progress, preserving prior entries until a complete scan can safely confirm deletions.
- Updating rom metadata poller to start even if overmind connection is not available.
- PS3/PS4 folder ROMs are now inventoried as entry_type: folder with recursive size/mtime, but no MD5 hashing. Drone exposes a peer folder manifest endpoint and sync recreates the full directory tree file-by-file on the target. Overmind preserves entry_type through ROM metadata, master lists, sync actions, bulk sync, and sync activity. Folder ROM sync matches by system/path when there is no MD5.
- Cleaning up navbar UI.
- Refactor time!  Breaking more components out into modular pieces.
- Adding more unit test for rom metadata caching behvaiors
- Sends a full ROM inventory without calculating new ROM MD5 hashes first in drone_api.py (line 6967). Calculates missing ROM MD5 values afterward and sends incremental rom_hash_patch updates from drone_api.py (line 7144). Sends hash updates every 1000 processed ROMs by default, configurable through ROM_METADATA_MD5_BATCH_SIZE, defined in drone_api.py (line 116). Preserves existing cached hashes for unchanged ROMs, avoiding unnecessary recalculation.
- Refactor time!  Breaking out logic into files for more structured codebase and easier readability
- Updating code to push log data and emulator configs every 30 seconds instead of having to be asked by overmind to do so.

## [v0.0.17] - 2026-05-25

- Fixing script to pull all proper newly created content folders down for js / css

## [v0.0.16] - 2026-05-25

- Refactor time.  Time to break out the css/html/javascript.
- Consolidating rom-metadata, bios, and artwork into a new streamoined assets api.  Adding artwork syncing for roms.
- Adding ability to claim ownership of drone with overmind email/password.  Adding BIOS sync capaibilities across drones.
- Enhancing duplicate auth token usage validation.  Adding bulk sync across drones.
- Enhancing drone selection for downloads.  Fixing downloads/sync state so the UI shows closer to live data.
- Fixing rom sync from drone to overmind
- Drone speed samples now test through Cloudflare Speed Test download/upload endpoints instead of using Overmind as the bandwidth target, run on initial connected startup check-in and every 10 minutes by default, and expose configurable endpoint, byte count, request timeout, and interval settings.
- Drone now handles Overmind Kiosk mode enable/disable actions by updating EmulationStation UI mode settings and restarting EmulationStation when available.
- Peer checks and asset transfers now prefer the public Drone endpoint supplied in the Overmind swarm snapshot, so Drones on separate networks can route directly to one another.
- Asset transfers now reject Drones that Overmind has not verified as publicly resolvable, including when processing previously queued sync actions.
- Added a Claim Ownership section to the Overmind Integration page. Added local admin route POST /admin/integrations/overmind/claim-ownership. Claim requires an https:// Overmind URL, email, and password. Drone sends credentials to Overmind over HTTPS, stores only the returned Drone bearer token, and does not log or persist the password.
- Heartbeat no longer scans ROMs, sends ROM metadata, or hashes MD5s. Heartbeat now logs send start, endpoint, success/failure, status, and duration. Added independent ROM metadata poller with disk cache at /userdata/system/drone-app/rom_metadata_cache.json. Poller uses ROM_METADATA_POLL_SECONDS, detects new/changed/deleted ROMs, hashes only new/changed files, checkpoints long discovery/hash builds for restart recovery, writes cache atomically, continues collecting locally without an Overmind connection, skips uploads when clean, and logs scan/hash/upload progress. Added tests for cache rebuild, deleted ROM handling, offline collection, restart recovery, and MD5 reuse.
- Log source uploads now fast-forward oversized post-outage backlogs to recent output with an explicit omission marker, keeping Overmind's Logs view current after reconnects.

## [v0.0.15] - 2026-05-23

- - Add mascot image from `content/batocera-swarm-mascot.jpg` in a common, polished UI location. - Use a placement consistent with Overmind where practical. - Ensure mascot is responsive and accessible with appropriate alt text. - Preserve existing Drone workflows and UI usability. - Manually verify mascot placement at desktop and mobile widths.
- feat: add download queue progress and cancellation
- Updating overmind integration page to indicate what is required and optional.
- Adding batocera swarm mascot image.
- feat: simplify Overmind linking with Drone-controlled naming

## [v0.0.14] - 2026-05-21

- Enhancing UI by removing md5 hashing where not required.
- Adding caching and speeding up UI

## [v0.0.13] - 2026-05-21

- Fixing load system bug

## [v0.0.12] - 2026-05-21

- Fixing bug with loading system on drone.

## [v0.0.11] - 2026-05-21

- Updating create-release.sh to work from any folder when executed.
- Tightening up UI and adding help.
- Adding ip address for router in system info page.
- - Add swarm connection controls and cert rotation - Rename active /alive references to /heartbeat - Add Overmind linked and Connected to Swarm admin badges - Add Overmind swarm connect and disconnect controls - Add connect, disconnect, and certificate rotation admin endpoints - Generate private key and CSR locally - Request signed certificate from Overmind with approved Drone bearer token - Preserve existing certificate unless signed certificate installation succeeds
- Fix Drone startup auth credential reference - Fix startup NameError caused by main() referencing auth outside create_server() - Attach auth object to server instance for safe startup logging - Read safe username from server-owned auth reference - Touch Drone API route wiring as needed
- More fixes for drone registration error with auth token
- Fixing bug where auth token was not being used after being accepted into swarm by overmind.
- Drone action polling now handles a batch per poll. Overmind returns actions: [...] while keeping legacy action for compatibility. Drone processes the returned batch sequentially, reports each action independently, and one completion-reporting failure does not stop the rest of that batch.
- - Drone ROM inventory is now disk-authoritative.   - Recursively scans `/userdata/roms/{system}`.   - Excludes sidecar, media, and metadata files.   - Includes ROMs that are missing from `gamelist.xml`.   - Enriches ROM records from `gamelist.xml` only when matched.   - ROM records now include:     - `md5`     - `size`     - `mtime`     - `relative_path`     - `source`     - `metadata_source`
- batocera.drone
- Updating to use shared mTLS cert for drones (may revise later)
- - Fixed host preference order to use `HOSTNAME_OVERRIDE`, then IPv4, then IPv6.
- Commenting out CI tests for now.
- - Overmind now requires a valid Drone authorization token for initial registration. - `/alive` remains bearer-token protected. - Valid Drone registration immediately creates the Drone and returns a Drone bearer token. - Fake/demo Overmind config is ignored when `USE_FAKE_DATA=false`. - Rotate Token now reuses the copy-friendly token modal UI. - Overmind accepts `certificate` and `system_info` in the Drone registration model. - New device creation now persists certificate metadata immediately. - Private key fields are stripped before storage. - Existing alive updates continue refreshing/storing certificate metadata. - Overmind exposes the stored public cert through: GET /api/devices/{requesting_drone_id}/peer-certificate/{peer_drone_id}
- feat: add local swarm onboarding, mTLS peer trust, and automatic ROM sync
- feat: add container support, swarm telemetry, and Batocera-like test runtime
- - Store swarm state returned from Overmind heartbeat responses - Run peer-to-peer connectivity checks against other swarm drones - Show peer status in admin Overmind integration page with pass/fail, failure reason, and last checked time - Report peer health results back to Overmind - Stream gameplay, ROM update, filesystem, and speed sample events to Overmind - Send speed samples every 5 minutes, including first startup/fake-data sample - Add certificate configuration/metadata support for Drone-to-Drone mTLS - Display certificate metadata in Drone admin page without exposing private key material - Improve LaunchBox artwork scraping and prevent duplicate media assignments - Add duplicate artwork detection/filtering across image, thumbnail, marquee, fanart, and boxart - Add admin artwork tools to non-admin ROM artwork pages - Show ROM MD5 hash on detail page and move download action to ROM detail only - Update Processed Overmind Actions table styling to match page theme
- Esuring permission and drone user are run async as to not hold up SERVICE ui.

## [v0.0.10] - 2026-05-15

- Moving all filesystem + securityt into SERVICE instead of root batocera_install.sh script.

## [v0.0.9] - 2026-05-15

- Moving sandbox user creation into service instead of running in batocera_install.sh to ensure user is always created before running app on machine.

## [v0.0.8] - 2026-05-14

- Adding back in DRONE_APP_BASE_URL as it is required for github link out.

## [v0.0.7] - 2026-05-14

- One last update for latest release to work properly.

## [v0.0.6] - 2026-05-14

- updating create release script to fix "latest" bug
- Cleaning up curl request to run the app on batocera machines.

## [v0.0.5] - 2026-05-14

- Updating create-release.sh
- Enhance create-release.sh with latest tag and changelog updates
- Context: There are two apps: - batocera.drone: runs on each Batocera device - batocera.overmind: central fleet management app - Overlord = user - Swarm = group of drones under an overlord - Drone device_id should be the MAC address - Demo user: demo@example.com
- Updating project image.
- Add logo image to README
- Uploading Hive Mind image
- Adding more overmind integration code for different action processing.
- Updating README to not be as technical.  Adding more technical pieces to bottom under Advanced User.
- for Drone: Update the scraper import to also pull any metadata info as well as media if available.  It looks like scraping mobygames might not be possible due to captcha / cloudflare.  We can remove the scraping for mobygames but let's leave the link out so people can navigate to the site manually.  admin artwork rom matches panel contains launchbox but isn’t defaulting <name> search like it should.  remove <system> from TheGamesDB link out.  Remove <system> from mobygames link out.

All notable changes to Batocera Drone will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [v0.0.4] - 2026-05-12

## [v0.0.3] - 2026-05-12
## [v0.0.2] - 2026-05-12
## [v0.0.1] - 2026-05-12
## [v0.0.1] - 2026-05-12

### Added
- Initial release pipeline with GitHub Actions
- Automated release notes generation
- Release script for local creation
