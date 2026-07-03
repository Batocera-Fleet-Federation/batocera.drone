"""RomRequestHandler Overmind-config admin handlers, as a mixin.

Extracted from ``drone_api.py``. Credentials update, Overmind config/start/claim-ownership,
swarm connect/disconnect, and API-certificate rotation. Composed onto ``RomRequestHandler``.
"""

import socket
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlparse

try:
    from ..transfer import local_network as _local_network
    from ..overmind.overmind_client import (
        _format_overmind_error,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )
    from ..overmind.registration import _register_or_claim_overmind_token
    from ..transfer.drone_network import (
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
    )
    from ..transfer.drone_tls import DroneCertificateManager
    from ..transfer.network_identity import drone_scheme as _drone_scheme
except ImportError:  # pragma: no cover - direct script execution fallback
    from transfer import local_network as _local_network  # type: ignore
    from overmind.overmind_client import (  # type: ignore
        _format_overmind_error,
        _overmind_post_json,
        _overmind_post_json_with_status,
    )
    from overmind.registration import _register_or_claim_overmind_token  # type: ignore
    from transfer.drone_network import (  # type: ignore
        _drone_advertised_api_port,
        _drone_network_payload,
        _drone_reachable_url,
    )
    from transfer.drone_tls import DroneCertificateManager  # type: ignore
    from transfer.network_identity import drone_scheme as _drone_scheme  # type: ignore


def _collect_system_info_payload(settings):
    """Delegate to the drone_api aggregator (lazy to avoid a cycle)."""
    try:
        from ..device.system_info import _collect_system_info_payload as _impl
    except ImportError:  # pragma: no cover - flat execution
        from device.system_info import _collect_system_info_payload as _impl  # type: ignore
    return _impl(settings)


