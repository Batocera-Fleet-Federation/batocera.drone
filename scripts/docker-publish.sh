#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-ghcr.io/batocera-fleet-federation/batocera-drone}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
DRY_RUN="false"
PUSH="false"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN="true" ;;
    --push) PUSH="true" ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

latest_tag="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 1)"
if [[ -z "$latest_tag" ]]; then
  version="v0.1.0"
else
  base="${latest_tag#v}"
  IFS=. read -r major minor patch <<<"$base"
  version="v${major}.${minor}.$((patch + 1))"
fi

cmd=(docker buildx build --platform "$PLATFORMS" -t "$IMAGE_NAME:$version" -t "$IMAGE_NAME:latest" .)
if [[ "$PUSH" == "true" ]]; then
  cmd+=(--push)
fi

echo "Image: $IMAGE_NAME"
echo "Version: $version"
echo "Platforms: $PLATFORMS"
echo "Push: $PUSH"
echo "${cmd[@]}"
if [[ "$DRY_RUN" != "true" ]]; then
  "${cmd[@]}"
fi
