---
name: drone-vpn-management
description: Use this when designing, reviewing, debugging, or modifying the Drone's VPN admin tile — OpenVPN client configuration/connection management, .ovpn upload and rewriting, VPN credential storage, connect/disconnect process control, VPN status detection, connecting automatically on Drone startup (unconditional + retried, no opt-in toggle), peer-to-peer VPN config/credential sharing between paired drones (single-hop-only provenance, share-revocation auto-disconnect/wipe), or app/device/vpn_manager.py, web/handlers_vpn.py, web/handlers_peer.py's _handle_peer_vpn_config.
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
  (the same small one-shot cert-pinned JSON client the remote-admin proxy
  uses -- **not** the big-file `DownloadManager` queue; a VPN config is a few
  KB of text, so progress/resume/cancel machinery would be pure overhead),
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

## Common failure patterns

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
  explicit 404 or "peer no longer paired" may disconnect and wipe credentials.

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
