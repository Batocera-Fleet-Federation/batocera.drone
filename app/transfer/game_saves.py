"""ROM-scoped save discovery and transactional, pinned-peer imports (stdlib only).

Inventories describe save *sets*: a state with its thumbnail, or all files in a
per-game directory. Browser callers receive opaque, expiring choices, never write
paths. All filesystem access is relative to no-follow directory descriptors.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

try:
    from . import local_network
    from .peer_connectivity import PeerIdentityHold, _peer_address_candidates, _peer_trust_cafile, _drone_client_ssl_context, _read_capped
    from .drone_network import _certificate_pem_fingerprint
    from ..device.game_activity import find_running_emulatorlauncher
except ImportError:  # pragma: no cover - flat execution
    from transfer import local_network  # type: ignore
    from transfer.peer_connectivity import PeerIdentityHold, _peer_address_candidates, _peer_trust_cafile, _drone_client_ssl_context, _read_capped  # type: ignore
    from transfer.drone_network import _certificate_pem_fingerprint  # type: ignore
    from device.game_activity import find_running_emulatorlauncher  # type: ignore

MAX_FILES = 2048
MAX_BYTES = 1024 * 1024 * 1024
MAX_SCAN = 20000
SAVE_SUFFIX = re.compile(r"\.(?:srm|sav|save|eep|fla|nv|nvm|rtc|dat|bin|gci|dsv|mcr|mcd|sra|srm|fs|st\d*|state\d*(?:\.auto)?|state\.auto|ss\d*|ppst|p2s|ml\d*|zst\d*|rts|s\d+)$", re.I)
STATE_SUFFIX = re.compile(r"\.(?:state\d*(?:\.auto)?|state\.auto|st\d*|ss\d*|ppst|p2s|zst\d*|s\d+)$", re.I)
TITLE_ID = re.compile(r"(?<![0-9a-f])(010[0-9a-f]{13})(?![0-9a-f])", re.I)
_HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
_LOCK = threading.RLock()
_CHOICES = {}


class SaveError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def relative(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or '\\' in value or '\x00' in value:
        raise SaveError('Invalid save path.')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.startswith('.') or ':' in p for p in parts):
        raise SaveError('Invalid save path.')
    return PurePosixPath(value)


@contextmanager
def directory(root, rel='', create=False):
    """Open every component with O_NOFOLLOW; keep parents pinned during I/O."""
    root = Path(root)
    # Walk from / as well, so an ancestor symlink cannot redirect a configured root.
    parts = root.absolute().parts[1:] + (tuple(relative(rel).parts) if rel else ())
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            if part in ('.', '..'):
                raise SaveError('Invalid save root.')
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


@contextmanager
def read_file(root, rel):
    path = relative(rel)
    parent = str(path.parent) if str(path.parent) != '.' else ''
    with directory(root, parent) as fd:
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(source, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SaveError('Save is not a regular file.')
            yield handle


def _digest(handle):
    digest = hashlib.sha256()
    size = 0
    while True:
        block = handle.read(1024 * 1024)
        if not block:
            return size, digest.hexdigest()
        size += len(block)
        if size > MAX_BYTES:
            raise SaveError('Save exceeds the 1 GiB transfer limit.')
        digest.update(block)


def game(repository, system, unique_id=None, rom_path=None):
    relative(system)
    if '/' in system:
        raise SaveError('Invalid system.')
    rom = (repository.find_rom_by_unique_id(system, unique_id) if unique_id is not None
           else repository.find_rom_by_path(system, str(relative(rom_path))))
    path = str(rom.get('relative_path') or rom.get('rom_path') or rom.get('rom_file') or rom.get('name') or '')
    path = path.removeprefix('./')
    rel = relative(path)
    target = repository.get_system_dir(system) / str(rel)
    if not target.exists():
        raise SaveError('This ROM is not present on this Drone.', 404)
    result = {'system': system, 'rom': str(rel), 'stem': rel.stem, 'filename': rel.name, 'ids': []}
    for text in (rel.name, str(rom.get('title_id') or ''), str(rom.get('serial') or ''), str(rom.get('game_id') or '')):
        result['ids'].extend(m.group(1).upper() for m in TITLE_ID.finditer(text))
    # Disc IDs are used by standalone save layouts, rather than the ROM filename.
    try:
        with target.open('rb') as handle:
            header = handle.read(16)
        if system in ('gc', 'gamecube', 'wii') and re.fullmatch(rb'[A-Z0-9]{6}', header[:6]):
            result['ids'].append(header[:6].decode())
    except OSError:
        pass
    result['ids'] = sorted(set(result['ids']))
    result['fingerprint'] = repository.build_fingerprint(target) if target.is_file() else ''
    return result


def source_game(repository, system, rom_path, fingerprint=''):
    """Resolve renamed copies by content identity; orphaned saves remain usable."""
    relative(system)
    if '/' in system:
        raise SaveError('Invalid system.')
    rel = relative(rom_path)
    found = False
    try:
        item = game(repository, system, rom_path=rom_path)
        found = True
        if not fingerprint or item['fingerprint'] == fingerprint:
            return item
    except FileNotFoundError:
        pass
    if fingerprint:
        try:
            _, rows = repository.list_assets(system, 'roms')
        except FileNotFoundError:
            rows = []
        for row in rows[:MAX_SCAN]:
            if str(row.get('fingerprint') or row.get('rom_fingerprint') or '') == fingerprint:
                item = game(repository, system, unique_id=row['unique_id'])
                if item['fingerprint'] == fingerprint:
                    return item
    if found:
        raise SaveError('This Drone has a different ROM with the same name.', 409)
    return {'system': system, 'rom': str(rel), 'stem': rel.stem, 'filename': rel.name,
            'ids': sorted({m.group(1).upper() for m in TITLE_ID.finditer(rel.name)}), 'fingerprint': fingerprint}


def _source_item(item, value):
    if value is None:
        return item
    if not isinstance(value, dict) or value.get('system') != item['system']:
        raise SaveError('Invalid source ROM.')
    if item.get('fingerprint') and value.get('fingerprint') != item['fingerprint']:
        raise SaveError('The remote ROM does not match this ROM.')
    rel = relative(value.get('rom'))
    ids = value.get('ids', [])
    if not isinstance(ids, list) or len(ids) > 16 or any(not isinstance(i, str) or not re.fullmatch(r'[A-Z0-9_-]{4,32}', i) for i in ids):
        raise SaveError('Invalid source game identifiers.')
    # Content identity does not authorize a peer to choose another local title ID.
    ids = [identifier for identifier in ids if identifier in item['ids']]
    return {'system': item['system'], 'rom': str(rel), 'stem': rel.stem, 'filename': rel.name,
            'ids': ids, 'fingerprint': str(value.get('fingerprint') or '')}


def _target_path(item, source, group):
    """Rebase ROM-named saves onto this machine's name, keeping emulator subfolders."""
    parts = list(relative(group['path']).parts)
    for index, part in enumerate(parts):
        for key in ('filename', 'stem'):
            name = source[key]
            if part.casefold() == name.casefold() or (index == len(parts) - 1 and part.casefold().startswith(name.casefold()) and SAVE_SUFFIX.fullmatch(part[len(name):])):
                parts[index] = item[key] + part[len(name):]
                return '/'.join(parts)
    return group['path']


