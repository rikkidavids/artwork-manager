"""Client helpers for the optional Synology/NAS Docker worker.

The Mac app remains the review UI.  When enabled, expensive write/check jobs
can be handed to a small HTTP worker running on the NAS so music files are
rewritten on the NAS-local filesystem instead of through SMB/VPN.
"""
from __future__ import annotations

import base64
import json
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import (
    load_settings,
    get_nas_worker_enabled,
    get_nas_worker_timeout,
    get_scan_min_artwork_size,
    get_preferred_artwork_size,
    get_deep_scan_all_files,
    get_scan_worker_threads,
)

EXPECTED_NAS_WORKER_BUILD = '5.04'
MIN_NAS_WORKER_API = 3
_COMPAT_CACHE_SECONDS = 300
_COMPAT_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}


class RemoteWorkerError(RuntimeError):
    pass


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _version_parts(value: str):
    parts = []
    for piece in str(value or '').replace('-', '.').split('.'):
        try:
            parts.append(int(''.join(ch for ch in piece if ch.isdigit()) or '0'))
        except Exception:
            parts.append(0)
    return tuple(parts or [0])


def worker_compatibility(info: Dict[str, Any]) -> Dict[str, Any]:
    build = str(info.get('worker_build') or '')
    api = _as_int(info.get('api'), 0)
    ok = bool(api >= MIN_NAS_WORKER_API)
    if build and _version_parts(build) < _version_parts(EXPECTED_NAS_WORKER_BUILD):
        ok = False
    if ok:
        msg = 'compatible'
    elif api < MIN_NAS_WORKER_API:
        msg = f'worker API {api or "unknown"} is older than required API {MIN_NAS_WORKER_API}'
    else:
        msg = f'worker build {build or "unknown"} is older than expected build {EXPECTED_NAS_WORKER_BUILD}'
    return {
        'ok': ok,
        'message': msg,
        'worker_build': build or 'unknown',
        'worker_api': api or 'unknown',
        'expected_build': EXPECTED_NAS_WORKER_BUILD,
        'min_api': MIN_NAS_WORKER_API,
    }


def worker_update_hint() -> str:
    return (
        'Update the Synology worker by copying the bundled nas_worker folder to the NAS, then rebuild/recreate '
        'the Docker/Container Manager project. Restarting the existing project can keep using the old cached image. '
        'Open the worker URL in a browser afterwards; it should show '
        f'worker_build {EXPECTED_NAS_WORKER_BUILD} and api {MIN_NAS_WORKER_API}.'
    )


def _normalise_url(url: str) -> str:
    url = (url or '').strip().rstrip('/')
    if not url:
        return ''
    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'http://' + url
    return url


def _strip_trailing_sep(value: str) -> str:
    value = str(value or '').strip()
    if value in ('/', ''):
        return value
    return value.rstrip('/\\')


def _unicode_nfc(value: str) -> str:
    """Use a stable Unicode form for paths sent to the Linux/NAS worker.

    macOS/SMB can report names such as Zoë in decomposed form (e + combining
    diaeresis) while Linux/Synology commonly stores the same visible name in
    composed form.  Normalising here avoids many false "folder does not exist"
    errors; the worker also performs a component-by-component fallback.
    """
    try:
        return unicodedata.normalize('NFC', str(value or ''))
    except Exception:
        return str(value or '')


