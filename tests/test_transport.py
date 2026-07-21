"""Phase 0 transport-abstraction tests.

These assert the new transport seam is byte-identical to the previous hard-wired
download dispatch: the selector returns the single DirectPublic transport, and
``_directpublic_fetch`` calls each ``_download_*_from_peer`` helper with exactly
the arguments ``DownloadManager._run_job`` used before the refactor.
"""

import threading
import unittest
from unittest import mock

import app.drone_api as drone_api
from app.transfer import download_manager
from app.transport import (
    DirectPublicTransport,
    DownloadRequest,
    TransferContext,
    TransportSelector,
)
from app.transport.base import PeerTransport
from app.transport.lan import LanDirectTransport


def _ctx(**overrides) -> TransferContext:
    base = dict(
        settings=mock.sentinel.settings,
        repository=mock.sentinel.repository,
        config={"config_url": "https://o", "config_token": "t"},
        peer={"drone_id": "peer-1", "public_reachable_url": "https://peer"},
        progress_callback=mock.sentinel.progress,
        cancellation_event=mock.sentinel.cancel,
    )
    base.update(overrides)
    return TransferContext(**base)


class TransportSelectorTests(unittest.TestCase):
    def test_requires_at_least_one_transport(self):
        with self.assertRaises(ValueError):
            TransportSelector([])

    def test_returns_only_transport(self):
        transport = DirectPublicTransport(lambda req, ctx: {"status": "completed"})
        selector = TransportSelector([transport])
        chosen = selector.select(DownloadRequest(asset_type="rom"), _ctx())
        self.assertIs(chosen, transport)
        self.assertEqual([t.name for t in selector.transports], ["direct-public"])

    def test_picks_first_usable_then_falls_back_to_first(self):
        class Never(PeerTransport):
            name = "never"

            def usable(self, request, context):
                return False

            def fetch(self, request, context):
                return {}

        class Always(PeerTransport):
            name = "always"

            def fetch(self, request, context):
                return {}

        never, always = Never(), Always()
        req = DownloadRequest(asset_type="rom")
        # First usable transport wins even when an unusable one precedes it.
        self.assertIs(TransportSelector([never, always]).select(req, _ctx()), always)
        # When none are usable, fall back to the highest-priority transport so
        # the underlying helper can raise its own descriptive error.
        self.assertIs(TransportSelector([never]).select(req, _ctx()), never)


class DirectPublicTransportTests(unittest.TestCase):
    def test_fetch_delegates_to_injected_fn(self):
        captured = {}

        def fake(req, ctx):
            captured["req"], captured["ctx"] = req, ctx
            return {"status": "completed", "ok": True}

        transport = DirectPublicTransport(fake)
        req = DownloadRequest(asset_type="rom", system="snes", relative_path="g.sfc")
        ctx = _ctx()
        result = transport.fetch(req, ctx)
        self.assertEqual(result, {"status": "completed", "ok": True})
        self.assertIs(captured["req"], req)
        self.assertIs(captured["ctx"], ctx)
        self.assertEqual(transport.name, "direct-public")
        self.assertTrue(transport.usable(req, ctx))


