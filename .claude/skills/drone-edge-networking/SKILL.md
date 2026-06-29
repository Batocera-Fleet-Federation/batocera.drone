---
name: drone-edge-networking
description: Use this when designing, reviewing, debugging, or modifying the Drone's outbound-only networking — the transport selector and PeerTransport tiers (LAN / direct-public / hole-punch / relay), the persistent Edge mux client, the relay/AssetFetch data path, UDP hole punching + reliable-UDP ARQ, transport fallback, transfer tokens, or anything under app/transport/.
---

# Drone Edge Networking & Transport Skill

## Goal

Keep Drone asset transfer **outbound-only** and **transport-agnostic**: a Drone
must move ROM/BIOS/save/artwork bytes to/from peers with **zero router config**
(no port-forward, no public DNS, no inbound HTTPS), choosing the best available
transport and falling through to the next on failure, while preserving every
existing download behavior.

The Drone holds **one persistent outbound connection** (the "mux") to the Overmind
**Edge** service. Direct peer transfers still happen Drone↔Drone; the Edge relays
bytes **only when direct paths fail**.

## Project context

This repo is **stdlib-only** (locked Batocera Python — no pydantic, no QUIC, no
`cryptography`). The networking lives in `app/transport/`:

```text
base.py          # PeerTransport ABC, DownloadRequest, TransferContext
selector.py      # TransportSelector: try tiers best-first, fall through on failure
lan.py           # LanDirectTransport  (same-NAT peer; reuses local mTLS /peer/*)
direct_public.py # DirectPublicTransport (wraps existing _download_*_from_peer)
relay_transfer.py# RelayReceiverTransport + serve_asset/open_receiver_channel
assetfetch.py    # FETCH/CHUNK/DONE codec over a channel (resumable/cancel/verify)
mux.py           # frame codec (== overmind edge/protocol.py, byte-identical)
mux_client.py    # MuxClient (threaded runner) + MuxSession (pure core) + RelayChannel
holepunch.py     # STUN candidate gather + mutual-confirm UDP punch
reliable_udp.py  # hand-rolled windowed-ARQ reliable stream over the punched socket
```

Wired in `drone_api._start_edge_mux_client` from `DRONE_EDGE_*` settings;
`DownloadManager._run_job` calls `TransportSelector.fetch`.

## Core rules

1. **Outbound only.** Never add an inbound listener or require a public
   IP/port-forward for a transport. The Drone dials out; the Edge and peers are
   reached via outbound sockets.
2. **stdlib only.** No new third-party imports. If you reach for QUIC/`cryptography`/
   pydantic, find a stdlib path (this is why hole-punch uses hand-rolled ARQ).
3. **Go through the selector.** Asset fetches route through `TransportSelector`, not
   a hard-coded `_download_*`. New transports implement `PeerTransport`.
4. **Best-first with fallback.** Order: `LAN → direct-public → hole-punch → relay`.
   A transport that raises must let the selector try the next; relay is last resort.
5. **Preserve the download contract.** One-active-download queue, progress, cancel,
   resume (byte offset), hash/fingerprint verify, and cached-MD5 reuse must behave
   identically — only *how the socket is obtained* may change.
6. **Authorize before transfer.** A relayed/punched transfer uses a short-lived
   transfer token minted by the control plane; do not invent ad-hoc trust.
7. **Reuse the existing trust model.** Direct/LAN/hole-punch keep cert-pinned mTLS
   (`_peer_trust_cafile`); do not disable verification to "make it work."
8. **Never block.** The mux read loop and `on_transfer_offer` must not block —
   hand serving off to a worker thread. Peer scans/punches must not block UI/API.
9. **Fail closed + bounded.** On transport failure, fall through then stop; no
   infinite retries, no plaintext fallback, no cert-verify disable.
10. **One wire format.** `mux.py` framing must stay byte-identical to Overmind's
    `edge/protocol.py` (there is a golden-vector test — keep it green).

## Transport selection rules

`TransportSelector.select`/`fetch` tries candidates best-first and falls through
when one is not `usable(...)` or its `fetch(...)` raises:

```text
1. LanDirectTransport     – peer shares our public IP (same NAT) → local mTLS /peer/*
2. DirectPublicTransport  – peer is port-forwarded/public → existing _download_*_from_peer
3. (hole-punch)           – reflexive candidates via Edge SIGNAL → reliable-UDP channel
4. RelayReceiverTransport – pull via the Edge relay (no port-forward); v1 = ROM only
```

Rules:
- A transport must implement `usable(request, context) -> bool` cheaply (no network)
  and `fetch(request, context) -> dict`.
- `usable` should reflect real preconditions (asset type supported, mux available,
  peer fields present), so the selector can skip it fast.
- Keep relay scoped to what it supports (`SUPPORTED_ASSET_TYPES`); let other asset
  types fall to other tiers rather than failing the whole transfer.
- Do not reorder tiers so relay runs before direct — that wastes the Edge and loses
  the point of P2P.

## Mux client rules

`MuxClient` keeps one persistent Edge connection in a background thread with
reconnect/backoff + ping; `MuxSession` is the pure protocol core (no I/O), so it is
unit-testable without sockets.

1. Connect via `connect_tls(DRONE_EDGE_URL, verify=DRONE_EDGE_VERIFY_TLS)`; default
   to verifying TLS in production, allow `verify=False` only for self-signed/local.
