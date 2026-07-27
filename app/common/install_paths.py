"""Where the Drone app is physically installed on disk.

A handful of features (Torrents, VPN) default their working directory to a
folder next to the app itself rather than under the userdata ROM/BIOS/saves
tree -- e.g. ``<install root>/torrents``, ``<install root>/vpn``. On a real
device that's ``/userdata/system/drone-app``; in a development checkout it's
the repo root. This is derived from ``__file__``, never from the process's
working directory, so it stays correct regardless of how the service
launcher starts the app.
"""

from pathlib import Path


def drone_install_root() -> Path:
    # <install root>/app/common/install_paths.py -> the folder the Drone app
    # is physically deployed in.
    return Path(__file__).resolve().parents[2]
