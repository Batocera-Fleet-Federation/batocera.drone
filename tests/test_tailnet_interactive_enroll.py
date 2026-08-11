"""tailnet_service.tailnet_enroll_interactive(): the no-authkey enrollment
path the GitHub enrollment-mailbox feature is built on (see
device/enrollment_mailbox.py). Unlike tailnet_enroll(), it shells out via
subprocess.Popen (not subprocess.run) and reads its stdout incrementally
with a real deadline, so these tests use a genuine OS pipe (os.pipe()) for
process.stdout rather than a plain Mock -- select.select() needs a real,
selectable file descriptor, and this also exercises the actual read1()/
select() loop rather than assuming it works.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.tailnet_service as tailnet_service
from app.drone_api import Settings


def _fake_tailscale_cli() -> mock.MagicMock:
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


def _status_json_result() -> mock.Mock:
    payload = {"BackendState": "Running", "Self": {}, "Peer": {}}
    return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")


def _fake_process(output: bytes, *, exit_code=0, leave_open: bool = False) -> mock.Mock:
    """A fake subprocess.Popen() result backed by a real OS pipe, so
    select.select() (which needs a real fd) behaves exactly as it would
    against a genuine tailscale subprocess."""
    read_fd, write_fd = os.pipe()
    if output:
        os.write(write_fd, output)
    if not leave_open:
        os.close(write_fd)  # EOF once `output` has been consumed
    process = mock.Mock()
    process.stdout = os.fdopen(read_fd, "rb")
    process.poll.return_value = exit_code
    process._write_fd = write_fd if leave_open else None
    return process


class TailnetEnrollInteractiveTests(unittest.TestCase):
    def test_not_installed_raises(self) -> None:
        cli = mock.MagicMock()
        cli.exists.return_value = False
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", cli):
            with self.assertRaises(RuntimeError):
                tailnet_service.tailnet_enroll_interactive()

    def test_daemon_start_failure_raises(self) -> None:
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value="daemon did not start"):
            with self.assertRaises(RuntimeError):
                tailnet_service.tailnet_enroll_interactive()

    def test_captures_printed_login_url(self) -> None:
        output = b"To authenticate, visit:\n\n\thttps://login.tailscale.com/a/abc123def456\n\n"
        process = _fake_process(output)
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=_status_json_result()), \
                mock.patch.object(tailnet_service.subprocess, "Popen", return_value=process):
            url = tailnet_service.tailnet_enroll_interactive(wait_seconds=2.0)
        self.assertEqual(url, "https://login.tailscale.com/a/abc123def456")

    def test_no_url_and_nonzero_exit_raises_with_detail(self) -> None:
        process = _fake_process(b"tailscale: some CLI error\n", exit_code=1)
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=_status_json_result()), \
                mock.patch.object(tailnet_service.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError) as ctx:
                tailnet_service.tailnet_enroll_interactive(wait_seconds=2.0)
        self.assertIn("some CLI error", str(ctx.exception))

    def test_timeout_with_no_url_raises(self) -> None:
        process = _fake_process(b"", leave_open=True)
        process.poll.return_value = None  # still running, never prints a URL
        try:
            with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                    mock.patch.object(tailnet_service.subprocess, "run", return_value=_status_json_result()), \
                    mock.patch.object(tailnet_service.subprocess, "Popen", return_value=process):
                with self.assertRaises(RuntimeError) as ctx:
                    tailnet_service.tailnet_enroll_interactive(wait_seconds=0.3)
            self.assertIn("did not print a login URL", str(ctx.exception))
        finally:
            os.close(process._write_fd)

    def test_never_passes_an_authkey_flag(self) -> None:
        # The whole point of this function is that no secret is ever
        # transmitted -- assert the Popen argv contains no --authkey.
        output = b"https://login.tailscale.com/a/xyz\n"
        process = _fake_process(output)
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service.subprocess, "run", return_value=_status_json_result()), \
                mock.patch.object(tailnet_service.subprocess, "Popen", return_value=process) as popen_mock:
            tailnet_service.tailnet_enroll_interactive(wait_seconds=2.0)
        argv = popen_mock.call_args[0][0]
        self.assertTrue(all("--authkey" not in arg for arg in argv))
        self.assertNotIn("--timeout=45s", argv)


if __name__ == "__main__":
    unittest.main()