def roots(settings, item):
    """Only save directories, never entire emulator configuration directories."""
    saves = Path(settings.saves_root).resolve()
    system = item['system']
    result = {'system': saves / system}
    aliases = {'gc': ('dolphin-emu',), 'gamecube': ('dolphin-emu',), 'wii': ('dolphin-emu',),
               'psp': ('psp', 'ppsspp'), 'ps2': ('ps2', 'pcsx2'), 'psx': ('psx', 'duckstation'),
               '3ds': ('3ds', 'citra-emu', 'azahar'), 'wiiu': ('wiiu', 'cemu')}
    for alias in aliases.get(system, ()):
        if alias != system:
            result['saves-' + alias] = saves / alias
    if system == 'switch':
        result['switch-nand'] = saves / 'switch' / 'nand'
        for emulator in ('yuzu', 'eden', 'citron', 'suyu'):
            # Batocera community builds use both layouts. nand is the only exposed subtree.
            result[emulator + '-saves'] = saves / 'switch' / emulator / 'nand'
            result[emulator + '-config'] = Path(settings.userdata_root).resolve() / 'system' / 'configs' / emulator / 'nand'
        result['ryujinx-saves'] = saves / 'switch' / 'ryujinx' / 'bis' / 'user' / 'save'
        result['ryujinx-config'] = Path(settings.userdata_root).resolve() / 'system' / 'configs' / 'Ryujinx' / 'bis' / 'user' / 'save'
    return result


