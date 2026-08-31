---
name: drone-vpn-management
description: Use this when designing, reviewing, debugging, or modifying the Drone's VPN admin tile — OpenVPN client configuration/connection management, .ovpn upload and rewriting, VPN credential storage, connect/disconnect process control, VPN status detection, connecting automatically on Drone startup (unconditional + retried, no opt-in toggle), self-healing/auto-reconnect on connection failure or a decrypt/replay-error flood (default-on, rate-limited), peer-to-peer VPN config/credential sharing between paired drones (single-hop-only provenance, share-revocation auto-disconnect/wipe), or app/device/vpn_manager.py, web/handlers_vpn.py, web/handlers_peer.py's _handle_peer_vpn_config.
---

# Drone VPN Management Skill

## Goal

Keep this skill accurate as the single source of truth for the VPN admin
tile's implementation details — the parent `drone-admin-features` skill only
carries a brief tile-level summary and points here for depth.

Provider-agnostic by design (Proton VPN, NordVPN, Private Internet Access,
...) — nothing in `vpn_manager.py` assumes a specific provider's `.ovpn`
wording beyond standard OpenVPN directives. Do not add provider-specific
branching; if a provider needs special handling, it almost certainly belongs
in `rewrite_ovpn_config()`'s generic rules instead.

## Architecture

```text
app/device/
  vpn_manager.py   # the whole feature: config rewrite, credential storage,
                    # connect/disconnect, status detection -- plain module
                    # functions, no class, no background thread
app/web/
  handlers_vpn.py   # admin route handlers (HandlersVpnMixin); delegates all
                     # real logic to vpn_manager.py -- including the sharing
                     # toggle and the pull-from-peer admin action
  handlers_peer.py  # _handle_peer_vpn_config: the mTLS GET /peer/vpn/config
                     # peer-serving endpoint (HandlersPeerMixin), delegates to
                     # vpn_manager.export_payload()
app/common/
  multipart.py        # shared multipart/form-data parser (also used by
                       # handlers_torrents.py's .torrent upload)
  install_paths.py     # drone_install_root() -- vpn_dir() is
                       # <install root>/vpn, fixed, not user-configurable
```

## Design: stateless, no background thread

Unlike `transfer/torrent_manager.py`'s `TorrentManager` (which runs its own
worker thread because a torrent queue has real scheduling work to do),
`vpn_manager.status()` recomputes the entire connection state **fresh on
every call** — scan `/proc` for the running PID, tail the log file, shell out
to `ip -4 addr show tun0` if connected. This is correct and sufficient because
a VPN connection is exactly one process; there's no queue, no concurrency
decision, nothing to schedule. The frontend already polls `GET /admin/vpn`
every 3s (same flash-free patch pattern as Torrents — see `drone-admin-features`),
so there's no need for a separately-cached state machine kept in sync by a
thread. Don't add one without a concrete reason a stateless recompute can't
handle.

`connect()`/`disconnect()` are guarded by a module-level `_CONNECT_LOCK`
purely to stop two racing clicks from spawning two openvpn processes —
everything else is unsynchronized by design.

## Root privilege model: direct subprocess, no IPC

The Drone app runs **as root** on a real device — confirmed by
`service_bootstrap.sh`'s own comment ("Drone runs as root...") and by
`launch_drone` exec'ing `python3 -m app.main` directly inside an
already-root shell, no `su`/`sudo`. This means spawning/signaling `openvpn`
needs zero privilege-escalation machinery: `connect()` and `disconnect()` use
plain `subprocess.run`/`os.kill`, exactly like the simpler
`automation.py:_recover_wifi()` precedent.

