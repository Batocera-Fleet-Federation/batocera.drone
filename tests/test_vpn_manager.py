"""OpenVPN admin feature: config rewriting, credential storage, connect/
disconnect process management, and status detection.

Provider-agnostic by design (Proton VPN, NordVPN, PIA, ...) -- nothing here
assumes a specific provider's .ovpn wording beyond standard OpenVPN
directives.
"""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


def _build_settings(test_case: unittest.TestCase, root: Path) -> Settings:
    """Build an isolated Settings AND pin vpn_manager's install-root-relative
    vpn_dir() under this test's own tmp dir for its whole lifetime -- vpn_dir()
    is deliberately not part of Settings (see the feature spec: it is never a
    user-configurable location), so it must be patched directly rather than
    via an env var that would only be active during Settings.from_env()."""
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "vpn-test",
    }
    patcher = mock.patch.object(vpn_manager, "_drone_install_root", return_value=root / "install-root")
    test_case.addCleanup(patcher.stop)
    patcher.start()
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


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


class RewriteConfigTests(unittest.TestCase):
    def test_replaces_bare_auth_user_pass(self) -> None:
        out = vpn_manager.rewrite_ovpn_config(SAMPLE_OVPN, Path("/x/vpn/auth.txt"))
        self.assertIn("auth-user-pass /x/vpn/auth.txt", out)

    def test_replaces_auth_user_pass_with_existing_path(self) -> None:
        text = SAMPLE_OVPN.replace("auth-user-pass\n", "auth-user-pass /provider/creds.txt\n")
        out = vpn_manager.rewrite_ovpn_config(text, Path("/x/vpn/auth.txt"))
        self.assertIn("auth-user-pass /x/vpn/auth.txt", out)
        self.assertNotIn("/provider/creds.txt", out)

    def test_strips_update_resolv_conf_hooks(self) -> None:
        out = vpn_manager.rewrite_ovpn_config(SAMPLE_OVPN, Path("/x/vpn/auth.txt"))
        self.assertNotIn("update-resolv-conf", out)

    def test_adds_auth_nocache_when_missing(self) -> None:
        out = vpn_manager.rewrite_ovpn_config(SAMPLE_OVPN, Path("/x/vpn/auth.txt"))
        self.assertEqual(out.count("auth-nocache"), 1)

    def test_does_not_duplicate_existing_auth_nocache(self) -> None:
        text = SAMPLE_OVPN + "auth-nocache\n"
        out = vpn_manager.rewrite_ovpn_config(text, Path("/x/vpn/auth.txt"))
        self.assertEqual(out.count("auth-nocache"), 1)

    def test_preserves_embedded_certificate_blocks(self) -> None:
        text = SAMPLE_OVPN + "<ca>\n-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n</ca>\n"
        out = vpn_manager.rewrite_ovpn_config(text, Path("/x/vpn/auth.txt"))
        self.assertIn("<ca>", out)
        self.assertIn("FAKE", out)

    def test_rejects_config_with_no_remote_directive(self) -> None:
        with self.assertRaises(ValueError):
            vpn_manager.rewrite_ovpn_config("client\ndev tun\nauth-user-pass\n", Path("/x/auth.txt"))

    def test_parsed_remotes_extracts_all_remote_lines(self) -> None:
        text = "remote a.example.net 1194\nremote b.example.net 443\nauth-user-pass\n"
        self.assertEqual(vpn_manager.parsed_remotes(text), ["a.example.net 1194", "b.example.net 443"])


class SaveUploadedConfigTests(unittest.TestCase):
    def test_rejects_non_ovpn_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.save_uploaded_config(settings, "config.txt", SAMPLE_OVPN.encode())

    def test_rejects_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.save_uploaded_config(settings, "client.ovpn", b"")

    def test_rejects_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            oversized = b"remote x 1194\n" + b"#" * (vpn_manager.VPN_UPLOAD_MAX_BYTES + 1)
            with self.assertRaises(ValueError):
                vpn_manager.save_uploaded_config(settings, "client.ovpn", oversized)

    def test_rejects_non_utf8_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.save_uploaded_config(settings, "client.ovpn", b"\xff\xfe\x00garbage")

    def test_rejects_config_without_remote_directive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.save_uploaded_config(settings, "client.ovpn", b"client\ndev tun\n")

    def test_successful_upload_writes_file_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = vpn_manager.save_uploaded_config(settings, "ProtonVPN-US.ovpn", SAMPLE_OVPN.encode())
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["config_filename"], "ProtonVPN-US.ovpn")
            self.assertEqual(result["remotes"], ["vpn.example.net 1194"])
            written = vpn_manager.config_path(settings).read_text()
            self.assertIn(f"auth-user-pass {vpn_manager.auth_path(settings)}", written)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_config"])
            self.assertEqual(state["config_filename"], "ProtonVPN-US.ovpn")


