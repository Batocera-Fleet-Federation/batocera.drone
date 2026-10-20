"""Real identity signatures and download acceptance boundaries for issue #333."""
import base64
import copy
import hashlib
import io
import json
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from app.common.settings import Settings
from app.transfer import local_network, peer_connectivity, peer_download, signed_inventory as inventory
from app.web.handlers_peer import HandlersPeerMixin


class Response(io.BytesIO):
    headers = {}


class SignedInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        root = Path(cls.keys.name)
        cls.cert, cls.key = root / 'identity.crt', root / 'identity.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
                        '-subj', '/CN=source'], check=True, capture_output=True)
        cls.certificate = cls.cert.read_bytes()
        cls.pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cls.certificate.decode())).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        with mock.patch.dict('os.environ', {'USERDATA_ROOT': str(self.root),
                'ROMS_ROOT': str(self.root / 'roms'), 'BIOS_ROOT': str(self.root / 'bios'),
                'DRONE_DEVICE_ID': 'receiver'}, clear=True):
            self.settings = Settings.from_env()
        self.source = SimpleNamespace(device_id='source', drone_key_file=self.key)
        self.peer = local_network.save_paired_peer(self.settings, {
            'drone_id': 'source', 'reachable_url': 'http://source:8080',
            'certificate_fingerprint': self.pin})
        peer_connectivity._save_local_peer_certificate(self.settings, 'source', self.certificate.decode())
        self.asset = self.root / 'source-game'
        self.asset.write_bytes(b'authentic content')
        self.nonce = 'ab' * 32

    def envelope(self, asset='roms', system='nes', rel='game.bin', nonce=None, target=None):
        return inventory.sign_inventory(self.source, asset, system, rel, nonce or self.nonce, target or self.asset)

    def fetch_response(self, request, **kwargs):
        query = parse_qs(urlparse(request.full_url).query, keep_blank_values=True)
        asset = urlparse(request.full_url).path.split('/')[-1]
        return Response(json.dumps(self.envelope(asset, query['system'][0], query['relative_path'][0], query['nonce'][0])).encode())

    def expected(self):
        return dict(version=1, drone_id='source', asset_type='roms', system='nes',
                    relative_path='game.bin', nonce=self.nonce, entry_type='file')

    def test_real_signature_and_full_file_digest(self):
        payload = inventory.verify_inventory(self.envelope(), self.certificate, self.expected())
        self.assertEqual(payload['files'][0]['sha256'], hashlib.sha256(self.asset.read_bytes()).hexdigest())

    def test_tamper_replay_and_cross_asset_substitution_are_rejected(self):
        envelope = self.envelope()
        tampered = copy.deepcopy(envelope)
        tampered['inventory']['files'][0]['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            inventory.verify_inventory(tampered, self.certificate, self.expected())
        for key, value in [('nonce', 'cd' * 32), ('drone_id', 'other'), ('asset_type', 'bios'),
                           ('system', 'snes'), ('relative_path', 'other.bin'), ('entry_type', 'folder'), ('version', 2)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                inventory.verify_inventory(envelope, self.certificate, {**self.expected(), key: value})
        with self.assertRaises(ValueError):
            inventory.verify_inventory({'inventory': envelope['inventory'], 'signature': ''}, self.certificate, self.expected())

    def test_signature_from_another_identity_is_rejected(self):
        other = self.root / 'other.key'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(other)], check=True, capture_output=True)
        self.source.drone_key_file = other
        with self.assertRaises(ValueError):
            inventory.verify_inventory(self.envelope(), self.certificate, self.expected())

    def test_full_digest_covers_bytes_outside_sampled_fingerprint_windows(self):
        from app.common.fingerprint import build_fingerprint
        self.asset.write_bytes(b'a' * (4 * 1024 * 1024))
        payload = inventory.verify_inventory(self.envelope(), self.certificate, self.expected())
        sampled = build_fingerprint(self.asset)
        with self.asset.open('r+b') as handle:
            handle.seek(1024 * 1024)
            handle.write(b'b')
        self.assertEqual(build_fingerprint(self.asset), sampled)
        with self.assertRaises(ValueError):
            inventory.verify_file(self.asset, payload['files'][0])
        self.assertFalse(self.asset.exists())

    def test_truncated_and_oversized_bodies_leave_existing_target_intact(self):
        target = self.settings.roms_root / 'nes/game.bin'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing')
        for data in (b'short', self.asset.read_bytes() + b'extra'):
            with self.subTest(size=len(data)), \
                 mock.patch.object(inventory, 'urlopen', side_effect=self.fetch_response), \
                 mock.patch.object(peer_download, 'urlopen', return_value=Response(data)):
                with self.assertRaises(ValueError):
                    peer_download._download_rom_from_peer(self.settings, {}, self.peer, 'nes', 'game.bin', overwrite=True)
                self.assertEqual(target.read_bytes(), b'existing')
                self.assertFalse(target.with_suffix('.bin.part').exists())

    def test_rom_and_bios_require_signed_bytes_even_without_caller_hash(self):
        for asset in ('roms', 'bios'):
            for corrupt in (False, True):
                with self.subTest(asset=asset, corrupt=corrupt):
                    data = b'attacker content' if corrupt else self.asset.read_bytes()
                    with mock.patch.object(inventory, 'urlopen', side_effect=self.fetch_response), \
                         mock.patch.object(peer_download, 'urlopen', return_value=Response(data)):
                        def download():
                            if asset == 'roms':
                                return peer_download._download_rom_from_peer(self.settings, {}, self.peer, 'nes', 'game.bin', overwrite=True)
                            return peer_download._download_bios_from_peer(self.settings, {}, self.peer, 'game.bin', overwrite=True)
                        if corrupt:
                            with self.assertRaises(ValueError):
                                download()
                        else:
                            self.assertEqual(download()['status'], 'completed')
                    target = (self.settings.roms_root / 'nes' if asset == 'roms' else self.settings.bios_root) / 'game.bin'
                    self.assertEqual(target.read_bytes(), self.asset.read_bytes())
                    self.assertFalse(target.with_suffix('.bin.part').exists())

    def test_no_unsigned_fallback_or_caller_selected_trust(self):
        for fault in ('unsigned', 'unpaired', 'wrong-pin', 'wrong-cert', 'oversized'):
            with self.subTest(fault=fault):
                pin = peer_connectivity._local_peer_cert_cache_path(self.settings, 'source')
                with mock.patch.object(inventory.local_network, 'get_paired_peer', return_value=(
                        None if fault == 'unpaired' else {'certificate_fingerprint': '0' * 64 if fault == 'wrong-pin' else self.pin})), \
                     mock.patch.object(inventory, 'urlopen', return_value=Response(
                         b'x' * (inventory.MAX_INVENTORY_BYTES + 1) if fault == 'oversized' else b'{}')), \
                     mock.patch.object(peer_download, 'urlopen') as body:
                    if fault == 'wrong-cert':
                        pin.write_bytes(b'invalid cert')
                    try:
                        with self.assertRaises((ValueError, ssl.SSLError)):
                            peer_download._download_rom_from_peer(self.settings, {}, self.peer, 'nes', 'game.bin',
                                                                  expected_fingerprint='attacker-hash')
                        body.assert_not_called()
                    finally:
                        pin.write_bytes(self.certificate)

    def test_folder_authenticates_non_marker_bytes(self):
        self.asset.unlink()
        self.asset.mkdir()
        (self.asset / 'marker.gdi').write_bytes(b'marker')
        (self.asset / 'track.bin').write_bytes(b'good track')
        def response(request, **kwargs):
            query = parse_qs(urlparse(request.full_url).query)
            return Response(json.dumps(self.envelope(rel='game', nonce=query['nonce'][0])).encode())
        def body(request, **kwargs):
            return Response(b'marker' if request.full_url.endswith('marker.gdi') else b'evil track')
        with mock.patch.object(inventory, 'urlopen', side_effect=response), \
             mock.patch.object(peer_download, 'urlopen', side_effect=body):
            with self.assertRaises(ValueError):
                peer_download._download_rom_folder_from_peer(self.settings, {}, self.peer, 'nes', 'game',
                                                             marker_relative_path='game/marker.gdi')
        self.assertFalse((self.settings.roms_root / 'nes/game/track.bin').exists())
        self.assertFalse((self.settings.roms_root / 'nes/game/marker.gdi').exists())

    def test_inventory_server_authorization_and_path_containment(self):
        repo = SimpleNamespace(get_local_system_dir=lambda s: self.root,
                               get_bios_root=lambda: self.root, is_local_bios_path=lambda p: True)
        handler = SimpleNamespace(repository=repo, settings=self.source,
                                  _peer_request_authorized=mock.Mock(return_value=False), _send_json=mock.Mock())
        query = {'system': ['nes'], 'relative_path': ['source-game'], 'nonce': [self.nonce]}
        HandlersPeerMixin._handle_peer_content_inventory(handler, 'roms', query)
        handler._send_json.assert_not_called()
        handler._peer_request_authorized.return_value = True
        HandlersPeerMixin._handle_peer_content_inventory(handler, 'roms', query)
        status, envelope = handler._send_json.call_args.args
        self.assertEqual(status, 200)
        inventory.verify_inventory(envelope, self.certificate, {**self.expected(), 'relative_path': 'source-game'})
        for path in ('../outside', '/outside', 'a/../source-game'):
            HandlersPeerMixin._handle_peer_content_inventory(handler, 'roms', {**query, 'relative_path': [path]})
            self.assertEqual(handler._send_json.call_args.args[0], 400)
        outside = self.root.parent / 'outside'
        (self.root / 'link').symlink_to(outside)
        HandlersPeerMixin._handle_peer_content_inventory(handler, 'roms', {**query, 'relative_path': ['link']})
        self.assertEqual(handler._send_json.call_args.args[0], 404)

    def test_even_signed_inventory_paths_cannot_escape_destination(self):
        for path in ('../escape', '/absolute', 'dir/../../escape', 'dir\\escape'):
            with self.subTest(path=path):
                envelope = self.envelope()
                envelope['inventory']['files'][0]['relative_path'] = path
                envelope['signature'] = base64.b64encode(inventory.openssl(
                    ['dgst', '-sha256', '-sign', str(self.key)], inventory.canonical(envelope['inventory']))).decode()
                with self.assertRaises(ValueError):
                    inventory.verify_inventory(envelope, self.certificate, self.expected())

    def test_identity_hold_prevents_inventory_dial(self):
        with mock.patch.object(inventory, '_require_swarm_networking', side_effect=PermissionError('identity hold')), \
             mock.patch.object(inventory, 'urlopen') as dial:
            with self.assertRaises(PermissionError):
                inventory.fetch_inventory(self.settings, {}, 'source', 'http://source:8080', 'roms', 'nes', 'game.bin', 'file')
            dial.assert_not_called()

    def test_folder_symlink_escape_is_not_signed(self):
        self.asset.unlink()
        self.asset.mkdir()
        (self.asset / 'escape').symlink_to(self.cert)
        with self.assertRaises(ValueError):
            self.envelope()
