# Issue #240 re-invite, live-game retry, capacity, and host-capability findings

Expected behavior was derived from issue sections 2.3–2.4 (invite, leave,
decline), 3.1 (reconnects, conflicting operations), 5.2–5.3 (Dolphin/PPSSPP
host capability), 8 (never silently select an unsupported mechanism), 11
(replace a *different* running game), 14 (maximum simultaneous sessions), and
17 (abandoned sessions must not hold resources). No earlier test expectations
were revised. No product code, security-owned tests, VERSION, or existing
suites were changed.

Suite: `adversarial-issue-240-invite-retry-capacity-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_invite_retry_capacity_uat.py'`

Fixtures use the simulated swarm, a fixed UTC clock, real temporary configuration
files and SQLite. This does not validate real RetroArch, PPSSPP, or Dolphin
gameplay.

## In-scope blocking findings

1. **Invite Peer after a lobby leave reports success but delivers no invitation.**
   Host a RetroArch lobby, guest accepts then `leave_session`. Host
   `invite_peer` returns without error and the host roster shows `invited`.
   Guest `list_invitations()` is empty. `peer_invite` treats the leftover
   `accepted` invitation as a duplicate and does not reopen it as pending.
   Expected: a new reviewable invitation (still no auto-launch). Control:
   `accept_invitation` after leave, without a new invite, does not rejoin.

2. **Invite Peer after a decline is the same silent duplicate.**
   Guest declines. Host `invite_peer` again. Host player is `invited`; guest
   has no pending invitation. Renewal only covers `expired`/`cancelled`, not
   `declined`. Expected: a new reviewable invitation. Distinct from replaying
   the original invite message.

3. **Retry Connection treats the session ROM already in the foreground as a replace conflict.**
   Three-player start; mallory confirms; guest never confirms; launch deadline
   fails the guest (`DEGRADED`). Set the guest foreground game to this
   session's ROM. Host retry delivers a new offer. Guest `confirm_launch`
   without `replace_running_game` raises `game_running` and would require
   killing that same game. Spec 11 warns before replacing a *different* game.
   Control: a different ROM still requires replace approval. Control:
   housekeeping still recovers a confirmed launch whose `launch_result` was
   dropped.

4. **A FAILED start occupies the only Host Game slot until it is ended.**
   Host Start fails with "EmulationStation temporarily unavailable" (`FAILED`).
   `create_session` raises `too_many_sessions`. `store.active()` includes
   `FAILED` (only `COMPLETED` is terminal) and
   `max_simultaneous_sessions` defaults to 1. Spec 14 limits live sessions;
   spec 17 forbids abandoned failed sessions from holding resources. Control:
   Retry of that failed session still recovers when no new session replaced it.

5. **PPSSPP without a built-in ad hoc server is auto-selected as the host mechanism.**
   Installed PPSSPP `ppsspp.ini` has `EnableWlan` and `ProAdhocServer` but no
   `EnableAdhocServer`. `create_session` selects `PPSSPP_ADHOC`. Detection
   records `builtin_server=False` ("can join but not host") as a reason, not a
   host-role blocker, so mechanism selection still treats it as usable.
   `validate_configuration` would refuse Start later. Spec 8: never silently
   select a mechanism the installed build cannot provide. Control: Dolphin
   nogui-only is refused (`mechanism_unusable` / not auto-selected).

## Passing controls in this suite

- Accept after leave without a new invite does not rejoin.
- Housekeeping recovers a confirmed launch whose result POST was dropped.
- Retry still requires approval when a different game is in the foreground.
- A failed session can still be retried when the user has not started another.
- Dolphin nogui-only is not an automatic Netplay host.

## Validation

- New registered command: 10 collected, 5 passed, 5 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  128 collected, 123 passed, only the 5 new failures (43.491 seconds).
- Existing `adversarial-issue-240-stale-result-reconnect-uat` is retained
  unchanged and passed on this tree (the previously blocking stale
  `launch_result` / missed-offer / retry-budget / leave-degraded cases).
- Registry: prior `adversarial-issue-240-*` suites retained; new timeout 300
  seconds; origin `adversarial`; enabled; disruptive false.
- `git diff --check`: passed. Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery
  actions.

## Out of scope

None from this pass. No unrelated existing suite was used as a #240 blocker.