2. Send `HELLO` with `device_id` + bearer `token` (+ capabilities/lan_addrs);
   treat `HELLO_ACK` as connected and stash `session_id` + `reflexive_addr`.
3. Answer `PING` with `PONG`; let idle-timeout drive reconnect.
4. Route relay `DATA`/relay-control frames to the right `RelayChannel`; only
   presence/keepalive go to `MuxSession.handle_frame`.
5. `on_transfer_offer` (sender side) must hand off to a worker thread and return
   immediately — never serve the asset on the read loop.
6. Relay reads block in the worker thread until the read loop feeds bytes; writes
   go out under the shared send lock. Don't add a second writer path.

## Relay data-path rules (AssetFetch)

- Receiver: `open_receiver_channel(mux, session_id, token, from_device, asset)` →
  registers a receiver leg, sends `TRANSFER_REQUEST`, waits for `RELAY_READY`,
  returns the channel. Then `assetfetch.download(channel, asset, write, offset=)`.
- Sender: on `TRANSFER_OFFER`, `serve_asset(mux, session_id, resolve)` opens a
  sender leg and streams via `assetfetch.serve_one`; `resolve` →
  `open_local_file_source(root, relative_path, offset)` (path-safe, under root).
- AssetFetch is **chunked** (`DEFAULT_CHUNK_SIZE`, never whole-file), **resumable**
  (offset), **cancellable** (`cancel.is_set()`), and the **receiver verifies**
  against the fingerprint/MD5 it already holds (sender `hash` is None) — exactly
  like the HTTP path.
- `open_local_file_source` must reject traversal and anything outside `root`.

## Hole-punch rules

- Gather a reflexive candidate from the Edge STUN reflector
  (`DRONE_EDGE_STUN_PORT`); exchange candidates via `SIGNAL` through the mux.
- Punch with a **mutual-confirm** handshake before sending data, so one side can't
  end up on UDP while the other fell back to relay.
- The data layer is `reliable_udp.ReliableUDPChannel`: a sliding-window ARQ
  (cumulative ACK + retransmit + reorder buffer) — stdlib only, constant-ish
  memory. `read_exactly` blocks until N bytes or EOF (returns short only at EOF).
- Hole-punch is best-effort: on any failure, fall through to relay. Gate with
  `DRONE_HOLEPUNCH_ENABLED`.

## Settings

```text
DRONE_EDGE_ENABLED      # turn the outbound mux + relay on
DRONE_EDGE_URL          # tls://host:port  (parse_edge_endpoint accepts tls/wss/https/bare)
DRONE_EDGE_VERIFY_TLS   # verify the Edge cert (true in prod; false for self-signed/local)
DRONE_EDGE_PING_SECONDS # mux keepalive cadence
DRONE_EDGE_STUN_PORT    # Edge STUN reflector port for hole-punch
DRONE_HOLEPUNCH_ENABLED # enable UDP hole punching
```

## Testing rules

- `MuxSession`, the codec, the selector, AssetFetch, hole-punch framing, and
  reliable-UDP are pure/socketpair-testable — add `unittest.TestCase` tests beside
  `tests/test_transport.py`, `test_mux_client.py`, `test_relay_transfer.py`,
  `test_assetfetch.py`, `test_holepunch.py`, `test_reliable_udp.py`.
- For socketpair tests, `shutdown(SHUT_RDWR)` before close so the peer's blocking
  read unblocks (makefile keeps the fd alive otherwise).
- The true end-to-end (real Drone client ↔ real Edge over loopback TLS) lives in
  `.github/tests/test_edge_relay_integration.py` — keep it green when changing the
  wire protocol or relay flow.

## Common failure patterns

Look for these first:

- A new fetch path bypasses the selector and calls `_download_*` directly.
- Relay runs before direct tiers (wastes the Edge / loses P2P).
- `on_transfer_offer` or the read loop blocks on serving an asset.
- A transport disables TLS verify or falls back to plaintext on failure.
- Hole-punch sends data before mutual confirm (one side relays, one side UDPs).
- AssetFetch buffers the whole file instead of streaming chunks.
- `open_local_file_source` allows `..` or escapes the root.
- The frame codec drifts from `edge/protocol.py` (golden-vector test breaks).
- A new third-party import sneaks in (breaks on the locked device).
- Resume/cancel/verify behavior changes when routed through a new transport.

## Expected output format

When completing transport/networking work, respond using this format:

```text
Objective:
...
Transport/selector changes:
...
Mux/protocol changes:
...
Relay / AssetFetch changes:
...
Hole-punch / reliable-UDP changes:
...
Security (tokens/mTLS/outbound-only):
...
Preserved download contract (queue/progress/cancel/resume/verify):
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

- add an inbound listener or require a public IP / port-forward,
- add a third-party dependency (stdlib only),
- bypass the `TransportSelector`,
- run relay before the direct tiers,
- disable TLS verification or add a plaintext fallback,
- block the mux read loop / `on_transfer_offer` / UI on a transfer or punch,
- send hole-punch data before mutual confirmation,
- buffer whole files in memory,
- weaken the one-active-download / progress / cancel / resume / verify contract,
- let the `mux.py` codec drift from `edge/protocol.py`.

## Default bias

When unsure, choose the option that keeps transfers outbound-only, tries direct
before relay, preserves the existing download contract, stays stdlib-only, keeps
mTLS/token authorization intact, streams rather than buffers, and falls through
cleanly (bounded, no plaintext) rather than failing hard.
