"""Shared approve/embed workflow used by modern UI surfaces."""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, Optional

from . import database as db
from .config import get_max_embedded_artwork_size, get_preferred_artwork_size, load_settings
from .embedder import embed_album, iter_music_files, save_approved_artwork_to_album_folder
from .remote_worker import deep_check_album_remote, embed_album_remote, worker_enabled_for_path
from .scanner import _deep_check_album_files, deep_check_album_problem_files
from .state import deep_check_resolved_note

ProgressCallback = Callable[[int, int, str], None]


class ApprovalBlocked(Exception):
    """Raised when an approval cannot safely start."""


def candidate_quality_label(candidate: Optional[Dict[str, Any]] = None) -> str:
    candidate = candidate or {}
    score = int(candidate.get('score') or 0)
    if score >= 80:
        return 'Good'
    if score >= 60:
        return 'Usable'
    return 'Weak'


def candidate_has_risky_warnings(candidate: Optional[Dict[str, Any]] = None) -> bool:
    candidate = candidate or {}
    warnings = [str(w).lower() for w in (candidate.get('warnings') or [])]
    risky_terms = ('below target', 'blurry', 'soft', 'scan', 'photo', 'not square', 'upscaled', 'watermark', 'small file', 'small size')
    if int(candidate.get('score') or 0) < 60:
        return True
    return any(any(term in warning for term in risky_terms) for warning in warnings)


def candidate_needs_warning(candidate: Optional[Dict[str, Any]] = None, settings: Optional[Dict[str, Any]] = None) -> bool:
    settings = settings or load_settings()
    if not bool(settings.get('warn_before_low_confidence_embed', True)):
        return False
    return candidate_quality_label(candidate) == 'Weak' or candidate_has_risky_warnings(candidate)


def candidate_warning_text(candidate: Optional[Dict[str, Any]] = None) -> str:
    candidate = candidate or {}
    quality = candidate_quality_label(candidate)
    warnings = [w for w in (candidate.get('warnings') or []) if 'same image' not in str(w).lower()]
    warn_txt = ', '.join(str(w) for w in warnings[:3]) if warnings else 'low quality score'
    return (
        f'This candidate is marked {quality} and may need review.\n\n'
        f'Score: {int(candidate.get("score") or 0)}/100\n'
        f'Warnings: {warn_txt}\n\n'
        'Embed it anyway?'
    )


def _candidate_id(candidate: Dict[str, Any]) -> Any:
    return candidate.get('candidate_id') if candidate.get('candidate_id') is not None else candidate.get('id')


def _album_folder(candidate: Dict[str, Any]) -> str:
    return str(candidate.get('album_folder') or candidate.get('album_path') or '').strip()


def _deep_check_summary_text(deep: Dict[str, Any], settings: Optional[Dict[str, Any]] = None) -> str:
    deep = deep or {}

    def count(key: str) -> int:
        try:
            return int(deep.get(key) or 0)
        except Exception:
            return 0

    checked = count('checked_files')
    target = deep.get('target_size') or get_preferred_artwork_size(settings)
    if not checked:
        return 'No supported music files checked.'
    bad = []
    for key, label in (
        ('missing_count', 'missing'),
        ('below_target_count', 'below target'),
        ('non_square_count', 'not square'),
        ('incompatible_count', 'not baseline JPEG'),
        ('unreadable_count', 'unreadable'),
    ):
        n = count(key)
        if n:
            bad.append(f'{n} {label}')
    if bad:
        return f'{checked} file(s) checked at {target}px: ' + ', '.join(bad)
    return f'Verified: {checked}/{checked} file(s) have target-size square baseline JPEG artwork at {target}px.'


def _local_deep_check_result(album_path: str, target_size: int, *, problem_files: bool = False) -> Dict[str, Any]:
    try:
        names = sorted(os.listdir(album_path), key=lambda item: item.lower())
    except Exception:
        names = []
    music = [n for n in names if os.path.isfile(os.path.join(album_path, n)) and n.lower().endswith(('.mp3', '.flac', '.m4a', '.mp4'))]
    deep = _deep_check_album_files(album_path, music, target_size)
    deep['source'] = 'mac-local'
    rows = deep_check_album_problem_files(album_path, target_size=target_size, limit=500) if problem_files else []
    return {'deep_file_check': deep, 'problem_files': rows}


