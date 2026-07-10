"""Tests for the AssetFetch protocol (app.transport.assetfetch)."""

import io
import socket
import threading
import unittest

from app.transport import assetfetch, mux
from app.transport.mux_client import TlsMuxLink


def _reader(data: bytes):
    return mux.reader_from_fileobj(io.BytesIO(data))


class _ReplayChannel:
    """A channel that replays a fixed byte script for reads and records sends."""

    def __init__(self, script: bytes = b""):
        self._read = _reader(script)
        self.sent = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def read_exactly(self, n: int) -> bytes:
        return self._read(n)

    def close(self) -> None:
        pass


class CodecTests(unittest.TestCase):
    def test_fetch_round_trip(self):
        frame = assetfetch.encode_fetch({"kind": "rom", "relative_path": "g.sfc"}, 100)
        mtype, payload = assetfetch.read_message(_reader(frame))
        self.assertEqual(mtype, assetfetch.AF_FETCH)
        self.assertEqual(
            assetfetch.decode_json(payload),
            {"asset": {"kind": "rom", "relative_path": "g.sfc"}, "offset": 100},
        )

    def test_chunk_round_trip(self):
        frame = assetfetch.encode_chunk(b"\x00\x01\x02binary")
        mtype, payload = assetfetch.read_message(_reader(frame))
        self.assertEqual(mtype, assetfetch.AF_CHUNK)
        self.assertEqual(payload, b"\x00\x01\x02binary")

    def test_done_and_err_and_cancel(self):
        for frame, expected_type, expected in (
            (assetfetch.encode_done(42, "h1"), assetfetch.AF_DONE, {"size": 42, "hash": "h1"}),
            (assetfetch.encode_err("not_found", "nope"), assetfetch.AF_ERR,
             {"code": "not_found", "message": "nope"}),
            (assetfetch.encode_cancel(), assetfetch.AF_CANCEL, {}),
        ):
            mtype, payload = assetfetch.read_message(_reader(frame))
            self.assertEqual(mtype, expected_type)
            self.assertEqual(assetfetch.decode_json(payload), expected)


class DownloadTests(unittest.TestCase):
    def test_raises_on_sender_error(self):
        channel = _ReplayChannel(assetfetch.encode_err("not_found", "nope"))
        with self.assertRaises(assetfetch.AssetFetchError):
            assetfetch.download(channel, {"kind": "rom"}, lambda b: None)
        # Even on error, it first sent the FETCH request.
        self.assertEqual(assetfetch.read_message(_reader(channel.sent[0]))[0], assetfetch.AF_FETCH)

    def test_cancel_before_read_sends_cancel_and_raises(self):
        cancel = threading.Event()
        cancel.set()
        channel = _ReplayChannel()  # read would raise EOF; must not be reached
        with self.assertRaises(assetfetch.AssetFetchCancelled):
            assetfetch.download(channel, {"kind": "rom"}, lambda b: None, cancel=cancel)
        self.assertEqual(len(channel.sent), 2)  # FETCH then CANCEL
        self.assertEqual(assetfetch.read_message(_reader(channel.sent[1]))[0], assetfetch.AF_CANCEL)

    def test_progress_reports_running_total_with_offset(self):
        chunks = assetfetch.encode_chunk(b"abcd") + assetfetch.encode_chunk(b"ef") + assetfetch.encode_done(6)
        channel = _ReplayChannel(chunks)
        seen = []
        assetfetch.download(channel, {"kind": "rom"}, lambda b: None, offset=10, progress=seen.append)
        self.assertEqual(seen, [14, 16])  # 10 + 4, then + 2


