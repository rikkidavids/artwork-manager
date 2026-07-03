#!/usr/bin/env python3
"""Artwork Manager NAS worker.

Runs inside Docker/Container Manager on a Synology/NAS.  The Mac app sends
artwork write and deep-check jobs here so files are modified locally on the NAS
instead of through SMB/VPN.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
import unicodedata
import uuid
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageOps
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

WORKER_BUILD = '4.53'
APP_BUILD = '4.53'
WORKER_API = 2
MINIMUM_MAC_APP_WORKER_API = 2
VERSION = f'Artwork Manager NAS Worker {WORKER_BUILD} / app build {APP_BUILD}'
MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
UPDATE_HINT = (
    'If this is not the build you expected, Synology is probably still running '
    'an older cached Docker image/container. Rebuild the project/image; do not only restart it. Build 4.53 also fixes Unicode-normalized NAS paths for accented folder names.'
)


def env_roots() -> List[Path]:
    raw = os.environ.get('AMW_MUSIC_ROOTS') or os.environ.get('AMW_MUSIC_ROOT') or '/music'
    roots = []
    for part in raw.split(':'):
        part = part.strip()
        if part:
            roots.append(Path(part).resolve())
    return roots or [Path('/music').resolve()]


MUSIC_ROOTS = env_roots()
BACKUP_ROOT = Path(os.environ.get('AMW_BACKUP_DIR') or '/backups').resolve()
API_TOKEN = os.environ.get('AMW_TOKEN') or ''
HOST = os.environ.get('AMW_HOST') or '0.0.0.0'
PORT = int(os.environ.get('AMW_PORT') or '8765')
SERVER_STARTED_AT = time.monotonic()
RECENT_JOBS = deque(maxlen=int(os.environ.get('AMW_RECENT_JOBS') or '50'))
ACTIVE_JOBS = {}
ACTIVE_ALBUMS = set()
JOB_LOCK = threading.RLock()


class WorkerBusyError(RuntimeError):
    pass


def now() -> str:
    return datetime.now().isoformat(timespec='seconds')


def duration_seconds(started: float) -> float:
    try:
        return round(max(0.0, time.monotonic() - float(started)), 3)
    except Exception:
        return 0.0


def path_status(path: Path, check_write: bool = False) -> Dict[str, Any]:
    """Return cheap filesystem diagnostics for mounted NAS paths."""
    out = {
        'path': str(path),
        'exists': False,
        'is_dir': False,
        'readable': False,
        'writable': False,
    }
    try:
        out['exists'] = path.exists()
        out['is_dir'] = path.is_dir()
        out['readable'] = os.access(path, os.R_OK)
        out['writable'] = os.access(path, os.W_OK)
        if check_write and out['is_dir'] and out['writable']:
            probe = path / '.amw_write_test'
            try:
                probe.write_text(now(), encoding='utf-8')
                probe.unlink(missing_ok=True)
                out['write_test_ok'] = True
            except Exception as exc:
                out['write_test_ok'] = False
                out['write_test_error'] = str(exc)
    except Exception as exc:
        out['error'] = str(exc)
    return out


def job_album_label(payload: Dict[str, Any]) -> str:
    artist = str(payload.get('artist') or '').strip()
    album = str(payload.get('album') or '').strip()
    if artist and album:
        return f'{artist} — {album}'
    if album:
        return album
    path = str(payload.get('album_folder') or '').rstrip('/')
    return Path(path).name if path else 'album'


def begin_job(kind: str, payload: Dict[str, Any]) -> Tuple[str, float, str]:
    album_folder = str(safe_path(payload.get('album_folder') or ''))
    job_id = uuid.uuid4().hex[:12]
    started_mono = time.monotonic()
    record = {
        'job_id': job_id,
        'kind': kind,
        'album_folder': album_folder,
        'label': job_album_label(payload),
        'started_at': now(),
        'duration_seconds': 0.0,
        'ok': None,
    }
    with JOB_LOCK:
        if album_folder in ACTIVE_ALBUMS:
            raise WorkerBusyError(f'Album is already being processed by the NAS worker: {album_folder}')
        ACTIVE_ALBUMS.add(album_folder)
        ACTIVE_JOBS[job_id] = record
    return job_id, started_mono, album_folder


def finish_job(job_id: str, started_mono: float, ok: bool, result: Dict[str, Any] | None = None, error: str = '') -> Dict[str, Any]:
    result = result or {}
    summary = {
        'job_id': job_id,
        'worker_build': WORKER_BUILD,
        'worker_api': WORKER_API,
        'api': WORKER_API,
        'duration_seconds': duration_seconds(started_mono),
        'finished_at': now(),
    }
    with JOB_LOCK:
        record = ACTIVE_JOBS.pop(job_id, None) or {'job_id': job_id}
        album_folder = record.get('album_folder') or ''
        if album_folder:
            ACTIVE_ALBUMS.discard(album_folder)
        record.update(summary)
        record['ok'] = bool(ok)
        if error:
            record['error'] = str(error)
        if isinstance(result, dict):
            if 'updated' in result:
                record['updated'] = result.get('updated')
            if 'total' in result:
                record['total'] = result.get('total')
            if 'failed' in result:
                try:
                    record['failed_count'] = len(result.get('failed') or [])
                except Exception:
                    record['failed_count'] = 0
            deep = result.get('deep_file_check') if isinstance(result.get('deep_file_check'), dict) else None
            if deep:
                record['checked_files'] = deep.get('checked_files')
                record['requires_action'] = bool(deep.get('requires_action'))
        RECENT_JOBS.appendleft(record)
    return summary


def status_payload(public: bool = False) -> Dict[str, Any]:
    with JOB_LOCK:
        active = [dict(v) for v in ACTIVE_JOBS.values()]
        recent = [dict(v) for v in list(RECENT_JOBS)]
    payload = {
        'ok': True,
        'service': 'Artwork Manager NAS Worker',
        'version': VERSION,
        'worker_build': WORKER_BUILD,
        'app_build': APP_BUILD,
        'worker_api': WORKER_API,
        'api': WORKER_API,
        'minimum_mac_app_worker_api': MINIMUM_MAC_APP_WORKER_API,
        'token_required': bool(API_TOKEN),
        'music_roots': [str(x) for x in MUSIC_ROOTS],
        'backup_root': str(BACKUP_ROOT),
        'time': now(),
        'uptime_seconds': duration_seconds(SERVER_STARTED_AT),
        'busy': bool(active),
        'active_jobs': active,
        'recent_jobs': recent,
        'recent_job_count': len(recent),
        'endpoints': ['GET /', 'GET /version', 'GET /health', 'GET /status', 'POST /embed', 'POST /deep-check', 'POST /path-check'],
        'update_hint': UPDATE_HINT,
        'build_marker': f'amw-worker-{WORKER_BUILD}-api-{WORKER_API}',
    }
    if not public:
        payload['filesystem'] = {
            'music_roots': [path_status(x) for x in MUSIC_ROOTS],
            'backup_root': path_status(BACKUP_ROOT),
        }
    return payload


def _unicode_forms(value: str) -> Tuple[str, str]:
    raw = str(value or '')
    try:
        return unicodedata.normalize('NFC', raw), unicodedata.normalize('NFD', raw)
    except Exception:
        return raw, raw


def _unicode_component_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    left_nfc, left_nfd = _unicode_forms(left)
    right_nfc, right_nfd = _unicode_forms(right)
    return left_nfc == right_nfc or left_nfd == right_nfd


def _resolve_unicode_equivalent_path(path: Path, root: Path) -> Tuple[Path, bool]:
    """Resolve a path under root, matching components by Unicode equivalence.

    macOS/SMB may send decomposed Unicode names (for example Zoe + combining
    diaeresis) while Synology/Linux often stores the visually identical folder
    using composed Unicode (Zoë).  pathlib's exact lookup treats those as
    different names.  This walks each component and substitutes the actual
    on-disk name when NFC/NFD-normalized forms match.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path, False
    candidate = root
    changed = False
    for part in rel.parts:
        exact = candidate / part
        if exact.exists():
            candidate = exact
            continue
        if not candidate.is_dir():
            candidate = exact
            continue
        try:
            children = list(candidate.iterdir())
        except Exception:
            candidate = exact
            continue
        matches = [child for child in children if _unicode_component_equal(child.name, part)]
        if not matches:
            candidate = exact
            continue
        # Prefer a directory match when walking album folders, then deterministic name order.
        matches.sort(key=lambda child: (not child.is_dir(), child.name.lower()))
        candidate = matches[0]
        changed = True
    return candidate, changed


