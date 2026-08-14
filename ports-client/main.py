#!/usr/bin/env python3
"""Entry point for the Batocera Drone Ports client -- what the Ports launcher runs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import logging_setup  # noqa: E402

# Before any other import that might print (including a failure importing
# one of them) -- this is the whole point, an early crash should still land
# in the shared log file, not vanish into whatever EmulationStation does
# with a Ports script's stdout/stderr (nothing persistent, in practice).
logging_setup.configure()

from client.config import ClientConfig  # noqa: E402
from client.http_client import DroneApiClient  # noqa: E402
from ui.app import PortsClientApp  # noqa: E402


def main() -> None:
    print("ports-client starting")
    config = ClientConfig.from_env()
    api_client = DroneApiClient(config)
    try:
        PortsClientApp(api_client).run()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
    finally:
        print("ports-client exiting")


if __name__ == "__main__":
    main()
