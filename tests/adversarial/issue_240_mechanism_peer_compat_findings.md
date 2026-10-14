# Issue #240 peer-capability mechanism selection findings

Expected behavior was derived from issue sections 5.1–5.3 (RetroArch/Dolphin
version comparison, PPSSPP host capability), 6 (streaming as fallback when
native is unsuitable), 8 (select from installed capabilities and participating
peers; forced is honoured or refused; never silently select an unsupported
mechanism), and 9 (Tailscale does not carry LAN broadcast). No earlier test
expectations were revised. No product code, security-owned tests, VERSION, or
existing suites were changed.

Suite: `adversarial-issue-240-mechanism-peer-compat-uat`
Command: `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_mechanism_peer_compat_uat.py'`

Fixtures use the simulated swarm, real temporary configuration files and
SQLite. This does not validate real RetroArch, Dolphin, PPSSPP, Switch, or
Sunshine gameplay.

## In-scope blocking findings

Mechanism selection (`evaluate_candidates` / `choose` / `create_session` /
`game_detail`) treats native Netplay as usable from each machine's own
LIKELY_COMPATIBLE assessment. Peer `emulator.version` and `emulator.core_version`
are already returned by `/match` and kept by `_trust_peer_assessment`, but they
are not compared. Join-time `validate_configuration` then refuses the guest.
When Sunshine/Moonlight is installed and running, spec 8 requires streaming
instead of a native session the invited peer cannot join. Existing
`adversarial-issue-240-deadline-version-uat` cases (no streaming) still require
create-then-`cannot_join`; this suite does not change that.

1. **Host Game selects RetroArch Netplay when the invited peer's RetroArch
   major.minor differs and streaming is available.**
   Host 1.19.1, guest 1.21.0, Sunshine running. `create_session` returns
   `RETROARCH_NETPLAY` ("remote streaming was not needed"). Accept would then
   fail `cannot_join` ("RetroArch versions are incompatible"). Expected:
   `REMOTE_STREAMING`, and the guest can join the lobby.

2. **Host Game selects RetroArch Netplay when the invited peer's core version
   differs and streaming is available.**
   Host snes9x 1.62.3, guest snes9x 1.60.0. Same doomed native selection.
   Expected: streaming.

3. **Advanced/forced RetroArch Netplay is honoured for a peer who cannot join
   it.**
   Same 1.19.1 vs 1.21.0 pair. Forced `RETROARCH_NETPLAY` creates a session
   instead of `mechanism_unusable`. Forced must be refused, not silently
   substituted and not accepted as a doomed session.

4. **View Compatible Games recommends RetroArch Netplay for an online peer
   whose RetroArch version is incompatible.**
   Both machines have `snes.core=snes9x`. Guest RetroArch is 1.21.0. Host
   `game_detail` recommendation is `RETROARCH_NETPLAY` with the native
   candidate still `usable`. Expected: streaming, native blocked.

5. **Host Game selects Dolphin Netplay when Dolphin versions differ and
   streaming is available.**
   Host 2506a, guest 2509, Sunshine running. `create_session` returns
   `DOLPHIN_NETPLAY` ("remote streaming was not needed"). Guest
   `validate_configuration` reports incompatible Dolphin versions. Expected:
   streaming.

## Passing controls in this suite

- Without streaming, incompatible RetroArch versions still create a native
  session and block join (`cannot_join`). Does not contradict
  `adversarial-issue-240-deadline-version-uat`.
- Switch LDN over Tailscale falls back to streaming; forced `SWITCH_LDN` is
  `mechanism_unusable`. Shows the engine already falls back when a native
  mechanism is actually marked unusable (broadcast / `needs_same_lan`).
- A PPSSPP build without `EnableAdhocServer` is not auto-selected; with
  Sunshine it selects streaming. Forced `PPSSPP_ADHOC` on that host is
  `mechanism_unusable`.
- A join-only PPSSPP guest does not block a host that can host.
- Invite Peer after the host removes a lobby member delivers a new pending
  invitation; accept does not launch.
- Confirming a launch after the host removed that player during STARTING does
  not start a game.

## Validation

- New registered command: 12 collected, 7 passed, 5 assertion failures above.
- `python3 -m unittest discover -s tests/adversarial -p 'test_issue_240_*.py'`:
  140 collected, 135 passed, only the 5 new failures (46.478 seconds).
- Existing `adversarial-issue-240-*` suites retained enabled and unchanged,
  including `adversarial-issue-240-invite-retry-capacity-uat` (now passing on
  this tree after the round-13 host-capability/re-invite fix) and
  `adversarial-issue-240-deadline-version-uat` (create-then-cannot_join without
  streaming still holds).
- Registry: timeout 300 seconds; origin `adversarial`; enabled; disruptive false.
- Branch remains `ai/claude/issue-240`.
- No product fixes, security-owned edits, VERSION changes, commits, or delivery
  actions.
