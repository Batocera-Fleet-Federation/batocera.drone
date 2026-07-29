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
    #
    # A release-versioned deploy layout puts the real files under
    # <install root>/.releases/<version>/app/..., with <install root>/app
    # a symlink to current/app -> .releases/<version>/app. Python's import
    # machinery already reports __file__ fully resolved *through* that
    # symlink chain (confirmed live: __file__ itself, before this function
    # ever touches it, is already
    # ".../drone-app/.releases/0.1.91-.../app/common/install_paths.py") --
    # so a fixed parents[2] silently lands inside the versioned release
    # directory instead of the stable install root. That broke VPN outright
    # (its directory is never user-configurable, so every call recomputed
    # the wrong path and every `openvpn --config <wrong path>` invocation
    # failed with "Error opening configuration file"); Torrents happened to
    # keep working only because its directory is normally a user-saved
    # absolute path in settings, not recomputed by this function on every
    # call.
    #
    # Walk up past a `.releases` path segment if one is present, so this
    # keeps working under that layout; fall through to the original
    # fixed-depth computation for a flat/dev-checkout layout where there is
    # no `.releases` directory at all.
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == ".releases":
            return parent.parent
    return resolved.parents[2]
