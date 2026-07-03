import os, csv, re
from collections import Counter
from datetime import datetime
from mutagen import File as MutagenFile
from mutagen.id3 import ID3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from .config import MUSIC_EXTENSIONS, REPORT_DIR, get_scan_min_artwork_size, get_preferred_artwork_size, get_deep_scan_all_files, load_settings
from .utils import image_dimensions_from_bytes, image_dimensions, normalize_for_match, clean_album_name, artwork_compatibility_from_bytes, artwork_compatibility_from_path, artwork_meets_target_size
from .state import evaluate_album_state, status_reason_note
from . import database as db

YEAR_RE = re.compile(r'(19|20)\d{2}')


def _alpha_key(name):
    """Case-insensitive natural-ish sort key for predictable scan order."""
    s = str(name or '')
    parts = re.split(r'(\d+)', s.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def _sort_names(names):
    try:
        return sorted(list(names), key=_alpha_key)
    except Exception:
        return list(names)


def _as_text_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _id3_text(audio, *frame_ids):
    out = []
    for frame_id in frame_ids:
        frame = audio.get(frame_id)
        if frame is None:
            continue
        text = getattr(frame, 'text', None)
        if text is not None:
            out.extend(_as_text_list(text))
        else:
            out.extend(_as_text_list(str(frame)))
    return out


def _id3_txxx(audio, *descriptions):
    wanted = {d.lower() for d in descriptions}
    out = []
    for key, frame in audio.items():
        if not key.startswith('TXXX'):
            continue
        desc = str(getattr(frame, 'desc', '') or '').lower()
        if desc in wanted:
            out.extend(_as_text_list(getattr(frame, 'text', None)))
    return out


def _mp4_values(tags, *keys):
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
                out.extend(_as_text_list(item))
        else:
            out.extend(_as_text_list(value))
    return out


def read_track_metadata(path):
    """Read the album identity tags we care about from MP3/FLAC/M4A/MP4 files."""
    ext = os.path.splitext(path)[1].lower()
    data = {
        'artist': [], 'albumartist': [], 'album': [], 'year': [],
        'mb_release_id': [], 'mb_releasegroup_id': []
    }
    try:
        if ext == '.mp3':
            audio = ID3(path)
            data['artist'].extend(_id3_text(audio, 'TPE1'))
            data['albumartist'].extend(_id3_text(audio, 'TPE2'))
            data['album'].extend(_id3_text(audio, 'TALB'))
            data['year'].extend(_id3_text(audio, 'TDRC', 'TDOR', 'TYER', 'TDAT'))
            data['mb_release_id'].extend(_id3_txxx(audio, 'MusicBrainz Album Id', 'MusicBrainz Release Id', 'MusicBrainz AlbumID'))
            data['mb_releasegroup_id'].extend(_id3_txxx(audio, 'MusicBrainz Release Group Id', 'MusicBrainz Release GroupID'))
        elif ext == '.flac':
            audio = FLAC(path)
            tags = audio.tags or {}
            data['artist'].extend(_tag_values(tags, 'artist'))
            data['albumartist'].extend(_tag_values(tags, 'albumartist', 'album artist'))
            data['album'].extend(_tag_values(tags, 'album'))
            data['year'].extend(_tag_values(tags, 'date', 'year', 'originaldate', 'originalyear'))
            data['mb_release_id'].extend(_tag_values(tags, 'musicbrainz_albumid', 'musicbrainz release id'))
            data['mb_releasegroup_id'].extend(_tag_values(tags, 'musicbrainz_releasegroupid', 'musicbrainz release group id'))
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(path)
            tags = audio.tags or {}
            data['artist'].extend(_mp4_values(tags, '©ART'))
            data['albumartist'].extend(_mp4_values(tags, 'aART'))
            data['album'].extend(_mp4_values(tags, '©alb'))
            data['year'].extend(_mp4_values(tags, '©day'))
            data['mb_release_id'].extend(_mp4_values(tags, '----:com.apple.iTunes:MusicBrainz Album Id', '----:com.apple.iTunes:MusicBrainz Release Id'))
            data['mb_releasegroup_id'].extend(_mp4_values(tags, '----:com.apple.iTunes:MusicBrainz Release Group Id'))
        else:
            audio = MutagenFile(path, easy=True)
            if audio and getattr(audio, 'tags', None):
                tags = audio.tags
                data['artist'].extend(_tag_values(tags, 'artist'))
                data['albumartist'].extend(_tag_values(tags, 'albumartist', 'album artist'))
                data['album'].extend(_tag_values(tags, 'album'))
                data['year'].extend(_tag_values(tags, 'date', 'year', 'originaldate', 'originalyear'))
                data['mb_release_id'].extend(_tag_values(tags, 'musicbrainz_albumid', 'musicbrainz release id'))
                data['mb_releasegroup_id'].extend(_tag_values(tags, 'musicbrainz_releasegroupid', 'musicbrainz release group id'))
    except Exception:
        pass
    return data


def _tag_values(tags, *keys):
    vals = []
    for key in keys:
        try:
            v = tags.get(key)
        except Exception:
            v = None
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            vals.extend([str(x) for x in v if x])
        elif v:
            vals.append(str(v))
    return vals


def _common(values):
    values = [str(v).strip() for v in values if str(v).strip()]
    if not values:
        return ''
    return Counter(values).most_common(1)[0][0]


def parse_folder_identity(folder, library_root):
    rel = os.path.relpath(folder, library_root)
    parts = rel.split(os.sep)
    artist = parts[0] if len(parts) >= 1 and parts[0] != '.' else ''
    album_part = parts[1] if len(parts) >= 2 else (parts[0] if parts and parts[0] != '.' else '')
    year = ''
    album = album_part
    m = re.match(r'^\((\d{4})\)\s*-\s*(.+)$', album_part)
    if m:
        year, album = m.group(1), m.group(2)
    else:
        m = re.match(r'^(\d{4})\s*-\s*(.+)$', album_part)
        if m:
            year, album = m.group(1), m.group(2)
        else:
            m = re.match(r'^(.+?)\s*\((\d{4})\)$', album_part)
            if m:
                album, year = m.group(1), m.group(2)
    return {
        'folder_artist': artist.strip(),
        'folder_album': clean_album_name(album).strip(),
        'folder_year': year,
    }


def inspect_album_identity(root, library_root, music_files):
    folder_meta = parse_folder_identity(root, library_root)
    tag_artists, tag_albumartists, tag_albums, tag_years = [], [], [], []
    mb_release_ids, mb_releasegroup_ids = [], []

    for fn in music_files[:10]:
        fp = os.path.join(root, fn)
        meta = read_track_metadata(fp)
        tag_artists.extend(meta.get('artist', []))
        tag_albumartists.extend(meta.get('albumartist', []))
        tag_albums.extend(meta.get('album', []))
        tag_years.extend(meta.get('year', []))
        mb_release_ids.extend(meta.get('mb_release_id', []))
        mb_releasegroup_ids.extend(meta.get('mb_releasegroup_id', []))

    artist_from_tags = _common(tag_albumartists) or _common(tag_artists)
    album_from_tags = _common(tag_albums)
    year_from_tags = _common([YEAR_RE.search(v).group(0) for v in tag_years if YEAR_RE.search(v)])

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
        'mb_release_id': _common(mb_release_ids),
        'mb_releasegroup_id': _common(mb_releasegroup_ids),
        'identity_confidence': confidence,
        'track_count': len(music_files),
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


def get_album_path(folder, library_root):
    rel = os.path.relpath(folder, library_root)
    parts = rel.split(os.sep)
    return os.path.join(library_root, parts[0], parts[1]) if len(parts) >= 2 else folder


def album_key(artist, album, album_path):
    return '|'.join([os.path.normcase(os.path.abspath(album_path)), normalize_for_match(artist), normalize_for_match(album)])


def _artwork_item(data):
    size = image_dimensions_from_bytes(data)
    if not size:
        return None
    compat = artwork_compatibility_from_bytes(data)
    item = {'width': size[0], 'height': size[1], 'bytes': data}
    item.update({
        'format': compat.get('format') or '',
        'is_baseline_jpeg': bool(compat.get('is_baseline_jpeg')),
        'is_progressive_jpeg': bool(compat.get('is_progressive_jpeg')),
        'compatible': bool(compat.get('compatible')),
        'compatibility_issue': compat.get('issue') or '',
    })
    return item


def embedded_artwork(path):
    ext = os.path.splitext(path)[1].lower(); out = []
    try:
        if ext == '.mp3':
            audio = ID3(path)
            for tag in audio.values():
                if getattr(tag, 'FrameID', None) == 'APIC':
                    item = _artwork_item(tag.data)
                    if item:
                        out.append(item)
        elif ext == '.flac':
            audio = FLAC(path)
            for pic in audio.pictures:
                item = _artwork_item(pic.data)
                if item:
                    out.append(item)
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(path)
            covr = audio.tags.get('covr', []) if audio.tags else []
            for cover in covr:
                item = _artwork_item(bytes(cover))
                if item:
                    out.append(item)
    except Exception:
        pass
    return out


def _album_folder_cover_status(album_path, target_size=None):
    """Check whether a player-friendly cover.jpg exists in the album folder.

    When the user has enabled album-folder artwork copies, scans should also
    flag albums where embedded art is OK but the folder copy is missing or not
    a target-size baseline JPEG. The app writes cover.jpg, so that is treated
    as the preferred compatible file; older cover.png/webp/jpeg variants are
    reported so the user can convert/save the current embedded art.
    """
    target_size = int(target_size or get_preferred_artwork_size())
    folder = album_path or ''
    if not folder or not os.path.isdir(folder):
        return {'ok': False, 'issue': 'album folder unavailable', 'path': ''}
    candidates = [
        os.path.join(folder, 'cover.jpg'),
        os.path.join(folder, 'cover.jpeg'),
        os.path.join(folder, 'cover.png'),
        os.path.join(folder, 'cover.webp'),
    ]
    existing = next((p for p in candidates if os.path.isfile(p)), '')
    if not existing:
        return {'ok': False, 'issue': 'folder cover missing', 'path': ''}
    dims = image_dimensions(existing)
    compat = artwork_compatibility_from_path(existing)
    if not existing.lower().endswith('.jpg'):
        return {'ok': False, 'issue': f'folder cover is {os.path.splitext(existing)[1].lstrip(".").upper() or "non-JPG"}', 'path': existing, 'dimensions': dims}
    if not compat.get('compatible'):
        return {'ok': False, 'issue': f'folder cover {compat.get("issue") or "not baseline JPEG"}', 'path': existing, 'dimensions': dims}
    if not dims:
        return {'ok': False, 'issue': 'folder cover unreadable', 'path': existing}
    w, h = dims
    if not artwork_meets_target_size(w, h, target_size):
        return {'ok': False, 'issue': f'folder cover below target ({w}×{h})', 'path': existing, 'dimensions': dims}
    return {'ok': True, 'issue': '', 'path': existing, 'dimensions': dims}


def _deep_check_album_files(root, music, target_size):
    """Inspect every supported file in an album folder for target-size/baseline art.

    Normal scans are deliberately quick and stop at the first actionable issue.
    Deep Check mode needs the full picture so an album cannot be marked Good
    just because the first track looks fine while a later track is missing,
    undersized, or has progressive/non-JPEG artwork.
    """
    target_size = int(target_size or get_preferred_artwork_size())
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
        'checked_at': db.now(),
    }

    def note_issue(fn, issue):
        if not result['first_issue_file']:
            result['first_issue_file'] = fn
            result['first_issue'] = issue

    for fn in music:
        fp = os.path.join(root, fn)
        result['checked_files'] += 1
        arts = embedded_artwork(fp)
        if not arts:
            result['missing_count'] += 1
            note_issue(fn, 'missing embedded artwork')
            continue

        # Use the largest embedded image in the file as the representative
        # cover, but still treat any non-compatible embedded cover as a convert
        # requirement so players do not encounter progressive/non-JPEG art.
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

        if not best:
            result['unreadable_count'] += 1
            note_issue(fn, 'unreadable embedded artwork')
            continue

        w, h = best.get('width'), best.get('height')
        try:
            w = int(w or 0); h = int(h or 0)
        except Exception:
            w = h = 0
        if w <= 0 or h <= 0:
            result['unreadable_count'] += 1
            note_issue(fn, 'unreadable embedded artwork')
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
        file_not_square = bool(w != h)
        file_below_target = not artwork_meets_target_size(w, h, target_size)
        if file_not_square:
            result['non_square_count'] += 1
            if not result.get('first_non_square_file'):
                result['first_non_square_file'] = fn
                result['first_non_square_dimensions'] = f'{w}×{h}'
        if file_below_target:
            result['below_target_count'] += 1
            note_issue(fn, f'below target ({w}×{h})')
        # ok_count means the track has fully acceptable artwork, not merely that
        # its dimensions reach the target. Progressive JPEG/PNG and non-square
        # files must not be counted as OK in Deep Check summaries.
        if not file_incompatible and not file_not_square and not file_below_target:
            result['ok_count'] += 1

    result['requires_action'] = bool(
        result['missing_count'] or result['below_target_count'] or
        result['non_square_count'] or result['incompatible_count'] or result['unreadable_count']
    )
    return result