class DirectPublicDispatchTests(unittest.TestCase):
    def test_rom_file(self):
        with mock.patch.object(download_manager, "_download_rom_from_peer", return_value={"status": "completed"}) as m:
            req = DownloadRequest(
                asset_type="rom", system="snes", relative_path="g.sfc",
                expected_size=10, expected_fingerprint="fp", entry_type="file",
            )
            ctx = _ctx()
            out = drone_api._directpublic_fetch(req, ctx)
        self.assertEqual(out, {"status": "completed"})
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "snes", "g.sfc",
            expected_size=10, expected_fingerprint="fp",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
            overwrite=False,
        )

    def test_rom_folder(self):
        with mock.patch.object(download_manager, "_download_rom_folder_from_peer", return_value={"s": 1}) as m:
            req = DownloadRequest(
                asset_type="rom", system="ps2", relative_path="game",
                expected_size=99, entry_type="folder",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "ps2", "game",
            expected_size=99, expected_fingerprint=None, marker_relative_path=None,
            progress_callback=ctx.progress_callback,
            cancellation_event=ctx.cancellation_event,
            overwrite=False,
        )

    def test_bios(self):
        with mock.patch.object(download_manager, "_download_bios_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="bios", relative_path="scph.bin",
                expected_size=512, expected_fingerprint="md5x",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "scph.bin",
            expected_size=512, expected_md5="md5x",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
            overwrite=False,
        )

    def test_saves(self):
        with mock.patch.object(download_manager, "_download_save_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="saves", system="snes", relative_path="g.srm",
                expected_size=8, expected_fingerprint="fp",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.config, ctx.peer, "snes", "g.srm",
            expected_size=8, expected_fingerprint="fp",
            cancellation_event=ctx.cancellation_event, overwrite=False,
        )

    def test_artwork(self):
        with mock.patch.object(download_manager, "_download_artwork_from_peer", return_value={}) as m:
            req = DownloadRequest(
                asset_type="artwork", system="snes", rom_path="g.sfc",
                artwork_type="boxart", overwrite=True, local_rom_path="/roms/snes/g.sfc",
            )
            ctx = _ctx()
            drone_api._directpublic_fetch(req, ctx)
        m.assert_called_once_with(
            ctx.settings, ctx.repository, ctx.config, ctx.peer, "snes", "g.sfc", "boxart",
            progress_callback=ctx.progress_callback, cancellation_event=ctx.cancellation_event,
            overwrite=True, local_rom_path="/roms/snes/g.sfc",
        )


class AssetRootTests(unittest.TestCase):
    def _settings(self, **roots):
        env = {"OVERMIND_DEVICE_ID": "dev1"}
        env.update(roots)
        with mock.patch.dict("os.environ", env, clear=True):
            return drone_api.Settings.from_env()

    def test_resolve_asset_root(self):
        settings = self._settings(ROMS_ROOT="/r", BIOS_ROOT="/b", SAVES_ROOT="/sv")
        self.assertEqual(str(drone_api._resolve_asset_root(settings, "rom")), "/r")
        self.assertEqual(str(drone_api._resolve_asset_root(settings, "bios")), "/b")
        self.assertEqual(str(drone_api._resolve_asset_root(settings, "saves")), "/sv")
        self.assertEqual(str(drone_api._resolve_asset_root(settings, "save")), "/sv")
        self.assertIsNone(drone_api._resolve_asset_root(settings, "artwork"))


class SelectorFetchTests(unittest.TestCase):
    """TransportSelector.fetch tries transports in order, falling back on failure."""

    class _Recording(PeerTransport):
        def __init__(self, name, result=None, error=None):
            self.name = name
            self._result = result
            self._error = error
            self.called = []

        def fetch(self, request, context):
            self.called.append(self.name)
            if self._error is not None:
                raise self._error
            return self._result

    def test_falls_back_to_next_on_failure(self):
        failing = self._Recording("fail", error=RuntimeError("boom"))
        working = self._Recording("ok", result={"status": "completed"})
        selector = TransportSelector([failing, working])
        out = selector.fetch(DownloadRequest(asset_type="rom"), _ctx(cancellation_event=None))
        self.assertEqual(out, {"status": "completed"})
        self.assertEqual(failing.called + working.called, ["fail", "ok"])

    def test_all_fail_raises_last_error(self):
        selector = TransportSelector(
            [
                self._Recording("a", error=RuntimeError("e1")),
                self._Recording("b", error=RuntimeError("e2")),
            ]
        )
        with self.assertRaises(RuntimeError) as caught:
            selector.fetch(DownloadRequest(asset_type="rom"), _ctx(cancellation_event=None))
        self.assertEqual(str(caught.exception), "e2")

    def test_cancellation_is_not_retried(self):
        cancel = threading.Event()

        class CancelOnFetch(PeerTransport):
            name = "cancel"

            def fetch(self, request, context):
                cancel.set()
                raise RuntimeError("cancelled")

        nope = self._Recording("nope", result={"status": "completed"})
        selector = TransportSelector([CancelOnFetch(), nope])
        with self.assertRaises(RuntimeError):
            selector.fetch(DownloadRequest(asset_type="rom"), _ctx(cancellation_event=cancel))
        self.assertEqual(nope.called, [])  # never retried after cancel


