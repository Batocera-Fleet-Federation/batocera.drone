# Hotfixes

Fixes for problems in software Drone does not own (RGS generators, emulator builds) on Batocera machines.
Standalone fixes live in self-contained folders with apply/check/revert scripts. Integrations that must run automatically are managed by Drone and documented with their feature.

These are **not** part of the drone release payload (`app/` and `content/`); they are stored here so they can be re-applied after an RGS update overwrites the files they patch.

| Hotfix | Fixes |
|---|---|
| [smash-gui-autoload](smash-gui-autoload/README.md) | Super Smash Bros. Ultimate hangs (Citron) or stalls in gameplay (Eden) when RGS starts it with command-line autoboot; loads the game through the emulator GUI instead. |
| [RGS Proton NAT-PMP integration](../docs/proton-rgs.md) | Drone-managed patch for Proton's forwarded P2P port; re-applied after RGS updates. |
