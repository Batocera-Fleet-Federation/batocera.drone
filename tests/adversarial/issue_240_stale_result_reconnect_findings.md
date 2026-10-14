# Issue #240 stale launch-result, reconnect, and remaining-player findings

Expected behavior was derived from issue sections 2.3 (remove/leave and
session status), 3.1 (disconnects, reconnects, stale operations), 11 (launch
coordination; no launch from a dead attempt), and 17 (timeouts, bounded retry,
player disconnects, partial launch failures). No earlier test expectations
were revised. No product code, security-owned tests, VERSION, or existing
suites were changed.

Suite: `adversarial-issue-240-stale-result-reconnect-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_stale_result_reconnect_uat.py'`

Fixtures use the simulated swarm, a fixed UTC clock, real temporary configuration
files and SQLite. This does not validate real RetroArch gameplay.

This suite does not repeat the protected late-`confirm_launch` deadline case
in `adversarial-issue-240-deadline-version-uat`. That path is a passing control
on this tree. The new assertions cover a different guest report
(`launch_result` after the host already failed the player), reconnect before
the deadline when `/launch` never arrived, retry-budget consumption while the
guest is still unreachable, and roster changes that leave remaining players
permanently `DEGRADED`.

## In-scope blocking findings

1. **A stale `launch_result` after the launch deadline revives a failed guest.**
   Host a three-player RetroArch lobby (host, guest, mallory). Start. Mallory
   confirms. Guest stays unconfirmed, goes unreachable, then the host ticks
   past `LAUNCH_DEADLINE`: guest is `failed`, session `DEGRADED`. Guest never
   calls `confirm_launch`. Independently set the session ROM as the foreground
   game and send `_guest_message(..., "launch_result", ok=True)`. The host
   marks the guest `running` and the session `RUNNING`. Drone did not launch
   the guest emulator.

   Expected: the report is refused or recorded as failure; the host does not
   revive that player. Recovery is a host retry that delivers a fresh offer.
   Control: the same `launch_result` without the session game in the foreground
   stays `failed`. Control: a host retry while the guest is reachable still
   recovers.

   Evidence: `_verified_launch_payload` accepts any reporter that currently
   runs the session ROM. `_respond_launch_result` still upgrades `failed` to
   `running`. Distinct from `confirm_launch`, which now checks `launch_allowed`.

2. **One missed `/launch` POST immediately fails the player, so they cannot join before the deadline.**
   Guest is unreachable at Start. Mallory is online and confirms. The host
   marks the guest `failed` as soon as the launch POST fails, instead of
   leaving them pending until `LAUNCH_DEADLINE`. Bringing the guest back
   before the deadline leaves `nothing_to_launch`.

   Expected: a brief disconnect during start is not a launch failure. The
   guest who returns before the deadline can still confirm. `_send_launch`
   writes `P_FAILED` on the first `peer_unreachable`.

3. **A sole unreachable guest at Start immediately fails the session and closes the host's game.**
   Same immediate `/launch` failure, with only one guest. The host has already
   started the ROM, then `evaluate_launch` sees no running guests, the session
   becomes `FAILED`, and `fail_session` exits the owned host game.

   Expected: wait until the launch deadline for that guest to reconnect;
   do not abort an already-started host game because the first POST missed.

4. **Retries while the guest is still unreachable spend the bounded retry budget.**
   After a deadline failure, retry the guest three times while they remain
   offline. Each retry immediately re-fails (`_send_launch` POST miss). When
   the guest returns, `retry_connection` raises `retry_limit` and they cannot
   confirm. Control: a single retry while the guest is reachable still
   delivers a fresh offer.

   Expected: bounded retries recover a returning player. Retrying an
   unreachable player must not consume the budget that would recover them
   once they are online. Spec 3.1 reconnects; spec 17 bounded retry.

5. **Removing a player from a healthy running session leaves it permanently `DEGRADED`.**
   Three-player session is `RUNNING`. Host removes the guest. Mallory is still
   `running`. After ticks the session stays `DEGRADED`. `remove_player` forces
   `DEGRADED` whenever any remaining guest is `running`. `_probe_player`
   recover treats `P_REMOVED` as a blocker for `DEGRADED` → `RUNNING`.

   Expected: Remove player is an intentional roster change (spec 2.3). Remaining
   connected players who are running the session game are a healthy session.

6. **A guest leaving a healthy running session leaves it permanently `DEGRADED`.**
   Same three-player `RUNNING` session. Guest `leave_session`. Mallory remains
   `running`. Session stays `DEGRADED`. `_respond_leave` sets `DEGRADED` on
   `RUNNING`; recover treats `P_LEFT` as a blocker.

   Expected: Leave session (spec 2.3) leaves remaining running players healthy.

## Passing controls in this suite

- `launch_result` after the deadline without the session game running stays
  `failed`.
- A host retry while the failed guest is reachable still delivers a fresh
  offer that `confirm_launch` can use.

## Validation

- New registered command: 8 collected, 2 passed, 6 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  118 collected, 112 passed, only the 6 new failures (40.406 seconds).
- Existing `adversarial-issue-240-deadline-version-uat` is retained unchanged
  and is a passing control for late `confirm_launch` (not this suite's
  `launch_result` path).
- Registry: prior `adversarial-issue-240-*` suites retained; new timeout 300
  seconds; origin `adversarial`; enabled; disruptive false.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery
  actions.

## Out of scope

None from this pass. No unrelated existing suite was used as a #240 blocker.
