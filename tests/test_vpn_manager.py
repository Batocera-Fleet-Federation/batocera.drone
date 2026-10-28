"""OpenVPN admin feature: config rewriting, credential storage, connect/
disconnect process management, and status detection.

Provider-agnostic by design (Proton VPN, NordVPN, PIA, ...) -- nothing here
assumes a specific provider's .ovpn wording beyond standard OpenVPN
directives.
"""

import io
import stat
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import List
from unittest import mock
from urllib.error import HTTPError

from app.transfer.peer_connectivity import _PEER_TLS_EVIDENCE

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

    def test_parsed_protocol_reads_top_level_proto(self) -> None:
        self.assertEqual(vpn_manager.parsed_protocol("proto tcp-client\nremote vpn.example.net 443\n"), "TCP")

    def test_parsed_protocol_reads_remote_protocols(self) -> None:
        text = "remote a.example.net 443 tcp4-client\nremote b.example.net 1194 udp4\n"
        self.assertEqual(vpn_manager.parsed_protocol(text), "TCP / UDP")

    def test_parsed_protocol_uses_openvpn_udp_default(self) -> None:
        self.assertEqual(vpn_manager.parsed_protocol("remote vpn.example.net 1194\n"), "UDP")


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
            self.assertEqual(result["protocol"], "UDP")
            written = vpn_manager.config_path(settings).read_text()
            self.assertIn(f"auth-user-pass {vpn_manager.auth_path(settings)}", written)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_config"])
            self.assertEqual(state["config_filename"], "ProtonVPN-US.ovpn")


class VpnImportDirTests(unittest.TestCase):
    def test_resolves_under_userdata_root_distinct_from_vpn_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            import_dir = vpn_manager.vpn_import_dir(settings)
            self.assertEqual(import_dir, Path(tmp) / "vpn-import")
            self.assertNotEqual(import_dir, vpn_manager.vpn_dir(settings))

    def test_env_override_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            custom = Path(tmp) / "custom-import-dir"
            with mock.patch.dict("os.environ", {"DRONE_VPN_IMPORT_DIR": str(custom)}):
                self.assertEqual(vpn_manager.vpn_import_dir(settings), custom.resolve())


class ListImportFilesTests(unittest.TestCase):
    def test_auto_creates_the_directory_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            self.assertFalse(vpn_manager.vpn_import_dir(settings).exists())
            result = vpn_manager.list_import_files(settings)
            self.assertTrue(vpn_manager.vpn_import_dir(settings).is_dir())
            self.assertEqual(result["files"], [])

    def test_only_returns_ovpn_files_sorted_ignoring_subdirs_and_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            (directory / "zprovider.OVPN").write_text("remote a 1194\n")
            (directory / "aprovider.ovpn").write_text("remote b 1194\n")
            (directory / "readme.txt").write_text("not a config\n")
            (directory / "subdir").mkdir()
            result = vpn_manager.list_import_files(settings)
            self.assertEqual(result["files"], ["aprovider.ovpn", "zprovider.OVPN"])
            self.assertEqual(result["directory"], str(directory))


class ImportFromFolderTests(unittest.TestCase):
    def test_happy_path_matches_a_direct_save_uploaded_config_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            (directory / "provider.ovpn").write_bytes(SAMPLE_OVPN.encode())

            result = vpn_manager.import_from_folder(settings, "provider.ovpn")

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["config_filename"], "provider.ovpn")
            self.assertEqual(result["remotes"], ["vpn.example.net 1194"])
            written = vpn_manager.config_path(settings).read_text()
            self.assertIn(f"auth-user-pass {vpn_manager.auth_path(settings)}", written)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_config"])

    def test_rejects_a_traversal_filename_and_touches_nothing_outside_the_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            outside_secret = Path(tmp) / "outside-secret.ovpn"
            outside_secret.write_bytes(SAMPLE_OVPN.encode())

            with self.assertRaises(FileNotFoundError):
                vpn_manager.import_from_folder(settings, "../outside-secret.ovpn")
            with self.assertRaises(FileNotFoundError):
                vpn_manager.import_from_folder(settings, "/etc/passwd")

            state = vpn_manager._load_state(settings)
            self.assertFalse(state["has_config"])
            self.assertFalse(vpn_manager.config_path(settings).exists())

    def test_unknown_filename_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(FileNotFoundError):
                vpn_manager.import_from_folder(settings, "does-not-exist.ovpn")

    def test_propagates_the_no_remote_directive_error_same_as_a_bad_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            directory = vpn_manager.vpn_import_dir(settings)
            directory.mkdir(parents=True)
            (directory / "bad.ovpn").write_text("client\ndev tun\n")
            with self.assertRaises(ValueError):
                vpn_manager.import_from_folder(settings, "bad.ovpn")


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


class ProcessStartTimeTests(unittest.TestCase):
    def test_live_proc_uses_fractional_boot_time_including_suspend(self) -> None:
        # Field 22 is 100.1 seconds since boot. The comm field can contain ')'.
        stat = "99 (openvpn (client)) " + " ".join(["0"] * 19 + ["10010"])
        with mock.patch.object(Path, "read_text", return_value=stat), \
                mock.patch.object(vpn_manager.time, "time", return_value=1000.75), \
                mock.patch.object(vpn_manager.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(vpn_manager.time, "clock_gettime", return_value=100.25) as clock, \
                mock.patch.object(vpn_manager.os, "sysconf", return_value=100):
            self.assertAlmostEqual(vpn_manager._process_started_at(99), 1000.6)
            clock.assert_called_once_with(7)
            self.assertTrue(vpn_manager._started_after_revocation(99, 1000.5))

    def test_unavailable_boot_clock_leaves_timing_unknown(self) -> None:
        stat = "99 (openvpn) " + " ".join(["0"] * 19 + ["10010"])
        with mock.patch.object(Path, "read_text", return_value=stat), \
                mock.patch.object(vpn_manager.time, "CLOCK_BOOTTIME", 7, create=True), \
                mock.patch.object(vpn_manager.time, "clock_gettime", side_effect=OSError("unavailable")):
            self.assertIsNone(vpn_manager._process_started_at(99))

    def test_revocation_boundary_has_no_grace_window(self) -> None:
        for started, expected in ((999.9, False), (1000.0, False),
                                  (1000.001, True), (1000.1, True), (None, False)):
            with self.subTest(started=started), \
                    mock.patch.object(vpn_manager, "_process_started_at", return_value=started):
                self.assertEqual(vpn_manager._started_after_revocation(99, 1000.0), expected)


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
                    mock.patch.object(vpn_manager, "VPN_DISCONNECT_KILL_WAIT_SECONDS", 0.0), \
                    mock.patch.object(vpn_manager.os, "kill") as kill, \
                    mock.patch.object(vpn_manager.time, "sleep"):
                result = vpn_manager.disconnect(settings)
            self.assertEqual(result["status"], "error")
            self.assertEqual(kill.call_count, 2)
            import signal

            self.assertEqual(kill.call_args_list[0].args, (888, signal.SIGTERM))
            self.assertEqual(kill.call_args_list[1].args, (888, signal.SIGKILL))

    def test_disconnect_confirms_exit_after_sigkill(self) -> None:
        for signal_error in (None, ProcessLookupError("already exited")):
            with self.subTest(signal_error=signal_error), tempfile.TemporaryDirectory() as tmp:
                settings = self._ready_settings(tmp)
                with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=[888, 888, None]), \
                        mock.patch.object(vpn_manager, "VPN_DISCONNECT_GRACE_SECONDS", 0.0), \
                        mock.patch.object(vpn_manager.os, "kill", side_effect=[None, signal_error]), \
                        mock.patch.object(vpn_manager.time, "sleep") as sleep:
                    result = vpn_manager.disconnect(settings)
                self.assertEqual(result["status"], "disconnected")
                sleep.assert_called_once_with(0.25)

    def test_disconnect_sigkill_failure_does_not_report_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            vpn_manager._save_state(settings, connected_at="still-connected")
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=888), \
                    mock.patch.object(vpn_manager, "VPN_DISCONNECT_GRACE_SECONDS", 0.0), \
                    mock.patch.object(vpn_manager.os, "kill", side_effect=[None, PermissionError("denied")]):
                result = vpn_manager.disconnect(settings)
            self.assertEqual(result["status"], "error")
            self.assertIn("denied", result["errors"][0])
            self.assertEqual(vpn_manager._load_state(settings)["connected_at"], "still-connected")


