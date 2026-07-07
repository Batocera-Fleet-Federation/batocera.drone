---
name: drone-admin-features
description: Use this when designing, reviewing, debugging, or modifying the Drone admin panel — System Logs, System Info, Emulators, Artwork & Metadata (scraping/gamelist), Integration (Overmind + Local Network panels), Automation, the ROMs/BIOS TreeGrid browser, per-system BIOS association, credentials/network-mode/certificate rotation, self-update buttons, or the admin route dispatch in app/web/api_routes.py and web/handlers_*.py.
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
  handlers_network.py     # 667 lines — Local Network peer discovery/pairing/sync
  handlers_overmind.py    # 374 lines — Overmind integration status/config/actions
  handlers_peer.py        # 503 lines — inbound P2P asset serving (mTLS)
  handlers_system.py      # 128 lines — network-mode, self-update, certificate rotate
  handlers_theme.py       # 139 lines — theme/branding assets
  static/js/drone.js      # ~5,700 lines — every admin panel's frontend
```

Any change touching an `admin/*` route must update **both** the dispatch entry in
`api_routes.py` and the owning `handlers_*.py` method — they're two halves of one
change, not independently useful.

## Admin menu (5 tiles)

`renderAdminMenu()` (`drone.js`, currently line 1864) renders exactly 5 tiles —
**System Logs, Emulators, Artwork & Metadata, Integration, Automation**. The old
doc documents only the first.

### System Logs

`GET /admin/logs/{source}?lines=200` (~60 supported emulator/EmulationStation/
Drone log sources), sidebar + main viewer UI. Gameplay logs (`/admin/gameplay-logs`)
were folded into this tile's scope rather than getting their own. **System Info**
is a sibling page reached from within this area (`renderAdminSystemInfoPage`,
`drone.js` ~line 5322, `GET /admin/system-info?speed=1`): runtime/CPU/memory/disk
health, network fields, and the **Drone/PixeN self-update buttons**
(`updateDroneApp()`/`runPixenUpdate()`, `drone.js` ~lines 1913-1941, routes
`/admin/system/update-drone` and `/admin/system/run-pixen-update`) — these live
on the System Info page, not a separate tile. Backend: `handlers_diagnostics.py`.

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

### Integration

`renderIntegrationPage()` (`drone.js` line 3805) toggles the Overmind and Local
Network integrations independently. Two sub-panels:

- **Open Overmind Integration** — `renderOvermindIntegrationPanel()`
  (`drone.js` line 4225): Configuration, Downloads, Asset Cache, and Processed
  Actions cards. Routes: `/admin/integrations/overmind/{status,actions,config,
  start,claim-ownership,swarm/connect,swarm/disconnect}`. Backend:
  `handlers_overmind.py`. **Regression to watch for**: commit `68f7283`
  ("Overmind integration page was being closed prematurely") fixed this panel
  deleting its own Configuration card on open — a `panel.querySelector(".card
  .log-card").remove()` call that fired unconditionally during render instead of
  only on a genuine close. Any future change to this panel's render/refresh
  lifecycle should re-check that opening it doesn't tear down its own DOM.
- **Local Network integration** — `renderLocalNetworkIntegrationPanel()`
  (`drone.js` line 3893): peer discovery, pairing-code rotation, per-peer
  pair/forget, browsing a peer's ROM/BIOS/save/config/gameplay-history assets,
  and bulk sync. Routes: `/admin/local-network/{status,discover,pairing-code/
  rotate,peers/{id}/{pair,forget,assets},sync,sync-bulk}`. Backend:
  `handlers_network.py`.

### Automation

`renderAutomationPage()` (`drone.js` line 4569) — idle-volume behavior
(`/admin/automation`, `/admin/automation/idle-volume`): lowers volume after a
period of no controller input, but active gameplay via emulatorlauncher
suppresses this even without controller input seen.

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
sourced from `Abdess/retrobios`). The system list travels to Overmind in the
`systems` field on each BIOS asset and is stored in a join table
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
- Reintroducing the premature Overmind-integration-panel-close bug class when
  touching that panel's render/refresh lifecycle.
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
Overmind-integration changes (if applicable):
...
Local-network changes (if applicable):
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
- reintroduce the premature-panel-close regression in the Overmind Integration
  panel,
- duplicate BIOS-association logic outside the `drone_bios_systems` join table
  and its vendored MD5 map,
- add a destructive action (purge, clear, rotate cert, remove-missing) without
  a confirm dialog, matching the existing pattern for those buttons.

## Default bias

When unsure, keep new admin functionality inside the fitting existing tile
(only add a 6th tile for a genuinely new category), keep frontend route names
symmetric with their backend route + handler, and keep destructive actions
behind an explicit confirm step like the existing ones.
