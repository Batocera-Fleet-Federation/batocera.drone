"""Issue #249: named adversarial create_server harnesses must stop LAN workers.

Expected behavior from the issue, not from a delivery patch:

- Combined unittest discovery failed because four protected harnesses
  (issues #152, #163, #165) called ``create_server`` and then removed a
  temporary userdata tree without ``stop_local_network_workers``.
- HTTP ``shutdown`` / ``server_close`` is not that join (#212). The missing
  call is harness teardown, not pairing trust (#213).
- Those files' original assertions stay as they are. The required change is
  additive: stop and join discovery before ``TemporaryDirectory.cleanup`` /
  ``shutil.rmtree``.
- ``stop_local_network_workers`` is a no-op when workers were never started,
  so a held-identity boot that skipped discovery still calls it.
- A comment or import is not a stop. The #212 scanner is the contract.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

# The four files the #213 finding named. Issue #249 is the worker-lifecycle
# follow-up; their media-keys / clone-auth / held-VPN assertions are unchanged.
NAMED_HARNESS_FILES = (
    "tests/adversarial/test_issue_152_media_keys_http_malformed_input.py",
    "tests/adversarial/test_issue_152_media_keys_hold_uat.py",
    "tests/adversarial/test_issue_163_clone_auth_http_uat.py",
    "tests/adversarial/test_issue_165_held_identity_vpn_startup.py",
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location(
        "_issue_249_harness_scanner",
        Path(__file__).resolve().with_name("test_issue_212_harness_teardown_boundaries.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_scanner = _load_scanner()


class NamedHarnessTeardownTests(unittest.TestCase):
    def test_named_harness_files_exist(self) -> None:
        missing = [rel for rel in NAMED_HARNESS_FILES if not (REPO_ROOT / rel).is_file()]
        self.assertEqual(missing, [])

    def test_named_create_server_harnesses_stop_before_temp_removal(self) -> None:
        self.maxDiff = None
        failures: list[str] = []
        for rel in NAMED_HARNESS_FILES:
            source = (REPO_ROOT / rel).read_text(encoding="utf-8")
            failures.extend(_scanner.harness_failures(source, filename=rel))
        self.assertEqual(failures, [], "\n".join(failures))

    def test_http_only_close_without_lan_stop_still_fails_the_scanner(self) -> None:
        """Issue #249 does not relax the #212 rule that HTTP close is not a join."""
        source = '''
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
        failures = _scanner.harness_failures(source, only_classes={"HttpOnly"})
        self.assertTrue(failures)

    def test_comment_is_not_a_stop(self) -> None:
        source = '''
import unittest, tempfile
from app.drone_api import create_server
class CommentOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = create_server(None)
    def tearDown(self):
        # stop_local_network_workers()
        pass
'''
        failures = _scanner.harness_failures(source, only_classes={"CommentOnly"})
        self.assertTrue(failures)

    def test_helper_close_must_stop_workers_not_only_http(self) -> None:
        source = '''
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
        failures = _scanner.harness_failures(source)
        self.assertTrue(any("HttpDrone.close" in item for item in failures))
