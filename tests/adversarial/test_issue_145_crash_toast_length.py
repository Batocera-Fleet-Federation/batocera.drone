"""Adversarial UAT for issue #145: crash toasts must stay short and actionable.

Issue #145: the too-many-controllers crash toast listed every connected
device plus the hostname, so EmulationStation dropped the unplug instruction
before a player could read it.

Invariants taken from the issue (not from the current implementation):

- Two toasts: what crashed, then what to do.
- With more than eight joystick-class devices and a memory-corruption crash,
  the toasts carry the controller *count* and a short unplug/relaunch action.
- Connected controller names never appear in the toast text.
- The details line is ``Details: Debug > Game Crashes.`` with no hostname.
- Device names (and hostname) remain on the Game Crashes history record.
- Every toast is capped at about 80 characters; overflow uses an ellipsis.
- Other crash causes do not grow a controller-name list just because many
  pads are plugged in.

The worker's unit test that ``build_messages`` omits "GameCube" is vacuous
once device names are no longer a parameter. These tests feed a fake
``SYS_INPUT_ROOT`` with more than eight ``js*`` nodes and assert through
``watch()``.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import crash_history
from app.device.fix_assets import game_crash_notifier
from app.drone_api import Settings


# Distinctive names from the issue's reproduction. Any of these in a toast is a
# regression of the original "lists every controller name" failure.
ISSUE_DEVICE_NAMES = [
    "Wii U GameCube Adapter Port 1",
    "Wii U GameCube Adapter Port 2",
    "DragonRise Inc. Generic USB Joystick",
    "DragonRise Inc. Generic USB Joystick",
    "DragonRise Inc. Generic USB Joystick",
    "DragonRise Inc. Generic USB Joystick",
    "Wii U GameCube Adapter Port 3",
    "Wii U GameCube Adapter Port 4",
    "Unknown SindenLightgun Mouse",
    "Sony Interactive Entertainment Wireless Controller",
    "Sony Interactive Entertainment Wireless Controller",
    "Sinden lightgun",
]

HOST_MARKERS = (
    "GameCube",
    "DragonRise",
    "Sinden",
    "SindenLightgun",
    "Wireless Controller",
    "living-room-pi",
    "batocera",
    "Admin >",
)

MEMORY_CORRUPTION_LOGS = (
    "stack smashing detected",
    "segmentation fault",
    "core dumped",
    "Aborted",
)

OTHER_CAUSE_LOGS = (
    "illegal instruction",
    "bus error",
    "Failed to load content",
)

DETAILS = "Details: Debug > Game Crashes."
TOAST_LIMIT = 80


def _settings(root: Path) -> Settings:
    env = {
        "USERDATA_ROOT": str(root),
        "ROMS_ROOT": str(root / "roms"),
        "BIOS_ROOT": str(root / "bios"),
        "SAVES_ROOT": str(root / "saves"),
        "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
        "DRONE_DEVICE_ID": "issue-145-adversarial",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        return Settings.from_env()


def _write_js_tree(root: Path, names) -> Path:
    sys_input = root / "sys_class_input"
    sys_input.mkdir(parents=True)
    for index, name in enumerate(names):
        device = sys_input / f"js{index}" / "device"
        device.mkdir(parents=True)
        (device / "name").write_text(name + "\n", encoding="utf-8")
    return sys_input


def _write_js_tree_malformed(root: Path) -> Path:
    """More than eight js* nodes, including unreadable and blank names."""
    sys_input = root / "sys_class_input"
    sys_input.mkdir(parents=True)
    names = ["Pad Alpha", "Pad Beta", "Pad Gamma", "Pad Delta", "Pad Epsilon"]
    for index, name in enumerate(names):
        device = sys_input / f"js{index}" / "device"
        device.mkdir(parents=True)
        (device / "name").write_text(name + "\n", encoding="utf-8")
    blank = sys_input / "js5" / "device"
    blank.mkdir(parents=True)
    (blank / "name").write_text("   \n", encoding="utf-8")
    missing = sys_input / "js6" / "device"
    missing.mkdir(parents=True)
    # js7 is a bare node (no device/name) — joystick_names falls back to "js7".
    (sys_input / "js7").mkdir()
    extra = sys_input / "js8" / "device"
    extra.mkdir(parents=True)
    (extra / "name").write_text("Evil, Hostname-looking batocera pad\n", encoding="utf-8")
    return sys_input


def _crash_stderr(signature: str = "stack smashing detected") -> str:
    return f"previous launch noise\n*** {signature} ***: terminated\n"


def _run_watch(
    tmp: Path,
    *,
    names=None,
    sys_input=None,
    stderr_text=None,
    rom="/userdata/roms/snes/Super Mario World.zip",
    system="snes",
    hostname="living-room-pi",
    stdout_text="",
    stdout_offset=0,
):
    """Drive watch() with a fake joystick tree and capture toasts + history."""
    if sys_input is None:
        sys_input = _write_js_tree(tmp, names if names is not None else ISSUE_DEVICE_NAMES)
    stderr_path = tmp / "stderr.log"
    stdout_path = tmp / "stdout.log"
    stderr_path.write_text(stderr_text if stderr_text is not None else _crash_stderr(), encoding="utf-8")
    stdout_path.write_text(stdout_text, encoding="utf-8")
    history = tmp / "system" / "game-crash-notifier" / "history.jsonl"
    config_path = tmp / "system" / "game-crash-notifier" / "config.json"
    captured = []

    def capture(messages, wait_seconds=30, gap_seconds=6):
        captured.append(list(messages))
        return len(messages)

    state = {
        "pid": 1,
        "start_time": None,
        "system": system,
        "rom": rom,
        "emulator": "libretro",
        "core": "snes9x",
        "started": 1000,
        "start_uptime": None,
        "stderr_offset": 0,
        "stdout_offset": stdout_offset,
    }
    with mock.patch.object(game_crash_notifier, "LAUNCH_STDERR", stderr_path), \
         mock.patch.object(game_crash_notifier, "LAUNCH_STDOUT", stdout_path), \
         mock.patch.object(game_crash_notifier, "HISTORY_PATH", history), \
         mock.patch.object(game_crash_notifier, "LOG_PATH", tmp / "notifier.log"), \
         mock.patch.object(game_crash_notifier, "CONFIG_PATH", config_path), \
         mock.patch.object(game_crash_notifier, "SYS_INPUT_ROOT", sys_input), \
         mock.patch.object(game_crash_notifier, "launcher_is_alive", return_value=False), \
         mock.patch.object(game_crash_notifier, "read_dmesg", return_value=""), \
         mock.patch.object(game_crash_notifier.time, "time", return_value=1005), \
         mock.patch.object(game_crash_notifier.socket, "gethostname", return_value=hostname), \
         mock.patch.object(game_crash_notifier, "send_notifications", side_effect=capture) as notify:
        game_crash_notifier.watch(state)
    return captured, history, notify


class Issue145TooManyControllersToastTests(unittest.TestCase):
    """End-to-end: stack-smash with 12 named joysticks must not list those names."""

    def test_watch_posts_short_actionable_toasts_and_keeps_names_in_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured, history_path, notify = _run_watch(root)

            notify.assert_called_once()
            self.assertEqual(len(captured), 1, "watch() must post exactly one toast pair")
            first, second = captured[0]
            joined = first + "\n" + second

            self.assertLessEqual(len(first), TOAST_LIMIT, f"first toast too long: {first!r}")
            self.assertLessEqual(len(second), TOAST_LIMIT, f"second toast too long: {second!r}")

            self.assertIn("Super Mario World", first)
            self.assertIn("crashed", first.lower())

            self.assertRegex(joined, r"12")
            self.assertRegex(second, r"(?i)unplug")
            self.assertRegex(second, r"(?i)relaunch")
            self.assertIn(DETAILS, second)
            self.assertFalse(
                second.endswith("..."),
                "the too-many-controllers action toast must be fully visible, "
                f"not truncated: {second!r}",
            )

            for marker in HOST_MARKERS:
                self.assertNotIn(
                    marker,
                    joined,
                    f"{marker!r} leaked into toast text {joined!r}",
                )

            record = json.loads(history_path.read_text(encoding="utf-8").splitlines()[-1])
            names_field = " ".join(record["joysticks"])
            self.assertEqual(record["joystick_count"], 12)
            self.assertIn("Wii U GameCube Adapter Port 1", names_field)
            self.assertIn("DragonRise", names_field)
            self.assertIn("Sinden", names_field)
            self.assertIn("Wireless Controller", names_field)
            self.assertEqual(record["hostname"], "living-room-pi")
            self.assertEqual(record["toasts"], [first, second])

            listed = crash_history.list_crashes(_settings(root))["crashes"]
            self.assertEqual(len(listed), 1)
            self.assertTrue(any("GameCube" in name for name in listed[0]["joysticks"]))
            page_toasts = " ".join(listed[0]["toasts"])
            self.assertNotIn("GameCube", page_toasts)
            self.assertNotIn("living-room-pi", page_toasts)

    def test_history_action_stays_the_diagnostic_action_not_the_device_list(self) -> None:
        """Issue: leave the crash record unchanged; only the toast drops names."""
        with tempfile.TemporaryDirectory() as tmp:
            captured, history_path, _ = _run_watch(Path(tmp))
            record = json.loads(history_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertNotIn("GameCube", record["action"])
            self.assertNotIn("DragonRise", record["action"])
            self.assertNotIn(", ".join(ISSUE_DEVICE_NAMES[:3]), record["action"])
            self.assertIn("unplug", record["action"].lower())
            first, second = captured[0]
            self.assertNotEqual(record["action"], second)

    def test_threshold_is_strictly_more_than_eight(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        verdict = game_crash_notifier.assess_launch("stack smashing detected", 5, "", config)
        at_limit = game_crash_notifier.build_messages("snes", "Game.zip", verdict, 8, config)
        over = game_crash_notifier.build_messages("snes", "Game.zip", verdict, 9, config)
        self.assertTrue(any("9" in text and "controller" in text.lower() for text in over))
        self.assertFalse(
            any(re.search(r"\b8\s+controllers?\b", text, re.I) for text in at_limit),
            f"exactly eight joysticks must not trigger the too-many hint: {at_limit!r}",
        )
        for text in at_limit + over:
            self.assertLessEqual(len(text), TOAST_LIMIT)

    def test_memory_corruption_signals_also_get_the_count_hint(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        for log in MEMORY_CORRUPTION_LOGS:
            with self.subTest(log=log):
                verdict = game_crash_notifier.assess_launch(log, 5, "", config)
                first, second = game_crash_notifier.build_messages(
                    "snes", "Super Mario World.zip", verdict, 12, config)
                self.assertIn("12", second)
                self.assertRegex(second, r"(?i)unplug")
                self.assertIn(DETAILS, second)
                self.assertLessEqual(len(first), TOAST_LIMIT)
                self.assertLessEqual(len(second), TOAST_LIMIT)
                self.assertFalse(second.endswith("..."), second)

        segv = game_crash_notifier.assess_launch(
            "", 40, "", config, "Emulator terminated by signal (11)\n")
        _, second = game_crash_notifier.build_messages("snes", "Game.zip", segv, 12, config)
        self.assertIn("12", second)
        self.assertIn(DETAILS, second)

    def test_non_corruption_causes_do_not_list_controllers_even_with_twelve_pads(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        for log in OTHER_CAUSE_LOGS:
            with self.subTest(log=log):
                verdict = game_crash_notifier.assess_launch(log, 5, "", config)
                first, second = game_crash_notifier.build_messages(
                    "snes", "Super Mario World.zip", verdict, 12, config)
                self.assertNotRegex(second, r"(?i)controller")
                self.assertNotIn("GameCube", first + second)
                self.assertIn(DETAILS, second)
                self.assertLessEqual(len(first), TOAST_LIMIT)
                self.assertLessEqual(len(second), TOAST_LIMIT)

    def test_watch_with_unreadable_joystick_names_still_omits_them_from_toasts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_input = _write_js_tree_malformed(root)
            captured, history_path, _ = _run_watch(root, sys_input=sys_input)
            first, second = captured[0]
            joined = first + second
            self.assertNotIn("Pad Alpha", joined)
            self.assertNotIn("batocera", joined)
            self.assertNotIn("js7", joined)
            record = json.loads(history_path.read_text(encoding="utf-8").splitlines()[-1])
            self.assertGreater(record["joystick_count"], 8)
            history_names = " ".join(record["joysticks"])
            self.assertIn("Pad Alpha", history_names)
            self.assertIn("batocera", history_names)

    def test_empty_and_missing_rom_still_fit_the_length_cap(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        verdict = game_crash_notifier.assess_launch("stack smashing detected", 5, "", config)
        for rom in ("", None, "/", "....", "a" * 200 + ".zip"):
            with self.subTest(rom=rom):
                messages = game_crash_notifier.build_messages("snes", rom, verdict, 12, config)
                self.assertEqual(len(messages), 2)
                for text in messages:
                    self.assertLessEqual(len(text), TOAST_LIMIT)
                    self.assertNotIn("living-room-pi", text)
                self.assertIn("12", messages[1])
                self.assertIn(DETAILS, messages[1])


class Issue145ToastLengthGuardTests(unittest.TestCase):
    """Backstop: every cause's toast stays glanceable; truncation uses an ellipsis."""

    def test_every_assessed_cause_toast_is_at_most_80_characters(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        long_rom = "/userdata/roms/snes/" + ("Extremely Long Game Title " * 8) + ".zip"
        samples = []
        for log in MEMORY_CORRUPTION_LOGS + OTHER_CAUSE_LOGS:
            samples.append(game_crash_notifier.assess_launch(log, 5, "", config))
        samples.append(game_crash_notifier.assess_launch(
            "", 3, "Out of memory: Killed process 9", config))
        samples.append(game_crash_notifier.assess_launch(
            "", 3, "", config, "Exiting configgen with status 9\n"))
        samples.append(game_crash_notifier.assess_launch(
            "", 40, "", config, "Emulator terminated by signal (11)\n"))
        samples.append(game_crash_notifier.assess_launch(
            "", 40, "", config, "Emulator terminated by signal (4)\n"))
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "/usr/lib/python3/site-packages/configgen/emulatorlauncher.py", '
            "line 1, in <module>\nKeyError: 'core'\n"
        )
        samples.append(game_crash_notifier.assess_launch(traceback, 5, "", config))

        self.assertTrue(all(item["crashed"] for item in samples))
        for verdict in samples:
            for joysticks in (0, 8, 9, 12, 99):
                for message in game_crash_notifier.build_messages(
                    "snes", long_rom, verdict, joysticks, config
                ):
                    self.assertLessEqual(
                        len(message),
                        TOAST_LIMIT,
                        f"sig={verdict['signature']!r} js={joysticks} -> {message!r}",
                    )
                    self.assertNotIn(" on ", message)
                    self.assertNotIn("Admin >", message)

    def test_toast_text_boundaries(self) -> None:
        limit = getattr(game_crash_notifier, "TOAST_MAX_CHARS", TOAST_LIMIT)
        self.assertLessEqual(limit, TOAST_LIMIT + 5)
        self.assertGreaterEqual(limit, TOAST_LIMIT - 5)
        self.assertEqual(game_crash_notifier.toast_text(""), "")
        exact = "n" * limit
        self.assertEqual(game_crash_notifier.toast_text(exact), exact)
        overflow = game_crash_notifier.toast_text("n" * (limit + 1))
        self.assertEqual(len(overflow), limit)
        self.assertTrue(overflow.endswith("..."))
        self.assertEqual(overflow, "n" * (limit - 3) + "...")
        # Trailing spaces at the cut must not push the result past the cap.
        padded = game_crash_notifier.toast_text(("n" * (limit - 10)) + (" " * 20) + "TAIL")
        self.assertLessEqual(len(padded), limit)
        self.assertTrue(padded.endswith("..."))
        self.assertNotIn("TAIL", padded)

    def test_details_line_never_includes_hostname_even_if_gethostname_is_called(self) -> None:
        config = {"short_session_seconds": 15, "joystick_hint_threshold": 8}
        verdict = game_crash_notifier.assess_launch("stack smashing detected", 5, "", config)
        with mock.patch.object(game_crash_notifier.socket, "gethostname", return_value="batocera"):
            messages = game_crash_notifier.build_messages(
                "snes", "Super Mario World.zip", verdict, 12, config)
        blob = " | ".join(messages)
        self.assertNotIn("batocera", blob)
        self.assertNotIn(" on ", blob)
        self.assertIn(DETAILS, blob)

    def test_build_messages_does_not_take_device_names_or_hostname(self) -> None:
        names = game_crash_notifier.build_messages.__code__.co_varnames[
            : game_crash_notifier.build_messages.__code__.co_argcount
        ]
        self.assertNotIn("device_names", names)
        self.assertNotIn("hostname", names)
        self.assertNotIn("names", names)


if __name__ == "__main__":
    unittest.main()
