#!/usr/bin/env python3
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.mock_data import seed_mock_userdata
from app.rom_api import Settings, create_server


def main() -> None:
    mock_root = Path(os.environ.get("MOCK_DATA_ROOT", ROOT / "local-data" / "mock-userdata"))
    seed_mock_userdata(mock_root)

    os.environ.setdefault("USERDATA_ROOT", str(mock_root))
    os.environ.setdefault("ROMS_ROOT", str(mock_root / "roms"))
    os.environ.setdefault("BIOS_ROOT", str(mock_root / "bios"))
    os.environ.setdefault("THEMES_ROOT", str(mock_root / "themes"))
    os.environ.setdefault("BATOCERA_CONF_FILE", str(mock_root / "system" / "batocera.conf"))
    os.environ.setdefault(
        "ES_SETTINGS_FILE",
        str(mock_root / "system" / "configs" / "emulationstation" / "es_settings.cfg"),
    )
    os.environ.setdefault("DRONE_APP_USERNAME", "admin")
    os.environ.setdefault("DRONE_APP_PASSWORD", "changeme")
    os.environ.setdefault("HTTPS_PORT", "8080")
    os.environ.setdefault("HTTP_ONLY", "1")
    os.environ.setdefault("LOG_DIR", str(ROOT / "local-data" / "logs"))
    os.environ.setdefault("ALLOW_CONTENT_DOWNLOAD", "true")

    settings = Settings.from_env()
    server = create_server(settings)
    print(f"Mock data root: {mock_root}")
    print(f"Mock server running on http://127.0.0.1:{settings.https_port}")
    print("Auth username: admin")
    print("Auth password: changeme")
    server.serve_forever()


if __name__ == "__main__":
    main()
