# Drone identity, clone detection and disk images

A Drone's swarm identity is a long-lived self-signed certificate + private key
(`drone.crt` / `drone.key`), a logical Drone id (`device-id`) and the peers that pinned
them. Copying a whole disk copies all of it, so two machines would claim to be the same
Drone. Drone therefore **binds its identity to the hardware it was provisioned on** and
provisions a fresh identity by itself when it wakes up on different hardware.

## User workflow (no special imaging mode)

1. Install and configure Drone; pair it with other Drones if you like.
2. Image the Batocera disk normally (dd, Clonezilla, a vendor tool…). The Drone may be
   running or stopped, paired or unpaired.
3. Restore the image to another physical machine.
4. Boot it. The clone detects different hardware, destroys the inherited keys, removes
   the inherited swarm state, creates a brand-new identity and starts as an independent,
   unpaired Drone. Nothing needs to be typed, approved or deleted.
5. Pair the new Drone into a swarm whenever you want, through the normal pairing flow.

The original keeps its identity and its swarm. Nothing is announced to the original's
peers: the clone never sends a removal or rotation on the original's behalf, because it is a
*new device*, not a replacement.

Deploying one image to 10, 50 or 100 machines works the same way — every machine
provisions its own identity independently; there is no coordinator.

## What is checked (and what is not)

`app/common/hardware_identity.py` (`HardwareIdentityProvider`) reads, where present:

| Evidence | Source | Weight |
| --- | --- | --- |
| DMI product UUID | `/sys/class/dmi/id/product_uuid` | primary |
| Raspberry Pi / device-tree board serial | `/proc/cpuinfo` `Serial`, `/sys/firmware/devicetree/base/serial-number` | primary |
| DMI board / product / chassis serial | `/sys/class/dmi/id/*_serial` | secondary |

