---
name: drone-torrents-management
description: Use this when designing, reviewing, debugging, or modifying the Drone's Torrents admin tile — watched-folder .torrent downloads, magnet-link submission, the local aria2c daemon/RPC lifecycle, the watched folder vs. download-location distinction, aria2c install, .torrent upload, force-start/cancel/delete, moving downloaded files out of a completed torrent, global pause/resume/bulk-clear, or app/transfer/torrent_manager.py, aria2_runtime.py, web/handlers_torrents.py.
---

# Drone Torrents Management Skill

## Goal

Keep this skill accurate as the single source of truth for the Torrents admin
tile's implementation details — the parent `drone-admin-features` skill only
carries a brief tile-level summary and points here for depth.

## Architecture

```text
app/transfer/
  torrent_manager.py   # TorrentManager: watched-folder scan, own start/stop
                        # scheduler, settings, snapshot -- the whole feature's
                        # state machine
  aria2_runtime.py      # find/install the aria2c binary, Aria2Daemon (spawn +
                        # health-check the RPC daemon; tun0 socket bind for the
                        # "Require VPN" kill switch; process-tree teardown),
                        # Aria2Rpc (JSON-RPC client)
app/device/
  vpn_manager.py        # tunnel_is_up() -- the fail-closed "is the managed
                        # OpenVPN tunnel live" check the kill switch gates on
app/web/
  handlers_torrents.py  # admin route handlers (HandlersTorrentsMixin);
                         # delegates all real logic to torrent_manager.py
app/common/
  multipart.py           # shared multipart/form-data parser (also used by
                          # handlers_vpn.py's .ovpn upload)
  install_paths.py        # drone_install_root() -- the default watched folder
                          # is <install root>/torrents, not under userdata_root
```

Unlike most Drone features, `TorrentManager` **does** run its own background
worker thread (`_worker`/`_tick`, polling every `DRONE_TORRENT_POLL_SECONDS`,
default 3s) — a real difference from `device/vpn_manager.py`'s stateless,
compute-on-request design, because a torrent queue has actual scheduling work
to do (which torrent gets a slot next), not just a single process to check.

## Magnet links: a second entry point into the same scheduler

`add_magnet(magnet_uri)` registers a magnet link exactly like
`_scan_watch_directory_locked()` registers a scanned `.torrent` file — same
entry shape, same `_torrents` dict, same scheduler — except there is no
backing file, so the entry's `torrent_file` is `""` and a new `magnet_uri`
field (added to `_ENTRY_PERSISTED_FIELDS`) carries the URI instead. `_tick()`'s
Phase B branches on `entry.get("magnet_uri")` to call `_add_magnet_via_rpc`
(`rpc.call("aria2.addUri", [[magnet_uri], options])`) instead of
`_add_torrent_via_rpc` — **never** call the latter on a magnet-only entry:
`Path(entry["torrent_file"]).read_bytes()` raises `TypeError` on an empty
string, which is not caught by that function's `except OSError`, and would
abort the whole tick's add/query loop for every other torrent too, not just
the magnet one. `Aria2Rpc.call()` (`aria2_runtime.py`) is a generic JSON-RPC
passthrough with no per-method allowlist, so `addUri` needed no client-side
change, only the new caller. The display name is a best-effort parse of the
magnet URI's own `dn=` parameter (`_magnet_display_name`), overwritten by the
real torrent name once aria2 resolves BitTorrent metadata (the existing
`bittorrent.info.name` handling in `_apply_aria2_status_locked` already
applies regardless of how an entry was added — no magnet-specific code
needed there).