class TailnetHelperTests(unittest.TestCase):
    def test_is_tailnet_address_classification(self):
        from app.transport.tailnet import is_tailnet_address

        for inside in ("100.64.0.1", "100.101.102.103", "100.127.255.254", "fd7a:115c:a1e0::1", "[100.64.0.9]"):
            self.assertTrue(is_tailnet_address(inside), inside)
        for outside in ("100.63.255.255", "100.128.0.1", "192.168.1.5", "fd00::1", "8.8.8.8", "", None, "garbage"):
            self.assertFalse(is_tailnet_address(outside), repr(outside))

    def _fake_socket_module(self, sockname=None, error=None):
        module = mock.MagicMock()
        if error is not None:
            module.socket.side_effect = error
        else:
            probe = mock.MagicMock()
            probe.getsockname.return_value = (sockname, 0)
            module.socket.return_value.__enter__.return_value = probe
        return module

    def test_get_tailnet_ip_returns_own_address_when_route_exists(self):
        from app.transport.tailnet import get_tailnet_ip

        module = self._fake_socket_module(sockname="100.101.102.5")
        self.assertEqual(get_tailnet_ip(socket_module=module), "100.101.102.5")

    def test_get_tailnet_ip_none_when_probe_resolves_via_default_route(self):
        # Without a tailnet route the kernel answers with the regular LAN
        # address, which must not be mistaken for a tailnet identity.
        from app.transport.tailnet import get_tailnet_ip

        module = self._fake_socket_module(sockname="192.168.1.10")
        self.assertIsNone(get_tailnet_ip(socket_module=module))

    def test_get_tailnet_ip_none_on_socket_error(self):
        from app.transport.tailnet import get_tailnet_ip

        module = self._fake_socket_module(error=OSError("no sockets here"))
        self.assertIsNone(get_tailnet_ip(socket_module=module))


