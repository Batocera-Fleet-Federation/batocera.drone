"""Session-gated ROM save chooser and paired-mTLS save exports."""
import os
from urllib.parse import unquote

try:
    from ..transfer import game_saves
    from ..transfer import local_network
    from .handlers_multiplayer import HandlersMultiplayerMixin
except ImportError:  # pragma: no cover - flat execution
    from transfer import game_saves  # type: ignore
    from transfer import local_network  # type: ignore
    from web.handlers_multiplayer import HandlersMultiplayerMixin  # type: ignore


def _first(query, key):
    return str((query.get(key) or [''])[0])


class HandlersGameSavesMixin:
    def _save_game(self, system, unique_id):
        return game_saves.game(self.repository, unquote(system), unique_id=unquote(unique_id))

    def _handle_game_saves_get(self, system, unique_id, query):
        try:
            item = self._save_game(system, unique_id)
            peer_id = _first(query, 'drone')
            if peer_id:
                result = game_saves.discover(self.settings, item, peer_id)
            else:
                peers = []
                for peer in local_network.paired_peers(self.settings):
                    peer_id = str(peer.get('drone_id') or peer.get('device_id') or '')
                    try:
                        game_saves.tls_peer(self.settings, peer_id)
                    except game_saves.SaveError:
                        continue
                    peers.append({'drone_id': peer_id, 'name': str(peer.get('name') or peer.get('hostname') or peer_id)})
                result = {'capabilities': game_saves.capabilities(item), 'drones': peers}
            self._send_json(200, result)
        except game_saves.SaveError as error:
            self._send_json(error.status, {'error': str(error)})
        except FileNotFoundError:
            self._send_json(404, {'error': 'ROM or save not found on this Drone.'})
        except (OSError, ValueError):
            self._send_json(502, {'error': 'Could not read saves from this Drone. It may be offline or need an update.'})

    def _handle_game_saves_import(self, system, unique_id):
        # Cookie authentication without the router's loopback pre-auth shortcut.
        if self.auth.authenticate_request(self.headers) is None:
            self._send_unauthorized()
            return
        if not self.settings.admin_enabled:
            self._send_json(403, {'error': 'admin disabled'})
            return
        if not HandlersMultiplayerMixin._multiplayer_origin_ok(self):
            self._send_json(403, {'error': 'Cross-origin save imports are not allowed.'})
            return
        if str(self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
            self._send_json(415, {'error': 'Save imports require a JSON body.'})
            return
        try:
            body = self._read_json_body()
            if not isinstance(body, dict):
                raise game_saves.SaveError('Invalid save import.')
            item = self._save_game(system, unique_id)
            result = game_saves.import_choice(self.settings, item, body.get('choice'), body.get('overwrite', False))
            self._send_json(200, result)
        except game_saves.SaveError as error:
            self._send_json(error.status, {'error': str(error)})
        except FileNotFoundError:
            self._send_json(404, {'error': 'ROM or save no longer exists. Refresh and retry.'})
        except (OSError, ValueError):
            self._send_json(502, {'error': 'Save import failed. Check the Drone connection and refresh before retrying.'})

    def _handle_peer_game_saves(self, query, download=False):
        # Save imports always require authenticated TLS, including in development HTTP mode.
        if self.settings.http_only:
            self._send_json(403, {'error': 'Save export requires paired HTTPS.'})
            return
        if not self._peer_request_authorized():
            return
        try:
            item = game_saves.source_game(self.repository, _first(query, 'system'), _first(query, 'rom'), _first(query, 'fingerprint'))
            groups = game_saves.inventory(self.settings, item, hashes=not download)
            if not download:
                self._send_json(200, {'saves': groups, 'game': item, 'capabilities': game_saves.capabilities(item, groups)})
                return
            group = next((g for g in groups if g['id'] == _first(query, 'save')), None)
            index = int(_first(query, 'file'))
            if group is None or not 0 <= index < len(group['files']):
                raise game_saves.SaveError('Save file no longer exists.', 404)
            root = game_saves.roots(self.settings, item)[group['layout']]
            with game_saves.read_file(root, group['files'][index]['path']) as handle:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                remaining = size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except game_saves.SaveError as error:
            self._send_json(error.status, {'error': str(error)})
        except FileNotFoundError:
            self._send_json(404, {'error': 'ROM or save not found.'})
        except (OSError, ValueError):
            self._send_json(400, {'error': 'Could not read the requested save.'})