**The gotcha that bit here**: `_restore_state()`'s restart-recovery gate used
to require `torrent_file` unconditionally
(`if not entry_id or not torrent_file or ...: continue`) — for a magnet-only
entry that's always empty, so the gate **silently dropped the entry
entirely** on every Drone restart (not merely losing its GID, the way a
`.torrent`-backed entry's stale GID is recovered). Fixed by accepting either
`torrent_file` **or** `magnet_uri` being truthy; the rest of the
restart-recovery logic (downgrade `queued`/`downloading` back to `queued`
with `gid: None` so the next tick re-adds it) is already field-driven and
needed no other change. `delete()`/`clear()`'s `Path(entry["torrent_file"]).
unlink(...)` calls are guarded the same way (skip when `torrent_file` is
empty) for the identical reason.

## Core design: the manager's own scheduler, not aria2's queue

Every torrent is added to aria2 **paused** (`aria2.addTorrent` with
`pause: "true"`), then `TorrentManager` itself calls `aria2.unpause` on
whichever GIDs it picks, up to `max_concurrent_downloads`. This indirection is
what makes **Force Start** genuinely bypass the concurrency limit — aria2 has
no per-download priority/override concept, so the only way to guarantee one
specific torrent starts regardless of the current slot count is for something
outside aria2 to decide who gets unpaused. aria2's own
`--max-concurrent-downloads` is set high (64) specifically to stay out of the
way of this external scheduling.

UI statuses are exactly `queued` / `downloading` / `complete` / `error`. A
torrent that finished downloading but is still seeding reports `complete`
with `seeding: true` — there is no separate "seeding" status.

### Automatic retry goes to the back of the queue

Real aria2 failures and failures while adding a `.torrent` remain visibly
`error` during a short exponential backoff (15 seconds initially, capped at 5
minutes). When the delay expires, the entry changes back to `queued` with a
new persisted `queue_position`, behind every torrent that is already waiting.
The scheduler uses this position rather than rewriting `added_at`, so the UI
keeps showing the torrent's true original add time while retries cannot starve
new work. `DRONE_TORRENT_RETRY_BASE_SECONDS` and
`DRONE_TORRENT_RETRY_MAX_SECONDS` override the defaults.

**Cancel is a requeue, not a terminal error** -- clicking it ("Send to queue"
in the UI) on a downloading or errored torrent stops it (`_remove_from_aria2`)
and sends it to the back of the queue with a fresh `queue_position`, so it
resumes on its own on the next tick with no Force Start needed (partial
progress is kept: the `.aria2` resume file on disk isn't touched). This lets
a slow torrent be bumped out of an active slot to free it for something else,
or an errored one be retried without jumping the queue the way Force Start
does. A `queued` torrent has nothing to do here, so the UI doesn't offer the
button for that status (though the backend will still no-op-requeue it if
called directly). Canceling a completed-but-still-seeding torrent is the one
exception -- that just stops seeding (`"Seeding stopped"`) since there's
nothing to requeue. `cancel()`'s return `status` is `"requeued"` or
`"seeding_stopped"` accordingly (an older `"cancelled"` value is gone). A
failed aria2 GID is removed before its retry is re-added so stopped/error
results do not collide with the fresh attempt.
Entries persisted from **before** this change (status `error`, message
`Canceled` -- the old terminal-cancel behavior) are exempted from the
first-tick-after-upgrade retry-eligibility sweep for pre-retry-metadata error
entries, so an old canceled torrent doesn't spontaneously resume the moment a
Drone with this fix starts up; a fresh Cancel today never produces such an
entry.

## Watched folder vs. download location (two independent settings)

`directory` — the folder scanned for new `.torrent` files (`_scan_watch_directory_locked`).
`download_directory` — where aria2 actually writes downloaded file payloads.

`download_directory` defaults to **empty**, meaning "same as `directory`"
(today's original behavior, and still what most users want) — resolved
**lazily** via `effective_download_directory(config)`, never baked into a
torrent at settings-save time. This matters: if a user never sets
`download_directory` and later changes `directory` (the watch folder), newly
scanned torrents keep following wherever `directory` currently points, not a
stale copy from before. Only an *explicit* `download_directory` value decouples
a torrent's payload location from the watch folder.

This is the intended way to send downloads to a different disk than wherever
the Drone app itself is installed — e.g. an external USB drive under `/media`,
or straight into `/userdata/roms/<system>` — without needing the `.torrent`
file itself to live there too. Both fields reuse the exact same
`GET /admin/torrents/browse` storage-root-scoped picker; the frontend's
`openTorrentDirBrowser(targetInputId, title)` takes which `<input>` to fill in
as a parameter (module-level `torrentDirBrowserTargetInputId` tracks it for
the modal's own "Use this folder" button) rather than duplicating the whole
browser modal per field.

`update_settings()` creates both directories on disk
(`mkdir(parents=True, exist_ok=True)`) if they don't already exist, but only
bothers with `download_directory` when it actually differs from `directory`
(avoids a redundant mkdir call).

The snapshot (`GET /admin/torrents`) exposes `effective_download_directory`
and `download_directory_exists` (in addition to the pre-existing `directory`/
`directory_exists`) precisely so the UI can show the *resolved* default in a
placeholder without recomputing the fallback logic client-side, and warn about
a not-yet-created download folder independently of the watch folder.

### `download_dir` keeps following the setting until a torrent actually starts

The `.torrent` file itself never moves once scanned (`torrent_file` is fixed
at scan time, permanently). `download_dir` used to be equally fixed at scan
time, but that surprised users: changing the download location and saving had
no effect on torrents that were already registered, even ones still sitting
in the queue with zero bytes downloaded. It now instead keeps tracking
whatever `effective_download_directory(config)` currently resolves to, for as
long as the torrent has never actually received data
(`_refresh_pending_download_dirs_locked`, called every tick and from
`force_start()`): status `queued` or `error` **and** `completed_bytes == 0`.
The instant a torrent has any real progress (including a torrent that's
`queued` only because it's globally paused mid-download,
[[drone-torrent-vpn-ui-polish-and-bootstrap-collision]]'s pause feature), it
is frozen at wherever it already is and never retargeted again.

**The gotcha that actually bit here**: the obvious-looking fix —
`aria2.changeOption(gid, {"dir": new_dir})` on an already-added (paused,
0-byte) BitTorrent download — silently "succeeds" (no RPC error) but **does
not actually relocate anything**. Confirmed against a real aria2c: after
`changeOption` + `unpause`, the payload still landed in the *original*
directory. FakeRpc-based unit tests can't catch this (the fake just no-ops
unrecognized methods) — this was only caught by live-driving a real drone
server + aria2c through the HTTP API and checking the filesystem. The actual
fix: when a `queued` entry with a GID needs retargeting,
`_refresh_pending_download_dirs_locked` clears its `gid` (after noting the
old one for `_remove_from_aria2`), so the very same tick's normal `to_add`
pass re-adds it from scratch with the fresh `download_dir` — reusing the
exact recovery path that already exists for a stale post-restart GID, rather
than inventing new machinery. `force_start()` does the equivalent inline
(drop the stale GID, let the immediate re-add carry `force_started` through
to an unpaused fresh add) since it doesn't go through `_tick()` at all.

## aria2c install

`aria2_runtime.find_aria2c()` prefers a system PATH install over the
Drone-managed copy (so a future Batocera image that ships aria2c natively is
picked up automatically with no code change). If missing, `install_aria2()`
downloads a pinned static-musl build from `abcfy2/aria2-static-build`
(version pinned via `ARIA2_DOWNLOAD_VERSION`) for the current
`platform.machine()`, verified by actually running `--version` on the
downloaded binary before accepting it. `ARIA2_STATIC_ASSETS` maps the common
Batocera architectures (x86_64, aarch64, armv7, armv6/arm, i686) to their
release asset names — extend this dict, not the download logic, when a new
arch needs support.

**`--rpc-save-upload-metadata=false` in `Aria2Daemon.start()` is
load-bearing, not cosmetic.** Without it, aria2 mirrors every RPC-uploaded
`.torrent` into its own `--dir` as `<infohash>.torrent`. Since the watch
folder and the download dir can be (and by default are) the *same* directory,
that mirrored copy gets picked up by the next scan as a brand-new torrent,
which aria2 then rejects as "InfoHash already registered" — a duplicate,
permanently-errored entry for every real torrent. This was only caught by a
live smoke test against a real aria2c binary; the mocked-RPC unit tests can't
surface it because the fake RPC never actually writes a mirrored file.

## "Require VPN" — the fail-closed torrent kill switch (`vpn_required`)

Added 2026-08-31 (commit `5007cdb`, hardened by `467d7cc` + `0e47794`). The
Torrents settings tile has a **"Require VPN"** toggle — persisted config key
`vpn_required` (`_normalize_torrent_settings`, `_bool_value("vpn_required")`,
default off). When on, **no torrent byte moves unless this Drone's own managed
OpenVPN tunnel is up**, and all aria2 traffic is bound to `tun0`. It is
enforced at two independent layers:

**Layer 1 — aria2 socket binding (`aria2_runtime.py`, `Aria2Daemon.start()`).**
The daemon is constructed with `bind_interface="tun0"` (via
`_ensure_rpc(..., vpn_required=True)`, whose `desired_interface = "tun0" if
vpn_required else None`) and launches with three extra args:

- `--interface=tun0` — every peer/tracker/download socket binds to the tunnel
  interface, so loss of `tun0` cannot let an established or new connection fall
  back to `wlan0`/`eth0`. **This is the actual kill switch.**
- `--disable-ipv6=true` — Batocera's OpenVPN is IPv4-only; this closes the
  independent IPv6 escape path.
- `--bt-enable-lpd=false` — no BitTorrent Local Peer Discovery broadcast onto
  the physical LAN while VPN-only mode is selected.

`_ensure_rpc` compares `getattr(daemon, "bind_interface", None)` against the
desired value and **tears down + relaunches** the daemon if they differ, so a
daemon started without `--interface` can never keep serving after the mode is
switched on (and vice versa). `update_settings()` also calls
`_reset_daemon_state_locked(...)` + `daemon.stop()` inline the moment
`vpn_required` changes, not waiting for the next tick.

**Layer 2 — the scheduler gate (`torrent_manager.py`, `_tick()`).** Every poll
(default 3s) `_tick` first reads `vpn_required` and computes
`vpn_blocked = vpn_required and not _vpn.tunnel_is_up(self.settings)`. If
blocked it calls `_stop_daemon_for_network_policy("Waiting for VPN
connection")` (full `daemon.stop()` + convert every process-local GID back to a
resumable `queued` entry) and **returns before Phase B** — the watched-folder
scan and persistence still run (new `.torrent` files show up as `queued` with
message `"Waiting for VPN connection"`), but `_ensure_rpc` and every aria2 RPC
are unreachable. `_ensure_rpc` has exactly one caller, `_tick` Phase B, so
there is no other path that can start the daemon while blocked.
`save_uploaded_torrents()` / `add_magnet()` only register an entry + `wake()`;
the actual aria2 add always flows through the gated `_tick`.

`force_start()` and `resume()` **also re-check** `_vpn.tunnel_is_up()` directly:
`force_start` returns `{"status": "vpn_required"}` (HTTP 409 in
`handlers_torrents.py`) — **"force" overrides the concurrency limit and the
global pause, but NOT the VPN gate**. `resume()` stops the daemon and returns
the snapshot unchanged if the tunnel is down.

`_vpn.tunnel_is_up(settings)` (`device/vpn_manager.py`) is deliberately
**fail-closed and log-independent**: it returns `True` only if the openvpn
binary exists, the exact managed `--config <path>` process PID is running, AND
`ip -4 addr show tun0` reports an address. Harmless replay-warning log spam
pushing the success marker out of the tail window does not flip it.

The snapshot (`GET /admin/torrents`) exposes `vpn_required` (bool) and
`vpn_ready` (`tunnel_is_up` result, or `True` when not required) so the UI can
render the "VPN-required mode is active…" warning banner
(`renderTorrentAlerts`) and the toggle state without recomputing.

**Daemon teardown must kill the whole process tree, not just the wrapper.**
aria2 is normally never killed except at Drone shutdown, but the kill switch
kills it on every tunnel loss — and an AppImage aria2c forks an AppRun wrapper
+ real aria2 child into a **separate session** that escapes the launcher's
process group. So `Aria2Daemon`: launches with `start_new_session=True`;
`stop()` does `aria2.forceShutdown` RPC, then `os.killpg(SIGTERM/SIGKILL)`,
**then** `_terminate_aria2_port_processes(port)` which scans `/proc/*/cmdline`
for any process carrying this daemon's unique `--rpc-listen-port=<port>` marker
+ `aria2c` and SIGTERM/SIGKILLs it. That last sweep is load-bearing: it runs
*even when the wrapper is already gone*, which is exactly when a detached child
used to keep transferring unbound after VPN-only mode engaged. Tests:
`Aria2VpnBindingTests`, `test_stop_terminates_the_entire_appimage_process_group`
in `tests/test_torrents.py`.

**Residual gaps (known, documented — do not "fix" without deciding):**

1. **~1 poll cycle (≤3s) startup race.** If `tun0` drops between `_tick`'s
   `tunnel_is_up` check passing and `Aria2Daemon.start()` spawning the process,
   aria2 launches with `--interface=tun0` pointing at a now-missing interface;
   aria2 logs a warning and proceeds *unbound* until the next tick kills it.
   The interface bind is the only mitigation and it does not cover
   "interface already gone at spawn". Self-corrects within
   `TORRENT_POLL_SECONDS`.
2. **DHT / UDP on a split-tunnel `.ovpn`.** DHT is left enabled (no
   `--enable-dht=false`). `--interface` binds aria2's sockets broadly, but a
   split-tunnel profile (no `redirect-gateway`) has not been verified to route
   DHT UDP through `tun0`. Moot for a normal full-tunnel profile (the common
   case, and what swarm VPN sharing propagates).
3. It is an **aria2-level** kill switch (trusts aria2 to honor `--interface`),
   **not** a kernel `iptables`/`nftables` killswitch. Reasonable for a
   stdlib-only Drone; weaker than a kernel-enforced rule.

Tests: `TorrentVpnRequirementTests` (daemon stop + torrents stay queued, daemon
binds to `tun0` when up, `force_start` cannot bypass) and `Aria2VpnBindingTests`
in `tests/test_torrents.py`. See also `drone-vpn-management`'s note that
`tunnel_is_up` is a load-bearing consumer.

## Magnet metadata hand-off (`followedBy`/`following`)

A magnet-added GID initially only fetches the BitTorrent **metadata** (the
reconstructed `.torrent` info dict -- a few KB/MB) via `ut_metadata`/DHT, then
reports itself `"complete"` at that tiny size -- aria2 automatically starts
the **real** content download under a brand-new GID, linked back via
`followedBy` (on the metadata GID) / `following` (on the new one). Without
following this hand-off, `_apply_aria2_status_locked` would take that
`"complete"`-at-a-few-MB status at face value and never look further: the UI
shows the torrent finished at a tiny size while the real, much larger
download -- often tens or hundreds of GB for a multi-file magnet -- runs to
completion completely untracked (no queue-slot accounting, no progress, no
move-files support). This is exactly a user-reported "pasting in a magnet
link downloads the wrong/tiny file" bug, confirmed live against a real
aria2c and a real multi-GB magnet link (a metadata GID completing at ~1.1MB
with `followedBy` pointing at a second GID already at 477GB total/34MB
downloaded with real peers attached). Fix: `_TELL_STATUS_KEYS` requests
`followedBy`, and `_apply_aria2_status_locked` checks it **before** writing
any of that response's total/completed/status onto the entry -- if present,
it retargets `entry["gid"]` at `followedBy[0]` and returns immediately
without touching those fields, so the next tick's query (now against the
real GID) populates the real numbers instead. Mocked-RPC unit tests catch
this one fine (just set `followedBy` in a `FakeRpc` status dict), but the
live repro is what actually found it -- this field is easy to forget
requesting/checking since it's absent from most tellStatus examples in
aria2's own docs.

