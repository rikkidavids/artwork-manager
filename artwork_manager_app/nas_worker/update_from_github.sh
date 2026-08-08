#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BRANCH="${AMW_GIT_BRANCH:-main}"

if ! command -v git >/dev/null 2>&1; then
  echo "ERROR: git was not found on this NAS." >&2
  echo "Install Synology's Git package, or update by copying the nas_worker folder manually." >&2
  exit 1
fi

REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  cat >&2 <<EOF
ERROR: this nas_worker folder is not inside a Git checkout yet.

First-time setup on the NAS:
  cd /volume1/docker
  git clone --filter=blob:none --sparse --branch $BRANCH https://github.com/rikkidavids/artwork-manager.git artwork-manager
  cd artwork-manager
  git sparse-checkout set artwork_manager_app/nas_worker
  cd artwork_manager_app/nas_worker
  chmod +x update_worker.sh update_from_github.sh
  cp .env.example .env
  vi .env
  ./update_worker.sh

After that, future updates can use:
  ./update_from_github.sh
EOF
  exit 1
fi

echo "Artwork Manager NAS Worker GitHub update"
echo "Repository: $REPO_ROOT"
echo "Branch: $BRANCH"

git -C "$REPO_ROOT" fetch origin "$BRANCH"

CURRENT_BRANCH="$(git -C "$REPO_ROOT" branch --show-current 2>/dev/null || true)"
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  echo "Switching checkout from ${CURRENT_BRANCH:-detached} to $BRANCH..."
  git -C "$REPO_ROOT" checkout "$BRANCH"
fi

git -C "$REPO_ROOT" pull --ff-only origin "$BRANCH"

echo
echo "Source updated. Rebuilding the NAS worker container..."
exec "$SCRIPT_DIR/update_worker.sh"
