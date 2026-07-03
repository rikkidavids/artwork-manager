"""Shared album queue-state evaluation helpers.

All code paths that need to decide an album's queue state should use this
module.  Keeping the decision here prevents contradictory rows such as a handled
album displaying as Convert because an old note was still attached, or an album
with saved options displaying as Needs Search.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Tuple, Optional

from .config import get_scan_min_artwork_size, get_preferred_artwork_size, load_settings
from .utils import artwork_meets_target_size

STATUS_CANDIDATE_FOUND = 'candidate_found'
STATUS_NEEDS_REVIEW = 'needs_review'
STATUS_MISSING_ARTWORK = 'missing_artwork'
STATUS_INCOMPATIBLE_ARTWORK = 'incompatible_artwork'
STATUS_NOT_SQUARE_ARTWORK = 'not_square_artwork'
STATUS_NO_CANDIDATE = 'no_candidate'
STATUS_APPROVED = 'approved'
STATUS_SKIPPED = 'reviewed_skipped'
STATUS_ALREADY_GOOD = 'already_good'
STATUS_IGNORED = 'ignored'

# Skipped and Ignored are explicit user decisions.  Preserve them unless the
# caller is deliberately re-reading disk and wants to re-open the row.
TERMINAL_USER_STATES = {STATUS_IGNORED, STATUS_SKIPPED}
COMPLETED_STATES = {STATUS_APPROVED, STATUS_ALREADY_GOOD, STATUS_SKIPPED, STATUS_IGNORED}

NEEDS_ATTENTION_STATES = {STATUS_NEEDS_REVIEW, STATUS_MISSING_ARTWORK, STATUS_NOT_SQUARE_ARTWORK, STATUS_INCOMPATIBLE_ARTWORK, STATUS_NO_CANDIDATE}
REVIEW_STATES = {STATUS_CANDIDATE_FOUND}
GOOD_STATES = {STATUS_APPROVED, STATUS_ALREADY_GOOD}


def workflow_bucket_for_status(status: str, *, active_search: bool = False) -> str:
    """Return the user-facing queue bucket for a stored/evaluated status.

    This keeps filter membership separate from the raw database status.  The
    queue can therefore show a temporary workflow state (for example actively
    Searching or pinned while the user reviews it) without rewriting the album's
    actual condition.
    """
    if active_search:
        return 'Needs Search'
    status = (status or '').strip()
    if status in {STATUS_NEEDS_REVIEW, STATUS_NO_CANDIDATE, 'pending', 'searching'}:
        return 'Needs Search'
    if status == STATUS_NOT_SQUARE_ARTWORK:
        return 'Not Square'
    if status == STATUS_INCOMPATIBLE_ARTWORK:
        return 'Convert'
    if status == STATUS_MISSING_ARTWORK:
        return 'Missing'
    if status == STATUS_CANDIDATE_FOUND:
        return 'Review'
    if status in GOOD_STATES:
        return 'Good'
    if status in {STATUS_SKIPPED, STATUS_IGNORED}:
        return 'Handled'
    return status.replace('_', ' ').title() if status else ''


def needs_attention_status(status: str) -> bool:
    return workflow_bucket_for_status(status) in {'Needs Search', 'Missing', 'Not Square', 'Convert'}


def workflow_note(state: str = '', reason: str = '', pinned_album_key: str = '') -> Dict[str, Any]:
    """Metadata for temporary user workflow state, not actual album status."""
    return {'workflow_state': {'state': state or '', 'reason': reason or '', 'pinned_album_key': pinned_album_key or ''}}


def normalise_notes(notes: Any) -> Dict[str, Any]:
    if isinstance(notes, dict):
        return dict(notes)
    if isinstance(notes, str) and notes.strip():
        try:
            parsed = json.loads(notes)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def folder_cover_required(settings: Any = None) -> bool:
    """Return whether missing/stale cover.jpg should affect queue state."""
    try:
        cfg = settings if isinstance(settings, dict) else load_settings()
    except Exception:
        cfg = {}
    return bool(cfg.get('save_approved_artwork_to_album_folder', False))


def _folder_cover_note_is_stale_after_approval(notes: Dict[str, Any], folder: Dict[str, Any]) -> bool:
    """Detect pre-4.19 folder-cover notes that survived a later approval.

    Those notes did not have a checked_at/saved_at timestamp, so they should not
    be allowed to turn an approved, correctly embedded album back into Convert.
    Fresh scanner/convert/approve notes include checked_at or saved_at and still
    participate in state evaluation.
    """
    approved = notes.get('approved_artwork') or {}
    if not isinstance(approved, dict) or not approved.get('approved_at'):
        return False
    if folder.get('checked_at') or folder.get('saved_at') or folder.get('saved_path'):
        return False
    return True


def needs_convert_reason(notes: Any, settings: Any = None) -> str:
    """Return the current compatibility/folder-cover requirement, if any."""
    notes = normalise_notes(notes)
    compat = notes.get('artwork_compatibility') or {}
    folder = notes.get('album_folder_cover') or {}
    if isinstance(compat, dict) and compat.get('needs_conversion'):
        issue = (compat.get('issue') or compat.get('format') or '').strip()
        if issue:
            return issue
        return 'embedded artwork needs conversion'
    if isinstance(folder, dict) and folder.get('needs_save'):
        if not folder_cover_required(settings):
            return ''
        if _folder_cover_note_is_stale_after_approval(notes, folder):
            return ''
        issue = (folder.get('issue') or '').strip()
        return issue or 'missing cover.jpg'
    return ''


def album_size_ok(width: Any, height: Any, target_size: Any = None) -> bool:
    try:
        target = int(target_size or get_scan_min_artwork_size())
    except Exception:
        target = 0
    return artwork_meets_target_size(width, height, target)


def _candidate_count(candidate_count: Any) -> int:
    try:
        return max(0, int(candidate_count or 0))
    except Exception:
        return 0


def _deep_file_check(notes: Dict[str, Any]) -> Dict[str, Any]:
    return effective_deep_file_check(notes)


def _deep_count(deep: Dict[str, Any], key: str) -> int:
    try:
        return max(0, int(deep.get(key) or 0))
    except Exception:
        return 0


def _time_text(value: Any) -> str:
    return str(value or '').strip()


def _latest_successful_artwork_write_time(notes: Dict[str, Any]) -> str:
    """Return the latest successful artwork-write timestamp in album notes."""
    times = []
    approved = notes.get('approved_artwork') or {}
    if isinstance(approved, dict) and approved.get('complete'):
        for key in ('approved_at', 'attempted_at'):
            value = _time_text(approved.get(key))
            if value:
                times.append(value)
    compat = notes.get('artwork_compatibility') or {}
    if isinstance(compat, dict) and not compat.get('needs_conversion'):
        value = _time_text(compat.get('converted_at'))
        if value:
            times.append(value)
    return max(times) if times else ''


def _deep_file_check_is_stale(notes: Dict[str, Any], deep: Dict[str, Any]) -> bool:
    """Return True when Deep Check facts pre-date a later successful write.

    Deep Check stores per-file artwork problems.  After Approve/Embed or
    Convert/Save rewrites every track successfully, those old per-file counts are
    no longer valid.  Keeping them active can make a freshly embedded 1200x1200
    album continue to display as Not Square or Needs Search.
    """
    if not isinstance(deep, dict) or not deep.get('enabled'):
        return False
    checked_at = _time_text(deep.get('checked_at'))
    write_at = _latest_successful_artwork_write_time(notes)
    return bool(checked_at and write_at and write_at >= checked_at)


def effective_deep_file_check(notes: Any) -> Dict[str, Any]:
    """Return the active Deep Check data, ignoring stale pre-write results."""
    notes_dict = normalise_notes(notes)
    deep = notes_dict.get('deep_file_check') if isinstance(notes_dict, dict) else None
    if not isinstance(deep, dict) or not deep.get('enabled'):
        return {}
    if _deep_file_check_is_stale(notes_dict, deep):
        return {}
    return deep


def deep_check_resolved_note(checked_files: Any, target_size: Any, dimensions: str = '', source: str = '') -> Dict[str, Any]:
    """Build a clean Deep Check note after a successful all-file rewrite."""
    try:
        checked = max(0, int(checked_files or 0))
    except Exception:
        checked = 0
    try:
        target = int(target_size or 0)
    except Exception:
        target = 0
    note = {
        'enabled': True,
        'checked_files': checked,
        'ok_count': checked,
        'missing_count': 0,
        'below_target_count': 0,
        'incompatible_count': 0,
        'non_square_count': 0,
        'unreadable_count': 0,
        'requires_action': False,
        'target_size': target,
        'checked_at': _time_text(datetime.now().isoformat(timespec='seconds')),
    }
    if dimensions:
        w, h = _parse_dimensions_text(dimensions)
        if w > 0 and h > 0:
            note['example_width'] = w
            note['example_height'] = h
            note['min_width'] = w
            note['min_height'] = h
    if source:
        note['source'] = source
    return {'deep_file_check': note}


def _parse_dimensions_text(value: Any) -> Tuple[int, int]:
    """Parse a stored dimension string such as ``1200×1180`` or ``1200x1180``."""
    text = str(value or '').strip().lower().replace('×', 'x')
    if 'x' not in text:
        return 0, 0
    left, right = text.split('x', 1)
    try:
        return int(left.strip()), int(right.strip())
    except Exception:
        return 0, 0


def _deep_first_not_square_dimensions(deep: Dict[str, Any]) -> Tuple[int, int]:
    dims = deep.get('first_non_square_dimensions') or ''
    w, h = _parse_dimensions_text(dims)
    if w > 0 and h > 0:
        return w, h
    try:
        w = int(deep.get('example_width') or 0)
        h = int(deep.get('example_height') or 0)
    except Exception:
        w = h = 0
    if w > 0 and h > 0 and w != h:
        return w, h
    return 0, 0


def _not_square_is_shape_work(deep: Dict[str, Any], target: Any = None) -> bool:
    """Return True when the deep-check failure is primarily a shape issue.

    A 1200×1180 embedded cover can be counted as both ``below target`` and
    ``not square`` in strict mode.  That should not send the user back to
    provider search: one edge is already at the target, so Convert/Save can
    square/rewrite the current artwork.  Genuinely small covers, such as
    600×500, still remain Needs Search.
    """
    if _deep_count(deep, 'non_square_count') <= 0:
        return False
    try:
        target_i = int(target or deep.get('target_size') or 0)
    except Exception:
        target_i = 0
    if target_i <= 0:
        return True
    w, h = _deep_first_not_square_dimensions(deep)
    if w <= 0 or h <= 0:
        return False
    return max(w, h) >= target_i


def dimensions_not_square(width: Any, height: Any) -> bool:
    """Return True for real, known artwork dimensions that are not square.

    Missing/unreadable artwork is handled by the Missing/Needs Search buckets;
    the Not Square filter should only catch artwork where both sides are known
    and differ.
    """
    try:
        w = int(width or 0)
        h = int(height or 0)
    except Exception:
        return False
    return w > 0 and h > 0 and w != h


def album_has_not_square_artwork(album: Dict[str, Any]) -> bool:
    """Return True when an album has any known non-square embedded artwork.

    Fast scans can only know the representative/preview file dimensions stored
    on the album row. Deep Check stores a per-file non_square_count, so this
    also catches later tracks whose embedded artwork differs from the preview.
    """
    album = album or {}
    notes = normalise_notes(album.get('notes_json') or album.get('notes'))
    deep = _deep_file_check(notes)
    if deep and _deep_count(deep, 'non_square_count') > 0:
        return True
    return dimensions_not_square(album.get('width'), album.get('height'))


def not_square_reason(album: Dict[str, Any]) -> str:
    """Short reason text for the Not Square filter/details pane."""
    album = album or {}
    notes = normalise_notes(album.get('notes_json') or album.get('notes'))
    deep = _deep_file_check(notes)
    if deep and _deep_count(deep, 'non_square_count') > 0:
        checked = _deep_count(deep, 'checked_files')
        count = _deep_count(deep, 'non_square_count')
        fn = deep.get('first_non_square_file') or deep.get('first_issue_file') or ''
        dims = deep.get('first_non_square_dimensions') or ''
        suffix = ''
        if dims:
            suffix += f' ({dims})'
        if fn:
            suffix += f' · first: {fn}'
        return f'{count}/{checked or "?"} file(s) not square{suffix}'
    if dimensions_not_square(album.get('width'), album.get('height')):
        return f'{album.get("width")}×{album.get("height")} is not square'
    return ''


def _target_from_notes(notes: Dict[str, Any], target_size: Any = None, settings: Any = None) -> int:
    try:
        if target_size:
            return int(target_size)
    except Exception:
        pass
    deep = _deep_file_check(notes)
    try:
        if deep.get('target_size'):
            return int(deep.get('target_size'))
    except Exception:
        pass
    try:
        if deep:
            return int(get_preferred_artwork_size(settings if isinstance(settings, dict) else None))
    except Exception:
        pass
    try:
        return int(get_scan_min_artwork_size(settings if isinstance(settings, dict) else None))
    except Exception:
        return 0


def _deep_file_issue_state(deep: Dict[str, Any], current_status: str = '') -> Tuple[str, str]:
    checked = _deep_count(deep, 'checked_files')
    target = deep.get('target_size') or ''
    first_file = deep.get('first_issue_file') or ''
    suffix = f' · first: {first_file}' if first_file else ''
    missing = _deep_count(deep, 'missing_count')
    below = _deep_count(deep, 'below_target_count')
    incompatible = _deep_count(deep, 'incompatible_count')
    unreadable = _deep_count(deep, 'unreadable_count')
    if missing:
        reason = f'{missing}/{checked or "?"} file(s) missing embedded artwork{suffix}'
        return (STATUS_NO_CANDIDATE if current_status == STATUS_NO_CANDIDATE else STATUS_MISSING_ARTWORK), reason
    if unreadable:
        reason = f'{unreadable}/{checked or "?"} file(s) have unreadable embedded artwork{suffix}'
        return (STATUS_NO_CANDIDATE if current_status == STATUS_NO_CANDIDATE else STATUS_MISSING_ARTWORK), reason
    non_square = _deep_count(deep, 'non_square_count')
    if non_square and _not_square_is_shape_work(deep, target):
        ns_file = deep.get('first_non_square_file') or first_file
        ns_dims = deep.get('first_non_square_dimensions') or ''
        ns_suffix = f' · first: {ns_file}' if ns_file else suffix
        dims_txt = f' ({ns_dims})' if ns_dims else ''
        return STATUS_NOT_SQUARE_ARTWORK, f'{non_square}/{checked or "?"} file(s) not square{dims_txt}{ns_suffix}'
    if below:
        target_txt = f' {target}px' if target else ''
        reason = f'{below}/{checked or "?"} file(s) below target{target_txt}{suffix}'
        return (STATUS_NO_CANDIDATE if current_status == STATUS_NO_CANDIDATE else STATUS_NEEDS_REVIEW), reason
    if non_square:
        ns_file = deep.get('first_non_square_file') or first_file
        ns_dims = deep.get('first_non_square_dimensions') or ''
        ns_suffix = f' · first: {ns_file}' if ns_file else suffix
        dims_txt = f' ({ns_dims})' if ns_dims else ''
        return STATUS_NOT_SQUARE_ARTWORK, f'{non_square}/{checked or "?"} file(s) not square{dims_txt}{ns_suffix}'
    if incompatible:
        reason = f'{incompatible}/{checked or "?"} file(s) not baseline JPEG{suffix}'
        return STATUS_INCOMPATIBLE_ARTWORK, reason
    return '', ''


def evaluate_album_state(
    width: Any = None,
    height: Any = None,
    notes: Any = None,
    current_status: str = '',
    candidate_count: Any = 0,
    target_size: Any = None,
    preserve_user_terminal: bool = True,
    settings: Any = None,
) -> Tuple[str, str]:
    """Return ``(status, reason)`` for an album from current facts.

    Precedence is intentional and mirrors the queue workflow:
    Review > Missing > Needs Search > Not Square > Convert > Good.

    Shape wins when one edge already reaches the target but the other edge is
    short because the embedded image is not square.  Genuinely small artwork
    remains Needs Search.
    """
    status = (current_status or '').strip()
    notes_dict = normalise_notes(notes)

    if preserve_user_terminal and status in TERMINAL_USER_STATES:
        return status, 'user handled'

    candidates = _candidate_count(candidate_count)
    if candidates > 0 and status not in {STATUS_APPROVED, STATUS_ALREADY_GOOD}:
        return STATUS_CANDIDATE_FOUND, f'{candidates} option' + ('' if candidates == 1 else 's')

    deep = _deep_file_check(notes_dict)
    if deep:
        deep_status, deep_reason = _deep_file_issue_state(deep, status)
        if deep_status:
            return deep_status, deep_reason

    reason = needs_convert_reason(notes_dict, settings)

    try:
        w = int(width or 0)
        h = int(height or 0)
    except Exception:
        w = h = 0

    if w <= 0 or h <= 0:
        if status == STATUS_NO_CANDIDATE:
            return STATUS_NO_CANDIDATE, 'no suitable artwork found; embedded artwork missing'
        return STATUS_MISSING_ARTWORK, 'embedded artwork missing'

    target_for_size = _target_from_notes(notes_dict, target_size, settings)
    size_ok = album_size_ok(w, h, target_for_size)

    # Shape is a visible artwork problem, so it wins over conversion/format
    # cleanup.  If one edge already reaches the target, the best next action is
    # Convert/Save to square/rewrite the current artwork, not a provider search.
    if dimensions_not_square(w, h):
        try:
            target_i = int(target_for_size or 0)
        except Exception:
            target_i = 0
        if size_ok or target_i <= 0 or max(w, h) >= target_i:
            return STATUS_NOT_SQUARE_ARTWORK, f'embedded artwork not square ({w}×{h})'

    if not size_ok:
        if status == STATUS_NO_CANDIDATE:
            return STATUS_NO_CANDIDATE, f'no suitable artwork found; embedded artwork below target ({w}×{h})'
        return STATUS_NEEDS_REVIEW, f'embedded artwork below target ({w}×{h})'

    if reason:
        return STATUS_INCOMPATIBLE_ARTWORK, reason

    if status == STATUS_APPROVED:
        return STATUS_APPROVED, 'approved artwork embedded'

    return STATUS_ALREADY_GOOD, f'{w}×{h} OK'



def evaluate_album_record(
    album: Dict[str, Any],
    *,
    candidate_count: Optional[Any] = None,
    target_size: Any = None,
    preserve_user_terminal: bool = True,
    settings: Any = None,
) -> Tuple[str, str]:
    """Evaluate a database album row/dict without changing the database."""
    album = album or {}
    notes = album.get('notes_json') if isinstance(album, dict) else None
    if notes is None and isinstance(album, dict):
        notes = album.get('notes')
    if candidate_count is None and isinstance(album, dict):
        candidate_count = album.get('candidate_count') or 0
    return evaluate_album_state(
        (album or {}).get('width'),
        (album or {}).get('height'),
        notes,
        current_status=(album or {}).get('status') or '',
        candidate_count=candidate_count or 0,
        target_size=target_size,
        preserve_user_terminal=preserve_user_terminal,
        settings=settings,
    )


def good_reason_from_notes(album: Dict[str, Any]) -> str:
    """Return a short explanation for why an album is considered Good."""
    album = album or {}
    notes = normalise_notes(album.get('notes_json') or album.get('notes'))
    state_eval = notes.get('state_evaluation') or {}
    if isinstance(state_eval, dict) and state_eval.get('reason'):
        return str(state_eval.get('reason'))
    deep = _deep_file_check(notes)
    if deep and _deep_count(deep, 'checked_files'):
        checked = _deep_count(deep, 'checked_files')
        target = deep.get('target_size') or ''
        target_txt = f' at {target}px' if target else ''
        return f'deep checked {checked} file(s){target_txt}'
    approved = notes.get('approved_artwork') or {}
    if isinstance(approved, dict) and approved.get('complete'):
        dims = approved.get('embedded_dimensions') or approved.get('dimensions') or ''
        return ('approved artwork embedded' + (f' ({dims})' if dims else '')).strip()
    compat = notes.get('artwork_compatibility') or {}
    if isinstance(compat, dict) and compat.get('accepted_as_is_at'):
        return 'marked Good by user'
    try:
        w = int(album.get('width') or 0)
        h = int(album.get('height') or 0)
        if w > 0 and h > 0:
            return f'{w}×{h} OK'
    except Exception:
        pass
    return 'marked Good'


def status_reason_note(status: str, reason: str) -> Dict[str, Any]:
    return {'state_evaluation': {'status': status or '', 'reason': reason or ''}}
