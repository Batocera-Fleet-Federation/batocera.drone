"""Remote multiplayer streaming, built on Sunshine (host) and Moonlight (guest).

Drone does not implement video capture, codecs, audio transport or input
forwarding. It integrates the established pair: **Sunshine** captures and
encodes the host's screen (hardware encoders where the GPU has them) and
receives the guest's controllers as virtual gamepads; **Moonlight** is the
client. Their authenticated pairing is done by a PIN in Sunshine's own web UI,
which Drone neither holds credentials for nor bypasses, so that step stays
manual. What Drone adds: dependency detection on both ends, a quality profile
bounded by your settings, host-performance checks, launching the game on the
host, and the lifecycle around it.

Drone conservatively limits its streaming lobby to the host plus one remote
guest. Sunshine owns pairing, input policy and stream teardown independently.
"""

from typing import Dict, List, Optional

from .. import models as m
from ..dependencies import managed_binary
from .base import ASSISTED, CONTENT_HOST_ONLY, TIER_EXPERIMENTAL, EmulatorAdapter

SUNSHINE_PORTS = "TCP 47984, 47989, 47990, 48010; UDP 47998-48000, 48002, 48010"
RESOLUTION_ORDER = ("854x480", "1280x720", "1920x1080", "2560x1440", "3840x2160")


def build_stream_profile(stream_settings: dict, hardware: dict) -> dict:
    """Recommended stream profile, never above the user's configured ceilings."""
    quality = stream_settings.get("quality", "balanced")
    ceiling = stream_settings.get("max_resolution", "1280x720")
    wanted = {"low_latency": "1280x720", "balanced": ceiling, "quality": ceiling}[quality]
    resolution = min(wanted, ceiling, key=RESOLUTION_ORDER.index)
    fps = int(stream_settings.get("max_fps", 60))
    if quality == "low_latency":
        fps = min(fps, 60)
    bitrate = min(int(stream_settings.get("max_bitrate_kbps", 10000)),
                  {"low_latency": 8000, "balanced": 15000, "quality": 40000}[quality])
    preference = stream_settings.get("encoding", "auto")
    hardware_available = bool(hardware.get("vaapi") or hardware.get("nvenc"))
    encoder = "hardware" if (preference != "software" and hardware_available) else "software"
    return {"resolution": resolution, "fps": fps, "bitrate_kbps": bitrate, "encoder": encoder,
            "hardware_encoder_available": hardware_available,
            "note": "Choose these values in Moonlight; encoder detection is a hardware hint, not a verified codec probe."}


