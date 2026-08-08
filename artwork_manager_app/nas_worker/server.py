#!/usr/bin/env python3
"""Artwork Manager NAS worker.

Runs inside Docker/Container Manager on a Synology/NAS.  The Mac app sends
artwork write and deep-check jobs here so files are modified locally on the NAS
instead of through SMB/VPN.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image, ImageOps
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover

WORKER_BUILD = '5.04'
APP_BUILD = '5.04'
WORKER_API = 3
MINIMUM_MAC_APP_WORKER_API = 3
VERSION = f'Artwork Manager NAS Worker {WORKER_BUILD} / app build {APP_BUILD}'
MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
YEAR_RE = re.compile(r'(19|20)\d{2}')
UPDATE_HINT = (
    'If this is not the build you expected, Synology is probably still running '
    'an older cached Docker image/container. Rebuild the project/image; do not only restart it. Build 5.04 adds NAS-local library scanning for VPN use.'
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
        'endpoints': ['GET /', 'GET /version', 'GET /health', 'GET /status', 'POST /scan-library', 'POST /embed', 'POST /deep-check', 'POST /path-check'],
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


def alpha_key(name: Any):
    parts = re.split(r'(\d+)', str(name or '').lower())
    return [int(part) if part.isdigit() else part for part in parts]


def sort_names(names):
    try:
        return sorted(list(names), key=alpha_key)
    except Exception:
        return list(names)


def path_resume_key(path: Any) -> str:
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path or ''))))
    except Exception:
        return str(path or '')


def clean_album_name(name: str) -> str:
    name = str(name or '')
    name = re.sub(r'^\(\d{4}\)\s*-\s*', '', name)
    name = re.sub(r'^\d{4}\s*-\s*', '', name)
    return name.strip()


def normalize_for_match(name: str) -> str:
    name = clean_album_name(name).lower()
    name = re.sub(r'[^a-z0-9]+', ' ', name)
    return ' '.join(name.split())


def as_text_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def tag_values(tags: Any, *keys: str) -> List[str]:
    vals = []
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value is None:
            continue
        vals.extend(as_text_list(value))
    return vals


def id3_text(audio: Any, *frame_ids: str) -> List[str]:
    out = []
    for frame_id in frame_ids:
        frame = audio.get(frame_id)
        if frame is None:
            continue
        text = getattr(frame, 'text', None)
        out.extend(as_text_list(text if text is not None else str(frame)))
    return out


def id3_txxx(audio: Any, *descriptions: str) -> List[str]:
    wanted = {d.lower() for d in descriptions}
    out = []
    for key, frame in audio.items():
        if not str(key).startswith('TXXX'):
            continue
        desc = str(getattr(frame, 'desc', '') or '').lower()
        if desc in wanted:
            out.extend(as_text_list(getattr(frame, 'text', None)))
    return out


def mp4_values(tags: Any, *keys: str) -> List[str]:
    out = []
    for key in keys:
        value = tags.get(key)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, bytes):
                    try:
                        item = item.decode('utf-8', errors='ignore')
                    except Exception:
                        item = ''
                out.extend(as_text_list(item))
        else:
            out.extend(as_text_list(value))
    return out


def read_track_metadata(path: Path) -> Dict[str, List[str]]:
    ext = path.suffix.lower()
    data = {
        'artist': [], 'albumartist': [], 'album': [], 'year': [],
        'mb_release_id': [], 'mb_releasegroup_id': [],
    }
    try:
        if ext == '.mp3':
            audio = ID3(str(path))
            data['artist'].extend(id3_text(audio, 'TPE1'))
            data['albumartist'].extend(id3_text(audio, 'TPE2'))
            data['album'].extend(id3_text(audio, 'TALB'))
            data['year'].extend(id3_text(audio, 'TDRC', 'TDOR', 'TYER', 'TDAT'))
            data['mb_release_id'].extend(id3_txxx(audio, 'MusicBrainz Album Id', 'MusicBrainz Release Id', 'MusicBrainz AlbumID'))
            data['mb_releasegroup_id'].extend(id3_txxx(audio, 'MusicBrainz Release Group Id', 'MusicBrainz Release GroupID'))
        elif ext == '.flac':
            audio = FLAC(str(path))
            tags = audio.tags or {}
            data['artist'].extend(tag_values(tags, 'artist'))
            data['albumartist'].extend(tag_values(tags, 'albumartist', 'album artist'))
            data['album'].extend(tag_values(tags, 'album'))
            data['year'].extend(tag_values(tags, 'date', 'year', 'originaldate', 'originalyear'))
            data['mb_release_id'].extend(tag_values(tags, 'musicbrainz_albumid', 'musicbrainz release id'))
            data['mb_releasegroup_id'].extend(tag_values(tags, 'musicbrainz_releasegroupid', 'musicbrainz release group id'))
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(str(path))
            tags = audio.tags or {}
            data['artist'].extend(mp4_values(tags, '©ART'))
            data['albumartist'].extend(mp4_values(tags, 'aART'))
            data['album'].extend(mp4_values(tags, '©alb'))
            data['year'].extend(mp4_values(tags, '©day'))
            data['mb_release_id'].extend(mp4_values(tags, '----:com.apple.iTunes:MusicBrainz Album Id', '----:com.apple.iTunes:MusicBrainz Release Id'))
            data['mb_releasegroup_id'].extend(mp4_values(tags, '----:com.apple.iTunes:MusicBrainz Release Group Id'))
    except Exception:
        pass
    return data


def common_value(values: List[str]) -> str:
    values = [str(v).strip() for v in values if str(v).strip()]
    if not values:
        return ''
    return Counter(values).most_common(1)[0][0]


def parse_folder_identity(folder: Path, library_root: Path) -> Dict[str, str]:
    rel = os.path.relpath(str(folder), str(library_root))
    parts = rel.split(os.sep)
    artist = parts[0] if len(parts) >= 1 and parts[0] != '.' else ''
    album_part = parts[1] if len(parts) >= 2 else (parts[0] if parts and parts[0] != '.' else '')
    year = ''
    album = album_part
    match = re.match(r'^\((\d{4})\)\s*-\s*(.+)$', album_part)
    if match:
        year, album = match.group(1), match.group(2)
    else:
        match = re.match(r'^(\d{4})\s*-\s*(.+)$', album_part)
        if match:
            year, album = match.group(1), match.group(2)
        else:
            match = re.match(r'^(.+?)\s*\((\d{4})\)$', album_part)
            if match:
                album, year = match.group(1), match.group(2)
    return {
        'folder_artist': artist.strip(),
        'folder_album': clean_album_name(album).strip(),
        'folder_year': year,
    }


def inspect_album_identity(folder: Path, library_root: Path, music_names: List[str]) -> Dict[str, Any]:
    folder_meta = parse_folder_identity(folder, library_root)
    tag_artists: List[str] = []
    tag_albumartists: List[str] = []
    tag_albums: List[str] = []
    tag_years: List[str] = []
    mb_release_ids: List[str] = []
    mb_releasegroup_ids: List[str] = []
    for name in music_names[:10]:
        meta = read_track_metadata(folder / name)
        tag_artists.extend(meta.get('artist', []))
        tag_albumartists.extend(meta.get('albumartist', []))
        tag_albums.extend(meta.get('album', []))
        tag_years.extend(meta.get('year', []))
        mb_release_ids.extend(meta.get('mb_release_id', []))
        mb_releasegroup_ids.extend(meta.get('mb_releasegroup_id', []))

    artist_from_tags = common_value(tag_albumartists) or common_value(tag_artists)
    album_from_tags = common_value(tag_albums)
    year_from_tags = common_value([YEAR_RE.search(v).group(0) for v in tag_years if YEAR_RE.search(v)])
    search_artist = artist_from_tags or folder_meta['folder_artist']
    search_album = album_from_tags or folder_meta['folder_album']
    year = year_from_tags or folder_meta['folder_year'] or ''
    artist_agree = not (artist_from_tags and folder_meta['folder_artist']) or normalize_for_match(artist_from_tags) == normalize_for_match(folder_meta['folder_artist'])
    album_agree = not (album_from_tags and folder_meta['folder_album']) or normalize_for_match(album_from_tags) == normalize_for_match(folder_meta['folder_album'])
    if artist_from_tags and album_from_tags and artist_agree and album_agree:
        confidence = 'High'
        source_summary = 'tags + folder agreement'
    elif (artist_from_tags or album_from_tags) and (artist_agree or album_agree):
        confidence = 'Medium'
        source_summary = 'tags supported by folder structure'
    elif search_artist or search_album:
        confidence = 'Low'
        source_summary = 'folder structure and partial tags'
    else:
        confidence = 'Low'
        source_summary = 'weak metadata'
    if (artist_from_tags and folder_meta['folder_artist'] and not artist_agree) or (album_from_tags and folder_meta['folder_album'] and not album_agree):
        source_summary = 'tags/folder mismatch'
        confidence = 'Low'
    return {
        'artist': search_artist or folder_meta['folder_artist'],
        'album': search_album or folder_meta['folder_album'],
        'search_artist': search_artist or folder_meta['folder_artist'],
        'search_album': search_album or folder_meta['folder_album'],
        'year': year,
        'mb_release_id': common_value(mb_release_ids),
        'mb_releasegroup_id': common_value(mb_releasegroup_ids),
        'identity_confidence': confidence,
        'track_count': len(music_names),
        'notes': {
            'source_summary': source_summary,
            'folder_artist': folder_meta['folder_artist'],
            'folder_album': folder_meta['folder_album'],
            'folder_year': folder_meta['folder_year'],
            'tag_artist': artist_from_tags,
            'tag_album': album_from_tags,
            'tag_year': year_from_tags,
        },
    }


def get_album_path(folder: Path, library_root: Path) -> Path:
    rel = os.path.relpath(str(folder), str(library_root))
    parts = rel.split(os.sep)
    return library_root / parts[0] / parts[1] if len(parts) >= 2 else folder


def folder_music_fingerprint(folder: Path, music_names: List[str]) -> Dict[str, Any]:
    parts = []
    total_size = 0
    max_mtime_ns = 0
    for name in sort_names(music_names or []):
        try:
            st = (folder / name).stat()
            size = int(getattr(st, 'st_size', 0) or 0)
            mtime_ns = int(getattr(st, 'st_mtime_ns', int(getattr(st, 'st_mtime', 0) * 1000000000)) or 0)
        except Exception:
            size = -1
            mtime_ns = -1
        total_size += max(0, size)
        max_mtime_ns = max(max_mtime_ns, mtime_ns)
        parts.append(f'{name}\0{size}\0{mtime_ns}')
    digest = hashlib.sha1('\0'.join(parts).encode('utf-8', errors='ignore')).hexdigest()
    return {
        'version': 1,
        'file_count': len(music_names or []),
        'total_size': total_size,
        'max_mtime_ns': max_mtime_ns,
        'digest': digest,
    }


def fingerprint_matches(saved: Any, current: Any) -> bool:
    if not isinstance(saved, dict) or not isinstance(current, dict):
        return False
    return (
        int(saved.get('version') or 0) == int(current.get('version') or 0) and
        int(saved.get('file_count') or -1) == int(current.get('file_count') or -2) and
        int(saved.get('total_size') or -1) == int(current.get('total_size') or -2) and
        int(saved.get('max_mtime_ns') or -1) == int(current.get('max_mtime_ns') or -2) and
        str(saved.get('digest') or '') == str(current.get('digest') or '')
    )


def target_tolerance(mode: Any) -> float:
    return 1.0 if str(mode or '').strip().lower() == 'strict' else 0.98


def scan_artwork_meets_target_size(width: Any, height: Any, target_size: Any, tolerance: float = 1.0) -> bool:
    try:
        w, h, target = int(width or 0), int(height or 0), int(target_size or 0)
    except Exception:
        return False
    if target <= 0:
        return True
    if w <= 0 or h <= 0:
        return False
    if w >= target and h >= target:
        return True
    if max(w, h) >= target and min(w, h) >= int(round(target * float(tolerance or 1.0))):
        return True
    return False


def image_dimensions_path(path: Path):
    try:
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


def album_folder_cover_status(album_path: Path, target_size: int, folder_files: List[str] | None, tolerance: float) -> Dict[str, Any]:
    candidate_names = ('cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp')
    if folder_files is not None:
        by_lower = {str(name).lower(): str(name) for name in folder_files}
        existing_name = next((by_lower.get(name) for name in candidate_names if by_lower.get(name)), '')
        existing = album_path / existing_name if existing_name else None
    else:
        if not album_path.is_dir():
            return {'ok': False, 'issue': 'album folder unavailable', 'path': ''}
        existing = next((album_path / name for name in candidate_names if (album_path / name).is_file()), None)
    if not existing:
        return {'ok': False, 'issue': 'folder cover missing', 'path': ''}
    dims = image_dimensions_path(existing)
    try:
        compat = image_format_info(existing.read_bytes())
    except Exception as exc:
        compat = {'compatible': False, 'issue': f'cannot read file: {exc}'}
    if existing.suffix.lower() != '.jpg':
        return {'ok': False, 'issue': f'folder cover is {existing.suffix.lstrip(".").upper() or "non-JPG"}', 'path': str(existing), 'dimensions': dims}
    if not compat.get('compatible'):
        return {'ok': False, 'issue': f'folder cover {compat.get("issue") or "not baseline JPEG"}', 'path': str(existing), 'dimensions': dims}
    if not dims:
        return {'ok': False, 'issue': 'folder cover unreadable', 'path': str(existing)}
    if not scan_artwork_meets_target_size(dims[0], dims[1], target_size, tolerance):
        return {'ok': False, 'issue': f'folder cover below target ({dims[0]}×{dims[1]})', 'path': str(existing), 'dimensions': dims}
    return {'ok': True, 'issue': '', 'path': str(existing), 'dimensions': dims}


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


def artwork_meets_target_size(w: int, h: int, target: int, tolerance: float = 1.0) -> bool:
    return scan_artwork_meets_target_size(w, h, target, tolerance)


def deep_check(album_folder: Path, target_size: int, problem_files: bool = False, tolerance: float = 1.0) -> Dict[str, Any]:
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
        file_below = not artwork_meets_target_size(w, h, target_size, tolerance)
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


def deep_check_summary(check: Dict[str, Any]) -> str:
    if not check:
        return ''
    bits = []
    checked = int(check.get('checked_files') or 0)
    if check.get('missing_count'):
        bits.append(f"{check.get('missing_count')}/{checked} missing")
    if check.get('below_target_count'):
        bits.append(f"{check.get('below_target_count')}/{checked} below target")
    if check.get('non_square_count'):
        bits.append(f"{check.get('non_square_count')}/{checked} not square")
    if check.get('incompatible_count'):
        bits.append(f"{check.get('incompatible_count')}/{checked} not baseline")
    if check.get('unreadable_count'):
        bits.append(f"{check.get('unreadable_count')}/{checked} unreadable")
    return '; '.join(bits) or f'{checked} file(s) OK'


def analyze_scan_album(
    folder: Path,
    library_root: Path,
    files: List[str],
    music: List[str],
    fingerprint: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    include_missing = bool(settings.get('include_missing', True))
    deep_scan_enabled = bool(settings.get('deep_scan_all_files'))
    scan_min = int(settings.get('scan_min_artwork_size') or 1000)
    preferred = int(settings.get('preferred_artwork_size') or scan_min or 1000)
    min_artwork_size = preferred if deep_scan_enabled else scan_min
    tolerance = target_tolerance(settings.get('target_size_match_mode'))
    identity = inspect_album_identity(folder, library_root, music)
    notes = dict(identity.get('notes') or {})
    notes['scan_fingerprint'] = fingerprint
    identity['notes'] = notes
    artist = str(identity.get('artist') or '')
    album = str(identity.get('album') or '')
    album_path = get_album_path(folder, library_root)

    album_low = False
    album_incompatible = False
    album_not_square = False
    folder_cover_issue = ''
    folder_cover_status: Dict[str, Any] | None = None
    compatibility_issue = ''
    example = ''
    dims: Tuple[Any, Any] = (None, None)

    if deep_scan_enabled:
        deep = deep_check(folder, preferred, problem_files=False, tolerance=tolerance).get('deep_file_check') or {}
        notes = dict(identity.get('notes') or {})
        notes['deep_file_check'] = deep
        identity['notes'] = notes
        example = deep.get('first_issue_file') or deep.get('example_file') or (music[0] if music else '')
        dims = (
            deep.get('example_width') or deep.get('min_width'),
            deep.get('example_height') or deep.get('min_height'),
        )
        if deep.get('missing_count') or deep.get('unreadable_count'):
            album_low = True
            if deep.get('missing_count') and not dims[0]:
                dims = (None, None)
        if deep.get('below_target_count'):
            album_low = True
        if deep.get('non_square_count'):
            album_not_square = True
        if deep.get('incompatible_count'):
            album_incompatible = True
            compatibility_issue = deep_check_summary(deep) or 'embedded artwork needs conversion'
    else:
        for name in music:
            arts = embedded_artwork(folder / name)
            if not arts:
                example = example or name
                if include_missing:
                    album_low = True
                    dims = (None, None)
                continue
            for art in arts:
                w, h = art['width'], art['height']
                if dims == (None, None):
                    dims = (w, h)
                    example = example or name
                if not scan_artwork_meets_target_size(w, h, min_artwork_size, tolerance):
                    album_low = True
                    example = name
                    dims = (w, h)
                    break
                if w != h:
                    album_not_square = True
                    example = name
                    dims = (w, h)
                    break
                if not art.get('compatible'):
                    album_incompatible = True
                    example = name
                    dims = (w, h)
                    compatibility_issue = art.get('compatibility_issue') or 'not baseline JPEG'
                    break
            if album_low or album_not_square or album_incompatible:
                break

    if settings.get('save_approved_artwork_to_album_folder'):
        cover_files = files if path_resume_key(folder) == path_resume_key(album_path) else None
        folder_cover_status = album_folder_cover_status(album_path, preferred, cover_files, tolerance)
        if not folder_cover_status.get('ok'):
            album_incompatible = True
            folder_cover_issue = folder_cover_status.get('issue') or 'folder cover missing'
            compatibility_issue = compatibility_issue or folder_cover_issue

    notes = dict(identity.get('notes') or {})
    if album_incompatible:
        notes['artwork_compatibility'] = {
            'issue': compatibility_issue or 'one or more files need baseline JPEG conversion',
            'needs_conversion': True,
            'format': compatibility_issue or 'scan check',
        }
    if folder_cover_issue:
        notes['album_folder_cover'] = {
            'needs_save': True,
            'issue': folder_cover_issue,
            'path': (folder_cover_status or {}).get('path') or '',
            'checked_at': now(),
        }
    identity['notes'] = notes
    requires_action = bool(album_low or album_not_square or album_incompatible)
    return {
        'artist': artist,
        'album': album,
        'album_path': str(album_path),
        'search_artist': identity.get('search_artist') or artist,
        'search_album': identity.get('search_album') or album,
        'year': identity.get('year') or '',
        'mb_release_id': identity.get('mb_release_id') or '',
        'mb_releasegroup_id': identity.get('mb_releasegroup_id') or '',
        'identity_confidence': identity.get('identity_confidence') or '',
        'track_count': identity.get('track_count') or len(music),
        'width': dims[0],
        'height': dims[1],
        'example_file': example or '',
        'requires_action': requires_action,
        'scan_fingerprint': fingerprint,
        'identity': identity,
    }


def scan_library_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    library_root = safe_path(payload.get('library_root') or '')
    if not library_root.is_dir():
        raise ValueError('Library root does not exist inside the container')
    resume = bool(payload.get('resume', True))
    deep_scan = bool(payload.get('deep_scan_all_files'))
    settings = {
        'include_missing': bool(payload.get('include_missing', True)),
        'deep_scan_all_files': deep_scan,
        'scan_min_artwork_size': int(payload.get('scan_min_artwork_size') or 1000),
        'preferred_artwork_size': int(payload.get('preferred_artwork_size') or payload.get('scan_min_artwork_size') or 1000),
        'target_size_match_mode': payload.get('target_size_match_mode') or 'Relaxed',
        'save_approved_artwork_to_album_folder': bool(payload.get('save_approved_artwork_to_album_folder')),
    }
    known_by_path: Dict[str, Dict[str, Any]] = {}
    if resume and not deep_scan:
        for item in payload.get('known_albums') or []:
            if not isinstance(item, dict):
                continue
            album_path = str(item.get('album_path') or '')
            if not album_path:
                continue
            known_by_path[album_path] = item
            known_by_path[path_resume_key(album_path)] = item

    max_workers = max(1, min(int(payload.get('max_workers') or 4), 32))
    if deep_scan:
        max_workers = min(max_workers, 8)
    max_pending = max(1, max_workers * 3)
    max_albums = int(payload.get('max_albums') or 0)
    submitted_paths = set()
    albums: List[Dict[str, Any]] = []
    fingerprint_updates: List[Dict[str, Any]] = []
    processed = 0
    skipped = 0
    backfilled = 0

    pending = set()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for root_text, dirs, files in os.walk(library_root):
            dirs[:] = sort_names(dirs)
            files = sort_names(files)
            music = [name for name in files if str(name).lower().endswith(MUSIC_EXTENSIONS)]
            if not music:
                continue
            folder = Path(root_text)
            album_path = get_album_path(folder, library_root)
            album_path_key = path_resume_key(album_path)
            if album_path_key in submitted_paths:
                continue
            submitted_paths.add(album_path_key)
            if max_albums and processed >= max_albums:
                break
            processed += 1

            existing = known_by_path.get(str(album_path)) or known_by_path.get(album_path_key)
            fingerprint = None
            if existing and resume and not deep_scan:
                saved_fingerprint = existing.get('scan_fingerprint')
                fingerprint = folder_music_fingerprint(folder, music)
                if saved_fingerprint:
                    if fingerprint_matches(saved_fingerprint, fingerprint):
                        skipped += 1
                        continue
                else:
                    album_key = str(existing.get('album_key') or '')
                    if album_key:
                        fingerprint_updates.append({
                            'fingerprint_update': True,
                            'album_key': album_key,
                            'album_path': str(album_path),
                            'scan_fingerprint': fingerprint,
                        })
                    backfilled += 1
                    continue
            if fingerprint is None:
                fingerprint = folder_music_fingerprint(folder, music)

            pending.add(executor.submit(analyze_scan_album, folder, library_root, files, music, fingerprint, settings))
            if len(pending) >= max_pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done:
                    albums.append(fut.result())

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                albums.append(fut.result())

    albums.sort(key=lambda item: (str(item.get('artist') or '').lower(), str(item.get('album') or '').lower(), str(item.get('album_path') or '').lower()))
    return {
        'library_root': str(library_root),
        'processed_albums': processed,
        'changed_albums': len(albums),
        'skipped_unchanged': skipped,
        'fingerprints_backfilled': backfilled,
        'albums': albums,
        'fingerprint_updates': fingerprint_updates,
        'worker_build': WORKER_BUILD,
        'api': WORKER_API,
        'worker_api': WORKER_API,
        'source': 'nas-worker-scan',
    }


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
            if route == '/scan-library':
                scan_payload = dict(payload)
                scan_payload['album_folder'] = scan_payload.get('library_root') or ''
                job_id, started_mono, _album = begin_job('scan-library', scan_payload)
                result = scan_library_job(payload)
                worker = finish_job(job_id, started_mono, True, result=result)
                result['remote_worker'] = True
                result['remote_worker_job_id'] = job_id
                result['remote_worker_build'] = WORKER_BUILD
                result['remote_worker_api'] = WORKER_API
                result['remote_worker_duration_seconds'] = worker.get('duration_seconds')
                self._send(200, {'ok': True, 'result': result, 'worker': worker})
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
