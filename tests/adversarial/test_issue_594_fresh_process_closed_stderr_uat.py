"""UAT for #594 (follow-up to #586): the VPN sharing poller survives closed stderr in a real process.

#586 wrapped every sharing-revocation tick so an unexpected Exception is logged and the loop keeps
its cadence. The logging path writes to sys.stderr, so the guard has to hold when the process's
stderr is genuinely unusable, not only when a Python stand-in raises. The in-process suites use a
fake stream; this one runs the real run_sharing_revocation_poller in a fresh interpreter whose file
descriptor 2 has been closed at the OS level, so every write to stderr fails with EBADF.

Expected behavior: the first tick raises, the failure report cannot be written, and the poller still
runs every later tick until it is retired. The child reports its counts on stdout, since stderr is
gone by design.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

CHILD = r'''
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
import app.device.vpn_manager as vpn_manager

os.close(2)  # real closed stderr: every write to fd 2 now fails with EBADF
calls = []
sleeps = []


def check(settings):
    calls.append(settings)
    if len(calls) == 1:
        raise RuntimeError("unexpected first tick")
    return False


def fake_sleep(seconds):
    sleeps.append(seconds)


def retired(_generation):
    # Stay live for three wake-ups, then report retirement.
    return len(sleeps) > 3


vpn_manager.check_sharing_revocation = check
vpn_manager.time.sleep = fake_sleep
vpn_manager.poller_retired = retired
vpn_manager.run_sharing_revocation_poller(None)
os.write(1, json.dumps({"calls": len(calls), "sleeps": len(sleeps)}).encode())
'''


class ClosedStderrPollerFreshProcessUAT(unittest.TestCase):
    def test_closed_stderr_failure_does_not_stop_the_real_poller(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", CHILD, str(REPO_ROOT)],
            capture_output=True,
            timeout=120,
            cwd=str(REPO_ROOT),
        )

        # The exit status is not asserted: CPython's final flush of the closed fd 2 makes the
        # interpreter exit non-zero (120). The stdout line is written only after the poller
        # returns normally, so its presence proves the loop survived the closed stderr.
        self.assertTrue(completed.stdout, completed.stderr.decode("utf-8", "replace"))
        result = json.loads(completed.stdout.decode("utf-8"))
        # Tick 1 raised and could not be reported to a closed stderr; ticks 2 and 3 still ran.
        # Three live wake-ups give three checks; the fourth wake sees retirement and returns.
        self.assertEqual(result["calls"], 3)
        self.assertEqual(result["sleeps"], 4)


if __name__ == "__main__":
    unittest.main()
