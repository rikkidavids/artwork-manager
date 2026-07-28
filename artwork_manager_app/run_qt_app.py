"""Entry point for the PySide6 review UI."""
from __future__ import annotations

import importlib.util
import sys


def _check_runtime() -> bool:
    if sys.version_info < (3, 10):
        print('The Qt artwork review app needs Python 3.10 or newer.')
        print('Create a newer environment, then run: python -m artwork_manager_app.run_qt_app')
        return False
    if importlib.util.find_spec('PySide6') is None:
        print('PySide6 is not installed in this environment.')
        print('Install the Qt artwork review dependencies with:')
        print('  python -m pip install -r requirements-qt.txt')
        return False
    return True


def main() -> int:
    if not _check_runtime():
        return 1
    from .qt_app import main as qt_main
    return int(qt_main() or 0)


if __name__ == '__main__':
    raise SystemExit(main())
