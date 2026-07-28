---
name: drone-architecture-overview
description: Use this when you need the big-picture shape of the fleet before diving into a specific change — how Drones pair, how a transfer picks a route, how mTLS trust works, what's inside a single Drone process, or how VPN sharing fits together. Good first stop for "how does the swarm actually work", onboarding, cross-cutting design decisions, or explaining the architecture to someone. For implementation depth once you know which piece you're touching, use the narrower skills this one points to.
---

# Drone Swarm — High-Level Architecture

## Goal

Be the map, not the territory. This skill gives the shape of the whole system in
one place — enough to orient a design decision or a "how does X reach Y" question
— and then hands off to the deep, implementation-level skills for the actual work.
Do not duplicate their depth here; when this skill and a deeper skill disagree,
the deeper skill is right and this one is stale and should be fixed.

A rendered, interactive companion to this document exists at
`batocera.drone/docs/swarm-architecture.html` — open it in a browser for the
visual version (a geographic multi-Drone topology, the connectivity fallback
chain, the trust model, a single Drone's internals, and VPN sharing/revocation,
each with click-to-expand explainers for the underlying technologies). Keep the
two in sync: a real architectural change belongs in both.

## The one fact everything else follows from

**There is no central hub.** `batocera.overmind` — the old FastAPI/Postgres
control plane — is fully retired. Every Drone is a standalone Batocera
device-agent process that pairs *directly* with a handful of others. There is no
server anywhere that knows about the whole fleet, brokers a transfer, or is a
single point of failure. If you find yourself designing something that assumes a
component which "sees every Drone" or "coordinates the fleet," stop — that's the
retired architecture, not this one.

**The federation-root `CLAUDE.md` (in `.github/`) is stale about this and has
caused real confusion twice in one session** — it still describes Overmind as a
live hub with an Edge relay/mux/hole-punch system. `batocera.drone`'s own
`CLAUDE.md` and this skill are the current, authoritative picture. If you're
reading from the root doc, cross-check against here before trusting it.

Corollary: Drones make **outbound connections only**. No port-forward, no public
IP, and no inbound HTTPS is required for the common case. A Drone that can't make
any outbound-reachable path to a peer simply can't reach it — see "no relay of
last resort" below.

## Pairing and trust (the security model)

Two Drones become peers by pairing **directly**, never through a third party:

- **LAN discovery** — a short-lived rotating pairing code, exchanged over the
  local network (`transfer/local_network.py`).
- **Tailnet, code-free** — accepted only when the pairing request's source IP is
  itself an already-online tailnet address (`device/tailnet_service.py`).

Pairing exchanges each Drone's **self-signed** mTLS certificate and pins the
peer's exact fingerprint — there is no shared CA and no third party granting
authorization. The stored result (`transfer/local_network.py`'s
`local_paired_peers`) is the *only* authority: a peer is trusted because it's in
*this Drone's own* paired-peer list, nothing else. (A legacy "managed" mTLS mode
signed by the retired hub still exists in `DroneCertificateManager` for
backward compatibility; new pairing is always self-signed + pinned.)

Every Drone exposes two separate listeners, and they enforce different things:

| Listener | Port(s) | Auth |
|---|---|---|
| Admin / Browser | `:443`, `:8443` compat | Session cookie (SQLite-backed, 30-day sliding expiry). Never asks for a client cert. |
| Peer | `:8543` (`DRONE_PEER_MTLS_PORT`) | mTLS required; client cert fingerprint must match the pinned value for that exact `drone_id`, every request. |

Application authorization *is* the paired-peer-list check above — connectivity
checks (below) confirm a peer is *reachable*, never that it's *authorized*. Depth:
`drone-p2p-transfer-security` skill.

## Reaching a specific peer (the transport layer)

`app/transport/`'s `TransportSelector` tries each tier best-first and falls
through on failure — used by every asset transfer (ROM/BIOS/save/movie), the
remote-admin proxy, and VPN config pulls alike:

1. **LAN-direct** (`transport/lan.py`) — both Drones report the same public IP
   ⇒ reach the peer's local IP directly. Zero configuration.
2. **Tailnet-direct** (also `transport/lan.py`, via `transport/tailnet.py`'s
   address detection) — both Drones are on the same Tailscale mesh ⇒ a
   NAT-traversed private address (100.64.0.0/10) reachable across any network.
   Recommended for cross-network pairs; no router configuration needed.
3. **Direct-WAN** (`transport/direct_public.py`) — legacy fallback: the peer is
   port-forwarded and passes a live reachability probe.

