---
name: drone-edge-networking
description: Use this when designing, reviewing, debugging, or modifying the Drone's outbound-only networking — the transport selector and PeerTransport tiers (LAN/tailnet-direct, direct-public), the Tailscale tailnet detection helper, transport fallback, or anything under app/transport/.
---

# Drone Transport & Tailnet Networking Skill

## Goal

Keep Drone asset transfer **outbound-only** and **transport-agnostic**: a Drone must
move ROM/BIOS/save/artwork bytes to/from paired peers with **zero router config** (no
port-forward, no public DNS, no inbound HTTPS) wherever possible, choosing the best
available transport and falling through to the next on failure, while preserving
every existing download behavior.

There is **no central hub**. The fleet is a peer-to-peer Drone swarm: two Drones pair
directly (LAN discovery/pairing-code, or code-free pairing over a shared Tailscale
**tailnet**), exchange cert-pinned mTLS identities, and transfer assets directly to
each other. A **tailnet** (mesh-VPN overlay) is what makes a peer in a different
house reachable without any port-forwarding — every device on the same tailnet gets a
stable address that looks like a LAN peer to the transfer stack regardless of NAT.

## Project context

This repo is **stdlib-only** (locked Batocera Python — no pydantic, no QUIC, no
`cryptography`). The networking lives in `app/transport/`:

```text
base.py          # PeerTransport ABC, DownloadRequest, TransferContext
selector.py      # TransportSelector: try tiers best-first, fall through on failure
lan.py           # LanDirectTransport (same-NAT peer OR tailnet peer; reuses local mTLS /peer/*)
tailnet.py       # is_tailnet_address / get_tailnet_ip -- stdlib CGNAT-range detection, no settings gate
direct_public.py # DirectPublicTransport (direct-WAN fallback; wraps _download_*_from_peer)
```

`DownloadManager._run_job` calls `TransportSelector.fetch`.

## Retired: the Edge/mux/relay/hole-punch stack

Earlier versions of this fleet ran a central hub (Overmind) with an always-on relay
service (the "Edge") that Drones held one persistent outbound mux connection to, used
for signaling, hole-punching, and last-resort relay when no direct path existed. **The
hub, and the whole Edge client stack, have been fully removed from this repo** —
`mux.py`, `mux_client.py`, `relay_transfer.py`, `assetfetch.py`, `holepunch.py`,
`reliable_udp.py` and their tests are all deleted, along with `settings.edge_enabled`/
`DRONE_EDGE_*`/`DRONE_HOLEPUNCH_ENABLED`. There is no gated escape hatch anymore — the
fleet is peer-to-peer only, coordinated by direct pairing (LAN discovery/pairing-code
or tailnet). New cross-network capability belongs in the tailnet/LAN path below; there
is no relay tier to fall back to, so a peer that is neither on the same LAN/tailnet nor
port-forwarded is simply unreachable today.

**Cross-repo note:** `.github/tests/test_edge_relay_integration.py` (a sibling repo)
still imports `app.transport.{assetfetch,relay_transfer,mux_client}` from this repo to
exercise the old Overmind Edge relay path end to end. Those modules no longer exist
here, so that test is now broken — it needs its own fix/removal in the `.github` repo
(out of scope for this repo's cleanup; flag it rather than silently patching a sibling
repo's CI test).

## Core rules

1. **Outbound only.** Never add an inbound listener or require a public IP/port-forward
   for a transport — a Drone dials out to a peer's advertised address (LAN, tailnet, or
   direct-WAN); it never needs to accept an unsolicited inbound connection.
2. **stdlib only.** No new third-party imports. If you reach for a mesh-networking or
   crypto library, find a stdlib path (this is why tailnet detection is a plain
   `socket` probe, not a Tailscale client library).
3. **Go through the selector.** Asset fetches route through `TransportSelector`, not a
   hard-coded `_download_*`. New transports implement `PeerTransport`.
4. **Best-first with fallback.** Order: `LAN/tailnet-direct → direct-public`. A
   transport that raises must let the selector try the next.
5. **Preserve the download contract.** One-active-download queue, progress, cancel,
   resume (byte offset), hash/fingerprint verify, and cached-MD5 reuse must behave
   identically — only *how the socket is obtained* may change.
6. **Reuse the existing trust model.** Every tier keeps cert-pinned mTLS
   (`_peer_trust_cafile`); do not disable verification to "make it work."