class AutoConnectTests(unittest.TestCase):
    """maybe_auto_connect has no opt-in toggle -- being configured (has_config +
    has_credentials + openvpn installed) is the only condition. See
    SharingProvenanceGateTests etc. for the unrelated sharing_enabled gate,
    which only affects P2P export, not this boot-time connect behavior.

    bootstrap_vpn_from_swarm() is only ever invoked here, and only when this
    drone has no usable config of its own -- see BootstrapVpnFromSwarmTests
    for that function's own peer-search/skip/import behavior in isolation."""

    def test_does_nothing_when_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "connect") as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_not_called()

    def test_connects_when_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", return_value={"status": "connecting"}) as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_called_once_with(settings)

    def test_retries_on_transient_failure_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            results = [
                {"status": "error", "errors": ["network not ready"]},
                {"status": "error", "errors": ["network not ready"]},
                {"status": "connecting"},
            ]
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", side_effect=results) as connect, \
                    mock.patch.object(vpn_manager, "time") as fake_time:
                vpn_manager.maybe_auto_connect(settings)
            self.assertEqual(connect.call_count, 3)
            self.assertEqual(fake_time.sleep.call_count, 2)  # between attempts only, never after the final one

    def test_already_running_short_circuits_like_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", return_value={"status": "already_running"}) as connect:
                vpn_manager.maybe_auto_connect(settings)
            connect.assert_called_once()

    def test_gives_up_after_max_attempts_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "VPN_AUTO_CONNECT_MAX_ATTEMPTS", 3), \
                    mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", return_value={"status": "error", "errors": ["auth failed"]}) as connect, \
                    mock.patch.object(vpn_manager, "time"):
                vpn_manager.maybe_auto_connect(settings)  # must not raise
            self.assertEqual(connect.call_count, 3)

    def test_never_raises_even_if_connect_blows_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", side_effect=RuntimeError("boom")) as connect:
                vpn_manager.maybe_auto_connect(settings)  # must not raise
            # A raised (not returned) failure aborts the loop rather than retrying blindly.
            connect.assert_called_once()

    def test_swarm_bootstrap_not_attempted_when_already_ready(self) -> None:
        # Never override an existing local configuration, deliberate or not.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", return_value={"status": "connecting"}), \
                    mock.patch.object(vpn_manager, "bootstrap_vpn_from_swarm") as bootstrap:
                vpn_manager.maybe_auto_connect(settings)
            bootstrap.assert_not_called()

    def test_swarm_bootstrap_attempted_when_not_ready_and_nothing_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "bootstrap_vpn_from_swarm", return_value=False) as bootstrap, \
                    mock.patch.object(vpn_manager, "connect") as connect:
                vpn_manager.maybe_auto_connect(settings)
            bootstrap.assert_called_once_with(settings)
            connect.assert_not_called()  # still nothing usable -- nothing to connect

    def test_connects_after_a_successful_swarm_bootstrap(self) -> None:
        # bootstrap_vpn_from_swarm's real job is writing a usable local config
        # (via import_from_peer) as a side effect -- simulate that directly so
        # the second validate_ready() check genuinely passes, exercising the
        # real fall-through into the connect loop below it.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))

            def fake_bootstrap(_settings) -> bool:
                vpn_manager.import_from_peer(
                    _settings,
                    {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": True, "username": "peeruser", "password": "peerpass123"},
                    source_peer_id="peer-1", source_peer_name="Peer One",
                )
                return True

            with mock.patch.object(vpn_manager, "bootstrap_vpn_from_swarm", side_effect=fake_bootstrap) as bootstrap, \
                    mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "connect", return_value={"status": "connecting"}) as connect:
                vpn_manager.maybe_auto_connect(settings)
            bootstrap.assert_called_once_with(settings)
            connect.assert_called_once_with(settings)
            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "peer-1")


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

    def test_status_reports_saved_profile_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            tcp_profile = SAMPLE_OVPN.replace("proto udp", "proto tcp-client")
            vpn_manager.save_uploaded_config(settings, "proton-tcp.ovpn", tcp_profile.encode())
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None):
                result = vpn_manager.status(settings)
            self.assertEqual(result["protocol"], "TCP")

    def test_tunnel_is_up_requires_managed_process_and_interface_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                self.assertTrue(vpn_manager.tunnel_is_up(settings))

    def test_tunnel_is_up_fails_closed_without_process_or_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                self.assertFalse(vpn_manager.tunnel_is_up(settings))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value=None):
                self.assertFalse(vpn_manager.tunnel_is_up(settings))

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

    def test_live_tunnel_remains_connected_after_success_marker_scrolls_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            replay_flood = "\n".join(
                f"AEAD Decrypt error: bad packet ID (may be a replay): [ #{i} ]" for i in range(500)
            )
            settings = self._settings_with_running_process(tmp, replay_flood)
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                result = vpn_manager.status(settings)
            self.assertEqual(result["status"], "connected")
            self.assertEqual(result["tunnel_ip"], "10.8.0.2")

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


class ConnectedDisconnectedNotificationTests(unittest.TestCase):
    """status() must fire vpn_connected/vpn_disconnected exactly once per
    genuine transition -- never on a repeated poll of an unchanged status
    (the admin UI polls status() every 3s)."""

    def _settings_with_running_process(self, tmp: str, log_text: str):
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
        vpn_manager.log_path(settings).write_text(log_text, encoding="utf-8")
        return settings

    def test_first_ever_disconnected_status_does_not_fire_disconnected(self) -> None:
        # No prior status recorded yet -- this is not a real "it just went
        # down" transition, just the first observation ever.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                    mock.patch.object(vpn_manager, "_notifications") as fake_notifications:
                vpn_manager.status(settings)
            fake_notifications.record_event.assert_not_called()

    def test_connecting_fires_once_and_not_again_while_still_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "Initialization Sequence Completed\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"), \
                    mock.patch.object(vpn_manager, "_notifications") as fake_notifications:
                vpn_manager.status(settings)
                vpn_manager.status(settings)
                vpn_manager.status(settings)
            fake_notifications.record_event.assert_called_once()
            self.assertEqual(fake_notifications.record_event.call_args[0][1], "vpn_connected")

    def test_disconnecting_after_connected_fires_disconnected_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_running_process(tmp, "Initialization Sequence Completed\n")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                vpn_manager.status(settings)  # establishes "connected" as the baseline, unmocked notifications
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=None), \
                    mock.patch.object(vpn_manager, "_notifications") as fake_notifications:
                vpn_manager.status(settings)
                vpn_manager.status(settings)
            fake_notifications.record_event.assert_called_once()
            self.assertEqual(fake_notifications.record_event.call_args[0][1], "vpn_disconnected")


