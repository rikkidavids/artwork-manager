import sqlite3, json, os, threading
from datetime import datetime
from .config import DB_PATH

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS albums (
  album_key TEXT PRIMARY KEY,
  artist TEXT, album TEXT, album_path TEXT,
  status TEXT DEFAULT 'pending',
  width INTEGER, height INTEGER, example_file TEXT,
  last_scanned TEXT,
  notes TEXT,
  search_artist TEXT,
  search_album TEXT,
  year TEXT,
  mb_release_id TEXT,
  mb_releasegroup_id TEXT,
  identity_confidence TEXT,
  track_count INTEGER
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  album_key TEXT, source TEXT, image_path TEXT, width INTEGER, height INTEGER,
  source_url TEXT, source_detail TEXT, release_title TEXT, release_mbid TEXT, source_meta TEXT, warnings TEXT, score INTEGER DEFAULT 0, score_summary TEXT, rejected INTEGER DEFAULT 0,
  approved INTEGER DEFAULT 0, candidate_state TEXT DEFAULT 'available', state_reason TEXT, state_updated_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  album_key TEXT, action TEXT, payload TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS scan_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  library_root TEXT,
  is_running INTEGER DEFAULT 0,
  stop_requested INTEGER DEFAULT 0,
  processed_albums INTEGER DEFAULT 0,
  total_albums INTEGER DEFAULT 0,
  updated_at TEXT
);
'''

ALBUM_ADDITIONS = [
    ('artist', 'TEXT'), ('album', 'TEXT'), ('album_path', 'TEXT'),
    ('status', "TEXT DEFAULT 'pending'"),
    ('width', 'INTEGER'), ('height', 'INTEGER'), ('example_file', 'TEXT'),
    ('last_scanned', 'TEXT'), ('notes', 'TEXT'),
    ('search_artist', 'TEXT'), ('search_album', 'TEXT'), ('year', 'TEXT'),
    ('mb_release_id', 'TEXT'), ('mb_releasegroup_id', 'TEXT'), ('identity_confidence', 'TEXT'),
    ('track_count', 'INTEGER'),
]
CANDIDATE_ADDITIONS = [
    ('source_url', 'TEXT'), ('source_detail', 'TEXT'), ('release_title', 'TEXT'),
    ('release_mbid', 'TEXT'), ('source_meta', 'TEXT'), ('warnings', 'TEXT'),
    ('score', 'INTEGER DEFAULT 0'), ('score_summary', 'TEXT'),
    ('rejected', 'INTEGER DEFAULT 0'), ('approved', 'INTEGER DEFAULT 0'),
    ('candidate_state', "TEXT DEFAULT 'available'"), ('state_reason', 'TEXT'),
    ('state_updated_at', 'TEXT'), ('created_at', 'TEXT'),
]
SCAN_ADDITIONS = [
    ('library_root', 'TEXT'), ('is_running', 'INTEGER DEFAULT 0'),
    ('stop_requested', 'INTEGER DEFAULT 0'), ('processed_albums', 'INTEGER DEFAULT 0'),
    ('total_albums', 'INTEGER DEFAULT 0'), ('updated_at', 'TEXT'),
]


def _initialise_schema(conn):
    """Create/migrate the SQLite schema once per app process.

    Older builds ran the full CREATE TABLE / PRAGMA table_info / CREATE INDEX
    sequence on every database call. That is safe, but it is noticeably wasteful
    on large queues because UI refreshes open SQLite frequently. Keep the cheap
    per-connection pragmas in connect(), and do schema/migration work only once.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except Exception:
            pass
        conn.executescript(SCHEMA)
        for table, additions in {
            'albums': ALBUM_ADDITIONS,
            'candidates': CANDIDATE_ADDITIONS,
            'scan_state': SCAN_ADDITIONS,
        }.items():
            cols = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
            for col, typ in additions:
                if col not in cols:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typ}')
        conn.executescript('''
        CREATE INDEX IF NOT EXISTS idx_albums_status_artist_album ON albums(status, artist, album);
        CREATE INDEX IF NOT EXISTS idx_albums_path ON albums(album_path);
        CREATE INDEX IF NOT EXISTS idx_candidates_album_flags ON candidates(album_key, approved, rejected);
        CREATE INDEX IF NOT EXISTS idx_candidates_source_url ON candidates(album_key, source, source_url);
        CREATE INDEX IF NOT EXISTS idx_candidates_score ON candidates(album_key, score);
        CREATE INDEX IF NOT EXISTS idx_candidates_state ON candidates(album_key, candidate_state);
        ''')
        conn.commit()
        _SCHEMA_READY = True


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL + a modest busy timeout makes background searches and UI refreshes
    # less likely to block one another while still keeping the database safe.
    try:
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass
    _initialise_schema(conn)
    return conn

def now():
    return datetime.now().isoformat(timespec='seconds')


def upsert_album(album_key, artist, album, album_path, status='pending', width=None, height=None, example_file=None, meta=None):
    meta = meta or {}
    search_artist = meta.get('search_artist') or artist
    search_album = meta.get('search_album') or album
    year = meta.get('year') or ''
    mb_release_id = meta.get('mb_release_id') or ''
    mb_releasegroup_id = meta.get('mb_releasegroup_id') or ''
    identity_confidence = meta.get('identity_confidence') or ''
    track_count = meta.get('track_count')
    notes = meta.get('notes')
    if isinstance(notes, (dict, list)):
        notes = json.dumps(notes)
    with connect() as c:
        c.execute('''
        INSERT INTO albums(album_key,artist,album,album_path,status,width,height,example_file,last_scanned,
                           notes,search_artist,search_album,year,mb_release_id,mb_releasegroup_id,identity_confidence,track_count)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(album_key) DO UPDATE SET
          artist=excluded.artist,
          album=excluded.album,
          album_path=excluded.album_path,
          status=excluded.status,
          width=excluded.width,
          height=excluded.height,
          example_file=excluded.example_file,
          last_scanned=excluded.last_scanned,
          notes=excluded.notes,
          search_artist=excluded.search_artist,
          search_album=excluded.search_album,
          year=excluded.year,
          mb_release_id=excluded.mb_release_id,
          mb_releasegroup_id=excluded.mb_releasegroup_id,
          identity_confidence=excluded.identity_confidence,
          track_count=excluded.track_count
        ''',
        (album_key, artist, album, album_path, status, width, height, example_file, now(),
         notes, search_artist, search_album, year, mb_release_id, mb_releasegroup_id, identity_confidence, track_count))


def _decode_album(row):
    if not row:
        return None
    d = dict(row)
    notes = d.get('notes')
    if notes:
        try:
            d['notes_json'] = json.loads(notes)
        except Exception:
            d['notes_json'] = None
    else:
        d['notes_json'] = None
    return d


def get_album(album_key):
    with connect() as c:
        row = c.execute('SELECT * FROM albums WHERE album_key=?', (album_key,)).fetchone()
        return _decode_album(row)


def get_album_by_path(album_path):
    with connect() as c:
        row = c.execute('SELECT * FROM albums WHERE album_path=?', (album_path,)).fetchone()
        return _decode_album(row)


def find_album_by_path(album_path):
    return get_album_by_path(album_path)


def _path_resume_key(path):
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(str(path or ''))))
    except Exception:
        return str(path or '')


def existing_album_resume_info():
    """Return saved album path/key/fingerprint data for fast resume scans."""
    out = {}
    with connect() as c:
        rows = c.execute('SELECT album_key, album_path, notes FROM albums WHERE album_path IS NOT NULL AND album_path<>""').fetchall()
    for row in rows:
        path = row['album_path'] or ''
        notes = {}
        try:
            parsed = json.loads(row['notes'] or '{}')
            if isinstance(parsed, dict):
                notes = parsed
        except Exception:
            notes = {}
        info = {
            'album_key': row['album_key'],
            'album_path': path,
            'scan_fingerprint': notes.get('scan_fingerprint') if isinstance(notes.get('scan_fingerprint'), dict) else None,
        }
        out[path] = info
        out[_path_resume_key(path)] = info
    return out


def existing_album_keys():
    """Return all album keys already known to the queue database.

    Scans use this once at startup instead of opening SQLite for every album
    folder. That keeps resume scans much quicker on large libraries.
    """
    with connect() as c:
        return {r['album_key'] for r in c.execute('SELECT album_key FROM albums')}




def update_album_notes(album_key, updates):
    """Merge small status/search metadata into an album's JSON notes."""
    if not album_key:
        return
    updates = updates or {}
    with connect() as c:
        row = c.execute('SELECT notes FROM albums WHERE album_key=?', (album_key,)).fetchone()
        notes = {}
        if row and row['notes']:
            try:
                existing = json.loads(row['notes'])
                if isinstance(existing, dict):
                    notes.update(existing)
            except Exception:
                pass
        notes.update(updates)
        c.execute('UPDATE albums SET notes=?, last_scanned=? WHERE album_key=?', (json.dumps(notes), now(), album_key))


def update_album_path(album_key, album_path, example_file=None, width=None, height=None):
    """Repair a saved queue item's album folder path after a drive/NAS move."""
    if not album_key or not album_path:
        return
    with connect() as c:
        if example_file is None and width is None and height is None:
            c.execute('UPDATE albums SET album_path=?, last_scanned=? WHERE album_key=?', (album_path, now(), album_key))
        else:
            c.execute('UPDATE albums SET album_path=?, example_file=COALESCE(?,example_file), width=COALESCE(?,width), height=COALESCE(?,height), last_scanned=? WHERE album_key=?',
                      (album_path, example_file, width, height, now(), album_key))

def _default_status_reason(status):
    return {
        'candidate_found': 'artwork option waiting for review',
        'needs_review': 'embedded artwork needs a better cover',
        'missing_artwork': 'embedded artwork missing',
        'incompatible_artwork': 'embedded or folder artwork needs conversion/save',
        'not_square_artwork': 'embedded artwork is not square',
        'no_candidate': 'no suitable artwork options found',
        'approved': 'approved artwork embedded',
        'reviewed_skipped': 'skipped by user',
        'already_good': 'marked Good',
        'ignored': 'ignored by user',
        'pending': 'pending scan',
    }.get(status or '', (status or '').replace('_', ' ') or 'status updated')


def set_album_status(album_key, status, notes=None, reason=None):
    if isinstance(notes, (dict, list)):
        notes = json.dumps(notes)
    if notes is None:
        # Keep every status write explainable.  Specific action paths can update
        # the reason immediately afterwards with more detail; this default
        # prevents blank/old reasons from surviving simple transitions.
        with connect() as c:
            row = c.execute('SELECT notes FROM albums WHERE album_key=?', (album_key,)).fetchone()
            merged = {}
            if row and row['notes']:
                try:
                    parsed = json.loads(row['notes'])
                    if isinstance(parsed, dict):
                        merged.update(parsed)
                except Exception:
                    pass
            merged['state_evaluation'] = {'status': status or '', 'reason': reason or _default_status_reason(status)}
            c.execute('UPDATE albums SET status=?, notes=?, last_scanned=? WHERE album_key=?',
                      (status, json.dumps(merged), now(), album_key))
    else:
        with connect() as c:
            c.execute('UPDATE albums SET status=?, notes=COALESCE(?,notes), last_scanned=? WHERE album_key=?',
                      (status, notes, now(), album_key))


def add_candidate(album_key, candidate):
    with connect() as c:
        if candidate.get('source_url'):
            existing = c.execute('''SELECT id FROM candidates
                                    WHERE album_key=? AND source=? AND source_url=?''',
                                 (album_key, candidate.get('source'), candidate.get('source_url'))).fetchone()
            if existing:
                return existing['id']
        existing = c.execute('SELECT id FROM candidates WHERE album_key=? AND source=? AND image_path=?',
                             (album_key, candidate.get('source'), candidate.get('image_path'))).fetchone()
        if existing:
            return existing['id']
        source_meta = candidate.get('source_meta') or {}
        if not isinstance(source_meta, str):
            source_meta = json.dumps(source_meta)
        state = candidate.get('candidate_state') or 'available'
        state_reason = candidate.get('state_reason') or 'downloaded and ready for review'
        state_now = now()
        c.execute('''INSERT INTO candidates(album_key,source,image_path,width,height,source_url,source_detail,release_title,release_mbid,source_meta,warnings,score,score_summary,candidate_state,state_reason,state_updated_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (album_key, candidate.get('source'), candidate.get('image_path'), candidate.get('width'), candidate.get('height'),
                   candidate.get('source_url'), candidate.get('source_detail') or '', candidate.get('release_title'), candidate.get('release_mbid'), source_meta, json.dumps(candidate.get('warnings', [])),
                   int(candidate.get('score') or 0), candidate.get('score_summary') or '', state, state_reason, state_now, state_now))
        return c.execute('SELECT last_insert_rowid()').fetchone()[0]


