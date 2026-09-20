# Hotfixes

One-off fixes for problems in software Drone does not own (RGS generators, emulator builds) on Batocera machines.
Each folder is self-contained and idempotent: `check.sh` (is it in place?), `apply.sh` (install/repair), `revert.sh` (exact inverse), plus a README with the problem, the evidence and the limits.

These are **not** part of the drone release payload (`app/` and `content/`); they are stored here so they can be re-applied after an RGS update overwrites the files they patch.

| Hotfix | Fixes |
|---|---|
| [smash-gui-autoload](smash-gui-autoload/README.md) | Super Smash Bros. Ultimate hangs (Citron) or stalls in gameplay (Eden) when RGS starts it with command-line autoboot; loads the game through the emulator GUI instead. |
