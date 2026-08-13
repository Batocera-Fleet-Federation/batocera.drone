#!/usr/bin/env python3
"""Entry point for the Batocera Drone Ports client -- what the Ports launcher runs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client.config import ClientConfig  # noqa: E402
from client.http_client import DroneApiClient  # noqa: E402
from ui.app import PortsClientApp  # noqa: E402


def main() -> None:
    config = ClientConfig.from_env()
    api_client = DroneApiClient(config)
    PortsClientApp(api_client).run()


if __name__ == "__main__":
    main()
