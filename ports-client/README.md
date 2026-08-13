# Batocera Drone -- Ports client

A native, controller-first app installed into Batocera's **Ports** menu that
talks to the Drone running on the same machine. Scoped to swarm-related
activities only: show the swarm, connect to it (Tailnet/Local Network),
reference a peer's ROM/BIOS library, download ROMs and Movies from a peer,
VPN, and Backups. See the architecture plan this was built from for the
full rationale.

**The browser UI is untouched.** This is a second, additive way to reach
the same backend -- nothing here changes `app/web/`.

**Scope reduction (2026-08-13):** Local Assets browsing (Systems/Movies/Music
of *this* device's own inventory) and the Admin Debug (System Info + Logs)
and Automation (idle volume / idle game exit / wifi recovery) tiles were
removed, code and tests, per an explicit decision to keep this app narrowly
focused on swarm activities -- browsing/managing this device's own local
state is left to the browser UI. `ui/shell.py`'s top nav is now a flat
Swarm/VPN/Backups (no "Admin" wrapper -- with Debug and Automation gone, an
Admin grouping around just VPN+Backups was pure overhead).

**Status:** every top-level section is built and tested. Swarm (Overview:
read-only federation/peer list; Tailnet: join an existing swarm by pasting
an auth key; Local Network: this drone's own pairing code + discover/pair
with a nearby drone; Reference ROMs: mount a paired peer's ROM/BIOS library
over the network instead of copying it, with Reference/Unreference per
peer; Request Assets: pick a paired peer, browse their Systems+ROMs or
Movies with a search box, and pull an individual item), VPN (status +
Connect/Disconnect), and Backups (list + Create). Deliberately **not**
built anywhere a gamepad-only console UI has no good answer: VPN config
upload/credentials/sharing (needs a file picker + typed secrets), Backups
download/delete/apply (nowhere on the console to save a downloaded file
to, and destructive actions are safer left to the browser's confirmation
flow), Tailnet/Local Network peer *management* -- rotate-auth-key, sharing
toggles, pull-from-peer, forget/dismiss -- and Request Assets' Music
(peer inventory has no music type at all) or bulk "sync everything" (see
`client/endpoints.py`'s module comments for the full list and reasoning).

## Code separation

This app never imports Drone's Python internals. It only ever talks to
Drone over HTTP, exactly like the browser does (`client/http_client.py`).
That's a deliberate boundary: no shared runtime state, no coupling to
Drone's threading/SQLite internals, independent of the web UI's release
cadence.

## No login screen

Unlike the browser UI, it does **not** make you log in. It always talks to
Drone over loopback (127.0.0.1) by design -- it runs on the same device --
and Drone's session gate (`app/common/auth.py`'s
`SessionAuth.authenticate_request`) now treats *any* loopback caller as
pre-authenticated, cookie or not: physical access to the device is already
this codebase's root of trust (Drone runs as root), so making someone type
a password to talk to the box they're already sitting at added friction
with no real security benefit. `ui/screens/login.py` still carries a
manual username/password form, but it's a rare fallback -- it only shows
up if `DRONE_PORTS_CLIENT_HOST` is pointed at a non-loopback host, or
Drone doesn't have this exemption yet.

- `client/` -- stdlib-only (`urllib`, `http.cookiejar`) HTTP client + a
  couple of thin per-endpoint wrappers (`client/endpoints.py`).
- `ui/` -- the native window and screens (imgui_bundle + PySDL2).
- `main.py` -- entry point; what the Ports launcher execs.

## Running it

The easiest way, from the repo root -- sets up a local venv with the real
runtime deps and launches the app windowed, against a local mock Drone:

```bash
python3 scripts/run_mock_server.py &   # a full Drone, HTTP-only on :8080, fake data, no root
scripts/run_client_now.sh              # packages (venv + deps) and runs this app locally
```

Or by hand:

```bash
cd ports-client
python3 -m pytest tests/                              # stdlib-only tests, no extra deps
python3 -m pip install imgui_bundle PySDL2 PyOpenGL numpy pysdl2-dll  # dev deps (see note below)
PORTS_CLIENT_DEV_WINDOWED=1 python3 main.py            # windowed instead of fullscreen, for dev
```

`pysdl2-dll` is a dev-only convenience package (bundled SDL2 binaries) for
machines without a system SDL2 -- **never vendor it on-device**, where the
Batocera image already has `libSDL2.so` (the same one EmulationStation
itself links against).

## Architecture note: why this isn't using HelloImGui's high-level runner

An earlier draft used imgui_bundle's high-level `hello_imgui`/`immapp.run()`
API with `PlatformBackendType.sdl`. That turned out not to work: inspecting
the actual compiled `_imgui_bundle*.so` in the published PyPI wheel (via
`strings`, looking for `SDL_*` vs `glfw*` symbols) showed it's built with
**GLFW only** -- on every platform tested, not just macOS. GLFW's Linux
backend needs X11/Wayland, which Batocera doesn't run (EmulationStation
itself only works here via SDL2/KMS-DRM), so that path was never going to
reach real hardware either.