def _candidate_lifecycle_for_flags(approved=None, rejected=None, default_state=None, reason=None):
    state = default_state
    state_reason = reason or ''
    if approved is True:
        state = 'approved'
        state_reason = state_reason or 'approved by user'
    elif rejected is True:
        # Rows rejected because another candidate was approved are not user
        # rejects; keep the lifecycle metadata honest so repair/debug views can
        # distinguish superseded options from images the user disliked.
        if 'superseded' in (state_reason or '').lower():
            state = 'superseded'
            state_reason = state_reason or 'superseded by approved artwork'
        else:
            state = 'rejected'
            state_reason = state_reason or 'rejected by user'
    elif approved is False and rejected is False:
        state = 'available'
        state_reason = state_reason or 'available for review'
    return state, state_reason


def mark_candidate(candidate_id, approved=None, rejected=None, state_reason=None):
    if candidate_id is None:
        return
    sets = []
    vals = []
    if approved is not None:
        sets.append('approved=?')
        vals.append(1 if approved else 0)
    if rejected is not None:
        sets.append('rejected=?')
        vals.append(1 if rejected else 0)
    state, reason = _candidate_lifecycle_for_flags(approved=approved, rejected=rejected, reason=state_reason)
    if state:
        sets.append('candidate_state=?')
        vals.append(state)
        sets.append('state_reason=?')
        vals.append(reason)
        sets.append('state_updated_at=?')
        vals.append(now())
    if not sets:
        return
    vals.append(candidate_id)
    with connect() as c:
        c.execute(f'UPDATE candidates SET {", ".join(sets)} WHERE id=?', vals)


