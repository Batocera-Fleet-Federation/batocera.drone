"""Tests for the Drone Edge mux client (app.transport.mux_client)."""

import io
import socket
import threading
import time
import unittest

from app.transport import mux
from app.transport.mux_client import (
    MuxClient,
    MuxSession,
    RelayChannel,
    TlsMuxLink,
    parse_edge_endpoint,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class MuxSessionTests(unittest.TestCase):
    def _session(self, **kwargs) -> MuxSession:
        defaults = dict(device_id="d1", token="tok", capabilities=["relay", "lan"])
        defaults.update(kwargs)
        return MuxSession(**defaults)

    def test_build_hello_payload(self):
        session = self._session(lan_addrs=["192.168.1.5"], app_version="9.9.9")
        kind, payload = mux.read_frame(mux.reader_from_fileobj(_bytes_reader(session.build_hello())))
        self.assertEqual(kind, mux.FRAME_CONTROL)
        msg = mux.decode_control(payload)
        self.assertEqual(msg["type"], mux.MSG_HELLO)
        self.assertEqual(msg["device_id"], "d1")
        self.assertEqual(msg["token"], "tok")
        self.assertEqual(msg["capabilities"], ["relay", "lan"])
        self.assertEqual(msg["lan_addrs"], ["192.168.1.5"])
        self.assertEqual(msg["app_version"], "9.9.9")

    def test_hello_ack_marks_connected(self):
        session = self._session()
        self.assertFalse(session.connected)
        out = session.handle_frame(
            *_control({"type": mux.MSG_HELLO_ACK, "session_id": "s1", "reflexive_addr": "9.9.9.9:42"})
        )
        self.assertEqual(out, [])
        self.assertTrue(session.connected)
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.reflexive_addr, "9.9.9.9:42")

    def test_ping_is_answered_with_pong_echoing_token(self):
        session = self._session()
        out = session.handle_frame(*_control({"type": mux.MSG_PING, "t": 7}))
        self.assertEqual(len(out), 1)
        reply = mux.decode_control(_frame_payload(out[0]))
        self.assertEqual(reply, {"type": mux.MSG_PONG, "t": 7})

    def test_presence_invokes_callback(self):
        seen = {}
        session = self._session(on_presence=lambda swarm: seen.setdefault("swarm", swarm))
        session.handle_frame(*_control({"type": mux.MSG_PRESENCE, "swarm": [{"drone_id": "p2"}]}))
        self.assertEqual(seen["swarm"], [{"drone_id": "p2"}])

    def test_bye_clears_connected(self):
        session = self._session()
        session.handle_frame(*_control({"type": mux.MSG_HELLO_ACK, "session_id": "s1"}))
        session.handle_frame(*_control({"type": mux.MSG_BYE}))
        self.assertFalse(session.connected)

    def test_data_frames_are_ignored_in_phase1(self):
        session = self._session()
        self.assertEqual(session.handle_frame(mux.FRAME_DATA, b"\x00\x01"), [])

    def test_last_activity_advances_with_clock(self):
        clock = _Clock()
        session = self._session(now=clock)
        start = session.last_activity
        clock.t += 5
        session.handle_frame(*_control({"type": mux.MSG_PONG}))
        self.assertEqual(session.last_activity, start + 5)


class ParseEndpointTests(unittest.TestCase):
    def test_schemes_and_defaults(self):
        self.assertEqual(parse_edge_endpoint("tls://edge.example:9443"), ("edge.example", 9443))
        self.assertEqual(parse_edge_endpoint("wss://edge.example:443"), ("edge.example", 443))
        self.assertEqual(parse_edge_endpoint("https://edge.example"), ("edge.example", 443))
        self.assertEqual(parse_edge_endpoint("edge.example:8000"), ("edge.example", 8000))
        self.assertEqual(parse_edge_endpoint("edge.example", default_port=9000), ("edge.example", 9000))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_edge_endpoint("")


