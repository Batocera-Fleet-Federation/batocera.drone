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

**Status:** every top-level section is built and tested. About (the landing
tab -- what this app is, why it exists as a Ports-menu companion, and the
HTTPS URL for the full web dashboard, sourced live from this drone's own
`swarm/overview` self-entry rather than hardcoded), Swarm (Overview:
read-only federation/peer list; Tailnet: join an existing swarm by pasting
an auth key; Local Network: this drone's own pairing code + discover/pair
with a nearby drone; Reference ROMs: mount a paired peer's ROM/BIOS library
over the network instead of copying it, with Reference/Unreference per
peer; Request Assets: pick a paired peer, browse their Systems+ROMs or
Movies with a search box, and pull an individual item), VPN (status,
Connect/Disconnect, credentials, sharing, and getting a config onto the
device -- see "Full controller navigation" below), and Backups (list,
Create, and Apply/restore -- see "Backup restore" below). Deliberately
**not** built anywhere a gamepad-only console UI still has no good answer:
VPN config upload via a file-picker dialog (the two Get Config flows below
cover this without one), Backups download/delete/email (nowhere on the
console to save a downloaded file to, and delete/email are lower-value
from a controller), Tailnet/Local Network peer *management* --
rotate-auth-key, sharing toggles, pull-from-peer, forget/dismiss -- and
Request Assets' Music (peer inventory has no music type at all) or bulk
"sync everything" (see `client/endpoints.py`'s module comments for the
full list and reasoning).

## Backup restore + About landing page + visual theme pass (2026-08-13)

**Backups: Apply (restore) added.** `ui/screens/backups.py`'s original
"apply is destructive, better left to the browser" reasoning was revisited
once the virtual keyboard made a real gamepad-navigable confirmation
possible -- each complete backup now gets an "Apply this Backup" button
that opens a `begin_popup_modal` confirmation mirroring the browser's own
ack-checkbox-gated warning (overlay semantics, EmulationStation restart,
cannot be undone) almost word-for-word, B-button-cancelable like the
virtual keyboard's own popup. The actual `POST /admin/config-backups/{id}/apply`
call can legitimately take much longer than the client's normal 15s
default (it stops EmulationStation, copies files, restarts it), so
`DroneApiClient.post()`/`_send()` gained an optional per-call `timeout`
override (180s for this one call) rather than risk the exact "could not
reach Drone... read operation timed out" failure mode described below --
the operation would have kept running server-side and succeeded, just
past the client's patience. Applying is still synchronous (no threading
anywhere in ports-client), so the confirm click defers the call to the
**next** frame, not "later in this same `draw()` call" -- see "Loading
spinner + a same-frame deferral bug" below, which corrects an initial,
subtly wrong version of this that shipped earlier the same day.

**About: a new landing tab.** `ui/screens/about.py`, now the shell's
default section, mirrors drone.js's own landing page (`renderHelpPage`,
the `#home`/`#help` route) in tone: what this app is, why it exists
specifically as a gamepad-navigable Ports-menu companion rather than "the
same thing as the browser," and the HTTPS URL for the full dashboard for
everything this console app doesn't cover (library browsing, Torrents,
System Info, Automation, notifications). The URL is sourced live from
`GET /admin/swarm/overview`'s self-entry `reachable_url` field (the same
value the backend computes for its own peer-discovery announcements) --
never hardcoded or built from a Host header.

**A real logo, not just text.** `ui/assets.py` loads
`ui/assets/logo.png` (the Batocera Fleet Federation marquee built earlier
this session, reused rather than duplicated) as a GPU texture via
`hello_imgui.image_and_size_from_encoded_data`, cached after first load.
**This must never be called from `on_enter()`** -- uploading a texture
needs a live GL context, which only exists once `ui/app.py`'s real render
loop is running; `on_enter()` is also reachable from plain unit tests with
no window/context at all, where the call doesn't raise a catchable Python
exception but hard-crashes the process (confirmed the hard way while
building this). Both `about.py` and `shell.py` only ever touch it from
inside `draw()`, which no committed test calls directly, matching this
codebase's existing test-vs-render split. The About page crops the image
to its wordmark's actual content band via UV coordinates (the source PNG
has large transparent margins built for EmulationStation's wide marquee
slot -- showing the full square left an odd empty gap here); the shell's
top bar shows a small uncropped version next to the app name.

