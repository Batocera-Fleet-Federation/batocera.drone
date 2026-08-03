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

### Artwork (Artwork & Metadata / Theme Gallery / Movies tabs)

`renderArtworkTabBar(active)` collapses the former standalone Artwork & Metadata
and Theme tiles into one entry point (`#admin/artwork`, icon `bi-images`), plus a
third **Movies** tab (`#admin/movies`) added alongside them for the bulk TMDb
scraper below.

- **Artwork & Metadata** — scraping (LaunchBox/TheGamesDB/MobyGames):
  `/admin/artwork/{launchbox,thegamesdb,mobygames}/{search,apply}`; gamelist
  maintenance: `/admin/artwork/gamelist/{update,remove,remove-missing}`; plus
  `/admin/artwork/missing`, marquee crop, and `/admin/artwork/upload`. Backend:
  `handlers_artwork.py`.
- **Theme Gallery** — `renderThemeGalleryPage()` — browse and preview installed
  EmulationStation theme artwork (`#theme`, outside the admin route tree; reached
  via this tab, not a standalone tile anymore).
- **Movies** — `renderAdminMoviesArtworkPage()` — TMDb API key entry (reusing the
  same `has_api_key`-gated form shape as the per-movie details page's scraper
  card, see the ROMs/BIOS TreeGrid section's Movies-tab writeup below) plus a
  **bulk scrape** action: one "Rescan all movies" checkbox and a Start button
  that scrapes either every movie or only ones still missing a poster. See
  "Bulk movie scraping" under the Movies-tab writeup for the background-job
  design.

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
admin panel) — the navbar link is labeled **Assets** (`systemsMenuBtn` in
`index.html`; the `id` and the `#systems` hash are unchanged, only the visible
text was renamed): `system > games | bios > files`, 10 files per page with a
"Show more" button. Sentinel root `BIOS_TREE_ROOT = "__bios__"` (`drone.js`
line 65) plus the `renderSystemsTree`-family functions (`drone.js` lines
806-1273) drive both the per-system view and the top-level shared/unassigned
bucket. Backend listing: `handlers_content.py` (`_handle_bios_list`,
`system`/`unassigned` query params).

The Assets page is two tabs, `renderAssetsTabBar(active)` (built on the shared
`renderAdminPanelTabs` helper, same pattern as Debug/Artwork/Swarm) prepended
onto each tab's own render function — **Systems** (`#systems`, unchanged
tree above) and **Movies** (`#movies`, its own top-level router entry →
`renderMoviesPage()`), not two sections stacked on one page. Both tabs are
genuinely separate routes, each with its own dedicated page function, mirroring
Debug's system-info/logs/emulators split — not one shared render function
branching on a sub-hash.

