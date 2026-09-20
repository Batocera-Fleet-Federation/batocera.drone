# Hotfix: Smash Ultimate native CLI launch (Eden + Citron on RGS Batocera)

## Outcome

RGS can launch Smash directly from EmulationStation without automating the emulator GUI. This bundle selects the native
command-line frontend already contained in each installed AppImage:

* Citron: `citron.AppImage citron-cmd -c <disposable-config> -f -g <rom>`
* Eden: `XDG_CONFIG_HOME=<disposable-root> eden.AppImage eden-cli -f -g <rom>`

The original [`smash-gui-autoload`](../smash-gui-autoload/README.md) workaround remains installed and can be selected immediately
if a CLI regression is discovered.

## Why it works

The Qt frontends start asynchronous game-list population and command-line `BootGame()` concurrently. Game-list population clears
and rebuilds the shared manual content provider while `BootGame()` is resolving installed update/DLC layers. The GUI workaround
avoids the race by waiting. The SDL/command-line frontends avoid it by not starting the Qt game-list worker at all.

`smash-native-launch.sh` copies the RGS-generated configuration into `/tmp` for each launch. Citron receives that copy through
`-c`. Eden v0.2.1 crashes when `eden-cli -c` is used, so its copy is placed at Eden's normal SDL path,
`$XDG_CONFIG_HOME/eden/sdl2-config.ini`. Both methods keep RGS's graphics and multiplayer mappings while preventing the CLI
frontend from rewriting `/userdata/system/configs/yuzu/qt-config.ini`.

## Install and operate

Run as root on the Batocera machine:

```sh
./apply.sh
./check.sh
./set-mode.sh native   # default
./set-mode.sh gui      # immediate rollback to the preserved GUI workaround
./revert.sh            # restore ordinary RGS -f -g behavior
```

The generator change applies only when the emulator is Eden or Citron and the ROM path contains Smash title ID
`01006A800016E000`. Every other Switch launch is unchanged. `apply.sh` migrates either form of the preceding GUI patch and stores
a pre-native generator copy under `/userdata/system/backups/hotfixes/smash-native-cli/`.

## Limits

* Automated verification proves update-layer application, guest execution, and preservation of four controller mappings. A
  physical-controller match and ES exit-hotkey test remain the final acceptance checks.
* The SDL frontend does not provide the Qt menus while a game is running.
* Reapply after an RGS update if `check.sh` reports `MISSING`.

See [CODEX-CROSS-VERIFICATION.md](CODEX-CROSS-VERIFICATION.md) for the independent diagnosis, source evidence, and test record.
