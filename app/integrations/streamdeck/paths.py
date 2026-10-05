"""The Stream Deck ownership boundary: every file this integration writes.

Everything lives below ``<install root>/integrations/streamdeck/`` (the install
root comes from ``common.install_paths.drone_install_root`` -- the same root
Torrents/VPN already use). The tree is split into two classes so lifecycle
operations stay narrowly scoped:

* tooling (``python/``, ``lib/``, ``rendered/``, ``state/``) -- dependencies,
  caches and runtime state. "Remove Tooling" and "Reinstall Tooling" only ever
  touch these.
* content (``config/``, ``scripts/``, ``images/``, ``logs/``) -- what the
  administrator created. Only "Remove Tooling + Configuration" deletes it, and
  it does so by removing the validated integration root itself.

Nothing here follows symbolic links: a symlinked component anywhere inside the
boundary is refused rather than written through or recursively deleted.
"""

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX development hosts
    fcntl = None  # type: ignore

try:
    from ...common.install_paths import drone_install_root
except ImportError:  # pragma: no cover - flat execution
    from common.install_paths import drone_install_root  # type: ignore


INTEGRATION_ID = "streamdeck"
TOOLING_DIRS = ("python", "lib", "rendered", "state")
CONTENT_DIRS = ("config", "scripts", "images", "logs")


class OwnershipError(ValueError):
    """A path escaped, or was redirected out of, the integration directory."""


def default_integration_root() -> Path:
    return drone_install_root() / "integrations" / INTEGRATION_ID


@dataclass(frozen=True)
class StreamDeckPaths:
    root: Path

    @classmethod
    def default(cls) -> "StreamDeckPaths":
        return cls(default_integration_root())

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(os.path.abspath(str(self.root))))

    # -- directories ------------------------------------------------------
    @property
    def python_dir(self) -> Path:
        return self.root / "python"

    @property
    def lib_dir(self) -> Path:
        return self.root / "lib"

    @property
    def rendered_dir(self) -> Path:
        return self.root / "rendered"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def scripts_dir(self) -> Path:
        return self.root / "scripts"

    @property
    def images_dir(self) -> Path:
        return self.root / "images"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def commands_dir(self) -> Path:
        return self.state_dir / "commands"

    @property
    def results_dir(self) -> Path:
        return self.state_dir / "results"

    @property
    def preview_dir(self) -> Path:
        return self.state_dir / "preview"

    # -- files ------------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.config_dir / "streamdeck.json"

    @property
    def runtime_document(self) -> Path:
        """Compiled, fully-resolved profile document the worker consumes."""
        return self.state_dir / "runtime.json"

    @property
    def worker_status_file(self) -> Path:
        return self.state_dir / "worker-status.json"

    @property
    def manager_status_file(self) -> Path:
        return self.state_dir / "manager-status.json"

    @property
    def dependency_marker(self) -> Path:
        return self.state_dir / "dependencies.json"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "runtime.pid"

    @property
    def runtime_lock(self) -> Path:
        return self.state_dir / "runtime.lock"

    @property
    def launch_lock(self) -> Path:
        return self.state_dir / "launch.lock"

    @property
    def runtime_log(self) -> Path:
        return self.logs_dir / "runtime.log"

    @property
    def runtime_console_log(self) -> Path:
        return self.logs_dir / "runtime-console.log"

    @property
    def install_log(self) -> Path:
        return self.logs_dir / "install.log"

    # -- ownership --------------------------------------------------------
    def owned(self, *parts: str) -> Path:
        """Return ``root/parts`` after proving it cannot escape the boundary."""
        target = self.root.joinpath(*parts)
        return self.assert_owned(target)

    def assert_owned(self, target: Path) -> Path:
        target = Path(os.path.abspath(str(target)))
        try:
            relative = target.relative_to(self.root)
        except ValueError as error:
            raise OwnershipError(f"path is outside the Stream Deck integration directory: {target}") from error
        current = self.root
        if self.root.parent.is_symlink():
            raise OwnershipError("the integrations directory must not be a symbolic link")
        if current.is_symlink():
            raise OwnershipError("the Stream Deck integration directory must not be a symbolic link")
        for part in relative.parts:
            if part in ("", ".", ".."):
                raise OwnershipError("integration paths must be normalized")
            current = current / part
            if current.is_symlink():
                raise OwnershipError(f"refusing to follow a symbolic link inside the integration: {current}")
        return target

    def ensure(self, *directories: Path) -> None:
        """Create the root plus the given owned directories (private, no symlinks)."""
        for directory in (self.root, *directories):
            directory = self.assert_owned(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not directory.is_dir():
                raise OwnershipError(f"expected a directory: {directory}")

    def ensure_layout(self) -> None:
        self.ensure(
            self.config_dir, self.scripts_dir, self.images_dir, self.logs_dir,
            self.rendered_dir, self.state_dir, self.commands_dir, self.results_dir, self.preview_dir,
        )

    def remove_owned_tree(self, target: Path) -> bool:
        """Delete one owned file or directory without following symlinks."""
        target = self.assert_owned(target)
        if target == self.root:
            raise OwnershipError("use remove_root() to delete the integration directory")
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            return True
        if target.is_dir():
            shutil.rmtree(target)
            return True
        return False

    def remove_root(self, expected_root: Optional[Path] = None) -> bool:
        """Delete the whole integration directory, only at its expected location."""
        expected = Path(os.path.abspath(str(expected_root or default_integration_root())))
        if self.root != expected or self.root.name != INTEGRATION_ID or self.root.parent.name != "integrations":
            raise OwnershipError(f"refusing to remove an unexpected integration directory: {self.root}")
        self.assert_owned(self.root)
        if not self.root.exists():
            return False
        shutil.rmtree(self.root)
        return True


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(raw, mode)
        os.replace(raw, path)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    atomic_write_bytes(path, data.encode("utf-8"), mode=mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.is_symlink():
            return default
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


@contextmanager
def try_file_lock(path: Path) -> Iterator[bool]:
    """Non-blocking exclusive ``flock``; yields whether it was acquired.

    Cross-process: the worker and the Drone process coordinate game launches
    and single-instance runtime ownership through these lock files.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    acquired = False
    try:
        if fcntl is None:  # pragma: no cover
            acquired = True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, PermissionError):
                acquired = False
        yield acquired
    finally:
        if acquired and fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def lock_is_held(path: Path) -> bool:
    """True when another open file description holds ``path``'s flock."""
    if not path.exists() or path.is_symlink():
        return False
    with try_file_lock(path) as acquired:
        return not acquired
