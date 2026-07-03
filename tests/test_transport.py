"""Phase 0 transport-abstraction tests.

These assert the new transport seam is byte-identical to the previous hard-wired
download dispatch: the selector returns the single DirectPublic transport, and
``_directpublic_fetch`` calls each ``_download_*_from_peer`` helper with exactly
the arguments ``DownloadManager._run_job`` used before the refactor.
"""

import io
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import app.drone_api as drone_api
from app.transfer import download_manager, edge_relay
from app.transport import (
    DirectPublicTransport,
    DownloadRequest,
    TransferContext,
    TransportSelector,
    assetfetch,
    mux,
)
from app.transfer.peer_selection import select_best_peer
from app.transport.base import PeerTransport
from app.transport.lan import LanDirectTransport
from app.transport.mux_client import RelayChannel, TlsMuxLink


def _ctx(**overrides) -> TransferContext:
    base = dict(
        settings=mock.sentinel.settings,
        repository=mock.sentinel.repository,
        config={"overmind_url": "https://o", "overmind_token": "t"},
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
            expected_size=99, progress_callback=ctx.progress_callback,
            cancellation_event=ctx.cancellation_event,
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
            cancellation_event=ctx.cancellation_event,
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


class EdgeTokenTests(unittest.TestCase):
    """_edge_token_for must read the live (post-claim) token, falling back to env."""

    def _settings(self, token_env=None):
        env = {"OVERMIND_DEVICE_ID": "dev1"}
        if token_env is not None:
            env["OVERMIND_DRONE_TOKEN"] = token_env
        with mock.patch.dict("os.environ", env, clear=True):
            return drone_api.Settings.from_env()

    def test_prefers_live_config_token(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(
            edge_relay, "overmind_load_config", return_value={"overmind_token": "live-token"}
        ):
            self.assertEqual(drone_api._edge_token_for(settings), "live-token")

    def test_falls_back_to_env_token_when_config_empty(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(edge_relay, "overmind_load_config", return_value={}):
            self.assertEqual(drone_api._edge_token_for(settings), "env-token")

    def test_empty_when_no_token_anywhere(self):
        settings = self._settings()
        with mock.patch.object(edge_relay, "overmind_load_config", return_value={}):
            self.assertEqual(drone_api._edge_token_for(settings), "")

    def test_handles_config_error(self):
        settings = self._settings(token_env="env-token")
        with mock.patch.object(
            edge_relay, "overmind_load_config", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(drone_api._edge_token_for(settings), "env-token")


class TransferOfferServeTests(unittest.TestCase):
    """Sender side: a TRANSFER_OFFER serves the local asset over a relay leg."""

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

    def test_serve_transfer_offer_streams_local_rom(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snes").mkdir()
            (root / "snes" / "g.sfc").write_bytes(b"X" * 5000)
            settings = self._settings(ROMS_ROOT=str(root))

            sender_sock, receiver_sock = socket.socketpair()

            class FakeMux:
                def open_relay_session(self, session_id, role, ready_timeout=20.0):
                    assert role == "sender"
                    return TlsMuxLink(sender_sock)

            offer = {
                "session_id": "a" * 32,
                "asset": {"kind": "rom", "relative_path": "snes/g.sfc"},
            }
            result = {}

            def run():
                result.update(drone_api._serve_transfer_offer(settings, FakeMux(), offer))

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                received = bytearray()
                assetfetch.download(
                    TlsMuxLink(receiver_sock),
                    {"kind": "rom", "relative_path": "snes/g.sfc"},
                    received.extend,
                )
                thread.join(5.0)
                self.assertEqual(bytes(received), b"X" * 5000)
                self.assertEqual(result.get("status"), "completed")
            finally:
                for sock in (sender_sock, receiver_sock):
                    try:
                        sock.close()
                    except OSError:
                        pass


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


class _FakeRelayMux:
    """Stands in for the Edge+sender: marks the receiver channel ready on request
    and, when the receiver sends an AssetFetch FETCH, feeds back CHUNK + DONE."""

    def __init__(self, content: bytes):
        self._content = content
        self.channel = None

    def start_relay_session(self, session_id, role):
        self.channel = RelayChannel(session_id, self._on_send)
        return self.channel

    def send_transfer_request(self, session_id, token, source_device, asset):
        self.channel.mark_ready()

    def close_relay_session(self, session_id):
        if self.channel is not None:
            self.channel.close()

    def _on_send(self, frame_bytes):
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(frame_bytes)))
        if kind != mux.FRAME_DATA:
            return
        _, asset_frame = mux.parse_relay_data(payload)
        message_type, _ = assetfetch.read_message(mux.reader_from_fileobj(io.BytesIO(asset_frame)))
        if message_type == assetfetch.AF_FETCH:
            self.channel.feed(assetfetch.encode_chunk(self._content))
            self.channel.feed(assetfetch.encode_done(len(self._content)))


class RelayDownloadEndToEndTests(unittest.TestCase):
    def _settings(self, roms_root):
        with mock.patch.dict(
            "os.environ", {"OVERMIND_DEVICE_ID": "rx", "ROMS_ROOT": str(roms_root)}, clear=True
        ):
            return drone_api.Settings.from_env()

    def test_relay_download_rom_writes_and_verifies(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            content = b"ROM!" * 1000
            fake = _FakeRelayMux(content)
            config = {"overmind_url": "https://o", "overmind_token": "t"}
            with mock.patch.object(edge_relay, "_EDGE_MUX_CLIENT", fake), mock.patch.object(
                edge_relay, "_request_transfer_session", return_value=("s" * 32, "tok")
            ):
                activity = drone_api._relay_download_rom(
                    settings,
                    config,
                    {"drone_id": "TX"},
                    "snes",
                    "g.sfc",
                    expected_size=len(content),
                    expected_fingerprint=None,
                )
            self.assertEqual(activity["status"], "completed")
            self.assertEqual(activity["transport"], "relay")
            self.assertEqual(activity["bytes_transferred"], len(content))
            self.assertEqual(activity["source_drone_id"], "TX")
            self.assertEqual((Path(directory) / "snes" / "g.sfc").read_bytes(), content)

    def test_relay_download_rom_skips_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            (Path(directory) / "snes").mkdir()
            (Path(directory) / "snes" / "g.sfc").write_bytes(b"already here")
            with mock.patch.object(edge_relay, "_EDGE_MUX_CLIENT", _FakeRelayMux(b"x")):
                activity = drone_api._relay_download_rom(
                    settings, {}, {"drone_id": "TX"}, "snes", "g.sfc"
                )
            self.assertEqual(activity["status"], "skipped")

    def test_tags_holepunch_when_direct(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(directory)
            content = b"ROM!" * 1000
            fake = _FakeRelayMux(content)
            config = {"overmind_url": "https://o", "overmind_token": "t"}
            with mock.patch.object(edge_relay, "_EDGE_MUX_CLIENT", fake), mock.patch.object(
                edge_relay, "_request_transfer_session", return_value=("s" * 32, "tok")
            ), mock.patch.object(edge_relay, "_maybe_holepunch", side_effect=lambda s, ch: (ch, True)):
                activity = drone_api._relay_download_rom(
                    settings, config, {"drone_id": "TX"}, "snes", "g.sfc", expected_size=len(content)
                )
            self.assertEqual(activity["transport"], "holepunch")
            self.assertEqual((Path(directory) / "snes" / "g.sfc").read_bytes(), content)


class HolepunchWiringTests(unittest.TestCase):
    def _settings(self, **env):
        base = {"OVERMIND_DEVICE_ID": "dev1"}
        base.update(env)
        with mock.patch.dict("os.environ", base, clear=True):
            return drone_api.Settings.from_env()

    def test_edge_stun_addr(self):
        settings = self._settings(DRONE_EDGE_URL="tls://edge.example:9443", DRONE_EDGE_STUN_PORT="9444")
        self.assertEqual(drone_api._edge_stun_addr(settings), ("edge.example", 9444))

    def test_edge_stun_addr_none_without_url(self):
        self.assertIsNone(drone_api._edge_stun_addr(self._settings()))

    def test_maybe_holepunch_disabled_or_no_stun(self):
        channel = object()
        disabled = self._settings(DRONE_HOLEPUNCH_ENABLED="0", DRONE_EDGE_URL="tls://e:9443")
        self.assertEqual(drone_api._maybe_holepunch(disabled, channel), (channel, False))
        self.assertEqual(drone_api._maybe_holepunch(self._settings(), channel), (channel, False))

    def test_maybe_holepunch_delegates_with_stun_addr(self):
        settings = self._settings(DRONE_EDGE_URL="tls://edge.example:9443")
        channel, udp = object(), object()
        with mock.patch.object(
            drone_api._holepunch, "negotiate_direct_channel", return_value=(udp, True)
        ) as negotiate:
            self.assertEqual(drone_api._maybe_holepunch(settings, channel), (udp, True))
        self.assertEqual(negotiate.call_args[0][0], channel)
        self.assertEqual(negotiate.call_args[0][1], ("edge.example", 9444))

    def test_maybe_holepunch_swallows_errors(self):
        settings = self._settings(DRONE_EDGE_URL="tls://edge.example:9443")
        channel = object()
        with mock.patch.object(
            drone_api._holepunch, "negotiate_direct_channel", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(drone_api._maybe_holepunch(settings, channel), (channel, False))


class LanDirectTransportTests(unittest.TestCase):
    def _transport(self, my_public, fetch_fn=lambda r, c: {"status": "completed"}):
        return LanDirectTransport(fetch_fn, local_network=lambda: {"public_ip": my_public})

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

    @staticmethod
    def _ctx(peer):
        return TransferContext(settings=None, repository=None, config={}, peer=peer)


class PeerSelectionRelayTests(unittest.TestCase):
    DIRECT = {
        "device_id": "direct",
        "online": True,
        "public_resolvable": True,
        "public_reachable_url": "https://198.51.100.5:443",
    }
    RELAY = {"device_id": "relay-src", "online": True, "edge_online": True}

    def test_edge_online_peer_selected_when_no_direct(self):
        peer = select_best_peer([self.RELAY], [], "me")
        self.assertEqual(peer["device_id"], "relay-src")

    def test_direct_preferred_over_relay_even_if_slower(self):
        fast_relay = {**self.RELAY, "last_speed_sample": {"upload_mbps": 999}}
        slow_direct = {**self.DIRECT, "last_speed_sample": {"upload_mbps": 1}}
        peer = select_best_peer([fast_relay, slow_direct], [], "me")
        self.assertEqual(peer["device_id"], "direct")

    def test_unreachable_peer_skipped(self):
        self.assertIsNone(select_best_peer([{"device_id": "nope", "online": True}], [], "me"))

    def test_allow_relay_false_excludes_edge_only(self):
        self.assertIsNone(select_best_peer([self.RELAY], [], "me", allow_relay=False))

    def test_failed_direct_check_falls_back_to_relay(self):
        peer_both = {**self.DIRECT, "device_id": "p", "edge_online": True}
        checks = [{"target_drone_id": "p", "status": "fail"}]
        self.assertEqual(select_best_peer([peer_both], checks, "me")["device_id"], "p")

    def test_failed_direct_check_without_relay_is_skipped(self):
        peer_direct = {**self.DIRECT, "device_id": "p"}
        checks = [{"target_drone_id": "p", "status": "fail"}]
        self.assertIsNone(select_best_peer([peer_direct], checks, "me"))


if __name__ == "__main__":
    unittest.main()
