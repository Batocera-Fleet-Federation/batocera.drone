#!/usr/bin/env bash
set -euo pipefail

PROJECT="Batocera Drone"
REPO="Batocera-Fleet-Federation/batocera.drone"
DEFAULT_BRANCH="main"
LATEST_TAG="latest"
INSTALL_SCRIPT="scripts/batocera_install.sh"

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
  echo "  $0 v0.0.6"
  echo "  $0 v0.0.6 --push"
  exit 1
fi

if ! echo "$VERSION" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
  error "Version must match vMAJOR.MINOR.PATCH, example: v0.0.6"
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  error "Not inside a Git repository."
fi

if ! command -v gh >/dev/null 2>&1; then
  error "GitHub CLI gh is required."
fi

if [[ ! -f "$INSTALL_SCRIPT" ]]; then
  error "Install script not found: $INSTALL_SCRIPT"
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

CHANGELOG="CHANGELOG.md"
TODAY="$(date +%Y-%m-%d)"

echo "══════════════════════════════════════════════════════════════"
echo "  $PROJECT Release"
echo "  Version        : $VERSION"
echo "  Latest Tag     : $LATEST_TAG"
echo "  Install Asset  : $INSTALL_SCRIPT"
echo "  Mode           : $PUSH_MODE"
echo "  Repo           : $REPO"
echo "══════════════════════════════════════════════════════════════"
echo ""

update_changelog() {
  if [[ ! -f "$CHANGELOG" ]]; then
    warn "$CHANGELOG not found. Skipping changelog update."
    return
  fi

  if grep -qE "^## \[$VERSION\]" "$CHANGELOG"; then
    warn "$VERSION already exists in $CHANGELOG. Skipping changelog update."
    return
  fi

  PREVIOUS_TAG="$(git tag --sort=-version:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)"

  if [[ -n "$PREVIOUS_TAG" ]]; then
    NOTES="$(git log "${PREVIOUS_TAG}..HEAD" --pretty=format:'- %s' || true)"
  else
    NOTES="$(git log --pretty=format:'- %s' || true)"
  fi

  if [[ -z "$NOTES" ]]; then
    NOTES="- Initial release."
  fi

  TMP_FILE="$(mktemp)"

  {
    echo "# Changelog"
    echo ""
    echo "## [$VERSION] - $TODAY"
    echo ""
    echo "$NOTES"
    echo ""
  } > "$TMP_FILE"

  if grep -qE '^# Changelog' "$CHANGELOG"; then
    tail -n +3 "$CHANGELOG" >> "$TMP_FILE"
  else
    cat "$CHANGELOG" >> "$TMP_FILE"
  fi

  mv "$TMP_FILE" "$CHANGELOG"
  info "Updated $CHANGELOG"
}

update_changelog

if ! git diff --quiet -- "$CHANGELOG" 2>/dev/null; then
  info "Committing changelog update..."
  git add "$CHANGELOG"

  if [[ "$PUSH_MODE" == "push" ]]; then
    git commit -m "Update changelog for $VERSION"
  else
    warn "Dry-run: would commit changelog update."
    git diff --cached -- "$CHANGELOG"
    git reset -- "$CHANGELOG" >/dev/null
  fi
fi

RELEASE_NOTES_FILE="$(mktemp)"
PREVIOUS_TAG="$(git tag --sort=-version:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)"

{
  echo "$PROJECT $VERSION"
  echo ""
  if [[ -n "$PREVIOUS_TAG" ]]; then
    echo "Changes since $PREVIOUS_TAG:"
    echo ""
    git log "${PREVIOUS_TAG}..HEAD" --pretty=format:'- %s' || true
  else
    echo "Initial release."
  fi
} > "$RELEASE_NOTES_FILE"

if [[ "$PUSH_MODE" == "dry-run" ]]; then
  warn "Dry-run only. No tag, release, asset upload, or push will be created."
  echo ""
  echo "Would create tag:"
  echo "  git tag -a $VERSION -m \"$PROJECT $VERSION\""
  echo ""
  echo "Would push:"
  echo "  git push origin $DEFAULT_BRANCH"
  echo "  git push origin $VERSION"
  echo ""
  echo "Would create GitHub release:"
  echo "  gh release create $VERSION --repo $REPO --title \"$PROJECT $VERSION\" --notes-file $RELEASE_NOTES_FILE"
  echo ""
  echo "Would upload release asset:"
  echo "  gh release upload $VERSION $INSTALL_SCRIPT --repo $REPO --clobber"
  rm -f "$RELEASE_NOTES_FILE"
  exit 0
fi

info "Creating annotated tag $VERSION..."
git tag -a "$VERSION" -m "$PROJECT $VERSION"

info "Pushing branch and tag..."
git push origin "$DEFAULT_BRANCH"
git push origin "$VERSION"

info "Creating GitHub release..."
gh release create "$VERSION" \
  --repo "$REPO" \
  --title "$PROJECT $VERSION" \
  --notes-file "$RELEASE_NOTES_FILE"

info "Uploading install script release asset..."
gh release upload "$VERSION" \
  "$INSTALL_SCRIPT" \
  --repo "$REPO" \
  --clobber

if git rev-parse "$LATEST_TAG" >/dev/null 2>&1; then
  info "Updating local $LATEST_TAG tag..."
  git tag -d "$LATEST_TAG" >/dev/null
fi

info "Creating/updating $LATEST_TAG tag..."
git tag -a "$LATEST_TAG" -m "$PROJECT latest release: $VERSION"

if git ls-remote --exit-code --tags origin "refs/tags/$LATEST_TAG" >/dev/null 2>&1; then
  info "Deleting remote $LATEST_TAG tag..."
  git push origin ":refs/tags/$LATEST_TAG"
fi

git push origin "$LATEST_TAG"

rm -f "$RELEASE_NOTES_FILE"

info "Release complete: $VERSION"