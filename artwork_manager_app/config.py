from pathlib import Path
import json
import os
import shutil

APP_NAME = 'Artwork Review Manager'
BUILD_VERSION = 'Build 4.88 — Qt storage cleanup'
MIN_ARTWORK_SIZE = 1000
MUSIC_EXTENSIONS = ('.mp3', '.flac', '.m4a', '.mp4')
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
REQUEST_DELAY_SECONDS = 1.2
USER_AGENT = 'ArtworkReviewManager/2.1 (personal-library-tool; +https://discogs.com)'

APP_DIR = Path(__file__).resolve().parent
BUNDLED_DATA_DIR = APP_DIR / 'app_data'

def _default_data_dir():
    override = os.environ.get('ARTWORK_MANAGER_DATA_DIR')
    if override:
        return Path(override).expanduser()
    return Path.home() / 'Library'/ 'Application Support'/ APP_NAME

DATA_DIR = _default_data_dir()
TEMP_DIR = DATA_DIR / 'temporary_candidates'
APPROVED_DIR = DATA_DIR / 'approved_artwork'
BACKUP_DIR = DATA_DIR / 'backups'
IMPORT_DIR = DATA_DIR / 'manual_imports'
REPORT_DIR = DATA_DIR / 'reports'
PREVIEW_CACHE_DIR = DATA_DIR / 'preview_cache'
DB_PATH = DATA_DIR / 'artwork_manager.sqlite3'
SETTINGS_PATH = DATA_DIR / 'settings.json'

def _copy_tree_contents(src, dst):
    for item in src.iterdir():
        if item.name == '__pycache__':
            continue
        target = dst / item.name
        try:
            if item.is_dir():
                if item.name == 'mock_images':
                    continue
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif not target.exists():
                shutil.copy2(item, target)
        except Exception:
            pass

def migrate_bundled_app_data():
    """Move older in-bundle app data to Application Support when possible.

    Early test builds stored the queue database and settings inside the .app
    bundle. App updates are safer when persistent data lives in
    ~/Library/Application Support/Artwork Review Manager instead. This migration
    copies any data bundled with the running app only when the Application
    Support database/settings are not already present.
    """
    try:
        if not BUNDLED_DATA_DIR.exists() or BUNDLED_DATA_DIR.resolve() == DATA_DIR.resolve():
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        has_existing = DB_PATH.exists() or SETTINGS_PATH.exists()
        bundled_has_data = any(BUNDLED_DATA_DIR.iterdir())
        if bundled_has_data and not has_existing:
            _copy_tree_contents(BUNDLED_DATA_DIR, DATA_DIR)
            marker = DATA_DIR / 'migrated_from_bundle.txt'
            if not marker.exists():
                marker.write_text(f'Migrated app data from:\n{BUNDLED_DATA_DIR}\n', encoding='utf-8')
    except Exception:
        pass

migrate_bundled_app_data()

for p in (DATA_DIR, TEMP_DIR, APPROVED_DIR, BACKUP_DIR, IMPORT_DIR, REPORT_DIR, PREVIEW_CACHE_DIR):
    p.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    'discogs_token': '',
    'discogs_enabled': True,
    'musicbrainz_enabled': True,
    'deezer_enabled': True,
    'itunes_enabled': True,
    'fanarttv_enabled': False,
    # Search order for enabled providers. Users can reorder these in Settings.
    'provider_order': ['deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv'],
    # Albums are queued when embedded artwork is missing or smaller than this.
    'scan_min_artwork_size': MIN_ARTWORK_SIZE,
    # Network candidates below this size are ignored/saved as unsuitable.
    'fetch_min_artwork_size': MIN_ARTWORK_SIZE,
    # Desired target used for scoring, warnings, approval resizing/conversion, and badges.
    'preferred_artwork_size': MIN_ARTWORK_SIZE,
    # How strictly target-size checks are applied. Relaxed allows artwork a few
    # pixels short on one edge, as long as the other edge reaches the target.
    'target_size_match_mode': 'Relaxed',
    'max_candidates_per_album': 5,
    'batch_search_count': 5,
    'last_library_path': '',
    # When approving/embedding, also save the chosen artwork as a file inside the album folder.
    'save_approved_artwork_to_album_folder': False,
    # Save full music-file backups before embedding. Off by default because backups can grow very large.
    'backup_before_embedding': False,
    # Ask before embedding candidates marked Weak or with quality warnings.
    'warn_before_low_confidence_embed': True,
    # Convert approved artwork to target-size baseline JPEG before embedding/saving.
    'resize_approved_artwork': True,
    # Verify every supported file after Approve + Embed before marking the album Good.
    'verify_after_embed_before_good': True,
    # Optional slower scan mode: inspect every supported file in each album
    # against the user's preferred target size and baseline-JPEG rules.
    'deep_scan_all_files': False,
    # Per-album scan checks are I/O bound, especially on NAS/SMB shares. A
    # modest thread pool hides network latency without overwhelming the share.
    'scan_worker_threads': 8,
    # Optional NAS/Synology Docker worker. When enabled and a path mapping matches,
    # heavy write/check jobs run on the NAS instead of over SMB/VPN.
    'nas_worker_enabled': False,
    'nas_worker_url': 'http://nas.local:8765',
    'nas_worker_token': '',
    'nas_worker_local_prefix': '',
    'nas_worker_remote_prefix': '/music',
    'nas_worker_timeout': 900,
    # Review layout density: Comfortable or Compact.
    'ui_density': 'Comfortable',
    # Keep normal logs quiet; enable this to show provider/search/file-level details.
    'verbose_log': False,
    'layout': {
        'geometry': '',
        'queue_filter': 'Needs Work',
        'queue_columns': {},
        'qt_main_splitter': [],
        'right_panel_w': 0,
        'queue_search': '',
        # Main work-list position: True puts the queue on the left and review pane on the right.
        'queue_left_layout': True,
    },
}

