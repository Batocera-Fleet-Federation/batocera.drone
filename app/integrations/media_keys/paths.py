"""The Media Keys ownership boundary: everything lives below
``<install root>/integrations/media-keys/`` and nothing follows symlinks.

``config/`` and ``scripts/`` are administrator content; ``state/`` (last press
results, hold tracking), ``logs/`` and ``bin/`` (the ``drone-media-key``
launcher hotkeygen runs) are regenerated runtime files. The only file written
outside this tree is the single hotkeygen override (see ``hotkeygen.py``).
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from ...common.install_paths import drone_install_root
    from ..streamdeck.paths import OwnershipError
except ImportError:  # pragma: no cover - flat execution
    from common.install_paths import drone_install_root  # type: ignore
    from integrations.streamdeck.paths import OwnershipError  # type: ignore


INTEGRATION_ID = "media-keys"
CONTENT_DIRS = ("config", "scripts")


def default_integration_root() -> Path:
    return drone_install_root() / "integrations" / INTEGRATION_ID


@dataclass(frozen=True)
class MediaKeysPaths:
    root: Path

    @classmethod
    def default(cls) -> "MediaKeysPaths":
        return cls(default_integration_root())

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.abspath(str(self.root))))

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "media-keys.json"

    @property
    def press_file(self) -> Path:
        return self.state_dir / "last-press.json"

    @property
    def hold_file(self) -> Path:
        return self.state_dir / "hold.json"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / "drone-media-key"

    def owned(self, *parts: str) -> Path:
        return self.assert_owned(self.root.joinpath(*parts))

    def assert_owned(self, target: Path) -> Path:
        target = Path(os.path.abspath(str(target)))
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise OwnershipError(f"path is outside the Media Keys integration directory: {target}") from error
        if self.root.parent.is_symlink() or self.root.is_symlink():
            raise OwnershipError("the Media Keys integration directory must not be a symbolic link")
        current = self.root
        for part in relative.parts:
            if part in ("", ".", ".."):
                raise OwnershipError("integration paths must be normalized")
            current = current / part
            if current.is_symlink():
                raise OwnershipError(f"refusing to follow a symbolic link inside the integration: {current}")
        return target

    def ensure(self, *directories: Path) -> None:
        for directory in (self.root, *directories):
            directory = self.assert_owned(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not directory.is_dir():
                raise OwnershipError(f"expected a directory: {directory}")

    def ensure_layout(self) -> None:
        self.ensure(self.config_dir, self.scripts_dir, self.state_dir, self.logs_dir, self.bin_dir)

    def remove_root(self, expected_root: Optional[Path] = None) -> bool:
        expected = Path(os.path.abspath(str(expected_root or default_integration_root())))
        if self.root != expected or self.root.name != INTEGRATION_ID or self.root.parent.name != "integrations":
            raise OwnershipError(f"refusing to remove an unexpected integration directory: {self.root}")
        self.assert_owned(self.root)
        if not self.root.exists():
            return False
        shutil.rmtree(self.root)
        return True
