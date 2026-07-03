import os, shutil, time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import quote
from pathlib import Path
from . import database as db
from .scanner import embedded_artwork
from .providers.musicbrainz import MusicBrainzProvider
from .providers.discogs import DiscogsProvider
from .providers.deezer import DeezerProvider
from .providers.itunes import ITunesProvider
from .providers.fanarttv import FanartTVProvider
from .config import IMPORT_DIR, load_settings, get_preferred_artwork_size, get_max_candidates_per_album, MUSIC_EXTENSIONS
from .state import status_reason_note
from .utils import (
    image_dimensions,
    quality_warnings,
    sanitize_filename,
    clean_album_name,
    score_artwork,
    image_perceptual_hash,
    image_perceptual_hash_from_bytes,
    hamming_distance,
)

PROVIDER_CLASSES = {
    'musicbrainz': MusicBrainzProvider,
    'deezer': DeezerProvider,
    'itunes': ITunesProvider,
    'discogs': DiscogsProvider,
    'fanarttv': FanartTVProvider,
}


def _set_search_status(album_key, status, reason=''):
    """Set search workflow status with an explicit shared state reason."""
    # Let set_album_status merge the state_evaluation note in one write.  Older
    # code wrote the custom reason first and then immediately overwrote it with
    # the generic default reason, which made search/no-option outcomes harder to
    # understand in the details pane.
    db.set_album_status(album_key, status, reason=reason or status.replace('_', ' '))

# Small in-process caches used only while the app is running. These avoid
# repeatedly reopening the same candidate images or music files for perceptual
# hashes during Search More / duplicate checks.
_SIG_CACHE = {}
_CURRENT_ART_SIG_CACHE = {}


def _provider_enabled(settings, key, include_fallbacks=True):
    if key == 'musicbrainz':
        return bool(settings.get('musicbrainz_enabled', True))
    if not include_fallbacks:
        return False
    if key == 'deezer':
        return bool(settings.get('deezer_enabled', True))
    if key == 'itunes':
        return bool(settings.get('itunes_enabled', True))
    if key == 'discogs':
        return bool(settings.get('discogs_enabled', True))
    if key == 'fanarttv':
        return bool(settings.get('fanarttv_enabled', False))
    return False


def enabled_providers(include_fallbacks=True):
    settings = load_settings()
    requested = settings.get('provider_order') or ['deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv']
    order = []
    for key in requested:
        if key in PROVIDER_CLASSES and key not in order:
            order.append(key)
    for fallback in ('deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv'):
        if fallback not in order:
            order.append(fallback)
    providers = []
    for key in order:
        if _provider_enabled(settings, key, include_fallbacks=include_fallbacks):
            providers.append(PROVIDER_CLASSES[key]())
    return providers


def _candidate_signature(candidate):
    path = candidate.get('image_path')
    if not path or not os.path.exists(path):
        return None
    try:
        st = os.stat(path)
        cache_key = (path, int(st.st_mtime), int(st.st_size))
        cached = _SIG_CACHE.get(cache_key)
        if cached:
            return dict(cached)
        sig = {
            'source_url': (candidate.get('source_url') or '').strip(),
            'width': int(candidate.get('width') or 0),
            'height': int(candidate.get('height') or 0),
            'filesize': int(st.st_size),
            'phash': image_perceptual_hash(path),
        }
        _SIG_CACHE[cache_key] = sig
        if len(_SIG_CACHE) > 2000:
            _SIG_CACHE.clear()
        return dict(sig)
    except Exception:
        return None


def _is_duplicate_signature(sig, known):
    if not sig:
        return False
    for other in known:
        if not other:
            continue
        if sig.get('source_url') and other.get('source_url') and sig['source_url'] == other['source_url']:
            return True
        if sig.get('width') == other.get('width') and sig.get('height') == other.get('height') and sig.get('filesize') == other.get('filesize'):
            return True
        a = sig.get('phash')
        b = other.get('phash')
        if a and b and hamming_distance(a, b) <= 4:
            return True
    return False





def _first_music_file(album_path):
    try:
        for root, _, files in os.walk(album_path or ''):
            for fn in files:
                if fn.lower().endswith(MUSIC_EXTENSIONS):
                    return os.path.join(root, fn)
    except Exception:
        return None
    return None


