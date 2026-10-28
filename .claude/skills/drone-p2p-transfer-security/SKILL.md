---
name: drone-p2p-transfer-security
description: Use this when designing, reviewing, debugging, or modifying Drone peer-to-peer file transfer, drone-to-drone connectivity checks, TCP ping/port probing, swarm peer selection, download source selection, mTLS certificates, secure file transfer, or paired-peer metadata.
---

# Drone Peer-to-Peer Transfer and Security Skill

## Goal

Ensure Drone peer-to-peer file transfer is secure, observable, efficient, and based on current connectivity data.

Drones should not blindly download from any paired peer. They should periodically test connectivity to other paired drones, store those results locally, and use that data when deciding which peer is the best download source.

All Drone-to-Drone downloads must use application-created, cert-pinned mTLS.

## Project Context

The Batocera Fleet Federation is a **peer-to-peer Drone swarm — there is no central
coordination service.** Two Drones become peers by **pairing directly**: LAN discovery
+ a rotating pairing code, or code-free pairing over a shared **Tailscale tailnet**
(accepted only from an already-online tailnet source IP). Pairing exchanges each
Drone's self-signed mTLS certificate and pins its fingerprint — there is no shared CA
and no third party granting authorization; a peer is authorized because it is in
*this Drone's own* paired-peer list, nothing else.

Each Drone stores, per paired peer, locally:

- drone id, self-reported name/hostname,
- LAN IP, tailnet IP, WAN/public IP, advertised API port,
- pinned certificate fingerprint,
- connectivity/health check results (status, latency, failure reason, which address
  type succeeded),
- available files/sync metadata (fetched live from the peer, not cached long-term).

Drone should use this information to periodically test peer reachability and store
results locally.

## Core Rules

When working on Drone peer-to-peer transfer logic, follow these rules:

1. Never assume a peer is reachable just because it's in the paired-peer list.
2. Periodically perform connectivity checks against every paired peer.
3. Store peer connectivity results locally.
4. Use stored peer connectivity results before choosing a download source.
5. Prefer recently reachable peers over stale or unknown peers.
6. Prefer LAN/tailnet peers over legacy WAN peers when both are available.
7. Do not download from drones that are unpaired, untrusted, stale, unreachable, or missing a valid pinned certificate.
8. All Drone-to-Drone downloads must use mTLS.
9. mTLS certificates must be created and managed by the application (self-signed, pinned at pairing).
10. Do not fall back to plaintext HTTP for Drone-to-Drone downloads.
11. Do not disable TLS verification to "make it work."
12. Do not trust an address for a peer that isn't already in this Drone's own paired-peer list.
13. Avoid exposing file transfer endpoints without authentication and authorization.
14. Avoid loading large file manifests into memory without pagination or streaming.
15. Log enough transfer and connectivity data to debug issues without exposing secrets.

## Signed ROM/BIOS inventories (#333)

`transfer/signed_inventory.py` authenticates ROM and BIOS content independently of
transport. Before any new file transfer, `_download_rom_from_peer`,
`_download_bios_from_peer`, and `_download_rom_folder_from_peer` request
`GET /peer/content-inventory/{roms,bios}` with an unpredictable 256-bit nonce,
asset-relative path and ROM system (empty for BIOS). The authorized handler signs
a bounded inventory using the existing Drone identity key via OpenSSL. The signed
payload binds protocol version, source Drone id, asset type, system, path, entry
type, nonce, file sizes and full-file SHA-256 hashes. Folder inventories include
every child, not only the marker. Symlink children are refused.

The receiver loads the certificate from its local peer cache, checks its SHA-256
fingerprint against the **persisted paired record**, and verifies the signature
and every request binding. Never accept a certificate or trust pin from the
caller or the response. The nonce prevents replay without clock synchronization.
There is no unsigned/old-peer fallback: source peers need this endpoint before
ROM/BIOS pulls can succeed. Upgrade source peers as well as receivers.

