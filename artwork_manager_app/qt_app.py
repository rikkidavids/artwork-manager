"""Experimental PySide6 UI for read-only artwork review.

This prototype deliberately reuses the existing database, state, and media
helpers. It is not a replacement for the Tk app yet: the first goal is to make
queue browsing and artwork inspection feel modern while keeping writes disabled.
"""
from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from . import database as db
from .config import APP_DIR, BUILD_VERSION, load_settings
from .scanner import embedded_artwork
from .state import evaluate_album_record, workflow_bucket_for_status
from .utils import open_path

MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
FILTERS = ('All', 'Needs Attention', 'Review', 'Missing', 'Needs Search', 'Not Square', 'Convert', 'Good', 'Handled')


def _text(value: Any, fallback: str = '') -> str:
    value = '' if value is None else str(value)
    value = value.strip()
    return value if value else fallback


def _album_size(album: Dict[str, Any]) -> str:
    w, h = album.get('width'), album.get('height')
    if w is None or h is None:
        return 'Missing'
    try:
        return f'{int(w)} x {int(h)}'
    except Exception:
        return 'Missing'


def _path_tail(path: str, parts: int = 3) -> str:
    path = _text(path)
    if not path:
        return ''
    try:
        bits = Path(path).parts
        if len(bits) > parts:
            return os.path.join('...', *bits[-parts:])
    except Exception:
        pass
    return path


def _image_pixmap(source: Any) -> Optional[QPixmap]:
    pix = QPixmap()
    if isinstance(source, (bytes, bytearray)):
        if pix.loadFromData(bytes(source)):
            return pix
        return None
    path = _text(source)
    if path and os.path.exists(path) and pix.load(path):
        return pix
    return None


def configure_app_font(app: QApplication) -> None:
    font = QFont('Helvetica Neue')
    font.setPointSize(13)
    app.setFont(font)


def _first_music_file(album: Dict[str, Any]) -> str:
    album_path = _text(album.get('album_path'))
    if not album_path:
        return ''
    example = _text(album.get('example_file'))
    if example:
        fp = os.path.join(album_path, example)
        if os.path.exists(fp):
            return fp
    # Keep the prototype gentle on NAS paths: only inspect the album top level.
    try:
        for name in sorted(os.listdir(album_path), key=lambda item: item.lower()):
            fp = os.path.join(album_path, name)
            if os.path.isfile(fp) and name.lower().endswith(MUSIC_EXTENSIONS):
                return fp
    except Exception:
        return ''
    return ''


