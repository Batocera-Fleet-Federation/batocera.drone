"""Issue #740: emailed digest must fall back to the drone id when the peer name is blank.

Quoted requirement: "Update the email and notification to be clear about
who/what/when/where."

The bell-side label helpers already strip whitespace-only names and fall back
to the drone id. The digest-side grouping helpers must do the same: a stored
row whose source/target name is whitespace-only but whose id is known must
still identify the peer by id, never render a blank "who" as "unknown".
"""

import unittest

from app.device import smtp_manager


class Issue740DigestBlankPeerFallback(unittest.TestCase):
    def test_download_summary_falls_back_to_source_id_on_blank_name(self) -> None:
        # Issue #740: "Update the email and notification to be clear about
        # who/what/when/where" -- a whitespace-only source name must not
        # render as a blank "who" in the email; the known id must win instead.
        rows = [
            {
                "event_type": "asset_downloaded",
                "details": {
                    "system": "snes",
                    "source_drone_name": "   ",
                    "source_drone_id": "source-a",
                    "size_bytes": 10,
                },
            }
        ]
        text = "\n".join(smtp_manager._asset_summary_lines(rows))
        self.assertIn("from source-a", text)
        self.assertNotIn("unknown source", text)

    def test_upload_summary_falls_back_to_target_id_on_blank_name(self) -> None:
        # Issue #740: "Update the email and notification to be clear about
        # who/what/when/where" -- same blank-"who" guard on the uploader side.
        rows = [
            {
                "event_type": "asset_uploaded",
                "details": {
                    "system": "snes",
                    "target_drone_name": "  ",
                    "target_drone_id": "downloader-1",
                    "size_bytes": 10,
                },
            }
        ]
        text = "\n".join(smtp_manager._asset_summary_lines(rows))
        self.assertIn("to downloader-1", text)
        self.assertNotIn("unknown peer", text)


if __name__ == "__main__":
    unittest.main()