## "InfoHash already registered" recovery: two independent triggers

aria2 rejects an `addTorrent`/`addUri` call for an infohash it already has
active or paused with errorCode=12, `"InfoHash <hash> is already
registered."` (`_ALREADY_REGISTERED_INFOHASH_RE`). This is recoverable, not a
real failure -- our own prior add for the same torrent can still be sitting
there even though *we* think it failed -- but it surfaces two structurally
different ways, and both had to be handled separately because each is
invisible to the other's recovery path:

1. **Synchronous** (add-time): the `aria2.addTorrent`/`addUri` RPC call
   itself raises `Aria2RpcError` with this message -- e.g. a client-side
   timeout on a slow-to-parse large torrent races a still-successful
   server-side add (`ARIA2_ADD_TIMEOUT_SECONDS` is deliberately longer than
   status-poll timeouts for this reason). Caught in `_add_torrent_via_rpc`/
   `_add_magnet_via_rpc`'s `except Aria2RpcError` block, which calls
   `_recover_from_already_registered` right there.
2. **Asynchronous** (query-time, live bug fixed 2026-08-05): aria2 can
   instead *accept* the duplicate add with a brand-new GID, which only then
   fails on its own -- reported solely on a later `aria2.tellStatus` poll
   (`status: "error"`, matching `errorMessage`), never as an exception from
   the add call. Confirmed live and reproduced deterministically against a
   real aria2c binary (a duplicate `addUri` for an in-flight magnet's
   info-hash is accepted with a new GID that errors out on its own the very
   next tick). This is the actual shape of a user-reported "torrents go into
   error state when seemingly downloading just fine right before" -- the
   real download, under its original healthy GID elsewhere in aria2, was fine
   the whole time; only the doomed duplicate ever errored. Caught in
   `_query_torrent_via_rpc` (checks `result.get("errorMessage")` on every
   status response, not just ones that raised) and applied in
   `_apply_aria2_status_locked`'s `"recovered_gid" in outcome` branch, which
   retargets `entry["gid"]` and hands back the doomed GID for the same Phase
   D `_remove_from_aria2` cleanup that orphaned adds already use.

