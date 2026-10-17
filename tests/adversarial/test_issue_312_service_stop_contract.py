"""Issue #312 deployed service-stop contract.

Expected behavior, derived from the shutdown requirement before inspecting the
delivery: a supported ``service_bootstrap.sh stop`` must leave the Python
process an opportunity to run ``drone_api.main``'s cleanup.  SIGKILL cannot
run a ``finally`` block, so it must not be the launcher mechanism used to
terminate a Drone listener.  A later escalation is only safe after the
graceful, process-directed shutdown has completed.

The test runs an isolated copy of the deployed shell entrypoint with only its
hard-coded runtime paths and ``lsof`` command redirected into a temporary
fixture.  The listener fixture records whether it received the graceful signal
that lets Python execute cleanup.  The real ``drone_api.main`` signal UAT
separately proves that this graceful signal retires the generation.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]


CHILD = r"""
import os
from pathlib import Path
import signal
import time

marker = Path(os.environ["SHUTDOWN_MARKER"])
marker.with_name("ready").write_text("1")

def graceful_shutdown(signum, frame):
    marker.write_text("cleanup ran")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
while True:
    time.sleep(0.05)
"""


class ServiceStopContractTests(unittest.TestCase):
    def test_stop_signals_listener_gracefully_before_it_can_be_force_killed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "graceful-cleanup"
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD],
                env=dict(os.environ, SHUTDOWN_MARKER=str(marker)),
            )
            supervisor = subprocess.Popen(["sleep", "60"])
            self.addCleanup(self._reap, child)
            self.addCleanup(self._reap, supervisor)
            self._wait_for(root / "ready", child)

            pid_file = root / "supervisor.pid"
            control_pid_file = root / "control.pid"
            input_pid_file = root / "input.pid"
            pid_file.write_text(str(supervisor.pid))
            launcher = (REPO / "app" / "service_bootstrap.sh").read_text(encoding="utf-8")
            replacements = {
                'WORK_DIR="/userdata/system/drone-app"': f"WORK_DIR={shlex.quote(str(root / 'app'))}",
                'PID_FILE="/tmp/drone-server.pid"': f"PID_FILE={shlex.quote(str(pid_file))}",
                'CONTROL_PID_FILE="/tmp/drone-server-control.pid"': f"CONTROL_PID_FILE={shlex.quote(str(control_pid_file))}",
                'INPUT_MONITOR_PID_FILE="/tmp/drone-input-activity-monitor.pid"': f"INPUT_MONITOR_PID_FILE={shlex.quote(str(input_pid_file))}",
            }
            for old, new in replacements.items():
                self.assertIn(old, launcher, f"fixture rewrite target vanished: {old}")
                launcher = launcher.replace(old, new, 1)
            launcher_path = root / "service_bootstrap.sh"
            launcher_path.write_text(launcher, encoding="utf-8")

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_lsof = bin_dir / "lsof"
            fake_lsof.write_text(f"#!/bin/sh\nprintf '%s\\n' {child.pid}\n", encoding="utf-8")
            fake_lsof.chmod(0o755)
            result = subprocess.run(
                ["sh", str(launcher_path), "stop"],
                env=dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}"),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            child.wait(timeout=5)
            self.assertTrue(
                marker.exists(),
                "service stop killed the listener without SIGTERM; drone_api.main "
                "cannot run stop_local_network_workers() before SIGKILL",
            )

    @staticmethod
    def _wait_for(ready: Path, child: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if child.poll() is not None:
                raise AssertionError("listener fixture exited before becoming ready")
            if time.monotonic() >= deadline:
                raise AssertionError("listener fixture never became ready")
            time.sleep(0.01)

    @staticmethod
    def _reap(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