7. **Presence, not settings, gates tailnet.** `tailnet.py` has no on/off switch —
   without a running Tailscale daemon the probe simply finds no route and returns
   `None`, and every downstream branch (pairing, LAN transport, discovery payloads)
   degrades cleanly to "no tailnet address." Don't add a redundant enable flag.
8. **Never block.** Peer scans/discovery must not block UI/API.
9. **Fail closed + bounded.** On transport failure, fall through then stop; no
   infinite retries, no plaintext fallback, no cert-verify disable.

## Transport selection rules

`TransportSelector.select`/`fetch` tries candidates best-first and falls through when
one is not `usable(...)` or its `fetch(...)` raises:

```text
1. LanDirectTransport     – peer is same-NAT (matches our public IP) OR reachable
                            over a shared tailnet → local mTLS /peer/*
2. DirectPublicTransport  – peer is port-forwarded/public → existing
                            _download_*_from_peer
```

`LanDirectTransport.lan_url(peer)` tries the tailnet address first
(`_tailnet_url`), then the same-public-IP LAN path (`_same_network_url`) — a tailnet
peer is preferred ahead of a stale literal-IP/hostname fallback so a peer that moved
networks doesn't delay every request.

Rules:
- A transport must implement `usable(request, context) -> bool` cheaply (no network)
  and `fetch(request, context) -> dict`.
- `usable` should reflect real preconditions (asset type supported, peer fields
  present), so the selector can skip it fast.
- Do not reorder tiers so direct-public runs before LAN/tailnet-direct — that defeats
  the point of P2P.

## Tailnet detection (`tailnet.py`)

- `TAILNET_IPV4_NETWORK` (`100.64.0.0/10`, the CGNAT range Tailscale allocates from)
  and `TAILNET_IPV6_NETWORK` (`fd7a:115c:a1e0::/48`) are the two ranges checked.
- `get_tailnet_ip()` does a UDP `connect()` to Tailscale's quad-100 virtual service
  address (`100.100.100.100:53`) and reads back `getsockname()` — this sends no
  packets, it just asks the kernel which source address *would* be used, which is
  the device's own tailnet IP only if the tailnet route exists.
- Shared by the LAN-direct transport (this package) and the pairing/identity payloads
  in `transfer/local_network.py` (`transfer` imports from `transport`, never the
  reverse) — don't duplicate the detection logic in the transfer layer.
- `device/tailnet_service.py` owns actually installing/enrolling/watchdogging the
  Tailscale daemon (`ensure_tailnet_networking`, `tailnet_status`,
  `tailnet_rotate_auth_key`) — a separate concern from this package's pure address
  detection.

## Settings

```text
DRONE_TAILNET_WATCHDOG_INTERVAL_SECONDS  # how often the watchdog re-checks the daemon is up
DRONE_LOCAL_HEALTH_INTERVAL_SECONDS      # how often paired peers are health-checked (default 30s)
```

## Testing rules

The selector, LAN/tailnet transport, and tailnet address detection are pure/
socketpair-testable — add `unittest.TestCase` tests beside `tests/test_transport.py`.

## Common failure patterns

Look for these first:

- A new fetch path bypasses the selector and calls `_download_*` directly.
- Direct-public is reordered ahead of LAN/tailnet-direct.
- A transport disables TLS verify or falls back to plaintext on failure.
- `open_local_file_source`/equivalent path resolution allows `..` or escapes the root.
- A new third-party import sneaks in (breaks on the locked device).
- Resume/cancel/verify behavior changes when routed through a new transport.
- Adding a tailnet on/off setting instead of relying on presence detection.

## Expected output format

When completing transport/networking work, respond using this format:

```text
Objective:
...
Transport/selector changes:
...
Tailnet/LAN detection changes:
...
Security (mTLS/outbound-only):
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

- add an inbound listener or require a public IP / port-forward for a new tier,
- add a third-party dependency (stdlib only),
- bypass the `TransportSelector`,
- run direct-public before LAN/tailnet-direct,
- disable TLS verification or add a plaintext fallback,
- block peer scans/discovery or the UI on a transfer,
- weaken the one-active-download / progress / cancel / resume / verify contract,
- add a settings gate to tailnet detection (presence is the gate, by design).

## Default bias

When unsure, choose the option that keeps transfers outbound-only, tries LAN/tailnet
direct before the direct-public fallback, preserves the existing download contract,
stays stdlib-only, keeps mTLS intact, streams rather than buffers, and falls through
cleanly (bounded, no plaintext) rather than failing hard.
