"""Issue #192: identity provisioning must not silently succeed on a mock userdata_root.

Issue #192 (filed from a #170 probe): with Settings whose userdata_root is a
unittest mock and device_id='AA:BB:CC:DD:EE:FF',

- identity_provisioning._choose_device_id returned the configured device_id
  instead of minting a fresh random id,
- swarm_reset._clear_pinned_peer_certificates returned 0 (reported as success),
- _retire_device_id returned without retiring the old id.

Those short-circuits violate the clone invariants: a new identity gets a fresh
logical id, inherited pins are actually cleared before provisioning is treated
complete, and the copied device-id file is retired so DRONE_DEVICE_ID cannot
revive it.

Expected behavior, derived from the issue and docs/drone-identity.md before
relying on the patch:

- A unittest mock is not a filesystem path. The three helpers must fail loudly
  (TypeError: userdata_root must be a filesystem path) rather than substitute
  settings.device_id, report zero pins cleared, or return as if retirement ran.
- Every mock flavour that can impersonate Path or str (spec, spec_set, autospec,
  AsyncMock, NonCallable*, MagicMock subclass) is rejected the same way.
- Env overrides (DRONE_DEVICE_ID_FILE, DRONE_STATE_DATABASE_FILE) are not a
  license to treat a mock userdata_root as a real tree.
- The working directory stays empty: pathlib must not stringify the mock into a
  relative filename or operate on ./system/drone-app.
- A real userdata_root still mints a new id (not the configured one), retires a
  persisted file, and reports 0 only when the pin directory is genuinely absent.

Each rejection case runs from its own empty temporary cwd.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning as identity
from app.common.device_identity import random_device_id
from app.transfer import swarm_reset


CONFIGURED_ID = "AA:BB:CC:DD:EE:FF"
REJECTION = "userdata_root must be a filesystem path"
MAC_SHAPED = r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$"


class _SpecSubclass(mock.MagicMock):
    """User-defined MagicMock subclass; its type lives in this test module."""


class _PathLikeRoot:
    """A non-mock os.PathLike; it is a legitimate root and must not be over-rejected."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __fspath__(self) -> str:
        return str(self._path)


MOCK_ROOT_FLAVOURS = {
    "MagicMock()": lambda: mock.MagicMock(),
    "Mock()": lambda: mock.Mock(),
    "NonCallableMock()": lambda: mock.NonCallableMock(),
    "NonCallableMagicMock()": lambda: mock.NonCallableMagicMock(),
    "AsyncMock()": lambda: mock.AsyncMock(),
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "Mock(spec=Path)": lambda: mock.Mock(spec=Path),
    "NonCallableMock(spec=Path)": lambda: mock.NonCallableMock(spec=Path),
    "NonCallableMagicMock(spec=Path)": lambda: mock.NonCallableMagicMock(spec=Path),
    "AsyncMock(spec=Path)": lambda: mock.AsyncMock(spec=Path),
    "MagicMock(spec_set=Path)": lambda: mock.MagicMock(spec_set=Path),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
    "create_autospec(Path)": lambda: mock.create_autospec(Path),
    "MagicMock subclass with spec=Path": lambda: _SpecSubclass(spec=Path),
    "MagicMock(spec=str)": lambda: mock.MagicMock(spec=str),
    "create_autospec(str, instance=True)": lambda: mock.create_autospec(str, instance=True),
}


def _mock_named_entries(directory: Path) -> list[str]:
    names: list[str] = []
    if not directory.exists():
        return names
    for path in directory.rglob("*"):
        name = path.name
        if "MagicMock" in name or "NonCallable" in name or name.startswith("<Mock"):
            names.append(str(path.relative_to(directory)))
    return names


class _CwdSandbox(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()
        self._previous_cwd = os.getcwd()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self._tmpdir.name).resolve()
        os.chdir(self.cwd)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmpdir.cleanup()
        self._env.stop()

    def _settings(self, userdata_root, device_id: str = CONFIGURED_ID) -> SimpleNamespace:
        return SimpleNamespace(userdata_root=userdata_root, device_id=device_id)

    def _assert_cwd_clean(self) -> None:
        self.assertEqual(_mock_named_entries(self.cwd), [])
        self.assertFalse((self.cwd / "system").exists(), msg="cwd gained a system/ tree")
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [])