class SharingEnabledTests(unittest.TestCase):
    def test_defaults_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            self.assertFalse(vpn_manager.status(settings)["sharing_enabled"])

    def test_set_sharing_enabled_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = vpn_manager.set_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])
            self.assertTrue(vpn_manager.status(settings)["sharing_enabled"])
            self.assertTrue(vpn_manager._load_state(settings)["sharing_enabled"])


class ExportPayloadTests(unittest.TestCase):
    def test_none_when_sharing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            self.assertIsNone(vpn_manager.export_payload(settings))

    def test_none_when_no_config_even_if_sharing_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.set_sharing_enabled(settings, True)
            self.assertIsNone(vpn_manager.export_payload(settings))

    def test_returns_config_and_credentials_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "ProtonVPN-US.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "tokenuser", "tokenpass123")
            vpn_manager.set_sharing_enabled(settings, True)
            payload = vpn_manager.export_payload(settings)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["config_filename"], "ProtonVPN-US.ovpn")
            self.assertEqual(payload["remotes"], ["vpn.example.net 1194"])
            self.assertIn(f"auth-user-pass {vpn_manager.auth_path(settings)}", payload["config_text"])
            self.assertTrue(payload["has_credentials"])
            self.assertEqual(payload["username"], "tokenuser")
            self.assertEqual(payload["password"], "tokenpass123")
            self.assertFalse(payload["connected"])  # nothing actually running in this test

    def test_connected_true_when_tunnel_is_actually_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "user", "pass")
            vpn_manager.set_sharing_enabled(settings, True)
            vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
            vpn_manager.log_path(settings).write_text("Initialization Sequence Completed\n", encoding="utf-8")
            with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=999), \
                    mock.patch.object(vpn_manager, "_tunnel_ip", return_value="10.8.0.2"):
                payload = vpn_manager.export_payload(settings)
            self.assertIsNotNone(payload)
            self.assertTrue(payload["connected"])

    def test_credentials_excluded_when_not_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.set_sharing_enabled(settings, True)
            payload = vpn_manager.export_payload(settings)
            self.assertIsNotNone(payload)
            self.assertFalse(payload["has_credentials"])
            self.assertNotIn("username", payload)
            self.assertNotIn("password", payload)


class ImportFromPeerTests(unittest.TestCase):
    def test_rejects_missing_config_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.import_from_peer(settings, {"config_filename": "client.ovpn"}, source_peer_id="peer-1")

    def test_rejects_missing_source_peer_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with self.assertRaises(ValueError):
                vpn_manager.import_from_peer(settings, {"config_text": SAMPLE_OVPN}, source_peer_id="")

    def test_reimports_auth_user_pass_to_local_auth_path_not_peers(self) -> None:
        # The peer's own exported config_text already points auth-user-pass at
        # *their* install-root auth.txt -- importing must re-rewrite it to point
        # at *our* local auth_path(), the same way a fresh upload would, since
        # save_uploaded_config (which import_from_peer delegates to) re-runs
        # rewrite_ovpn_config unconditionally.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            foreign_text = SAMPLE_OVPN.replace("auth-user-pass\n", "auth-user-pass /some/other/drone/vpn/auth.txt\n")
            peer_payload = {
                "config_filename": "PeerShared.ovpn",
                "config_text": foreign_text,
                "remotes": ["vpn.example.net 1194"],
                "has_credentials": False,
            }
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            written = vpn_manager.config_path(settings).read_text()
            self.assertIn(f"auth-user-pass {vpn_manager.auth_path(settings)}", written)
            self.assertNotIn("/some/other/drone/vpn/auth.txt", written)

    def test_imports_credentials_when_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {
                "config_filename": "client.ovpn",
                "config_text": SAMPLE_OVPN,
                "has_credentials": True,
                "username": "peeruser",
                "password": "peerpass123",
            }
            result = vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            self.assertTrue(result["credentials_imported"])
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["username"], "peeruser")
            self.assertEqual(vpn_manager.auth_path(settings).read_text(), "peeruser\npeerpass123\n")

    def test_skips_credentials_when_not_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {
                "config_filename": "client.ovpn",
                "config_text": SAMPLE_OVPN,
                "has_credentials": False,
            }
            result = vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1")
            self.assertFalse(result["credentials_imported"])
            self.assertFalse(vpn_manager._load_state(settings)["has_credentials"])

    def test_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": False}
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            state = vpn_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["source_peer_name"], "Peer One")

    def test_manual_upload_after_import_clears_provenance(self) -> None:
        # A genuine fresh manual upload always resets provenance to
        # "self-owned" -- this is how a drone can go from "imported, can't
        # share" back to being a real source of its own.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": False}
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "peer-1")
            vpn_manager.save_uploaded_config(settings, "MyOwn.ovpn", SAMPLE_OVPN.encode())
            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "")


class SharingProvenanceGateTests(unittest.TestCase):
    def test_set_sharing_enabled_rejected_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": False}
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            with self.assertRaises(ValueError):
                vpn_manager.set_sharing_enabled(settings, True)
            self.assertFalse(vpn_manager._load_state(settings)["sharing_enabled"])

    def test_disabling_sharing_is_always_allowed_even_for_imported_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": False}
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            result = vpn_manager.set_sharing_enabled(settings, False)
            self.assertFalse(result["sharing_enabled"])

    def test_export_payload_none_for_imported_config_even_if_sharing_somehow_enabled(self) -> None:
        # Defense in depth: export_payload re-checks provenance itself rather
        # than trusting the sharing_enabled flag alone.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": False}
            vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
            vpn_manager._save_state(settings, sharing_enabled=True)  # bypass the normal setter on purpose
            self.assertIsNone(vpn_manager.export_payload(settings))

    def test_self_uploaded_config_can_be_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            result = vpn_manager.set_sharing_enabled(settings, True)
            self.assertTrue(result["sharing_enabled"])


