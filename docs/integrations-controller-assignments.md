# Controller Assignments integration

Admin → Integrations → **Controller Assignments** (`#admin/integrations/controller-assignments`)
chooses which controllers become Players 1–8 for each system or game.

## Why a launch wrapper

EmulationStation decides player order in memory and passes it to `emulatorlauncher` as
`-p<n>index/guid/name/devicepath/nbbuttons/nbhats/nbaxes`. configgen
(`Controller.load_for_players`) trusts those arguments, and `gameStart` scripts run only after
they are loaded, so a hook cannot change the order. Editing `es_settings.cfg` does not help
either: EmulationStation reads it only at startup.

So the integration writes **one** file, `/userdata/system/configs/emulationstation/es_systems_zzz_drone_controllers.cfg`.
It holds a *partial* `<system>` node (`<name>` + `<command>`) for every system with a launch
command. Batocera's EmulationStation merges overlay children into the existing definition (verified
on a live device with `es_systems_override.cfg`), so path, emulators and theme still come from
Batocera, RGS, or the Drone network overlay. Only the command gains a prefix:

```
<install root>/integrations/controller-assignments/bin/drone-controller-order --runtime <state>/runtime.json -- <command in effect>
```

The file sorts last (`zzz`), carries `data-drone-owned="controller-assignments"`, and is only
replaced or removed when that marker is present. `network_es_overlay.py` skips it
(`COMMAND_ONLY_OVERLAYS`) so the wrapper is never frozen into another overlay. Both Drone
mergers (`device_control._resolve_es_systems_effective`, `network_es_overlay._effective_system_nodes`)
merge partial overlay nodes field by field, as EmulationStation does.

Writing or removing the overlay needs **one EmulationStation restart** (the page detects this by
comparing the overlay change time with the EmulationStation process start, and offers the restart).
Rule edits never do: the wrapper reads `state/runtime.json` on every launch.

## The wrapper (`launch_order.py`)

Self-contained and stdlib-only. Its source is copied verbatim to `bin/drone-controller-order`,
so a launch never imports the Drone app. Any exception execs the original command unchanged.
Order of authority:

1. **Batocera's own player assignment wins.** A controller pinned by `INPUT P<n>GUID/NAME/PATH`
   that EmulationStation actually passed stays in the slot EmulationStation gave it.
2. **Light guns are never moved** (udev `ID_INPUT_GUN=1`, or a gun-like name).
3. The remaining controllers fill the **open** slots by the most specific rule:
   game rule → system rule → preset → global best effort → default order. Controllers a rule
   does not mention follow in EmulationStation's order. Nobody is dropped and the set of slot
   numbers is unchanged. Each player's argument group moves whole, including its `index`.

`state/last-launch.json` records what happened (shown as "Last launch"). One line is also
written to `es_launch_stderr.log` with the prefix `[drone-controller-order]`.

## Matching

`classify()` tries an exact USB `vendor:product` first (SDL tables, hid-nintendo, common clones
and adapters), then the name (word-bounded patterns: GameCube, N64, SNES, NES, Sega,
PlayStation, Xbox, Nintendo, arcade), then the vendor alone. The Nintendo Switch Online NES pads
reuse Joy-Con IDs (`057e:2006/2007`), so they are recognised by name. Identity for a specific
controller is its USB port path (`usb:1-2.2.1`) or Bluetooth address, so identical controllers can
be told apart. GUID is used only when no port is recorded.

**Best effort** (`fit_score`) uses `SYSTEM_PROFILES`: per system family, the preferred controller
families (best first), whether an analog stick is needed, and the minimum button count. A preset
switches it on for one family of systems. The global setting switches it on for every system in
`SYSTEM_PROFILE_BY_SYSTEM`. The default order breaks ties.

## Limits

EmulationStation passes at most 8 controllers. The wrapper reorders those but cannot add a
controller that EmulationStation left out, and "Last launch" warns about it.

Tests: `tests/test_controller_assignments.py`.
