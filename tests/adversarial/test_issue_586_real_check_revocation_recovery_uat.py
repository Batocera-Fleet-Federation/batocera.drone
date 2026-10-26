"""Adversarial UAT for #586 using the real sharing-revocation check.

The existing #586 suite replaces check_sharing_revocation with a mock, so it
never exercises the real failure path. Here the real check runs under the real
poller loop (only sleep, generation and peer I/O are controlled):

* check_sharing_revocation's own handler reports through print(..., file=stderr).
  If stderr is broken, that print raises out of the check. The poller guard must
  absorb it so the next tick still runs the real check.
* A revocation that dies midway (disconnect raises) must not leave credentials
  behind forever: a later tick has to finish the revocation.
* Transient peer failures must not revoke; only an explicit 404 does.
* BaseException (SystemExit) raised inside the real check must still propagate.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


SAMPLE_OVPN = """client
dev tun
proto udp
remote vpn.example.net 1194
remote-cert-tls server
auth-user-pass
up /etc/openvpn/update-resolv-conf
down /etc/openvpn/update-resolv-conf
verb 3
"""

_PEER_ID = "peer-586-real"


class _ClosedStream:
    """Stands in for a closed stderr: every write and flush fails."""

    def write(self, _text: str) -> int:
        raise OSError("stderr closed")

    def flush(self) -> None:
        raise OSError("stderr closed")


class Issue586RealCheckRevocationRecoveryUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
        self.addCleanup(patcher.stop)
        patcher.start()
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-586-real-check-recovery",
        }, clear=True):
            self.settings = Settings.from_env()

    def _import_shared_config(self) -> None:
        payload = {
            "config_filename": "client.ovpn",
            "config_text": SAMPLE_OVPN,
            "has_credentials": True,
            "username": "peeruser",
            "password": "peerpass123",
        }
        vpn_manager.import_from_peer(self.settings, payload, source_peer_id=_PEER_ID, source_peer_name="Peer One")

    def _run_live_ticks(self, *, live_ticks: int, stderr=None) -> list[float]:
        """Run the real poller loop with the real check for ``live_ticks`` wake-ups."""
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        def retired(_generation: int) -> bool:
            return len(sleeps) > live_ticks

        stream = stderr if stderr is not None else io.StringIO()
        with mock.patch.object(vpn_manager, "poller_generation", return_value=31), \
                mock.patch.object(vpn_manager, "poller_retired", new=retired), \
                mock.patch.object(vpn_manager.time, "sleep", new=fake_sleep), \
                mock.patch.object(vpn_manager.sys, "stderr", stream):
            vpn_manager.run_sharing_revocation_poller(self.settings)
        return sleeps

    def test_closed_stderr_inside_real_check_does_not_stop_later_ticks(self) -> None:
        # Real check: _load_state fails, so check_sharing_revocation reaches its
        # handler, whose print() fails on the closed stderr and escapes the check.
        load_state = mock.Mock(side_effect=RuntimeError("state database locked"))

        with mock.patch.object(vpn_manager, "_load_state", load_state):
            sleeps = self._run_live_ticks(live_ticks=3, stderr=_ClosedStream())

        self.assertEqual(load_state.call_count, 3)
        self.assertEqual(len(sleeps), 4)

    def test_real_check_failure_is_logged_once_and_next_tick_runs(self) -> None:
        real_load_state = vpn_manager._load_state
        calls: list[int] = []

        def fail_once_then_real(settings):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("state database locked")
            return real_load_state(settings)

        stderr = io.StringIO()
        with mock.patch.object(vpn_manager, "_load_state", side_effect=fail_once_then_real):
            self._run_live_ticks(live_ticks=2, stderr=stderr)

        self.assertEqual(len(calls), 2)
        self.assertEqual(stderr.getvalue().count("VPN sharing revocation check failed"), 1)
        self.assertIn("VPN sharing revocation check failed: RuntimeError: state database locked", stderr.getvalue())

    def test_revocation_interrupted_by_disconnect_error_completes_on_later_tick(self) -> None:
        self._import_shared_config()
        disconnect = mock.Mock(side_effect=[RuntimeError("pid lookup failed"), {"status": "not_running"}])

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", disconnect):
            self._run_live_ticks(live_ticks=2)

        self.assertEqual(disconnect.call_count, 2)
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertIn("no longer paired", state["revoked_reason"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())

    def test_revocation_with_failed_state_save_is_retried_and_completes(self) -> None:
        self._import_shared_config()
        real_save_state = vpn_manager._save_state
        save_calls: list[int] = []

        def flaky_save(settings, **updates):
            # The first save performed by the revocation (has_credentials=False)
            # fails after the credentials file was already removed.
            if updates.get("has_credentials") is False and not save_calls:
                save_calls.append(1)
                raise OSError("disk full")
            return real_save_state(settings, **updates)

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}), \
                mock.patch.object(vpn_manager, "_save_state", side_effect=flaky_save):
            self._run_live_ticks(live_ticks=2)

        self.assertEqual(len(save_calls), 1)
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertFalse(vpn_manager.auth_path(self.settings).exists())

    def test_transient_peer_errors_never_revoke_but_a_404_does(self) -> None:
        self._import_shared_config()
        outcomes = [
            URLError("peer unreachable"),
            HTTPError("https://peer/", 503, "Service Unavailable", None, None),
            HTTPError("https://peer/", 404, "Not Found", None, None),
        ]
        get_json = mock.Mock(side_effect=outcomes)
        peer = {"peer_id": _PEER_ID, "address": "203.0.113.5"}

        with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", get_json), \
                mock.patch.object(vpn_manager, "disconnect", return_value={"status": "not_running"}):
            self._run_live_ticks(live_ticks=2)
            after_transient = vpn_manager._load_state(self.settings)
            self.assertTrue(after_transient["has_credentials"])
            self._run_live_ticks(live_ticks=1)

        self.assertEqual(get_json.call_count, 3)
        state = vpn_manager._load_state(self.settings)
        self.assertFalse(state["has_credentials"])
        self.assertIn("turned off sharing", state["revoked_reason"])

    def test_system_exit_inside_real_check_still_propagates(self) -> None:
        self._import_shared_config()

        with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit):
                self._run_live_ticks(live_ticks=2)


if __name__ == "__main__":
    unittest.main()
