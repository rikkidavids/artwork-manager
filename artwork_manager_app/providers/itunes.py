import re
import time
import requests
from urllib.parse import quote_plus
from ..config import USER_AGENT, TEMP_DIR, REQUEST_DELAY_SECONDS, get_fetch_min_artwork_size, get_preferred_artwork_size
from ..utils import sanitize_filename, clean_album_name, image_dimensions_from_bytes, quality_warnings, normalize_for_match, score_artwork, artwork_meets_target_size


class ITunesProvider:
    name = 'iTunes'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})

    def _get_json(self, url, timeout=12):
        last = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=timeout)
                if r.status_code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    last = r
                    continue
                if r.status_code != 200:
                    return None
                return r.json()
            except Exception as exc:
                last = exc
                time.sleep(1.5 * (attempt + 1))
        return None

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
        if len(wanted_n) >= 6 and (wanted_n in found_n or found_n in wanted_n):
            return True
        wanted_words = self._word_set(wanted_n)
        found_words = self._word_set(found_n)
        return len(wanted_words) >= 2 and wanted_words.issubset(found_words)

    def _year_match(self, found_year, wanted_year):
        if not wanted_year or not found_year:
            return True
        try:
            return abs(int(str(found_year)[:4]) - int(str(wanted_year)[:4])) <= 2
        except Exception:
            return True

    def _result_matches_identity(self, item, artist, album, year=''):
        title_ok = self._title_match(item.get('collectionName', ''), album)
        artist_strength = self._artist_match_strength(item.get('artistName', ''), artist)
        if not (title_ok and artist_strength):
            return False
        found_year = (item.get('releaseDate') or '')[:4]
        if self._year_match(found_year, year):
            return True
        title_exact = normalize_for_match(clean_album_name(item.get('collectionName', ''))) == normalize_for_match(clean_album_name(album))
        return bool(title_exact and artist_strength >= 2)

    def _score_result(self, item, artist, album, year=''):
        score = 0
        found_artist = normalize_for_match(item.get('artistName', ''))
        found_title = normalize_for_match(clean_album_name(item.get('collectionName', '')))
        artist_variants = [normalize_for_match(v) for v in self._artist_variants(artist)] or [normalize_for_match(artist)]
        want_artist = artist_variants[0] if artist_variants else normalize_for_match(artist)
        want_title = normalize_for_match(clean_album_name(album))
        if found_title == want_title:
            score += 85
        elif want_title and want_title in found_title:
            score += 40
        artist_strength = self._artist_match_strength(item.get('artistName', ''), artist)
        if artist_strength >= 3:
            score += 65
        elif artist_strength >= 2:
            score += 42
        elif artist_strength >= 1:
            score += 25
        found_year = (item.get('releaseDate') or '')[:4]
        if year and found_year:
            if found_year == str(year):
                score += 18
            elif found_year[:3] == str(year)[:3]:
                score += 6
            else:
                score -= 8
        if item.get('artworkUrl100'):
            score += 8
        return score

    def search_albums(self, artist, album, year='', limit=20, log=None, stop_event=None):
        clean = clean_album_name(album)
        queries = []
        def add_query(q):
            q = ' '.join((q or '').split())
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        artist_variants = self._artist_variants(artist) or ([artist] if artist else [])
        if clean and artist_variants:
            for artist_variant in artist_variants:
                add_query(f'{artist_variant} {clean}')
        elif clean:
            add_query(clean)
        elif artist_variants:
            for artist_variant in artist_variants:
                add_query(artist_variant)
        results = []
        seen_ids = set()
        for term in queries:
            if stop_event and stop_event.is_set():
                break
            url = f'https://itunes.apple.com/search?term={quote_plus(term)}&media=music&entity=album&limit={int(limit)}'
            if log:
                log(f'  iTunes/Apple API search: {term}')
            data = self._get_json(url)
            if not isinstance(data, dict):
                continue
            for item in data.get('results') or []:
                cid = item.get('collectionId')
                if not cid or cid in seen_ids:
                    continue
                if item.get('wrapperType') not in ('collection', None):
                    continue
                if not self._result_matches_identity(item, artist, album, year=year):
                    continue
                item['_local_score'] = self._score_result(item, artist, album, year=year)
                results.append(item)
                seen_ids.add(cid)
            time.sleep(REQUEST_DELAY_SECONDS)
        results.sort(key=lambda x: x.get('_local_score', 0), reverse=True)
        return results[:limit]

    def release_label(self, item):
        title = item.get('collectionName', '')
        artist = item.get('artistName', '')
        date = (item.get('releaseDate') or '')[:10]
        country = item.get('country') or ''
        genre = item.get('primaryGenreName') or ''
        bits = [b for b in (artist, title, date, country, genre) if b]
        return ' — '.join(bits)

    def _artwork_url_variants(self, url):
        if not url:
            return []
        out = []
        # iTunes artwork URLs usually end in something like 100x100bb.jpg.
        # Replacing that segment asks Apple's CDN for larger square sizes.
        for size in (1400, 1200, 1000, 600):
            u = re.sub(r'\d+x\d+(bb|cc|bf|sr)?\.(jpg|jpeg|png|webp)$', f'{size}x{size}bb.\\2', url)
            if u == url:
                u = re.sub(r'\d+x\d+', f'{size}x{size}', url)
            detail = f'{size}px artwork'
            if u and u not in [x[1] for x in out]:
                out.append((detail, u))
        if url not in [x[1] for x in out]:
            out.append(('Original API artwork', url))
        return out

    def get_candidates_from_release(self, artist, album, album_key, item, max_candidates=5, log=None, stop_event=None):
        candidates = []
        fetch_min_size = get_fetch_min_artwork_size()
        target_size = get_preferred_artwork_size()
        title = item.get('collectionName', '')
        collection_id = item.get('collectionId')
        release_date = item.get('releaseDate') or ''
        if log:
            log(f'  Trying iTunes/Apple album: {self.release_label(item)}')
        for source_detail, url in self._artwork_url_variants(item.get('artworkUrl100')):
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
                    log(f'    Skipped iTunes {source_detail} below fetch minimum ({w}x{h}; minimum {fetch_min_size}px)')
                continue
            ctype = img_r.headers.get('Content-Type', '').lower()
            ext = '.png' if 'png' in ctype else '.webp' if 'webp' in ctype else '.jpg'
            base = f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - iTunes - option - {len(candidates) + 1}'
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
                'release_title': title + (f' ({str(release_date)[:4]})' if release_date else ''),
                'release_mbid': f'itunes:{collection_id}' if collection_id else '',
                'source_meta': {
                    'source_artist': item.get('artistName') or '',
                    'source_title': title,
                    'source_year': str(release_date)[:4] if release_date else '',
                    'release_date': release_date,
                    'country': item.get('country') or '',
                    'format': item.get('collectionType') or '',
                    'track_count': item.get('trackCount') or '',
                    'genre': item.get('primaryGenreName') or '',
                    'source_page': item.get('collectionViewUrl') or '',
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
        else:
            artist, album, album_key, year = album_info, '', None, ''
        results = self.search_albums(artist, album, year=year, limit=12, log=log, stop_event=stop_event)
        candidates = []
        for item in results:
            if stop_event and stop_event.is_set():
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                break
            candidates.extend(self.get_candidates_from_release(artist, album, album_key, item, remaining, log=log, stop_event=stop_event))
            time.sleep(REQUEST_DELAY_SECONDS)
        return candidates
