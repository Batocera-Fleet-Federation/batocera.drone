# Integrations and the Stream Deck integration

Admin -> Integrations is the home for optional extensions that connect this Drone
with hardware or software on **the same Batocera machine**. Elgato Stream Deck is
the first integration. Everything here is local-only: no integration route is
reachable over `/peer/*`, proxied to another Drone, or able to run commands on
another machine.

## Integrations architecture

`app/integrations/registry.py` is deliberately small and static:

- `IntegrationDescriptor` -- id, name, description, icon, configure route,
  capabilities, one-line documentation.
- `Integration` (ABC) -- `get_status()`, `card_status()`, `install()`, `enable()`,
  `disable()`, `repair(reinstall=)`, `remove(include_configuration=)`. Long
  operations return a job descriptor instead of blocking a request.
- `IntegrationRegistry` -- `register`, `get`, `ids`, `cards()`. A provider that
  throws is shown as an `error` card; it never hides the others.
- `build_integration_registry(settings, repository)` -- the one reviewed place
  integrations are registered. There is no dynamic plugin loading.

Each integration owns its lifecycle, configuration, diagnostics, and in-UI help,
keeps its files under `<install root>/integrations/<id>/`, and must be removable
without touching Batocera or other Drone features. To add one: implement
`Integration`, register it in `build_integration_registry`, add its handler branch
in `web/handlers_integrations.py`, its page in `web/static/js/integrations.js`, and
its OpenAPI paths in `web/openapi_spec.py`.

The Integrations browser bundle is loaded separately from `drone.js`. Release,
self-update, staging, and service-start validation therefore require the UI bundle,
API handler, registry, and Stream Deck manager as one atomic payload. An archive
missing any of them is rejected before overlay/launch. Development/codeload installs
whose `app/VERSION` is `dev` are intentionally upgraded to the latest validated
semantic release; they are not skipped by version comparison.

Card fields (`GET /v1/api/admin/integrations`): id, name, description, icon,
configure_route, capabilities, documentation, installed, enabled, health
(`disabled|healthy|installing|waiting|degraded|error`), health_message, version,
metrics.

## Stream Deck at a glance

```
Browser (integrations.js) --session/same-origin/JSON--> handlers_integrations
   --> StreamDeckIntegration (manager.py, Drone process, stdlib only)
         config/  scripts/  images/  (validated, atomic writes)
         compiles state/runtime.json   ----------------+
         supervises -->  worker.py (child process)     |
                          imports StreamDeck + Pillow  |
                          ONLY from private lib/  <----+ reads runtime.json
                          StreamDeckRuntime: devices, keys, actions, rendering
```

| Module | Process | Responsibility |
|---|---|---|
| `paths.py` | both | Ownership boundary, atomic writes, `flock` helpers |
| `config.py` | Drone | Schema, strict API validation, lenient load + recovery |
| `dependencies.py` | Drone | Isolated tooling install/verify/remove |
| `manager.py` | Drone | Lifecycle jobs, supervisor, status, apply, diagnostics, tests |
| `compiler.py` | Drone | Saved config -> resolved `state/runtime.json` + render specs |
| `games.py` | Drone | Game picker/resolution over the existing ROM library |
| `scripts.py` | both | Custom script store + execution |
| `images.py` | Drone | Upload validation and storage of originals |
| `jobs.py` | Drone | In-memory background jobs (progress, cancel) |
| `actions.py` | both | Typed built-in action registry + `ActionContext` |
| `game_runtime.py` | both | Active-game detection, bounded exit wait, start/stop events |
| `game_launcher.py`, `emulationstation.py` | both | Normal Batocera launch path |
| `launch.py` | both | Exit-before-launch state machine + cross-process lock |
| `dispatcher.py` | both | One typed execution path per action type |
| `process.py` | both | The only subprocess boundary |
| `devices.py` | worker | `StreamDeckDevice` abstraction, library adapter, fake, USB detection |
| `runtime.py` | worker | Hotplug, key input, profiles, rules, commands |
| `render.py` | worker | Pillow key rendering + cache |
| `worker.py` | worker | Entry point, single-instance lock, logging, signals |

## Storage and dependency isolation

