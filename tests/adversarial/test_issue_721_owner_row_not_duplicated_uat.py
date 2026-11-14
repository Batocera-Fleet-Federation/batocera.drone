"""Issue #721: local 'Available on' must not duplicate the owner after a check.

Quoted acceptance criterion: "The owner row isn't duplicated
(`exclude_drone` / registry de-dup)."

Local game/movie/track pages render "This Drone · This page" immediately (no
`also-on` on open). Clicking "Check other Drones" fans out through
`GET /media-browse/{kind}/also-on`. The endpoint includes this Drone unless
`exclude_drone` is set, and `keyOf` for a local owner without `drone_id` does
not match the local copy the endpoint returns, so a second "This Drone" row
appears under the owner.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA_BROWSE_JS = ROOT / "app" / "web" / "static" / "js" / "media_browse.js"
HARNESS = Path(__file__).resolve().parent / "issue_721_media_browse_harness.js"


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
            "issue 721 harness failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise AssertionError(
            f"harness did not print JSON: {err}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from err


class LocalOwnerRowNotDuplicatedUat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _run_harness()

    def _assert_local_surface(self, name: str, data: dict) -> None:
        # Issue #721: "The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self.assertNotIn("error", data, f"{name}: {data}")
        opened = data["opened"]
        checked = data["checked"]
        self.assertEqual(
            opened["alsoOn"],
            [],
            f"{name} must not call also-on until Check other Drones is clicked: {opened}",
        )
        self.assertEqual(
            opened["thisDrone"],
            1,
            f"{name} should show one owner row on open: {opened}",
        )
        self.assertTrue(opened["thisPage"], f"{name} local page should label This page: {opened}")
        self.assertEqual(
            len(checked["alsoOn"]),
            1,
            f"{name} Check other Drones should call also-on once: {checked}",
        )
        self.assertTrue(
            checked["arcade"],
            f"{name} should still append other holders (Arcade): {checked['hostHtml']}",
        )
        self.assertEqual(
            checked["thisDrone"],
            1,
            f"{name} owner row was duplicated after Check other Drones "
            f"(also-on={checked['alsoOn']!r}, ownerKey={checked['ownerKey']!r}): "
            f"{checked['hostHtml']}",
        )

    def test_local_movie_page_does_not_duplicate_this_drone_after_check(self):
        # Issue #721: "The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_local_surface("local movie", self.payload["movie"])

    def test_local_music_page_does_not_duplicate_this_drone_after_check(self):
        # Issue #721: "The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_local_surface("local music", self.payload["music"])

    def test_local_game_page_does_not_duplicate_this_drone_after_check(self):
        # Issue #721: "The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_local_surface("local game", self.payload["game"])


if __name__ == "__main__":
    unittest.main()
