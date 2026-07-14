"""RomRequestHandler system/automation admin handlers, as a mixin.

Extracted from ``drone_api.py``. Overmind processed-actions view, drone self-update
trigger, API status, idle-volume automation status/config, and the API certificate view.
Composed onto ``RomRequestHandler``.
"""

import time
from threading import Thread

try:
    from .route_config import api_url
    from ..common.self_update import (
        DRONE_SELF_UPDATE_EXIT_CODE,
        _download_latest_drone_app,
        _restart_drone_process_soon,
        is_drone_auto_update_enabled,
        set_drone_auto_update_enabled,
    )
    from ..device.automation import (
        _load_automation_config,
        _push_automation_config_to_overmind,
        _read_last_input_activity,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _save_automation_config,
        _wifi_recovery_status,
    )
    from ..device.device_control import _get_audio_volume
    from ..device.pixen import run_pixen_upgrade
    from ..overmind.overmind_game_logs import find_running_emulatorlauncher as _find_running_emulatorlauncher
    from ..transfer.drone_tls import DroneCertificateManager
except ImportError:  # pragma: no cover - direct script execution fallback
    from common.self_update import (  # type: ignore
        DRONE_SELF_UPDATE_EXIT_CODE,
        _download_latest_drone_app,
        _restart_drone_process_soon,
        is_drone_auto_update_enabled,
        set_drone_auto_update_enabled,
    )
    from device.automation import (  # type: ignore
        _load_automation_config,
        _push_automation_config_to_overmind,
        _read_last_input_activity,
        _reset_idle_game_exit_armed_state,
        _reset_idle_volume_armed_state,
        _reset_wifi_recovery_check_state,
        _save_automation_config,
        _wifi_recovery_status,
    )
    from device.device_control import _get_audio_volume  # type: ignore
    from device.pixen import run_pixen_upgrade  # type: ignore
    from overmind.overmind_game_logs import find_running_emulatorlauncher as _find_running_emulatorlauncher  # type: ignore
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from web.route_config import api_url  # type: ignore


class HandlersSystemMixin:
    def _handle_admin_overmind_actions(self) -> None:
        self._send_json(200, {"actions": self._load_processed_overmind_actions()})

    def _handle_admin_drone_update(self) -> None:
        result = _download_latest_drone_app(self.settings)
        result["restart"] = {
            "scheduled": True,
            "exit_code": DRONE_SELF_UPDATE_EXIT_CODE,
            "note": "The Drone app process will restart so the downloaded version is loaded. Batocera itself is not restarted.",
        }
        self._send_json(200, result)
        try:
            self.wfile.flush()
        except Exception:
            pass
        _restart_drone_process_soon()

    def _handle_admin_drone_auto_update_get(self) -> None:
        self._send_json(200, {"enabled": is_drone_auto_update_enabled(self.settings)})

    def _handle_admin_drone_auto_update_post(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "enabled must be true or false"})
            return
        try:
            saved = set_drone_auto_update_enabled(self.settings, enabled)
        except OSError as error:
            self._send_json(500, {"error": f"Unable to save automatic update setting: {error}"})
            return
        self._send_json(200, {"enabled": saved})

    def _handle_admin_pixn_update(self) -> None:
        result = run_pixen_upgrade(self.settings)
        self._send_json(200, result)

    def _handle_admin_api_status(self) -> None:
        metadata = DroneCertificateManager(self.settings).ensure_certificate()
        self._send_json(
            200,
            {
                "swagger_url": api_url("/swagger"),
                "openapi_url": api_url("/openapi.json"),
                "certificate_download_url": api_url("/admin/api/certificate"),
                "mtls_enabled": self.settings.drone_mtls_enabled,
                "certificate": metadata,
                "guidance": {
                    "curl": "curl --cert /path/to/client.crt --key /path/to/client.key -k https://drone-host/health",
                    "warning": "Do not share Drone private key material. The download endpoint provides the public certificate only.",
                    "lifecycle": f"Drone creates or reuses a local certificate on startup. Default lifetime is {self.settings.drone_cert_days} days; expired certificates are recreated on restart.",
                },
            },
        )

    def _handle_admin_automation_status(self) -> None:
        config = _load_automation_config(self.settings)
        last_activity = _read_last_input_activity()
        idle_seconds = int(time.time() - last_activity) if last_activity is not None else None
        try:
            game_running = _find_running_emulatorlauncher() is not None
        except Exception:
            game_running = False
        self._send_json(
            200,
            {
                "idle_volume": config["idle_volume"],
                "idle_game_exit": config["idle_game_exit"],
                "wifi_recovery": config["wifi_recovery"],
                "wifi_status": _wifi_recovery_status(self.settings),
                "input_monitor": {
                    "available": last_activity is not None,
                    "idle_seconds": idle_seconds,
                    "last_activity_epoch": last_activity,
                },
                "current_volume": _get_audio_volume(self.settings),
                "game_running": game_running,
            },
        )

    def _handle_admin_automation_idle_volume(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        config = _load_automation_config(self.settings)
        merged = {**config["idle_volume"], **payload}
        saved = _save_automation_config(self.settings, {"idle_volume": merged})
        # Re-evaluate from scratch against the new settings on the next poll tick.
        _reset_idle_volume_armed_state()
        # Push the change to Overmind immediately so the per-Drone admin view reflects
        # it without waiting for the next hourly system_info refresh. Best-effort and
        # off-thread so the UI save isn't blocked on Overmind latency; the heartbeat
        # reconciles it regardless.
        Thread(
            target=_push_automation_config_to_overmind,
            args=(self.settings,),
            name="idle-volume-overmind-push",
            daemon=True,
        ).start()
        self._send_json(200, {"idle_volume": saved["idle_volume"]})

    def _handle_admin_automation_idle_game_exit(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        config = _load_automation_config(self.settings)
        merged = {**config["idle_game_exit"], **payload}
        saved = _save_automation_config(self.settings, {"idle_game_exit": merged})
        # Re-evaluate from scratch against the new settings on the next poll tick.
        _reset_idle_game_exit_armed_state()
        # Same best-effort immediate Overmind push as idle-volume (see above).
        Thread(
            target=_push_automation_config_to_overmind,
            args=(self.settings,),
            name="idle-game-exit-overmind-push",
            daemon=True,
        ).start()
        self._send_json(200, {"idle_game_exit": saved["idle_game_exit"]})

    def _handle_admin_automation_wifi_recovery(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        config = _load_automation_config(self.settings)
        merged = {**config["wifi_recovery"], **payload}
        saved = _save_automation_config(self.settings, {"wifi_recovery": merged})
        _reset_wifi_recovery_check_state()
        Thread(
            target=_push_automation_config_to_overmind,
            args=(self.settings,),
            name="wifi-recovery-overmind-push",
            daemon=True,
        ).start()
        self._send_json(200, {"wifi_recovery": saved["wifi_recovery"]})

    def _handle_admin_api_certificate(self) -> None:
        metadata = DroneCertificateManager(self.settings).ensure_certificate()
        cert_file = self.settings.drone_cert_file
        if metadata.get("status") != "loaded" or not cert_file.exists():
            raise FileNotFoundError()
        self._stream_file(cert_file, "application/x-pem-file", as_attachment=True)
