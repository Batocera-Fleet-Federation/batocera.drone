# Multiplayer

Drone Multiplayer lets trusted Drones in your swarm play the same game together:
you pick a game, invite paired peers, Drone chooses the mechanism that the
installed emulators genuinely support, checks compatibility, prepares the
session-specific settings, launches the game and cleans everything up
afterwards.

It is **peer-to-peer and decentralised**. There is no multiplayer server, no
account and no coordinator: any paired Drone can host a session, and every
Drone only ever changes its own state in response to an authenticated, explicit
action.

> **Validation status — read this first.** Everything below is implemented and
> covered by automated tests that run against a *simulated* swarm (in-process
> Drones, scripted machines, a fake launcher). No emulator was started and no two
> real Batocera machines played together in the course of writing this. Real
> detection is exercised by installed-binary checks in
> `tests/test_multiplayer_real_environment.py`. The acceptance benchmark
> (RetroArch between two real Batocera systems, then Dolphin) is still to be
> performed by a person — see [Manual hardware validation](#manual-hardware-validation).
> Nothing is marked *verified* until a session you confirmed worked.

## Contents

1. [Architecture](#architecture)
2. [Supported emulators](#supported-emulators)
3. [Mechanisms and how one is chosen](#mechanisms-and-how-one-is-chosen)
4. [Host a game](#host-a-game) / [Join a game](#join-a-game)
5. [Invitations and the session protocol](#invitations-and-the-session-protocol)
6. [Networking, LAN and Tailscale](#networking-lan-and-tailscale)
7. [Game matching](#game-matching)
8. [Compatibility classes and the database](#compatibility-classes-and-the-database)
9. [Temporary emulator configuration](#temporary-emulator-configuration)
10. [Controllers](#controllers)
11. [Remote streaming](#remote-streaming)
12. [Settings](#settings)
13. [API](#api)
14. [Reliability and recovery](#reliability-and-recovery)
15. [Security considerations](#security-considerations)
16. [Troubleshooting](#troubleshooting)
17. [Adapter development guide](#adapter-development-guide)
18. [Manual hardware validation](#manual-hardware-validation)
19. [Dependencies, disabling and removal](#dependencies-disabling-and-removal)

## Architecture

```
 Browser (#multiplayer)                       Browser (#multiplayer)
        │  /v1/api/admin/multiplayer/*               │
 ┌──────▼─────────────┐   mTLS /peer/multiplayer/*  ┌▼───────────────────┐
 │  Drone A (host)    │◄───────────────────────────►│  Drone B (guest)   │
 │  MultiplayerOrchestrator                         │  MultiplayerOrchestrator
 │  ├ SessionStore  (SQLite, versioned)             │  ├ SessionStore (replica)
 │  ├ ConfigGuard   (journaled settings)            │  ├ ConfigGuard
 │  ├ Adapters      (RetroArch, Dolphin, …)         │  ├ Adapters
 │  └ GameLauncher  (EmulationStation API)          │  └ GameLauncher
 └──────┬─────────────┘                             └──────┬─────────────┘
        │ emulator gameplay traffic (RetroArch/PPSSPP/…) — separate transport
        └──────────────────────────────────────────────────┘
```

* **Control plane** — session coordination travels over the Drone's existing,
  cert-pinned mTLS peer channel (`/v1/api/peer/multiplayer/*`), on LAN or
  Tailscale, with the same address failover every other swarm call uses. Nothing
  new is trusted: the caller's identity is the pinned certificate, never a field
  in the body.
* **Data plane** — gameplay traffic is whatever the chosen emulator speaks
  (RetroArch Netplay TCP, PPSSPP's ad hoc server, a Sunshine/Moonlight stream, …).
  It is deliberately *not* tunnelled through the HTTPS API.
* **One authority per session** — the host. Guests hold read-only replicas that
  only move forward (monotonic `version`), so stale or duplicated updates cannot
  overwrite newer state. Every guest action is a request to the host.

Code lives in `app/multiplayer/` (see the layout in `CLAUDE.md`), HTTP in
`app/web/handlers_multiplayer.py`, UI in `app/web/static/js/multiplayer.js`.

### Session state machine

```
CREATED → DISCOVERING → INVITING ⇄ CONFIGURING ⇄ WAITING_FOR_PLAYERS ⇄ READY
                                                                      │
                                                                  STARTING ──► FAILED ─► CONFIGURING (retry) / STOPPING
                                                                      │
                                                         RUNNING ⇄ DEGRADED ──► STOPPING ──► COMPLETED
```

Lobby states (`DISCOVERING … READY`) are interchangeable as players come and go;
`CREATED` is never re-entered, `STARTING` only follows `READY`, and `COMPLETED`
is final. Illegal transitions raise `illegal_transition` (HTTP 409) and are
refused by `SessionStore.mutate`.

## Supported emulators

Legend — **Implemented**: Drone configures and launches the session itself.
**Assisted / Manual**: Drone validates and prepares, the players do the listed
steps. **Detection only**: Drone detects the install and says plainly that native
networking is not automated. **Tested** here means *automated tests against a
simulated machine*; **real-hardware validation** is separate and still open for
every row.

| Emulator / mechanism | Systems | Drone does | Status | Automated tests | Real-hardware validation |
|---|---|---|---|---|---|
| **RetroArch Netplay** (`RETROARCH_NETPLAY`) | NES, SNES, Genesis/Mega Drive, Master System, Sega CD, 32X, Neo Geo/FBNeo/MAME, PC Engine, Game Boy family, Atari, Lynx, … | Detects RetroArch and the core, checks deterministic save states, compares core + core version, writes per-ROM `netplay.*` keys in `batocera.conf`, launches via EmulationStation, restores | **Implemented** — *automated launch is only claimed after the installed `configgen` sources are found to contain every `netplay.*` key*; otherwise manual guidance | simulated | **not yet** (primary benchmark) |
| **Dolphin Netplay** (`DOLPHIN_NETPLAY`) | GameCube, Wii | Detects version, requires equal versions, reads disc ID of plain `.iso/.gcm`, compares content, lists exact steps | **Manual** — Dolphin has no CLI/RPC to start Netplay | simulated | not yet (second benchmark) |
| **PPSSPP ad hoc** (`PPSSPP_ADHOC`) | PSP | Offers the mechanism only if `ppsspp.ini` has `EnableWlan`, `ProAdhocServer` (+`EnableAdhocServer` to host); edits `[Network]` for the session; launches | **Experimental / assisted or manual** (launcher preservation must be detected) | simulated | not yet |
| **Switch LDN / LAN** (`SWITCH_LDN`) | Switch (Ryubing/Ryujinx; Eden detected only) | Reads emulator settings for a multiplayer mode, enforces same-LAN requirement, lists steps. Does not edit settings, join rooms or touch console auth | **Experimental / manual** | simulated | not yet |
| **Generic LAN** (`EMULATED_LAN`) | PS2, Xbox, DOS/Windows, Dreamcast/Naomi/Atomiswave, GameCube BBA | Connectivity/latency diagnostics, reachable host address, honest broadcast warning | **Experimental / manual** | simulated | not yet |
| **Remote streaming** (`REMOTE_STREAMING`) | any | Detects Sunshine (host) / Moonlight (guest), host-performance gate, bounded quality profile, launches the game on the host | **Experimental / assisted**; host + **one** remote guest | simulated | not yet |
| RPCS3, PCSX2, xemu, Cemu, Flycast, melonDS, Azahar, Vita3K, Xenia, shadPS4 | PS3, PS2, Xbox, Wii U, Dreamcast, DS, 3DS, Vita, Xbox 360, PS4 | Detect install; report native networking as **not automated**; remote streaming is offered instead | **Detection only** — not presented as working | simulated | n/a |

Anything not in this table is **unsupported**. An adapter never advertises a
capability the installed build cannot provide: capability documents
(`GET /admin/multiplayer/adapters`) are computed from what is actually on the
machine (binaries, version output, the emulator's own config keys, `configgen`
sources, libretro core info) and cached for 30 s.

### Emulator-specific limitations

* **RetroArch** — slot assignment is RetroArch's (connection order); Drone sends
  guest launch offers in slot order after the host's port is confirmed listening. Cores without
  deterministic save states are classed *Experimental* and may desync. The host's
  core is pinned for the ROM during the session so both sides emulate identically.
  When no core is set for a game Drone resolves it from `batocera.conf`, then Batocera's
  `configgen-defaults.yml`, then the first installed candidate core — never silently a core that is not
  installed — and the reason says which one was used (`core_source`). A blocker is always listed before
  informational notes so the selector shows the real cause.
  The `netplay.*` per-game keys follow Batocera's launcher; if your build's
  launcher lacks any of them Drone falls back to manual instructions.
* **Dolphin** — the Netplay window is Dolphin's own (Tools → Start NetPlay…).
  Batocera starts Dolphin game-only, so you need Dolphin's desktop interface. Drone
  refuses to start (with *"the host and client are running incompatible Dolphin
  versions"*) when versions differ. Game ID is read only from plain `.iso/.gcm`;
  other containers (RVZ/WIA/GCZ/WBFS) are matched as files with full SHA-256 equality.
* **PPSSPP** — PSP titles differ widely; none is marked verified until a session
  you confirm. Hosting needs the `EnableAdhocServer` key in `ppsspp.ini` (the build
  can run the ad hoc server). A build that only has `EnableWlan` and `ProAdhocServer`
  can join a host but is not auto-selected, and cannot be forced, as the host
  mechanism. A stored value of `False` still counts: the key is present, and the
  session turns the server on for the host. The PRO ad hoc server port is 27312. Settings are global in PPSSPP, so
  the `[Network]` keys change for the session and are restored afterwards **only if**
  installed configgen sources are readable and do not overwrite those networking
  keys. Otherwise Drone gives manual setup instructions and performs no launch/edit.
* **Switch** — "local multiplayer" does not imply LDN support; it is per game and
  per emulator. LAN discovery uses broadcast, which Tailscale does not carry, so
  peers reachable only over the tailnet are refused for this mechanism. Eden's
  multiplayer uses room servers configured inside the emulator.
* **Generic LAN** — emulated consoles discover each other by broadcast; a routed
  link cannot carry that. Drone ships **no relay** (`RelayProvider` is the isolated
  seam; `relay_provider` can only be `none`). UDP reachability cannot be proven by a
  connection test and is reported as unverified.
* **Streaming** — see [Remote streaming](#remote-streaming).

## Mechanisms and how one is chosen

Mechanisms: `RETROARCH_NETPLAY`, `DOLPHIN_NETPLAY`, `PPSSPP_ADHOC`, `SWITCH_LDN`,
`EMULATED_LAN`, `EMULATOR_NATIVE_NETWORK`, `REMOTE_STREAMING`.

`multiplayer/mechanism.py` evaluates every candidate for the game against the host
**and every invited peer**:

1. Blockers: unsupported/incompatible classification, missing install or unsuitable
   build, configuration still needed, a peer without a compatible copy (per the
   mechanism's content rule), a peer reachable only over Tailscale when LAN broadcast
   is required, streaming switched off or Moonlight missing.
2. A manually forced mechanism (*Advanced* in the host dialog) is honoured only if it
   passes — otherwise the request is refused with the reasons. **Drone never
   silently substitutes an unsupported mechanism.**
3. Otherwise your saved preference if usable, else the best **native** mechanism by
   compatibility class, else remote streaming — only when no native mechanism works.
4. The reasoning is stored (`mechanism_reason`) and shown in the lobby, including
   which native options were ruled out and why.

Peer emulator versions are compared up front (RetroArch major.minor and core version, Dolphin exact version). When another mechanism such as streaming can serve the group, a native mechanism whose version the peer cannot join is ruled out (also when forced); with no alternative it stays selectable with a warning and the guest is refused at join with the specific reason.

Adding a player can never silently change the mechanism under players who already
joined (`mechanism_change`, HTTP 409).
Before anyone joins, a mechanism change refreshes all pending invitations and the
host's configuration evidence. Game-detail recommendations ask peers about the
host's selected RetroArch core, even when their own default core differs.
Pending invitations reserve player capacity. If adding a peer would select a
mechanism with fewer seats than the resulting roster needs, Drone refuses the
invite with `session_full` and preserves the existing mechanism and invitations.
Remove pending players first, or create a session for a smaller group. Acceptance
and launch also reject overfilled lobbies retained from an older installation;
slot allocation never exceeds the published capacity.

## Host a game

1. Open **Multiplayer** → **Games**, choose a system. The table shows each game's
   player count (from `gamelist.xml`), a status, per-mechanism compatibility and
   which online peers own a matching copy.
2. **Host…** on a game. The dialog explains, per mechanism, whether it is usable and
   why, and lets you tick online peers to invite. *Advanced* lets you force a
   mechanism or pick a RetroArch core for this session only.
3. **Create session.** Drone checks the game file, asks each peer whether and how it
   can play, chooses the mechanism, and sends invitations.
4. In the **lobby** the host can invite more peers, assign player slots (assigning a
   taken slot swaps), remove players and watch connection state/latency. Set
   yourself ready.
5. When everyone is ready, **Play Together**. If another game is running on the host,
   Drone asks you to approve closing it first (`game_running`), including when that
   process is already this ROM: host Netplay settings are applied by launching again.
   A failed start does not keep the only Host Game slot. **Retry** still recovers that
   session until you host a different game or end it; hosting another game replaces it.
6. Drone applies the session-scoped settings, launches, waits until the emulator is
   really up and listening, then asks each guest (in slot order) to launch.
7. **End session** closes the game Drone started, restores settings and tells guests.
   Afterwards you can record whether it *worked* (see
   [Compatibility](#compatibility-classes-and-the-database)).

## Join a game

An invitation arrives in the bell and under **Multiplayer → Pending invitations**
(and the **Join Game** button). **Accept** first validates *this* machine: do you
have a matching copy, is the emulator/core installed with a compatible version, is
the mechanism enabled? If not, you get the specific reason and nothing is sent.
Accepting joins the lobby only — **no game starts**. When the host starts, you get a
*Launch on this Drone* button. If a *different* game is running here you must approve
replacing it. If this session's game is already in the foreground, confirming keeps
that process; Drone does not close it to launch it again. A host can never launch,
or close, anything on your Drone. Leaving the lobby, or declining, does not block a
later **Invite Peer**: that is a new invitation you accept or decline. Joining again
without that new invitation does not restore membership.

Optional, opt-in only: *Privacy → Launching a game here → Automatic* launches
without asking **when no other game is running**. *Automatically accept
invitations* (off by default) only joins the lobby.

Queued execution retains whether consent was automatic or explicit. Switching to
confirmation-only revokes queued automatic consent; disabling multiplayer revokes
both kinds. The offer then needs fresh local confirmation. Re-enabling a setting
does not revive revoked consent. Policy writes and guest resource acquisition
share a lock: a settings change waits for an execution already acquiring resources
to finish, and cannot interrupt an emulator launch that has already begun.

## Invitations and the session protocol

Peer endpoints (mTLS; caller = pinned certificate identity):

| Endpoint | From → to | Purpose |
|---|---|---|
| `POST /peer/multiplayer/match` | any → any | "Do you own this game, and how can you play it?" (≤ 40 queries) |
| `POST /peer/multiplayer/invite` | host → guest | Invitation (session ID, host, game, emulator requirements, mechanism, capacity, expiry) |
| `POST /peer/multiplayer/respond` | guest → host | `accept`, `decline`, `leave`, `ready`, `not_ready`, `launch_result` |
| `POST /peer/multiplayer/update` | host → guests | Newer session snapshot (version-gated) |
| `POST /peer/multiplayer/launch` | host → guest | Launch *offer* (port + one-time session credential); needs local confirmation |
| `GET /peer/multiplayer/status?session=` | any member | Liveness / state. The host recovers an unreachable player only when this reply shows the player still holds the session and (once live, for mechanisms that launch games) has its game running; an answering Drone alone is not participation. Ending a session also pushes a withdrawal to invitees who never joined. A fresh host `invite` (not a replay of the old message) renews an expired, withdrawn, or declined invitation, and an accepted one after the guest has left. It does not reopen an invitation the guest is still in, and accepting the previous invitation does not rejoin. |
| `GET /peer/multiplayer/compatibility` | any → any | Locally confirmed compatibility reports (see below) |

Rules enforced on every message: paired caller; `source_drone_id` (if present) must
equal the certificate identity; random `message_id` + `issued_at` inside a ±15 min
window, duplicates are reported as `duplicate` and never re-applied; session
membership; host-only actions; strictly newer `version` for snapshots; snapshots are
**rebuilt field by field** from untrusted JSON (bounded text, enumerated states,
validated identifiers) — a raw peer object is never stored; a joined guest rejects
updates that change the game, emulator, core or mechanism it agreed to.

Invitation lifetime = `invitation_ttl_minutes` (default 15) and never beyond the
session's own expiry. The receiver caps pending invitations at 20, honours
*Invite permissions* (any paired / allow-list / nobody) and *Accept invitations*.

An `accept` response request includes `reviewed_session`, the bounded snapshot the
guest validated. The host checks its game, emulator, mechanism and capacity under
the session lock before changing membership. Missing review evidence is refused
with `invitation_review_required`; changed identity is refused with
`session_identity_changed`, leaving the player invited. Invite again to refresh a
lost invitation. Roster-only version changes do not invalidate consent, and delayed
older invitations or withdrawals cannot overwrite a newer pending invitation.

The per-session credential (RetroArch Netplay password) is generated by the host,
sent only inside the mTLS launch offer, kept out of every API view, audit event and
log, and deleted when the session ends or fails.

## Networking, LAN and Tailscale

* Reachability, latency (median of three) and the route (`lan` / `tailnet` /
  `public`) come from the swarm's normal peer client (`/peer/health`), so LAN and
  Tailscale peers behave exactly as for transfers. **Connectivity diagnostics** on
  the Settings page and per session.
* The address an emulator client dials is the one *your* Drone already uses to reach
  the host (never an address the host supplies in a message), validated before it is
  written to any setting.
* Game ports are **session-scoped**: RetroArch picks a free port in the configured
  range (default 55435–55535) per session; nothing is opened persistently and Drone
  never edits a firewall. Batocera has no firewall by default, so the emulator's
  listener is reachable on the host's network for the session's lifetime and is
  protected by the per-session password — keep the swarm on networks you trust.
* **Tailscale** carries unicast TCP/UDP, so RetroArch Netplay, PPSSPP's ad hoc server,
  Dolphin direct connections and Sunshine/Moonlight work. It does **not** carry LAN
  broadcast, so mechanisms that rely on it (Switch LDN, many emulated-LAN games) are
  refused for tailnet-only peers instead of failing mysteriously. Tailscale relayed
  (DERP) paths add noticeable latency — Drone warns above ~120 ms.
* Cross-network play needs a shared tailnet (recommended) or a port-forwarded peer,
  exactly as for transfers.

## Game matching

Reuses the Drone's own ROM cache and gamelist metadata — no second library — and the
**already cached** sampled fingerprints (`sample-fp-v1`). Browser matching never hashes the library; a game without a cached fingerprint
is matched by name and size and marked *unverified*. Listing is paginated and per
system. RetroArch and Dolphin additionally hash the **selected** ROM with full SHA-256
on session creation and acceptance; different content cannot join. Hashes are cached
by file identity, size and timestamps, invalidated on changes, and bounded to 512
entries. A hash has a 30-second budget; multi-file game sets are not yet supported by
this verifier.

| Level | Meaning |
|---|---|
| `exact` | cached fingerprints equal |
| `same_file` | same file name and size, fingerprint not available to compare |
| `title` | same normalised title (region/revision tags stripped), different content |
| `none` | no copy |

Requirements per mechanism: RetroArch and Dolphin need a candidate match plus full SHA-256 equality; PPSSPP and generic LAN accept `title`; Switch LDN requires a matching copy; **streaming needs the game on the host
only**. Multiplayer capability and player counts come from `gamelist.xml` `players`;
a console being multiplayer-capable never makes a game verified, and an unknown
player count yields *Untested*.

## Compatibility classes and the database

`VERIFIED_COMPATIBLE`, `LIKELY_COMPATIBLE`, `REQUIRES_CONFIGURATION`,
`EXPERIMENTAL`, `INCOMPATIBLE`, `UNSUPPORTED`, `UNKNOWN`. The adapter produces a
*technical* class (e.g. a deterministic core ⇒ Likely); the database (`multiplayer`
namespace of the state DB, works offline, tiny bundled seed in
`app/multiplayer/data/compat_seed.json`) overlays evidence:

* After a session you press **It worked / It didn't**. That is recorded against the
  *tested configuration fingerprint* (mechanism, ROM fingerprint/name/size, emulator/core
  versions and inspected Batocera settings). This is a local user report, not an
  exhaustive proof of BIOS, in-game settings or all emulator overrides. Before launching,
  Drone reinspects the local configuration; if it changed since the lobby was reviewed,
  create a new session. This check also runs when a queued launch worker starts.
  `VERIFIED_COMPATIBLE` is reported only while the current configuration matches a
  tested one with more successes than failures — one success is never universal proof.
* Failed starts record an orchestration diagnostic automatically, without changing
  gameplay success/failure counts. A launcher outage, missed confirmation or Drone
  restart does not establish that a game is incompatible. A user-confirmed **It
  didn't** result after play still counts against that tested configuration.
* Reports shared between Drones (`GET /peer/multiplayer/compatibility`, imported via
  *Multiplayer → compatibility import*) keep their `peer:<id>` attribution, never
  overwrite what this machine observed, and are capped below verified. The first local
  result starts fresh local evidence instead of promoting imported successes.
* Browser filters: *Ready to Play* (usable + Verified/Likely), *Configuration
  Required*, *Untested* (Unknown/Experimental), *Unsupported*.

## Temporary emulator configuration

`multiplayer/config_guard.py` changes only the keys a session needs, in files Drone
does not own (`batocera.conf` per-ROM keys; PPSSPP `[Network]`):

1. validate the target (inside userdata, no symlinks), keys and values (no control
   characters, no `=` or quotes in a ROM name, bounded length);
2. record each original value in a **write-ahead journal** (state DB) and back the
   file up;
3. write atomically, re-read and verify; roll back on any mismatch;
4. restore on end/failure/leave — a key the user changed in the meantime is left
   alone; an interrupted run is finished by `recover()` at the next start.

Edits preserve unrelated bytes, including Unicode separators inside values and
ROM names, CRLF line endings and an unterminated final line. New journal entries
retain the exact original owned line so restoration also preserves its spacing
and spelling, without replacing concurrent user edits elsewhere in the file.
Older journals containing only values remain recoverable with value restoration.

RetroArch also journals the expected join-password fields in configgen's generated
`retroarchcustom.cfg` before launch, even if that file does not exist yet. This is a
watch-only operation: Drone does not create or overwrite the generated profile.
Cleanup removes/restores only its matching credential fields and preserves all other
settings. Different sessions cannot own the same configuration keys simultaneously.

*Settings → Restore emulator settings automatically* can be turned off for
inspection; use the session's **Restore** (`POST …/restore-config`) afterwards. Drone
never changes controller mappings, audio, video or other global settings.

## Controllers

Detection reads the kernel's joystick-class input devices (read-only; it never
enumerates USB). The lobby shows each player's controller count; slots are assigned
by the host. Batocera's permanent pad mapping is never rewritten: native netplay
slot order is the emulator's. Drone sends launch offers in requested slot order,
but actual slots depend on when players confirm/connect; verify assignments in the
emulator. Drone creates
no virtual input devices; for streaming, Sunshine's own virtual gamepads receive the
guest's Moonlight controllers. *Privacy → Remote controllers* disables new Drone
streaming invitations/launches. It does **not** revoke existing Sunshine pairings,
enforce individual virtual-pad slots, or disconnect streams. Configure Sunshine for
controller-only input (disable keyboard/mouse), authorise pairing locally, and disconnect
Moonlight when leaving. These external permissions are outside Drone's control.

## Remote streaming

Drone does **not** implement video, audio or input transport. It integrates:

* **Sunshine** (host) — captures and encodes (VAAPI/NVENC when the GPU has them,
  software otherwise) and exposes the guest's controllers as virtual gamepads;
* **Moonlight** (guest) — the client.

Drone detects both, refuses a host with neither a hardware encoder nor ≥ 4 CPU
cores, launches the game on the host, bounds the recommended profile by your
Streaming settings (resolution, FPS, bitrate, encoding preference) and shows it to the
guest. **Remaining manual step:** the one-time PIN pairing in Sunshine's web UI
(Drone holds no Sunshine credentials and does not bypass its authentication).
Drone conservatively limits its streaming lobby to the host plus one remote guest;
this is a Drone limit, not a claim about Sunshine's client capacity. Hardware encoder
probes are hints, not codec validation. Pairing, stream connection/termination and
virtual controller mapping remain manual in Sunshine/Moonlight.

### Installing Sunshine from Drone

Batocera does not ship Sunshine, so **Multiplayer → Settings → Remote streaming** has an
**Install Sunshine** button (and **Start** / **Uninstall**). The host dialog links there when
Sunshine is the only thing blocking streaming. What it does, in a background job with a progress bar:

1. Looks up the latest release of `LizardByte/Sunshine` over HTTPS and picks the AppImage for this
   machine's architecture (x86_64 or aarch64; anything else is reported as unsupported).
2. Downloads it, and **refuses to install** unless the file matches the SHA-256 digest GitHub's release
   API publishes (no digest, wrong size, or a download URL outside the official repository are all refused).
3. Unpacks it with the AppImage's own `--appimage-extract` (no FUSE, nothing mounted) into
   `/userdata/system/drone-multiplayer/sunshine/` and writes one launcher plus one Batocera service,
   `/userdata/system/services/DRONE_SUNSHINE`, then enables and starts it. Both survive reboots and Drone updates.

**Uninstall** stops the service and deletes exactly those two things (a same-named service Drone did not
write is never touched, and a Sunshine Drone did not install is never removed). Sunshine's own settings and
client pairings under `/userdata/system/.config/sunshine` are kept. Stop/uninstall are refused while a
streaming session is active. After the first start, open Sunshine's web UI on the machine and set its
username/password straight away — Drone never sees or stores them. Install needs internet access on the Drone.
Service ownership is recorded in an HMAC-authenticated receipt, signed with a private installation key
(`sunshine/.service-key`, mode `0600`) created only after the verified application is installed. The receipt
binds the service's path, filesystem identity and contents and survives Drone restarts. Copied comments,
copied service files and public SHA-256 sidecars cannot authorize start, stop or removal. The receipt key
is local installation state, never shared with peers, and is removed with the managed application.
Legacy checksum-only receipts and missing/invalid keys are not automatically adopted: the service is
left alone for administrator inspection. Remove a conflicting service manually after reviewing it, then
reinstall Sunshine through Drone to establish new provenance. This does not protect against a root
administrator who can alter Drone's code or private installation state.
**Moonlight** (the guest side) is not installed by Drone; set up Batocera's Moonlight client on the guest.
Like every other streaming claim here, the installer is covered by simulated tests only: a real install on
x86_64 and aarch64 Batocera hardware is still to be validated.

## Settings

**Multiplayer → Settings**: General (enable, invitations, auto-accept — off by
default, preferred mechanism, max simultaneous sessions, session timeout, invitation
lifetime, auto-restore), Networking (transport preference, session port range,
relay `none`, connectivity test), Emulators (detection, readiness, per-adapter
enable, core overrides per system, *Re-detect*), Streaming (enable, quality,
max resolution/FPS/bitrate, encoding), Privacy (invite permission + allow-list,
local launch authorization — *confirm* by default, remote controller authorization,
audit logging). All values are validated and bounded server-side; unknown keys are
rejected.

## API

All under `/v1/api`; full schemas in the OpenAPI document.

Browser API (`/admin/multiplayer/…`) — reads use the normal session gate; **every
mutation needs a real login, `admin_enabled`, a same-origin `Origin` and a JSON
body** (the same hardening as Integrations):

`GET ""` dashboard · `GET adapters[/{id}]` · `POST adapters/refresh` · `GET peers` ·
`GET games/systems|shared|games?system=…&q&mechanism&players&status&peer&include_peers&limit&offset|games/{system}/{id}` ·
`GET|POST sessions` · `GET sessions/{id}[/diagnostics]` · `POST sessions/{id}/invite|assign-slot|remove-player|ready|start|launch|retry|leave|end|result|restore-config` ·
`GET invitations` · `POST invitations/{id}/accept|decline` · `POST join` · `GET history` ·
`GET|POST settings` · `POST connectivity/test` · `GET compatibility` · `POST compatibility/import`.

Errors are structured: `{"error": "<readable message>", "code": "<stable code>", "details": {...}}`
(e.g. `game_running`, `cannot_join`, `no_mechanism`, `mechanism_unusable`,
`illegal_transition`, `stale_message`). No endpoint accepts a command line, a path or
a raw configuration key.

## Reliability and recovery

* **Idempotent** creates (`client_request_id`), invites, accepts, ends and replayed
  messages.
* **Timeouts**: invitations (15 min), sessions (4 h default), launch confirmation
  window (5 min), launch deadline (120 s), bounded retries (3), bounded waits for the
  emulator to start/listen.
* **Launch offers**: confirmation checks the host's current session and membership
  over the existing pinned peer channel. Queued guest workers check again before
  applying configuration or launching. A failed/ended host or a return to the lobby
  voids the offer; an unreachable host blocks confirmation until it answers. A
  guest restart can retain an unconfirmed offer while the host is still starting.
  Confirmation also requires the host to still expect that player within its
  launch deadline. Each start and delivered retry is a launch *wave*: a success
  report must carry that wave, and a late report after the deadline or from an
  earlier wave cannot revive a failed player. Recovery is a host retry that
  opens a new wave and a fresh offer.
  A transport miss while sending `/launch` is not a launch failure. The guest
  stays expected until the deadline; housekeeping resends the current wave when
  they return (local confirmation still required). A sole missed POST must not
  abort an already-started host game. Retrying while a player is unreachable
  does not spend the delivered-offer retry budget.
* **Housekeeping** (`multiplayer/worker.py`, one daemon thread, 10 s sweep): expires
  invitations/sessions, probes silent players (→ `unreachable`, session `DEGRADED`,
  recoverable), ends a guest's session when the host stays unreachable for 2 minutes
  (**host migration is not assumed**: emulator sessions can't move hosts), ends the
  session when the host's game exits, makes a guest leave when its own game exits,
  retries failed publishes.
  Reconnecting restores the player's previous participation state: a pending guest
  still needs to launch, while a previously running guest must report the session
  game running. `settle_live` treats leave/remove as roster changes: remaining
  running players are a healthy `RUNNING` session.
* **Restart recovery** (`recover()`): restores orphaned journals, fails sessions that
  were `STARTING`, resumes ones whose game is still running, finishes `STOPPING`.
  A guest whose automated session game has exited leaves and restores its settings
  immediately. An interrupted guest launch reports failure to the host immediately;
  undelivered recovery reports are retained locally and retried by housekeeping.
  Manual and streaming participation is not inferred from a local emulator process.
* **Cleanup failures**: adapter shutdown, game shutdown, relay cleanup and configuration
  restoration are attempted independently. Guest launch failures are saved before cleanup,
  so a restoration outage cannot leave a consumed offer stuck as launching. Restart recovery
  also attempts restoration when guest process shutdown fails. Failures appear in diagnostics; if game
  shutdown fails, close the remaining game locally. Housekeeping retries abandoned
  configuration journals up to three times, with exponential backoff starting at 30
  seconds between retries. The retry budget survives restarts of the guard and remains
  in the journal on failure. Manual **Restore** remains available after the budget is
  exhausted. Recovery also runs with multiplayer disabled, but respects the automatic
  configuration restoration setting and leaves live-session settings alone.
* Disabling an emulator adapter blocks new launches, including a host launch queued
  before the setting changed. Existing sessions can still be ended and cleaned up.
* A remote end never closes a guest's game; the guest is told the session is over and
  closes it when ready.

## Security considerations

* Only paired peers (pinned certificate) can speak the protocol; identity comes from
  the certificate. Unknown machines are never trusted; discovery grants nothing.
* Explicit consent everywhere: invitation acceptance, launch confirmation, replacing
  a running game — on the machine concerned. Defaults are the safest values.
* Private keys are never exchanged. The session credential is per session, never
  logged, not in API views, deleted at the end.
* Inputs are bounded/allow-listed before use; peer snapshots are rebuilt; emulator
  names are plain tokens; hosts written to settings are validated; ROM names are
  rejected if they could break a key; only fixed `argv` lists are run (`shell=False`);
  the game is launched through EmulationStation's loopback API.
* Config edits are confined to the userdata tree, refuse symlinks, are journaled,
  atomic and verified.
* DoS bounds: players ≤ 8, events/history bounded, ≤ 20 pending invitations, ≤ 40
  match queries, replay window pruned.
* Audit: session events go to the audit log (`multiplayer_session`) and
  invitations/launch offers to the bell (`multiplayer_invitation`); neither is
  emailed. *Privacy → audit logging* disables the audit rows.
* Cloned disks: sessions, invitations, history, the replay window, the invite
  allow-list and imported compatibility reports are cleared by `swarm_reset`, after
  restoring any journaled settings. Nothing is announced to the original's swarm.
* Residual risk to be aware of: the emulator's own listener (e.g. Netplay) is
  reachable on the host's network while a session runs.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "No multiplayer mechanism is usable…" | Open the game's *Host…* dialog: it lists each mechanism's blockers (missing game on a peer, no core selected, core not installed, Sunshine not running, …). |
| RetroArch game blocked: "No known RetroArch core … is installed" / "core … is not installed" | Install the core from Batocera's Main Menu → Updates & Downloads → Content Downloader (Drone does not install cores). Cores in `/usr/lib/libretro` and `/userdata/cores` are both detected. |
| A RetroArch game has no core set | Not a problem: Drone uses Batocera's own default core for the system (read from its configgen defaults), else the first installed core, for the session only. Pick another under *Advanced* when hosting. |
| Streaming: "Sunshine is not installed on this (host) machine" | *Multiplayer → Settings → Remote streaming → Install Sunshine*. |
| "Core … versions differ" / "RetroArch versions are incompatible" | Update both Drones' RetroArch/core to the same version. |
| "…incompatible Dolphin versions" | Install the same Dolphin build everywhere. |
| Guest: "did not answer" for host port | Peers don't share a LAN/tailnet route, or the host's game exited. Run *Connectivity diagnostics*. |
| Switch/LAN game "reachable only over tailnet" | The mechanism needs LAN broadcast; use the same local network or remote streaming. |
| "outside the accepted time window" | Peer messages must be within ±15 minutes of each other's clocks (replay protection). A Batocera machine with no real-time clock needs NTP/a correct date. |
| Session stuck `STARTING` | The guest hasn't confirmed launch (5 min window) — ask them; or *Retry*. |
| Settings not restored | Check *Restore emulator settings automatically*; use the session's **Restore**; diagnostics lists pending keys. |
| Streaming: guest can't connect | Pair Moonlight with Sunshine (PIN in Sunshine's web UI); confirm Sunshine is running on the host. |

## Adapter development guide

1. Create `app/multiplayer/adapters/<name>.py` with a class extending
   `EmulatorAdapter` (`adapters/base.py`). Set `id`, `name`, `tier`
   (`implemented`/`experimental`/`detect_only`), `automation`, `systems`,
   `mechanisms`, `binaries`, `protocols`, `permissions`, `requirements`,
   `limitations`, `content_match`, `needs_same_lan`, `guest_launches`.
2. **Detect, don't assume.** Implement `_detect_features()` from real evidence (binary
   found, version output, the emulator's own config keys, launcher sources). If the
   evidence is missing return `available: False` with a reason, or lower
   `effective_automation()` to `manual`.
3. Implement `assess_game(game, overrides)` returning `self.assessment(mechanism,
   classification, reasons, emulator={…}, max_players=…)`. Never return
   `VERIFIED_COMPATIBLE` — evidence comes from confirmed sessions (`compat_db`).
4. Implement `validate_configuration(session, role)` (blocking problems, with actionable
   messages), `prepare_session` (ports, steps), `configure_networking` /
   `configure_controllers` (return **edit dicts**, never write files:
   `{"path", "kind": "kv"|"ini", "key", "section", "value"}` — `ConfigGuard` applies
   and restores them), `session_port`, `check_session_health`.
5. Unsupported operations should raise `CapabilityError` with a message a person can
   act on (the base class does for `launch` on `manual` adapters).
6. Register it in `adapters/__init__.py::build_adapters`. The orchestrator, mechanism
   engine, UI and API pick it up without further changes.
7. Tests: add a `FakeProbe`-based test in `tests/test_multiplayer_adapters.py`
   (installed/not installed, feature present/absent, classification, edits). Mark
   simulated results as simulated; do not claim real validation without it.

## Manual hardware validation

Still required before calling any row "verified" (record results with *It worked*):

1. **RetroArch (benchmark 1)** — two Batocera machines, same ROM and core, same LAN:
   host a 2-player SNES game; confirm Drone's launcher recognises the `netplay.*`
   keys (adapter shows *Automation: full*), the guest connects, controls work, **End**
   restores `batocera.conf` (diff against a backup), and that a Drone restart mid-session
   recovers cleanly. Repeat over Tailscale.
2. **Dolphin (benchmark 2)** — same build on both, GameCube title; follow the lobby steps.
3. PPSSPP ad hoc with a known multiplayer title; Switch LAN play with Ryubing on one LAN;
   Sunshine/Moonlight pairing and a streamed session (note latency and Tailscale path).
4. Confirm on a real Batocera that `emulatorlauncher`/`configgen` paths match those the
   adapter searches (`/usr/lib/python3*/site-packages/configgen`), the libretro core/info
   paths (`/usr/lib/libretro`, `/usr/share/libretro/info`) and the PPSSPP ini path.
5. Swarm regression: pairing, membership gossip, transfers and identity provisioning still
   behave (the repository's container swarm tests live in the sibling `.github` repo and were
   not run as part of this change).

## Dependencies, disabling and removal

Drone adds **no** Python dependencies and installs no packages (stdlib only). Optional
tools are the player's own: RetroArch/Dolphin/PPSSPP/Ryubing (Batocera), Sunshine and
Moonlight (streaming). Everything Drone writes is in its SQLite state DB
(`multiplayer` namespace: sessions, invitations, history, settings, compatibility,
config journal), plus transient backups in
`<userdata>/system/drone-app/multiplayer-backups/` (deleted on restore).

Disable: *Settings → General → Enable multiplayer* off (existing sessions can still be
ended; cleanup always works) and/or disable individual adapters. Remove: end all sessions
(or *Restore*), then delete the `multiplayer` rows from `app_state`; there are no services,
tunnels or files to uninstall. The worker thread is part of the Drone process; the
installer/uninstaller need no changes. Upgrades are file-level: the release gates require
`multiplayer.js`, `handlers_multiplayer.py` and `multiplayer/{__init__,orchestrator}.py`.

## Interface evidence and remaining validation

The RetroArch settings follow Batocera's
[libretro generator](https://github.com/batocera-linux/batocera.linux/blob/master/package/batocera/core/batocera-configgen/configgen/configgen/generators/libretro/libretroGenerator.py)
and [configuration generator](https://github.com/batocera-linux/batocera.linux/blob/master/package/batocera/core/batocera-configgen/configgen/configgen/generators/libretro/libretroConfig.py).
Drone still checks the installed sources rather than assuming a Batocera version
exposes those keys. Sunshine's
[input options](https://docs.lizardbyte.dev/projects/sunshine/v0.22.2/about/advanced_usage.html)
are managed by Sunshine, independently of Drone's swarm certificates. The only dependency Drone installs
is Sunshine, and only when you press *Install Sunshine* (see "Installing Sunshine from Drone"); it creates
one `DRONE_SUNSHINE` service and no firewall rules.

Security regression tests in `tests/test_multiplayer_hardening.py` cover hostile
session IDs, changed acceptance snapshots, completed-session resurrection,
launch/end races, replay-window exhaustion, overlapping config ownership,
full-content mismatch, imported-evidence isolation and process ownership.