def safe_path(value: str) -> Path:
    if not value:
        raise ValueError('Missing path')
    try:
        p = Path(value).resolve(strict=False)
    except TypeError:
        p = Path(value).resolve()
    for root in MUSIC_ROOTS:
        root_resolved = root.resolve(strict=False)
        try:
            p.relative_to(root_resolved)
        except ValueError:
            continue
        resolved, _changed = _resolve_unicode_equivalent_path(p, root_resolved)
        try:
            final = resolved.resolve(strict=False)
        except TypeError:
            final = resolved.resolve()
        try:
            final.relative_to(root_resolved)
        except ValueError:
            raise ValueError(f'Path resolves outside allowed music roots: {final}')
        return final
    raise ValueError(f'Path is outside allowed music roots: {p}')


def iter_music_files(album_folder: Path):
    for root, _, files in os.walk(album_folder):
        for fn in sorted(files):
            if fn.lower().endswith(MUSIC_EXTENSIONS):
                yield Path(root) / fn


def image_dimensions_from_bytes(data: bytes):
    try:
        with Image.open(BytesIO(data)) as img:
            return img.size
    except Exception:
        return None


def image_format_info(data: bytes) -> Dict[str, Any]:
    try:
        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or '').upper()
            is_jpeg = fmt == 'JPEG'
            progressive = bool(img.info.get('progressive') or img.info.get('progression'))
            baseline = bool(is_jpeg and not progressive)
            return {
                'format': fmt,
                'is_baseline_jpeg': baseline,
                'is_progressive_jpeg': bool(is_jpeg and progressive),
                'compatible': baseline,
                'issue': '' if baseline else ('progressive JPEG' if is_jpeg and progressive else (fmt or 'not JPEG')),
            }
    except Exception:
        return {'format': '', 'is_baseline_jpeg': False, 'is_progressive_jpeg': False, 'compatible': False, 'issue': 'unreadable artwork'}