**Movies tab** (`renderMoviesPage()`): movies have no system/artwork
association, so instead of the ROM tree's `system > games` shape this is a
plain **filesystem tree** built from each movie's on-disk relative path.
`GET /movies` with no `limit` query param returns the *entire* inventory in
one response (`_handle_movies_list` in `handlers_movies.py` — `limit`/`offset`
still work for a genuinely paged caller, mirroring `_handle_rom_list`'s
dual-mode shape, but the tree needs the whole set client-side to build
hierarchy, and movie libraries are far smaller than ROM sets so this is
cheap). `buildMoviesTree()`/`renderMoviesTreeNode()`/`toggleMoviesFolder()` in
`drone.js` are a line-for-line mirror of the Emulators tab's
`buildEmulatorConfigTree()`/`renderEmulatorConfigTreeNode()`/
`toggleEmulatorConfigFolder()` (arbitrary nesting depth via a `--tree-depth`
CSS custom property, `moviesTreeExpanded`/`emulatorConfigTreeExpanded` — same
shape, separate Set) — the one difference is the CSS: `.movies-tree-row` (in
`drone.css`, next to `.emulator-tree-row`) reuses the same depth-indentation
`calc()` but does **not** hide `.tree-grid-action` the way the emulator
variant does, since movie rows need their Watch/Download buttons visible.
Search (`#moviesTreeSearch`) filters live via `oninput` (no Filter/Clear
buttons, unlike the Systems tree's search) since the whole tree is already
loaded client-side — matches the Emulators tab's filter UX, not the Systems
tab's.

**Movie file discovery is an allowlist, not a denylist**
(`storage/movies_store.py`'s `_VIDEO_SUFFIXES` — mp4/mkv/avi/mov/webm/m4v/wmv/
flv/mpg/mpeg/m2ts/ts/3gp): `_iter_movie_files()` only yields files with a
recognized video extension. Getting this wrong (e.g. reverting to an
ignored-suffix denylist like `.tmp`/`.lock`/`.part`) lets scraper metadata
XML, `.nfo` files, poster images, etc. sitting in `movies_root` show up as
"movies" and get synced/transferred as if they were one — this is the actual
shape of a real bug that shipped and was reported ("the movies shown are all
xml files").

Each leaf row has **Watch** (opens `openMoviePlayerModal()`, a
dynamically-created Bootstrap modal with an inline `<video controls autoplay>`
pointed at `GET /movies/{entry_key}/stream`) and **Download**
(`GET /movies/{entry_key}/download`, plain `_stream_file(..., as_attachment=True)`).
Both routes are session-gated like `/systems`/`/bios` (not `/admin/*` —
browsing your own library isn't admin-only) and respect `downloads_enabled`
the same way `_handle_download` does. The stream route is the one place in
this app that implements HTTP `Range`/206 Partial Content
(`_stream_movie_range` in `handlers_movies.py`) — necessary because a
`<video>` element needs Range support to seek/scrub a large file without
re-downloading from byte 0; nothing else here needed that (the per-game
preview clips served by `_handle_public_video` are short enough for a plain
full-body response). Closing the modal (`hidden.bs.modal`) clears the
`<video>`'s `src` and calls `.load()` so a movie doesn't keep
decoding/streaming in the background after dismissal.

**Casting (Chromecast/AirPlay) from the player modal** exists because a cast
receiver fetches the video file *itself*, directly -- no browser, no
session cookie, and it can't get past this Drone's self-signed HTTPS cert
either. **On by default**, opt-out via `DRONE_CAST_ENABLED=0` -- same "on
unless you turn it off" default as `http_redirect_port`, acceptable here
specifically because the narrow token gate (below) keeps this from being a
wide-open port the way a naive "just serve movies over HTTP" would be:

- `settings.cast_enabled` (`DRONE_CAST_ENABLED`, default **on**) gates a
  second, deliberately minimal plain-HTTP listener on `settings.cast_http_port`
  (`DRONE_CAST_HTTP_PORT`, default 8095) -- `drone_api.py`'s
  `_CastHttpHandler`, a sibling to `_HttpRedirectHandler` (same "deliberately
  separate, minimal class, real app surface never reachable over
  unencrypted HTTP" reasoning) that serves **exactly one route**,
  `GET /public/movies/{entry_key}/cast-stream?token=...`, Range-aware, and
  404s everything else -- not the movie list, not artwork, not the detail
  JSON. Set `cast_enabled` to false and this listener is never bound at
  all, so nothing about the app's exposed surface changes from before this
  feature existed.
- The token is single-movie-scoped, minted by an *already-authenticated*
  request (`POST /movies/{entry_key}/cast-token`, session-gated like every
  other movies route) and stored the same way session tokens are
  (`storage/movie_cast_tokens.py` -- an opaque random SQLite row, not a
  signed/stateless token, consistent with how the rest of this app does
  auth) with a generous 12-hour TTL (a cast session can legitimately run
  for hours). Knowing the token for one movie proves nothing about any
  other. `movies_store.resolve_movie_stream_path` and
  `common/http_range.parse_range_header` are the same path-traversal-safe
  lookup and Range-parsing logic the authenticated stream route uses --
  extracted to pure/shared functions specifically so this second listener
  doesn't duplicate (and risk drifting from) that security-critical code.
- **The cast URL's host is a raw LAN IP, deliberately NOT the browser's
  `Host` header** (`_cast_stream_host` in `handlers_movies.py`) -- the one
  place in this app that doesn't build a self-referential URL from `Host`.
  Found live, and the single most confusing failure this feature had:
  casting *appeared* to connect (TV switches to the cast screen), flickered,
  and dropped back to idle without playing. Nothing about the media was
  wrong -- it was DNS. The URL echoed back whatever hostname the **browser**
  used (`batocera`, `batocera.local`, ...) and **a Chromecast generally
  cannot resolve local hostnames at all**: its firmware commonly pins public
  DNS (8.8.8.8) and does no mDNS/search-domain lookup, so the receiver's
  very first fetch died before requesting a byte of video. That the *phone*
  resolves the name fine says nothing about the *TV*. The fix uses
  `self.connection.getsockname()[0]` -- the local address this client
  actually reached the Drone on, therefore reachable from the network the
  client is on, which is the same network the receiver must be on anyway.
  Preferred over re-deriving "some LAN IP" from the interface list, which on
  this device also turns up tailnet/container addresses a TV can't route to.
  Falls back to `Host` only when the socket address isn't advertisable
  (loopback in local dev). **Don't "simplify" this back to the `Host`
  header.**
- **CORS headers on every cast response** (`_send_cast_cors_headers`, plus a
  `do_OPTIONS` preflight): Google's default media receiver runs its playback
  page on Google's own origin, so this Drone is cross-origin to it and it
  can reject media that doesn't allow it. Permissive (`*`) is fine here --
  the route is already gated by a token the caller had to be authenticated
  to mint.
- **Frontend** (`drone.js`): `openMoviePlayerModal` feature-detects AirPlay
  (`HTMLVideoElement.prototype.webkitShowPlaybackTargetPicker` -- Safari
  only, hidden entirely elsewhere) and lazily loads the Google Cast Sender
  SDK (`loadCastSenderSdk`, `https://www.gstatic.com/...cast_sender.js`,
  added to the CSP's `script-src`/`connect-src`) the first time the modal
  opens, rendering a `<google-cast-launcher>` custom element (invisible
  until the SDK detects a receiver on the network -- absence of the icon
  during dev/testing doesn't mean it's broken). Both paths call
  `mintMovieCastToken` first, then either swap the local `<video>`'s `src`
  to the returned `cast_url` before calling
  `webkitShowPlaybackTargetPicker()` (AirPlay) or `session.loadMedia()` on
  the active Cast session, keyed off a `cast.framework.CastContextEventType
  .SESSION_STATE_CHANGED` listener and a module-level
  `currentPlayerEntryKey` (set when the modal opens, cleared when it
  closes) so the session-started handler knows *which* movie to load.
  Verified end-to-end with real `curl` requests during development (mint →
  fetch with zero cookies → 200 with correct bytes; Range request → 206;
  wrong/missing token → 404; any other path on the cast port → 404 even
  with a valid token) since the Cast SDK's own device-discovery doesn't
  activate outside a genuine Chrome install with a real receiver nearby
  (a known SDK limitation, not something to debug in this codebase).
- **`disableRemotePlayback` on the `<video>` element is load-bearing, not
  decorative.** Found live on a real device: Android Chrome puts its *own*
  native cast icon directly in a plain `<video controls>` element's control
  bar (the HTML Remote Playback API's default UI) -- a completely separate
  affordance from `<google-cast-launcher>`/`cast.framework` above, and one
  this app never controls. Tapping *that* icon connects a session using
  whatever the video's `src` happens to be at that moment -- the original
  session-cookie-gated HTTPS stream URL, which the TV can't fetch -- so the
  receiver connects and shows its idle "ready" screen while the phone,
  none the wiser, just keeps playing locally. Symptom looked exactly like a
  broken cast flow (TV "connects" but never plays; phone never stops) but
  the actual `<google-cast-launcher>` → token → `loadMedia()` path was
  never involved at all. `disableRemotePlayback` suppresses only that
  native Chrome/Android affordance -- it does not touch AirPlay (a
  separate WebKit-specific mechanism, `x-webkit-airplay`/
  `webkitShowPlaybackTargetPicker`, unaffected by this attribute) or the
  Google Cast Sender SDK integration (also unaffected -- different system
  entirely). `loadMovieOntoCastSession` also now pauses the local
  `<video>` once `session.loadMedia()` succeeds, so a *correctly* cast
  movie doesn't keep decoding/playing locally either.
- **`protocol_version = "HTTP/1.1"` on the cast listener is load-bearing.**
  Every other listener in this app inherits `BaseHTTPRequestHandler`'s
  HTTP/1.0 default and is fine there (browsers cope). **A Chromecast does
  not**: on an HTTP/1.0 progressive stream it commonly buffers forever
  without ever starting playback -- confirmed live as the second distinct
  cause of "TV shows a permanent loading spinner", after the DNS one above
  was fixed and the receiver could finally reach the URL at all. Raising it
  here is only safe because *every* response this handler can produce
  carries an accurate `Content-Length` (200/206 the real body length,
  404/204 an explicit 0), which is what makes keep-alive framing
  unambiguous -- verified with three sequential range requests served over
  one reused TCP connection. The `_response_started` flag exists for the
  same reason: under keep-alive, emitting a second response into a
  connection whose body is already partly written would corrupt the *next*
  response on that socket (harmless under HTTP/1.0, where the close ended
  the message anyway).
- **A resolved `loadMedia()` promise does NOT mean playback started** -- it
  only means the receiver *accepted* the request, and it can then fail in
  **two different ways** that need catching separately:
  `PlayerState.IDLE` + `IdleReason.ERROR`, **or** never reporting anything
  at all and buffering indefinitely. The second is what an unsupported
  container actually does in practice (confirmed on a real device with an
  MKV: cast connects, permanent spinner, **no error event is ever
  emitted**) -- so an error-only listener still leaves the UI claiming
  "Casting started" forever. `watchCastSessionForPlaybackFailure` therefore
  pairs the error hook with a `CAST_PLAYBACK_START_TIMEOUT_MS` (25s,
  deliberately generous -- a large file over a slow LAN legitimately takes
  a while to start) stall timeout, and settles on the first of
  PLAYING/error/timeout. `likelyUnsupportedOnChromecast` turns the two
  causes that dominate this library (`.mkv`, and `x265`/`HEVC` in the
  filename) into an explanation worth acting on, used both up-front (said
  at cast time rather than making the user watch a spinner for 25s first)
  and in the failure message. It only ever shapes *messaging* -- it never
  blocks the attempt, since newer Google TV hardware does play some of
  these.
- **Cast requests are logged** (`_log_cast`, one concise line per range
  served plus every rejected token), unlike `_HttpRedirectHandler`'s
  deliberately silent listener. Casting fails *on the receiver* -- off
  device, nothing to inspect -- so "did the TV ever actually fetch
  anything, and what did we answer?" is the first question worth asking
  when it doesn't work, and it's otherwise unanswerable. Look for
  `cast-stream <ip> GET 206 video/... bytes A-B/N <filename>` in the
  Drone's stdout log.
- **Known real-world limitation, not a code issue**: Chromecast's default
  media receiver **doesn't support Matroska at all** and only handles
  HEVC/H.265 on newer hardware -- between them that's most of a
  scene-release library, and it is the reason an `.mkv` will still show a
  permanent spinner no matter how correct the server side is. There is no
  server-side fix short of remuxing/transcoding (not implemented; would
  need ffmpeg on a low-power device). AirPlay/Apple TV is considerably
  more forgiving and is the right answer for MKV. Casting also only works
  when the receiver is on the same LAN as this Drone; it can't reach in
  over a tailnet the way a phone browser can (and the LAN-IP cast URL
  above makes that a clear failure rather than a mysterious one).

**Movie artwork/metadata scraper (TMDb)** mirrors the ROM scraper shape
(search → pick a match → apply) but is its own self-contained module,
`app/movies/` (`tmdb_client.py` — stdlib `urlopen` HTTP client, api-key
query-param auth, `append_to_response=credits` to fetch cast in the same
call as details; `metadata_manager.py` — orchestration: settings storage,
search, apply, artwork download). Settings (just the API key, sanitized —
never returned to the browser, same convention as SMTP's password) live in
the shared `state_store.py` `app_state` table (`movies_scraper.json`
namespace), not a dedicated file. Scraped fields live in a new
`movies_metadata_entries` SQLite table (`storage/movies_store.py`) keyed by
`entry_key`, with a loose `extra_json` blob column (overview/tagline/
genres/cast/release_date/rating/runtime_minutes) — the same
loosely-structured-field pattern `rom_cache_entries`/`bios_cache_entries`
already use, rather than one column per field.

Artwork (poster + backdrop) downloads to an `images/` folder **sibling to
the specific movie file**, not one shared root folder —
`<movie's own folder>/images/<safe-stem>-tmdb-<field>.jpg`
(`metadata_manager._artwork_path`), mirroring the ROM convention of an
`images/` folder next to the content it decorates. This per-folder
placement is deliberate, not incidental: two different shows that both
happen to have a same-named episode file (e.g. two `S01E01.mp4`s in
different show folders) must not collide and overwrite each other's art —
see `test_movies_metadata_manager.py`'s
`test_artwork_for_same_basename_in_different_folders_does_not_collide`.
Served back via `GET /movies/{entry_key}/artwork/{field}` (`field` is
`poster`/`backdrop`, mapped to the stored relative path via
`_ARTWORK_FIELD_COLUMNS` in `handlers_movies.py`) — session-gated like the
rest of the movies routes (viewing scraped art isn't admin-only), while the
scrape/apply/settings routes themselves are under `/admin/movies/*` (an
admin action). Admin routes: `GET/POST /admin/movies/scraper-settings`,
`GET /admin/movies/{entry_key}/scrape/search?q=`,
`POST /admin/movies/{entry_key}/scrape/apply` (`{tmdb_id}`).

**Scraped title replaces the filename everywhere in the UI.** Once a movie
has been scraped, its TMDb `title` is shown instead of the raw filename —
`_apply_movie_display_titles()` (`handlers_movies.py`) overlays a
`display_title` field onto every `/movies` list row (one bulk
`list_movie_display_titles()` lookup, not a query per row) and
`_handle_movie_detail` does the equivalent for the single-movie detail
response; `drone.js` reads `movie.display_title` (falling back to the raw
filename for anything never scraped) at every display site — the tree row
label, the player-modal title, the details-page `<h2>`, and the Netflix
explorer's card captions.

**Movie details page** (`renderMovieDetailsPage(entryKey)`, route
`#movies/<entry_key>` — parsed by `parseMoviesHash()`, dispatched from the
same `hash.startsWith("#movies")` router branch as the tree/explorer views)
is reached by clicking a movie's title in the tree row (now a `<button>`,
`.movie-tree-title-btn`, styled to read as plain label text — the one tree
row across the whole app where the label is a real button instead of a
span/div, since it needs to navigate) or a card in the explorer. Fetches
`GET /movies/{entry_key}` (backdrop as a hero background-image, poster,
tagline, genre badges, cast chips, overview, Watch/Download actions) then
independently renders a scraper card below it
(`renderMovieScraperCard()`): if no TMDb API key is configured yet, an
inline key-entry form (`renderMovieScraperApiKeyForm`); once one exists, a
search box + result list (`renderMovieScraperSearchUi`/
`searchMovieScraper`/`renderMovieScraperResult`) where picking a match
(`applyMovieScraperResult`) calls the apply endpoint and re-renders the
whole page from the fresh data — mirrors the ROM LaunchBox/TheGamesDB
search-then-apply UX (`list-group-item-action` rows with a thumbnail), not
a new pattern. The scraper card is admin-gated (`adminEnabled` check,
matching every other `/admin/*`-backed UI element) even though the details
page itself is reachable by anyone who can browse the library.

**Bulk movie scraping** (the Movies tab on the Artwork admin page,
`renderAdminMoviesArtworkPage()`) auto-scrapes the whole library in one
click instead of the per-movie manual search-and-apply flow above, with no
human picking a match. "Rescan all movies" (unchecked by default) controls
the candidate set: unchecked only queues movies with no poster yet
(`metadata_manager._has_artwork()` — a movie counts as "has artwork" once it
has a poster, regardless of backdrop); checked queues every movie,
re-scraping ones already done. Unlike the manual per-movie search box (which
still defaults its query to the simple `metadata_manager.clean_movie_query()`
punctuation-strip — a human sees and can edit it before searching), the bulk
job has no human in the loop, so it needs to get the query right
automatically. Two pieces make that work, both in
`app/movies/filename_parser.py` (pure, no I/O — see
`test_movies_filename_parser.py`, written against a real 257-file mixed
movie/TV library):

- **`classify(file_path, file_name)`** decides *what kind of thing a file
  is* before anything gets searched: `"episode"` if the filename matches the
  Sonarr/TRaSH `Show (Year) - SxxEyy - Episode Title` convention (or the
  older `1x04` style); `"extra"` if any path segment is one of
  Plex/Kodi/Jellyfin's standard local-extras folder names (`Featurettes`,
  `Behind the Scenes`, `Deleted Scenes`, `Interviews`, `Scenes`, `Shorts`,
  `Trailers`, `Other`) — bonus cast-interview/making-of clips living
  alongside real episodes match this exactly and are skipped without ever
  burning a TMDb call; otherwise `"movie"`.
- **`search_candidates(stem)`** turns a movie filename into an ordered
  `(title, year)` ladder, most-precise first: scene-release convention
  always puts the year immediately after the title
  ("28.Days.Later.2002.1080p...") so truncating there in one cut both drops
  every trailing quality/codec/release-group tag *and* yields a title+year
  pair to pass through TMDb's `primary_release_year` filter — the single
  biggest disambiguator for remakes/reboots sharing a title (this app's own
  test library alone has a dozen different "Halloween" movies spanning
  1978-2022). The same title is retried once more without the year filter
  as a fallback rung, then an aggressive pass strips a controlled
  vocabulary of scene tokens (resolution/source/codec/audio/edition/
  language tags) plus a trailing `-GROUPNAME` release tag for filenames with
  no year at all (the `"Ant-Man (1080p).mkv"` style). That trailing-tag
  strip is single-token-only (`-[A-Za-z0-9.]+$`, no spaces) specifically so
  it can't eat a legitimately hyphenated title's second half — an earlier,
  looser version of this regex turned "Ant-Man" into "Ant" and "Bride of
  Re-Animator" into "Bride of Re" by matching from the first hyphen to end
  of string; see that regression test in `test_movies_metadata_manager.py`.
  `_run_bulk_scrape_job` (movies) and `_search_movie_with_ladder` try each
  rung against `client.search(title, year=year)` and stop at the first hit.
- **TV episodes** get their own path: the show title parsed by `classify()`
  is searched via `TmdbClient.search_tv`/`first_air_date_year` (same
  year-then-no-year two-rung ladder, `_search_show_with_ladder`), then
  `metadata_manager.apply_tv_episode()` fetches show-level details
  (poster/backdrop/genres/cast — all show-level in TMDb's data model) plus
  this specific episode's title/overview/air-date/still via
  `TmdbClient.tv_episode_details`, and saves it as an ordinary
  `movies_metadata_entries` row with `provider="tmdb_tv"` and an
  `extra_json.media_type = "tv_episode"` marker (`show_title`/
  `season_number`/`episode_number` alongside it — no schema change, same
  loose-`extra_json`-blob convention as the movie fields). The bulk job
  caches both the resolved TMDb show id and the fetched show-details payload
  **per job run**, keyed by show title, so a season with a dozen episode
  files costs exactly one TV search and one show-details fetch, not one of
  each per episode (`test_multiple_episodes_of_the_same_show_only_search_and_fetch_show_details_once`).
  The movie details page shows a `TV · S01E01` badge + show title above the
  episode title when `metadata.media_type === "tv_episode"`
  (`renderMovieDetailShell` in `drone.js`).

This is a one-shot **background job with a pollable status**, not one of
this app's forever-loop pollers (VPN self-heal, SMTP digest, the ROM
metadata scan) — for that shape, the closest and intentionally-copied
precedent is **Config Backups** (`device/config_backup.py` +
`storage/config_backup_store.py`), not those always-on threads. Mirrored
directly:

- **State lives in a SQLite row, not an in-process flag** —
  `storage/movie_scrape_jobs.py`'s `movie_scrape_jobs` table
  (`status: running|complete|error`, `total`/`processed`/`current_movie`,
  `matched_count`/`skipped_count`/`failed_count`, `error_message`). Only the
  most recent job matters (`latest()`) — unlike config backups, a bulk
  scrape run isn't a downloadable artifact worth listing historically, so
  this store has no `list_all`.
- **`any_running()` is the guard**, checked before `create_running()`
  inserts the new row — same shape as config backup's `any_creating()`. A
  second start attempt gets `{"status": "already_running"}`, mapped to HTTP
  409 by the handler (`_handle_admin_movie_scrape_bulk_start`), exactly like
  `_handle_admin_config_backups_create`'s `already_creating` → 409 mapping.
  **Unlike config backups**, `metadata_manager.start_bulk_scrape()` wraps
  the check-then-insert in `_BULK_SCRAPE_START_LOCK` (a module-level
  `threading.Lock()`) — found live-testing this feature: two POSTs arriving
  on different request-handling threads at nearly the same instant could
  both read "nothing running yet" before either had inserted its row,
  starting two jobs at once. The SQLite row is still what makes the guard
  survive a process restart; the lock only closes the same-process race
  window between the read and the write. See
  `test_two_truly_simultaneous_starts_only_let_one_through` in
  `test_movies_metadata_manager.py` — a genuine two-thread test, not a
  sequential "pre-insert a running row" check like the rest of that file's
  guard tests, and it needs a real per-call delay in its fake TMDb client
  (`search_delay_seconds`) or the winning job can finish before the losing
  call even checks, making the race impossible to observe.
- **`threading.Thread(daemon=True)`**, no pool, no cancel — one-shot, exactly
  like `_run_backup_job`. `_run_bulk_scrape_job` stops early (breaks out of
  the loop, counting every remaining candidate as failed) the moment TMDb
  itself becomes unavailable (bad/revoked key, network down) rather than
  retrying the same doomed call once per remaining movie; a single movie
  with no TMDb match, or an empty query after cleaning (skipped, no request
  even attempted), does **not** stop the run.
- **Frontend polling**: `startMovieBulkScrapeAutoRefreshIfNeeded()` /
  `patchMovieBulkScrapeLive()` in `drone.js` — the identical
  only-poll-while-something-is-running, `document.hidden`-aware,
  in-flight-guarded `setInterval(..., 2000)` shape as Config Backups'
  `startConfigBackupsAutoRefreshIfNeeded`. (This is also why a
  claude-in-chrome browser-automation session watching this page live may
  see zero poll ticks: `document.hidden` is true for a backgrounded MCP tab,
  same as it would be for a real minimized browser tab — check the job's
  real state with a direct `GET /admin/movies/scrape/bulk` fetch instead of
  waiting on the visible progress bar to move.)

**Netflix-style movie explorer** (`renderMovieExplorerPage()`, route
`#movies/explore`, reached via the **Browse** button on the Movies tab) is
a full-bleed grid of poster cards that visually **replaces the whole app
chrome** while active — `document.body.classList.toggle
("movie-explorer-active", ...)` is set unconditionally on every route
change (same mechanism as the pre-existing `artwork-page` toggle), and
`body.movie-explorer-active` CSS rules hide `.sidebar`/`#managedPeerBanner`/
`#systemInfoBar` and strip the `.app-shell` padding/border so
`.movie-explorer-overlay` reads as its own full-screen page rather than
content embedded in the normal layout — **the router/render pipeline is
otherwise unchanged**, this is a pure CSS takeover of the existing `#content`
container, not a separate app mount. A **Back to Drone view** button
(`setHash('#movies')`) is the only way out. Cards
(`renderMovieExplorerCard`) point their `<img>` straight at
`movieArtworkUrl(entryKey, "poster")` with an inline `onerror` fallback to a
film-icon placeholder (same one-line `onerror` pattern the image-lightbox
already uses) rather than a bulk metadata prefetch — the plain `/movies`
list response has no poster path on it (only the single-movie detail
endpoint does), so probing per-card via the artwork endpoint's own 404 is
simpler than a second bulk endpoint. Search (`filterMovieExplorer`) is
client-side over the already-fetched `moviesAllRows` (reused from the tree
page's state var — refetched only if empty, so entering the explorer
directly doesn't require visiting the tree first), same live-`oninput`
convention as the tree's own search box.

**Explorer category sidebar** (Type: All/Movies/Shows, Genres: derived from
scraped metadata) is a left-side filter panel added to the explorer, backed
entirely by fields the `/movies` list response overlays onto every row
regardless of scrape status: `kind` (`"movie"`/`"episode"`/`"extra"`, from
`filename_parser.classify()` run server-side per row in
`HandlersMoviesMixin._apply_movie_kind_and_genres` — no TMDb key or scrape
needed, works immediately for every file) and `genres` (from scraped
`movies_metadata_entries.extra_json`, empty until scraped). Client-side
state (`movieExplorerTypeFilter`/`movieExplorerGenreFilter`, reset on every
page visit) combines with the existing search filter in
`filterMovieExplorer`; "extra" content only shows under the "All" type
bucket (Featurettes-style bonus clips aren't really "a movie" or "an
episode" — see the bulk-scraper writeup above), never under "Movies" or
"Shows" specifically.

**Episodes group into one card per season** in the explorer instead of one
card per episode file — `groupMoviesForExplorer()` groups every `kind ===
"episode"` row by `(show_title, season_number)` after the type/genre/search
filters run, leaving movie/extra rows as individual cards untouched. The
grouping key is always `show_title` **as parsed straight from the
filename** (present on every episode row pre-scrape, same source as `kind`
— see the category sidebar section above), never the scraped TMDb name —
a show with only some episodes scraped could otherwise split into two
cards for the same season the moment the parsed and TMDb names differ even
slightly (casing, "and" vs "&", ...). `scraped_show_title` carries the
TMDb name separately, only once at least one episode in the group has been
scraped, purely as a nicer *display* label
(`representative.scraped_show_title || representative.show_title`) —
grouping and display are deliberately decoupled fields for exactly this
reason. A season card's poster comes from its lowest-numbered episode's
`entry_key` (`movieArtworkUrl` + the same inline-`onerror`-to-icon fallback
every other card already uses) and clicking it goes to
`showDetailHash(rawShowTitle, seasonNumber)`, not the single-movie detail
page.

**Show detail page** (`renderShowDetailsPage`, route
`#movies/show/<url-encoded-show-title>[/<season-number>]`, parsed by
`parseMoviesHash`'s new `show/` branch) is what a season card opens: a
season-tab strip (one button per season number found among that show's
episodes) above an episode list for whichever season is selected — defaults
to the lowest season number if the hash's season segment is missing or
doesn't exist for this show. **Switching seasons is just a hash change**
(each tab links to `showDetailHash(showTitle, n)`) — the router re-renders
the whole page on every click, which is simultaneously the answer to
"clicking a season should update the artwork/metadata" (the header
poster/backdrop/overview/genres come from an on-demand `GET
/movies/{entry_key}` fetch of the newly-selected season's lowest-numbered
episode, so they always reflect the season now selected, not a stale
season) and keeps the current season bookmarkable/back-button-able for
free, same `#hash`-encodes-page-state convention every other stateful view
in this app already follows — no separate partial-DOM-patch code path was
needed. Reuses `.movie-detail-hero`/`.movie-detail-poster`/
`.movie-detail-title`/`.movie-genre-badge` verbatim from the single-movie
detail page's CSS for visual consistency; the overview shown prefers
`metadata.season_overview` (see below) over the representative episode's
own `overview`. Each episode row (`renderShowDetailEpisodeRow`) links to
the **existing** single-movie detail page via `movieDetailHash(entry_key)`
for Watch/Download/full metadata — this page only ever aggregates and
routes, it doesn't duplicate the per-episode detail view.

**Season-level TMDb data**: `TmdbClient.tv_season_details(tv_id,
season_number)` hits `/tv/{id}/season/{n}`, which — unlike the show-level
`/tv/{id}` — has its **own poster** (often visibly different from the
show's general poster; TMDb has no season-level `backdrop_path` though, so
backdrops stay show-level as before). `apply_tv_episode` now fetches this
(via a new `season_details` param, cached per `(tv_id, season_number)` in
`_run_bulk_scrape_job` exactly like `show_details` is cached per `tv_id` —
one extra TMDb call per season, not per episode) and **prefers the season
poster over the show poster** for what gets downloaded as the episode
file's own artwork, falling back to the show poster only when TMDb has none
for that season. `season_name`/`season_overview` are stored in
`extra_json` alongside the existing per-episode fields. This is what
actually makes the show detail page's season-switching change the artwork,
not just re-list episodes — before this, every episode of every season
downloaded the identical show-wide poster, so there was nothing to
visually change between seasons.

**The plain Movies tree** (`renderMoviesTreeLeafRow`) also shows a small
poster thumbnail per row now, same `movieArtworkUrl` + inline-`onerror`-to-
film-icon pattern as the explorer cards — this was the actual fix for "I
scraped movies but don't see artwork anywhere," since the tree (not the
explorer) is what `#movies` — the app's default landing route, see below —
actually renders.

**`body.movies-page-active`** (toggled for any `#movies*` hash, alongside
the pre-existing narrower `movie-explorer-active`) hides just
`#systemInfoBar` on the tree and single-movie detail pages too (sidebar/nav
stay, unlike the explorer's full takeover). Both this and the pre-existing
`movie-explorer-active` rule need **`!important`** on `display: none` —
`#systemInfoBar` carries Bootstrap's `.d-flex` utility class
(`display: flex !important` in `templates/index.html`), so a plain
`display: none` here loses the cascade and silently does nothing; this was
true for the explorer's version of this rule too before it was fixed
alongside the new one.

**Default landing route is now `#movies`**, not the help/tour page — the
router's `hash === "" || hash === "#"` branch redirects via `setHash`
(same one-line pattern the `#bios` redirect already used), and `#home`/
`#help` still work as explicit routes for anyone who wants the tour.
Movies loads fast (no gamelist scan involved), unlike the Artwork &
Metadata tab.

**The Admin Artwork page (`renderMissingArtworkPage`) now paints its shell
before scanning**, not after: the tab bar + a scoped spinner render into
`content` immediately, then the (potentially multi-second) gamelist/ROM
scan runs and replaces just that placeholder. Previously nothing rendered
into `content` until the scan resolved, so navigating here left whatever
page you came from sitting there looking stuck for the whole scan — the
scan itself wasn't and isn't made faster, only the *page* stops blocking on
it before showing anything.

**Artwork response caching** (`_stream_cached_image` in
`handlers_peer.py`, shared by ROM artwork and movie posters/backdrops/
thumbnails alike) now actually sets a browser-cacheable
`Cache-Control: public, max-age=3600` — it always *tried* to, but
`_send_security_headers()`'s blanket `Cache-Control: no-store` (the right
default for the session-gated JSON/HTML responses everywhere else) was
sent as a **second, separate header line** right after it, and per HTTP
semantics multiple `Cache-Control` headers concatenate with `no-store`
winning regardless of what else is present — so every image response was
silently uncacheable in the browser the whole time, only the server-side
`image_cache`/`image_miss_cache` (`common/http_cache.py`'s
`ExpiringLRUCache`/`ExpiringKeyCache`) was actually helping. Fixed by
giving `_send_security_headers(cache_control="no-store")` a parameter
instead of a hardcoded literal — every other call site is unaffected (all
zero-arg), only `_stream_cached_image` passes the real value, so there's
now exactly one `Cache-Control` header on that response, not two.

**Bulk-scrape breakdown + retry**: the "X matched · Y skipped · Z failed"
line on the Movies admin tab is now three clickable segments
(`toggleMovieBulkScrapeBreakdown`), each opening a paginated list
(`GET /admin/movies/scrape/bulk/items/{status}`, backed by
`storage/movie_scrape_job_items.py` — one row per movie in the *most
recent* run, same "only the latest run matters" convention
`movie_scrape_jobs` uses, cleared and repopulated by
`metadata_manager._run_bulk_scrape_job` via `_record_item` on every
outcome) with the human-readable reason `_run_bulk_scrape_job` recorded for
each (`"no TMDb results for any tried title/year"`, `"TMDb is
rate-limiting this Drone (429), retries exhausted"`, etc.). Failed items
get a per-row **Retry** and a bulk **Retry all N** button
(`metadata_manager.retry_bulk_scrape_items`, `POST
/admin/movies/scrape/bulk/retry` with either `{status: "failed"}` or an
explicit `{entry_keys: [...]}`) — this reuses the same
`_run_bulk_scrape_job` machinery scoped to just that candidate set, and
deliberately does **not** clear the items table first, so a retry updates
just the retried rows in place while everything else from the last run
stays exactly as it was. This exists because a real 2,667-movie run once
came back "1,054 matched, 1,576 failed," and the actual cause turned out to
be TMDb rate-limiting cascading through the old "any `TmdbUnavailableError`
mid-job marks every remaining candidate failed and stops" handling — not
1,576 genuinely unmatchable movies. Two things now make that specific
failure mode rarer *and* recoverable: `TmdbClient` retries a 429 with
backoff (honoring `Retry-After`) before giving up
(`TMDB_MAX_429_RETRIES`), and the bulk job pauses briefly before each
TMDb-touching candidate (`_throttle_before_tmdb_call`,
`_REQUEST_THROTTLE_SECONDS`) so a large library doesn't trip rate-limiting
in the first place — but when a run *does* still hit a wall, "Retry all
failed" is the recovery path instead of a full rescan-all.

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
