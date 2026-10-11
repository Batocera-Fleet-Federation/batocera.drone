"""User-level acceptance tests for issue #126: Transfers loading toast blocks Swarm.

Opening Transfers must not hold the global "Loading transfers..." toast while the
page's data loads (the request panel waits on the peer overview probe). Navigating
on to the Swarm page must not inherit that toast, and Transfers still shows its
own inline loading placeholders.

Covers the Transfers page and Swarm page wiring in drone.js.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.drone_api import DownloadManager, RomRepository, Settings


ROOT = Path(__file__).resolve().parents[1]


class TransfersLoadingToastUatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = ROOT.joinpath("app/web/static/js/drone.js").read_text(encoding="utf-8")

    def _transfers_page_body(self) -> str:
        page_start = self.js.index("async function renderTransfersPage()")
        return self.js[page_start:self.js.index("\n}\n", page_start) + 3]

    def test_transfers_page_does_not_show_a_blocking_loading_toast(self) -> None:
        body = self._transfers_page_body()
        self.assertNotIn("setLoading(", body)
        self.assertNotIn("Loading transfers...", body)
        # Failures still surface as a toast, not silently.
        self.assertIn("Failed to load transfers:", body)

    def test_transfers_panels_show_inline_loading_placeholders(self) -> None:
        panel_start = self.js.index("async function renderIntegrationTransfersPanel(")
        panel_body = self.js[panel_start:self.js.index("async function renderLocalTransferRequestPanel(", panel_start)]
        self.assertIn('id="transfersBody"', panel_body)
        self.assertIn("Loading transfers...", panel_body)

    def test_swarm_page_clears_any_leftover_loading_toast_on_entry(self) -> None:
        page_start = self.js.index("async function renderSwarmPage()")
        page_body = self.js[page_start:self.js.index("// Issue #35", page_start)]
        self.assertIn("setLoading(false);", page_body)
        self.assertNotIn('setLoading(true, "Loading swarm...")', page_body)

    def test_transfers_live_refresh_starts_before_the_peer_probe_is_awaited(self) -> None:
        # The request panel waits on the peer overview probe. If the live refresh
        # only started after that await, a slow probe would freeze the queue at
        # its first snapshot, so queued and downloading rows never updated.
        body = self._transfers_page_body()
        refresh_at = body.index("startTransfersAutoRefresh();")
        self.assertLess(refresh_at, body.index("await panelReady;"))
        self.assertIn("const panelReady = renderIntegrationTransfersPanel(", body)

    def test_transfers_panel_lists_active_and_queued_rows_in_the_live_table(self) -> None:
        # Queued (and pending/paused) jobs must reach the live table, not just Recent.
        start = self.js.index("function renderTransfersPanel(payload, uploads)")
        body = self.js[start:self.js.index("function refreshTransfersPanel()", start)]
        self.assertIn("const queued = transferPayload.queued || [];", body)
        self.assertIn("...queued.map(row => ({ ...row, _direction: \"download\" }))", body)
        self.assertIn("renderTransferRows(currentPager.page.rows", body)


class TransfersQueueSnapshotUatTests(unittest.TestCase):
    """The server side of the Transfers page: queued work must be in the snapshot."""

    def test_enqueued_job_is_reported_as_queued_in_the_transfers_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                "os.environ",
                {
                    "USERDATA_ROOT": str(root / "userdata"),
                    "DRONE_STATE_DATABASE_FILE": str(root / "state.db"),
                    "ROMS_ROOT": str(root / "roms"),
                    "BIOS_ROOT": str(root / "bios"),
                    "DRONE_DEVICE_ID": "target-a",
                },
                clear=True,
            ):
                settings = Settings.from_env()
            repo = RomRepository(root / "roms", root / "bios")
            with mock.patch("app.drone_api.Thread.start"):
                manager = DownloadManager(settings, repo)
            queued = manager.enqueue_rom({}, {"drone_id": "source-a"}, "snes", "Queued.zip")
            snapshot = manager.snapshot()
            self.assertEqual([job["job_id"] for job in snapshot["queued"]], [queued["job_id"]])
            self.assertEqual(snapshot["queued"][0]["queue_position"], 1)
            self.assertEqual(snapshot["active"], [])
            self.assertEqual(snapshot["recent"], [])


if __name__ == "__main__":
    unittest.main()
