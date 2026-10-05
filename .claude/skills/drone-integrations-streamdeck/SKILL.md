---
name: drone-integrations-streamdeck
description: Use this when changing Admin -> Integrations (the shared optional-integration registry/card page) or anything in the Elgato Stream Deck integration — tooling install/repair/remove, the isolated worker process and supervisor, device discovery/hotplug, profiles/buttons/context rules, built-in actions, Launch Game (exit-before-launch, ES launch API, active-game detection), custom scripts, image upload/rendering, diagnostics — or when adding another optional local integration. Covers app/integrations/, web/handlers_integrations.py, static/js/integrations.js.
---

# Drone Integrations and Stream Deck

Full design reference: `docs/integrations-streamdeck.md` (module map, storage
table, lifecycle, sequence diagrams, API list, manual hardware test). Read it
before non-trivial work; keep it accurate in the same change.

## Integrations framework (shared)

- `app/integrations/registry.py`: `IntegrationDescriptor`, `Integration` ABC
  (`get_status`, `card_status`, `install`, `enable`, `disable`, `repair`, `remove`),
  `IntegrationRegistry`, and `build_integration_registry()` — the **one** reviewed
  place integrations are registered. No dynamic plugin loading.
- An integration owns its lifecycle, status, configuration, diagnostics and in-UI
  help; keeps every file under `drone_install_root()/integrations/<id>/`; never
  casually modifies Batocera; removal is narrowly scoped to its own directory.
- Local-only. Routes live under `/v1/api/admin/integrations/...` in
  `web/handlers_integrations.py`, reached from `api_routes.py` after the session
  gate + `admin_enabled`. They require a **real session cookie** (loopback
  pre-auth is not accepted), JSON bodies (uploads: multipart) and same-origin
  `Origin`. Never expose them on `/peer/*`, never proxy to a peer Drone.
- Adding one: implement `Integration`, register it, add a handler branch, a page in
  `static/js/integrations.js` (all values `escapeHtml`'d, delegated
  `data-sd-*` handlers, no inline JS with data), OpenAPI paths in
  `web/openapi_spec.py::_integration_paths`, tests, docs, and this skill.
- Delivery is atomic: release/self-update/startup validation must require
  `integrations.js`, `handlers_integrations.py`, the registry, and the Stream Deck
  manager together. A source tree with `VERSION=dev` must still converge through
  the updater to the latest semantic release; otherwise a device can remain on an
  older UI forever while appearing to run normally.

## Stream Deck shape

- Drone process (stdlib only): `manager.py` (lifecycle jobs, supervisor, status,
  apply, diagnostics, admin tests), `config.py`, `compiler.py` (config →
  `state/runtime.json` with resolved games/art/render specs), `games.py`,
  `scripts.py`, `images.py`, `dependencies.py`, `jobs.py`.
- Child worker (`python3 -m app.integrations.streamdeck.worker`): the **only**
  importer of StreamDeck/Pillow, from the private `lib/`. `runtime.py` (hotplug,
  keys, profiles, rules, command spool), `devices.py`, `render.py`. Single
  instance via `state/runtime.lock` flock; exits when its parent Drone dies.
- Shared: `actions.py`, `dispatcher.py`, `launch.py`, `game_runtime.py`,
  `game_launcher.py`, `emulationstation.py`, `process.py`, `paths.py`, `logs.py`.
- Drone → worker: recompile `runtime.json` + spool `reload`/`identify`/
  `test-button`/`test-connection` into `state/commands/`; results in
  `state/results/`. No reboot or restart needed to apply.

## DO

- Enumerate dynamically (`DeviceManager().enumerate()` behind
  `LibraryDeviceProvider`); use `get_capabilities()` (layout, key image size) —
  the UI/API never see library objects. Support many decks; key-less decks too.
- Keep dependencies isolated: venv-or-bundled-pip runs `pip install --target
  lib.staging`, verify, swap into `lib/`; idempotent via `state/dependencies.json`.
- Built-ins: stable IDs in `BuiltInActionRegistry` with availability +
  confirmation metadata; trusted commands only in `BatoceraControl`.
- Launch Game: structured `{id, name, system, rom_path(relative)}` resolved
  against the ROM cache (`RomRepository.search_roms` / `list_assets`); missing →
  "Game not found", never launched; relink by path after a rescan.
