"""RomRequestHandler diagnostics handlers (logs + gameplay logs + system info), as a mixin.

Extracted from ``drone_api.py``. Tails drone/ES log sources, reports recent gameplay
events, and builds the system-info payload (GPU/perf/disk, optional speed test). Composed
onto ``RomRequestHandler``.
"""

import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

try:
    from ..app_version import drone_app_version as _drone_app_version
    from ..common.logtail import _tail_lines
    from ..device.tailnet_service import tailnet_status
    from ..device.pixen import is_pixen_installed as _is_pixen_installed
    from ..device.pixen import pixen_script_path as _pixen_script_path
    from ..device.device_control import _apply_audio_volume, _apply_screen_mode, _get_audio_volume, _get_screen_mode
    from ..device.system_metrics import _collect_gpu_info, _collect_performance_metrics, _sample_speed
    from ..overmind.overmind_client import _format_overmind_error
    from ..overmind.overmind_game_logs import (
        load_gameplay_history as _load_gameplay_history,
        pending_game_event_count as _pending_game_event_count,
    )
    from ..transfer.drone_network import _get_router_ip_address
except ImportError:  # pragma: no cover - direct script execution fallback
    from app_version import drone_app_version as _drone_app_version  # type: ignore
    from common.logtail import _tail_lines  # type: ignore
    from device.tailnet_service import tailnet_status  # type: ignore
    from device.pixen import is_pixen_installed as _is_pixen_installed  # type: ignore
    from device.pixen import pixen_script_path as _pixen_script_path  # type: ignore
    from device.device_control import _apply_audio_volume, _apply_screen_mode, _get_audio_volume, _get_screen_mode  # type: ignore
    from device.system_metrics import _collect_gpu_info, _collect_performance_metrics, _sample_speed  # type: ignore
    from overmind.overmind_client import _format_overmind_error  # type: ignore
    from overmind.overmind_game_logs import (  # type: ignore
        load_gameplay_history as _load_gameplay_history,
        pending_game_event_count as _pending_game_event_count,
    )
    from transfer.drone_network import _get_router_ip_address  # type: ignore


