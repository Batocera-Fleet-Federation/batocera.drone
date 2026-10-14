"""Environment probes, network helpers, launcher glue, worker lifecycle and the clone-identity reset."""

import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from app.multiplayer import models as m
from app.multiplayer.environment import SystemProbe, extract_version
from app.multiplayer.identity_reset import reset_multiplayer_identity_state
from app.multiplayer.launcher import GameLauncher
from app.multiplayer.network import NetworkService, RelayProvider, route_scope, valid_host
from app.multiplayer.store import SessionStore
from multiplayer_support import make_swarm
from test_multiplayer_core import make_settings, sample_session


class ProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.settings = make_settings(tmp.name)

    def test_version_extraction(self):
        self.assertEqual(extract_version("RetroArch: Version: 1.19.1 (Git abc)"), "1.19.1")
        self.assertEqual(extract_version("Dolphin 2506a"), "")  # the generic parser wants a dotted number
        from app.multiplayer.adapters.dolphin import DOLPHIN_VERSION
        self.assertEqual(extract_version("Dolphin 2506a", DOLPHIN_VERSION), "2506a")
        self.assertEqual(extract_version("dolphin-emu 2412", DOLPHIN_VERSION), "2412")
        self.assertEqual(extract_version("Dolphin 5.0-21460", DOLPHIN_VERSION), "5.0-21460")
        self.assertEqual(extract_version("PPSSPP v1.18.1-12-gabc"), "1.18.1-12-gabc")
        self.assertEqual(extract_version(""), "")

    def test_conf_precedence_is_per_game_then_system_then_global(self):
        conf = Path(self.settings.batocera_conf_file)
        conf.write_text('global.core=g\nsnes.core=s\nsnes["Kart.sfc"].core=game\n# snes.core=ignored\n', encoding="utf-8")
        probe = SystemProbe(self.settings)
        self.assertEqual(probe.conf_value("snes", "Kart.sfc", "core"), "game")
        self.assertEqual(probe.conf_value("snes", "Other.sfc", "core"), "s")
        self.assertEqual(probe.conf_value("nes", "x.nes", "core"), "g")
        self.assertEqual(probe.conf_value("nes", "x.nes", "missing"), "")
        conf.write_text("snes.core=changed\n", encoding="utf-8")  # re-read when the file changes
        self.assertEqual(SystemProbe(self.settings).conf_value("snes", "Other.sfc", "core"), "changed")

    def test_controllers_are_joysticks_only(self):
        devices = self.root / "devices"
        devices.write_text('I: Bus=0003\nN: Name="Xbox Controller"\nH: Handlers=event5 js0\n\n'
                           'I: Bus=0003\nN: Name="USB Keyboard"\nH: Handlers=sysrq kbd event3\n\n'
                           'I: Bus=0003\nN: Name="Pad 2"\nH: Handlers=event6 js1\n', encoding="utf-8")
        probe = SystemProbe(self.settings, input_devices=devices)
        self.assertEqual([c["name"] for c in probe.controllers()], ["Xbox Controller", "Pad 2"])
        self.assertEqual(SystemProbe(self.settings, input_devices=self.root / "missing").controllers(), [])

    def test_hardware_and_process_detection(self):
        dri = self.root / "dri"
        dri.mkdir()
        (dri / "renderD128").write_text("")
        proc = self.root / "proc"
        (proc / "123").mkdir(parents=True)
        (proc / "123" / "comm").write_text("sunshine\n")
        probe = SystemProbe(self.settings, which=lambda name: None, proc_root=proc, dri_dir=dri)
        self.assertTrue(probe.hardware()["vaapi"])
        self.assertFalse(probe.hardware()["nvenc"])
        self.assertTrue(probe.process_running(["Sunshine"]))
        self.assertFalse(probe.process_running(["retroarch"]))

    def test_version_probe_uses_argv_without_a_shell_and_survives_failures(self):
        probe = SystemProbe(self.settings)
        with mock.patch("app.multiplayer.environment.subprocess.run") as run:
            run.return_value = mock.Mock(stdout="RetroArch 1.19.1", stderr="")
            self.assertEqual(probe.version_of("/usr/bin/retroarch"), "1.19.1")
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["/usr/bin/retroarch", "--version"])
            self.assertIs(kwargs["shell"], False)
            run.side_effect = OSError("nope")
            self.assertEqual(probe.version_of("/usr/bin/retroarch"), "")
        self.assertEqual(probe.version_of(None), "")

    def test_file_introspection_finds_all_tokens_across_files(self):
        a, b = self.root / "a.py", self.root / "b.py"
        a.write_text("netplay.mode")
        b.write_text("netplay.password")
        found = SystemProbe(self.settings).file_contains_all([str(a), str(b)], ["netplay.mode", "netplay.password", "netplay.nope"])
        self.assertEqual(found, {"netplay.mode": True, "netplay.password": True, "netplay.nope": False})

    def test_tcp_probe(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        probe = SystemProbe(self.settings)
        self.assertTrue(probe.tcp_open("127.0.0.1", server.getsockname()[1], 1.0))
        self.assertFalse(probe.tcp_open("127.0.0.1", 1, 0.2))


class NetworkTests(unittest.TestCase):
    def test_route_scope(self):
        self.assertEqual(route_scope("https://100.101.102.103:8543"), "tailnet")
        self.assertEqual(route_scope("https://192.168.1.5:8543"), "lan")
        self.assertEqual(route_scope("https://batocera.local:8543"), "lan")
        self.assertEqual(route_scope("https://8.8.8.8:8543"), "public")
        self.assertEqual(route_scope("https://box.example.ts.net"), "tailnet")
        self.assertEqual(route_scope(""), "unknown")

    def test_valid_host_only_returns_safe_values(self):
        self.assertEqual(valid_host("10.0.0.5"), "10.0.0.5")
        self.assertEqual(valid_host("[fe80::1]"), "fe80::1")
        self.assertEqual(valid_host("batocera.local"), "batocera.local")
        for bad in ("a b", "x;rm -rf /", "host\nname", "-bad", "", "a" * 300, "a=b"):
            self.assertEqual(valid_host(bad), "", bad)

    def test_port_allocation_skips_busy_and_reserved_ports(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = NetworkService(make_settings(tmp.name))
        for _ in range(50):  # keep the whole probe window below 65535
            busy = socket.socket()
            busy.bind(("0.0.0.0", 0))
            if busy.getsockname()[1] < 60000:
                break
            busy.close()
        busy.listen(1)
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        chosen = service.allocate_port(port, port + 20, in_use={port + 1})  # the range is clamped at 65535
        self.assertNotIn(chosen, (port, port + 1))
        with self.assertRaises(m.MultiplayerError) as caught:
            service.allocate_port(port, port, in_use=set())
        self.assertEqual(caught.exception.code, "no_free_port")

    def test_connectivity_test_reports_median_latency_route_and_advice(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clock = iter([0.0, 0.010, 1.0, 1.030, 2.0, 2.020])
        service = NetworkService(make_settings(tmp.name), get_json=lambda *a, **k: ({}, "https://100.64.0.2:8543"), clock=lambda: next(clock))
        result = service.test_peer({"drone_id": "peer-1"})
        self.assertTrue(result["reachable"])
        self.assertEqual((result["scope"], result["latency_ms"], result["samples"]), ("tailnet", 20.0, 3))
        self.assertTrue(any("Tailscale" in advice for advice in result["advice"]))

    def test_unreachable_peer_gets_actionable_advice_and_no_exception(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def fail(*args, **kwargs):
            raise OSError("no route to host")
        service = NetworkService(make_settings(tmp.name), get_json=fail)
        result = service.test_peer({"drone_id": "peer-1"})
        self.assertFalse(result["reachable"])
        self.assertIn("no route", result["error"])
        self.assertTrue(result["advice"])

    def test_http_errors_become_readable_multiplayer_errors(self):
        import io
        import json
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def reject(*args, **kwargs):
            raise HTTPError("https://x", 409, "Conflict", {}, io.BytesIO(json.dumps({"error": "Session is full", "code": "session_full"}).encode()))
        service = NetworkService(make_settings(tmp.name), post_json=reject)
        with self.assertRaises(m.MultiplayerError) as caught:
            service.post_checked({"drone_id": "p"}, "/respond", {})
        self.assertEqual((caught.exception.code, caught.exception.message, caught.exception.status), ("session_full", "Session is full", 409))

        def down(*args, **kwargs):
            raise OSError("connection refused")
        with self.assertRaises(m.MultiplayerError) as caught:
            NetworkService(make_settings(tmp.name), post_json=down).post_checked({"drone_id": "p"}, "/respond", {})
        self.assertEqual(caught.exception.code, "peer_unreachable")

    def test_no_relay_ships_and_the_seam_says_so(self):
        relay = RelayProvider()
        self.assertFalse(relay.available())
        with self.assertRaises(m.CapabilityError):
            relay.open_session("s", ["a"])
        relay.close_session("s")  # cleanup is always safe


class LauncherTests(unittest.TestCase):
    class FakeEs:
        def __init__(self, status=200, error=None):
            self.status, self.error, self.calls = status, error, []

        def launch(self, path):
            self.calls.append(path)
            if self.error:
                raise self.error
            return self.status, "ok"

    def test_launch_goes_through_emulationstation_only(self):
        es = self.FakeEs()
        self.assertEqual(GameLauncher(es_api=es).launch_game("/roms/snes/Kart.sfc")["launcher"], "emulationstation-api")
        self.assertEqual(es.calls, ["/roms/snes/Kart.sfc"])

    def test_launch_failures_are_explained(self):
        from app.integrations.streamdeck.emulationstation import EmulationStationUnavailable
        for es, code in ((self.FakeEs(error=EmulationStationUnavailable("down")), "es_unavailable"), (self.FakeEs(status=404), "es_unknown_game"),
                         (self.FakeEs(status=500), "es_rejected")):
            with self.subTest(code=code), self.assertRaises(m.MultiplayerError) as caught:
                GameLauncher(es_api=es).launch_game("/x")
            self.assertEqual(caught.exception.code, code)

    def test_session_game_detection(self):
        runtime = mock.Mock()
        runtime.get_active_game.return_value = {"rom_path": "/roms/snes/Kart.sfc"}
        launcher = GameLauncher(runtime=runtime)
        self.assertTrue(launcher.is_session_game("/roms/snes/Kart.sfc"))
        self.assertFalse(launcher.is_session_game("/roms/snes/Other.sfc"))
        runtime.get_active_game.side_effect = RuntimeError("proc unreadable")
        self.assertIsNone(launcher.active_game())


class IdentityResetTests(unittest.TestCase):
    def test_clone_reset_removes_peer_bound_state_restores_settings_and_keeps_local_knowledge(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hub, nodes = make_swarm(tmp.name, open_ports=(55435,))
        host = nodes["host"]
        before = host.conf_text
        sid = host.orch.create_session({"system": "snes", "game_id": "g1", "peers": ["guest"], "emulator_override": {"core": "snes9x"}})["id"]
        host.orch.update_settings({"privacy": {"invite_permission": "allowlist", "allowed_inviters": ["guest"]}})
        host.orch.compat.record_session_result(system="snes", title_key="local-title", title="Local", mechanism=m.RETROARCH_NETPLAY,
                                               config_fp="aa", emulator_version="1", success=True)
        host.orch.compat.import_shared([{"system": "snes", "gameId": "imported", "mechanisms": {m.RETROARCH_NETPLAY: {"tested": {"bb": {"successes": 1}}}}}], "guest")
        adapter = host.orch.adapter_map["retroarch"]
        session = host.orch.store.get(sid)
        host.orch.apply_edits(sid, adapter.configure_networking(session, m.HOST_ROLE, host.orch.launch_context(session, m.HOST_ROLE, port=55435)))
        self.assertNotEqual(host.conf_text, before)

        result = reset_multiplayer_identity_state(host.settings)

        self.assertEqual(result["restored_sessions"], 1)
        self.assertEqual(result["imported_reports_removed"], 1)
        self.assertEqual(host.conf_text, before)
        self.assertEqual(host.orch.store.all(), [])
        self.assertEqual(host.orch.store.invitations(), [])
        self.assertEqual(host.orch.get_settings()["privacy"]["allowed_inviters"], [])
        self.assertIsNotNone(host.orch.compat.get("snes", "local-title"))
        self.assertIsNone(host.orch.compat.get("snes", "imported"))
        self.assertEqual(reset_multiplayer_identity_state(host.settings)["restored_sessions"], 0)  # idempotent

    def test_swarm_reset_calls_the_multiplayer_reset(self):
        from app.transfer import swarm_reset
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = make_settings(tmp.name)
        store = SessionStore(settings)
        store.create(sample_session())
        with mock.patch("app.transfer.swarm_reset._clear_pinned_peer_certificates", return_value=0), \
                mock.patch("app.transfer.swarm_reset._revoke_shared_credentials", return_value=[]), \
                mock.patch("app.device.nfs_export_manager.revoke_exports_for_identity_change"):
            swarm_reset.reset_swarm_identity_state(settings)
        self.assertEqual(store.all(), [])


class WorkerTests(unittest.TestCase):
    def test_worker_is_idempotent_and_stoppable(self):
        from app.multiplayer import worker
        worker.stop_multiplayer_worker()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = make_settings(tmp.name)
        with mock.patch.object(worker, "SWEEP_SECONDS", 0.05), mock.patch.object(worker, "STARTUP_DELAY_SECONDS", 0.0):
            first = worker.start_multiplayer_worker(settings, mock.Mock())
            self.assertIsNotNone(first)
            self.assertIsNone(worker.start_multiplayer_worker(settings, mock.Mock()))
        worker.stop_multiplayer_worker()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())


if __name__ == "__main__":
    unittest.main()
