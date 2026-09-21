# Admin Fixes

The web Admin panel contains a **Fixes** page for opt-in compatibility workarounds that do not belong in Batocera's read-only system image. Each fix has a live status card, an enable/disable switch, a short summary, and a title button that opens the complete technical explanation and rollback impact.

## Lindbergh input-device guard

LinuxLoader and the SDL3 build on affected Batocera releases can crash during joystick initialization when a cabinet exposes more joystick-class devices than LinuxLoader's eight-controller model expects. The guard is installed as the supported Batocera game hook:

`/userdata/system/scripts/drone-lindbergh-input-device-guard.py`

It acts only on `lindbergh` launches. When `/sys/class/input/js*` exceeds eight entries, it groups entries by physical USB parent, protects devices whose names identify them as light guns, ignores every single-controller device, and temporarily unbinds a suitable multi-port adapter. `gameStop` restores the adapter. A detached watchdog handles abnormal `emulatorlauncher` exits, the next launch repairs stale state, and rebooting also restores normal USB binding.

Configuration and the rotating activity log are stored under `/userdata/system/input-device-guard` and `/userdata/system/logs/input-device-guard.log`. Disabling the fix restores any active adapter before removing the Drone-owned hook.

## Switch GUI-autoload workaround

Some Eden and Citron revisions start command-line autoboot before their asynchronous game-list worker has finished rebuilding the content provider used for updates and DLC. The managed workaround generalizes the proven Smash-only GUI load path to:

- all Switch games; or
- any set of Switch games selected from the library shown in Drone.

Drone discovers `.xci`, `.nsp`, `.nca`, and `.nro` entries under `/userdata/roms/switch` and uses `gamelist.xml` names when available. It installs a launcher and GUI automation wrapper beside the RGS Yuzu-family generator, then adds one marked dispatch block to `yuzuMainlineGenerator.py`. The launcher reads `/userdata/system/switch-gui-workaround/config.json` at every launch. Unselected games immediately execute the original `-f -g` command; selected games start the GUI, wait for indexing, invoke File > Load File, and fall back to `-f -g` if GUI loading cannot be confirmed.

The first enable stores a pristine generator copy under `/userdata/system/backups/hotfixes/drone-switch-gui-workaround`. Enabling migrates the older marked Smash-only block if present. The original `hotfixes/smash-gui-autoload` bundle and its documentation remain in the repository as the independently verified reference implementation. Disabling writes `enabled: false` before removing Drone's generator block, so even a failed generator rewrite leaves the launcher on the normal path.

## API

- `GET /v1/api/admin/fixes` lists the catalog, live install state, and Switch library.
- `POST /v1/api/admin/fixes/{fix_id}` accepts `enabled`, plus optional `scope` (`all` or `selected`) and `selected_games`.

Both endpoints require the ordinary authenticated admin session and respect `ADMIN_ENABLED`.
