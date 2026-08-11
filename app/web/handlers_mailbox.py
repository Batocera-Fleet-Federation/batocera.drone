"""RomRequestHandler enrollment-mailbox admin handlers, as a mixin.

The admin endpoints for the GitHub-Issues "enrollment mailbox" feature:
settings snapshot, save settings (repo/token), the P2P sharing toggle/
pull-from-peer, and an on-demand "check now" button. Composed onto
``RomRequestHandler``, mirroring ``handlers_smtp.py``'s shape exactly. See
``device/enrollment_mailbox.py`` for the actual config/sharing/notify logic
this delegates to.
"""

from urllib.error import HTTPError

try:
    from ..device import enrollment_mailbox as _mailbox
    from ..transfer import local_network as _local_network
    from ..transfer.peer_connectivity import _peer_get_json_for_peer
except ImportError:  # pragma: no cover - direct script execution fallback
    from device import enrollment_mailbox as _mailbox  # type: ignore
    from transfer import local_network as _local_network  # type: ignore
    from transfer.peer_connectivity import _peer_get_json_for_peer  # type: ignore

MAILBOX_PEER_PULL_ENDPOINT = "/v1/api/peer/mailbox/config"


class HandlersMailboxMixin:
    def _handle_admin_mailbox_status(self) -> None:
        self._send_json(200, _mailbox.get_settings(self.settings))

    def _handle_admin_mailbox_config_update(self, payload: dict) -> None:
        try:
            result = _mailbox.update_settings(self.settings, payload)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_mailbox_sharing(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            result = _mailbox.set_sharing_enabled(self.settings, bool(payload.get("enabled")))
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_mailbox_pull_from_peer(self, payload: dict) -> None:
        """Pull a mailbox configuration from a paired peer and adopt it.

        Mirrors ``handlers_smtp.py``'s ``_handle_admin_smtp_pull_from_peer``
        exactly: the same small one-shot cert-pinned JSON client.
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
                peer, MAILBOX_PEER_PULL_ENDPOINT, self.settings, peer_id=peer_id
            )
        except HTTPError as error:
            if error.code == 404:
                self._send_json(
                    404, {"error": "That drone has mailbox sharing turned off, or has no configuration set up yet."}
                )
            else:
                self._send_json(502, {"error": f"That drone rejected the request (HTTP {error.code})."})
            return
        except Exception as error:
            self._send_json(502, {"error": f"Could not reach that drone: {error}"})
            return
        peer_name = str(peer.get("name") or peer.get("hostname") or peer_id)
        try:
            result = _mailbox.import_from_peer(self.settings, remote_payload, source_peer_id=peer_id, source_peer_name=peer_name)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, result)

    def _handle_admin_mailbox_check_now(self) -> None:
        result = _mailbox.check_and_notify_if_needed(self.settings)
        self._send_json(200 if result.get("status") != "error" else 502, result)