**Visual theme pass.** `ui/theme.py` already reproduced drone.css's exact
color palette, but a flat ImGui look with no page texture, no card
elevation, and tight default spacing still read as "plain" next to the
browser UI's grid-textured, gradient, drop-shadowed cards. Closed most of
that gap with pure ImGui draw-list work, no new dependencies: a faint 42px
grid drawn on the background draw list every frame
(`draw_background_grid`, reproducing drone.css's own `body` background
exactly), the root window switched to `WindowFlags_.no_background` so that
grid shows through instead of being painted over by the window's own
opaque fill (child windows/frames keep their real backgrounds, so they now
read as cards sitting on the grid), a left-to-right gradient behind the
shell's top bar matching `.sidebar`'s CSS gradient, more generous window/
frame padding and rounding, and the previously-unused `ACCENT_HOT` (pink,
`--admin-accent-hot` in drone.css) now used for the app name in the top
bar and the About page's kicker text. Deliberately not attempted: an icon
font (Bootstrap Icons is what actually carries a lot of the web UI's visual
interest) -- no icon font is vendored, and picking/bundling/mapping one is
a meaningfully bigger, separate piece of work, not a same-session polish
item.

## Loading spinner + a same-frame deferral bug (2026-08-13, later same day)

**The bug:** Backups' original "Applying..." deferral (described above)
checked and executed the blocking call from the tail end of the *same*
`draw()` invocation that first armed it -- later in the function body, but
still before that frame's `imgui.render()`/SDL buffer swap ever happens.
Dear ImGui draw commands only reach the screen once a frame's `draw()`
returns and `ui/app.py`'s loop calls `render()` + `SDL_GL_SwapWindow()`;
until then the *previous* frame is still the one actually on screen. So
the blocking call was running before the frame containing "Applying..."
was ever presented -- the UI still visibly froze first, exactly the
failure this was supposed to prevent, just with an extra unused flag.
Caught by writing a real regression test that asserts *which frame number*
the POST fires on (`smoke_spinner_deferral.py`-style: arm on frame 2,
assert zero POSTs on frame 2, assert exactly one POST on frame 3) --
the earlier "no exception across 4 frames" smoke test could never have
caught this, since same-frame-vs-next-frame timing isn't an exception.

**The fix:** the pending-id check now runs at the *very top* of `draw()`,
before anything later in that same call (the confirmation popup, in
Backups' case) can arm a new one. A `backup_id`/request landing in the
pending slot can therefore only ever be picked up on the frame *after* the
one that set it -- by which point the "Applying..."/"Requesting..." row
was already rendered, swapped, and genuinely on screen for the whole
duration of the block. Applied to both `ui/screens/backups.py` (renamed
`_apply_in_flight_id` -> `_apply_pending_id` to make the semantics --
"armed, not yet started" -- clearer) and the same pattern newly added to
`ui/screens/swarm.py`'s Request Assets pull (`_request_item` now only
arms `_pending_request`; `_do_request_item` is the actual blocking call,
executed from the top of `draw()`).

**The spinner:** `ui/widgets.spinner()` -- a small rotating arc drawn with
`ImDrawList.add_polyline` against `imgui.get_time()`, no image asset, no
animation-frame bookkeeping. Paired with the deferral fix above wherever a
blocking call is now genuinely visible-before-it-blocks: Backups' Apply
row and Request Assets' per-item Request row. Deliberately not added
to every screen's initial `on_enter()` load -- those complete fast enough
in practice (and the worst offender, `swarm/overview` under a failing
storage mount, was a separate real bug fixed in the main Drone app, not a
"needs a spinner" problem) that a spinner there would mostly just flash.