def map_album_path_to_worker(album_path: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """Return the NAS-worker path for a Mac-visible album path.

    Example:
      /Volumes/Music/Artist/Album + /Volumes/Music -> /music/Artist/Album
    """
    settings = settings or load_settings()
    path = str(album_path or '').strip()
    if not path:
        return ''
    local_prefix = _strip_trailing_sep(settings.get('nas_worker_local_prefix') or '')
    remote_prefix = _strip_trailing_sep(settings.get('nas_worker_remote_prefix') or '/music') or '/music'

    # If no mapping is configured, assume the stored path is already meaningful
    # to the worker.  This keeps the feature usable for people who scan via a
    # mounted path that is identical inside the container.
    if not local_prefix:
        return _unicode_nfc(path)

    # Try direct string prefix first.  Avoid Path.resolve() because network
    # volumes can be slow/unavailable over VPN.
    norm_path = path.rstrip('/\\')
    if norm_path == local_prefix:
        return _unicode_nfc(remote_prefix)
    for sep in ('/', '\\'):
        prefix = local_prefix + sep
        if norm_path.startswith(prefix):
            suffix = norm_path[len(prefix):].replace('\\', '/')
            return _unicode_nfc(remote_prefix + ('/' + suffix if suffix else ''))
    return ''


def map_worker_path_to_local(worker_path: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """Return the Mac-visible path for a worker/container path."""
    settings = settings or load_settings()
    path = str(worker_path or '').strip()
    if not path:
        return ''
    local_prefix = _strip_trailing_sep(settings.get('nas_worker_local_prefix') or '')
    remote_prefix = _strip_trailing_sep(settings.get('nas_worker_remote_prefix') or '/music') or '/music'
    if not local_prefix:
        return path
    norm_path = path.rstrip('/\\')
    if norm_path == remote_prefix:
        return _unicode_nfc(local_prefix)
    prefix = remote_prefix + '/'
    if norm_path.startswith(prefix):
        suffix = norm_path[len(prefix):].replace('\\', '/')
        return _unicode_nfc(local_prefix + ('/' + suffix if suffix else ''))
    return path


def worker_enabled_for_path(album_path: str, settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or load_settings()
    return bool(get_nas_worker_enabled(settings) and _normalise_url(settings.get('nas_worker_url') or '') and map_album_path_to_worker(album_path, settings))


def _headers(settings: Dict[str, Any]) -> Dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    token = str(settings.get('nas_worker_token') or '').strip()
    if token:
        headers['X-Artwork-Worker-Token'] = token
    return headers


def _decode_json(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw or '{}')
    except Exception as exc:
        raise RemoteWorkerError(f'NAS worker returned invalid JSON: {raw[:200]}') from exc
    if not isinstance(parsed, dict):
        raise RemoteWorkerError(f'NAS worker returned unexpected JSON: {str(parsed)[:200]}')
    return parsed


def _http_error_message(endpoint: str, exc: HTTPError, body: str = '') -> str:
    parsed: Dict[str, Any] = {}
    try:
        parsed = json.loads(body or '{}')
    except Exception:
        parsed = {}
    msg = str(parsed.get('message') or parsed.get('error') or body or exc)
    if exc.code == 401:
        return 'NAS worker rejected the request. Check that the API token in the Mac app matches AMW_TOKEN in docker-compose.yml.'
    if exc.code == 404 and endpoint in ('/health', '/status', '/version', '/'):
        return (
            f'NAS worker responded, but {endpoint} is missing or returned 404. '
            'This usually means Synology is still running an older cached worker image/container. '
            + worker_update_hint()
        )
    if parsed.get('busy'):
        return f'NAS worker is busy: {msg}'
    return f'NAS worker error: {msg}'


def _get(endpoint: str, settings: Optional[Dict[str, Any]] = None, timeout: int = 8) -> Dict[str, Any]:
    settings = settings or load_settings()
    base = _normalise_url(settings.get('nas_worker_url') or '')
    if not base:
        raise RemoteWorkerError('NAS worker URL is not configured.')
    req = Request(base + endpoint, headers=_headers(settings), method='GET')
    started = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8', 'replace')
        except Exception:
            pass
        raise RemoteWorkerError(_http_error_message(endpoint, exc, body)) from exc
    except URLError as exc:
        raise RemoteWorkerError(f'Could not reach NAS worker: {exc.reason}') from exc
    except Exception as exc:
        raise RemoteWorkerError(f'Could not reach NAS worker: {exc}') from exc
    parsed = _decode_json(raw)
    parsed['_request_duration_seconds'] = round(max(0.0, time.monotonic() - started), 3)
    if parsed.get('ok') is False and endpoint in ('/health', '/status', '/version', '/'):
        err = str(parsed.get('error') or parsed.get('message') or 'worker reported failure')
        if err == 'not found':
            raise RemoteWorkerError(
                f'NAS worker responded, but {endpoint} returned not found. '
                'This usually means Synology is still running an older cached worker image/container. '
                + worker_update_hint()
            )
        raise RemoteWorkerError(f'NAS worker error: {err}')
    return parsed


def _post(endpoint: str, payload: Dict[str, Any], settings: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    base = _normalise_url(settings.get('nas_worker_url') or '')
    if not base:
        raise RemoteWorkerError('NAS worker URL is not configured.')
    url = base + endpoint
    data = json.dumps(payload).encode('utf-8')
    req = Request(url, data=data, headers=_headers(settings), method='POST')
    started = time.monotonic()
    try:
        with urlopen(req, timeout=timeout or get_nas_worker_timeout(settings)) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except HTTPError as exc:
        body = ''
        try:
            body = exc.read().decode('utf-8', 'replace')
        except Exception:
            pass
        raise RemoteWorkerError(_http_error_message(endpoint, exc, body)) from exc
    except URLError as exc:
        raise RemoteWorkerError(f'Could not reach NAS worker: {exc.reason}') from exc
    except Exception as exc:
        raise RemoteWorkerError(f'NAS worker request failed: {exc}') from exc
    parsed = _decode_json(raw)
    elapsed = round(max(0.0, time.monotonic() - started), 3)
    parsed['_request_duration_seconds'] = elapsed
    if isinstance(parsed.get('worker'), dict):
        parsed['_worker_duration_seconds'] = parsed['worker'].get('duration_seconds') or elapsed
        parsed['_worker_job_id'] = parsed['worker'].get('job_id') or ''
        parsed['_worker_build'] = parsed['worker'].get('worker_build') or ''
    if not parsed.get('ok', False):
        raise RemoteWorkerError(parsed.get('error') or 'NAS worker reported failure.')
    return parsed


def check_worker(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    info = _get('/health', settings=settings, timeout=8)
    info['compatibility'] = worker_compatibility(info)
    return info


def worker_version(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read public worker version details without requiring the API token."""
    settings = settings or load_settings()
    info = _get('/version', settings=settings, timeout=8)
    info['compatibility'] = worker_compatibility(info)
    return info


def worker_status(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings = settings or load_settings()
    info = _get('/status', settings=settings, timeout=8)
    info['compatibility'] = worker_compatibility(info)
    return info


def _cache_key(settings: Dict[str, Any]) -> Tuple[str, str]:
    return (_normalise_url(settings.get('nas_worker_url') or ''), str(settings.get('nas_worker_token') or '').strip())


def ensure_worker_compatible(settings: Optional[Dict[str, Any]] = None, *, force: bool = False) -> Dict[str, Any]:
    """Verify the configured worker is new enough before write/check jobs.

    The result is cached briefly so batch operations do not add a health-check
    request before every album, while still catching stale/restarted containers.
    """
    settings = settings or load_settings()
    key = _cache_key(settings)
    now = time.monotonic()
    if not force:
        cached = _COMPAT_CACHE.get(key)
        if cached and now - cached[0] <= _COMPAT_CACHE_SECONDS:
            info = cached[1]
        else:
            info = check_worker(settings)
            _COMPAT_CACHE[key] = (now, info)
    else:
        info = check_worker(settings)
        _COMPAT_CACHE[key] = (now, info)
    compat = info.get('compatibility') or worker_compatibility(info)
    if not compat.get('ok'):
        raise RemoteWorkerError(f'{compat.get("message")}. {worker_update_hint()}')
    return info


def embed_album_remote(
    album_folder: str,
    image_path: str,
    album_key: str,
    *,
    artist: str = '',
    album: str = '',
    backup: bool = False,
    max_artwork_size: Optional[int] = None,
    make_square: bool = False,
    save_folder_cover: bool = False,
    embed: bool = True,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = settings or load_settings()
    ensure_worker_compatible(settings)
    remote_album = map_album_path_to_worker(album_folder, settings)
    if not remote_album:
        raise RemoteWorkerError('Album path does not match the NAS worker path mapping.')
    img_path = Path(image_path)
    if not img_path.exists():
        raise RemoteWorkerError('Artwork image file is missing on the Mac.')
    image_b64 = base64.b64encode(img_path.read_bytes()).decode('ascii')
    payload = {
        'album_folder': remote_album,
        'album_key': album_key,
        'artist': artist or '',
        'album': album or '',
        'image_b64': image_b64,
        'backup': bool(backup),
        'max_artwork_size': int(max_artwork_size or 0) if max_artwork_size else None,
        'make_square': bool(make_square),
        'save_folder_cover': bool(save_folder_cover),
        'embed': bool(embed),
    }
    out = _post('/embed', payload, settings=settings)
    result = out.get('result') if isinstance(out.get('result'), dict) else out
    result['remote_worker'] = True
    result['remote_album_folder'] = remote_album
    result['remote_worker_duration_seconds'] = out.get('_worker_duration_seconds') or out.get('_request_duration_seconds')
    result['remote_worker_job_id'] = out.get('_worker_job_id') or result.get('remote_worker_job_id') or ''
    result['remote_worker_build'] = out.get('_worker_build') or ''
    return result



def worker_path_check(album_folder: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ask the worker whether one mapped album path exists/readable/writable.

    This uses the optional /path-check endpoint.  Older
    workers should be rebuilt to 5.04 so Settings can verify the exact path
    mapping before write jobs run.
    """
    settings = settings or load_settings()
    remote_album = map_album_path_to_worker(album_folder, settings)
    if not remote_album:
        raise RemoteWorkerError('Album path does not match the NAS worker path mapping.')
    out = _post('/path-check', {'album_folder': remote_album}, settings=settings, timeout=10)
    result = out.get('result') if isinstance(out.get('result'), dict) else out
    if isinstance(result, dict):
        result['remote_album_folder'] = remote_album
        result['remote_worker_duration_seconds'] = out.get('_worker_duration_seconds') or out.get('_request_duration_seconds')
    return result

def deep_check_album_remote(album_folder: str, *, target_size: Optional[int] = None, settings: Optional[Dict[str, Any]] = None, problem_files: bool = False) -> Dict[str, Any]:
    settings = settings or load_settings()
    ensure_worker_compatible(settings)
    remote_album = map_album_path_to_worker(album_folder, settings)
    if not remote_album:
        raise RemoteWorkerError('Album path does not match the NAS worker path mapping.')
    payload = {
        'album_folder': remote_album,
        'target_size': int(target_size or 0) if target_size else None,
        'problem_files': bool(problem_files),
    }
    out = _post('/deep-check', payload, settings=settings)
    out['remote_worker'] = True
    out['remote_album_folder'] = remote_album
    out['remote_worker_duration_seconds'] = out.get('_worker_duration_seconds') or out.get('_request_duration_seconds')
    out['remote_worker_job_id'] = out.get('_worker_job_id') or ''
    out['remote_worker_build'] = out.get('_worker_build') or ''
    return out


def _known_scan_albums(settings: Dict[str, Any]) -> list[Dict[str, Any]]:
    try:
        from . import database as db
        resume_info = db.existing_album_resume_info()
    except Exception:
        resume_info = {}
    seen = set()
    out = []
    for info in (resume_info or {}).values():
        if not isinstance(info, dict):
            continue
        album_key = str(info.get('album_key') or '').strip()
        album_path = str(info.get('album_path') or '').strip()
        remote_path = map_album_path_to_worker(album_path, settings)
        if not album_key or not remote_path:
            continue
        dedupe = (album_key, remote_path)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({
            'album_key': album_key,
            'album_path': remote_path,
            'scan_fingerprint': info.get('scan_fingerprint') if isinstance(info.get('scan_fingerprint'), dict) else None,
        })
    return out


def scan_library_remote(
    library_root: str,
    *,
    include_missing: bool = True,
    resume: bool = True,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask the NAS worker to walk/check the library locally on the NAS."""
    settings = settings or load_settings()
    ensure_worker_compatible(settings)
    remote_root = map_album_path_to_worker(library_root, settings)
    if not remote_root:
        raise RemoteWorkerError('Library path does not match the NAS worker path mapping.')
    deep_scan = get_deep_scan_all_files(settings)
    payload = {
        'library_root': remote_root,
        'include_missing': bool(include_missing),
        'resume': bool(resume),
        'deep_scan_all_files': bool(deep_scan),
        'scan_min_artwork_size': get_scan_min_artwork_size(settings),
        'preferred_artwork_size': get_preferred_artwork_size(settings),
        'target_size_match_mode': str(settings.get('target_size_match_mode') or 'Relaxed'),
        'save_approved_artwork_to_album_folder': bool(settings.get('save_approved_artwork_to_album_folder', False)),
        'max_workers': get_scan_worker_threads(settings),
        'known_albums': [] if deep_scan or not resume else _known_scan_albums(settings),
    }
    out = _post('/scan-library', payload, settings=settings, timeout=get_nas_worker_timeout(settings))
    result = out.get('result') if isinstance(out.get('result'), dict) else out
    albums = []
    for item in result.get('albums') or []:
        if not isinstance(item, dict):
            continue
        mapped = dict(item)
        remote_album = str(mapped.get('album_path') or '')
        mapped['remote_album_path'] = remote_album
        mapped['album_path'] = map_worker_path_to_local(remote_album, settings)
        albums.append(mapped)
    result['albums'] = albums
    result['remote_library_root'] = remote_root
    result['local_library_root'] = library_root
    result['remote_worker'] = True
    result['remote_worker_duration_seconds'] = out.get('_worker_duration_seconds') or out.get('_request_duration_seconds')
    result['remote_worker_job_id'] = out.get('_worker_job_id') or ''
    result['remote_worker_build'] = out.get('_worker_build') or result.get('worker_build') or ''
    return result
