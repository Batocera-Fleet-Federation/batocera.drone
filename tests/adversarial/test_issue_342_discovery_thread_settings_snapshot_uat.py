"""UAT for #342: a running LAN discovery worker owns its Settings DB snapshot.

The worker receives an announcement asynchronously.  Its state write must use
the database captured in its injected Settings object, rather than re-reading
the mutable process environment between worker startup and packet receipt.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.common.settings import Settings
from app.storage import state_store
from app.transfer import local_network


class DiscoveryThreadSettingsSnapshotUAT(unittest.TestCase):
    def test_received_announcement_stays_in_starting_settings_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-342-discovery-") as temporary:
            root = Path(temporary)
            worker_database = root / "worker-state.sqlite3"
            later_database = root / "later-state.sqlite3"
            with mock.patch.dict(
                os.environ,
                {
                    "USERDATA_ROOT": str(root / "userdata"),
                    "DRONE_DEVICE_ID": "issue-342-worker",
                    "DRONE_STATE_DATABASE_FILE": str(worker_database),
                },
                clear=True,
            ):
                settings = Settings.from_env()

            packet = json.dumps(
                {
                    "service": local_network.DISCOVERY_SERVICE,
                    "kind": "announce",
                    "drone_id": "issue-342-peer",
                    "name": "Issue 342 peer",
                    "scheme": "https",
                    "api_port": 8543,
                }
            ).encode("utf-8")
            stop = threading.Event()

            class OneAnnouncementSocket:
                def __init__(self, *_args: object) -> None:
                    self.delivered = False

                def setsockopt(self, *_args: object) -> None:
                    pass

                def bind(self, _address: object) -> None:
                    pass

                def settimeout(self, _timeout: float) -> None:
                    pass

                def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
                    if not self.delivered:
                        self.delivered = True
                        return packet, ("198.51.100.42", 42042)
                    stop.set()
                    raise socket.timeout()

                def close(self) -> None:
                    pass

            # Simulate a second harness taking ownership of the process env
            # after this worker was configured but before it handles a packet.
            with mock.patch.dict(
                os.environ,
                {"DRONE_STATE_DATABASE_FILE": str(later_database)},
                clear=True,
            ), mock.patch.object(local_network.socket, "socket", OneAnnouncementSocket), mock.patch.object(
                local_network, "_join_multicast_group"
            ), mock.patch.object(local_network, "announce", return_value=True):
                worker = local_network.start_discovery_worker(settings, lambda: "", stop_event=stop)
                worker.join(timeout=5)

            self.assertFalse(worker.is_alive(), "discovery worker did not exit after its deterministic packet")
            peers = state_store.load_payload(worker_database, "local_discovered_peers", {})
            self.assertEqual(peers["issue-342-peer"]["source_ip"], "198.51.100.42")
            self.assertTrue(worker_database.is_file())
            self.assertFalse(later_database.exists())


if __name__ == "__main__":
    unittest.main()
