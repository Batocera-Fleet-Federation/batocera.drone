"""Elgato Stream Deck integration (see docs/integrations-streamdeck.md).

Drone side (stdlib only): ``manager`` (lifecycle, supervision, apply), ``config``,
``compiler``, ``games``, ``scripts``, ``images``, ``dependencies``, ``jobs``.
Shared by both processes: ``actions`` (built-in registry), ``launch``
(exit-before-launch), ``game_runtime``, ``game_launcher``, ``dispatcher``,
``process``, ``paths``, ``logs``. Worker only (imports StreamDeck/Pillow from the
private ``lib/``): ``worker``, ``runtime``, ``devices`` adapters, ``render``.
"""
