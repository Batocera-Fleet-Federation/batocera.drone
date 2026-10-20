#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-ghcr.io/batocera-fleet-federation/batocera-drone}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
DRY_RUN="false"
PUSH="false"
VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    --push)
      PUSH="true"
      shift
      ;;
    --version)
      if [[ $# -lt 2 ]]; then
        echo "--version requires a value" >&2
        exit 2
      fi
      VERSION="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  # Capture all tags before taking the first line: `git | head -n 1` can SIGPIPE
  # git once head exits, which pipefail turns into a failed validation (exit 141).
  tags="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname)"
  latest_tag="${tags%%$'\n'*}"
  if [[ -z "$latest_tag" ]]; then
    VERSION="v0.1.0"
  else
    base="${latest_tag#v}"
    IFS=. read -r major minor patch <<<"$base"
    VERSION="v${major}.${minor}.$((patch + 1))"
  fi
fi
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Version must match vMAJOR.MINOR.PATCH: $VERSION" >&2
  exit 2
fi

cmd=(docker buildx build --platform "$PLATFORMS" -t "$IMAGE_NAME:$VERSION" -t "$IMAGE_NAME:latest" .)
if [[ "$PUSH" == "true" ]]; then
  cmd+=(--push)
fi

echo "Image: $IMAGE_NAME"
echo "Version: $VERSION"
echo "Platforms: $PLATFORMS"
echo "Push: $PUSH"
echo "${cmd[@]}"
if [[ "$DRY_RUN" != "true" ]]; then
  "${cmd[@]}"
fi