def _walk(root):
    """Bounded, no-follow walk; yield regular files only, including relative names."""
    count = 0
    try:
        with directory(root) as root_fd:
            def visit(fd, prefix='', depth=0):
                nonlocal count
                if depth > 16:
                    raise SaveError('Save directory is too deep.')
                for name in sorted(os.listdir(fd)):
                    count += 1
                    if count > MAX_SCAN:
                        raise SaveError('Save scan is too large; narrow the save directory.')
                    if name.startswith('.'):
                        continue
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    rel = prefix + name
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            yield from visit(child, rel + '/', depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        yield rel
            yield from visit(root_fd)
    except FileNotFoundError:
        return


def _named_group(item, rel):
    """Exact game-name boundaries; never offer a shared memory card as one game's save."""
    parts = relative(rel).parts
    names = [item['stem'], item['filename']] + item['ids']
    for index, part in enumerate(parts):
        for name in names:
            if part.casefold() == name.casefold() and index < len(parts) - 1:
                return '/'.join(parts[:index + 1]), 'directory'
    filename = parts[-1]
    base = filename[:-4] if filename.lower().endswith('.png') else filename
    for name in names:
        if base.casefold().startswith(name.casefold()) and SAVE_SUFFIX.fullmatch(base[len(name):]):
            return '/'.join((*parts[:-1], base)), 'state' if STATE_SUFFIX.search(base) else 'save'
    return None


def _switch_group(item, layout, rel):
    parts = relative(rel).parts
    if layout.startswith('ryujinx-'):
        # Ryujinx's numbered slot is identified by its ExtraData, never its slot number.
        return None
    if len(parts) >= 6 and parts[:3] == ('user', 'save', '0000000000000000'):
        if re.fullmatch(r'[0-9a-fA-F]{32}', parts[3]) and parts[4].upper() in item['ids']:
            return '/'.join(parts[:5]), 'directory'
    return None


def _ryujinx_slots(root, item):
    slots = {}
    try:
        with directory(root) as fd:
            for name in os.listdir(fd)[:MAX_SCAN]:
                if not re.fullmatch(r'[0-9a-fA-F]{16}', name):
                    continue
                try:
                    with read_file(root, name + '/ExtraData') as handle:
                        data = handle.read(512)
                    if len(data) == 512 and f'{int.from_bytes(data[:8], "little"):016X}' in item['ids']:
                        slots[name] = data[8:24].hex()
                except (OSError, SaveError):
                    continue
    except FileNotFoundError:
        pass
    return slots


def inventory(settings, item, hashes=True):
    groups = {}
    for layout, root in roots(settings, item).items():
        slots = _ryujinx_slots(root, item) if layout.startswith('ryujinx-') else {}
        for rel in _walk(root):
            if layout == 'system' or layout.startswith('saves-'):
                # Switch NAND slots need local-profile remapping by the dedicated layouts.
                match = None if item['system'] == 'switch' and any(p in ('nand', 'bis') for p in relative(rel).parts) else _named_group(item, rel)
            elif slots:
                slot, _, tail = rel.partition('/')
                match = (slot + '/0', 'directory') if slot in slots and tail.startswith('0/') else None
            else:
                match = _switch_group(item, layout, rel)
            if not match:
                continue
            group_path, kind = match
            key = hashlib.sha256((layout + ':' + group_path).encode()).hexdigest()
            group = groups.setdefault(key, {'id': key, 'layout': layout, 'path': group_path, 'kind': kind, 'files': []})
            if len(group['files']) >= MAX_FILES:
                raise SaveError('Too many files in this save set.')
            file = {'path': rel}
            with read_file(root, rel) as handle:
                info = os.fstat(handle.fileno())
                file.update(size=info.st_size, modified_time=int(info.st_mtime))
                if hashes:
                    file['size'], file['sha256'] = _digest(handle)
            group['files'].append(file)
    result = []
    for group in groups.values():
        if all(f['path'].lower().endswith('.png') for f in group['files']):
            continue
        group['size'] = sum(f['size'] for f in group['files'])
        if group['size'] > MAX_BYTES:
            raise SaveError('Save set exceeds the 1 GiB transfer limit.')
        group['modified_time'] = max(f['modified_time'] for f in group['files'])
        result.append(group)
    if len(result) > 256:
        raise SaveError('Too many save sets for this ROM.')
    return result


def capabilities(item, groups=()):
    # Absence of a file is not evidence that an emulator cannot save.
    no_saves = item['system'] in ('imageviewer', 'musicplayer', 'kodi')
    return {
        'saves': 'unsupported' if no_saves else ('available' if any(g['kind'] != 'state' for g in groups) else 'unknown'),
        'states': 'unsupported' if item['system'] in ('switch', 'imageviewer', 'musicplayer', 'kodi')
                  else ('available' if any(g['kind'] == 'state' for g in groups) else 'unknown'),
        'message': ('This system does not support game saves or saved states.' if no_saves else
                    'Saved states are not supported by this system’s emulators.' if item['system'] == 'switch' else
                    'Save and state support depends on the emulator and game. Missing files do not mean saves are unsupported.'),
    }


def peer_query(item, **extra):
    return urlencode({'system': item['system'], 'rom': item['rom'], 'fingerprint': item.get('fingerprint', ''), **extra})


def tls_peer(settings, peer_id):
    peer = local_network.get_paired_peer(settings, str(peer_id))
    if not peer or not peer.get('certificate_fingerprint'):
        raise SaveError('Select a paired Drone with a pinned certificate.', 403)
    routes = _peer_address_candidates(peer, settings=settings, peer_id=peer_id)
    if not routes or any(urlparse(route).scheme.lower() != 'https' for route in routes):
        raise SaveError('Save transfers require a paired HTTPS Drone.', 403)
    return peer


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SaveError('The paired Drone redirected a save request.', 502)


def _open_peer(settings, peer, endpoint, timeout=15):
    """Use the paired identity, require mTLS, refuse proxies and redirects."""
    peer_id = str(peer.get('drone_id') or peer.get('device_id') or '')
    peer = tls_peer(settings, peer_id)
    certificate = _peer_trust_cafile(settings, peer_id=peer_id)
    if not certificate or _certificate_pem_fingerprint(certificate.read_text()) != peer['certificate_fingerprint'].lower():
        raise SaveError('The Drone’s cached certificate does not match its pairing pin.', 403)
    last_error = None
    for address in _peer_address_candidates(peer, settings=settings, peer_id=peer_id):
        url = address + endpoint
        context = _drone_client_ssl_context(settings, url, verify=True, cafile=certificate)
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect())
        try:
            return opener.open(Request(url, headers={'Accept': 'application/octet-stream', 'User-Agent': 'batocera-drone-game-saves/1.0'}), timeout=timeout)
        except (HTTPError, PeerIdentityHold, ssl.SSLCertVerificationError):
            raise
        except (OSError, URLError) as error:
            last_error = error
    raise SaveError('The paired Drone is unavailable.', 502) from last_error


