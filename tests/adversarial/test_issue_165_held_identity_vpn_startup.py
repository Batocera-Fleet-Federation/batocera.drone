"""Issue #165: a held identity must not start VPN auto-connect or self-heal.

When swarm_networking_allowed() is False (RECOVERY_REQUIRED: missing or
contradictory hardware evidence, or an imaged in-flight journal on
identifierless hardware), create_server() must not start
vpn_manager.maybe_auto_connect or vpn_manager.run_self_heal_poller. An
OpenVPN profile already on that disk — locally uploaded or imported from a
peer — must not come up by itself while swarm networking is held.

A READY identity must still auto-connect and start the self-heal poller.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.common import identity_provisioning as identity
from app.common.hardware_identity import HardwareIdentityProvider
from app.common.settings import Settings
from app.device import vpn_manager
from app.storage.state_store import database_path, open_database
from app.transfer.drone_tls import DroneCertificateManager
from tests.test_hardware_identity import UUID_A, UUID_B, x86_tree


OPENSSL_AVAILABLE = shutil.which("openssl") is not None

SAMPLE_OVPN = """client
dev tun
proto udp
remote vpn.example.net 1194
remote-cert-tls server
auth-user-pass
verb 3
"""

VPN_AUTO_THREAD = "drone-vpn-auto-connect"
VPN_SELF_HEAL_THREAD = "drone-vpn-self-heal"
VPN_SHARING_THREAD = "drone-vpn-sharing-revocation"

# Unrelated create_server() starters. Pre-mark them so this suite only
# exercises the VPN identity gate and does not inherit forever-loops from
# torrents, SMTP, Tailscale, share replay, or the game-process monitor.
_UNRELATED_STARTED_FLAGS = (
    "_ROM_METADATA_POLLER_STARTED",
    "_ROM_METADATA_WATCHER_STARTED",
    "_LOCAL_NETWORK_WORKERS_STARTED",
    "_GAME_PROCESS_MONITOR_STARTED",
    "_AUTOMATION_POLLER_STARTED",
    "_NETWORK_SHARE_BOOT_REPLAY_ATTEMPTED",
    "_NETWORK_SHARE_WATCHDOG_STARTED",
    "_NFS_EXPORT_BOOT_REPLAY_ATTEMPTED",
    "_SMTP_BOOTSTRAP_ATTEMPTED",
    "_SMTP_SHARING_POLLER_STARTED",
    "_AUDIT_EMAIL_POLLER_STARTED",
    "_TAILNET_BOOTSTRAP_ATTEMPTED",
    "_TAILNET_SHARING_POLLER_STARTED",
    "_STREAMDECK_SUPERVISOR_STARTED",
)
_VPN_FLAGS = (
    "_VPN_AUTO_CONNECT_ATTEMPTED",
    "_VPN_SELF_HEAL_POLLER_STARTED",
    "_VPN_SHARING_POLLER_STARTED",
)
_MANAGER_FLAGS = ("_DOWNLOAD_MANAGER", "_TORRENT_MANAGER")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def provider_for(root: Path) -> HardwareIdentityProvider:
    return HardwareIdentityProvider(root, container_probe=lambda: False, machine="x86_64")


def image_disk(source: Path, target: Path) -> None:
    with open_database(database_path(source)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copytree(source, target, symlinks=True)


def settings_for(root: Path, **extra: str) -> Settings:
    for folder in ("roms", "bios", "saves"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "vpn").mkdir(parents=True, exist_ok=True)
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "TLS_SELF_SIGNED_DIR": str(root / "system" / "certs"),
        "DRONE_VPN_DIR": str(root / "vpn"),
        "HTTP_ONLY": "1",
        "HTTPS_PORT": "0",
        "HTTP_REDIRECT_PORT": "0",
        "DRONE_COMPAT_HTTPS_PORTS": "",
        "DRONE_PEER_MTLS_PORT": str(free_port()),
        "DRONE_CAST_ENABLED": "0",
        "ROM_METADATA_POLL_SECONDS": "0",
        "DRONE_AUTO_UPDATE_POLL_SECONDS": "0",
        "DRONE_APP_USERNAME": "admin",
        "DRONE_APP_PASSWORD": "changeme",
        **extra,
    }
    with mock.patch.dict(os.environ, env, clear=False):
        return Settings.from_env()


class VpnStartupProbe:
    """Drive create_server() and record which VPN boot paths actually ran."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        self.thread_names: list[str] = []
        self.connect_calls: list = []
        self.bootstrap_calls: list = []
        self.self_heal_poller_calls: list = []
        self.sharing_poller_calls: list = []
        self.check_and_self_heal_calls: list = []
        self.auto_connect_started = threading.Event()
        self.self_heal_started = threading.Event()
        self._flag_originals: dict[str, object] = {}
        self.server = None

    def _quiet_unrelated_starters(self) -> None:
        for name in _UNRELATED_STARTED_FLAGS + _VPN_FLAGS + _MANAGER_FLAGS:
            self._flag_originals[name] = getattr(drone_api, name)
        for name in _UNRELATED_STARTED_FLAGS:
            setattr(drone_api, name, True)
        for name in _VPN_FLAGS:
            setattr(drone_api, name, False)
        for name in _MANAGER_FLAGS:
            setattr(drone_api, name, object())

    def restore_flags(self) -> None:
        for name, value in self._flag_originals.items():
            setattr(drone_api, name, value)
        self._flag_originals.clear()

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.server_close()
            except OSError:
                pass
            peer = getattr(self.server, "peer_mtls_server", None)
            if peer is not None:
                try:
                    peer.shutdown()
                except Exception:
                    pass
                try:
                    peer.server_close()
                except OSError:
                    pass
            self.server = None
        self.restore_flags()

    def boot(self, settings: Settings, *, wait_for_auto: bool = False) -> None:
        self._quiet_unrelated_starters()
        real_thread = drone_api.Thread
        probe = self

        class RecordingThread(real_thread):
            def start(self, *args, **kwargs):
                probe.thread_names.append(self.name)
                return super().start(*args, **kwargs)

        def spy_connect(current_settings):
            probe.connect_calls.append(current_settings)
            probe.auto_connect_started.set()
            return {"status": "connecting"}

        def spy_bootstrap(current_settings):
            probe.bootstrap_calls.append(current_settings)
            return False

        def spy_self_heal_poller(current_settings):
            probe.self_heal_poller_calls.append(current_settings)
            probe.self_heal_started.set()

        def spy_sharing_poller(current_settings):
            probe.sharing_poller_calls.append(current_settings)

        def spy_check_and_self_heal(current_settings):
            probe.check_and_self_heal_calls.append(current_settings)
            return None

        patches = [
            mock.patch.object(drone_api, "Thread", RecordingThread),
            mock.patch.object(drone_api._vpn_manager, "connect", spy_connect),
            mock.patch.object(drone_api._vpn_manager, "bootstrap_vpn_from_swarm", spy_bootstrap),
            mock.patch.object(drone_api._vpn_manager, "run_self_heal_poller", spy_self_heal_poller),
            mock.patch.object(drone_api._vpn_manager, "run_sharing_revocation_poller", spy_sharing_poller),
            mock.patch.object(drone_api._vpn_manager, "check_and_self_heal", spy_check_and_self_heal),
            mock.patch.object(drone_api._vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"),
            mock.patch.object(vpn_manager, "find_openvpn_binary", return_value="/usr/sbin/openvpn"),
            mock.patch.dict(os.environ, {"DRONE_VPN_DIR": str(Path(settings.userdata_root) / "vpn")}, clear=False),
        ]
        for patcher in patches:
            patcher.start()
            self.case.addCleanup(patcher.stop)
        try:
            self.server = drone_api.create_server(settings)
        except PermissionError as error:
            self.restore_flags()
            self.case.skipTest(f"Socket bind is not allowed here: {error}")
        self.case.addCleanup(self.close)
        if wait_for_auto:
            self.auto_connect_started.wait(2.0)
            self.self_heal_started.wait(2.0)
        else:
            # Give a wrongly-started auto-connect thread time to call connect().
            self.auto_connect_started.wait(0.4)
            self.self_heal_started.wait(0.2)

    def assert_vpn_boot_held(self) -> None:
        names = set(self.thread_names)
        self.case.assertNotIn(VPN_AUTO_THREAD, names)
        self.case.assertNotIn(VPN_SELF_HEAL_THREAD, names)
        self.case.assertNotIn(VPN_SHARING_THREAD, names)
        self.case.assertEqual(self.connect_calls, [])
        self.case.assertEqual(self.bootstrap_calls, [])
        self.case.assertEqual(self.self_heal_poller_calls, [])
        self.case.assertEqual(self.sharing_poller_calls, [])
        self.case.assertEqual(self.check_and_self_heal_calls, [])
        self.case.assertFalse(drone_api._VPN_AUTO_CONNECT_ATTEMPTED)
        self.case.assertFalse(drone_api._VPN_SELF_HEAL_POLLER_STARTED)
        self.case.assertFalse(drone_api._VPN_SHARING_POLLER_STARTED)

    def assert_vpn_boot_allowed(self) -> None:
        names = set(self.thread_names)
        self.case.assertIn(VPN_AUTO_THREAD, names)
        self.case.assertIn(VPN_SELF_HEAL_THREAD, names)
        self.case.assertTrue(self.connect_calls or self.auto_connect_started.is_set())
        self.case.assertTrue(self.self_heal_poller_calls)
        self.case.assertTrue(drone_api._VPN_AUTO_CONNECT_ATTEMPTED)
        self.case.assertTrue(drone_api._VPN_SELF_HEAL_POLLER_STARTED)


def _isolate_vpn_dir(settings: Settings):
    vpn_root = Path(settings.userdata_root) / "vpn"
    vpn_root.mkdir(parents=True, exist_ok=True)
    return mock.patch.dict(os.environ, {"DRONE_VPN_DIR": str(vpn_root)}, clear=False)


def seed_imported_vpn(settings: Settings) -> None:
    with _isolate_vpn_dir(settings):
        vpn_manager.import_from_peer(
            settings,
            {
                "config_filename": "client.ovpn",
                "config_text": SAMPLE_OVPN,
                "has_credentials": True,
                "username": "peeruser",
                "password": "peerpass123",
            },
            source_peer_id="aa:bb:cc:dd:ee:01",
            source_peer_name="Peer One",
        )
        state = vpn_manager._load_state(settings)
    if not state["has_config"] or not state["has_credentials"]:
        raise AssertionError("imported OpenVPN profile was not written to disk")
    if not state["source_peer_id"]:
        raise AssertionError("imported OpenVPN profile lost its peer provenance")


def seed_local_vpn(settings: Settings) -> None:
    with _isolate_vpn_dir(settings):
        vpn_manager.save_uploaded_config(settings, "client.ovpn", SAMPLE_OVPN.encode())
        vpn_manager.save_credentials(settings, "local-user", "local-pass")


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class HeldIdentityVpnStartupUAT(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-165-vpn-held-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hw_a = x86_tree(self.tmp / "hw-a")
        self.hw_b = x86_tree(self.tmp / "hw-b", uuid=UUID_B, board="BOARDSERIAL02")
        self.disk = self.tmp / "disk"
        self.disk.mkdir()

    def _install_ready(self, hardware: Path | None = None) -> Settings:
        settings = settings_for(self.disk)
        result = identity.ensure_identity(settings, provider_for(hardware or self.hw_a))
        self.assertEqual(result.state, identity.State.READY)
        self.assertTrue(identity.swarm_networking_allowed())
        return settings

    def test_insufficient_evidence_imported_profile_does_not_auto_connect(self) -> None:
        settings = self._install_ready()
        seed_imported_vpn(settings)
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.reset_runtime_state_for_tests()
        held = identity.ensure_identity(settings_for(self.disk), provider_for(blind))
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertEqual(held.reason, "insufficient_evidence")
        self.assertFalse(identity.swarm_networking_allowed())
        with _isolate_vpn_dir(settings):
            self.assertTrue(vpn_manager._load_state(settings)["has_credentials"])

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(self.disk))
        probe.assert_vpn_boot_held()

    def test_contradictory_evidence_local_profile_does_not_start_vpn_pollers(self) -> None:
        settings = self._install_ready()
        seed_local_vpn(settings)
        odd = x86_tree(self.tmp / "hw-odd", uuid=UUID_A, board="REPLACEDBOARD")
        identity.reset_runtime_state_for_tests()
        held = identity.ensure_identity(settings_for(self.disk), provider_for(odd))
        self.assertEqual(held.reason, "contradictory_evidence")
        self.assertFalse(identity.swarm_networking_allowed())

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(self.disk))
        probe.assert_vpn_boot_held()

    def test_imaged_in_flight_journal_on_identifierless_hardware_does_not_start_vpn(self) -> None:
        original = self._install_ready()
        seed_imported_vpn(original)
        middle = self.tmp / "middle"
        image_disk(self.disk, middle)
        clone = settings_for(middle)
        with mock.patch.object(identity, "_save_binding", side_effect=OSError("interrupted write")):
            failed = identity.ensure_identity(clone, provider_for(self.hw_b))
        self.assertEqual(failed.state, identity.State.RECOVERY_REQUIRED)
        self.assertTrue(identity._journal_path(clone).exists())

        third = self.tmp / "third"
        image_disk(middle, third)
        identity.reset_runtime_state_for_tests()
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        held = identity.ensure_identity(settings_for(third), provider_for(blind))
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)
        self.assertFalse(identity.swarm_networking_allowed())

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(third))
        probe.assert_vpn_boot_held()

    def test_ready_identity_still_auto_connects_and_starts_self_heal(self) -> None:
        settings = self._install_ready()
        seed_imported_vpn(settings)
        self.assertTrue(identity.swarm_networking_allowed())

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(self.disk), wait_for_auto=True)
        probe.assert_vpn_boot_allowed()

    def test_held_boot_does_not_latch_flags_so_recovery_can_auto_connect(self) -> None:
        settings = self._install_ready()
        seed_local_vpn(settings)
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.reset_runtime_state_for_tests()
        held = identity.ensure_identity(settings_for(self.disk), provider_for(blind))
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)

        held_probe = VpnStartupProbe(self)
        held_probe.boot(settings_for(self.disk))
        held_probe.assert_vpn_boot_held()
        held_probe.close()

        identity.reset_runtime_state_for_tests()
        recovered = identity.ensure_identity(settings_for(self.disk), provider_for(self.hw_a))
        self.assertEqual(recovered.state, identity.State.READY)
        self.assertTrue(identity.swarm_networking_allowed())

        ready_probe = VpnStartupProbe(self)
        ready_probe.boot(settings_for(self.disk), wait_for_auto=True)
        ready_probe.assert_vpn_boot_allowed()

    def test_create_server_reads_live_swarm_networking_allowed_not_stale_true(self) -> None:
        """A READY install that later publishes a hold must still skip VPN boot.

        create_server() must consult swarm_networking_allowed() at the moment
        it starts pollers. A cached True from an earlier ensure_identity in
        this process is not a license to bring the tunnel up.
        """
        settings = self._install_ready()
        seed_imported_vpn(settings)
        identity._publish(
            identity.State.RECOVERY_REQUIRED,
            identity.STATUS_RECOVERY,
            "certificate_mismatch",
            False,
        )
        self.assertFalse(identity.swarm_networking_allowed())

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(self.disk))
        probe.assert_vpn_boot_held()

    def test_empty_and_malformed_vpn_state_still_must_not_start_held_pollers(self) -> None:
        settings = self._install_ready()
        vpn_dir = Path(settings.userdata_root) / "vpn"
        vpn_dir.mkdir(parents=True, exist_ok=True)
        (vpn_dir / "state.json").write_text("{not-json", encoding="utf-8")
        (vpn_dir / "client.ovpn").write_bytes(b"")
        blind = self.tmp / "hw-blind"
        blind.mkdir()
        identity.reset_runtime_state_for_tests()
        held = identity.ensure_identity(settings_for(self.disk), provider_for(blind))
        self.assertEqual(held.state, identity.State.RECOVERY_REQUIRED)

        probe = VpnStartupProbe(self)
        probe.boot(settings_for(self.disk))
        probe.assert_vpn_boot_held()


