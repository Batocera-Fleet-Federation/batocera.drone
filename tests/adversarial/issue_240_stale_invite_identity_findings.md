# Issue #240 stale invitation identity and host-core recommendation findings

Expected behavior was derived from issue sections 2.1–2.4 (Host Game, Invite
Players, invitation contents, explicit accept), 3.1 (conflicting operations,
stale session updates), 5.1 (RetroArch core comparison), 6 (streaming when
native is unsuitable), 8 (participating peers; never silently select an
unsupported mechanism), and 17 (abandoned sessions must not hold the lobby).
No earlier test expectations were revised. No product code, security-owned
tests, VERSION, or existing suites were changed.

Suite: `adversarial-issue-240-stale-invite-identity-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_stale_invite_identity_uat.py'`

Fixtures use the simulated swarm, real temporary configuration files and
SQLite. This does not validate real RetroArch or Sunshine gameplay.

## In-scope blocking findings

1. **Invite Players after Host Game can change the mechanism without replacing
   the first guest's pending invitation.**
   Host a RetroArch lobby with no peers, Invite the compatible guest
   (`RETROARCH_NETPLAY`), then Invite an incompatible RetroArch peer while the
   first invitation is still pending. The host switches to `REMOTE_STREAMING`.
   The first guest's pending invitation still names `RETROARCH_NETPLAY`. Accept
   raises `session_identity_changed`. Spec 2.4: the invitation the user reviews
   is the session they join. Spec 8: adding a participating peer may change the
   mechanism; pending invitees must be asked again. Control: Invite the
   incompatible peer first (create-without-peers then one invite) delivers
   streaming and that guest can join. Control: inviting the incompatible peer
   after the first guest has already joined is `mechanism_change`.

2. **Accept of that stale invitation joins the guest on the host only.**
   `validate_for_join` uses the old native snapshot and passes. `/respond`
   accept runs on the host and marks the guest `joined`. `require_same_identity`
   then fails on the guest, so no replica is adopted. Host player status is
   `joined`; guest `store.get(session)` is `None`. Spec 3.1: membership is not
   a one-sided host mutation. Distinct from `cannot_join`, which refuses before
   `/respond` and leaves the host player `invited`.

3. **The host publish after that ghost join cancels the guest invitation, and
   Invite Peer is a no-op.**
   `list_invitations()` is empty after the failed accept. Host still has the
   guest as `joined`, so a later `invite_peer` returns success without
   delivering anything. The guest cannot ready (`session_not_found`). Start
   raises `players_not_ready` waiting for that ghost. Spec 2.3/17: the lobby
   must not wait on a machine that is not in the session.

4. **View Compatible Games compares the peer's default core, not the host's
   session core.**
   Host `snes.core=snes9x` (1.62.3). Guest `snes.core=bsnes` (115) but also has
   `snes9x_libretro.so` installed. `game_detail` marks `RETROARCH_NETPLAY`
   unusable ("core versions differ") and recommends streaming. `create_session`
   with the host core selects native and the guest can join. Spec 5.1/8: core
   comparison is the core the host will run. The Host Game dialog lists only
   `usable` mechanisms under Advanced, so native is hidden even though Host
   Game would pick it.

## Passing controls in this suite

- Create without peers, then invite an incompatible guest: mechanism switches
  to streaming and that guest accepts.
- Invite an incompatible peer after someone has joined: `mechanism_change`,
  native session unchanged.
- Create with the incompatible peer listed up front: streaming, guest joins.
- Create with the host core still selects native when the guest has that core
  installed, even if their default core is different.

## Validation

- New registered command: 8 collected, 3 passed, 5 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  148 collected, 143 passed, only the 5 new failures (46.449 seconds).
- Existing `adversarial-issue-240-mechanism-peer-compat-uat` is retained
  unchanged and passed on this tree (the previously blocking peer-version
  selection cases). All other prior `adversarial-issue-240-*` suites retained
  enabled and unchanged.
- Registry: 20 `adversarial-issue-240-*` suites; prior 19 retained; new timeout
  300 seconds; origin `adversarial`; enabled; disruptive false.
- Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery
  actions.

## Out of scope

None from this pass. No unrelated existing suite was used as a #240 blocker.

## Adjudication note (epoch 6)
Four tests in `test_issue_240_stale_invite_identity_uat.py` invited two peers into a lobby whose live mechanism
is REMOTE_STREAMING (three occupied seats vs. the published two-seat capacity). Spec 2.3 and the capacity suite
(`test_issue_240_capacity_config_fidelity_uat.py`) correctly forbid that, so the setup was invalid, not the product.
The tests were rewritten to reach the same stale-invitation conditions with host + one guest (the guest misses a
withdrawal, the host upgrades RetroArch, the refreshed streaming invitation is not delivered). The identity
invariants (no host-only join, failed accept keeps the invitation, re-invite delivers the live mechanism, host not
stuck) are unchanged. `tests/test_multiplayer_invitation_identity.py` is not an adversarial file and still uses the
over-capacity setup; the worker must rework it.
