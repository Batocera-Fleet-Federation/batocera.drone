"""Adversarial acceptance for Issue #83: ``_install_unlink_guard`` must
patch ``os.remove`` even when it is a distinct function from ``os.unlink``.

On CPython/POSIX, ``os.remove is os.unlink`` is False. The pre-#83 guard
only installed on ``os.remove`` when that identity held, so ``os.remove``
stayed unpatched for the rename-aside-unlink window. The defense-in-depth
invariant is: every primitive that can delete a path by name during that
window must go through the identity-checked guard, including a future
caller that uses ``os.remove`` rather than ``Path.unlink`` / ``os.unlink``.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.transfer.torrent_manager import (
    _install_unlink_guard,
    _restore_unlink_guard,
    _retry_unlink_pending_removal_file,
    _stat_fingerprint,
)


class OsRemoveIsDistinctFromUnlinkTests(unittest.TestCase):
    """The bug's premise: identity-based patching of os.remove never fires
    on this interpreter."""

    def test_os_remove_is_not_the_same_object_as_os_unlink(self) -> None:
        self.assertIsNot(
            os.remove,
            os.unlink,
            "this suite is written for CPython/POSIX where os.remove and "
            "os.unlink are distinct; identity-gated install(os, 'remove') "
            "would never patch os.remove here",
        )


class InstallUnlinkGuardAlwaysPatchesOsRemoveTests(unittest.TestCase):
    def test_install_patches_os_remove_even_when_it_is_not_os_unlink(self) -> None:
        self.assertIsNot(os.remove, os.unlink)
        original_remove = os.remove
        original_unlink = os.unlink

        def guarded(target, dir_fd=None, **kwargs):
            raise AssertionError("guarded must not run in this install-only test")

        patches = _install_unlink_guard(guarded, original_unlink)
        try:
            self.assertIs(
                os.remove,
                guarded,
                "os.remove must be patched for the guard window even though "
                "it is not os.unlink",
            )
            self.assertIs(os.unlink, guarded)
        finally:
            _restore_unlink_guard(patches)

        self.assertIs(os.remove, original_remove)
        self.assertIs(os.unlink, original_unlink)

    def test_restore_puts_back_the_pre_guard_os_remove(self) -> None:
        sentinel = object()
        original_remove = os.remove

        def fake_remove(path, *args, **kwargs):
            return sentinel

        os.remove = fake_remove
        try:
            def guarded(target, dir_fd=None, **kwargs):
                return None

            patches = _install_unlink_guard(guarded, os.unlink)
            self.assertIs(os.remove, guarded)
            _restore_unlink_guard(patches)
            self.assertIs(os.remove, fake_remove)
            self.assertIs(os.remove(os.devnull), sentinel)
        finally:
            os.remove = original_remove


class OsRemoveDuringGuardWindowTests(unittest.TestCase):
    """While a pending-removal retry holds the process-wide unlink guard,
    os.remove of the held inode must be identity-checked (rename-aside),
    and os.remove of an unrelated path must still delete that path."""

    def test_os_remove_of_held_inode_during_guard_does_not_clobber_replacement(self) -> None:
        """If a concurrent os.remove hits the original name after a
        replacement has taken it, the unpatched os.remove would delete the
        replacement. The guard must no-op when the inode at the name is
        no longer the held one."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "solo.torrent"
            target.write_bytes(b"original-payload")
            fingerprint = _stat_fingerprint(os.stat(target))
            held_dev_ino = (os.stat(target).st_dev, os.stat(target).st_ino)

            guard_active = threading.Event()
            replacement_ready = threading.Event()
            remove_done = threading.Event()
            real_lstat = os.lstat
            saw_guard_lstat = []

            def delaying_lstat(p, *args, **kwargs):
                path_str = os.fspath(p)
                if not saw_guard_lstat and path_str == str(target):
                    saw_guard_lstat.append(True)
                    guard_active.set()
                    if not replacement_ready.wait(timeout=5):
                        raise AssertionError("replacement thread never finished")
                    if not remove_done.wait(timeout=5):
                        raise AssertionError("os.remove thread never finished")
                    # Re-stat after the swap so the guard sees the replacement
                    # inode, matching a real TOCTOU where lstat races with
                    # a same-name rewrite.
                    return real_lstat(p, *args, **kwargs)
                return real_lstat(p, *args, **kwargs)

            def swap_and_remove():
                if not guard_active.wait(timeout=5):
                    return
                # Replace the name with a new inode while the guard is live.
                os.rename(str(target), str(root / "aside-original.bin"))
                target.write_bytes(b"replacement-must-survive")
                replacement_ready.set()
                # Concurrent caller using os.remove (the unpatched primitive).
                os.remove(str(target))
                remove_done.set()

            thread = threading.Thread(target=swap_and_remove)
            thread.start()
            try:
                with mock.patch("os.lstat", delaying_lstat):
                    status = _retry_unlink_pending_removal_file(str(target), fingerprint)
            finally:
                thread.join(timeout=5)

            self.assertTrue(saw_guard_lstat, "never entered the unlink-guard window")
            self.assertFalse(thread.is_alive())
            # Replacement must still be at the watch-folder name.
            self.assertTrue(target.exists(), "replacement at the original name was deleted")
            self.assertEqual(target.read_bytes(), b"replacement-must-survive")
            replacement_st = os.stat(target)
            self.assertNotEqual(
                (replacement_st.st_dev, replacement_st.st_ino),
                held_dev_ino,
            )
            self.assertIn(status, ("unlinked", "mismatch", "gone", "busy"))

    def test_unrelated_os_remove_during_guard_window_still_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "solo.torrent"
            target.write_bytes(b"original")
            fingerprint = _stat_fingerprint(os.stat(target))

            other = root / "unrelated.txt"
            other.write_bytes(b"delete-me")

            guard_active = threading.Event()
            other_deleted = threading.Event()
            real_lstat = os.lstat
            triggered = []

            def delaying_lstat(p, *args, **kwargs):
                result = real_lstat(p, *args, **kwargs)
                if not triggered and os.fspath(p) == str(target):
                    triggered.append(True)
                    guard_active.set()
                    if not other_deleted.wait(timeout=5):
                        raise AssertionError("unrelated os.remove never finished")
                return result

            def delete_other():
                if not guard_active.wait(timeout=5):
                    return
                os.remove(str(other))
                other_deleted.set()

            thread = threading.Thread(target=delete_other)
            thread.start()
            try:
                with mock.patch("os.lstat", delaying_lstat):
                    status = _retry_unlink_pending_removal_file(str(target), fingerprint)
            finally:
                thread.join(timeout=5)

            self.assertTrue(triggered)
            self.assertFalse(thread.is_alive())
            self.assertEqual(status, "unlinked")
            self.assertFalse(target.exists())
            self.assertFalse(
                other.exists(),
                "os.remove of an unrelated path during the guard window "
                "must still delete that path",
            )


if __name__ == "__main__":
    unittest.main()