The fix: `ui/app.py` does its own windowing via **PySDL2** (ctypes bindings
to the system's own `libSDL2.so` -- nothing to vendor for that part) and
uses imgui_bundle only for the ImGui core plus its bundled
`python_backends.sdl2_backend.SDL2Renderer` (a first-party-maintained
reference integration, not hand-rolled here) for input translation and
draw-call issuing. This is the same shape the original plan called for
("Python + PySDL2, with imgui_bundle vendored for widgets/gamepad nav") --
this correction just fixes *how* the two are wired together.

## Vendoring (on-device has no pip)

```bash
scripts/vendor_deps.sh 312 x86_64  manylinux_2_28_x86_64
scripts/vendor_deps.sh 312 aarch64 manylinux_2_28_aarch64
scripts/build_release_bundle.sh x86_64
scripts/build_release_bundle.sh aarch64
```

**The Python tag (`312`) must match the real device's `python3`.** A CI
build initially targeted `311`, which produced a compiled
`_imgui_bundle*.so` that a real Python-3.12.8 Batocera unit's import
machinery silently refused to load (`ModuleNotFoundError: No module named
'imgui_bundle._imgui_bundle'` -- a mismatched ABI tag in the compiled
extension's filename means CPython's finder doesn't recognize it as the
module at all, not a normal ImportError). Found and fixed 2026-08-13 via
live debugging against a real device (see `drone-live-debugging` skill).
If a future Batocera release ships a different Python version, this needs
re-verifying against real hardware -- `python3 -c "import sys;
print(sys.implementation.cache_tag)"` on the device is the source of
truth, not an assumption carried over from the main app's own
`vendor_deps.sh` precedent (which is how `311` ended up here originally).

See `requirements-vendor.txt` for exactly what's vendored and why --
notably `numpy`, despite being listed as an optional "extra" in
imgui_bundle's own package metadata, is a **verified hard runtime
requirement** (its texture-upload path crashes without it; confirmed by
actually running the render loop, not just reading the metadata).

`vendor_deps.sh` produces `vendor/common/` (PySDL2 + PyOpenGL, arch-independent)
and `vendor/<arch>/` (imgui_bundle + numpy, compiled per-arch+per-Python-version)
-- neither is checked in (see `.gitignore`). `build_release_bundle.sh`
assembles both into the on-device layout `launcher/batocera-drone-client.sh`
expects, as a tarball under `dist/`.

## Packaging & deploy (Phase 5)

**Wired up, 2026-08-13:** `.github/workflows/release.yml`'s release job now
builds both arch bundles on the same `ubuntu-latest` runner as everything
else -- both are pure wheel *downloads* (pip's
`--platform`/`--python-version`/`--implementation`/`--abi` flags fetch
prebuilt manylinux wheels without compiling or executing anything), so no
QEMU/cross-compilation is needed, unlike the multi-arch Docker image build
in the same workflow. Both tarballs are attached to the GitHub Release
alongside the existing web-app assets.

`scripts/batocera_install.sh`'s `install_ports_client()` now downloads the
matching release asset (`batocera-drone-client-<arch>.tar.gz`) when no
local bundle is found, mirroring `install_tailscale_mesh()`'s own
download-with-a-clear-retry-message pattern -- a local pre-built bundle
next to the installer, or a `DRONE_PORTS_CLIENT_BUNDLE` override, still
wins over the download (useful for local testing without network access).
Verified locally end-to-end: built both bundles for real
(`vendor_deps.sh` + `build_release_bundle.sh` for both x86_64 and
aarch64), confirmed the tarball's internal layout matches what the
launcher expects, and exercised `install_ports_client()`'s own logic
against both the local-bundle path (extracts correctly) and the
download-fallback path (a real 404 against the not-yet-existing release
asset, confirming the URL construction and the clean-failure path both
work as designed -- extraction and download-fallback verified against a
real bundle/real network call, not just read from source).

**Deliberately not done yet:** the Ports client isn't wired into the
Drone's own self-update poller (`app/common/self_update.py`) the way the
web app is -- it only gets installed/refreshed when `batocera_install.sh`
itself is (re-)run, not automatically on every push to `main`. Folding it
into the live self-update daemon means that background process would
start writing into `/userdata/roms/ports/` (a directory EmulationStation
actively scans, and where this app might currently be the running
foreground process on the very TV the Drone is attached to) -- a
meaningfully different risk class from replacing the Drone's own `app/`
files in place, and a decision that deserves its own discussion rather
than folding in silently alongside this packaging work.

## Verification status

Be precise about what's actually been exercised vs. still assumed, since a
GUI app's correctness isn't provable from code review alone:

**Verified for real, this session:**
- The full HTTP/session-auth client, against both a hand-written fake
  server and the **real** Drone server (`create_server()` from
  `app.drone_api`, same harness `tests/test_integration_mock_server.py`
  uses) -- login, session persistence across relaunches, 401 handling,
  unreachable-server handling. See `tests/test_http_client*.py`.
- All screen orchestration logic (session resume, error handling, nav-state
  transitions) via `unittest`, no live ImGui frame needed. See
  `tests/test_screens.py`.
- The full render loop -- PySDL2 window/GL-context creation, imgui_bundle's
  SDL2Renderer, the custom theme, and **every** AppShell section (flat
  Swarm/VPN/Backups) -- actually running for real frames on a dev machine
  (Python 3.11 + imgui_bundle 1.92.801, GL 3.3 core), not just statically
  reviewed. Re-verified after the 2026-08-13 scope reduction: booted the
  real app against the real mock server, confirmed the shell starts on
  `swarm` with exactly `{"swarm", "vpn", "backups"}` as its content keys (no
  stray `admin_tab`/`assets` remnants), and switched through all three
  sections across real rendered frames with no crash.
- Clean process exit on `SIGTERM` (91ms in testing), matching how
  Batocera's exit-hotkey daemon will actually stop this process.
- Swarm's actual response-shape assumptions (`/admin/swarm/overview`)
  against the real server, not just read from handler source.
- VPN/Backups' response shapes and action results
  (`GET /admin/vpn`, `POST /admin/vpn/{connect,disconnect}`,
  `GET`/`POST /admin/config-backups`) against the real server -- including
  a real `{"status": "error",
  "errors": [...]}` response from a failed VPN connect. That specific
  shape (plural `errors` array, not singular `error`) is a documented
  real incident in `drone-vpn-management`'s skill: `drone.js` originally
  only read `error` and silently discarded the actual reason on this
  exact endpoint. `client/http_client.py`'s `_extract_error_message` now
  handles both, with a unit test (`test_errors_array_response_joins_into_message`)
  plus the real-server one above, so this client doesn't repeat that bug.
- The VPN connect/disconnect actions and Backups' Create action, driven
  through real rendered frames (not just the orchestration-logic unit
  tests), including state actually changing between frames (VPN status
  flips connected/disconnected, a new backup appears in the list).
- **A real Retina/HiDPI rendering bug, found and fixed after a user
  reported the UI "looked weird" on a Mac.** The vendored
  `imgui_bundle.python_backends.sdl2_backend.SDL2Renderer.process_inputs()`
  hardcodes `io.display_framebuffer_scale = (1, 1)` every frame -- correct
  only on a 1x-scale display. On a 2x Retina display the GL drawable is
  twice the window's logical size, so the viewport computed from that
  wrong scale covered only a quarter of the real framebuffer (confirmed:
  `SDL_GetWindowSize` reported 1280x720 while `SDL_GL_GetDrawableSize`
  reported 2560x1440 on the machine this was found on). `ui/app.py` now
  overrides `display_framebuffer_scale` every frame right after
  `process_inputs()`, computed from the real window-vs-drawable-size ratio
  via `SDL_GL_GetDrawableSize`. Verified fixed by driving a real frame and
  reading back `imgui.get_io().display_framebuffer_scale` (was `(1, 1)`,
  now correctly `(2, 2)`). This is almost certainly invisible on real
  Batocera hardware -- TV/monitor outputs there are effectively always
  1x scale -- but it's a real bug regardless of platform, not a
  macOS-only cosmetic quirk, and would reproduce on any HiDPI display.
- **A second real rendering bug, found after a user asked whether a
  "movable window inside the main window" during testing was normal --
  it wasn't.** No screen ever called `imgui.begin()`; every screen just
  called widget functions (`imgui.text`/`button`/...) directly. Left
  alone, Dear ImGui silently redirects calls made outside any explicit
  window into its built-in fallback `"Debug##Default"` window -- an
  ordinary movable, resizable, titled window floating inside the SDL2
  surface, not a full-screen app. This was masked in an earlier draft by
  HelloImGui's `DefaultImGuiWindowType.provide_full_screen_window`
  (see the "why this isn't using HelloImGui" note above) and never
  replaced when `ui/app.py` moved off that runner. Fixed by wrapping each
  frame in one chrome-less `imgui.begin("##root", flags=...)` pinned to
  `imgui.get_main_viewport()`'s pos/size (`no_title_bar`, `no_resize`,
  `no_move`, `no_collapse`, ...). Verified by capturing
  `imgui.get_window_pos()`/`get_window_size()` from inside a real screen's
  `draw()` call: was an arbitrary default before the fix, now exactly
  `(0, 0)` / the full window size on every frame.