**Request Assets: same false-negative-timeout bug, different endpoint.**
Live investigation (`drone-live-debugging` skill) of a user report --
"same api timeout error" downloading a game from a paired peer --
found `127.0.0.1 - 500 internal error "/v1/api/admin/local-network/sync":
SSLError: [SYS] unknown error (_ssl.c:2417)` in this Drone's own log at
the same moment the peer's own log showed the artwork+ROM transfer
completing successfully seconds later. `POST /admin/local-network/sync`
only *queues* a job (202, near-instant by design -- the real transfer runs
later over Drone-to-Drone P2P, never through this request) but the local
Drone was mid poll-cycle (concurrent ROM/BIOS metadata scanning, visible
interleaved in the same log window) at the time, plausibly enough to push
an otherwise-fast request past the client's 15s budget; the SSLError is
consistent with the client giving up and closing the loopback socket
before the (still-successful) response could be written back. Same
mitigation as Backups apply: `endpoints.request_asset` now sends its own
longer timeout (30s -- generous for what should be a near-instant queue
call, still short enough not to mask a genuinely offline peer for long).

## Full controller navigation + virtual keyboard (2026-08-13)

**The vendored SDL2/ImGui backend had three real gaps, not zero** (found
reading `imgui_bundle.python_backends.sdl2_backend` directly -- confirmed
no other vendored backend in the package, sdl2/sdl3/glfw/pygame/pyglet,
covers any of them either): it never set ImGui's `HasGamepad` flag (which
the nav system checks before gamepad nav activates *at all* -- confirmed
live, a simulated gamepad button press was silently ignored until this
flag was set), had zero analog left-stick support, and never translated
shoulder buttons (L1/R1) to any `imgui.Key`. `ui/gamepad.py` patches all
three -- plain functions called from `ui/app.py`'s render loop, mirroring
the existing `_fix_hidpi_framebuffer_scale` pattern for patching a
vendored-backend gap, not a `SDL2Renderer` subclass. The analog stick is
**polled every frame**, not event-driven off `SDL_CONTROLLERAXISMOTION` --
that event only fires on change, and ImGui's nav-repeat timer needs the
*current* held value every frame, the same way a held D-pad button
repeats. L1/R1 drive a small bonus: quick Swarm/VPN/Backups tab-cycling
in `ui/shell.py` (polish, not a functional gap -- D-pad/stick nav can
already reach those same top-bar buttons directly).

**`ui/virtual_keyboard.py`** is a custom on-screen QWERTY keyboard (Dear
ImGui has none built in) that auto-opens when a text field is activated
via gamepad specifically -- never via mouse, so mouse+keyboard users keep
typing directly through the real `imgui.input_text` widget, completely
unaffected. Detection uses `imgui.get_current_context().active_id_source == imgui.internal.InputSource.gamepad`,
checked right after `imgui.is_item_activated()`. A single module-level
session (not a per-field dict -- a popup modal blocks everything else
while open, so at most one can ever be active) is matched by the exact
ImGui label string a call site passes in. `virtual_keyboard.input_text()`
is a drop-in replacement for `imgui.input_text` -- every existing text
field (login form, Tailnet auth key, LAN pairing codes, both search
boxes) plus the two new VPN credential fields now go through it.