This is a **deliberate** choice not to extend the separate file-drop
"control worker" IPC (`DRONE_SERVICE_CONTROL_DIR`, used by
`device_control.py`'s non-root fallback path for things like emulator-kill)
to VPN. That IPC exists for actions that must work even in a hypothetical
non-root deployment; VPN management is a much rarer, opt-in admin action, not
a core hot path, so the added complexity of a second IPC channel wasn't
justified. If a genuine non-root deployment mode becomes common, revisit this
— today, VPN connect/disconnect will fail outright (with an honest error) if
the Drone process somehow isn't root.

## PID tracking via /proc, never `killall`

`_find_running_openvpn_pid(config_path, proc_root=Path("/proc"))` scans
`/proc/<pid>/cmdline` for a process whose `argv[0]` contains `openvpn` **and**
whose arguments contain our exact `--config <path>` string — the same
technique as `device/game_activity.py`'s `find_running_emulatorlauncher`,
including the injectable `proc_root` parameter for tests (fake a `/proc` tree
under a tmp dir rather than mocking `subprocess`). This precise match means
`disconnect()` can never touch an unrelated openvpn process that happens to be
running on the same box for some other reason — a real (if narrow) safety
property `killall openvpn` would not have.

`connect()` spawns with `--daemon`, which forks and detaches — the `Popen`
object's own PID is useless almost immediately, which is *why* PID discovery
has to go through `/proc` after the fact rather than tracking a subprocess
handle.

`disconnect()` sends `SIGTERM`, polls up to `VPN_DISCONNECT_GRACE_SECONDS`
for the PID to disappear, then escalates to `SIGKILL` if it's still alive.

## Config storage: fixed location, not user-configurable

`vpn_dir(settings)` is `<install root>/vpn/` (via
`common/install_paths.drone_install_root()`), matching Torrents' own
install-root-relative default — but unlike Torrents' watch folder, this is
**never exposed as a configurable field in the UI**, per the original feature
spec. The only override is the `DRONE_VPN_DIR` environment variable, which
exists **purely for test isolation / ops escape hatches** — never wire it to
a UI control.

```text
<vpn_dir>/client.ovpn   # the uploaded, rewritten config
<vpn_dir>/auth.txt       # username\npassword\n, chmod 600
<vpn_dir>/vpn.log         # openvpn's own --log output
```

### The test-isolation gotcha (read this before writing a new test)

Because `vpn_dir()` is intentionally **not** part of `Settings` (no
`settings.vpn_dir` field), a test that only overrides `USERDATA_ROOT` etc. via
`Settings.from_env()` does **not** isolate VPN file writes — every such test
silently writes real files (including fake credentials) into the actual
repo's `<repo>/vpn/` directory. This actually happened while building this
feature.

The fix, and the pattern every new test must follow: patch
`vpn_manager._drone_install_root` directly, and do it with
`test_case.addCleanup(patcher.stop); patcher.start()` — **not** a `with
mock.patch.object(...):` block — because the patch must outlive the narrow
`Settings.from_env()` call and stay active for the rest of the test body. See
`tests/test_vpn_manager.py`'s `_build_settings(test_case, root)` helper for
the exact shape to copy. Any *other* future feature that adds an
install-root-relative path outside `Settings` (Torrents' watch folder is
exempt — tests there always call `update_settings({"directory": ...})` with a
tmp path before doing any real I/O, which is a different, equally valid way
to avoid the same trap) should check for this class of bug too.

### Real live incident: a release-versioned deploy layout broke `drone_install_root()` outright

Found 2026-07-28 while live-debugging a user report of "Failed to connect:
Request failed: 400" on every VPN connect attempt (both auto-connect-on-boot
and a manual click). Root cause was in `common/install_paths.py`'s
`drone_install_root()`, not in this module — but it broke VPN specifically
and completely, because `vpn_dir()` is the one consumer with **no** way to
override the computed path from user-facing settings (Torrents' watch folder
is user-configurable and happened to mask the same underlying bug there).

The device's deploy mechanism lays out `<install root>/app` as a symlink
chain (`app -> current/app -> .releases/<version>/app`) for rollback-friendly
deploys. `drone_install_root()` used to assume `Path(__file__).resolve().
parents[2]` always lands two directories above a *stable* `app/` — true
before this layout existed, false now: Python's import machinery reports
`__file__` **already fully resolved through the symlink chain** (confirmed
live: `__file__` itself, before `drone_install_root()` even touches it, was
already `.../drone-app/.releases/0.1.91-.../app/common/install_paths.py`), so
a fixed `parents[2]` silently landed inside the versioned release directory
— which has no `vpn/` subfolder at all — instead of the stable install root.
Every `openvpn --config <that wrong path>/vpn/client.ovpn` invocation then
failed with `Error opening configuration file`, whose last output line
("Use --help for more information.") is what `connect()`'s error-reduction
surfaced. Reproduced by calling the real `vpn_manager.connect()` directly on
the device via the `drone-live-debugging` skill's on-device-script technique,
not by guessing from source alone.

**Fix**: `drone_install_root()` now walks the resolved path's parents for a
segment literally named `.releases` and returns *its* parent if found,
falling back to the original fixed-depth computation when there's no such
segment (a plain dev checkout). See `tests/test_install_paths.py` — in
particular, the test that uses the exact real resolved path string found on
the live device, which is the one that would have caught this before it
shipped.

