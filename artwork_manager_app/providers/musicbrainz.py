import time, requests, re
from urllib.parse import quote
from ..config import USER_AGENT, TEMP_DIR, REQUEST_DELAY_SECONDS, get_fetch_min_artwork_size, get_preferred_artwork_size
from ..utils import sanitize_filename, clean_album_name, image_dimensions_from_bytes, quality_warnings, normalize_for_match, score_artwork, artwork_meets_target_size


class MusicBrainzProvider:
    name = 'MusicBrainz'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})

    def _get(self, url, timeout=15):
        last = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code in (429, 503):
                    time.sleep(3 * (attempt + 1))
                    last = r
                    continue
                return r
            except Exception as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        if isinstance(last, Exception):
            raise last
        return last

    def _artist_from_mb(self, rel):
        try:
            return ' / '.join(ac.get('artist', {}).get('name', '') for ac in rel.get('artist-credit', []) if ac.get('artist'))
        except Exception:
            return ''

    def _word_set(self, value):
        return {x for x in normalize_for_match(value).split() if x}

    def _dedupe_texts(self, values):
        out = []
        seen = set()
        for value in values:
            value = ' '.join((value or '').split())
            key = normalize_for_match(value)
            if value and key and key not in seen:
                out.append(value)
                seen.add(key)
        return out

    def _artist_variants(self, artist):
        raw = ' '.join((artist or '').split())
        if not raw:
            return []
        values = [raw]
        quote_re = r'[\"“”‘’][^\"“”‘’]+[\"“”‘’]'
        values.append(re.sub(quote_re, ' ', raw))
        values.append(re.sub(r'\([^)]*\)', ' ', raw))
        values.append(re.sub(r'\[[^\]]*\]', ' ', raw))
        words = raw.split()
        norm_words = [normalize_for_match(w) for w in words]
        if len(words) >= 3 and norm_words[0] and norm_words[-1]:
            values.append(f'{words[0]} {words[-1]}')
        return self._dedupe_texts(values)

    def _artist_match_strength(self, found, wanted):
        found_n = normalize_for_match(found)
        if not wanted:
            return 2
        if not found_n:
            return 0
        best = 0
        for variant in self._artist_variants(wanted):
            want_n = normalize_for_match(variant)
            if not want_n:
                continue
            if found_n == want_n:
                best = max(best, 3)
            elif want_n in found_n or found_n in want_n:
                best = max(best, 2)
            else:
                wanted_words = self._word_set(want_n)
                found_words = self._word_set(found_n)
                if wanted_words and wanted_words.issubset(found_words):
                    best = max(best, 2)
        return best

    def _artist_match(self, found, wanted):
        return self._artist_match_strength(found, wanted) > 0

    def _title_match(self, found, wanted):
        found_n = normalize_for_match(clean_album_name(found))
        wanted_n = normalize_for_match(clean_album_name(wanted))
        if not wanted_n:
            return True
        if not found_n:
            return False
        if found_n == wanted_n:
            return True
        # Allow small expansions, e.g. deluxe/version text, but not unrelated common titles.
        if len(wanted_n) >= 6 and (wanted_n in found_n or found_n in wanted_n):
            return True
        return False

    def _year_match(self, found_year, wanted_year):
        if not wanted_year or not found_year:
            return True
        try:
            return abs(int(str(found_year)[:4]) - int(str(wanted_year)[:4])) <= 2
        except Exception:
            return True

    def _release_matches_identity(self, rel, artist, album, year=''):
        title_ok = self._title_match(rel.get('title', ''), album)
        artist_strength = self._artist_match_strength(self._artist_from_mb(rel), artist)
        if not (title_ok and artist_strength):
            return False
        rel_year = (rel.get('date') or '')[:4]
        if self._year_match(rel_year, year):
            return True
        title_exact = normalize_for_match(clean_album_name(rel.get('title', ''))) == normalize_for_match(clean_album_name(album))
        return bool(title_exact and artist_strength >= 2)

    def _score_release(self, rel, artist, album, year=''):
        score = int(rel.get('score') or 0)
        mb_artist = normalize_for_match(self._artist_from_mb(rel))
        mb_title = normalize_for_match(rel.get('title', ''))
        artist_variants = [normalize_for_match(v) for v in self._artist_variants(artist)] or [normalize_for_match(artist)]
        want_artist = artist_variants[0] if artist_variants else normalize_for_match(artist)
        want_title = normalize_for_match(clean_album_name(album))
        if mb_title == want_title:
            score += 80
        elif want_title and want_title in mb_title:
            score += 35
        artist_strength = self._artist_match_strength(self._artist_from_mb(rel), artist)
        if artist_strength >= 3:
            score += 65
        elif artist_strength >= 2:
            score += 42
        elif artist_strength >= 1:
            score += 25
        rel_year = (rel.get('date') or '')[:4]
        if year and rel_year:
            if rel_year == str(year):
                score += 20
            elif rel_year[:3] == str(year)[:3]:
                score += 8
            else:
                score -= 8
        status = (rel.get('status') or '').lower()
        if status == 'official':
            score += 4
        packaging = (rel.get('packaging') or '').lower()
        if 'cardboard' in packaging or 'jewel' in packaging:
            score += 2
        return score

    def search_releases(self, artist, album, year='', limit=20, stop_event=None):
        clean = clean_album_name(album)
        queries = []
        def add_query(q):
            if q and q not in queries:
                queries.append(q)
        artist_variants = self._artist_variants(artist) or ([artist] if artist else [])
        if artist_variants and clean:
            for artist_variant in artist_variants:
                if year:
                    add_query(f'artist:"{artist_variant}" AND release:"{clean}" AND date:{year}')
                add_query(f'artist:"{artist_variant}" AND release:"{clean}"')
        # Only use title-only searches when there is no artist. For common titles like
        # “Killer”, title-only fallback can pull the wrong album, e.g. Alice Cooper.
        if not artist:
            if clean and year:
                add_query(f'release:"{clean}" AND date:{year}')
            if clean:
                add_query(f'release:"{clean}"')
        seen, seen_ids = [], set()
        for q in queries:
            if stop_event and stop_event.is_set():
                break
            url = f'https://musicbrainz.org/ws/2/release/?query={quote(q)}&fmt=json&limit={limit}'
            r = self._get(url)
            if not r or r.status_code != 200:
                continue
            try:
                releases = r.json().get('releases', [])
            except Exception:
                releases = []
            for rel in releases:
                mbid = rel.get('id')
                if not mbid or mbid in seen_ids:
                    continue
                if not self._release_matches_identity(rel, artist, clean, year):
                    continue
                rel['_local_score'] = self._score_release(rel, artist, clean, year)
                seen.append(rel)
                seen_ids.add(mbid)
            time.sleep(REQUEST_DELAY_SECONDS)
        seen.sort(key=lambda r: r.get('_local_score', 0), reverse=True)
        return seen[:limit]

    def fetch_release(self, mbid):
        url = f'https://musicbrainz.org/ws/2/release/{mbid}?inc=artist-credits+recordings&fmt=json'
        r = self._get(url)
        if r and r.status_code == 200:
            try:
                rel = r.json()
                rel['_local_score'] = 9999
                return rel
            except Exception:
                return None
        return None

    def release_label(self, rel):
        title = rel.get('title', '')
        date = rel.get('date') or ''
        country = rel.get('country') or ''
        status = rel.get('status') or ''
        artist = self._artist_from_mb(rel)
        bits = [b for b in (artist, title, date, country, status) if b]
        return ' — '.join(bits)

    def get_candidates_from_release(self, artist, album, album_key, rel, max_candidates=5, log=None, stop_event=None):
        candidates = []
        fetch_min_size = get_fetch_min_artwork_size()
        target_size = get_preferred_artwork_size()
        mbid = rel.get('id')
        title = rel.get('title', '')
        if not mbid:
            return candidates
        if log:
            log(f'  Trying selected MusicBrainz release: {self.release_label(rel)}')
        try:
            r = self.session.get(f'https://coverartarchive.org/release/{mbid}', timeout=15)
        except Exception:
            return candidates
        if r.status_code != 200:
            if log:
                log(f'    Cover Art Archive returned HTTP {r.status_code}')
            return candidates
        try:
            images = r.json().get('images', [])
        except Exception:
            images = []
        front = [i for i in images if i.get('front') is True] or images
        for info in front:
            if stop_event and stop_event.is_set():
                break
            thumbs = info.get('thumbnails', {}) or {}
            url_options = [
                ('Original', info.get('image')),
                ('1200px thumbnail', thumbs.get('1200')),
                ('500px thumbnail', thumbs.get('500')),
                ('Large thumbnail', thumbs.get('large')),
            ]
            urls = []
            seen_urls = set()
            for source_detail, u in url_options:
                if u and u not in seen_urls:
                    urls.append((source_detail, u))
                    seen_urls.add(u)
            for source_detail, url in urls:
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
                        log(f'    Skipped artwork below fetch minimum ({w}x{h}; minimum {fetch_min_size}px)')
                    continue
                ctype = img_r.headers.get('Content-Type', '').lower()
                ext = '.png' if 'png' in ctype else '.webp' if 'webp' in ctype else '.jpg'
                base = f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - {self.name} - option - {len(candidates) + 1}'
                path = TEMP_DIR / f'{base}{ext}'
                i = 1
                while path.exists():
                    path = TEMP_DIR / f'{base}_{i}{ext}'
                    i += 1
                path.write_bytes(img_r.content)
                scored = score_artwork(path, target_size)
                cand = {
                    'source': self.name,
                    'source_detail': source_detail,
                    'artist': artist,
                    'album': album,
                    'album_key': album_key,
                    'image_path': str(path),
                    'width': w,
                    'height': h,
                    'source_url': url,
                    'release_title': title + (f' ({(rel.get("date") or "")[:4]})' if rel.get('date') else ''),
                    'release_mbid': mbid,
                    'source_meta': {
                        'source_artist': self._artist_from_mb(rel),
                        'source_title': title,
                        'source_year': (rel.get('date') or '')[:4],
                        'release_date': rel.get('date') or '',
                        'country': rel.get('country') or '',
                        'status': rel.get('status') or '',
                        'packaging': rel.get('packaging') or '',
                        'track_count': sum(len(m.get('tracks') or []) for m in (rel.get('media') or [])),
                        'source_page': f'https://musicbrainz.org/release/{mbid}' if mbid else '',
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
        if isinstance(album_info, dict):
            artist = album_info.get('search_artist') or album_info.get('artist', '')
            album = album_info.get('search_album') or album_info.get('album', '')
            album_key = album_info.get('album_key')
            year = album_info.get('year') or ''
            direct_mbid = album_info.get('mb_release_id') or ''
        else:
            artist, album, album_key = album_info, '', None
            year = ''
            direct_mbid = ''
        candidates = []
        if direct_mbid:
            rel = self.fetch_release(direct_mbid)
            if rel:
                if log:
                    log(f'  Using MusicBrainz release ID from tags: {direct_mbid}')
                candidates.extend(self.get_candidates_from_release(artist, album, album_key, rel, max_candidates=max_candidates, log=log, stop_event=stop_event))
                if candidates:
                    return candidates
                if log:
                    log(f'  Tagged MusicBrainz release had no artwork at or above the fetch minimum ({get_fetch_min_artwork_size()}px); falling back to search.')
        releases = self.search_releases(artist, album, year=year, stop_event=stop_event)
        for rel in releases:
            if stop_event and stop_event.is_set():
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                break
            candidates.extend(self.get_candidates_from_release(artist, album, album_key, rel, remaining, log=log, stop_event=stop_event))
        return candidates
