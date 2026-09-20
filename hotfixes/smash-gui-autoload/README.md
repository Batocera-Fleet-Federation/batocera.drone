# Hotfix: Smash Ultimate GUI-autoload (Eden + Citron on RGS Batocera)

## Problem
RGS launches Switch games with command-line autoboot: `./<emulator>.AppImage -f -g <rom>`.
For Super Smash Bros. Ultimate (title ID `01006A800016E000`, with an installed update and DLC) that autoboot starts the game
before the emulator has indexed the update/DLC content:

| Emulator | Symptom with autoboot |
|---|---|
| Citron `2026.2.1` | Hangs forever on "Launching...". The emulated console boots, then never issues its first read of its own storage. |
| Eden `v0.2.1` | Loads, but reports the wrong game version and gameplay stalls (fighters freeze while the world keeps moving). |

Loading the same ROM from the emulator's own File > Load File works every time (7/7 Citron, 3/3+ Eden).
Ruled out (tested): ES environment variables, `-f`, shader backend/cache, per-game ini, game-list scan, multicore / fastmem /
async GPU, controller config, `vm.max_map_count`, disk I/O, a newer Citron build (2026-04-27 segfaults on Batocera 43.1), and
disabling the update (which removes the DLC fighters, whose assets ship inside the update).

## What the hotfix does
* `gui-autoload.sh <citron|eden> <rom>` starts the emulator **GUI** (no `-g`), waits for the game list to settle, then drives
  File > Load File with `xdotool` (Ctrl+O, type path, Enter), presses F11 for fullscreen, and hands over.
  A black `mpv` cover hides the waiting; the emulator dialog is visible for ~2 s (Qt only accepts the shortcut while focused).
  If the game never appears it falls back to the original `-f -g` launch.
* A 4-line block in `yuzuMainlineGenerator.py` uses the wrapper for **Smash only, with Eden or Citron**; everything else is unchanged.

## Use (run on the Batocera machine as root)
```
./check.sh    # exit 0 = in place, 1 = missing
./apply.sh    # install / repair; idempotent; refuses (exit 3) and touches nothing if the generator layout changed
./revert.sh [--remove-wrapper]   # exact inverse of apply.sh (byte-identical original)
```
`RGS_GEN_DIR=/tmp/copy ./apply.sh` operates on a copy of the generator (used by the tests).

## Re-applying after an RGS update
An RGS update that replaces `generators/yuzu/yuzuMainlineGenerator.py` silently removes the patch: run `./check.sh`, and if it
says MISSING run `./apply.sh`. To do it automatically at boot, add to `/userdata/system/custom.sh`:
```
[ -x /userdata/system/backups/hotfixes/smash-gui-autoload/check.sh ] || exit 0
/userdata/system/backups/hotfixes/smash-gui-autoload/check.sh >/dev/null || /userdata/system/backups/hotfixes/smash-gui-autoload/apply.sh
```
If `apply.sh` exits 3 the upstream generator changed shape: the hotfix must be re-fitted (or the bug fixed upstream).

## Requirements
`python3`, `bash`, `xdotool`, `wmctrl` (all present on Batocera 43.1), `mpv` (optional; only for the cover).
Optional tuning file `gui-autoload.conf` next to the wrapper: `GA_FULLSCREEN=f11|config|none`, `GA_HIDE=cover|0`, `GA_START_FLAGS=`.

## Known limits
* ~20 s slower start than autoboot; ~2 s of the emulator dialog is visible.
* Depends on the emulator's File menu shortcut (Ctrl+O) and F11; a UI change upstream could break it (the fallback then runs).
* The real fix belongs upstream (Citron/Eden autoboot ordering) or in the RGS generator source.
