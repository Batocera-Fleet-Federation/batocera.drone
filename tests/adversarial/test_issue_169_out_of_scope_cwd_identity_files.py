"""Out-of-scope adversarial finding for issue #169: spec'd roots reach file ops in cwd.

The #169 root cause is ``Path(settings.userdata_root)`` turning a spec'd mock into
``Path('.')``. The state-DB symptom is in scope for #169 and is covered by
``test_issue_169_record_collision_spec_mock.py``. These identity and swarm-reset helpers
use the same wrapping for plain file operations, not SQLite:

- ``identity_provisioning._retire_device_id`` unlinks ``./system/drone-app/device-id``.
- ``swarm_reset._clear_pinned_peer_certificates`` unlinks every file under
  ``./system/drone-app/local-peer-certs``.
- ``identity_provisioning._choose_device_id`` creates ``./system/drone-app/device-id``.

Production roots are real paths, so this is a test-hygiene and blast-radius defect rather
than a production trigger. It is reported as out_of_scope because #169 names the state DB,
not file artifacts. Each assertion is evidence for a separate issue. The unlink finding is
the most serious: a spec'd root run from a directory that holds such a tree would delete
files there.

Each case runs from a fresh empty cwd, and seeded files are real files there, so the
evidence is observable without touching the repository.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.common import identity_provisioning
from app.transfer import swarm_reset


SPEC_ROOT_FACTORIES = {
    "MagicMock(spec=Path)": lambda: mock.MagicMock(spec=Path),
    "create_autospec(Path, instance=True)": lambda: mock.create_autospec(Path, instance=True),
}


class IdentityFilesSpecRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict("os.environ", {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    @contextlib.contextmanager
    def _fresh_cwd(self):
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            os.chdir(cwd)
            try:
                yield cwd
            finally:
                os.chdir(previous)

    def test_retire_device_id_does_not_unlink_cwd_file(self) -> None:
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(root=label), self._fresh_cwd() as cwd:
                seeded = cwd / "system" / "drone-app" / "device-id"
                seeded.parent.mkdir(parents=True)
                seeded.write_text("aa:bb:cc:dd:ee:ff\n", encoding="utf-8")
                settings = SimpleNamespace(userdata_root=factory(), device_id="aa:bb:cc:dd:ee:ff")

                identity_provisioning._retire_device_id(settings)

                self.assertTrue(seeded.exists(), msg="a spec'd root unlinked a file under the cwd")

    def test_choose_device_id_does_not_write_into_cwd(self) -> None:
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(root=label), self._fresh_cwd() as cwd:
                settings = SimpleNamespace(userdata_root=factory(), device_id="aa:bb:cc:dd:ee:ff")

                with contextlib.redirect_stdout(io.StringIO()):
                    identity_provisioning._choose_device_id(settings, {})

                self.assertEqual(sorted(p.name for p in cwd.iterdir()), [], msg="a spec'd root wrote into the cwd")

    def test_clear_pinned_peer_certificates_does_not_unlink_cwd_pins(self) -> None:
        for label, factory in SPEC_ROOT_FACTORIES.items():
            with self.subTest(root=label), self._fresh_cwd() as cwd:
                pin = cwd / "system" / "drone-app" / "local-peer-certs" / "peer.crt"
                pin.parent.mkdir(parents=True)
                pin.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
                settings = SimpleNamespace(userdata_root=factory())

                swarm_reset._clear_pinned_peer_certificates(settings)

                self.assertTrue(pin.exists(), msg="a spec'd root unlinked a pinned certificate under the cwd")


if __name__ == "__main__":
    unittest.main()
