# Issue #240 independent adversarial findings

Acceptance rules derived from issue sections 7/13 (evidence for tested settings only),
10 (restoration and interrupted-operation recovery), and 17 (partial failures,
meaningful errors, no abandoned resources). No earlier expectations were revised.
No product code, security-owned tests, VERSION, or existing suites were changed.

Registered suite: `adversarial-issue-240-launch-evidence-recovery-uat`.
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_launch_evidence_recovery_uat.py'`.
Four deterministic tests, real temporary configuration files/SQLite, simulated peer
transport and emulator. This does not validate real RetroArch gameplay.

## Blocking, in scope

1. **Compatibility evidence describes lobby settings rather than played settings.**
   Create a ready RetroArch lobby with `snes.rewind=0`; change the host's permanent
   setting to `snes.rewind=1` before starting; complete and confirm the session;
   restore `snes.rewind=0`. The old, unplayed settings now receive
   `VERIFIED_COMPATIBLE`. The fixture explicitly refreshes detection and proves the
   two settings yield distinct configuration fingerprints. An unchanged-settings
   control passes. `record_result` uses `local.tested_config_fingerprint` captured
   during creation, while `run_start` neither rebinds that evidence to launch-time
   settings nor rejects drift. The test accepts a clear refusal before launch as
   an alternative to recording actual launched settings.

2. **Guest failed-launch bookkeeping is skipped on a restoration backend error.**
   Start the host; have the guest launcher fail with `EmulationStation unavailable`;
   fault the guest restoration backend with SQLite `OperationalError`. After local
   confirmation, the guest remains `launching` with a consumed offer, instead of
   recording failed launch/error and retaining the journal for bounded recovery.
   `run_guest_launch` invokes restoration before recording the failed launch, outside
   an independent cleanup-error guard. SQLite journal access can raise even though
   ConfigGuard turns ordinary file-write failures into returned errors.

3. **Guest interrupted-launch recovery abandons configuration after shutdown failure.**
   Simulate a restart after guest configuration was applied and its game began,
   with persisted `launch_status=launching`. Fault emulator shutdown with `OSError`.
   `recover()` returns but leaves temporary per-ROM netplay settings in place and
   launch status unchanged. Its outer exception handler skips restoration and
   failure bookkeeping when `exit_game()` raises. Process shutdown must not skip
   independent restoration; host cleanup already implements that separation.

All three are inside the requested multiplayer reliability/compatibility scope.
No out-of-scope findings were discovered in this pass.

## Validation outcomes

- New registered command: 4 collected, 1 passed, 3 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  93 collected, 90 passed, only the 3 new failures (29.741 seconds).
- Existing `adversarial-issue-240-cleanup-error-uat`: 4 passed unchanged.
- Registry comparison against HEAD confirms all existing suites and top-level
  metadata are preserved exactly; new timeout is 300 seconds.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