Both funnel into the same `_recover_from_already_registered` /
`_find_existing_gid_for_infohash` lookup (adopt whichever registered copy for
that infohash has the most `completedLength`, across `tellActive` +
`tellWaiting`) -- **do not duplicate this lookup** if a third trigger turns up
later; add another caller into it instead.

**Side effect worth knowing:** a same-tick recovery can hand
`_pick_startable_gids_locked` a `queued` entry whose just-adopted gid turns
out to already be active (not paused) in aria2 -- `aria2.unpause` on it then
returns a harmless `"cannot be unpaused now"` error. Phase D's unpause loop
matches this specific message (`_CANNOT_BE_UNPAUSED_RE`) and swallows it
silently rather than logging it as a real unpause failure; an unrelated
unpause error still logs normally.

Tests: `AlreadyRegisteredRecoveryTests` (sync trigger) and
`AsyncAlreadyRegisteredRecoveryTests` (async trigger + the unpause-suppression
side effect) in `tests/test_torrents.py`.

## Restart / GID lifecycle

aria2 GIDs do not survive a daemon restart (a fresh `Aria2Daemon` is a fresh
aria2 process with no memory of prior GIDs). `_restore_state()` handles this
on Drone startup: any entry that was `queued`/`downloading` drops back to
`queued` with `gid: None`, so the next `_tick()` re-adds it (still paused) --
the `.aria2` control file on disk lets aria2 resume from wherever the last
run left off rather than starting over. A `complete`/`error` entry is left
alone. The same "vanished GID -> requeue" recovery also fires mid-session if
aria2 itself restarts unexpectedly (`_apply_aria2_status_locked`'s `"error"`
branch matches a `GID ... is not found` RPC error, not just process-startup).

