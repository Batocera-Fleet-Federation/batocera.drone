"""Security regression: Switch NAND/Ryujinx saves must not bypass profile remapping via the 'system' layout."""
import hashlib
import io
import json
import tempfile
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


class SwitchNandLayoutBypassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.settings = SimpleNamespace(
            saves_root=self.root / 'saves',
            userdata_root=self.root,
            device_id='local',
        )
        self.title = '01006A800016E000'
        self.item = {
            'system': 'switch',
            'rom': f'Smash [{self.title}].nsp',
            'stem': f'Smash [{self.title}]',
            'filename': f'Smash [{self.title}].nsp',
            'ids': [self.title],
        }
        self.peer = {
            'drone_id': 'remote',
            'name': 'Remote Drone',
            'certificate_fingerprint': 'a' * 64,
        }
        self.addCleanup(saves._CHOICES.clear)

    def _malicious_group(self, path, content=b'peer save'):
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

    def test_malicious_peer_cannot_bypass_switch_profile_remapping_via_system_layout(self):
        """Regression for Switch save import: peer must not bypass local profile remapping using layout='system'."""
        # Create two local Switch profiles so Switch NAND destination is ambiguous and must be rejected.
        eden_root = saves.roots(self.settings, self.item)['eden-config']
        for uid in ('B' * 32, 'C' * 32):
            p = eden_root / 'user/save/0000000000000000' / uid / self.title / 'old.bin'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'local')

        attacker_uuid = 'A' * 32
        group = self._malicious_group(f'nand/user/save/0000000000000000/{attacker_uuid}/{self.title}')
        with mock.patch.object(saves, 'tls_peer', return_value=self.peer), mock.patch.object(
            saves, '_open_peer', return_value=_Response(json.dumps({'saves': [group]}).encode())
        ):
            with self.assertRaises(saves.SaveError):
                saves.discover(self.settings, self.item, 'remote')

    def test_malicious_peer_cannot_bypass_ryujinx_slot_mapping_via_system_layout(self):
        """Regression for Switch save import: peer must not write a raw Ryujinx bis slot via layout='system'."""
        group = self._malicious_group(f'ryujinx/bis/user/save/0000000000000009/{self.title}')
        with self.assertRaises(saves.SaveError):
            saves.validate_group(self.settings, self.item, group)


if __name__ == '__main__':
    unittest.main()
