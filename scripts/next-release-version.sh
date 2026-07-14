#!/usr/bin/env bash
set -euo pipefail

SEMVER_PATTERN='^v([0-9]+)\.([0-9]+)\.([0-9]+)$'

LATEST_VERSION="${1:-}"
COMMIT_SUBJECT="${2:-}"

if [[ "$#" -lt 1 ]]; then
  LATEST_VERSION="$(
    git tag --sort=-v:refname \
      | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
      | head -n 1 \
      || true
  )"
fi

if [[ "$#" -lt 2 ]]; then
  COMMIT_SUBJECT="$(git log -1 --pretty=%s)"
fi

if [[ -z "$LATEST_VERSION" ]]; then
  printf '%s\n' "v0.0.1"
  exit 0
fi

if [[ ! "$LATEST_VERSION" =~ $SEMVER_PATTERN ]]; then
  printf 'Latest version must match vMAJOR.MINOR.PATCH: %s\n' "$LATEST_VERSION" >&2
  exit 1
fi

major="${BASH_REMATCH[1]}"
minor="${BASH_REMATCH[2]}"
patch="${BASH_REMATCH[3]}"
subject="${COMMIT_SUBJECT%%$'\n'*}"
subject="$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')"

case "$subject" in
  "increment major version"*)
    major=$((major + 1))
    minor=0
    patch=0
    ;;
  "incremenet patch version"*|"increment patch version"*)
    # Preserve the requested behavior: bump the middle component and reset
    # the final component (for example, v1.3.10 becomes v1.4.0).
    minor=$((minor + 1))
    patch=0
    ;;
  *)
    # Normal main-branch commits advance the final component.
    patch=$((patch + 1))
    ;;
esac

printf 'v%s.%s.%s\n' "$major" "$minor" "$patch"
