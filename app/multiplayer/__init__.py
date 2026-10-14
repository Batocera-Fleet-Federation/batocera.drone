"""Unified multiplayer orchestration over the existing peer-to-peer swarm.

No central coordinator: every Drone runs its own ``MultiplayerOrchestrator`` and
any trusted peer can host a session. Sessions are coordinated over the
already-authenticated ``/peer/*`` mTLS channel; emulator gameplay traffic uses
whatever transport the chosen mechanism needs. See ``docs/multiplayer.md``.
"""
