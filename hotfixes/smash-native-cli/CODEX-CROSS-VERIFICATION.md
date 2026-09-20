# Codex cross-verification: Smash native launch

Date: 2026-09-20

Machine: `batocera.local`, Batocera 43.1

Title: Super Smash Bros. Ultimate, `01006A800016E000`
Installed emulators: Citron 2026.2.1 (`544456b8b`), Eden v0.2.1

## Independent findings

1. Both installed Qt frontends start game-list population before command-line boot. Their game-list workers clear the manual
   content provider before repopulating it, while `BootGame()` uses that provider to resolve installed content. The provider in
   these revisions is not synchronized. This is a concrete race, and it matches the observed difference between immediate `-g`
   boot and File > Load after scanning.
2. Disabling Smash's update makes Citron autoboot; disabling DLC alone does not. Restoring the update restores the failure.
3. Both AppImages contain a separate native frontend selected through AppRun (`citron-cmd` and `eden-cli`). These paths do not
   construct the Qt game list.
4. A later Citron change (`13e8c06f44f4bc63a8edb54b40d3c07e953333bc`, “fix: Race Condition w/ Shutdown Logic”)
   cancels population at the start of `BootGame()`. That source-level mitigation is consistent with this diagnosis.

## Machine tests

| Test | Result |
|---|---|
| Installed Citron `citron-cmd`, disposable RGS config, three launches | 3/3 reached guest services with the update layer applied |
| Installed Eden `eden-cli`, isolated default SDL config | Reached guest services with the update layer applied |
| Eden with complete RGS Qt config copied to `eden/sdl2-config.ini` | Reached guest services; players 0–3 remained type 5 and connected |
| Eden `eden-cli -c <config>` | Immediate `std::logic_error`; avoided by the XDG default-path method |
| Citron stable 2026-03-20 AppImage | Race-fixed build began loading Smash, then SIGSEGV on this Batocera image |
| Existing Qt GUI-autoload workaround | Previously verified working and retained as selectable fallback |
| Installed native bridge, Citron | Applied update, loaded Smash, entered guest/HID services, and removed its temporary config on TERM |
| Installed native bridge, Eden | Applied update, loaded Smash, entered guest/HID services, and shut down its AppImage process group on TERM |

Representative Eden evidence from the final config test:

```text
PatchRomFS: RomFS: Update (v0.30.0) applied successfully
PatchExeFS: ExeFS: Update (v0.30.0) applied successfully
Load: Loading Super Smash Bros. Ultimate (01006A800016E000) ...
PopLaunchParameter: called, kind=2
player_0_type=5 ... player_3_type=5
player_0_connected=true ... player_3_connected=true
```

## Conclusion

The GUI automation is a valid recovery path, but it is not the best default. The native frontends remove the failing Qt worker
from the launch sequence and can consume disposable copies of RGS's generated settings. A custom Qt build with population
cancellation would offer full GUI parity, but would impose an emulator-build maintenance burden and the available fixed Citron
release is incompatible with this Batocera installation.

The remaining acceptance criterion is hands-on input: complete a match with the attached controllers and exit through the ES
hotkey. The launcher deliberately preserves the GUI workaround through `set-mode.sh gui` until that check is complete.
