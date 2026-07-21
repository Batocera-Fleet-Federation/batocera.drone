"""RomRequestHandler config-file + emulator-config serving handlers, as a mixin.

Extracted from ``drone_api.py``. Serves Batocera config files (raw/json/diff, size-capped)
+ the config-source registry, and lists/serves per-emulator config roots+files. Composed
onto ``RomRequestHandler``.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

try:
    from ..common.logtail import _read_file_tail
    from ..device.device_control import _resolve_es_systems_effective
    from ..device.emulator_configs import (
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.logtail import _read_file_tail  # type: ignore
    from device.device_control import _resolve_es_systems_effective  # type: ignore
    from device.emulator_configs import (  # type: ignore
        list_emulator_config_files as _list_emulator_config_files,
        read_emulator_config_file as _read_emulator_config_file,
    )


class HandlersConfigMixin:
    def _handle_admin_config(self, config_source: str, max_bytes: int, output_format: str = "json") -> None:
        from pathlib import Path

        requested_source = (config_source or "").strip()
        normalized_source = requested_source.lower()
        safe_max_bytes = max(1024, min(int(max_bytes), 1048576))
        normalized_format = (output_format or "json").strip().lower()

        # Curated set of meaningful configs for Batocera/ES/emulators.
        config_path_candidates = {
            "batocera": ["/userdata/system/batocera.conf"],
            "es_systems": [
                "/userdata/system/configs/emulationstation/es_systems.cfg",
                "/usr/share/emulationstation/es_systems.cfg",
            ],
            "emulationstation": [
                "/userdata/system/.emulationstation/es_settings.cfg",
                "/userdata/system/configs/emulationstation/es_settings.cfg",
            ],
            "es_input": [
                "/userdata/system/.emulationstation/es_input.cfg",
                "/userdata/system/configs/emulationstation/es_input.cfg",
            ],
            "es_gamelists": [
                "/userdata/roms",
                "/userdata/system/.emulationstation/gamelists",
                "/userdata/system/configs/emulationstation/gamelists",
            ],
            "retroarch": [
                "/userdata/system/configs/retroarch/retroarch.cfg",
                "/userdata/system/.config/retroarch/retroarch.cfg",
                "/userdata/system/configs/retroarch/retroarchcustom.cfg",
                "/userdata/system/configs/all/retroarch.cfg",
                "/userdata/system/.emulationstation/es_settings.cfg",
            ],
            "mame": [
                "/userdata/system/configs/mame/mame.ini",
                "/userdata/system/configs/mame/default.cfg",
                "/userdata/system/configs/mame",
            ],
            "dolphin": ["/userdata/system/configs/dolphin-emu/Dolphin.ini"],
            "psx2": [
                "/userdata/system/configs/PCSX2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/PCSX2/inis/PCSX2.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2.ini",
            ],
            "pcsx2": [
                "/userdata/system/configs/PCSX2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/PCSX2/inis/PCSX2.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2_ui.ini",
                "/userdata/system/configs/pcsx2/inis/PCSX2.ini",
            ],
            "rpcs3": ["/userdata/system/configs/rpcs3/config.yml"],
            "ppsspp": ["/userdata/system/configs/ppsspp/PSP/SYSTEM/ppsspp.ini"],
            "duckstation": [
                "/userdata/system/configs/duckstation/settings.ini",
                "/userdata/system/configs/duckstation/duckstation.ini",
                "/userdata/system/configs/duckstation/config/settings.ini",
            ],
            "citra": [
                "/userdata/system/configs/citra-emu/qt-config.ini",
                "/userdata/system/configs/citra-emu/config/qt-config.ini",
                "/userdata/system/configs/citra/config/qt-config.ini",
            ],
            "yuzu": [
                "/userdata/system/configs/yuzu/qt-config.ini",
                "/userdata/system/configs/yuzu/config/qt-config.ini",
            ],
            "ryujinx": [
                "/userdata/system/configs/Ryujinx/Config.json",
                "/userdata/system/configs/ryujinx/Config.json",
                "/userdata/system/configs/Ryujinx/config.json",
                "/userdata/system/configs/ryujinx/config.json",
            ],
            "cemu": ["/userdata/system/configs/cemu/settings.xml"],
            "xemu": ["/userdata/system/configs/xemu/xemu.toml"],
            "xenia": [
                "/userdata/system/configs/xenia/xenia.config.toml",
                "/userdata/system/configs/xenia/xenia-canary.config.toml",
            ],
            "flycast": ["/userdata/system/configs/flycast/emu.cfg"],
            "dosbox": [
                "/userdata/system/configs/dosbox/dosboxx.conf",
                "/userdata/system/configs/dosbox/dosbox.conf",
                "/userdata/system/configs/dosbox/dosbox-0.74.conf",
            ],
            "scummvm": [
                "/userdata/system/configs/scummvm/scummvm.ini",
                "/userdata/system/configs/scummvm/scummvmrc",
                "/userdata/system/.scummvmrc",
            ],
            "snes9x": [
                "/userdata/system/configs/snes9x/snes9x.conf",
                "/userdata/system/configs/snes9x/snes9x-gtk.conf",
            ],
            "bsnes": [
                "/userdata/system/configs/bsnes/settings.bml",
                "/userdata/system/configs/bsnes/bsnes.cfg",
                "/userdata/system/configs/bsnes/config.bml",
            ],
            "fceux": [
                "/userdata/system/configs/fceux/fceux.cfg",
                "/userdata/system/configs/fceux/fceux.conf",
            ],
            "mednafen": [
                "/userdata/system/configs/mednafen/mednafen.cfg",
                "/userdata/system/.mednafen/mednafen.cfg",
            ],
            "mgba": [
                "/userdata/system/configs/mgba/config.ini",
                "/userdata/system/configs/mgba/qt.ini",
            ],
            "wine": [
                "/userdata/system/configs/wine/user.reg",
                "/userdata/system/configs/wine/system.reg",
                "/userdata/system/wine-bottles/system.reg",
                "/userdata/system/wine-bottles/user.reg",
            ],
            "shadps4": [
                "/userdata/system/configs/shadps4/user/config.toml",
                "/userdata/system/configs/shadPS4/user/config.toml",
                "/userdata/system/configs/shadps4/config.toml",
                "/userdata/system/configs/shadPS4/config.toml",
                "/userdata/system/configs/shadps4/shadps4.toml",
                "/userdata/system/configs/shadPS4/shadps4.toml",
            ],
            "themes": ["/userdata/themes"],
            "controllers": ["/userdata/system/configs/emulationstation/es_input.cfg"],
        }
        def _resolve_userdata_path(candidate: str) -> str:
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            return candidate

        if normalized_source not in config_path_candidates:
            self._send_json(404, {"error": f"Unknown config source: {requested_source}"})
            return

        resolved_candidates = [_resolve_userdata_path(path) for path in config_path_candidates[normalized_source]]

        if normalized_source == "es_systems":
            source_path, systems = _resolve_es_systems_effective(self.settings)
            if source_path is None:
                self._send_json(404, {
                    "error": f"Config path not found for source: {requested_source}",
                    "attempted_paths": resolved_candidates,
                })
                return
            if normalized_format == "xml":
                try:
                    raw_bytes, truncated = _read_file_tail(source_path, safe_max_bytes)
                    raw_text = raw_bytes.decode("utf-8", errors="replace")
                except Exception as error:
                    self._send_json(500, {"error": f"Failed to read config: {str(error)}"})
                    return
                lines = raw_text.splitlines()
                if truncated:
                    lines.insert(0, f"[truncated] showing last {safe_max_bytes} bytes of file")
                self._send_json(
                    200,
                    {
                        "source": normalized_source,
                        "path": str(source_path),
                        "type": "xml",
                        "format": "xml",
                        "max_bytes": safe_max_bytes,
                        "truncated": truncated,
                        "content": lines,
                    },
                )
                return
            parsed_json = {
                "source_file": str(source_path),
                "systems": systems,
                "count": len(systems),
            }
            rendered = json.dumps(parsed_json, indent=2)
            self._send_json(
                200,
                {
                    "source": normalized_source,
                    "path": str(source_path),
                    "type": "json",
                    "format": "json",
                    "max_bytes": safe_max_bytes,
                    "truncated": False,
                    "parsed": parsed_json,
                    "content": rendered.splitlines(),
                },
            )
            return

        selected_path = None
        selected_is_dir = False
        for candidate in resolved_candidates:
            path = Path(candidate)
            if path.exists():
                selected_path = path
                selected_is_dir = path.is_dir()
                break

        def _find_first_file(candidates):
            for candidate in candidates:
                path = Path(candidate)
                if path.exists() and path.is_file():
                    return path
            return None

        # Fallback discovery for sources with diverse Batocera layouts.
        if selected_path is None and normalized_source == "retroarch":
            search_roots = [
                Path(_resolve_userdata_path("/userdata/system/configs")),
                Path(_resolve_userdata_path("/userdata/system/.config")),
                Path(_resolve_userdata_path("/userdata/system")),
            ]
            target_names = {"retroarch.cfg", "retroarchcustom.cfg"}
            for root in search_roots:
                if not root.exists() or not root.is_dir():
                    continue
                checked = 0
                try:
                    for path in root.rglob("*"):
                        checked += 1
                        if checked > 4000:
                            break
                        if path.is_file() and path.name.lower() in target_names:
                            selected_path = path
                            selected_is_dir = False
                            break
                    if selected_path is not None:
                        break
                except Exception:
                    continue

        # Generic fallback discovery for known emulator config formats.
        if selected_path is None:
            discovery_filenames = {
                "psx2": {"pcsx2_ui.ini", "pcsx2.ini"},
                "pcsx2": {"pcsx2_ui.ini", "pcsx2.ini"},
                "duckstation": {"settings.ini", "duckstation.ini"},
                "citra": {"qt-config.ini"},
                "yuzu": {"qt-config.ini"},
                "ryujinx": {"config.json"},
                "xenia": {"xenia.config.toml", "xenia-canary.config.toml"},
                "dosbox": {"dosboxx.conf", "dosbox.conf", "dosbox-0.74.conf"},
                "scummvm": {"scummvm.ini", "scummvmrc"},
                "snes9x": {"snes9x.conf", "snes9x-gtk.conf"},
                "bsnes": {"settings.bml", "config.bml", "bsnes.cfg"},
                "fceux": {"fceux.cfg", "fceux.conf"},
                "mednafen": {"mednafen.cfg"},
                "mgba": {"config.ini", "qt.ini"},
                "wine": {"user.reg", "system.reg"},
                "shadps4": {"config.toml", "shadps4.toml"},
            }
            root_hints = {
                "psx2": {"pcsx2"},
                "pcsx2": {"pcsx2"},
                "duckstation": {"duckstation"},
                "citra": {"citra"},
                "yuzu": {"yuzu"},
                "ryujinx": {"ryujinx"},
                "xenia": {"xenia"},
                "dosbox": {"dosbox"},
                "scummvm": {"scummvm"},
                "snes9x": {"snes9x"},
                "bsnes": {"bsnes"},
                "fceux": {"fceux"},
                "mednafen": {"mednafen"},
                "mgba": {"mgba"},
                "wine": {"wine", "wine-bottles"},
                "shadps4": {"shadps4"},
            }
            if normalized_source in discovery_filenames:
                targets = discovery_filenames[normalized_source]
                hints = root_hints.get(normalized_source, set())
                search_roots = [
                    Path(_resolve_userdata_path("/userdata/system/configs")),
                    Path(_resolve_userdata_path("/userdata/system/.config")),
                    Path(_resolve_userdata_path("/userdata/system")),
                    Path(_resolve_userdata_path("/userdata")),
                ]
                best_match = None
                for root in search_roots:
                    if not root.exists() or not root.is_dir():
                        continue
                    checked = 0
                    try:
                        for path in root.rglob("*"):
                            checked += 1
                            if checked > 10000:
                                break
                            if not path.is_file():
                                continue
                            file_name = path.name.lower()
                            if file_name not in targets:
                                continue
                            full = str(path).lower()
                            if hints and not any(h in full for h in hints):
                                continue
                            if best_match is None or len(str(path)) < len(str(best_match)):
                                best_match = path
                    except Exception:
                        continue
                if best_match is not None:
                    selected_path = best_match
                    selected_is_dir = False

        if selected_path is None and normalized_source == "es_gamelists":
            # Prefer actual gamelist XML files from /userdata/roms trees.
            roms_root = Path(_resolve_userdata_path("/userdata/roms"))
            if roms_root.exists() and roms_root.is_dir():
                checked = 0
                found = []
                try:
                    for path in roms_root.rglob("gamelist.xml"):
                        checked += 1
                        if checked > 2000:
                            break
                        if path.is_file():
                            found.append(path)
                            if len(found) >= 100:
                                break
                except Exception:
                    found = []
                if found:
                    selected_path = roms_root
                    selected_is_dir = True

        # Last chance for controller config alias.
        if selected_path is None and normalized_source == "controllers":
            selected_path = _find_first_file([
                _resolve_userdata_path("/userdata/system/configs/emulationstation/es_input.cfg"),
                _resolve_userdata_path("/userdata/system/.emulationstation/es_input.cfg"),
            ])
            selected_is_dir = bool(selected_path and selected_path.is_dir())

        if selected_path is None:
            self._send_json(404, {
                "error": f"Config path not found for source: {requested_source}",
                "attempted_paths": resolved_candidates,
            })
            return

        try:
            if selected_is_dir:
                entries = []
                if normalized_source == "es_gamelists" and selected_path == Path(_resolve_userdata_path("/userdata/roms")):
                    checked = 0
                    for gamelist in sorted(selected_path.rglob("gamelist.xml")):
                        checked += 1
                        if checked > 500:
                            entries.append("... (truncated gamelist.xml results)")
                            break
                        rel = gamelist.relative_to(selected_path)
                        entries.append(f"[file] {rel}")
                else:
                    for child in sorted(selected_path.iterdir(), key=lambda p: p.name.lower()):
                        kind = "dir" if child.is_dir() else "file"
                        entries.append(f"[{kind}] {child.name}")
                        if len(entries) >= 500:
                            entries.append("... (truncated directory listing)")
                            break
                self._send_json(200, {
                    "source": normalized_source,
                    "path": str(selected_path),
                    "type": "directory",
                    "max_bytes": safe_max_bytes,
                    "truncated": len(entries) > 500,
                    "content": entries,
                })
                return

            raw, truncated = _read_file_tail(selected_path, safe_max_bytes)
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()
            if truncated:
                lines.insert(0, f"[truncated] showing last {safe_max_bytes} bytes of file")

            self._send_json(200, {
                "source": normalized_source,
                "path": str(selected_path),
                "type": "file",
                "max_bytes": safe_max_bytes,
                "truncated": truncated,
                "content": lines,
            })
        except Exception as error:
            self._send_json(500, {"error": f"Failed to read config: {str(error)}"})

    def _detect_emulator_version(self, source: str) -> Optional[str]:
        if self.settings.use_fake_data and source not in {"batocera", "es_systems", "emulationstation", "es_input", "themes", "controllers"}:
            return "Mock 1.0"

        command_candidates = {
            "retroarch": [["retroarch", "--version"]],
            "mame": [["mame", "-help"]],
            "dolphin": [["dolphin-emu", "--version"], ["dolphin", "--version"]],
            "pcsx2": [["pcsx2", "--version"], ["PCSX2", "--version"]],
            "rpcs3": [["rpcs3", "--version"]],
            "ppsspp": [["PPSSPPSDL", "--version"], ["ppsspp", "--version"]],
            "duckstation": [["duckstation-qt", "--version"], ["duckstation", "--version"]],
            "citra": [["citra", "--version"]],
            "yuzu": [["yuzu", "--version"]],
            "ryujinx": [["Ryujinx", "--version"], ["ryujinx", "--version"]],
            "cemu": [["cemu", "--version"]],
            "xemu": [["xemu", "--version"]],
            "xenia": [["xenia", "--version"]],
            "flycast": [["flycast", "--version"]],
            "dosbox": [["dosbox", "--version"], ["dosbox-x", "--version"]],
            "scummvm": [["scummvm", "--version"]],
            "snes9x": [["snes9x", "--version"]],
            "bsnes": [["bsnes", "--version"]],
            "fceux": [["fceux", "--version"]],
            "mednafen": [["mednafen", "-help"]],
            "mgba": [["mgba-qt", "--version"], ["mgba", "--version"]],
            "wine": [["wine", "--version"]],
            "shadps4": [["shadps4", "--version"], ["shadPS4", "--version"]],
        }
        for command in command_candidates.get(source, []):
            executable = shutil.which(command[0])
            if not executable:
                continue
            try:
                result = subprocess.run(
                    [executable, *command[1:]],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except Exception:
                continue
            output = (result.stdout or result.stderr or "").strip().splitlines()
            if output:
                return output[0][:120]
        return None

    def _handle_admin_config_sources(self) -> None:
        from pathlib import Path

        def _resolve_userdata_path(candidate: str) -> str:
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            return candidate

        # Always keep these top-level debugging sources available.
        base_sources = [
            "batocera",
            "es_systems",
            "emulationstation",
            "es_input",
            "themes",
            "controllers",
        ]
        # Emulator sources should appear only when a matching folder or file exists
        # under /userdata/system/configs (strict detection, no fuzzy substring scan).
        emulator_presence_rules = {
            "retroarch": [
                ("retroarch", "dir"),
            ],
            "mame": [
                ("mame", "dir"),
            ],
            "dolphin": [
                ("dolphin-emu", "dir"),
                ("dolphin", "dir"),
            ],
            "pcsx2": [
                ("PCSX2", "dir"),
                ("pcsx2", "dir"),
            ],
            "rpcs3": [
                ("rpcs3", "dir"),
            ],
            "ppsspp": [
                ("ppsspp", "dir"),
            ],
            "duckstation": [
                ("duckstation", "dir"),
            ],
            "citra": [
                ("citra-emu", "dir"),
                ("citra", "dir"),
            ],
            "yuzu": [
                ("yuzu", "dir"),
            ],
            "ryujinx": [
                ("Ryujinx/Config.json", "file"),
                ("ryujinx/Config.json", "file"),
                ("Ryujinx/config.json", "file"),
                ("ryujinx/config.json", "file"),
            ],
            "cemu": [
                ("cemu", "dir"),
            ],
            "xemu": [
                ("xemu", "dir"),
            ],
            "xenia": [
                ("xenia/xenia.config.toml", "file"),
                ("xenia/xenia-canary.config.toml", "file"),
            ],
            "flycast": [
                ("flycast", "dir"),
            ],
            "dosbox": [
                ("dosbox/dosboxx.conf", "file"),
                ("dosbox/dosbox.conf", "file"),
                ("dosbox/dosbox-0.74.conf", "file"),
            ],
            "scummvm": [
                ("scummvm/scummvm.ini", "file"),
                ("scummvm/scummvmrc", "file"),
            ],
            "snes9x": [
                ("snes9x/snes9x.conf", "file"),
                ("snes9x/snes9x-gtk.conf", "file"),
            ],
            "bsnes": [
                ("bsnes/settings.bml", "file"),
                ("bsnes/config.bml", "file"),
                ("bsnes/bsnes.cfg", "file"),
            ],
            "fceux": [
                ("fceux/fceux.cfg", "file"),
                ("fceux/fceux.conf", "file"),
            ],
            "mednafen": [
                ("mednafen/mednafen.cfg", "file"),
            ],
            "mgba": [
                ("mgba/config.ini", "file"),
                ("mgba/qt.ini", "file"),
            ],
            "wine": [
                ("wine/user.reg", "file"),
                ("wine/system.reg", "file"),
            ],
            "shadps4": [
                ("shadps4/user/config.toml", "file"),
                ("shadPS4/user/config.toml", "file"),
                ("shadps4/config.toml", "file"),
                ("shadPS4/config.toml", "file"),
                ("shadps4/shadps4.toml", "file"),
                ("shadPS4/shadps4.toml", "file"),
            ],
        }

        configs_root = Path(_resolve_userdata_path("/userdata/system/configs"))
        discovered = set(base_sources)
        if configs_root.exists() and configs_root.is_dir():
            for source, checks in emulator_presence_rules.items():
                for rel_path, required_kind in checks:
                    path = configs_root / rel_path
                    if required_kind == "dir" and path.exists() and path.is_dir():
                        discovered.add(source)
                        break
                    if required_kind == "file" and path.exists() and path.is_file():
                        discovered.add(source)
                        break

        ordered_sources = base_sources + [source for source in emulator_presence_rules.keys() if source in discovered]
        versions = {source: self._detect_emulator_version(source) for source in ordered_sources}
        self._send_json(
            200,
            {
                "sources": ordered_sources,
                "versions": versions,
                "scan_root": str(configs_root),
            },
        )

    def _handle_admin_emulators(self) -> None:
        self._send_json(200, _list_emulator_config_files(self.settings, max_configs=250))

    def _handle_admin_emulator_file(self, root_name: str, relative_path: str, max_bytes: int) -> None:
        try:
            self._send_json(200, _read_emulator_config_file(self.settings, root_name, relative_path, max_bytes=max_bytes))
        except FileNotFoundError as error:
            self._send_json(404, {"error": str(error)})
