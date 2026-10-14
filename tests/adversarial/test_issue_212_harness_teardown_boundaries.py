"""Issue #212 boundaries: create_server harnesses must stop LAN workers before rmtree.

Expected behavior is derived from the issue and unittest's lifecycle, not from
the delivery patch:

- ``create_server`` starts the process-global LAN discovery worker against that
  test's real state DB. HTTP ``shutdown`` / ``server_close`` / thread join does
  not stop that worker.
- Callers that own a temporary userdata tree must call
  ``stop_local_network_workers`` so the discovery thread is joined before the
  tree is removed. Otherwise a late announce can WAL-open a deleted sqlite
  path (``PytestUnhandledThreadExceptionWarning``).
- unittest runs ``tearDown`` *before* ``addCleanup`` callbacks. An
  ``addCleanup(stop_...)`` is too late if ``tearDown`` already called
  ``TemporaryDirectory.cleanup``.
- ``addCleanup`` itself is LIFO: registering ``tmp.cleanup`` first and the
  stop callback later is the safe order; the reverse races.
- A comment, import, or string mentioning the helper does not stop the worker.
- ``stop_local_network_workers`` is a no-op when nothing was started, so
  calling it on a harness that skipped workers is still the required contract.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# Harnesses named by issue #212, plus the #191 reference that already stopped.
IN_SCOPE_HARNESS_FILES = (
    "tests/test_mcp_server.py",
    "tests/test_media_keys.py",
    "tests/test_streamdeck_content.py",
    "tests/test_unit.py",
    "ports-client/tests/test_http_client_integration.py",
    "tests/adversarial/test_issue_152_media_keys_http_malformed_input.py",
    "tests/adversarial/test_issue_152_media_keys_hold_uat.py",
    "tests/adversarial/test_issue_163_clone_auth_http_uat.py",
    "tests/adversarial/test_issue_165_held_identity_vpn_startup.py",
    "tests/test_integration_mock_server.py",
)

UNIT_LISTENER_CLASSES = {
    "CreateServerHttpRedirectTests",
    "CreateServerCastListenerTests",
}

LAN_STOP = "stop_local_network_workers"
CREATE_SERVER = "create_server"
REMOVE_ATTRS = {"cleanup", "rmtree"}


def _call_attr(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _walks_call(node: ast.AST, name: str) -> bool:
    return any(_call_attr(child) == name for child in ast.walk(node))


def _is_remove_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_attr(node) in REMOVE_ATTRS


def _is_lan_stop_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_attr(node) == LAN_STOP


def _nested_functions(fn: ast.FunctionDef) -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for child in ast.walk(fn):
        if isinstance(child, ast.FunctionDef) and child is not fn:
            found[child.name] = child
    return found


def _function_lan_stop_before_remove(fn: ast.FunctionDef) -> tuple[bool, bool, bool]:
    """Return (has_stop, has_remove, stop_happens_before_remove_in_this_fn)."""
    saw_stop = False
    has_stop = False
    has_remove = False
    stop_before = False
    for stmt in fn.body:
        stmt_stop = any(_is_lan_stop_call(child) for child in ast.walk(stmt))
        stmt_remove = any(_is_remove_call(child) for child in ast.walk(stmt))
        if stmt_stop:
            has_stop = True
            saw_stop = True
        if stmt_remove:
            has_remove = True
            if saw_stop:
                stop_before = True
    if has_stop and not has_remove:
        stop_before = True
    return has_stop, has_remove, stop_before


def _addcleanup_kind(call: ast.Call, nested: dict[str, ast.FunctionDef], helpers: dict[str, ast.ClassDef]) -> str:
    if not call.args:
        return "other"
    arg = call.args[0]
    if isinstance(arg, ast.Name):
        if arg.id == LAN_STOP:
            return "lan_stop"
        nested_fn = nested.get(arg.id)
        if nested_fn is not None:
            has_stop, has_remove, stop_before = _function_lan_stop_before_remove(nested_fn)
            if has_stop and (not has_remove or stop_before):
                return "lan_stop"
            if has_remove:
                return "remove"
        for helper in helpers.values():
            for method in helper.body:
                if isinstance(method, ast.FunctionDef) and method.name == arg.id:
                    if _walks_call(method, LAN_STOP):
                        return "lan_stop"
                    if any(_is_remove_call(child) for child in ast.walk(method)):
                        return "remove"
    if isinstance(arg, ast.Attribute):
        if arg.attr == LAN_STOP:
            return "lan_stop"
        if arg.attr in REMOVE_ATTRS:
            return "remove"
        for helper in helpers.values():
            for method in helper.body:
                if isinstance(method, ast.FunctionDef) and method.name == arg.attr:
                    if _walks_call(method, LAN_STOP):
                        return "lan_stop"
    return "other"


def _collect_addcleanups(fn: ast.FunctionDef, helpers: dict[str, ast.ClassDef]) -> list[str]:
    nested = _nested_functions(fn)
    kinds: list[str] = []
    for child in ast.walk(fn):
        if isinstance(child, ast.Call) and _call_attr(child) == "addCleanup":
            kinds.append(_addcleanup_kind(child, nested, helpers))
    return kinds


def _helper_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    helpers: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        if _walks_call(node, CREATE_SERVER):
            helpers[node.name] = node
    return helpers


def _class_uses_create_server(cls: ast.ClassDef, helpers: dict[str, ast.ClassDef]) -> bool:
    if _walks_call(cls, CREATE_SERVER):
        return True
    for child in ast.walk(cls):
        if isinstance(child, ast.Call) and _call_attr(child) in helpers:
            return True
    return False


def _teardown_order(fn: ast.FunctionDef) -> tuple[int | None, int | None]:
    stop_line = None
    remove_line = None
    for stmt in fn.body:
        if stop_line is None and any(_is_lan_stop_call(child) for child in ast.walk(stmt)):
            stop_line = stmt.lineno
        if remove_line is None and any(_is_remove_call(child) for child in ast.walk(stmt)):
            remove_line = stmt.lineno
    return stop_line, remove_line


def harness_failures(source: str, filename: str = "<harness>", only_classes: set[str] | None = None) -> list[str]:
    """Return human-readable contract breaches for create_server test classes."""
    tree = ast.parse(source, filename=filename)
    helpers = _helper_classes(tree)
    failures: list[str] = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if only_classes is not None and cls.name not in only_classes:
            continue
        methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
        uses_server = _class_uses_create_server(cls, helpers)
        if not uses_server:
            continue
        is_testcase = "TestCase" in cls.name or any(
            (isinstance(base, ast.Name) and "Test" in base.id)
            or (isinstance(base, ast.Attribute) and "Test" in base.attr)
            for base in cls.bases
        )
        if not is_testcase and cls.name not in (only_classes or set()):
            # Helper that owns create_server: its close/stop must join workers
            # if it also shuts the HTTP server, because tests rmtree after close.
            closer = methods.get("close") or methods.get("stop")
            if closer is not None and _walks_call(closer, "shutdown") and not _walks_call(closer, LAN_STOP):
                failures.append(
                    f"{filename}:{cls.name}.{closer.name} shuts the HTTP server after "
                    f"create_server without {LAN_STOP}"
                )
            continue

        setup = methods.get("setUp")
        teardown = methods.get("tearDown")
        add_kinds: list[str] = []
        if setup is not None:
            add_kinds.extend(_collect_addcleanups(setup, helpers))
        for method in methods.values():
            if method.name in {"setUp", "tearDown"}:
                continue
            add_kinds.extend(_collect_addcleanups(method, helpers))
        for helper in helpers.values():
            if helper is cls:
                continue
            for method in helper.body:
                if isinstance(method, ast.FunctionDef):
                    add_kinds.extend(_collect_addcleanups(method, helpers))

        td_stop, td_remove = (None, None)
        if teardown is not None:
            td_stop, td_remove = _teardown_order(teardown)

        remove_in_add = any(kind == "remove" for kind in add_kinds)
        stop_in_add = any(kind == "lan_stop" for kind in add_kinds)

        if td_remove is not None:
            if td_stop is None or td_stop > td_remove:
                failures.append(
                    f"{filename}:{cls.name}.tearDown removes temp userdata "
                    f"without a prior {LAN_STOP} (addCleanup stop runs after tearDown)"
                )
        elif remove_in_add:
            if td_stop is not None:
                continue
            if not stop_in_add:
                failures.append(
                    f"{filename}:{cls.name} registers temp userdata removal via "
                    f"addCleanup without {LAN_STOP} on that lifecycle"
                )
            else:
                first_remove = add_kinds.index("remove")
                last_stop = max(i for i, kind in enumerate(add_kinds) if kind == "lan_stop")
                if last_stop < first_remove:
                    failures.append(
                        f"{filename}:{cls.name} addCleanup LIFO is inverted: "
                        f"{LAN_STOP} is registered before tmp.cleanup, so cleanup runs first"
                    )
        elif td_stop is None and not stop_in_add:
            failures.append(
                f"{filename}:{cls.name} calls create_server and owns a temp tree "
                f"but never calls {LAN_STOP}"
            )
    return failures


SAFE_TEARDOWN = '''
import unittest, tempfile
from app.transfer.peer_workers import stop_local_network_workers
from app.drone_api import create_server
class SafeTearDown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = create_server(None)
    def tearDown(self):
        stop_local_network_workers()
        self._tmp.cleanup()
'''

UNSAFE_TEARDOWN_AFTER = '''
import unittest, tempfile
from app.transfer.peer_workers import stop_local_network_workers
from app.drone_api import create_server
class UnsafeAfter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = create_server(None)
    def tearDown(self):
        self._tmp.cleanup()
        stop_local_network_workers()
'''

UNSAFE_STOP_IN_ADD_CLEANUP = '''
import unittest, tempfile
from app.transfer.peer_workers import stop_local_network_workers
from app.drone_api import create_server
class StopTooLate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(stop_local_network_workers)
        self.server = create_server(None)
    def tearDown(self):
        self._tmp.cleanup()
'''

SAFE_LIFO = '''
import unittest, tempfile
from app.drone_api import create_server
from app.transfer.peer_workers import stop_local_network_workers
class SafeLifo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = create_server(None)
        def stop():
            self.server.shutdown()
            stop_local_network_workers()
        self.addCleanup(stop)
'''

UNSAFE_LIFO = '''
import unittest, tempfile
from app.drone_api import create_server
from app.transfer.peer_workers import stop_local_network_workers
class UnsafeLifo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        def stop():
            stop_local_network_workers()
        self.addCleanup(stop)
        self.server = create_server(None)
        self.addCleanup(self._tmp.cleanup)
'''

UNSAFE_HTTP_ONLY_STOP = '''
import unittest, tempfile
from app.drone_api import create_server
class HttpOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = create_server(None)
        def stop():
            self.server.shutdown()
            self.server.server_close()
        self.addCleanup(stop)
'''

COMMENT_ONLY = '''
import unittest, tempfile
from app.drone_api import create_server
class CommentOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = create_server(None)
    def tearDown(self):
        # stop_local_network_workers()
        self._tmp.cleanup()
'''

HELPER_CLOSE_WITHOUT_STOP = '''
import unittest, tempfile, shutil
from app.drone_api import create_server
class HttpDrone:
    def __init__(self, case, settings):
        self.server = create_server(settings)
    def close(self):
        self.server.shutdown()
        self.server.server_close()
class CloneCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(shutil.rmtree, "/tmp/x")
    def test_one(self):
        drone = HttpDrone(self, None)
        drone.close()
'''


class UnittestLifecycleBoundaryTests(unittest.TestCase):
    def test_tearDown_stop_then_cleanup_is_safe(self) -> None:
        self.assertEqual(harness_failures(SAFE_TEARDOWN, only_classes={"SafeTearDown"}), [])

    def test_tearDown_cleanup_then_stop_is_too_late(self) -> None:
        failures = harness_failures(UNSAFE_TEARDOWN_AFTER, only_classes={"UnsafeAfter"})
        self.assertTrue(failures)
        self.assertTrue(any("without a prior" in item for item in failures))

    def test_addCleanup_stop_cannot_rescue_tearDown_cleanup(self) -> None:
        """tearDown runs before addCleanup; this is the inverted-phase race."""
        failures = harness_failures(UNSAFE_STOP_IN_ADD_CLEANUP, only_classes={"StopTooLate"})
        self.assertTrue(failures)

    def test_addCleanup_lifo_stop_registered_after_cleanup_is_safe(self) -> None:
        self.assertEqual(harness_failures(SAFE_LIFO, only_classes={"SafeLifo"}), [])

    def test_addCleanup_lifo_stop_registered_before_cleanup_is_unsafe(self) -> None:
        failures = harness_failures(UNSAFE_LIFO, only_classes={"UnsafeLifo"})
        self.assertTrue(failures)
        self.assertTrue(any("LIFO" in item for item in failures))

    def test_http_shutdown_without_lan_stop_is_unsafe(self) -> None:
        failures = harness_failures(UNSAFE_HTTP_ONLY_STOP, only_classes={"HttpOnly"})
        self.assertTrue(failures)

    def test_comment_mentioning_stop_does_not_satisfy_the_contract(self) -> None:
        failures = harness_failures(COMMENT_ONLY, only_classes={"CommentOnly"})
        self.assertTrue(failures)

    def test_helper_close_that_only_shuts_http_is_unsafe(self) -> None:
        failures = harness_failures(HELPER_CLOSE_WITHOUT_STOP)
        self.assertTrue(any("HttpDrone.close" in item for item in failures))


class ListedHarnessSourceTests(unittest.TestCase):
    def test_listed_harness_files_exist(self) -> None:
        missing = [rel for rel in IN_SCOPE_HARNESS_FILES if not (REPO_ROOT / rel).is_file()]
        self.assertEqual(missing, [])

    def test_named_create_server_harnesses_stop_before_temp_removal(self) -> None:
        self.maxDiff = None
        failures: list[str] = []
        for rel in IN_SCOPE_HARNESS_FILES:
            path = REPO_ROOT / rel
            only = UNIT_LISTENER_CLASSES if rel.endswith("tests/test_unit.py") else None
            failures.extend(harness_failures(path.read_text(encoding="utf-8"), filename=rel, only_classes=only))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_scanner_ignores_mock_patcher_stop_as_lan_stop(self) -> None:
        source = '''
import unittest, tempfile
from unittest import mock
from app.drone_api import create_server
class PatcherStop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch("x")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = create_server(None)
'''
        failures = harness_failures(source, only_classes={"PatcherStop"})
        self.assertTrue(failures)
