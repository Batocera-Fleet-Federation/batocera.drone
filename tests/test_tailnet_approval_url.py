"""A pending-device-approval tailnet account surfaces a clickable auth_url.

Some Tailscale accounts (device approval on tenant-wide, or an auth key
created without "Pre-authenticated") never auto-authorize a new device from
the key alone -- `tailscale up --timeout=...` just times out waiting for a
state that needs a human to visit a one-time URL first. Before this, that URL
was only ever visible by SSHing in and reading tailscaled's own log; the
admin UI showed a dead-end "timed out" message with nothing to click.
"""

import unittest
from unittest import mock

from app.device import tailnet_service
from app.web import handlers_network


def _completed(stdout: str = "", stderr: str = "", returncode: int = 1):
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


def _fake_tailscale_cli() -> mock.MagicMock:
    # pathlib.Path uses __slots__, so .exists can't be patched on the real
    # module-level TAILSCALE_CLI instance -- replace it wholesale instead,
    # same as tests/test_tailnet_sharing.py's helper of the same name.
    cli = mock.MagicMock()
    cli.exists.return_value = True
    cli.__str__.return_value = "/userdata/system/tailscale/bin/tailscale"
    return cli


class FindTailscaleAuthUrlTests(unittest.TestCase):
    def test_finds_the_url_in_stdout(self) -> None:
        text = "Success.\nTo authenticate, visit:\n\n\thttps://login.tailscale.com/a/1e866d70019e76\n\n"
        self.assertEqual(
            tailnet_service._find_tailscale_auth_url(text, ""),
            "https://login.tailscale.com/a/1e866d70019e76",
        )

    def test_finds_the_url_in_stderr_when_stdout_has_none(self) -> None:
        self.assertEqual(
            tailnet_service._find_tailscale_auth_url("", "visit https://login.tailscale.com/a/abc123 please"),
            "https://login.tailscale.com/a/abc123",
        )

    def test_no_match_returns_empty_string(self) -> None:
        self.assertEqual(
            tailnet_service._find_tailscale_auth_url("timeout waiting for Running state", "exit code 1"),
            "",
        )


class TailnetEnrollApprovalRequiredTests(unittest.TestCase):
    def _enroll(self, stdout: str, stderr: str = ""):
        with mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value=""), \
                mock.patch.object(
                    tailnet_service, "_run_cli", return_value=_completed(stdout=stdout, stderr=stderr)
                ):
            return tailnet_service.tailnet_enroll("tskey-auth-fake")

    def test_pending_approval_raises_with_the_auth_url(self) -> None:
        stdout = "To authenticate, visit:\n\n\thttps://login.tailscale.com/a/deadbeef\n\ntimeout waiting for Tailscale service to enter a Running state"
        with self.assertRaises(tailnet_service.TailnetApprovalRequired) as ctx:
            self._enroll(stdout)
        self.assertEqual(ctx.exception.auth_url, "https://login.tailscale.com/a/deadbeef")
        self.assertNotIn("tskey-auth-fake", str(ctx.exception))

    def test_failure_without_an_auth_url_stays_a_plain_runtime_error(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._enroll("", stderr="some other failure")
        self.assertNotIsInstance(ctx.exception, tailnet_service.TailnetApprovalRequired)

    def test_rotate_auth_key_preserves_the_auth_url_instead_of_rewrapping_it(self) -> None:
        stdout = "visit: https://login.tailscale.com/a/feedface\ntimeout waiting for Tailscale service to enter a Running state"
        with mock.patch.object(tailnet_service, "tailnet_status", return_value={"installed": True, "enrolled": True}), \
                mock.patch.object(tailnet_service, "TAILSCALE_CLI", _fake_tailscale_cli()), \
                mock.patch.object(tailnet_service, "_start_daemon_if_needed", return_value=""), \
                mock.patch.object(tailnet_service, "_run_cli", side_effect=[_completed(returncode=0), _completed(stdout=stdout, returncode=1)]):
            with self.assertRaises(tailnet_service.TailnetApprovalRequired) as ctx:
                tailnet_service.tailnet_rotate_auth_key("tskey-auth-fake")
        self.assertEqual(ctx.exception.auth_url, "https://login.tailscale.com/a/feedface")


class TailnetEnrollHandlerApprovalResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = handlers_network.HandlersNetworkMixin()
        self.handler.settings = mock.Mock()
        self.handler._send_json = mock.Mock()

    def test_enroll_handler_includes_auth_url_in_the_502_body(self) -> None:
        # Raised via handlers_network's own imported reference to the class,
        # not tailnet_service's -- a sibling test file's importlib.reload()
        # of tailnet_service (for its env-var-override tests) mints a new,
        # distinct class object there, which would make an exception built
        # from tailnet_service.TailnetApprovalRequired silently miss this
        # handler's `except TailnetApprovalRequired` and fall through to the
        # generic RuntimeError branch instead, depending on test run order.
        error = handlers_network.TailnetApprovalRequired("needs approval", "https://login.tailscale.com/a/cafef00d")
        with mock.patch.object(handlers_network, "tailnet_enroll", side_effect=error):
            self.handler._handle_admin_tailnet_enroll({"auth_key": "tskey-auth-fake"})
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["auth_url"], "https://login.tailscale.com/a/cafef00d")

    def test_rotate_handler_includes_auth_url_in_the_502_body(self) -> None:
        error = handlers_network.TailnetApprovalRequired("needs approval", "https://login.tailscale.com/a/cafef00d")
        with mock.patch.object(handlers_network, "tailnet_rotate_auth_key", side_effect=error):
            self.handler._handle_admin_tailnet_rotate_auth_key({"auth_key": "tskey-auth-fake"})
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertEqual(payload["auth_url"], "https://login.tailscale.com/a/cafef00d")

    def test_plain_runtime_error_has_no_auth_url_key(self) -> None:
        with mock.patch.object(handlers_network, "tailnet_enroll", side_effect=RuntimeError("could not run")):
            self.handler._handle_admin_tailnet_enroll({"auth_key": "tskey-auth-fake"})
        status, payload = self.handler._send_json.call_args.args
        self.assertEqual(status, 502)
        self.assertNotIn("auth_url", payload)


if __name__ == "__main__":
    unittest.main()
