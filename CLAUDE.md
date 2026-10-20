# CLAUDE.md — Batocera Drone

Guidance for Claude Code when working in **this repo** (the device agent that runs
on each Batocera machine). One of three repos in the Batocera Fleet Federation;
shared infra (`.github/`) is a sibling. The former hub repo (`batocera.overmind/`)
has been **retired** — the fleet is peer-to-peer only now, so a networking change
is generally self-contained to this repo.

## What this is

A **stdlib-only** (`http.server`) web app + SQLite cache. Source in `app/`. Runs on
locked-down Batocera Python — **no third-party deps may be assumed** (no pydantic,
no QUIC, no `cryptography`); everything new must work on the stdlib. Drones make
**outbound connections only**: cert-pinned mTLS to paired peers for direct transfers
(LAN, a shared Tailscale tailnet, or direct-WAN if port-forwarded). Nothing here
exposes a public inbound API.

## Commands

```bash
python -m pytest tests/               # unittest-style tests run under pytest
python -m pytest tests/test_unit.py -k <expr>
python -m pytest tests/test_transport.py    # transport/networking (transport selector, LAN/tailnet)
python -m pytest tests/test_streamdeck_*.py # Integrations + Stream Deck (fake hardware; no device needed)
python app/main.py                    # run the drone locally
```

Tests build `Settings` via `Settings.from_env()` with
`mock.patch.dict("os.environ", {...}, clear=True)`; set `USERDATA_ROOT`,
`ROMS_ROOT`, `BIOS_ROOT`, `SAVES_ROOT`, `DRONE_STATE_DATABASE_FILE`,
`DRONE_DEVICE_ID` to temp dirs (`OVERMIND_DEVICE_ID`/`OVERMIND_LOG_FILE` still work as
back-compat fallback names for `DRONE_DEVICE_ID`/`ACTIVITY_LOG_FILE`, but new tests
should use the current names). Tests use `unittest.TestCase`.

## Architecture

**Web app + package layout (refactor in progress).** `app/` is being decomposed
from one giant `app/drone_api.py` into cohesive subpackages. `drone_api.py` is now a
**compatibility shim** that re-exports the moved names — dual
`try: from .pkg.mod ... except ImportError: from pkg.mod` — so
`from app.drone_api import X` and the flat `python app/main.py` entrypoint both keep
working. In flat-mode modules, import moved subpackages from their top-level package
(for example, `from transfer import swarm_membership`); a bare module import will not
find files nested under a subpackage. Every `except ImportError` fallback must follow this
(`from common.settings import Settings`, never `from settings import Settings`);
`tests/test_module_import_modes.py` rejects bare nested-module names in any such fallback. Layout:

- `common/` — settings, device_identity, hardware_identity + identity_provisioning (clone-safe identity), logging_setup, http_cache, fingerprint, auth,
  runtime_state (shared mutable Event/Lock singletons: `_ROM_METADATA_ACTIVE/_WAKE/_LOCK`,
  `_GAMELIST_WRITE_LOCK`, `_ES_LIFECYCLE_LOCK` — imported by every module that needs
  one; reassigned `*_STARTED` flags/singletons stay in drone_api), http_errors
  (stdlib HTTPError/URLError formatting), self_update
- `storage/` — state_store, rom_metadata_store, saves_store (SQLite)
- `device/` — device_control, system_metrics, automation, system_info
  (`_collect_system_info_payload`), game_activity (gameplay-session detection: ES-log
  parsing, `GameProcessMonitor`, gameplay history), emulator_configs (local admin
  config-file browser: `list_emulator_config_files`/`read_emulator_config_file`),
  tailnet_service, pixen
- `roms/` — scrapers, rom_fs_watcher, gamelist, rom_inventory, rom_metadata_state
  (cache snapshot build + upload-clean marking + status + poll-activity guard),
  rom_scanner (`_poll_rom_metadata_cache` filesystem scan + `_hash_rom_metadata_batches`
  sampled-fingerprint hashing + `_poll_rom_metadata_once`/`_complete_local_rom_metadata_cache`
  — the whole local scan+hash+cache-clean cycle now that there's nowhere to upload to;
  takes `RomRepository` as a param), and the `RomRepository` **mixins** — rom_artwork_apply,
  rom_artwork_gamelist, rom_scan, rom_systems, rom_asset_bios (the class is now a slim
  `__init__` + static delegators composed from these; see the god-class-split note below)
