"""A paired peer's display name must not be able to forge digest attribution.

Issue #740: "Update the email and notification to be clear about who
downloaded from who."

The digest's new per-source ("from <peer>") and per-target ("to <peer>")
group lines embed the peer-supplied display name verbatim. Paired-peer
records store the remote `name`/`hostname` exactly as the peer sent it
(`_handle_peer_pair`, `_local_pair_peer`, swarm gossip cards), and the new
label helpers only `.strip()` it, so an embedded newline forges extra
digest lines -- falsifying exactly the who-downloaded-from-who record the
issue adds. Each emitted digest line must therefore be a single physical
line no matter what a peer named itself.
"""

import unittest

import app.device.smtp_manager as smtp_manager
from app.transfer.download_manager import _peer_label


def _download_row(source_name):
    return {
        "event_type": "asset_downloaded",
        "details": {
            "source_drone_name": source_name,
            "source_drone_id": "attacker-id",
            "system": "snes",
            "size_bytes": 1024,
        },
    }


def _upload_row(target_name):
    return {
        "event_type": "asset_uploaded",
        "details": {
            "target_drone_name": target_name,
            "target_drone_id": "attacker-id",
            "system": "snes",
            "size_bytes": 1024,
        },
    }


class DigestPeerLabelForgeryTests(unittest.TestCase):
    def test_download_source_with_newline_cannot_forge_digest_lines(self):
        forged = "attacker\n    from TRUSTED-DRONE: 42 assets (9.9 GB)"
        lines = smtp_manager._asset_summary_lines([_download_row(forged)])
        for line in lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
        body = "\n".join(lines)
        self.assertNotIn("from TRUSTED-DRONE", body)

    def test_upload_target_with_newline_cannot_forge_digest_lines(self):
        forged = "attacker\n    to TRUSTED-DRONE: 42 assets (9.9 GB)"
        lines = smtp_manager._asset_summary_lines([_upload_row(forged)])
        for line in lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("\r", line)
        body = "\n".join(lines)
        self.assertNotIn("to TRUSTED-DRONE", body)

    def test_peer_label_carries_no_line_breaks(self):
        label = _peer_label({
            "name": "attacker\nfrom TRUSTED-DRONE",
            "hostname": "attacker-host",
            "drone_id": "attacker-id",
        })
        self.assertNotIn("\n", label)
        self.assertNotIn("\r", label)


if __name__ == "__main__":
    unittest.main()