class HandlersOvermindMixin:
    def _handle_admin_credentials_update(self, payload: dict) -> None:
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not getattr(self.auth, "credential_store", None):
            raise ValueError("credential storage is not available")
        result = self.auth.credential_store.update(username, password)
        self._send_json(200, {"credentials": result, "message": "Drone credentials updated."})

    def _handle_admin_overmind_config(self, payload: dict) -> None:
        raw_url = str(payload.get("overmind_url") or "").strip()
        raw_email = str(payload.get("overmind_email") or "").strip()
        raw_drone_name = str(payload.get("drone_name") or "").strip()
        raw_password = payload.get("overmind_password")
        raw_auth_token = payload.get("overmind_auth_token")
        raw_token = payload.get("overmind_token")

        if not raw_url:
            raise ValueError("overmind_url is required")
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("overmind_url must be a valid http/https URL")
        if raw_email and ("@" not in raw_email or raw_email.startswith("@") or raw_email.endswith("@")):
            raise ValueError("overmind_email must be a valid email address")

        existing = self._load_overmind_config()
        new_config = dict(existing)
        new_config["overmind_url"] = raw_url.rstrip("/")
        new_config["overmind_email"] = raw_email
        new_config["drone_name"] = raw_drone_name or socket.gethostname()
        claim_password = str(raw_password or "") if raw_password is not None else ""
        if raw_password is not None and not claim_password:
            raise ValueError("overmind_password cannot be empty when provided")
        if raw_auth_token is not None:
            auth_token_value = str(raw_auth_token)
            if not auth_token_value:
                raise ValueError("overmind_auth_token cannot be empty when provided")
            new_config["overmind_auth_token"] = auth_token_value
            new_config.pop("overmind_token", None)
            new_config["swarm_connection_status"] = "disconnected"
            self._save_json_state(self._overmind_swarm_path(), [])
            self._save_json_state(self._overmind_peer_results_path(), [])
        if raw_token is not None:
            token_value = str(raw_token)
            if not token_value:
                raise ValueError("overmind_token cannot be empty when provided")
            new_config["overmind_token"] = token_value
        if not str(new_config.get("overmind_auth_token") or "").strip() and not str(new_config.get("overmind_token") or "").strip():
            raise ValueError("authorization token is required to connect this Drone to Overmind")
        new_config["requested_at"] = self._now_iso()
        new_config["integration_state"] = "configured"
        new_config["last_error"] = None
        new_config["notes"] = "Configuration saved. Drone will report heartbeat and collect Overmind actions on its polling interval."
        overmind_active = _local_network.is_overmind_mode(self.settings)
        if new_config.get("overmind_auth_token") and overmind_active:
            base_url = str(new_config.get("overmind_url") or "").strip().rstrip("/")
            new_config["integration_enabled"] = True
            token = _register_or_claim_overmind_token(self.settings, self.repository, new_config, base_url)
            if token:
                new_config = self._load_overmind_config()
            else:
                refreshed = self._load_overmind_config()
                if refreshed.get("integration_state") == "pending_approval":
                    new_config = refreshed
                    new_config["integration_enabled"] = True
                else:
                    new_config["integration_enabled"] = False
        elif new_config.get("overmind_auth_token"):
            new_config["integration_enabled"] = False
            new_config["integration_state"] = "disabled"
            new_config["notes"] = "Configuration saved. Enable Overmind integration to connect this Drone."
        if raw_password is not None:
            if not overmind_active:
                raise ValueError("enable Overmind integration before claiming ownership")
            if parsed.scheme != "https":
                raise ValueError("claim ownership requires an https Overmind URL")
            if not raw_email:
                raise ValueError("overmind_email is required to claim ownership")
            if not str(new_config.get("overmind_token") or "").strip():
                raise ValueError("authorization token is required before claiming ownership")
            base_url = raw_url.rstrip("/")
            network_payload = _drone_network_payload(self.settings)
            claim_payload = {
                "device_id": self.settings.overmind_device_id,
                "device_name": new_config["drone_name"],
                "email": raw_email,
                "password": claim_password,
                "network": network_payload,
                "api_port": _drone_advertised_api_port(self.settings),
                "scheme": _drone_scheme(self.settings),
                "reachable_url": _drone_reachable_url(self.settings, network_payload),
                "certificate": DroneCertificateManager(self.settings).metadata(),
                "system_info": _collect_system_info_payload(self.settings),
            }
            print(
                f"Overmind ownership claim requested for {self.settings.overmind_device_id}: endpoint={base_url}/api/drones/claim-ownership",
                file=sys.stdout,
                flush=True,
            )
            try:
                status_code, response = _overmind_post_json_with_status(
                    f"{base_url}/api/drones/claim-ownership",
                    claim_payload,
                    settings=self.settings,
                )
            except HTTPError as error:
                print(
                    f"Overmind ownership claim failed for {self.settings.overmind_device_id}: status={error.code}",
                    file=sys.stderr,
                    flush=True,
                )
                self._send_json(error.code if 400 <= error.code < 600 else 502, {"error": "ownership claim failed"})
                return
            except Exception as error:
                print(
                    f"Overmind ownership claim failed for {self.settings.overmind_device_id}: {_format_overmind_error(error)}",
                    file=sys.stderr,
                    flush=True,
                )
                self._send_json(502, {"error": "ownership claim failed"})
                return
            if status_code >= 400:
                self._send_json(status_code, {"error": "ownership claim failed"})
                return
            new_config["claimed_at"] = self._now_iso()
            new_config["ownership_claim_status"] = response.get("status") or "claimed"
            new_config["notes"] = "Configuration saved. Ownership claim recorded in Overmind; authorization token remains the Drone connection credential."
            new_config.pop("overmind_password", None)
        self._save_overmind_config(new_config)
        self._send_json(200, self._overmind_public_payload(new_config))

    def _handle_admin_overmind_start(self, payload: dict) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        password = str(config.get("overmind_password") or "")
        auth_token = str(config.get("overmind_auth_token") or "")
        token = str(config.get("overmind_token") or "")
        if not str(config.get("overmind_url") or "").strip():
            raise ValueError("overmind_url is not configured")
        if not token and not auth_token:
            raise ValueError("overmind authorization token is not configured")

        if "overmind_password" in payload:
            supplied = str(payload.get("overmind_password") or "")
            if not supplied:
                raise ValueError("overmind_password cannot be empty")
            config["overmind_password"] = supplied
            password = supplied
        if "overmind_token" in payload:
            supplied_token = str(payload.get("overmind_token") or "")
            if not supplied_token:
                raise ValueError("overmind_token cannot be empty")
            config["overmind_token"] = supplied_token
        if "overmind_auth_token" in payload:
            supplied_auth = str(payload.get("overmind_auth_token") or "")
            if not supplied_auth:
                raise ValueError("overmind_auth_token cannot be empty")
            config["overmind_auth_token"] = supplied_auth

        config["integration_enabled"] = True
        config["integration_state"] = "polling"
        config["swarm_connection_status"] = "connected"
        config["last_started_at"] = self._now_iso()
        config["last_error"] = None
        config["notes"] = (
            "Integration active. Drone periodically calls Overmind, claims actions, performs local collection, "
            "and posts completion results back to the Overmind API."
        )
        self._save_overmind_config(config)
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_claim_ownership(self, payload: dict) -> None:
        if not self._require_overmind_mode():
            return
        raw_url = str(payload.get("overmind_url") or "").strip()
        email = str(payload.get("email") or "").strip()
        password = str(payload.get("password") or "")
        drone_name = str(payload.get("drone_name") or "").strip() or socket.gethostname()
        if not raw_url:
            raise ValueError("overmind_url is required")
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("claim ownership requires an https Overmind URL")
        if not email or "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ValueError("email must be a valid email address")
        if not password:
            raise ValueError("password is required")

        base_url = raw_url.rstrip("/")
        network_payload = _drone_network_payload(self.settings)
        claim_payload = {
            "device_id": self.settings.overmind_device_id,
            "device_name": drone_name,
            "email": email,
            "password": password,
            "network": network_payload,
            "api_port": _drone_advertised_api_port(self.settings),
            "scheme": _drone_scheme(self.settings),
            "reachable_url": _drone_reachable_url(self.settings, network_payload),
            "certificate": DroneCertificateManager(self.settings).metadata(),
            "system_info": _collect_system_info_payload(self.settings),
        }
        print(
            f"Overmind ownership claim requested for {self.settings.overmind_device_id}: endpoint={base_url}/api/drones/claim-ownership",
            file=sys.stdout,
            flush=True,
        )
        try:
            status_code, response = _overmind_post_json_with_status(
                f"{base_url}/api/drones/claim-ownership",
                claim_payload,
                settings=self.settings,
            )
        except HTTPError as error:
            print(
                f"Overmind ownership claim failed for {self.settings.overmind_device_id}: status={error.code}",
                file=sys.stderr,
                flush=True,
            )
            self._send_json(error.code if 400 <= error.code < 600 else 502, {"error": "ownership claim failed"})
            return
        except Exception as error:
            print(
                f"Overmind ownership claim failed for {self.settings.overmind_device_id}: {_format_overmind_error(error)}",
                file=sys.stderr,
                flush=True,
            )
            self._send_json(502, {"error": "ownership claim failed"})
            return
        token = str(response.get("drone_token") or "").strip()
        if status_code >= 400 or not token:
            self._send_json(status_code if status_code >= 400 else 502, {"error": "ownership claim failed"})
            return

        config = self._load_overmind_config()
        config.update({
            "overmind_url": base_url,
            "overmind_email": email,
            "drone_name": drone_name,
            "overmind_token": token,
            "integration_enabled": True,
            "integration_state": "polling",
            "swarm_connection_status": "connected",
            "claimed_at": self._now_iso(),
            "last_error": None,
            "notes": "Ownership claimed through Overmind credentials. Drone heartbeat and ROM metadata polling are active.",
        })
        config.pop("overmind_password", None)
        self._save_overmind_config(config)
        print(f"Overmind ownership claim succeeded for {self.settings.overmind_device_id}", file=sys.stdout, flush=True)
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_overmind_swarm_connect(self) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        if not base_url:
            raise ValueError("overmind_url is not configured")
        if not str(config.get("overmind_auth_token") or "").strip():
            raise ValueError("overmind authorization token is not configured")
        config["integration_enabled"] = True
        config["requested_at"] = self._now_iso()
        config["integration_state"] = "approval_requested"
        config["swarm_connection_status"] = "approval requested"
        token = _register_or_claim_overmind_token(self.settings, self.repository, config, base_url)
        refreshed = self._load_overmind_config()
        self._send_json(200, self._overmind_public_payload(refreshed if token or refreshed else config))

    def _handle_admin_overmind_swarm_disconnect(self) -> None:
        if not self._require_overmind_mode():
            return
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if base_url and token:
            try:
                _overmind_post_json(f"{base_url}/api/devices/{quote(self.settings.overmind_device_id, safe='')}/disconnect", {}, token=token, settings=self.settings)
            except Exception as error:
                config["integration_state"] = "disconnect_failed"
                config["swarm_connection_status"] = "disconnect failed"
                config["last_error"] = _format_overmind_error(error)
                self._save_overmind_config(config)
                self._send_json(502, self._overmind_public_payload(config))
                return
        config["integration_enabled"] = False
        config["integration_state"] = "disconnected"
        config["swarm_connection_status"] = "disconnected"
        config["notes"] = "Drone disconnected from its Overmind swarm. Its retained recovery credential is used only for lightweight heartbeats."
        self._save_overmind_config(config)
        self._save_json_state(self._overmind_swarm_path(), [])
        self._send_json(200, self._overmind_public_payload(config))

    def _handle_admin_api_certificate_rotate(self) -> None:
        config = self._load_overmind_config()
        base_url = str(config.get("overmind_url") or "").strip().rstrip("/")
        token = str(config.get("overmind_token") or "").strip()
        if not base_url or not token:
            raise ValueError("approved Overmind connection is required before certificate rotation")
        manager = DroneCertificateManager(self.settings)
        csr = manager.generate_rotation_csr()
        try:
            signed = _overmind_post_json(
                f"{base_url}/api/devices/{quote(self.settings.overmind_device_id, safe='')}/certificate/sign",
                {"csr_pem": csr["csr_pem"], "days": max(1, int(self.settings.drone_cert_days))},
                token=token,
                settings=self.settings,
            )
            metadata = manager.install_signed_certificate(
                str(signed.get("certificate_pem") or ""),
                csr["pending_key"],
                str(signed.get("ca_certificate_pem") or "") or None,
            )
            self._send_json(200, {"status": "rotated", "certificate": metadata})
        except Exception as error:
            try:
                csr["pending_key"].unlink(missing_ok=True)
                csr["pending_csr"].unlink(missing_ok=True)
            except Exception:
                pass
            self._send_json(502, {"status": "failed", "error": _format_overmind_error(error), "certificate": manager.metadata()})