def set_candidate_state(candidate_id, state, reason=''):
    if candidate_id is None or not state:
        return
    with connect() as c:
        c.execute('UPDATE candidates SET candidate_state=?, state_reason=?, state_updated_at=? WHERE id=?',
                  (state, reason or '', now(), candidate_id))


def mark_album_candidates(album_key, approved=None, rejected=None, except_candidate_id=None, state_reason=None):
    sets = []
    vals = []
    if approved is not None:
        sets.append('approved=?')
        vals.append(1 if approved else 0)
    if rejected is not None:
        sets.append('rejected=?')
        vals.append(1 if rejected else 0)
    state, reason = _candidate_lifecycle_for_flags(approved=approved, rejected=rejected, reason=state_reason)
    if state:
        sets.append('candidate_state=?')
        vals.append(state)
        sets.append('state_reason=?')
        vals.append(reason)
        sets.append('state_updated_at=?')
        vals.append(now())
    if not sets:
        return
    q = f'UPDATE candidates SET {", ".join(sets)} WHERE album_key=?'
    vals.append(album_key)
    if except_candidate_id is not None:
        q += ' AND id<>?'
        vals.append(except_candidate_id)
    with connect() as c:
        c.execute(q, vals)


def active_candidate_count(album_key):
    if not album_key:
        return 0
    with connect() as c:
        row = c.execute('SELECT COUNT(*) AS n FROM candidates WHERE album_key=? AND approved=0 AND rejected=0', (album_key,)).fetchone()
        return int(row['n'] if row else 0)


