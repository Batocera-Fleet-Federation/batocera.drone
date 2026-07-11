# CLAUDE.md — Batocera Drone

Guidance for Claude Code when working in **this repo** (the device agent that runs
on each Batocera machine). One of three repos in the Batocera Fleet Federation;
the hub (`batocera.overmind/`) and shared infra (`.github/`) are siblings. A
networking change often spans this repo **and** the Overmind repo
(`src/overmind/edge/`) — cross-reference both.

## What this is

A **stdlib-only** (`http.server`) web app + SQLite cache. Source in `app/`. Runs on
locked-down Batocera Python — **no third-party deps may be assumed** (no pydantic,
no QUIC, no `cryptography`); everything new must work on the stdlib. Drones make
**outbound connections only**: a persistent mux to the Overmind **Edge**, plus
cert-pinned mTLS to peers for direct transfers. Nothing here exposes a public
inbound API.

## Commands

```bash
python -m pytest tests/               # unittest-style tests run under pytest
python -m pytest tests/test_unit.py -k <expr>
python -m pytest tests/test_transport.py tests/test_mux_client.py tests/test_relay_transfer.py \
                 tests/test_holepunch.py tests/test_reliable_udp.py    # transport/networking
python app/main.py                    # run the drone locally
```

Tests build `Settings` via `Settings.from_env()` with
`mock.patch.dict("os.environ", {...}, clear=True)`; set `USERDATA_ROOT`,
`ROMS_ROOT`, `BIOS_ROOT`, `SAVES_ROOT`, `DRONE_STATE_DATABASE_FILE`,
`OVERMIND_DEVICE_ID` to temp dirs. `test_download_manager_pushes_terminal_sync_activity`
is order-flaky — re-run or run isolated. Tests use `unittest.TestCase`.

## Architecture

**Web app + package layout (refactor in progress).** `app/` is being decomposed
from one giant `app/drone_api.py` into cohesive subpackages. `drone_api.py` is now a
**compatibility shim** that re-exports the moved names — dual
`try: from .pkg.mod ... except ImportError: from pkg.mod` — so
`from app.drone_api import X` and the flat `python app/main.py` entrypoint both keep
working. Layout:

- `common/` — settings, device_identity, logging_setup, http_cache, fingerprint, auth,
  runtime_state (shared mutable Event/Lock singletons: `_ROM_METADATA_ACTIVE/_WAKE/_LOCK`,
  `_ASSET_/_SAVES_PUSH_REQUESTED`, `_GAMELIST_WRITE_LOCK` — imported by every module that
  needs one; reassigned `*_STARTED` flags/singletons stay in drone_api)
- `storage/` — state_store, rom_metadata_store, saves_store (SQLite)
- `overmind/` — contract/reporting/game_logs/filesystem/collectors + overmind_client,
  overmind_config, registration (token register/claim + action reporting), actions
  (`_execute_overmind_action` dispatcher), heartbeat_sync (heartbeat-drift → resync
  request + local thumbprint readers), saves_sync (`_sync_saves_to_overmind`),
  rom_sync (the ROM-metadata→Overmind poll/upload pipeline: `_poll_rom_metadata_once`,
  `_sync_rom_metadata_to_overmind(_locked)`, `_complete`/`_defer` — param-based on
  `RomRepository`), action_poller (`_start_overmind_action_poller` — the heartbeat +
  action-poll background loop)
- `device/` — device_control, system_metrics, automation
- `roms/` — scrapers, rom_fs_watcher, gamelist, rom_inventory, rom_metadata_state
  (cache snapshot build + upload-clean marking + status + poll-activity guard),
  rom_scanner (`_poll_rom_metadata_cache` filesystem scan + `_hash_rom_metadata_batches`
  sampled-fingerprint hashing; takes `RomRepository` as a param), and the `RomRepository`
  **mixins** — rom_artwork_apply, rom_artwork_gamelist, rom_scan, rom_systems,
  rom_asset_bios (the class is now a slim `__init__` + static delegators composed from
  these; see the god-class-split note below)
- `transfer/` — peer_selection, peer_connectivity (cert trust/pinning + peer HTTP
  client + health/pairing), peer_workers (public-IP probe + health-check/local-net
  worker threads), peer_download (`_download_*_from_peer` direct tier + state helpers),
  edge_relay (Edge mux client + relay/hole-punch tiers), download_manager
  (`DownloadManager` queue + tier dispatch + `_directpublic_fetch`), download_errors
  (`DownloadCancelled`), transfer_files, local_network, network_identity,
  drone_network, drone_tls (`DroneCertificateManager`)
- `web/` — api_routes/ui_routes (handler mixins), route_config, api_models, the FastAPI
  bridge (api_app/api_bridge/openapi_spec), the `RomRequestHandler` `_handle_*` **mixins**
  (handlers_peer/content/artwork/network/overmind/config), **plus `static/` + `templates/`**
- `transport/` — the outbound P2P stack (unchanged; also imported cross-repo as
  `app.transport` by `.github` tests, so it stays at `app/`)

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
head/middle/tail windows, constant cost; BIOS uses full-file MD5). A `cache_changes`/pending-changes
queue tracks created/updated/deleted so uploads send deltas; whole-set thumbprints
are echoed in the heartbeat. Schema is created inline with `CREATE TABLE IF NOT
EXISTS` + `_ensure_column`; **never bump applied schema in place** — add
tables/columns idempotently. See `drone-db-management` skill.

