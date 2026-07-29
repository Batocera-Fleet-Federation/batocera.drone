---
name: drone-admin-features
description: Use this when designing, reviewing, debugging, or modifying the Drone admin panel — the Debug tile (System Info/System Logs/Emulators tabs), the Artwork tile (Artwork & Metadata/Theme Gallery tabs), Torrents, VPN, Email (SMTP + notifications), the top-level Automation nav tab, the Swarm page (Swarm/Transfers tabs — pairing, tailnet, remote peer management, ROMs/BIOS/saves/movies P2P sync), the notifications bell/dropdown, the ROMs/BIOS TreeGrid browser, per-system BIOS association, credentials/network-mode/certificate rotation, self-update buttons, the session-cookie login gate, or the admin route dispatch in app/web/api_routes.py and web/handlers_*.py. For deep Torrents/aria2, VPN/OpenVPN, or SMTP/notifications implementation detail, see the dedicated drone-torrents-management, drone-vpn-management, and drone-smtp-notifications skills instead.
---

# Drone Admin Features Skill

## Goal

Keep the admin-features picture matching the actual 5-tile admin panel (some tiles
are tabbed panels bundling what used to be separate tiles), not the frozen
single-feature doc. `ADMIN_FEATURES.md` at the repo root is titled "Admin
Features - Logs Viewer" and has never been updated since it was written — it
documents only the very first admin tile, misattributes the frontend to
`index.html`, and misattributes the backend to a monolithic `drone_api.py`. This
skill supersedes it.

## Project context