def fit_to_square_canvas(img: Image.Image, target: int) -> Image.Image:
    target = max(1, int(target or max(img.size)))
    work = ImageOps.exif_transpose(img).convert('RGB')
    w, h = work.size
    scale = min(target / max(1, w), target / max(1, h))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    if (new_w, new_h) != (w, h):
        resample = getattr(Image.Resampling, 'LANCZOS', getattr(Image, 'LANCZOS', 3))
        work = work.resize((new_w, new_h), resample)
    canvas = Image.new('RGB', (target, target), (255, 255, 255))
    canvas.paste(work, ((target - new_w) // 2, (target - new_h) // 2))
    return canvas


def prepare_jpeg_bytes_from_bytes(source: bytes, max_size: int | None = None, make_square: bool = False) -> Tuple[bytes, str]:
    with Image.open(BytesIO(source)) as img:
        img = ImageOps.exif_transpose(img).convert('RGB')
        if make_square:
            target = int(max_size or max(img.size))
            img = fit_to_square_canvas(img, target)
        elif max_size:
            max_size = int(max_size)
            w, h = img.size
            if max(w, h) != max_size:
                scale = max_size / max(1, max(w, h))
                new = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
                resample = getattr(Image.Resampling, 'LANCZOS', getattr(Image, 'LANCZOS', 3))
                img = img.resize(new, resample)
        out = BytesIO()
        # optimize=False avoids Pillow writing progressive JPEGs unless asked.
        img.save(out, format='JPEG', quality=95, progressive=False, optimize=False)
        return out.getvalue(), 'image/jpeg'


def artwork_item(data: bytes):
    dims = image_dimensions_from_bytes(data)
    if not dims:
        return None
    compat = image_format_info(data)
    return {
        'width': dims[0],
        'height': dims[1],
        'bytes': data,
        'format': compat.get('format') or '',
        'is_baseline_jpeg': bool(compat.get('is_baseline_jpeg')),
        'is_progressive_jpeg': bool(compat.get('is_progressive_jpeg')),
        'compatible': bool(compat.get('compatible')),
        'compatibility_issue': compat.get('issue') or '',
    }


def embedded_artwork(path: Path):
    ext = path.suffix.lower()
    out = []
    try:
        if ext == '.mp3':
            audio = ID3(str(path))
            for tag in audio.values():
                if getattr(tag, 'FrameID', None) == 'APIC':
                    item = artwork_item(tag.data)
                    if item:
                        out.append(item)
        elif ext == '.flac':
            audio = FLAC(str(path))
            for pic in audio.pictures:
                item = artwork_item(pic.data)
                if item:
                    out.append(item)
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(str(path))
            covr = audio.tags.get('covr', []) if audio.tags else []
            for cover in covr:
                item = artwork_item(bytes(cover))
                if item:
                    out.append(item)
    except Exception:
        pass
    return out


def embed_file(path: Path, image_bytes: bytes, mime='image/jpeg') -> bool:
    ext = path.suffix.lower()
    if ext == '.mp3':
        try:
            audio = ID3(str(path))
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall('APIC')
        audio.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=image_bytes))
        audio.save(str(path), v2_version=3)
        return True
    if ext == '.flac':
        audio = FLAC(str(path))
        audio.clear_pictures()
        pic = Picture()
        pic.type = 3
        pic.mime = mime
        pic.desc = 'Cover'
        pic.data = image_bytes
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                pic.width, pic.height = img.size
                pic.depth = len(img.getbands()) * 8
        except Exception:
            pass
        audio.add_picture(pic)
        audio.save()
        return True
    if ext in ('.m4a', '.mp4'):
        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio.tags['covr'] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
        return True
    return False