`cancel()` deliberately does **not** use `killall`/broad process signals --
disconnect-equivalent operations here are pure RPC (`aria2.forceRemove` +
`aria2.removeDownloadResult` on the specific GID), since aria2 itself is a
single shared daemon process the whole manager depends on. It is killed
(`Aria2Daemon.stop()` — process-group + `/proc` port-marker sweep, see the
"Require VPN" section) at Drone shutdown (`--stop-with-process=<drone-pid>`
ties its lifetime to the Drone process) **and** on every VPN-tunnel loss while
`vpn_required` is on (`_stop_daemon_for_network_policy`) or when the
`vpn_required` / `bind_interface` policy changes — each time, its
process-local GIDs are converted back to resumable `queued` entries first.

## Moving downloaded files out (`list_files`/`move_files`)

A completed torrent's row grows a "Move files" button (`status === "complete"`
only). The backend never trusts a client-supplied path directly -- it only
moves files that are in the torrent's own **known files** set, computed by
`_resolve_known_files(entry)`:

1. Preferred source: `entry["files"]`, a list of absolute paths captured from
   aria2's own `aria2.tellStatus` response (`_TELL_STATUS_KEYS` includes
   `"files"`; `_apply_aria2_status_locked` copies `result["files"][*]["path"]`
   into the entry on every successful status poll, not just at completion, so
   it's populated well before the torrent finishes). This field is added to
   `_ENTRY_PERSISTED_FIELDS`, so it survives a Drone restart even after aria2's
   own GID/history does not.
