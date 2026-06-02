import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent


def drone_app_version() -> str:
    env_version = (os.environ.get("DRONE_APP_VERSION") or "").strip()
    if env_version and env_version != "dev":
        return env_version
    try:
        version = (APP_DIR / "VERSION").read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
        if version:
            return version
    except Exception:
        pass
    return env_version or "dev"
