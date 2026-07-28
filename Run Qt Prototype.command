#!/bin/zsh
set -e

cd "$(dirname "$0")"

if ! command -v python3.11 >/dev/null 2>&1; then
  echo "Python 3.11 is required for the Qt prototype."
  echo "Install Python 3.11 or newer, then run this launcher again."
  exit 1
fi

if [ ! -d ".venv-qt" ]; then
  echo "Creating Qt prototype environment..."
  python3.11 -m venv .venv-qt
fi

echo "Installing/updating Qt prototype dependencies..."
.venv-qt/bin/python -m pip install --upgrade pip
.venv-qt/bin/python -m pip install -r requirements-qt.txt

echo "Starting Artwork Manager Qt Prototype..."
.venv-qt/bin/python -m artwork_manager_app.run_qt_app
