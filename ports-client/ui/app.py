"""Boots the native window and drives the screen-nav render loop.

Uses PySDL2 (ctypes bindings to the system's own libSDL2.so -- the same one
EmulationStation links against, so this needs nothing vendored beyond the
Python wrapper itself) for the window/GL-context/event pump, and
imgui_bundle's bundled ``python_backends.sdl2_backend.SDL2Renderer`` (a
first-party-maintained reference integration, not something hand-rolled
here) to translate SDL2 events into ImGui input and issue the draw calls.

This is a deliberate correction from an earlier draft that used
imgui_bundle's higher-level ``HelloImGui``/``immapp.run()`` runner: the
published imgui_bundle wheel (verified by inspecting the compiled
``_imgui_bundle*.so`` for its linked symbols) is built with GLFW only --
zero ``SDL_*`` symbols, only ``glfw*`` ones and a ``libglfw.so.3`` dependency
-- on every platform, not just macOS. GLFW's Linux backend needs X11/Wayland,
which Batocera doesn't run (EmulationStation itself only works here via
SDL2/KMS-DRM), so the high-level runner's SDL backend selection was
unreachable on real hardware too. This low-level path sidesteps that
entirely by doing our own windowing through PySDL2 against the system SDL2,
and only using imgui_bundle for the ImGui core + its GL3 render backend
(GLSL #version 330 core -- requesting an OpenGL 3.3 core context below).
"""

import ctypes
import os
import signal
import sys

import OpenGL.GL as gl
import sdl2
from imgui_bundle import imgui
from imgui_bundle.python_backends.sdl2_backend import SDL2Renderer

from client.http_client import DroneApiClient

from .nav import Navigator
from .screens.login import LoginScreen
from .shell import AppShell
from .theme import apply_drone_theme

_FORCE_EXIT_GRACE_SECONDS = 3
_FALLBACK_WINDOW_SIZE = (1280, 720)


