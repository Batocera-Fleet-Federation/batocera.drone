# Issue #240 — capacity after mechanism switch, and lossless configuration restore

Suite: `adversarial-issue-240-capacity-config-fidelity-uat`
(`tests/adversarial/test_issue_240_capacity_config_fidelity_uat.py`). Simulated peers; real temp files/SQLite.

## 1. Mechanism switch overfills the lobby (spec 2.3, 6, 8)
`SessionMixin.invite_peer` only checks `occupied >= max_players` before planning. When the new peer forces a
fallback from RetroArch Netplay (4 players) to REMOTE_STREAMING (host + 1 guest), `add()` shrinks
`max_players` to 2 without checking that already-invited players still fit; nothing in `_respond_accept`
re-checks capacity. Reproduction: host, `amy`, `bob` invited to a 4-player native lobby, then invite `cat`
(incompatible RetroArch). Result: `REMOTE_STREAMING`, `max_players=2`, all of host/amy/bob/cat occupy
seats; amy and bob both accept; `bob` holds slot 3. Fix direction: refuse the invite (like `mechanism_change`)
when the new capacity is below the number of occupied seats including the new peer.

## 2. `ConfigGuard` rewrites lines it does not own (spec 10)
`_kv_*`/`_ini_*` use `str.splitlines()`, which also splits on U+2028, U+2029, `\x0b`, `\x0c`, `\x1c-\x1e`,
`\x85`, and normalises `\r\n` and a missing final newline. Applying temporary keys then restoring turned
`system.hostname=BA\x85TOCERA` into two lines, split a per-ROM key containing U+2028, and split an INI value,
both *while applied* and after restore. Fix direction: split on `"\n"` only (keepends) and write back the
original terminators.
