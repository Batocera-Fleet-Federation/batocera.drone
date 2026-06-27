"""Tests for the Drone Edge mux client (app.transport.mux_client)."""

import socket
import threading
import unittest

from app.transport import mux
from app.transport.mux_client import (
    MuxClient,
    MuxSession,
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


def _bytes_reader(data: bytes):
    import io

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