def backup_file(path: Path, album_key: str) -> str:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe = ''.join(ch if ch.isalnum() or ch in ' ._-' else '_' for ch in (album_key or 'album'))[:120]
    dest_dir = BACKUP_ROOT / safe / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    shutil.copy2(path, dest)
    return str(dest)


def save_cover(album_folder: Path, image_bytes: bytes, max_size: int | None = None, make_square: bool = False) -> str:
    data, _ = prepare_jpeg_bytes_from_bytes(image_bytes, max_size=max_size, make_square=make_square)
    out = album_folder / 'cover.jpg'
    out.write_bytes(data)
    for stale_name in ('cover.jpeg', 'cover.png', 'cover.webp'):
        stale = album_folder / stale_name
        if stale.exists() and stale.is_file():
            try:
                stale.unlink()
            except Exception:
                pass
    return str(out)


def embed_album_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    album_folder = safe_path(payload.get('album_folder') or '')
    if not album_folder.is_dir():
        raise ValueError('Album folder does not exist inside the container')
    source = base64.b64decode(payload.get('image_b64') or '')
    max_size = payload.get('max_artwork_size') or None
    make_square = bool(payload.get('make_square'))
    embed = bool(payload.get('embed', True))
    backup = bool(payload.get('backup'))
    save_folder = bool(payload.get('save_folder_cover'))
    album_key = payload.get('album_key') or str(album_folder)
    prepared, mime = prepare_jpeg_bytes_from_bytes(source, max_size=max_size, make_square=make_square)
    dims = image_dimensions_from_bytes(prepared) or (None, None)
    files = list(iter_music_files(album_folder))
    backups = []
    failed = []
    updated = 0
    if embed:
        if not files:
            return {
                'album_folder': str(album_folder), 'updated': 0, 'total': 0, 'failed': [], 'backups': [],
                'image_width': dims[0], 'image_height': dims[1], 'no_audio_files': True,
                'message': 'No supported audio files found in album folder',
            }
        for fp in files:
            try:
                if backup:
                    backups.append({'file': str(fp), 'backup': backup_file(fp, album_key)})
                if embed_file(fp, prepared, mime):
                    updated += 1
            except Exception as exc:
                failed.append({'file': str(fp), 'error': str(exc)})
    album_artwork_copy = ''
    if save_folder:
        try:
            album_artwork_copy = save_cover(album_folder, source, max_size=max_size, make_square=make_square)
        except Exception as exc:
            failed.append({'file': str(album_folder), 'error': f'Folder cover copy failed: {exc}'})
    return {
        'album_folder': str(album_folder),
        'updated': updated,
        'total': len(files) if embed else 0,
        'failed': failed,
        'backups': backups,
        'image_width': dims[0],
        'image_height': dims[1],
        'album_artwork_copy': album_artwork_copy,
    }


def artwork_meets_target_size(w: int, h: int, target: int) -> bool:
    return int(w or 0) >= int(target or 0) and int(h or 0) >= int(target or 0)