def discover(settings, item, peer_id):
    peer = tls_peer(settings, peer_id)
    with _open_peer(settings, peer, '/v1/api/peer/game-saves?' + peer_query(item), timeout=5) as response:
        payload = json.loads(_read_capped(response, 2 * 1024 * 1024))
    if not isinstance(payload, dict):
        raise SaveError('The Drone returned an invalid save inventory.', 502)
    source = _source_item(item, payload.get('game'))
    groups = payload.get('saves')
    if not isinstance(groups, list) or len(groups) > 256:
        raise SaveError('The Drone returned an invalid save inventory.', 502)
    choices = []
    for group in groups:
        group = validate_group(settings, source, group)
        token = secrets.token_urlsafe(32)
        with _LOCK:
            now = time.monotonic()
            for key, value in list(_CHOICES.items()):
                if value['expires'] < now:
                    _CHOICES.pop(key, None)
            while _CHOICES and (len(_CHOICES) >= 1024 or sum(len(c['group']['files']) for c in _CHOICES.values()) + len(group['files']) > 8192):
                _CHOICES.pop(next(iter(_CHOICES)))
            _CHOICES[token] = {'expires': now + 600, 'device': settings.device_id, 'item': item,
                               'peer': peer_id, 'group': group, 'source': source}
        choices.append({'choice': token, 'name': group['path'], 'kind': group['kind'], 'layout': group['layout'],
                        'file_count': len(group['files']), 'file_size': sum(f['size'] for f in group['files']),
                        'modified_time': group.get('modified_time')})
    return {'saves': choices}


