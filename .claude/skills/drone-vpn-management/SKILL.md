---
name: drone-vpn-management
description: Use this when designing, reviewing, debugging, or modifying the Drone's VPN admin tile — OpenVPN client configuration/connection management, .ovpn upload and rewriting, VPN credential storage, connect/disconnect process control, VPN status detection, auto-start-on-boot, or app/device/vpn_manager.py, web/handlers_vpn.py.
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
                     # real logic to vpn_manager.py
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
password is never stored anywhere except that one file, and never returned to
the browser by any endpoint.

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

## Auto-start on boot: no separate OS service

`maybe_auto_connect(settings)` runs once, in a background thread kicked off
from `create_server()` (so it can't delay the server accepting its first
request — `connect()` can block for several seconds). It simply checks the
persisted `auto_start` flag and `validate_ready()`, then calls `connect()` if
both pass. There is **no** new systemd/init.d unit created for this — the
Drone app itself is already the boot-time service (`DRONE_SERVER`), so
"start on boot" is satisfied by the already-boot-triggered app auto-connecting
on its own startup. Do not build a second boot-ordering-dependent OS service
for this; it would be strictly more complex and less reliable than reusing
the existing one.

## Common failure patterns

- Writing a test that calls `vpn_manager.save_uploaded_config`/
  `save_credentials`/etc. without patching `_drone_install_root` first — see
  "The test-isolation gotcha" above; this silently pollutes the real repo.
- Using `killall openvpn` or matching on process name alone instead of the
  exact `--config <path>` argument — could affect an unrelated openvpn
  process on the same box.
- Adding a background polling thread for VPN status "for consistency with
  Torrents" — not needed; a VPN connection has no scheduling work, only
  Torrents' queue does.
- Storing the plaintext password anywhere other than `auth.txt` (including
  logs, the JSON state, or an API response).
- Making `vpn_dir()`/the config path user-configurable in the UI — the
  feature spec is explicit that this is fixed, unlike Torrents' watch folder.
- Polling `check_public_ip()` automatically instead of leaving it a
  user-triggered button.

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
- return the VPN password in any API response,
- make the install-root-relative storage location user-configurable,
- add a background thread/cached state machine without a concrete reason a
  stateless recompute can't handle,
- poll the public-IP check automatically,
- extend the non-root "control worker" IPC to VPN without first confirming a
  real non-root deployment need (today's assumption is root-always, matching
  the rest of the device-control code).

## Default bias

When unsure, keep VPN management stateless (recompute on request) rather than
introducing cached state, prefer precise `/proc`-based PID matching over
broad process signals, and treat the fixed `<install root>/vpn/` location and
its `DRONE_VPN_DIR` test-only override as settled unless the user explicitly
asks to make storage configurable.
