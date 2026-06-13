#!/usr/bin/env python3
"""Set the Batocera system output volume as the privileged service worker.

Canonical implementation of "change the machine volume", invoked by the
privileged (root) Drone service worker as ``python3 app/set_volume.py <0-100>``
and reused in-process by the Drone app when it happens to run as root. ``0``
means mute. Uses ``batocera-audio setSystemVolume <level>`` (the supported
Batocera command, which both applies the level live and persists it); falls
back to ALSA ``amixer`` only on non-Batocera/dev hosts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


MIXER_CONTROL = "Master"


def _clamp(level: int) -> int:
    return max(0, min(100, int(level)))


def _run(command: list) -> str:
    """Run a command, returning its combined output; raise OSError with that output on failure."""
    proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise OSError(f"{' '.join(command)} exited {proc.returncode}: {output or '(no output)'}")
    return output


def set_audio_volume(level: int) -> None:
    """Apply the output volume (0-100, 0 = mute). Raises on failure.

    Prefers ``batocera-audio setSystemVolume <level>`` which sets the absolute
    system volume live and persists it. Falls back to ALSA ``amixer`` only when
    batocera-audio is unavailable (dev/container hosts); at least one tool must
    be present, otherwise there is no way to honour the request.
    """
    level = _clamp(level)

    audio = shutil.which("batocera-audio")
    if audio:
        output = _run([audio, "setSystemVolume", str(level)])
        print(f"batocera-audio setSystemVolume {level}: {output or 'ok'}")
        return

    amixer = shutil.which("amixer")
    if amixer:
        if level <= 0:
            _run([amixer, "-q", "sset", MIXER_CONTROL, "mute"])
        else:
            _run([amixer, "-q", "sset", MIXER_CONTROL, f"{level}%", "unmute"])
        return

    raise OSError("No volume tool found (batocera-audio / amixer)")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: set_volume.py <0-100>", file=sys.stderr)
        return 2
    try:
        level = int(sys.argv[1])
    except ValueError:
        print("Usage: set_volume.py <0-100>", file=sys.stderr)
        return 2
    set_audio_volume(level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
