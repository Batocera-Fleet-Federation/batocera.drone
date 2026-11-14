"""Issue #721: remote Check other Drones must append other holders.

Quoted acceptance criterion: "Clicking \\"Check other Drones\\" runs also-on
once, shows progress inline and appends the other holders' rows. The owner
row isn't duplicated (`exclude_drone` / registry de-dup)."

A remote movie/track page or Games details dialog already shows the owning
peer. also-on still returns this Drone when a local copy exists (exclude_drone
is the remote owner). That local copy is another holder and must appear under
the owner; fillAvailability currently drops every is_local row, so This Drone
never shows.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEDIA_BROWSE_JS = ROOT / "app" / "web" / "static" / "js" / "media_browse.js"
HARNESS = Path(__file__).resolve().parent / "issue_721_remote_other_holders_harness.js"


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
            "issue 721 remote-holders harness failed:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as err:
        raise AssertionError(
            f"harness did not print JSON: {err}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from err


class RemoteCheckAppendsOtherHoldersUat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = _run_harness()

    def _assert_remote_surface(self, name: str, data: dict) -> None:
        # Issue #721: "Clicking "Check other Drones" runs also-on once, shows
        # progress inline and appends the other holders' rows. The owner row
        # isn't duplicated (`exclude_drone` / registry de-dup)."
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
            0,
            f"{name} owner on open is the remote Drone, not This Drone: {opened}",
        )
        self.assertGreaterEqual(
            opened["arcadeRows"],
            1,
            f"{name} should show the owning Arcade row immediately: {opened}",
        )
        self.assertTrue(opened["copy"], f"{name} owner should offer Copy: {opened}")
        self.assertFalse(opened["spinner"], f"{name} spinner must not block the owner: {opened}")
        self.assertEqual(
            len(checked["alsoOn"]),
            1,
            f"{name} Check other Drones should call also-on once: {checked}",
        )
        self.assertTrue(
            any("exclude_drone=peer-1" in url for url in checked["alsoOn"]),
            f"{name} also-on should exclude the owner drone: {checked['alsoOn']}",
        )
        self.assertTrue(
            checked["bedroom"],
            f"{name} should append other remote holders (Bedroom): {checked['hostHtml']}",
        )
        self.assertEqual(
            checked["thisDroneRows"],
            1,
            f"{name} should append This Drone as another holder when also-on "
            f"returns a local copy: {checked['hostHtml']}",
        )
        self.assertEqual(
            checked["arcadeRows"],
            1,
            f"{name} owner row was duplicated after Check other Drones: "
            f"{checked['labels']!r} {checked['hostHtml']}",
        )
        self.assertTrue(
            checked["checkBtn"],
            f"{name} owner row / Check other Drones must stay usable: {checked}",
        )

    def test_remote_movie_page_appends_local_holder_without_duplicating_owner(self):
        # Issue #721: "Clicking "Check other Drones" runs also-on once, shows progress inline and appends the other holders' rows. The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_remote_surface("remote movie", self.payload["movie"])

    def test_remote_music_page_appends_local_holder_without_duplicating_owner(self):
        # Issue #721: "Clicking "Check other Drones" runs also-on once, shows progress inline and appends the other holders' rows. The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_remote_surface("remote music", self.payload["music"])

    def test_games_details_dialog_appends_local_holder_without_duplicating_owner(self):
        # Issue #721: "Clicking "Check other Drones" runs also-on once, shows progress inline and appends the other holders' rows. The owner row isn't duplicated (`exclude_drone` / registry de-dup)."
        self._assert_remote_surface("games dialog", self.payload["games"])


if __name__ == "__main__":
    unittest.main()