def active_candidate_counts(album_keys=None):
    """Return {album_key: active_candidate_count} with one database query.

    Queue consistency checks can touch hundreds or thousands of albums. Calling
    active_candidate_count() once per album opens SQLite repeatedly; this bulk
    helper keeps repairs/checks fast and reduces UI stalls.
    """
    keys = [k for k in (album_keys or []) if k]
    with connect() as c:
        if keys:
            placeholders = ','.join('?' for _ in keys)
            rows = c.execute(
                f'''SELECT album_key, COUNT(*) AS n FROM candidates
                    WHERE approved=0 AND rejected=0 AND album_key IN ({placeholders})
                    GROUP BY album_key''',
                keys,
            ).fetchall()
        else:
            rows = c.execute('''SELECT album_key, COUNT(*) AS n FROM candidates
                                WHERE approved=0 AND rejected=0
                                GROUP BY album_key''').fetchall()
    counts = {str(r['album_key']): int(r['n'] or 0) for r in rows}
    for key in keys:
        counts.setdefault(str(key), 0)
    return counts


def evaluate_and_set_album_state(album_key, *, candidate_count=None, target_size=None, preserve_user_terminal=True, settings=None):
    """Re-evaluate and persist one album's queue status/reason.

    This is the single database-side doorway for action-completion paths that
    need to turn facts (embedded size, candidates, conversion notes, folder-cover
    requirements) into the stored queue status.
    """
    if not album_key:
        return {'status': '', 'reason': ''}
    from .state import evaluate_album_record, normalise_notes, status_reason_note
    album = get_album(album_key)
    if not album:
        return {'status': '', 'reason': ''}
    if candidate_count is None:
        candidate_count = active_candidate_count(album_key)
    status, reason = evaluate_album_record(
        album,
        candidate_count=candidate_count,
        target_size=target_size,
        preserve_user_terminal=preserve_user_terminal,
        settings=settings,
    )
    notes = normalise_notes(album.get('notes_json') or album.get('notes'))
    notes.update(status_reason_note(status, reason))
    with connect() as c:
        c.execute('UPDATE albums SET status=?, notes=?, last_scanned=? WHERE album_key=?',
                  (status, json.dumps(notes), now(), album_key))
    return {'status': status, 'reason': reason}


