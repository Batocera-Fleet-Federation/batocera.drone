# Independent adversarial review of #240

Only tests and suite registration changed. No product fixes, older test revisions,
security-owned edits, VERSION changes, commits or delivery actions were made.

## Executed checks

All seven registered `adversarial-issue-240-*` argv commands were run in the
foreground: 48 tests collected, 43 passed, 5 failed. The five older suites passed
unchanged (42 tests). The six new tests contain one passing retry-limit control
and five failing acceptance cases. `git diff --check` passed. Existing suite
objects and top-level manifest metadata were compared against HEAD and retained
exactly. Both new suites are enabled, non-disruptive, origin `adversarial`, with
explicit unittest discovery argv and 300-second timeouts.

Fixtures use temporary local files/SQLite plus the established fake swarm,
installed-emulator probe and launcher. Gameplay addresses are fixed in these
fixtures. Results do not establish real Batocera or emulator connectivity.

## In-scope findings

Run `python3 -m unittest discover -s tests/adversarial -p test_issue_240_retry_recovery_uat.py`:

1. **Retry cannot recover a guest after either host or guest launch failure.**
   Create, accept, ready, inject a temporary launch rejection, start, remove the
   rejection, Retry, accept again locally, ready again and start. The guest remains
   `local.ended=true`; the host's new launch offer cannot be used. Both failure
   origins have separate tests. `guest_cleanup` closes local participation on
   FAILED, but `after_replica_update` does not reopen it on retry and the already
   accepted invitation does not renew it. Sections 3 and 17 require usable recovery,
   not just a host state reset. No game should launch until local confirmation.
2. **Reconnection never restores healthy session state.** After a running guest
   misses two probes, the host becomes DEGRADED. Bringing that same guest back
   restores its player status to running while the session remains DEGRADED with
   every player running. `_probe_player` recovers player status without settling
   session health. Sections 3.1 and 17 require reconnect handling and accurate state.
3. **A guest error after ES starts the game leaves an orphan process.** The local
   launcher accepts and starts the selected game, then process confirmation raises
   an error. Configuration is restored, but the game Drone just started stays active.
   `run_guest_launch`'s exception path does not stop an owned process. Sections 3,
   11 and 17 require cleanup after partial launch failures. This test targets local
   failure of an owned launch, not remote permission to close arbitrary guest games.

Run `python3 -m unittest discover -s tests/adversarial -p test_issue_240_content_evidence_uat.py`:

4. **Successful-session evidence transfers to different full ROM content.** Play
   and locally confirm revision A. Change one byte in both peers' ROMs outside the
   repository's real sample-fp-v1 windows, preserving filename, size and actual
   sampled fingerprint. A new session computes a different full SHA-256, but its
   assessment still says VERIFIED_COMPATIBLE. `validation_fingerprint` binds evidence
   to sampled metadata instead of full selected content. Sections 7 and 13 require
   evidence specific to the tested content/configuration. The unchanged-content
   control first asserts that genuine local success does verify revision A.

Blocking suites: `adversarial-issue-240-retry-recovery-uat` and
`adversarial-issue-240-content-evidence-uat`.

## Dispute adjudication

Accepted: the two disputed suites are outside #240 and must not block this issue.
Their executable assertions, manifest metadata and enabled status remain unchanged
for scheduled runs. No earlier test is weakened or retired.

The exact discovery commands for `test_issue_212_protected_harness_followups.py`
and `test_issue_220_out_of_scope_directory_db_path.py` were run on this branch and
an isolated temporary extraction of `git archive 7194ace`. They fail identically:

- #212: 2 tests, 1 failure listing 7 missing LAN-worker teardown operations in four
  older protected harness files. This is test infrastructure maintenance unrelated
  to Multiplayer; file a separate issue labelled test-infrastructure and assign to
  the harness maintainer. Suite: `adversarial-issue-212-protected-harness-followups`.
- #220: 2 tests, 5 assertion failures: directory paths `/`, `..`, and a temporary
  directory as Path/string do not raise ValueError before SQLite; `Path('')` passed
  to `database_path_for_legacy_file` derives a parent database instead of rejecting
  the input. SQLite/chmod are mocked by the retained suite. This predates #240 and
  concerns storage path validation; file a separate issue labelled storage and
  assign to the storage maintainer. Suite:
  `adversarial-issue-220-out-of-scope-directory-db-path`.
