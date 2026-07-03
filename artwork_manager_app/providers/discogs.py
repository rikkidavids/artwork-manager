import time, requests
from urllib.parse import quote_plus
from ..config import USER_AGENT, TEMP_DIR, REQUEST_DELAY_SECONDS, load_settings, get_fetch_min_artwork_size, get_preferred_artwork_size
from ..utils import sanitize_filename, clean_album_name, image_dimensions_from_bytes, quality_warnings, normalize_for_match, score_artwork, artwork_meets_target_size


class DiscogsProvider:
    name = 'Discogs'

    def __init__(self):
        self.settings = load_settings()
        self.token = self.settings.get('discogs_token', '').strip()
        self.enabled = bool(self.settings.get('discogs_enabled', True))
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT})
        if self.token:
            self.session.headers.update({'Authorization': f'Discogs token={self.token}'})

    def test_connection(self):
        if not self.token:
            return False, 'No Discogs token saved.'
        r = self.session.get('https://api.discogs.com/oauth/identity', timeout=12)
        if r.status_code == 200:
            user = r.json().get('username', 'Discogs user')
            return True, f'Connected as {user}.'
        return False, f'Discogs returned HTTP {r.status_code}: {r.text[:200]}'

    def _word_set(self, value):
        return {x for x in normalize_for_match(value).split() if x}

    def _result_matches_identity(self, res, artist, album, year=''):
        title = res.get('title', '')
        title_norm = normalize_for_match(title)
        want_artist = normalize_for_match(artist)
        want_album = normalize_for_match(clean_album_name(album))
        if want_artist and want_artist not in title_norm:
            words = self._word_set(want_artist)
            if not words or not words.issubset(self._word_set(title_norm)):
                return False
        if want_album:
            if title_norm == want_album:
                pass
            elif len(want_album) >= 6 and want_album in title_norm:
                pass
            else:
                return False
        if year and res.get('year'):
            try:
                if abs(int(str(res.get('year'))[:4]) - int(str(year)[:4])) > 2:
                    return False
            except Exception:
                pass
        return True

    def _score_result(self, res, artist, album, year=''):
        title = res.get('title', '')
        year_res = str(res.get('year') or '')
        want_artist = normalize_for_match(artist)
        want_album = normalize_for_match(clean_album_name(album))
        title_norm = normalize_for_match(title)
        score = 0
        if want_artist and want_artist in title_norm:
            score += 55
        if want_album and want_album in title_norm:
            score += 75
        if year and year_res:
            if year_res == str(year):
                score += 18
            elif year_res[:3] == str(year)[:3]:
                score += 6
        if res.get('cover_image'):
            score += 5
        return score

    def search(self, artist, album, year='', limit=20):
        q = ('https://api.discogs.com/database/search?type=release'
             f'&artist={quote_plus(artist)}&release_title={quote_plus(clean_album_name(album))}&per_page={limit}')
        r = self.session.get(q, timeout=15)
        if r.status_code in (429, 503):
            time.sleep(5)
            r = self.session.get(q, timeout=15)
        if r.status_code != 200:
            return []
        raw_results = r.json().get('results', [])
        results = []
        for res in raw_results:
            if not self._result_matches_identity(res, artist, album, year=year):
                continue
            res['_local_score'] = self._score_result(res, artist, album, year=year)
            results.append(res)
        results.sort(key=lambda x: x.get('_local_score', 0), reverse=True)
        return results

    def release_label(self, res):
        title = res.get('title', '')
        year = str(res.get('year') or '')
        country = res.get('country') or ''
        fmt = ', '.join(res.get('format') or [])
        bits = [b for b in (title, year, country, fmt) if b]
        return ' — '.join(bits)

    def get_candidates_from_release(self, artist, album, album_key, res, max_candidates=5, log=None, stop_event=None):
        candidates = []
        fetch_min_size = get_fetch_min_artwork_size()
        target_size = get_preferred_artwork_size()
        urls = []
        for url in (res.get('cover_image'), res.get('thumb')):
            if url and 'spacer.gif' not in url and url not in urls:
                urls.append(url)
        title = res.get('title', '')
        if log:
            log(f'  Trying selected Discogs release: {self.release_label(res)}')
        for url in urls:
            if stop_event and stop_event.is_set():
                break
            try:
                img_r = self.session.get(url, timeout=20)
            except Exception:
                continue
            if img_r.status_code != 200:
                continue
            dims = image_dimensions_from_bytes(img_r.content)
            if not dims:
                continue
            w, h = dims
            if not artwork_meets_target_size(w, h, fetch_min_size):
                if log:
                    log(f'    Skipped Discogs artwork below fetch minimum ({w}x{h}; minimum {fetch_min_size}px)')
                continue
            ctype = img_r.headers.get('Content-Type', '').lower()
            ext = '.png' if 'png' in ctype else '.webp' if 'webp' in ctype else '.jpg'
            base = f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - Discogs - option - {len(candidates) + 1}'
            path = TEMP_DIR / f'{base}{ext}'
            i = 1
            while path.exists():
                path = TEMP_DIR / f'{base}_{i}{ext}'
                i += 1
            path.write_bytes(img_r.content)
            scored = score_artwork(path, target_size)
            cand = {
                'source': self.name,
                'artist': artist,
                'album': album,
                'album_key': album_key,
                'image_path': str(path),
                'width': w,
                'height': h,
                'source_url': url,
                'release_title': title + (f' ({str(res.get("year"))[:4]})' if res.get('year') else ''),
                'release_mbid': str(res.get('id', '')),
                'source_meta': {
                    'source_artist': title.split(' - ', 1)[0] if ' - ' in title else '',
                    'source_title': title.split(' - ', 1)[1] if ' - ' in title else title,
                    'source_year': str(res.get('year') or '')[:4],
                    'country': res.get('country') or '',
                    'format': ', '.join(res.get('format') or []),
                    'label': ', '.join(res.get('label') or []),
                    'source_page': f'https://www.discogs.com/release/{res.get("id")}' if res.get('id') else '',
                },
                'warnings': quality_warnings(path, target_size),
                'score': scored.get('score', 0),
                'score_summary': scored.get('summary', ''),
                'score_reasons': scored.get('reasons', []),
            }
            candidates.append(cand)
            if len(candidates) >= max_candidates:
                return candidates
        return candidates

    def get_candidates(self, album_info, max_candidates=5, log=None, stop_event=None):
        if not self.enabled:
            return []
        if not self.token:
            if log:
                log('  Discogs skipped: open Settings and paste your Discogs token.')
            return []
        if isinstance(album_info, dict):
            artist = album_info.get('search_artist') or album_info.get('artist', '')
            album = album_info.get('search_album') or album_info.get('album', '')
            album_key = album_info.get('album_key')
            year = album_info.get('year') or ''
        else:
            artist, album, album_key = album_info, '', None
            year = ''
        candidates = []
        try:
            results = self.search(artist, album, year=year, limit=12)
        except Exception as exc:
            if log:
                log(f'  Discogs search failed: {exc}')
            return []
        for res in results:
            if stop_event and stop_event.is_set():
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                break
            candidates.extend(self.get_candidates_from_release(artist, album, album_key, res, remaining, log=log, stop_event=stop_event))
            time.sleep(REQUEST_DELAY_SECONDS)
        return candidates