class LanDirectTransportTests(unittest.TestCase):
    def _transport(self, my_public, fetch_fn=lambda r, c: {"status": "completed"}, my_tailnet=None):
        network = {"public_ip": my_public, "tailnet_ip": my_tailnet}
        return LanDirectTransport(fetch_fn, local_network=lambda: network)

    def test_lan_url_when_same_public_ip(self):
        transport = self._transport("203.0.113.7")
        peer = {"public_ip": "203.0.113.7", "local_ip": "192.168.1.50", "api_port": 8443}
        self.assertEqual(transport.lan_url(peer), "https://192.168.1.50:8443")

    def test_lan_url_default_port_omitted(self):
        transport = self._transport("203.0.113.7")
        peer = {"public_ip": "203.0.113.7", "local_ip": "192.168.1.50", "api_port": 443}
        self.assertEqual(transport.lan_url(peer), "https://192.168.1.50")

    def test_no_lan_url_when_public_ip_differs(self):
        transport = self._transport("203.0.113.7")
        self.assertIsNone(transport.lan_url({"public_ip": "198.51.100.9", "local_ip": "192.168.1.50"}))

    def test_no_lan_url_without_local_ip_or_own_public(self):
        self.assertIsNone(self._transport("203.0.113.7").lan_url({"public_ip": "203.0.113.7"}))
        self.assertIsNone(self._transport("").lan_url({"public_ip": "203.0.113.7", "local_ip": "192.168.1.5"}))

    def test_usable_reflects_lan_url(self):
        transport = self._transport("203.0.113.7")
        same = self._ctx({"public_ip": "203.0.113.7", "local_ip": "192.168.1.5"})
        other = self._ctx({"public_ip": "198.51.100.9", "local_ip": "192.168.1.5"})
        self.assertTrue(transport.usable(DownloadRequest(asset_type="rom"), same))
        self.assertFalse(transport.usable(DownloadRequest(asset_type="rom"), other))

    def test_fetch_points_direct_path_at_lan_address(self):
        captured = {}

        def fake(request, context):
            captured["peer"] = context.peer
            return {"status": "completed"}

        transport = self._transport("203.0.113.7", fetch_fn=fake)
        ctx = self._ctx({"drone_id": "p", "public_ip": "203.0.113.7", "local_ip": "192.168.1.50"})
        transport.fetch(DownloadRequest(asset_type="rom"), ctx)
        self.assertEqual(captured["peer"]["public_reachable_url"], "https://192.168.1.50")
        self.assertEqual(captured["peer"]["reachable_url"], "https://192.168.1.50")
        self.assertEqual(captured["peer"]["drone_id"], "p")  # original fields preserved

    def test_lan_url_via_tailnet_when_both_on_tailnet(self):
        transport = self._transport("203.0.113.7", my_tailnet="100.64.0.2")
        self.assertEqual(
            transport.lan_url({"tailnet_ip": "100.64.0.9", "api_port": 8443}),
            "https://100.64.0.9:8443",
        )
        self.assertEqual(
            transport.lan_url({"tailnet_ip": "100.64.0.9", "api_port": 443}),
            "https://100.64.0.9",
        )

    def test_tailnet_url_from_resolved_network_field(self):
        transport = self._transport("203.0.113.7", my_tailnet="100.64.0.2")
        peer = {"resolved_network": {"tailnet_ip": "100.64.0.9"}, "api_port": 443}
        self.assertEqual(transport.lan_url(peer), "https://100.64.0.9")

    def test_tailnet_preferred_over_same_lan_match(self):
        # Tailnet addresses stay stable when the peer later changes networks;
        # Tailscale still forms a direct peer-to-peer path on the same LAN.
        transport = self._transport("203.0.113.7", my_tailnet="100.64.0.2")
        peer = {"public_ip": "203.0.113.7", "local_ip": "192.168.1.50", "tailnet_ip": "100.64.0.9"}
        self.assertEqual(transport.lan_url(peer), "https://100.64.0.9")

    def test_no_tailnet_url_when_self_not_on_tailnet(self):
        transport = self._transport("203.0.113.7", my_tailnet=None)
        self.assertIsNone(transport.lan_url({"tailnet_ip": "100.64.0.9"}))

    def test_tailnet_peer_value_must_be_in_tailnet_range(self):
        # A stale/garbage tailnet_ip field must not open an arbitrary-address path.
        transport = self._transport("203.0.113.7", my_tailnet="100.64.0.2")
        self.assertIsNone(transport.lan_url({"tailnet_ip": "192.168.1.9"}))
        self.assertIsNone(transport.lan_url({"tailnet_ip": ""}))

    def test_fetch_points_direct_path_at_tailnet_address(self):
        captured = {}

        def fake(request, context):
            captured["peer"] = context.peer
            return {"status": "completed"}

        transport = self._transport("203.0.113.7", fetch_fn=fake, my_tailnet="100.64.0.2")
        ctx = self._ctx({"drone_id": "p", "public_ip": "198.51.100.9", "tailnet_ip": "100.64.0.9"})
        self.assertTrue(transport.usable(DownloadRequest(asset_type="rom"), ctx))
        transport.fetch(DownloadRequest(asset_type="rom"), ctx)
        self.assertEqual(captured["peer"]["public_reachable_url"], "https://100.64.0.9")
        self.assertEqual(captured["peer"]["reachable_url"], "https://100.64.0.9")
        self.assertEqual(captured["peer"]["drone_id"], "p")

    @staticmethod
    def _ctx(peer):
        return TransferContext(settings=None, repository=None, config={}, peer=peer)


if __name__ == "__main__":
    unittest.main()
