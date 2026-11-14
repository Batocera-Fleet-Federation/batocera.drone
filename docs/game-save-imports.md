# Import game saves from a paired Drone

Open a locally installed ROM's Assets detail page. **Saves & Saved States from
the Swarm** shows save/state support and a **Check saves** button for each paired
HTTPS Drone. Checking a Drone lists only existing save sets associated with the
selected game. Select **Download & apply** on the desired file, state slot, or
save directory. The list includes its source, emulator layout, size and date.
Unreachable or older Drones report an error with a retry button.

Close the game first. Drone verifies every file's full SHA-256 and length before
installing the complete save set. Game progress is ready at the next launch;
saved states are ready in the emulator's load-state menu. Drone does not launch a
game or force a state into a running emulator. State compatibility still depends
on the emulator/core version and game revision.

**Overwrite existing local saves** is off by default. This keeps the previous
local set separately before applying the downloaded set. Backups are private
`.drone-save-*/previous/` directories inside that layout's save root, excluded
from discovery and save-cache scans. With the toggle on, the previous set is
removed after successful installation. A failed verification changes no local
saves; a failed installation rolls back. If rollback itself fails (for example a
filesystem failure), the recovery copy remains in that private directory.

## Layouts and matching

The generic adapter applies to every system directory under `SAVES_ROOT`,
including nested emulator/core directories. It recognizes exact ROM stems and
full filenames with save/state extensions, and complete per-game directories.
States include their PNG thumbnails. A prefix such as `Mario` never matches
`Mario Kart` or `Mario2`. Known standalone roots (Dolphin, PPSSPP, PCSX2,
DuckStation, Citra/Azahar and Cemu) are searched too. GameCube/Wii disc-header IDs
are used when available. Renamed copies can be located using the ROM cache's
sampled content identity; installed filenames are rebased to the receiving ROM's
name. Saves left behind after removal of a remote ROM can still be discovered
by the requested name.

Switch NAND saves for Eden/Yuzu/Citron/Suyu are grouped by title ID and source
profile; the incoming set is mapped to the local emulator's profile. These
layouts are inspected under `saves/switch/<emulator>/nand`,
`system/configs/<emulator>/nand` and `saves/<emulator>` — stock Batocera symlinks
`configs/<emulator>/nand/user/save` to `saves/yuzu` (and Ryujinx's
`bis/user/save` to `saves/Ryujinx`), and the scan never follows symlinks, so the
link targets are scanned directly. The ROM filename or metadata must identify its
16-character Switch title ID (for example `[01006A800016E000]` for Super Smash
Bros. Ultimate). A local NAND profile must already exist and be unambiguous:
emulators leave stub profiles behind, so when several exist the one that already
holds non-hidden data for this title wins; otherwise Drone refuses to guess.
The all-zero user (device saves) only maps onto itself. Ryujinx numbered saves are
matched by the title ID in the 512-byte `ExtraData0` (older builds: `ExtraData`)
record, skipping BCAT, temporary and cache save types (they share the title ID
but hold no progress), then mapped to the existing local slot for that title;
run the game once locally to create the slot. Source
user IDs and slot numbers are never blindly installed as local users.

Save support is reported as **unknown** when there is no positive evidence.
Missing files are not reported as emulator incapability. Switch saved states are
explicitly unsupported. Shared multi-game memory-card images and unknown
emulator-specific database formats cannot safely be attributed to one ROM and
are not offered as that ROM's save. These need a format-aware adapter, rather
than copying every save on a machine. No actual Switch hardware/game session is
part of the automated tests.

## Protocol and security

`GET /systems/{system}/roms/{unique_id}/saves` lists support and paired sources;
`?drone=<id>` checks just that source. `POST
/admin/systems/{system}/roms/{unique_id}/saves` takes an opaque, ten-minute choice
and a boolean `overwrite`. The choice is bound to the local Drone, ROM and
paired source. Mutations require a real login, enabled administration,
same-origin requests and JSON.

Peers implement `GET /peer/game-saves` and `/peer/game-saves/file` on the existing
mTLS surface. Both Drones need this feature. Transfers recheck the pairing pin,
require HTTPS and client certificates, and reject proxies and redirects. Peer
paths are validated against known save layouts and game associations. File I/O
uses no-follow directory descriptors; symlinks and special files cannot redirect
reads/writes. Transfers are bounded to 2,048 files and 1 GiB per set, inventories
to 256 sets and 2 MiB, and filesystem scans to 20,000 entries per layout. Switch
scans are pruned to where a match can exist (a NAND layout only walks
`user/save/0000000000000000`; the generic layout skips `nand`/`bis` and emulator
folders), so a machine with a real Switch NAND is not rejected as "too large".
Imports apply the same rule: a generic Switch save may not contain any spelling
of `nand` or `bis` (case, Unicode and trailing-dot variants alias the real tree on
case-folding volumes such as exFAT, NTFS and APFS) or start in an emulator folder,
so profile and Ryujinx slot ids are always remapped to this machine.

Discovery waits up to 25 s (the peer hashes candidate files) and reports *why* it
failed: an older peer without this endpoint ("does not support save sharing yet"),
a refusal, a timeout, an unreadable list or an unreachable Drone. A peer that has
a different ROM under the same name answers 409, which is shown as "nothing to
offer", not an error. The ROM page chooser is deliberately compact: one status +
overwrite line and one line per Drone.

Layout references: [Batocera state support](https://wiki.batocera.org/emulationstation_savestates),
[Switch save attributes](https://github.com/switchbrew/libnx/blob/master/nx/include/switch/services/fs.h),
[Eden profile/save-path behavior](https://github.com/eden-emulator/Issue-Reports/issues/706).