def validate_group(settings, item, group):
    if not isinstance(group, dict) or not _HEX_ID.fullmatch(str(group.get('id', ''))):
        raise SaveError('Invalid save set.')
    layout, path, kind = group.get('layout'), group.get('path'), group.get('kind')
    if not all(isinstance(value, str) for value in (layout, path, kind)) or layout not in roots(settings, item) or kind not in ('save', 'state', 'directory'):
        raise SaveError('Unsupported save layout.')
    relative(path)
    if group['id'] != hashlib.sha256((layout + ':' + path).encode()).hexdigest():
        raise SaveError('Invalid save identity.')
    files = group.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise SaveError('Invalid save file count.')
    total, seen = 0, set()
    for file in files:
        if not isinstance(file, dict):
            raise SaveError('Invalid save file.')
        rel = str(relative(file.get('path')))
        if rel in seen:
            raise SaveError('Duplicate save file.')
        seen.add(rel)
        if kind == 'directory':
            if not rel.startswith(path + '/'):
                raise SaveError('File is outside the save set.')
        elif rel != path and rel.lower() != (path + '.png').lower():
            raise SaveError('File is outside the save set.')
        if layout == 'system' or layout.startswith('saves-'):
            if _named_group(item, rel) != (path, kind):
                raise SaveError('Save does not belong to this ROM.')
        elif layout.startswith('ryujinx-'):
            if not re.fullmatch(r'[0-9a-fA-F]{16}/0', path) or kind != 'directory':
                raise SaveError('Invalid Ryujinx save slot.')
        elif _switch_group(item, layout, rel) != (path, kind):
            raise SaveError('Save does not belong to this Switch title.')
        if type(file.get('size')) is not int or not 0 <= file['size'] <= MAX_BYTES or not _HEX_ID.fullmatch(str(file.get('sha256', ''))):
            raise SaveError('Invalid save size or checksum.')
        total += file['size']
    if total > MAX_BYTES:
        raise SaveError('Save set exceeds the transfer limit.')
    modified = group.get('modified_time')
    return {'id': group['id'], 'layout': layout, 'path': path, 'kind': kind,
            'modified_time': modified if type(modified) is int and 0 <= modified <= 4102444800 else None,
            'files': [{'path': f['path'], 'size': f['size'], 'sha256': f['sha256']} for f in files]}


def _switch_root(settings, item, group):
    candidates = roots(settings, item)
    requested = candidates[group['layout']]
    ryujinx = group['layout'].startswith('ryujinx-')
    available = []
    for layout, root in candidates.items():
        if layout == 'system' or layout.startswith('saves-') or layout.startswith('ryujinx-') != ryujinx:
            continue
        try:
            if ryujinx:
                usable = bool(_ryujinx_slots(root, item))
            else:
                with directory(root, 'user/save/0000000000000000') as fd:
                    usable = any(re.fullmatch(r'[0-9a-fA-F]{32}', name) for name in os.listdir(fd))
            if usable:
                available.append(root)
        except FileNotFoundError:
            continue
    if requested in available:
        return requested
    if len(available) == 1:
        return available[0]
    raise SaveError('Start this game once with the intended local Switch emulator and profile; the destination must be unambiguous.', 409)


def _switch_destination(settings, item, group, root):
    layout = group['layout']
    if layout.startswith('ryujinx-'):
        slots = _ryujinx_slots(root, item)
        if len(slots) != 1:
            raise SaveError('Start this game once in Ryujinx with the intended local profile before importing its save.', 409)
        return next(iter(slots)) + '/0'
    path = relative(group['path'])
    prefix = 'user/save/0000000000000000'
    with directory(root, prefix) as fd:
        users = [name for name in os.listdir(fd) if re.fullmatch(r'[0-9a-fA-F]{32}', name)]
    if len(users) != 1:
        raise SaveError('Start this game with the intended local Switch profile; importing requires one unambiguous local profile.', 409)
    title = path.name.upper()
    with directory(root, prefix + '/' + users[0]) as fd:
        title = next((name for name in os.listdir(fd) if name.upper() == title), title)
    return prefix + '/' + users[0] + '/' + title


