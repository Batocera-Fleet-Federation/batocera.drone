"""Tests for UDP hole-punch primitives (app.transport.holepunch)."""

import json
import socket
import threading
import unittest

from app.transport.holepunch import (
    HolePunchUnavailable,
    gather_udp_candidate,
    hole_punch,
    negotiate_direct_channel,
    parse_addr,
)


class ParseAddrTests(unittest.TestCase):
    def test_ipv4(self):
        self.assertEqual(parse_addr("203.0.113.7:5000"), ("203.0.113.7", 5000))

    def test_ipv6(self):
        self.assertEqual(parse_addr("[2001:db8::1]:9000"), ("2001:db8::1", 9000))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_addr("nonsense")


class GatherCandidateTests(unittest.TestCase):
    def _start_reflector(self):
        reflector = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        reflector.bind(("127.0.0.1", 0))
        reflector.settimeout(5.0)

        def serve():
            try:
                _data, addr = reflector.recvfrom(512)
                reflector.sendto(json.dumps({"ip": addr[0], "port": addr[1]}).encode(), addr)
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return reflector, reflector.getsockname(), thread

    def test_learns_reflexive_address(self):
        reflector, addr, thread = self._start_reflector()
        try:
            sock, reflexive = gather_udp_candidate(addr, timeout=5.0)
            try:
                self.assertTrue(reflexive.startswith("127.0.0.1:"))
                host, port = reflexive.rsplit(":", 1)
                self.assertEqual(host, "127.0.0.1")
                self.assertEqual(int(port), sock.getsockname()[1])
            finally:
                sock.close()
            thread.join(5.0)
        finally:
            reflector.close()

    def test_timeout_raises_unavailable(self):
        # Nothing is listening on this port; the STUN request times out.
        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dead.bind(("127.0.0.1", 0))
        dead_addr = dead.getsockname()
        dead.close()
        with self.assertRaises(HolePunchUnavailable):
            gather_udp_candidate(dead_addr, timeout=0.3)


class HolePunchTests(unittest.TestCase):
    def test_two_sockets_punch_each_other(self):
        s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s1.bind(("127.0.0.1", 0))
        s2.bind(("127.0.0.1", 0))
        results = {}

        def punch(name, sock, peer):
            results[name] = hole_punch(sock, peer, attempts=15, interval=0.1)

        try:
            t1 = threading.Thread(target=punch, args=("s1", s1, s2.getsockname()), daemon=True)
            t2 = threading.Thread(target=punch, args=("s2", s2, s1.getsockname()), daemon=True)
            t1.start()
            t2.start()
            t1.join(5.0)
            t2.join(5.0)
            self.assertTrue(results.get("s1"))
            self.assertTrue(results.get("s2"))
        finally:
            s1.close()
            s2.close()

    def test_punch_to_nobody_fails(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dead.bind(("127.0.0.1", 0))
        dead_addr = dead.getsockname()
        dead.close()
        try:
            self.assertFalse(hole_punch(sock, dead_addr, attempts=2, interval=0.05))
        finally:
            sock.close()


class _FakeSock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSignalChannel:
    def __init__(self, recvs=()):
        self.sent = []
        self._recvs = list(recvs)

    def send_signal(self, payload):
        self.sent.append(payload)

    def recv_signal(self, timeout):
        return self._recvs.pop(0) if self._recvs else None


class NegotiateDirectChannelTests(unittest.TestCase):
    def test_success_returns_direct_channel(self):
        sock = _FakeSock()
        # Peer sends its candidate, then confirms the punch.
        channel = _FakeSignalChannel([{"candidate": "198.51.100.4:6000"}, {"punched": True}])
        result, is_direct = negotiate_direct_channel(
            channel,
            ("stun", 9444),
            gather=lambda addr: (sock, "203.0.113.7:5000"),
            punch=lambda s, addr, attempts=20: True,
            make_channel=lambda s, addr: ("udp", addr),
        )
        self.assertTrue(is_direct)
        self.assertEqual(result, ("udp", ("198.51.100.4", 6000)))
        self.assertEqual(channel.sent, [{"candidate": "203.0.113.7:5000"}, {"punched": True}])

    def test_no_peer_candidate_falls_back_to_relay(self):
        sock = _FakeSock()
        channel = _FakeSignalChannel([])  # no peer candidate
        result, is_direct = negotiate_direct_channel(
            channel, ("s", 1), gather=lambda a: (sock, "x:1"), punch=lambda *a, **k: True
        )
        self.assertFalse(is_direct)
        self.assertIs(result, channel)
        self.assertTrue(sock.closed)

    def test_punch_failure_falls_back_to_relay(self):
        sock = _FakeSock()
        channel = _FakeSignalChannel([{"candidate": "198.51.100.4:6000"}])
        result, is_direct = negotiate_direct_channel(
            channel, ("s", 1), gather=lambda a: (sock, "x:1"), punch=lambda *a, **k: False
        )
        self.assertFalse(is_direct)
        self.assertIs(result, channel)
        self.assertTrue(sock.closed)

    def test_unconfirmed_punch_falls_back_to_relay(self):
        sock = _FakeSock()
        # Peer sent a candidate but never confirms the punch.
        channel = _FakeSignalChannel([{"candidate": "198.51.100.4:6000"}])
        result, is_direct = negotiate_direct_channel(
            channel, ("s", 1), gather=lambda a: (sock, "x:1"), punch=lambda *a, **k: True
        )
        self.assertFalse(is_direct)
        self.assertIs(result, channel)
        self.assertTrue(sock.closed)

    def test_gather_failure_falls_back_to_relay(self):
        channel = _FakeSignalChannel([{"candidate": "x:1"}])

        def boom(addr):
            raise HolePunchUnavailable("no stun")

        result, is_direct = negotiate_direct_channel(
            channel, ("s", 1), gather=boom, punch=lambda *a, **k: True
        )
        self.assertFalse(is_direct)
        self.assertIs(result, channel)


if __name__ == "__main__":
    unittest.main()
