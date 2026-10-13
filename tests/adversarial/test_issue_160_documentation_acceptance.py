"""Issue #160 documentation acceptance (requirement 15).

The README must carry the user-facing clone workflow, and docs/drone-identity.md the
operational detail an administrator needs: what is checked and why, when identity
regenerates, what is preserved and cleared, what happens to trusted peers, Tailscale
behaviour, limitations, ambiguity diagnostics and hardware-replacement recovery.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


class IdentityDocumentationTests(unittest.TestCase):
    def assertMentions(self, text: str, label: str, *patterns: str) -> None:
        for pattern in patterns:
            self.assertRegex(text, re.compile(pattern, re.IGNORECASE | re.DOTALL), msg=f"{label}: missing /{pattern}/")

    def test_readme_explains_the_no_special_steps_workflow(self) -> None:
        readme = read("README.md")
        self.assertMentions(
            readme,
            "README",
            r"image the batocera disk",
            r"restore the image",
            r"boot",
            r"automatically",
            r"pair the new drone",
            r"no special imaging mode",
            r"docs/drone-identity\.md",
        )

    def test_identity_guide_covers_every_required_topic(self) -> None:
        guide = read("docs/drone-identity.md")
        self.assertMentions(guide, "guide/hardware", r"DMI", r"Raspberry Pi", r"SHA-256", r"fingerprint_version",
                            r"hostname", r"machine-id", r"MAC", r"disk")
        self.assertMentions(guide, "guide/regeneration", r"different hardware", r"confident|high-confidence", r"missing\W+identifier is never")
        self.assertMentions(guide, "guide/preserved", r"preserved", r"ROMs", r"credentials")
        self.assertMentions(guide, "guide/cleared", r"cleared", r"paired peers", r"pinned", r"membership", r"tombstones")
        self.assertMentions(guide, "guide/trusted peers", r"original.{0,200}(keeps|retain)", r"(never|nothing).{0,80}(announce|told|notif)")
        self.assertMentions(guide, "guide/tailscale", r"Tailscale", r"logout", r"auth key")
        self.assertMentions(guide, "guide/limits", r"limitation", r"virtual", r"spoof", r"already.{0,20}cloned")
        self.assertMentions(guide, "guide/diagnose", r"identity/status", r"unavailable_reason", r"RECOVERY_REQUIRED")
        self.assertMentions(guide, "guide/replacement", r"authorize-hardware-change", r"password", r"resolve")
        self.assertMentions(guide, "guide/collision", r"duplicate", r"never revokes|never.{0,40}revoke")

    def test_documentation_never_instructs_logging_secrets_or_raw_serials(self) -> None:
        guide = read("docs/drone-identity.md")
        self.assertRegex(guide, re.compile(r"raw serials are never logged", re.IGNORECASE))


if __name__ == "__main__":
    unittest.main()
