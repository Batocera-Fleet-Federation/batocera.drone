#!/usr/bin/env bash
# -------------------------------------------------------------------
# create-release.sh – Batocera Drone release helper
#
# Creates a signed tag for the given version, updates CHANGELOG.md,
# pushes the tag, and optionally triggers the GitHub Actions release
# workflow.
#
# Usage:
#   ./scripts/create-release.sh v1.2.3              # dry-run by default
#   ./scripts/create-release.sh v1.2.3 --push       # push tag & trigger CI
#   ./scripts/create-release.sh v1.2.3 --dry-run    # explicit dry-run
#
# Requirements:
#   - git, gh (GitHub CLI), optionally jq for JSON parsing
# -------------------------------------------------------------------
set -euo pipefail

PROJECT="Batocera Drone"
REPO="Batocera-Fleet-Federation/batocera.drone"

# ── Colour helpers ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Parse arguments ─────────────────────────────────────────────────
VERSION=""
PUSH_MODE="dry-run"

for arg in "$@"; do
  case "$arg" in
    --push)     PUSH_MODE="push" ;;
    --dry-run)  PUSH_MODE="dry-run" ;;
    --no-push)  PUSH_MODE="dry-run" ;;
    --*)        error "Unknown option: $arg" ;;
    *)          VERSION="$arg" ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  echo "Usage: $0 <version> [--push|--dry-run]"
  echo ""
  echo "Examples:"
  echo "  $0 v1.2.3             # dry-run (default)"
  echo "  $0 v1.2.3 --push      # create tag, update CHANGELOG, push"
  exit 1
fi

# Validate version format
if ! echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+'; then
  error "Version must match vMAJOR.MINOR.PATCH (e.g., v1.2.3)"
fi

# ── Pre-flight checks ───────────────────────────────────────────────
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  error "Not inside a Git repository."
fi

if ! command -v gh &>/dev/null; then
  error "GitHub CLI (gh) is required. Install it from https://cli.github.com/"
fi

# Ensure we're on the default branch (main) and it's clean
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  error "Releases must be cut from the 'main' branch (currently on '$CURRENT_BRANCH')."
fi

if ! git diff-index --quiet HEAD --; then
  error "Working tree has uncommitted changes. Commit or stash them first."
fi

# Fetch latest tags
git fetch --tags origin 2>/dev/null || true

# Check if tag already exists
if git rev-parse "$VERSION" >/dev/null 2>&1; then
  error "Tag $VERSION already exists."
fi

# ── Summary ─────────────────────────────────────────────────────────
echo "══════════════════════════════════════════════════════════════"
echo "  $PROJECT Release"
echo "  Version : $VERSION"
echo "  Mode    : $PUSH_MODE"
echo "  Repo    : $REPO"
echo "══════════════════════════════════════════════════════════════"
echo ""

# ── Update CHANGELOG.md ────────────────────────────────────────────
CHANGELOG="CHANGELOG.md"
if [[ -f "$CHANGELOG" ]]; then
  # Find the Unreleased section and add a new heading for this version
  TODAY=$(date +%Y-%m-%d)
  # Insert "## [$VERSION] - $TODAY" after the "## [Unreleased]" line
  if grep -q "^## \[Unreleased\]" "$CHANGELOG"; then
    if [[ "$(uname)" == "Darwin" ]]; then
      sed -i '' "/^## \[Unreleased\]/a\\
## [$VERSION] - $TODAY
" "$CHANGELOG"
    else
      sed -i "/^## \[Unreleased\]/a ## [$VERSION] - $TODAY" "$CHANGELOG"
    fi
    info "Updated $CHANGELOG with version $VERSION"
  else
    warn "[Unreleased] section not found in $CHANGELOG. Skipping automatic update."
  fi
else
  warn "$CHANGELOG not found. Skipping."
fi

# ── Dry-run: just show what would happen ────────────────────────────
if [[ "$PUSH_MODE" == "dry-run" ]]; then
  echo ""
  info "DRY-RUN mode – no changes were pushed."
  info "To actually release, re-run with the --push flag:"
  echo ""
  echo "    $0 $VERSION --push"
  echo ""
  exit 0
fi

# ── Commit CHANGELOG changes (if any) ───────────────────────────────
if ! git diff-index --quiet HEAD -- "$CHANGELOG" 2>/dev/null; then
  git add "$CHANGELOG"
  git commit -m "chore: bump version to $VERSION"
  git push origin "$CURRENT_BRANCH"
  info "Committed and pushed $CHANGELOG update."
fi

# ── Create & push tag ───────────────────────────────────────────────
info "Creating tag: $VERSION"
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"

info "Tag $VERSION pushed."

# ── Create GitHub Release ───────────────────────────────────────────
if command -v gh &>/dev/null; then
  info "Creating GitHub Release..."
  gh release create "$VERSION" \
    --title "$VERSION" \
    --generate-notes \
    --repo "$REPO"
  info "GitHub Release created: https://github.com/$REPO/releases/tag/$VERSION"
else
  warn "GitHub CLI (gh) not available. Skipping release creation."
  warn "Create the release manually at https://github.com/$REPO/releases/new?tag=$VERSION"
fi

echo ""
info "Release $VERSION completed successfully!"