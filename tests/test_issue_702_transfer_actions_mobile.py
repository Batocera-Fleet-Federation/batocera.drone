"""Transfer Actions (cancel/pause/resume/retry) stay reachable on phone widths."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TransferActionsMobileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = (ROOT / "app/web/static/css/drone.css").read_text(encoding="utf-8")
        start = self.css.index("/* Transfer summaries stay short on phones")
        self.block = self.css[start:]

    def test_actions_cell_is_never_hidden_on_phones(self) -> None:
        hidden = re.findall(r"([^{}]+)\{\s*display:\s*none;?\s*\}", self.block)
        for selector in hidden:
            self.assertNotIn("nth-child(10)", selector)
            self.assertNotIn("download-actions", selector)

    def test_actions_cell_spans_full_width_on_phones(self) -> None:
        self.assertRegex(
            self.block,
            r"table\.bff-stack\.transfer-table td\.download-actions\s*\{[^}]*grid-column:\s*1\s*/\s*-1",
        )

    def test_actions_cell_is_rendered_for_active_rows(self) -> None:
        js = (ROOT / "app/web/static/js/drone.js").read_text(encoding="utf-8", errors="replace")
        self.assertIn('<td class="download-actions">${actions}</td>', js)


if __name__ == "__main__":
    unittest.main()
