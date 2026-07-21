---
name: drone-live-debugging
description: Use this when debugging a real, running Drone device — on your local network via SSH, or a Drone you can't directly reach because it's on a different network/behind NAT, in which case identify and inspect it through another Drone's own cached view of it. Covers log locations, the local SQLite state DB, peer-cert/trust inspection, and live-reproducing swallowed-exception peer calls.
---

# Debugging a Live Drone

For the top-level methodology this plugs into (when to reach for live debugging,
credentials/identity gotchas, the anchor-first approach), see the
`bff-live-debugging` skill in `.github`. This skill is the Drone-specific depth:
what's on the device, how to read it, and how to investigate a Drone you can't SSH
into directly.

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
/userdata/system/logs/drone-app/drone.log      # narration -- pairing/tailnet/automation/
                                                #  transfer lines from every subsystem
/userdata/system/logs/drone-app/startup.log    # full daemon output incl. HTTP access log
/userdata/system/logs/drone-app/stdout.log     # same content as drone.log + HTTP access log
/userdata/system/logs/drone-app/stderr.log     # tracebacks / uncaught errors
```

A Drone that hasn't been redeployed since the Overmind package was retired may still
be writing to the old filename, `overmind.log` (the default only changed name; an
already-running process keeps whatever it opened at startup) — check both if
`drone.log` is missing or empty.

`drone.log`/`stdout.log` narrate the Drone's own decisions with timestamps — this is
almost always the fastest way to find out what really happened, because each step
prints a line. The shared logging helper (`_drone_log` in `common/logging_setup.py`)
is used by every subsystem, not just one. Expect lines like:

- `Peer health check: source=<id> target=<id> status=pass|fail address=<addr> latency=<ms>ms`
  (the background paired-peer health loop, `peer_workers.py`).
- `Idle-volume automation set volume to <n>% after <s>s idle`,
  `Idle-game-exit automation exited the running game after <s>s idle` /
  `Idle-game-exit automation could not exit the game: <reason>`,
  `Automation poller thread started: poll_seconds=<n>` (`device/automation.py`).
- Pairing/tailnet lines from `transfer/local_network.py` and `device/tailnet_service.py`.
- HTTP access lines for every `/v1/api/admin/*` and `/v1/api/peer/*` request.

Older log history (from before the Overmind package was retired) may still contain
`Claimed N Overmind action(s) for <device_id>: <action>` / `Executing Overmind action
<name> (<id>) payload={...}` / `Processed Overmind action <name> (<id>): <status> -
<message>` lines. That action-dispatch code no longer exists — local-network P2P is
always on now (no mode/toggle at all), so no current build can print these
regardless of env vars; treat any occurrence as historical, from a log file that
predates the update to the current app version.

```bash
# grep for a game name, peer/device id, or config key — whatever anchor you have
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
for ns in (\"local_paired_peers\", \"local_peer_checks\", \"automation_config.json\"):
    row = db.execute(\"SELECT payload FROM app_state WHERE namespace=? AND state_key=?\", (ns, \"payload\")).fetchone()
    print(\"===\", ns, \"===\")
    print(json.dumps(json.loads(row[0]), indent=2) if row else \"MISSING\")
"'
```

Useful namespaces — **and, critically, who writes each and when** (the store only
holds the latest snapshot; knowing the writer + cadence is how you reason about
what the state was at a *past* failure instant). The fleet is peer-to-peer (no
central hub), so these are all populated locally by this Drone's own workers:

- `local_paired_peers` — this Drone's own paired-peer map: `drone_id`, self-reported
  `name`, `local_ip`, `tailnet_ip`, `public_ip`, advertised port, and the pinned
  certificate fingerprint captured at pairing time. **Writer: whatever pairing flow
  ran** (LAN pairing-code accept, or tailnet code-free pairing) — updated only when
  peers are paired/forgotten, not on a timer.
- `local_peer_checks` — **this Drone's own ground-truth probe results** against its
  paired peers: `target_drone_id`, `status` (`pass`/`fail`), `latency_ms`,
  `target_address` actually probed, `failure_reason` (verbose — includes the actual
  SSL/network error text). **Writer: the local-network health loop
  (`transfer/peer_workers.py:_start_local_network_workers`'s `health_loop`), every
  `DRONE_LOCAL_HEALTH_INTERVAL_SECONDS` (default 30s), overwriting the whole list
  after probing every paired peer.** Its history is reconstructible: each run logs
  one `Peer health check: source=... target=... status=... latency=...` line per
  peer, so you can line up "what did the checks say at time T" from the log even
  though the store only keeps the latest run. **This is also the fastest way to
  catch a stale/bad peer certificate** — a `failure_reason` like `missing or
  incorrect trusted CA bundle ... self-signed certificate` means the cached cert for
  that peer no longer matches what the peer presents (common after a peer reinstall
  that kept the same `device_id`).
- `automation_config.json` — idle-volume/idle-game-exit/wifi-recovery config
  (`device/automation.py`); read live by the automation poller thread every
  `AUTOMATION_POLL_SECONDS` (default 15s), not cached/stale in the way the peer
  caches are.
When a namespace's provenance matters, find **every** writer before reasoning:
`grep -rn "local_peer_checks" app/` (or whichever namespace) — a cache with a
scheduled wholesale overwriter means "state at failure time" must come from the
writer's logs, never from the current store contents.

Also read-only and useful: the ROM/BIOS cache tables (`rom_cache_entries`,
`bios_cache_entries`, `cache_state`) if you need to confirm what the Drone
currently believes is on disk, independent of what its own admin UI shows.

### Peer certificate trust — files, not just the check result

```bash
sshpass -p linux ssh -o StrictHostKeyChecking=no root@<host>.local \
  'ls -la /userdata/system/drone-app/peer-certs/ /userdata/system/drone-app/local-peer-certs/'
```

File names are `<device_id with colons replaced by underscores>.crt`. Compare
`mtime` across peers — a cert cached far more recently than its siblings suggests
that peer's identity/cert was recently re-fetched (e.g. after a rotation), while
one that's stale relative to a peer's *actual* current cert is exactly the
`local_peer_checks` failure pattern above.

### Reproducing a swallowed-exception peer call live

`_check_peer` (`app/transfer/peer_connectivity.py`) catches every exception from its
HTTP call and reduces it to a `failure_reason: str(error)` string — informative, but
it loses the exception's class and traceback. To see the real error, write a tiny
script that imports the app's own modules and calls the underlying primitive
**without** that reduction, then run it *on the Drone itself* so it uses the same
cached certs and config as production:

```python
# probe.py
import sys, os, traceback
sys.path.insert(0, ".")
os.environ.setdefault("USERDATA_ROOT", "/userdata")
from transfer.peer_connectivity import _check_peer, _peer_get_json_for_peer
from common.settings import Settings

settings = Settings.from_env()
peer = {  # copy real values from local_paired_peers above
    "drone_id": "58:47:ca:7e:38:57",
    "local_ip": "192.168.0.207",
    "tailnet_ip": "100.94.12.3",
    "public_ip": "72.176.228.250",
    "scheme": "https", "api_port": 443,
}
print(_check_peer(settings, peer, config={}))  # the reduced failure_reason, if any

try:
    print(_peer_get_json_for_peer(peer, "/health", settings, peer_id=peer["drone_id"], config={}))
except Exception:
    traceback.print_exc()  # the real error, not reduced to a string
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

The same on-device technique works for **pure decision functions**, not just
network calls — and it's the decisive move when a failure looks impossible from
the current state. Example: reproduce the actual address/connectivity check the
current default (explicit-request) transfer flow makes for one paired peer:

```python
# probe_peer.py — run from /userdata/system/drone-app/app
from common.settings import Settings
from transfer import local_network as _local_network
from transfer.peer_connectivity import _check_peer, _preferred_peer_address

settings = Settings.from_env()
peer = next(p for p in _local_network.paired_peers(settings) if p.get("drone_id") == "58:47:ca:7e:38:57")
print("preferred address:", _preferred_peer_address(peer, settings=settings, peer_id=peer["drone_id"]))
print("live check:", _check_peer(settings, peer, config={"network_mode": "local_network"}))
```

If this returns the right peer *now* but the same call failed minutes ago, the code
is exonerated — the input state differed at the failure instant. Pivot to the
namespace-provenance analysis above (who overwrote the input, when, with what) and
reconstruct the failure-time state from the writers' log lines rather than
re-checking the code again.

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

If that fails, and there is no central hub to query instead, your one indirect
option is:

1. **Ask a Drone you *can* reach for its cached view of the target.** Any Drone that
   has the target paired maintains `local_paired_peers`/`local_peer_checks` about it
   (Case 1 above) — SSH into that reachable Drone and read its cache for the
   `drone_id` you actually care about. This gives you the target's last-known
   `local_ip`/`tailnet_ip`/`public_ip` and the *reaching* Drone's live connectivity
   probe result against it — genuinely useful even though it's secondhand, and it
   doesn't require reaching the target at all. If the reaching Drone is also on a
   shared tailnet with the target, `tailscale status` on it (via
   `device/tailnet_service.py:tailnet_status`) is another independent secondhand
   reachability signal.

If the target Drone's `public_ip` is populated and you need to check whether it's
*currently* live from the outside, a plain unauthenticated probe is safe and
read-only:

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
- Treat `local_peer_checks`/`local_paired_peers` as a **cache**, not live truth —
  it reflects the last time this Drone happened to probe, which may be up to
  `DRONE_LOCAL_HEALTH_INTERVAL_SECONDS` stale.

## Expected output format

```text
Device(s) inspected:
... (hostname/IP used, hostname/uname confirmation, drone_id if known)

Reachability:
... (direct SSH, or indirect via another paired Drone's cache)

Evidence:
... (exact log lines, cache contents, or probe reproduction output)

Conclusion:
... (proven vs. likely; what you could not verify and why)
```