class HandlersDiagnosticsMixin:
    def _handle_admin_logs(self, log_source: str, lines: int) -> None:
        import subprocess
        from pathlib import Path

        requested_source = (log_source or "").strip()
        normalized_source = requested_source.lower()
        safe_lines = max(1, min(int(lines), 5000))

        # Only expose explicit, installer-owned diagnostic files. Never accept
        # a path from the request or turn the log viewer into a file browser.
        log_path_candidates = {
            "es_launch_stdout": ["/userdata/system/logs/es_launch_stdout.log"],
            "es_launch_stderr": ["/userdata/system/logs/es_launch_stderr.log"],
            "tailscaled": ["/userdata/system/logs/tailscaled.log"],
            "drone_stdout": [str((self.settings.log_dir / self.settings.stdout_log_file).resolve())],
            "drone_stderr": [str((self.settings.log_dir / self.settings.stderr_log_file).resolve())],
            "drone_overmind": [str((self.settings.log_dir / self.settings.overmind_log_file).resolve())],
        }

        def _resolve_userdata_path(candidate: str) -> str:
            if candidate.startswith("/userdata/"):
                suffix = candidate[len("/userdata/") :]
                return str((self.settings.userdata_root / suffix).resolve())
            if candidate == "/userdata":
                return str(self.settings.userdata_root.resolve())
            return candidate

        if normalized_source not in log_path_candidates:
            self._send_json(404, {"error": f"Unknown log source: {requested_source}"})
            return

        def _dedupe(values):
            seen = set()
            result = []
            for value in values:
                item = str(value)
                if item in seen:
                    continue
                seen.add(item)
                result.append(item)
            return result

        # Build a list of fallback file-name patterns we can search for in common roots.
        names = [normalized_source]
        filename_candidates = []
        for name in names:
            filename_candidates.extend([f"{name}.log", f"{name}.txt", f"{name}_log.txt"])

        candidate_paths = [_resolve_userdata_path(path) for path in log_path_candidates[normalized_source]]
        common_roots = [
            _resolve_userdata_path("/userdata/system/logs"),
            _resolve_userdata_path("/userdata/system/configs"),
            _resolve_userdata_path("/userdata/system/.config"),
            _resolve_userdata_path("/userdata/system"),
        ]
        for root in common_roots:
            for filename in filename_candidates:
                candidate_paths.append(f"{root}/{filename}")

        candidate_paths = _dedupe(candidate_paths)

        log_path = None
        for candidate in candidate_paths:
            path = Path(candidate)
            if path.exists() and path.is_file():
                log_path = path
                break

        # Final fallback: bounded recursive search for matching filenames.
        searched_roots = []
        if log_path is None:
            max_dirs_per_root = 1500
            for root in common_roots:
                root_path = Path(root)
                if not root_path.exists() or not root_path.is_dir():
                    continue
                searched_roots.append(root)
                try:
                    checked = 0
                    for path in root_path.rglob("*"):
                        checked += 1
                        if checked > max_dirs_per_root:
                            break
                        if not path.is_file():
                            continue
                        path_name = path.name.lower()
                        if path_name in {name.lower() for name in filename_candidates}:
                            log_path = path
                            break
                    if log_path is not None:
                        break
                except Exception:
                    # Ignore unreadable trees and continue search.
                    continue

        if log_path is None:
            attempted = candidate_paths[:12]
            self._send_json(404, {
                "error": f"Log file not found for source: {requested_source}",
                "attempted_paths": attempted,
                "searched_roots": searched_roots,
            })
            return

        try:
            log_content = _tail_lines(log_path, safe_lines)
            self._send_json(200, {
                "source": normalized_source,
                "path": str(log_path),
                "lines": safe_lines,
                "content": log_content,
            })
        except Exception as e:
            self._send_json(500, {"error": f"Internal error: {str(e)}"})

    def _handle_admin_gameplay_logs(self) -> None:
        try:
            sessions = _load_gameplay_history(self.settings)
            sessions.sort(key=lambda row: str(row.get("played_at") or ""), reverse=True)
            self._send_json(
                200,
                {
                    "type": "game_logs",
                    "collected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "sessions": sessions,
                    "logs": [],
                    "pending_spool_events": _pending_game_event_count(self.settings),
                },
            )
        except Exception as error:
            self._send_json(500, {"error": _format_overmind_error(error)})

    def _handle_admin_system_info(self, include_speed: bool = False) -> None:
        router_ip_address = _get_router_ip_address() or "Unavailable"
        runtime_metrics = _collect_performance_metrics(self.settings.userdata_root)
        audio_volume = 75 if self.settings.use_fake_data else _get_audio_volume(self.settings)
        if self.settings.use_fake_data:
            tailnet = {
                "installed": True,
                "running": True,
                "enrolled": True,
                "tailnet_ip": "100.64.0.10",
                "hostname": "batocera-devbox",
                "backend_state": "Running",
                "version": "1.80.0 (Fake)",
                "dns_name": "batocera-devbox.example.ts.net",
                "tailnet_name": "example.ts.net",
                "magic_dns_suffix": "example.ts.net",
                "relay": "dfw",
                "health": [],
                "peers": [
                    {
                        "tailnet_id": "fake-peer",
                        "name": "Demo Arcade Cabinet",
                        "tailnet_ip": "100.64.0.11",
                        "online": True,
                    }
                ],
            }
        else:
            try:
                tailnet = tailnet_status()
            except Exception as error:
                # System Info remains useful even if an unexpected CLI/status
                # parsing failure occurs. Surface the failure in the Tailnet card.
                tailnet = {
                    "installed": False,
                    "running": False,
                    "enrolled": False,
                    "tailnet_ip": "",
                    "hostname": "",
                    "backend_state": "Unavailable",
                    "version": "",
                    "dns_name": "",
                    "tailnet_name": "",
                    "magic_dns_suffix": "",
                    "relay": "",
                    "health": [f"Unable to read Tailscale status: {error}"],
                    "peers": [],
                }
        gpu_info = (
            {
                "vendor": "NVIDIA (Fake)",
                "model": "GeForce RTX 4090 (Fake)",
                "driver": "nvidia",
                "renderer": "NVIDIA GeForce RTX 4090/PCIe/SSE2 (Fake)",
                "pci_devices": [],
            }
            if self.settings.use_fake_data
            else _collect_gpu_info()
        )
        pixen_installed = _is_pixen_installed(self.settings)
        pixen_script = str(_pixen_script_path(self.settings))
        speed_sample = _sample_speed() if include_speed else {
            "upload_mbps": None,
            "download_mbps": None,
            "latency_ms": None,
            "source": "not_sampled",
            "sampled_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        if self.settings.use_fake_data:
            fake_router_ip_address = router_ip_address if router_ip_address != "Unavailable" else "192.168.1.1"
            entries = [
                {"key": "Machine ID", "value": self.settings.overmind_device_id},
                {"key": "Integrated with Overmind", "value": "yes" if self._load_overmind_config().get("integration_enabled") else "no"},
                {"key": "Batocera Version", "value": "v43-dev (Fake)"},
                {"key": "Model", "value": "Batocera DevBox (Fake)"},
                {"key": "System", "value": "Linux 6.6.0-fake"},
                {"key": "Architecture", "value": "x86_64"},
                {"key": "CPU model", "value": "AMD Ryzen 7 7800X3D (Fake)"},
                {"key": "CPU cores / threads", "value": "8 / 16"},
                {"key": "CPU max frequency", "value": "5.00 GHz"},
                {"key": "Temperature", "value": "51 C"},
                {"key": "Available memory", "value": "25.4 GiB / 32 GiB"},
                {"key": "Display resolution", "value": "1920x1080"},
                {"key": "Display refresh rate", "value": "60 Hz"},
                {"key": "Data partition available space", "value": "812 GiB"},
                {"key": "Network IP address", "value": "192.168.1.123"},
                {"key": "Router IP Address", "value": fake_router_ip_address},
                {"key": "PixN Installed", "value": "yes" if pixen_installed else "no"},
                {"key": "Battery", "value": "N/A"},
            ]
            fields = {
                "batocera_version": "v43-dev (Fake)",
                "model": "Batocera DevBox (Fake)",
                "system": "Linux 6.6.0-fake",
                "architecture": "x86_64",
                "cpu_model": "AMD Ryzen 7 7800X3D (Fake)",
                "cpu_topology": "8 / 16",
                "cpu_max_frequency": "5.00 GHz",
                "temperature": "51 C",
                "available_memory": "25.4 GiB / 32 GiB",
                "display_resolution": "1920x1080",
                "display_refresh_rate": "60 Hz",
                "data_partition_available_space": "812 GiB",
                "network_ip_address": "192.168.1.123",
                "router_ip_address": fake_router_ip_address,
                "battery": "N/A",
                "machine_id": self.settings.overmind_device_id,
                "overmind_integrated": "yes" if self._load_overmind_config().get("integration_enabled") else "no",
                "drone_app_version": _drone_app_version(),
                "pixen_installed": pixen_installed,
                "pixen_script_path": pixen_script,
                "audio_volume": audio_volume,
                "gpu_vendor": gpu_info.get("vendor"),
                "gpu_model": gpu_info.get("model"),
                "gpu_driver": gpu_info.get("driver"),
            }
            raw = "\n".join(f"{item['key']}: {item['value']}" for item in entries)
            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": raw.splitlines(),
                    "entries": entries,
                    "fields": fields,
                    "drone_app_version": _drone_app_version(),
                    "pixen_installed": pixen_installed,
                    "pixen_script_path": pixen_script,
                    "audio_volume": audio_volume,
                    "gpu_info": gpu_info,
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                    "tailnet_status": tailnet,
                },
            )
            return

        try:
            result = subprocess.run(
                ["batocera-info"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            raw = (result.stdout or "").strip()
            lines = raw.splitlines() if raw else []

            entries = []
            for line in lines:
                text = str(line or "").strip()
                if not text:
                    continue
                if ":" in text:
                    key, value = text.split(":", 1)
                    entries.append({"key": key.strip(), "value": value.strip()})
                else:
                    entries.append({"key": text, "value": ""})

            # Canonical fields for common UI needs.
            fields = {}
            for entry in entries:
                key_lower = entry["key"].lower()
                value = entry["value"]
                if key_lower in ("version", "batocera version"):
                    fields["batocera_version"] = value
                elif key_lower == "model":
                    fields["model"] = value
                elif key_lower == "system":
                    fields["system"] = value
                elif key_lower == "architecture":
                    fields["architecture"] = value
                elif key_lower == "cpu model":
                    fields["cpu_model"] = value
                elif key_lower.startswith("cpu cores"):
                    fields["cpu_topology"] = value
                elif key_lower == "cpu max frequency":
                    fields["cpu_max_frequency"] = value
                elif key_lower == "temperature":
                    fields["temperature"] = value
                elif key_lower == "available memory":
                    fields["available_memory"] = value
                elif key_lower == "display resolution":
                    fields["display_resolution"] = value
                elif key_lower == "display refresh rate":
                    fields["display_refresh_rate"] = value
                elif key_lower == "data partition available space":
                    fields["data_partition_available_space"] = value
                elif key_lower == "network ip address":
                    fields["network_ip_address"] = value
                elif key_lower == "router ip address":
                    fields["router_ip_address"] = value
                elif key_lower == "battery":
                    fields["battery"] = value

            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries.insert(0, {"key": "Integrated with Overmind", "value": overmind_integrated})
            entries.insert(0, {"key": "Machine ID", "value": self.settings.overmind_device_id})
            entries.append({"key": "PixN Installed", "value": "yes" if pixen_installed else "no"})
            if not fields.get("router_ip_address"):
                router_entry = {"key": "Router IP Address", "value": router_ip_address}
                network_index = next(
                    (
                        index
                        for index, entry in enumerate(entries)
                        if str(entry.get("key", "")).lower() == "network ip address"
                    ),
                    None,
                )
                if network_index is None:
                    entries.insert(2, router_entry)
                else:
                    entries.insert(network_index + 1, router_entry)
                fields["router_ip_address"] = router_ip_address
            fields["machine_id"] = self.settings.overmind_device_id
            fields["overmind_integrated"] = overmind_integrated
            fields["drone_app_version"] = _drone_app_version()
            fields["pixen_installed"] = pixen_installed
            fields["pixen_script_path"] = pixen_script
            fields["audio_volume"] = audio_volume
            fields["gpu_vendor"] = gpu_info.get("vendor")
            fields["gpu_model"] = gpu_info.get("model")
            fields["gpu_driver"] = gpu_info.get("driver")

            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": lines,
                    "entries": entries,
                    "fields": fields,
                    "drone_app_version": _drone_app_version(),
                    "pixen_installed": pixen_installed,
                    "pixen_script_path": pixen_script,
                    "audio_volume": audio_volume,
                    "gpu_info": gpu_info,
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                    "tailnet_status": tailnet,
                },
            )
        except Exception as error:
            overmind_integrated = "yes" if self._load_overmind_config().get("integration_enabled") else "no"
            entries = [
                {"key": "Machine ID", "value": self.settings.overmind_device_id},
                {"key": "Integrated with Overmind", "value": overmind_integrated},
                {"key": "Router IP Address", "value": router_ip_address},
                {"key": "PixN Installed", "value": "yes" if pixen_installed else "no"},
                {"key": "System Info", "value": f"batocera-info unavailable: {str(error)}"},
            ]
            raw = "\n".join(f"{item['key']}: {item['value']}" for item in entries)
            self._send_json(
                200,
                {
                    "raw": raw,
                    "lines": raw.splitlines(),
                    "entries": entries,
                    "fields": {
                        "machine_id": self.settings.overmind_device_id,
                        "overmind_integrated": overmind_integrated,
                        "router_ip_address": router_ip_address,
                        "drone_app_version": _drone_app_version(),
                        "pixen_installed": pixen_installed,
                        "pixen_script_path": pixen_script,
                        "audio_volume": audio_volume,
                        "gpu_vendor": gpu_info.get("vendor"),
                        "gpu_model": gpu_info.get("model"),
                        "gpu_driver": gpu_info.get("driver"),
                    },
                    "drone_app_version": _drone_app_version(),
                    "pixen_installed": pixen_installed,
                    "pixen_script_path": pixen_script,
                    "audio_volume": audio_volume,
                    "gpu_info": gpu_info,
                    "runtime_metrics": runtime_metrics,
                    "speed_sample": speed_sample,
                    "tailnet_status": tailnet,
                    "warning": f"Failed to run batocera-info: {str(error)}",
                },
            )

    def _handle_admin_system_volume(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            level = int(payload.get("level"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "A numeric volume level from 0 to 100 is required"})
            return
        if level < 0 or level > 100 or level % 5 != 0:
            self._send_json(400, {"error": "Volume must be from 0 to 100 in increments of 5"})
            return
        try:
            applied = _apply_audio_volume(self.settings, level)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            self._send_json(500, {"error": f"Unable to set volume: {error}"})
            return
        self._send_json(200, {"audio_volume": applied})

    def _handle_admin_screen_mode_get(self) -> None:
        self._send_json(200, {"screen_mode": _get_screen_mode(self.settings)})

    def _handle_admin_screen_mode_post(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in {"full", "kiosk", "kid"}:
            self._send_json(400, {"error": "Screen mode must be one of: full, kiosk, kid"})
            return
        try:
            _settings_path, restarted = _apply_screen_mode(self.settings, mode)
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as error:
            self._send_json(500, {"error": f"Unable to update screen mode: {error}"})
            return
        self._send_json(200, {"screen_mode": mode, "emulationstation_restarted": restarted})

    # HandlersNetworkMixin methods now live in web/handlers_network.py (composed onto RomRequestHandler).

    # HandlersOvermindMixin methods now live in web/handlers_overmind.py (composed onto RomRequestHandler).

    # HandlersConfigMixin methods now live in web/handlers_config.py (composed onto RomRequestHandler).
