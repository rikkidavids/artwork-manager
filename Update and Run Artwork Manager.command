#!/bin/zsh
set -euo pipefail

on_error() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo
    echo "Update or launch failed."
    echo "Press Return to close this window."
    read -r _
  fi
}
trap on_error EXIT

# Resolve symlinks so a Desktop shortcut can point at this repo script.
SCRIPT_PATH="${0:A}"
REPO_DIR="${SCRIPT_PATH:h}"
cd "$REPO_DIR"

echo "Artwork Manager"
echo "App folder: $REPO_DIR"
echo

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed or is not available in Terminal."
  exit 1
fi

if [ ! -d ".git" ]; then
  echo "This shortcut must point to an artwork-manager Git checkout."
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Local changes were found, so the updater stopped before pulling."
  echo
  git status --short
  echo
  echo "Commit, stash, or discard those local changes first, then run this again."
  exit 1
fi

echo "Fetching latest changes..."
git fetch origin

if git show-ref --verify --quiet refs/heads/qt-prototype; then
  echo "Switching to qt-prototype..."
  git switch qt-prototype
else
  echo "Creating local qt-prototype branch..."
  git switch -c qt-prototype --track origin/qt-prototype
fi

echo "Pulling latest qt-prototype updates..."
git pull --ff-only origin qt-prototype

if ! command -v python3.11 >/dev/null 2>&1; then
  echo
  echo "Python 3.11 is required for the Qt prototype."
  echo "Install Python 3.11 or newer, then run this shortcut again."
  exit 1
fi

if [ ! -d ".venv-qt" ]; then
  echo
  echo "Creating Qt prototype environment..."
  python3.11 -m venv .venv-qt
fi

echo
echo "Installing/updating Qt prototype dependencies..."
.venv-qt/bin/python -m pip install --upgrade pip
.venv-qt/bin/python -m pip install -r requirements-qt.txt

echo
echo "Starting Artwork Manager..."
.venv-qt/bin/python -m artwork_manager_app.run_qt_app
