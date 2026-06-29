"""Tests for the mux wire-protocol codec (app.transport.mux)."""

import io
import struct
import unittest

from app.transport import mux


class _ShortReadFile:
    """A binary file-like that hands back at most ``chunk`` bytes per read, to
    exercise reader_from_fileobj's loop across partial reads (as real sockets do)."""

    def __init__(self, data: bytes, chunk: int = 3) -> None:
        self._buf = io.BytesIO(data)
        self._chunk = chunk

    def read(self, n: int) -> bytes:
        return self._buf.read(min(n, self._chunk))


class EncodeDecodeTests(unittest.TestCase):
    def test_control_round_trip(self):
        msg = {"type": mux.MSG_HELLO, "device_id": "d1", "caps": ["relay", "lan"]}
        frame = mux.encode_control(msg)
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(frame)))
        self.assertEqual(kind, mux.FRAME_CONTROL)
        self.assertEqual(mux.decode_control(payload), msg)

    def test_control_requires_type(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.encode_control({"device_id": "d1"})

    def test_decode_control_rejects_non_object(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.decode_control(b"[1, 2, 3]")

    def test_decode_control_rejects_bad_json(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.decode_control(b"\xff\xfe not json")

    def test_encode_frame_rejects_oversized_payload(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.encode_frame(mux.FRAME_DATA, b"x" * (mux.MAX_FRAME_PAYLOAD + 1))

    def test_data_frame_round_trip(self):
        blob = bytes(range(256)) * 4
        frame = mux.encode_frame(mux.FRAME_DATA, blob)
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(frame)))
        self.assertEqual(kind, mux.FRAME_DATA)
        self.assertEqual(payload, blob)

    def test_empty_payload_round_trip(self):
        frame = mux.encode_frame(mux.FRAME_DATA, b"")
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(frame)))
        self.assertEqual((kind, payload), (mux.FRAME_DATA, b""))


class ReadFrameTests(unittest.TestCase):
    def test_reads_multiple_frames_in_sequence(self):
        a = mux.encode_control({"type": mux.MSG_PING})
        b = mux.encode_control({"type": mux.MSG_PONG})
        reader = mux.reader_from_fileobj(io.BytesIO(a + b))
        self.assertEqual(mux.decode_control(mux.read_frame(reader)[1]), {"type": mux.MSG_PING})
        self.assertEqual(mux.decode_control(mux.read_frame(reader)[1]), {"type": mux.MSG_PONG})

    def test_eof_at_frame_boundary_raises_eoferror(self):
        reader = mux.reader_from_fileobj(io.BytesIO(b""))
        with self.assertRaises(EOFError):
            mux.read_frame(reader)

    def test_truncated_header_raises(self):
        reader = mux.reader_from_fileobj(io.BytesIO(b"\x01\x00"))  # 2 of 5 header bytes
        with self.assertRaises(mux.MuxProtocolError):
            mux.read_frame(reader)

    def test_truncated_payload_raises(self):
        frame = mux.encode_control({"type": mux.MSG_HELLO})
        reader = mux.reader_from_fileobj(io.BytesIO(frame[:-1]))  # drop last payload byte
        with self.assertRaises(mux.MuxProtocolError):
            mux.read_frame(reader)

    def test_declared_payload_too_large_raises(self):
        header = struct.pack(">BI", mux.FRAME_DATA, mux.MAX_FRAME_PAYLOAD + 1)
        reader = mux.reader_from_fileobj(io.BytesIO(header))
        with self.assertRaises(mux.MuxProtocolError):
            mux.read_frame(reader)

    def test_reader_handles_partial_reads(self):
        frame = mux.encode_control({"type": mux.MSG_HELLO, "device_id": "abcdefghij"})
        reader = mux.reader_from_fileobj(_ShortReadFile(frame, chunk=3))
        kind, payload = mux.read_frame(reader)
        self.assertEqual(kind, mux.FRAME_CONTROL)
        self.assertEqual(mux.decode_control(payload)["device_id"], "abcdefghij")


class RelayFramingTests(unittest.TestCase):
    def test_relay_data_round_trip(self):
        session_id = "a" * mux.RELAY_SESSION_ID_LEN
        frame = mux.encode_relay_data(session_id, b"chunk-bytes")
        kind, payload = mux.read_frame(mux.reader_from_fileobj(io.BytesIO(frame)))
        self.assertEqual(kind, mux.FRAME_DATA)
        got_session, data = mux.parse_relay_data(payload)
        self.assertEqual(got_session, session_id)
        self.assertEqual(data, b"chunk-bytes")

    def test_session_id_must_be_fixed_width(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.encode_relay_data("too-short", b"x")

    def test_parse_rejects_short_payload(self):
        with self.assertRaises(mux.MuxProtocolError):
            mux.parse_relay_data(b"shorter-than-session-id")


if __name__ == "__main__":
    unittest.main()
