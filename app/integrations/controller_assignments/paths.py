"""The Controller Assignments ownership boundary.

Everything lives below ``<install root>/integrations/controller-assignments/``
and nothing follows symlinks: ``config/`` is administrator content; ``bin/``
(the launch wrapper), ``state/`` (compiled runtime, last-launch report) and
``logs/`` are regenerated. The only file written outside this tree is the
EmulationStation command overlay (see ``es_overlay.py``).
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


INTEGRATION_ID = "controller-assignments"


def default_integration_root() -> Path:
    return drone_install_root() / "integrations" / INTEGRATION_ID


@dataclass(frozen=True)
class ControllerAssignmentsPaths:
    root: Path

    @classmethod
    def default(cls) -> "ControllerAssignmentsPaths":
        return cls(default_integration_root())

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.abspath(str(self.root))))

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "controller-assignments.json"

    @property
    def runtime_file(self) -> Path:
        return self.state_dir / "runtime.json"

    @property
    def report_file(self) -> Path:
        return self.state_dir / "last-launch.json"

    @property
    def wrapper(self) -> Path:
        return self.bin_dir / "drone-controller-order"

    def owned(self, *parts: str) -> Path:
        return self.assert_owned(self.root.joinpath(*parts))

    def assert_owned(self, target: Path) -> Path:
        target = Path(os.path.abspath(str(target)))
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise OwnershipError(f"path is outside the Controller Assignments directory: {target}") from error
        if self.root.parent.is_symlink() or self.root.is_symlink():
            raise OwnershipError("the Controller Assignments directory must not be a symbolic link")
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
            directory.mkdir(parents=True, exist_ok=True, mode=0o755)
            if not directory.is_dir():
                raise OwnershipError(f"expected a directory: {directory}")

    def ensure_layout(self) -> None:
        # 0755: the wrapper runs as root from EmulationStation and must reach
        # bin/ and state/ (the Drone may run as another user).
        self.ensure(self.config_dir, self.state_dir, self.bin_dir)

    def remove_root(self, expected_root: Optional[Path] = None) -> bool:
        expected = Path(os.path.abspath(str(expected_root or default_integration_root())))
        if self.root != expected or self.root.name != INTEGRATION_ID or self.root.parent.name != "integrations":
            raise OwnershipError(f"refusing to remove an unexpected integration directory: {self.root}")
        self.assert_owned(self.root)
        if not self.root.exists():
            return False
        shutil.rmtree(self.root)
        return True
