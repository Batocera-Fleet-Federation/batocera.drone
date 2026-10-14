# Issue #240 stale launch-offer and compatibility-evidence findings

Expected behavior was derived from issue sections 7–8 (compatibility evidence,
native mechanism selection), 11 (launch coordination, no launch from a dead
session), and 17 (recovery after restarts, no abandoned emulator processes,
meaningful errors). No earlier test expectations were revised. No product code,
security-owned tests, VERSION, or existing suites were changed.

Suite: `adversarial-issue-240-stale-offer-compat-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_stale_offer_compat_uat.py'`

Fixtures use the simulated swarm, real temporary configuration files and SQLite.
This does not validate real RetroArch gameplay.

## In-scope blocking findings

1. **An infrastructure launch failure marks the game INCOMPATIBLE and blocks a new native session.**
   Host a RetroArch lobby, fail the host launch with "EmulationStation temporarily
   unavailable" (the game never starts), end the failed session, and Host Game
   again. `CompatibilityDatabase.evaluate` reports `INCOMPATIBLE` /
   `failed-sessions` because `fail_session` records a negative play result for
   the tested fingerprint. `create_session` then raises `no_mechanism` instead of
   selecting RetroArch Netplay. Control: a user-confirmed failure after both
   machines actually reached RUNNING still counts against that configuration.

2. **A stale launch offer starts the guest game after the host has already failed.**
   Host Start delivers an awaiting-confirmation offer; take the guest offline;
   host `recover()` fails the STARTING session (Drone restart while starting).
   Bring the guest back. Guest `recover()` leaves the offer in place. Guest
   `confirm_launch` starts the ROM even though the host session is FAILED.
   Expected: confirmation is refused or voided; the guest emulator is not
   started. Control: guest recover while the host is still STARTING keeps the
   offer, and confirmation still launches.

These are issue #240 reliability and matching findings. The previously blocking
`adversarial-issue-240-restart-participation-uat` suite now passes unchanged on
this tree.

## Validation

- New registered command: 4 collected, 2 passed (user-confirmed play failure still counts; unconfirmed offer still launches while host is STARTING), 2 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`: 101 collected, 99 passed, only the 2 new failures (33.038 seconds).
- Existing `adversarial-issue-240-restart-participation-uat`: 4 passed unchanged.
- Registry: 15 `adversarial-issue-240-*` suites; prior 14 retained; new timeout 300 seconds; origin `adversarial`; enabled; disruptive false.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery actions.

## Out of scope

None from this pass. No unrelated existing suite was used as a #240 blocker.
