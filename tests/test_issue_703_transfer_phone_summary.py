"""Phone transfer rows keep File and Progress visible (drone-admin-features: compact status/file/progress)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TransferPhoneSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        css = (ROOT / "app/web/static/css/drone.css").read_text(encoding="utf-8")
        start = css.index("/* Transfer summaries stay short on phones")
        self.block = css[start:]
        self.hidden = set()
        for selector in re.findall(r"([^{}]+)\{\s*display:\s*none;?\s*\}", self.block):
            self.hidden.update(int(n) for n in re.findall(r"nth-child\((\d+)\)", selector))

    def test_file_column_is_not_hidden_on_phones(self) -> None:
        self.assertNotIn(6, self.hidden)

    def test_progress_column_is_not_hidden_on_phones(self) -> None:
        self.assertNotIn(8, self.hidden)

    def test_status_system_and_speed_stay_visible(self) -> None:
        for column in (2, 7, 9):
            self.assertNotIn(column, self.hidden)

    def test_only_participant_columns_are_hidden(self) -> None:
        self.assertEqual(self.hidden, {3, 4, 5})


if __name__ == "__main__":
    unittest.main()
