"""Issue #695 UAT: Transfers shows participants, opens details on row click, and has no asset request UI.

Issue sentences verified:
- "Remove UI for browsing and requesting assets from other drones in swarm in the swarm transfers page."
- "Transfers page should be updated to also show who initiated the request, who is downloading and who is uploading."
- "when a row is clicked a modal shows with all info." (click opens the modal)

The real Transfers panel runs in a Node vm (issue_695_transfer_ui.py) from real
backend rows. A download pulled by this Drone is initiated and downloaded here
and uploaded by the source peer; an upload to a peer is initiated and downloaded
by that peer and uploaded here.
"""
import re
import tempfile
import unittest

from issue_695_transfer_ui import build_backend_snapshots, modal_pairs, run_transfers_panel

PARTICIPANT_CELL = re.compile(r'<td class="small mono transfer-participant">([^<]*)</td>')


def _index(result: dict, direction: str) -> int:
    for index, row in enumerate(result["rows"]):
        if row["details"].get("_direction") == direction:
            return index
    raise AssertionError(f"no {direction} row rendered: {result['bodyHtml']}")


class TransferRolesAndActivationUat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            downloads, uploads = build_backend_snapshots(tmp)
        cls.result = run_transfers_panel(downloads, uploads)

    def _row_cells(self, direction: str) -> list:
        index = _index(self.result, direction)
        cells = PARTICIPANT_CELL.findall(self.result["bodyHtml"])
        # Three participant cells per rendered row, in the same DOM order as result["rows"].
        per_row = [cells[i:i + 3] for i in range(0, len(cells), 3)]
        return per_row[index]

    def test_download_row_shows_initiator_downloader_and_uploader(self):
        # Issue #695: "...show who initiated the request, who is downloading and who is uploading."
        self.assertEqual(self._row_cells("download"), ["target-a", "target-a", "source-a"])

    def test_upload_row_shows_requesting_peer_as_initiator_and_downloader(self):
        # Issue #695: "...show who initiated the request, who is downloading and who is uploading."
        self.assertEqual(self._row_cells("upload"), ["peer-b", "peer-b", "target-a"])

    def test_row_click_opens_modal_with_the_row_details(self):
        # Issue #695: "when a row is clicked a modal shows with all info."
        for direction, expected_file in (("download", "Game0.zip"), ("upload", "snes/Game1.zip")):
            index = _index(self.result, direction)
            step = next(s for s in self.result["clicks"] if s["label"] == f"click row {index}")
            self.assertIsNotNone(step["pairs"], f"{direction} row click did not open the details modal")
            pairs = modal_pairs(step)
            self.assertEqual(pairs.get("File"), expected_file)
            self.assertIn("Initiated by", pairs)
            self.assertIn("Downloading", pairs)
            self.assertIn("Uploading", pairs)

    def test_transfers_page_mounts_no_asset_request_or_browse_ui(self):
        # Issue #695: "Remove UI for browsing and requesting assets from other drones in swarm in the swarm transfers page."
        rendered = self.result["panelHtml"] + self.result["bodyHtml"]
        for marker in ("localTransferRequestPanel", "localAssetsCard", "Request Assets", "Request assets"):
            self.assertNotIn(marker, rendered)
        # The app shell also calls /auth/session on load; the Transfers panel itself must
        # not ask any peer, inventory, media-browse or swarm endpoint.
        self.assertIn("GET /v1/api/admin/downloads", " ".join(self.result["fetched"]))
        for call in self.result["fetched"]:
            for forbidden in ("/peer/", "/inventory", "/media-browse", "/local-network", "/swarm"):
                self.assertNotIn(forbidden, call, f"Transfers page requested an asset-browse endpoint: {call}")


if __name__ == "__main__":
    unittest.main()
