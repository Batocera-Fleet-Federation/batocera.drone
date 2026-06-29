"""Tests for relay transfer flows (app.transport.relay_transfer)."""

import socket
import tempfile
import threading
import unittest
from pathlib import Path

from app.transport import assetfetch
from app.transport.base import DownloadRequest, TransferContext
from app.transport.mux_client import RelayChannel, TlsMuxLink
from app.transport.relay_transfer import (
    RelayReceiverTransport,
    open_local_file_source,
    open_receiver_channel,
    serve_asset,
)


class LocalFileSourceTests(unittest.TestCase):
    def test_reads_whole_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "snes").mkdir()
            (root / "snes" / "g.sfc").write_bytes(b"abcdef")
            source = open_local_file_source(root, "snes/g.sfc")
            self.assertIsNotNone(source)
            chunks, meta = source
            self.assertEqual(b"".join(chunks), b"abcdef")
            self.assertEqual(meta["size"], 6)
            self.assertIsNone(meta["hash"])

    def test_offset_seeks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "g.bin").write_bytes(b"0123456789")
            chunks, meta = open_local_file_source(root, "g.bin", offset=4)
            self.assertEqual(b"".join(chunks), b"456789")
            self.assertEqual(meta["size"], 10)  # size is the full file

    def test_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(open_local_file_source(directory, "../etc/passwd"))
            self.assertIsNone(open_local_file_source(directory, "a/../../x"))
            self.assertIsNone(open_local_file_source(directory, "/abs/path"))

    def test_missing_and_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(open_local_file_source(directory, "nope.bin"))
            self.assertIsNone(open_local_file_source(directory, ""))


class ServeAssetTests(unittest.TestCase):
    def test_serves_asset_over_channel(self):
        sender_sock, receiver_sock = socket.socketpair()
        content = b"ROMDATA" * 100

        class FakeMux:
            def open_relay_session(self, session_id, role, ready_timeout=20.0):
                assert role == "sender"
                return TlsMuxLink(sender_sock)

        result = {}

        def run():
            result.update(
                serve_asset(
                    FakeMux(),
                    "s" * 32,
                    lambda asset, offset: ([content], {"size": len(content), "hash": None}),
                )
            )

        server_thread = threading.Thread(target=run, daemon=True)
        server_thread.start()
        try:
            received = bytearray()
            meta = assetfetch.download(
                TlsMuxLink(receiver_sock), {"kind": "rom", "relative_path": "g"}, received.extend
            )
            server_thread.join(5.0)
            self.assertEqual(bytes(received), content)
            self.assertEqual(meta["size"], len(content))
            self.assertEqual(result.get("status"), "completed")
        finally:
            for sock in (sender_sock, receiver_sock):
                try:
                    sock.close()
                except OSError:
                    pass


class OpenReceiverChannelTests(unittest.TestCase):
    class _FakeMux:
        def __init__(self, channel):
            self.channel = channel
            self.calls = []

        def start_relay_session(self, session_id, role):
            self.calls.append(("start", session_id, role))
            return self.channel

        def send_transfer_request(self, session_id, token, from_device, asset):
            self.calls.append(("request", session_id, token, from_device, asset))

        def close_relay_session(self, session_id):
            self.calls.append(("close", session_id))

    def test_happy_path_requests_then_returns_channel(self):
        channel = RelayChannel("s" * 32, lambda frame: None)
        channel.mark_ready()
        mux = self._FakeMux(channel)
        result = open_receiver_channel(
            mux, "s" * 32, "tok", "B", {"kind": "rom", "relative_path": "g"}, ready_timeout=1.0
        )
        self.assertIs(result, channel)
        self.assertEqual(mux.calls[0], ("start", "s" * 32, "receiver"))
        self.assertEqual(mux.calls[1][0], "request")
        self.assertEqual(mux.calls[1][3], "B")

    def test_timeout_closes_and_raises(self):
        channel = RelayChannel("s" * 32, lambda frame: None)  # never becomes ready
        mux = self._FakeMux(channel)
        with self.assertRaises(TimeoutError):
            open_receiver_channel(mux, "s" * 32, "tok", "B", {}, ready_timeout=0.01)
        self.assertIn(("close", "s" * 32), mux.calls)

    def test_rejected_closes_and_raises(self):
        channel = RelayChannel("s" * 32, lambda frame: None)
        channel.fail()
        mux = self._FakeMux(channel)
        with self.assertRaises(ConnectionError):
            open_receiver_channel(mux, "s" * 32, "tok", "B", {}, ready_timeout=1.0)
        self.assertIn(("close", "s" * 32), mux.calls)


class RelayReceiverTransportTests(unittest.TestCase):
    @staticmethod
    def _ctx(peer):
        return TransferContext(settings=None, repository=None, config={}, peer=peer)

    def test_usable_requires_rom_mux_and_peer(self):
        peer = {"drone_id": "TX"}
        available = RelayReceiverTransport(lambda r, c: {}, is_available=lambda: True)
        self.assertTrue(available.usable(DownloadRequest(asset_type="rom"), self._ctx(peer)))
        # v1 only relays ROM files.
        self.assertFalse(available.usable(DownloadRequest(asset_type="bios"), self._ctx(peer)))
        # No live mux -> not usable.
        offline = RelayReceiverTransport(lambda r, c: {}, is_available=lambda: False)
        self.assertFalse(offline.usable(DownloadRequest(asset_type="rom"), self._ctx(peer)))
        # No peer device id -> not usable.
        self.assertFalse(available.usable(DownloadRequest(asset_type="rom"), self._ctx({})))

    def test_fetch_delegates_to_fetch_fn(self):
        transport = RelayReceiverTransport(
            lambda request, context: {"status": "completed", "transport": "relay"},
            is_available=lambda: True,
        )
        result = transport.fetch(DownloadRequest(asset_type="rom"), self._ctx({"drone_id": "TX"}))
        self.assertEqual(result["transport"], "relay")
        self.assertEqual(transport.name, "relay")


if __name__ == "__main__":
    unittest.main()
