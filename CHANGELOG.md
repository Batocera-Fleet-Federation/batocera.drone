# Changelog

## [v0.0.16] - 2026-05-25

- Refactor time.  Time to break out the css/html/javascript.
- Consolidating rom-metadata, bios, and artwork into a new streamoined assets api.  Adding artwork syncing for roms.
- Adding ability to claim ownership of drone with overmind email/password.  Adding BIOS sync capaibilities across drones.
- Enhancing duplicate auth token usage validation.  Adding bulk sync across drones.
- Enhancing drone selection for downloads.  Fixing downloads/sync state so the UI shows closer to live data.
- Fixing rom sync from drone to overmind
- Added a Claim Ownership section to the Overmind Integration page. Added local admin route POST /admin/integrations/overmind/claim-ownership. Claim requires an https:// Overmind URL, email, and password. Drone sends credentials to Overmind over HTTPS, stores only the returned Drone bearer token, and does not log or persist the password.
- Heartbeat no longer scans ROMs, sends ROM metadata, or hashes MD5s. Heartbeat now logs send start, endpoint, success/failure, status, and duration. Added independent ROM metadata poller with disk cache at /userdata/system/drone-app/rom_metadata_cache.json. Poller uses ROM_METADATA_POLL_SECONDS, detects new/changed/deleted ROMs, hashes only new/changed files, writes cache atomically, skips uploads when clean, and logs scan/hash/upload progress. Added tests for cache rebuild, deleted ROM handling, and MD5 reuse.

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
