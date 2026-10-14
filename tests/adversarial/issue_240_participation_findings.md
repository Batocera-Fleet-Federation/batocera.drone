# Issue #240 additional participation acceptance findings

Scope: issue sections 2.4 (invitations), 3.1 (disconnect/reconnect/restart),
17 (recovery and meaningful errors), and 19 (integration tests).
No dispute was presented. Earlier tests and their expectations are unchanged;
`tests/adversarial/security/` and security-origin suite registrations are untouched.

Suite: `adversarial-issue-240-participation-uat`

Reproduce from the repository root:

```sh
python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_participation_uat.py'
```

These five tests use the existing simulated swarm. Temporary directories isolate
SQLite, ROMs and config files; a fixed UTC clock and fixed gameplay address avoid
time and interpreter hash-seed dependencies. Network failure is an explicit hub
flag; there are no real emulator processes or external services.

## 1. Ending a session leaves an invitation to that session pending

Test: `test_ending_host_withdraws_delivered_pending_invitation`.
Create a host session and deliver its invitation; end the host session before the
guest accepts; run guest housekeeping. `list_invitations()` still offers the ended
session as pending. The host knows the session is over and the guest is reachable.
The invitation must be withdrawn rather than leave a dead Join Game action.

Evidence: `SessionMixin.publish` skips players without `joined_at`, including
pending invitees. The guest has cancellation handling in
`ProtocolMixin._maybe_cancel_invitation`, but the terminal snapshot never arrives.

## 2. Explicit renewal reports success but cannot renew an expired invitation

Test: `test_explicit_renewal_of_expired_invitation_can_be_accepted`.
Expire a delivered invitation, let guest housekeeping classify it as expired,
then call the host's `invite_peer` for the same open session. The host returns
without delivery error but the receiving user has no pending invitation and
cannot accept it. Expected: a fresh reviewable invitation (still no auto-launch),
or an actionable refusal instead of apparent successful delivery.

Evidence: `_deliver_invitation` sends a fresh message, while `peer_invite` treats
any existing non-pending invitation as a duplicate without examining renewal.
The test requires renewal because the existing public operation accepts it as
successful. This is distinct from replaying a message or reversing a user's
explicit decline. No earlier declined-invitation expectation was revised.

## 3. Online Drone is mistaken for a recovered running participant

Tests: `test_online_guest_that_lost_replica_is_not_recovered_as_running` and
`test_online_guest_whose_game_exited_is_not_recovered_as_running`.
Start native RetroArch orchestration on both simulated machines; disconnect the
guest until the host reports DEGRADED. Reconnect the guest after either losing its
replica and game, or only losing its game. The real peer status response reports
`session: null` and/or `game_running: false`. The host nevertheless marks the player
running and restores RUNNING session status. A process answering the coordination
API is insufficient evidence that an emulator session resumed. Keep the player
failed/unreachable or require a recovery action; do not invent successful gameplay.

Evidence: `LifecycleMixin._probe_player` discards the returned status body and
uses HTTP success alone to recover the player. The positive control
`test_reachable_guest_with_live_game_does_recover_without_relaunch` passes,
confirming healthy reconnection remains possible without launching again.

## Validation

- Existing issue #240 adversarial suites: 48 tests passed before adding this suite.
- Product Multiplayer tests: 218 collected, 215 passed, 3 skipped (unavailable emulator binaries).
- New suite: 5 tests collected; 1 passed, 4 assertion failures (three findings).
- No product fixes, dependency additions, commits, or VERSION edits.
- No newly observed out-of-scope finding is used as a blocker.
- Simulated results do not establish real Batocera/RetroArch connectivity; the
  issue's two-machine hardware acceptance benchmark remains outstanding.
