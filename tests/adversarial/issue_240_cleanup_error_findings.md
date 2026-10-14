# Issue #240 independent cleanup error acceptance

Expected behavior was derived from issue sections 10 (restore settings), 14/18
(disable functionality/adapters safely), and 17 (restoration failures, bounded
recovery and no abandoned resources). Tests simulate emulator/process failures;
real configuration files and SQLite state are confined to temporary directories.
No product fixes, earlier expectation changes, or security-owned edits were made.

Suite: `adversarial-issue-240-cleanup-error-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p test_issue_240_cleanup_error_uat.py`

## In-scope blocking findings

1. **Disabling an adapter does not prevent a new host launch.** Create and ready
   a RetroArch lobby; save `disabled_adapters=["retroarch"]`; adapter detail says
   `enabled=false`; Start still launches. `start_session` validates installation
   but does not honor the disabled-adapter setting. Expected: actionable rejection
   before touching emulator configuration or launching.
2. **An emulator shutdown exception skips independent configuration cleanup.**
   Start both simulated participants; inject `OSError` from host `exit_game`;
   End leaves temporary per-ROM netplay/core settings and a pending journal.
   `cleanup_host_resources` calls exit before restoration without a finally or
   equivalent independent error handling. Expected: attempt configuration and
   other resource cleanup even if process shutdown fails, and report failure.
3. **A transient restoration failure has no housekeeping recovery.** Inject a
   temporary `_atomic_write` error during End, then restore healthy storage.
   Six housekeeping sweeps over three simulated minutes leave session settings
   and journal pending. Completed sessions are skipped by `_tick_session`;
   `tick` sweeps generated watches but not pending restore journals. Expected:
   bounded restoration recovery for ended sessions, retaining diagnostics on
   permanent failure. A restart should not be the only automatic recovery path.

## Passing control and previous suite

The new control test confirms failed restore errors are visible in events, the
journal survives, and manual restoration succeeds once storage recovers.
The unchanged `adversarial-issue-240-lobby-dial-cleanup-uat` suite passed all
10 tests, including slot displacement, gameplay routing, classification and
partial-launch/generated-profile cleanup. No dispute was supplied or adjudicated.

These are issue #240 findings; none is classified outside scope. Tests are
simulated acceptance checks, not evidence of real Batocera netplay.
