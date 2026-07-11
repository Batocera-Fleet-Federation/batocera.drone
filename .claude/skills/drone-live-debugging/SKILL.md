---
name: drone-live-debugging
description: Use this when debugging a real, running Drone device — on your local network via SSH, or a Drone you can't directly reach because it's on a different network/behind NAT, in which case identify and inspect it through Overmind and through another Drone's own cached view of it. Covers log locations, the local SQLite state DB, peer-cert/trust inspection, and live-reproducing swallowed-exception peer calls.
---

# Debugging a Live Drone

For the cross-repo methodology this plugs into (CloudWatch, migrations, the
in-memory-dict-vs-Lambda bug class), see the `bff-live-debugging` skill in
`.github`. This skill is the Drone-specific depth: what's on the device, how to
read it, and how to investigate a Drone you can't SSH into directly.

## Goal

Get hard evidence of what a real Drone actually did (or didn't do), read-only,
without guessing from source code alone.

## Case 1 — a Drone on your local network (you have SSH)

Credentials: `.github/.credentials`, `root`/`linux`, `sshpass -p linux ssh -o
StrictHostKeyChecking=no root@<host>.local '<cmd>'`.

**Identity caveat first:** `*.local` is mDNS — it resolves to whatever answers on
*your current LAN*, not a fixed device. Don't assume a hostname means the same
physical box it meant last session. Verify:

```bash
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local 'hostname; uname -a'
```

`uname -a` also tells you real hardware (`aarch64` = genuine Raspberry Pi,
`x86_64` = a PC/VM regardless of what its Batocera hostname implies). If the host
key looks wrong (`UpdateHostkeys is disabled because the host key is not trusted` /
sudden `Permission denied`), a *different* device is now answering that mDNS name —
clear the stale entry (`ssh-keygen -R <host>.local`) and reconnect; retry once
before assuming credentials changed, SSH auth failures over a flaky link are
common and transient.

### Log files — start here, always

```text
/userdata/system/logs/drone-app/overmind.log   # Overmind sync/heartbeat/action narration
/userdata/system/logs/drone-app/startup.log    # full daemon output incl. HTTP access log
/userdata/system/logs/drone-app/stdout.log     # same content as overmind.log + HTTP access log
/userdata/system/logs/drone-app/stderr.log     # tracebacks / uncaught errors
```

`overmind.log`/`stdout.log` narrate the Drone's own decisions with timestamps —
this is almost always the fastest way to find out what really happened, because
each step prints a line: `Claimed N Overmind action(s) for <device_id>: <action>`,
`Executing Overmind action <name> (<id>) payload={...}`, `Processed Overmind action
<name> (<id>): <status> - <message>`, `ROM sync activity push started/succeeded:
... status=... rom=...`, `Failed to report Overmind action completion <id>:
HTTPError status=... url=...`.

```bash
# grep for a game name, action id, or system — whatever anchor you have
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local \
  'grep -ai "dark souls" /userdata/system/logs/drone-app/*.log'

# then pull the full sequence around the timestamp that turns up, filtering noise
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local \
  'grep -n "2026-07-11T03:4" /userdata/system/logs/drone-app/stdout.log \
   | grep -v "GET /v1/api/admin/downloads\|GET /v1/api/admin/system\|heartbeat"'
```

Note: `grep` over these files sometimes reports "binary file matches" (embedded
control characters from a crashed process or partial write) and silently skips the
match — if a plain `grep` comes up empty for something you're sure is there, retry
with `grep -a` to force text mode.

### The local SQLite state DB — "looking in the database" on the Drone side

`/userdata/system/drone-app/rom_metadata_cache.sqlite3` holds more than the ROM
cache — it's the Drone's general key/value state store (table `app_state`,
columns `namespace`, `state_key`, `payload`) plus the relational ROM/BIOS cache
tables. Always open it read-only (`?mode=ro`) so you can't corrupt live state:

```bash
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local 'python3 -c "
import sqlite3, json
db = sqlite3.connect(\"file:/userdata/system/drone-app/rom_metadata_cache.sqlite3?mode=ro\", uri=True)
for ns in (\"overmind_swarm.json\", \"peer_checks.json\"):
    row = db.execute(\"SELECT payload FROM app_state WHERE namespace=? AND state_key=?\", (ns, \"payload\")).fetchone()
    print(\"===\", ns, \"===\")
    print(json.dumps(json.loads(row[0]), indent=2) if row else \"MISSING\")
"'
```

Useful namespaces:
- `overmind_swarm.json` — this Drone's **cached view of every other Drone**:
  `drone_id`, self-reported `name`, `online`, `public_ip`, `public_resolvable`,
  `reachable_url`, `public_reachable_url`, `edge_online`, `last_speed_sample`. This
  is the input to peer selection (`select_best_peer` in
  `app/transfer/peer_selection.py`) — if a sync failed with "no healthy source
  peer", check whether the source's row here shows `online: false` or
  `public_resolvable: false` at roughly the right time.
- `peer_checks.json` — the last connectivity probe results this Drone ran against
  its peers: `target_drone_id`, `status` (`pass`/`fail`), `latency_ms`,
  `failure_reason` (verbose — includes the actual SSL/network error text), and
  which local cert file was used. **This is the fastest way to catch a stale/bad
  peer certificate** — a `failure_reason` like `missing or incorrect trusted CA
  bundle ... self-signed certificate` means the cached cert for that peer no
  longer matches what the peer is actually presenting (common after a peer's
  device was reinstalled/reset while keeping the same `device_id`).

Also read-only and useful: the ROM/BIOS cache tables (`rom_cache_entries`,
`bios_cache_entries`, `cache_state`) if you need to confirm what the Drone
currently believes is on disk, independent of what the Overmind UI shows.

### Peer certificate trust — files, not just the check result

```bash
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local \
  'ls -la /userdata/system/drone-app/peer-certs/ /userdata/system/drone-app/local-peer-certs/'
```

File names are `<device_id with colons replaced by underscores>.crt`. Compare
`mtime` across peers — a cert cached far more recently than its siblings suggests
that peer's identity/cert was recently re-fetched (e.g. after a rotation), while
one that's stale relative to a peer's *actual* current cert is exactly the
`peer_checks.json` failure pattern above.

### Reproducing a swallowed-exception peer call live

Several Drone peer functions (e.g. `_resolve_rom_by_gamelist_id_from_peer` in
`app/transfer/peer_download.py`) do `try: ... except Exception: return None` around
an HTTP call to another Drone — safe for the app, but it throws away the real
error. To see it, write a tiny script that imports the app's own modules and calls
the underlying primitive **without** the swallowing except, then run it *on the
Drone itself* so it uses the same cached certs and config as production:

```python
# probe.py
import sys, os, json, traceback
sys.path.insert(0, ".")
os.environ.setdefault("USERDATA_ROOT", "/userdata")
from transfer.peer_download import _resolve_rom_by_gamelist_id_from_peer, _peer_get_json, _peer_address
from common.settings import Settings

settings = Settings.from_env()
peer = {  # copy real values from overmind_swarm.json above
    "drone_id": "58:47:ca:7e:38:57",
    "public_reachable_url": "https://72.176.228.250",
    "reachable_url": "https://192.168.0.207",
    "public_resolvable": True,
    "scheme": "https", "api_port": 443,
}
print(_resolve_rom_by_gamelist_id_from_peer(settings, {}, peer, "ps3", "24645"))

address = _peer_address(peer)
try:
    print(_peer_get_json(f"{address}/v1/api/peer/roms-by-id/ps3/24645", settings, peer_id=peer["drone_id"], config={}))
except Exception:
    traceback.print_exc()  # the real error, not swallowed
```

```bash
sshpass -p linux scp -o StrictHostKeyChecking=no ./probe.py root@<host>.local:/tmp/probe.py
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local \
  'cd /userdata/system/drone-app/app && python3 /tmp/probe.py'
```

Running `cd .../app` first matters — the app's modules import as a flat package
(`from transfer... `/`from common...`), not `app.transfer...`, when run this way.

A successful reproduction is informative, not proof the *original* failure had the
same cause — note that explicitly if you can't pin the exact historical moment
(cross-network peer connectivity can be transient).

### Process/version sanity check

Before trusting anything else, confirm you're actually looking at a live, current
process (a stale pre-update process is a real, recurring failure mode in this
fleet):

```bash
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local '
ps aux | grep main.py | grep -v grep
python3 -c "import sys; sys.path.insert(0,\"/userdata/system/drone-app/app\"); from app_version import drone_app_version; print(drone_app_version())"
'
```

If the process start time predates a known deploy, or the version is older than
`releases/latest`, that alone can explain the symptom — a restart
(`batocera-services restart DRONE_SERVER`) picks up the latest staged release, but
**never restart a remote Drone without explicit user approval.**

## Case 2 — a Drone you can't SSH to directly (different network/NAT)

The fleet is **outbound-only by design** (see the root `CLAUDE.md`): a Drone with
no port-forwarding has no way to accept an inbound SSH connection from you unless
you're on its LAN or the user has explicitly set up remote access. Don't assume
you can reach every Drone the same way — check first:

```bash
ping -c 1 -W 2 <name>.local   # only resolves if you're on the same LAN/mDNS segment
```

If that fails, you have two indirect options, in order of how much they tell you:

1. **Ask a Drone you *can* reach for its cached view of the target.** Every Drone
   maintains `overmind_swarm.json`/`peer_checks.json` about every other Drone it
   knows about (Case 1 above) — SSH into any reachable Drone and read its cache for
   the `drone_id` you actually care about. This gives you the target's
   self-reported `online`/`public_ip`/`reachable_url`/`edge_online` state and the
   *reaching* Drone's live connectivity probe result against it — genuinely useful
   even though it's secondhand, and it doesn't require reaching the target at all.
2. **Query Overmind directly for its device record.** Overmind is reachable from
   anywhere and holds the authoritative registration state (`last_seen`, IP/port,
   edge presence) for every Drone regardless of network topology — see the
   `overmind-live-debugging` skill (`batocera.overmind`) for how to pull this via
   CloudWatch/RDS instead of guessing from a Drone's possibly-stale local cache.

If the target Drone's `public_reachable_url` is populated and you need to check
whether it's *currently* live from the outside, a plain unauthenticated probe is
safe and read-only:

```bash
curl -sk -o /dev/null -w "http_code=%{http_code} time=%{time_total}\n" https://<public_ip>/health
```

(`-k` because these are self-signed certs — expected here, not a security issue
for a health probe.) Do not attempt to reach `/v1/api/admin/*` endpoints without
proper auth, and do not attempt to open any inbound path to a Drone that isn't
already there — this fleet is intentionally outbound-only.

## Safety rules

- Read-only. Never modify config, restart services, or delete files on a live
  Drone without explicit user approval, even mid-investigation.
- Never assume a hostname is the same device it was in a previous session —
  verify with `hostname`/`uname -a` every time before drawing conclusions.
- Don't try to make a NAT'd/unreachable Drone reachable (port-forwarding, opening
  ports) as a debugging shortcut — that's a network change, not a diagnosis.
- Treat `peer_checks.json`/`overmind_swarm.json` as a **cache**, not live truth —
  it reflects the last time this Drone happened to probe, which may be minutes
  stale.

## Expected output format

```text
Device(s) inspected:
... (hostname/IP used, hostname/uname confirmation, device_id from Overmind if known)

Reachability:
... (direct SSH, or indirect via which other Drone's cache / Overmind record)

Evidence:
... (exact log lines, cache contents, or probe reproduction output)

Conclusion:
... (proven vs. likely; what you could not verify and why)
```
