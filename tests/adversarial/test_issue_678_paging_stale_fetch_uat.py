"""Issue #678 UAT: a stale in-flight page fetch must not freeze further paging.

" paging to views and searches so that only 20 are shown at a time" only
works when every abandoned fetch leaves its Drone entry fetchable again.
``loadPeer`` marks the entry ``busy`` while page N is in flight and
``loadMore`` (scroll to the end) refuses entries that are still ``busy``.
If a fetch is abandoned because its token went stale (e.g. the user opens
another tab, which bumps every other kind's token) the entry must still be
released, otherwise that Drone is stuck at its first 20 items with no retry
affordance and scrolling can never load page N+1.
"""

import re
import unittest
from pathlib import Path

MEDIA_BROWSE_JS = (
    Path(__file__).resolve().parent.parent.parent
    / "app"
    / "web"
    / "static"
    / "js"
    / "media_browse.js"
)


def extract_function(source, signature):
    """Return the brace-balanced body of the named function declaration."""
    start = source.index(signature)
    open_brace = source.index("{", start)
    depth = 0
    for pos in range(open_brace, len(source)):
        char = source[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError("unbalanced braces in %s" % signature)


class StaleFetchPagingTests(unittest.TestCase):
    def test_stale_token_abandon_releases_busy(self):
        script = MEDIA_BROWSE_JS.read_text(encoding="utf-8")
        body = extract_function(script, "async function loadPeer(")
        self.assertIn("entry.busy = true;", body)
        self.assertIn("loadMore", script)  # next page loads only on scroll
        lines = body.splitlines()
        uses_finally_release = "finally" in body and "entry.busy = false" in body.split("finally", 1)[1]
        stuck = []
        for lineno, line in enumerate(lines, start=1):
            if "token !==" in line and re.search(r"\breturn\s*;", line):
                window = "\n".join(lines[max(0, lineno - 4) : lineno - 1])
                if "entry.busy = false" not in window and not uses_finally_release:
                    stuck.append((lineno, line.strip()))
        self.assertEqual(
            stuck,
            [],
            "stale-token abandon must release entry.busy so loadMore can "
            "fetch the next 20 (stuck lines: %r)" % (stuck,),
        )


if __name__ == "__main__":
    unittest.main()