class CheckSharingRevocationTests(unittest.TestCase):
    def test_unlink_failure_disables_credentials_and_retries_cleanup_without_peer(self) -> None:
        for stop_result in ({"status": "error"}, RuntimeError("stop failed"), {"status": "disconnected"}):
            with self.subTest(stop_result=stop_result), tempfile.TemporaryDirectory() as tmp:
                settings = self._imported_settings(tmp)
                auth = vpn_manager.auth_path(settings)
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                        mock.patch.object(vpn_manager, "disconnect", side_effect=[stop_result]), \
                        mock.patch.object(Path, "unlink", side_effect=OSError("busy")), \
                        mock.patch.object(vpn_manager, "_log_poller_failure"):
                    self.assertTrue(vpn_manager.check_sharing_revocation(settings))
                state = vpn_manager._load_state(settings)
                self.assertTrue(auth.exists())
                self.assertFalse(state["has_credentials"])
                self.assertTrue(state["credential_wipe_pending"])
                self.assertEqual(state["disconnect_retry_pending"], stop_result != {"status": "disconnected"})
                with mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="openvpn"):
                    self.assertTrue(vpn_manager.validate_ready(settings))

                # A successful stop does not discard a still-failing file wipe.
                with mock.patch("app.transfer.local_network.get_paired_peer") as peer, \
                        mock.patch.object(vpn_manager, "disconnect", return_value={"status": "disconnected"}), \
                        mock.patch.object(Path, "unlink", side_effect=OSError("still busy")), \
                        mock.patch.object(vpn_manager, "_log_poller_failure"):
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                    peer.assert_not_called()
                self.assertFalse(vpn_manager._load_state(settings)["disconnect_retry_pending"])
                self.assertTrue(vpn_manager._load_state(settings)["credential_wipe_pending"])

                with mock.patch("app.transfer.local_network.get_paired_peer") as peer, \
                        mock.patch.object(vpn_manager, "disconnect") as disconnect:
                    self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                    peer.assert_not_called()
                    disconnect.assert_not_called()
                self.assertFalse(auth.exists())
                after = vpn_manager._load_state(settings)
                self.assertFalse(after["credential_wipe_pending"])
                self.assertEqual(after["revoked_at"], state["revoked_at"])

    def test_retry_does_not_stop_newer_tunnel_after_revoked_process_exited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            failed = {"status": "error", "pid": 42, "errors": ["Failed to stop openvpn (pid 42): denied"]}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", return_value=failed), \
                    mock.patch.object(vpn_manager, "_log_poller_failure"):
                vpn_manager.check_sharing_revocation(settings)
            self.assertEqual(vpn_manager._load_state(settings)["disconnect_retry_pid"], 42)
            # The revoked pid is gone; a different (newly imported) tunnel runs as pid 99.
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=99), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect:
                vpn_manager.check_sharing_revocation(settings)
                disconnect.assert_not_called()
            state = vpn_manager._load_state(settings)
            self.assertFalse(state["disconnect_retry_pending"])
            self.assertIsNone(state["disconnect_retry_pid"])

    def test_stop_raising_before_pid_report_still_binds_retry_to_revoked_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            # disconnect() finds pid 42, then the signal call raises a non-OSError.
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=42), \
                    mock.patch.object(vpn_manager.os, "kill", side_effect=RuntimeError("signal failed")), \
                    mock.patch.object(vpn_manager, "_log_poller_failure"):
                vpn_manager.check_sharing_revocation(settings)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["disconnect_retry_pending"])
            self.assertEqual(state["disconnect_retry_pid"], 42)
            # A newly imported tunnel (pid 99) must not be stopped by the retry.
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=99), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect:
                vpn_manager.check_sharing_revocation(settings)
                disconnect.assert_not_called()
            self.assertFalse(vpn_manager._load_state(settings)["disconnect_retry_pending"])

    def test_pidless_retry_skips_process_started_after_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("boom")), \
                    mock.patch.object(vpn_manager, "_log_poller_failure"):
                vpn_manager.check_sharing_revocation(settings)
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["disconnect_retry_pending"])
            self.assertIsNone(state["disconnect_retry_pid"])
            since = state["disconnect_retry_since"]
            self.assertIsNotNone(since)
            # A tunnel that began after the revocation is newer and is left alone.
            with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=99), \
                    mock.patch.object(vpn_manager, "_process_started_at", return_value=since + 60), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect:
                vpn_manager.check_sharing_revocation(settings)
                disconnect.assert_not_called()
            self.assertFalse(vpn_manager._load_state(settings)["disconnect_retry_pending"])

    def test_pidless_retry_still_stops_process_that_predates_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                    mock.patch.object(vpn_manager, "disconnect", side_effect=RuntimeError("boom")), \
                    mock.patch.object(vpn_manager, "_log_poller_failure"):
                vpn_manager.check_sharing_revocation(settings)
            since = vpn_manager._load_state(settings)["disconnect_retry_since"]
            for started in (since - 3600, None):  # older, or timing unknown: fail closed
                with mock.patch.object(vpn_manager, "_find_running_openvpn_pid", return_value=99), \
                        mock.patch.object(vpn_manager, "_process_started_at", return_value=started), \
                        mock.patch.object(vpn_manager, "disconnect", return_value={"status": "error", "pid": 99, "errors": ["x"]}) as disconnect, \
                        mock.patch.object(vpn_manager, "_log_poller_failure"):
                    vpn_manager.check_sharing_revocation(settings)
                    disconnect.assert_called_once()
                state = vpn_manager._load_state(settings)
                self.assertTrue(state["disconnect_retry_pending"])
                self.assertEqual(state["disconnect_retry_pid"], 99)

    def test_pending_wipe_does_not_remove_fresh_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch.object(vpn_manager, "disconnect", return_value={"status": "error"}), \
                    mock.patch.object(Path, "unlink", side_effect=OSError("busy")), \
                    mock.patch.object(vpn_manager, "_log_poller_failure"):
                with self.assertRaises(OSError):
                    vpn_manager._revoke_local_credentials(settings, "revoked")
            vpn_manager.save_uploaded_config(settings, "fresh.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "fresh-user", "fresh-password")
            with mock.patch.object(vpn_manager, "disconnect", return_value={"status": "disconnected"}) as disconnect:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                disconnect.assert_called_once()
            self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])
            self.assertIn("fresh-user", vpn_manager.auth_path(settings).read_text())

    def _imported_settings(self, tmp: str, *, with_credentials: bool = True):
        settings = _build_settings(self, Path(tmp))
        peer_payload = {
            "config_filename": "client.ovpn",
            "config_text": SAMPLE_OVPN,
            "has_credentials": with_credentials,
            "username": "peeruser",
            "password": "peerpass123",
        }
        vpn_manager.import_from_peer(settings, peer_payload, source_peer_id="peer-1", source_peer_name="Peer One")
        return settings

    def test_noop_when_config_is_self_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
            vpn_manager.save_credentials(settings, "me", "mypass")
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_noop_when_no_credentials_to_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp, with_credentials=False)
            with mock.patch("app.transfer.local_network.get_paired_peer") as get_peer:
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                get_peer.assert_not_called()

    def test_revokes_when_source_peer_no_longer_paired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            state = vpn_manager._load_state(settings)
            self.assertFalse(state["has_credentials"])
            self.assertIn("no longer paired", state["revoked_reason"])
            self.assertEqual(state["source_peer_id"], "peer-1")  # provenance persists

    def test_revokes_when_peer_returns_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
            error._peer_tls_evidence = _PEER_TLS_EVIDENCE
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertTrue(vpn_manager.check_sharing_revocation(settings))
            state = vpn_manager._load_state(settings)
            self.assertFalse(state["has_credentials"])
            self.assertEqual(state["username"], "")
            self.assertFalse(vpn_manager.auth_path(settings).exists())
            self.assertIn("turned off sharing", state["revoked_reason"])
            self.assertIsNotNone(state["revoked_at"])
            # The config file + provenance survive so it can never become shareable.
            self.assertTrue(vpn_manager.config_path(settings).is_file())
            self.assertEqual(state["source_peer_id"], "peer-1")

    def test_does_not_revoke_on_plaintext_http_404(self) -> None:
        # An http_only route is unauthenticated: an on-path 404 must not wipe anything.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("http://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
            error.peer_route = "http://peer"
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["revoked_reason"], "")

    def test_redirected_plaintext_404_does_not_revoke(self) -> None:
        # The error URL may claim https after a redirect; pinned TLS evidence is required.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://elsewhere/v1/api/peer/vpn/config", 404, "not found", None, None)
            error.peer_route = "http://peer"
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            self.assertEqual(vpn_manager._load_state(settings)["revoked_reason"], "")

    def test_stale_revocation_preserves_replacement_credentials(self) -> None:
        # Import on another thread while the peer request/lookup is in flight.
        # Waiting for it here also proves network I/O does not hold the lock.
        for peer_gone in (True, False):
            for manual_upload in (True, False):
                with self.subTest(peer_gone=peer_gone, manual_upload=manual_upload), tempfile.TemporaryDirectory() as tmp:
                    settings = self._imported_settings(tmp)
                    payload = {
                        "config_filename": "replacement.ovpn", "config_text": SAMPLE_OVPN,
                        "has_credentials": True, "username": "new-user", "password": "new-pass",
                    }

                    def replace_credentials():
                        if manual_upload:
                            vpn_manager.save_uploaded_config(settings, "replacement.ovpn", SAMPLE_OVPN.encode())
                            vpn_manager.save_credentials(settings, "new-user", "new-pass")
                        else:
                            vpn_manager.import_from_peer(settings, payload, source_peer_id="peer-2")

                    with ThreadPoolExecutor(max_workers=1) as executor:
                        def stale_answer(*args, **kwargs):
                            executor.submit(replace_credentials).result(timeout=5)
                            if peer_gone:
                                return None
                            error = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
                            error._peer_tls_evidence = _PEER_TLS_EVIDENCE
                            raise error

                        with mock.patch("app.transfer.local_network.get_paired_peer",
                                        side_effect=stale_answer if peer_gone else None,
                                        return_value={"drone_id": "peer-1"}), \
                                mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=stale_answer), \
                                mock.patch.object(vpn_manager, "disconnect") as disconnect:
                            self.assertFalse(vpn_manager.check_sharing_revocation(settings))
                        disconnect.assert_not_called()

                    state = vpn_manager._load_state(settings)
                    self.assertEqual(state["source_peer_id"], "" if manual_upload else "peer-2")
                    self.assertTrue(state["has_credentials"])
                    self.assertEqual(state["username"], "new-user")
                    self.assertEqual(state["revoked_reason"], "")
                    self.assertIsNone(state["revoked_at"])
                    self.assertEqual(vpn_manager.auth_path(settings).read_text(), "new-user\nnew-pass\n")

    def test_concurrent_state_updates_do_not_restore_stale_fields(self) -> None:
        # A slow field-only write (the status poll) must not clobber provenance
        # that another writer saved while it was in flight.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            in_save = Event()
            release = Event()
            real_save = vpn_manager._save_state_payload

            def slow_save(*args, **kwargs):
                if not in_save.is_set():
                    in_save.set()
                    self.assertTrue(release.wait(timeout=5))
                return real_save(*args, **kwargs)

            with ThreadPoolExecutor(max_workers=2) as executor, \
                    mock.patch.object(vpn_manager, "_save_state_payload", side_effect=slow_save):
                status_write = executor.submit(vpn_manager._save_state, settings, connected_at=None)
                self.assertTrue(in_save.wait(timeout=5))
                provenance_write = executor.submit(vpn_manager._save_state, settings, source_peer_id="peer-2")
                release.set()
                status_write.result(timeout=5)
                provenance_write.result(timeout=5)

            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "peer-2")

    def test_import_waits_for_revocation_cleanup(self) -> None:
        # The compare and wipe must be one critical section: an import that
        # arrives during disconnect must finish after the old credentials go.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            started = Event()
            finished = Event()
            payload = {
                "config_filename": "replacement.ovpn", "config_text": SAMPLE_OVPN,
                "has_credentials": True, "username": "new-user", "password": "new-pass",
            }

            def replace_credentials():
                started.set()
                try:
                    vpn_manager.import_from_peer(settings, payload, source_peer_id="peer-2")
                finally:
                    finished.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = []

                def disconnect_during_import(_settings):
                    pending.append(executor.submit(replace_credentials))
                    self.assertTrue(started.wait(timeout=5))
                    self.assertFalse(finished.wait(timeout=0.1))

                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=None), \
                        mock.patch.object(vpn_manager, "disconnect", side_effect=disconnect_during_import):
                    self.assertTrue(vpn_manager.check_sharing_revocation(settings))
                pending[0].result(timeout=5)

            state = vpn_manager._load_state(settings)
            self.assertEqual(state["source_peer_id"], "peer-2")
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["revoked_reason"], "")
            self.assertEqual(vpn_manager.auth_path(settings).read_text(), "new-user\nnew-pass\n")

    def test_disconnect_exception_does_not_block_revocation_cleanup(self) -> None:
        for peer_gone in (True, False):
            with self.subTest(peer_gone=peer_gone), tempfile.TemporaryDirectory() as tmp:
                settings = self._imported_settings(tmp)
                peer = None if peer_gone else {"drone_id": "peer-1"}
                rejection = HTTPError("https://peer/v1/api/peer/vpn/config", 404, "not found", None, None)
                rejection._peer_tls_evidence = _PEER_TLS_EVIDENCE
                failure = RuntimeError("process table unreadable")
                with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                        mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=rejection), \
                        mock.patch.object(vpn_manager, "_find_running_openvpn_pid", side_effect=failure), \
                        mock.patch.object(vpn_manager, "_log_poller_failure") as log_failure:
                    vpn_manager.check_sharing_revocation(settings)

                self.assertFalse(vpn_manager.auth_path(settings).exists())
                state = vpn_manager._load_state(settings)
                self.assertFalse(state["has_credentials"])
                self.assertEqual(state["username"], "")
                expected_reason = (vpn_manager._SHARING_REVOKED_PEER_GONE if peer_gone
                                   else vpn_manager._SHARING_REVOKED_PEER_OFF)
                self.assertEqual(state["revoked_reason"], expected_reason)
                self.assertIsNotNone(state["revoked_at"])
                self.assertTrue(vpn_manager.config_path(settings).is_file())
                self.assertEqual(state["source_peer_id"], "peer-1")
                self.assertEqual(state["source_peer_name"], "Peer One")
                log_failure.assert_called_once_with("VPN sharing revocation check failed", failure)

    def test_does_not_revoke_on_transient_network_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=OSError("unreachable")):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["revoked_reason"], "")

    def test_does_not_revoke_on_non_404_http_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            error = HTTPError("https://peer/v1/api/peer/vpn/config", 500, "server error", None, None)
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=error):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])

    def test_noop_when_still_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            still_shared_payload = {"config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "has_credentials": True, "username": "peeruser", "password": "peerpass123"}
            with mock.patch("app.transfer.local_network.get_paired_peer", return_value=peer), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=(still_shared_payload, "https://peer")):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])

    def test_never_raises_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=RuntimeError("boom")):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))

    def test_never_raises_when_stderr_rejects_surrogate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._imported_settings(tmp)
            raw = io.BytesIO()
            strict_stderr = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", write_through=True)
            with mock.patch("app.transfer.local_network.get_paired_peer", side_effect=RuntimeError("roster \udcff")), \
                    mock.patch.object(vpn_manager.sys, "stderr", strict_stderr):
                self.assertFalse(vpn_manager.check_sharing_revocation(settings))
            self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])
            strict_stderr.flush()
            self.assertIn(b"\\udcff", raw.getvalue())

    def test_poller_continues_after_unexpected_check_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "poller_generation", return_value=7), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=[False, False, True]), \
                    mock.patch.object(vpn_manager.time, "sleep"), \
                    mock.patch.object(vpn_manager, "check_sharing_revocation", side_effect=[RuntimeError("boom"), False]) as check, \
                    mock.patch.object(vpn_manager, "_log_poller_failure") as log_failure:
                vpn_manager.run_sharing_revocation_poller(settings)

            self.assertEqual(check.call_count, 2)
            log_failure.assert_called_once()

    def test_retirement_before_generation_read_skips_checks(self) -> None:
        from app.common import runtime_state

        real_generation = vpn_manager.poller_generation
        entered = Event()
        gate = Event()

        def gated_generation() -> int:
            entered.set()
            gate.wait(10)
            return real_generation()

        check = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "poller_generation", gated_generation), \
                    mock.patch.object(vpn_manager.time, "sleep", return_value=None), \
                    mock.patch.object(vpn_manager, "check_sharing_revocation", check):
                thread = Thread(
                    target=vpn_manager.run_sharing_revocation_poller,
                    args=(settings,),
                    daemon=True,
                )
                thread.start()
                self.assertTrue(entered.wait(10), "poller never reached generation capture")
                runtime_state.retire_pollers()
                gate.set()
                thread.join(10)

        self.assertFalse(thread.is_alive())
        check.assert_not_called()

    def test_bound_generation_skips_checks_without_rereading_poller_generation(self) -> None:
        from app.common import runtime_state

        bound = runtime_state.poller_generation()
        runtime_state.retire_pollers()
        check = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "poller_generation", side_effect=AssertionError("must use bound generation")), \
                    mock.patch.object(vpn_manager.time, "sleep", return_value=None), \
                    mock.patch.object(vpn_manager, "check_sharing_revocation", check):
                vpn_manager.run_sharing_revocation_poller(settings, generation=bound)

        check.assert_not_called()