class SaveCredentialsTests(unittest.TestCase):
    def test_rejects_empty_username_or_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.save_credentials(settings, "", "pw")
            with self.assertRaises(ValueError):
                vpn_manager.save_credentials(settings, "user", "")

    def test_writes_auth_file_with_600_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = vpn_manager.save_credentials(settings, "tokenuser", "tokenpass123")
            self.assertEqual(result["username"], "tokenuser")
            path = vpn_manager.auth_path(settings)
            self.assertEqual(path.read_text(), "tokenuser\ntokenpass123\n")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["username"], "tokenuser")

    def test_password_is_never_persisted_in_json_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_credentials(settings, "tokenuser", "super-secret-password")
            from app.storage.state_store import database_path, load_payload

            raw_state = load_payload(database_path(settings.userdata_root), vpn_manager.VPN_STATE_NAMESPACE, {})
            self.assertNotIn("super-secret-password", str(raw_state))


class ValidateReadyTests(unittest.TestCase):
    def test_reports_all_missing_pieces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value=None):
                errors = vpn_manager.validate_ready(settings)
            self.assertEqual(len(errors), 3)

    def test_ready_when_everything_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"):
                errors = vpn_manager.validate_ready(settings)
            self.assertEqual(errors, [])


def _write_fake_proc_process(proc_root: Path, pid: int, argv: list) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True)
    (process_dir / "cmdline").write_bytes(b"\x00".join(part.encode() for part in argv) + b"\x00")


class FindRunningOpenvpnTests(unittest.TestCase):
    def test_finds_process_matching_our_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            cfg = Path(tmp) / "vpn" / "client.ovpn"
            _write_fake_proc_process(proc_root, 555, ["/usr/sbin/openvpn", "--config", str(cfg), "--daemon"])
            pid = vpn_manager._find_running_openvpn_pid(cfg, proc_root=proc_root)
            self.assertEqual(pid, 555)

    def test_ignores_unrelated_openvpn_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            cfg = Path(tmp) / "vpn" / "client.ovpn"
            other_cfg = Path(tmp) / "someone-elses" / "other.ovpn"
            _write_fake_proc_process(proc_root, 555, ["/usr/sbin/openvpn", "--config", str(other_cfg)])
            self.assertIsNone(vpn_manager._find_running_openvpn_pid(cfg, proc_root=proc_root))

    def test_ignores_non_openvpn_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            cfg = Path(tmp) / "vpn" / "client.ovpn"
            _write_fake_proc_process(proc_root, 42, ["/usr/bin/python3", "--config", str(cfg)])
            self.assertIsNone(vpn_manager._find_running_openvpn_pid(cfg, proc_root=proc_root))

    def test_no_proc_directory_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                vpn_manager._find_running_openvpn_pid(Path(tmp) / "x.ovpn", proc_root=Path(tmp) / "nonexistent-proc")
            )


class ConnectDisconnectTests(unittest.TestCase):
    def _ready_settings(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        return settings

    def test_connect_short_circuits_on_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))  # no config/credentials
            with mock.patch.object(subprocess, "run") as run:
                result = vpn_manager.connect(settings)
            run.assert_not_called()
            self.assertEqual(result["status"], "error")
            self.assertTrue(result["errors"])

    def test_connect_spawns_openvpn_daemonized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                    mock.patch.object(vpn_manager.subprocess, "run", return_value=completed) as run:
                result = vpn_manager.connect(settings)
            self.assertEqual(result["status"], "connecting")
            args = run.call_args[0][0]
            self.assertEqual(args[0], "/usr/sbin/openvpn")
            self.assertIn("--daemon", args)
            self.assertIn(str(vpn_manager.config_path(settings)), args)

    def test_connect_reports_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=123), \
                    mock.patch.object(vpn_manager.subprocess, "run") as run:
                result = vpn_manager.connect(settings)
            run.assert_not_called()
            self.assertEqual(result["status"], "already_running")

    def test_connect_surfaces_openvpn_preflight_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            completed = mock.Mock(returncode=1, stdout="", stderr="Options error: bad directive\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                    mock.patch.object(vpn_manager.subprocess, "run", return_value=completed):
                result = vpn_manager.connect(settings)
            self.assertEqual(result["status"], "error")
            self.assertIn("Options error", result["errors"][0])

    def test_disconnect_when_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None):
                result = vpn_manager.disconnect(settings)
            self.assertEqual(result["status"], "not_running")

    def test_disconnect_sends_sigterm_and_confirms_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            pid_sequence = iter([777, None])
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=lambda *a, **k: next(pid_sequence)), \
                    mock.patch.object(vpn_manager.os, "kill") as kill:
                result = vpn_manager.disconnect(settings)
            self.assertEqual(result["status"], "disconnected")
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0], 777)

    def test_disconnect_escalates_to_sigkill_after_grace_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=888), \
                    mock.patch.object(vpn_manager, "VPN_DISCONNECT_GRACE_SECONDS", 0.01), \
                    mock.patch.object(vpn_manager.os, "kill") as kill, \
                    mock.patch.object(vpn_manager.time, "sleep"):
                vpn_manager.disconnect(settings)
            self.assertEqual(kill.call_count, 2)
            import signal

            self.assertEqual(kill.call_args_list[0].args, (888, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (888, signal.SIGKILL))


class AutoConnectTests(unittest.TestCase):
    def test_does_nothing_when_auto_start_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "connect") as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_not_called()

    def test_does_nothing_when_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.set_auto_start(settings, True)
            with mock.patch.object(vpn_manager, "connect") as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_not_called()

    def test_connects_when_enabled_and_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            vpn_manager.set_auto_start(settings, True)
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect") as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_called_once_with(settings)

    def test_never_raises_even_if_connect_blows_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            vpn_manager.set_auto_start(settings, True)
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", side_effect=RuntimeError("boom")):
                vpn_manager.maybe_auto_connect(settings)  # must not raise