def deep_check_album_problem_files(root, target_size=None, limit=200):
    """Return detailed per-file artwork problems for one album folder.

    Deep Check stores counts and the first issue so normal queue drawing stays
    fast.  This on-demand helper is used by the UI when the user explicitly asks
    to see which tracks are holding the album back.
    """
    try:
        names = _sort_names(os.listdir(root))
    except Exception:
        return []
    music = [n for n in names if os.path.isfile(os.path.join(root, n)) and n.lower().endswith(MUSIC_EXTENSIONS)]
    try:
        target = int(target_size or get_preferred_artwork_size())
    except Exception:
        target = int(get_preferred_artwork_size())
    rows = []
    for fn in music:
        if limit and len(rows) >= int(limit):
            break
        fp = os.path.join(root, fn)
        issues = []
        dims = ''
        arts = embedded_artwork(fp)
        if not arts:
            rows.append({'file': fn, 'dimensions': '', 'issues': ['missing embedded artwork']})
            continue
        best = None
        for art in arts:
            try:
                w = int(art.get('width') or 0)
                h = int(art.get('height') or 0)
                area = w * h
            except Exception:
                w = h = area = 0
            if best is None or area > int(best.get('width') or 0) * int(best.get('height') or 0):
                best = art
            if not art.get('compatible'):
                issue = art.get('compatibility_issue') or 'not baseline JPEG'
                if issue not in issues:
                    issues.append(issue)
        if not best:
            rows.append({'file': fn, 'dimensions': '', 'issues': ['unreadable embedded artwork']})
            continue
        try:
            w = int(best.get('width') or 0)
            h = int(best.get('height') or 0)
        except Exception:
            w = h = 0
        if w <= 0 or h <= 0:
            rows.append({'file': fn, 'dimensions': '', 'issues': ['unreadable embedded artwork']})
            continue
        dims = f'{w}×{h}'
        if not artwork_meets_target_size(w, h, target):
            issues.append(f'below target {target}px')
        if w != h:
            issues.append('not square')
        if issues:
            rows.append({'file': fn, 'dimensions': dims, 'issues': issues})
    return rows


