"""Shared process-wide runtime state: mutable coordination singletons.

Extracted from ``drone_api.py``. These ``Event``/``Lock`` objects coordinate the
ROM-metadata poll/sync pipeline and gamelist writes across threads. They are *mutated*
(``set``/``clear``/``acquire``) and never reassigned, so every module that needs one
imports the same object from here (a dependency-free leaf) rather than reaching back
into ``drone_api``. Reassigned flags/singletons (the ``*_STARTED`` bootstrap flags,
``_DOWNLOAD_MANAGER``, ``_GAME_PROCESS_MONITOR``) stay in ``drone_api`` because a
``global`` reassignment can't target another module's binding.
"""

from threading import Event, Lock, RLock

# Set while a ROM-metadata poll/scan is running, so overlapping triggers coalesce
# instead of launching a second concurrent scan.
_ROM_METADATA_ACTIVE = Event()
# Wakes the ROM-metadata poller (file-watcher debounce, heartbeat drift, or an
# Overmind action can all request an out-of-band resync).
_ROM_METADATA_WAKE = Event()
# Set when an Overmind heartbeat reports asset thumbprints that differ from what the
# Drone holds locally, so the next metadata poll pushes a full inventory to resync.
_ASSET_PUSH_REQUESTED = Event()
# Same idea for game saves, tracked independently so a saves drift does not force a
# (much larger) ROM/BIOS inventory push and vice versa.
_SAVES_PUSH_REQUESTED = Event()
# Serializes the ROM-metadata cache read-modify-write + upload against the poller.
_ROM_METADATA_LOCK = RLock()
# Serializes gamelist.xml read-modify-write so concurrent artwork downloads (the
# download pool runs several at once) can't clobber each other's <image>/<video>
# entries on the same system. Writes are sub-millisecond, so one global lock is free.
_GAMELIST_WRITE_LOCK = Lock()