**There is no relay of any kind.** If a peer is neither on the same LAN/tailnet
nor port-forwarded, it is simply unreachable — this is deliberate, not a gap
(the old Edge-relay design this replaced is retired; don't reintroduce it).
It's also **single-source P2P, not torrent-style swarming**: one active transfer
per job, from one peer the user (or, for VPN bootstrap, the Drone itself)
explicitly picked. Depth: `drone-edge-networking` skill (name is legacy —
covers this transport layer despite it).

## Inside one Drone

Stdlib `http.server` + SQLite — no database server, no message queue, no
external runtime dependency, because Batocera's on-device Python can't assume
any third-party package is installed. Runs as **root**, on-device
(`python3 -m app.main`, supervised by `app/service_bootstrap.sh`).

- **SQLite state store** (one file, WAL mode) — paired peers, sessions, VPN
  state, and the scanned ROM/BIOS/save/movie caches. See `drone-db-management`.
- **Background daemon threads**, all started from `create_server()`:
  - Peer health check, every ~30s, all paired peers, jittered, cached locally.
  - ROM/BIOS/saves/movies filesystem scan, inotify-debounced.
  - VPN connect + swarm bootstrap, on startup (see below).
  - VPN sharing-revocation poll, every ~5 min, only if this Drone imported a
    shared config.
- **VPN manager** (`device/vpn_manager.py`) — deliberately stateless
  (`status()` recomputes from `/proc` + the log on every call rather than
  keeping cached state in sync with a thread) and spawns a real `openvpn` OS
  process that self-daemonizes into its own session — it survives a Drone-app
  restart untouched; only a full machine reboot takes it down. Depth:
  `drone-vpn-management` skill.

## VPN: one subscription, the whole swarm

Built on the P2P/security primitives above, not a separate system:

- Each Drone can independently run its own OpenVPN client (upload a provider's
  `.ovpn`, save credentials, connect/disconnect, live status).
- **Sharing** (`sharing_enabled`, off by default) lets a paired peer pull this
  Drone's config *and* credentials over the same mTLS peer channel everything
  else uses (`GET /peer/vpn/config`) — not a separate transport.
- **Single-hop only**: a Drone that imports a shared config can use it but can
  never re-share it — enforced twice, independently (`set_sharing_enabled`
  refuses to turn on for an imported config; `export_payload` independently
  refuses to serve one). Provenance (`source_peer_id`) is permanent and only
  ever cleared by a genuine fresh upload.
- **Revocation**: turning sharing off auto-disconnects and wipes credentials
  (not the config, not the provenance) on every Drone that had pulled it, within
  one poll cycle (~5 min) — Drones are outbound-only with no push channel, so
  this has to be a periodic check, not a notification.
- **Swarm bootstrap**: a Drone with no usable config of its own, on startup,
  adopts the first paired peer found both sharing *and* actively connected right
  now (not merely configured to share) — never overrides a config a Drone
  already has. `maybe_auto_connect()` also retries connecting a few times with a
  short delay, since a fresh boot can hit transient conditions that resolve
  moments later.

Depth: `drone-vpn-management` skill (this is the fullest treatment; the section
above is a summary).

## Common failure patterns

- Designing anything that assumes a central coordinator, a fleet-wide view, or a
  relay of last resort — all retired. Re-read "The one fact everything else
  follows from" above.
- Trusting the federation-root `.github/CLAUDE.md` on networking/architecture
  without cross-checking here or `batocera.drone/CLAUDE.md` — it's known stale.
- Treating peer *reachability* (a connectivity check) as peer *authorization*
  (the paired-peer list) — they are different checks with different failure
  modes; see `drone-p2p-transfer-security`.
- Letting this skill and `docs/swarm-architecture.html` drift apart after a real
  architecture change — update both, or neither is trustworthy.

## Where to go next

- **P2P transfer, mTLS, peer selection depth** → `drone-p2p-transfer-security`
- **Transport tiers / LAN / tailnet detail** → `drone-edge-networking`
- **VPN feature implementation depth** → `drone-vpn-management`
- **Admin UI, routes, Swarm page, remote peer management** → `drone-admin-features`
- **SQLite schemas/migrations** → `drone-db-management`
- **Debugging a real, running Drone** → `drone-live-debugging`

## Default bias

When a task spans more than one of the areas above, start here to get the shape
right, then drop into the specific skill for implementation. When you learn
something here is wrong or out of date, fix this file in the same change —
don't leave the map contradicting the territory for the next person (or session)
who reads it first.