- Exit-before-launch through `LaunchGameCoordinator`: reuse the central
  `exit-game` built-in, `GameRuntime.wait_for_exit` (polls the real process,
  bounded), refuse to launch on timeout, one transition at a time (thread lock +
  `state/launch.lock`), extra presses → `busy`.
- Launch through EmulationStation's loopback API (`POST :1234/launch`) so ES runs
  its normal launch path (controllers, shaders, per-game settings, hooks).
- Key-down triggers once; debounce; no overlap per key; dangerous built-ins use
  hold-to-confirm (`hold_duration_ms`).
- Validate every ID/path through `config.safe_id/hex_id/normalize_rom_path` and
  `StreamDeckPaths.assert_owned` (refuses symlinks); validate uploads (ext, MIME,
  structure, Pillow decode subprocess, size/dimensions).
- Render with Pillow at the device key size, then `PILHelper.to_native_key_format`;
  cache in `rendered/`; preview specs come from the same compiler output.
- Handle no device / hotplug / disconnect / reconnect; log each distinct error once.
- Preserve config on disable/repair/reinstall/remove-tooling; full removal only
  via `StreamDeckPaths.remove_root` at the expected location.
- Use fakes in tests: `FakeStreamDeckDevice`/`FakeDeviceProvider`, fake
  GameRuntime/GameLauncher/actions, `FakeRunner`; never require hardware.

## DON'T

- Hardcode the Stream Deck Mini (or any model/layout).
- Globally `pip install`, delete global pip/setuptools, or require
  `batocera-save-overlay`; don't import StreamDeck/Pillow in the Drone process.
- Expose Stream Deck configuration to remote Drones / peer routes.
- Implement built-ins or Launch Game as editable custom scripts, or save raw
  shell commands for either (`validate_button` rejects `command`/`cmd`/`shell`).
- Bypass ES's launch path (no direct `emulatorlauncher`/emulator binaries).
- Launch a second game before the first has exited, or use fixed sleeps as the
  synchronization mechanism.
- Use `shell=True` — every subprocess goes through `ProcessRunner`
  (argument arrays, own process group, timeout, bounded output).
- Scatter files outside `<install root>/integrations/streamdeck/`.
- Log custom script source (IDs/names only).

## Gotchas

- `device_id` is the serial when it is a safe identifier, else a stable hash —
  per-device settings key on it. HID `get_serial_number()` is often a truncated
  USB iSerial (Mini: 12 vs 14 chars); `serials_match` / `same_physical_device`
  merge those into one Connected Devices row. Keep the runtime `id` as the
  command key; display the longer USB serial.
- Stream Deck UI follows `bff-ui-theme-functionality`: `themed-table`,
  `themed-modal` + `btn-close-white`, `themed-accordion`. Help/confirm opened
  from the button editor must use `sd-modal-nested` so it stacks above the
  editor (Bootstrap otherwise paints both at z-index 1055).
- `LibraryStreamDeckDevice.connected()` re-enumerates HID; the runtime only calls
  it on a USB-signature change or every 3 s.
- Lifecycle ops share `_lifecycle_lock`; install/repair jobs hold it for minutes,
  so short ops fail fast while a job runs but wait out a supervisor pass.
- Uploads need installed tooling (the Pillow decode is the final validation).
- macOS dev: tmp paths resolve through `/private/var`; compare resolved paths.

## Tests

`tests/test_streamdeck_integration.py` (registry, config, actions, launch,
games, lifecycle, dependencies), `tests/test_streamdeck_runtime.py` (fake
hardware runtime + real worker process against a fake `StreamDeck` package),
`tests/test_streamdeck_content.py` (scripts, process runner, uploads, renderer,
compiler, real `RomRepository`, handler gates, real HTTP server routes).
Its HTTP UAT also fetches `/`, `drone.js`, `integrations.js`, and `drone.css` and
verifies the Admin tile, route, both page renderers, and the dark-theme contract
(`themed-table` / `themed-modal` / `themed-accordion` / nested help stacking).
Release/update tests reject archives missing any Integrations UI/backend
component and exercise upgrading a `dev` source deployment.
