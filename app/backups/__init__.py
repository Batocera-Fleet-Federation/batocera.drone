"""Categorized, swarm-aware backups of Batocera/EmulationStation/emulator settings,
saves and save states (stdlib only).

``categories`` decides which file belongs to which restorable part, ``confmerge``
keeps secrets out of archives and merges ``batocera.conf`` key by key, ``archive``
builds and verifies archives, ``restore`` previews and applies them, and ``service``
ties that to the SQLite store, background jobs and paired peers.
"""
