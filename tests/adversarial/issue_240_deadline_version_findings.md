# Issue #240 launch-deadline offer and version-evidence findings

Expected behavior was derived from issue sections 3.1 (disconnects, stale
operations, host unavailability), 5.1 (RetroArch core/emulator version
comparison), 7 (one success is not proof across emulator versions), 11 (launch
coordination; no launch from a dead attempt), 14 (disable multiplayer), and 17
(timeouts, meaningful errors, no abandoned start). No earlier test expectations
were revised. No product code, security-owned tests, VERSION, or existing
suites were changed.

Suite: `adversarial-issue-240-deadline-version-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_deadline_version_uat.py'`

Fixtures use the simulated swarm, a fixed UTC clock, real temporary configuration
files and SQLite. This does not validate real RetroArch gameplay.

## In-scope blocking finding

1. **A guest can confirm a launch offer after the host has already failed that player for the launch deadline.**
   Host a three-player RetroArch lobby (host, guest, mallory). Start. Guest goes
   unreachable with an unconfirmed offer. Mallory confirms, so the session stays
   live. Advance past `LAUNCH_DEADLINE` (120s) and tick: the host marks the guest
   `failed` and the session `DEGRADED`. Bring the guest back. Guest `recover()`
   leaves the offer in place. Guest `confirm_launch` starts the ROM.

   Expected: confirmation is refused or voided; the guest emulator is not started;
   the host does not revive that player to `running`. Recovery is a host retry that
   delivers a new offer. Control: the same guest still joins when they confirm
   before the deadline while mallory is already running.

   Evidence: `_require_live_launch_host` treats host `DEGRADED` plus player status
   `failed` as a live launch authorization. `_respond_launch_result` also accepts
   success from a `failed` player. The offer expiry window (5 minutes) is longer
   than the launch deadline (2 minutes), so the stale offer remains locally valid.

## Passing controls in this suite

- Confirming after the host completed while the guest was offline does not launch
  (the previously blocking stale-offer suite now also passes unchanged).
- RetroArch core version mismatch and RetroArch emulator version mismatch block
  join with an actionable message.
- A confirmed success does not verify a different RetroArch version.
- Disabling Multiplayer refuses Start before any emulator launch.
- Host loss during a running session restores the guest's temporary settings
  without closing the locally confirmed game.
- An infrastructure launch failure cannot be recorded as play evidence.

## Validation

- New registered command: 9 collected, 8 passed, 1 assertion failure above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  110 collected, 109 passed, only the 1 new failure (36.478 seconds).
- Existing `adversarial-issue-240-stale-offer-compat-uat`: 4 passed unchanged
  (the prior infrastructure-failure and host-FAILED-offer findings are addressed
  on this tree; this suite does not weaken them).
- Registry: 16 `adversarial-issue-240-*` suites; prior 15 retained; new timeout
  300 seconds; origin `adversarial`; enabled; disruptive false.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery
  actions.

## Out of scope

None from this pass. No unrelated existing suite was used as a #240 blocker.
