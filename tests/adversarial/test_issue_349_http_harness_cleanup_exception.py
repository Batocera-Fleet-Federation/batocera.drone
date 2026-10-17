"""Issue #349: HTTP teardown must retire LAN workers even when shutdown fails.

The protected #152 harnesses own callbacks that shut down their HTTP server and
then retire process-wide LAN workers. Execute those callbacks with deterministic
fakes so a shutdown exception cannot hide a leaked worker generation.
"""

import ast
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
HARNESSES = (
    ROOT / "tests" / "adversarial" / "test_issue_152_media_keys_http_malformed_input.py",
    ROOT / "tests" / "adversarial" / "test_issue_152_media_keys_hold_uat.py",
)


class HttpHarnessCleanupExceptionTests(unittest.TestCase):
    def _assert_shutdown_exception_still_stops_lan_workers(self, harness):
        source = harness.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(harness))
        callback = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "stop"
        )
        module = ast.Module(body=[callback], type_ignores=[])
        ast.fix_missing_locations(module)

        shutdown_error = RuntimeError("HTTP shutdown failed")
        server = Mock()
        server.shutdown.side_effect = shutdown_error
        server.server_close = Mock()
        thread = Mock()
        namespace = {"self": Mock(server=server), "thread": thread,
                     "stop_local_network_workers": Mock()}
        exec(compile(module, str(harness), "exec"), namespace)

        with self.assertRaisesRegex(RuntimeError, "HTTP shutdown failed"):
            namespace["stop"]()
        namespace["stop_local_network_workers"].assert_called_once_with()


    def test_malformed_input_harness_cleanup(self):
        self._assert_shutdown_exception_still_stops_lan_workers(HARNESSES[0])

    def test_hold_uat_harness_cleanup(self):
        self._assert_shutdown_exception_still_stops_lan_workers(HARNESSES[1])


if __name__ == "__main__":
    unittest.main()
