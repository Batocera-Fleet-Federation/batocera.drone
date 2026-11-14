"""Issue #722: the Ports client README uses the Search Assets name.

Quoted requirement:
- "Rename the tab from \"Request Assets\" to \"Search Assets\" (Swarm sub-tab label, docstrings, README, tests)."
"""

from __future__ import annotations

import unittest
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "ports-client" / "README.md"


class SearchAssetsReadmeRenameUat(unittest.TestCase):
    def test_readme_no_longer_calls_the_tab_request_assets(self) -> None:
        # "Rename the tab from \"Request Assets\" to \"Search Assets\" (Swarm sub-tab label, docstrings, README, tests)."
        text = README.read_text(encoding="utf-8")
        self.assertIn("Search Assets", text)
        leftovers = [
            line.strip()
            for line in text.splitlines()
            if "Request Assets" in line
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
