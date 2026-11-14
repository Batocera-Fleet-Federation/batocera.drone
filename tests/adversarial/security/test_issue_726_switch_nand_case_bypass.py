"""Security regression: Switch profile trees must not bypass remapping via case variants.

Issue #726: "Only saves that exist for the rom on remote drones should be an option
for download." A paired peer must not offer a Switch NAND or Ryujinx profile tree
through the generic system layout. The denylist compares path parts to the exact
strings ``nand`` and ``bis``, so ``NAND``, ``Nand``, ``BIS`` and ``Bis`` are
accepted, discovered as a downloadable save, and on a case-insensitive save volume
(this macOS temp volume folds case) written into the live emulator tree under an
attacker-chosen profile, skipping local profile/slot remapping.

Regression protected: Switch NAND/Ryujinx saves must not bypass profile remapping
via the system layout when ``nand`` or ``bis`` differ only by case.
Confidence: medium.
"""
import hashlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import game_saves as saves


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class SwitchNandCaseBypassTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            saves_root=Path('/no/such/saves'),
            userdata_root=Path('/no/such/userdata'),
            device_id='local',
        )
        self.title = '01006A800016E000'
        self.item = {
            'system': 'switch',
            'rom': f'Smash [{self.title}].nsp',
            'stem': f'Smash [{self.title}]',
            'filename': f'Smash [{self.title}].nsp',
            'ids': [self.title],
            'fingerprint': 'fp-local',
        }
        self.peer = {
            'drone_id': 'remote',
            'name': 'Remote Drone',
            'certificate_fingerprint': 'a' * 64,
        }
        self.addCleanup(saves._CHOICES.clear)

    def _group(self, path, content=b'peer-profile'):
        return {
            'id': hashlib.sha256(('system:' + path).encode()).hexdigest(),
            'layout': 'system',
            'path': path,
            'kind': 'directory',
            'files': [{
                'path': path + '/progress.bin',
                'size': len(content),
                'sha256': hashlib.sha256(content).hexdigest(),
            }],
        }

    def test_system_layout_rejects_nand_and_bis_case_variants(self):
        """A peer must not bypass Switch profile remapping by changing only the case of nand or bis."""
        attacker = 'A' * 32
        slot = '0000000000000009'
        paths = [
            f'NAND/user/save/0000000000000000/{attacker}/{self.title}',
            f'Nand/user/save/0000000000000000/{attacker}/{self.title}',
            f'eden/NAND/user/save/0000000000000000/{attacker}/{self.title}',
            f'yuzu/NAND/user/save/0000000000000000/{attacker}/{self.title}',
            f'ryujinx/BIS/user/save/{slot}/{self.title}',
            f'ryujinx/Bis/user/save/{slot}/{self.title}',
            f'BIS/user/save/{slot}/{self.title}',
        ]
        for path in paths:
            with self.subTest(path=path):
                with self.assertRaises(saves.SaveError):
                    saves.validate_group(self.settings, self.item, self._group(path))
                payload = json.dumps({'saves': [self._group(path)]}).encode()
                with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
                    saves, '_open_peer', return_value=_Response(payload)
                ):
                    with self.assertRaises(saves.SaveError):
                        saves.discover(self.settings, self.item, 'remote')


if __name__ == '__main__':
    unittest.main()
