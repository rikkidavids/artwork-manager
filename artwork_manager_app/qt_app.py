"""Experimental PySide6 UI for artwork review.

This prototype deliberately reuses the existing database, state, and media
helpers. It is not a replacement for the Tk app yet: queue browsing, artwork
inspection, and Approve + Embed live here first while scanning/search remain in
the stable app.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .approval import ApprovalBlocked, approve_candidate, candidate_needs_warning, candidate_warning_text
from . import database as db
from .config import APP_DIR, BUILD_VERSION, get_max_candidates_per_album, load_settings, save_settings
from .review_queue import build_candidates
from .scanner import embedded_artwork
from .state import evaluate_album_record, workflow_bucket_for_status
from .utils import open_path

MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
FILTERS = ('All', 'Needs Attention', 'Review', 'Missing', 'Needs Search', 'Not Square', 'Convert', 'Good', 'Handled')
QUEUE_COLUMNS = ('status', 'artist', 'album', 'current', 'candidates')
DEFAULT_QUEUE_COLUMN_WIDTHS = {
    'status': 96,
    'artist': 220,
    'album': 360,
    'current': 128,
    'candidates': 68,
}
QUEUE_COLUMN_ALIASES = {
    'size': 'current',
    'options': 'candidates',
}
BUCKET_COLORS = {
    'Review': ('#17345c', '#e7f0ff'),
    'Missing': ('#5c3217', '#fff0df'),
    'Needs Search': ('#514017', '#fff7d6'),
    'Not Square': ('#5b2345', '#fde8f2'),
    'Convert': ('#3b2f67', '#eee9ff'),
    'Good': ('#174c33', '#e4f7ed'),
    'Handled': ('#46505d', '#edf0f5'),
}


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


def _line_icon(kind: str, color: str = '#4b5563') -> QIcon:
    pix = QPixmap(20, 20)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    if kind == 'refresh':
        painter.drawLine(5, 7, 14, 7)
        painter.drawLine(14, 7, 11, 4)
        painter.drawLine(14, 7, 11, 10)
        painter.drawLine(15, 13, 6, 13)
        painter.drawLine(6, 13, 9, 10)
        painter.drawLine(6, 13, 9, 16)
    elif kind == 'app':
        painter.drawRoundedRect(QRectF(3.5, 4.5, 13, 10), 2, 2)
        painter.drawLine(8, 16, 12, 16)
        painter.drawLine(10, 14.5, 10, 16)
    elif kind == 'search':
        painter.drawEllipse(QRectF(4, 4, 8.5, 8.5))
        painter.drawLine(11, 11, 16, 16)
    elif kind == 'stop':
        painter.drawLine(5, 5, 15, 15)
        painter.drawLine(15, 5, 5, 15)
    elif kind == 'check':
        painter.drawLine(4.5, 10.5, 8.5, 14.5)
        painter.drawLine(8.5, 14.5, 16, 5.5)
    elif kind == 'folder':
        painter.drawLine(3.5, 7, 7.5, 7)
        painter.drawLine(7.5, 7, 9.5, 9)
        painter.drawLine(9.5, 9, 16.5, 9)
        painter.drawLine(16.5, 9, 16.5, 15)
        painter.drawLine(16.5, 15, 3.5, 15)
        painter.drawLine(3.5, 15, 3.5, 7)
    elif kind == 'link':
        painter.drawRoundedRect(QRectF(4, 6, 10, 10), 2, 2)
        painter.drawLine(10, 4, 16, 4)
        painter.drawLine(16, 4, 16, 10)
        painter.drawLine(16, 4, 10, 10)
    painter.end()
    return QIcon(pix)


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


class ApprovalWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, candidate: Dict[str, Any], backup: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.candidate = dict(candidate or {})
        self.backup = bool(backup)

    def run(self) -> None:
        try:
            result = approve_candidate(
                self.candidate,
                backup=self.backup,
                progress=lambda done, total, path: self.progress.emit(int(done or 0), int(total or 0), str(path or '')),
            )
            self.completed.emit(result)
        except ApprovalBlocked as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f'Embedding failed: {exc}')


class SearchWorker(QThread):
    status_update = Signal(str)
    log_line = Signal(str)
    candidate_found = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, infos: List[Dict[str, Any]], max_per_album: int, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.infos = [dict(info or {}) for info in infos]
        self.max_per_album = int(max_per_album or 0)
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            before = {
                info['album_key']: len(db.load_candidates_for_album(info['album_key'], include_rejected=False))
                for info in self.infos
                if info.get('album_key')
            }
            build_candidates(
                self.infos,
                max_per_album=self.max_per_album,
                include_fallbacks=True,
                stop_event=self.stop_event,
                log=lambda line: self.log_line.emit(str(line or '')),
                status=lambda text: self.status_update.emit(str(text or '')),
                on_candidate=lambda candidate: self.candidate_found.emit(candidate),
            )
            after = {
                info['album_key']: len(db.load_candidates_for_album(info['album_key'], include_rejected=False))
                for info in self.infos
                if info.get('album_key')
            }
            saved = sum(max(0, after.get(key, 0) - before.get(key, 0)) for key in before)
            status_counts: Dict[str, int] = {}
            for info in self.infos:
                album = db.get_album(info.get('album_key')) or {}
                status = album.get('status') or 'unknown'
                status_counts[status] = status_counts.get(status, 0) + 1
            self.completed.emit({
                'album_key': self.infos[0].get('album_key') if self.infos else '',
                'album_count': len(self.infos),
                'saved': saved,
                'stopped': self.stop_event.is_set(),
                'status_counts': status_counts,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


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


class CleanComboBox(QComboBox):
    """ComboBox with a small painted chevron instead of Qt's bulky arrow box."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(30)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor('#687385'))
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        mid_y = self.height() / 2.0
        right = self.width() - 18
        painter.drawLine(int(right - 4), int(mid_y - 2), int(right), int(mid_y + 2))
        painter.drawLine(int(right), int(mid_y + 2), int(right + 4), int(mid_y - 2))


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
        self.approval_worker: Optional[ApprovalWorker] = None
        self.search_worker: Optional[SearchWorker] = None
        self.last_approval_result: Optional[Dict[str, Any]] = None
        self.last_search_log: List[str] = []
        self.pending_approval_row = 0
        self._restoring_queue_columns = False
        self.queue_column_save_timer = QTimer(self)
        self.queue_column_save_timer.setSingleShot(True)
        self.queue_column_save_timer.timeout.connect(self._save_queue_column_widths)

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
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        refresh = QAction(_line_icon('refresh'), 'Refresh', self)
        refresh.setToolTip('Reload the queue from the database')
        refresh.triggered.connect(lambda: self.reload_queue(select_first=False))
        toolbar.addAction(refresh)

        open_tk = QAction(_line_icon('app'), 'Open Stable Tk App', self)
        open_tk.setToolTip('Show where write actions still live')
        open_tk.triggered.connect(self._show_tk_hint)
        toolbar.addAction(open_tk)

        toolbar.addSeparator()

        readonly = QLabel('Qt prototype')
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
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([620, 760])

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
        self.filter_combo = CleanComboBox()
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
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setCornerButtonEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setMinimumSectionSize(52)
        header.setToolTip('Drag column dividers to resize the album list.')
        for col in range(self.table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.sectionResized.connect(self._queue_column_resized)
        self.table.itemSelectionChanged.connect(self._select_table_album)
        layout.addWidget(self.table, 1)
        QTimer.singleShot(0, self._apply_queue_column_widths)

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
        lower.setObjectName('reviewSplitter')
        lower.setChildrenCollapsible(False)
        self.candidate_list = QListWidget()
        self.candidate_list.setObjectName('candidateList')
        self.candidate_list.setSpacing(2)
        self.candidate_list.currentRowChanged.connect(self._select_candidate)
        lower.addWidget(self.candidate_list)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setObjectName('detailsText')
        self.details.setLineWrapMode(QTextEdit.WidgetWidth)
        lower.addWidget(self.details)
        lower.setSizes([320, 560])
        layout.addWidget(lower, 2)

        self.approval_status = QLabel('')
        self.approval_status.setObjectName('mutedLabel')
        layout.addWidget(self.approval_status)

        self.approval_progress = QProgressBar()
        self.approval_progress.setTextVisible(False)
        self.approval_progress.setVisible(False)
        self.approval_progress.setFixedHeight(8)
        layout.addWidget(self.approval_progress)

        actions = QHBoxLayout()
        self.find_btn = QPushButton('Find Artwork')
        self.find_btn.setObjectName('primaryButton')
        self.find_btn.setIcon(_line_icon('search', '#ffffff'))
        self.find_btn.setIconSize(QSize(16, 16))
        self.find_btn.clicked.connect(self.find_artwork_for_selected_album)
        actions.addWidget(self.find_btn)

        self.stop_search_btn = QPushButton('Stop')
        self.stop_search_btn.setObjectName('dangerButton')
        self.stop_search_btn.setIcon(_line_icon('stop', '#ffffff'))
        self.stop_search_btn.setIconSize(QSize(16, 16))
        self.stop_search_btn.clicked.connect(self.stop_search)
        self.stop_search_btn.setVisible(False)
        actions.addWidget(self.stop_search_btn)

        self.approve_btn = QPushButton('Approve + Embed')
        self.approve_btn.setObjectName('approveButton')
        self.approve_btn.setIcon(_line_icon('check', '#ffffff'))
        self.approve_btn.setIconSize(QSize(16, 16))
        self.approve_btn.clicked.connect(self.approve_selected_candidate)
        actions.addWidget(self.approve_btn)

        self.backup_checkbox = QCheckBox('Backup')
        self.backup_checkbox.setChecked(bool(self.settings.get('backup_before_embedding', False)))
        self.backup_checkbox.setToolTip('Save music-file backups before embedding')
        self.backup_checkbox.toggled.connect(self._save_backup_preference)
        actions.addWidget(self.backup_checkbox)

        self.open_folder_btn = QPushButton('Open Album Folder')
        self.open_folder_btn.setObjectName('quietButton')
        self.open_folder_btn.setIcon(_line_icon('folder'))
        self.open_folder_btn.setIconSize(QSize(16, 16))
        self.open_folder_btn.clicked.connect(self.open_album_folder)
        self.open_source_btn = QPushButton('Open Source Page')
        self.open_source_btn.setObjectName('quietButton')
        self.open_source_btn.setIcon(_line_icon('link'))
        self.open_source_btn.setIconSize(QSize(16, 16))
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
        self._refresh_action_states()

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

    def _queue_column_widths_from_settings(self) -> Dict[str, int]:
        layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
        saved = layout.get('queue_columns') if isinstance(layout.get('queue_columns'), dict) else {}
        widths = self._default_queue_column_widths()
        for name, value in saved.items():
            name = QUEUE_COLUMN_ALIASES.get(name, name)
            if name not in widths:
                continue
            try:
                widths[name] = max(52, int(value))
            except Exception:
                pass
        return widths

    def _default_queue_column_widths(self) -> Dict[str, int]:
        available = max(0, int(self.table.viewport().width()) - 2)
        if available <= 80:
            return dict(DEFAULT_QUEUE_COLUMN_WIDTHS)
        minimums = {'status': 68, 'artist': 90, 'album': 110, 'current': 82, 'candidates': 52}
        available = max(sum(minimums.values()), available)
        if available < 680:
            status_w, current_w, candidates_w = 78, 116, 52
            artist_min, album_min, artist_cap = 90, 110, 160
        else:
            status_w, current_w, candidates_w = 96, 128, 68
            artist_min, album_min, artist_cap = 140, 220, 260
        remaining = max(
            artist_min + album_min,
            available - status_w - current_w - candidates_w,
        )
        artist_w = min(artist_cap, max(artist_min, int(remaining * 0.36)))
        album_w = max(album_min, remaining - artist_w)
        return {
            'status': status_w,
            'artist': artist_w,
            'album': album_w,
            'current': current_w,
            'candidates': candidates_w,
        }

    def _apply_queue_column_widths(self) -> None:
        widths = self._queue_column_widths_from_settings()
        self._restoring_queue_columns = True
        try:
            for col, name in enumerate(QUEUE_COLUMNS):
                self.table.setColumnWidth(col, widths.get(name, DEFAULT_QUEUE_COLUMN_WIDTHS.get(name, 100)))
        finally:
            self._restoring_queue_columns = False

    def _queue_column_resized(self, _logical_index: int, _old_size: int, _new_size: int) -> None:
        if self._restoring_queue_columns:
            return
        self.queue_column_save_timer.start(350)

    def _save_queue_column_widths(self) -> None:
        try:
            widths = {name: int(self.table.columnWidth(col)) for col, name in enumerate(QUEUE_COLUMNS)}
            layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
            layout = dict(layout)
            layout['queue_columns'] = widths
            self.settings['layout'] = layout
            save_settings({'layout': {'queue_columns': widths}})
        except Exception:
            pass

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
                if col == 0:
                    fg, bg = BUCKET_COLORS.get(bucket, ('#46505d', '#edf0f5'))
                    item.setForeground(QBrush(QColor(fg)))
                    item.setBackground(QBrush(QColor(bg)))
                    item.setTextAlignment(Qt.AlignCenter)
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
        self._refresh_action_states()

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
            item.setSizeHint(QSize(0, 58))
            self.candidate_list.addItem(item)
        if self.current_candidates:
            self.candidate_list.setCurrentRow(0)
        else:
            self.candidate_panel.set_placeholder('No saved candidate', 'Use the stable app to search artwork')
        self._refresh_action_states()

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
        self._refresh_action_states()

    def _selected_candidate(self) -> Optional[Dict[str, Any]]:
        row = self.candidate_list.currentRow()
        if 0 <= row < len(self.current_candidates):
            return self.current_candidates[row]
        return None

    def _album_to_search_info(self, album: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not album:
            return None
        return {
            'artist': album.get('artist', ''),
            'album': album.get('album', ''),
            'album_key': album.get('album_key'),
            'album_path': album.get('album_path') or album.get('album_folder'),
            'search_artist': album.get('search_artist') or album.get('artist', ''),
            'search_album': album.get('search_album') or album.get('album', ''),
            'year': album.get('year') or '',
            'mb_release_id': album.get('mb_release_id') or '',
            'mb_releasegroup_id': album.get('mb_releasegroup_id') or '',
            'identity_confidence': album.get('identity_confidence') or '',
            'example_file': album.get('example_file') or '',
            'width': album.get('width'),
            'height': album.get('height'),
        }

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
        if self.last_approval_result and album and self.last_approval_result.get('album_key') == album.get('album_key'):
            lines.extend([
                '',
                'Last approval:',
                _text(self.last_approval_result.get('final_reason'), '-'),
                f"Files: {int(self.last_approval_result.get('updated_files') or 0)} / {int(self.last_approval_result.get('total_files') or 0)}",
            ])
        if self.last_search_log:
            lines.extend(['', 'Search log:'])
            lines.extend(self.last_search_log[-8:])
        self.details.setPlainText('\n'.join(lines))

    def _save_backup_preference(self) -> None:
        try:
            value = bool(self.backup_checkbox.isChecked())
            self.settings['backup_before_embedding'] = value
            save_settings({'backup_before_embedding': value})
        except Exception:
            pass

    def _refresh_action_states(self) -> None:
        has_candidate = self._selected_candidate() is not None
        approval_busy = self.approval_worker is not None and self.approval_worker.isRunning()
        search_busy = self.search_worker is not None and self.search_worker.isRunning()
        busy = approval_busy or search_busy
        album = self.current_album or {}
        bucket = self._album_bucket(album) if album else ''
        can_search = bool(album and _text(album.get('album_key')) and _text(album.get('album_path')) and bucket not in {'Good', 'Handled'})
        self.find_btn.setEnabled(can_search and not busy)
        self.stop_search_btn.setVisible(search_busy)
        self.stop_search_btn.setEnabled(search_busy)
        self.approve_btn.setEnabled(has_candidate and not busy)
        self.backup_checkbox.setEnabled(not busy)
        self.open_folder_btn.setEnabled(bool(self.current_album and _text(self.current_album.get('album_path'))))
        self.open_source_btn.setEnabled(bool(has_candidate and _text((self._selected_candidate() or {}).get('source_url'))))

    def find_artwork_for_selected_album(self) -> None:
        info = self._album_to_search_info(self.current_album)
        if not info or not info.get('album_key') or not info.get('album_path'):
            QMessageBox.warning(self, 'Cannot search artwork', 'Select an album with a known folder first.')
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        max_per_album = get_max_candidates_per_album(self.settings)
        artist = _text(info.get('search_artist') or info.get('artist'), 'Unknown Artist')
        album = _text(info.get('search_album') or info.get('album'), 'Unknown Album')
        self.last_search_log = [f'Searching: {artist} - {album}']
        self.approval_status.setText(f'Searching providers for {artist} - {album}...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage('Finding artwork...')

        worker = SearchWorker([info], max_per_album, self)
        worker.status_update.connect(self._search_status)
        worker.log_line.connect(self._search_log)
        worker.candidate_found.connect(self._search_candidate_found)
        worker.completed.connect(self._search_completed)
        worker.failed.connect(self._search_failed)
        worker.finished.connect(lambda w=worker: self._search_worker_finished(w))
        self.search_worker = worker
        self._refresh_action_states()
        worker.start()

    def stop_search(self) -> None:
        if self.search_worker is not None and self.search_worker.isRunning():
            self.search_worker.stop()
            self.approval_status.setText('Stopping artwork search...')
            self.statusBar().showMessage('Stopping artwork search...')
            self._refresh_action_states()

    def _search_status(self, text: str) -> None:
        text = _text(text, 'Searching artwork...')
        self.approval_status.setText(text)
        self.statusBar().showMessage(text)

    def _search_log(self, line: str) -> None:
        line = _text(line)
        if not line:
            return
        self.last_search_log.append(line)
        self.last_search_log = self.last_search_log[-30:]
        self._render_details(self._selected_candidate())

    def _search_candidate_found(self, candidate: object) -> None:
        cand = dict(candidate or {})
        if self.current_album and cand.get('album_key') == self.current_album.get('album_key'):
            self._load_candidates(self.current_album)
            self.candidate_list.setCurrentRow(max(0, self.candidate_list.count() - 1))
        source = _text(cand.get('source'), 'Artwork')
        dims = ''
        if cand.get('width') and cand.get('height'):
            dims = f" {cand.get('width')} x {cand.get('height')}"
        self.approval_status.setText(f'Saved {source}{dims}')

    def _search_completed(self, result: object) -> None:
        result = dict(result or {})
        self.approval_progress.setVisible(False)
        saved = int(result.get('saved') or 0)
        stopped = bool(result.get('stopped'))
        if stopped:
            message = f'Search stopped after saving {saved} option(s).'
        elif saved:
            message = f'Found {saved} new artwork option(s).'
        else:
            message = 'No new artwork options found.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self.reload_queue(select_first=False)
        self._select_album_key(result.get('album_key'), fallback_first=True)
        if not saved and not stopped:
            QMessageBox.information(self, 'Artwork search', message)

    def _search_failed(self, message: str) -> None:
        self.approval_progress.setVisible(False)
        self.approval_status.setText(f'Search failed: {message}')
        self.statusBar().showMessage('Artwork search failed')
        QMessageBox.warning(self, 'Artwork search failed', message)
        self._refresh_action_states()

    def _search_worker_finished(self, worker: SearchWorker) -> None:
        if self.search_worker is worker:
            self.search_worker = None
        self._refresh_action_states()

    def approve_selected_candidate(self) -> None:
        candidate = self._selected_candidate()
        if not candidate:
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        if candidate_needs_warning(candidate, self.settings):
            answer = QMessageBox.question(
                self,
                'Embed lower-confidence artwork?',
                candidate_warning_text(candidate),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.statusBar().showMessage('Approval cancelled. Candidate left unchanged.')
                return

        self.last_approval_result = None
        backup = bool(self.backup_checkbox.isChecked())
        self._save_backup_preference()
        self.approval_status.setText('Preparing embed...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage('Embedding artwork...')
        self.pending_approval_row = max(0, self.table.currentRow())

        worker = ApprovalWorker(candidate, backup, self)
        worker.progress.connect(self._approval_progress)
        worker.completed.connect(self._approval_completed)
        worker.failed.connect(self._approval_failed)
        worker.finished.connect(lambda w=worker: self._approval_worker_finished(w))
        self.approval_worker = worker
        self._refresh_action_states()
        worker.start()

    def _approval_progress(self, done: int, total: int, path: str) -> None:
        if total > 0:
            self.approval_progress.setRange(0, total)
            self.approval_progress.setValue(max(0, min(done, total)))
            self.approval_status.setText(f'Embedding {done}/{total}: {_path_tail(path, parts=2)}')
            self.statusBar().showMessage(f'Embedding {done}/{total}')
        else:
            self.approval_progress.setRange(0, 0)
            self.approval_status.setText(_text(path, 'Embedding artwork...'))

    def _approval_completed(self, result: object) -> None:
        result = dict(result or {})
        self.last_approval_result = result
        self.approval_progress.setVisible(False)
        complete = bool(result.get('approval_complete'))
        updated = int(result.get('updated_files') or result.get('updated') or 0)
        total = int(result.get('total_files') or result.get('total') or 0)
        if complete:
            message = f'Approved and embedded artwork into {updated}/{total} file(s).'
            self.approval_status.setText(message)
            self.statusBar().showMessage(message)
        else:
            message = result.get('final_reason') or 'Approval incomplete; candidate kept for retry.'
            self.approval_status.setText(message)
            self.statusBar().showMessage(message)
            QMessageBox.warning(self, 'Approval incomplete', f'{message}\n\nUpdated {updated}/{total} file(s).')
        self.reload_queue(select_first=False)
        if complete:
            self._select_next_actionable_row(start_row=self.pending_approval_row)
        else:
            self._select_album_key(result.get('album_key'), fallback_first=True)

    def _approval_failed(self, message: str) -> None:
        self.approval_progress.setVisible(False)
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, 'Cannot approve artwork', message)
        self._refresh_action_states()

    def _approval_worker_finished(self, worker: ApprovalWorker) -> None:
        if self.approval_worker is worker:
            self.approval_worker = None
        self._refresh_action_states()

    def _select_album_key(self, album_key: Any, *, fallback_first: bool = False) -> bool:
        key = _text(album_key)
        if key:
            for row, album in enumerate(self.visible_albums):
                if _text(album.get('album_key')) == key:
                    self.table.selectRow(row)
                    return True
        if fallback_first and self.visible_albums:
            self.table.selectRow(0)
            return True
        return False

    def _select_next_actionable_row(self, *, start_row: int = 0) -> bool:
        if not self.visible_albums:
            return False
        actionable = {'Review', 'Missing', 'Needs Search', 'Not Square', 'Convert'}
        start = max(0, min(int(start_row or 0), len(self.visible_albums) - 1))
        for offset in range(len(self.visible_albums)):
            row = (start + offset) % len(self.visible_albums)
            if self._album_bucket(self.visible_albums[row]) in actionable:
                self.table.selectRow(row)
                return True
        self.table.selectRow(0)
        return True

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
            'Use the existing Tk app for Scan, Convert/Save, bulk maintenance, and NAS worker settings. This Qt prototype can browse the queue, Find Artwork for the selected album, and Approve + Embed saved candidates.',
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.queue_column_save_timer.isActive():
            self.queue_column_save_timer.stop()
            self._save_queue_column_widths()
        if self.search_worker is not None:
            try:
                self.search_worker.stop()
                self.search_worker.wait(500)
            except Exception:
                pass
        if self.approval_worker is not None:
            try:
                self.approval_worker.wait(500)
            except Exception:
                pass
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
            QLabel {
                background: transparent;
            }
            QToolBar#mainToolbar {
                background: #fbfbfd;
                border-bottom: 1px solid #d8d8de;
                spacing: 8px;
                padding: 6px;
            }
            QToolButton {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 5px 8px;
            }
            QToolButton:hover {
                background: #eef2f7;
                border-color: #dde3ec;
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
            QLineEdit:hover, QComboBox:hover, QTextEdit:hover, QListWidget:hover, QTableWidget:hover {
                border-color: #c5c9d3;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QListWidget:focus, QTableWidget:focus {
                border-color: #8aa4d6;
            }
            QComboBox {
                padding: 5px 30px 5px 9px;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 28px;
                border: 0;
                background: transparent;
            }
            QComboBox::down-arrow {
                image: none;
                width: 0;
                height: 0;
            }
            QComboBox QAbstractItemView {
                background: #ffffff;
                border: 1px solid #d9d9df;
                border-radius: 8px;
                padding: 4px;
                outline: 0;
                selection-background-color: #e7efff;
                selection-color: #111827;
            }
            QTableWidget {
                gridline-color: transparent;
                alternate-background-color: #fbfbfd;
            }
            QTableWidget::item, QListWidget::item {
                padding: 6px;
                border-bottom: 1px solid #eef0f4;
            }
            QTableWidget::item:selected, QListWidget::item:selected {
                background: #dbeafe;
                color: #111827;
            }
            QListWidget::item:hover {
                background: #f1f5fb;
            }
            QTextEdit#detailsText {
                line-height: 1.35;
            }
            QHeaderView::section {
                background: #f1f2f5;
                border: 0;
                border-right: 1px solid #dde1e8;
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
            QPushButton#primaryButton {
                color: #ffffff;
                background: #2563eb;
                border-color: #1d4ed8;
                font-weight: 700;
            }
            QPushButton#primaryButton:hover {
                background: #1d4ed8;
                border-color: #1e40af;
            }
            QPushButton#approveButton {
                color: #ffffff;
                background: #16834a;
                border-color: #126b3d;
                font-weight: 700;
            }
            QPushButton#approveButton:hover {
                background: #126b3d;
                border-color: #0f5b34;
            }
            QPushButton#dangerButton {
                color: #ffffff;
                background: #dc2626;
                border-color: #b91c1c;
                font-weight: 700;
            }
            QPushButton#dangerButton:hover {
                background: #b91c1c;
                border-color: #991b1b;
            }
            QPushButton#quietButton {
                background: #fbfbfd;
            }
            QPushButton:disabled {
                color: #9a9aa2;
                background: #f4f4f6;
                border-color: #d9d9df;
            }
            QCheckBox {
                spacing: 8px;
                color: #343a46;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #b8bfca;
                background: #ffffff;
            }
            QCheckBox::indicator:hover {
                border-color: #8aa4d6;
            }
            QCheckBox::indicator:checked {
                background: #2563eb;
                border-color: #2563eb;
            }
            QCheckBox::indicator:checked:disabled {
                background: #9ca3af;
                border-color: #9ca3af;
            }
            QProgressBar {
                background: #e9e9ef;
                border: 0;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background: #2563eb;
                border-radius: 4px;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 3px 2px 3px 0;
            }
            QScrollBar::handle:vertical {
                background: #c9ced8;
                border-radius: 5px;
                min-height: 34px;
            }
            QScrollBar::handle:vertical:hover {
                background: #aeb6c4;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
                border: 0;
                background: transparent;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 10px;
                margin: 0 3px 2px 3px;
            }
            QScrollBar::handle:horizontal {
                background: #c9ced8;
                border-radius: 5px;
                min-width: 34px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #aeb6c4;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
                border: 0;
                background: transparent;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
            QSplitter::handle {
                background: transparent;
            }
            QSplitter::handle:hover {
                background: #e4e9f2;
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
