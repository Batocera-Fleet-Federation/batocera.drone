"""User-level acceptance tests for issue #126: Transfers loading toast blocks Swarm.

Opening Transfers must not hold the global "Loading transfers..." toast while the
page's data loads (the request panel waits on the peer overview probe). Navigating
on to the Swarm page must not inherit that toast, and Transfers still shows its
own inline loading placeholders.

Covers the Transfers page and Swarm page wiring in drone.js.
"""

import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
