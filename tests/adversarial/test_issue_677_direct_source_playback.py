"""Issue #677: exercise the remote movie Play action in the browser script."""
import subprocess
import unittest
from pathlib import Path


class DirectSourcePlaybackUAT(unittest.TestCase):
    def test_remote_movie_player_does_not_request_media_from_browsing_drone(self):
        # "When streaming an asset like a movie from batocera UI, and that movie
        # is a remote asset, it should always stream directly from the source
        # rather than proxying through the drone being used." Confidence: high.
        script = Path(__file__).resolve().parents[2] / "app/web/static/js/media_browse.js"
        harness = r'''
const fs = require("fs"), vm = require("vm"), assert = require("assert");
let source = fs.readFileSync(process.argv[1], "utf8");
// Expose the existing action handler while leaving its implementation intact.
source = source.replace('window.DroneRemoteMedia = { onRouteRendered,',
                        'window.__testPlay = play; window.DroneRemoteMedia = { onRouteRendered,');
const host = { innerHTML: "", firstElementChild: { addEventListener() {} }, scrollIntoView() {} };
const context = {
  window: {}, document: { readyState: "loading", addEventListener() {} },
  sessionStorage: { getItem() { return null; } },
  escapeHtml: s => s, showToast() {}, IntersectionObserver: class { observe() {} }, URL, console
};
vm.createContext(context);
vm.runInContext(source, context);
assert.strictEqual(typeof context.window.__testPlay, "function", "could not find Play action");
(async () => {
  await context.window.__testPlay("movies", {
    drone_id: "source-drone", entry_key: "a123", is_local: false,
    drone_name: "Source", address: "http://source.example:8080"
  }, { querySelector() { return host; } });
  const match = host.innerHTML.match(/<video\b[^>]*\bsrc=["']([^"']+)["']/i);
  assert(match, "remote movie Play must create a video player");
  const url = new URL(match[1], "http://browsing-drone.example:8080/");
  assert.notStrictEqual(url.origin, "http://browsing-drone.example:8080",
    `remote movie player requests the browsing drone: ${url.href}`);
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", harness, str(script)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
