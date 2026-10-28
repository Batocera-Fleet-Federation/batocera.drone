"""Registration check for the missing security suite files in Issue #631."""

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class Issue631SecuritySuiteFilesExistTests(unittest.TestCase):
    def test_enabled_security_suite_commands_point_to_existing_test_files(self):
        """The manifest registers 13 adversarial-security suites whose test files are absent from tests/adversarial/security/."""
        registry = json.loads((ROOT / ".swarm/tests.json").read_text())
        missing = []

        for suite in registry["suites"]:
            if not suite.get("enabled") or suite.get("origin") != "adversarial-security":
                continue
            command = suite["command"]
            try:
                start = Path(command[command.index("-s") + 1])
                pattern = command[command.index("-p") + 1]
            except (ValueError, IndexError):
                missing.append((suite["id"], "command lacks explicit unittest discovery path/pattern"))
                continue

            test_file = start / pattern
            if not (ROOT / test_file).is_file():
                missing.append((suite["id"], str(test_file)))

        self.assertEqual([], missing, "registered security suites need present test files")


if __name__ == "__main__":
    unittest.main()
