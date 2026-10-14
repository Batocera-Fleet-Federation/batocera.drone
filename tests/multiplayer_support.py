"""Fakes for the multiplayer tests: a fake machine, a fake swarm network, a fake launcher.

All results from these fakes are SIMULATED -- they exercise the orchestration,
state machine and protocol, not a real emulator. Real-emulator behaviour is
covered only by the opt-in checks in ``test_multiplayer_real_environment.py``.
"""

import io
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock
from urllib.error import HTTPError

from app.common.settings import Settings
from app.multiplayer.adapters import build_adapters
from app.multiplayer.environment import SystemProbe
from app.multiplayer.models import MultiplayerError
from app.multiplayer.network import NetworkService
from app.multiplayer.orchestrator import MultiplayerOrchestrator


class FakeProbe(SystemProbe):
    """A scriptable Batocera machine."""

    def __init__(self, settings, *, binaries=None, versions=None, files=None, conf=None, controllers=None,
                 hardware=None, processes=(), open_ports=()):
        super().__init__(settings)
        self.binaries: Dict[str, str] = dict(binaries or {})
        self.versions: Dict[str, str] = dict(versions or {})
        self.files: Dict[str, str] = dict(files or {})
        self.conf: Dict[str, str] = dict(conf or {})
        self._controllers: List[dict] = list(controllers if controllers is not None else [{"name": "Pad", "handler": "js0"}])
        self._hardware = hardware or {"cpu_count": 4, "vaapi": True, "nvenc": False}
        self.processes = set(processes)
        self.open_ports = set(open_ports)

    def find_binary(self, names, extra=()):
        for name in list(names) + list(extra):
            if name in self.binaries:
                return self.binaries[name]
        return None

    def version_of(self, binary, arguments=("--version",), timeout=4.0, pattern=None):
        return self.versions.get(binary or "", "")

    def process_running(self, names):
        return any(name in self.processes for name in names)

    def exists(self, path):
        return str(path) in self.files or any(key.startswith(str(path).rstrip("/") + "/") for key in self.files)

    def read_text(self, path, limit=0):
        return self.files.get(str(path), "")

    def glob(self, pattern, limit=400):
        import fnmatch
        return sorted(key for key in self.files if fnmatch.fnmatch(key, pattern))[:limit]

    def conf_value(self, system, rom_name, key):
        for name in (f'{system}["{rom_name}"].{key}', f"{system}.{key}", f"global.{key}"):
            if self.conf.get(name):
                return self.conf[name]
        return ""

    def controllers(self):
        return list(self._controllers)

    def hardware(self):
        return dict(self._hardware)

    def tcp_open(self, host, port, timeout=1.5):
        return int(port) in self.open_ports


class FakeLauncher:
    """Pretends to be EmulationStation + process detection."""

    def __init__(self):
        self.active: Optional[dict] = None
        self.launched: List[str] = []
        self.exited = 0
        self.fail_launch: Optional[str] = None
        self.start_never_happens = False

    def active_game(self):
        return dict(self.active) if self.active else None

    def is_session_game(self, game_path):
        return bool(self.active) and self.active["rom_path"] == str(game_path)

    def launch_game(self, game_path):
        if self.fail_launch:
            raise MultiplayerError("es_unavailable", self.fail_launch, 409)
        self.launched.append(str(game_path))
        if not self.start_never_happens:
            self.active = {"rom_path": str(game_path), "name": Path(game_path).stem, "system": "snes", "emulator": "libretro"}
        return {"status": "accepted"}

    def exit_game(self):
        self.exited += 1
        self.active = None
        return {"status": "ok"}

    def wait_for_exit(self, timeout):
        return self.active is None

    def wait_for_start(self, game_path, timeout):
        return dict(self.active) if self.active and self.active["rom_path"] == str(game_path) else None