Frontend for **all** admin panels lives in one file:
`app/web/static/js/drone.js` (~7,200 lines) — **not** `index.html`. Backend
routing dispatch is `app/web/api_routes.py`
(`if parts[0] == "admin" and not self.settings.admin_enabled: reject` gates the
whole tree), with actual handler logic split across `web/handlers_*.py` mixins
per the god-class-decomposition refactor (see the repo's own `CLAUDE.md`):

```text
app/web/
  api_routes.py           # admin/* route dispatch table (ApiRoutesMixin)
  handlers_artwork.py     # 710 lines — scraping providers, gamelist edit, uploads
  handlers_auth.py        # login/logout/session-status (HandlersAuthMixin) --
                          # the ONLY routes dispatched before the session-cookie gate
  handlers_config.py      # 618 lines — emulator config viewer/editor
  handlers_content.py     # 483 lines — ROM/BIOS listing (also used by the tree UI)
  handlers_diagnostics.py # 376 lines — logs, system-info, gameplay-logs
  handlers_downloads.py   # 116 lines — download queue pause/resume/cancel/retry
  handlers_network.py     # 667 lines — pairing, LAN discovery, tailnet, swarm overview
  handlers_remote_admin.py # session-cookie-gated proxy: drive a paired peer's own
                          # /admin/* surface from this Drone's Swarm page
  handlers_peer.py        # 503 lines — inbound P2P asset serving (mTLS)
  handlers_system.py      # 128 lines — network-mode, self-update, certificate rotate
  handlers_theme.py       # 139 lines — theme/branding assets
  handlers_torrents.py    # watched-folder torrent queue admin routes (see
                          # drone-torrents-management skill; backs onto
                          # transfer/torrent_manager.py + transfer/aria2_runtime.py)
  handlers_vpn.py         # OpenVPN admin routes (see drone-vpn-management skill;
                          # backs onto device/vpn_manager.py)
  handlers_smtp.py        # SMTP admin routes (see drone-smtp-notifications
                          # skill; backs onto device/smtp_manager.py)
  handlers_notifications.py # notifications-inbox admin routes (see
                          # drone-smtp-notifications skill; backs onto
                          # storage/audit_store.py)
  static/js/drone.js      # ~7,200 lines — every admin panel's frontend
common/
  auth.py                 # DroneCredentialStore + SessionAuth/SessionStore (see
                          # "Login and sessions" below) + the 401 brute-force blocker
  multipart.py            # shared multipart/form-data parser (file uploads --
                          # Torrents .torrent upload and VPN .ovpn upload both use it)
  install_paths.py        # drone_install_root() -- where the Drone app is physically
                          # deployed; Torrents' and VPN's default/fixed directories
                          # are relative to this, not to userdata_root
```

Any change touching an `admin/*` route must update **both** the dispatch entry in
`api_routes.py` and the owning `handlers_*.py` method — they're two halves of one
change, not independently useful.

## Login and sessions (replaces the old Basic Auth)

The whole admin/browser surface (not just `/admin/*`) sits behind a session
cookie now, not HTTP Basic Auth — `WWW-Authenticate: Basic` was what made
browsers pop their own native credential dialog, which is both bad UX and
**unscriptable by browser automation** (outside the DOM, invisible to
claude-in-chrome or any CDP-based tool). `/` (root HTML), `GET /auth/session`,
`POST /auth/login`, and `POST /auth/logout` are the only routes reachable
without a valid session cookie (`handlers_auth.py`); everything else —
including every `admin/*` route — requires one, checked via
`SessionAuth.authenticate_request()` (`common/auth.py`) rather than the old
`BasicAuth.check()`. Sessions are SQLite-backed (`storage/state_store.py`'s
`sessions` table), 30-day sliding expiry. A `401` from the gateway itself
carries a custom `X-Drone-Auth-Required` marker header (not
`WWW-Authenticate`) so `drone.js`'s `_handleApiUnauthorized` can tell "this
gateway's own session expired" apart from a remote-admin-proxy 401. See
`_handle_admin_credentials_update` for the one place a plain-text password is
handled server-side (never returned to the browser); changing credentials
revokes every other session's cookie except the caller's own.

## Admin menu (5 tiles, some of them tabbed)

`renderAdminMenu()` (`drone.js`) renders exactly 5 tiles — **Debug, Artwork,
Torrents, VPN, Email**. Debug and Artwork are each a tabbed panel bundling what used
to be separate tiles; the shared `renderAdminPanelTabs(active, tabs)` helper builds
the tab bar for both (and for the Swarm page's Swarm/Transfers tabs — see below),
prepended via string concatenation onto each underlying page's existing
`content.innerHTML` template, so none of the underlying render functions needed
restructuring. There is no "Integration" tile — pairing, tailnet, and fleet
management live on the **Swarm** page, which is a top-level nav item (`#admin/swarm`,
alongside Systems/Controls/Automation/Swarm/Admin in `index.html`'s sidebar), not
one of these 5 admin tiles. See "The Swarm page" below. **Automation** is also not
one of these tiles — it was moved out to its own top-level navbar link
(`automationMenuBtn`, `#admin/automation`). The **notifications bell** (top-left,
global) is not a tile either — see "Notifications bell" below.

### Debug (System Info / System Logs / Emulators tabs)

`renderDebugTabBar(active)` puts these three former standalone tiles behind one
entry point (`#admin/system-info`, icon `bi-bug`), tab-switching via
`renderAdminPanelTabs`; each underlying page function is otherwise unchanged.

- **System Info** — `renderAdminSystemInfoPage` (`drone.js`, `GET
  /admin/system-info?speed=1`): runtime/CPU/memory/disk health, network fields,
  the **Drone/PixeN self-update buttons** (`updateDroneApp()`/`runPixenUpdate()`,
  routes `/admin/system/update-drone` and `/admin/system/run-pixen-update`), and
  the **Restart EmulationStation** button (`restartEmulationStation()`, `POST
  /admin/system/restart-emulationstation` → `batocera-es-swissknife --restart`,
  backed by `device/device_control.py`'s `_restart_emulationstation()`). Backend:
  `handlers_diagnostics.py` (+ `handlers_system.py` for the restart route). Also
  hosts an **Asset Cache** card: `renderAssetCachePanel(payload, false)` fed by
  `GET /admin/asset-cache`, refreshed via `window.refreshSystemInfoAssetCache`.
  `purgeAssetCache()`/`clearPendingAssetChanges()` check
  `window.location.hash === "#admin/system-info"` before calling that refresh hook
  (falling back to the standalone orphaned `#admin/asset-cache` route otherwise).
- **System Logs** — `GET /admin/logs/{source}?lines=200` (~60 supported
  emulator/EmulationStation/Drone log sources), sidebar + main viewer UI. Gameplay
  logs (`/admin/gameplay-logs`) were folded into this tab's scope rather than
  getting their own.
- **Emulators** — `renderEmulatorsPage()` (`drone.js`) — a tree-style
  config-file browser (`GET /admin/emulators`, `GET /admin/emulators/file`) for
  viewing/editing emulator config files on the machine. Backend:
  `handlers_config.py`.

### Artwork (Artwork & Metadata / Theme Gallery tabs)

`renderArtworkTabBar(active)` collapses the former standalone Artwork & Metadata
and Theme tiles into one entry point (`#admin/artwork`, icon `bi-images`).

- **Artwork & Metadata** — scraping (LaunchBox/TheGamesDB/MobyGames):
  `/admin/artwork/{launchbox,thegamesdb,mobygames}/{search,apply}`; gamelist
  maintenance: `/admin/artwork/gamelist/{update,remove,remove-missing}`; plus
  `/admin/artwork/missing`, marquee crop, and `/admin/artwork/upload`. Backend:
  `handlers_artwork.py`.
- **Theme Gallery** — `renderThemeGalleryPage()` — browse and preview installed
  EmulationStation theme artwork (`#theme`, outside the admin route tree; reached
  via this tab, not a standalone tile anymore).

### Torrents

Watched-folder torrent downloads via a locally-spawned aria2c daemon —
provider-agnostic (any `.torrent` file, single-source, no swarm-style
multi-peer logic). Own force-start scheduler (aria2 is only ever told to add
paused; the manager itself picks who gets to run, which is what makes Force
Start able to bypass the concurrency limit). Two independent, both-optional,
both-storage-root-scoped folders: where `.torrent` files are watched, and where
aria2 actually writes downloaded payloads (can be a different disk/mount
entirely, e.g. `/media/<external-drive>` — defaults to the watch folder if
unset). A completed torrent can have its downloaded files moved elsewhere on
disk (with an optional cleanup of what's left behind); Delete removes
downloaded files too now; a global Pause/Resume toggle and a scoped bulk Clear
sit left of the Refresh button; the table always surfaces actively-downloading
torrents first. See the dedicated **drone-torrents-management** skill for the
aria2 RPC lifecycle, restart/PID-recovery behavior, upload mechanics, and all
of the above in depth.

### VPN

Provider-agnostic OpenVPN client management (Proton VPN, NordVPN, PIA, ...) —
upload a `.ovpn`, save credentials, Connect/Disconnect, live status, log
viewer/download. Connects automatically whenever the Drone starts up and a
config is ready (retried a few times; no opt-in toggle, no UI switch for this
-- see the drone-vpn-management skill's "Connecting on boot" section). Unlike
Torrents, there's no background worker for status itself: it's recomputed
fresh on every request (`/proc` scan for the running PID, tail the log, query
`tun0`) since a VPN connection is exactly one process. A "Share with Swarm"
card adds P2P sharing to a paired peer: an
off-by-default toggle (`sharing_enabled`) plus a "Pull Configuration" picker
that fetches another paired drone's config+credentials over the same
mTLS `/peer/*` channel ROM/BIOS transfers use. A separate, default-on
"Automatically reconnect if the VPN connection fails" toggle
(`self_heal_enabled`) drives a background watchdog that detects both explicit
connection errors and a decrypt/replay-error flood in the log, then
reconnects on its own -- rate-limited so it can't loop forever against a
persistently broken connection. See the dedicated **drone-vpn-management**
skill for the config rewrite rules, credential storage, process-management
design, the peer-sharing design (including why it's gated on top of plain
pairing, unlike every other asset type), and the self-heal detection/backoff
design.

### Email (SMTP + notification digest)

Provider-agnostic SMTP configuration (outgoing mail only, no IMAP -- this app
never reads a mailbox) -- host/port/STARTTLS-or-SSL/auth/from-address/
recipient. A "Send mail from this drone" master switch
gates the Test Email button and a background poller that emails a digest of
recent activity roughly every 5 minutes (there is no OS cron in this app --
every periodic feature, including this one, is an in-process thread; see
drone-smtp-notifications). Every outgoing email identifies its sending drone
(hostname + device_id, stamped in the From display name, subject, and body)
so a swarm owner can tell multiple drones' emails apart at a glance. Ten
independent toggles choose which activity types (VPN connected/disconnected,
newly connected to swarm, assets added/removed, a manual control submitted,
an automation setting updated, asset uploaded/downloaded, a torrent
finishing) are included in that digest --
these toggles only affect what gets emailed, not what's logged or shown in
the notifications bell (see below), which is unconditional. A "Share with
Swarm" card mirrors VPN's peer-sharing design closely: an off-by-default
`sharing_enabled` toggle, single-hop provenance (an imported config can't be
re-shared), and a "Pull Configuration" picker -- plus a drone with no email
setup of its own automatically adopts a sharing peer's settings on startup
(VPN's swarm-bootstrap has no equivalent gate on "is the peer's tunnel
actually up right now" here, since SMTP has no persistent-connection concept
to check). See the dedicated **drone-smtp-notifications** skill for the full
settings-field split (shared vs. local-only), the audit_log/notifications
SQLite schema, the digest poller, and every notification hook site in depth.

### Automation (top-level navbar tab, not an admin tile)

`renderAutomationPage()` (`drone.js`) — three independent automations, each with
its own enable config: **idle-volume** (`/admin/automation/idle-volume`) sets the
volume to a configured target after a period of no controller input (raises or
lowers, whichever the target requires — active gameplay via emulatorlauncher
suppresses it even without input seen); **idle-game-exit**
(`/admin/automation/idle-game-exit`) exits the running game via
`batocera-es-swissknife --emukill` after its own configured idle period, but only
while a game is actually running; **wifi-recovery**
(`/admin/automation/wifi-recovery`) checks the wireless connection every 60s and
power-cycles it (`batocera-wifi disable` then `enable`) when it's down. The two
idle automations poll `last-input-activity` (written by the privileged
input-activity monitor) every `AUTOMATION_POLL_SECONDS`. Backend:
`app/device/automation.py`. Reached via `automationMenuBtn` in `index.html`
(`#admin/automation`) — it used to be an admin tile, but moved out to the
top-level navbar alongside Systems/Controls/Swarm/Admin.

## Notifications bell (top-left, global — not an admin tile)

Reuses the mascot image in the top-left brand block (`index.html`'s
`.brand-mark` `<span>`, `id="notificationsBellBtn"`) — before this feature
that icon was purely decorative (no id, no click handler at all); the
adjacent `#brandHomeBtn` text link already owned "go home" and is untouched.
An unread-count badge (`#notificationsUnreadBadge`) sits on the icon,
polling `GET /admin/notifications/unread-count` every 20s
(`startNotificationsPoll()`, started/stopped from `applyAdminVisibility()`
alongside the other admin-gated nav links, since `/admin/*` 403s when
`admin_enabled` is off). Clicking opens a Bootstrap 5 dropdown
(`data-bs-toggle="dropdown"`, no custom show/hide code) listing the most
recent 20 notifications, populated on open (`show.bs.dropdown` →
`refreshNotificationsDropdown()`) rather than kept continuously live-polled —
an inbox that's closed doesn't need 3s freshness, only the badge does.
Clicking a row marks it read; a small dismiss button removes just that one;
"Clear all" (confirm-gated, matching this app's destructive-action
convention) deletes every notification row — this never touches the
underlying `audit_log` permanent trail, only the separate `notifications`
inbox table. See the dedicated **drone-smtp-notifications** skill for the
full schema and the 10 event types that populate this inbox.

## The Swarm page (top-level nav, not an admin tile)

Fleet management lives on its own top-level nav item, `#admin/swarm`
(`swarmMenuBtn` in `index.html`, alongside Systems/Controls/Automation/Swarm/Admin)
— **not** inside the 4-tile Admin menu. `renderSwarmPage()` (`drone.js`) replaced
the old Integration page entirely; `#admin/integration` redirects here for
old-link compatibility (the redirect comment literally says "Overmind integration is
disabled (the fleet is Overmind-free) and the Local Network configuration moved to
the Swarm page"). There is no central hub anymore — every Drone pairs directly with
its peers.

The page itself is now two tabs via `renderSwarmTabBar(active)` (shared
`renderAdminPanelTabs` helper, same pattern as Debug/Artwork above), defaulting to
**Swarm**: the fleet-overview/tailnet/pairing content below lives under the
**Swarm** tab; the download/upload queue (formerly its own top-level `Transfers`
navbar link, `transfersMenuBtn`) lives under the **Transfers** tab
(`renderTransfersPage()` → `#admin/transfers`, still its own route/page, just
reached via this tab bar instead of a separate navbar item now).

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
  is the real authorization check: `/admin/remote/connect` logs into the peer's own
  `POST /auth/login` with the submitted credentials and caches **only the resulting
  session cookie, server-side only, in memory, never on disk, never returned to the
  browser**. That cookie is resent as a `Cookie` header on every proxied call, so the
  target's own `SessionAuth.authenticate_request()` runs independently each time,
  exactly as if the browser had connected to it directly — whatever that login can
  do locally is exactly what it can do remotely, nothing more. (`PeerProxyResponse`
  carries the peer's `Set-Cookie` back from the login call; see
  `_cookie_pair_from_set_cookie` in `handlers_remote_admin.py`.)
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
BIOS appears in both places, not a bug). On the **Systems** page tree
(`renderSystems()`), this bucket renders as its own bottom leaf node in a
separate `.tree-grid-bios-section` (dashed-border caption "Not part of any
single system", `bi-cpu` icon) below the regular per-system `.tree-grid`, so it
doesn't read as just another system sharing the systems' own root level.

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
- Sending a `WWW-Authenticate: Basic` header on any 401 -- that is exactly
  what triggers a browser's own native credential dialog, which is both bad
  UX and invisible/unscriptable to browser automation. Use the
  `X-Drone-Auth-Required` marker instead (see "Login and sessions" above).
- Declaring a top-level `function bootstrap()` (or `async function bootstrap()`)
  anywhere in `drone.js` -- silently clobbers `window.bootstrap` (the real
  Bootstrap UI library), breaking every `data-bs-dismiss="modal"` button
  app-wide in a way that looks like a per-modal bug. See "The window.bootstrap
  collision gotcha" above.

## Live-refreshing tile pattern (Torrents, VPN, Email)

Any tile that polls its own status every few seconds should patch specific
already-mounted DOM nodes by id, never re-render (`.innerHTML =`) the whole
tile on every poll tick — replacing the whole subtree on a 3s timer visibly
flashes and clobbers in-progress edits in any form on the same page (e.g. an
unsaved settings field). The established shape, followed by
`renderTorrentsLive`/`patchTorrentsLive`, `renderVpnLive`/`patchVpnLive`, and
`renderSmtpLive`/`patchSmtpLive` in `drone.js`: a `render*Live(payload)`
builds the full skeleton once on page mount (stable container ids for each
region that changes), and a separate `patch*Live(payload)` — called by both
the `setInterval` poll and the manual Refresh button — only ever sets
`.innerHTML` on those specific already-mounted leaf nodes. The notifications
bell's own unread-count poll (global, not tile-scoped) follows the same
leaf-patch discipline at a lighter 20s cadence — see "Notifications bell"
above.

## Modal dismiss buttons: the `window.bootstrap` collision gotcha

`drone.js` has its own app-init entry point, which **must never be named
`bootstrap`** (as a top-level `function`/`async function` declaration). A
top-level function declaration is hoisted and becomes a property of the global
object as soon as the script's global scope evaluates — since `index.html`
loads `bootstrap.bundle.min.js` (which sets `window.bootstrap` to the real
Bootstrap UI library) *before* `drone.js`, a same-named function declaration in
`drone.js` silently **overwrites `window.bootstrap` with the app's own
function**, discarding the library reference entirely. This actually happened
(the login-page/session-bootstrap entry point was originally named
`bootstrap()`) and the app's init function is now `bootstrapApp()` instead —
don't reintroduce the collision under a different name that still happens to
be `bootstrap`.

**Symptom, if this regresses:** every `window.bootstrap?.Modal` check in
`drone.js` (the standard way this app opens a Bootstrap modal — see any
`openXModal()` function) silently evaluates false and falls back to a manual
`modal.classList.add("show"); modal.style.display = "block"` path. That path
displays the modal fine (CSS doesn't care how the classes got there), so
**the bug is invisible until someone clicks a dismiss control** — no
`.modal-backdrop` element is ever created, and any button using Bootstrap's
own `data-bs-dismiss="modal"` markup silently does nothing: the global
delegated dismiss handler (registered once, at `bootstrap.bundle.min.js` load
time) calls `Modal.getOrCreateInstance(modalEl).hide()`, which lazily
constructs a *fresh* instance that was never told it's shown
(`_isShown` false), so `.hide()` no-ops. This exact bug was reported as "the
Cancel button doesn't work" on the Torrents folder-browser modal and traced
back to this collision, not to anything torrent-specific — check
`typeof window.bootstrap` (`"object"`, not `"function"`) and
`!!window.bootstrap.Modal` first whenever a `data-bs-dismiss`/Bootstrap-API
modal in this app won't close, before assuming the bug is in the modal's own
markup or its custom open/close functions.

## Browser-automation note: `window.confirm()` blocks the tab

Every destructive admin action in this app (Delete, Cancel, Clear, purge,
remove-missing, ...) guards itself with a plain `window.confirm("...")` before
calling the API — a real, intentional safety pattern (see the Safety rules
below), not a bug. It also means clicking one of those buttons under
claude-in-chrome/CDP-based automation triggers a native, **unscriptable**
browser dialog that freezes the tab (screenshots/JS eval start timing out)
until a human clicks a button in it — there is no CDP-level way to accept or
dismiss it programmatically in this app (no `Page.javascriptDialogOpening`
auto-handler is wired up). If you need to browser-test a confirm-gated action,
warn the user first and expect to ask them to click through it, rather than
assuming a `key`/`click` retry will ever unblock the tab on its own.

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
(only add a new tile for a genuinely new category, as Torrents, VPN, and
Email were),
keep frontend route names symmetric with their backend route + handler, follow
the live-refreshing tile pattern above for anything that polls, and keep
destructive actions behind an explicit confirm step like the existing ones.