def _install(root, target_path, staged, group, overwrite, backup):
    """Atomic set replacement, with an original copy kept when overwrite is off.

    All incoming bytes are verified before this runs. Replacement of a directory
    exchanges the complete set, rather than mixing two incompatible game saves.
    """
    path = relative(target_path)
    parent = str(path.parent) if str(path.parent) != '.' else ''
    with directory(root, parent, create=True) as fd:
        if group['kind'] == 'directory':
            names = [path.name]
        else:
            names = [path.name + ('.png' if f['path'].lower().endswith('.png') else '') for f in group['files']]
        moved, installed = [], []
        try:
            for name in names:
                try:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    raise SaveError('Refusing to replace a symlink or special file.')
                os.rename(name, str(backup / name), src_dir_fd=fd)
                moved.append(name)
            for name in names:
                os.rename(str(staged / name), name, dst_dir_fd=fd)
                installed.append(name)
        except Exception:
            for name in installed:
                if group['kind'] == 'directory':
                    # Our newly installed tree contains only validated regular files.
                    os.rename(name, str(staged / name), src_dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
            for name in moved:
                os.rename(str(backup / name), name, dst_dir_fd=fd)
            raise
        return bool(moved) and not overwrite


def import_choice(settings, item, token, overwrite=False):
    if type(overwrite) is not bool:
        raise SaveError('Overwrite must be a boolean.')
    with _LOCK:
        if find_running_emulatorlauncher():
            raise SaveError('Close the running game before importing saves.', 409)
        choice = _CHOICES.get(str(token))
        if not choice or choice['expires'] < time.monotonic() or choice['device'] != settings.device_id or choice['item'] != item:
            raise SaveError('This save selection expired. Check the Drone again.', 409)
        peer = tls_peer(settings, choice['peer'])
        group = choice['group']
        source = choice['source']
        validate_group(settings, source, group)
        root = roots(settings, item)[group['layout']]
        target_path = _target_path(item, source, group)
        if item['system'] == 'switch' and group['layout'] != 'system':
            try:
                root = _switch_root(settings, item, group)
                target_path = _switch_destination(settings, item, group, root)
            except FileNotFoundError:
                raise SaveError('Start this game once on the local Switch emulator before importing its save.', 409) from None
        # Private staging stays on the same filesystem and is excluded from inventories.
        with directory(root, create=True):
            pass
        work = Path(tempfile.mkdtemp(prefix='.drone-save-', dir=root))
        staged, backup = work / 'incoming', work / 'previous'
        staged.mkdir(mode=0o700)
        backup.mkdir(mode=0o700)
        retained = False
        try:
            for index, file in enumerate(group['files']):
                if group['kind'] == 'directory':
                    tail = relative(file['path']).relative_to(relative(group['path']))
                    local = staged / relative(target_path).name / str(tail)
                else:
                    local = staged / (relative(target_path).name + ('.png' if file['path'].lower().endswith('.png') else ''))
                local.parent.mkdir(parents=True, exist_ok=True)
                endpoint = '/v1/api/peer/game-saves/file?' + peer_query(source, save=group['id'], file=str(index))
                response = _open_peer(settings, peer, endpoint, timeout=60)
                with response, local.open('xb') as handle:
                    digest, size = hashlib.sha256(), 0
                    while True:
                        chunk = response.read(min(1024 * 1024, file['size'] - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > file['size']:
                            raise SaveError('Remote save grew during transfer. Refresh and retry.', 409)
                        digest.update(chunk)
                        handle.write(chunk)
                    if size != file['size'] or digest.hexdigest() != file['sha256']:
                        raise SaveError('Remote save changed during transfer. Refresh and retry.', 409)
                    handle.flush()
                    os.fsync(handle.fileno())
            if find_running_emulatorlauncher():
                raise SaveError('A game started during the transfer. Close it before importing saves.', 409)
            retained = True  # Preserve recovery data even if rollback itself fails.
            retained = _install(root, target_path, staged, group, overwrite, backup)
            _CHOICES.pop(str(token), None)
            local_network.record_activity(settings, {'asset_type': 'saves', 'system': item['system'], 'rom_name': item['rom'],
                                                   'source_drone_id': choice['peer'], 'status': 'completed', 'action': 'download',
                                                   'file_count': len(group['files']), 'bytes_transferred': sum(f['size'] for f in group['files'])})
            return {'status': 'completed', 'applied': True, 'file_count': len(group['files']), 'backup_kept': retained}
        finally:
            if retained and any(backup.iterdir()):
                shutil.rmtree(staged, ignore_errors=True)
            else:
                shutil.rmtree(work, ignore_errors=True)
