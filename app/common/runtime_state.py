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
# Wakes the ROM-metadata poller (file-watcher debounce or a completed download can
# request an out-of-band resync).
_ROM_METADATA_WAKE = Event()
# Serializes the ROM-metadata cache read-modify-write against the poller.
_ROM_METADATA_LOCK = RLock()
# Serializes gamelist.xml read-modify-write so concurrent artwork downloads (the
# download pool runs several at once) can't clobber each other's <image>/<video>
# entries on the same system. Writes are sub-millisecond, so one global lock is free.
_GAMELIST_WRITE_LOCK = Lock()
# Serializes the root-direct "stop EmulationStation -> write config -> start
# EmulationStation" sequence (screen mode, ES collections/music-volume/screensaver)
# across concurrent HTTP handler threads. Without this, two overlapping requests each
# call `stop`/`start` on the same process/X session -- confirmed live to corrupt the
# session into a permanent crash-restart loop (black screen) that doesn't self-heal.
# The whole stop->write->start sequence is held under this lock, not just the
# subprocess calls, so a second caller waits for the first to fully finish (including
# its own re-read of state) before starting its own -- never interleaves with it.
_ES_LIFECYCLE_LOCK = Lock()
