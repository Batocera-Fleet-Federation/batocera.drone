#!/usr/bin/env bash
set -euo pipefail

PROJECT="Batocera Drone"
REPO="Batocera-Fleet-Federation/batocera.drone"
DEFAULT_BRANCH="main"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

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
  echo "  $0 v0.0.1"
  echo "  $0 v0.0.1 --push"
  exit 1
fi

if ! echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  error "Version must match vMAJOR.MINOR.PATCH, example: v0.0.1"
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  error "Not inside a Git repository."
fi

if ! command -v gh >/dev/null 2>&1; then
  error "GitHub CLI gh is required."
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]]; then
  error "Releases must be cut from '$DEFAULT_BRANCH'. Current branch: '$CURRENT_BRANCH'"
fi

if ! git diff-index --quiet HEAD --; then
  error "Working tree has uncommitted changes. Commit or stash them first."
fi

info "Fetching latest refs and tags..."
git fetch origin "$DEFAULT_BRANCH" --tags --quiet

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/$DEFAULT_BRANCH")"

if [[ "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
  error "Local $DEFAULT_BRANCH is not up to date with origin/$DEFAULT_BRANCH. Pull/rebase first."
fi

if git rev-parse "$VERSION" >/dev/null 2>&1; then
  error "Local tag $VERSION already exists."
fi

if git ls-remote --exit-code --tags origin "refs/tags/$VERSION" >/dev/null 2>&1; then
  error "Remote tag $VERSION already exists."
fi

if gh release view "$VERSION" --repo "$REPO" >/dev/null 2>&1; then
  error "GitHub Release $VERSION already exists."
fi

echo "══════════════════════════════════════════════════════════════"
echo "  $PROJECT Release"
echo "  Version : $VERSION"
echo "  Mode    : $PUSH_MODE"
echo "  Repo    : $REPO"
echo "══════════════════════════════════════════════════════════════"
echo ""

CHANGELOG="CHANGELOG.md"
TODAY="$(date +%Y-%m-%d)"

update_changelog() {
  if [[ ! -f "$CHANGELOG" ]]; then
    warn "$CHANGELOG not found. Skipping changelog update."
    return
  fi

  if grep -qE "^## \[$VERSION\]" "$CHANGELOG"; then
    warn "$VERSION already exists in $CHANGELOG. Skipping changelog update."
    return
  fi

  if ! grep -qE "^## \[Unreleased\]" "$CHANGELOG"; then
    warn "[Unreleased] section not found in $CHANGELOG. Skipping changelog update."
    return
  fi

  tmp_file="$(mktemp)"

  awk -v version="$VERSION" -v today="$TODAY" '
    /^## \[Unreleased\]/ {
      print
      print ""
      print "## [" version "] - " today
      next
    }
    { print }
  ' "$CHANGELOG" > "$tmp_file"

  mv "$tmp_file" "$CHANGELOG"

  info "Updated $CHANGELOG with $VERSION"
}

get_previous_tag() {
  git tag --sort=-v:refname \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
    | grep -v "^${VERSION}$" \
    | head -1 || true
}

if [[ "$PUSH_MODE" == "dry-run" ]]; then
  PREVIOUS_TAG="$(get_previous_tag)"

  info "DRY-RUN mode. No files, tags, or releases will be created."
  info "Would validate repo, update CHANGELOG.md, commit, tag, push, and create GitHub Release."

  if [[ -n "$PREVIOUS_TAG" ]]; then
    info "Would generate release notes from $PREVIOUS_TAG to $VERSION."
  else
    info "No previous version tag found. Would generate initial release notes."
  fi

  echo ""
  echo "To release:"
  echo "  $0 $VERSION --push"
  exit 0
fi

update_changelog

if [[ -f "$CHANGELOG" ]] && ! git diff --quiet -- "$CHANGELOG"; then
  git add "$CHANGELOG"
  git commit -m "chore: bump version to $VERSION"
  git push origin "$DEFAULT_BRANCH"
  info "Committed and pushed $CHANGELOG update."
else
  info "No changelog changes to commit."
fi

info "Creating annotated tag: $VERSION"
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"

PREVIOUS_TAG="$(get_previous_tag)"

info "Creating GitHub Release..."

if [[ -n "$PREVIOUS_TAG" ]]; then
  info "Generating release notes from $PREVIOUS_TAG to $VERSION."

  gh release create "$VERSION" \
    --title "$VERSION" \
    --generate-notes \
    --notes-start-tag "$PREVIOUS_TAG" \
    --repo "$REPO"
else
  info "No previous version tag found. Creating initial generated release notes."

  gh release create "$VERSION" \
    --title "$VERSION" \
    --generate-notes \
    --repo "$REPO"
fi

info "Release created: https://github.com/$REPO/releases/tag/$VERSION"
info "Release $VERSION completed successfully."