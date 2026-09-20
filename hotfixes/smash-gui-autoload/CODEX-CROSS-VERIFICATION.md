# Codex cross-verification: Smash launch failure

Date: 2026-09-20

Machine: `batocera.local`, Batocera 43.1

Title: Super Smash Bros. Ultimate, `01006A800016E000`

Installed emulators: Citron 2026.2.1 (`544456b8b`), Eden v0.2.1

## Source-level diagnosis

Both installed Qt frontends begin asynchronous game-list population before command-line `BootGame()`. The game-list worker
clears and rebuilds the manual content provider while `BootGame()` uses that provider to resolve installed update and DLC layers.
The provider in these revisions is not synchronized. This accounts for the observed difference between immediate `-g` boot and
File > Load after game-list population.

A later Citron change (`13e8c06f44f4bc63a8edb54b40d3c07e953333bc`, “fix: Race Condition w/ Shutdown Logic”)
cancels population at the beginning of `BootGame()`, consistent with this diagnosis. The Citron 2026-03-20 AppImage contains that
change but SIGSEGVs on this Batocera installation after beginning to load Smash. Eden v0.2.1 has no equivalent cancellation.

## Native CLI experiment and rejection

Both installed AppImages contain an SDL/command-line frontend (`citron-cmd` and `eden-cli`) that avoids the Qt game-list worker.
A candidate RGS bridge launched those frontends with disposable copies of the RGS-generated configuration.

Initial log-only tests appeared successful because both emulators applied the update and reached guest and HID services. The
required hands-on ES test disproved that conclusion:

| Emulator | Log result | Actual display/runtime result |
|---|---|---|
| Eden `eden-cli` | Update applied; guest and four HID devices initialized | SDL window remained at `FPS: 0`; CPU/GPU threads idle; Batocera launch artwork remained visible |
| Citron `citron-cmd` | Update applied; guest and four HID devices initialized | SDL window remained at `FPS: 0`; Batocera launch artwork remained visible |

The native bridge was removed from the live generator and reverted on `master`. This is why guest-service markers alone must not
be treated as launch success for this title; a presented frame and playable match are necessary acceptance criteria.

## Verified deployment conclusion

The GUI-autoload workaround remains the best deployable fix with the currently installed emulator binaries. It waits for game-list
population and then invokes File > Load, which has passed actual gameplay testing. A native Qt solution requires patched emulator
builds that serialize/cancel game-list population before `BootGame()` while remaining compatible with Batocera 43.1.

The live machine was restored to the marked `gui-autoload` generator block after the failed CLI test, and both CLI processes were
terminated. The original wrapper remains the active Smash path for Eden and Citron.
