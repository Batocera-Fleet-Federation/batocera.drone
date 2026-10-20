"""Issue #333 UAT: inventory signing only admits regular source files.

A peer can request an inventory for any local asset-relative path.  A FIFO is
not ROM/BIOS content: attempting to hash it waits for a writer indefinitely.
The signed-inventory endpoint must reject it before hashing, exactly as it
rejects non-regular children while walking a folder inventory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.transfer import signed_inventory
from app.web.handlers_peer import HandlersPeerMixin


class Issue333InventorySourceSpecialFileUAT(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_fifo_request_is_rejected_without_opening_or_hashing_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="issue-333-special-file-") as temporary:
            root = Path(temporary)
            fifo = root / "blocked.rom"
            os.mkfifo(fifo)
            handler = SimpleNamespace(
                repository=SimpleNamespace(
                    get_local_system_dir=lambda _system: root,
                    get_bios_root=lambda: root,
                    is_local_bios_path=lambda _relative_path: True,
                ),
                settings=SimpleNamespace(device_id="issue-333-source", drone_key_file=root / "unused.key"),
                _peer_request_authorized=mock.Mock(return_value=True),
                _send_json=mock.Mock(),
            )
            query = {
                "system": ["nes"],
                "relative_path": [fifo.name],
                "nonce": ["ab" * 32],
            }

            with mock.patch.object(signed_inventory, "_require_swarm_networking"), \
                    mock.patch.object(signed_inventory, "file_digest", side_effect=AssertionError("FIFO must not be opened")) as digest:
                HandlersPeerMixin._handle_peer_content_inventory(handler, "roms", query)

            digest.assert_not_called()
            handler._send_json.assert_called_once_with(400, {"error": "invalid content inventory request"})


if __name__ == "__main__":
    unittest.main()
