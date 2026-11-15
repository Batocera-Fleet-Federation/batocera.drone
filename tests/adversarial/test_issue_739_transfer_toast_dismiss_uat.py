"""Issue #739: the mobile transfer toast must be dismissible and stay dismissed.

Quoted requirement: "Also, when downloading an asset on mobile the transfer
modal needs to have a \"close\" button so the user can dismiss it."
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA_BROWSE_JS = ROOT / "app" / "web" / "static" / "js" / "media_browse.js"
HARNESS = Path(__file__).resolve().parent / "issue_739_transfer_toast_harness.js"


def _run_harness() -> dict:
    env = os.environ.copy()
    env["MEDIA_BROWSE_JS"] = str(MEDIA_BROWSE_JS)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ROOT),
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            "issue 739 transfer-toast harness failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise AssertionError(
            f"harness did not print JSON: {err}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from err


class TransferToastDismissUat(unittest.TestCase):
    def test_close_button_dismisses_running_transfer_toast(self):
        # Issue #739: "when downloading an asset on mobile the transfer modal
        # needs to have a \"close\" button so the user can dismiss it."
        payload = _run_harness()
        self.assertNotIn("error", payload, payload)
        self.assertGreaterEqual(payload["toastBeforeClose"], 1, payload)
        self.assertTrue(payload["closeButtonPresent"], payload)
        self.assertFalse(payload["closeHidden"], payload)
        self.assertEqual(payload["toastImmediatelyAfterClose"], 0, payload)
        self.assertEqual(
            payload["toastAfterPollWindow"],
            0,
            "dismissing the transfer toast while a download is running must keep it "
            f"dismissed after the next poll: {payload}",
        )
        self.assertEqual(
            payload["toastAfterNextPoll"],
            0,
            "dismissing the transfer toast while a download is running must keep it "
            f"dismissed after later polls: {payload}",
        )


if __name__ == "__main__":
    unittest.main()
