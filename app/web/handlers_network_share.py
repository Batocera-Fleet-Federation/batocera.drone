"""RomRequestHandler Network Share handlers, as a mixin.

Thin delegates onto ``device/network_share_manager.py`` -- see that module for
the actual mount/rename/symlink orchestration this exposes to the admin UI.
Composed onto ``RomRequestHandler``.
"""

from urllib.parse import unquote

try:
    from ..device import network_share_manager as _network_share
except ImportError:  # pragma: no cover - direct script execution fallback
    from device import network_share_manager as _network_share  # type: ignore


class HandlersNetworkShareMixin:
    def _handle_admin_network_shares_list(self) -> None:
        self._send_json(200, {"shares": _network_share.status(self.settings)})

    def _handle_admin_network_share_enable(self, peer_id: str) -> None:
        # peer_id arrives as a raw URL path segment -- unlike query-string
        # values, Python's stdlib server does not auto-decode these, and peer
        # ids look like MAC addresses (e.g. "58:47:ca:7e:38:57"), so the ":"s
        # are percent-encoded on the wire and must be unquoted explicitly.
        peer_id = unquote(peer_id)
        try:
            result = _network_share.enable(self.settings, peer_id)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        status_code = 200 if result.get("status") == "mounted" else 502
        self._send_json(status_code, result)

    def _handle_admin_network_share_disable(self, peer_id: str) -> None:
        peer_id = unquote(peer_id)
        result = _network_share.disable(self.settings, peer_id)
        status_code = 404 if result.get("status") == "not_found" else 200
        self._send_json(status_code, result)