class BootstrapVpnFromSwarmTests(unittest.TestCase):
    """bootstrap_vpn_from_swarm's own peer-search/skip/import logic, in
    isolation from maybe_auto_connect (see AutoConnectTests for the wiring:
    only called when not already ready, and how a success falls through into
    the connect loop)."""

    SHARED_CONNECTED = {
        "config_filename": "client.ovpn", "config_text": SAMPLE_OVPN, "remotes": ["vpn.example.net 1194"],
        "has_credentials": True, "username": "peeruser", "password": "peerpass123", "connected": True,
    }
    SHARED_NOT_CONNECTED = {**SHARED_CONNECTED, "connected": False}

    def test_no_paired_peers_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[]):
                self.assertFalse(vpn_manager.bootstrap_vpn_from_swarm(settings))
            self.assertFalse(vpn_manager._load_state(settings)["has_config"])

    def test_skips_peer_that_is_sharing_but_not_connected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=(self.SHARED_NOT_CONNECTED, "https://peer")):
                self.assertFalse(vpn_manager.bootstrap_vpn_from_swarm(settings))
            self.assertFalse(vpn_manager._load_state(settings)["has_config"])

    def test_imports_from_the_connected_sharing_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            peer = {"drone_id": "peer-1", "name": "Peer One"}
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", return_value=(self.SHARED_CONNECTED, "https://peer")):
                self.assertTrue(vpn_manager.bootstrap_vpn_from_swarm(settings))
            state = vpn_manager._load_state(settings)
            self.assertTrue(state["has_config"])
            self.assertTrue(state["has_credentials"])
            self.assertEqual(state["source_peer_id"], "peer-1")
            self.assertEqual(state["source_peer_name"], "Peer One")

    def test_skips_unreachable_peer_and_succeeds_on_the_next(self) -> None:
        offline_peer = {"drone_id": "peer-offline", "name": "Offline Peer"}
        working_peer = {"drone_id": "peer-2", "name": "Peer Two"}
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))

            def fake_get_json(peer, *_args, **_kwargs):
                if peer["drone_id"] == "peer-offline":
                    raise OSError("unreachable")
                return self.SHARED_CONNECTED, "https://peer"

            with mock.patch("app.transfer.local_network.paired_peers", return_value=[offline_peer, working_peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=fake_get_json):
                self.assertTrue(vpn_manager.bootstrap_vpn_from_swarm(settings))
            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "peer-2")

    def test_skips_peer_whose_shared_payload_fails_to_import(self) -> None:
        # A malformed/empty config_text makes import_from_peer raise -- must
        # not abort the whole search, just move on to the next peer.
        broken_peer = {"drone_id": "peer-broken", "name": "Broken Peer"}
        working_peer = {"drone_id": "peer-2", "name": "Peer Two"}
        broken_payload = {**self.SHARED_CONNECTED, "config_text": ""}
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))

            def fake_get_json(peer, *_args, **_kwargs):
                if peer["drone_id"] == "peer-broken":
                    return broken_payload, "https://peer"
                return self.SHARED_CONNECTED, "https://peer"

            with mock.patch("app.transfer.local_network.paired_peers", return_value=[broken_peer, working_peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=fake_get_json):
                self.assertTrue(vpn_manager.bootstrap_vpn_from_swarm(settings))
            self.assertEqual(vpn_manager._load_state(settings)["source_peer_id"], "peer-2")

    def test_returns_false_when_no_peer_qualifies(self) -> None:
        peer = {"drone_id": "peer-1", "name": "Peer One"}
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch("app.transfer.local_network.paired_peers", return_value=[peer]), \
                    mock.patch("app.transfer.peer_connectivity._peer_get_json_for_peer", side_effect=OSError("unreachable")):
                self.assertFalse(vpn_manager.bootstrap_vpn_from_swarm(settings))


class RecentLogFloodTests(unittest.TestCase):
    def _settings_with_log(self, tmp: str, log_text: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
        vpn_manager.log_path(settings).write_text(log_text, encoding="utf-8")
        return settings

    def test_none_when_log_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._settings_with_log(tmp, "Initialization Sequence Completed\n")
            self.assertIsNone(vpn_manager._recent_log_flood(settings))

    def test_none_when_only_a_few_errors(self) -> None:
        # A handful of replay errors is normal network jitter, not a real problem.
        with tempfile.TemporaryDirectory() as tmp:
            log = "Initialization Sequence Completed\n" + "\n".join(
                f"2026-07-28 13:51:07 AEAD Decrypt error: bad packet ID (may be a replay): [ #{i} ]" for i in range(3)
            )
            settings = self._settings_with_log(tmp, log)
            self.assertIsNone(vpn_manager._recent_log_flood(settings))

    def test_detects_flood_in_recent_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = "\n".join(
                f"2026-07-28 13:51:07 AEAD Decrypt error: bad packet ID (may be a replay): [ #{i} ]" for i in range(20)
            )
            settings = self._settings_with_log(tmp, log)
            result = vpn_manager._recent_log_flood(settings)
            self.assertIsNotNone(result)
            self.assertIn("decrypt/replay errors", result)

    def test_old_flood_scrolled_out_of_recent_window_is_ignored(self) -> None:
        # A burst that already happened and stopped must not look "currently
        # broken" -- this is the real incident (498 replay errors accumulated
        # over hours) if it were scanned as a whole rather than recently.
        with tempfile.TemporaryDirectory() as tmp:
            flood = "\n".join(f"2026-07-28 12:00:00 AEAD Decrypt error: bad packet ID: [ #{i} ]" for i in range(30))
            healthy_tail = "\n".join(f"2026-07-28 13:00:{i:02d} some other benign log line {i}" for i in range(50))
            settings = self._settings_with_log(tmp, flood + "\n" + healthy_tail)
            self.assertIsNone(vpn_manager._recent_log_flood(settings))


class SelfHealReasonTests(unittest.TestCase):
    def test_none_when_disconnected(self) -> None:
        # Never second-guess a disconnect -- whether a human did it on purpose
        # or the sharing-revocation poller did it after wiping credentials.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            snapshot = {"status": "disconnected", "message": "connection refused", "pid": None}
            self.assertIsNone(vpn_manager._self_heal_reason(snapshot, settings))

    def test_reason_from_explicit_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            snapshot = {"status": "error", "message": "AUTH_FAILED", "pid": 123}
            self.assertEqual(vpn_manager._self_heal_reason(snapshot, settings), "AUTH_FAILED")

    def test_reason_from_log_flood_while_status_says_connecting(self) -> None:
        # The real incident: status() reported "connecting", not "error",
        # because the flood had pushed the success marker out of its own
        # detection window -- self-heal must catch this case too.
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
            log = "\n".join(f"2026-07-28 13:51:07 AEAD Decrypt error: bad packet ID: [ #{i} ]" for i in range(20))
            vpn_manager.log_path(settings).write_text(log, encoding="utf-8")
            snapshot = {"status": "connecting", "message": "", "pid": 123}
            self.assertIsNotNone(vpn_manager._self_heal_reason(snapshot, settings))

    def test_replay_flood_does_not_restart_a_live_tunnel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
            log = "\n".join(f"AEAD Decrypt error: bad packet ID: [ #{i} ]" for i in range(20))
            vpn_manager.log_path(settings).write_text(log, encoding="utf-8")
            snapshot = {"status": "connecting", "message": "", "pid": 123}
            with mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True):
                self.assertIsNone(vpn_manager._self_heal_reason(snapshot, settings))

    def test_none_when_connected_and_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            vpn_manager.vpn_dir(settings).mkdir(parents=True, exist_ok=True)
            vpn_manager.log_path(settings).write_text("Initialization Sequence Completed\n", encoding="utf-8")
            snapshot = {"status": "connected", "message": "", "pid": 123}
            self.assertIsNone(vpn_manager._self_heal_reason(snapshot, settings))

    def test_none_without_a_pid_even_if_status_says_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            snapshot = {"status": "connecting", "message": "", "pid": None}
            self.assertIsNone(vpn_manager._self_heal_reason(snapshot, settings))


class ReconnectTests(unittest.TestCase):
    def test_calls_disconnect_then_connect_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            calls: List[str] = []
            with mock.patch.object(vpn_manager, "disconnect", side_effect=lambda s: (calls.append("disconnect"), {"status": "not_running"})[1]), \
                    mock.patch.object(vpn_manager, "connect", side_effect=lambda s: (calls.append("connect"), {"status": "connecting"})[1]):
                result = vpn_manager.reconnect(settings)
            self.assertEqual(calls, ["disconnect", "connect"])
            self.assertEqual(result["status"], "connecting")


class CheckAndSelfHealTests(unittest.TestCase):
    def _ready_settings(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        return settings

    def test_noop_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            vpn_manager.set_self_heal_enabled(settings, False)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "boom", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            reconnect.assert_not_called()

    def test_noop_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))  # no config/credentials at all
            with mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            reconnect.assert_not_called()

    def test_noop_when_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "connected", "message": "", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            reconnect.assert_not_called()

    def test_noop_when_disconnected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "disconnected", "message": "", "pid": None}), \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            reconnect.assert_not_called()

    def test_reconnects_on_error_and_records_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "TLS Error", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}) as reconnect:
                result = vpn_manager.check_and_self_heal(settings)
            reconnect.assert_called_once_with(settings)
            self.assertEqual(result["action"], "reconnected")
            self.assertEqual(result["reason"], "TLS Error")
            state = vpn_manager._load_state(settings)
            self.assertEqual(state["self_heal_last_reason"], "TLS Error")
            self.assertIsNotNone(state["self_heal_last_at"])
            self.assertEqual(len(state["self_heal_attempts"]), 1)

    def test_rate_limited_too_soon_after_last_heal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            vpn_manager._save_state(settings, self_heal_last_at=now_iso, self_heal_attempts=[now_iso])
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "boom", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            reconnect.assert_not_called()

    def test_reconnects_again_once_the_min_interval_elapses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            stale = (datetime.now(timezone.utc) - timedelta(seconds=vpn_manager.VPN_SELF_HEAL_MIN_INTERVAL_SECONDS + 5)).replace(microsecond=0).isoformat()
            vpn_manager._save_state(settings, self_heal_last_at=stale, self_heal_attempts=[stale])
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "boom", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}) as reconnect:
                result = vpn_manager.check_and_self_heal(settings)
            reconnect.assert_called_once()
            self.assertEqual(result["action"], "reconnected")

    def test_pauses_after_hitting_the_window_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            now = datetime.now(timezone.utc)
            # Old enough to clear the min-interval cooldown, recent enough to
            # still count against the rolling-window cap.
            stale_enough = (now - timedelta(seconds=vpn_manager.VPN_SELF_HEAL_MIN_INTERVAL_SECONDS + 5)).replace(microsecond=0).isoformat()
            attempts = [stale_enough] * vpn_manager.VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW
            vpn_manager._save_state(settings, self_heal_last_at=stale_enough, self_heal_attempts=attempts)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "boom", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                result = vpn_manager.check_and_self_heal(settings)
            reconnect.assert_not_called()
            self.assertEqual(result["action"], "paused")

    def test_cap_ages_out_and_self_heal_resumes_later(self) -> None:
        # The cap is a temporary backoff, not a permanent give-up.
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            long_ago = (datetime.now(timezone.utc) - timedelta(seconds=vpn_manager.VPN_SELF_HEAL_WINDOW_SECONDS + 60)).replace(microsecond=0).isoformat()
            attempts = [long_ago] * vpn_manager.VPN_SELF_HEAL_MAX_ATTEMPTS_PER_WINDOW
            vpn_manager._save_state(settings, self_heal_last_at=long_ago, self_heal_attempts=attempts)
            with mock.patch.object(vpn_manager, "status", return_value={"status": "error", "message": "boom", "pid": 1}), \
                    mock.patch.object(vpn_manager, "reconnect", return_value={"status": "connecting"}) as reconnect:
                result = vpn_manager.check_and_self_heal(settings)
            reconnect.assert_called_once()
            self.assertEqual(result["action"], "reconnected")

    def test_never_raises_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready_settings(tmp)
            with mock.patch.object(vpn_manager, "status", side_effect=RuntimeError("boom")):
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))


