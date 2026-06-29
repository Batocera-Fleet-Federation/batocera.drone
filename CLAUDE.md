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

**Web app:** `app/drone_api.py` is the large core (HTTP handlers via
`ApiRoutesMixin`/`UiRoutesMixin`, scanning, sync, peer transfer).
`Settings.from_env()` reads `*_ROOT` env vars (default `/userdata/{roms,bios,saves}`).

**SQLite cache:** `rom_metadata_store.py` + `saves_store.py` persist scanned asset
metadata in the shared state DB (`state_store.py`). Files are fingerprinted with a
sampled hash (`RomRepository.build_fingerprint`, `sample-fp-v1` — head/middle/tail
windows, constant cost; BIOS uses full-file MD5). A `cache_changes`/pending-changes
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
`transfer_files.py`) and fetch with `_download_*_from_peer` (cert pinning via
`_peer_trust_cafile`, SSL-retry). Peer selection in `peer_selection.py`. See
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
- `drone_api.py` is large — prefer targeted edits over restructuring; match
  surrounding comment density + idiom.
- Add focused `unittest.TestCase` tests beside existing ones.

## Skills (`.claude/skills/`, auto-surfaced)

`drone-db-management`, `drone-p2p-transfer-security`, `drone-edge-networking`,
`bff-ui-theme-functionality`. Consult the matching skill before non-trivial work.
