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

    def _handle_admin_network_reference_get(self) -> None:
        # Issue #35: the Reference ROMs page's state -- the saved peer/system
        # selection plus every peer's live mount status in one round trip.
        selection = _network_share.get_reference_selection(self.settings)
        shares = _network_share.status(self.settings)
        active_share = next(
            (share for share in shares if str(share.get("peer_id") or "") == selection.get("active_peer_id")),
            None,
        )
        self._send_json(200, {"selection": selection, "active_share": active_share, "shares": shares})

    def _handle_admin_network_reference_selection(self, payload: dict) -> None:
        payload = payload if isinstance(payload, dict) else {}
        try:
            selection = _network_share.save_reference_selection(
                self.settings,
                str(payload.get("peer_id") or ""),
                str(payload.get("peer_name") or ""),
                payload.get("selected_systems"),
            )
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        self._send_json(200, {"selection": selection})

    def _handle_admin_network_share_enable(self, peer_id: str) -> None:
        # peer_id arrives as a raw URL path segment -- unlike query-string
        # values, Python's stdlib server does not auto-decode these, and peer
        # ids look like MAC addresses (e.g. "58:47:ca:7e:38:57"), so the ":"s
        # are percent-encoded on the wire and must be unquoted explicitly.
        peer_id = unquote(peer_id)
        try:
            result = _network_share.request_enable(self.settings, peer_id)
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
            return
        status_code = 200 if result.get("status") == "mounted" else 202
        self._send_json(status_code, _network_share.public_record(result))

    def _handle_admin_network_share_disable(self, peer_id: str) -> None:
        peer_id = unquote(peer_id)
        result = _network_share.request_disable(self.settings, peer_id)
        status_code = 404 if result.get("status") == "not_found" else 202
        self._send_json(status_code, _network_share.public_record(result))