class SetSelfHealEnabledTests(unittest.TestCase):
    def test_defaults_to_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            self.assertTrue(vpn_manager.status(settings)["self_heal_enabled"])

    def test_can_be_disabled_and_reenabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            result = vpn_manager.set_self_heal_enabled(settings, False)
            self.assertFalse(result["self_heal_enabled"])
            self.assertFalse(vpn_manager._load_state(settings)["self_heal_enabled"])
            result = vpn_manager.set_self_heal_enabled(settings, True)
            self.assertTrue(result["self_heal_enabled"])


class IdentityHoldTests(unittest.TestCase):
    """While swarm_networking_allowed() is False no path may raise the tunnel."""

    def _ready(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        return settings

    def test_connect_refused_without_spawning_openvpn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager.subprocess, "run") as run:
                result = vpn_manager.connect(settings)
            run.assert_not_called()
            self.assertEqual(result["status"], "error")
            self.assertTrue(result["identity_held"])

    def test_reconnect_refused_without_disconnecting_existing_tunnel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "disconnect") as disconnect, \
                    mock.patch.object(vpn_manager, "connect") as connect:
                result = vpn_manager.reconnect(settings)
            disconnect.assert_not_called()
            connect.assert_not_called()
            self.assertTrue(result["identity_held"])

    def test_auto_connect_skips_bootstrap_and_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"), \
                    mock.patch.object(vpn_manager, "bootstrap_vpn_from_swarm") as bootstrap, \
                    mock.patch.object(vpn_manager.subprocess, "run") as run:
                vpn_manager.maybe_auto_connect(settings)
            bootstrap.assert_not_called()
            run.assert_not_called()

    def test_self_heal_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False), \
                    mock.patch.object(vpn_manager, "status") as status, \
                    mock.patch.object(vpn_manager, "reconnect") as reconnect:
                self.assertIsNone(vpn_manager.check_and_self_heal(settings))
            status.assert_not_called()
            reconnect.assert_not_called()

    def test_disconnect_still_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=False):
                self.assertEqual(vpn_manager.disconnect(settings)["status"], "not_running")