def _deep_check_summary(check):
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


def analyze_album_folder(root, library_root, include_missing=True, music_files=None, identity=None, min_artwork_size=None, force_deep_check=False):
    """Inspect one album folder and return a queue row if artwork needs action.

    `scan_library` passes the file list and identity it has already computed,
    avoiding an extra os.listdir() and a second metadata read of up to 10 tracks
    for every album folder.
    """
    if music_files is None:
        try:
            names = _sort_names(os.listdir(root))
        except Exception:
            return None
        music = [n for n in names if os.path.isfile(os.path.join(root, n)) and n.lower().endswith(MUSIC_EXTENSIONS)]
    else:
        music = list(music_files)
    if not music:
        return None

    try:
        settings = load_settings()
    except Exception:
        settings = {}
    deep_check_enabled = bool(force_deep_check or get_deep_scan_all_files(settings))
    min_artwork_size = int(min_artwork_size or (get_preferred_artwork_size(settings) if deep_check_enabled else get_scan_min_artwork_size(settings)))
    identity = identity or inspect_album_identity(root, library_root, music)
    artist = identity['artist']
    album = identity['album']
    album_path = get_album_path(root, library_root)
    key = album_key(artist, album, album_path)

    album_low = False
    album_incompatible = False
    album_not_square = False
    folder_cover_issue = ''
    folder_cover_status = None
    compatibility_issue = ''
    example = None
    dims = (None, None)
    deep_check = None

    if deep_check_enabled:
        deep_check = _deep_check_album_files(root, music, get_preferred_artwork_size(settings))
        notes = dict(identity.get('notes') or {})
        notes['deep_file_check'] = deep_check
        identity = dict(identity)
        identity['notes'] = notes
        example = deep_check.get('first_issue_file') or deep_check.get('example_file') or (music[0] if music else '')
        dims = (deep_check.get('example_width') or deep_check.get('min_width'), deep_check.get('example_height') or deep_check.get('min_height'))
        if deep_check.get('missing_count') or deep_check.get('unreadable_count'):
            album_low = True
            if deep_check.get('missing_count') and not dims[0]:
                dims = (None, None)
        if deep_check.get('below_target_count'):
            album_low = True
        if deep_check.get('non_square_count'):
            album_not_square = True
        if deep_check.get('incompatible_count'):
            album_incompatible = True
            compatibility_issue = _deep_check_summary(deep_check) or 'embedded artwork needs conversion'
    else:
        for fn in music:
            fp = os.path.join(root, fn)
            arts = embedded_artwork(fp)
            if not arts:
                example = example or fn
                if include_missing:
                    album_low = True
                    dims = (None, None)
                continue
            for art in arts:
                w, h = art['width'], art['height']
                if dims == (None, None):
                    dims = (w, h)
                    example = example or fn
                if not artwork_meets_target_size(w, h, min_artwork_size):
                    album_low = True
                    example = fn
                    dims = (w, h)
                    break
                if w != h:
                    album_not_square = True
                    example = fn
                    dims = (w, h)
                    break
                if not art.get('compatible'):
                    album_incompatible = True
                    example = fn
                    dims = (w, h)
                    compatibility_issue = art.get('compatibility_issue') or 'not baseline JPEG'
                    break
            if album_low or album_not_square or album_incompatible:
                break
    if settings.get('save_approved_artwork_to_album_folder', False):
        folder_cover_status = _album_folder_cover_status(album_path, get_preferred_artwork_size(settings))
        if not folder_cover_status.get('ok'):
            album_incompatible = True
            folder_cover_issue = folder_cover_status.get('issue') or 'folder cover missing'
            compatibility_issue = compatibility_issue or folder_cover_issue

    if album_low or album_not_square or album_incompatible:
        if album_incompatible and deep_check_enabled:
            notes = dict(identity.get('notes') or {})
            notes['artwork_compatibility'] = {
                'issue': compatibility_issue or 'one or more files need baseline JPEG conversion',
                'needs_conversion': True,
                'format': compatibility_issue or 'deep file check',
            }
            identity = dict(identity)
            identity['notes'] = notes
        if album_incompatible and not album_low:
            notes = dict(identity.get('notes') or {})
            notes['artwork_compatibility'] = {
                'issue': compatibility_issue,
                'needs_conversion': True,
                'format': compatibility_issue,
            }
            if folder_cover_issue:
                notes['album_folder_cover'] = {
                    'needs_save': True,
                    'issue': folder_cover_issue,
                    'path': (folder_cover_status or {}).get('path') or '',
                    'checked_at': db.now(),
                }
            identity = dict(identity)
            identity['notes'] = notes
        elif folder_cover_issue:
            notes = dict(identity.get('notes') or {})
            notes['album_folder_cover'] = {
                'needs_save': True,
                'issue': folder_cover_issue,
                'path': (folder_cover_status or {}).get('path') or '',
                'checked_at': db.now(),
            }
            identity = dict(identity)
            identity['notes'] = notes
        return (artist, album, dims[0] or 'Missing', dims[1] or 'Missing', example or '', album_path, key, identity)

    notes = dict(identity.get('notes') or {})
    status, status_reason = evaluate_album_state(
        dims[0],
        dims[1],
        notes,
        current_status='already_good',
        candidate_count=0,
        target_size=min_artwork_size,
        preserve_user_terminal=False,
    )
    notes.update(status_reason_note(status, status_reason))
    identity = dict(identity)
    identity['notes'] = notes
    db.upsert_album(
        key, artist, album, album_path, status=status,
        width=dims[0], height=dims[1], example_file=example or '', meta=identity,
    )
    return None