`<DRONE_INSTALL_DIR>` is `common.install_paths.drone_install_root()` (the same root
Torrents and VPN use; `/userdata/system/drone-app` on a device). Everything lives
below `<DRONE_INSTALL_DIR>/integrations/streamdeck/`:

| Path | Class | Contents |
|---|---|---|
| `python/` | tooling | Private venv used only to run pip |
| `lib/` | tooling | `streamdeck==0.10.0` + Pillow (`pip install --target`) |
| `rendered/` | tooling | Device-sized PNG cache (content-addressed, max 600) |
| `state/` | tooling | `runtime.json`, worker status, pid, locks, command spool, previews, dependency marker |
| `config/streamdeck.json` | content | Profiles, buttons, device settings, rules, safeguards |
| `scripts/` | content | `<32-hex>.sh` + `<32-hex>.json` metadata |
| `images/` | content | Uploaded originals `<32-hex>.<ext>` + metadata |
| `logs/` | content | `runtime.log` (rotating 1 MiB x 3), `install.log`, `runtime-console.log` |

Every path goes through `StreamDeckPaths.assert_owned`, which refuses anything
outside the root and any symbolic link inside it (including a redirected
`integrations/` parent). Directories are created `0700`, files `0600` (scripts
`0700`).

The Drone itself stays stdlib-only. Dependencies are installed by
`DependencyManager`:

1. pip comes from a private venv in `python/` (`python3 -m venv`; its own
   ensurepip bootstraps pip *inside the venv*), or -- if venv is unavailable --
   from the pip wheel bundled with the stdlib `ensurepip`, executed straight from
   the wheel. Batocera's global Python, pip, and setuptools are never installed,
   upgraded, or removed, and `batocera-save-overlay` is never needed.
2. Packages install into `lib.staging`, are import-verified (and confirmed to load
   from that directory), then swapped into `lib/`. A failed or interrupted
   install leaves the previous `lib/` intact, so it is safe to retry.
3. `state/dependencies.json` records the versions and the interpreter tag. Startup
   only checks this marker (no reinstall on boot); a Python upgrade after a
   Batocera update shows as "needs repair" and the supervisor auto-repairs at most
   every 30 minutes while enabled.

Only the worker imports StreamDeck/Pillow, by prepending `lib/` to its own
`sys.path`; the Drone process never imports them. Upload decoding uses a short
Pillow subprocess for the same reason.

## Lifecycle

| Operation | Behavior |
|---|---|
| Enable (job) | Checking environment -> Preparing runtime -> Installing Stream Deck support -> Installing image support -> Verifying -> Detecting devices -> Creating configuration -> Starting Stream Deck service -> Connecting -> Ready. Idempotent; a second enable reuses tooling and reloads a running worker. |
| Disable | Sets `enabled=false`, stops the worker (devices are reset and released). Profiles, images, scripts, logs kept. |
| Repair (job) | Stops the worker, verifies the installed libraries (reinstalls only if broken), recovers a malformed config, recompiles, restarts and reconnects. |
| Reinstall Tooling (job) | Removes and reinstalls only `python/` + `lib/`; configuration kept. |
| Remove Tooling Only | Disables, stops, deletes `python/`, `lib/`, `rendered/`, `state/`. Keeps `config/`, `scripts/`, `images/`, `logs/`. |
| Remove Tooling + Configuration | Disables, stops, deletes the whole validated `integrations/streamdeck/` directory -- only at its expected location and only if named `integrations/streamdeck`. |

Only one lifecycle operation runs at a time (`_lifecycle_lock`); disable/remove/
tests wait briefly for a supervisor pass but fail fast while an install/repair job
is running.

## Runtime / service

- Started by the Drone (`start_runtime`) as
  `python3 -m app.integrations.streamdeck.worker --root ... --roms-root ... --parent-pid <drone>`
  with a clean environment, its own session, and stdout to `logs/runtime-console.log`.
- Single instance: the worker holds `state/runtime.lock` (`flock`) for its whole
  life; a duplicate waits up to 10 s, then exits with code 3. "Running" is decided
  by the lock, not a pid file. Stop sends SIGTERM only to a pid whose
  `/proc/<pid>/cmdline` is this worker for this root, then SIGKILL after 10 s.