def _current_art_signature(info):
    try:
        album_path = info.get('album_path') or info.get('album_folder') or ''
        example = info.get('example_file') or ''
        fp = os.path.join(album_path, example) if example else _first_music_file(album_path)
        if not fp or not os.path.exists(fp):
            return None
        st = os.stat(fp)
        cache_key = (info.get('album_key') or album_path, fp, int(st.st_mtime), int(st.st_size))
        cached = _CURRENT_ART_SIG_CACHE.get(cache_key)
        if cached:
            return dict(cached)
        arts = embedded_artwork(fp)
        if not arts:
            return None
        art = arts[0]
        sig = {
            'width': int(art.get('width') or 0),
            'height': int(art.get('height') or 0),
            'phash': image_perceptual_hash_from_bytes(art.get('bytes') or b''),
        }
        _CURRENT_ART_SIG_CACHE[cache_key] = sig
        if len(_CURRENT_ART_SIG_CACHE) > 1000:
            _CURRENT_ART_SIG_CACHE.clear()
        return dict(sig)
    except Exception:
        return None


def _looks_like_current(sig, current_sig):
    if not sig or not current_sig:
        return False
    return bool(sig.get('phash') and current_sig.get('phash') and hamming_distance(sig.get('phash'), current_sig.get('phash')) <= 4)


def _album_is_finalized(album_key):
    try:
        album = db.get_album(album_key) or {}
        return album.get('status') in ('approved', 'reviewed_skipped', 'already_good', 'ignored')
    except Exception:
        return False

def _existing_signatures(album_key):
    out = []
    for cand in db.load_candidates_for_album(album_key, include_rejected=False):
        sig = _candidate_signature(cand)
        if sig:
            out.append(sig)
    return out



def _summarize_provider_stats(stats):
    lines = []
    for name, st in stats.items():
        parts = []
        if st.get('saved'):
            parts.append(f"{st['saved']} saved")
        if st.get('duplicates'):
            parts.append(f"{st['duplicates']} duplicate skipped")
        if st.get('same_current'):
            parts.append(f"{st['same_current']} same as current")
        if st.get('below_or_rejected'):
            parts.append(f"{st['below_or_rejected']} unsuitable/skipped")
        if st.get('errors'):
            parts.append('error')
        if not parts:
            parts.append('searched, no saved options')
        lines.append(f"{name}: " + ', '.join(parts))
    return lines



def _provider_result_limit(target_total_per_album, existing_count):
    """How many candidates each provider may return during a parallel search.

    Providers are searched in parallel for speed, so each provider gets the same
    remaining album-level limit. The final save step still respects the album
    total limit after dedupe/scoring. This avoids stopping just because a fast
    provider found something first while preserving the user's configured cap.
    """
    try:
        return max(1, int(target_total_per_album) - int(existing_count))
    except Exception:
        return 5


def _fetch_provider_candidates(provider, info, max_candidates, log=None, stop_event=None):
    """Fetch candidates for one provider. Intended for a background worker.

    Candidate rows are not written to the database here; the caller performs all
    dedupe and DB writes on the search worker thread after provider futures
    return.
    """
    try:
        try:
            cands = provider.get_candidates(info, max_candidates=max_candidates, log=log, stop_event=stop_event)
        except TypeError:
            cands = provider.get_candidates(info, max_candidates=max_candidates, log=log)
        return provider.name, list(cands or []), None
    except Exception as exc:
        return provider.name, [], exc

