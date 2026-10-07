# Admin Fixes

The web Admin panel contains a **Fixes** page for opt-in compatibility workarounds that do not belong in Batocera's read-only system image. Each fix has a live status card, an enable/disable switch, a short summary, and a title button that opens the complete technical explanation and rollback impact.

## Lindbergh input-device guard

LinuxLoader and the SDL3 build on affected Batocera releases can crash during joystick initialization when a cabinet exposes more joystick-class devices than LinuxLoader's eight-controller model expects. The guard is installed as the supported Batocera game hook:

`/userdata/system/scripts/drone-lindbergh-input-device-guard.py`

It acts only on `lindbergh` launches. When `/sys/class/input/js*` exceeds eight entries, it groups entries by physical USB parent, protects devices whose names identify them as light guns, ignores every single-controller device, and temporarily unbinds a suitable multi-port adapter. `gameStop` restores the adapter. A detached watchdog handles abnormal `emulatorlauncher` exits, the next launch repairs stale state, and rebooting also restores normal USB binding.

Configuration and the rotating activity log are stored under `/userdata/system/input-device-guard` and `/userdata/system/logs/input-device-guard.log`. Disabling the fix restores any active adapter before removing the Drone-owned hook.

## Game crash notifier

Batocera returns silently to EmulationStation when an emulator dies at launch. This fix installs `/userdata/system/scripts/drone-game-crash-notifier.py`, which starts a detached watcher at every `gameStart` (any system) tied to the `emulatorlauncher` process. When the launcher exits, the watcher reads only the part of `es_launch_stderr.log` written during that session and scores it:

- a crash signature (stack-smashing abort, segmentation fault, core dump, abort, illegal instruction, bus error, launcher traceback, or "Failed to load content") is enough on its own;
- a short session (under 15 seconds) only counts when `dmesg` also shows a segfault or out-of-memory kill in the same window, so quitting a game quickly is never flagged;
- the launcher's own `ERROR` log level is ignored, because it logs all emulator stderr that way even on a clean exit.

On a crash it waits for EmulationStation's loopback API and posts a toast (`POST 127.0.0.1:1234/notify`) as two toasts a few seconds apart: what happened (game, system, likely cause), then what to do for that cause (for example check the ROM/BIOS, try another core, or unplug USB controllers) plus where to read the details (Admin > Debug > Game Crashes on this Drone's hostname). When more than eight joystick nodes are connected and the failure looks like memory corruption, the second toast lists the connected controllers by name and count so the culprit adapter is obvious. Every detected crash is also saved to `/userdata/system/game-crash-notifier/history.jsonl` (newest 50 kept) and shown in **Admin → Debug → Game Crashes**. Each entry records the game, ROM path and whether the file exists (and its size), system, emulator and core, what was detected and the suggested action, session length, connected controllers by name, free memory, Batocera version, any matching kernel log line, and the launch-log lines around the failure, with a button to open the full launch log. The page can clear the history and warns when the fix is off, since nothing new is recorded then. Routes: `GET /v1/api/admin/crash-history` and `POST /v1/api/admin/crash-history/clear`. Configuration is `/userdata/system/game-crash-notifier/config.json` and activity is logged to `/userdata/system/logs/game-crash-notifier.log`. Disabling the fix just removes the hook. Detection depends on emulator error wording, and the toast is brief.

## Switch GUI-autoload workaround

Some Eden and Citron revisions start command-line autoboot before their asynchronous game-list worker has finished rebuilding the content provider used for updates and DLC. The managed workaround generalizes the proven Smash-only GUI load path to:

- all Switch games; or
- any set of Switch games selected from the library shown in Drone.

Drone discovers `.xci`, `.nsp`, `.nca`, and `.nro` entries under `/userdata/roms/switch` and uses `gamelist.xml` names when available. It installs a launcher and GUI automation wrapper beside the RGS Yuzu-family generator, then adds one marked dispatch block to `yuzuMainlineGenerator.py`. Eden, Eden Legacy, Eden PGO, Citron, and Citron Legacy are supported while preserving the exact AppImage selected in EmulationStation. The launcher reads `/userdata/system/switch-gui-workaround/config.json` at every launch. Unselected games immediately execute the original `-f -g` command; selected games start the GUI, wait for indexing, invoke File > Load File, and fall back to `-f -g` if GUI loading cannot be confirmed.

The first enable stores a pristine generator copy under `/userdata/system/backups/hotfixes/drone-switch-gui-workaround`. Enabling migrates the older marked Smash-only block if present. The original `hotfixes/smash-gui-autoload` bundle and its documentation remain in the repository as the independently verified reference implementation. Disabling writes `enabled: false` before removing Drone's generator block, so even a failed generator rewrite leaves the launcher on the normal path.

## API

- `GET /v1/api/admin/fixes` lists the catalog, live install state, and Switch library.
- `POST /v1/api/admin/fixes/{fix_id}` accepts `enabled`, plus optional `scope` (`all` or `selected`) and `selected_games`.

Both endpoints require the ordinary authenticated admin session and respect `ADMIN_ENABLED`.
