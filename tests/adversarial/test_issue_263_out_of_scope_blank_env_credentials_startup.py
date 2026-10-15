"""Out-of-scope evidence from issue #263 testing: a blank DRONE_CREDENTIALS_FILE at startup.

The #263 fix makes ``DroneCredentialStore`` refuse a blank credentials path. ``Settings.from_env``
maps an empty ``DRONE_CREDENTIALS_FILE`` to ``Path('.')`` (``os.environ.get`` returns the empty
string, not the default), and ``create_server`` builds the store with no try/except. The result
is that a blank env value now aborts startup, where it previously started and silently derived a
database in the parent directory.

Whether a blank value should fall back to the default credentials file or fail loudly is a
product decision, so this is reported separately. The convention the rest of ``Settings`` already
follows is that a blank env value means "unset" (``DRONE_APP_USERNAME`` uses ``or None``, and
``DRONE_STATE_DATABASE_FILE`` is gated on truthiness). This suite asserts that convention.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.common.auth import DroneCredentialStore
from app.common.settings import Settings
from app.storage.state_store import database_path


class BlankCredentialsEnvStartupOutOfScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.userdata = self.root / "userdata"
        self.environment = {
            "USERDATA_ROOT": str(self.userdata),
            "ROMS_ROOT": str(self.root / "roms"),
            "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"),
            "DRONE_CREDENTIALS_FILE": "",
            "DRONE_DEVICE_ID": "adversarial-263",
        }

    def test_blank_credentials_env_resolves_to_the_default_credentials_file(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.credentials_file, self.userdata / "system" / "drone-app" / "credentials.json")

    def test_store_built_from_blank_credentials_env_constructs_at_startup(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            settings = Settings.from_env()
            store = DroneCredentialStore(
                settings.credentials_file,
                settings.username,
                settings.password,
                state_database_file=database_path(settings.userdata_root),
            )
        self.assertIsInstance(store, DroneCredentialStore)


if __name__ == "__main__":
    unittest.main()
