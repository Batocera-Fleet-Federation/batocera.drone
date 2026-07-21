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
working. Layout:

- `common/` — settings, device_identity, logging_setup, http_cache, fingerprint, auth,
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
  peer_workers (public-IP probe + health-check/local-net worker threads), peer_download
  (`_download_*_from_peer` direct tier + state helpers), download_manager
  (`DownloadManager` queue + tier dispatch + `_directpublic_fetch`), download_errors
  (`DownloadCancelled`), transfer_files, local_network, network_identity,
  drone_network, drone_tls (`DroneCertificateManager`)
- `web/` — api_routes/ui_routes (handler mixins), route_config, api_models, the FastAPI
  bridge (api_app/api_bridge/openapi_spec), server_tls, the `RomRequestHandler` `_handle_*`
  **mixins** (handlers_peer/content/artwork/network/config/system/downloads/diagnostics/
  es_collections/remote_admin/theme), **plus `static/` + `templates/`**
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
(default `/userdata/{roms,bios,saves}`).

**Extracting more:** move the code to the right subpackage, add the dual re-export to
`drone_api.py`, then **repoint test monkeypatches** — tests patch `app.drone_api.X`,
but if the *caller* of `X` also moved, patch the new module (e.g.
`app.device.device_control._request_volume_service_control`). Find missing deps with
an AST undefined-name check (watch `global`-declared module vars) and, if you lose a
function body mid-edit, recover it from `git show HEAD:app/drone_api.py`. Verify BOTH
import modes (`import app.drone_api` **and** `PYTHONPATH=app python3 -c "import
drone_api"`) + the full suite after each move. Files referenced **by path** in
`app/service_bootstrap.sh` / `scripts/run_now.sh` (main, drone_api,
web/{api_routes,ui_routes,route_config}, set_screen_mode, set_volume,
input_activity_monitor) require updating those scripts in lockstep.

**Deploy staging (important now that `app/` is ~87 modules):** the device path is safe —
`service_bootstrap.sh` sets `DRONE_APP_ARCHIVE_URL=drone-app.tar.gz` and `run_now.sh` stages
the **whole `app/` tree** from that archive (or from a `file://` `DRONE_APP_BASE_URL` via
`copytree`). Both cover every module automatically — **no per-file list to maintain.** The
**legacy individual-file fallback** in `run_now.sh` (the `else` branch when
`DRONE_APP_BASE_URL` is unset) only downloads `drone_api.py` + `web/{api_routes,ui_routes,
route_config}.py` and therefore **cannot stage the multi-module app** — it has been
incomplete since the first extractions and is not used by the device. Don't rely on it; use
the archive or `file://` path. `service_bootstrap.sh`'s `validate_local_app` file list +
import check are a post-deploy sanity gate, not the staging mechanism.

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
identity itself is `DroneCertificateManager` in `transfer/drone_tls.py`). See
`drone-p2p-transfer-security` skill.

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

## Conventions

- **stdlib only** — no new third-party imports; if you reach for one, find a
  stdlib equivalent.
- **UI:** Bootstrap 5.3 dark theme; `table table-sm align-middle` in
  `table-responsive`; always `escapeHtml` user data. Match the existing Drone
  UI's branding/paging conventions. See `bff-ui-theme-functionality` skill.
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
the upstream batocera-emulationstation source), `bff-ui-theme-functionality`.
Consult the matching skill before non-trivial work.