class EndToEndTests(unittest.TestCase):
    def test_download_and_serve_over_socketpair(self):
        a, b = socket.socketpair()
        content = bytes(range(256)) * 50  # 12,800 bytes across many chunks

        def resolve(asset, offset):
            data = content[offset:]
            pieces = [data[i : i + 1000] for i in range(0, len(data), 1000)]
            return pieces, {"size": len(content), "hash": "h1"}

        server_result = {}

        def serve():
            server_result.update(assetfetch.serve_one(TlsMuxLink(b), resolve))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        received = bytearray()
        meta = assetfetch.download(TlsMuxLink(a), {"kind": "rom", "relative_path": "g"}, received.extend)
        thread.join(5.0)

        self.assertEqual(bytes(received), content)
        self.assertEqual(meta, {"size": len(content), "hash": "h1"})
        self.assertEqual(server_result.get("status"), "completed")
        self.assertEqual(server_result.get("bytes"), len(content))
        for sock in (a, b):
            try:
                sock.close()
            except OSError:
                pass

    def test_serve_reports_not_found(self):
        a, b = socket.socketpair()

        def serve():
            assetfetch.serve_one(TlsMuxLink(b), lambda asset, offset: None)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with self.assertRaises(assetfetch.AssetFetchError):
            assetfetch.download(TlsMuxLink(a), {"kind": "rom"}, lambda chunk: None)
        thread.join(5.0)
        for sock in (a, b):
            try:
                sock.close()
            except OSError:
                pass


class ServeSessionTests(unittest.TestCase):
    """serve_session serves many FETCHes over one channel until close/CANCEL."""

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def test_multiple_fetches_then_close(self):
        a, b = socket.socketpair()
        assets = {"one": b"first asset", "two": b"second asset bytes"}

        def resolve(asset, offset):
            data = assets.get(str(asset.get("relative_path") or ""))
            if data is None:
                return None
            return [data[offset:]], {"size": len(data), "hash": None}

        server_result = {}

        def serve():
            server_result.update(assetfetch.serve_session(TlsMuxLink(b), resolve))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        link = TlsMuxLink(a)
        for name, expected in assets.items():
            received = bytearray()
            assetfetch.download(link, {"kind": "rom", "relative_path": name}, received.extend)
            self.assertEqual(bytes(received), expected)
        # Receiver closing the channel is what ends the session (EOF at a frame
        # boundary), exactly like a single-file receiver from an older drone.
        self._close(a)
        thread.join(5.0)
        self.assertEqual(server_result.get("status"), "completed")
        self.assertEqual(server_result.get("fetches"), 2)
        self.assertEqual(server_result.get("bytes"), sum(len(v) for v in assets.values()))
        self._close(b)

    def test_not_found_keeps_session_alive(self):
        a, b = socket.socketpair()

        def resolve(asset, offset):
            if asset.get("relative_path") == "missing":
                return None
            return [b"ok"], {"size": 2, "hash": None}

        server_result = {}

        def serve():
            server_result.update(assetfetch.serve_session(TlsMuxLink(b), resolve))

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        link = TlsMuxLink(a)
        with self.assertRaises(assetfetch.AssetFetchError):
            assetfetch.download(link, {"kind": "rom", "relative_path": "missing"}, lambda c: None)
        received = bytearray()
        assetfetch.download(link, {"kind": "rom", "relative_path": "there"}, received.extend)
        self.assertEqual(bytes(received), b"ok")
        self._close(a)
        thread.join(5.0)
        self.assertEqual(server_result.get("status"), "completed")
        self._close(b)

    def test_cancel_ends_session(self):
        a, b = socket.socketpair()
        server_result = {}

        def serve():
            server_result.update(
                assetfetch.serve_session(TlsMuxLink(b), lambda asset, offset: ([b"x"], {"size": 1, "hash": None}))
            )

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        TlsMuxLink(a).send(assetfetch.encode_cancel())
        thread.join(5.0)
        self.assertEqual(server_result.get("status"), "cancelled")
        self.assertEqual(server_result.get("fetches"), 0)
        for sock in (a, b):
            self._close(sock)


if __name__ == "__main__":
    unittest.main()
