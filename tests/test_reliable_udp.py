"""Tests for the windowed reliable-UDP channel (app.transport.reliable_udp)."""

import queue
import threading
import unittest

from app.transport import assetfetch
from app.transport.reliable_udp import ReliableUDPChannel


def _make_pair(loss_pred=None):
    """Two channels wired through in-memory queues. ``loss_pred(n)`` may drop the
    n-th datagram sent A->B (to exercise retransmission)."""
    a_inbox: "queue.Queue[bytes]" = queue.Queue()
    b_inbox: "queue.Queue[bytes]" = queue.Queue()
    sent = {"n": 0}

    def a_send(data):
        sent["n"] += 1
        if loss_pred and loss_pred(sent["n"]):
            return  # dropped in flight
        b_inbox.put(data)

    def b_send(data):
        a_inbox.put(data)

    def make_recv(inbox):
        def recv(timeout):
            try:
                return inbox.get(timeout=timeout)
            except queue.Empty:
                return None

        return recv

    kwargs = dict(mtu_payload=100, window=8, rto=0.1, tick=0.02)
    chan_a = ReliableUDPChannel(send_datagram=a_send, recv_datagram=make_recv(a_inbox), **kwargs)
    chan_b = ReliableUDPChannel(send_datagram=b_send, recv_datagram=make_recv(b_inbox), **kwargs)
    return chan_a, chan_b


def _read_all(channel, total):
    received = bytearray()
    while len(received) < total:
        chunk = channel.read_exactly(total - len(received))
        if not chunk:
            break
        received.extend(chunk)
    return bytes(received)


class ReliableTransferTests(unittest.TestCase):
    def test_ordered_delivery_no_loss(self):
        chan_a, chan_b = _make_pair()
        payload = bytes(range(256)) * 20  # ~5 KB across many 100-byte packets
        got = {}
        reader = threading.Thread(target=lambda: got.update(data=_read_all(chan_b, len(payload))))
        reader.start()
        chan_a.send(payload)
        reader.join(15)
        chan_a.close()
        chan_b.close()
        self.assertEqual(got["data"], payload)

    def test_recovers_from_packet_loss(self):
        drop = {3, 7, 12, 18}

        def loss(n):
            if n in drop:
                drop.discard(n)  # drop each once; the retransmit gets through
                return True
            return False

        chan_a, chan_b = _make_pair(loss_pred=loss)
        payload = bytes(range(200)) * 12
        got = {}
        reader = threading.Thread(target=lambda: got.update(data=_read_all(chan_b, len(payload))))
        reader.start()
        chan_a.send(payload)
        reader.join(15)
        chan_a.close()
        chan_b.close()
        self.assertEqual(got["data"], payload)


class AssetFetchOverReliableUdpTests(unittest.TestCase):
    def test_download_and_serve(self):
        receiver, sender = _make_pair()
        content = bytes(range(256)) * 30
        result = {}

        def serve():
            result.update(
                assetfetch.serve_one(
                    sender, lambda asset, offset: ([content], {"size": len(content), "hash": None})
                )
            )

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        received = bytearray()
        meta = assetfetch.download(receiver, {"kind": "rom", "relative_path": "g"}, received.extend)
        server_thread.join(15)
        receiver.close()
        sender.close()
        self.assertEqual(bytes(received), content)
        self.assertEqual(meta["size"], len(content))
        self.assertEqual(result.get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
