# Issue #615 validation and test disputes

The existing caller-side generation snapshot passes all 15 tests in
`test_issue_615*.py`. No additional production change is required for the
reported startup/retirement race. Protected tests and `.swarm/tests.json`
remain unchanged.

## Authentication evidence

The repository VPN skill (`.claude/skills/drone-vpn-management/SKILL.md`,
"Sharing revocation poller") specifies that a peer 404 requires process-local
evidence attached after receiving a status on a verified pinned TLS connection;
`peer_route` is diagnostic only (#599). The independent security test
`tests/adversarial/security/test_issue_599_revocation_requires_verified_channel.py::RevocationRequiresVerifiedChannelSecurityTests::test_route_stamp_and_https_error_url_are_not_evidence`
explicitly rejects both fabricated HTTPS URLs and mixed-case HTTPS route stamps.
Its `test_pinned_tls_404_still_revokes` checks the genuine TLS case.

The following reproduced failures use fabricated HTTPError responses without
that verified-channel evidence. They cannot be made to pass by trusting URL or
route metadata without contradicting the #599 security requirement. Each line
requests independent adjudication; none of these tests was modified.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_586_real_check_revocation_recovery_uat.py::test_transient_peer_errors_never_revoke_but_a_404_does: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_surrogate_failure_then_real_revocation_uat.py::test_surrogate_log_failure_is_followed_by_real_404_credential_revocation: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_broken_stderr_during_real_check_error_report_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_closed_stderr_on_failed_tick_does_not_stop_later_revocation: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_disconnect_failure_on_revoking_tick_wipes_without_retrying_disconnect: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_long_run_of_unexpected_failures_does_not_retire_poller: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_non_string_error_during_real_check_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_repeated_hostile_failures_then_real_revocation_still_happens: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_poller_tick_survival_uat.py::test_surrogate_roster_failure_inside_real_check_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::SmtpSharingPollerTickSurvivalUAT::test_broken_stderr_during_real_check_error_report_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::SmtpSharingPollerTickSurvivalUAT::test_closed_stderr_on_failed_tick_does_not_stop_later_revocation: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::SmtpSharingPollerTickSurvivalUAT::test_repeated_hostile_failures_then_real_revocation_still_happens: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::SmtpSharingPollerTickSurvivalUAT::test_surrogate_roster_failure_inside_real_check_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::TailnetSharingPollerTickSurvivalUAT::test_broken_stderr_during_real_check_error_report_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::TailnetSharingPollerTickSurvivalUAT::test_closed_stderr_on_failed_tick_does_not_stop_later_revocation: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::TailnetSharingPollerTickSurvivalUAT::test_repeated_hostile_failures_then_real_revocation_still_happens: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_587_sibling_poller_tick_survival_uat.py::TailnetSharingPollerTickSurvivalUAT::test_surrogate_roster_failure_inside_real_check_is_survived: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_597_disconnect_failure_wipe_uat.py::test_reports_revocation_as_true_when_wipe_happened (peer='peer-off'): expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_597_disconnect_failure_wipe_uat.py::test_wipe_and_revocation_record_survive_disconnect_error (peer='peer-off'): expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_597_fresh_disconnect_finally_uat.py::test_real_disconnect_failure_still_wipes_on_peer_off: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_598_stale_revocation_reimport_uat.py::test_same_source_404_still_wipes_credentials_refreshed_during_the_call: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_598_revocation_atomicity_uat.py::test_failed_replacement_import_does_not_block_genuine_404_wipe: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_598_revocation_atomicity_uat.py::test_later_404_from_new_source_still_revokes_after_stale_skip: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_598_provenance_identity_boundaries_uat.py::test_same_source_credential_refresh_during_404_is_still_revoked: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_598_provenance_identity_boundaries_uat.py::test_second_overlapping_404_after_wipe_does_not_disconnect_again: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_599_status_redirect_and_stamp_boundaries_uat.py::test_http_404_then_later_pinned_https_filename_without_stamp_is_not_used_by_live_client: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

SWARM_TEST_DISPUTE: tests/adversarial/test_issue_599_status_redirect_and_stamp_boundaries_uat.py::test_mixed_case_https_recorded_route_is_authenticated: expects authentication or revocation from an unstamped mock HTTPError; the #599 verified-channel requirement in the VPN skill and security test cited above rejects URL/peer_route metadata as authority. #615 changes generation capture only.

## Separate disconnect retry finding

`tests/adversarial/test_issue_597_out_of_scope_failed_disconnect_not_retried_uat.py::OutOfScopeFailedDisconnectNotRetriedUAT::test_failed_disconnect_is_retried_on_a_later_tick_while_peer_gone`
also reproduces. Its module documentation explicitly identifies failed-stop retry
as a separate defect outside #597. This is a legitimate remaining behavior gap,
not an authentication expectation dispute. #615 addresses retirement before a
started worker captures its generation; adding persistent disconnect retries
would change revocation behavior beyond that scope. It remains unfixed.