Now that typed secrets are gamepad-enterable, **VPN credentials/sharing
are back in scope** (previously excluded specifically for lack of a
keyboard). Getting the actual `.ovpn` **config file** onto the device is a
separate problem the virtual keyboard doesn't solve, so the VPN screen's
"Get a VPN Config" flow offers two gamepad-native paths: pulling one from
an already-configured paired peer (reuses the existing
`POST /admin/vpn/pull-from-peer`, zero typing or file picker at all), or
importing one a PC dropped into a new local drop folder
(`<userdata_root>/vpn-import`, reachable at `\\<device-ip>\share\vpn-import`
over Batocera's own default guest SMB export -- new backend endpoints,
`GET/POST /admin/vpn/import-folder*` in `app/device/vpn_manager.py` +
`app/web/handlers_vpn.py`, delegating to the existing
`save_uploaded_config()` so the `.ovpn`/`remote`-directive validation and
rewrite logic isn't duplicated).

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
- **Gamepad nav infrastructure + virtual keyboard (2026-08-13):** confirmed
  live that `imgui.is_key_pressed` for a gamepad key genuinely does
  nothing until `io.backend_flags` has `HasGamepad` set -- a simulated
  R1 press was silently ignored before the fix, correctly triggered
  `AppShell`'s quick-tab-switch after it, proving Gap 1 was a real
  functional blocker, not just theoretically incomplete. The virtual
  keyboard's full round trip was driven through real frames end-to-end:
  simulated a gamepad-sourced activation on a real field (Tailnet auth
  key), typed characters via the actual `_draw_key_rows` button logic in
  a live frame, pressed Done, and confirmed the committed value flowed
  back into the screen's own state on the next frame via the real
  `input_text()` label-matching path -- not just the isolated state-machine
  unit tests. The redesigned VPN screen (Get Config peer-pull and
  folder-import sub-flows, credentials fields, sharing checkbox, the
  source_peer_id guard state) was driven through real frames against both
  the mock server and the real backend with no exceptions. The new
  `/admin/vpn/import-folder*` backend endpoints were verified twice over,
  independently: raw HTTP against a real live server (404 for an unknown
  filename, 200 with correctly-parsed remotes for a real dropped file),
  and again through ports-client's own `endpoints.py` wrappers against
  that same real server. Also re-deployed the full bundle to the real
  `batocera.local` device (isolated scratch dir, matching the existing
  cp312-ABI/cert-path incidents' verification pattern) with all of this
  session's new code loaded -- confirmed a clean boot and a stable,
  actively-rendering process (10% CPU, zero tracebacks).
- **Backup restore, the About page, and the theme pass (2026-08-13):**
  the full Apply-backup confirm-then-restore flow driven through real
  frames -- opened the confirmation popup, checked the ack box, confirmed,
  and verified the deferred two-phase call actually fired
  `POST /admin/config-backups/{id}/apply` with the longer 180s timeout and
  surfaced the real `restored_file_count`/`restarted_emulationstation`
  result text. The About screen's real GPU texture load (`hello_imgui.
  image_and_size_from_encoded_data`) driven through real frames against a
  real SDL2/GL context -- confirmed it crashes the process (not a catchable
  exception) when attempted without a live context, which is exactly why
  it's called from `draw()` and not `on_enter()`. The whole shell (About/
  Swarm/VPN/Backups) re-verified rendering cleanly across 8 real frames
  with the new gradient top bar, background grid, and logo texture all
  active together, plus visual review of actual captured framebuffer
  screenshots (`glReadPixels`) for all four sections -- not just "no
  exception," actually looked at.

**Not verified -- needs real Batocera hardware or a Linux dev box, per the
plan's Phase 1:**
- KMS/DRM rendering specifically (dev verification above used a normal
  windowed/GLX-ish desktop GL context on macOS, not the DRM path).
- Whether the GPU driver on ARM Batocera devices exposes desktop **OpenGL
  3.3 core** (what imgui_bundle's bundled Python GL3 renderer requires,
  GLSL `#version 330`) rather than GLES-only -- a real risk for some of the
  fleet's ARM boards/handhelds, unverified either way.
- **Real physical gamepad feel** -- everything above proves the plumbing is
  genuinely wired correctly (not just "should work"), but a real button
  press/stick tilt on real hardware, and whether `active_id_source` really
  reports `gamepad` (not `keyboard`) for a real controller's activation,
  are both still unconfirmed; no physical controller was available in this
  environment. See `ui/virtual_keyboard.py`'s docstring for the concrete
  fallback (`is_item_activated() and not is_mouse_clicked(0)`) if the
  `active_id_source` check misbehaves on real hardware.
- Whether a popup modal (the virtual keyboard) cleanly receives D-pad/stick
  nav focus with no extra glue code on real hardware -- standard documented
  ImGui behavior, no counter-evidence found reading the vendored backend,
  but unconfirmed live.
- The `_DEADZONE = 0.25` analog-stick constant in `ui/gamepad.py` is a
  defensible default, not a validated value -- may need tuning (0.15-0.35)
  once tested against real, possibly-worn analog sticks.
- The theme's actual visual appearance against a real display (colors were
  taken directly from `app/web/static/css/drone.css`'s `:root` values, not
  eyeballed against a running Drone UI side-by-side).
