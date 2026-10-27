"""Security acceptance must check individual registrations, not just a directory."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts/run_security_tests.py"


class SecurityRegistryRunnerTests(unittest.TestCase):
    def run_registry(self, sources, patterns):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".swarm").mkdir()
            (root / "checks").mkdir()
            for filename, source in sources.items():
                (root / "checks" / filename).write_text(source)
            suites = [{
                "id": pattern, "enabled": True, "origin": "adversarial-security",
                "command": ["python3", "-m", "unittest", "discover",
                            "-s", "checks", "-p", pattern],
                "requirements": {"files": ["checks/" + pattern]},
            } for pattern in patterns]
            registry = root / ".swarm/tests.json"
            registry.write_text(json.dumps({"suites": suites}))
            return subprocess.run(
                [sys.executable, str(RUNNER), "--registry", str(registry)],
                capture_output=True, text=True, timeout=30,
            )

    PASS = "import unittest\nclass Boundary(unittest.TestCase):\n    def test_boundary(self): pass\n"

    def test_surviving_suite_does_not_hide_missing_registration(self):
        result = self.run_registry({"test_live.py": self.PASS},
                                   ["test_live.py", "test_missing.py"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("test_missing.py: missing required files", result.stderr)
        self.assertNotIn("Ran 1 test", result.stderr)

    def test_existing_but_empty_registered_file_fails(self):
        result = self.run_registry({"test_empty.py": "# No tests\n"}, ["test_empty.py"])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("No security tests were collected", result.stderr)

    def test_registered_test_actually_runs(self):
        result = self.run_registry({"test_live.py": self.PASS}, ["test_live.py"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_registered_failure_is_preserved(self):
        source = self.PASS.replace("pass", "self.fail('security boundary violated')")
        result = self.run_registry({"test_live.py": source}, ["test_live.py"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("security boundary violated", result.stderr)

    def test_no_enabled_security_suites_fails(self):
        result = self.run_registry({}, [])
        self.assertEqual(result.returncode, 2, result.stderr)