class RgsTunnelReconcileTests(unittest.TestCase):
    """RGS follows the tunnel address after a VPN reconnect (issue #581)."""

    def _ready(self, tmp: str) -> Settings:
        settings = _build_settings(self, Path(tmp))
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "user", "pass")
        return settings

    def _rgs(self, **overrides):
        state = {"available": True, "installed": True, "enabled": True, "running": True}
        state.update(overrides)
        return state

    def _reconcile(self, settings, *, address, rgs, restart=None, start=None):
        restart = restart or mock.Mock(return_value=rgs)
        start = start or mock.Mock(return_value=rgs)
        with mock.patch.object(vpn_manager, "tunnel_is_up", return_value=True), \
                mock.patch.object(vpn_manager, "_tunnel_ip", return_value=address), \
                mock.patch.object(vpn_manager._rgs_manager, "status", return_value=rgs), \
                mock.patch.object(vpn_manager._rgs_manager, "restart", restart), \
                mock.patch.object(vpn_manager._rgs_manager, "start", start):
            result = vpn_manager.reconcile_rgs_with_tunnel(settings)
        return result, restart, start

    def test_address_change_restarts_running_rgs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17")
            result, restart, start = self._reconcile(settings, address="10.96.0.18", rgs=self._rgs())
            self.assertEqual(result["action"], "restart")
            restart.assert_called_once()
            start.assert_not_called()
            self.assertEqual(vpn_manager._load_state(settings)["rgs_tunnel_ip"], "10.96.0.18")
            # The same address on the next tick is a no-op.
            result, restart, _ = self._reconcile(settings, address="10.96.0.18", rgs=self._rgs())
            self.assertIsNone(result)
            restart.assert_not_called()

    def test_unchanged_address_does_not_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17")
            result, restart, start = self._reconcile(settings, address="10.96.0.17", rgs=self._rgs())
            self.assertIsNone(result)
            restart.assert_not_called()
            start.assert_not_called()

    def test_first_observation_records_address_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            result, restart, start = self._reconcile(settings, address="10.96.0.17", rgs=self._rgs())
            self.assertIsNone(result)
            restart.assert_not_called()
            start.assert_not_called()
            self.assertEqual(vpn_manager._load_state(settings)["rgs_tunnel_ip"], "10.96.0.17")

    def test_disabled_rgs_is_never_started_or_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17")
            rgs = self._rgs(enabled=False, running=False)
            result, restart, start = self._reconcile(settings, address="10.96.0.18", rgs=rgs)
            self.assertIsNone(result)
            restart.assert_not_called()
            start.assert_not_called()
            self.assertEqual(vpn_manager._load_state(settings)["rgs_tunnel_ip"], "")

    def test_enabled_but_stopped_rgs_is_started_when_tunnel_comes_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            rgs = self._rgs(running=False)
            result, restart, start = self._reconcile(settings, address="10.96.0.17", rgs=rgs)
            self.assertEqual(result["action"], "start")
            start.assert_called_once()
            restart.assert_not_called()

    def test_stopped_rgs_is_started_even_when_address_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17")
            result, restart, start = self._reconcile(settings, address="10.96.0.17", rgs=self._rgs(running=False))
            self.assertEqual(result["action"], "start")
            start.assert_called_once()
            restart.assert_not_called()
            # A start does not arm the restart throttle.
            self.assertIsNone(vpn_manager._load_state(settings)["rgs_restart_last_at"])

    def test_restarts_are_rate_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            recent = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17", rgs_restart_last_at=recent)
            result, restart, _ = self._reconcile(settings, address="10.96.0.18", rgs=self._rgs())
            self.assertEqual(result["action"], "deferred")
            restart.assert_not_called()
            # Deferred changes are not recorded, so the next eligible tick still acts.
            self.assertEqual(vpn_manager._load_state(settings)["rgs_tunnel_ip"], "10.96.0.17")

    def test_failed_restart_is_reported_and_retried_later(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._ready(tmp)
            vpn_manager._save_state(settings, rgs_tunnel_ip="10.96.0.17")
            restart = mock.Mock(side_effect=OSError("RGS service restart failed"))
            result, _, _ = self._reconcile(settings, address="10.96.0.18", rgs=self._rgs(), restart=restart)
            self.assertEqual(result["action"], "failed")
            self.assertEqual(vpn_manager._load_state(settings)["rgs_tunnel_ip"], "10.96.0.17")

    def test_noop_without_tunnel_or_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager._rgs_manager, "restart") as restart:
                self.assertIsNone(vpn_manager.reconcile_rgs_with_tunnel(settings))
            restart.assert_not_called()
            settings = self._ready(tmp)
            with mock.patch.object(vpn_manager, "tunnel_is_up", return_value=False), \
                    mock.patch.object(vpn_manager._rgs_manager, "restart") as restart:
                self.assertIsNone(vpn_manager.reconcile_rgs_with_tunnel(settings))
            restart.assert_not_called()

    def test_retired_self_heal_poller_does_not_reconcile_rgs_after_slow_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = _build_settings(self, Path(tmp))
            with mock.patch.object(vpn_manager, "poller_generation", return_value=7), \
                    mock.patch.object(vpn_manager, "poller_retired", side_effect=[False, True]), \
                    mock.patch.object(vpn_manager.time, "sleep"), \
                    mock.patch.object(vpn_manager, "check_and_self_heal") as check, \
                    mock.patch.object(vpn_manager, "reconcile_rgs_with_tunnel") as reconcile:
                vpn_manager.run_self_heal_poller(settings)

            check.assert_called_once_with(settings)
            reconcile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
