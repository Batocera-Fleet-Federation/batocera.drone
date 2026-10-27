"""Issue #588: inner VPN/RGS helpers must not raise while reporting failures.

#582's poller-level suites mock check_and_self_heal / reconcile_rgs_with_tunnel
to raise, so a poller Exception guard can hide a naive print() inside the
helpers. Those helpers are also called on their own, document "Never raises",
and must keep that contract when:

* os.fsdecode leaves a lone surrogate in exception text
* FileNotFoundError is constructed from a surrogate path (str() keeps it)
* exception formatting itself raises
* stderr is a strict UTF-8 TextIOWrapper, or is closed/broken
"""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.device.vpn_manager as vpn_manager
from app.drone_api import Settings


class _BrokenStderr(io.TextIOBase):
    encoding = "utf-8"
    errors = "strict"

    def write(self, _text: str) -> int:
        raise OSError("simulated stderr EIO")

    def flush(self) -> None:
        raise OSError("simulated stderr EIO")


class _UnprintableError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception formatting failed")


def _strict_utf8_stderr() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(
        raw, encoding="utf-8", errors="strict", write_through=True,
    ), raw


class Issue588HelperNoRaiseContractUAT(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        with mock.patch.dict("os.environ", {
            "USERDATA_ROOT": str(root),
            "ROMS_ROOT": str(root / "roms"),
            "BIOS_ROOT": str(root / "bios"),
            "SAVES_ROOT": str(root / "saves"),
            "DRONE_STATE_DATABASE_FILE": str(root / "state.sqlite3"),
            "DRONE_DEVICE_ID": "adversarial-588-helper-no-raise",
        }, clear=True):
            self.settings = Settings.from_env()

    def _call_helpers(self, *, error: BaseException, stderr) -> tuple[object, object]:
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=error), \
                mock.patch.object(vpn_manager.sys, "stderr", stderr):
            check_result = vpn_manager.check_and_self_heal(self.settings)
            rgs_result = vpn_manager.reconcile_rgs_with_tunnel(self.settings)
        return check_result, rgs_result

    def test_premise_strict_utf8_stderr_rejects_a_raw_surrogate(self) -> None:
        stream, _ = _strict_utf8_stderr()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("bad \udcff path\n")

    def test_surrogate_message_on_strict_stderr_returns_none(self) -> None:
        stream, raw = _strict_utf8_stderr()
        check_result, rgs_result = self._call_helpers(
            error=RuntimeError("openvpn log: bad \udcff path"),
            stderr=stream,
        )

        self.assertIsNone(check_result)
        self.assertIsNone(rgs_result)
        stream.flush()
        report = raw.getvalue().decode("utf-8")
        self.assertIn("VPN self-heal check failed", report)
        self.assertIn("RGS tunnel reconcile failed", report)
        self.assertIn("\\udcff", report)
        self.assertNotIn("\udcff", report)

    def test_file_not_found_surrogate_path_message_returns_none(self) -> None:
        # FileNotFoundError(path) keeps the lone surrogate in str(); OSError
        # with a filename= argument repr-escapes it and would not exercise
        # the encoder.
        stream, raw = _strict_utf8_stderr()
        check_result, rgs_result = self._call_helpers(
            error=FileNotFoundError("/userdata/vpn/bad\udcff/openvpn.log"),
            stderr=stream,
        )

        self.assertIsNone(check_result)
        self.assertIsNone(rgs_result)
        stream.flush()
        self.assertIn(b"\\udcff", raw.getvalue())

    def test_unprintable_inner_exception_returns_none(self) -> None:
        stream, raw = _strict_utf8_stderr()
        check_result, rgs_result = self._call_helpers(
            error=_UnprintableError("ignored"),
            stderr=stream,
        )

        self.assertIsNone(check_result)
        self.assertIsNone(rgs_result)
        stream.flush()
        report = raw.getvalue().decode("utf-8")
        self.assertIn("_UnprintableError: <unprintable exception>", report)

    def test_broken_stderr_still_returns_none(self) -> None:
        check_result, rgs_result = self._call_helpers(
            error=OSError("rgs unit: bad \udcff path"),
            stderr=_BrokenStderr(),
        )
        self.assertIsNone(check_result)
        self.assertIsNone(rgs_result)

    def test_keyboard_interrupt_from_inner_work_is_not_swallowed(self) -> None:
        with mock.patch.object(vpn_manager, "_swarm_networking_allowed", return_value=True), \
                mock.patch.object(vpn_manager, "_load_state", side_effect=KeyboardInterrupt), \
                mock.patch.object(vpn_manager.sys, "stderr", _BrokenStderr()):
            with self.assertRaises(KeyboardInterrupt):
                vpn_manager.check_and_self_heal(self.settings)


if __name__ == "__main__":
    unittest.main()