class Hub:
    """The simulated swarm: routes peer calls between in-process orchestrators."""

    def __init__(self):
        self.nodes: Dict[str, "Node"] = {}
        self.offline = set()
        self.log: List[tuple] = []

    def add(self, node: "Node") -> None:
        self.nodes[node.device_id] = node


class FakeNetwork(NetworkService):
    def __init__(self, settings, hub: Hub, me: str):
        super().__init__(settings)
        self.hub = hub
        self.me = me
        self.scope = "lan"

    def candidates(self, peer):
        return [f"https://{self.peer_id(peer)}.local:8543"]

    def peer_scope(self, peer, preferred="auto"):
        return self.hub.nodes[self.peer_id(peer)].network.scope if self.peer_id(peer) in self.hub.nodes else "unknown"

    def host_address(self, peer, preferred="auto"):
        return f"10.0.0.{abs(hash(self.peer_id(peer))) % 200 + 2}"

    def allocate_port(self, start, end, in_use):
        for port in range(start, end + 1):
            if port not in in_use:
                return port
        raise MultiplayerError("no_free_port", "none", 409)

    def _route(self, peer, endpoint, payload):
        target_id = self.peer_id(peer)
        node = self.hub.nodes.get(target_id)
        self.hub.log.append((self.me, target_id, endpoint.split("?")[0]))
        if node is None or target_id in self.hub.offline or self.me in self.hub.offline:
            raise OSError(f"{target_id} is offline")
        orch = node.orch
        path, _, query = endpoint.partition("?")
        try:
            if path == "/invite":
                return orch.peer_invite(self.me, payload)
            if path == "/update":
                return orch.peer_update(self.me, payload)
            if path == "/respond":
                return orch.peer_respond(self.me, payload)
            if path == "/launch":
                return orch.peer_launch(self.me, payload)
            if path == "/match":
                return orch.peer_match(self.me, payload)
            if path == "/status":
                return orch.peer_status(self.me, query.partition("=")[2])
            if path == "/compatibility":
                return orch.peer_compat_export(self.me)
        except MultiplayerError as error:
            body = json.dumps(error.to_dict()).encode()
            raise HTTPError("https://fake", error.status, error.message, {}, io.BytesIO(body))
        raise AssertionError(f"unrouted fake endpoint {endpoint}")

    def post(self, peer, endpoint, payload, *, timeout=5.0):
        return json.loads(json.dumps(self._route(peer, endpoint, payload)))

    def get(self, peer, endpoint, *, timeout=5.0):
        return json.loads(json.dumps(self._route(peer, endpoint, {})))

    def test_peer(self, peer, samples=3):
        online = self.peer_id(peer) in self.hub.nodes and self.peer_id(peer) not in self.hub.offline
        return {"peer_id": self.peer_id(peer), "reachable": online, "scope": "lan", "address": "10.0.0.2", "error": "" if online else "offline",
                "latency_ms": 4.0 if online else None, "samples": 1, "advice": []}


class FakeRepo:
    def __init__(self, roms_root: Path, games: Dict[str, List[dict]]):
        self.roms_root = Path(roms_root)
        self.games = games

    def list_systems(self):
        return [{"name": name, "rom_count": len(items)} for name, items in self.games.items()]

    def list_assets(self, system, asset_type, include_fingerprint=True):
        items = []
        for game in self.games.get(system, []):
            relative = game["file"]
            items.append({"unique_id": game["id"], "name": relative, "rom_file": relative, "relative_path": relative, "rom_path": relative,
                          "byte_count": game.get("size", 1000), "fingerprint": game.get("fingerprint", ""), "entry_type": "file",
                          "title": game.get("title", Path(relative).stem), "gamelist": {"players": game.get("players", "")},
                          "existing": {"image": game.get("image", "")}})
        return self.roms_root / system, items