class StreamingAdapter(EmulatorAdapter):
    id = "streaming"
    name = "Remote streaming (Sunshine + Moonlight)"
    tier = TIER_EXPERIMENTAL
    automation = ASSISTED
    systems = frozenset({"*"})
    mechanisms = (m.REMOTE_STREAMING,)
    binaries = ("sunshine",)
    extra_binary_paths = ("/usr/bin/sunshine",)
    protocols = (f"Sunshine/Moonlight (GameStream-style): {SUNSHINE_PORTS}",)
    permissions = ("Launch the game on the host through EmulationStation", "Read-only detection of Sunshine/Moonlight")
    requirements = (
        "Host: Sunshine installed and running; hardware encoding recommended (VAAPI/NVENC)",
        "Guest: Moonlight installed",
        "A one-time PIN pairing between Moonlight and the host's Sunshine web UI",
    )
    limitations = (
        "Drone currently limits streaming sessions to the host plus one remote guest",
        "Drone does not enforce virtual-gamepad slots or revoke Sunshine input/pairings; disable keyboard and mouse in Sunshine",
        "Pairing needs the PIN step in Sunshine's own web UI (Drone does not hold Sunshine credentials)",
        "Latency depends on the network path; Tailscale relays (DERP) add noticeable lag",
        "Only the host needs the game",
    )
    content_match = CONTENT_HOST_ONLY
    guest_launches = False
    max_players = 2

    def binary(self) -> Optional[str]:
        # Also finds the Sunshine that Drone installed from Multiplayer settings.
        extra = (str(managed_binary(self.probe.settings)),) + tuple(self.extra_binary_paths)
        return self._cached("binary", lambda: self.probe.find_binary(self.binaries, extra))

    def moonlight_binary(self) -> Optional[str]:
        return self.probe.find_binary(("moonlight", "moonlight-qt", "Moonlight"), ("/usr/bin/moonlight-qt",))

    def sunshine_running(self) -> bool:
        return self.probe.process_running(("sunshine",))

    def _detect_features(self) -> Dict[str, dict]:
        host = self.detect_installed()
        guest = self.moonlight_binary() is not None
        reasons = []
        if not host:
            reasons.append("Sunshine (host) is not installed; Drone can install it from Multiplayer settings")
        if not guest:
            reasons.append("Moonlight (guest) is not installed; Batocera's Moonlight client must be set up there")
        return {m.REMOTE_STREAMING: {"available": host or guest, "host_ready": host, "guest_ready": guest,
                                     "sunshine_running": self.sunshine_running() if host else False,
                                     "hardware": self.probe.hardware(), "reason": "; ".join(reasons)}}

    def readiness(self) -> dict:
        feature = self.detect_features()[m.REMOTE_STREAMING]
        if not feature["available"]:
            return {"status": "not_installed", "message": "Neither Sunshine nor Moonlight was found on this machine."}
        parts = [f"Sunshine {'found' if feature['host_ready'] else 'missing'}",
                 f"Moonlight {'found' if feature['guest_ready'] else 'missing'}"]
        return {"status": "manual_steps_required", "message": "; ".join(parts) + ". Pairing is a manual PIN step."}

    def assess_game(self, game: dict, overrides: Optional[dict] = None) -> Optional[dict]:
        players = game.get("players")
        extra = {"emulator": {"adapter": self.id, "name": "sunshine", "core": "", "version": self.detect_version(), "core_version": ""},
                 "max_players": 2 if players is None or players >= 2 else None,
                 "guest_ready": self.moonlight_binary() is not None}
        if players == 1:
            return self.assessment(m.REMOTE_STREAMING, m.UNSUPPORTED, ["Game metadata lists a single player."], **extra)
        feature = self.detect_features()[m.REMOTE_STREAMING]
        if not feature["host_ready"]:
            return self.assessment(m.REMOTE_STREAMING, m.UNSUPPORTED, ["Sunshine is not installed on this (host) machine. Install it from Multiplayer > Settings > Remote streaming."], **extra)
        reasons = ["Streams the host's local multiplayer to one remote guest; only the host needs the game."]
        if not feature["sunshine_running"]:
            return self.assessment(m.REMOTE_STREAMING, m.REQUIRES_CONFIGURATION,
                                   reasons + ["Sunshine is installed but not running. Start it from Multiplayer > Settings > Remote streaming."], **extra)
        if not (feature["hardware"]["vaapi"] or feature["hardware"]["nvenc"]) and feature["hardware"]["cpu_count"] < 4:
            return self.assessment(m.REMOTE_STREAMING, m.UNSUPPORTED,
                                   reasons + ["No hardware encoder and fewer than 4 CPU cores: software encoding would not keep up."], **extra)
        if players is None:
            return self.assessment(m.REMOTE_STREAMING, m.UNKNOWN, reasons + ["The game's local multiplayer support is not recorded."], **extra)
        return self.assessment(m.REMOTE_STREAMING, m.LIKELY_COMPATIBLE, reasons, **extra)

    def validate_configuration(self, session: dict, role: str) -> List[str]:
        feature = self.detect_features()[m.REMOTE_STREAMING]
        if role == m.HOST_ROLE and not feature["host_ready"]:
            return ["Sunshine is not installed on the host."]
        if role == m.GUEST_ROLE and not feature["guest_ready"]:
            return ["Moonlight is not installed on this machine; set up Batocera's Moonlight client first."]
        return []

    def prepare_session(self, session: dict, role: str, context: dict) -> dict:
        profile = session.get("stream_profile") or {}
        wanted = (f"{profile.get('resolution', '')} at {profile.get('fps', '')} FPS, up to {profile.get('bitrate_kbps', '')} kbit/s "
                  f"({profile.get('encoder', 'software')} encoding)")
        if role == m.HOST_ROLE:
            steps = ["Make sure Sunshine is running. Drone starts the game on this machine.",
                     "When the guest asks to pair, enter their PIN in Sunshine's web UI (https://localhost:47990)."]
        else:
            steps = [f"In Moonlight add the host at {context.get('host_address', 'the host')} and pair using the PIN shown in Moonlight.",
                     f"Connect and stream the host's desktop/app, using about {wanted}.",
                     "Your controllers appear as extra gamepads on the host through Sunshine."]
        return {"role": role, "manual_steps": steps}