def run_album_deep_check(
    info: Dict[str, Any],
    *,
    target_size: Optional[int] = None,
    problem_files: bool = False,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = settings or load_settings()
    album_path = (info or {}).get('album_path') or (info or {}).get('album_folder') or ''
    target = int(target_size or get_preferred_artwork_size(settings))
    if not album_path:
        raise ValueError('Album path is missing.')
    if worker_enabled_for_path(album_path, settings):
        return deep_check_album_remote(album_path, target_size=target, settings=settings, problem_files=problem_files)
    if not os.path.isdir(album_path):
        raise ValueError('Album folder could not be found. Reconnect the drive/NAS or locate the album folder first.')
    return _local_deep_check_result(album_path, target, problem_files=problem_files)


def persist_deep_check_and_verification(
    info: Dict[str, Any],
    result: Dict[str, Any],
    *,
    verification_source: str = 'manual check',
    expected_dimensions: str = '',
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = settings or load_settings()
    key = (info or {}).get('album_key') or ''
    if not key:
        return {'ok': False, 'summary': 'Album key missing.'}
    deep = result.get('deep_file_check') if isinstance(result, dict) else {}
    if not isinstance(deep, dict):
        deep = {}
    rows = list((result or {}).get('problem_files') or []) if isinstance(result, dict) else []
    checked = int(deep.get('checked_files') or 0)
    ok = bool(checked > 0 and not deep.get('requires_action'))
    checked_at = deep.get('checked_at') or db.now()
    summary = _deep_check_summary_text(deep, settings=settings)
    verification = {
        'ok': ok,
        'summary': summary,
        'checked_files': checked,
        'target_size': deep.get('target_size') or get_preferred_artwork_size(settings),
        'checked_at': checked_at,
        'source': verification_source,
        'expected_dimensions': expected_dimensions or '',
        'problem_count': len(rows),
        'problem_files': rows[:50],
    }
    updates = {
        'deep_file_check': deep,
        'last_verification': verification,
        'last_problem_files': {
            'checked_at': checked_at,
            'target_size': verification.get('target_size'),
            'rows': rows[:50],
            'problem_count': len(rows),
            'source': verification_source,
        },
    }
    try:
        db.update_album_notes(key, updates)
    except Exception:
        pass
    try:
        width = deep.get('example_width') or deep.get('min_width')
        height = deep.get('example_height') or deep.get('min_height')
        example = deep.get('first_issue_file') or deep.get('example_file') or None
        width = int(width) if width not in (None, '') else None
        height = int(height) if height not in (None, '') else None
        if width and height:
            db.update_album_path(key, (info or {}).get('album_path') or (info or {}).get('album_folder') or '', example_file=example, width=width, height=height)
    except Exception:
        pass
    return {'ok': ok, 'summary': summary, 'deep_file_check': deep, 'problem_files': rows, 'checked_files': checked}


def verify_album_after_write(
    info: Dict[str, Any],
    *,
    settings: Optional[Dict[str, Any]] = None,
    target_size: Optional[int] = None,
    expected_dimensions: str = '',
    attempts: int = 4,
    delay: float = 0.6,
) -> Dict[str, Any]:
    settings = settings or load_settings()
    last_persisted = None
    for attempt in range(max(1, int(attempts or 1))):
        if attempt:
            time.sleep(float(delay or 0.6))
        result = run_album_deep_check(info, target_size=target_size, problem_files=True, settings=settings)
        persisted = persist_deep_check_and_verification(
            info,
            result,
            verification_source='post-embed verification' if attempt == 0 else f'post-embed verification retry {attempt + 1}',
            expected_dimensions=expected_dimensions,
            settings=settings,
        )
        last_persisted = persisted
        if persisted.get('ok'):
            return persisted
    return last_persisted or {'ok': False, 'summary': 'Verification did not complete.', 'deep_file_check': {}, 'problem_files': []}


def approve_candidate(
    candidate: Dict[str, Any],
    *,
    backup: bool = False,
    progress: Optional[ProgressCallback] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    candidate = dict(candidate or {})
    settings = settings or load_settings()
    album_key = candidate.get('album_key') or ''
    album_folder = _album_folder(candidate)
    image_path = str(candidate.get('image_path') or '').strip()
    if not album_key:
        raise ApprovalBlocked('Album record is missing its queue key.')
    if not image_path or not os.path.exists(image_path):
        raise ApprovalBlocked('The selected artwork image file is missing. Search or import artwork again.')

    use_remote_worker = worker_enabled_for_path(album_folder, settings)
    if not use_remote_worker:
        if not album_folder or not os.path.isdir(album_folder):
            raise ApprovalBlocked('Album folder is unavailable. Reconnect the drive/NAS or locate the album folder, then try again.')
        try:
            embeddable_files = list(iter_music_files(album_folder))
        except Exception as exc:
            raise ApprovalBlocked(f'The album folder could not be read: {exc}') from exc
        if not embeddable_files:
            raise ApprovalBlocked('No supported audio files were found in this album folder.')

    try:
        db.set_candidate_state(_candidate_id(candidate), 'selected', 'selected for approval/embed')
    except Exception:
        pass

    resize_enabled = bool(settings.get('resize_approved_artwork', True))
    max_artwork_size = get_max_embedded_artwork_size(settings) if resize_enabled else None
    folder_copy_enabled = bool(settings.get('save_approved_artwork_to_album_folder', False))
    folder_copy_error = ''
    album_artwork_copy = ''

    if use_remote_worker:
        if progress:
            progress(0, 0, 'NAS worker')
        result = embed_album_remote(
            album_folder,
            image_path,
            album_key,
            artist=candidate.get('artist') or '',
            album=candidate.get('album') or '',
            backup=backup,
            max_artwork_size=max_artwork_size,
            make_square=resize_enabled,
            save_folder_cover=folder_copy_enabled,
            embed=True,
            settings=settings,
        )
        album_artwork_copy = result.get('album_artwork_copy') or ''
    else:
        result = embed_album(
            album_folder,
            image_path,
            album_key,
            backup=backup,
            progress=progress,
            max_artwork_size=max_artwork_size,
            make_square=resize_enabled,
        )
        if folder_copy_enabled:
            try:
                album_artwork_copy = save_approved_artwork_to_album_folder(
                    image_path,
                    candidate.get('artist') or '',
                    candidate.get('album') or '',
                    album_folder,
                    max_artwork_size=max_artwork_size,
                    make_square=resize_enabled,
                )
            except Exception as exc:
                folder_copy_error = str(exc)
                result.setdefault('failed', []).append({
                    'file': album_folder,
                    'error': f'Folder cover copy failed: {exc}',
                })

    result = dict(result or {})
    try:
        album_candidates = db.load_candidates_for_album(album_key, include_rejected=True)
    except Exception:
        album_candidates = [candidate]

    embedded_w = result.get('image_width') or result.get('width') or candidate.get('width')
    embedded_h = result.get('image_height') or result.get('height') or candidate.get('height')
    embedded_dims = f"{embedded_w or '?'}x{embedded_h or '?'}"
    approved_at = db.now()
    failed_items = list(result.get('failed') or [])
    embed_failures = [item for item in failed_items if 'Folder cover copy failed' not in str(item.get('error', ''))]
    try:
        updated_files = int(result.get('updated') or 0)
        total_files = int(result.get('total') or 0)
    except Exception:
        updated_files = total_files = 0
    no_audio_files = bool(result.get('no_audio_files') or total_files <= 0)
    embed_ok = bool(total_files > 0 and updated_files == total_files and not embed_failures)
    verify_required = bool(settings.get('verify_after_embed_before_good', True))
    verification: Dict[str, Any] = {}
    verified_ok = True
    if embed_ok and verify_required:
        verification_info = {
            'album_key': album_key,
            'album_path': album_folder,
            'artist': candidate.get('artist') or '',
            'album': candidate.get('album') or '',
        }
        try:
            verification = verify_album_after_write(
                verification_info,
                settings=settings,
                target_size=max_artwork_size or get_preferred_artwork_size(settings),
                expected_dimensions=embedded_dims,
            )
            verified_ok = bool(verification.get('ok'))
        except Exception as exc:
            verified_ok = False
            verification = {'ok': False, 'summary': f'post-embed verification failed: {exc}', 'checked_at': db.now(), 'problem_files': []}
            try:
                db.update_album_notes(album_key, {'last_verification': verification})
            except Exception:
                pass
        result['post_embed_verification'] = verification

    folder_needs_save = bool(folder_copy_enabled and not album_artwork_copy)
    approval_complete = bool(embed_ok and not folder_needs_save and verified_ok)
    final_status = 'approved' if approval_complete else 'candidate_found'
    if approval_complete:
        final_reason = 'approved artwork embedded and verified' if verify_required else 'approved artwork embedded'
    elif no_audio_files:
        final_reason = 'no supported audio files found; artwork option kept for review'
    elif embed_ok and verify_required and not verified_ok:
        final_reason = 'post-embed verification found problem files; artwork option kept for review'
    else:
        final_reason = 'approval incomplete; artwork option kept for retry'

    if approval_complete:
        db.mark_candidate(_candidate_id(candidate), approved=True, state_reason='approved and embedded successfully')
        db.mark_album_candidates(album_key, rejected=True, except_candidate_id=_candidate_id(candidate), state_reason='superseded by approved artwork')
        db.set_album_status(album_key, 'approved')
    else:
        db.set_album_status(album_key, final_status)
        try:
            db.set_candidate_state(_candidate_id(candidate), 'failed_embed', final_reason)
        except Exception:
            pass

    db.update_album_notes(album_key, {
        'approved_artwork': {
            'source': candidate.get('source') or '',
            'source_detail': candidate.get('source_detail') or '',
            'dimensions': f"{candidate.get('width') or '?'}x{candidate.get('height') or '?'}",
            'embedded_dimensions': embedded_dims,
            'updated_files': updated_files,
            'total_files': total_files,
            'score': int(candidate.get('score') or 0),
            'source_url': candidate.get('source_url') or '',
            'approved_at': approved_at if approval_complete else '',
            'attempted_at': approved_at,
            'resized_to_target': max_artwork_size or '',
            'complete': approval_complete,
            'verify_required': verify_required,
            'verified': bool(verified_ok) if verify_required else False,
            'verification_summary': verification.get('summary', ''),
        },
        'artwork_compatibility': {
            'needs_conversion': bool((not embed_ok) and not no_audio_files),
            'issue': '' if (embed_ok or no_audio_files) else 'embedding failed or incomplete',
            'converted_to': f'{embedded_dims} baseline JPEG' if embed_ok else '',
            'converted_at': approved_at if embed_ok else '',
        },
        'album_folder_cover': {
            'needs_save': folder_needs_save,
            'issue': (folder_copy_error or 'folder cover missing') if folder_needs_save else '',
            'saved_path': album_artwork_copy or '',
            'saved_at': approved_at if album_artwork_copy else '',
            'checked_at': approved_at,
        },
        'state_evaluation': {
            'status': final_status,
            'reason': final_reason,
        },
        'partial_failure': {
            'reason': '' if approval_complete else final_reason,
            'failed_items': len(failed_items),
            'updated_files': updated_files,
            'total_files': total_files,
            'checked_at': approved_at,
        },
    })
    if approval_complete:
        try:
            db.update_album_notes(album_key, deep_check_resolved_note(
                updated_files,
                max_artwork_size or get_preferred_artwork_size(settings),
                embedded_dims,
                source='approve/embed',
            ))
        except Exception:
            pass
    if embedded_w and embedded_h:
        db.update_album_path(album_key, album_folder, width=embedded_w, height=embedded_h)

    result.update({
        'approval_complete': approval_complete,
        'final_status': final_status,
        'final_reason': final_reason,
        'album_key': album_key,
        'album_folder': album_folder,
        'candidate_id': _candidate_id(candidate),
        'album_artwork_copy': album_artwork_copy,
        'candidate_rows_seen': len(album_candidates),
        'updated_files': updated_files,
        'total_files': total_files,
        'failed_items': failed_items,
        'embedded_dimensions': embedded_dims,
        'verification': verification,
    })
    return result
