"""Closing the main window must not tear down a job that is writing music files."""
import threading

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from artwork_manager_app import qt_app  # noqa: E402


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


class _SlowApproval(qt_app.ApprovalWorker):
    def __init__(self, parent, release):
        super().__init__({}, False, parent)
        self._release = release

    def run(self):
        self._release.wait(10)


def test_close_is_refused_while_artwork_is_being_written(app, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, 'information', lambda *a, **k: shown.append(a[1]))
    win = qt_app.QtArtworkWindow()
    win.show()
    release = threading.Event()
    worker = _SlowApproval(win, release)
    win.approval_worker = worker
    worker.start()
    try:
        assert win.close() is False
        assert shown == ['Still saving artwork']
    finally:
        release.set()
        worker.wait(5000)
    assert win.close() is True
