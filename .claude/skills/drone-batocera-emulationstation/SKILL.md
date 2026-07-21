---
name: drone-batocera-emulationstation
description: Use this when the Drone reads, writes, or restarts anything Batocera/EmulationStation-owned — es_settings.cfg (screen mode, system/music volume, screensaver, systems-displayed/HiddenSystems, grouped systems/.ungroup, automatic/custom game collections), es_systems.cfg (system/group definitions), the collections/custom-*.cfg files, or the stop/write/overlay-save/start EmulationStation restart sequence. Covers what's Drone-owned vs. Batocera/ES-owned state, the privileged-worker request/result file dance for non-root, and where to find ground truth in the upstream batocera-emulationstation source when a setting's exact key/semantics aren't documented anywhere else.
---

# Batocera / EmulationStation Integration Skill

## Goal

The Drone reads and writes a fair amount of state that it does not own —
Batocera OS settings and EmulationStation's own config. Almost none of it is
documented anywhere accessible (Batocera's wiki is thin and sometimes stale on
exact config keys), so getting it right means reading the real
`batocera-emulationstation` C++ source, not guessing. This skill records what's
already been reverse-engineered this way (so it isn't re-discovered from
scratch next time) and how to find more when a new setting is needed.

## Two categories of state: Drone-owned vs. Batocera/ES-owned

**Drone-owned**: the ROM/BIOS/saves metadata cache (`storage/rom_metadata_store.py`,
`storage/saves_store.py`, the `rom_metadata_cache.sqlite3` DB), the Drone's own
settings (env vars via `common/settings.py`, `automation_config.json` via
`storage/state_store.py`). The Drone fully controls read/write here — no
restart, no cross-referencing, no risk of stomping on someone else's writer.

**Batocera/ES-owned**: `es_settings.cfg`, `es_systems.cfg`, the
`collections/custom-*.cfg` files, ALSA/pulseaudio system volume. The Drone is a
**guest** here — Batocera and EmulationStation are the primary owners/writers,
the Drone is reading their config and occasionally writing to it on the user's
behalf (screen mode, volume, collections, etc.). This is the harder half, and
what the rest of this skill is about.

## es_settings.cfg — EmulationStation's main config file

- Location: `/userdata/system/configs/emulationstation/es_settings.cfg`,
  overridable via the `ES_SETTINGS_FILE` env var (`Settings.es_settings_file`).
- Format: flat XML `<map>` of typed nodes, no nesting:
  `<string name="X" value="Y"/>`, `<int name="X" value="123"/>`,
  `<bool name="X" value="true"/>`.
- **The single most important gotcha**: EmulationStation only reads this file
  at its own startup. If the Drone edits it while ES is already running, the
  change is invisible until ES restarts — there is no live-reload or IPC
  mechanism to tell a running ES process "re-read your settings." Every write
  path in this codebase therefore restarts EmulationStation as part of the
  same operation (see "The restart sequence" below). Forgetting this produces
  a very specific, very confusing bug shape: the write succeeds, the value is
  correct in the file, but nothing visibly changes — which is exactly what
  happened with idle-volume automation's config before it was fixed (a
  separate, one-directional-comparison bug, but the *symptom* — "I changed the
  setting and nothing happened" — is the general failure mode any time this
  restart step is skipped or fails silently).

Known keys the Drone reads/writes today (`app/device/device_control.py` +
`app/device/es_collections.py`):

| Key | Type | Notes |
|---|---|---|
| `UIMode` | string | `Full` / `Kiosk` / `Kid` (title-cased in the file; the Drone API surfaces lowercase `full`/`kiosk`/`kid`) |
| `MusicVolume` | int | 0-100. **Not** the same subsystem as system/game audio volume — see below |
| `ScreenSaverTime` | int | **Milliseconds**, 0 = disabled. ES's own settings UI shows/edits this in whole minutes (`value / 60000`) — always convert |
| `HiddenSystems` | string | **Semicolon**-separated system names to hide from the carousel. Absence = nothing hidden |
| `CollectionSystemsAuto` | string | **Comma**-separated enabled built-in auto-collection names (see the fixed list below) |
| `CollectionSystemsCustom` | string | **Comma**-separated enabled custom-collection names |
| `<systemName>.ungroup` | bool | Per-system. `true` = shown standalone; `false`/absent = folded into its group's shared entry. **Inverted from what you'd guess** — see Gotchas |