2. Fallback (pre-upgrade entries, or aria2 not reporting `files` for some
   reason): a single-file guess, `Path(download_dir) / name`.

`move_files(entry_id, requested_paths, destination, cleanup=...)` intersects
`requested_paths` against the known-files set (anything not in it is silently
dropped, not moved) and validates `destination` against `_browse_roots()`
exactly like the folder browser -- the same security boundary, reused. Moved
files are individually `shutil.move`d with a `name (2).ext` collision suffix
(same pattern as `save_uploaded_torrents`).

**Cleanup semantics (`cleanup=True`), verbiage: "Delete the remaining
downloaded files after moving (only if the move succeeds)"** -- cleanup only
fires when *every* requested file moved without error
(`all_succeeded = not errors and len(moved) == len(selected)`); a partial
failure never deletes anything. What "cleanup" deletes is computed by
`_torrent_root_dir(entry, known_files)`: if all known files share one first
path segment under `download_dir` (aria2 made a dedicated per-torrent
subfolder -- the common case for multi-file torrents), that whole subfolder is
`shutil.rmtree`'d, which correctly sweeps up files the user did *not* select
too. If the known files sit directly in `download_dir` (single-file torrents,
or a `download_dir` shared by multiple torrents), there is no dedicated
subfolder to remove -- cleanup only unlinks the specific known files, **never**
`download_dir` itself, since that folder may hold other torrents' payloads.
This same `_remove_downloaded_payload` helper backs `delete()` and `clear()`
below.

A destination the user picks is remembered in `self._recent_move_locations`
(module constant `MOVE_RECENT_LOCATIONS_MAX = 8`, most-recent-first, deduped,
persisted as a top-level `recent_move_locations` snapshot field) -- the
frontend merges this with a small hardcoded suggestion list
(`/userdata/roms`, `/userdata/bios`, `/userdata/saves`, `/userdata/movies`) as
quick-pick chips, recent locations first.

## Delete now removes downloaded files too

`delete()` used to explicitly keep payload files (`downloaded_files_kept:
true`). It now calls `_remove_downloaded_payload(entry)` after removing the
`.torrent` file and registry entry, and reports
`downloaded_files_removed: bool` instead -- **this is a breaking response-field
rename**, not additive; anything reading the old field name needs updating.
Applies regardless of the torrent's status (queued/downloading/error/complete)
-- an in-flight download's partial files are removed too. The frontend's
confirm dialog text was updated to say so explicitly; don't silently soften it
back to "keeps files."

## Global pause / resume / bulk clear

Mirrors `download_manager.py`'s `pause()`/`resume()`/`clear_queue()` pattern,
adapted for aria2: `pause()` sets `self._paused` (persisted) and calls the real
aria2 RPC method `aria2.pauseAll` (pauses every active/waiting download in one
call); `resume()` clears the flag and calls `aria2.unpauseAll`. `_tick()`'s
Phase C skips `_pick_startable_gids_locked()` entirely while `self._paused` --
newly-scanned `.torrent` files still get registered and added to aria2 (still
paused, harmless), they just never get unpaused until resume. Force Start is
**not** blocked by the global pause (deliberate -- "force" means override
everything, consistent with it already bypassing the concurrency limit).

