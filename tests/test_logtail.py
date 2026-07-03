"""Tests for the log-viewer tail reader (``common/logtail.py``).

The diagnostics log viewer serves the *tail* of multi-MB log files by seeking from
the end and reading only the last ``max_bytes`` — so it must return exactly the last
window, report truncation, trim to the requested line count, and never choke on
non-UTF-8 log bytes. Extracted untested; this locks the byte/line/truncation contract.
"""
import tempfile
import unittest
from pathlib import Path

from app.common.logtail import _read_file_tail, _tail_lines


class ReadFileTailTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "log.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, data: bytes):
        self.path.write_bytes(data)

    def test_small_file_returned_whole_not_truncated(self):
        self._write(b"0123456789")
        data, truncated = _read_file_tail(self.path, 100)
        self.assertEqual(data, b"0123456789")
        self.assertFalse(truncated)

    def test_large_file_returns_only_last_window_and_flags_truncation(self):
        self._write(b"0123456789")
        data, truncated = _read_file_tail(self.path, 4)
        self.assertEqual(data, b"6789")
        self.assertTrue(truncated)

    def test_exact_boundary_is_not_truncated(self):
        self._write(b"0123456789")
        data, truncated = _read_file_tail(self.path, 10)
        self.assertEqual(data, b"0123456789")
        self.assertFalse(truncated)

    def test_nonpositive_max_bytes_clamps_to_one(self):
        self._write(b"0123456789")
        data, truncated = _read_file_tail(self.path, 0)
        self.assertEqual(data, b"9")
        self.assertTrue(truncated)

    def test_empty_file(self):
        self._write(b"")
        self.assertEqual(_read_file_tail(self.path, 100), (b"", False))


class TailLinesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "log.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_last_n_lines(self):
        self.path.write_bytes(b"l1\nl2\nl3\nl4\nl5\n")
        self.assertEqual(_tail_lines(self.path, 2), ["l4", "l5"])

    def test_line_count_beyond_available_returns_all(self):
        self.path.write_bytes(b"l1\nl2\nl3\n")
        self.assertEqual(_tail_lines(self.path, 50), ["l1", "l2", "l3"])

    def test_nonpositive_line_count_returns_at_least_last_line(self):
        self.path.write_bytes(b"l1\nl2\nl3\n")
        self.assertEqual(_tail_lines(self.path, 0), ["l3"])

    def test_truncation_prepends_banner(self):
        self.path.write_bytes(b"\n".join(b"line%04d" % i for i in range(200)))
        out = _tail_lines(self.path, 3, max_bytes=50)
        self.assertTrue(out[0].startswith("[truncated]"))
        self.assertIn("50 bytes", out[0])
        self.assertEqual(len(out), 4)  # banner + 3 requested lines

    def test_no_banner_when_not_truncated(self):
        self.path.write_bytes(b"l1\nl2\n")
        out = _tail_lines(self.path, 5)
        self.assertFalse(any(line.startswith("[truncated]") for line in out))

    def test_invalid_utf8_is_replaced_not_raised(self):
        self.path.write_bytes(b"good\n\xff\xfe garbage\nend\n")
        out = _tail_lines(self.path, 5)
        self.assertEqual(out[-1], "end")
        self.assertEqual(len(out), 3)  # decoding never dropped or crashed on the bad line


if __name__ == "__main__":
    unittest.main()