**Not in es_settings.cfg**: system/game audio volume. That's a Batocera OS-level
concern (`batocera-audio setSystemVolume`, ALSA `amixer` as a fallback),
applied live with no restart needed — a completely different subsystem from
`MusicVolume`. Don't conflate the two "volume" settings; `device_control.py`
keeps them as separate functions (`_get_audio_volume`/`_apply_audio_volume` vs.
the music-volume path in `es_collections.py`) for exactly this reason.

The built-in auto-collection names (fixed set, from
`CollectionSystemManager::getSystemDecls()` — NOT user-extensible, unlike
custom collections): `all`, `recent`, `favorites`, `2players`, `4players`,
`neverplayed`, `retroachievements`, `arcade`, `vertical`, `lightgun`, `wheel`,
`trackball`, `spinner`. Per-genre auto-collections also exist in EmulationStation
but are dynamically generated from scraped genre metadata, not a fixed list —
the Drone does not currently expose them.

## es_systems.cfg — system and group definitions

- Base file: `/usr/share/emulationstation/es_systems.cfg` (read-only, ships
  with Batocera). Optional per-user override: the exact same filename under
  `/userdata/system/configs/emulationstation/`. Optional per-system overlays:
  `es_systems_<name>.cfg` in the same directory, merged in by `<name>`.
- **Reuse the existing merger** — `_resolve_es_systems_effective(settings)` in
  `device_control.py` already handles base + override + overlay merging
  correctly (last-writer-wins by system name, sorted). Don't write a second
  parser; extend `_parse_es_systems_cfg`'s captured tags if you need one more
  field off each `<system>` block (it currently captures `name`, `fullname`,
  `path`, `extension`, `command`, `platform`, `theme`, `group`).
- Two *different* "hidden" concepts live near each other here — don't confuse
  them:
  - `<system>`'s own `hidden` attribute/child in es_systems.cfg: a
    **definition-time** hide (the system was never a real candidate to show,
    e.g. deprecated/internal entries). `_parse_es_systems_cfg` surfaces this
    as `data["hidden"]`.
  - `HiddenSystems` in es_settings.cfg: a **runtime, user-configured** hide
    list. This is what the Drone's "Systems Displayed" checklist actually
    edits. `get_es_collections_state` deliberately excludes
    definition-hidden systems from that checklist entirely — they were never
    real candidates, so they don't need a checkbox.
- `<group>groupname</group>` inside a `<system>` block declares which "family"
  a system belongs to for the grouping feature (e.g. several Sega Genesis/Mega
  Drive region variants grouped under `megadrive`). The group name is often
  itself also a real system name (the one used as the shared/default entry).

## The custom collections directory

`/userdata/system/configs/emulationstation/collections/custom-<name>.cfg` — one
file per user-created custom collection (a list of ROM paths, written by
EmulationStation itself when a user creates a collection in its own UI).
**Existence of the file is not the same as "enabled"** — a collection only
shows up in the carousel if its name is *also* listed in
`CollectionSystemsCustom`. `get_es_collections_state` reports the union of
"discovered on disk" and "currently enabled" so a disabled-but-existing
collection still shows up in the checklist (unchecked).

## The restart sequence — the canonical pattern, don't reinvent it

`app/set_screen_mode.py`, `app/set_volume.py`, and `app/set_es_collections.py`
are the **canonical, single-source-of-truth** implementations of "touch
Batocera/ES-owned state and make it take effect": stop EmulationStation → write
the change → `batocera-save-overlay` (best-effort, non-blocking — headless
service-worker context, must never hang the restart) → start EmulationStation
(with a `batocera-es-swissknife --restart` fallback if the init-script start
fails). Every step logs its combined output so the privileged worker's log
captures a real failure reason instead of a generic "it didn't work."