class MockRootHelperRejectionTests(_CwdSandbox):
    """The #192 reproduction: mock userdata_root must not no-op or substitute the old id."""

    def test_issue_reproduction_choose_device_id_does_not_return_configured_id(self) -> None:
        settings = self._settings(mock.MagicMock())
        with self.assertRaisesRegex(TypeError, REJECTION):
            identity._choose_device_id(settings, {})
        self._assert_cwd_clean()

    def test_issue_reproduction_retire_device_id_does_not_return_silently(self) -> None:
        settings = self._settings(mock.MagicMock())
        with self.assertRaisesRegex(TypeError, REJECTION):
            identity._retire_device_id(settings)
        self._assert_cwd_clean()

    def test_issue_reproduction_clear_pins_does_not_report_zero_success(self) -> None:
        settings = self._settings(mock.MagicMock())
        with self.assertRaisesRegex(TypeError, REJECTION):
            swarm_reset._clear_pinned_peer_certificates(settings)
        self._assert_cwd_clean()

    def test_every_mock_flavour_is_rejected_by_all_three_helpers(self) -> None:
        for label, factory in MOCK_ROOT_FLAVOURS.items():
            with self.subTest(mock=label, helper="_choose_device_id"):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    identity._choose_device_id(self._settings(factory()), {})
                self._assert_cwd_clean()
            with self.subTest(mock=label, helper="_retire_device_id"):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    identity._retire_device_id(self._settings(factory()))
                self._assert_cwd_clean()
            with self.subTest(mock=label, helper="_clear_pinned_peer_certificates"):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    swarm_reset._clear_pinned_peer_certificates(self._settings(factory()))
                self._assert_cwd_clean()

    def test_choose_device_id_does_not_return_the_configured_id_even_if_a_caller_swallows_typeerror(self) -> None:
        # Guard the invariant independently of exception type: the helper must not
        # hand back AA:BB:CC:DD:EE:FF the way the mock short-circuit did.
        settings = self._settings(mock.MagicMock())
        chosen = None
        try:
            chosen = identity._choose_device_id(settings, {})
        except TypeError:
            chosen = None
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertIsNone(chosen)

    def test_empty_and_missing_journal_new_device_id_do_not_bypass_the_guard(self) -> None:
        settings = self._settings(mock.MagicMock())
        for journal in ({}, {"new_device_id": ""}, {"new_device_id": None}, {"new_device_id": 0}):
            with self.subTest(journal=journal):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    identity._choose_device_id(settings, journal)
                self._assert_cwd_clean()


class MockRootMustNotTouchCwdArtifactsTests(_CwdSandbox):
    """Seeded files under cwd must survive a mocked root (the #169 cwd blast radius)."""

    def test_retire_does_not_unlink_a_seeded_cwd_device_id(self) -> None:
        seeded = self.cwd / "system" / "drone-app" / "device-id"
        seeded.parent.mkdir(parents=True)
        seeded.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        with self.assertRaisesRegex(TypeError, REJECTION):
            identity._retire_device_id(self._settings(mock.MagicMock(spec=Path)))
        self.assertTrue(seeded.exists())
        self.assertEqual(seeded.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_choose_does_not_write_a_device_id_under_cwd(self) -> None:
        with self.assertRaisesRegex(TypeError, REJECTION):
            identity._choose_device_id(self._settings(mock.MagicMock(spec=Path)), {})
        self.assertFalse((self.cwd / "system").exists())

    def test_clear_pins_does_not_unlink_seeded_cwd_certificates(self) -> None:
        pin = self.cwd / "system" / "drone-app" / "local-peer-certs" / "peer.crt"
        pin.parent.mkdir(parents=True)
        pin.write_text("-----BEGIN CERTIFICATE-----\ninherited\n", encoding="utf-8")
        with self.assertRaisesRegex(TypeError, REJECTION):
            swarm_reset._clear_pinned_peer_certificates(self._settings(mock.MagicMock(spec=Path)))
        self.assertTrue(pin.exists())
        self.assertIn("inherited", pin.read_text(encoding="utf-8"))


class EnvOverrideDoesNotBypassMockRootTests(_CwdSandbox):
    def test_drone_device_id_file_override_does_not_let_choose_reuse_or_write(self) -> None:
        override = self.cwd / "override-device-id"
        override.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"DRONE_DEVICE_ID_FILE": str(override)}, clear=True):
            with self.assertRaisesRegex(TypeError, REJECTION):
                identity._choose_device_id(self._settings(mock.MagicMock()), {})
        self.assertEqual(override.read_text(encoding="utf-8"), CONFIGURED_ID + "\n")

    def test_drone_device_id_file_override_does_not_let_retire_unlink(self) -> None:
        override = self.cwd / "override-device-id"
        override.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {"DRONE_DEVICE_ID_FILE": str(override)}, clear=True):
            with self.assertRaisesRegex(TypeError, REJECTION):
                identity._retire_device_id(self._settings(mock.MagicMock()))
        self.assertTrue(override.exists())

    def test_drone_state_database_file_override_does_not_let_pin_clear_report_zero(self) -> None:
        db = self.cwd / "state.sqlite3"
        with mock.patch.dict("os.environ", {"DRONE_STATE_DATABASE_FILE": str(db)}, clear=True):
            with self.assertRaisesRegex(TypeError, REJECTION):
                swarm_reset._clear_pinned_peer_certificates(self._settings(mock.MagicMock()))
        self.assertFalse(db.exists())
        self._assert_cwd_clean()


