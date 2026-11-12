"""Issue #695 UAT: a clicked transfer row opens a modal with all of that row's info.

Issue sentence verified: "when a row is clicked a modal shows with all info."

The real Transfers panel is rendered in a Node vm (see issue_695_transfer_ui.py)
from real backend rows. Every populated text field of a row must appear in the
modal's detail list; the modal currently omits fields the backend sends
(for example `sync_id`, `source_action_id`, `file_type`, `entry_type`).
"""
import tempfile
import unittest

from issue_695_transfer_ui import build_backend_snapshots, run_transfers_panel


class TransferModalAllInfoUat(unittest.TestCase):
    def test_modal_lists_every_populated_field_of_each_row(self):
        # Issue #695: "when a row is clicked a modal shows with all info."
        with tempfile.TemporaryDirectory() as tmp:
            downloads, uploads = build_backend_snapshots(tmp)
        result = run_transfers_panel(downloads, uploads)
        self.assertEqual(len(result["rows"]), 2, result["bodyHtml"])

        missing = {}
        for index, row in enumerate(result["rows"]):
            details = row["details"]
            label = f"row {index} ({details.get('_direction')})"
            step = next(s for s in result["clicks"] if s["label"] == f"click row {index}")
            self.assertIsNotNone(step["pairs"], f"clicking {label} did not open the details modal")
            shown = " ".join(value for _, value in step["pairs"])
            absent = [
                key for key, value in details.items()
                if not key.startswith("_") and isinstance(value, str) and value.strip() and value not in shown
            ]
            if absent:
                missing[label] = absent
        self.assertEqual(
            missing, {},
            "details modal omits populated row fields (issue #695: 'a modal shows with all info'): "
            f"{missing}",
        )


if __name__ == "__main__":
    unittest.main()