**Poll loop:** `_start_rom_metadata_poller` + `RomFilesystemWatcher` (inotify,
debounced) wake on file changes; `_poll_rom_metadata_once` syncs ROM metadata then
game saves to Overmind. Heartbeat thumbprint mismatch
(`_maybe_request_*_push_from_heartbeat`) queues a resync push.

**P2P transfer (direct tier):** peers serve assets at
`GET /peer/{roms,bios,saves}/...` (mTLS-gated, path-traversal-safe via
`transfer/transfer_files.py`) and fetch with `_download_*_from_peer` (now in
`transfer/peer_download.py`; cert pinning + peer client in
`transfer/peer_connectivity.py` via `_peer_trust_cafile`/`_fetch_peer_certificate`,
SSL-retry; the mTLS identity itself is `DroneCertificateManager` in
`transfer/drone_tls.py`; the Edge mux client + relay/hole-punch tiers are
`transfer/edge_relay.py`). Peer selection in `transfer/peer_selection.py`. See
`drone-p2p-transfer-security` skill.

## Transport layer (`app/transport/`) — the outbound-only networking

This is **single-source P2P, not torrent-style swarming**: each transfer pulls the
whole asset from **one** best peer (`peer_selection.select_best_peer` → one peer;
`DownloadManager` keeps one active download) — no piece-level multi-peer fetch.

The download path is **transport-agnostic**. `DownloadManager._run_job` calls
`TransportSelector.fetch` (`selector.py`), which tries `PeerTransport`s best-first
and falls through to the next on failure:

1. `LanDirectTransport` (`lan.py`) — same-NAT peer, reuses the local mTLS `/peer/*` path.
2. `DirectPublicTransport` (`direct_public.py`) — wraps the existing
   `_download_*_from_peer` (the legacy public-IP path, kept as a tier).
3. `RelayReceiverTransport` (`relay_transfer.py`) — pull via the Edge relay (no
   port-forward). v1 relays ROM files; other asset types fall back to other tiers.

All implement the `PeerTransport` ABC (`base.py`: `usable`/`fetch`,
`DownloadRequest`, `TransferContext`). Supporting modules:

- `mux_client.py` — the persistent outbound Edge connection. `MuxClient` (threaded
  runner, reconnect/backoff, ping) + `MuxSession` (pure protocol core, no I/O) +
  `RelayChannel` (a reliable ordered byte stream for one transfer, tunneled over
  the mux). `connect_tls(url, verify=...)` + `TlsMuxLink`. Wired in
  `drone_api._start_edge_mux_client` from `DRONE_EDGE_*` settings.
- `mux.py` — frame codec (1-byte kind + uint32-BE length + payload; `CONTROL`
  JSON, `DATA` binary). **Byte-identical** to Overmind's `edge/protocol.py`.
- `assetfetch.py` — `FETCH/CHUNK/DONE` request/response over a channel; resumable
  (offset), cancellable, hash-verified. `relay_transfer.serve_asset` (sender) /
  `open_receiver_channel` (receiver) drive it over the mux.
- `holepunch.py` + `reliable_udp.py` — STUN candidate gather + mutual-confirm punch,
  then a **hand-rolled windowed-ARQ** reliable byte stream over the punched UDP
  socket (no QUIC — stdlib only). `DRONE_HOLEPUNCH_ENABLED` gates it.

**Preserved invariants:** the one-active-download queue, progress, cancel, resume,
hash verify, and cached-MD5 behavior are all unchanged — only *how the socket is
obtained* moved behind the selector. See `drone-edge-networking` skill.

## How the networking fits together

The Drone holds one outbound mux to the Edge. To pull an asset: ask the control
plane to authorize → get a short-lived token → `send_transfer_request` over the
mux → the Edge offers it to the sender → both sides try transports best-first
(`LAN → direct-public → hole-punch → relay`). Direct/LAN/hole-punch reuse
cert-pinned mTLS `/peer/*`; relay runs `assetfetch` over the mux and the bytes are
relayed Drone↔Drone (the Edge never sees plaintext on the other tiers; relay legs
are TLS to the Edge). Bytes never touch the control plane.

**Without the Edge (`DRONE_EDGE_ENABLED` off / `enable_edge=false`):** the relay
tier is dropped and the selector is `[LAN-direct, direct-public]`. **Same-LAN P2P
still works** — LAN-direct compares the peer's public IP (Overmind metadata)
against this drone's own (`_build_local_ip_addresses`, not the Edge) and connects
over the local mTLS path. **Cross-network P2P does not** unless the peer is
port-forwarded *and* Overmind's reachability probe is on (which auto-defaults on
when there's no Edge). So: one-LAN fleet → no Edge needed; multi-site fleet → run
the Edge (or self-host it) or go back to port-forwarding. Hole-punch + relay both
require the Edge.

## Conventions

- **stdlib only** — no new third-party imports; if you reach for one, find a
  stdlib equivalent (this is why hole-punch uses hand-rolled ARQ, not QUIC).
- **UI:** Bootstrap 5.3 dark theme; `table table-sm align-middle` in
  `table-responsive`; always `escapeHtml` user data. Mirror Overmind's
  branding/paging. See `bff-ui-theme-functionality` skill.
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
directly), `bff-ui-theme-functionality`. Consult the matching skill before
non-trivial work.
