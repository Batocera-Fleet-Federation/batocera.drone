# Issue #240 lobby, dial, classification, and cleanup findings

Only `tests/adversarial/test_issue_240_lobby_dial_cleanup_uat.py` and its
`.swarm/tests.json` registration were added. Earlier adversarial suites, including
the protected findings notes, were not revised. No product code was changed.

Suite: `adversarial-issue-240-lobby-dial-cleanup-uat`

```
python3 -m unittest discover -s tests/adversarial -p test_issue_240_lobby_dial_cleanup_uat.py
```

10 tests collected. 4 controls passed. 6 assertions failed. The simulated swarm
does not start emulators and does not establish real Batocera connectivity.

## Failures against the issue

1. **Assigning an unslotted invitee onto the host's slot clears the host.**
   `assign_slot` swaps by writing the assignee's current slot onto the holder.
   An invited peer's slot is `None`, so the host is left with no slot while the
   guest takes slot 1. A free slot (guest to 2) keeps both players placed.
   Sections 2.3 and 12 require every active player to keep a distinct slot inside
   the session capacity. `sessions.py` `assign_slot`.

2. **A known LAN address is skipped when an earlier `.local` name is also a candidate.**
   Control-plane order lists the hostname before the literal IP. Both are scope
   `lan`, so gameplay dial uses the name first. If `arcade.local` resolves to
   `8.8.8.8`, the guest Netplay setting is that public address. If DNS fails, the
   setting is the unresolved name. A peer that has only `192.168.8.20` dials that
   address and does not call DNS. Sections 9 and 16: prefer the direct LAN address
   already known for the peer, and do not publish emulator gameplay on a public
   address by default. `network.py` `host_address`.

3. **A paired peer's failure count reclassifies this machine's own assessment.**
   A RetroArch row with core `snes9x` is `LIKELY_COMPATIBLE` before import.
   Importing that exact fingerprint from a paired peer with 3 failures and 0
   successes stores `source=peer:…` and `evaluate` then returns `INCOMPATIBLE`
   with basis `failed-sessions`, so mechanism selection drops Netplay. A failure
   recorded on this machine for the same fingerprint still correctly becomes
   `INCOMPATIBLE`. Sections 7, 13 and 16: peer metadata stays attributed and
   untrusted; it must not become local proof, including a blocking failure class.
   `compat_db.py` `evaluate`.

4. **A host launch that starts the session ROM and then raises leaves the process running.**
   The launcher sets the session ROM active (pid 4242) and then raises
   `es_rejected`. The session becomes `FAILED`, `batocera.conf` is restored, and
   the guest does not launch. `host_launched` is still unset, so
   `cleanup_host_resources` does not call `exit_game`. The ROM stays active.
   Sections 3, 11 and 17 require cleanup of a process this session started.
   `lifecycle.py` `run_start` / `cleanup_host_resources`.

5. **A generated Netplay password that appears after a missed restore is kept.**
   `watch_generated` journals `retroarchcustom.cfg` before the file exists.
   Restoring while the file is absent counts the keys as left alone and deletes
   the journal. Writing the journaled `netplay_password` /
   `netplay_spectate_password` lines afterwards leaves the session secret in the
   profile; a second restore does nothing. When the file already contains those
   lines at the first restore, the secret is removed and `batocera.conf` is put
   back. Sections 10, 16 and 17: temporary credentials must be removed, including
   after an interrupted restore. `config_guard.py` `_restore`.

## Suites retained

The ten earlier `adversarial-issue-240-*` suites were run unchanged and passed
(75 tests). `adversarial-issue-212-protected-harness-followups` and
`adversarial-issue-220-out-of-scope-directory-db-path` still fail for reasons
outside #240. They stay enabled. They are not #240 blockers.
