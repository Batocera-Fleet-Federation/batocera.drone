---
name: drone-multiplayer
description: Use this when designing, reviewing, debugging, or modifying Drone Multiplayer — the top-level Multiplayer page (dashboard/games/lobby/settings), app/multiplayer/ (orchestrator, session state machine, peer protocol, emulator adapters for RetroArch/Dolphin/PPSSPP/Switch/LAN/streaming, mechanism selection, game matching, compatibility database, ConfigGuard temporary emulator settings, launcher, worker, clone reset), web/handlers_multiplayer.py, the /peer/multiplayer/* endpoints, web/static/js/multiplayer.js, or when adding support for another emulator.
---

# Drone Multiplayer

Decentralised multiplayer on the existing swarm. No hub, no account. Read
`docs/multiplayer.md` for the user-facing behaviour; this skill is the maintainer's map.

## Map

| Concern | Where |
|---|---|
| Vocabulary, states, transitions, validators | `multiplayer/models.py` (`TRANSITIONS`, `clean_*`, `normalize_title`, `config_fingerprint`) |
| Sessions, invitations, history, replay window | `multiplayer/store.py` (`SessionStore.mutate` enforces the machine + bumps `version`; `apply_replica` is guest-side, forward-only; `sanitize_session` rebuilds peer data) |
| Orchestrator | `orchestrator.py` = `core` + `sessions` (host create/invite/lobby) + `protocol` (inbound peer messages, guest accept/decline/leave) + `lifecycle` (start/launch/fail/end/tick/recover/results/diagnostics) + `library` (read side) |
| Adapters | `multiplayer/adapters/*` (`base.EmulatorAdapter`, registry in `__init__.py`) |
| Mechanism choice | `mechanism.py` (`evaluate_candidates` → blockers/warnings, `choose` → reason, forced is never substituted) |
| Matching / assessment | `matching.py` (`GameLibrary` over the ROM cache, `match_level`, `GameAssessor`, `browser_status`) |
| Compatibility | `compat_db.py` (evidence per tested config fingerprint; shared reports capped) |
| Temporary settings | `config_guard.py` (kv + ini, journal, atomic, verify, restore) |
| Network | `network.py` (route scope, latency via `/peer/health`, session ports, `RelayProvider` seam = none) |
| Launch / process | `launcher.py` (EmulationStation loopback API, `GameRuntime`, `BatoceraControl.exit_game`) |
| Probes | `environment.py` (`SystemProbe`; the only place that touches PATH/files/subprocess for detection) |
| Settings | `settings_store.py` |
| Optional dependency install (Sunshine) | `dependencies.py` (`SunshineInstaller`: release lookup → SHA-256 verify → `--appimage-extract` → Drone-owned dir + `DRONE_SUNSHINE` service; background job; `LibraryMixin.dependency_action` allowlists install/start/stop/uninstall) |
| HTTP | `web/handlers_multiplayer.py`; routes in `web/api_routes.py`; schemas in `web/openapi_spec.py::_multiplayer_paths` |
| UI | `web/static/js/multiplayer.js` (hash `#multiplayer[/games[/system]|/session/<id>|/settings]`), router branch + nav button in `drone.js`/`index.html` |
| Clone safety | `multiplayer/identity_reset.py`, called from `transfer/swarm_reset.py` |

## Protocol rules (do not weaken)

1. Peer endpoints use `_peer_request_authorized()` + `_peer_requester_device_id()`; `check_message` requires a paired
   caller, `source_drone_id == certificate identity`, a fresh `message_id`/`issued_at`, and treats replays as
   `duplicate`.
2. Host is the single authority; guests only *request* (`respond`). Snapshots move forward by `version`; host
   mutations go through `SessionStore.mutate`. Never write session state around it.
3. A guest never launches, and never has a running game closed, because a peer asked. `peer_launch` only stores a
   pending offer. Replacing a *different* foreground game needs `replace_running_game` on this machine. The session
   ROM already in the foreground is participation: confirm it without exiting that process. The host still asks
   before relaunching its own already-open ROM, because Netplay settings are applied by launching again.
4. The guest dials the address *it* already uses for the host (`NetworkService.host_address`), never one from a message,
   and writes per-game keys with **its own** ROM file name (`local.rom_name`).
5. Acceptance responses must match the reviewed invitation (`require_same_identity`) and confirm local guest membership.
   `adopt_replica` cannot overwrite another host's session. Completed sessions cannot resurrect; leaving voids offers.
6. The per-session credential never appears in views, audit, logs, or after the session ends.
7. State per peer ⇒ add it to `identity_reset.py`.

## Adapter contract

Implement detect → assess → validate → prepare → (configure_networking/controllers as **edit dicts**) → launch via the
shared launcher → health. Rules: evidence-based `_detect_features`; lower `effective_automation()` when evidence is
missing (RetroArch requires every `netplay.*` key in the installed `configgen` sources); raise `CapabilityError` for
unsupported operations; never return `VERIFIED_COMPATIBLE` (only the DB does, after a confirmed session for that exact
`config_fingerprint`); `content_match` = `exact` / `title` / `host_only`; `needs_same_lan` for broadcast-discovery
mechanisms (Tailscale carries no broadcast); `guest_launches=False` when guests follow steps instead (streaming).
Detection results are memoised 30 s (`adapter.refresh()` in tests after changing the fake machine).

## Honest status

Tests run against a **simulated** swarm (`tests/multiplayer_support.py`: `FakeProbe`, `FakeNetwork`, `FakeLauncher`,
`make_swarm`). Do not describe an emulator integration as working/verified without a real session; the documented
benchmarks (RetroArch between two real Batocera machines, then Dolphin) are still open. Keep `docs/multiplayer.md`'s
matrix and "Manual hardware validation" in sync with reality.

## Gotchas

* Name clash avoided on purpose: `peer_health()` (stored health checks) vs `peer_status()` (inbound endpoint).
* `FAILED` may only move to `STOPPING`/`CONFIGURING`; retry therefore does two mutations (reset, then `settle_lobby`).
  A failed host session stays retryable and still counts as active until the user ends it or hosts a different game.
  That new `create_session` ends the failed one first (`STOPPING` → `COMPLETED`) so it no longer holds the only slot.
  The same `client_request_id` still returns the failed session instead of replacing it.
* A fresh invite (`message_id` not a replay) reopens `expired`, `cancelled`, `declined`, and `accepted` after the
  guest has left (`ended` or left/removed/declined). Accepting the old invitation without that invite does not rejoin.
  Do not reopen an `accepted` invitation while the guest is still in the lobby.
* Lobby states are interchangeable via `settle_lobby`; `CREATED` is never re-entered.
* Live roster health is `settle_live` (leave/remove are not `DEGRADED`). Launch offers are waves (`local.launch_wave`); a `launch_result` success must carry the current wave before the deadline.
* Existing tests assert exact literals in `drone.js` (e.g. the `adminLinks` array) — add to such structures instead of editing the literal.
* Python 3.9 compatible (dev machines), stdlib only; `Optional[...]`/`typing` not `X | Y`.
* `notifications.LOCAL_ONLY_EVENT_TYPES` keeps `multiplayer_*` audit/bell rows out of SMTP digests and the 18 toggles.
* A running `create_server` in tests starts the (idempotent) worker; call `stop_multiplayer_worker()` in cleanup.

## Hardening and external dependencies

- `dependencies.py` is the **only** place Drone installs anything for multiplayer. Keep it fail-closed: official
  `LizardByte/Sunshine` HTTPS release, required `sha256:` digest from the release API, size cap, extract with the
  AppImage's own `--appimage-extract` (never run from the download dir), fixed argv, marker-checked service file,
  uninstall never touches a Sunshine/service Drone did not write and keeps Sunshine's own config/pairings. Endpoints
  are `GET /admin/multiplayer/dependencies` and `POST .../dependencies/sunshine/{install,start,stop,uninstall}`
  (same session + same-origin + JSON gate as other mutations; stop/uninstall refused while streaming is in use).
  Moonlight and libretro cores are **not** installed by Drone (no verified Batocera store API) — adapters give the
  Content Downloader hint instead.
- RetroArch core resolution order: Drone override → `batocera.conf` → Batocera `configgen-defaults.yml` (+`-arch`) →
  first installed candidate (`core_source` says which). Put blockers before notes in `reasons`; the selector shows
  `reasons[:2]` for UNSUPPORTED/INCOMPATIBLE.

- `GameLibrary.content_hash` computes a selected file's SHA-256, caches by file/stat identity, and refuses changes
  during hashing. RetroArch/Dolphin joining requires full equality; sampled fingerprints only locate candidates.
- `ConfigGuard` serialises journal/write/restore across all sessions and refuses overlapping key ownership.
  `watch_generated` journals RetroArch's generated password fields without writing configgen's profile; keep this
  alongside per-ROM batocera.conf restoration so generated profiles do not retain the join credential.
- Replay capacity fails closed with 429; never evict an unexpired ID to admit new messages.
- Launch workers and cleanup hold the session lock; queued workers recheck state/consent before touching resources.
  Host process ownership includes the detected PID so ending cannot kill a later game at the same ROM path.
- Server close joins its owned multiplayer housekeeping worker; each worker generation has its own stop event.
- PPSSPP must inspect configgen preservation before applying ini edits/launching. Uninspected or overriding builds
  remain manual. `host_capable` / `host_block` on the host assessment stop auto-selection (and a forced choice) when
  `ppsspp.ini` has no `EnableAdhocServer` key; a guest with that build can still join a host that has the key.
  `EnableAdhocServer = False` means the key exists. A Dolphin nogui binary alone cannot provide the GUI Netplay lobby.
- Sunshine owns pairing, input and streams. Drone cannot revoke its pairings, enforce its gamepad slots or stop its
  existing streams; the UI must say so. Drone's privacy disable only blocks new streaming invitations/launches.
- Unknown game capability needs the Advanced selection, not an automatic recommendation.
