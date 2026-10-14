# Issue #240 session-truth acceptance findings

Scope: issue sections 2.2 and 2.4 (invitation lifetime, ready vs untested),
3 and 3.1 (orchestration, untrusted peer updates, reconnect), 7 and 8
(matching honesty and mechanism selection), 11 (the active game is the
session game), and 17 (recovery must not invent a running session).

No dispute was presented. Earlier adversarial tests and their expectations
are unchanged. `tests/adversarial/security/` and security-origin suites are
untouched.

Suite: `adversarial-issue-240-session-truth-uat`

```sh
python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_session_truth_uat.py'
```

Ten tests, simulated swarm, fixed UTC clock, fixed gameplay address.
Temporary directories hold SQLite, ROMs and config. No emulator binaries
and no external network. Four controls pass (honest confirm still runs,
the same ROM still recovers, a known preference still applies, and an
UNKNOWN-only candidate stays unselected until forced). Six fail.

## 1. The host admits a peer after the invitation lifetime

Test: `test_host_rejects_accept_after_the_invitation_lifetime`.
Create a session, advance the clock by `invitation_ttl_minutes + 1` (still
inside the separate session timeout), and send a fresh accept on the peer
channel. `peer_respond` joins the guest. The host checks `session.expires_at`
only. It does not enforce the invitation TTL it advertised in
`invite_expires_at`, and `tick` does not retire the invited player.

Expected: the accept is refused, the player does not become joined, and a
host tick no longer lists that peer as invited. No game launches. An
explicit later `invite_peer` remains the renewal path covered by the
existing participation suite. A declined invitation is not reopened.

## 2. A launch-success report is trusted with no session game

Tests: `test_launch_report_without_the_session_game_does_not_complete` and
`test_launch_report_for_a_different_game_does_not_complete`.
Host starts RetroArch orchestration and offers a launch. The guest has not
confirmed and has not launched. The guest then reports `launch_result`
`ok=true`, first with no foreground game and then with a different ROM.
`_respond_launch_result` marks the player running. `evaluate_launch` moves
the session to RUNNING. The guest's launcher list stays empty.

Expected: success counts only when that machine is running this session's
ROM. Another title must not satisfy the check, and this machine must not
close that other title. The passing control
`test_confirmed_launch_of_the_session_game_still_runs` still reaches RUNNING
after a local confirm.

## 3. Reconnect treats any foreground game as the session

Test: `test_other_foreground_game_is_not_a_recovered_participant`.
Run a session, degrade the guest, then point the guest's active game at a
different ROM while its replica remains. `peer_status` sets `game_running`
from `active_game() is not None`. `_participation_gap` treats that flag as
the session game, and the host restores RUNNING.

Expected: keep the player unreachable or failed. Do not relaunch and do not
stop the other game. The passing control
`test_same_session_game_still_recovers_without_relaunch` still restores
RUNNING when the session ROM is the foreground game.

## 4. The guest stays in the session after its game is replaced

Test: `test_replaced_session_game_makes_the_guest_leave_without_killing_it`.
After RUNNING, replace the guest's foreground ROM and tick twice. The guest
only leaves when `active_game()` is missing, so a different ROM keeps
`game_misses` at zero and the host still shows the player running.

Expected: leave the session because the session game is gone, tell the host,
and leave the replacement game running. The host already uses ROM identity
for its own process (`_owns_host_game`); the guest path and the status
probe do not.

## 5. A saved preference auto-selects UNKNOWN capability

Test: `test_preference_does_not_auto_select_unknown_capability`.
`choose()` drops UNKNOWN from the automatic pool, then returns immediately
when the preferred mechanism is usable, without applying that drop.
Emulated LAN classified UNKNOWN is selected over remote streaming classified
LIKELY_COMPATIBLE. Forced/advanced selection of the UNKNOWN mechanism still
works, and a preference for a known usable mechanism still works.

Expected: preference may choose only a mechanism whose capability is known.
UNKNOWN stays on the advanced path, including when another usable mechanism
exists. Game-specific support outranks a blanket preference.

## Validation

- New suite command collects 10 tests: 4 passed, 6 failed.
- No product fixes, dependency additions, commits, or VERSION edits.
- Simulated results do not establish real Batocera or RetroArch connectivity.