class JournalResumeMustNotSubstituteConfiguredIdTests(_CwdSandbox):
    """Resume may return a previously minted id; it must never fall back to settings.device_id."""

    def test_resume_id_is_not_replaced_by_the_configured_device_id(self) -> None:
        minted = "02:11:22:33:44:55"
        settings = self._settings(mock.MagicMock(), device_id=CONFIGURED_ID)
        try:
            chosen = identity._choose_device_id(settings, {"new_device_id": minted})
        except TypeError as error:
            self.assertRegex(str(error), REJECTION)
            self._assert_cwd_clean()
            return
        self.assertEqual(chosen, minted)
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self._assert_cwd_clean()


class RealRootPositiveControlTests(_CwdSandbox):
    """A silent early-return on every input would make rejection tests pass vacuously."""

    def test_choose_device_id_mints_a_fresh_id_not_the_configured_one(self) -> None:
        root = self.cwd / "userdata"
        (root / "system" / "drone-app").mkdir(parents=True)
        persisted = root / "system" / "drone-app" / "device-id"
        persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        settings = self._settings(root)
        chosen = identity._choose_device_id(settings, {})
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertRegex(chosen, MAC_SHAPED)
        self.assertEqual(persisted.read_text(encoding="utf-8").strip(), chosen)
        first_octet = int(chosen.split(":", 1)[0], 16)
        self.assertEqual(first_octet & 0x02, 0x02, msg="fresh id must be locally administered")
        self.assertEqual(first_octet & 0x01, 0, msg="fresh id must be unicast")

    def test_choose_device_id_accepts_pathlike_root(self) -> None:
        root = self.cwd / "userdata-pathlike"
        (root / "system" / "drone-app").mkdir(parents=True)
        settings = self._settings(_PathLikeRoot(root), device_id=CONFIGURED_ID)
        chosen = identity._choose_device_id(settings, {})
        self.assertNotEqual(chosen, CONFIGURED_ID)
        self.assertRegex(chosen, MAC_SHAPED)

    def test_retire_device_id_unlinks_the_persisted_file(self) -> None:
        root = self.cwd / "userdata-retire"
        persisted = root / "system" / "drone-app" / "device-id"
        persisted.parent.mkdir(parents=True)
        persisted.write_text(CONFIGURED_ID + "\n", encoding="utf-8")
        identity._retire_device_id(self._settings(root))
        self.assertFalse(persisted.exists())

    def test_retire_device_id_is_idempotent_when_the_file_is_already_gone(self) -> None:
        root = self.cwd / "userdata-retire-missing"
        root.mkdir()
        identity._retire_device_id(self._settings(root))

    def test_clear_pins_returns_zero_only_when_the_directory_is_absent(self) -> None:
        root = self.cwd / "userdata-no-pins"
        root.mkdir()
        self.assertEqual(swarm_reset._clear_pinned_peer_certificates(self._settings(root)), 0)

    def test_clear_pins_unlinks_real_certificates_and_counts_them(self) -> None:
        root = self.cwd / "userdata-pins"
        pin_dir = root / "system" / "drone-app" / "local-peer-certs"
        pin_dir.mkdir(parents=True)
        (pin_dir / "a.crt").write_text("a", encoding="utf-8")
        (pin_dir / "b.crt").write_text("b", encoding="utf-8")
        self.assertEqual(swarm_reset._clear_pinned_peer_certificates(self._settings(root)), 2)
        self.assertEqual(list(pin_dir.iterdir()), [])

    def test_two_real_roots_do_not_share_a_minted_id(self) -> None:
        first = self.cwd / "one"
        second = self.cwd / "two"
        for root in (first, second):
            (root / "system" / "drone-app").mkdir(parents=True)
        left = identity._choose_device_id(self._settings(first), {})
        right = identity._choose_device_id(self._settings(second), {})
        self.assertNotEqual(left, right)
        self.assertNotEqual(left, CONFIGURED_ID)
        self.assertNotEqual(right, CONFIGURED_ID)


class MalformedRootRejectionTests(_CwdSandbox):
    def test_none_and_int_roots_raise_typeerror_not_silent_success(self) -> None:
        for value in (None, 123, object(), ["userdata"]):
            with self.subTest(value=type(value).__name__):
                with self.assertRaisesRegex(TypeError, REJECTION):
                    identity._choose_device_id(self._settings(value), {})
                with self.assertRaisesRegex(TypeError, REJECTION):
                    identity._retire_device_id(self._settings(value))
                with self.assertRaisesRegex(TypeError, REJECTION):
                    swarm_reset._clear_pinned_peer_certificates(self._settings(value))

    def test_random_device_id_is_not_the_configured_fixture_id(self) -> None:
        # Domain check: uniqueness of a mint must not collapse onto the issue's example MAC.
        self.assertNotEqual(random_device_id(), CONFIGURED_ID)


if __name__ == "__main__":
    unittest.main()