If you're adding a new Batocera/ES-owned setting, **extend
`set_es_collections.py`'s field vocabulary** (see the walkthrough below) rather
than writing a new restart script. Screen mode and system volume predate this
pattern and got their own scripts because they predate `es_collections.py`
existing at all — there's no reason for a fourth script.

**Two entry points, always both** — every one of these scripts is called two
ways, and both must exist even though only one is exercised in the current
deployment:
1. **Root-direct**: when the Drone process itself runs as root (checked via
   `os.geteuid() == 0`), the script's function is imported and called
   in-process. **This is the live path today** — verify with
   `ps aux | grep main.py` on a real device before assuming otherwise; it has
   flipped before (an earlier stale-unprivileged-worker incident, see the
   `drone-live-debugging` skill).
2. **Privileged-service-worker**: when the Drone runs unprivileged, it drops a
   `.request` file in `DRONE_SERVICE_CONTROL_DIR` (default
   `/userdata/system/drone-app/control`) and polls for a matching `.result`
   file. The root-owned worker loop (`service_bootstrap.sh`'s
   `service_control_worker()`) polls for these files every second and
   dispatches to a `set_*_as_root()` shell function, which just shells out to
   `python3 app/set_*.py <arg>`.

   For a **scalar** argument (screen mode string, volume int), the convention
   is: extract the value from the request file's first line
   (`head -n 1 "$request" | tr -cd '0-9'` etc.), delete the request file, *then*
   run the helper with the extracted value. For a **JSON blob** argument (ES
   collections' partial-update dict), that convention breaks — embedding JSON
   as a raw shell argument or extracting it via `head -n1`/`tr` is fragile. So
   `set_es_collections.py` instead takes a **file path** as its one CLI arg and
   reads+parses the JSON itself; the shell side keeps the request file in
   place until the helper (which needs to read it) returns, only removing it
   afterward. The loop is single-threaded and synchronous, so there's no race
   between "helper still reading the file" and "next iteration checks for a
   new request."

## Adding a new ES-settings field — worked example (screensaver)

This is the exact sequence `screensaver_minutes` followed; repeat it for the
next field:

1. **Find the real key, type, units, and range** — see "Finding ground truth"
   below. (`ScreenSaverTime`, int, milliseconds, UI range 0-120 minutes.)
2. **Read side** (`es_collections.py`, `get_es_collections_state`): read the
   raw typed value, convert units if needed, clamp, fall back to ES's own
   default if absent. Add the friendly field to the returned dict.
3. **Write side** (`es_collections.py`, `_build_low_level_updates`): accept
   the friendly field from the partial-update dict, validate/clamp, convert
   units, emit the low-level field `set_es_collections.py` understands.
4. **Privileged script** (`set_es_collections.py`, `_write_updates`): handle
   the new low-level field key, write it to the XML tree.
5. **Sync the recognized-keys allowlist**:
   `app/web/handlers_es_collections.py` (`_handle_admin_es_collections_post`) — the
   only allowlist now; the old central-hub action-dispatcher copy was removed with
   the rest of the retired Overmind package.
6. **OpenAPI schema** (`app/web/openapi_spec.py`): add the field to
   `EsCollectionsState` and `EsCollectionsUpdateRequest`, and to
   `tests/test_openapi_contract.py` if you added a new path (not needed for a
   field on an existing path).
7. **UI**: `app/web/static/js/drone.js` (a control on the System Info page,
   POSTing to `/admin/es-collections` with just the one changed field — the
   endpoint accepts partial updates). No second UI to keep in sync — a paired
   peer's System Info page is reached through the Swarm page's remote-management
   proxy (`handlers_remote_admin.py`, see the `drone-admin-features` skill), which
   drives this exact same route rather than a separate remote-control surface.
8. **Tests**: `tests/test_es_collections.py` in this repo (state-read,
   apply/clamp/validate, the privileged-script XML write).

## Finding ground truth: the upstream batocera-emulationstation source

Batocera's own wiki (wiki.batocera.org) is useful for broad orientation but is
thin and sometimes stale on exact `es_settings.cfg` key names — don't trust it
as the final word on a specific key. The real source of truth is the
`batocera-linux/batocera-emulationstation` C++ repo on GitHub. The `gh` CLI is
already authenticated in this environment and is the fastest way in:

```bash
# Find every place a setting is read or written:
gh search code "ScreenSaverTime" --repo batocera-linux/batocera-emulationstation

# Then read the file(s) that turn up, e.g. the settings-menu screen that
# reveals units/range/conversion (the C++ UI code is usually the clearest
# spec available — it's what actually validates/converts the value):
gh api repos/batocera-linux/batocera-emulationstation/contents/es-app/src/guis/GuiCollectionSystemsOptions.cpp \
  -H "Accept: application/vnd.github.raw"
```

Files worth knowing about when hunting for a setting:
- `es-core/src/Settings.cpp` / `Settings.h` — every setting's name, type, and
  default value (`IMPLEMENT_STATIC_INT_SETTING(ScreenSaverTime, 5 * 60 * 1000)`
  is exactly this pattern).
- `es-app/src/guis/GuiCollectionSystemsOptions.cpp` — the "GAME COLLECTION
  SETTINGS" screen: systems-displayed (`HiddenSystems`), grouped systems
  (`.ungroup`), automatic collections (`CollectionSystemsAuto`), custom
  collections (`CollectionSystemsCustom`) — i.e. the entire
  `es_collections.py` feature set was reverse-engineered from this one file.
- `es-app/src/guis/GuiGeneralScreensaverOptions.cpp` — screensaver settings
  (`ScreenSaverTime` and friends), including the slider bounds
  (`SliderComponent(mWindow, 0.f, 120.0f, 1.f, "m")` — literally the 0-120
  minute range used in `es_collections.py`).
- `es-app/src/CollectionSystemManager.cpp` — `getSystemDecls()` is the fixed
  list of built-in auto-collection names/labels reproduced in
  `AUTO_COLLECTION_DECLS`.

When a live device is reachable, cross-check the source-derived understanding
against the real file (`ssh` + read `es_settings.cfg`/`es_systems.cfg`
directly) before writing code — the source tells you what's *possible*, the
live file tells you what's *actually configured* right now, and a key that's
never been customized simply won't appear in the file at all (read code must
treat absence as "use the default," not as an error).

