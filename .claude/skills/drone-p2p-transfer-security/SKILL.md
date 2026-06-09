---
name: drone-p2p-transfer-security
description: Use this when designing, reviewing, debugging, or modifying Drone peer-to-peer file transfer, drone-to-drone connectivity checks, TCP ping/port probing, swarm peer selection, download source selection, mTLS certificates, secure file transfer, or Overmind-provided peer metadata.
---

# Drone Peer-to-Peer Transfer and Security Skill

## Goal

Ensure Drone peer-to-peer file transfer is secure, observable, efficient, and based on current swarm connectivity data.

Drones should not blindly download from any available peer. They should periodically test connectivity to other drones in the swarm, store those results locally, and use that data when deciding which peer is the best download source.

All Drone-to-Drone downloads must use application-created mTLS certificates.

## Project Context

The Batocera Fleet Federation system has:

- **Overmind**: central coordination service that knows about users, drones, swarm membership, approvals, peer metadata, and sync state.
- **Drone**: Batocera-side application that can participate in peer-to-peer file transfer with other approved drones in the swarm.

Overmind may return information about other drones that are eligible peers, including:

- drone ID,
- hostname,
- LAN IP,
- WAN IP,
- advertised port,
- NAT/port-forwarding status,
- owner or swarm relationship,
- connection status,
- last seen time,
- capabilities,
- available files,
- sync metadata,
- certificate or certificate identity metadata.

Drone should use this information to periodically test peer reachability and store results locally.

## Core Rules

When working on Drone peer-to-peer transfer logic, follow these rules:

1. Never assume a peer is reachable just because Overmind says it exists.
2. Periodically perform TCP connectivity checks against candidate drones returned by Overmind.
3. Store peer connectivity results locally.
4. Use stored peer connectivity results before choosing a download source.
5. Prefer recently reachable peers over stale or unknown peers.
6. Prefer LAN/local peers over WAN peers when both are available and authorized.
7. Do not download from drones that are unauthorized, untrusted, stale, unreachable, or missing valid mTLS identity.
8. All Drone-to-Drone downloads must use mTLS.
9. mTLS certificates must be created and managed by the application.
10. Do not fall back to plaintext HTTP for Drone-to-Drone downloads.
11. Do not disable TLS verification to “make it work.”
12. Do not trust user-provided peer addresses without Overmind authorization.
13. Avoid exposing file transfer endpoints without authentication and authorization.
14. Avoid loading large file manifests into memory without pagination or streaming.
15. Log enough transfer and connectivity data to debug issues without exposing secrets.

## Peer Discovery Rules

Drone should obtain peer candidates from Overmind.

Peer candidate data from Overmind should be treated as authorization and discovery input, not proof of live connectivity.

Drone should periodically refresh peer metadata from Overmind and store relevant peer information locally.

Peer records should be associated with the local swarm/user context to prevent cross-swarm leakage.

## TCP Ping / Connectivity Check Rules

Drones should periodically perform TCP ping checks against other drones in the swarm using information returned by Overmind.

A TCP ping means opening a TCP socket to the peer host and port with a short timeout. It should confirm basic port reachability, not full file authorization.

Connectivity checks should:

1. Use peer IP/host and port from Overmind-provided metadata.
2. Test LAN IP first when available.
3. Test WAN IP only when LAN is unavailable or failed.
4. Use short timeouts.
5. Avoid excessive retry storms.
6. Run periodically in the background.
7. Store success/failure results locally.
8. Track latency where possible.
9. Track which address type succeeded: LAN or WAN.
10. Never block UI/API page loads.
11. Never block core Drone startup for a long peer scan.
12. Be rate-limited and jittered to avoid synchronized swarm spikes.

Recommended TCP check behavior:

```text
timeout: 1-3 seconds
retries: 1-2 per peer per interval
interval: periodic background job
jitter: enabled
result storage: required
```

Example Python TCP check pattern:

Do not treat TCP reachability as file authorization. Authorization must still be enforced during mTLS/API/file transfer.

## Peer Connectivity Storage Rules

Connectivity check results must be stored locally before peer selection.

Do not keep peer reachability only in process memory.

If the project already has peer tables, update the existing schema rather than creating duplicate tables.

## Peer Selection Rules

Before downloading a file, Drone must choose a source peer using stored peer connectivity results.

Peer selection should consider:

1. Overmind authorization.
2. Peer swarm membership.
3. File availability.
4. Recent successful TCP connectivity.
5. Recent successful mTLS verification.
6. LAN reachability.
7. WAN reachability.
8. Latency.
9. Transfer history.
10. Peer freshness.
11. Failure count.
12. Backoff status.

Preferred order:

```text
1. Authorized LAN peer with recent TCP success and valid mTLS.
2. Authorized WAN peer with recent TCP success and valid mTLS.
3. Recently reachable peer with lowest latency and valid mTLS.
4. Defer transfer or ask Overmind for alternate source.
```

Do not select peers where:

```text
supports_p2p = false
supports_mtls = false
last_seen_at is stale
latest TCP check failed
latest TCP check is too old
mTLS identity is invalid
peer is not authorized by Overmind
peer is from another swarm
peer is in backoff due to repeated failures
```

Adjust the freshness window based on the project’s expected network volatility.

## mTLS Requirements

All Drone-to-Drone downloads must use mTLS certificates created by the application.

Rules:

