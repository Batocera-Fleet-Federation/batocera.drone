"""EmulationStation auto-collection classification using installed resources only."""
from functools import lru_cache
from pathlib import Path
import re
import xml.etree.ElementTree as ET

RESOURCE_DIR = Path('/usr/share/emulationstation/resources')
TRAITS = ('lightgun', 'wheel', 'trackball', 'spinner', 'vertical')
PLAYER_TRAITS = ('2players', '4players')


def parse_players_range(value):
    """Mirror FileData::parsePlayersRange, including ES's unlimited sentinel."""
    value = str(value or '')
    if not value:
        return (-1, -1)
    value = value.replace('+', '-99999')
    def integer(text):
        match = re.match(r'\s*([+-]?[0-9]+)', text)
        try:
            return int(match.group(1)) if match else 0
        except ValueError:
            return 0
    if '-' in value:
        low, high = value.rsplit('-', 1)
        return integer(low), integer(high)
    return 1, integer(value)


def indexed_name(stem):
    """Keep ES's precedence quirk: digits inside brackets are retained."""
    result = []
    inpar = inblock = False
    for char in stem.lower():
        if (not inpar and not inblock and 'a' <= char <= 'z') or '0' <= char <= '9':
            result.append(char)
        elif char == '(':
            inpar = True
        elif char == ')':
            inpar = False
        elif char == '[':
            inblock = True
        elif char == ']':
            inblock = False
    return ''.join(result)


def resource_signature():
    result = []
    for name in ('gamesdb.xml', 'arcaderoms.xml'):
        path = RESOURCE_DIR / name
        try:
            stat = path.stat()
            result.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            result.append((str(path), None, None))
    return tuple(result)


@lru_cache(maxsize=4)
def _resources(signature):
    games = {}
    vertical = set()
    known_arcade = set()
    for path, _, _ in signature:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        if Path(path).name == 'gamesdb.xml':
            for system in root.findall('system'):
                entries = {}
                for game in system.findall('game'):
                    game_id = game.get('id', '')
                    if not game_id or game_id == 'default':
                        continue
                    entries[game_id] = {trait for trait in TRAITS[:-1]
                                        if game.find('gun' if trait == 'lightgun' else trait) is not None}
                for name in system.get('id', '').split(','):
                    games[name.strip()] = entries
        else:
            for rom in root.findall('rom'):
                name = rom.get('id', '')
                if name:
                    known_arcade.add(name)
                if (name and rom.get('vert') == 'true'
                        and rom.get('device') != 'true' and rom.get('bios') != 'true'):
                    vertical.add(name)
    return games, vertical, known_arcade


class GameTraits:
    def __init__(self, settings):
        try:
            from ..device.device_control import _resolve_es_systems_effective
        except ImportError:  # pragma: no cover - flat execution
            from device.device_control import _resolve_es_systems_effective
        _, systems = _resolve_es_systems_effective(settings)
        self.platforms = {row['name']: str(row.get('platform') or '') for row in systems}
        self.signature = resource_signature()
        self.games, self.vertical, self.known_arcade = _resources(self.signature)
        # JSON-compatible, persisted scan gate also detects platform overlay changes.
        self.version = repr((self.signature, sorted(self.platforms.items())))

    def classify(self, system, file_path):
        stem = Path(file_path).stem
        arcade = 'arcade' in re.split(r'[,\s]+', self.platforms.get(system, ''))
        traits = set()
        if arcade:
            traits.update(self.games.get('arcade', {}).get(stem, ()))
            if stem in self.vertical:
                traits.add('vertical')
            # Arcade platforms use exact set-name lookup only; never fall back
            # to ES's normalized substring matching.
            return sorted(traits)
        name = indexed_name(stem)
        for game_id, flags in self.games.get(system, {}).items():
            if game_id in name:
                traits.update(flags)
        return sorted(traits)