## Gotchas encountered (so they aren't rediscovered)

- **`HiddenSystems` uses `;`; `CollectionSystemsAuto`/`CollectionSystemsCustom`
  use `,`.** Easy to mix up since they're all "comma-separated-list-ish"
  string settings at a glance — they are not all the same separator.
- **`.ungroup` semantics are inverted from the checkbox label.** `ungroup=true`
  means "show this system standalone" (i.e. the "Grouped Systems" checkbox for
  it should render *unchecked*). `ungroup=false` or absent means "stays folded
  into the group" (checkbox *checked*). `drone.js`
  renders "grouped" checkboxes checked-by-default and, on save, computes the new
  `ungrouped_systems` list from whichever boxes are **unchecked** — and it's
  **full-replace semantics**: the caller must send the complete desired
  ungrouped set, not a diff, because `_build_low_level_updates` recomputes
  every groupable system's `.ungroup` flag from that one list (anything
  previously ungrouped but absent from the new list gets re-grouped).
- **A `<hidden>` system in es_systems.cfg is not a `HiddenSystems` entry.**
  They're unrelated mechanisms at different layers (definition-time vs.
  runtime) — see the es_systems.cfg section above.
- **System/game audio volume and `MusicVolume` are different subsystems.**
  `batocera-audio setSystemVolume` (OS-level, ALSA/pulseaudio, applies live) vs.
  `MusicVolume` (ES-internal setting, needs an ES restart to take effect when
  changed externally). Don't reuse one code path for the other.