One real side effect worth knowing: pausing a **seeding** (`complete` +
`seeding: true`) torrent also pauses its upload via `aria2.pauseAll`, and
`_apply_aria2_status_locked` maps aria2's `"paused"` status to our `"queued"`
UI status (there's no separate "seeding-paused" state in the 4-value enum) --
so a paused, still-registered seeding torrent will display as `queued` until
resumed. This is a pre-existing status-mapping simplification, not a new bug;
don't "fix" it by adding a 5th status without deliberately deciding to expand
the enum everywhere it's checked.

`clear(payload)` bulk-processes torrents matching `scope` (`"completed"` =
only `status == "complete"`, `"all"` = every entry) against three independent
boolean flags: `delete_from_ui`, `delete_torrent_file`, `delete_downloaded_files`
(at least one required, else `{"status": "no_action_selected"}`). Any entry
touched by `delete_downloaded_files` **or** `delete_from_ui` gets
`_remove_from_aria2(gid)` called first (even if staying registered) --
otherwise a live aria2 download would keep writing into a folder `clear()` is
about to delete out from under it. If files are deleted but the entry is
**not** removed from the UI, the live entry is patched with `files: []` and
`message: "Downloaded files removed"` so the row doesn't lie about having
content.

## Display sort: actively-downloading first

`snapshot()` re-sorts the **already-built** `torrents` response list by
`_STATUS_DISPLAY_PRIORITY` (`downloading=0, queued=1, error=2, complete=3`,
tiebreak by `added_at`) right before returning. This is deliberately a
presentation-only pass over the final list -- `_sorted_entries_locked()`
(FIFO by `added_at`, used internally for scheduling fairness and restore
order) is **untouched**, so reordering the API response never changes which
queued torrent gets the next free slot.

## File allocation is a checkbox, not aria2's 4-way enum

aria2 supports `none`/`prealloc`/`trunc`/`falloc` for `--file-allocation`, but
the UI only exposes a single "File allocation" toggle switch: on maps to
`prealloc` (aria2's own recommended default, steadier writes, slower start),
off maps to `none`. The other two modes are supported at the
`_normalize_torrent_settings`/API level (a direct API caller can still send
`trunc`/`falloc`) but deliberately not surfaced in the simplified UI control —
don't rebuild that mapping as a 4-way dropdown without a specific reason,
since `falloc` in particular can error out on some FAT-formatted userdata
partitions.

## Live-refresh (flash-free) pattern

See `drone-admin-features`' "Live-refreshing tile pattern" section --
`renderTorrentsLive`/`patchTorrentsLive` in `drone.js` is the original
implementation of that pattern (VPN's is the second). Patch, never
re-`innerHTML` the whole tile, on every 3s poll tick.

## Common failure patterns

- Baking `effective_download_directory()`'s result into settings storage
  instead of resolving it lazily -- breaks the "changing the watch folder also
  moves the un-overridden default" behavior.
- Forgetting `--rpc-save-upload-metadata=false` if `Aria2Daemon`'s launch args
  are ever touched -- silently duplicates every torrent (see above).
- Touching `Aria2Daemon.start()`'s VPN args (`--interface=tun0`,
  `--disable-ipv6=true`, `--bt-enable-lpd=false`) or `stop()`'s process-tree
  teardown without re-verifying the "Require VPN" kill switch -- these are the
  no-leak boundary, not cosmetic. See the "Require VPN" section.
- Adding any new torrent code path that reaches aria2 (a new RPC caller, a new
  entry point) without routing it through the gated `_tick` / without an
  `_vpn.tunnel_is_up()` re-check -- `force_start()` and `resume()` re-check for
  a reason; a path that skips the gate is a leak while `vpn_required` is on.
- Assuming `_ensure_rpc` will refuse to start a daemon when the VPN is down --
  it won't; the gate is `_tick` returning *before* Phase B ever calls
  `_ensure_rpc`. Don't move the `vpn_blocked` early-return.
- Using `killall openvpn`-style broad process signals against aria2c --
  there's one shared daemon for the whole manager; use RPC calls against a
  specific GID instead.
- Assuming a torrent's `download_dir` is permanently fixed at scan/add time --
  it isn't anymore; it follows the current setting until the torrent actually
  has bytes on disk (`_refresh_pending_download_dirs_locked`). Conversely,
  don't assume it's *always* live-updated either -- once `completed_bytes > 0`
  it's frozen, by design (a torrent already downloading must not move).
- Trying to retarget an already-added BitTorrent download's directory via
  `aria2.changeOption`'s `dir` option -- confirmed against a real aria2c that
  this silently does nothing (no RPC error, but the file still lands at the
  original directory). Drop the GID and let it re-add fresh instead.
- Trusting a magnet-added GID's own `"complete"` status/size at face value --
  check `followedBy` first (see "Magnet metadata hand-off" above); otherwise
  a torrent shows finished at a tiny metadata-only size while the real
  download runs untracked under a GID this manager never queries.
- Adding a new folder-picker field without reusing
  `openTorrentDirBrowser(targetInputId, title)` -- don't duplicate the modal.
- Writing a test that touches the real, unconfigured default directory
  (`<install root>/torrents`) instead of calling `update_settings({"directory": ...})`
  first with a tmp path -- see the `DRONE_VPN_DIR`-equivalent gotcha in
  `drone-vpn-management` for why this class of bug is easy to introduce for
  any install-root-relative path.
- Assuming `default_torrent_directory()` (via `common/install_paths.
  drone_install_root()`) always resolves to the stable install root -- a real
  production bug (found live 2026-07-28, see `drone-vpn-management`'s "Real
  live incident" section) had it silently landing inside a versioned
  `.releases/<version>/` deploy subfolder instead. Torrents wasn't visibly
  broken by it only because the watch/download folders are normally
  user-configured (bypassing this function); an un-configured install on the
  same deploy layout would hit it identically to how VPN did.
- Trusting a client-supplied file path in `move_files()` instead of
  intersecting against `_resolve_known_files(entry)` -- would let a caller
  move arbitrary files the torrent never downloaded.
- Computing `_torrent_root_dir()` cleanup against a shared `download_dir` and
  `rmtree`-ing it directly -- only remove the dedicated per-torrent subfolder
  when one genuinely exists; a shared download dir holding other torrents'
  files must never be wholesale-deleted.
- Assuming `delete()` still keeps downloaded files -- that changed; it now
  calls `_remove_downloaded_payload()` too, and the response field is
  `downloaded_files_removed`, not the old `downloaded_files_kept`.
- Declaring a top-level `function bootstrap()`/`async function bootstrap()` in
  `drone.js` -- see the "window.bootstrap collision" note in
  `drone-admin-features`'s modal section; it silently breaks every
  `data-bs-dismiss="modal"` button app-wide, including any new Torrents modal.
- Requiring `torrent_file` unconditionally anywhere new code is added -- a
  magnet-only entry has none; check `magnet_uri` too (or use
  `entry.get("torrent_file")` truthiness rather than assuming it's always
  set) the same way `_restore_state()`, `delete()`, and `clear()` now do.
- Calling `_add_torrent_via_rpc` on an entry that has `magnet_uri` set
  (or vice versa) -- `_tick()`'s Phase B must branch on `entry.get("magnet_uri")`
  before picking which RPC caller to use.
- Assuming `_recover_from_already_registered` at add-time is the only place
  "InfoHash already registered" can appear -- it can also surface later,
  asynchronously, on a plain `tellStatus` poll with no add-time exception at
  all. See "InfoHash already registered recovery: two independent triggers"
  above; a fix that only checks the synchronous add-time exception path
  leaves the async one flapping into `error` forever.

## Expected output format

When completing Torrents work, respond using this format:

```text
Objective:
...
TorrentManager changes (torrent_manager.py):
...
aria2 runtime changes (aria2_runtime.py):
...
Backend route + handler changes (api_routes.py + handlers_torrents.py):
...
Frontend changes (drone.js):
...
Watched-folder / download-location semantics (if applicable):
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

- remove `--rpc-save-upload-metadata=false` or otherwise change
  `Aria2Daemon`'s launch args without re-running a live smoke test against a
  real aria2c binary (mocked-RPC tests cannot catch this class of bug),
- use `killall`/process-name-based signals against aria2c,
- bake a resolved default path into persisted settings where the source
  value should keep tracking a different, live setting,
- add a folder-picker without scoping it to the existing storage roots
  (`_browse_roots()`),
- move or delete a file in `move_files()`/`clear()` without first checking it
  against `_resolve_known_files(entry)` -- never trust a client-supplied path
  directly,
- `rmtree` a torrent's `download_dir` itself in cleanup logic -- only remove a
  genuine dedicated per-torrent subfolder (`_torrent_root_dir()`), since the
  dir can be shared across torrents,
- weaken the "Require VPN" kill switch: don't remove `--interface=tun0` /
  `--disable-ipv6=true` / `--bt-enable-lpd=false`, don't drop the `_tick`
  `vpn_blocked` early-return or the `force_start`/`resume` `tunnel_is_up`
  re-checks, don't loosen `_vpn.tunnel_is_up` to trust log-tail wording, and
  don't add an aria2-reaching code path that bypasses the gated `_tick`. Any
  change here needs a live check against a real aria2c + a real tunnel drop,
  not just mocked-RPC tests.

## Default bias

When unsure, keep the watched-folder/download-location split (two independent,
lazily-resolved settings) rather than collapsing them, keep the manager (not
aria2) as the source of truth for concurrency/scheduling decisions, and
validate any aria2 daemon-launch-args change against a real binary, not just
mocks.