* Values are normalized, and empty / placeholder / known-duplicated ones ("To Be Filled By
  O.E.M.", `0000…`, `03000200-0400-0500-0006-000700080009`, …) are discarded.
* Each kind is hashed with SHA-256 (versioned: `fingerprint_version` 1); only those digests
  are stored. Raw serials are never logged, stored or returned by the API — the status
  endpoint shows kinds and short digest prefixes only.
* **Never** used as identity: hostname, IP address, a lone MAC, disk/filesystem UUIDs,
  `/etc/machine-id`, anything that only lives on the cloneable disk.
* Containers are treated as having no hardware identity (they report the *host's* DMI),
  so Docker test swarms never "detect" each other as clones; each container gets its own
  certificate from its own volume (the image sets `DRONE_CONTAINER=1`; `/.dockerenv`, podman and
  cgroup markers are also recognized). Give every container a distinct `DRONE_DEVICE_ID` and its
  own `/userdata` volume — never bake a populated `drone-app/` identity directory into an image.
  To emulate cloned hardware in an integration test, point `DRONE_HARDWARE_ID_ROOT` at a fixture
  tree.

### Verdicts

| Verdict | Meaning | Action |
| --- | --- | --- |
| same | the unit-unique identifiers match | keep everything, write nothing |
| different | every comparable identifier differs (primary identifiers decide; secondary serials only when no primary can be compared) | **automatic clone provisioning** |
| insufficient | an identifier that used to exist is now missing, or nothing comparable | keep keys/state, hold swarm networking, ask an administrator |
| contradictory | some identifiers match, others differ (or two sources disagree) | keep keys/state, hold swarm networking, ask an administrator |

A *missing* identifier is never read as *new* hardware.

## The identity binding

`<userdata>/system/drone-app/identity/binding.json` (mode 0600, directory 0700) holds the
schema version, a random installation id, the logical Drone id, the identity-certificate
fingerprint, the hashed hardware evidence + its fingerprint version, the provisioning
time/origin and a short event history. It is bookkeeping, **not proof of hardware**: it is
copied with the disk, which is exactly why only a *confident hardware mismatch* reveals a
clone. It is created when the identity is created, reused on every normal start (no writes),
survives Drone and Batocera upgrades, and is reconciled with the certificate on disk and the
device id (a rotation from the admin UI, or automatic renewal of an expired certificate, updates it). A
*valid* certificate that the binding never recorded (for example one copied in from another installation)
is not adopted silently: it holds swarm networking (`certificate_mismatch`) until an administrator resolves it.

## Startup order

`main()` runs `ensure_identity()` after logging is configured and before **any** listener,
poller, discovery worker, peer worker, sharing poller, share/NFS replay, download manager or
Tailscale bootstrap exists:

1. take the exclusive provisioning lock (`identity/provisioning.lock`, flock + in-process lock);
2. read the journal, binding, certificate and current hardware evidence;
3. decide (same → ready; first install → provision; mismatch → clone provisioning;
   insufficient/contradictory → recovery);
4. verify the result, release the lock, then start normal networking.

Concurrent starts cannot create two identities, and repeated starts are idempotent.

## Clone provisioning (what happens on different hardware)

State machine: `UNINITIALIZED → VALIDATING → CLONE_DETECTED → PROVISIONING → READY`, or
`RECOVERY_REQUIRED`. Progress is journaled in `identity/journal.json` (atomic writes, fsync).

1. **Retire the inherited identity first**: the old `drone.key` is overwritten and removed
   along with `drone.crt` and the HTTPS leaf it signed. A failed attempt therefore can never
   reactivate the inherited identity.
2. **Targeted swarm reset** in one SQLite transaction + the pin directory (see below).
3. **New logical id**: random, locally administered, MAC-shaped (not derived from the NIC, so
   cloned VMs / spoofed MACs cannot collide). A `DRONE_DEVICE_ID` copied along with the disk is
   replaced too: the retired id is recorded in the binding so the stale environment pin cannot
   bring it back.
4. **Tailscale**: see below.
5. **New certificate** via the existing `DroneCertificateManager` (generated in a private
   scratch directory and renamed into place); verified to differ from the inherited one and
   to match its key. The HTTPS leaf is re-issued by the normal startup reconcile.
6. Write the binding, delete the journal, log `[Identity] Clone provisioning completed`.

Every cleanup step must succeed before the clone becomes `READY`: a pinned peer certificate that cannot
be deleted, an imported credential or stored enrollment key that cannot be revoked, or an inherited Tailscale node that can be neither cleared nor confirmed offline (`tailscale down` must succeed), fails the run
instead of leaving a partly trusting clone, and the next start retries from the journal.

An interrupted run (power loss, full disk, `openssl` failure) leaves `RECOVERY_REQUIRED`
(swarm networking held) and resumes on the next start; a certificate already produced by the
earlier attempt is reused.

### State that is cleared

Paired peers, discovered peers (including `identity-conflict` rows), pinned peer certificates
(`local-peer-certs/`), the swarm membership roster/epochs/tombstones, the pairing code, peer
health checks, the peer route cache, sync activity, tailnet "forgotten peers", network-share
and NFS-export authorizations/references, and queued downloads that name a source peer.
Credentials imported *from a peer* (VPN/SMTP) are revoked using the existing revocation
path; our own VPN/SMTP config stays, with sharing switched off. The stored Tailscale auth key
is cleared.

### State that is preserved

Login credentials, ROMs/BIOS/saves/media, artwork and metadata caches, game and emulator
configuration, UI preferences, notification/audit history, Wi-Fi/automation settings, the
Torrents queue and other settings that do not name a swarm identity. Foreign keys and schema
are untouched (idempotent `DELETE`s only; never a database rebuild).

## Tailscale on a clone

A clone also copies Tailscale's node key. Drone removes only the **local** daemon state
(`/userdata/system/tailscale/state`) after stopping `tailscaled`, then restarts it. It never
runs `tailscale logout` (that would deregister the node key the original still uses) and never
re-enrolls by itself: the clone comes up *not enrolled* (`NeedsLogin`) and the Swarm page's
Tailnet card shows that it needs a new auth key. LAN pairing keeps working independently.
Because the DRONE_TAILNET service can start before Drone, the clone may briefly present the
inherited node; Drone resets it within seconds of starting. If the daemon cannot be stopped,
the node is taken offline with `tailscale down` and the reset retried on the next start.

## Existing installations (one-time migration)

An installation without a binding keeps its certificate, pairings and swarm exactly as they
are; a hardware baseline is recorded (`binding_migrated`) if usable identifiers exist.

**Limitation:** a disk that was *already* cloned before this version cannot be told apart from
the original by comparing hardware, because nothing was recorded. Both machines adopt their
own hardware as the baseline and still share a certificate. The recovery path for such a clone:
on the **clone**, call `POST /v1/api/admin/identity/resolve` with `{"action": "new", "password": …}`
(real login session + password). That provisions a new identity exactly like automatic clone
provisioning, and the clone can then be paired normally. New installations are clone-aware from
their first boot, and every machine that has run this version is protected from then on.

## Hardware replacement / recovery

*Planned motherboard swap*: before replacing the board, an administrator calls
`POST /v1/api/admin/identity/authorize-hardware-change` (real login session, same-origin JSON,
**password re-entry**; default 24 h, max 72 h). The next hardware change then rebinds this
identity to the new hardware instead of provisioning a new one, and the authorization is
consumed. **Do not image the disk while an authorization is pending** — every machine booted
from that image would keep this identity. Cancel with `POST …/cancel-hardware-change`.

*Held states* (`RECOVERY_REQUIRED`: identifiers missing or contradictory, unreadable binding,
failed provisioning): keys and state are left intact, **swarm networking is held** (no
discovery, pairing, `/peer/*` surface, mTLS listener, peer workers, sharing, share replays,
downloads; outbound peer TLS contexts are refused) while the admin UI keeps working. An
administrator resolves it with `POST /v1/api/admin/identity/resolve` (`action: keep` — keep
this identity on this hardware and record it as the new baseline; or `action: new` —
provision a new identity). Both need a real session and the password; the Drone restarts
afterwards. Peers can never cause a reset.

## Duplicate-identity detection (defence in depth)

When two endpoints claim one identity, Drone isolates the connection and logs it
(`[Identity] Duplicate identity observed …`, stored in `app_events` namespace
`identity_collisions`); it never overwrites a pinned certificate, never revokes the original
and never trusts peer-supplied hardware claims. Covered: an mTLS caller presenting *this*
Drone's own certificate (HTTP 409), a pairing request using our certificate or device id,
membership gossip carrying our certificate, and discovery announcements with our device id or
certificate fingerprint (stored as `identity-conflict:*` rows, never paired).

## Diagnosing ambiguous identities

`GET /v1/api/admin/identity/status` (also summarized as **Drone Identity** in Admin → System
Info) returns the state, status, `hardware` (kinds present, fingerprint prefix, architecture,
`virtualized`, `container`, `unavailable_reason`), `hardware_verification`, any `recovery`
reason and the recent identity events. `unavailable_reason` values: `no_unique_hardware_identifier`
(placeholders/missing), `conflicting_hardware_identifiers`, `container_shares_host_hardware`,
`disabled`. On Batocera `product_uuid` and the DMI serials are root-readable only; run Drone as
root (the supported mode) or Drone will see no DMI evidence.

Environment: `DRONE_HARDWARE_ID_ROOT` (read a fixture tree instead of `/`, for tests/emulated
hardware), `DRONE_HARDWARE_IDENTITY_DISABLED=1` (no hardware evidence), `DRONE_IDENTITY_DIR`.
Identity management is skipped for `USE_FAKE_DATA`, `DRONE_MTLS_MODE=managed` and explicit
`TLS_CERT_FILE` deployments, where certificates are provisioned outside Drone.

## Known limitations

* **Identical virtual hardware** (a VM cloned with the same UUID/serials), **spoofed
  identifiers**, and platforms with **no trustworthy unique identifier** (many SBCs, containers,
  hosts where DMI is unreadable) cannot be detected: the clone matches the original. The
  binding then records "verification unavailable" and the identity is kept. Provision such
  machines from a fresh install or rotate/re-pair manually.
* The binding file is cloneable bookkeeping, not a proof; an attacker who controls the booted
  system can lie about hardware.
* A clone of an *unbound* (pre-upgrade or identifier-less) installation adopts a baseline
  instead of provisioning.
* Pre-authorizing a hardware change and imaging the disk before it is consumed duplicates the
  identity.
* The old identity's peers still hold the original's pins; that is intended — they keep trusting
  the original and never learn about the clone.
