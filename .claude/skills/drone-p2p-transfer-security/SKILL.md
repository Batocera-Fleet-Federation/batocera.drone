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

## Peer Discovery and Pairing Rules

Peer candidates come from **direct pairing**, not a third-party directory:

- **LAN discovery**: a lightweight multicast/broadcast announce (`transfer/local_network.py`)
  lets Drones on the same network find each other and pair with a short-lived rotating
  code.
- **Tailnet pairing**: once a Drone is enrolled in a tailnet (`device/tailnet_service.py`),
  other online tailnet devices can pair code-free — accepted only when the pairing
  request's source IP is itself an online tailnet address, never from an arbitrary IP.
- A drone appearing through both LAN and tailnet discovery **deduplicates to one
  paired-peer entry**; local discovery wins when both agree on the same peer.
- Forgetting a peer (`forget_peer`) is sticky — an explicitly forgotten tailnet peer
  stays forgotten until the user restores it, even if tailnet discovery keeps seeing it.

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
/userdata/system/configs
```

Use the project's actual Batocera paths when known.

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
