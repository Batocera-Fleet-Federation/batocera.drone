"""Acceptance and registry integration checks for the issue #616 runner."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_security_tests.py"


class SecurityRunnerAcceptanceTests(unittest.TestCase):
    def run_fixture(self, source=None, pattern="test_*.py"):
        with tempfile.TemporaryDirectory() as tmp:
            if source is not None:
                Path(tmp, "test_boundary.py").write_text(source)
            return subprocess.run(
                [sys.executable, str(RUNNER), "-s", tmp, "-p", pattern],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )

    def test_missing_registered_file_is_not_a_pass(self):
        result = self.run_fixture(pattern="test_deleted_security_check.py")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("No security tests were collected", result.stderr)

    def test_existing_file_without_tests_is_not_a_pass(self):
        result = self.run_fixture("# Accidentally removed all test cases.\n")
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_real_passing_test_is_reported(self):
        result = self.run_fixture(
            "import unittest\nclass Boundary(unittest.TestCase):\n"
            "    def test_check(self): self.assertTrue(True)\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_security_failure_is_preserved(self):
        result = self.run_fixture(
            "import unittest\nclass Boundary(unittest.TestCase):\n"
            "    def test_check(self): self.fail('boundary violated')\n"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("boundary violated", result.stderr)

    def test_import_failure_is_preserved(self):
        result = self.run_fixture("raise RuntimeError('broken security import')\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("broken security import", result.stderr)


class SecurityRegistryIntegrationTests(unittest.TestCase):
    def test_enabled_security_suites_have_files_and_collect_real_tests(self):
        registry = json.loads((ROOT / ".swarm/tests.json").read_text())
        suites = [s for s in registry["suites"]
                  if s.get("enabled") and s.get("origin") == "adversarial-security"]
        self.assertGreaterEqual(len(suites), 13)
        for suite in suites:
            with self.subTest(suite=suite["id"]):
                command = suite["command"]
                self.assertEqual(command[:2], ["python3", "scripts/run_security_tests.py"])
                for filename in suite["requirements"]["files"]:
                    self.assertTrue((ROOT / filename).is_file(), filename)
                start = command[command.index("-s") + 1]
                pattern = command[command.index("-p") + 1]
                loader = unittest.TestLoader()
                discovered = loader.discover(str(ROOT / start), pattern=pattern)
                self.assertFalse(loader.errors, loader.errors)
                self.assertGreater(discovered.countTestCases(), 0, suite["id"])


if __name__ == "__main__":
    unittest.main()
