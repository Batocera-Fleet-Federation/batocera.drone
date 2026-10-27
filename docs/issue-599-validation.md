# Issue 599 validation and adjudication evidence

Protected tests and `.swarm/tests.json` were left untouched. Checks ran as foreground subprocesses. Zero collected tests are not passes.

Functional checks: VPN (144), SMTP (80), tailnet sharing (27), transport (37), import modes (8), peer response size caps (4), new verified-connection transport tests (5): 305 passed. Mixed-case binary-download wire checks: 3 passed.

Live #599 checks: plaintext preservation (4), pinned HTTPS revocation (2), local peer removal (1), sibling pollers and wire boundaries (7) passed. The boundary suite passed 8 of 10; its two synthetic route-stamp trust assertions are disputed below.

SWARM_SECURITY_DISPUTE: adversarial-security-issue-279-rejects-multi-certificate-pem: collected zero tests; `tests/adversarial/security/test_issue_279_rejects_multi_certificate_pem.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-279-rejects-non-x509-pem: collected zero tests; `tests/adversarial/security/test_issue_279_rejects_non_x509_pem.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-191-discovery-infinite-port: collected zero tests; `tests/adversarial/security/test_issue_191_discovery_infinite_port.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-191-discovery-packet-type: collected zero tests; `tests/adversarial/security/test_issue_191_discovery_packet_type.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-155-hardlinked-lock-chown: collected zero tests; `tests/adversarial/security/test_issue_155_hardlinked_lock_chown.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-160-reprovisioning-boundaries: collected zero tests; `tests/adversarial/security/test_issue_160_reprovisioning_boundaries.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-163-clone-auth-revocation: collected zero tests; `tests/adversarial/security/test_issue_163_clone_auth_revocation.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-213-pair-by-address-scheme-downgrade: collected zero tests; `tests/adversarial/security/test_issue_213_pair_by_address_scheme_downgrade.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-240-launch-offer-binding: collected zero tests; `tests/adversarial/security/test_issue_240_launch_offer_binding.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-240-execution-revocation: collected zero tests; `tests/adversarial/security/test_issue_240_execution_revocation.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-246-tls-pin-scheme-rewrite: collected zero tests; `tests/adversarial/security/test_issue_246_tls_pin_scheme_rewrite.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-332-lan-tier-scheme-casing: collected zero tests; `tests/adversarial/security/test_issue_332_lan_tier_scheme_casing.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-security-issue-240-sunshine-pidfile: collected zero tests; `tests/adversarial/security/test_issue_240_sunshine_pidfile_security.py` is absent in this checkout. This is missing protected validation input, not evidence of a product vulnerability. Neither the protected tests nor manifest may be changed by this implementer.
SWARM_SECURITY_DISPUTE: adversarial-issue-586-real-check-revocation-recovery-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_transient_peer_errors_never_revoke_but_a_404_does (test_issue_586_real_check_revocation_recovery_uat.Issue586RealCheckRevocationRecoveryUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-587-surrogate-recovery-revocation: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_surrogate_log_failure_is_followed_by_real_404_credential_revocation (test_issue_587_surrogate_failure_then_real_revocation_uat.Issue587RecoveryUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-587-poller-tick-survival-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_broken_stderr_during_real_check_error_report_is_survived (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_closed_stderr_on_failed_tick_does_not_stop_later_revocation (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_disconnect_failure_on_revoking_tick_wipes_without_retrying_disconnect (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_long_run_of_unexpected_failures_does_not_retire_poller (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_non_string_error_during_real_check_is_survived (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_repeated_hostile_failures_then_real_revocation_still_happens (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`
- `test_surrogate_roster_failure_inside_real_check_is_survived (test_issue_587_poller_tick_survival_uat.Issue587PollerTickSurvivalUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-587-sibling-poller-tick-survival-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_broken_stderr_during_real_check_error_report_is_survived (test_issue_587_sibling_poller_tick_survival_uat.SmtpSharingPollerTickSurvivalUAT)`
- `test_closed_stderr_on_failed_tick_does_not_stop_later_revocation (test_issue_587_sibling_poller_tick_survival_uat.SmtpSharingPollerTickSurvivalUAT)`
- `test_repeated_hostile_failures_then_real_revocation_still_happens (test_issue_587_sibling_poller_tick_survival_uat.SmtpSharingPollerTickSurvivalUAT)`
- `test_surrogate_roster_failure_inside_real_check_is_survived (test_issue_587_sibling_poller_tick_survival_uat.SmtpSharingPollerTickSurvivalUAT)`
- `test_broken_stderr_during_real_check_error_report_is_survived (test_issue_587_sibling_poller_tick_survival_uat.TailnetSharingPollerTickSurvivalUAT)`
- `test_closed_stderr_on_failed_tick_does_not_stop_later_revocation (test_issue_587_sibling_poller_tick_survival_uat.TailnetSharingPollerTickSurvivalUAT)`
- `test_repeated_hostile_failures_then_real_revocation_still_happens (test_issue_587_sibling_poller_tick_survival_uat.TailnetSharingPollerTickSurvivalUAT)`
- `test_surrogate_roster_failure_inside_real_check_is_survived (test_issue_587_sibling_poller_tick_survival_uat.TailnetSharingPollerTickSurvivalUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-586-out-of-scope-poller-capture-race-uat: This is outside #599: the test mocks check_sharing_revocation entirely and asserts poller generation capture timing. app/device/vpn_manager.py run_sharing_revocation_poller captures poller_generation on entry; this patch changes neither generation capture nor retirement.

Affected assertions:

- `test_retirement_before_thread_capture_stops_the_poller_before_any_check (test_issue_586_out_of_scope_poller_capture_race_uat.SharingPollerCaptureRaceOutOfScopeUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-597-disconnect-failure-wipe-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_reports_revocation_as_true_when_wipe_happened (test_issue_597_disconnect_failure_wipe_uat.DisconnectFailureWipeUAT) (peer='peer-off')`
- `test_wipe_and_revocation_record_survive_disconnect_error (test_issue_597_disconnect_failure_wipe_uat.DisconnectFailureWipeUAT) (peer='peer-off')`

SWARM_SECURITY_DISPUTE: adversarial-issue-597-out-of-scope-failed-disconnect-not-retried: This is outside #599: the test uses a locally missing peer, bypassing HTTP evidence entirely. app/device/vpn_manager.py _revoke_credentials_from_peer returns early after has_credentials becomes false, so retrying a failed disconnect is a separate existing lifecycle requirement.

Affected assertions:

- `test_failed_disconnect_is_retried_on_a_later_tick_while_peer_gone (test_issue_597_out_of_scope_failed_disconnect_not_retried_uat.OutOfScopeFailedDisconnectNotRetriedUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-597-fresh-disconnect-finally-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_real_disconnect_failure_still_wipes_on_peer_off (test_issue_597_fresh_disconnect_finally_uat.Issue597DisconnectFinallyUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-598-stale-revocation-reimport-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_same_source_404_still_wipes_credentials_refreshed_during_the_call (test_issue_598_stale_revocation_reimport_uat.Issue598StaleRevocationReimportUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-598-revocation-atomicity-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_failed_replacement_import_does_not_block_genuine_404_wipe (test_issue_598_revocation_atomicity_uat.Issue598RevocationAtomicityUAT)`
- `test_later_404_from_new_source_still_revokes_after_stale_skip (test_issue_598_revocation_atomicity_uat.Issue598RevocationAtomicityUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-598-provenance-identity-boundaries-uat: These fixtures replace _peer_get_json_for_peer with bare HTTPError(https URL, 404, ...) exceptions and supply no verified-channel evidence. An https filename is not proof of authenticated revocation. app/transfer/peer_connectivity.py peer_http_error_is_authenticated must reject these exceptions under #599; the live pinned HTTPS 404 UAT verifies genuine revocation still works. This is a fixture contract conflict, not a reason to trust unauthenticated 404s.

Affected assertions:

- `test_same_source_credential_refresh_during_404_is_still_revoked (test_issue_598_provenance_identity_boundaries_uat.Issue598ProvenanceIdentityBoundariesUAT)`
- `test_second_overlapping_404_after_wipe_does_not_disconnect_again (test_issue_598_provenance_identity_boundaries_uat.Issue598ProvenanceIdentityBoundariesUAT)`

SWARM_SECURITY_DISPUTE: adversarial-issue-599-status-redirect-and-stamp-boundaries-uat: The fixtures assign only error.peer_route and expect authentication/revocation. Route strings cannot prove a TLS handshake, cached pin validation, or the final response channel. app/transfer/peer_connectivity.py _peer_get_json now stamps evidence only on the verified direct TLS connection; peer_http_error_is_authenticated rejects bare route stamps. The real pinned TLS 404 UAT passes. Accepting these synthetic route stamps would restore the weakness this patch removes.

Affected assertions:

- `test_http_404_then_later_pinned_https_filename_without_stamp_is_not_used_by_live_client (test_issue_599_status_redirect_and_stamp_boundaries_uat.Issue599StatusRedirectAndStampBoundariesUAT)`
- `test_mixed_case_https_recorded_route_is_authenticated (test_issue_599_status_redirect_and_stamp_boundaries_uat.Issue599StatusRedirectAndStampBoundariesUAT)`