def deep_check(album_folder: Path, target_size: int, problem_files: bool = False) -> Dict[str, Any]:
    files = list(iter_music_files(album_folder))
    result = {
        'enabled': True,
        'target_size': target_size,
        'checked_files': 0,
        'missing_count': 0,
        'below_target_count': 0,
        'incompatible_count': 0,
        'unreadable_count': 0,
        'non_square_count': 0,
        'ok_count': 0,
        'first_issue_file': '',
        'first_issue': '',
        'first_non_square_file': '',
        'first_non_square_dimensions': '',
        'min_width': None,
        'min_height': None,
        'example_file': '',
        'example_width': None,
        'example_height': None,
        'checked_at': now(),
        'source': 'nas-worker',
    }
    problems = []

    def note_issue(fn: str, issue: str):
        if not result['first_issue_file']:
            result['first_issue_file'] = fn
            result['first_issue'] = issue

    for fp in files:
        fn = fp.name
        issues = []
        result['checked_files'] += 1
        arts = embedded_artwork(fp)
        if not arts:
            result['missing_count'] += 1
            note_issue(fn, 'missing embedded artwork')
            issues.append('missing embedded artwork')
            if problem_files:
                problems.append({'file': fn, 'dimensions': '', 'issues': issues})
            continue
        best = None
        file_incompatible = False
        file_incompat_issue = ''
        for art in arts:
            try:
                area = int(art.get('width') or 0) * int(art.get('height') or 0)
            except Exception:
                area = 0
            if best is None or area > int(best.get('width') or 0) * int(best.get('height') or 0):
                best = art
            if not art.get('compatible'):
                file_incompatible = True
                file_incompat_issue = file_incompat_issue or art.get('compatibility_issue') or 'not baseline JPEG'
        if file_incompatible:
            result['incompatible_count'] += 1
            note_issue(fn, file_incompat_issue)
            issues.append(file_incompat_issue)
        if not best:
            result['unreadable_count'] += 1
            note_issue(fn, 'unreadable embedded artwork')
            issues.append('unreadable embedded artwork')
            if problem_files:
                problems.append({'file': fn, 'dimensions': '', 'issues': issues})
            continue
        w, h = int(best.get('width') or 0), int(best.get('height') or 0)
        dims = f'{w}×{h}' if w and h else ''
        if w <= 0 or h <= 0:
            result['unreadable_count'] += 1
            note_issue(fn, 'unreadable embedded artwork')
            issues.append('unreadable embedded artwork')
            if problem_files:
                problems.append({'file': fn, 'dimensions': dims, 'issues': issues})
            continue
        if result['example_width'] is None:
            result['example_file'] = fn
            result['example_width'] = w
            result['example_height'] = h
        if result['min_width'] is None or min(w, h) < min(int(result['min_width'] or 0), int(result['min_height'] or 0)):
            result['min_width'] = w
            result['min_height'] = h
            result['example_file'] = fn
            result['example_width'] = w
            result['example_height'] = h
        file_not_square = (w != h)
        file_below = not artwork_meets_target_size(w, h, target_size)
        if file_not_square:
            result['non_square_count'] += 1
            if not result['first_non_square_file']:
                result['first_non_square_file'] = fn
                result['first_non_square_dimensions'] = dims
            issues.append('not square')
        if file_below:
            result['below_target_count'] += 1
            note_issue(fn, f'below target ({dims})')
            issues.append(f'below target {target_size}px')
        if not file_incompatible and not file_not_square and not file_below:
            result['ok_count'] += 1
        elif problem_files:
            problems.append({'file': fn, 'dimensions': dims, 'issues': issues})
    result['requires_action'] = bool(result['missing_count'] or result['below_target_count'] or result['non_square_count'] or result['incompatible_count'] or result['unreadable_count'])
    return {'deep_file_check': result, 'problem_files': problems}