- The worker exits on its own when the Drone that started it is gone (like
  aria2's `--stop-with-process`), so a Drone restart/update never leaves a stale
  worker holding the hardware.
- Supervisor thread (started from `create_server`, every 10 s): restores the
  worker after a reboot/Drone restart when enabled, restarts a crashed worker with
  exponential backoff (max 5 min), and auto-repairs missing tooling (max every
  30 min). It never delays the web listener.
- Drone -> worker communication is a file spool: the Drone writes
  `state/commands/<ns>-<id>.json` (`reload`, `identify`, `test-button`,
  `test-connection` only) and waits for `state/results/<id>.json`. Configuration
  changes are applied by recompiling `state/runtime.json` and sending `reload`; no
  Batocera reboot or worker restart is needed.
- Worker status (`state/worker-status.json`) is rewritten only when it changes,
  plus a 60 s heartbeat.

## Device abstraction, discovery, hotplug

`StreamDeckDevice` exposes `transport_id`, `device_id()` (serial, or a stable hash if
the serial is unsafe), `model`, `key_count`, `rows`, `columns`, `key_image_size`,
`has_key_images`, `open/close/reset/connected`, `serial()`, `firmware()`,
`set_brightness`, `set_key_image`, `clear_key`, `to_native`,
`register_key_callback`, and `get_capabilities()`. The API/UI only ever see the
capabilities dict. `LibraryStreamDeckDevice` adapts python-elgato-streamdeck
(`DeviceManager().enumerate()`, `PILHelper.to_native_key_format`);
`FakeStreamDeckDevice`/`FakeDeviceProvider` simulate model, geometry, images,
brightness, presses, unplug/replug for tests.

HID `get_serial_number()` is often a truncated USB iSerial (Stream Deck Mini:
12 characters over HID vs 14 on the USB descriptor). `devices.serials_match` /
`same_physical_device` treat a prefix match of 8+ characters as one physical
deck, so Connected Devices does not list the runtime row and the USB row
together. The runtime `id` stays the command key; the longer USB serial is
shown. Sysfs interface nodes (`7-2:1.0`) are skipped. Per-device settings
resolve across those serial aliases.

The Stream Deck admin UI uses the shared dark-theme contract in
`bff-ui-theme-functionality`: `themed-table`, `themed-modal` + `btn-close-white`,
`themed-accordion`. Help opened from the button editor is stacked with
`sd-modal-nested` so it is not hidden behind the editor.

Nothing is hard-coded to the Mini: layout and key image size come from the device.
Multiple decks are attached independently, each with its own brightness and
startup profile; decks without key screens (Pedal) attach without images.

Hotplug in the worker loop (every 0.25 s): the sysfs USB fingerprint (`0fd9`
devices) is sampled at most once a second; a change, or every 30 s, triggers a full
enumerate. `connected()` (which re-enumerates HID in the library) is checked only
on a USB change or every 3 s. A device that fails to open is isolated (others keep
working) and retried after 10 s; a device write failure detaches it and the next
reconcile re-attaches it. Each distinct error is logged once; the status exposes
the active errors. No device at startup is a normal "waiting" state.

Before tooling exists, `detect_usb_devices` (stdlib sysfs scan) lets the page show
attached decks; its model table is a display hint only.

## Input handling and dangerous actions

- Key-down is the trigger; key-up never triggers.
- A second key-down without key-up is ignored; key-downs within 0.25 s of the last
  trigger are ignored (debounce); a key whose previous action is still running
  ignores new presses (no overlap).
- Dangerous built-ins (`reboot-system`, `shutdown-system`,
  `restart-emulationstation`) require **hold-to-confirm**: the key shows "HOLD" and
  the action runs only if the key is still held after `hold_duration_ms` (default
  1500, 500-5000). Releasing early cancels and restores the key. Applying a new
  profile cancels pending holds. The safeguard can be turned off in Overview ->
  Safeguards (the press itself then confirms). Browser tests of dangerous actions
  always require an explicit confirmation.

## Built-in actions

Profiles store `{"action_type": "builtin", "action_id": "<id>"}` -- never a command.
`BuiltInActionRegistry` resolves the ID to a `BuiltInAction` (id, display_name,
description, category, default_icon, default_label, dangerous,
confirmation_required, compatibility, `availability(context)`, `validate`,
`execute`). Trusted commands are spelled out only in `BatoceraControl` and run via
`ProcessRunner` (argument arrays, `shell=False`).

| ID | Category | Implementation | Availability |
|---|---|---|---|
| `exit-game` | Game | `batocera-es-swissknife --emukill` (exit codes 0/20/21/22/25 accepted) | a game is running |
| `reboot-system` | System | `batocera-es-swissknife --reboot`, fallback `reboot` (hold) | tool present |
| `shutdown-system` | System | `batocera-es-swissknife --shutdown`, fallback `poweroff` (hold) | tool present |
| `restart-emulationstation` | System | `device_control._restart_emulationstation` -- same as the admin button (hold) | always |
| `volume-up` | Audio | `batocera-audio setSystemVolume +5`, fallback `amixer -q sset Master 5%+` | tool present |
| `volume-down` | Audio | `batocera-audio setSystemVolume -5`, fallback `amixer ... 5%-` | tool present |
| `mute-toggle` | Audio | `batocera-audio setSystemVolume mute-toggle`, fallback `amixer ... toggle` | tool present |
| `pause-toggle` | Game | RetroArch UDP `PAUSE_TOGGLE` | RetroArch running with `network_cmd_enable=true` and content loaded |
| `save-state` | State | RetroArch UDP `SAVE_STATE` | same |
| `load-state` | State | RetroArch UDP `LOAD_STATE` | same |

Pause/save/load read the active RetroArch process's own config files for
`network_cmd_enable`/`network_cmd_port` and verify `GET_STATUS` reports loaded
content before sending; other emulators report "unavailable" with a reason
(no blind keystroke injection). Unknown IDs, unavailable actions, and unconfirmed
dangerous actions return structured `error`/`unavailable`/`confirmation-required`
results and are logged. To add an action: register another `BuiltInAction` in
`_register_defaults` (and a default background in `compiler.ACTION_BACKGROUNDS`).

`ActionContext` carries active_game, system, emulator, core, rom, frontend_state,
device_id, profile_id, key_index, trigger, confirmed, requested_by, timestamp.

## Game library, Launch Game, GameRuntime, GameLauncher

**Library/search** (`games.py`) reuses the Drone's existing ROM library -- no second
model: `RomRepository.search_roms` (SQLite FTS over the ROM cache) for text
search, `RomRepository.list_assets(system, "roms")` (cache rows with their
`gamelist.xml` entry: title, favorite, artwork references) for per-system browse,
filter, and resolution. Results are paged; a broad search enriches at most four
uncached systems per query.

**Saved shape** (never a command or a user-typed path):

```json
{"action_type": "game",
 "game": {"id": "<ROM cache unique_id>", "name": "Super Smash Bros. Ultimate",
          "system": "switch", "rom_path": "<path relative to roms/switch/>",
          "metadata_source": "drone-rom-cache"}}
```

**Resolution** on every apply/compile: by `id`; else by saved relative path
(rescanned game -> relinked automatically, `resolution: "path"`); else
`installed: false` -> "Game not found. Relink this button." The button keeps its
configuration, the key shows the fallback art with a warning, and pressing it never
launches anything. Relink Game in the editor picks a replacement.

**GameRuntime** (`game_runtime.py`) reuses `device.game_activity.find_running_emulatorlauncher`
(the same `/proc` scan behind gameplay history and idle-exit): `is_game_running()`,
`get_active_game()` (system, rom_path, name, emulator, core, pid, started_at),
`wait_for_exit(timeout)` (polls the process; then a bounded settle on ES
`/runningGame`), `wait_for_start(rom, timeout)`, `subscribe_to_game_start/stop`,
`poll()`.

**GameLauncher** (`game_launcher.py`) confines the ROM to `<roms>/<system>/` (after
resolving symlinks) and launches through **EmulationStation's local API**:
`POST http://127.0.0.1:1234/launch` with the ROM path. ES then runs the game through
its normal path (`ViewController::launch` -> `es_systems.cfg` command ->
`emulatorlauncher`), so the game gets the same emulator choice, controllers,
shaders, per-game settings, hooks, and environment as a launch from the ES menu.
Drone never runs emulator binaries or `emulatorlauncher` directly and has no
shell fallback. ES must be running; a 404 means ES does not list the game (update
gamelists).

**Exit-before-launch** (`launch.py`, `LaunchGameCoordinator.launch`):

```
validate game (missing/escaping ROM -> error, nothing else happens)
active = GameRuntime.get_active_game()
if active is the same ROM            -> "already-running"
if active:
    state EXITING_CURRENT_GAME
    BuiltInActionRegistry.execute("exit-game")   # the central handler
    GameRuntime.wait_for_exit(exit_timeout)      # polls; never a blind sleep
    timeout -> ERROR "did not exit ... new game was not launched"
state LAUNCHING_GAME -> GameLauncher.launch(game)
GameRuntime.wait_for_start(rom, launch_confirm_timeout) -> RUNNING | launch-unconfirmed
```

**Concurrency (v1):** one transition at a time across threads *and* processes
(`threading.Lock` + `state/launch.lock` `flock`, shared by the worker and the
browser's Test Game Launch). Additional Launch Game requests during a transition
are rejected with `{"status": "busy", "error": "A game launch is already in
progress."}` -- no queueing, never a second emulator. The same key also ignores
presses while its own action runs. States: `IDLE`, `EXITING_CURRENT_GAME`,
`LAUNCHING_GAME`, `RUNNING`, `ERROR`.

Logged: button press, requested game/system, previous game, exit requested/
completed/timeout, launch started, result, duration, failure.

**Test Game Launch** (button editor) runs the identical sequence via a background
job after an explicit browser confirmation that it may close the running game.

## Custom scripts

The only editable executable action. `ScriptStore`:

- IDs are server-generated 32-hex values, re-validated on every access; files are
  always `scripts/<id>.sh` + `<id>.json` -- no request can name a path; symlinks
  are refused.
- Code must start with `#!`, be text, and be <= 64 KiB.
- Executed directly (the shebang chooses the interpreter) through `ProcessRunner`:
  `shell=False`, own process group, clean environment (`PATH`, `HOME`, `LANG`,
  `DRONE_STREAMDECK_ROOT`, and `DRONE_STREAMDECK_*` context: trigger, key,
  device id, profile id, game system, game ROM) -- the Drone's own environment is
  not inherited. Timeout default 30 s (1-300), output capped at 64 KiB per stream.
- Run/Test is a cancellable background job reporting status (Running, Completed,
  Failed, Timed Out, Cancelled), exit code, stdout, stderr, duration.
- Assignable to multiple buttons; deletion is blocked while assigned.
- Execution is logged with id, name, status, exit code, duration, requesting admin
  -- never the script source.

## Images

**Uploads** (`images.py`): PNG, JPEG, WebP; max 5 MiB and 4096x4096. Checked by
extension, declared MIME type, a stdlib structural parse (PNG chunk CRCs, JPEG
frame header, WebP header), and a full decode with the isolated Pillow in a
subprocess (`MAX_IMAGE_PIXELS` bounded) -- uploads therefore require installed
tooling. Originals are stored as `images/<id>.<ext>` and never executed;
unreferenced uploads are pruned on Apply after a one-hour grace period.

**Rendering** (`render.py`, worker): the compiler produces a *render spec* per key
(`generated` | `image` | `blank`) that both the browser preview and the worker use.
The worker renders at the device's real key size with Pillow: **Fill/Crop**
(default, covers the key, centered crop), **Fit** (whole image, background
border), **Stretch** (ignores aspect ratio); then `PILHelper.to_native_key_format`
applies the model's rotation/flip/format. Results are cached in `rendered/` keyed
by spec + source identity (path, size, mtime) + key size, so artwork is resized
once. Sources must be inside `images/` or the ROM root (symlinks resolved). An
unreadable image falls back to generated art; a render exception shows "ERR" on
that key only. The exact images last sent to the device are saved in
`state/preview/<device>/<key>.png` ("Show as rendered").

**Generated art**: text (wrapped, auto-fitted), optional secondary text, built-in
vector symbols (exit, power, reboot, restart, volume-up/down, mute, pause, save,
load, play, next, previous, profile, script, game, star), background/text colors,
text size, alignment. Built-ins have default art (symbol + label + color), so
nothing has to be designed before use.

**Game artwork**: local only, never downloaded. Automatic order: gamelist `image`,
`marquee` (logo), `wheel`, `thumbnail` (box art), `boxart`, `fanart`; then the
conventional `images/<rom stem>-image|-thumb|-marquee.*`; then a generated title
button. A specific field can be chosen; users can still generate or upload art.

## Profiles and context rules

`config/streamdeck.json`:

```json
{"schema_version": 1, "enabled": true, "default_profile_id": "default",
 "settings": {"confirm_dangerous_actions": true, "hold_duration_ms": 1500, "auto_apply": false,
              "exit_timeout_seconds": 20, "launch_confirm_timeout_seconds": 45, "script_timeout_seconds": 30},
 "devices": [{"device_id": "AL12345", "brightness": 60, "startup_profile_id": ""}],
 "profiles": [{"id": "default", "name": "Default", "buttons": [
   {"key": 0, "action_type": "builtin", "action_id": "exit-game", "label": "",
    "image": {"type": "default", "fit": "fill", "...": "..."}}]}],
 "context_rules": [{"id": "...", "enabled": true, "event": "game-start", "system": "switch",
                    "emulator": "", "profile_id": "<profile id>"}]}
```

- Profile IDs are stable and independent of names. `Default` always exists; the
  default profile and the only profile cannot be deleted. Add, rename, duplicate,
  delete, set default. Deleting a profile clears navigation buttons, device startup
  references, and rules that point at it.
- Navigation buttons: next, previous, go-to profile.
- Optional, editable context rules switch every deck's profile on `game-start`
  (optionally filtered by system and/or emulator/core) or `game-stop`. They are
  driven by `GameRuntime.poll()` start/stop events (every 2 s in the worker); the
  context model (`ActionContext`, game events) leaves room for frontend-state
  rules later without changing the runtime.
- Loading is lenient: a malformed button/profile/rule is dropped with a warning; an
  unreadable file is moved aside to `streamdeck.json.broken-<ts>` and replaced by
  defaults (disabled). API input is validated strictly (400).

## API

All under `/v1/api/admin/integrations` (see `web/openapi_spec.py`). GETs are
side-effect free; mutations are POST.

- `GET /` cards; `GET /streamdeck/status|devices|profiles|actions|scripts|logs`
- `GET /streamdeck/games?q=&system=&limit=&offset=`, `/games/systems`,
  `/games/artwork?system=&rom_path=&field=`
- `GET /streamdeck/scripts/{id}`, `/jobs/{id}`, `/images/{id}`, `/preview/{device}/{key}`
- `POST /streamdeck/enable|disable|repair|remove|apply|test-connection|settings|rules`
- `POST /streamdeck/devices/{id}/settings|identify|test-button`
- `POST /streamdeck/profiles`, `/profiles/{id}/update|duplicate|delete`,
  `/profiles/{id}/buttons/{key}`
- `POST /streamdeck/actions/test`, `/games/test-launch`
- `POST /streamdeck/scripts`, `/scripts/{id}/update|duplicate|delete|test`,
  `/jobs/{id}/cancel`, `/images/upload` (multipart)

## Security

- Every route needs a **real session cookie** (the loopback pre-authentication used
  by on-device tooling is not accepted here) and `admin_enabled`.
- Mutations must declare `Content-Type: application/json` (uploads: multipart) and,
  when the browser sends `Origin`, be same-origin -- a cross-site page cannot drive
  the code-executing endpoints.
- Local only: never on `/peer/*`, never proxied, never run on another machine.
- IDs (script, image, job, device, profile) are validated; ROM paths are relative,
  normalized, and confined after resolving symlinks; artwork/preview/log reads are
  confined to their owned roots; uploads are validated and never executed.
- No `shell=True` anywhere; built-ins and launches use fixed executables and
  argument arrays; scripts run the saved file directly with a clean environment.
- ES API client accepts only loopback `http://` URLs.
- The requesting admin is recorded on lifecycle, button, and execution log events.

## Logging

Drone-side events go to the existing activity log (`drone.log`, Admin -> Debug ->
System Logs "drone activity") as one-line `[streamdeck] event=... key=value`
records (values quoted so crafted names cannot forge lines). The worker logs the
same format to `logs/runtime.log`. Logged: enable/disable/install/repair/remove,
runtime start/stop, device attach/detach/reconnect, profile changes, button
presses, built-in executions, the full game-launch sequence, script executions,
render and device communication errors. Polling iterations are not logged; each
distinct error is logged once.

## Testing

- `tests/test_streamdeck_integration.py` -- registry, config/profiles, built-ins,
  launch coordinator (fake GameRuntime/GameLauncher: no game, active game, delayed
  exit, exit timeout, failures, competing launches, cross-process lock), game
  library, launcher confinement, lifecycle (enable twice, install failure/retry,
  disable, repair, reinstall, remove narrowness), dependencies.
- `tests/test_streamdeck_runtime.py` -- fake hardware: no/one/many devices,
  invalid devices, open/write failures, disconnect/reconnect, key input, debounce,
  overlap, hold-to-confirm, profile navigation, context rules, identify/test
  button/test connection, reload; plus the real worker process end to end against
  a fake `StreamDeck` package in a temporary `lib/` (needs Pillow).
- `tests/test_streamdeck_content.py` -- scripts (CRUD, execution, timeout,
  cancel, output, traversal), process runner, uploads, renderer (fill/fit/stretch,
  generated, artwork, per-device sizes, cache), compiler, the real
  `RomRepository`, handler security gates, and every route through a real Drone
  HTTP server. Static + HTTP UAT cover the dark-theme contract (tables, modals,
  accordions, nested help stacking) as well as the Admin tile, router branch,
  integration list, and Stream Deck renderer shipped together.
- `tests/test_self_update_extraction.py` / `tests/test_release_version.py` -- a
  `dev` source deployment upgrades to the latest semantic release, and build/update
  archives are rejected if any required Integrations UI/backend file is absent.

No physical hardware is needed for any automated test.

## Manual physical acceptance (Stream Deck Mini `0fd9:0063`)

1. With no tooling installed, attach the deck; Admin -> Integrations shows the
   Stream Deck card (Not Installed, Disabled, "Detected on USB: 1").
2. Configure -> Enable Stream Deck: watch the progress steps; Overview shows
   Tooling Installed, Runtime Running, Device Connected, model, serial, firmware,
   6 keys, the 2x3 layout; Default profile exists.
3. Diagnostics: Test Connection, Identify Buttons (1-6 for 4 s, then restored),
   Test Selected Button ("TEST", no action runs). Move the brightness slider.
4. Key 1: Built-In -> Exit Current Game, default EXIT art -> Save -> Apply. Start a
   game from ES, press the key: back to ES.
5. Key 2: Launch Game, search "Super Smash Bros. Ultimate", select; artwork is
   offered automatically; Apply. Press it from ES: the game starts exactly as from
   the ES menu (same emulator/controllers). Exit.
6. Key 3: another game. Start key 2's game, press key 3: the runtime log shows
   exit requested -> exit completed -> launch started; the second game starts only
   after the first is gone. Press keys rapidly during a transition: "busy", never
   two emulators.
7. Custom Scripts: create, save, Run/Test (stdout shown), assign to key 4, upload a
   PNG/JPEG/WebP for it, Apply, verify the key image and that the press runs it.
8. Hold Reboot for less than the hold time (cancelled), then hold fully.
9. Unplug and replug the deck: Disconnected -> Connected, keys restored. Reboot
   Batocera: runtime restarts, keys and profile restored, buttons work.
10. Disable (runtime stops, keys cleared), Enable (configuration returns).
11. Remove Tooling Only (python/, lib/, rendered/, state/ gone; config/scripts/
    images/logs kept), Enable (tooling reinstalls, configuration returns).
12. Remove Tooling + Configuration: only `integrations/streamdeck/` is deleted;
    Batocera and every other Drone feature still work.

## Known limitations

- Pause/Save/Load State work only for RetroArch cores with network commands
  enabled; other emulators report "unavailable".
- The first enable needs internet access (PyPI). Platforms without Pillow wheels
  on PyPI (e.g. 32-bit ARM builds) cannot install image support.
- The HID transport needs Batocera's `libhidapi`; if it is missing, Test
  Connection and the install log report "hid_transport: unavailable".
- Uploads require installed tooling (the full Pillow decode is the final check).
- Launch Game requires EmulationStation (its local API) to be running and to list
  the game.
- Competing Launch Game presses are rejected, not queued.
- Context rules cover game start/stop by system/emulator; frontend-state rules
  are a future extension.