def folder_has_music(root):
    try:
        names = _sort_names(os.listdir(root))
    except Exception:
        return False
    return any(os.path.isfile(os.path.join(root, n)) and n.lower().endswith(MUSIC_EXTENSIONS) for n in names)


def count_album_folders(library_root, stop_event=None):
    total = 0
    for root, dirs, files in os.walk(library_root):
        dirs[:] = _sort_names(dirs)
        files = _sort_names(files)
        if stop_event and stop_event.is_set():
            break
        if any(n.lower().endswith(MUSIC_EXTENSIONS) for n in files):
            total += 1
    return total


def scan_library(library_root, include_missing=True, progress=None, stop_event=None, on_album=None, total_albums=None, resume=True):
    rows = []
    seen_keys = set()
    processed_music = 0
    try:
        scan_settings = load_settings()
    except Exception:
        scan_settings = {}
    deep_check_enabled = get_deep_scan_all_files(scan_settings)
    # Deep Check must not skip already-known albums; otherwise turning the
    # setting on would leave old rows based on the faster scan. Normal
    # Scan/Resume keeps its quick incremental behaviour.
    known_keys = db.existing_album_keys() if (resume and not deep_check_enabled) else set()
    last_db_progress = 0
    min_artwork_size = get_preferred_artwork_size(scan_settings) if deep_check_enabled else get_scan_min_artwork_size(scan_settings)

    for root, dirs, files in os.walk(library_root):
        dirs[:] = _sort_names(dirs)
        files = _sort_names(files)
        if stop_event and stop_event.is_set():
            break
        music = [n for n in files if n.lower().endswith(MUSIC_EXTENSIONS)]
        if not music:
            continue

        processed_music += 1
        if progress:
            progress(processed_music, total_albums, root)

        identity = inspect_album_identity(root, library_root, music)
        artist = identity['artist']
        album = identity['album']
        album_path = get_album_path(root, library_root)
        key = album_key(artist, album, album_path)

        if resume and key in known_keys:
            if processed_music - last_db_progress >= 25:
                db.update_scan_progress(processed_music, total_albums)
                last_db_progress = processed_music
            continue

        row = analyze_album_folder(root, library_root, include_missing=include_missing, music_files=music, identity=identity, min_artwork_size=min_artwork_size)
        if not row:
            known_keys.add(key)
            if processed_music - last_db_progress >= 25:
                db.update_scan_progress(processed_music, total_albums)
                last_db_progress = processed_music
            continue

        artist, album, w, h, example, album_path, key, identity = row
        if key not in seen_keys:
            rows.append(row)
            seen_keys.add(key)
            known_keys.add(key)
            notes = identity.get('notes') or {}
            width_value = None if w == 'Missing' else w
            height_value = None if h == 'Missing' else h
            status, status_reason = evaluate_album_state(
                width_value,
                height_value,
                notes,
                current_status='needs_review',
                candidate_count=0,
                target_size=min_artwork_size,
                preserve_user_terminal=False,
            )
            if status == 'already_good':
                status = 'needs_review'
                status_reason = 'artwork needs review'
            try:
                identity = dict(identity)
                merged_notes = dict(identity.get('notes') or {})
                merged_notes.update(status_reason_note(status, status_reason))
                identity['notes'] = merged_notes
            except Exception:
                pass
            db.upsert_album(key, artist, album, album_path, status=status,
                            width=width_value,
                            height=height_value,
                            example_file=example,
                            meta=identity)
            db.update_scan_progress(processed_music, total_albums)
            last_db_progress = processed_music
            info = {
                'artist': artist,
                'album': album,
                'album_path': album_path,
                'album_key': key,
                'search_artist': identity.get('search_artist', artist),
                'search_album': identity.get('search_album', album),
                'year': identity.get('year', ''),
                'mb_release_id': identity.get('mb_release_id', ''),
                'mb_releasegroup_id': identity.get('mb_releasegroup_id', ''),
                'identity_confidence': identity.get('identity_confidence', ''),
                'notes': identity.get('notes', {}),
            }
            if on_album:
                on_album(row, info, len(rows))

    db.update_scan_progress(processed_music, total_albums)
    return rows, {}


def write_low_res_csv(rows):
    path = REPORT_DIR / f'low_res_artwork_folders_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['Artist Folder', 'Album Folder', 'Artwork Width', 'Artwork Height', 'Example File', 'Album Path', 'Album Key'])
        clean_rows = [r[:7] for r in rows]
        w.writerows(clean_rows)
    return str(path)
