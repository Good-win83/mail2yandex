#!/usr/bin/env bash
# Update private GitHub checkout and recreate Mail2Yandex Docker service.
# Run as the Unix user who owns this checkout and has access to Docker.
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
ROOT="$PWD"

if ! command -v git >/dev/null 2>&1 || ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: git and Docker are required" >&2
  exit 1
fi

if [[ "$(git rev-parse --show-toplevel 2>/dev/null)" != "$ROOT" ]]; then
  echo "ERROR: run script from the Mail2Yandex Git checkout" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing. Copy .env.example and set credentials first." >&2
  exit 1
fi

if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "ERROR: checkout main branch before updating" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "ERROR: working tree has uncommitted/untracked files; aborting" >&2
  git status --short >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 is required" >&2
  exit 1
fi

# Prevent overlapping deploys.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$ROOT/.git/mail2yandex-update.lock"
  flock -n 9 || { echo "ERROR: update already running" >&2; exit 1; }
fi

OLD_REV="$(git rev-parse HEAD)"
echo "Current revision: $OLD_REV"
echo "Fetching latest main from private repository..."
GIT_TERMINAL_PROMPT=0 git fetch --no-tags origin main
NEW_REV="$(git rev-parse FETCH_HEAD)"
if [[ "$NEW_REV" == "$OLD_REV" ]]; then
  echo "Already at latest revision."
else
  # Refuse force-pushed/non-fast-forward history.
  git merge-base --is-ancestor "$OLD_REV" "$NEW_REV" || {
    echo "ERROR: remote history is not a fast-forward; manual intervention required" >&2
    exit 1
  }
  git merge --ff-only "$NEW_REV"
fi

echo "Validating Docker Compose configuration..."
docker compose config --quiet
echo "Building Docker image..."
docker compose build
echo "Updating service..."
docker compose up -d --remove-orphans
echo "Service status:"
docker compose ps
echo "Updated from $OLD_REV to $(git rev-parse HEAD)"
echo "Note: if Docker build/start failed, inspect the logs; the script does not roll back automatically."
