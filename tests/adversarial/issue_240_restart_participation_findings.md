# Issue #240 restart participation acceptance findings

Expected behavior was derived from issue sections 3.1 (Drone restarts, disconnects,
accurate session state), 10 (restore settings after interrupted operations), and 17
(recovery after restarts, no abandoned resources, meaningful errors). No earlier
test expectations were revised. No product code, security-owned tests, VERSION, or
existing suites were changed.

Suite: `adversarial-issue-240-restart-participation-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_restart_participation_uat.py'`

Fixtures use the simulated swarm, real temporary configuration files and SQLite.
This does not validate real RetroArch gameplay.

## In-scope blocking findings

1. **Guest restart after the session game has exited leaves temporary settings in place.**
   Run a RetroArch session to RUNNING on both simulated machines; drop the guest's
   foreground game; call `recover()`. Host restart in the same situation ends the
   session and restores `batocera.conf`. Guest `recover()` reports zeros and leaves
   per-ROM netplay keys plus a pending journal, with `launch_status=running`. Two
   later housekeeping ticks eventually leave and restore. Restart recovery must
   restore the guest's session settings as soon as the session game is gone, and
   must not keep the host's player row in `running`. The still-running host game
   stays up. Control: guest restart while the session ROM is still foreground keeps
   the session. Control: host restart after its game exits completes and restores.

2. **Guest restart during launch never reaches the host inside the launch window.**
   Host Start delivers a launch offer; apply guest Netplay edits and persist
   `launch_status=launching` as an in-flight launch worker; `recover()`. The guest
   records `failed` and restores configuration (covered by the earlier launch-evidence
   suite). The host remains `STARTING` with the guest `launching` after both machines
   tick, still inside the 120s launch deadline. `recover()` does not send a launch
   result, and the host probe treats a still-LAUNCHING guest as healthy even when
   that guest is not running the session game. Expected: the host observes a failed
   launch without waiting for the deadline.

These are issue #240 reliability findings. No new out-of-scope finding was used as
a blocker. The previously blocking `adversarial-issue-240-launch-evidence-recovery-uat`
suite now passes unchanged on this tree.

## Validation

- New registered command: 4 collected, 2 passed (host dead-game recover; healthy guest recover), 2 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`: 97 collected, 95 passed, only the 2 new failures (31.361 seconds).
- Existing `adversarial-issue-240-launch-evidence-recovery-uat`: 4 passed unchanged.
- Registry: 14 `adversarial-issue-240-*` suites; prior 13 retained; new timeout 300 seconds; origin `adversarial`; enabled; disruptive false.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery actions.
