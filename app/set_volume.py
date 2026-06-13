#!/usr/bin/env python3
"""Set the Batocera system output volume as the privileged service worker.

Canonical implementation of "change the machine volume", invoked by the
privileged (root) Drone service worker as ``python3 app/set_volume.py <0-100>``
and reused in-process by the Drone app when it happens to run as root. ``0``
means mute. Persists the level via ``batocera-settings-set audio.volume`` and
applies it live with ALSA so it takes effect without a reboot.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


MIXER_CONTROL = "Master"


def _clamp(level: int) -> int:
    return max(0, min(100, int(level)))


def set_audio_volume(level: int) -> None:
    """Persist and apply the output volume. Raises on failure.

    Runs whichever tools are available: ``batocera-settings-set`` persists the
    value across reboots and ``amixer`` applies it immediately. At least one of
    them must be present, otherwise there is no way to honour the request.
    """
    level = _clamp(level)
    applied = False

    settings_set = shutil.which("batocera-settings-set")
    if settings_set:
        subprocess.run([settings_set, "audio.volume", str(level)], check=True)
        applied = True

    amixer = shutil.which("amixer")
    if amixer:
        if level <= 0:
            subprocess.run([amixer, "-q", "sset", MIXER_CONTROL, "mute"], check=True)
        else:
            subprocess.run([amixer, "-q", "sset", MIXER_CONTROL, f"{level}%", "unmute"], check=True)
        applied = True

    if not applied:
        raise OSError("No volume tool found (batocera-settings-set / amixer)")


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
