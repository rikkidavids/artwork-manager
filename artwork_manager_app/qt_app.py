"""PySide6 UI for artwork review.

This view reuses the existing database, state, scanner, provider, and media
helpers while the migration from the established Tk window continues.
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .approval import (
    ApprovalBlocked,
    approve_candidate,
    candidate_needs_warning,
    candidate_warning_text,
    convert_embedded_artwork,
)
from . import database as db
from .config import (
    APP_DIR,
    BUILD_VERSION,
    get_batch_search_count,
    get_deep_scan_all_files,
    get_fetch_min_artwork_size,
    get_max_candidates_per_album,
    get_nas_worker_timeout,
    get_preferred_artwork_size,
    get_scan_min_artwork_size,
    get_scan_worker_threads,
    get_target_size_match_mode,
    load_settings,
    save_settings,
)
from .review_queue import build_candidates, google_images_url, manual_import
from .remote_worker import check_worker, deep_check_album_remote, worker_enabled_for_path, worker_status, worker_update_hint
from .providers.deezer import DeezerProvider
from .providers.discogs import DiscogsProvider
from .providers.itunes import ITunesProvider
from .providers.musicbrainz import MusicBrainzProvider
from .scanner import (
    _deep_check_album_files,
    analyze_album_folder,
    deep_check_album_problem_files,
    embedded_artwork,
    inspect_album_identity,
    scan_library,
    write_low_res_csv,
)
from .state import evaluate_album_record, evaluate_album_state, workflow_bucket_for_status
from .utils import open_path

MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
WORK_BUCKETS = {'Missing', 'Needs Search', 'Not Square', 'Convert'}
ACTIONABLE_BUCKETS = WORK_BUCKETS | {'Review'}
DONE_BUCKETS = {'Good', 'Handled'}
QUEUE_FILTERS = ('Needs Work', 'Review', 'Done', 'All')
FILTER_CHIPS = (
    ('Needs Work', 'Needs Work'),
    ('Review', 'Review'),
    ('Done', 'Done'),
    ('All', 'All'),
)
FILTER_TOOLTIPS = {
    'Needs Work': 'Albums that need artwork, conversion, or a fresh search.',
    'Review': 'Albums with saved artwork options ready to check.',
    'Done': 'Albums already marked good, approved, handled, or skipped.',
    'All': 'Show every album record.',
}
PROVIDER_OPTIONS = (
    ('deezer_enabled', 'Deezer'),
    ('itunes_enabled', 'Apple / iTunes'),
    ('musicbrainz_enabled', 'MusicBrainz / Cover Art Archive'),
    ('discogs_enabled', 'Discogs'),
    ('fanarttv_enabled', 'fanart.tv'),
)
QUEUE_COLUMNS = ('status', 'artist', 'album', 'current', 'candidates')
DEFAULT_QUEUE_COLUMN_WIDTHS = {
    'status': 104,
    'artist': 220,
    'album': 360,
    'current': 128,
    'candidates': 68,
}
QUEUE_COLUMN_MIN_WIDTHS = {
    'status': 88,
    'artist': 90,
    'album': 110,
    'current': 104,
    'candidates': 52,
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


def _queue_status_label(bucket: str) -> str:
    return {
        'Needs Search': 'Search',
        'Not Square': 'Square',
        'Handled': 'Done',
    }.get(bucket, bucket)


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


def _candidate_dimensions(candidate: Dict[str, Any]) -> str:
    width, height = candidate.get('width'), candidate.get('height')
    if width and height:
        return f'{width} x {height}'
    return 'Unknown size'


def _candidate_warnings(candidate: Dict[str, Any]) -> List[str]:
    warnings = candidate.get('warnings') or []
    if isinstance(warnings, str):
        warnings = [warnings]
    return [_text(w) for w in warnings if _text(w)]


def _candidate_quality_hint(candidate: Dict[str, Any]) -> str:
    try:
        score = int(candidate.get('score') or 0)
    except Exception:
        score = 0
    warnings = ' '.join(w.lower() for w in _candidate_warnings(candidate))
    if 'not square' in warnings or 'aspect' in warnings or 'stretched' in warnings:
        return 'Check shape'
    if 'below target' in warnings:
        return 'Below target'
    if 'small file' in warnings or 'small size' in warnings or 'blurry' in warnings or 'soft' in warnings:
        return 'Check quality'
    if score >= 85:
        return 'Excellent'
    if score >= 60:
        return 'Usable'
    return 'Weak'


def _candidate_option_meta(candidate: Dict[str, Any]) -> str:
    return f"{_candidate_dimensions(candidate)} - {int(candidate.get('score') or 0)}/100 - {_candidate_quality_hint(candidate)}"


def _deep_count(deep: Dict[str, Any], key: str) -> int:
    try:
        return max(0, int(deep.get(key) or 0))
    except Exception:
        return 0


def _deep_check_summary_text(deep: Dict[str, Any]) -> str:
    deep = deep or {}
    checked = _deep_count(deep, 'checked_files')
    bits = []
    for key, label in (
        ('missing_count', 'missing'),
        ('below_target_count', 'below target'),
        ('non_square_count', 'not square'),
        ('incompatible_count', 'not baseline'),
        ('unreadable_count', 'unreadable'),
    ):
        count = _deep_count(deep, key)
        if count:
            bits.append(f'{count}/{checked or count} {label}')
    if bits:
        return '; '.join(bits)
    if checked:
        return f'{checked} file(s) OK'
    return 'No supported music files checked'


def _musicbrainz_artist(raw: Dict[str, Any]) -> str:
    try:
        return ' / '.join(
            _text(credit.get('artist', {}).get('name'))
            for credit in raw.get('artist-credit', [])
            if isinstance(credit, dict) and isinstance(credit.get('artist'), dict)
        )
    except Exception:
        return ''


def _release_row_values(item: Dict[str, Any]) -> List[str]:
    source = _text(item.get('source'), 'Source')
    raw = item.get('raw') if isinstance(item.get('raw'), dict) else {}
    if source == 'MusicBrainz':
        title = _text(raw.get('title'))
        artist = _musicbrainz_artist(raw)
        date = _text(raw.get('date'))[:4]
        country = _text(raw.get('country'))
        extra = f"score {_text(raw.get('score') or raw.get('_local_score'))}".strip()
    elif source == 'Deezer':
        title = _text(raw.get('title'))
        artist_obj = raw.get('artist') if isinstance(raw.get('artist'), dict) else {}
        artist = _text(artist_obj.get('name'))
        date = _text(raw.get('release_date'))[:4]
        country = ''
        extra = f"Deezer ID {_text(raw.get('id'))}".strip()
    elif source == 'iTunes':
        title = _text(raw.get('collectionName'))
        artist = _text(raw.get('artistName'))
        date = _text(raw.get('releaseDate'))[:4]
        country = _text(raw.get('country'))
        extra = f"iTunes ID {_text(raw.get('collectionId'))}".strip()
    else:
        title = _text(raw.get('title'))
        artist = title.split(' - ', 1)[0] if ' - ' in title else ''
        date = _text(raw.get('year'))
        country = _text(raw.get('country'))
        extra = ', '.join(_text(fmt) for fmt in (raw.get('format') or []) if _text(fmt))
    return [source, title, artist, date, country, extra]


def _provider_for_release_source(source: str):
    source = _text(source)
    if source == 'MusicBrainz':
        return MusicBrainzProvider()
    if source == 'Deezer':
        return DeezerProvider()
    if source == 'iTunes':
        return ITunesProvider()
    if source == 'Discogs':
        return DiscogsProvider()
    raise ValueError(f'Unsupported release source: {source}')


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
    elif kind == 'scan':
        painter.drawLine(3.5, 7, 7.5, 7)
        painter.drawLine(7.5, 7, 9.5, 9)
        painter.drawLine(9.5, 9, 16.5, 9)
        painter.drawLine(16.5, 9, 16.5, 15)
        painter.drawLine(16.5, 15, 3.5, 15)
        painter.drawLine(3.5, 15, 3.5, 7)
        painter.drawEllipse(QRectF(9, 3.5, 5.5, 5.5))
        painter.drawLine(13.5, 8, 17, 11.5)
    elif kind == 'settings':
        painter.drawEllipse(QRectF(7, 7, 6, 6))
        painter.drawLine(10, 3, 10, 5.5)
        painter.drawLine(10, 14.5, 10, 17)
        painter.drawLine(3, 10, 5.5, 10)
        painter.drawLine(14.5, 10, 17, 10)
        painter.drawLine(5, 5, 6.8, 6.8)
        painter.drawLine(13.2, 13.2, 15, 15)
        painter.drawLine(15, 5, 13.2, 6.8)
        painter.drawLine(6.8, 13.2, 5, 15)
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
    elif kind == 'more':
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(QRectF(4.5, 8.5, 2.8, 2.8))
        painter.drawEllipse(QRectF(8.6, 8.5, 2.8, 2.8))
        painter.drawEllipse(QRectF(12.7, 8.5, 2.8, 2.8))
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
    # Keep NAS browsing light: only inspect the album top level for a preview file.
    try:
        for name in sorted(os.listdir(album_path), key=lambda item: item.lower()):
            fp = os.path.join(album_path, name)
            if os.path.isfile(fp) and name.lower().endswith(MUSIC_EXTENSIONS):
                return fp
    except Exception:
        return ''
    return ''


def _library_root_for_album_path(album_path: str) -> str:
    path = Path(album_path).expanduser()
    try:
        return str(path.parent.parent) if path.parent and path.parent.parent else str(path.parent)
    except Exception:
        return str(path.parent)


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


class ConvertSaveWorker(QThread):
    progress = Signal(int, int, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, album: Dict[str, Any], backup: bool, settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.album = dict(album or {})
        self.backup = bool(backup)
        self.settings = dict(settings or {})

    def run(self) -> None:
        try:
            result = convert_embedded_artwork(
                self.album,
                backup=self.backup,
                settings=self.settings,
                progress=lambda done, total, path: self.progress.emit(int(done or 0), int(total or 0), str(path or '')),
            )
            self.completed.emit(result)
        except ApprovalBlocked as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(f'Convert/Save failed: {exc}')


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
                'album_keys': [info.get('album_key') for info in self.infos if info.get('album_key')],
                'album_count': len(self.infos),
                'saved': saved,
                'stopped': self.stop_event.is_set(),
                'status_counts': status_counts,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class ReleaseSearchWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, artist: str, album: str, year: str, settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.artist = _text(artist)
        self.album = _text(album)
        self.year = _text(year)
        self.settings = dict(settings or {})
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            out: List[Dict[str, Any]] = []
            if self.settings.get('musicbrainz_enabled', True) and not self.stop_event.is_set():
                provider = MusicBrainzProvider()
                for rel in provider.search_releases(self.artist, self.album, year=self.year, limit=20, stop_event=self.stop_event):
                    out.append({'source': 'MusicBrainz', 'raw': rel})
            if self.settings.get('deezer_enabled', True) and not self.stop_event.is_set():
                provider = DeezerProvider()
                for res in provider.search_albums(self.artist, self.album, year=self.year, limit=20, stop_event=self.stop_event):
                    out.append({'source': 'Deezer', 'raw': res})
            if self.settings.get('itunes_enabled', True) and not self.stop_event.is_set():
                provider = ITunesProvider()
                for res in provider.search_albums(self.artist, self.album, year=self.year, limit=20, stop_event=self.stop_event):
                    out.append({'source': 'iTunes', 'raw': res})
            if self.settings.get('discogs_enabled', True) and not self.stop_event.is_set():
                provider = DiscogsProvider()
                if getattr(provider, 'token', ''):
                    for res in provider.search(self.artist, self.album, year=self.year, limit=20):
                        out.append({'source': 'Discogs', 'raw': res})
            self.completed.emit({'items': out, 'stopped': self.stop_event.is_set()})
        except Exception as exc:
            self.failed.emit(str(exc))


class ReleaseImportWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        album: Dict[str, Any],
        item: Dict[str, Any],
        artist: str,
        album_title: str,
        settings: Dict[str, Any],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.album = dict(album or {})
        self.item = dict(item or {})
        self.artist = _text(artist)
        self.album_title = _text(album_title)
        self.settings = dict(settings or {})
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            album_key = _text(self.album.get('album_key'))
            album_path = _text(self.album.get('album_path'))
            if not album_key:
                raise ValueError('The selected album has no saved queue key.')
            provider = _provider_for_release_source(_text(self.item.get('source')))
            cands = provider.get_candidates_from_release(
                self.artist or _text(self.album.get('artist')),
                self.album_title or _text(self.album.get('album')),
                album_key,
                self.item.get('raw') if isinstance(self.item.get('raw'), dict) else {},
                max_candidates=8,
                stop_event=self.stop_event,
            )
            added = 0
            for cand in cands:
                cand = dict(cand or {})
                cand.update({'album_folder': album_path})
                cand['candidate_id'] = db.add_candidate(album_key, cand)
                added += 1
            if added:
                db.set_album_status(album_key, 'candidate_found')
                db.update_album_notes(album_key, {
                    'state_evaluation': {
                        'status': 'candidate_found',
                        'reason': f'{added} selected-release artwork option(s) imported',
                    }
                })
            self.completed.emit({'album_key': album_key, 'added': added})
        except Exception as exc:
            self.failed.emit(str(exc))


class AlbumRescanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, album: Dict[str, Any], settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.album = dict(album or {})
        self.settings = dict(settings or {})

    def run(self) -> None:
        try:
            album_path = _text(self.album.get('album_path'))
            if not album_path or not os.path.isdir(album_path):
                raise ValueError('The selected album folder could not be found.')
            try:
                names = sorted(os.listdir(album_path), key=lambda item: item.lower())
            except Exception as exc:
                raise ValueError(f'Could not read the selected album folder: {exc}') from exc
            music = [
                name for name in names
                if os.path.isfile(os.path.join(album_path, name)) and name.lower().endswith(MUSIC_EXTENSIONS)
            ]
            if not music:
                self.completed.emit({
                    'album_key': _text(self.album.get('album_key')),
                    'message': 'No supported music files were found in the selected album folder.',
                    'status': '',
                })
                return

            library_root = _library_root_for_album_path(album_path)
            identity = inspect_album_identity(album_path, library_root, music)
            row = analyze_album_folder(
                album_path,
                library_root,
                include_missing=True,
                music_files=music,
                identity=identity,
                force_deep_check=False,
            )
            if row:
                artist, album_name, width, height, example, album_path2, album_key, identity = row
                width_value = None if width == 'Missing' else width
                height_value = None if height == 'Missing' else height
                try:
                    candidate_count = len(db.load_candidates_for_album(album_key, include_rejected=False))
                except Exception:
                    candidate_count = 0
                status, status_reason = evaluate_album_state(
                    width_value,
                    height_value,
                    identity.get('notes') or {},
                    current_status=(db.get_album(album_key) or {}).get('status') or 'needs_review',
                    candidate_count=candidate_count,
                    preserve_user_terminal=False,
                    settings=self.settings,
                )
                identity = dict(identity)
                notes = dict(identity.get('notes') or {})
                notes['state_evaluation'] = {'status': status, 'reason': status_reason}
                identity['notes'] = notes
                db.upsert_album(
                    album_key,
                    artist,
                    album_name,
                    album_path2,
                    status=status,
                    width=width_value,
                    height=height_value,
                    example_file=example,
                    meta=identity,
                )
            else:
                fresh = db.find_album_by_path(album_path) or {}
                album_key = _text(fresh.get('album_key') or self.album.get('album_key'))
                status = _text(fresh.get('status'), 'already_good')
                status_reason = 'current embedded artwork meets the configured rules'

            self.completed.emit({
                'album_key': album_key,
                'status': status,
                'reason': status_reason,
                'message': 'Album refreshed from disk.',
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class AlbumDeepCheckWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, album: Dict[str, Any], settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.album = dict(album or {})
        self.settings = dict(settings or {})

    def run(self) -> None:
        try:
            album_path = _text(self.album.get('album_path'))
            album_key = _text(self.album.get('album_key'))
            if not album_path:
                raise ValueError('The selected album has no saved folder path.')
            target = get_preferred_artwork_size(self.settings)
            if worker_enabled_for_path(album_path, self.settings):
                result = deep_check_album_remote(
                    album_path,
                    target_size=target,
                    settings=self.settings,
                    problem_files=True,
                )
                deep = result.get('deep_file_check') if isinstance(result.get('deep_file_check'), dict) else {}
                rows = result.get('problem_files') if isinstance(result.get('problem_files'), list) else []
                source = 'NAS worker'
            else:
                if not os.path.isdir(album_path):
                    raise ValueError('The selected album folder could not be found.')
                try:
                    names = sorted(os.listdir(album_path), key=lambda item: item.lower())
                except Exception as exc:
                    raise ValueError(f'Could not read the selected album folder: {exc}') from exc
                music = [
                    name for name in names
                    if os.path.isfile(os.path.join(album_path, name)) and name.lower().endswith(MUSIC_EXTENSIONS)
                ]
                deep = _deep_check_album_files(album_path, music, target)
                deep['source'] = 'mac-local'
                rows = deep_check_album_problem_files(album_path, target_size=target, limit=500)
                result = {'deep_file_check': deep, 'problem_files': rows}
                source = 'Mac local'
            self.completed.emit({
                'album_key': album_key,
                'album_path': album_path,
                'target_size': target,
                'deep_file_check': deep,
                'problem_files': rows,
                'source': source,
                'raw_result': result,
            })
        except Exception as exc:
            self.failed.emit(str(exc))


class NasWorkerCheckWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, mode: str, settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.mode = mode
        self.settings = dict(settings or {})

    def run(self) -> None:
        try:
            if self.mode == 'status':
                result = worker_status(self.settings)
            else:
                result = check_worker(self.settings)
            self.completed.emit({'mode': self.mode, 'result': result})
        except Exception as exc:
            self.failed.emit(str(exc))


class LibraryScanWorker(QThread):
    progress = Signal(int, int, str)
    album_queued = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, library_root: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.library_root = library_root
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        rows = []
        try:
            db.start_scan(self.library_root, 0)

            def progress(done: int, total: int, path: str) -> None:
                self.progress.emit(int(done or 0), int(total or 0), str(path or ''))

            def on_album(row: object, info: Dict[str, Any], _count: int) -> None:
                rows.append(row)
                self.album_queued.emit({
                    'artist': _text(info.get('artist'), 'Unknown Artist'),
                    'album': _text(info.get('album'), 'Unknown Album'),
                    'album_key': info.get('album_key'),
                })

            scan_library(
                self.library_root,
                include_missing=True,
                progress=progress,
                stop_event=self.stop_event,
                on_album=on_album,
                total_albums=0,
                resume=True,
            )
            stopped = self.stop_event.is_set()
            db.finish_scan(stopped=stopped)
            csv_path = write_low_res_csv(rows) if rows else ''
            self.completed.emit({'stopped': stopped, 'queued': len(rows), 'csv': csv_path})
        except Exception as exc:
            try:
                db.finish_scan(stopped=self.stop_event.is_set())
            except Exception:
                pass
            self.failed.emit(str(exc))


class SettingsDialog(QDialog):
    settings_saved = Signal(object)

    def __init__(self, settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.settings = dict(settings or {})
        self.provider_checks: Dict[str, QCheckBox] = {}
        self.spinboxes: Dict[str, QSpinBox] = {}
        self.checkboxes: Dict[str, QCheckBox] = {}
        self.edits: Dict[str, QLineEdit] = {}
        self.target_mode = QComboBox()
        self.nas_check_worker: Optional[NasWorkerCheckWorker] = None

        self.setWindowTitle('Settings')
        self.resize(680, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        tabs = QTabWidget()
        tabs.addTab(self._scroll_tab(self._build_artwork_tab()), 'Artwork')
        tabs.addTab(self._scroll_tab(self._build_providers_tab()), 'Providers')
        tabs.addTab(self._scroll_tab(self._build_nas_tab()), 'NAS Worker')
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton('Save Settings', QDialogButtonBox.AcceptRole)
        save_btn.setObjectName('primaryButton')
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _scroll_tab(self, body: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(body)
        return scroll

    def _spin(self, key: str, value: int, minimum: int, maximum: int, suffix: str = '') -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(int(value or minimum))
        if suffix:
            spin.setSuffix(suffix)
        self.spinboxes[key] = spin
        return spin

    def _check(self, key: str, label: str, default: bool = False) -> QCheckBox:
        check = QCheckBox(label)
        check.setChecked(bool(self.settings.get(key, default)))
        self.checkboxes[key] = check
        return check

    def _edit(self, key: str, placeholder: str = '', password: bool = False) -> QLineEdit:
        edit = QLineEdit(_text(self.settings.get(key)))
        edit.setPlaceholderText(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.Password)
        self.edits[key] = edit
        return edit

    def _build_artwork_tab(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 8, 2)
        layout.setSpacing(12)

        scan_group = QGroupBox('Artwork Rules')
        form = QFormLayout(scan_group)
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow('Queue if embedded artwork is below', self._spin('scan_min_artwork_size', get_scan_min_artwork_size(self.settings), 1, 10000, ' px'))
        form.addRow('Only save fetched artwork at least', self._spin('fetch_min_artwork_size', get_fetch_min_artwork_size(self.settings), 1, 10000, ' px'))
        form.addRow('Preferred target size', self._spin('preferred_artwork_size', get_preferred_artwork_size(self.settings), 1, 10000, ' px'))
        self.target_mode.addItems(['Relaxed', 'Strict'])
        self.target_mode.setCurrentText(get_target_size_match_mode(self.settings))
        form.addRow('Target matching', self.target_mode)
        layout.addWidget(scan_group)

        scan_options = QGroupBox('Scanning')
        scan_layout = QVBoxLayout(scan_options)
        scan_layout.addWidget(self._check('deep_scan_all_files', 'Deep check every music file during scan', get_deep_scan_all_files(self.settings)))
        scan_form = QFormLayout()
        scan_form.addRow('Album folders checked at once', self._spin('scan_worker_threads', get_scan_worker_threads(self.settings), 1, 32))
        scan_layout.addLayout(scan_form)
        hint = QLabel('For NAS libraries, 4-12 workers is usually the useful range.')
        hint.setObjectName('mutedLabel')
        scan_layout.addWidget(hint)
        layout.addWidget(scan_options)

        review_group = QGroupBox('Review + Approval')
        review_form = QFormLayout(review_group)
        review_form.addRow('Options saved per album', self._spin('max_candidates_per_album', get_max_candidates_per_album(self.settings), 1, 25))
        review_form.addRow('Batch search count', self._spin('batch_search_count', get_batch_search_count(self.settings), 1, 50))
        review_form.addRow('', self._check('save_approved_artwork_to_album_folder', 'Save approved artwork into the album folder', False))
        review_form.addRow('', self._check('warn_before_low_confidence_embed', 'Warn before lower-confidence embeds', True))
        review_form.addRow('', self._check('resize_approved_artwork', 'Convert approved artwork to target-size JPEG', True))
        review_form.addRow('', self._check('verify_after_embed_before_good', 'Verify all files before marking Good', True))
        review_form.addRow('', self._check('backup_before_embedding', 'Backup before embed by default', False))
        layout.addWidget(review_group)
        layout.addStretch(1)
        return body

    def _build_providers_tab(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 8, 2)
        layout.setSpacing(12)

        group = QGroupBox('Artwork Providers')
        group_layout = QVBoxLayout(group)
        for key, label in PROVIDER_OPTIONS:
            check = QCheckBox(label)
            check.setChecked(bool(self.settings.get(key, True if key != 'fanarttv_enabled' else False)))
            self.provider_checks[key] = check
            group_layout.addWidget(check)
        layout.addWidget(group)

        discogs = QGroupBox('Discogs')
        discogs_form = QFormLayout(discogs)
        discogs_form.addRow('Token', self._edit('discogs_token', 'Optional fallback search token', password=True))
        hint = QLabel('Provider order is preserved from your existing settings; this dialog keeps the common on/off controls simple.')
        hint.setObjectName('mutedLabel')
        hint.setWordWrap(True)
        discogs_form.addRow('', hint)
        layout.addWidget(discogs)
        layout.addStretch(1)
        return body

    def _build_nas_tab(self) -> QWidget:
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 8, 2)
        layout.setSpacing(12)

        group = QGroupBox('NAS Worker')
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._check('nas_worker_enabled', 'Use NAS worker when path mapping matches', False))
        form = QFormLayout()
        form.addRow('Worker URL', self._edit('nas_worker_url', 'http://nas.local:8765'))
        form.addRow('API token', self._edit('nas_worker_token', '', password=True))
        form.addRow('Mac path prefix', self._edit('nas_worker_local_prefix', '/Volumes/Music'))
        form.addRow('Worker path prefix', self._edit('nas_worker_remote_prefix', '/music'))
        form.addRow('Timeout', self._spin('nas_worker_timeout', get_nas_worker_timeout(self.settings), 5, 7200, ' sec'))
        group_layout.addLayout(form)
        hint = QLabel('Example mapping: /Volumes/Music on the Mac maps to /music inside the worker container.')
        hint.setObjectName('mutedLabel')
        hint.setWordWrap(True)
        group_layout.addWidget(hint)

        actions = QHBoxLayout()
        self.nas_test_btn = QPushButton('Test Worker')
        self.nas_test_btn.setObjectName('quietButton')
        self.nas_test_btn.clicked.connect(lambda: self._run_nas_worker_check('test'))
        self.nas_status_btn = QPushButton('Worker Status')
        self.nas_status_btn.setObjectName('quietButton')
        self.nas_status_btn.clicked.connect(lambda: self._run_nas_worker_check('status'))
        actions.addWidget(self.nas_test_btn)
        actions.addWidget(self.nas_status_btn)
        actions.addStretch(1)
        group_layout.addLayout(actions)

        self.nas_result_label = QLabel('')
        self.nas_result_label.setObjectName('mutedLabel')
        self.nas_result_label.setWordWrap(True)
        group_layout.addWidget(self.nas_result_label)
        layout.addWidget(group)
        layout.addStretch(1)
        return body

    def _current_settings(self) -> Dict[str, Any]:
        settings = {
            'provider_order': self.settings.get('provider_order') or ['deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv'],
            'target_size_match_mode': self.target_mode.currentText() if self.target_mode.currentText() in {'Relaxed', 'Strict'} else 'Relaxed',
        }
        for key, check in self.provider_checks.items():
            settings[key] = check.isChecked()
        for key, spin in self.spinboxes.items():
            settings[key] = int(spin.value())
        for key, check in self.checkboxes.items():
            settings[key] = check.isChecked()
        for key, edit in self.edits.items():
            text = edit.text().strip()
            if key == 'nas_worker_remote_prefix' and not text:
                text = '/music'
            settings[key] = text
        return settings

    def _set_nas_check_busy(self, busy: bool, text: str = '') -> None:
        self.nas_test_btn.setEnabled(not busy)
        self.nas_status_btn.setEnabled(not busy)
        if text:
            self.nas_result_label.setText(text)

    def _run_nas_worker_check(self, mode: str) -> None:
        if self.nas_check_worker is not None and self.nas_check_worker.isRunning():
            return
        settings = self._current_settings()
        label = 'Reading worker status...' if mode == 'status' else 'Testing NAS worker...'
        self._set_nas_check_busy(True, label)
        worker = NasWorkerCheckWorker(mode, settings, self)
        worker.completed.connect(self._nas_worker_check_completed)
        worker.failed.connect(self._nas_worker_check_failed)
        worker.finished.connect(lambda w=worker: self._nas_worker_check_finished(w))
        self.nas_check_worker = worker
        worker.start()

    def _nas_worker_summary_lines(self, mode: str, result: Dict[str, Any]) -> List[str]:
        version = _text(result.get('version'), 'unknown')
        build = _text(result.get('worker_build'), 'unknown')
        api = _text(result.get('api'), 'unknown')
        compat = result.get('compatibility') if isinstance(result.get('compatibility'), dict) else {}
        timing = result.get('_request_duration_seconds')
        busy = 'busy' if result.get('busy') else 'idle'
        timing_text = f' - {float(timing):.2f}s' if isinstance(timing, (int, float)) else ''
        lines = [
            f'NAS worker {busy}: {version}{timing_text}',
            f'Build/API: {build} / {api}',
        ]
        if not compat.get('ok', True):
            lines.extend(['', f"Update needed: {compat.get('message') or 'incompatible worker'}", worker_update_hint()])
        if mode == 'status':
            uptime = int(result.get('uptime_seconds') or 0)
            lines.append(f'Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s')
            recent = result.get('recent_jobs') if isinstance(result.get('recent_jobs'), list) else []
            active = result.get('active_jobs') if isinstance(result.get('active_jobs'), list) else []
            if active:
                lines.extend(['', 'Active jobs:'])
                for job in active[:5]:
                    lines.append(f"- {_text(job.get('kind'), 'job')} - {_text(job.get('label'), 'album')}")
            if recent:
                lines.extend(['', 'Recent jobs:'])
                for job in recent[:6]:
                    ok = 'OK' if job.get('ok') else 'Failed'
                    duration = job.get('duration_seconds')
                    duration_text = f'{float(duration):.1f}s' if isinstance(duration, (int, float)) else '?s'
                    lines.append(f"- {ok} - {_text(job.get('kind'), 'job')} - {duration_text} - {_text(job.get('label'), 'album')}")
            if not active and not recent:
                lines.extend(['', 'No worker jobs recorded since the container started.'])
        else:
            roots = ', '.join(_text(root) for root in (result.get('music_roots') or []) if _text(root))
            if roots:
                lines.append(f'Music roots: {roots}')
        return lines

    def _nas_worker_check_completed(self, payload: object) -> None:
        payload = dict(payload or {})
        mode = _text(payload.get('mode'), 'test')
        result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
        lines = self._nas_worker_summary_lines(mode, result)
        self.nas_result_label.setText(lines[0] if lines else 'NAS worker check complete.')
        self._set_nas_check_busy(False)
        title = 'NAS Worker Status' if mode == 'status' else 'NAS Worker OK'
        QMessageBox.information(self, title, '\n'.join(lines))

    def _nas_worker_check_failed(self, message: str) -> None:
        message = _text(message, 'NAS worker check failed.')
        self.nas_result_label.setText(message)
        self._set_nas_check_busy(False)
        QMessageBox.warning(self, 'NAS worker check failed', message)

    def _nas_worker_check_finished(self, worker: NasWorkerCheckWorker) -> None:
        if self.nas_check_worker is worker:
            self.nas_check_worker = None
        self._set_nas_check_busy(False)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.nas_check_worker is not None and self.nas_check_worker.isRunning():
            QMessageBox.information(self, 'NAS worker check running', 'Wait for the NAS worker check to finish before closing Settings.')
            event.ignore()
            return
        super().closeEvent(event)

    def save(self) -> None:
        try:
            save_settings(self._current_settings())
            self.settings = load_settings()
            self.settings_saved.emit(self.settings)
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, 'Settings not saved', str(exc))


class ScanDialog(QDialog):
    scan_finished = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.settings = load_settings()
        self.worker: Optional[LibraryScanWorker] = None
        self.queued_count = 0

        self.setWindowTitle('Scan Library')
        self.resize(620, 260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        title = QLabel('Scan Library')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)

        hint = QLabel('Choose your music folder. The scan runs in the background and saves albums that need artwork into the queue.')
        hint.setObjectName('mutedLabel')
        hint.setWordWrap(True)
        layout.addWidget(hint)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(_text(self.settings.get('last_library_path')))
        self.path_edit.setPlaceholderText('Choose music library folder...')
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton('Choose')
        browse.setObjectName('quietButton')
        browse.clicked.connect(self.choose_folder)
        path_row.addWidget(browse)
        layout.addLayout(path_row)

        self.status_label = QLabel('Ready to scan.')
        self.status_label.setObjectName('mutedLabel')
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        actions = QHBoxLayout()
        self.start_btn = QPushButton('Start Scan')
        self.start_btn.setObjectName('primaryButton')
        self.start_btn.setIcon(_line_icon('scan', '#ffffff'))
        self.start_btn.clicked.connect(self.start_scan)
        actions.addWidget(self.start_btn)

        self.stop_btn = QPushButton('Stop')
        self.stop_btn.setObjectName('dangerButton')
        self.stop_btn.setIcon(_line_icon('stop', '#ffffff'))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_scan)
        actions.addWidget(self.stop_btn)
        actions.addStretch(1)

        close = QPushButton('Close')
        close.setObjectName('quietButton')
        close.clicked.connect(self.close)
        actions.addWidget(close)
        layout.addLayout(actions)

    def choose_folder(self) -> None:
        start = self.path_edit.text().strip() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, 'Choose Music Library', start)
        if folder:
            self.path_edit.setText(folder)

    def start_scan(self) -> None:
        folder = self.path_edit.text().strip()
        if not folder or not os.path.isdir(folder):
            QMessageBox.warning(self, 'Folder not found', 'Choose a valid music folder first.')
            return
        if self.worker is not None and self.worker.isRunning():
            return
        save_settings({'last_library_path': folder})
        self.settings = load_settings()
        self.queued_count = 0
        self.status_label.setText('Scanning library...')
        self.progress.setRange(0, 0)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        worker = LibraryScanWorker(folder, self)
        worker.progress.connect(self._scan_progress)
        worker.album_queued.connect(self._album_queued)
        worker.completed.connect(self._scan_completed)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(lambda w=worker: self._scan_worker_finished(w))
        self.worker = worker
        worker.start()

    def stop_scan(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.stop_btn.setEnabled(False)
            self.status_label.setText('Stopping after the current album finishes...')

    def _scan_progress(self, done: int, total: int, path: str) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(max(0, min(done, total)))
            self.status_label.setText(f'Checked {done:,} of {total:,} albums - {_path_tail(path, parts=3)}')
        else:
            self.progress.setRange(0, 0)
            self.status_label.setText(f'Checked {done:,} album folders - {_path_tail(path, parts=3)}')

    def _album_queued(self, info: object) -> None:
        self.queued_count += 1
        info = dict(info or {})
        self.status_label.setText(f'Queued {self.queued_count:,}: {_text(info.get("artist"))} - {_text(info.get("album"))}')

    def _scan_completed(self, result: object) -> None:
        result = dict(result or {})
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        queued = int(result.get('queued') or 0)
        if result.get('stopped'):
            message = f'Scan stopped. {queued:,} album(s) queued.'
        else:
            message = f'Scan complete. {queued:,} album(s) queued.'
        csv_path = _text(result.get('csv'))
        if csv_path:
            message += f' Report saved: {_path_tail(csv_path, parts=2)}'
        self.status_label.setText(message)
        self.scan_finished.emit()

    def _scan_failed(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText('Scan failed.')
        QMessageBox.warning(self, 'Scan failed', message)
        self.scan_finished.emit()

    def _scan_worker_finished(self, worker: LibraryScanWorker) -> None:
        if self.worker is worker:
            self.worker = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, 'Scan still running', 'Stop the scan before closing this window.')
            event.ignore()
            return
        super().closeEvent(event)


class SquareArtworkLabel(QLabel):
    def __init__(self, text: str = ''):
        super().__init__(text)
        self.setMinimumSize(180, 180)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(320, 320)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(180, 180)


class ImagePanel(QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName('imagePanel')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._syncing_square = False
        self.title_label = QLabel(title)
        self.title_label.setObjectName('panelTitle')
        self.image_label = SquareArtworkLabel('No artwork')
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setObjectName('artworkPreview')
        self.meta_label = QLabel('')
        self.meta_label.setObjectName('mutedLabel')
        self.meta_label.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addStretch(1)
        layout.addWidget(self.image_label, 0, Qt.AlignHCenter)
        layout.addStretch(1)
        layout.addWidget(self.meta_label)
        QTimer.singleShot(0, self._sync_preview_square)

    def set_placeholder(self, text: str, meta: str = '') -> None:
        self.image_label.setText(text)
        self.image_label.setPixmap(QPixmap())
        self.image_label.setProperty('sourcePixmap', None)
        self.meta_label.setText(meta)

    def set_image(self, source: Any, meta: str = '') -> bool:
        pix = _image_pixmap(source)
        if not pix:
            self.set_placeholder('No artwork', meta)
            return False
        self._sync_preview_square()
        target = self.image_label.size()
        scaled = pix.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setText('')
        self.image_label.setPixmap(scaled)
        self.image_label.setProperty('sourcePixmap', pix)
        self.meta_label.setText(meta)
        return True

    def _sync_preview_square(self) -> None:
        if self._syncing_square:
            return
        self._syncing_square = True
        try:
            margins = self.layout().contentsMargins()
            spacing = self.layout().spacing()
            title_h = self.title_label.sizeHint().height()
            meta_h = self.meta_label.sizeHint().height()
            available_w = max(120, self.width() - margins.left() - margins.right())
            available_h = max(120, self.height() - margins.top() - margins.bottom() - title_h - meta_h - (spacing * 4))
            side = max(180, min(available_w, available_h))
            size = QSize(side, side)
            if self.image_label.size() != size:
                self.image_label.setFixedSize(size)
        finally:
            self._syncing_square = False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._sync_preview_square()
        pix = self.image_label.property('sourcePixmap')
        if isinstance(pix, QPixmap) and not pix.isNull():
            scaled = pix.scaled(self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        super().resizeEvent(event)


class ElidedLabel(QLabel):
    def __init__(self, text: str = '', elide_mode: Qt.TextElideMode = Qt.ElideRight):
        super().__init__()
        self._full_text = ''
        self.elide_mode = elide_mode
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = str(text or '')
        self.setToolTip(self._full_text)
        self.update()

    def text(self) -> str:
        return self._full_text

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, self.fontMetrics().height() + 6)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, self.fontMetrics().height() + 6)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(self.foregroundRole()))
        margins = self.contentsMargins()
        rect = self.rect().adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        text = self.fontMetrics().elidedText(self._full_text, self.elide_mode, max(0, rect.width()))
        painter.drawText(rect, self.alignment() | Qt.AlignVCenter, text)
        painter.end()


class CandidateOptionWidget(QFrame):
    def __init__(self, candidate: Dict[str, Any]):
        super().__init__()
        self.setObjectName('candidateOption')
        self.setProperty('selected', False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        thumb = QLabel('Art')
        thumb.setObjectName('candidateThumb')
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setFixedSize(48, 48)
        thumb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        pix = _image_pixmap(candidate.get('image_path'))
        if pix and not pix.isNull():
            thumb.setText('')
            thumb.setPixmap(pix.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        source = ElidedLabel(_text(candidate.get('source'), 'Artwork'))
        source.setObjectName('candidateSource')
        source.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        meta = ElidedLabel(_candidate_option_meta(candidate))
        meta.setObjectName('candidateMeta')
        meta.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        release = ElidedLabel(_text(candidate.get('release_title'), 'Saved artwork option'))
        release.setObjectName('candidateRelease')
        release.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)
        text_layout.addWidget(source)
        text_layout.addWidget(meta)
        text_layout.addWidget(release)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(9)
        layout.addWidget(thumb, 0, Qt.AlignTop)
        layout.addLayout(text_layout, 1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty('selected', bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(0, 66)


class ProblemFilesDialog(QDialog):
    def __init__(self, album: Dict[str, Any], result: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle('Problem Files')
        self.resize(780, 540)
        self.setMinimumSize(620, 420)

        rows = result.get('problem_files') if isinstance(result.get('problem_files'), list) else []
        deep = result.get('deep_file_check') if isinstance(result.get('deep_file_check'), dict) else {}
        target = int(result.get('target_size') or deep.get('target_size') or 0)
        source = _text(result.get('source') or deep.get('source'), 'Deep Check')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel('Problem Files')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)

        album_title = ElidedLabel(f"{_text(album.get('artist'), 'Unknown Artist')} - {_text(album.get('album'), 'Unknown Album')}")
        album_title.setObjectName('mutedLabel')
        layout.addWidget(album_title)

        folder = ElidedLabel(_text(result.get('album_path') or album.get('album_path')))
        folder.setObjectName('mutedLabel')
        layout.addWidget(folder)

        target_text = f'Target: {target}px square baseline JPEG' if target else 'Target: configured artwork rules'
        summary = QLabel(f'{target_text} - {source} - {_deep_check_summary_text(deep)}')
        summary.setObjectName('mutedLabel')
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.table = QTableWidget(len(rows), 3)
        self.table.setHorizontalHeaderLabels(['File', 'Size', 'Issue'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for row_index, row in enumerate(rows):
            file_item = QTableWidgetItem(_text(row.get('file'), '-'))
            size_item = QTableWidgetItem(_text(row.get('dimensions'), '-'))
            issues = ', '.join(_text(issue) for issue in (row.get('issues') or []) if _text(issue))
            issue_item = QTableWidgetItem(issues or '-')
            self.table.setItem(row_index, 0, file_item)
            self.table.setItem(row_index, 1, size_item)
            self.table.setItem(row_index, 2, issue_item)
        layout.addWidget(self.table, 1)

        if not rows:
            empty = QLabel('No problem files found by the on-demand check.')
            empty.setObjectName('emptyHint')
            layout.addWidget(empty)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ReleasePickerDialog(QDialog):
    release_imported = Signal(object)

    def __init__(self, album: Dict[str, Any], settings: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.album = dict(album or {})
        self.settings = dict(settings or {})
        self.results: List[Dict[str, Any]] = []
        self.search_worker: Optional[ReleaseSearchWorker] = None
        self.import_worker: Optional[ReleaseImportWorker] = None

        self.setWindowTitle('Choose Release')
        self.resize(860, 560)
        self.setMinimumSize(680, 440)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        title = QLabel('Choose Release')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)

        hint = QLabel('Use this when automatic searching picked the wrong album artwork.')
        hint.setObjectName('mutedLabel')
        layout.addWidget(hint)

        form_wrap = QFrame()
        form_layout = QGridLayout(form_wrap)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setHorizontalSpacing(8)
        form_layout.setVerticalSpacing(8)
        self.artist_edit = QLineEdit(_text(self.album.get('search_artist') or self.album.get('artist')))
        self.album_edit = QLineEdit(_text(self.album.get('search_album') or self.album.get('album')))
        self.year_edit = QLineEdit(_text(self.album.get('year')))
        self.year_edit.setMaximumWidth(90)
        self.search_btn = QPushButton('Search Releases')
        self.search_btn.setObjectName('primaryButton')
        self.search_btn.clicked.connect(self.search_releases)
        form_layout.addWidget(QLabel('Artist'), 0, 0)
        form_layout.addWidget(self.artist_edit, 0, 1)
        form_layout.addWidget(QLabel('Album'), 1, 0)
        form_layout.addWidget(self.album_edit, 1, 1)
        form_layout.addWidget(QLabel('Year'), 0, 2)
        form_layout.addWidget(self.year_edit, 0, 3)
        form_layout.addWidget(self.search_btn, 1, 2, 1, 2)
        form_layout.setColumnStretch(1, 1)
        layout.addWidget(form_wrap)

        self.status_label = QLabel('Search for the exact album/release, then choose the one that matches your copy.')
        self.status_label.setObjectName('mutedLabel')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(['Source', 'Release / Album', 'Artist', 'Year', 'Country', 'Format / Score'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        widths = [96, 250, 170, 66, 78, 150]
        for col, width in enumerate(widths):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._refresh_use_state)
        self.table.doubleClicked.connect(lambda _idx=None: self.use_selected_release())
        layout.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        self.browser_btn = QPushButton('Open Search in Browser')
        self.browser_btn.setObjectName('quietButton')
        self.browser_btn.clicked.connect(self.open_browser_search)
        bottom.addWidget(self.browser_btn)
        bottom.addStretch(1)
        self.use_btn = QPushButton('Use Selected Release')
        self.use_btn.setObjectName('approveButton')
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self.use_selected_release)
        self.close_btn = QPushButton('Close')
        self.close_btn.setObjectName('quietButton')
        self.close_btn.clicked.connect(self.reject)
        bottom.addWidget(self.use_btn)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

        QTimer.singleShot(150, self.search_releases)

    def _busy(self) -> bool:
        return any([
            self.search_worker is not None and self.search_worker.isRunning(),
            self.import_worker is not None and self.import_worker.isRunning(),
        ])

    def _set_busy(self, busy: bool, text: str = '') -> None:
        self.search_btn.setEnabled(not busy)
        self.browser_btn.setEnabled(not busy)
        self.use_btn.setEnabled(False if busy else self.table.currentRow() >= 0)
        self.close_btn.setEnabled(not busy)
        if text:
            self.status_label.setText(text)

    def search_releases(self) -> None:
        if self._busy():
            return
        artist = self.artist_edit.text().strip()
        album = self.album_edit.text().strip()
        year = self.year_edit.text().strip()
        if not artist and not album:
            self.status_label.setText('Enter an artist or album name first.')
            return
        self.results = []
        self.table.setRowCount(0)
        self._set_busy(True, 'Searching MusicBrainz, Deezer, iTunes, and Discogs releases...')
        worker = ReleaseSearchWorker(artist, album, year, self.settings, self)
        worker.completed.connect(self._release_search_completed)
        worker.failed.connect(self._release_search_failed)
        worker.finished.connect(lambda w=worker: self._release_search_finished(w))
        self.search_worker = worker
        worker.start()

    def _release_search_completed(self, payload: object) -> None:
        payload = dict(payload or {})
        self.results = [dict(item or {}) for item in (payload.get('items') or [])]
        self.table.setRowCount(len(self.results))
        for row, item in enumerate(self.results):
            for col, value in enumerate(_release_row_values(item)):
                self.table.setItem(row, col, QTableWidgetItem(value))
        if self.results:
            self.table.selectRow(0)
        stopped = ' Search stopped.' if payload.get('stopped') else ''
        self.status_label.setText(f'Found {len(self.results)} release match(es).{stopped}')
        self._set_busy(False)
        self._refresh_use_state()

    def _release_search_failed(self, message: str) -> None:
        self.status_label.setText(f'Release search failed: {_text(message)}')
        self._set_busy(False)
        QMessageBox.warning(self, 'Release search failed', _text(message, 'Release search failed.'))

    def _release_search_finished(self, worker: ReleaseSearchWorker) -> None:
        if self.search_worker is worker:
            self.search_worker = None
        self._set_busy(False)

    def _refresh_use_state(self) -> None:
        self.use_btn.setEnabled(bool(not self._busy() and 0 <= self.table.currentRow() < len(self.results)))

    def _selected_release_item(self) -> Optional[Dict[str, Any]]:
        row = self.table.currentRow()
        if 0 <= row < len(self.results):
            return self.results[row]
        return None

    def use_selected_release(self) -> None:
        item = self._selected_release_item()
        if not item or self._busy():
            return
        self._set_busy(True, 'Downloading artwork from the selected release...')
        worker = ReleaseImportWorker(
            self.album,
            item,
            self.artist_edit.text().strip(),
            self.album_edit.text().strip(),
            self.settings,
            self,
        )
        worker.completed.connect(self._release_import_completed)
        worker.failed.connect(self._release_import_failed)
        worker.finished.connect(lambda w=worker: self._release_import_finished(w))
        self.import_worker = worker
        worker.start()

    def _release_import_completed(self, payload: object) -> None:
        payload = dict(payload or {})
        added = int(payload.get('added') or 0)
        if added:
            self.status_label.setText(f'Added {added} artwork option(s) from the selected release.')
            self.release_imported.emit(payload)
            self.accept()
        else:
            self.status_label.setText('Selected release had no artwork at or above the fetch minimum. Try another release or use Google/import.')
            self._set_busy(False)

    def _release_import_failed(self, message: str) -> None:
        self.status_label.setText(f'Artwork download failed: {_text(message)}')
        self._set_busy(False)
        QMessageBox.warning(self, 'Selected release failed', _text(message, 'Artwork download failed.'))

    def _release_import_finished(self, worker: ReleaseImportWorker) -> None:
        if self.import_worker is worker:
            self.import_worker = None
        self._set_busy(False)

    def open_browser_search(self) -> None:
        artist = self.artist_edit.text().strip()
        album = self.album_edit.text().strip()
        query = quote(f'{artist} {album} deezer itunes apple discogs musicbrainz release'.strip())
        if query:
            webbrowser.open(f'https://www.google.com/search?q={query}')

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._busy():
            QMessageBox.information(self, 'Release search running', 'Wait for the release search/download to finish before closing this window.')
            event.ignore()
            return
        super().closeEvent(event)


class QtArtworkWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.albums: List[Dict[str, Any]] = []
        self.visible_albums: List[Dict[str, Any]] = []
        self.current_album: Optional[Dict[str, Any]] = None
        self.current_candidates: List[Dict[str, Any]] = []
        self.candidate_widgets: List[CandidateOptionWidget] = []
        self.queue_filter = 'All'
        self.filter_chips: Dict[str, QPushButton] = {}
        self.current_art_worker: Optional[CurrentArtWorker] = None
        self.current_art_workers: List[CurrentArtWorker] = []
        self.approval_worker: Optional[ApprovalWorker] = None
        self.convert_worker: Optional[ConvertSaveWorker] = None
        self.search_worker: Optional[SearchWorker] = None
        self.album_rescan_worker: Optional[AlbumRescanWorker] = None
        self.deep_check_worker: Optional[AlbumDeepCheckWorker] = None
        self.scan_dialog: Optional[ScanDialog] = None
        self.last_approval_result: Optional[Dict[str, Any]] = None
        self.last_convert_result: Optional[Dict[str, Any]] = None
        self.last_search_log: List[str] = []
        self.last_search_album_key = ''
        self.pending_approval_row = 0
        self._restoring_queue_controls = False
        self._restoring_queue_columns = False
        self._restoring_main_splitter = False
        self.queue_filter_save_timer = QTimer(self)
        self.queue_filter_save_timer.setSingleShot(True)
        self.queue_filter_save_timer.timeout.connect(self._save_queue_filter_state)
        self.queue_column_save_timer = QTimer(self)
        self.queue_column_save_timer.setSingleShot(True)
        self.queue_column_save_timer.timeout.connect(self._save_queue_column_widths)
        self.main_splitter_save_timer = QTimer(self)
        self.main_splitter_save_timer.setSingleShot(True)
        self.main_splitter_save_timer.timeout.connect(self._save_main_splitter_sizes)
        self._first_queue_load = True

        self.setWindowTitle(f'Artwork Manager - {BUILD_VERSION}')
        self.resize(1380, 860)
        self.setMinimumSize(1100, 700)

        self._build_actions()
        self._build_ui()
        self._apply_style()
        QApplication.instance().installEventFilter(self)
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

        scan = QAction(_line_icon('scan'), 'Scan Library', self)
        scan.setToolTip('Scan your music folder and update the review queue')
        scan.triggered.connect(self.open_scan_dialog)
        toolbar.addAction(scan)

        settings = QAction(_line_icon('settings'), 'Settings', self)
        settings.setToolTip('Artwork, provider, approval, and NAS worker settings')
        settings.triggered.connect(self.open_settings_dialog)
        toolbar.addAction(settings)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(12, 10, 12, 12)
        root_layout.setSpacing(10)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(self._main_splitter_moved)
        self.main_splitter = splitter
        root_layout.addWidget(splitter, 1)

        left = self._build_queue_panel()
        right = self._build_review_panel()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        QTimer.singleShot(0, self._apply_main_splitter_sizes)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())

    def open_scan_dialog(self) -> None:
        if self.scan_dialog is not None and self.scan_dialog.isVisible():
            self.scan_dialog.raise_()
            self.scan_dialog.activateWindow()
            return
        dialog = ScanDialog(self)
        dialog.scan_finished.connect(lambda: self.reload_queue(select_first=False))
        dialog.finished.connect(lambda _result=0, dlg=dialog: self._scan_dialog_closed(dlg))
        self.scan_dialog = dialog
        dialog.show()

    def _scan_dialog_closed(self, dialog: ScanDialog) -> None:
        if self.scan_dialog is dialog:
            self.scan_dialog = None

    def open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        dialog.settings_saved.connect(self._settings_saved)
        dialog.exec()

    def _settings_saved(self, settings: object) -> None:
        self.settings = dict(settings or load_settings())
        self.backup_checkbox.blockSignals(True)
        try:
            self.backup_checkbox.setChecked(bool(self.settings.get('backup_before_embedding', False)))
        finally:
            self.backup_checkbox.blockSignals(False)
        self.reload_queue(select_first=False)
        self.statusBar().showMessage('Settings saved.')

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
        saved_layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
        self.queue_filter = self._normalise_queue_filter(saved_layout.get('queue_filter'))
        saved_search = _text(saved_layout.get('queue_search'))

        self._restoring_queue_controls = True
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search artist, album, folder...')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setText(saved_search)
        self._restoring_queue_controls = False
        self.search_edit.textChanged.connect(self._queue_search_changed)
        controls.addWidget(self.search_edit, 1)
        layout.addLayout(controls)

        chips = QHBoxLayout()
        chips.setSpacing(4)
        for filter_name, label in FILTER_CHIPS:
            chip = QPushButton(label)
            chip.setObjectName('filterChip')
            chip.setCheckable(True)
            chip.setMinimumWidth(0)
            chip.setMinimumHeight(28)
            chip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            chip.setToolTip(FILTER_TOOLTIPS.get(filter_name, ''))
            chip.clicked.connect(lambda _checked=False, name=filter_name: self._set_queue_filter(name))
            chips.addWidget(chip)
            self.filter_chips[filter_name] = chip
        chips.addStretch(1)
        layout.addLayout(chips)

        self.queue_empty_label = QLabel('')
        self.queue_empty_label.setObjectName('emptyHint')
        self.queue_empty_label.setVisible(False)
        layout.addWidget(self.queue_empty_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Status', 'Artist', 'Album', 'Size', 'Opts'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setCornerButtonEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsMovable(False)
        header.setMinimumSectionSize(min(QUEUE_COLUMN_MIN_WIDTHS.values()))
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

        self.album_title = ElidedLabel('No album selected')
        self.album_title.setObjectName('albumTitle')
        self.album_subtitle = ElidedLabel('')
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
        self.candidate_list.setTextElideMode(Qt.ElideRight)
        self.candidate_list.currentRowChanged.connect(self._select_candidate)
        lower.addWidget(self.candidate_list)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setObjectName('detailsText')
        self.details.setLineWrapMode(QTextEdit.WidgetWidth)
        self.details.setPlaceholderText('Select an album to review.')
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

        options = QHBoxLayout()
        options.addStretch(1)
        self.import_btn = QPushButton('Import Image')
        self.import_btn.setObjectName('quietButton')
        self.import_btn.setIcon(_line_icon('folder'))
        self.import_btn.setIconSize(QSize(16, 16))
        self.import_btn.clicked.connect(self.import_image_for_current_album)
        self.more_menu = QMenu(self)
        self.google_action = QAction(_line_icon('search'), 'Open Google Images', self)
        self.google_action.triggered.connect(self.open_google_images_for_current_album)
        self.choose_release_action = QAction(_line_icon('search'), 'Choose Release', self)
        self.choose_release_action.triggered.connect(self.choose_release_for_current_album)
        self.refresh_album_action = QAction(_line_icon('refresh'), 'Refresh From Disk', self)
        self.refresh_album_action.triggered.connect(self.refresh_current_album_from_disk)
        self.problem_files_action = QAction(_line_icon('scan'), 'Show Problem Files', self)
        self.problem_files_action.triggered.connect(self.show_problem_files_for_current_album)
        self.convert_save_action = QAction(_line_icon('refresh'), 'Convert/Save Current Artwork', self)
        self.convert_save_action.triggered.connect(self.convert_save_current_artwork)
        self.mark_good_action = QAction(_line_icon('check'), 'Mark Current Artwork Good', self)
        self.mark_good_action.triggered.connect(self.mark_current_album_good)
        self.ignore_action = QAction(_line_icon('stop'), 'Ignore Album', self)
        self.ignore_action.triggered.connect(self.ignore_current_album)
        self.rework_action = QAction(_line_icon('refresh'), 'Rework Album', self)
        self.rework_action.triggered.connect(self.rework_current_album)
        self.more_menu.addAction(self.google_action)
        self.more_menu.addAction(self.choose_release_action)
        self.more_menu.addAction(self.refresh_album_action)
        self.more_menu.addAction(self.problem_files_action)
        self.more_menu.addAction(self.convert_save_action)
        self.more_menu.addSeparator()
        self.more_menu.addAction(self.mark_good_action)
        self.more_menu.addAction(self.ignore_action)
        self.more_menu.addSeparator()
        self.more_menu.addAction(self.rework_action)
        self.more_btn = QPushButton('More')
        self.more_btn.setObjectName('quietButton')
        self.more_btn.setIcon(_line_icon('more'))
        self.more_btn.setIconSize(QSize(16, 16))
        self.more_btn.setMenu(self.more_menu)
        self.more_btn.setToolTip('More album actions')
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
        options.addWidget(self.import_btn)
        options.addWidget(self.more_btn)
        options.addWidget(self.open_folder_btn)
        options.addWidget(self.open_source_btn)
        self.backup_checkbox = QCheckBox('Backup before embed')
        self.backup_checkbox.setObjectName('inlineOption')
        self.backup_checkbox.setChecked(bool(self.settings.get('backup_before_embedding', False)))
        self.backup_checkbox.setToolTip('Save music-file backups before embedding')
        self.backup_checkbox.toggled.connect(self._save_backup_preference)
        options.addWidget(self.backup_checkbox)
        layout.addLayout(options)

        actions = QHBoxLayout()
        self.find_btn = QPushButton('Find Artwork')
        self.find_btn.setObjectName('primaryButton')
        self.find_btn.setIcon(_line_icon('search', '#ffffff'))
        self.find_btn.setIconSize(QSize(16, 16))
        self.find_btn.clicked.connect(self.find_artwork_for_selected_album)
        actions.addWidget(self.find_btn)

        self.search_next_btn = QPushButton('Search Next')
        self.search_next_btn.setObjectName('quietButton')
        self.search_next_btn.setIcon(_line_icon('search'))
        self.search_next_btn.setIconSize(QSize(16, 16))
        self.search_next_btn.clicked.connect(self.search_next_batch)
        actions.addWidget(self.search_next_btn)

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

        self.reject_btn = QPushButton('Reject Option')
        self.reject_btn.setObjectName('quietButton')
        self.reject_btn.setIcon(_line_icon('stop'))
        self.reject_btn.setIconSize(QSize(16, 16))
        self.reject_btn.setToolTip('Reject the selected saved artwork option')
        self.reject_btn.clicked.connect(self.reject_selected_candidate)
        actions.addWidget(self.reject_btn)

        self.skip_btn = QPushButton('Skip Album')
        self.skip_btn.setObjectName('quietButton')
        self.skip_btn.setToolTip('Mark this album as handled without embedding artwork')
        self.skip_btn.clicked.connect(self.skip_current_album)
        actions.addWidget(self.skip_btn)

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
        if self._first_queue_load:
            self._choose_initial_queue_filter()
            self._first_queue_load = False
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

    def _queue_search_changed(self, _text_value: str = '') -> None:
        self.apply_filters()
        self._schedule_queue_filter_state_save()

    def _normalise_queue_filter(self, filter_name: Any) -> str:
        filter_name = _text(filter_name, 'All')
        if filter_name in QUEUE_FILTERS:
            return filter_name
        if filter_name in WORK_BUCKETS or filter_name == 'Needs Attention':
            return 'Needs Work'
        if filter_name in DONE_BUCKETS:
            return 'Done'
        if filter_name == 'Review':
            return 'Review'
        return 'All'

    def _choose_initial_queue_filter(self) -> None:
        if self.search_edit.text().strip():
            return
        counts = self._queue_bucket_counts()
        current = self._normalise_queue_filter(self.queue_filter)
        if counts.get(current, 0) > 0 or current == 'All':
            self.queue_filter = current
            return
        for filter_name in ('Needs Work', 'Review', 'Done', 'All'):
            if counts.get(filter_name, 0) > 0:
                self.queue_filter = filter_name
                return
        self.queue_filter = 'All'

    def _set_queue_filter(self, filter_name: str) -> None:
        filter_name = self._normalise_queue_filter(filter_name)
        if self.queue_filter == filter_name:
            self._refresh_filter_chips()
            self._schedule_queue_filter_state_save()
            return
        self.queue_filter = filter_name
        self.apply_filters()
        self._schedule_queue_filter_state_save()

    def _schedule_queue_filter_state_save(self) -> None:
        if self._restoring_queue_controls:
            return
        self.queue_filter_save_timer.start(350)

    def _save_queue_filter_state(self) -> None:
        try:
            search_text = self.search_edit.text() if hasattr(self, 'search_edit') else ''
            filter_name = self._normalise_queue_filter(self.queue_filter)
            layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
            layout = dict(layout)
            layout['queue_filter'] = filter_name
            layout['queue_search'] = search_text
            self.settings['layout'] = layout
            save_settings({'layout': {'queue_filter': filter_name, 'queue_search': search_text}})
        except Exception:
            pass

    def _refresh_filter_chips(self, counts: Optional[Dict[str, int]] = None) -> None:
        if not self.filter_chips:
            return
        counts = counts or self._queue_bucket_counts()
        selected_filter = self._normalise_queue_filter(self.queue_filter)
        for filter_name, label in FILTER_CHIPS:
            chip = self.filter_chips.get(filter_name)
            if chip is None:
                continue
            chip.blockSignals(True)
            chip.setText(f'{label} {int(counts.get(filter_name, 0)):,}')
            chip.setChecked(filter_name == selected_filter)
            chip.blockSignals(False)

    def _queue_bucket_counts(self) -> Dict[str, int]:
        counts = {name: 0 for name in QUEUE_FILTERS}
        counts['All'] = len(self.albums)
        for album in self.albums:
            bucket = self._album_bucket(album)
            if bucket in WORK_BUCKETS:
                counts['Needs Work'] = counts.get('Needs Work', 0) + 1
            elif bucket == 'Review':
                counts['Review'] = counts.get('Review', 0) + 1
            elif bucket in DONE_BUCKETS:
                counts['Done'] = counts.get('Done', 0) + 1
        return counts

    def apply_filters(self) -> None:
        current_key = _text((self.current_album or {}).get('album_key'))
        query = self.search_edit.text().strip().lower() if hasattr(self, 'search_edit') else ''
        selected_filter = self._normalise_queue_filter(self.queue_filter)
        visible = []
        counts = {name: 0 for name in QUEUE_FILTERS}
        counts['All'] = len(self.albums)
        for album in self.albums:
            bucket = self._album_bucket(album)
            if bucket in WORK_BUCKETS:
                counts['Needs Work'] = counts.get('Needs Work', 0) + 1
            elif bucket == 'Review':
                counts['Review'] = counts.get('Review', 0) + 1
            elif bucket in DONE_BUCKETS:
                counts['Done'] = counts.get('Done', 0) + 1
            if selected_filter == 'Needs Work':
                if bucket not in WORK_BUCKETS:
                    continue
            elif selected_filter == 'Review':
                if bucket != 'Review':
                    continue
            elif selected_filter == 'Done':
                if bucket not in DONE_BUCKETS:
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
        self._refresh_filter_chips(counts)
        self._sync_selection_after_filter(current_key)

    def _sync_selection_after_filter(self, preferred_key: str = '') -> None:
        if not self.visible_albums:
            self._clear_review_state()
            return
        if preferred_key and self._select_album_key(preferred_key, fallback_first=False):
            return
        self._select_visible_row(0)

    def _clear_review_state(self) -> None:
        self.current_album = None
        self.current_candidates = []
        self.candidate_widgets = []
        self.candidate_list.clear()
        self.album_title.setText('No album selected')
        self.album_subtitle.setText('')
        self.current_panel.set_placeholder('No artwork')
        self.candidate_panel.set_placeholder('No artwork')
        self.details.setPlainText('')
        self.details.setPlaceholderText('Select an album to review.')
        self.open_folder_btn.setEnabled(False)
        self.open_source_btn.setEnabled(False)
        busy = any([
            self.search_worker is not None and self.search_worker.isRunning(),
            self.approval_worker is not None and self.approval_worker.isRunning(),
            self.convert_worker is not None and self.convert_worker.isRunning(),
        ])
        if not busy:
            self.approval_status.setText('')
            self.approval_progress.setVisible(False)
        self._refresh_action_states()

    def _update_queue_empty_state(self) -> None:
        if not hasattr(self, 'queue_empty_label'):
            return
        if self.visible_albums:
            self.queue_empty_label.setVisible(False)
            self.queue_empty_label.setText('')
            return
        if not self.albums:
            message = 'No album records loaded.'
        elif self.search_edit.text().strip():
            message = 'No albums match this search.'
        else:
            message = f'No albums in {self._normalise_queue_filter(self.queue_filter)}.'
        self.queue_empty_label.setText(message)
        self.queue_empty_label.setVisible(True)

    def _sort_bucket(self, album: Dict[str, Any]) -> int:
        order = {'Review': 0, 'Missing': 1, 'Needs Search': 2, 'Not Square': 3, 'Convert': 4, 'Good': 5, 'Handled': 6}
        return order.get(self._album_bucket(album), 99)

    def _queue_filter_for_album_key(self, album_key: Any) -> str:
        key = _text(album_key)
        if not key:
            return ''
        for album in self.albums:
            if _text(album.get('album_key')) != key:
                continue
            bucket = self._album_bucket(album)
            if bucket in WORK_BUCKETS:
                return 'Needs Work'
            if bucket == 'Review':
                return 'Review'
            if bucket in DONE_BUCKETS:
                return 'Done'
            return 'All'
        return ''

    def _first_album_key_in_buckets(self, album_keys: List[Any], buckets: set[str]) -> str:
        wanted = {_text(key) for key in album_keys if _text(key)}
        if not wanted:
            return ''
        by_key = {_text(album.get('album_key')): album for album in self.albums}
        for key in album_keys:
            key = _text(key)
            album = by_key.get(key)
            if album and self._album_bucket(album) in buckets:
                return key
        return ''

    def _searchable_batch_albums(self) -> List[Dict[str, Any]]:
        if not self.visible_albums:
            return []
        current_row = self.table.currentRow()
        if current_row < 0 or current_row >= len(self.visible_albums):
            current_row = 0
        ordered = self.visible_albums[current_row:] + self.visible_albums[:current_row]
        out: List[Dict[str, Any]] = []
        seen = set()
        for album in ordered:
            key = _text(album.get('album_key'))
            if not key or key in seen or not _text(album.get('album_path')):
                continue
            bucket = self._album_bucket(album)
            if bucket not in {'Missing', 'Needs Search', 'Not Square'}:
                continue
            out.append(album)
            seen.add(key)
        return out

    def _queue_column_widths_from_settings(self) -> Dict[str, int]:
        layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
        saved = layout.get('queue_columns') if isinstance(layout.get('queue_columns'), dict) else {}
        widths = self._default_queue_column_widths()
        for name, value in saved.items():
            name = QUEUE_COLUMN_ALIASES.get(name, name)
            if name not in widths:
                continue
            try:
                widths[name] = max(QUEUE_COLUMN_MIN_WIDTHS.get(name, 52), int(value))
            except Exception:
                pass
        return widths

    def _default_queue_column_widths(self) -> Dict[str, int]:
        available = max(0, int(self.table.viewport().width()) - 2)
        if available <= 80:
            return dict(DEFAULT_QUEUE_COLUMN_WIDTHS)
        minimums = dict(QUEUE_COLUMN_MIN_WIDTHS)
        available = max(sum(minimums.values()), available)
        if available < 680:
            status_w, current_w, candidates_w = 88, 116, 52
            artist_min, album_min, artist_cap = 90, 110, 160
        else:
            status_w, current_w, candidates_w = 104, 128, 68
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

    def _main_splitter_sizes_from_settings(self) -> List[int]:
        layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
        raw = layout.get('qt_main_splitter')
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            sizes = []
            for value in raw[:2]:
                try:
                    sizes.append(max(180, int(value)))
                except Exception:
                    return []
            return sizes
        return []

    def _apply_main_splitter_sizes(self) -> None:
        if not hasattr(self, 'main_splitter'):
            return
        sizes = self._main_splitter_sizes_from_settings() or [620, 760]
        self._restoring_main_splitter = True
        try:
            self.main_splitter.setSizes(sizes)
        finally:
            self._restoring_main_splitter = False

    def _main_splitter_moved(self, _pos: int, _index: int) -> None:
        if self._restoring_main_splitter:
            return
        self.main_splitter_save_timer.start(350)

    def _save_main_splitter_sizes(self) -> None:
        try:
            sizes = [int(size) for size in self.main_splitter.sizes()]
            layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
            layout = dict(layout)
            layout['qt_main_splitter'] = sizes
            self.settings['layout'] = layout
            save_settings({'layout': {'qt_main_splitter': sizes}})
        except Exception:
            pass

    def _render_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.visible_albums))
        for row, album in enumerate(self.visible_albums):
            bucket = self._album_bucket(album)
            values = [
                _queue_status_label(bucket),
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
                    fg, _bg = BUCKET_COLORS.get(bucket, ('#46505d', '#edf0f5'))
                    item.setForeground(QBrush(QColor(fg)))
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self.count_label.setText(f'{len(self.visible_albums)} shown')
        self._update_queue_empty_state()

    def _select_table_album(self) -> None:
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        if not rows:
            return
        row = rows[0]
        if 0 <= row < len(self.visible_albums):
            self.show_album(self.visible_albums[row])

    def _select_visible_row(self, row: int) -> bool:
        if not (0 <= row < len(self.visible_albums)):
            return False
        album = self.visible_albums[row]
        key = _text(album.get('album_key'))
        self.table.selectRow(row)
        if _text((self.current_album or {}).get('album_key')) != key:
            self.show_album(album)
        return True

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
        self.candidate_widgets = []
        key = album.get('album_key')
        if key:
            try:
                self.current_candidates = db.load_candidates_for_album(key, include_rejected=False)
            except Exception:
                self.current_candidates = []
        for cand in self.current_candidates:
            widget = CandidateOptionWidget(cand)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, cand)
            item.setSizeHint(widget.sizeHint())
            self.candidate_list.addItem(item)
            self.candidate_list.setItemWidget(item, widget)
            self.candidate_widgets.append(widget)
        if self.current_candidates:
            self.candidate_list.setCurrentRow(0)
        else:
            self.candidate_panel.set_placeholder('No candidate artwork')
        self._refresh_action_states()

    def _select_candidate(self, row: int) -> None:
        self._refresh_candidate_option_selection(row)
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

    def _refresh_candidate_option_selection(self, selected_row: int) -> None:
        for index, widget in enumerate(self.candidate_widgets):
            widget.set_selected(index == selected_row)

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
            notes = album.get('notes_json') if isinstance(album.get('notes_json'), dict) else {}
            deep = notes.get('deep_file_check') if isinstance(notes.get('deep_file_check'), dict) else {}
            lines.extend([
                f"Status: {workflow_bucket_for_status(status)}",
                f"Why: {reason or _text(status, 'No status recorded')}",
                f"Current size: {_album_size(album)}",
                f"Options: {int(album.get('candidate_count') or 0)}",
                '',
                'Album folder:',
                _text(album.get('album_path'), '-'),
            ])
            if deep:
                lines.extend([
                    '',
                    'Deep check:',
                    _deep_check_summary_text(deep),
                ])
                first_issue = _text(deep.get('first_issue_file') or deep.get('first_non_square_file'))
                if first_issue:
                    lines.append(f'First issue: {first_issue}')
                problem_files = notes.get('last_problem_files') if isinstance(notes.get('last_problem_files'), dict) else {}
                if problem_files.get('problem_count'):
                    lines.append(f"Problem files: {int(problem_files.get('problem_count') or 0)}")
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
        if self.last_convert_result and album and self.last_convert_result.get('album_key') == album.get('album_key'):
            lines.extend([
                '',
                'Last Convert/Save:',
                _text(self.last_convert_result.get('conversion_reason'), '-'),
                f"Result: {_text(self.last_convert_result.get('final_bucket'), 'Done')} - {_text(self.last_convert_result.get('final_reason'), '-')}",
                f"Artwork: {_text(self.last_convert_result.get('embedded_dimensions'), '-')}",
            ])
        if self.last_search_log and album and self.last_search_album_key == _text(album.get('album_key')):
            recent = [_text(line) for line in self.last_search_log[-3:] if _text(line)]
            if recent:
                lines.extend(['', 'Recent search:'])
                lines.extend(recent)
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
        convert_busy = self.convert_worker is not None and self.convert_worker.isRunning()
        search_busy = self.search_worker is not None and self.search_worker.isRunning()
        rescan_busy = self.album_rescan_worker is not None and self.album_rescan_worker.isRunning()
        deep_check_busy = self.deep_check_worker is not None and self.deep_check_worker.isRunning()
        busy = approval_busy or convert_busy or search_busy or rescan_busy or deep_check_busy
        album = self.current_album or {}
        bucket = self._album_bucket(album) if album else ''
        can_search = bool(album and _text(album.get('album_key')) and _text(album.get('album_path')) and bucket not in {'Good', 'Handled'})
        batch_count = get_batch_search_count(self.settings)
        self.search_next_btn.setText(f'Search Next {batch_count}')
        self.search_next_btn.setToolTip(f'Search the next {batch_count} missing, needs-search, or not-square albums in the visible queue')
        can_search_next = bool(self._searchable_batch_albums())
        has_album_key = bool(album and _text(album.get('album_key')))
        can_use_active_album = bool(has_album_key and bucket not in {'Good', 'Handled'} and not busy)
        can_rework = bool(has_album_key and bucket in {'Good', 'Handled'} and not busy)
        self.find_btn.setEnabled(can_search and not busy)
        self.search_next_btn.setEnabled(can_search_next and not busy)
        self.stop_search_btn.setVisible(search_busy)
        self.stop_search_btn.setEnabled(search_busy)
        self.approve_btn.setEnabled(has_candidate and not busy)
        self.reject_btn.setEnabled(has_candidate and not busy)
        self.skip_btn.setEnabled(can_use_active_album)
        self.backup_checkbox.setEnabled(not busy)
        self.import_btn.setEnabled(can_use_active_album)
        self.more_btn.setEnabled(bool(has_album_key and not busy))
        self.google_action.setEnabled(bool(has_album_key and not busy))
        self.choose_release_action.setEnabled(bool(has_album_key and _text(album.get('album_path')) and bucket not in {'Good', 'Handled'} and not busy))
        self.refresh_album_action.setEnabled(bool(has_album_key and _text(album.get('album_path')) and not busy))
        self.problem_files_action.setEnabled(bool(has_album_key and _text(album.get('album_path')) and not busy))
        self.convert_save_action.setEnabled(bool(has_album_key and _text(album.get('album_path')) and bucket in {'Not Square', 'Convert'} and not busy))
        self.mark_good_action.setEnabled(can_use_active_album)
        self.ignore_action.setEnabled(can_use_active_album)
        self.rework_action.setEnabled(can_rework)
        self.open_folder_btn.setEnabled(bool(self.current_album and _text(self.current_album.get('album_path'))))
        self.open_source_btn.setEnabled(bool(has_candidate and _text((self._selected_candidate() or {}).get('source_url'))))

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt naming
        try:
            if event.type() == QEvent.Type.KeyPress and self.isActiveWindow():
                key = event.key()
                modifiers = event.modifiers()
                command_pressed = bool(modifiers & Qt.ControlModifier) or bool(modifiers & Qt.MetaModifier)
                if key == Qt.Key_F and command_pressed:
                    self._focus_queue_search()
                    return True
                if modifiers & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier):
                    return super().eventFilter(watched, event)
                if self._focus_is_text_entry():
                    return super().eventFilter(watched, event)
                if key == Qt.Key_F and self.find_btn.isEnabled():
                    self.find_artwork_for_selected_album()
                    return True
                if key == Qt.Key_A and self.approve_btn.isEnabled():
                    self.approve_selected_candidate()
                    return True
                if key == Qt.Key_R and self.reject_btn.isEnabled():
                    self.reject_selected_candidate()
                    return True
                if key == Qt.Key_S and self.skip_btn.isEnabled():
                    self.skip_current_album()
                    return True
                if key == Qt.Key_N:
                    self._select_next_actionable_from_current()
                    return True
        except Exception:
            pass
        return super().eventFilter(watched, event)

    def _focus_is_text_entry(self) -> bool:
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QTextEdit))

    def _focus_queue_search(self) -> None:
        self.search_edit.setFocus(Qt.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _select_next_actionable_from_current(self) -> bool:
        if not self.visible_albums:
            return False
        current_row = self.table.currentRow()
        start_row = current_row + 1 if current_row >= 0 else 0
        selected = self._select_next_actionable_row(start_row=start_row)
        if not selected:
            self.statusBar().showMessage('No actionable albums in the current queue.')
        return selected

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
        self.last_search_album_key = _text(info.get('album_key'))
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

    def search_next_batch(self) -> None:
        if self.search_worker is not None and self.search_worker.isRunning():
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        batch_count = get_batch_search_count(self.settings)
        albums = self._searchable_batch_albums()[:batch_count]
        infos = [self._album_to_search_info(album) for album in albums]
        infos = [info for info in infos if info and info.get('album_key') and info.get('album_path')]
        if not infos:
            self.approval_status.setText('No visible albums need batch search.')
            self.statusBar().showMessage('No visible albums need batch search.')
            self._refresh_action_states()
            return
        max_per_album = get_max_candidates_per_album(self.settings)
        first = infos[0]
        first_label = f"{_text(first.get('artist'), 'Unknown Artist')} - {_text(first.get('album'), 'Unknown Album')}"
        self.last_search_album_key = _text(first.get('album_key'))
        self.last_search_log = [f'Search Next: {len(infos)} album(s)', f'First: {first_label}']
        self.approval_status.setText(f'Searching next {len(infos)} album(s)...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage(f'Searching next {len(infos)} album(s)...')

        worker = SearchWorker(infos, max_per_album, self)
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
        album_count = int(result.get('album_count') or 1)
        album_keys = [_text(key) for key in (result.get('album_keys') or []) if _text(key)]
        if album_count > 1:
            if stopped:
                message = f'Search Next stopped after saving {saved} option(s).'
            elif saved:
                message = f'Search Next found {saved} new option(s) across {album_count} album(s).'
            else:
                message = 'Search Next finished. No new artwork options found.'
        elif stopped:
            message = f'Search stopped after saving {saved} option(s).'
        elif saved:
            message = f'Found {saved} new artwork option(s).'
        else:
            message = 'No new artwork options found.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        target_key = _text(result.get('album_key'))
        self.reload_queue(select_first=False)
        if album_count > 1:
            target_key = self._first_album_key_in_buckets(album_keys, {'Review'}) or target_key
        if target_key and not self._select_album_key(target_key, fallback_first=False):
            target_filter = self._queue_filter_for_album_key(target_key)
            if target_filter:
                self.queue_filter = target_filter
                self.apply_filters()
                self._schedule_queue_filter_state_save()
                self._select_album_key(target_key, fallback_first=True)
            else:
                self._select_album_key(target_key, fallback_first=True)
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        if album_count <= 1 and not saved and not stopped:
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

    def _reclassify_after_candidate_rejection(self, album_key: Any) -> Dict[str, Any]:
        key = _text(album_key)
        if not key:
            return {'status': '', 'reason': ''}
        try:
            db.set_album_status(key, 'no_candidate', reason='all saved artwork options rejected')
            return db.evaluate_and_set_album_state(
                key,
                candidate_count=0,
                preserve_user_terminal=False,
                settings=self.settings,
            )
        except Exception:
            try:
                db.set_album_status(key, 'no_candidate', reason='all saved artwork options rejected')
            except Exception:
                pass
            return {'status': 'no_candidate', 'reason': 'all saved artwork options rejected'}

    def reject_selected_candidate(self) -> None:
        candidate = self._selected_candidate()
        album = self.current_album or {}
        album_key = _text((candidate or {}).get('album_key') or album.get('album_key'))
        if not candidate or not album_key:
            return
        current_row = max(0, self.table.currentRow())
        try:
            db.mark_candidate(candidate.get('candidate_id'), rejected=True)
        except Exception as exc:
            QMessageBox.warning(self, 'Could not reject option', str(exc))
            return

        remaining = db.load_candidates_for_album(album_key, include_rejected=False)
        if remaining and self.current_album:
            self.current_candidates = remaining
            self._load_candidates(self.current_album)
            self.candidate_list.setCurrentRow(min(self.candidate_list.count() - 1, max(0, self.candidate_list.currentRow())))
            self._render_details(self._selected_candidate())
            self.approval_status.setText('Rejected artwork option.')
            self.statusBar().showMessage('Rejected artwork option.')
            self.reload_queue(select_first=False)
            self._select_album_key(album_key, fallback_first=True)
        else:
            self._reclassify_after_candidate_rejection(album_key)
            self.reload_queue(select_first=False)
            self._select_next_actionable_row(start_row=current_row)
            self.approval_status.setText('Rejected the last option. Album moved back to search.')
            self.statusBar().showMessage('Rejected the last option.')
        self._refresh_action_states()

    def skip_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        artist = _text(album.get('artist'), 'Unknown Artist')
        album_name = _text(album.get('album'), 'Unknown Album')
        answer = QMessageBox.question(
            self,
            'Skip album?',
            f'Skip {artist} - {album_name} and remove it from the active review workflow?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        current_row = max(0, self.table.currentRow())
        try:
            db.set_album_status(album_key, 'reviewed_skipped')
            db.mark_album_candidates(album_key, rejected=True)
        except Exception as exc:
            QMessageBox.warning(self, 'Could not skip album', str(exc))
            return
        self.reload_queue(select_first=False)
        self._select_next_actionable_row(start_row=current_row)
        message = f'Skipped {artist} - {album_name}.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def _album_display_name(self, album: Dict[str, Any]) -> str:
        artist = _text(album.get('artist'), 'Unknown Artist')
        album_name = _text(album.get('album'), 'Unknown Album')
        return f'{artist} - {album_name}'

    def _mark_album_good_in_db(self, album_key: str, reason: str = 'marked good by user') -> None:
        marked_at = db.now()
        db.set_album_status(album_key, 'already_good')
        db.update_album_notes(album_key, {
            'artwork_compatibility': {
                'needs_conversion': False,
                'issue': '',
                'accepted_as_is_at': marked_at,
            },
            'album_folder_cover': {
                'needs_save': False,
                'issue': '',
                'accepted_as_is_at': marked_at,
            },
            'state_evaluation': {
                'status': 'already_good',
                'reason': reason,
            },
        })

    def mark_current_album_good(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        label = self._album_display_name(album)
        bucket = self._album_bucket(album)
        warning = ''
        if bucket in {'Missing', 'Needs Search', 'Not Square', 'Convert'}:
            warning = '\n\nThis album is still in a work queue bucket, so only mark it Good if the current embedded artwork is acceptable.'
        answer = QMessageBox.question(
            self,
            'Mark current artwork good?',
            f'Mark {label} as Good and remove it from the active workflow?{warning}',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        current_row = max(0, self.table.currentRow())
        try:
            self._mark_album_good_in_db(album_key)
            db.mark_album_candidates(album_key, rejected=True, state_reason='album marked good by user')
        except Exception as exc:
            QMessageBox.warning(self, 'Could not mark album Good', str(exc))
            return
        self.reload_queue(select_first=False)
        self._select_next_actionable_row(start_row=current_row)
        message = f'Marked Good: {label}.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def ignore_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        label = self._album_display_name(album)
        answer = QMessageBox.question(
            self,
            'Ignore album?',
            f'Ignore {label} and remove it from the active workflow?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        current_row = max(0, self.table.currentRow())
        try:
            db.set_album_status(album_key, 'ignored')
            db.mark_album_candidates(album_key, rejected=True, state_reason='album ignored by user')
        except Exception as exc:
            QMessageBox.warning(self, 'Could not ignore album', str(exc))
            return
        self.reload_queue(select_first=False)
        self._select_next_actionable_row(start_row=current_row)
        message = f'Ignored: {label}.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def _reopened_status_for_album(self, album: Dict[str, Any]) -> str:
        if not album:
            return 'needs_review'
        try:
            status, _reason = evaluate_album_record(
                album,
                candidate_count=0,
                preserve_user_terminal=False,
                settings=self.settings,
            )
            if status in {'missing_artwork', 'needs_review', 'not_square_artwork', 'incompatible_artwork'}:
                return status
        except Exception:
            pass
        try:
            if int(album.get('width') or 0) <= 0 or int(album.get('height') or 0) <= 0:
                return 'missing_artwork'
        except Exception:
            return 'needs_review'
        return 'needs_review'

    def rework_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        label = self._album_display_name(album)
        answer = QMessageBox.question(
            self,
            'Rework album?',
            f'Return {label} to Needs Work and clear saved artwork options?\n\nThis does not change the music files.',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            fresh = db.get_album(album_key) or album
            removed_rows = db.delete_candidates_for_album(album_key)
            new_status = self._reopened_status_for_album(fresh)
            db.set_album_status(album_key, new_status)
            db.update_album_notes(album_key, {
                'reworked_at': db.now(),
                'reworked_reason': 'Manual Rework Album action',
            })
        except Exception as exc:
            QMessageBox.warning(self, 'Could not rework album', str(exc))
            return
        self.queue_filter = 'Needs Work'
        self.reload_queue(select_first=False)
        self._set_queue_filter('Needs Work')
        self._select_album_key(album_key, fallback_first=True)
        message = f'Reworked: {label}. Cleared {removed_rows} saved option row(s).'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def refresh_current_album_from_disk(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        album_path = _text(album.get('album_path'))
        if not album_key or not album_path:
            return
        if self.album_rescan_worker is not None and self.album_rescan_worker.isRunning():
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        label = self._album_display_name(album)
        self.approval_status.setText(f'Refreshing from disk: {label}...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage('Refreshing selected album from disk...')
        worker = AlbumRescanWorker(album, self.settings, self)
        worker.completed.connect(self._album_rescan_completed)
        worker.failed.connect(self._album_rescan_failed)
        worker.finished.connect(lambda w=worker: self._album_rescan_worker_finished(w))
        self.album_rescan_worker = worker
        self._refresh_action_states()
        worker.start()

    def _album_rescan_completed(self, result: object) -> None:
        result = dict(result or {})
        self.approval_progress.setVisible(False)
        album_key = _text(result.get('album_key'))
        message = _text(result.get('message'), 'Album refreshed from disk.')
        if result.get('reason'):
            message = f"{message} {result.get('reason')}"
        self.reload_queue(select_first=False)
        if album_key and not self._select_album_key(album_key, fallback_first=False):
            target_filter = self._queue_filter_for_album_key(album_key)
            if target_filter:
                self.queue_filter = target_filter
                self.apply_filters()
                self._schedule_queue_filter_state_save()
                self._select_album_key(album_key, fallback_first=True)
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def _album_rescan_failed(self, message: str) -> None:
        self.approval_progress.setVisible(False)
        message = _text(message, 'Album refresh failed.')
        self.approval_status.setText(message)
        self.statusBar().showMessage('Album refresh failed')
        QMessageBox.warning(self, 'Refresh from disk failed', message)
        self._refresh_action_states()

    def _album_rescan_worker_finished(self, worker: AlbumRescanWorker) -> None:
        if self.album_rescan_worker is worker:
            self.album_rescan_worker = None
        self._refresh_action_states()

    def show_problem_files_for_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        album_path = _text(album.get('album_path'))
        if not album_key or not album_path:
            return
        if self.deep_check_worker is not None and self.deep_check_worker.isRunning():
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        label = self._album_display_name(album)
        self.approval_status.setText(f'Checking every file: {label}...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage('Checking selected album for problem files...')
        worker = AlbumDeepCheckWorker(album, self.settings, self)
        worker.completed.connect(self._deep_check_completed)
        worker.failed.connect(self._deep_check_failed)
        worker.finished.connect(lambda w=worker: self._deep_check_worker_finished(w))
        self.deep_check_worker = worker
        self._refresh_action_states()
        worker.start()

    def _persist_deep_check_result(self, result: Dict[str, Any]) -> Dict[str, Any]:
        album_key = _text(result.get('album_key'))
        if not album_key:
            return {}
        deep = result.get('deep_file_check') if isinstance(result.get('deep_file_check'), dict) else {}
        rows = result.get('problem_files') if isinstance(result.get('problem_files'), list) else []
        target = result.get('target_size') or deep.get('target_size') or get_preferred_artwork_size(self.settings)
        checked_at = deep.get('checked_at') or db.now()
        summary = _deep_check_summary_text(deep)
        note_updates: Dict[str, Any] = {
            'deep_file_check': deep,
            'last_verification': {
                'ok': bool(_deep_count(deep, 'checked_files') > 0 and not deep.get('requires_action')),
                'summary': summary,
                'checked_files': deep.get('checked_files') or 0,
                'target_size': target,
                'checked_at': checked_at,
                'source': result.get('source') or deep.get('source') or '',
                'problem_count': len(rows),
                'problem_files': rows[:50],
            },
        }
        if rows:
            note_updates['last_problem_files'] = {
                'checked_at': checked_at,
                'target_size': target,
                'rows': rows[:50],
                'problem_count': len(rows),
                'source': result.get('source') or deep.get('source') or '',
            }
        try:
            db.update_album_notes(album_key, note_updates)
            width = deep.get('example_width') or deep.get('min_width')
            height = deep.get('example_height') or deep.get('min_height')
            try:
                width = int(width) if width not in (None, '') else None
                height = int(height) if height not in (None, '') else None
            except Exception:
                width = height = None
            db.update_album_path(
                album_key,
                _text(result.get('album_path')),
                example_file=deep.get('first_issue_file') or deep.get('example_file') or None,
                width=width,
                height=height,
            )
            state = db.evaluate_and_set_album_state(
                album_key,
                target_size=target,
                preserve_user_terminal=False,
                settings=self.settings,
            )
            db.update_album_notes(album_key, {'state_evaluation': state})
            return state
        except Exception:
            return {}

    def _deep_check_completed(self, result: object) -> None:
        result = dict(result or {})
        self.approval_progress.setVisible(False)
        album_key = _text(result.get('album_key'))
        state = self._persist_deep_check_result(result)
        summary = _deep_check_summary_text(result.get('deep_file_check') if isinstance(result.get('deep_file_check'), dict) else {})
        message = f'Problem file check complete: {summary}.'
        self.reload_queue(select_first=False)
        if album_key and not self._select_album_key(album_key, fallback_first=False):
            target_filter = self._queue_filter_for_album_key(album_key)
            if target_filter:
                self.queue_filter = target_filter
                self.apply_filters()
                self._schedule_queue_filter_state_save()
                self._select_album_key(album_key, fallback_first=True)
        album = db.get_album(album_key) if album_key else None
        album = album or self.current_album or {}
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()
        if state:
            result['state'] = state
        dialog = ProblemFilesDialog(album, result, self)
        dialog.exec()

    def _deep_check_failed(self, message: str) -> None:
        self.approval_progress.setVisible(False)
        message = _text(message, 'Problem file check failed.')
        self.approval_status.setText(message)
        self.statusBar().showMessage('Problem file check failed')
        QMessageBox.warning(self, 'Problem files failed', message)
        self._refresh_action_states()

    def _deep_check_worker_finished(self, worker: AlbumDeepCheckWorker) -> None:
        if self.deep_check_worker is worker:
            self.deep_check_worker = None
        self._refresh_action_states()

    def choose_release_for_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        dialog = ReleasePickerDialog(album, self.settings, self)
        dialog.release_imported.connect(self._release_picker_imported)
        dialog.exec()

    def _release_picker_imported(self, payload: object) -> None:
        payload = dict(payload or {})
        album_key = _text(payload.get('album_key'))
        added = int(payload.get('added') or 0)
        self.queue_filter = 'Review'
        self.reload_queue(select_first=False)
        self._set_queue_filter('Review')
        if album_key:
            self._select_album_key(album_key, fallback_first=True)
        message = f'Added {added} artwork option(s) from the selected release.'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self._refresh_action_states()

    def open_google_images_for_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        try:
            fresh = db.get_album(album_key) or {}
        except Exception:
            fresh = {}
        artist = _text(fresh.get('search_artist') or fresh.get('artist') or album.get('search_artist') or album.get('artist'))
        album_name = _text(fresh.get('search_album') or fresh.get('album') or album.get('search_album') or album.get('album'))
        if artist or album_name:
            webbrowser.open(google_images_url(artist, album_name))

    def convert_save_current_artwork(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        album_path = _text(album.get('album_path'))
        if not album_key or not album_path:
            return
        if self.convert_worker is not None and self.convert_worker.isRunning():
            return
        try:
            self.settings = load_settings()
        except Exception:
            pass
        self.last_convert_result = None
        label = self._album_display_name(album)
        backup = bool(self.backup_checkbox.isChecked())
        self._save_backup_preference()
        self.approval_status.setText(f'Preparing Convert/Save: {label}...')
        self.approval_progress.setVisible(True)
        self.approval_progress.setRange(0, 0)
        self.statusBar().showMessage('Converting/saving current artwork...')
        self.pending_approval_row = max(0, self.table.currentRow())

        worker = ConvertSaveWorker(album, backup, self.settings, self)
        worker.progress.connect(self._convert_save_progress)
        worker.completed.connect(self._convert_save_completed)
        worker.failed.connect(self._convert_save_failed)
        worker.finished.connect(lambda w=worker: self._convert_save_worker_finished(w))
        self.convert_worker = worker
        self._refresh_action_states()
        worker.start()

    def _convert_save_progress(self, done: int, total: int, path: str) -> None:
        if total > 0:
            self.approval_progress.setRange(0, total)
            self.approval_progress.setValue(max(0, min(done, total)))
            self.approval_status.setText(f'Convert/Save {done}/{total}: {_path_tail(path, parts=2)}')
            self.statusBar().showMessage(f'Convert/Save {done}/{total}')
        else:
            self.approval_progress.setRange(0, 0)
            self.approval_status.setText(_text(path, 'Preparing Convert/Save...'))

    def _convert_save_completed(self, result: object) -> None:
        result = dict(result or {})
        self.last_convert_result = result
        self.approval_progress.setVisible(False)
        bucket = _text(result.get('final_bucket'), 'Done')
        reason = _text(result.get('final_reason'), 'Convert/Save complete.')
        dims = _text(result.get('embedded_dimensions'), '-')
        warnings = len(result.get('failed_items') or [])
        cover = _text(result.get('album_artwork_copy'))
        message = f'Convert/Save complete: {bucket} ({dims}).'
        if cover:
            message += ' cover.jpg saved.'
        if warnings:
            message += f' {warnings} warning(s).'
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        self.reload_queue(select_first=False)
        if bucket == 'Good':
            self._select_next_actionable_row(start_row=self.pending_approval_row)
        else:
            self._select_album_key(result.get('album_key'), fallback_first=True)
        if warnings or bucket in {'Convert', 'Not Square', 'Needs Search', 'Missing'}:
            QMessageBox.information(self, 'Convert/Save finished', f'{message}\n\n{reason}')

    def _convert_save_failed(self, message: str) -> None:
        self.approval_progress.setVisible(False)
        message = _text(message, 'Convert/Save failed.')
        self.approval_status.setText(message)
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, 'Cannot Convert/Save artwork', message)
        self._refresh_action_states()

    def _convert_save_worker_finished(self, worker: ConvertSaveWorker) -> None:
        if self.convert_worker is worker:
            self.convert_worker = None
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
                    return self._select_visible_row(row)
        if fallback_first and self.visible_albums:
            return self._select_visible_row(0)
        return False

    def _select_next_actionable_row(self, *, start_row: int = 0) -> bool:
        if not self.visible_albums:
            return False
        start = max(0, min(int(start_row or 0), len(self.visible_albums) - 1))
        for offset in range(len(self.visible_albums)):
            row = (start + offset) % len(self.visible_albums)
            if self._album_bucket(self.visible_albums[row]) in ACTIONABLE_BUCKETS:
                return self._select_visible_row(row)
        return self._select_visible_row(0)

    def open_album_folder(self) -> None:
        album = self.current_album or {}
        path = _text(album.get('album_path'))
        if path:
            open_path(path)

    def import_image_for_current_album(self) -> None:
        album = self.current_album or {}
        album_key = _text(album.get('album_key'))
        if not album_key:
            return
        start = _text(album.get('album_path')) or str(Path.home())
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            'Import Artwork Image',
            start,
            'Images (*.jpg *.jpeg *.png *.webp)',
        )
        path = _text(path)
        if not path:
            return
        try:
            manual_import(
                path,
                _text(album.get('artist'), 'Unknown Artist'),
                _text(album.get('album'), 'Unknown Album'),
                album_key,
                _text(album.get('album_path')),
            )
            db.set_album_status(album_key, 'candidate_found')
            db.update_album_notes(album_key, {
                'state_evaluation': {
                    'status': 'candidate_found',
                    'reason': 'manual artwork option imported',
                }
            })
        except Exception as exc:
            QMessageBox.warning(self, 'Import failed', str(exc))
            return
        self.queue_filter = 'Review'
        self.reload_queue(select_first=False)
        self._set_queue_filter('Review')
        self._select_album_key(album_key, fallback_first=True)
        self.approval_status.setText('Imported artwork option.')
        self.statusBar().showMessage('Imported artwork option.')
        self._refresh_action_states()

    def open_source_page(self) -> None:
        row = self.candidate_list.currentRow()
        if 0 <= row < len(self.current_candidates):
            url = _text(self.current_candidates[row].get('source_url'))
            if url:
                webbrowser.open(url)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self.scan_dialog is not None:
            worker = getattr(self.scan_dialog, 'worker', None)
            if worker is not None and worker.isRunning():
                QMessageBox.information(self, 'Scan still running', 'Stop the scan before closing Artwork Manager.')
                event.ignore()
                return
        if self.album_rescan_worker is not None and self.album_rescan_worker.isRunning():
            QMessageBox.information(self, 'Refresh still running', 'Wait for the selected album refresh to finish before closing Artwork Manager.')
            event.ignore()
            return
        if self.deep_check_worker is not None and self.deep_check_worker.isRunning():
            QMessageBox.information(self, 'Problem file check still running', 'Wait for the selected album check to finish before closing Artwork Manager.')
            event.ignore()
            return
        if self.convert_worker is not None and self.convert_worker.isRunning():
            QMessageBox.information(self, 'Convert/Save still running', 'Wait for the selected album Convert/Save to finish before closing Artwork Manager.')
            event.ignore()
            return
        if self.queue_filter_save_timer.isActive():
            self.queue_filter_save_timer.stop()
            self._save_queue_filter_state()
        if self.queue_column_save_timer.isActive():
            self.queue_column_save_timer.stop()
            self._save_queue_column_widths()
        if self.main_splitter_save_timer.isActive():
            self.main_splitter_save_timer.stop()
            self._save_main_splitter_sizes()
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
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
        if self.convert_worker is not None:
            try:
                self.convert_worker.wait(500)
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
            QFrame#sidebar, QFrame#imagePanel {
                background: #ffffff;
                border: 1px solid #e0e3ea;
                border-radius: 6px;
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
            QLabel#emptyHint {
                color: #6b7280;
                padding: 1px 2px 2px 2px;
            }
            QLabel#artworkPreview {
                background: #ffffff;
                border: 1px solid #dfe3eb;
                border-radius: 4px;
                color: #777b84;
            }
            QLineEdit, QTextEdit, QListWidget, QTableWidget, QComboBox, QSpinBox {
                background: #ffffff;
                border: 1px solid #d9d9df;
                border-radius: 6px;
                padding: 5px;
                selection-background-color: #dbeafe;
                selection-color: #111827;
            }
            QLineEdit:hover, QTextEdit:hover, QListWidget:hover, QTableWidget:hover, QComboBox:hover, QSpinBox:hover {
                border-color: #c5c9d3;
            }
            QLineEdit:focus, QTextEdit:focus, QListWidget:focus, QTableWidget:focus, QComboBox:focus, QSpinBox:focus {
                border-color: #8aa4d6;
            }
            QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {
                border: 0;
                width: 22px;
                background: transparent;
            }
            QTabWidget::pane {
                border: 1px solid #e0e3ea;
                border-radius: 6px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: #fbfbfd;
                border: 1px solid #d7dce6;
                border-bottom-color: #e0e3ea;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 7px 12px;
                margin-right: 4px;
                color: #485465;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-color: #d7dce6;
                border-bottom-color: #ffffff;
                color: #17345c;
            }
            QMenu {
                background: #ffffff;
                border: 1px solid #d7dce6;
                border-radius: 6px;
                padding: 5px;
            }
            QMenu::item {
                padding: 7px 28px 7px 24px;
                border-radius: 4px;
                color: #20242d;
            }
            QMenu::item:selected {
                background: #e7f0ff;
                color: #17345c;
            }
            QMenu::item:disabled {
                color: #a0a4ad;
            }
            QMenu::separator {
                height: 1px;
                background: #edf0f5;
                margin: 5px 4px;
            }
            QGroupBox {
                background: #ffffff;
                border: 1px solid #e0e3ea;
                border-radius: 6px;
                margin-top: 14px;
                padding: 12px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                left: 8px;
                color: #1d1d1f;
            }
            QGroupBox QLabel, QGroupBox QCheckBox {
                background: transparent;
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
            QListWidget#candidateList {
                padding: 4px;
            }
            QListWidget#candidateList::item {
                padding: 0;
                margin: 2px;
                border: 0;
            }
            QFrame#candidateOption {
                background: #ffffff;
                border: 1px solid #e1e5ec;
                border-radius: 6px;
            }
            QFrame#candidateOption[selected="true"] {
                background: #e7f0ff;
                border-color: #8aa4d6;
            }
            QLabel#candidateThumb {
                background: #f8fafc;
                border: 1px solid #e1e5ec;
                border-radius: 4px;
                color: #6b7280;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#candidateSource {
                color: #111827;
                font-weight: 700;
            }
            QLabel#candidateMeta {
                color: #485465;
                font-size: 12px;
            }
            QLabel#candidateRelease {
                color: #6b7280;
                font-size: 12px;
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
            QPushButton#quietButton::menu-indicator {
                width: 0;
                image: none;
            }
            QPushButton#filterChip {
                background: #fbfbfd;
                border: 1px solid #d7dce6;
                border-radius: 6px;
                color: #485465;
                font-size: 12px;
                font-weight: 600;
                padding: 5px 7px;
            }
            QPushButton#filterChip:hover {
                background: #f0f4ff;
                border-color: #b7c5df;
            }
            QPushButton#filterChip:checked {
                background: #e7f0ff;
                border-color: #8aa4d6;
                color: #17345c;
            }
            QPushButton:disabled {
                color: #9a9aa2;
                background: #f4f4f6;
                border-color: #d9d9df;
            }
            QPushButton#primaryButton:disabled,
            QPushButton#approveButton:disabled,
            QPushButton#dangerButton:disabled {
                color: #9a9aa2;
                background: #f4f4f6;
                border-color: #d9d9df;
            }
            QCheckBox {
                background: transparent;
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
    app.setApplicationName('Artwork Manager')
    configure_app_font(app)
    icon_path = APP_DIR / 'assets' / 'app_icon.png'
    if icon_path.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))
    win = QtArtworkWindow()
    win.show()
    return app.exec()