1. Use TLS for every Drone-to-Drone file transfer.
2. Require client certificate authentication.
3. Require server certificate verification.
4. Certificates must be created, stored, rotated, and managed by the application.
5. Do not use self-signed certificates without an application trust model.
6. Do not disable hostname or certificate verification.
7. Do not fall back to plaintext transfer.
8. Do not allow file download endpoints without mTLS.
9. Bind certificate identity to the Drone identity.
10. Validate that the certificate presented by a peer matches the expected peer Drone identity.
11. Store only non-secret certificate metadata such as fingerprint, subject, issuer, serial, and expiration.
12. Never log private keys.

The application-created certificate system should define:

```text
local Drone certificate
local Drone private key
trusted CA or trust bundle
peer certificate identity mapping
certificate rotation process
certificate expiration handling
certificate revocation or invalidation strategy
```

## Certificate Storage Rules

Private keys must be protected.

Do not:

- commit certificates or private keys,
- log private keys,
- send private keys to Overmind unless explicitly part of a secure designed flow,
- store private keys in world-readable paths,
- copy private keys into prompts or issue logs,
- expose certificates in API responses unless they are public certificate material and intended to be exposed.

Recommended persistent path pattern:

```text
/userdata/system/bff/certs/
```

Recommended files:

```text
/userdata/system/bff/certs/drone.crt
/userdata/system/bff/certs/drone.key
/userdata/system/bff/certs/ca.crt
```

Private key permissions should be restrictive where supported:

```bash
chmod 600 /userdata/system/bff/certs/drone.key
chmod 644 /userdata/system/bff/certs/drone.crt
chmod 644 /userdata/system/bff/certs/ca.crt
```

If Batocera filesystem constraints affect permissions, document the limitation and use the safest available approach.

## mTLS Validation Rules

During transfer, validate:

1. The peer presents a certificate.
2. The certificate chains to the application-trusted CA or trust bundle.
3. The certificate is not expired.
4. The certificate identity maps to the expected peer Drone ID.
5. The peer is authorized by Overmind.
6. The peer belongs to the same swarm.
7. The requested file is allowed to be served.
8. The local Drone is authorized to request that file.

mTLS proves peer identity. It does not replace application-level authorization.

Both are required:

```text
mTLS identity validation
+
application authorization
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
request file by file_id or sync_item_id
resolve file_id to approved local path
verify path is inside approved root
verify requester is authorized
stream file
```

## Path Safety Rules

Before serving any local file:

1. Resolve the requested path.
2. Normalize it.
3. Confirm it is inside an allowed root.
4. Reject `..` traversal.
5. Reject symlink escapes where possible.
6. Reject files not indexed or authorized for sync.
7. Prefer file IDs over raw paths in APIs.

Approved roots may include:

```text
/userdata/roms
/userdata/saves
/userdata/system/configs
```

Use the project’s actual Batocera paths when known.

## Transfer Source Decision Rules

Before initiating a download:

1. Ask local database for eligible peers.
2. Filter to Overmind-authorized peers.
3. Filter to peers with recent successful TCP check.
4. Filter to peers with valid mTLS verification.
5. Filter to peers known to have the requested file.
6. Prefer LAN over WAN.
7. Prefer lower latency.
8. Avoid peers with recent transfer failures.
9. Start transfer using mTLS.
10. Record transfer result locally.

Do not perform expensive peer discovery inline during a user-facing request unless required. Prefer background refresh plus cached peer selection.

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

- peer metadata refresh start/end,
- number of peers returned by Overmind,
- number of peers checked,
- TCP check success/failure counts,
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

- Does peer discovery come from Overmind?
- Is peer connectivity tested periodically?
- Are TCP check results stored locally?
- Is stored connectivity used before download source selection?
- Are stale connectivity records avoided?
- Is LAN preferred over WAN where appropriate?
- Is every Drone-to-Drone download protected by mTLS?
- Does the client verify the server certificate?
- Does the server verify the client certificate?
- Is the certificate identity mapped to Drone identity?
- Is Overmind authorization still checked?
- Are private keys protected and never logged?
- Are file paths validated against approved roots?
- Are file transfers streamed?
- Are retries bounded?
- Is failure/backoff state persisted?
- Are logs useful but not sensitive?

## Common Failure Patterns

Look for these first:

- Drone downloads from the first peer returned by Overmind without checking reachability.
- TCP checks run but results are not stored.
- TCP check results are stored but not used for peer selection.
- Peer checks block UI/API requests.
- Peer connectivity is stored only in memory.
- Drone falls back to non-TLS download after TLS failure.
- TLS verification is disabled.
- Client certificate is optional instead of required.
- Certificate identity is not mapped to Drone ID.
- File endpoint accepts raw paths without validation.
- Drone serves files outside approved Batocera roots.
- Peer failure causes infinite retry loop.
- WAN peer is selected even though LAN peer is reachable.
- Overmind peer metadata is treated as proof of reachability.

## Expected Output Format

When completing P2P transfer or security work, respond using this format:

```text
Root cause / objective:
...

Peer discovery changes:
...

TCP connectivity check changes:
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
- trust arbitrary peer addresses,
- trust peer reachability without TCP checks,
- choose download peers without using stored connectivity results,
- use in-memory-only peer connectivity state,
- expose file transfer endpoints without authorization,
- serve arbitrary raw file paths,
- allow path traversal,
- load large files fully into memory before sending,
- retry forever without backoff.

## Default Bias

When unsure, choose the option that keeps:

- peer discovery authorized by Overmind,
- connectivity periodically checked,
- connectivity results stored locally,
- peer selection based on recent reachability,
- LAN preferred when safe and available,
- downloads protected by mTLS,
- certificate identity tied to Drone identity,
- file paths validated,
- transfers streamed,
- retries bounded,
- secrets protected,
- local state durable across restarts.