class MuxClientSocketPairTests(unittest.TestCase):
    """End-to-end over a real socketpair: HELLO -> HELLO_ACK -> PING -> PONG."""

    def test_handshake_and_keepalive(self):
        client_sock, server_sock = socket.socketpair()
        server_sock.settimeout(5.0)
        session = MuxSession(device_id="d1", token="tok", capabilities=["relay"])
        stop = threading.Event()

        client = MuxClient(
            connect=lambda: TlsMuxLink(client_sock),
            session_factory=lambda: session,
            ping_interval=30.0,  # large, so the test (not a timer) drives traffic
            stop_event=stop,
        )
        thread = threading.Thread(target=client.run_forever, daemon=True)
        thread.start()
        try:
            server_reader = mux.reader_from_fileobj(server_sock.makefile("rb"))

            # 1) Drone sends HELLO first.
            kind, payload = mux.read_frame(server_reader)
            hello = mux.decode_control(payload)
            self.assertEqual(hello["type"], mux.MSG_HELLO)
            self.assertEqual(hello["device_id"], "d1")

            # 2) Edge replies HELLO_ACK; Drone should mark itself connected.
            server_sock.sendall(
                mux.encode_control(
                    {"type": mux.MSG_HELLO_ACK, "session_id": "s1", "reflexive_addr": "203.0.113.7:5555"}
                )
            )

            # 3) Edge pings; Drone must answer PONG.
            server_sock.sendall(mux.encode_control({"type": mux.MSG_PING, "t": 99}))
            kind, payload = mux.read_frame(server_reader)
            pong = mux.decode_control(payload)
            self.assertEqual(pong, {"type": mux.MSG_PONG, "t": 99})
        finally:
            stop.set()
            # shutdown (not just close) forces EOF to the client reader even
            # though server_reader's makefile still holds an fd reference.
            for action in (lambda: server_sock.shutdown(socket.SHUT_RDWR), server_sock.close):
                try:
                    action()
                except OSError:
                    pass
            thread.join(timeout=5.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(session.connected)
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.reflexive_addr, "203.0.113.7:5555")


class RelayChannelTests(unittest.TestCase):
    def _channel(self):
        sent = []
        return RelayChannel("s" * 32, sent.append), sent

    def test_read_exactly_returns_fed_bytes(self):
        channel, _ = self._channel()
        channel.feed(b"hello world")
        self.assertEqual(channel.read_exactly(5), b"hello")
        self.assertEqual(channel.read_exactly(6), b" world")

    def test_read_exactly_across_feeds(self):
        channel, _ = self._channel()
        channel.feed(b"ab")
        channel.feed(b"cd")
        self.assertEqual(channel.read_exactly(4), b"abcd")

    def test_read_exactly_returns_partial_then_empty_after_close(self):
        channel, _ = self._channel()
        channel.feed(b"abc")
        channel.close_remote()
        self.assertEqual(channel.read_exactly(10), b"abc")  # partial at EOF
        self.assertEqual(channel.read_exactly(10), b"")  # subsequent reads = EOF

    def test_send_encodes_relay_data(self):
        channel, sent = self._channel()
        channel.send(b"payload")
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(sent[0])))
        self.assertEqual(kind, mux.FRAME_DATA)
        self.assertEqual(mux.parse_relay_data(payload), ("s" * 32, b"payload"))

    def test_close_sends_relay_close_and_eofs(self):
        channel, sent = self._channel()
        channel.close()
        message = mux.decode_control(mux.read_frame(mux.reader_from_fileobj(io.BytesIO(sent[0])))[1])
        self.assertEqual(message["type"], mux.MSG_RELAY_CLOSE)
        self.assertEqual(channel.read_exactly(1), b"")

    def test_ready_event(self):
        channel, _ = self._channel()
        self.assertFalse(channel.wait_ready(0.01))
        channel.mark_ready()
        self.assertTrue(channel.wait_ready(0.01))

    def test_read_blocks_until_fed(self):
        channel, _ = self._channel()

        def feeder():
            time.sleep(0.05)
            channel.feed(b"late")

        thread = threading.Thread(target=feeder)
        thread.start()
        self.assertEqual(channel.read_exactly(4), b"late")
        thread.join()


