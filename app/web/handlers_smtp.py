"""RomRequestHandler SMTP-admin handlers, as a mixin.

The admin email endpoints: settings snapshot, save settings (host/port/tls/
auth/from/recipient + IMAP fields), the notification-enabled switch, the
per-event-type toggle set, the P2P sharing toggle/pull-from-peer, and the
Test Email button. Composed onto ``RomRequestHandler``, mirroring
``handlers_vpn.py``'s shape exactly. See ``device/smtp_manager.py`` for the
actual settings/sharing/send logic this delegates to.
"""

from urllib.error import HTTPError

try:
    from ..device import smtp_manager as _smtp
    from ..transfer import local_network as _local_network
    from ..transfer.peer_connectivity import _peer_get_json_for_peer
except ImportError:  # pragma: no cover - direct script execution fallback
    from device import smtp_manager as _smtp  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore

SMTP_PEER_PULL_ENDPOINT = "/v1/api/peer/smtp/config"


class HandlersSmtpMixin:
    def _handle_admin_smtp_status(self) -> None:
        self._send_json(200, _smtp.get_settings(self.settings))

    def _handle_admin_smtp_settings_update(self, payload: dict) -> None:
        try:
            result = _smtp.update_settings(self.settings, payload)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_smtp_enabled(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        result = _smtp.set_smtp_enabled(self.settings, bool(payload.get("enabled")))
        self._send_json(200, result)

    def _handle_admin_smtp_notifications_update(self, payload: dict) -> None:
        result = _smtp.update_notification_toggles(self.settings, payload)
        self._send_json(200, result)

    def _handle_admin_smtp_sharing(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            result = _smtp.set_sharing_enabled(self.settings, bool(payload.get("enabled")))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_smtp_pull_from_peer(self, payload: dict) -> None:
        """Pull SMTP/IMAP settings from a paired peer and adopt them locally.

        Mirrors ``handlers_vpn.py``'s ``_handle_admin_vpn_pull_from_peer``
        exactly: the same small one-shot cert-pinned JSON client, not the
        big-file download queue -- SMTP settings are a few fields of text.
        """
        payload = payload if isinstance(payload, dict) else {}
        peer_id = str(payload.get("peer_id") or "").strip()
        if not peer_id:
            raise ValueError("peer_id is required")
        peer = _local_network.get_paired_peer(self.settings, peer_id)
        if not peer:
            self._send_json(404, {"error": "That drone is not a paired peer."})
            return
        try:
            remote_payload, _address = _peer_get_json_for_peer(
                peer, SMTP_PEER_PULL_ENDPOINT, self.settings, peer_id=peer_id
            )
        except HTTPError as error:
            if error.code == 404:
                self._send_json(
                    404, {"error": "That drone has SMTP sharing turned off, or has no configuration set up yet."}
                )
            else:
                self._send_json(502, {"error": f"That drone rejected the request (HTTP {error.code})."})
            return
        except Exception as error:
            self._send_json(502, {"error": f"Could not reach that drone: {error}"})
            return
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            result = _smtp.import_from_peer(self.settings, remote_payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_smtp_test(self) -> None:
        result = _smtp.send_test_email(self.settings)
        self._send_json(200 if result.get("status") == "ok" else 502, result)