A **second, independent** bug was found and fixed in the same session,
worth knowing about even though it isn't specific to this module: `apiPost`/
`api` in `drone.js` only ever read a `data.error` (singular string) field out
of a failed response body — `_handle_admin_vpn_connect`'s `{"status":
"error", "errors": [...]}` shape (plural array) fell through to a generic
`Request failed: 400`, discarding the actual reason. This is exactly the
symptom text the user reported. Fixed by having both helpers also fall back
to joining a `data.errors` array when `data.error` is absent — a good
reminder that a backend error shape and the frontend's generic error-message
extraction can silently drift apart, and the failure mode is a genuinely
unhelpful error message, not a crash, so it's easy to miss in review.

## `.ovpn` config rewrite rules (`rewrite_ovpn_config`)

Applied to every uploaded config, regardless of provider:

1. Any line starting with `auth-user-pass` (bare, or with an existing inline
   path argument from the provider) is replaced with
   `auth-user-pass <our auth.txt path>` — always pointing at the
   Drone-managed credentials file, never whatever the provider shipped.
2. `up`/`down` lines referencing `update-resolv-conf` are dropped entirely —
   Batocera has no such script; leaving it in place would fail the tunnel
   when openvpn can't exec a missing hook binary.
3. `auth-nocache` is appended if not already present (so a rejected
   credential is never silently retried from a stale cache) — not duplicated
   if the provider's config already has it.
4. Rejects the upload with `ValueError` if there is **no** `remote` directive
   at all — the strongest available signal that the file isn't a real OpenVPN
   client config, checked before ever touching openvpn itself.

This is a **line-based text scan**, deliberately not a full OpenVPN config
parser — embedded `<ca>`/`<cert>`/`<key>` blocks (common in single-file
all-in-one provider exports, including Proton VPN's) pass through untouched
because none of their content matches the patterns above.

"VPN Server" in the UI is parsed from the **stored config's own** `remote`
lines (`parsed_remotes()`), not scraped from the live openvpn log — available
immediately after upload, before ever connecting, and not dependent on
log-wording differences across openvpn versions.

## Credentials

`auth.txt` is plaintext on disk (`username\npassword\n`), `chmod 0o600` —
this is not a design choice to relax, it's a hard requirement of OpenVPN's
own `auth-user-pass <file>` mechanism, which expects exactly that format.
Only the **username** is persisted in the JSON state (for display); the
password is never stored anywhere except that one file, and **never returned
to the browser by any `/admin/*` endpoint** -- this rule is unchanged. The one
new exception is deliberate and lives entirely on a different channel: see
"Peer-to-peer sharing" below, which is a Drone-to-Drone mTLS payload, never a
browser response.

## Peer-to-peer sharing (`export_payload` / `import_from_peer`)

A paired peer can pull this drone's VPN config **and credentials** over the
same cert-pinned mTLS `/peer/*` channel used for ROM/BIOS/save/movie/artwork
transfers -- see the `drone-p2p-transfer-security` skill for that channel's
base guarantees (paired-only, mTLS required, no plaintext fallback). This
lets an owner who legitimately controls every drone in the swarm (and the one
VPN subscription behind it) avoid re-typing the same OpenVPN token on every
machine.

**This is opt-in per drone, on top of the base pairing check** — a deliberate
deviation from the ROM/BIOS/saves/movies default (there, pairing itself is
the only authorization check; see the P2P skill's "a peer is authorized
because it is in this Drone's own paired-peer list, nothing else"). VPN
credentials are more sensitive than a ROM file, so a second, explicit gate
exists: `vpn_manager.set_sharing_enabled()` / the `sharing_enabled` state
field, surfaced as an **off-by-default** "Allow paired drones to pull this
VPN configuration" switch on the VPN tile. Do not remove this gate or default
it to on "for consistency with ROMs" -- that consistency was deliberately
rejected for this feature.

- **Serving side**: `GET /peer/vpn/config` (`handlers_peer.py`'s
  `_handle_peer_vpn_config`, dispatched in `api_routes.py`) checks
  `_peer_request_authorized()` (the standard pinned-cert/paired check) *and*
  `vpn_manager.export_payload(settings)`, which itself returns `None` (→ 404)
  unless `sharing_enabled` is on *and* a config has been uploaded. When
  present, credentials are read straight from `auth.txt` and included in the
  JSON payload alongside the config text and remotes.
- **Pulling side**: the admin action `POST /admin/vpn/pull-from-peer`
  (`handlers_vpn.py`'s `_handle_admin_vpn_pull_from_peer`) resolves the
  chosen peer via `transfer/local_network.get_paired_peer`, fetches
  `/v1/api/peer/vpn/config` with `transfer/peer_connectivity._peer_get_json_for_peer`
  (a small one-shot cert-pinned JSON client -- **not** the big-file
  `DownloadManager` queue; a VPN config is a few KB of text, so
  progress/resume/cancel machinery would be pure overhead),
  and hands the result to `vpn_manager.import_from_peer(settings, payload)`.
- **`import_from_peer` deliberately does not write files itself** — it calls
  the *existing*, already-tested `save_uploaded_config()` /
  `save_credentials()` unchanged, treating the peer's exported bytes exactly
  like a fresh browser upload. This matters for correctness, not just code
  reuse: `save_uploaded_config()` re-runs `rewrite_ovpn_config()` against
  *this* drone's own `auth_path()`, so the peer's `auth-user-pass` line
  (which pointed at *their* install-root path) gets correctly re-pointed at
  ours — this holds even if the two drones' install roots differ, with no
  separate "peer import" rewrite path to keep in sync with the upload one.
- The frontend (`drone.js`: `renderVpnPage`'s "Share with Swarm" card,
  `setVpnSharing`, `loadVpnPullPeerOptions`, `pullVpnConfigFromPeer`) sources
  the peer picker from `GET /admin/swarm/overview` filtered to
  `!drone.is_self && drone.online`, the same pattern the Transfers page's
  "Connected Drone" dropdown already uses (`renderLocalTransferRequestPanel`).

### Single-hop only: an imported config can never be re-shared

**Only the drone whose owner originally uploaded/typed a config can ever
share it.** A drone that pulled a config from a peer is a *consumer*, not a
new source — it cannot re-share what it imported, at any distance. This is
tracked as **provenance**, not inferred from `sharing_enabled` alone:

- `_load_state()` carries `source_peer_id` / `source_peer_name` — empty for a
  self-uploaded config, set to the peer's drone id/name for an imported one.
- `save_uploaded_config()` — a **direct**, real upload — always resets
  provenance to empty as part of its "fresh write" semantics: this is the
  *only* way a drone goes from "imported" back to "self-owned, can share."
- `import_from_peer(settings, payload, *, source_peer_id, source_peer_name="")`
  takes the peer identity as **caller-supplied keyword arguments, never from
  the wire payload** (the pulling drone already authenticated that peer via
  mTLS+pairing before calling this — trust what *we* dialed, not a
  self-reported field). It calls `save_uploaded_config()` first (which clears
  provenance, see above), then immediately re-applies the real provenance in
  a follow-up `_save_state()` call. Get this ordering backwards and every
  import would silently look self-owned.
- **Enforcement is two-layered, both required:** `set_sharing_enabled(settings,
  True)` raises `ValueError` outright if `source_peer_id` is set (surfaces as
  a 400 to the admin UI, which also hides the toggle entirely in this state —
  see `renderVpnPage`'s conditional in `drone.js`); `export_payload()`
  *independently* re-checks `source_peer_id` and returns `None` even if
  `sharing_enabled` were somehow set anyway. Don't remove either check on the
  assumption the other one covers it — `export_payload`'s check is the one
  that actually matters, since it's the point data would leave the drone.

### Revocation: turning sharing off strips it from everyone who pulled it

When the source drone's owner turns `sharing_enabled` off (or removes their
config), every drone that had pulled it must lose it: disconnect, remove the
credentials, and show why. Drones are **outbound-only with no push channel**,
so the only way a pulling drone can learn this is to periodically ask —
this is the one deliberate exception to this module's "no background thread"
bias (see "Design" above), because unlike status, an on-demand recompute
cannot detect a revocation nobody is actively looking for.

- `run_sharing_revocation_poller(settings)` is a forever-loop (sleep, then
  `check_sharing_revocation`), started as its own daemon thread from
  `create_server()` **exactly like `maybe_auto_connect`** — a
  `_VPN_SHARING_POLLER_STARTED` guard flag, threading owned by the caller, the
  function itself threading-agnostic. Interval: `DRONE_VPN_SHARING_CHECK_INTERVAL_SECONDS`
  (default 300s, floored at 30s) — an ops/test-only env var, not a UI setting,
  matching `DRONE_VPN_DIR`'s precedent.
- `check_sharing_revocation(settings)` is a no-op unless `source_peer_id` is
  set *and* `has_credentials` is true (nothing to revoke otherwise). It
  resolves the source peer via `local_network.get_paired_peer` and re-calls
  the *same* `GET /peer/vpn/config` endpoint the original pull used.
- **Only two outcomes count as revocation**: the peer is no longer in this
  drone's paired-peer list at all, or it answers with `404` (sharing off, or
  it no longer has a config — `_handle_peer_vpn_config` returns 404 for both,
  and both mean "stop relying on this"). **Every other outcome — unreachable,
  timeout, any other HTTP status, an unexpected exception — changes nothing.**
  `check_sharing_revocation` never raises and never revokes on a guess; a
  flaky or briefly-offline peer must not strip a working VPN setup. Do not
  loosen this to "any error revokes" — that turns ordinary network flakiness
  into data loss.
- **What revocation actually does** (`_revoke_local_credentials`): calls the
  existing `disconnect()`, deletes `auth.txt`, clears `has_credentials`/
  `username`, and sets `revoked_reason` + `revoked_at`. It deliberately
  **leaves the `.ovpn` config file and `source_peer_id` in place** — wiping
  provenance here would reopen the single-hop-only hole (see above): a
  credential-less imported config with no recorded origin would look
  indistinguishable from a fresh self-upload and could pass the sharing
  gate. Provenance is only ever cleared by a genuine new
  `save_uploaded_config()` call.
- The revoked-reason notice must render in the **live-polled** region
  (`renderVpnLive`/`patchVpnLive`'s `vpnRevokedNotice` node, via
  `renderVpnRevokedNotice`), not the static once-per-page-load template —
  revocation can happen while the owner is already sitting on the VPN page,
  and the existing 3s poll is what surfaces it without a manual refresh.

## Status detection

`status()` builds the full snapshot on every call:

- `installed` / `binary_path`: `shutil.which("openvpn")`.
- `pid`: `/proc` scan (above); `None` means `disconnected` and clears any
  persisted `connected_at`.
- Log tail (`_tail_lines`, shared with the System Logs viewer's own tailing
  helper) is scanned for `_SUCCESS_MARKER` ("Initialization Sequence
  Completed") → `connected`, or any of `_FAILURE_MARKERS` (AUTH_FAILED, TLS
  errors, DNS/connection failures) → `error` with the matching log line as
  `message`. Neither marker yet → `connecting`.
- `connected_at` is set (and persisted) the **first time** a status call
  observes the completion marker while none was already stored — there is no
  background thread to set it proactively, so this is deliberately
  idempotent/lazy, self-correcting on the next 3s poll either way.
- `tunnel_ip`: only queried when `connected`, via `ip -4 addr show tun0`
  (regex `inet (\d+\.\d+\.\d+\.\d+)`) — matches the literal verification
  command from the original feature spec rather than an ioctl-based approach,
  since it's simpler and exactly what a human would run by hand to check.

## `tunnel_is_up()` — the fail-closed check the torrent kill switch depends on

`tunnel_is_up(settings, interface="tun0")` is a **separate, deliberately
log-independent** predicate from `status()` (added with the Torrents "Require
VPN" mode, commit `5007cdb`). It returns `True` only if: the openvpn binary
exists, `_find_running_openvpn_pid(config_path(settings))` finds the exact
managed process, **and** `_tunnel_ip(interface)` reports an IPv4 address.

It exists because `status()`'s log-tail classification is not safe to gate a
kill switch on — a flood of harmless decrypt/replay warnings can push the
`_SUCCESS_MARKER` line out of the tail window and flip `status()` to
`connecting`/`error` on a tunnel that is actually fine. `tunnel_is_up` checks
process + interface reality only, so it stays stable through log spam and
**fails closed** (any doubt → `False` → torrents blocked).

**Load-bearing consumers** — do not weaken this to trust log wording, and keep
it fail-closed:
- `transfer/torrent_manager.py`: `_tick()` (per-poll gate → stop aria2),
  `force_start()`, `resume()`, `snapshot()`'s `vpn_ready` field.
- `transfer/aria2_runtime.py`: indirectly — when `tunnel_is_up` is the gate,
  `_ensure_rpc` launches `Aria2Daemon` bound to `--interface=tun0`.

Full kill-switch behavior lives in the **`drone-torrents-management`** skill's
"Require VPN" section. A change to `tunnel_is_up`, `_find_running_openvpn_pid`,
or `_tunnel_ip` must be checked against that consumer, not just `status()`.

## Public-IP verification is on-demand only

`check_public_ip()` (`curl -4 -s https://ipinfo.io/ip`) is wired to a
**button** in the UI, never polled automatically — an outbound HTTPS request
on every 3s status tick would be wasteful and could hit rate limits. If a
"verify automatically" feature is ever requested, poll it far less frequently
than the status tick, not on the same cadence.

## Connecting on boot: unconditional, retried, no separate OS service

`maybe_auto_connect(settings)` runs in a background thread kicked off from
`create_server()` (so it can't delay the server accepting its first request —
`connect()` can block for several seconds). **Being configured is the only
condition** — `validate_ready()` (has_config + has_credentials + openvpn
installed) gates it, nothing else. There used to be a separate `auto_start`
opt-in toggle (state field, `set_auto_start()`, `POST /admin/vpn/auto-start`,
a UI switch); it was **removed 2026-07-28** after a user reported VPN staying
disconnected across Drone-app restarts even with the toggle already on — see
"Common failure patterns" below for the real bug that turned out to be. The
user's explicit call: connect-if-configured should not depend on a switch a
person has to remember to flip per drone. Do not reintroduce a gating toggle
for this without the user asking for one again.

**Retries `VPN_AUTO_CONNECT_MAX_ATTEMPTS` times** (default 4, env
`DRONE_VPN_AUTO_CONNECT_MAX_ATTEMPTS`) with a
`VPN_AUTO_CONNECT_RETRY_DELAY_SECONDS` delay between attempts (default 15s,
env `DRONE_VPN_AUTO_CONNECT_RETRY_DELAY_SECONDS`) before giving up, and logs
every outcome (which attempt succeeded, or every error hit before giving up)
— unlike the single-shot version this replaced, which called `connect()`
exactly once and **discarded its result even on failure**, so a boot-time
hiccup (network/`/dev/net/tun`/an external drive not mounted yet) produced a
disconnected VPN with zero log evidence anything was even attempted. A
`connecting`/`already_running` result stops the loop immediately; a raised
(not returned) exception from `connect()` also stops immediately without
retrying, since `connect()` itself is already defensive and a raise from it
is unexpected enough that blind retrying isn't obviously safe.

There is **no** new systemd/init.d unit created for this — the Drone app
itself is already the boot-time service (`DRONE_SERVER`), so connecting on
boot is satisfied by the already-boot-triggered app auto-connecting on its
own startup. Do not build a second boot-ordering-dependent OS service for
this; it would be strictly more complex and less reliable than reusing the
existing one.

### Swarm bootstrap: a fresh/unconfigured drone adopts a connected peer's shared VPN

Added 2026-07-28, same day as the toggle removal above, per an explicit user
ask: "if a drone is not connected to a VPN, and the swarm contains a drone
that has VPN connected + sharing VPN credentials, I want the drone to
automatically pull these credentials, apply them, and start the VPN on
startup." `bootstrap_vpn_from_swarm(settings)` implements this, called from
`maybe_auto_connect` **only when `validate_ready()` already failed** — i.e.
this drone has no usable config of its own:

```python
if validate_ready(settings):
    bootstrap_vpn_from_swarm(settings)
if validate_ready(settings):
    return
# ... existing connect-retry loop, unchanged, now possibly using a freshly-adopted config
```

Key design decisions, and why:

- **Trigger is "no usable local config," not "temporarily disconnected."**
  The user's literal words ("not connected to a VPN") could be read either
  way; this skill's interpretation is deliberate, not an oversight —
  `validate_ready()` failing (no config, or no credentials, e.g. right after
  a revocation) is the trigger. A drone that already has its own saved
  config/credentials is **never** touched by this, even if its own tunnel
  happens to be down for some unrelated reason right now (bad password, wrong
  server, provider outage) — auto-replacing a deliberately-configured setup
  with a borrowed one on every restart would be a much bigger, more
  surprising behavior change than what was asked for. If this reading turns
  out wrong, that's a product question for the user, not something to "fix"
  unilaterally back toward the broader reading.
- **Only adopts from a peer that is actively `connected` right now**, not
  merely `sharing_enabled` + configured. `export_payload()` (`vpn_manager.py`)
  now includes `"connected": status(settings).get("status") == "connected"`
  in its response — purely additive, the existing serve gate
  (`sharing_enabled` + `has_config`) and the existing manual "Pull
  Configuration" UI flow are unchanged and still work regardless of a peer's
  live connection state. `bootstrap_vpn_from_swarm` is the one caller that
  filters on it, because a live tunnel is the strongest available signal the
  shared credentials genuinely work, and it prevents every drone in a
  freshly-booted fleet from racing to adopt from an equally fresh, equally
  unconnected peer.
- **One pass over paired peers at boot, not a background search.** Matches
  "start the VPN on startup" literally. If no qualifying peer is found this
  boot, nothing retries the *search* until the next actual Drone-app restart
  (the existing `connect()` retry loop only retries the *connect* step, and
  only runs once a usable config already exists). Do not turn this into a
  perpetual poller without the user asking for that specifically — see
  `run_sharing_revocation_poller` for what a genuinely justified persistent
  background thread looks like in this module, and note this isn't one.
- **Reuses `import_from_peer` unchanged** — same as the manual pull flow, so
  the auth-user-pass re-rewrite and provenance tracking (`source_peer_id`)
  apply identically regardless of whether the import was human-triggered or
  automatic. This also means an auto-bootstrapped config **cannot itself be
  re-shared** (see the single-hop-only section above) and **will be
  auto-revoked** by `check_sharing_revocation`'s existing poller exactly like
  a manually-pulled one, with no special-casing needed for either.
- **Per-peer failures are silently skipped, not logged as errors** — offline,
  not sharing, sharing but not connected, or an import failure (e.g. a
  malformed payload) all just move on to the next paired peer.
  `paired_peers()`'s own order is used as-is; no LAN/latency-based ranking was
  added for this bootstrap case (it's best-effort and rare, not a real
  transfer). Only the terminal outcome is logged: which peer's config was
  adopted, or nothing at startup.

## Self-heal: detect a broken tunnel and reconnect automatically

Added 2026-07-28 after a real live incident: two Drones behind the same home
router, both authenticated to the identical ProtonVPN credentials *and* the
identical server node (a side effect of swarm VPN sharing propagating the
whole `.ovpn`, server included, not just credentials -- see "Swarm bootstrap"
above), produced a sustained flood of `AEAD Decrypt error: bad packet ID`
lines on one of them for hours. Nothing noticed or recovered; a human had to
find it by reading the raw openvpn log. User's explicit ask: default-on,
toggleable-off, general-purpose self-healing -- "for any reason," not just
this one incident's exact signature.

**Detection (`_self_heal_reason`, `_recent_log_flood`)** covers two cases,
deliberately scoped to what's actually observable rather than active network
probing (e.g. no ping-through-the-tunnel check -- that would be a materially
bigger feature than "notice openvpn's own signals"):

1. `status()` already says `"error"` (the existing `_FAILURE_MARKERS`:
   `AUTH_FAILED`, TLS errors, connection refused, etc.) -- self-heal is the
   first thing that actually *acts* on this; previously a human had to notice
   and click Connect again.
2. **A repeating-error flood in the *most recent* log lines**, checked
   regardless of what `status()` itself reports -- this is what actually
   catches the incident above, where `status()` reported `"connecting"` (not
   `"error"`) because the flood of error lines had pushed the
   `_SUCCESS_MARKER` out of `status()`'s own 400-line detection tail. Only the
   last `_UNHEALTHY_LOG_WINDOW_LINES` (40) are scanned, not the whole log,
   specifically so a burst that already happened and stopped ages out on its
   own -- a healthy, already-connected tunnel logs almost nothing, so a recent
   window dominated by this pattern is a strong "actively broken right now"
   signal without needing to parse log timestamps.

**`"disconnected"` never triggers self-heal, on purpose** -- it's either a
human who disconnected deliberately, or `check_sharing_revocation`'s own
intentional disconnect after wiping credentials it no longer has any right to
use. Self-healing either of those would be a real bug, not a feature. This is
why `check_and_self_heal` also independently gates on `has_config`/
`has_credentials` -- there is nothing to reconnect *with* once revoked, so it
can never fight the revocation flow.

**Rate-limited two independent ways**, because a background watchdog with no
brake is how you get a reconnect storm against your own VPN provider:
`VPN_SELF_HEAL_MIN_INTERVAL_SECONDS` (120s) between any two actual reconnect
actions, and `VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW` (5) inside a rolling
`VPN_SELF_HEAL_WINDOW_SECONDS` (30 min). The window cap is a **temporary**
backoff, not a permanent give-up -- attempts age out of the rolling window on
their own (`_prune_self_heal_attempts`), so a connection still broken an hour
later gets tried again rather than staying down forever with no path back
except a manual click or a full restart. `run_self_heal_poller` checks every
`VPN_SELF_HEAL_CHECK_INTERVAL_SECONDS` (60s) -- its own daemon thread, started
from `create_server()` exactly like the sharing-revocation poller, not folded
into that poller's slower 5-minute cadence (different concern, different
natural interval, matches this module's existing one-thread-per-concern
style).

**Honest limitation, worth restating to a user who reports a similar
incident**: self-heal is disconnect-then-reconnect to the *same* config. For
a genuinely transient failure (a network blip, a momentary provider hiccup)
that's exactly the right fix. For the incident that prompted this feature,
reconnecting alone does **not** actually resolve the root cause -- both
Drones will keep landing on the same colliding server node and the flood will
likely resume, bounded only by the rate limit above. Self-heal is a safety
net and a diagnostic signal (`self_heal_last_reason`/`self_heal_recent_count`
in `status()`, surfaced in the UI), not a substitute for fixing same-node
credential collisions by pointing Drones at different server nodes. Do not
oversell this feature as "fixes" that class of problem when explaining it.

## Common failure patterns

- Treating `"disconnected"` as something self-heal should fix — it must never
  reconnect a tunnel a human (or the revocation flow) intentionally took down.
- Scanning the *whole* VPN log for the decrypt/replay-error pattern instead of
  just the recent window — a burst hours ago that already resolved would
  permanently look "unhealthy" and never stop triggering reconnects.
- Treating the self-heal window cap as a permanent give-up (e.g. clearing
  `self_heal_attempts` only via a manual action) — it must keep aging out on
  its own so a still-broken connection is retried later without a human
  needing to intervene.
- Implying self-heal "fixes" a same-server-node credential collision (the
  incident that prompted this feature) — reconnecting to the same broken
  config will very likely re-trigger the same flood; the actual fix is
  different server nodes, which self-heal does not attempt.
- Calling `bootstrap_vpn_from_swarm` (or its equivalent) when this drone
  already has its own config/credentials — it must only ever run after
  `validate_ready()` has already failed. Running it unconditionally would
  mean a perfectly good local VPN setup gets silently clobbered by whatever
  paired peer happens to answer first on every restart.
- Filtering peers on `sharing_enabled` alone for the swarm-bootstrap case —
  `export_payload()`'s serve gate doesn't require the source to be
  *connected*, only *sharing*, since the existing manual pull flow doesn't
  need that. The bootstrap-specific `connected` check has to happen on the
  *caller* side (`bootstrap_vpn_from_swarm`), not by changing what
  `/peer/vpn/config` serves.
- **The real bug that prompted the connect-on-boot rewrite**: the original
  `maybe_auto_connect()` called `connect()` exactly once and threw away the
  result on both success *and* failure — a transient boot-time condition
  (network/tun-module/external-drive-not-mounted-yet) failed silently, with
  no log line, and VPN just stayed disconnected until a human noticed and
  manually reconnected. Live-debugging evidence (process/session inspection
  + `service_bootstrap.sh` review) ruled out anything actively killing
  openvpn on a Drone-app restart (it's `setsid()`-daemonized, in its own
  session, independent of the app process) — the bug was purely "one silent
  attempt, no retry, no visibility." Any future one-shot "try once at
  startup" pattern in this codebase should be viewed with the same
  suspicion: does a transient boot-time failure actually get retried and
  logged, or silently swallowed?
- Reintroducing a toggle that gates connecting on boot behind anything other
  than "is a config ready" — this was a deliberate, explicit product
  decision (a user asked for it after finding the old opt-in toggle wasn't
  enough), not an oversight to "fix" back.
- Writing a test that calls `vpn_manager.save_uploaded_config`/
  `save_credentials`/etc. without patching `_drone_install_root` first — see
  "The test-isolation gotcha" above; this silently pollutes the real repo.
- Assuming `common/install_paths.drone_install_root()` always lands at the
  stable install root — under a release-versioned deploy layout it doesn't
  unless it specifically walks past a `.releases` path segment; see "Real
  live incident" above. This is the single most impactful bug found in this
  module so far (it broke VPN connect outright, unconditionally, in
  production) and is easy to reintroduce if that function is ever
  "simplified" back to a fixed-depth `parents[N]` computation.
- Using `killall openvpn` or matching on process name alone instead of the
  exact `--config <path>` argument — could affect an unrelated openvpn
  process on the same box.
- Adding a background polling thread for VPN status "for consistency with
  Torrents" — not needed; a VPN connection has no scheduling work, only
  Torrents' queue does. (The sharing-revocation poller is a *different*,
  narrowly-scoped background thread with its own concrete justification —
  see "Revocation" above — not a precedent for adding others.)
- Storing the plaintext password anywhere other than `auth.txt` (including
  logs, the JSON state, or an API response).
- Making `vpn_dir()`/the config path user-configurable in the UI — the
  feature spec is explicit that this is fixed, unlike Torrents' watch folder.
- Polling `check_public_ip()` automatically instead of leaving it a
  user-triggered button.
- Defaulting `sharing_enabled` to on, or removing the check, "for consistency
  with how ROMs/BIOS/saves already sync" — that consistency was deliberately
  rejected for VPN credentials; see "Peer-to-peer sharing" above.
- Writing peer-imported config/credential bytes to disk directly instead of
  routing them through `save_uploaded_config()`/`save_credentials()` — skips
  the local `auth-user-pass` rewrite and the `.ovpn`/size/encoding validation,
  and risks silently reusing the *peer's* install-root path.
- Adding the big-file `DownloadManager`/`TransportSelector` machinery to the
  pull path — a VPN config is a few KB of text fetched with one synchronous
  `_peer_get_json_for_peer` call, not a queued/resumable transfer.
- Trusting a `source_peer_id`/self-reported identity field from the *wire
  payload* when importing — it's a caller-supplied argument (the peer id the
  pulling drone itself dialed and authenticated), never something the peer's
  response gets to claim about itself.
- Clearing `source_peer_id` anywhere in the revocation path
  (`_revoke_local_credentials`) — that would let a now-credential-less
  imported config pass the "is this self-owned" check and become shareable.
  Only a genuine new `save_uploaded_config()` call may clear provenance.
- Treating any non-200 response (timeouts, 5xx, unrelated errors) from the
  revocation check as "revoked" — only an explicit 404 (not sharing / no
  config) or the peer no longer being paired counts; anything else is
  transient and must leave the local credentials untouched.
- Rendering the revoked-reason notice only in `renderVpnPage`'s static
  one-time template instead of the live-polled `vpnRevokedNotice` node —
  revocation can happen while the page is already open.

## Expected output format

When completing VPN work, respond using this format:

```text
Objective:
...
vpn_manager.py changes:
...
Backend route + handler changes (api_routes.py + handlers_vpn.py):
...
Frontend changes (drone.js):
...
Config rewrite rule changes (if applicable):
...
Status-detection changes (if applicable):
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

- relax `auth.txt`'s `0o600` permissions or store the password anywhere else,
- use `killall`/process-name-only matching for disconnect,
- return the VPN password in any **`/admin/*`** (browser-facing) API response
  — the peer-to-peer `/peer/vpn/config` payload is the one intentional,
  narrowly-scoped exception, gated by pairing + `sharing_enabled`,
- make the install-root-relative storage location user-configurable,
- add a background thread/cached state machine without a concrete reason a
  stateless recompute can't handle,
- poll the public-IP check automatically,
- extend the non-root "control worker" IPC to VPN without first confirming a
  real non-root deployment need (today's assumption is root-always, matching
  the rest of the device-control code),
- default `sharing_enabled` to on, or let any endpoint other than
  `/peer/vpn/config` (mTLS, paired, sharing-gated) return credentials,
- let an imported config be re-shared — enforce single-hop-only in both
  `set_sharing_enabled` (reject) and `export_payload` (independently refuse),
- clear `source_peer_id` anywhere except a genuine fresh `save_uploaded_config()`
  call, especially not during revocation cleanup,
- treat a network error, timeout, or non-404 status as revocation — only an
  explicit 404 or "peer no longer paired" may disconnect and wipe credentials,
- call `bootstrap_vpn_from_swarm` when this drone already has a usable local
  config — it must stay gated behind `validate_ready()` already having failed,
- adopt a shared config from a peer that is merely `sharing_enabled` but not
  currently `connected` — the bootstrap path specifically requires a proven
  working tunnel on the source, unlike the manual pull flow,
- let `check_and_self_heal` act on a `"disconnected"` status, ever,
- remove or weaken either self-heal rate limit (the per-reconnect cooldown or
  the rolling-window cap) — both exist specifically to stop a background loop
  from hammering the user's VPN provider,
- default `self_heal_enabled` to off — this feature was explicitly requested
  as default-on, opt-out,
- make `tunnel_is_up()` depend on log-tail wording, or let it return `True`
  on any doubt — it must stay fail-closed (the Torrents "Require VPN" kill
  switch gates on it; see the `tunnel_is_up()` section and
  `drone-torrents-management`).

## Default bias

When unsure, keep VPN management stateless (recompute on request) rather than
introducing cached state, prefer precise `/proc`-based PID matching over
broad process signals, and treat the fixed `<install root>/vpn/` location and
its `DRONE_VPN_DIR` test-only override as settled unless the user explicitly
asks to make storage configurable. For peer sharing, keep the extra
`sharing_enabled` opt-in gate on top of pairing (don't quietly relax it to
match ROM/BIOS's pairing-only trust), and keep `import_from_peer` a thin
wrapper over the existing `save_uploaded_config`/`save_credentials` rather
than a parallel write path. For provenance/revocation specifically: treat
"only the original creator can share" as a hard invariant enforced at two
independent points (`set_sharing_enabled` + `export_payload`), never at just
one; when a revocation check's outcome is ambiguous (an error type not
explicitly seen before), default to **not** revoking — a false negative
(stale access lingers a bit longer) is recoverable next poll, a false
positive (a legitimate user's VPN breaks from a network blip) is not.
