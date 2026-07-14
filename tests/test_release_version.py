import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "next-release-version.sh"
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"


def next_version(current: str, subject: str) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT), current, subject],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_normal_commit_advances_final_component():
    assert next_version("v0.1.50", "Fix transfer status") == "v0.1.51"
    assert next_version("v1.0.1", "Fix transfer status") == "v1.0.2"


def test_major_prefix_advances_major_and_resets_other_components():
    assert next_version("v0.1.50", "increment major version for stable release") == "v1.0.0"
    assert next_version("v1.3.10", "Increment Major Version") == "v2.0.0"


def test_requested_patch_prefix_advances_middle_component():
    assert next_version("v1.3.10", "incremenet patch version for API changes") == "v1.4.0"
    assert next_version("v1.3.10", "increment patch version for API changes") == "v1.4.0"


def test_missing_first_version_starts_at_v0_0_1():
    assert next_version("", "Initial release") == "v0.0.1"


def test_release_workflow_is_main_only_and_uploads_drone_assets():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "branches:\n      - main" in workflow
    assert "tags:" not in workflow
    assert "scripts/next-release-version.sh" in workflow
    assert "dist/drone-app.tar.gz" in workflow
    assert "scripts/batocera_install.sh" in workflow
    assert "refs/tags/latest --force" in workflow
