"""Issue #212 out-of-scope follow-ups: protected adversarial harnesses still lack the LAN stop.

Issue #212 names four harnesses under tests/adversarial/ that still remove temp
userdata without ``stop_local_network_workers``. Those files are protected
(a fixer may not edit them), and the issue itself files them as other tests'
teardown follow-ups rather than part of this delivery. So their assertion lives
here, in a separate retained suite, instead of blocking the in-scope suite.

Each failing case below is evidence for a separate, labelled follow-up issue:
the teardown contract is the same as in test_issue_212_harness_teardown_boundaries.py
(the shared scanner is loaded from that sibling file so the rules cannot drift).
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

PROTECTED_HARNESS_FILES = (
    "tests/adversarial/test_issue_152_media_keys_http_malformed_input.py",
    "tests/adversarial/test_issue_152_media_keys_hold_uat.py",
    "tests/adversarial/test_issue_163_clone_auth_http_uat.py",
    "tests/adversarial/test_issue_165_held_identity_vpn_startup.py",
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "_issue_212_harness_scanner",
        Path(__file__).resolve().with_name("test_issue_212_harness_teardown_boundaries.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_scanner = _load_scanner()


class ProtectedHarnessFollowupTests(unittest.TestCase):
    def test_protected_harness_files_exist(self) -> None:
        missing = [rel for rel in PROTECTED_HARNESS_FILES if not (REPO_ROOT / rel).is_file()]
        self.assertEqual(missing, [])

    def test_protected_create_server_harnesses_stop_before_temp_removal(self) -> None:
        self.maxDiff = None
        failures: list[str] = []
        for rel in PROTECTED_HARNESS_FILES:
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            failures.extend(_scanner.harness_failures(source, filename=rel))
        self.assertEqual(failures, [], "\n".join(failures))