def load_settings():
    data = DEFAULT_SETTINGS.copy()
    data['layout'] = DEFAULT_SETTINGS['layout'].copy()
    try:
        if SETTINGS_PATH.exists():
            loaded = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                layout = loaded.pop('layout', None)
                data.update(loaded)
                if isinstance(layout, dict):
                    data['layout'].update(layout)
    except Exception:
        pass
    return data

def save_settings(settings):
    data = load_settings()
    incoming = settings or {}
    layout = incoming.get('layout') if isinstance(incoming, dict) else None
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            if key != 'layout':
                data[key] = value
    if isinstance(layout, dict):
        current_layout = data.get('layout') if isinstance(data.get('layout'), dict) else {}
        current_layout.update(layout)
        data['layout'] = current_layout
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
    return data

def _settings_int(settings, key, default, minimum=None, maximum=None):
    try:
        value = int(settings.get(key, default))
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def get_scan_min_artwork_size(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'scan_min_artwork_size', MIN_ARTWORK_SIZE, minimum=1, maximum=10000)


def get_fetch_min_artwork_size(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'fetch_min_artwork_size', MIN_ARTWORK_SIZE, minimum=1, maximum=10000)


def get_preferred_artwork_size(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'preferred_artwork_size', MIN_ARTWORK_SIZE, minimum=1, maximum=10000)



def get_target_size_match_mode(settings=None):
    settings = settings or load_settings()
    raw = str(settings.get('target_size_match_mode', 'Relaxed') or 'Relaxed').strip().lower()
    if raw in {'strict', 'exact'}:
        return 'Strict'
    return 'Relaxed'


def get_target_size_tolerance(settings=None):
    """Return the lower-edge tolerance used by target-size checks.

    Strict means both artwork edges must be at or above the configured target.
    Relaxed accepts a near miss on the shorter edge (currently 98%) when the
    longer edge reaches the target, which avoids rejecting provider images such
    as 1200x1190 or 1400x1388.
    """
    return 1.0 if get_target_size_match_mode(settings) == 'Strict' else 0.98


def get_deep_scan_all_files(settings=None):
    settings = settings or load_settings()
    return bool(settings.get('deep_scan_all_files', False))


def get_scan_worker_threads(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'scan_worker_threads', 8, minimum=1, maximum=32)


def get_nas_worker_enabled(settings=None):
    settings = settings or load_settings()
    return bool(settings.get('nas_worker_enabled', False))


def get_nas_worker_timeout(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'nas_worker_timeout', 900, minimum=5, maximum=7200)

def get_max_candidates_per_album(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'max_candidates_per_album', 5, minimum=1, maximum=25)


def get_batch_search_count(settings=None):
    settings = settings or load_settings()
    return _settings_int(settings, 'batch_search_count', 5, minimum=1, maximum=50)


def get_approved_artwork_target_size(settings=None):
    # Approved artwork conversion uses the user's Preferred / target artwork
    # size setting. This keeps embedding/saved cover files aligned with the
    # same target used during review.
    settings = settings or load_settings()
    return get_preferred_artwork_size(settings)


def get_max_embedded_artwork_size(settings=None):
    # Backwards-compatible name retained for older code paths. It now returns
    # the user's approved-artwork target size rather than a separate maximum.
    return get_approved_artwork_target_size(settings)