class MuxClientRelayTests(unittest.TestCase):
    """The mux client demultiplexes relay READY/DATA to the right RelayChannel."""

    def test_open_session_then_receive_and_send(self):
        client_sock, edge_sock = socket.socketpair()
        edge_sock.settimeout(5.0)
        session = MuxSession(device_id="d1", token="t", capabilities=["relay"])
        stop = threading.Event()
        client = MuxClient(
            connect=lambda: TlsMuxLink(client_sock),
            session_factory=lambda: session,
            ping_interval=30.0,
            stop_event=stop,
        )
        client_thread = threading.Thread(target=client.run_forever, daemon=True)
        client_thread.start()

        sid = "a" * 32
        results = {}

        def receiver():
            try:
                channel = client.open_relay_session(sid, "receiver", ready_timeout=5.0)
                results["data"] = channel.read_exactly(7)
                channel.send(b"ackack")
            except Exception as error:  # noqa: BLE001
                results["error"] = error

        try:
            edge_reader = mux.reader_from_fileobj(edge_sock.makefile("rb"))
            # HELLO -> HELLO_ACK so the client is connected.
            self.assertEqual(mux.decode_control(mux.read_frame(edge_reader)[1])["type"], mux.MSG_HELLO)
            edge_sock.sendall(
                mux.encode_control({"type": mux.MSG_HELLO_ACK, "session_id": "x", "reflexive_addr": "1:1"})
            )

            worker = threading.Thread(target=receiver, daemon=True)
            worker.start()

            # Edge sees RELAY_OPEN, answers RELAY_READY, then streams a DATA frame.
            open_msg = mux.decode_control(mux.read_frame(edge_reader)[1])
            self.assertEqual(open_msg["type"], mux.MSG_RELAY_OPEN)
            self.assertEqual((open_msg["session_id"], open_msg["role"]), (sid, "receiver"))
            edge_sock.sendall(mux.encode_control({"type": mux.MSG_RELAY_READY, "session_id": sid}))
            edge_sock.sendall(mux.encode_relay_data(sid, b"payload"))

            # The channel.send(b"ackack") must arrive as a relay DATA frame.
            kind, payload = mux.read_frame(edge_reader)
            self.assertEqual(kind, mux.FRAME_DATA)
            self.assertEqual(mux.parse_relay_data(payload), (sid, b"ackack"))
            worker.join(5.0)
        finally:
            stop.set()
            for action in (lambda: edge_sock.shutdown(socket.SHUT_RDWR), edge_sock.close):
                try:
                    action()
                except OSError:
                    pass
            client_thread.join(5.0)

        self.assertNotIn("error", results)
        self.assertEqual(results.get("data"), b"payload")

    def _connect(self, **client_kwargs):
        client_sock, edge_sock = socket.socketpair()
        edge_sock.settimeout(5.0)
        session = MuxSession(device_id="d1", token="t", capabilities=["relay"])
        stop = threading.Event()
        client = MuxClient(
            connect=lambda: TlsMuxLink(client_sock),
            session_factory=lambda: session,
            ping_interval=30.0,
            stop_event=stop,
            **client_kwargs,
        )
        thread = threading.Thread(target=client.run_forever, daemon=True)
        thread.start()
        edge_reader = mux.reader_from_fileobj(edge_sock.makefile("rb"))
        self.assertEqual(mux.decode_control(mux.read_frame(edge_reader)[1])["type"], mux.MSG_HELLO)
        edge_sock.sendall(
            mux.encode_control({"type": mux.MSG_HELLO_ACK, "session_id": "x", "reflexive_addr": "1:1"})
        )
        return client, edge_sock, edge_reader, stop, thread

    @staticmethod
    def _teardown(edge_sock, stop, thread):
        stop.set()
        for action in (lambda: edge_sock.shutdown(socket.SHUT_RDWR), edge_sock.close):
            try:
                action()
            except OSError:
                pass
        thread.join(5.0)

    def test_transfer_offer_invokes_hook(self):
        offers = []
        client, edge_sock, edge_reader, stop, thread = self._connect(on_transfer_offer=offers.append)
        try:
            edge_sock.sendall(
                mux.encode_control(
                    {
                        "type": mux.MSG_TRANSFER_OFFER,
                        "session_id": "a" * 32,
                        "from_device": "TX",
                        "to_device": "d1",
                        "asset": {"kind": "rom", "relative_path": "g"},
                    }
                )
            )
            deadline = time.time() + 5.0
            while not offers and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(len(offers), 1)
            self.assertEqual(offers[0]["from_device"], "TX")
        finally:
            self._teardown(edge_sock, stop, thread)

    def test_transfer_error_fails_channel(self):
        client, edge_sock, edge_reader, stop, thread = self._connect()
        try:
            session_id = "a" * 32
            channel = client.start_relay_session(session_id, "receiver")
            self.assertEqual(
                mux.decode_control(mux.read_frame(edge_reader)[1])["type"], mux.MSG_RELAY_OPEN
            )
            client.send_transfer_request(session_id, "tok", "TX", {"kind": "rom", "relative_path": "g"})
            self.assertEqual(
                mux.decode_control(mux.read_frame(edge_reader)[1])["type"], mux.MSG_TRANSFER_REQUEST
            )
            edge_sock.sendall(
                mux.encode_control(
                    {"type": mux.MSG_TRANSFER_ERROR, "session_id": session_id, "reason": "offline"}
                )
            )
            self.assertTrue(channel.wait_ready(5.0))  # fail() unblocks wait_ready
            self.assertTrue(channel.failed)
        finally:
            self._teardown(edge_sock, stop, thread)


def _bytes_reader(data: bytes):
    return io.BytesIO(data)


def _control(message: dict):
    """Return (kind, payload) for a control message, as handle_frame expects."""
    frame = mux.encode_control(message)
    return mux.read_frame(mux.reader_from_fileobj(_bytes_reader(frame)))


def _frame_payload(frame: bytes) -> bytes:
    _, payload = mux.read_frame(mux.reader_from_fileobj(_bytes_reader(frame)))
    return payload


if __name__ == "__main__":
    unittest.main()