class CheckPublicIpTests(unittest.TestCase):
    def test_returns_ip_on_success(self) -> None:
        completed = mock.Mock(returncode=0, stdout="203.0.113.5\n", stderr="")
        with mock.patch.object(vpn_manager.shutil, "which", return_value="/usr/bin/curl"), \
                mock.patch.object(vpn_manager.subprocess, "run", return_value=completed):
            result = vpn_manager.check_public_ip()
        self.assertEqual(result["ip"], "203.0.113.5")
        self.assertIn("checked_at", result)

    def test_reports_error_when_curl_missing(self) -> None:
        with mock.patch.object(vpn_manager.shutil, "which", return_value=None):
            result = vpn_manager.check_public_ip()
        self.assertIn("error", result)

    def test_reports_error_on_garbage_output(self) -> None:
        completed = mock.Mock(returncode=0, stdout="<html>not an ip</html>", stderr="")
        with mock.patch.object(vpn_manager.shutil, "which", return_value="/usr/bin/curl"), \
                mock.patch.object(vpn_manager.subprocess, "run", return_value=completed):
            result = vpn_manager.check_public_ip()
        self.assertIn("error", result)


class StatusTests(unittest.TestCase):
    def _settings_with_running_process(self, tmp: str, log_text: str):
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
        vpn_manager.log_path(settings).write_text(log_text, encoding="utf-8")
        return settings

    def test_disconnected_when_no_process_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None):
                result = vpn_manager.status(settings)
            self.assertEqual(result["status"], "disconnected")
            self.assertIsNone(result["connected_at"])

    def test_connecting_when_process_running_but_not_yet_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "Mon Jan 1 00:00:00 2026 UDPv4 link local\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999):
                result = vpn_manager.status(settings)
            self.assertEqual(result["status"], "connecting")

    def test_connected_when_log_shows_completion_and_persists_connected_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "Initialization Sequence Completed\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                result = vpn_manager.status(settings)
            self.assertEqual(result["status"], "connected")
            self.assertEqual(result["tunnel_ip"], "10.8.0.2")
            self.assertIsNotNone(result["connected_at"])
            self.assertIsNotNone(result["connected_duration_seconds"])
            # A second call must not move connected_at forward.
            first_connected_at = result["connected_at"]
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                second = vpn_manager.status(settings)
            self.assertEqual(second["connected_at"], first_connected_at)

    def test_error_when_log_shows_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "AUTH_FAILED\nExiting due to fatal error\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999):
                result = vpn_manager.status(settings)
            self.assertEqual(result["status"], "error")
            self.assertIn("AUTH_FAILED", result["message"])

    def test_disconnecting_clears_previously_persisted_connected_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "Initialization Sequence Completed\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                connected = vpn_manager.status(settings)
            self.assertIsNotNone(connected["connected_at"])
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None):
                disconnected = vpn_manager.status(settings)
            self.assertEqual(disconnected["status"], "disconnected")
            self.assertIsNone(disconnected["connected_at"])

    def test_validation_errors_included_in_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value=None):
                result = vpn_manager.status(settings)
            self.assertTrue(result["validation_errors"])
            self.assertFalse(result["installed"])


if __name__ == "__main__":
    unittest.main()