class CurrentArtWorker(QThread):
    loaded = Signal(str, object, object, str)

    def __init__(self, album_key: str, source_file: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.album_key = album_key
        self.source_file = source_file

    def run(self) -> None:
        data = None
        dims = None
        try:
            arts = embedded_artwork(self.source_file)
            if arts:
                art = max(arts, key=lambda item: int(item.get('width') or 0) * int(item.get('height') or 0))
                data = art.get('bytes')
                if art.get('width') and art.get('height'):
                    dims = (art.get('width'), art.get('height'))
        except Exception:
            data = None
            dims = None
        self.loaded.emit(self.album_key, data, dims, self.source_file)


class ImagePanel(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName('imagePanel')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.title_label = QLabel(title)
        self.title_label.setObjectName('panelTitle')
        self.image_label = QLabel('No artwork')
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumSize(260, 260)
        self.image_label.setObjectName('artworkPreview')
        self.meta_label = QLabel('')
        self.meta_label.setObjectName('mutedLabel')
        self.meta_label.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.image_label, 1)
        layout.addWidget(self.meta_label)

    def set_placeholder(self, text: str, meta: str = '') -> None:
        self.image_label.setText(text)
        self.image_label.setPixmap(QPixmap())
        self.meta_label.setText(meta)

    def set_image(self, source: Any, meta: str = '') -> bool:
        pix = _image_pixmap(source)
        if not pix:
            self.set_placeholder('No artwork', meta)
            return False
        target = self.image_label.size()
        scaled = pix.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setText('')
        self.image_label.setPixmap(scaled)
        self.image_label.setProperty('sourcePixmap', pix)
        self.meta_label.setText(meta)
        return True

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        pix = self.image_label.property('sourcePixmap')
        if isinstance(pix, QPixmap) and not pix.isNull():
            scaled = pix.scaled(self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        super().resizeEvent(event)


class QtArtworkWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.albums: List[Dict[str, Any]] = []
        self.visible_albums: List[Dict[str, Any]] = []
        self.current_album: Optional[Dict[str, Any]] = None
        self.current_candidates: List[Dict[str, Any]] = []
        self.current_art_worker: Optional[CurrentArtWorker] = None
        self.current_art_workers: List[CurrentArtWorker] = []

        self.setWindowTitle(f'Artwork Manager Qt Prototype - {BUILD_VERSION}')
        self.resize(1380, 860)
        self.setMinimumSize(1100, 700)

        self._build_actions()
        self._build_ui()
        self._apply_style()
        self.reload_queue(select_first=True)

    def _build_actions(self) -> None:
        toolbar = QToolBar('Main')
        toolbar.setMovable(False)
        toolbar.setObjectName('mainToolbar')
        self.addToolBar(toolbar)
        style = self.style()

        refresh = QAction(style.standardIcon(QStyle.SP_BrowserReload), 'Refresh', self)
        refresh.setToolTip('Reload the queue from the database')
        refresh.triggered.connect(lambda: self.reload_queue(select_first=False))
        toolbar.addAction(refresh)

        open_tk = QAction(style.standardIcon(QStyle.SP_ComputerIcon), 'Open Stable Tk App', self)
        open_tk.setToolTip('Show where write actions still live')
        open_tk.triggered.connect(self._show_tk_hint)
        toolbar.addAction(open_tk)

        toolbar.addSeparator()

        readonly = QLabel('Read-only prototype')
        readonly.setObjectName('readonlyPill')
        toolbar.addWidget(readonly)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 10, 12, 12)
        root_layout.setSpacing(10)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        left = self._build_queue_panel()
        right = self._build_review_panel()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 900])

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def _build_queue_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName('sidebar')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel('Queue')
        title.setObjectName('sectionTitle')
        self.count_label = QLabel('')
        self.count_label.setObjectName('mutedLabel')
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.count_label)
        layout.addLayout(title_row)

        controls = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search artist, album, folder...')
        self.search_edit.textChanged.connect(self.apply_filters)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(FILTERS)
        self.filter_combo.currentTextChanged.connect(self.apply_filters)
        controls.addWidget(self.search_edit, 1)
        controls.addWidget(self.filter_combo)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Status', 'Artist', 'Album', 'Size', 'Opts'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._select_table_album)
        layout.addWidget(self.table, 1)

        return panel

    def _build_review_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 4, 0, 0)
        layout.setSpacing(12)

        self.album_title = QLabel('No album selected')
        self.album_title.setObjectName('albumTitle')
        self.album_subtitle = QLabel('')
        self.album_subtitle.setObjectName('mutedLabel')
        layout.addWidget(self.album_title)
        layout.addWidget(self.album_subtitle)

        image_grid = QGridLayout()
        image_grid.setSpacing(12)
        self.current_panel = ImagePanel('Current')
        self.candidate_panel = ImagePanel('Candidate')
        image_grid.addWidget(self.current_panel, 0, 0)
        image_grid.addWidget(self.candidate_panel, 0, 1)
        image_grid.setColumnStretch(0, 1)
        image_grid.setColumnStretch(1, 1)
        layout.addLayout(image_grid, 3)

        lower = QSplitter(Qt.Horizontal)
        lower.setChildrenCollapsible(False)
        self.candidate_list = QListWidget()
        self.candidate_list.setObjectName('candidateList')
        self.candidate_list.currentRowChanged.connect(self._select_candidate)
        lower.addWidget(self.candidate_list)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setObjectName('detailsText')
        lower.addWidget(self.details)
        lower.setSizes([320, 560])
        layout.addWidget(lower, 2)

        actions = QHBoxLayout()
        self.open_folder_btn = QPushButton('Open Album Folder')
        self.open_folder_btn.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.open_folder_btn.clicked.connect(self.open_album_folder)
        self.open_source_btn = QPushButton('Open Source Page')
        self.open_source_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.open_source_btn.clicked.connect(self.open_source_page)
        actions.addWidget(self.open_folder_btn)
        actions.addWidget(self.open_source_btn)
        actions.addStretch(1)
        layout.addLayout(actions)
        return panel

    def reload_queue(self, *, select_first: bool = False) -> None:
        try:
            self.settings = load_settings()
            self.albums = db.load_albums(actionable_only=False)
        except Exception as exc:
            QMessageBox.critical(self, 'Queue load failed', str(exc))
            self.albums = []
        self.apply_filters()
        if select_first and self.visible_albums:
            self.table.selectRow(0)
        self.statusBar().showMessage(f'Loaded {len(self.albums)} album records')

    def _album_bucket(self, album: Dict[str, Any]) -> str:
        try:
            status, _reason = evaluate_album_record(album, candidate_count=album.get('candidate_count'), settings=self.settings)
        except Exception:
            status = album.get('status') or ''
        return workflow_bucket_for_status(status)

    def _album_status_reason(self, album: Dict[str, Any]) -> tuple[str, str]:
        try:
            return evaluate_album_record(album, candidate_count=album.get('candidate_count'), settings=self.settings)
        except Exception:
            return album.get('status') or '', ''

    def apply_filters(self) -> None:
        query = self.search_edit.text().strip().lower() if hasattr(self, 'search_edit') else ''
        selected_filter = self.filter_combo.currentText() if hasattr(self, 'filter_combo') else 'All'
        visible = []
        for album in self.albums:
            bucket = self._album_bucket(album)
            if selected_filter == 'Needs Attention':
                if bucket not in {'Needs Search', 'Missing', 'Not Square', 'Convert'}:
                    continue
            elif selected_filter != 'All' and bucket != selected_filter:
                continue
            haystack = ' '.join([
                _text(album.get('artist')),
                _text(album.get('album')),
                _text(album.get('album_path')),
                bucket,
                _text(album.get('status')),
            ]).lower()
            if query and query not in haystack:
                continue
            visible.append(album)
        visible.sort(key=lambda item: (self._sort_bucket(item), _text(item.get('artist')).lower(), _text(item.get('album')).lower()))
        self.visible_albums = visible
        self._render_table()

    def _sort_bucket(self, album: Dict[str, Any]) -> int:
        order = {'Review': 0, 'Missing': 1, 'Needs Search': 2, 'Not Square': 3, 'Convert': 4, 'Good': 5, 'Handled': 6}
        return order.get(self._album_bucket(album), 99)

    def _render_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.visible_albums))
        for row, album in enumerate(self.visible_albums):
            bucket = self._album_bucket(album)
            values = [
                bucket,
                _text(album.get('artist'), 'Unknown Artist'),
                _text(album.get('album'), 'Unknown Album'),
                _album_size(album),
                str(int(album.get('candidate_count') or 0)),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col in (3, 4):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self.count_label.setText(f'{len(self.visible_albums)} shown')

    def _select_table_album(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            return
        row = rows[0]
        if 0 <= row < len(self.visible_albums):
            self.show_album(self.visible_albums[row])

    def show_album(self, album: Dict[str, Any]) -> None:
        self.current_album = album
        artist = _text(album.get('artist'), 'Unknown Artist')
        album_name = _text(album.get('album'), 'Unknown Album')
        self.album_title.setText(f'{artist} - {album_name}')
        self.album_subtitle.setText(_path_tail(_text(album.get('album_path')), parts=4))
        self.open_folder_btn.setEnabled(bool(_text(album.get('album_path'))))
        self.open_source_btn.setEnabled(False)
        self.current_panel.set_placeholder('Loading...', 'Reading embedded artwork')
        self._load_current_art(album)
        self._load_candidates(album)
        self._render_details()

    def _load_current_art(self, album: Dict[str, Any]) -> None:
        source_file = _first_music_file(album)
        if not source_file:
            self.current_panel.set_placeholder('No current preview', 'No readable example file')
            return
        key = _text(album.get('album_key'))
        worker = CurrentArtWorker(key, source_file, self)
        worker.loaded.connect(self._current_art_loaded)
        worker.finished.connect(lambda w=worker: self._current_art_worker_finished(w))
        self.current_art_workers.append(worker)
        self.current_art_worker = worker
        worker.start()

    def _current_art_loaded(self, album_key: str, data: Any, dims: Any, source_file: str) -> None:
        if not self.current_album or album_key != _text(self.current_album.get('album_key')):
            return
        if data:
            meta = ''
            if dims:
                meta = f'{dims[0]} x {dims[1]}'
            self.current_panel.set_image(data, meta)
        else:
            self.current_panel.set_placeholder('No embedded artwork', _path_tail(source_file, parts=2))

    def _current_art_worker_finished(self, worker: CurrentArtWorker) -> None:
        try:
            self.current_art_workers.remove(worker)
        except ValueError:
            pass
        if self.current_art_worker is worker:
            self.current_art_worker = self.current_art_workers[-1] if self.current_art_workers else None

    def _load_candidates(self, album: Dict[str, Any]) -> None:
        self.candidate_list.clear()
        self.current_candidates = []
        key = album.get('album_key')
        if key:
            try:
                self.current_candidates = db.load_candidates_for_album(key, include_rejected=False)
            except Exception:
                self.current_candidates = []
        for cand in self.current_candidates:
            label = self._candidate_label(cand)
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, cand)
            self.candidate_list.addItem(item)
        if self.current_candidates:
            self.candidate_list.setCurrentRow(0)
        else:
            self.candidate_panel.set_placeholder('No saved candidate', 'Use the stable app to search artwork')

    def _candidate_label(self, cand: Dict[str, Any]) -> str:
        source = _text(cand.get('source'), 'Artwork')
        dims = ''
        if cand.get('width') and cand.get('height'):
            dims = f" - {cand.get('width')} x {cand.get('height')}"
        score = f" - {int(cand.get('score') or 0)}/100"
        title = _text(cand.get('release_title'))
        if title:
            return f'{source}{dims}{score}\n{title}'
        return f'{source}{dims}{score}'

    def _select_candidate(self, row: int) -> None:
        if row < 0 or row >= len(self.current_candidates):
            self.candidate_panel.set_placeholder('No saved candidate')
            self.open_source_btn.setEnabled(False)
            self._render_details()
            return
        cand = self.current_candidates[row]
        meta = ''
        if cand.get('width') and cand.get('height'):
            meta = f"{cand.get('width')} x {cand.get('height')}"
        if cand.get('score') is not None:
            meta = (meta + ' - ' if meta else '') + f"{int(cand.get('score') or 0)}/100"
        self.candidate_panel.set_image(cand.get('image_path'), meta)
        self.open_source_btn.setEnabled(bool(_text(cand.get('source_url'))))
        self._render_details(cand)

    def _render_details(self, candidate: Optional[Dict[str, Any]] = None) -> None:
        album = self.current_album or {}
        status, reason = self._album_status_reason(album) if album else ('', '')
        lines = []
        if album:
            lines.extend([
                f"Status: {workflow_bucket_for_status(status)}",
                f"Why: {reason or _text(status, 'No status recorded')}",
                f"Current size: {_album_size(album)}",
                f"Options: {int(album.get('candidate_count') or 0)}",
                '',
                'Album folder:',
                _text(album.get('album_path'), '-'),
            ])
        if candidate:
            warnings = candidate.get('warnings') or []
            if isinstance(warnings, str):
                warnings = [warnings]
            lines.extend([
                '',
                'Selected candidate:',
                f"Source: {_text(candidate.get('source'), '-')}",
                f"Release: {_text(candidate.get('release_title'), '-')}",
                f"Size: {_text(candidate.get('width'), '?')} x {_text(candidate.get('height'), '?')}",
                f"Score: {int(candidate.get('score') or 0)}/100",
                f"Source page: {_text(candidate.get('source_url'), '-')}",
            ])
            if warnings:
                lines.append('Warnings: ' + '; '.join(_text(w) for w in warnings if _text(w)))
            summary = _text(candidate.get('score_summary'))
            if summary:
                lines.append('Score summary: ' + summary)
        self.details.setPlainText('\n'.join(lines))

    def open_album_folder(self) -> None:
        album = self.current_album or {}
        path = _text(album.get('album_path'))
        if path:
            open_path(path)

    def open_source_page(self) -> None:
        row = self.candidate_list.currentRow()
        if 0 <= row < len(self.current_candidates):
            url = _text(self.current_candidates[row].get('source_url'))
            if url:
                webbrowser.open(url)

    def _show_tk_hint(self) -> None:
        QMessageBox.information(
            self,
            'Stable app',
            'This Qt window is read-only. Use the existing Tk app for Scan, Find Artwork, Approve + Embed, and NAS worker actions.',
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        for worker in list(self.current_art_workers):
            try:
                worker.wait(250)
            except Exception:
                pass
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f5f5f7;
                color: #1d1d1f;
                font-size: 13px;
            }
            QToolBar#mainToolbar {
                background: #fbfbfd;
                border-bottom: 1px solid #d8d8de;
                spacing: 8px;
                padding: 6px;
            }
            QLabel#readonlyPill {
                color: #4f5f74;
                background: #e8eef7;
                border: 1px solid #cfd8e6;
                border-radius: 8px;
                padding: 4px 8px;
            }
            QFrame#sidebar, QFrame#imagePanel {
                background: #ffffff;
                border: 1px solid #dedee4;
                border-radius: 8px;
            }
            QLabel#sectionTitle {
                font-size: 18px;
                font-weight: 700;
            }
            QLabel#albumTitle {
                font-size: 24px;
                font-weight: 700;
            }
            QLabel#panelTitle {
                font-size: 15px;
                font-weight: 700;
            }
            QLabel#mutedLabel {
                color: #666a73;
            }
            QLabel#artworkPreview {
                background: #fafafa;
                border: 1px solid #e0e0e5;
                border-radius: 6px;
                color: #777b84;
            }
            QLineEdit, QComboBox, QTextEdit, QListWidget, QTableWidget {
                background: #ffffff;
                border: 1px solid #d9d9df;
                border-radius: 6px;
                padding: 5px;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QTableWidget {
                gridline-color: #ececf1;
            }
            QHeaderView::section {
                background: #f1f2f5;
                border: 0;
                border-bottom: 1px solid #d8d8de;
                padding: 6px;
                font-weight: 700;
            }
            QPushButton {
                background: #ffffff;
                border: 1px solid #c9cad1;
                border-radius: 6px;
                padding: 7px 10px;
            }
            QPushButton:hover {
                background: #f0f4ff;
                border-color: #a9b8d8;
            }
            QPushButton:disabled {
                color: #9a9aa2;
                background: #f4f4f6;
            }
            """
        )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName('Artwork Manager Qt Prototype')
    configure_app_font(app)
    icon_path = APP_DIR / 'assets' / 'app_icon.png'
    if icon_path.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))
    win = QtArtworkWindow()
    win.show()
    return app.exec()