- `transfer/` — peer_connectivity (cert trust/pinning + peer HTTP client + health/pairing),
  swarm_membership (approved-pair roster gossip and removal tombstones over already-paired peers),
  swarm_reset (targeted removal of identity-bound swarm state for a freshly provisioned clone),
  peer_workers (public-IP probe + health-check/local-net worker threads), peer_download
  (`_download_*_from_peer` direct tier + state helpers), download_manager
  (`DownloadManager` queue + tier dispatch + `_directpublic_fetch`), transfer_batch
  (per-batch started/finished bell entries; per-asset rows are audit-only), download_errors
  (`DownloadCancelled`), transfer_files, local_network, network_identity,
  drone_network, drone_tls (`DroneCertificateManager`)
- `integrations/` — the small static registry for optional local extensions, plus
  `shared/` (process runner, script store, Batocera built-in catalog) and `media_keys/`
  (hotkeygen-backed media-key bindings). Stream
  Deck owns isolated tooling/worker, devices, profiles, typed actions, game launching,
  scripts, images, and diagnostics under `<install root>/integrations/streamdeck/`;
  see `drone-integrations-streamdeck`.
- `web/` — api_routes/ui_routes (handler mixins), route_config, api_models, the FastAPI
  bridge (api_app/api_bridge/openapi_spec), server_tls, the `RomRequestHandler` `_handle_*`
  **mixins** (handlers_peer/content/artwork/network/config/system/downloads/diagnostics/identity/
  es_collections/network_share/theme), **plus `static/` + `templates/`**
- `multiplayer/` — unified multiplayer over the swarm (see the Multiplayer section below):
  `models` (states/mechanisms/validators), `store` (versioned sessions, replay window),
  `config_guard` (journaled temporary emulator settings), `adapters/` (RetroArch, Dolphin,
  PPSSPP, Switch, generic LAN, streaming, detect-only emulators), `matching`, `mechanism`,
  `compat_db`, `network`, `launcher`, `settings_store`, `worker`, `identity_reset`, and the
  `MultiplayerOrchestrator` (`orchestrator.py`, composed from `core`/`sessions`/`protocol`/
  `lifecycle`/`library` mixins) and `dependencies` (the one-click, checksum-verified Sunshine installer + `DRONE_SUNSHINE`
  service; nothing else is installed by multiplayer). HTTP: `web/handlers_multiplayer.py`; UI: `web/static/js/multiplayer.js`.
- `transport/` — the outbound P2P stack: `base` (PeerTransport ABC), `selector`
  (`TransportSelector`), `lan` (`LanDirectTransport`), `direct_public`
  (`DirectPublicTransport`), `tailnet` (tailnet address detection). Also imported
  cross-repo as `app.transport` by a `.github` integration test, so it stays at `app/`
  — see the note on that test in the `drone-edge-networking` skill.

**Still in `drone_api.py`** (not yet extracted): `RomRequestHandler` (most `_handle_*`
methods — being split into `web/handlers_*.py` **mixins**; `HandlersPeerMixin` done), the
poller-*starter* threads (they reassign `*_STARTED` flags), the server bootstrap
(`create_server`/`main`/`DroneThreadingHTTPServer` + TLS), and a few aggregators
(`_collect_system_info_payload`, `_get_download_manager`, `_resolve_asset_root`).
`RomRepository` is now a slim query object composed from mixins in `roms/` (split done).