def path_check(album_folder: Path, requested_album_folder: str = '') -> Dict[str, Any]:
    """Cheap mapping/read/write self-test for one album folder."""
    result = {
        'requested_album_folder': str(requested_album_folder or ''),
        'album_folder': str(album_folder),
        'unicode_normalized_match': bool(requested_album_folder and str(requested_album_folder) != str(album_folder)),
        'exists': False,
        'is_dir': False,
        'readable': False,
        'writable': False,
        'write_test_ok': False,
        'file_count': 0,
        'supported_music_file_count': 0,
        'sample_files': [],
        'music_roots': [str(x) for x in MUSIC_ROOTS],
        'checked_at': now(),
    }
    try:
        result['exists'] = album_folder.exists()
        result['is_dir'] = album_folder.is_dir()
        result['readable'] = os.access(album_folder, os.R_OK)
        result['writable'] = os.access(album_folder, os.W_OK)
        names = []
        if result['is_dir'] and result['readable']:
            names = sorted(os.listdir(album_folder), key=lambda x: x.lower())
            result['file_count'] = len(names)
            samples = []
            music_count = 0
            for name in names:
                fp = album_folder / name
                if fp.is_file() and name.lower().endswith(MUSIC_EXTENSIONS):
                    music_count += 1
                    if len(samples) < 8:
                        samples.append(name)
            result['supported_music_file_count'] = music_count
            result['sample_files'] = samples
        if result['is_dir'] and result['writable']:
            probe = album_folder / '.amw_path_check_write_test'
            try:
                probe.write_text(now(), encoding='utf-8')
                probe.unlink(missing_ok=True)
                result['write_test_ok'] = True
            except Exception as exc:
                result['write_test_error'] = str(exc)
    except Exception as exc:
        result['error'] = str(exc)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = f'ArtworkManagerNASWorker/{WORKER_BUILD}'

    def _send(self, status: int, obj: Dict[str, Any]):
        data = json.dumps(obj, indent=2, sort_keys=True).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Artwork-Worker-Build', WORKER_BUILD)
        self.send_header('X-Artwork-Worker-API', str(WORKER_API))
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        if not API_TOKEN:
            return True
        return self.headers.get('X-Artwork-Worker-Token') == API_TOKEN

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b'{}'
        return json.loads(raw.decode('utf-8') or '{}')

    def do_GET(self):
        route = self.path.split('?', 1)[0].rstrip('/') or '/'
        if route == '/favicon.ico':
            self.send_response(204)
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if route in ('/', '/version'):
            payload = status_payload(public=True)
            payload['message'] = 'Worker is running. Use /status with the API token, or Test NAS Worker in the Mac app, for authenticated checks.'
            self._send(200, payload)
            return
        if route in ('/health', '/status'):
            if not self._auth_ok():
                self._send(401, {'ok': False, 'error': 'unauthorized', 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API, 'message': 'Worker is running, but this endpoint requires the API token. Use Test NAS Worker in the Mac app.'})
                return
            self._send(200, status_payload(public=False))
            return
        self._send(404, {'ok': False, 'error': 'not found', 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API, 'message': 'Worker is running, but this endpoint does not exist.', 'endpoints': status_payload(public=True)['endpoints']})

    def do_POST(self):
        if not self._auth_ok():
            self._send(401, {'ok': False, 'error': 'unauthorized', 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API})
            return
        job_id = ''
        started_mono = 0.0
        try:
            payload = self._read_json()
            route = self.path.rstrip('/')
            if route == '/path-check':
                album_folder = safe_path(payload.get('album_folder') or '')
                result = path_check(album_folder, requested_album_folder=str(payload.get('album_folder') or ''))
                self._send(200, {'ok': True, 'result': result, 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API})
                return
            if route == '/embed':
                job_id, started_mono, _album = begin_job('embed', payload)
                result = embed_album_job(payload)
                worker = finish_job(job_id, started_mono, True, result=result)
                result['remote_worker'] = True
                result['remote_worker_job_id'] = job_id
                result['remote_worker_build'] = WORKER_BUILD
                result['remote_worker_api'] = WORKER_API
                result['remote_worker_duration_seconds'] = worker.get('duration_seconds')
                self._send(200, {'ok': True, 'result': result, 'worker': worker})
                return
            if route == '/deep-check':
                job_id, started_mono, album_folder = begin_job('deep-check', payload)
                target = int(payload.get('target_size') or 1000)
                result = deep_check(Path(album_folder), target, problem_files=bool(payload.get('problem_files')))
                worker = finish_job(job_id, started_mono, True, result=result)
                self._send(200, {'ok': True, **result, 'worker': worker})
                return
            self._send(404, {'ok': False, 'error': 'not found', 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API})
        except WorkerBusyError as exc:
            self._send(409, {'ok': False, 'error': str(exc), 'busy': True, 'status': status_payload(public=False)})
        except Exception as exc:
            if job_id:
                finish_job(job_id, started_mono, False, error=str(exc))
            self._send(500, {'ok': False, 'error': str(exc), 'worker_build': WORKER_BUILD, 'api': WORKER_API, 'worker_api': WORKER_API})

    def log_message(self, fmt, *args):
        if os.environ.get('AMW_VERBOSE'):
            super().log_message(fmt, *args)


if __name__ == '__main__':
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    print(f'{VERSION} listening on {HOST}:{PORT}', flush=True)
    print('Music roots: ' + ', '.join(str(x) for x in MUSIC_ROOTS), flush=True)
    print(UPDATE_HINT, flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