class PortsClientApp:
    def __init__(self, api_client: DroneApiClient):
        self.api_client = api_client
        self.navigator = Navigator(api_client)
        self._exit_requested = False
        # Batocera's global hotkey daemon SIGTERMs a Ports process on the exit
        # combo -- there's no window manager to Alt+F4 out of, so this is the
        # only route back to EmulationStation and it must exit cleanly. Since
        # we own the frame loop directly now (see module docstring), control
        # returns to plain Python bytecode every iteration, so the handler
        # below is reliably observed each frame -- the SIGALRM is still kept
        # as a bounded belt-and-suspenders in case a single SDL call ever
        # blocks (e.g. a stalled buffer swap): the hotkey combo must never be
        # able to strand the user unable to get back to EmulationStation.
        signal.signal(signal.SIGTERM, self._request_exit)
        signal.signal(signal.SIGINT, self._request_exit)
        signal.signal(signal.SIGALRM, self._force_exit)
        self._show_login()

    def _request_exit(self, *_args) -> None:
        self._exit_requested = True
        signal.alarm(_FORCE_EXIT_GRACE_SECONDS)

    def _force_exit(self, *_args) -> None:
        os._exit(1)

    def _show_login(self) -> None:
        self.navigator.go_to(LoginScreen(self.api_client, on_authenticated=self._show_home))

    def _show_home(self, username: str) -> None:
        self.navigator.go_to(AppShell(self.api_client, username, on_quit=self._request_exit))

    def run(self) -> None:
        window, gl_context = self._init_sdl_window()
        imgui.create_context()
        io = imgui.get_io()
        io.config_flags |= (
            imgui.ConfigFlags_.nav_enable_gamepad.value | imgui.ConfigFlags_.nav_enable_keyboard.value
        )
        # Every screen is a fixed, non-user-customizable full-screen layout
        # (see the root-window wrapper in _run_loop below) -- there is no
        # per-window state worth persisting. Left at ImGui's default, this
        # writes/reads "imgui.ini" relative to the process's CWD, which on a
        # real device is wherever EmulationStation launched the Ports script
        # from -- an unwanted stray file, not a crash, but pointless clutter
        # with nothing meaningful in it.
        io.set_ini_filename(None)
        apply_drone_theme()
        renderer = SDL2Renderer(window)
        self._open_connected_game_controllers()

        try:
            self._run_loop(window, renderer)
        finally:
            renderer.shutdown()
            sdl2.SDL_GL_DeleteContext(gl_context)
            sdl2.SDL_DestroyWindow(window)
            sdl2.SDL_Quit()

    def _run_loop(self, window, renderer: SDL2Renderer) -> None:
        event = sdl2.SDL_Event()
        while not self._exit_requested:
            while sdl2.SDL_PollEvent(ctypes.byref(event)) != 0:
                if event.type == sdl2.SDL_QUIT:
                    self._exit_requested = True
                elif event.type == sdl2.SDL_CONTROLLERDEVICEADDED:
                    sdl2.SDL_GameControllerOpen(event.cdevice.which)
                renderer.process_event(event)
            renderer.process_inputs()
            # SDL2Renderer.process_inputs() (imgui_bundle's own code, not ours)
            # hardcodes display_framebuffer_scale to (1, 1) every frame, which
            # is only correct on a 1x-scale display. On a Retina/HiDPI display
            # the GL drawable is larger than the window's logical size, so the
            # viewport it computes from that wrong scale covers only a
            # fraction of the real framebuffer -- the UI renders into a
            # corner of the window. Batocera's own TV/monitor outputs are
            # effectively always 1x, which is why this never shows up there.
            _fix_hidpi_framebuffer_scale(window, imgui.get_io())

            imgui.new_frame()
            # Every screen calls widget functions (imgui.text/button/...)
            # without ever opening an explicit imgui.begin() window. Left
            # alone, Dear ImGui silently redirects those calls into its
            # built-in fallback "Debug##Default" window -- an ordinary
            # movable, resizable, titled window floating inside the SDL2
            # surface, not a full-screen app -- which is exactly the "movable
            # window inside the main window" a user reported seeing. This
            # wraps every frame in one chrome-less window pinned to the full
            # viewport instead, the same effect HelloImGui's
            # DefaultImGuiWindowType.provide_full_screen_window gave for free
            # before ui/app.py moved off it (see the module docstring).
            viewport = imgui.get_main_viewport()
            imgui.set_next_window_pos(viewport.pos)
            imgui.set_next_window_size(viewport.size)
            root_flags = (
                imgui.WindowFlags_.no_title_bar.value
                | imgui.WindowFlags_.no_resize.value
                | imgui.WindowFlags_.no_move.value
                | imgui.WindowFlags_.no_collapse.value
                | imgui.WindowFlags_.no_saved_settings.value
                | imgui.WindowFlags_.no_bring_to_front_on_focus.value
                | imgui.WindowFlags_.no_nav_focus.value
            )
            is_expanded, _ = imgui.begin("##root", flags=root_flags)
            if is_expanded:
                self.navigator.draw()
            imgui.end()
            imgui.render()

            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(gl.GL_COLOR_BUFFER_BIT)
            renderer.render(imgui.get_draw_data())
            sdl2.SDL_GL_SwapWindow(window)

    @staticmethod
    def _open_connected_game_controllers() -> None:
        for index in range(sdl2.SDL_NumJoysticks()):
            if sdl2.SDL_IsGameController(index):
                sdl2.SDL_GameControllerOpen(index)

    @staticmethod
    def _init_sdl_window():
        if sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_GAMECONTROLLER) < 0:
            print(f"SDL_Init failed: {sdl2.SDL_GetError().decode('utf-8')}", file=sys.stderr)
            sys.exit(1)

        # GLSL #version 330 core, per imgui_bundle's bundled GL3 render
        # backend (opengl_backend_programmable.py) -- an OpenGL ES-only GPU
        # driver would need a different renderer; unverified on ARM Batocera
        # hardware, see README's "Verification status".
        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_DOUBLEBUFFER, 1)
        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MAJOR_VERSION, 3)
        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_MINOR_VERSION, 3)
        sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_PROFILE_MASK, sdl2.SDL_GL_CONTEXT_PROFILE_CORE)

        dev_windowed = os.environ.get("PORTS_CLIENT_DEV_WINDOWED")
        window_flags = sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_ALLOW_HIGHDPI
        width, height = _FALLBACK_WINDOW_SIZE
        if dev_windowed:
            window_flags |= sdl2.SDL_WINDOW_RESIZABLE
        else:
            # Fullscreen "desktop resolution" borderless mode -- matches the
            # rest of the Ports catalog and needs no window manager.
            window_flags |= sdl2.SDL_WINDOW_FULLSCREEN_DESKTOP

        window = sdl2.SDL_CreateWindow(
            b"Batocera Drone",
            sdl2.SDL_WINDOWPOS_CENTERED,
            sdl2.SDL_WINDOWPOS_CENTERED,
            width,
            height,
            window_flags,
        )
        if window is None:
            print(f"SDL_CreateWindow failed: {sdl2.SDL_GetError().decode('utf-8')}", file=sys.stderr)
            sys.exit(1)

        gl_context = sdl2.SDL_GL_CreateContext(window)
        if gl_context is None:
            print(f"SDL_GL_CreateContext failed: {sdl2.SDL_GetError().decode('utf-8')}", file=sys.stderr)
            sys.exit(1)

        sdl2.SDL_GL_MakeCurrent(window, gl_context)
        sdl2.SDL_GL_SetSwapInterval(1)
        return window, gl_context


def _fix_hidpi_framebuffer_scale(window, io) -> None:
    window_w, window_h = ctypes.c_int(0), ctypes.c_int(0)
    sdl2.SDL_GetWindowSize(window, ctypes.byref(window_w), ctypes.byref(window_h))
    drawable_w, drawable_h = ctypes.c_int(0), ctypes.c_int(0)
    sdl2.SDL_GL_GetDrawableSize(window, ctypes.byref(drawable_w), ctypes.byref(drawable_h))
    if window_w.value > 0 and window_h.value > 0:
        io.display_framebuffer_scale = (
            drawable_w.value / window_w.value,
            drawable_h.value / window_h.value,
        )
