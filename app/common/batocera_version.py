"""Canonical Batocera OS version string for this device.

Reads the on-disk ``batocera.version`` file directly (e.g. ``43.1``), rather than
relying on parsing free-form ``batocera-info`` CLI text, whose "Version" line can be
missing or differently labeled and previously caused callers to fall back to
unrelated Linux/kernel text (``fields.system``) as the reported "Batocera version".

Pure stdlib, no Drone-internal dependencies.
"""

from pathlib import Path
from typing import Optional

_SYSTEM_VERSION_FILE = Path("/usr/share/batocera/batocera.version")


def _read_batocera_version(userdata_root: Optional[Path] = None) -> Optional[str]:
    candidates = []
    if userdata_root is not None:
        candidates.append(userdata_root / "system" / "batocera.version")
    candidates.append(_SYSTEM_VERSION_FILE)
    for candidate in candidates:
        try:
            if candidate.exists():
                text = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
                if text:
                    return text
        except OSError:
            continue
    return None