RETRO_FILES = {
    "/usr/lib/python3.12/site-packages/configgen/emulatorlauncher.py":
        'netplay.mode netplay.server.ip netplay.server.port netplay.password netplay.nickname',
    "/usr/lib/libretro/snes9x_libretro.so": "binary",
    "/usr/share/libretro/info/snes9x_libretro.info": 'display_name = "Snes9x"\ndisplay_version = "1.62.3"\nsavestate = "true"\nsavestate_features = "deterministic"\n',
}


class Node:
    """One simulated Drone: settings, files, fake machine, orchestrator."""

    def __init__(self, hub: Hub, device_id: str, root: Path, *, games=None, files=None, binaries=None, versions=None,
                 conf=None, processes=(), open_ports=(), paired=()):
        self.device_id = device_id
        self.root = Path(root) / device_id
        for sub in ("userdata/system", "roms/snes", "bios", "saves"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        env = {
            "USERDATA_ROOT": str(self.root / "userdata"), "ROMS_ROOT": str(self.root / "roms"), "BIOS_ROOT": str(self.root / "bios"),
            "SAVES_ROOT": str(self.root / "saves"), "DRONE_STATE_DATABASE_FILE": str(self.root / "state.sqlite3"),
            "DRONE_DEVICE_ID": device_id, "BATOCERA_CONF_FILE": str(self.root / "userdata/system/batocera.conf"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.settings = Settings.from_env()
        (self.root / "userdata/system/batocera.conf").write_text(
            "# Batocera\nsystem.language=en_US\n" + "".join(f"{k}={v}\n" for k, v in (conf or {}).items()), encoding="utf-8")
        for system, items in (games or {}).items():
            (self.root / "roms" / system).mkdir(parents=True, exist_ok=True)
            for game in items:
                (self.root / "roms" / system / game["file"]).write_bytes(b"rom")
        self.repo = FakeRepo(self.root / "roms", games or {})
        self.probe = FakeProbe(self.settings, binaries=binaries or {"retroarch": "/usr/bin/retroarch"},
                               versions=versions or {"/usr/bin/retroarch": "1.19.1"},
                               files={**RETRO_FILES, **(files or {})}, conf=dict(conf or {}), processes=processes, open_ports=open_ports)
        self.launcher = FakeLauncher()
        self.network = FakeNetwork(self.settings, hub, device_id)
        self.peers = [{"drone_id": peer, "name": f"Drone {peer}"} for peer in paired]
        self.alerts: List[tuple] = []
        self.orch = MultiplayerOrchestrator(
            self.settings, self.repo, probe=self.probe, adapters=build_adapters(self.probe), launcher=self.launcher, network=self.network,
            paired_peers=lambda: list(self.peers), peer_checks=lambda: [{"target_drone_id": p["drone_id"], "status": "pass", "latency_ms": 5,
                                                                          "checked_at": "2099-01-01T00:00:00+00:00"} for p in self.peers],
            spawn=lambda fn, name: fn(), sleep=lambda seconds: None,
            audit=lambda settings, kind, title, message="", details=None: self.alerts.append((kind, title, details)),
            bell=lambda settings, kind, title, message="", details=None: self.alerts.append((kind, title, details)))
        hub.add(self)

    @property
    def conf_text(self) -> str:
        return (self.root / "userdata/system/batocera.conf").read_text(encoding="utf-8")


SNES_GAME = {"id": "g1", "file": "Super Mario Kart (USA).sfc", "title": "Super Mario Kart", "fingerprint": "ab" * 16, "players": "1-2", "size": 1000}


def make_swarm(tmp: str, names=("host", "guest"), games_for=None, **node_kwargs):
    """Fully paired swarm of simulated Drones, each owning the same SNES game unless told otherwise."""
    hub = Hub()
    nodes = {}
    for name in names:
        games = {"snes": [dict(SNES_GAME)]} if games_for is None or name in games_for else {}
        nodes[name] = Node(hub, name, Path(tmp), games=games, paired=[other for other in names if other != name], **node_kwargs)
    return hub, nodes
