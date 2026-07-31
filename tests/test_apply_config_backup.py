import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import apply_config_backup


def _make_archive(path: Path, members: dict) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            data = content.encode("utf-8")
            info.size = len(data)
            import io

            tar.addfile(info, io.BytesIO(data))


class ResolveRestoreDestinationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.userdata_root = self.root / "userdata"
        self.roms_root = self.root / "roms"
        self.saves_root = self.root / "saves"

    def tearDown(self):
        self._tmp.cleanup()

    def _resolve(self, arcname):
        return apply_config_backup._resolve_restore_destination(
            arcname, self.userdata_root, self.roms_root, self.saves_root
        )

    def test_manifest_is_skipped(self):
        self.assertIsNone(self._resolve("MANIFEST.txt"))

    def test_system_prefix_maps_under_userdata_system(self):
        dest = self._resolve("system/batocera.conf")
        self.assertEqual(dest, (self.userdata_root / "system" / "batocera.conf").resolve())

    def test_system_nested_configs_path(self):
        dest = self._resolve("system/configs/retroarch/retroarch.cfg")
        self.assertEqual(dest, (self.userdata_root / "system" / "configs" / "retroarch" / "retroarch.cfg").resolve())

    def test_roms_prefix_maps_under_roms_root(self):
        dest = self._resolve("roms/snes/gamelist.xml")
        self.assertEqual(dest, (self.roms_root / "snes" / "gamelist.xml").resolve())

    def test_saves_prefix_maps_under_saves_root(self):
        dest = self._resolve("saves/snes/game.srm")
        self.assertEqual(dest, (self.saves_root / "snes" / "game.srm").resolve())

    def test_unrecognized_prefix_is_rejected(self):
        self.assertIsNone(self._resolve("bios/somefile.bin"))

    def test_parent_traversal_is_rejected(self):
        self.assertIsNone(self._resolve("system/../../etc/passwd"))
        self.assertIsNone(self._resolve("roms/../../../etc/passwd"))
        self.assertIsNone(self._resolve("saves/../../outside.txt"))

    def test_empty_relative_component_is_rejected(self):
        self.assertIsNone(self._resolve("system/"))
        self.assertIsNone(self._resolve("roms/"))
        self.assertIsNone(self._resolve("saves/"))


class RestoreConfigBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.userdata_root = self.root / "userdata"
        self.roms_root = self.root / "roms"
        self.saves_root = self.root / "saves"
        self.archive_path = self.root / "backup.tar.gz"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, members, *, run_returncode=0):
        _make_archive(self.archive_path, members)
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=run_returncode, stdout="")

        with mock.patch("app.apply_config_backup.subprocess.run", side_effect=fake_run), \
             mock.patch("app.apply_config_backup.shutil.which", return_value="/usr/bin/batocera-es-swissknife"), \
             mock.patch("app.apply_config_backup.time.sleep"):
            result = apply_config_backup.restore_config_backup(
                self.archive_path, self.userdata_root, self.roms_root, self.saves_root
            )
        return result, calls

    def test_restores_files_to_their_mapped_roots(self):
        result, calls = self._run({
            "MANIFEST.txt": "manifest",
            "system/batocera.conf": "conf-bytes",
            "system/configs/retroarch/retroarch.cfg": "cfg-bytes",
            "roms/snes/gamelist.xml": "<gameList/>",
            "saves/snes/game.srm": "save-bytes",
        })
        self.assertEqual(result["restored_file_count"], 4)  # MANIFEST.txt excluded
        self.assertEqual(result["skipped_file_count"], 0)
        self.assertTrue(result["restarted_emulationstation"])
        self.assertEqual((self.userdata_root / "system" / "batocera.conf").read_text(), "conf-bytes")
        self.assertEqual(
            (self.userdata_root / "system" / "configs" / "retroarch" / "retroarch.cfg").read_text(), "cfg-bytes"
        )
        self.assertEqual((self.roms_root / "snes" / "gamelist.xml").read_text(), "<gameList/>")
        self.assertEqual((self.saves_root / "snes" / "game.srm").read_text(), "save-bytes")
        self.assertFalse((self.userdata_root / "system" / "MANIFEST.txt").exists())

    def test_kills_running_emulator_stops_and_starts_emulationstation(self):
        _result, calls = self._run({"system/batocera.conf": "conf-bytes"})
        self.assertIn(["/usr/bin/batocera-es-swissknife", "--emukill"], calls)
        self.assertIn([apply_config_backup.EMULATIONSTATION_SERVICE, "stop"], calls)
        self.assertIn(["batocera-save-overlay"], calls)
        self.assertIn([apply_config_backup.EMULATIONSTATION_SERVICE, "start"], calls)

    def test_restarts_even_when_overlay_save_fails(self):
        _make_archive(self.archive_path, {"system/batocera.conf": "conf-bytes"})
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            rc = 1 if command and command[0] == "batocera-save-overlay" else 0
            return mock.Mock(returncode=rc, stdout="overlay failure" if rc else "")

        with mock.patch("app.apply_config_backup.subprocess.run", side_effect=fake_run), \
             mock.patch("app.apply_config_backup.shutil.which", return_value=None), \
             mock.patch("app.apply_config_backup.time.sleep"):
            result = apply_config_backup.restore_config_backup(
                self.archive_path, self.userdata_root, self.roms_root, self.saves_root
            )
        self.assertEqual((self.userdata_root / "system" / "batocera.conf").read_text(), "conf-bytes")
        self.assertIn([apply_config_backup.EMULATIONSTATION_SERVICE, "start"], calls)
        self.assertTrue(result["restarted_emulationstation"])

    def test_retries_when_start_returns_without_emulationstation(self):
        _make_archive(self.archive_path, {"system/batocera.conf": "conf-bytes"})
        with mock.patch("app.apply_config_backup.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")) as run, \
             mock.patch("app.apply_config_backup._wait_for_emulationstation", side_effect=[False, True]), \
             mock.patch("app.apply_config_backup.shutil.which", return_value=None), \
             mock.patch("app.apply_config_backup.time.sleep"):
            apply_config_backup.restore_config_backup(
                self.archive_path, self.userdata_root, self.roms_root, self.saves_root
            )
        start_command = [apply_config_backup.EMULATIONSTATION_SERVICE, "start"]
        self.assertEqual([call.args[0] for call in run.call_args_list].count(start_command), 2)

    def test_raises_when_emulationstation_does_not_start_but_files_are_still_restored(self):
        _make_archive(self.archive_path, {"system/batocera.conf": "conf-bytes"})
        with mock.patch("app.apply_config_backup.subprocess.run", return_value=mock.Mock(returncode=1, stdout="")), \
             mock.patch("app.apply_config_backup.shutil.which", return_value=None), \
             mock.patch("app.apply_config_backup.time.sleep"):
            with self.assertRaises(RuntimeError):
                apply_config_backup.restore_config_backup(
                    self.archive_path, self.userdata_root, self.roms_root, self.saves_root
                )
        # The write itself is unaffected by the ES restart outcome -- already on disk.
        self.assertEqual((self.userdata_root / "system" / "batocera.conf").read_text(), "conf-bytes")

    def test_start_does_not_capture_output_via_pipe(self):
        _make_archive(self.archive_path, {"system/batocera.conf": "conf-bytes"})
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return mock.Mock(returncode=0, stdout="")

        with mock.patch("app.apply_config_backup.subprocess.run", side_effect=fake_run), \
             mock.patch("app.apply_config_backup.shutil.which", return_value=None), \
             mock.patch("app.apply_config_backup.time.sleep"):
            apply_config_backup.restore_config_backup(
                self.archive_path, self.userdata_root, self.roms_root, self.saves_root
            )
        start_calls = [kwargs for command, kwargs in calls if command == [apply_config_backup.EMULATIONSTATION_SERVICE, "start"]]
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(start_calls[0].get("stdout"), subprocess.DEVNULL)
        stop_calls = [kwargs for command, kwargs in calls if command == [apply_config_backup.EMULATIONSTATION_SERVICE, "stop"]]
        self.assertEqual(stop_calls[0].get("stdout"), subprocess.PIPE)

    def test_unsafe_and_unrecognized_members_are_skipped_not_restored(self):
        result, _calls = self._run({
            "system/batocera.conf": "conf-bytes",
            "bios/whatever.bin": "should-not-restore",
        })
        self.assertEqual(result["restored_file_count"], 1)
        self.assertEqual(result["skipped_file_count"], 1)
        self.assertEqual(result["skipped"][0]["path"], "bios/whatever.bin")
        self.assertFalse((self.userdata_root / "bios").exists())

    def test_cli_reads_json_request_file(self):
        _make_archive(self.archive_path, {"system/batocera.conf": "conf-bytes"})
        request_file = self.root / "apply-config-backup.request"
        request_file.write_text(
            (
                '{"archive_path": "%s", "userdata_root": "%s", "roms_root": "%s", "saves_root": "%s"}'
            )
            % (self.archive_path, self.userdata_root, self.roms_root, self.saves_root),
            encoding="utf-8",
        )
        with mock.patch("app.apply_config_backup.sys.argv", ["apply_config_backup.py", str(request_file)]), \
             mock.patch("app.apply_config_backup.subprocess.run", return_value=mock.Mock(returncode=0, stdout="")), \
             mock.patch("app.apply_config_backup.shutil.which", return_value=None), \
             mock.patch("app.apply_config_backup.time.sleep"):
            exit_code = apply_config_backup.main()
        self.assertEqual(exit_code, 0)
        self.assertEqual((self.userdata_root / "system" / "batocera.conf").read_text(), "conf-bytes")


if __name__ == "__main__":
    unittest.main()
