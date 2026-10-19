"""Sunshine install/start/remove from Multiplayer settings, against fakes (no network, no real AppImage)."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.multiplayer import models as m
from app.multiplayer.adapters.streaming import StreamingAdapter
from app.multiplayer.dependencies import (DOWNLOAD_PREFIX, SERVICE_MARKER, InstallError, SunshineInstaller, managed_binary,
                                          managed_root, select_release_asset, service_path)
from multiplayer_support import FakeProbe
from test_multiplayer_core import make_settings
from test_multiplayer_orchestrator import SwarmCase

IMAGE = b"fake-appimage-bytes"
NAME = "Sunshine_2026.1.1_x86_64.AppImage"


def release(**overrides):
    asset = {"name": NAME, "browser_download_url": f"{DOWNLOAD_PREFIX}v2026.1.1/{NAME}", "size": len(IMAGE),
             "digest": "sha256:" + hashlib.sha256(IMAGE).hexdigest()}
    asset.update(overrides)
    return {"tag_name": "v2026.1.1", "assets": [asset]}


class ReleaseSelectionTests(unittest.TestCase):
    def test_selects_the_matching_architecture_with_its_digest(self):
        asset = select_release_asset(release(), "x86_64")
        self.assertEqual((asset["name"], asset["size"], asset["tag"]), (NAME, len(IMAGE), "v2026.1.1"))
        self.assertEqual(asset["sha256"], hashlib.sha256(IMAGE).hexdigest())

    def test_refuses_anything_it_cannot_verify(self):
        for bad in ({"digest": ""}, {"digest": "md5:abc"}, {"browser_download_url": "https://evil.example/" + NAME},
                    {"browser_download_url": "http://github.com/LizardByte/Sunshine/releases/download/x/" + NAME},
                    {"size": 0}, {"size": 10 ** 12}, {"size": True}):
            with self.assertRaises(InstallError, msg=str(bad)):
                select_release_asset(release(**bad), "x86_64")

    def test_other_architecture_or_shape_is_rejected(self):
        with self.assertRaises(InstallError):
            select_release_asset(release(), "aarch64")
        with self.assertRaises(InstallError):
            select_release_asset({"assets": "nope"}, "x86_64")
        with self.assertRaises(InstallError):
            select_release_asset(release(name="Sunshine_x86_64.deb"), "x86_64")


class InstallerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = make_settings(tmp.name)
        self.probe = FakeProbe(self.settings)
        self.commands = []
        self.changed = 0

    def fake_download(self, url, destination, expected_size, progress, content=IMAGE):
        destination.write_bytes(content)
        progress(len(content))
        return hashlib.sha256(content).hexdigest() if content == IMAGE else "0" * 64

    def fake_run(self, argv, **kwargs):
        self.commands.append(list(argv))
        if argv[-1] == "--appimage-extract":
            root = Path(kwargs["cwd"]) / "squashfs-root"
            (root / "usr" / "bin").mkdir(parents=True)
            (root / "AppRun").write_text("#!/bin/sh\n")
            (root / "usr" / "bin" / "sunshine").write_text("bin")
        elif argv[0] == "sh" and argv[-1] == "start":
            self.probe.processes.add("sunshine")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def installer(self, **kwargs):
        options = dict(fetch_json=lambda url: release(), download=self.fake_download, run=self.fake_run,
                       arch=lambda: "x86_64", spawn=lambda fn, name: fn(), sleep=lambda seconds: None,
                       on_change=lambda: setattr(self, "changed", self.changed + 1))
        options.update(kwargs)
        return SunshineInstaller(self.settings, self.probe, **options)


class InstallTests(InstallerCase):
    def test_install_downloads_verifies_extracts_registers_one_service_and_starts_it(self):
        installer = self.installer()
        with mock.patch("app.multiplayer.dependencies.shutil.which", return_value=None):
            installer.start_install()
        job = installer.job()
        self.assertEqual((job["state"], job["percent"]), ("succeeded", 100), job)
        self.assertTrue(installer.is_managed_install())
        wrapper = managed_binary(self.settings).read_text()
        self.assertIn("AppRun", wrapper)
        self.assertIn(SERVICE_MARKER, service_path(self.settings).read_text())
        self.assertEqual(json.loads((managed_root(self.settings) / "version.json").read_text())["tag"], "v2026.1.1")
        self.assertIn(["sh", str(service_path(self.settings)), "start"], self.commands)
        self.assertFalse((managed_root(self.settings).parent / ".sunshine-staging").exists())
        self.assertGreaterEqual(self.changed, 1)
        # Every subprocess is a fixed argv list; nothing from the network reached a command line.
        for argv in self.commands:
            self.assertTrue(all(isinstance(part, str) for part in argv))
            self.assertNotIn(NAME, " ".join(argv[:1]))

    def test_checksum_mismatch_installs_nothing(self):
        installer = self.installer(download=lambda url, dest, size, progress: self.fake_download(url, dest, size, progress, b"tampered"))
        installer.start_install()
        self.assertEqual(installer.job()["state"], "failed")
        self.assertIn("checksum", installer.job()["message"])
        self.assertFalse(managed_root(self.settings).exists())
        self.assertFalse(service_path(self.settings).exists())
        self.assertEqual(self.commands, [])

    def test_failed_extraction_and_network_errors_are_reported_plainly(self):
        installer = self.installer(run=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr=""))
        installer.start_install()
        self.assertEqual(installer.job()["state"], "failed")
        self.assertIn("unpacked", installer.job()["message"])
        self.assertFalse(managed_root(self.settings).joinpath("app").exists())
        broken = self.installer(fetch_json=mock.Mock(side_effect=OSError("offline")))
        broken.start_install()
        self.assertEqual(broken.job()["state"], "failed")

    def test_unsupported_architecture_and_existing_install_are_refused(self):
        with self.assertRaises(m.MultiplayerError) as caught:
            self.installer(arch=lambda: "").start_install()
        self.assertEqual(caught.exception.code, "unsupported_architecture")
        self.probe.binaries["sunshine"] = "/usr/bin/sunshine"
        with self.assertRaises(m.MultiplayerError) as caught:
            self.installer().start_install()
        self.assertEqual(caught.exception.code, "already_installed")

    def test_second_request_while_running_does_not_start_another_job(self):
        queued = []
        installer = self.installer(spawn=lambda fn, name: queued.append(fn))
        installer.start_install()
        installer.start_install()
        self.assertEqual(len(queued), 1)
        self.assertEqual(installer.job()["state"], "running")
        with self.assertRaises(m.MultiplayerError):
            installer.uninstall()

    def test_does_not_overwrite_a_foreign_service_of_the_same_name(self):
        script = service_path(self.settings)
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\necho mine\n")
        installer = self.installer()
        installer.start_install()
        self.assertEqual(installer.job()["state"], "failed")
        self.assertEqual(script.read_text(), "#!/bin/sh\necho mine\n")


class UninstallTests(InstallerCase):
    def test_uninstall_removes_only_what_drone_installed(self):
        installer = self.installer()
        installer.start_install()
        config = Path(self.settings.userdata_root) / "system" / ".config" / "sunshine" / "sunshine.conf"
        config.parent.mkdir(parents=True)
        config.write_text("keep")
        self.probe.binaries.clear()
        status = installer.uninstall()
        self.assertFalse(managed_root(self.settings).exists())
        self.assertFalse(service_path(self.settings).exists())
        self.assertEqual(config.read_text(), "keep")
        self.assertFalse(status["installed"])

    def test_refuses_to_remove_a_sunshine_drone_did_not_install(self):
        self.probe.binaries["sunshine"] = "/usr/bin/sunshine"
        with self.assertRaises(m.MultiplayerError) as caught:
            self.installer().uninstall()
        self.assertEqual(caught.exception.code, "not_managed")

    def test_foreign_service_file_is_left_alone(self):
        installer = self.installer()
        installer.start_install()
        script = service_path(self.settings)
        script.write_text("#!/bin/sh\necho mine\n")
        installer.uninstall()
        self.assertTrue(script.exists())


class StatusAndAdapterTests(InstallerCase):
    def test_status_reports_what_the_ui_can_do(self):
        installer = self.installer()
        status = installer.status()
        self.assertTrue(status["can_install"] and not status["installed"] and not status["can_start"])
        installer.start_install()
        status = installer.status()
        self.assertTrue(status["installed"] and status["managed"] and status["can_uninstall"] and not status["can_install"])
        self.assertTrue(status["running"] and not status["can_start"])
        self.assertEqual(status["version"], "v2026.1.1")

    def test_streaming_adapter_finds_the_drone_installed_sunshine(self):
        adapter = StreamingAdapter(self.probe)
        self.assertFalse(adapter.detect_installed())
        reason = adapter.assess_game({"system": "n64", "rom_name": "x.z64", "players": 4})["reasons"][0]
        self.assertIn("Install it from Multiplayer", reason)
        self.probe.binaries[str(managed_binary(self.settings))] = str(managed_binary(self.settings))
        adapter.refresh()
        self.assertTrue(adapter.detect_installed())


class OrchestratorActionTests(SwarmCase):
    def setUp(self):
        super().setUp()
        self.installer = mock.Mock()
        self.installer.job.return_value = {"state": "idle"}
        self.installer.status.return_value = {"installed": False}
        self.h.sunshine = self.installer

    def test_actions_are_allowlisted(self):
        self.assertEqual(self.code(self.h.dependency_action, "sunshine", "rm -rf /"), "not_found")
        self.assertEqual(self.code(self.h.dependency_action, "other", "install"), "not_found")

    def test_install_and_audit(self):
        self.h.dependency_action("sunshine", "install", "admin")
        self.installer.start_install.assert_called_once()

    def test_stop_and_uninstall_are_blocked_while_streaming_is_in_use(self):
        with mock.patch.object(self.h.store, "active", return_value=[{"mechanism": m.REMOTE_STREAMING}]):
            self.assertEqual(self.code(self.h.dependency_action, "sunshine", "uninstall"), "streaming_in_use")
            self.assertEqual(self.code(self.h.dependency_action, "sunshine", "stop"), "streaming_in_use")
        self.installer.uninstall.assert_not_called()

    def test_start_failure_is_an_error_not_a_silent_success(self):
        self.installer.start_service.return_value = False
        self.assertEqual(self.code(self.h.dependency_action, "sunshine", "start"), "start_failed")


if __name__ == "__main__":
    unittest.main()