def delete_candidates_for_album(album_key):
    """Remove all saved candidate rows for one album.

    Used when a handled/finalized album is reopened for a fresh search.
    File cleanup is handled by the UI before/after approval; this function only
    removes stale database rows so provider source URLs can be saved again.
    """
    if not album_key:
        return 0
    with connect() as c:
        cur = c.execute('DELETE FROM candidates WHERE album_key=?', (album_key,))
        return int(cur.rowcount or 0)


def delete_candidate_ids(candidate_ids):
    """Delete specific candidate database rows and return the number removed.

    This is used by maintenance/repair actions when a candidate image file has
    already been cleaned from temporary storage but its database row remains.
    """
    ids = [int(x) for x in (candidate_ids or []) if x is not None]
    if not ids:
        return 0
    placeholders = ','.join('?' for _ in ids)
    with connect() as c:
        cur = c.execute(f'DELETE FROM candidates WHERE id IN ({placeholders})', ids)
        return int(cur.rowcount or 0)


def all_candidate_file_rows(include_rejected=True):
    """Return candidate ids/paths for stale-file maintenance."""
    q = 'SELECT id, album_key, image_path, rejected, approved FROM candidates'
    if not include_rejected:
        q += ' WHERE approved=0 AND rejected=0'
    with connect() as c:
        return [dict(r) for r in c.execute(q)]

def add_history(album_key, action, payload):
    with connect() as c:
        c.execute('INSERT INTO history(album_key,action,payload,created_at) VALUES(?,?,?,?)',
                  (album_key, action, json.dumps(payload), now()))


def last_history(action=None):
    with connect() as c:
        q = 'SELECT * FROM history' + (' WHERE action=?' if action else '') + ' ORDER BY id DESC LIMIT 1'
        row = c.execute(q, (action,) if action else ()).fetchone()
        return dict(row) if row else None


def history_rows(action=None, limit=500):
    with connect() as c:
        q = 'SELECT * FROM history' + (' WHERE action=?' if action else '') + ' ORDER BY id DESC LIMIT ?'
        args = (action, int(limit)) if action else (int(limit),)
        return [dict(r) for r in c.execute(q, args)]


def get_history(history_id):
    with connect() as c:
        row = c.execute('SELECT * FROM history WHERE id=?', (history_id,)).fetchone()
        return dict(row) if row else None


def start_scan(library_root, total_albums=0):
    with connect() as c:
        c.execute('''INSERT INTO scan_state(id,library_root,is_running,stop_requested,processed_albums,total_albums,updated_at)
        VALUES(1,?,1,0,0,?,?) ON CONFLICT(id) DO UPDATE SET library_root=excluded.library_root,
        is_running=1, stop_requested=0, processed_albums=0, total_albums=excluded.total_albums, updated_at=excluded.updated_at''',
                  (library_root, int(total_albums or 0), now()))


def update_scan_progress(processed_albums, total_albums=None):
    with connect() as c:
        if total_albums is None:
            c.execute('UPDATE scan_state SET processed_albums=?, updated_at=? WHERE id=1', (processed_albums, now()))
        else:
            c.execute('UPDATE scan_state SET processed_albums=?, total_albums=?, updated_at=? WHERE id=1',
                      (processed_albums, int(total_albums or 0), now()))