@unittest.skipUnless(OPENSSL_AVAILABLE, "openssl is required for identity provisioning")
class HeldIdentityDoesNotScheduleVpnThreads(unittest.TestCase):
    def setUp(self) -> None:
        identity.reset_runtime_state_for_tests()
        self.addCleanup(identity.reset_runtime_state_for_tests)
        self.tmp = Path(tempfile.mkdtemp(prefix="issue-165-vpn-starters-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_held_create_server_does_not_construct_vpn_boot_threads(self) -> None:
        hw = x86_tree(self.tmp / "hw-a")
        disk = self.tmp / "disk"
        disk.mkdir()
        settings = settings_for(disk)
        self.assertEqual(identity.ensure_identity(settings, provider_for(hw)).state, identity.State.READY)
        DroneCertificateManager(settings).ensure_certificate()
        identity._publish(identity.State.RECOVERY_REQUIRED, identity.STATUS_RECOVERY, "insufficient_evidence", False)

        scheduled = []
        real_thread = drone_api.Thread

        class Capture(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                scheduled.append((self.name, getattr(self._target, "__name__", "")))

            def start(self, *args, **kwargs):
                if self.name in {VPN_AUTO_THREAD, VPN_SELF_HEAL_THREAD, VPN_SHARING_THREAD}:
                    return None
                return super().start(*args, **kwargs)

        originals = {}
        for name in _UNRELATED_STARTED_FLAGS + _VPN_FLAGS + _MANAGER_FLAGS:
            originals[name] = getattr(drone_api, name)
        try:
            for name in _UNRELATED_STARTED_FLAGS:
                setattr(drone_api, name, True)
            for name in _VPN_FLAGS:
                setattr(drone_api, name, False)
            for name in _MANAGER_FLAGS:
                setattr(drone_api, name, object())
            with mock.patch.object(drone_api, "Thread", Capture), mock.patch.dict(
                os.environ, {"DRONE_VPN_DIR": str(disk / "vpn")}, clear=False
            ):
                try:
                    server = drone_api.create_server(settings_for(disk))
                except PermissionError as error:
                    self.skipTest(f"Socket bind is not allowed here: {error}")
                self.addCleanup(server.server_close)
        finally:
            for name, value in originals.items():
                setattr(drone_api, name, value)

        vpn_scheduled = [
            item
            for item in scheduled
            if item[0] in {VPN_AUTO_THREAD, VPN_SELF_HEAL_THREAD, VPN_SHARING_THREAD}
            or item[1] in {"maybe_auto_connect", "run_self_heal_poller", "run_sharing_revocation_poller"}
        ]
        self.assertEqual(vpn_scheduled, [])