Every file is checked against signed size/SHA-256 before replacing its target;
streams exceeding signed size are aborted and partial files removed. Existing
caller-supplied sampled fingerprints/BIOS MD5 remain compatibility checks, never
the content authentication authority. Existing-file skips do not install bytes.
Inventories are limited to 10,000 entries and an 8 MiB response; oversized folder
requests fail closed. Hashing streams files and does not persist new peer state.
OpenSSL is already required for Drone certificate lifecycle; no Python crypto
package or new key distribution is introduced. Saves/media/artwork are outside
this ROM/BIOS control and retain their existing validation.

## Peer Discovery and Pairing Rules

Peer candidates come from **direct pairing**, not a third-party directory:

- **LAN discovery**: a lightweight multicast/broadcast announce (`transfer/local_network.py`)
  lets Drones on the same network find each other and pair with a short-lived rotating
  code.
  - A multicast announcement is **unauthenticated**. `record_discovered_peer` never lets one
    change an already-paired peer's address, ports, tailnet route, or fingerprint (only
    `last_seen`); the discovered row mirrors the pinned record and keeps the claim as
    `announced_*`. Only pairing and authenticated tailnet probes update those fields.
  - The pairing code is only POSTed to a server whose certificate matches a trust anchor that did
    **not** come from the announcement/peer-info: the fingerprint pinned for an existing paired
    peer, or the one the operator typed from the target's pairing screen
    (`certificate_fingerprint` on the pair requests; `pairing.certificate_fingerprint` in the
    status payload). `_local_pair_peer` enforces it on the same TLS connection
    (`_PinnedFingerprintContext`) and checks the returned cert before touching the pin file;
    `_pair_peer_or_reply` returns 400 `certificate_fingerprint_required` for peers without one.
  - **Plaintext `http://` pairing is anchored too (#431):** the answering certificate is pinned on first
    contact, so an `http://` pair-by-address with neither a pinned record nor an operator-confirmed
    fingerprint is refused before anything is sent (handler 400 for any code, `_local_pair_peer`
    `ValueError`); with one, the answer's certificate must match it before the pin is written. The code
    itself still crosses a cleartext link, so prefer `https://`. Only code-free `tailnet_auto_pair` (WireGuard,
    source-address authorised) may pair over `http://` unanchored. Tests that exercise the answer path on
    an unpinned `http://` peer (`tests/adversarial/test_issue_279_*`, `tests/test_local_pair_peer_errors.py`)
    must pass a confirmed fingerprint; without one they test the refusal instead (#578).
  - **Peer JSON bodies are size-capped (#437):** every peer JSON read in `peer_connectivity.py` goes through
    `_read_capped` (reads `limit + 1`, raises `PeerResponseTooLarge`, a `ValueError`). Pairing answers and the
    `_fetch_peer_info` bootstrap fetch use `MAX_PEER_PAIRING_RESPONSE_BYTES` (64 KiB; `_local_pair_peer` turns an
    oversize body into `PeerPairingRejected` before any pin is written); `_peer_get_json`/`_peer_post_json` use
    `MAX_PEER_JSON_RESPONSE_BYTES` (8 MiB). Never add a bare `response.read()` for a peer JSON body.
    Tests: `tests/test_peer_response_size_cap.py`.
  - **No plaintext for TLS-paired peers (#246):** `_local_pair_peer` records
    `paired_over_tls` (true only when pairing used `https://`; announcements cannot set it; the inbound `_handle_peer_pair` and swarm-roster gossip also set it for https peers). It is sticky: `save_paired_peer` keeps it True for a TLS-pinned peer and `_handle_peer_pair` refuses (409) a `scheme=http` re-pair before touching the pinned cert, even with a valid pairing code or tailnet auto-pair.
    For such peers `_peer_address_candidates`/`_peer_address` drop non-https routes and
    `_local_pair_peer` refuses an http address. Peers paired over plain http (`http_only`
    fleets, which still pin the cert) keep their http routes.
- **Paired tailnet metadata refresh (#337):** the bootstrap `/v1/api/peer/info`
  body is only a discovery hint. Before accepting a paired Drone's tailnet IP,
  ports, scheme or advertised URL, re-fetch using `_fetch_peer_info` with the
  persisted `expected_fingerprint`. It authenticates the actual TLS connection
  via `_pairing_tls_context` (pinned identity or a leaf signed by it), requires
  HTTPS, rejects redirects, and the handler requires the same Drone id in the
  authenticated response. Missing/invalid pins and verification failures leave
  paired routes unchanged. A missing or copied body fingerprint proves nothing.
- **Tailnet pairing**: once a Drone is enrolled in a tailnet (`device/tailnet_service.py`),
  other online tailnet devices can pair code-free — accepted only when the pairing
  request's source IP is itself an online tailnet address, never from an arbitrary IP.
  Code-free pairing may only **add** a peer: `_handle_peer_pair` refuses (409) a tailnet-authorised
  request whose certificate differs from any pin already held for that Drone id (paired row or
  `local-peer-certs/<id>.crt`, via `_pinned_peer_fingerprints`) before anything is written; changing
  a pinned certificate needs the pairing code. A rejected fingerprint mismatch restores the previous
  cache file rather than unlinking it (issue #430).
- A drone appearing through both LAN and tailnet discovery **deduplicates to one
  paired-peer entry**; local discovery wins when both agree on the same peer.
- Forgetting a peer (`forget_peer`) is sticky — an explicitly forgotten tailnet peer
  stays forgotten until the user restores it, even if tailnet discovery keeps seeing it.
- **Swarm membership** (`transfer/swarm_membership.py`): approving a pair records
  that member and gossips the roster to already-paired peers
  (`POST /v1/api/peer/membership`). A join through any current member fills every
  roster. Callers must already be paired (mTLS fingerprint); discovery does not.
  A newer removal beats an older add, but records are unsigned, so a remove is
  honored only when both its `origin` and `peer_id` are the authenticated
  introducer, and its epoch is within `_MAX_REMOVAL_EPOCH_AHEAD` of the local
  clock (#600). Adds are bounded the same way (`_MAX_ADD_EPOCH_AHEAD`, same
  1_000_000; #636): an add whose epoch is more than the margin ahead of the
  local clock is ignored before it can ratchet `state['clock']` to the parse
  ceiling (1_000_000_000) and render future locally minted records
  unpropagatable. Minted clocks are clamped to `_MAX_EPOCH` so a previously
  poisoned clock cannot mint epochs beyond the parse ceiling. This permits a
  Drone to announce its own departure; it cannot remove another Drone or force a
  receiver's departure. Removal records are not relayed transitively. Gossip
  must not replace a fingerprint already pinned for that drone id. An explicit
  re-pair is a new approval and can rejoin; a stale add cannot.
- A trust pin is only ever a parseable X.509 certificate. `_certificate_pem_fingerprint`
  (`transfer/drone_network.py`) hands the PEM to OpenSSL via `load_verify_locations`
  before hashing; base64 decoding alone is not validation. Malformed input raises
  `ValueError` before `_save_local_peer_certificate` writes anything (#423).

Peer metadata should be refreshed periodically and stored locally; never treat a
cached address as proof of live connectivity — that's what connectivity checks are for.

## TCP Ping / Connectivity Check Rules

Drones should periodically perform connectivity checks against every paired peer.

A connectivity check means opening a connection to the peer (over its preferred
address) with a short timeout, hitting its own `/v1/api/peer/health`. It should
confirm basic reachability, not full file authorization.

Connectivity checks should:

1. Use address candidates in preference order: tailnet, then same-LAN, then legacy WAN.
2. Use short timeouts.
3. Avoid excessive retry storms.
4. Run periodically in the background (`DRONE_LOCAL_HEALTH_INTERVAL_SECONDS`, default 30s).
5. Store success/failure results locally.
6. Track latency where possible.
7. Track which address actually succeeded.
8. Never block UI/API page loads.
9. Never block core Drone startup for a long peer scan.
10. Be rate-limited and jittered to avoid synchronized spikes when many peers are paired.

Recommended check behavior:

```text
timeout: 1-3 seconds
retries: 1-2 per peer per interval
interval: periodic background job
jitter: enabled
result storage: required
```

Do not treat address reachability as file authorization. Authorization is still
enforced during mTLS/API/file transfer (the peer must be in the paired-peer list and
present its pinned certificate).

## Peer Connectivity Storage Rules

Connectivity check results must be stored locally before peer selection.

Do not keep peer reachability only in process memory.

If the project already has peer tables/state-store namespaces, update the existing
ones rather than creating duplicates.

## Peer Selection Rules

Before downloading a file, Drone must choose a source peer using stored peer
connectivity results.

Peer selection should consider:

1. Peer is in this Drone's own paired-peer list.
2. File availability.
3. Recent successful connectivity check.
4. Recent successful mTLS verification.
5. LAN/tailnet reachability.
6. Legacy WAN reachability.
7. Latency.
8. Transfer history.
9. Peer freshness.
10. Failure count.
11. Backoff status.

Preferred order:

```text
1. Paired LAN/tailnet peer with recent check success and valid mTLS.
2. Paired legacy-WAN peer with recent check success and valid mTLS.
3. Recently reachable paired peer with lowest latency and valid mTLS.
4. Defer the transfer -- there is no third party to ask for an alternate source.
```

Do not select peers where:

```text
peer is not in this Drone's paired-peer list
last_seen_at is stale
latest connectivity check failed
latest connectivity check is too old
mTLS identity is invalid or unpinned
peer is in backoff due to repeated failures
```

Adjust the freshness window based on the project's expected network volatility.

## mTLS Requirements

All Drone-to-Drone downloads must use mTLS certificates created by the application.

Rules:

1. Use TLS for every Drone-to-Drone file transfer.
2. Require client certificate authentication.
3. Require server certificate verification.
4. Certificates are self-signed by each Drone (`DroneCertificateManager`), exchanged
   and pinned by fingerprint at pairing time.
5. Do not trust a peer certificate that wasn't pinned during pairing.
6. Do not disable hostname or certificate verification.
7. Do not fall back to plaintext transfer.
8. Do not allow file download endpoints without mTLS.
9. Bind certificate identity to the paired Drone's id.
10. Validate that the certificate presented by a peer matches the pinned fingerprint for that Drone id.
11. Store only non-secret certificate metadata such as fingerprint, subject, issuer, serial, and expiration.
12. Never log private keys.

13. Identity is hardware-bound (`common/identity_provisioning.py`): never copy keys between
    machines, never treat a hostname/IP/lone MAC/disk UUID/`machine-id` as hardware identity,
    never rotate or revoke an identity because a *peer* claims a duplicate — only isolate and
    log (`record_collision`). A cloned disk must reprovision its own identity automatically and
    must never publish a membership removal/tombstone for the original.
14. While identity is unresolved (`RECOVERY_REQUIRED`) no `/peer/*` route is served on any
    listener and `_drone_client_ssl_context` refuses to present the client certificate. The check
    precedes the scheme test, so cleartext `http://` peer calls are held as well (`PeerIdentityHold`).

The application-created certificate system defines:

```text
local Drone certificate (self-signed)
local Drone private key
per-peer pinned certificate fingerprint (captured at pairing, not a shared CA)
certificate rotation process
certificate expiration handling
```

A legacy "managed" mTLS mode (certificate signed by the retired central hub) still
exists in `DroneCertificateManager` for backward compatibility — new pairing flows use
self-signed + pinning, not a signing authority.

## Certificate Storage Rules

Private keys must be protected.

Do not:

- commit certificates or private keys,
- log private keys,
- store private keys in world-readable paths,
- copy private keys into prompts or issue logs,
- expose certificates in API responses unless they are public certificate material and intended to be exposed.

Recommended persistent path pattern:

```text
/userdata/system/drone-app/certs/
```

Recommended files:

```text
/userdata/system/drone-app/certs/drone.crt
/userdata/system/drone-app/certs/drone.key
/userdata/system/drone-app/certs/ca.crt
```

Private key permissions should be restrictive where supported:

```bash
chmod 600 /userdata/system/drone-app/certs/drone.key
chmod 644 /userdata/system/drone-app/certs/drone.crt
```

If Batocera filesystem constraints affect permissions, document the limitation and use the safest available approach.

## mTLS Validation Rules

During transfer, validate:

1. The peer presents a certificate.
2. The certificate's fingerprint matches what was pinned for that Drone id at pairing time.
3. The certificate is not expired.
4. The certificate identity maps to the expected peer Drone id.
5. The peer is in this Drone's own paired-peer list.
6. The requested file is allowed to be served.
7. The local Drone is authorized to request that file.

mTLS proves peer identity. It does not replace application-level authorization.

Both are required:

```text
mTLS identity validation
+
application authorization (peer is paired)
```

## Transfer Endpoint Rules

Drone file transfer endpoints should:

1. Require mTLS.
2. Validate requesting peer identity.
3. Validate requested file access.
4. Prevent path traversal.
5. Only serve files from approved roots.
6. Stream files instead of loading entire files into memory.
7. Support resumable or ranged downloads where practical.
8. Enforce reasonable request limits.
9. Log transfer metadata without logging secrets.
10. Return clear error codes.

Never allow arbitrary path reads.

Bad:

```python
return FileResponse(request.query_params["path"])
```

Better:

```text
request file by file_id or relative path within a known root
resolve to an approved local path
verify path is inside approved root
verify requester is a paired, mTLS-authenticated peer
stream file
```

## Peer Coordination Endpoints That Are Not Transfers (Multiplayer)

`/peer/multiplayer/*` (see the `drone-multiplayer` skill) follows the same mTLS rules but
carries session coordination instead of files. Extra rules for control-plane endpoints:

- Identity is the pinned certificate (`_peer_requester_device_id`); a body field such as
  `source_drone_id` may only *agree* with it.
- Every message has a random `message_id` and `issued_at`; replays inside the window are
  reported as duplicates and never re-applied; stale/future timestamps are refused.
- Peer-supplied JSON is rebuilt field by field (bounded, enumerated, validated) before it is
  stored or used in a file name, setting or process argument.
- A peer may *propose* (invite, launch offer); it may never cause a game to start or close
  on this machine without that machine's own user confirming.
- Secrets sent over the channel (a session password) are never logged or returned to browsers.

## Path Safety Rules

Before serving any local file:

1. Resolve the requested path.
2. Normalize it.
3. Confirm it is inside an allowed root.
4. Reject `..` traversal.
5. Reject symlink escapes where possible.
6. Prefer file IDs / known-root-relative paths over raw paths in APIs.

Approved roots may include:

```text
/userdata/roms
/userdata/bios
/userdata/saves
/userdata/movies
/userdata/system/configs
```

Use the project's actual Batocera paths when known. `movies` is a **flat** asset
type (no system or artwork association, unlike roms/bios/saves) — its peer-serve
handler (`_handle_peer_movie_download`) and download function
(`_download_movie_from_peer`, both in `transfer/peer_download.py`) mirror BIOS's
flat (no-system) shape, but verify integrity with the sampled fingerprint
(`movies_store.build_movie_fingerprint`, same `sample-fp-v1` algorithm as
ROMs/saves) instead of BIOS's full-file MD5, since movie files are typically much
larger media files where a full hash would be wasteful.

**A real bug found here, worth checking for in any future asset type:**
`_collect_peer_inventory`'s movies branch (`handlers_peer.py`) only ever called
`movies_store.sync_movies_cache()` inside an `if self.settings.use_fake_data:`
guard — so on every real device (`use_fake_data=False`, always) the
`movies_cache_entries` table was never populated, and every peer inventory
request for movies correctly-but-uselessly reported zero results regardless of
how many real files were on disk (found live on a real drone: 580 files on
disk, 0 rows in the cache). The bug shipped because the only test
(`test_movies_inventory_pages_flat_with_no_system_field`) called
`sync_movies_cache()` manually before asserting on the inventory response,
which proves the *read* path but never exercises how the cache is supposed to
get populated in production. Fixed by syncing movies inside
`roms/rom_scanner.py`'s `_poll_rom_metadata_once` — the same always-on,
already-scheduled poll cycle that keeps the **saves** cache warm
(`_saves_store.sync_saves_cache`, same function, called two lines above) —
rather than adding a second poller. **When adding a new flat/cached asset
type, verify its `sync_*_cache()` is wired into a real, always-running poll
path, and add a regression test that calls the actual poll/scan entry point
(not the sync function directly) with `use_fake_data` unset/false** — a test
that manually primes the cache before asserting on it will pass even if
production never populates that cache at all.

**The VPN and SMTP config endpoints (`GET /peer/vpn/config`, `GET
/peer/smtp/config`) are the two deliberate exceptions to "pairing alone is
authorization."** Both still require `_peer_request_authorized()` like every
endpoint above, but each adds a **second, feature-owned gate** on top: the
source drone's own `sharing_enabled` flag (`device/vpn_manager.py` /
`device/smtp_manager.py` respectively), off by default and only ever flipped
on by that drone's owner. Every other asset type here
(roms/bios/saves/movies/artwork) is available to any paired peer with no
further check, by design (see "Core Rules" above) — VPN and SMTP credentials
are payloads sensitive enough to warrant an explicit opt-in on top of
pairing. Don't use this as precedent to add gates to the other asset types,
and don't remove either of these two "for consistency" — see the
`drone-vpn-management` and `drone-smtp-notifications` skills for the full
rationale (both share the same shape: `sharing_enabled` + single-hop
provenance + a revocation poller). They are also the only peer endpoints
whose payload carries a plaintext secret (the OpenVPN credential; the
SMTP password) by design; that is still within the rules above, since
mTLS + pairing was always the confidentiality boundary, not merely a
convention that happened to apply only to non-secret files. If a third
feature ever needs this same treatment, keep the pattern — a feature-owned
`sharing_enabled` gate on top of pairing, not a change to the other asset
types' pairing-only trust.

## Transfer Source Decision Rules

Before initiating a download:

1. Ask local paired-peer storage for eligible peers.
2. Filter to peers with recent successful connectivity check.
3. Filter to peers with valid pinned mTLS.
4. Filter to peers known to have the requested file.
5. Prefer LAN/tailnet over legacy WAN.
6. Prefer lower latency.
7. Avoid peers with recent transfer failures.
8. Start transfer using mTLS.
9. Record transfer result locally.

Do not perform expensive peer discovery inline during a user-facing request unless
required. Prefer background refresh plus cached peer selection.

## Failure Handling Rules

When a peer download fails:

1. Record the failure.
2. Mark the peer/file attempt as failed.
3. Apply backoff for that peer.
4. Try the next eligible peer if available.
5. Do not retry indefinitely.
6. Do not downgrade from mTLS to plaintext.
7. Do not disable cert verification.
8. Report enough diagnostic detail to debug the problem.

## Observability Rules

Log:

- peer/pairing state refresh start/end,
- number of paired peers,
- number of peers checked,
- connectivity check success/failure counts,
- latency summary,
- selected peer for transfer,
- transfer start/end,
- transfer byte count,
- transfer duration,
- mTLS verification success/failure,
- authorization denial reason,
- retry/backoff decisions.

Do not log:

- private keys,
- bearer tokens,
- session cookies,
- full credentials,
- sensitive certificate private material,
- arbitrary file contents.

## Local SQLite Tracking Tables

If adding transfer tracking, prefer relational tables.

## Security Review Checklist

Security acceptance suites registered in `.swarm/tests.json` must run through
`python3 scripts/run_security_tests.py -s <directory> -p <pattern>` so empty
discovery fails rather than reporting a false pass (#616). Include the runner
and test files in each suite's required files. When changing registrations, run
`python3 -m unittest discover -s tests -p test_security_suite_validation.py` to
check required files and actual collection. Do not delete hostile-input tests
solely because their line coverage overlaps other tests; their assertions protect
distinct security boundaries. Remove or replace a suite's registration together
with any intentionally retired test.

When reviewing P2P transfer code, verify:

- Does peer discovery come from direct pairing (LAN or tailnet), not an untrusted address?
- Is peer connectivity tested periodically?
- Are connectivity check results stored locally?
- Is stored connectivity used before download source selection?
- Are stale connectivity records avoided?
- Is LAN/tailnet preferred over legacy WAN where appropriate?
- Is every Drone-to-Drone download protected by mTLS?
- Does the client verify the server certificate?
- Does the server verify the client certificate?
- Is the certificate fingerprint pinned to the paired Drone's id?
- Is the peer still present in this Drone's own paired-peer list (not just cached)?
- Are private keys protected and never logged?
- Are file paths validated against approved roots?
- Are file transfers streamed?
- Are retries bounded?
- Is failure/backoff state persisted?
- Are logs useful but not sensitive?

## Common Failure Patterns

Look for these first:

- Drone downloads from the first paired peer without checking reachability.
- Connectivity checks run but results are not stored.
- Connectivity check results are stored but not used for peer selection.
- Peer checks block UI/API requests.
- Peer connectivity is stored only in memory.
- Drone falls back to non-TLS download after TLS failure.
- TLS verification is disabled.
- Client certificate is optional instead of required.
- Certificate fingerprint is not checked against the pinned value for that Drone id.
- File endpoint accepts raw paths without validation.
- Drone serves files outside approved Batocera roots.
- Peer failure causes infinite retry loop.
- Legacy WAN peer is selected even though a LAN/tailnet peer is reachable.
- An address is trusted for a peer that isn't in the local paired-peer list.

## Expected Output Format

When completing P2P transfer or security work, respond using this format:

```text
Root cause / objective:
...

Peer discovery/pairing changes:
...

Connectivity check changes:
...

Peer connectivity storage changes:
...

Peer selection changes:
...

mTLS/security changes:
...

Transfer endpoint changes:
...

Path safety changes:
...

Retry/backoff changes:
...

Local SQLite validation:
...

Runtime validation:
...

Risks:
...

Files changed:
...
```

## Safety Rules

Do not:

- disable TLS verification,
- make mTLS optional for Drone-to-Drone downloads,
- add plaintext fallback for file transfer,
- log private keys,
- commit generated certificates or private keys,
- trust an address for a peer that isn't in the local paired-peer list,
- trust peer reachability without connectivity checks,
- choose download peers without using stored connectivity results,
- use in-memory-only peer connectivity state,
- expose file transfer endpoints without authorization,
- serve arbitrary raw file paths,
- allow path traversal,
- load large files fully into memory before sending,
- retry forever without backoff.

## Default Bias

Every outbound peer dial is gated on `swarm_networking_allowed()` (identity hold): `_fetch_peer_info`
and each live connection in `_pairing_tls_context` call `_require_swarm_networking()` right before
connecting and raise `PeerIdentityHold` (the pair handlers reply 503). New outbound peer code must do the same.

When unsure, choose the option that keeps:

- peer discovery limited to directly paired peers (LAN or tailnet),
- connectivity periodically checked,
- connectivity results stored locally,
- peer selection based on recent reachability,
- LAN/tailnet preferred when safe and available,
- downloads protected by mTLS,
- certificate fingerprint pinned to the paired Drone's id,
- file paths validated,
- transfers streamed,
- retries bounded,
- secrets protected,
- local state durable across restarts.