def finish_scan(stopped=False):
    with connect() as c:
        c.execute('UPDATE scan_state SET is_running=0, stop_requested=?, updated_at=? WHERE id=1', (1 if stopped else 0, now()))


def get_scan_state():
    with connect() as c:
        row = c.execute('SELECT * FROM scan_state WHERE id=1').fetchone()
        return dict(row) if row else None


def _decode_candidate_row(r):
    d = dict(r)
    d['candidate_id'] = d['id']
    try:
        d['warnings'] = json.loads(d.get('warnings') or '[]')
    except Exception:
        d['warnings'] = []
    d['score'] = int(d.get('score') or 0)
    d['score_summary'] = d.get('score_summary') or ''
    d['source_detail'] = d.get('source_detail') or ''
    d['candidate_state'] = d.get('candidate_state') or ('approved' if d.get('approved') else ('rejected' if d.get('rejected') else 'available'))
    d['state_reason'] = d.get('state_reason') or ''
    d['state_updated_at'] = d.get('state_updated_at') or ''
    try:
        d['source_meta_json'] = json.loads(d.get('source_meta') or '{}')
    except Exception:
        d['source_meta_json'] = {}
    return d


def load_candidates(include_rejected=False):
    q = '''SELECT c.*, a.artist, a.album, a.album_path AS album_folder, a.album_key, a.search_artist, a.search_album, a.year, a.identity_confidence
           FROM candidates c JOIN albums a ON a.album_key=c.album_key
           WHERE c.approved=0 AND a.status NOT IN ('approved','reviewed_skipped','already_good','ignored')'''
    if not include_rejected:
        q += ' AND c.rejected=0'
    q += ' ORDER BY a.artist COLLATE NOCASE, a.album COLLATE NOCASE, c.id ASC'
    with connect() as c:
        return [_decode_candidate_row(r) for r in c.execute(q)]


def load_candidates_for_album(album_key, include_rejected=False):
    q = '''SELECT c.*, a.artist, a.album, a.album_path AS album_folder, a.album_key, a.search_artist, a.search_album, a.year, a.identity_confidence
           FROM candidates c JOIN albums a ON a.album_key=c.album_key
           WHERE c.approved=0 AND c.album_key=?'''
    if not include_rejected:
        q += ' AND c.rejected=0'
    q += ' ORDER BY COALESCE(c.score,0) DESC, c.id ASC'
    with connect() as c:
        return [_decode_candidate_row(r) for r in c.execute(q, (album_key,))]


def load_albums(actionable_only=True):
    with connect() as c:
        rows = []
        q = '''SELECT a.*, COUNT(CASE WHEN c.approved=0 AND c.rejected=0 THEN 1 END) AS candidate_count
               FROM albums a LEFT JOIN candidates c ON c.album_key=a.album_key'''
        if actionable_only:
            q += " WHERE a.status NOT IN ('already_good','approved','reviewed_skipped','ignored')"
        q += ''' GROUP BY a.album_key
                 ORDER BY CASE a.status
                   WHEN 'candidate_found' THEN 0
                   WHEN 'missing_artwork' THEN 1
                   WHEN 'no_candidate' THEN 2
                   WHEN 'needs_review' THEN 3
                   WHEN 'not_square_artwork' THEN 4
                   WHEN 'incompatible_artwork' THEN 5
                   WHEN 'already_good' THEN 6
                   WHEN 'approved' THEN 7
                   ELSE 8 END,
                 a.artist COLLATE NOCASE, a.album COLLATE NOCASE'''
        for r in c.execute(q):
            rows.append(_decode_album(r))
        return rows


def album_counts():
    with connect() as c:
        return {r['status']: r['n'] for r in c.execute('SELECT status, COUNT(*) n FROM albums GROUP BY status')}


def active_album_count():
    with connect() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM albums WHERE status NOT IN ('already_good','approved','reviewed_skipped','ignored')").fetchone()
        return int(row['n'] if row else 0)


def clear_queue():
    with connect() as c:
        c.execute('DELETE FROM candidates')
        c.execute('DELETE FROM albums')
        c.execute('DELETE FROM scan_state')