- **The loopback pre-auth exemption end-to-end**, live: with the real mock
  server running and zero cookies anywhere, `GET /v1/api/auth/session` from
  127.0.0.1 returns `{"authenticated": true, "username": "admin"}` and a
  protected endpoint (`/v1/api/systems`) returns real data -- confirmed via
  raw `curl` from loopback, then again by booting the real
  `PortsClientApp` against that same server and confirming it lands on
  `AppShell` (not `LoginScreen`) before a single frame renders (`Navigator.go_to`
  calls `on_enter()` synchronously, so the whole check-and-skip happens
  inside `PortsClientApp.__init__`).
- Swarm's Tailnet and Local Network response shapes and actions
  (`GET/POST /admin/tailnet/{status,enroll}`,
  `GET/POST /admin/local-network/{status,discover,pairing-code/rotate}`,
  `POST /admin/local-network/peers/{id}/pair`) against the real server --
  including the real "auth key is required" error for an empty-key enroll
  attempt, an 8-digit pairing code round-trip, code rotation actually
  changing the code, and a clean 404 ("discovered peer not found") for
  pairing with an unknown peer id, all via `test_http_client_integration.py`.
  Real frames driven through the enroll form and the discover/pair flow too.
- Reference ROMs (`GET /admin/network-shares`,
  `POST /admin/network-shares/{id}/{enable,disable}`) and Request Assets
  (`GET /admin/local-network/peers/{id}/assets?type={summary,roms,movies}`,
  `POST /admin/local-network/sync`) against the real server, including the
  real error text for every "not a paired peer" / "paired peer not found"
  case (no real second paired drone was available to test the success
  path against, so those specific error branches are what's actually
  verified end-to-end -- the success-path payload shapes came from reading
  `network_share_manager.py`/`handlers_peer.py`/`handlers_network.py`
  directly, not from a live two-drone pairing). Real frames driven through
  the full Request Assets flow: peer picker -> Systems -> select system ->
  ROM list -> Request -> Movies -> Request -> back to peer list.

**Not verified -- needs real Batocera hardware or a Linux dev box, per the
plan's Phase 1:**
- KMS/DRM rendering specifically (dev verification above used a normal
  windowed/GLX-ish desktop GL context on macOS, not the DRM path).
- Whether the GPU driver on ARM Batocera devices exposes desktop **OpenGL
  3.3 core** (what imgui_bundle's bundled Python GL3 renderer requires,
  GLSL `#version 330`) rather than GLES-only -- a real risk for some of the
  fleet's ARM boards/handhelds, unverified either way.
- Actual gamepad button-to-nav feel (event wiring is in place and modeled
  on imgui_bundle's own reference example, but no physical controller was
  available to test with here).
- The theme's actual visual appearance against a real display (colors were
  taken directly from `app/web/static/css/drone.css`'s `:root` values, not
  eyeballed against a running Drone UI side-by-side).