- **`amixer` can fail on a live Batocera box even when volume control is
  fine.** `amixer sget Master` has been observed to fail with `Mixer attach
  default error: Host is down` on a real device while `batocera-audio
  getSystemVolume` and `batocera-settings-get audio.volume` both succeed.
  `_get_audio_volume`'s fallback chain (`batocera-settings-get` →
  `batocera-audio` → `amixer`) exists precisely so one broken tool doesn't
  take down volume reporting — don't "simplify" it down to a single tool.
- **A setting simply absent from `es_settings.cfg` is normal, not an error.**
  ES (and this codebase) only writes a key once something changes it away from
  the default; every reader here must treat "key not found" as "use the
  documented default," never raise.

## Where this lives in the codebase

- `app/device/device_control.py` — screen mode (`_get_screen_mode`,
  `_apply_screen_mode`, `_set_screen_mode`), system audio volume
  (`_get_audio_volume`, `_apply_audio_volume`), es_systems.cfg parsing
  (`_parse_es_systems_cfg`, `_resolve_es_systems_effective`), EmulationStation
  restart/kill primitives (`_restart_emulationstation`,
  `_emulationstation_restart_command`, `_kill_running_emulator`), and the
  privileged-worker request/result primitives for screen mode and volume
  (`_request_service_control`, `_request_screen_mode_service_control`,
  `_request_volume_service_control`).
- `app/device/es_collections.py` — music volume, screensaver,
  systems-displayed, grouped-systems, auto/custom collections:
  `get_es_collections_state`, `apply_es_collections`,
  `_build_low_level_updates`, and its own privileged-worker primitive
  (`_request_es_collections_service_control` — separate from the ones above
  because its payload is a JSON blob, not a scalar).
- `app/set_screen_mode.py`, `app/set_volume.py`, `app/set_es_collections.py` —
  the canonical root-side restart scripts. Deliberately self-contained
  (stdlib only, **no imports from the rest of `app/`**) because the privileged
  worker invokes them in a minimal environment — if you need data from
  elsewhere in the app, compute it in the caller (`es_collections.py`, which
  has full package access) and pass it through the low-level updates dict,
  don't import into the script.
- `app/service_bootstrap.sh` — `service_control_worker()` polls `.request`
  files and dispatches to the `set_*_as_root()` shell functions;
  `validate_local_app()` is the post-deploy import-sanity gate (imports
  `app.drone_api`, which transitively imports every handler mixin including
  `es_collections`/`handlers_es_collections` — a broken new module fails this
  check automatically, no per-file allowlist to maintain).
- `app/web/handlers_diagnostics.py` — the local admin HTTP handlers for screen
  mode (`_handle_admin_screen_mode_get`/`_post`) and system volume
  (`_handle_admin_system_volume`).
- `app/web/handlers_es_collections.py` — the local admin HTTP handlers for the
  music-volume/screensaver/collections family
  (`_handle_admin_es_collections_get`/`_post`,
  `_handle_admin_music_volume_post`).
- `app/web/static/js/drone.js` — the System Info page controls (Screen Mode,
  Volume, Music Volume, Screensaver, Game Collections & Systems cards). A paired
  peer's System Info page is reached and driven through the Swarm page's
  credential-gated remote-management proxy (`handlers_remote_admin.py`) rather
  than a separate mirrored UI — see the `drone-admin-features` skill.

## Testing pattern

`tests/test_es_collections.py` is the dedicated home for everything in this
skill's scope (screen mode, volume, screensaver, collections, groups) even
though the production code spans two modules (`device_control.py` and
`es_collections.py`) — keeping the tests together makes the whole
Batocera/ES-owned-state surface area easy to find in one place. Build a
minimal realistic `es_settings.cfg` + `es_systems.cfg` fixture pair (see
`ES_SETTINGS_XML`/`ES_SYSTEMS_XML` at the top of the file) rather than mocking
XML parsing — this codebase's own live-device investigations are what
produced those fixtures' shape, and keeping them realistic is what catches
real bugs (e.g. the `;` vs `,` separator mismatch would have been caught
immediately by a fixture with both key types present).
