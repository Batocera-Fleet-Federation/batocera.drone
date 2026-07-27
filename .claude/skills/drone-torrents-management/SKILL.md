---
name: drone-torrents-management
description: Use this when designing, reviewing, debugging, or modifying the Drone's Torrents admin tile — watched-folder .torrent downloads, the local aria2c daemon/RPC lifecycle, the watched folder vs. download-location distinction, aria2c install, .torrent upload, force-start/cancel/delete, or app/transfer/torrent_manager.py, aria2_runtime.py, web/handlers_torrents.py.
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
                        # health-check the RPC daemon), Aria2Rpc (JSON-RPC client)
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

Changing either setting only affects **future** scans/additions — an
already-registered torrent keeps its original `torrent_file` and
`download_dir` exactly as recorded at scan time (`_ENTRY_PERSISTED_FIELDS`),
even if the settings change later. `update_settings()` creates both
directories on disk (`mkdir(parents=True, exist_ok=True)`) if they don't
already exist, but only bothers with `download_directory` when it actually
differs from `directory` (avoids a redundant mkdir call).

The snapshot (`GET /admin/torrents`) exposes `effective_download_directory`
and `download_directory_exists` (in addition to the pre-existing `directory`/
`directory_exists`) precisely so the UI can show the *resolved* default in a
placeholder without recomputing the fallback logic client-side, and warn about
a not-yet-created download folder independently of the watch folder.

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
single shared daemon process the whole manager depends on; it is never killed
except implicitly via `Aria2Daemon.stop()` at Drone shutdown
(`--stop-with-process=<drone-pid>` ties its lifetime to the Drone process so
nothing is ever orphaned).

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
- Using `killall openvpn`-style broad process signals against aria2c --
  there's one shared daemon for the whole manager; use RPC calls against a
  specific GID instead.
- Assuming a torrent's `download_dir` updates retroactively when settings
  change -- it's fixed at scan/add time, by design.
- Adding a new folder-picker field without reusing
  `openTorrentDirBrowser(targetInputId, title)` -- don't duplicate the modal.
- Writing a test that touches the real, unconfigured default directory
  (`<install root>/torrents`) instead of calling `update_settings({"directory": ...})`
  first with a tmp path -- see the `DRONE_VPN_DIR`-equivalent gotcha in
  `drone-vpn-management` for why this class of bug is easy to introduce for
  any install-root-relative path.

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
- delete a torrent's downloaded payload files (`delete()` removes the
  `.torrent` file and registry entry only; downloaded files are always kept),
- bake a resolved default path into persisted settings where the source
  value should keep tracking a different, live setting,
- add a folder-picker without scoping it to the existing storage roots
  (`_browse_roots()`).

## Default bias

When unsure, keep the watched-folder/download-location split (two independent,
lazily-resolved settings) rather than collapsing them, keep the manager (not
aria2) as the source of truth for concurrency/scheduling decisions, and
validate any aria2 daemon-launch-args change against a real binary, not just
mocks.