**God-class split via mixins:** extract a cohesive method group into `class SomeMixin:` in
a new module and compose it — `class RomRepository(SomeMixin, ...):`. Methods stay
`self`-bound so call sites (`repo.method()`) and tests (`repo.method`, `patch.object(repo,
…)`) are unchanged — **no test repoints**. Module-level deps (helpers/constants the methods
call that aren't `self.*`) are imported in the mixin module; anything still in `drone_api`
is lazy-imported. First done: `roms/rom_artwork_apply.py`.
`Settings.from_env()` (now `common/settings.py`, re-exported) reads `*_ROOT` env vars
(default `/userdata/{roms,bios,saves,movies,shows,music}`).

**Extracting more:** move the code to the right subpackage, add the dual re-export to
`drone_api.py`, then **repoint test monkeypatches** — tests patch `app.drone_api.X`,
but if the *caller* of `X` also moved, patch the new module (e.g.
`app.device.device_control._request_volume_service_control`). Find missing deps with
an AST undefined-name check (watch `global`-declared module vars) and, if you lose a
function body mid-edit, recover it from `git show HEAD:app/drone_api.py`. Verify BOTH
import modes (`import app.drone_api` **and** `PYTHONPATH=app python3 -c "import
drone_api"`) + the full suite after each move. Files referenced **by path** in
`app/service_bootstrap.sh` / `scripts/run_web_now.sh` (main, drone_api,
web/{api_routes,ui_routes,route_config}, set_screen_mode, set_volume,
input_activity_monitor) require updating those scripts in lockstep.

**Deploy staging (important now that `app/` is ~87 modules):** the device path is safe —
`service_bootstrap.sh` sets `DRONE_APP_ARCHIVE_URL=drone-app.tar.gz` and `run_web_now.sh` stages
the **whole `app/` tree** from that archive (or from a `file://` `DRONE_APP_BASE_URL` via
`copytree`) into a temporary directory, validates version + required payload files, then
overlays onto the live tree. Both cover every module automatically — **no per-file list to
maintain.** A failed or incomplete release download does not replace the installed build
and does not fall back to GitHub codeload. Source/codeload installs require an explicit
development option (`--dev` or `DRONE_APP_DEVELOPMENT=1`). The installer and service
recovery always run with development mode off. The **legacy individual-file fallback** in
`run_web_now.sh` (the `else` branch when `DRONE_APP_BASE_URL` is unset) only downloads
`drone_api.py` + `web/{api_routes,ui_routes, route_config}.py` and therefore **cannot
stage the multi-module app** — it has been incomplete since the first extractions and is
not used by the device. Don't rely on it; use the archive or `file://` path.
`service_bootstrap.sh`'s `validate_local_app` file list + import check are a post-deploy
sanity gate, not the staging mechanism.
The sanity gates deliberately name the separately loaded Integrations UI plus its
backend registry/handler/manager; a release without any one of them is incomplete.
Source/codeload deployments have `app/VERSION=dev`; the self-updater treats any
non-semantic installed version as unordered and converges it to the latest validated
release rather than permanently skipping updates.

**SQLite cache:** `storage/rom_metadata_store.py` + `storage/saves_store.py` persist
scanned asset metadata in the shared state DB (`storage/state_store.py`). Files are
fingerprinted with a sampled hash (`RomRepository.build_fingerprint`, a delegating
static method whose impl now lives in `common/fingerprint.py` — `sample-fp-v1`,
head/middle/tail windows, constant cost; BIOS uses full-file MD5). A `cache_changes`/
pending-changes queue tracks created/updated/deleted so the admin UI's pending-changes
view reflects exactly what changed since the last clean point; whole-set thumbprints
let a peer tell when a re-sync is needed. Schema is created inline with `CREATE TABLE
IF NOT EXISTS` + `_ensure_column`; **never bump applied schema in place** — add
tables/columns idempotently. See `drone-db-management` skill.

**Forever-loop pollers** (automation, outbound-mail worker, VPN/SMTP/tailnet revocation, VPN self-heal,
network-share watchdog) are latched on once per process by `create_server()` and sleep with `time.sleep`, so
they are retired by generation, not by stop event: each captures `runtime_state.poller_generation()` at start and
returns — before any I/O — the first time it wakes to a different one. `drone_api.retire_background_pollers()`
bumps the generation and clears their `*_STARTED` latches; `main()` calls it beside
`stop_local_network_workers()`. A plain HTTP shutdown does not (owner-driven, like the LAN workers). Any
test harness that calls `create_server` should call both on teardown: a leftover poller re-resolves
`DRONE_STATE_DATABASE_FILE` every tick, so it writes into (and re-creates the temp tree of) whatever later test
selected that env. New forever-loop pollers must follow the same capture-and-check shape.

**Poll loop:** `_start_rom_metadata_poller` + `RomFilesystemWatcher` (inotify,
debounced) wake on file changes; `_poll_rom_metadata_once` scans ROM/BIOS metadata,
hashes changed files, then syncs game saves — all purely local (there is no hub to
sync to; a completed local pass just marks the cache clean, see
`_complete_local_rom_metadata_cache`).

**P2P transfer (direct tier):** peers serve assets at
`GET /peer/{roms,bios,saves}/...` (mTLS-gated, path-traversal-safe via
`transfer/transfer_files.py`) and fetch with `_download_*_from_peer` (now in
`transfer/peer_download.py`; cert pinning + peer client in
`transfer/peer_connectivity.py` via `_peer_trust_cafile`, SSL-retry; the mTLS
identity itself is `DroneCertificateManager` in `transfer/drone_tls.py`). The
peer-pinned identity also signs fresh, nonce-bound ROM/BIOS content inventories
(`transfer/signed_inventory.py`, `/peer/content-inventory/{roms,bios}`). Receivers
verify against the persisted pairing pin and full-file SHA-256 for every file,
including folder children, before installing; caller fingerprints alone are not
content authority. Sources without this endpoint fail closed and must be upgraded.
The peer-pinned identity stays stable; `web/server_tls.py` reconciles a separate
hostname/IP-aware HTTPS leaf signed by that identity during startup, including
current LAN and Tailscale addresses. See
`drone-p2p-transfer-security` skill.

## Identity and clone safety (`common/identity_provisioning.py`)

The swarm identity (`drone.crt`/`drone.key`, logical `device-id`, pinned peers) is **bound to
hardware** by `identity/binding.json` (hashed DMI UUID / serials / Pi serial from
`common/hardware_identity.py`; never hostname, IP, lone MAC, disk UUID or `machine-id`).
`main()` calls `ensure_identity()` after logging, **before** `create_server()` — nothing
peer-facing may start earlier. Verdicts: same → no writes; confident different → automatic clone
provisioning (retire old key first, `transfer/swarm_reset.py` targeted SQLite/pin reset,
`tailnet_service.reset_local_node_identity()` — local state only, **never `tailscale logout`**,
fresh random device id, fresh cert via `DroneCertificateManager`, journaled/resumable);
missing/contradictory → `RECOVERY_REQUIRED` with `swarm_networking_allowed()` False (listener,
workers, pollers, `/peer/*` and outbound peer TLS all gated). `_drone_client_ssl_context` checks the hold before its scheme test, so `http://` peer GET/POST/download calls raise `PeerIdentityHold` too (the failover loops re-raise it rather than trying the next address). That includes the pair-by-address peer-info fetch (`_fetch_peer_info`, any scheme) and each live
dial in `_pairing_tls_context`: they raise `PeerIdentityHold` (503 from the handlers) before connecting. Admin overrides (`/admin/identity/*`,
`web/handlers_identity.py`) need a real session **and** password re-entry. Rules: a clone never
broadcasts a removal/rotation for the original; a peer can never force an identity reset
(collisions are isolated + logged via `record_collision`); when you add per-peer state
(table/namespace/file), add it to `swarm_reset.py`. Tests: `tests/test_hardware_identity.py`,
`tests/test_identity_provisioning.py` (fixture hardware trees via `HardwareIdentityProvider(root)`,
disk images via `image_disk`). Resume journals accept `new_device_id` only when it is absent,
`null`, or a valid non-empty Drone id; any other present value is rejected before a fresh id
can be minted. Keep `_choose_device_id()` behavior aligned with `_valid_journal()`. Depth:
`docs/drone-identity.md`.

## Admin credentials (`common/auth.py`)

**There is no universal default password** (the old `batocera`/`linux` fallback is gone — don't reintroduce
one, in code, Docker defaults or tests that assume it). `DroneCredentialStore.load()` returns persisted
PBKDF2 credentials; else the operator's `DRONE_APP_PASSWORD` (env); else `setup_required` where `check()`
matches nothing. `create_server()` calls `ensure_initial_password()`, which generates a random one-time
password, persists only its hash, and writes the plaintext to a 0600 `initial-password.txt` beside the
credentials file (never logged/returned over HTTP; `update()` deletes it; deleting it before the first
change regenerates). Loopback stays pre-authenticated, so the on-device UI can set real credentials. Tests
that need to log in pass `env_password=` or call `update()`.
The legacy credentials path must be file-shaped **and not an existing directory**
(`require_legacy_file_path`, checked in `__init__` and again at the top of `update()` before any write, so a
failed update never leaves new credentials behind); a symlink is accepted wherever it points because `unlink` removes only the link.

## Transport layer (`app/transport/`) — the outbound-only networking

This is **single-source P2P, not torrent-style swarming**: each transfer pulls the
whole asset from **one** paired peer (`DownloadManager` keeps one active download)
— no piece-level multi-peer fetch, no ranking of multiple candidate sources (the
user picks the peer explicitly on the Swarm/Transfers page).

The download path is **transport-agnostic**. `DownloadManager._run_job` calls
`TransportSelector.fetch` (`selector.py`), which tries `PeerTransport`s best-first
and falls through to the next on failure:

1. `LanDirectTransport` (`lan.py`) — same-NAT peer, or a peer reachable over a
   shared Tailscale tailnet; reuses the local mTLS `/peer/*` path.
2. `DirectPublicTransport` (`direct_public.py`) — the peer is port-forwarded/public;
   wraps the existing `_download_*_from_peer` direct-WAN path.

All implement the `PeerTransport` ABC (`base.py`: `usable`/`fetch`,
`DownloadRequest`, `TransferContext`). `tailnet.py` (address detection, no settings
gate — presence of a route is the gate) backs the tailnet half of tier 1.

**Preserved invariants:** the one-active-download queue, progress, cancel, resume,
hash verify, and cached-MD5 behavior are unchanged across both tiers. See the
`drone-edge-networking` skill (covers this transport layer despite the name —
kept for continuity with the retired Edge-networking work it replaced) for the
full selection rules and the note on a sibling-repo test that still imports the
now-deleted Edge relay modules from this repo.

## How the networking fits together

To pull an asset: the user picks a paired peer (or the swarm/transfers page picks
one implicitly for a single known source) → `DownloadManager` enqueues the job →
`TransportSelector` tries LAN/tailnet-direct, then direct-public, reusing
cert-pinned mTLS `/peer/*` on both tiers. There is no relay of any kind — if a peer
is neither on the same LAN/tailnet nor port-forwarded, it is simply unreachable.
**Same-LAN P2P** works out of the box (same-public-IP detection, no config).
**Cross-network P2P** needs either a shared Tailscale tailnet (recommended, zero
router config) or the peer port-forwarded for the direct-WAN fallback.

**Swarm membership** is still that paired-peer list, not a directory. An approved
pairing (`transfer/swarm_membership.py`) is gossiped only to Drones already
paired with the sender, so a join through any member converges on every roster.
A newer removal beats an older add. Discovery and an unpaired caller cannot
insert members, and gossip cannot replace a fingerprint already pinned locally.
Multicast announcements are unauthenticated too: they never rewrite a paired
peer's address/fingerprint (only `last_seen`), and the pairing code is only
sent over a connection pinned to the pinned or operator-confirmed fingerprint
(`_local_pair_peer`, `certificate_fingerprint` on the pair requests).
An operator-typed `http://` pair-by-address is anchored like `https://`: without a pinned record or a confirmed fingerprint `_pair_peer_or_reply` answers 400 and `_local_pair_peer` raises before sending anything, so an on-path attacker cannot get its own certificate pinned (only code-free `tailnet_auto_pair` is exempt).
A tailnet-authorised (code-free) `/peer/pair` request can add a peer but never replace a pinned certificate: `_handle_peer_pair` compares against `_pinned_peer_fingerprints` and answers 409 before writing, so only the pairing code can change a pin.
A peer paired over TLS (`paired_over_tls` on the paired record) is never dialed over plaintext `http://`
(filters in `_peer_address_candidates`/`_peer_address`, refusal in `_local_pair_peer`; the flag is sticky — `save_paired_peer` never lets any writer clear it, and `_handle_peer_pair` answers 409 to a non-https re-pair of such a peer, even with a valid code; set by `_local_pair_peer`, `_handle_peer_pair` and swarm gossip, never by announcements).
Tailnet enrollment follows the same peer-shared path: `bootstrap_tailnet_from_swarm`
runs at startup and is retried every `DRONE_TAILNET_BOOTSTRAP_INTERVAL_SECONDS`
(default 300) while a Drone is not enrolled.
Local-network shutdown joins discovery and peer-health workers before clearing
their generation. If the timeout expires, `stop_local_network_workers()` raises
`TimeoutError` and retains the stopped generation for retry; do not delete peer
state or replace the generation until shutdown succeeds. See `drone-edge-networking`.

## Integrations (`app/integrations/`) — optional local extensions

Admin -> Integrations lists every integration registered in
`integrations/registry.py::build_integration_registry` (static and reviewed — no
plugin loader). Each one owns its lifecycle/status/config/diagnostics/help, keeps
its files under `<install root>/integrations/<id>/`, configures **only this
machine** (never `/peer/*`, never proxied), and must be removable without touching
Batocera or other Drone features. Routes: `/v1/api/admin/integrations/...` in
`web/handlers_integrations.py` (real session cookie — loopback pre-auth is not
enough — plus `admin_enabled`, JSON/multipart bodies, same-origin `Origin`); page:
`web/static/js/integrations.js`.

**Media Keys** (`integrations/media_keys/`) is the second: Batocera `hotkeygen`
stays the key reader and the Drone adds one marker-checked override file mapping
volume/mute/exit/reboot to a generated launcher that runs a standalone dispatcher
(reads bindings from disk at press time; reboot/shutdown need a key hold). It shares
`integrations/shared/` (ProcessRunner, `ScriptStore`, `BatoceraControl` built-in
catalog, file helpers) with Stream Deck. See `drone-integrations-streamdeck`.

**Stream Deck** is the first. Its third-party deps (`streamdeck==0.10.0`, Pillow)
are installed into the integration's private `lib/` (venv/bundled pip, `--target`,
never global pip/setuptools, no `batocera-save-overlay`) and imported **only** by a
supervised child worker (`python3 -m app.integrations.streamdeck.worker`) — the
Drone process stays stdlib-only. Built-ins are stable IDs in a typed registry
(never stored commands); Launch Game is a structured library reference that exits
the running game via the central `exit-game` handler, waits for the process to
actually exit, then launches through EmulationStation's loopback API (`POST
:1234/launch`), one transition at a time; custom scripts are the only editable
executable action (ID-confined files, `ProcessRunner`, no `shell=True`). Tests use
fake devices/runtimes/launchers — no hardware. **Never poll `hid_enumerate` (or the
library's `connected()`) on a timer:** it probes every USB device on the host and made
generic DragonRise gamepads blip every ~3 s (ES controller lights cycling, other features
misbehaving) until v0.1.256 — hotplug detection is sysfs-signature based; see the
skill's Gotchas. Design reference:
`docs/integrations-streamdeck.md`; skill: `drone-integrations-streamdeck`.

## Multiplayer (`app/multiplayer/`) — decentralised session orchestration

Any paired Drone can host; **the host is the only authority** and guests hold version-gated
replicas (`SessionStore.apply_replica`). Coordination is `/v1/api/peer/multiplayer/{match,invite,
respond,update,launch,status,compatibility}` over the existing pinned-cert mTLS channel; the caller
identity is the certificate (`_peer_requester_device_id`), never a body field. Browser API is
`/v1/api/admin/multiplayer/...` (mutations need a real login + same-origin + JSON, like Integrations).
Gameplay traffic is the emulator's own transport, never the HTTPS API. Invariants to keep:

- **Never fabricate support.** Adapters detect from real evidence (binaries, version output, the emulator's own
  config keys, `configgen` sources, libretro info) and lower `effective_automation()` when evidence is missing;
  no code path returns `VERIFIED_COMPATIBLE` except `CompatibilityDatabase.evaluate` after a user-confirmed
  session on the same tested configuration fingerprint. Distinguish simulated tests from real validation.
- **Peer data is untrusted:** snapshots are rebuilt by `sanitize_session`; emulator names are plain tokens
  (`clean_token`); hosts/addresses go through `valid_host`; ROM names are checked in `scoped_key`; a guest uses
  its **own** file name/path (`local.rom_name`/`game_path`); a joined guest rejects changes to game/emulator/core/mechanism.
- **Consent is local:** an invitation never launches anything; a launch offer needs this machine's confirmation
  (unless the user opted into `auto`, which still never replaces a running game); replacing a running game needs
  an explicit `replace_running_game`; a remote end never closes a guest's game.
- Selected RetroArch/Dolphin ROMs require full SHA-256 equality at join (`GameLibrary.content_hash`);
  browser matching uses cached sampled fingerprints only. Hashing is bounded/cached, never library-wide.
  Evidence fingerprints include content/revision and inspected settings; imported reports cannot become local proof.
- Launch/configuration and cleanup serialise on the session lock; the HTTP server owns and joins its multiplayer
  housekeeping worker on close. Never reuse an old worker's stop event or launch after a cancelled offer.
- **Config changes only via `ConfigGuard`** (journal → atomic write → verify → restore; confined to userdata, no
  symlinks). Adapters return edit dicts, they never write files. Launch only via `GameLauncher` (EmulationStation API).
- Per-peer state lives in the `multiplayer` app_state namespace and is cleared by
  `multiplayer/identity_reset.py` from `transfer/swarm_reset.py` — keep that in sync when adding state.
- New emulator = new module in `multiplayer/adapters/` + one line in `build_adapters` (see `docs/multiplayer.md`).
- Tests use `tests/multiplayer_support.py` (fake machine/swarm/launcher). Release/staging gates require
  `multiplayer.js`, `handlers_multiplayer.py` and `multiplayer/{__init__,orchestrator}.py`.

Skill: `drone-multiplayer`. Depth: `docs/multiplayer.md`.

## Conventions

- **stdlib only** — no new third-party imports; if you reach for one, find a
  stdlib equivalent.
- **UI:** Bootstrap 5.3 dark theme (`drone.css` `--admin-*` tokens). This app
  never sets `data-bs-theme="dark"`, so Bootstrap light defaults would otherwise
  win. New markup must use `themed-table`, `themed-modal` + `btn-close-white`,
  and `themed-accordion`; always `escapeHtml` user data. Nested dialogs (help
  from an editor) need `sd-modal-nested`. See `bff-ui-theme-functionality`.
- `drone_api.py` is being **actively decomposed** into the `app/` subpackages above
  (via the re-export shim). Land new code in the fitting subpackage, not the shim;
  when you touch a cohesive cluster still in `drone_api.py`, prefer extracting it
  (behavior-preserving) over growing the monolith. Match surrounding comment density
  + idiom. See the "Web app + package layout" section for the move recipe.
- Add focused `unittest.TestCase` tests beside existing ones.

## Skills (`.claude/skills/`, auto-surfaced)

`drone-db-management`, `drone-p2p-transfer-security`, `drone-edge-networking`,
`drone-admin-features`, `drone-live-debugging` (debugging a real running Drone via
SSH — logs, the local SQLite state DB, peer-cert trust, reproducing
swallowed-exception peer calls live, and identifying a Drone you can't SSH to
directly), `drone-batocera-emulationstation` (what's Drone-owned vs.
Batocera/EmulationStation-owned state — es_settings.cfg/es_systems.cfg keys,
the stop/write/overlay-save/start EmulationStation restart pattern, the
privileged-worker request/result file dance, and how to find ground truth in
the upstream batocera-emulationstation source), `bff-ui-theme-functionality`,
`drone-integrations-streamdeck` (the shared optional-integration boundary and Stream
Deck local-only lifecycle/action/game/security invariants), `drone-multiplayer`
(session state machine, host-authority protocol, adapter contract, config guard, matching and
compatibility rules, honest-validation rules).
Consult the matching skill before non-trivial work.
