"""Regression checks for the empty security suites reported in Issue #616."""

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
EMPTY_DISCOVERY_SUITE_IDS = {
    "adversarial-security-issue-279-rejects-multi-certificate-pem",
    "adversarial-security-issue-279-rejects-non-x509-pem",
    "adversarial-security-issue-191-discovery-infinite-port",
    "adversarial-security-issue-191-discovery-packet-type",
    "adversarial-security-issue-155-hardlinked-lock-chown",
    "adversarial-security-issue-160-reprovisioning-boundaries",
    "adversarial-security-issue-163-clone-auth-revocation",
    "adversarial-security-issue-213-pair-by-address-scheme-downgrade",
    "adversarial-security-issue-240-launch-offer-binding",
    "adversarial-security-issue-240-execution-revocation",
    "adversarial-security-issue-246-tls-pin-scheme-rewrite",
    "adversarial-security-issue-332-lan-tier-scheme-casing",
    "adversarial-security-issue-240-sunshine-pidfile",
}


class Issue616SecuritySuiteRegistrationTests(unittest.TestCase):
    def test_reported_empty_security_suites_use_nonempty_runner(self):
        registry = json.loads((ROOT / ".swarm/tests.json").read_text())
        suites = {
            suite["id"]: suite
            for suite in registry["suites"]
            if suite.get("enabled") and suite.get("origin") == "adversarial-security"
        }
        self.assertTrue(EMPTY_DISCOVERY_SUITE_IDS.issubset(suites))

        for suite_id in sorted(EMPTY_DISCOVERY_SUITE_IDS):
            with self.subTest(suite=suite_id):
                command = suites[suite_id]["command"]
                result = subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, timeout=180
                )
                output = result.stdout + result.stderr
                if "Ran 0 tests" in output or "No tests were collected" in output:
                    self.assertNotEqual(
                        result.returncode, 0,
                        "registered security acceptance command passed with empty discovery",
                    )


if __name__ == "__main__":
    unittest.main()
