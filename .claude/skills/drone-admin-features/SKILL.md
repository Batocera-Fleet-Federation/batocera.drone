---
name: drone-admin-features
description: Use this when designing, reviewing, debugging, or modifying the Drone admin panel — System Info, System Logs, Emulators, Artwork & Metadata (scraping/gamelist), Automation, Theme, the Swarm page (pairing, tailnet, remote peer management), the ROMs/BIOS TreeGrid browser, per-system BIOS association, credentials/network-mode/certificate rotation, self-update buttons, or the admin route dispatch in app/web/api_routes.py and web/handlers_*.py.
---

# Drone Admin Features Skill

## Goal

Keep the admin-features picture matching the actual 5-tile admin panel, not the
frozen single-feature doc. `ADMIN_FEATURES.md` at the repo root is titled "Admin
Features - Logs Viewer" and has never been updated since it was written — it
documents only the very first admin tile, misattributes the frontend to
`index.html`, and misattributes the backend to a monolithic `drone_api.py`. This
skill supersedes it.

## Project context

Frontend for **all** admin panels lives in one file:
`app/web/static/js/drone.js` (~5,700 lines) — **not** `index.html`. Backend
routing dispatch is `app/web/api_routes.py` (~585 lines,
`if parts[0] == "admin" and not self.settings.admin_enabled: reject` gates the
whole tree), with actual handler logic split across `web/handlers_*.py` mixins
per the god-class-decomposition refactor (see the repo's own `CLAUDE.md`):

```text
app/web/
  api_routes.py           # ~585 lines — admin/* route dispatch table (ApiRoutesMixin)
  handlers_artwork.py     # 710 lines — scraping providers, gamelist edit, uploads
  handlers_config.py      # 618 lines — emulator config viewer/editor
  handlers_content.py     # 483 lines — ROM/BIOS listing (also used by the tree UI)
  handlers_diagnostics.py # 376 lines — logs, system-info, gameplay-logs
  handlers_downloads.py   # 116 lines — download queue pause/resume/cancel/retry
  handlers_network.py     # 667 lines — pairing, LAN discovery, tailnet, swarm overview
  handlers_remote_admin.py # credential-gated proxy: drive a paired peer's own
                          # /admin/* surface from this Drone's Swarm page
  handlers_peer.py        # 503 lines — inbound P2P asset serving (mTLS)
  handlers_system.py      # 128 lines — network-mode, self-update, certificate rotate
  handlers_theme.py       # 139 lines — theme/branding assets
  static/js/drone.js      # ~5,700 lines — every admin panel's frontend
```

Any change touching an `admin/*` route must update **both** the dispatch entry in
`api_routes.py` and the owning `handlers_*.py` method — they're two halves of one
change, not independently useful.

## Admin menu (6 tiles)

`renderAdminMenu()` (`drone.js`, currently line 1948) renders exactly 6 tiles —
**System Info, System Logs, Emulators, Artwork & Metadata, Automation, Theme**. The
old doc documents only System Logs. There is no "Integration" tile — pairing, tailnet,
and fleet management live on the **Swarm** page, which is a top-level nav item
(`#admin/swarm`, alongside Systems/Controls/Transfers/Admin in `index.html`'s
sidebar), not one of these 6 admin tiles. See "The Swarm page" below.

### System Info

`renderAdminSystemInfoPage` (`drone.js` ~line 5653, `GET /admin/system-info?speed=1`):
runtime/CPU/memory/disk health, network fields, and the **Drone/PixeN self-update
buttons** (`updateDroneApp()`/`runPixenUpdate()`, routes `/admin/system/update-drone`
and `/admin/system/run-pixen-update`). Backend: `handlers_diagnostics.py`. Also hosts
an **Asset Cache** card: `renderAssetCachePanel(payload, false)` fed by
`GET /admin/asset-cache`, refreshed via `window.refreshSystemInfoAssetCache`.
`purgeAssetCache()`/`clearPendingAssetChanges()` check
`window.location.hash === "#admin/system-info"` before calling that refresh hook
(falling back to the standalone orphaned `#admin/asset-cache` route otherwise).

### System Logs

`GET /admin/logs/{source}?lines=200` (~60 supported emulator/EmulationStation/
Drone log sources), sidebar + main viewer UI. Gameplay logs (`/admin/gameplay-logs`)
were folded into this tile's scope rather than getting their own.

### Emulators

`renderEmulatorsPage()` (`drone.js` line 4911) — a tree-style config-file browser
(`GET /admin/emulators`, `GET /admin/emulators/file`) for viewing/editing emulator
config files on the machine. Backend: `handlers_config.py`.

### Artwork & Metadata

Scraping (LaunchBox/TheGamesDB/MobyGames): `/admin/artwork/{launchbox,thegamesdb,
mobygames}/{search,apply}`; gamelist maintenance: `/admin/artwork/gamelist/
{update,remove,remove-missing}`; plus `/admin/artwork/missing`, marquee crop, and
`/admin/artwork/upload`. Backend: `handlers_artwork.py` (all in
`api_routes.py` ~lines 271-571 alongside other admin routes).

### Automation

`renderAutomationPage()` (`drone.js` line 4685) — two independent idle automations,
each with its own enable/idle-minutes config: **idle-volume**
(`/admin/automation/idle-volume`) sets the volume to a configured target after a
period of no controller input (raises or lowers, whichever the target requires —
active gameplay via emulatorlauncher suppresses it even without input seen), and
**idle-game-exit** (`/admin/automation/idle-game-exit`) exits the running game via
`batocera-es-swissknife --emukill` after its own configured idle period, but only
while a game is actually running. Both poll `last-input-activity` (written by the
privileged input-activity monitor) every `AUTOMATION_POLL_SECONDS`. Backend:
`app/device/automation.py`.

### Theme

`renderThemeGalleryPage()` — browse and preview installed EmulationStation theme
artwork (`#theme`, outside the admin route tree).

## The Swarm page (top-level nav, not an admin tile)

Fleet management lives on its own top-level nav item, `#admin/swarm`
(`swarmMenuBtn` in `index.html`, alongside Systems/Controls/Transfers/Admin) —
**not** inside the 6-tile Admin menu. `renderSwarmPage()` (`drone.js` ~line 4199)
replaced the old Integration page entirely; `#admin/integration` redirects here for
old-link compatibility (the redirect comment literally says "Overmind integration is
disabled (the fleet is Overmind-free) and the Local Network configuration moved to
the Swarm page"). There is no central hub anymore — every Drone pairs directly with
its peers.

- **Fleet overview** — a card grid (`renderSwarmDroneCard`) of this machine plus
  every paired peer, built from `GET /admin/swarm/overview`
  (`handlers_network.py:_handle_admin_swarm_overview`): each peer is probed live, in
  parallel with a short per-peer timeout budget, so one offline Drone degrades to
  `online: false` instead of hanging the whole page — this is a live probe on every
  page load, not a periodic-cache read. Each paired peer's card has a **Manage**
  button (see "Remote peer management" below).
- **Tailnet card** — `GET /admin/tailnet/status` + `POST /admin/tailnet/discover`
  (`device/tailnet_service.py` backs this): enrollment status, one-click setup
  (paste a Tailscale auth key), auth-key rotation, and code-free pairing with any
  other online tailnet device. Enroll/rotate (and drone startup, for a node
  hands-free-enrolled by the installer's `TS_AUTHKEY`) also make a best-effort,
  opt-in call to Tailscale's own admin API to disable key expiry for this
  device (`disable_key_expiry`/`_maybe_disable_key_expiry`) — so an unattended
  Drone never strands itself needing a human to paste a fresh key when the
  node key would otherwise expire. Opt-in via `DRONE_TAILSCALE_OAUTH_CLIENT_ID`/
  `DRONE_TAILSCALE_OAUTH_CLIENT_SECRET` (a Tailscale OAuth client, ideally
  scoped to just `devices:core:write` and tagged to this fleet); silent no-op
  without them configured, same as before this existed.
- **Pairing card** — the rotating local pairing code
  (`POST /admin/local-network/pairing-code/rotate`) used for same-LAN pairing.
- **Nearby Drones card** — LAN-discovered candidates (`POST /admin/local-network/discover`)
  with per-peer pair/forget actions. Routes:
  `/admin/local-network/{status,discover,pairing-code/rotate,peers/{id}/{pair,forget,assets}}`.
  Backend: `handlers_network.py`.

### Remote peer management (the "Manage" button)

Opens a **separate browser tab** at `?manage=<peer_id>` that proxies every admin
call for its whole lifetime to that one paired peer — the originating tab is
untouched, so there's no mixed local/remote state to track. Backend:
`handlers_remote_admin.py` (`HandlersRemoteAdminMixin`). Key properties:

- **Credential-gated, not a new role system** — the peer's own existing admin login
  is the real authorization check, verified once via `/admin/remote/connect` and
  cached **server-side only, in memory, never on disk, never returned to the
  browser**. The target's own `BasicAuth.check()` runs independently on every single
  proxied call, exactly as if the browser had connected to it directly — whatever
  that login can do locally is exactly what it can do remotely, nothing more.
- A persistent top-of-page banner (`managedPeerBanner` in `index.html`) names the
  peer whenever a tab is impersonating one; its absence is the "local" default.
- Only lightweight admin JSON/text crosses this proxy — ROM/BIOS/save/artwork
  *bytes* keep moving through the normal P2P transport directly between whichever
  two Drones are actually transferring; this feature never sits in that data path.
- Edge cases are classified, not pre-checked: unknown/forgotten peer → 404,
  offline/unreachable → 502, wrong/revoked credentials → 401 (session cleared),
  admin disabled on target → 409, an unsupported route → reported as a version
  mismatch.
- `peer_id` arrives as a URL path segment and must be `unquote()`d explicitly (unlike
  query-string values, Python's stdlib server does not auto-decode path segments) —
  a past regression here made every proxied call 404 even though `/admin/remote/connect`
  worked (its `peer_id` comes from the JSON body, not the path).

## ROMs/BIOS TreeGrid browser (new — absent from the old doc)

A compact, filesystem-tree-style browser reached from the main nav (not the
admin panel): `system > games | bios > files`, 10 files per page with a
"Show more" button. Sentinel root `BIOS_TREE_ROOT = "__bios__"` (`drone.js`
line 65) plus the `renderSystemsTree`-family functions (`drone.js` lines
806-1273) drive both the per-system view and the top-level shared/unassigned
bucket. Backend listing: `handlers_content.py` (`_handle_bios_list`,
`system`/`unassigned` query params).

## Per-system BIOS association (new — absent from the old doc)

BIOS files are filed under each system's own "BIOS" category instead of one
flat bucket, resolved by the **Drone** at scan time against a vendored
MD5→system_name reference table (`app/roms/data/bios_system_map.json`,
sourced from `Abdess/retrobios`). The resolved system list is exposed as the
`systems` field on each BIOS asset and stored locally in a join table
(`drone_bios_systems`, migration `0002.bios_system_association.sql`). A BIOS
matching **exactly one** system files under that system's BIOS category; a
BIOS matching **zero or two-plus** systems falls to the top-level
"Shared / Unassigned BIOS" bucket instead (intentional — a genuinely shared
BIOS appears in both places, not a bug).

## Other admin surfaces present in code but absent from the old doc

Credentials update (`/admin/credentials/update`), network-mode toggle
(`/admin/network-mode`), API certificate view/rotate (`/admin/api/status`,
`/admin/api/certificate`, `/admin/api/certificate/rotate` — backend
`handlers_system.py`), asset-cache purge/clear-pending
(`/admin/asset-cache/{purge,clear-pending}`), and downloads pause/resume/
cancel/retry/clear (`/admin/downloads/{pause,resume,clear}`,
`/admin/downloads/{id}/{cancel,retry}` — backend `handlers_downloads.py`).

## Common failure patterns

- Assuming the admin frontend lives in `index.html` — it's all in
  `static/js/drone.js`.
- Assuming the backend is a monolithic `drone_api.py` — routing is
  `api_routes.py`, logic is `handlers_*.py` mixins; a route change usually
  touches both files.
- Forgetting the `admin_enabled` gate check when adding a new `admin/*` route.
- Assuming the Swarm page's fleet overview reads from a periodic cache — it
  live-probes every paired peer on each page load; don't add an expensive
  unconditional per-peer fetch elsewhere that duplicates that cost.
- Filing a BIOS file under one system when it actually matches zero or
  multiple systems (must land in the shared/unassigned bucket instead).
- Adding a log/config/emulator-file viewer route without validating the
  requested path stays inside its expected directory.

## Expected output format

When completing admin-panel work, respond using this format:

```text
Objective:
...
Admin tile(s) touched:
...
Frontend changes (drone.js):
...
Backend route + handler changes (api_routes.py + handlers_*.py):
...
BIOS/tree changes (if applicable):
...
Swarm/pairing changes (if applicable):
...
Remote peer management changes (if applicable):
...
Tests:
...
Risks:
...
Files changed:
...
```

## Safety rules

Do not:

- add an `admin/*` route unguarded by the `admin_enabled` check,
- serve raw file paths from the log/config/emulator viewers without validating
  they resolve inside the expected directory,
- duplicate BIOS-association logic outside the `drone_bios_systems` join table
  and its vendored MD5 map,
- add a destructive action (purge, clear, rotate cert, remove-missing) without
  a confirm dialog, matching the existing pattern for those buttons.

## Default bias

When unsure, keep new admin functionality inside the fitting existing tile
(only add a 7th tile for a genuinely new category), keep frontend route names
symmetric with their backend route + handler, and keep destructive actions
behind an explicit confirm step like the existing ones.