def build_candidates(album_infos, max_per_album=None, include_fallbacks=True, log=None, stop_event=None, on_candidate=None, status=None):
    settings = load_settings()
    target_total_per_album = int(max_per_album or get_max_candidates_per_album(settings))
    providers = enabled_providers(include_fallbacks)
    queue = []
    for n, info in enumerate(album_infos, 1):
        if stop_event and stop_event.is_set():
            break
        sa = info.get('search_artist') or info.get('artist', '')
        salb = info.get('search_album') or info.get('album', '')
        yr = info.get('year') or ''
        conf = info.get('identity_confidence') or ''
        album_label = f'{info.get("artist", "")} - {info.get("album", "")}'
        if log:
            log(f'[{n}/{len(album_infos)}] Finding artwork options: {album_label}')
            log(f'  Search identity: artist="{sa}" album="{salb}" year="{yr}" confidence={conf or "unknown"}')
        if status:
            status(f'Searching {album_label} ({n}/{len(album_infos)})…')
        found = []
        provider_stats = {}
        known_sigs = _existing_signatures(info['album_key'])
        current_sig = _current_art_signature(info)
        existing_count = len(known_sigs)
        if existing_count >= target_total_per_album:
            if not _album_is_finalized(info['album_key']):
                _set_search_status(info['album_key'], 'candidate_found', f'{existing_count + len(found)} artwork option(s) available')
            if log:
                log(f'  Already have {existing_count} saved option(s); target total is {target_total_per_album}. Use Search More to fetch beyond that.')
            if status:
                status(f'{album_label} already has {existing_count} saved option(s).')
            continue
        # Search enabled providers in parallel.  This keeps the review flow
        # manual and thorough (we do not stop just because one good-looking
        # match appears), but avoids making the user wait for Deezer → Apple →
        # MusicBrainz → Discogs sequentially.  Results are still deduped and
        # saved on this worker thread after each provider finishes.
        provider_limit = _provider_result_limit(target_total_per_album, existing_count)
        provider_stats = {p.name: {'saved': 0, 'duplicates': 0, 'same_current': 0, 'below_or_rejected': 0, 'errors': 0} for p in providers}
        pending_names = [p.name for p in providers]
        if pending_names and status:
            status(f'Searching {", ".join(pending_names)} for {album_label}…')
        max_workers = max(1, min(len(providers), int(settings.get('parallel_provider_workers', 4) or 4)))
        futures = []
        executor = None
        try:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            for provider in providers:
                if stop_event and stop_event.is_set():
                    break
                futures.append(executor.submit(_fetch_provider_candidates, provider, info, provider_limit, log, stop_event))
            pending_futures = set(futures)
            while pending_futures:
                if stop_event and stop_event.is_set():
                    break
                done, pending_futures = wait(pending_futures, timeout=0.10, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for fut in done:
                    if stop_event and stop_event.is_set():
                        break
                    provider_name = 'Provider'
                    cands = []
                    exc = None
                    try:
                        provider_name, cands, exc = fut.result()
                    except Exception as e:
                        exc = e
                    provider_stats.setdefault(provider_name, {'saved': 0, 'duplicates': 0, 'same_current': 0, 'below_or_rejected': 0, 'errors': 0})
                    if provider_name in pending_names:
                        pending_names.remove(provider_name)
                    if exc is not None:
                        if log:
                            log(f'  {provider_name} error: {exc}')
                        provider_stats[provider_name]['errors'] += 1
                        if status and pending_names:
                            status(f'Searching {", ".join(pending_names)} for {album_label}… {existing_count + len(found)} option(s) saved so far.')
                        continue
                    for c in cands:
                        if stop_event and stop_event.is_set():
                            break
                        if _album_is_finalized(info['album_key']):
                            if log:
                                log('    Album was approved/skipped/ignored while search was running; discarding late result.')
                            try:
                                if c.get('image_path') and os.path.exists(c['image_path']):
                                    os.remove(c['image_path'])
                            except Exception:
                                pass
                            continue
                        remaining = target_total_per_album - existing_count - len(found)
                        if remaining <= 0:
                            # Keep the provider search thorough, but respect the
                            # configured saved-option cap.  Remove unused downloaded
                            # artwork so parallel search does not bloat temp storage.
                            provider_stats[provider_name]['below_or_rejected'] += 1
                            try:
                                if c.get('image_path') and os.path.exists(c['image_path']):
                                    os.remove(c['image_path'])
                            except Exception:
                                pass
                            continue
                        c.update({'album_folder': info['album_path']})
                        sig = _candidate_signature(c)
                        if _looks_like_current(sig, current_sig):
                            cur_min = min(int(current_sig.get('width') or 0), int(current_sig.get('height') or 0))
                            cand_min = min(int(c.get('width') or 0), int(c.get('height') or 0))
                            if cand_min <= max(1, int(cur_min * 1.10)):
                                if log:
                                    log(f'    Skipped artwork from {provider_name}: visually same as current embedded art and not meaningfully larger.')
                                provider_stats[provider_name]['same_current'] += 1
                                try:
                                    if c.get('image_path') and os.path.exists(c['image_path']):
                                        os.remove(c['image_path'])
                                except Exception:
                                    pass
                                continue
                            c['score'] = min(100, int(c.get('score') or 0) + 6)
                            meta = c.get('source_meta') or {}
                            if isinstance(meta, dict):
                                meta['same_as_current'] = True
                                meta['current_width'] = current_sig.get('width')
                                meta['current_height'] = current_sig.get('height')
                                c['source_meta'] = meta
                        if _is_duplicate_signature(sig, known_sigs):
                            if log:
                                log(f'    Skipped duplicate-looking artwork from {provider_name}.')
                            provider_stats[provider_name]['duplicates'] += 1
                            try:
                                if c.get('image_path') and os.path.exists(c['image_path']):
                                    os.remove(c['image_path'])
                            except Exception:
                                pass
                            continue
                        c['candidate_id'] = db.add_candidate(info['album_key'], c)
                        found.append(c)
                        provider_stats[provider_name]['saved'] += 1
                        queue.append(c)
                        if sig:
                            known_sigs.append(sig)
                        if status:
                            if pending_names:
                                status(f'Searching {", ".join(pending_names)} for {album_label}… {existing_count + len(found)} option(s) saved so far.')
                            else:
                                status(f'{existing_count + len(found)} artwork option(s) saved for {album_label}. Finishing search…')
                        if on_candidate:
                            on_candidate(c)
                    if status and pending_names:
                        status(f'Searching {", ".join(pending_names)} for {album_label}… {existing_count + len(found)} option(s) saved so far.')
        finally:
            if executor is not None:
                try:
                    stopped_now = bool(stop_event and stop_event.is_set())
                    executor.shutdown(wait=not stopped_now, cancel_futures=stopped_now)
                except TypeError:
                    try:
                        executor.shutdown(wait=not bool(stop_event and stop_event.is_set()))
                    except Exception:
                        pass
                except Exception:
                    pass
        summary_lines = _summarize_provider_stats(provider_stats)
        try:
            db.update_album_notes(info['album_key'], {
                'last_search_summary': summary_lines,
                'last_search_at': db.now(),
                'last_search_saved': len(found),
                'last_search_existing': existing_count,
            })
        except Exception:
            pass
        if stop_event and stop_event.is_set():
            if (found or existing_count) and not _album_is_finalized(info['album_key']):
                _set_search_status(info['album_key'], 'candidate_found', f'{existing_count + len(found)} artwork option(s) available')
            if status:
                status(f'Artwork search stopped after saving {len(found)} option(s) for {album_label}.')
            if log:
                log('  Artwork search stopped by user.')
            break
        if found or existing_count:
            if not _album_is_finalized(info['album_key']):
                _set_search_status(info['album_key'], 'candidate_found', f'{existing_count + len(found)} artwork option(s) available')
            if status:
                status(f'Finished {album_label}: {existing_count + len(found)} artwork option(s) available.')
        else:
            if not _album_is_finalized(info['album_key']):
                _set_search_status(info['album_key'], 'no_candidate', 'no suitable artwork options found')
            if status:
                status(f'No suitable artwork options found for {album_label}.')
            if log:
                log('  No suitable artwork options found.')
    return queue


def manual_import(image_path, artist, album, album_key, album_folder):
    dims = image_dimensions(image_path)
    if not dims:
        raise ValueError('Selected file is not a readable image.')
    ext = Path(image_path).suffix.lower() or '.jpg'
    dest = IMPORT_DIR / f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - manual{ext}'
    i = 1
    while dest.exists():
        dest = IMPORT_DIR / f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - manual_{i}{ext}'
        i += 1
    shutil.copy2(image_path, dest)
    target_size = get_preferred_artwork_size()
    scored = score_artwork(dest, target_size)
    cand = {
        'source': 'Manual Import', 'artist': artist, 'album': album, 'album_key': album_key,
        'album_folder': album_folder, 'image_path': str(dest), 'width': dims[0], 'height': dims[1],
        'source_url': '', 'release_title': 'Manual import', 'release_mbid': '',
        'source_meta': {'source_title': album, 'source_artist': artist, 'source_page': ''},
        'warnings': quality_warnings(dest, target_size),
        'score': scored.get('score', 0), 'score_summary': scored.get('summary', ''), 'score_reasons': scored.get('reasons', [])
    }
    cand['candidate_id'] = db.add_candidate(album_key, cand)
    return cand


def google_images_url(artist, album, target_size=None):
    artist = (artist or '').strip()
    album = clean_album_name(album or '').strip()
    # Guard against common library/container folder names being inferred as
    # artists and ending up at the start of the Google query.
    if artist.lower() in {'music', 'media', 'library', 'albums', 'unknown', 'various'}:
        artist = ''
    if target_size is None:
        target_size = get_preferred_artwork_size()
    try:
        target_size = int(target_size)
    except Exception:
        target_size = 0
    resolution = f'{target_size}x{target_size}' if target_size > 0 else ''
    # Keep manual Google searches focused on the release identity, but include
    # the configured target artwork resolution so external searches are biased
    # toward images the user can approve without extra conversion/rework.
    # udm=2 opens Google Images directly. tbs=isz:l applies the Large image-size filter.
    terms = ' '.join(x for x in (artist, album, resolution) if x).strip()
    query = quote(terms)
    return f'https://www.google.com/search?udm=2&tbs=isz:l&q={query}'
