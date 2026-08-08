#!/bin/sh
set -eu

cd "$(dirname "$0")"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "ERROR: Docker Compose was not found. Run this from Synology Container Manager/Terminal with Docker installed." >&2
  exit 1
fi

echo "Artwork Manager NAS Worker 5.04 rebuild/update"
echo "Project folder: $(pwd)"
echo "This rebuilds the image and recreates the container. A plain restart is not enough after code changes."

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo
  echo "Created local .env from .env.example."
  echo "Edit .env to set AMW_TOKEN and AMW_MUSIC_PATH before using the worker for real."
fi

if [ -f .env ] && grep -q '^AMW_TOKEN=change-me$' .env; then
  echo
  echo "WARNING: .env still uses AMW_TOKEN=change-me. Use a private token and paste the same value into the Mac app Settings."
fi

mkdir -p backups

$COMPOSE down --remove-orphans || true
$COMPOSE build --no-cache --pull
$COMPOSE up -d --force-recreate

echo
echo "Container status:"
$COMPOSE ps

echo
echo "Local worker root endpoint, if run on the NAS:"
echo "  curl -s http://127.0.0.1:8765/"
echo
echo "From your Mac, check:"
echo "  http://YOUR-NAS-IP:8765/"
echo
echo "You should see worker_build 5.04 and api 3. If not, Synology is still serving an older container/image."
