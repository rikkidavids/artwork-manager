import time, requests, re, difflib
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from ..config import USER_AGENT, TEMP_DIR, REQUEST_DELAY_SECONDS, get_fetch_min_artwork_size, get_preferred_artwork_size
from ..utils import sanitize_filename, clean_album_name, image_dimensions_from_bytes, quality_warnings, normalize_for_match, score_artwork, artwork_meets_target_size


class DeezerProvider:
    name = 'Deezer'
    STOPWORDS = {'the', 'a', 'an', 'and', 'of', 'in', 'on', 'at', 'to', 'for', 'with', 'vol', 'volume', 'edition', 'deluxe', 'remaster', 'remastered'}

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})

    @staticmethod
    def unwrap_source_url(value):
        """Return useful source URL candidates from pasted text.

        People often paste Google redirect links rather than the final Deezer
        page.  Keep this local and dependency-free: decode url/q/u parameters
        and percent-encoded links, then let the normal ID extractor decide.
        """
        raw = (value or '').strip()
        out = []

        def add(v):
            v = (v or '').strip()
            if v and v not in out:
                out.append(v)
                dec = unquote(v)
                if dec and dec != v and dec not in out:
                    out.append(dec)

        add(raw)
        for candidate in list(out):
            try:
                parsed = urlparse(candidate)
                qs = parse_qs(parsed.query or '')
            except Exception:
                continue
            for key in ('url', 'q', 'u'):
                for val in qs.get(key, []):
                    add(val)
        return out

    @classmethod
    def extract_album_id(cls, value):
        """Extract a Deezer album id from a Deezer URL, Google redirect URL, or id."""
        for candidate in cls.unwrap_source_url(value):
            if re.fullmatch(r'\d{3,}', candidate):
                return candidate
            m = re.search(r'deezer\.com/(?:[a-z]{2}/)?album/(\d+)', candidate, re.I)
            if m:
                return m.group(1)
            m = re.search(r'(?:^|[/?#&])album/(\d+)(?:\D|$)', candidate, re.I)
            if m and 'deezer' in candidate.lower():
                return m.group(1)
        return ''

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

    def _meaningful_words(self, value):
        return {w for w in self._word_set(value) if w not in self.STOPWORDS and len(w) > 1}

    def _album_artist(self, item):
        artist = item.get('artist') or {}
        if isinstance(artist, dict):
            return artist.get('name', '') or ''
        return ''

    def _clean_title_for_variants(self, value):
        value = clean_album_name(value or '')
        value = re.sub(r'\([^)]*\)', ' ', value)
        value = re.sub(r'\[[^\]]*\]', ' ', value)
        return ' '.join(value.split())

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
        """Return safe artist search/match variants.

        Library tags often contain nicknames or aliases inside quotes, e.g.
        Damian “Jr. Gong” Marley.  Deezer commonly stores the same artist as
        Damian Marley, so a strict artist match can reject the correct album
        even though Google/Deezer visibly has the cover.  Keep the original but
        also try conservative variants with quoted/bracketed nicknames removed.
        """
        raw = ' '.join((artist or '').split())
        if not raw:
            return []
        values = [raw]
        quote_re = r'[\"“”‘’][^\"“”‘’]+[\"“”‘’]'
        values.append(re.sub(quote_re, ' ', raw))
        values.append(re.sub(r'\([^)]*\)', ' ', raw))
        values.append(re.sub(r'\[[^\]]*\]', ' ', raw))
        # Some tags lose the quote punctuation during scanning, leaving
        # first-name + nickname + last-name.  When the first and last words are
        # distinctive, that two-word variant is useful and low-risk.
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

    def _title_variants(self, artist, album):
        clean = self._clean_title_for_variants(album)
        artist_clean = self._clean_title_for_variants(artist)
        variants = []
        def add(v):
            v = ' '.join((v or '').split())
            if v and v.lower() not in {x.lower() for x in variants}:
                variants.append(v)
        add(clean)
        # If the album title legitimately starts with the artist name, keep the
        # full title but also try the distinctive remainder as a fallback.
        clean_n = normalize_for_match(clean)
        artist_n = normalize_for_match(artist_clean)
        if artist_n and clean_n.startswith(artist_n + ' '):
            remainder_words = clean_n[len(artist_n):].strip().split()
            if len([w for w in remainder_words if w not in self.STOPWORDS]) >= 2:
                add(' '.join(remainder_words))
        words = clean.split()
        no_articles = [w for w in words if normalize_for_match(w) not in {'the', 'a', 'an'}]
        if len(no_articles) >= 2:
            add(' '.join(no_articles))
        meaningful = [w for w in normalize_for_match(clean).split() if w not in self.STOPWORDS and len(w) > 1]
        if len(meaningful) >= 2:
            add(' '.join(meaningful))
            add(' '.join(meaningful[-4:]))
        return variants

    def _title_match(self, found, wanted):
        found_clean = clean_album_name(found)
        wanted_clean = clean_album_name(wanted)
        found_n = normalize_for_match(found_clean)
        wanted_n = normalize_for_match(wanted_clean)
        if not wanted_n:
            return True
        if not found_n:
            return False
        if found_n == wanted_n:
            return True
        if len(wanted_n) >= 6 and (wanted_n in found_n or found_n in wanted_n):
            return True
        wanted_words = self._meaningful_words(wanted_n)
        found_words = self._meaningful_words(found_n)
        if not wanted_words or not found_words:
            return False
        # Handle articles/punctuation and cases like "I Am Kloot Play The Moolah
        # Rouge" vs "I Am Kloot Play Moolah Rouge".
        if wanted_words.issubset(found_words) or found_words.issubset(wanted_words):
            return True
        overlap = len(wanted_words & found_words)
        directional = overlap / max(1, min(len(wanted_words), len(found_words)))
        jaccard = overlap / max(1, len(wanted_words | found_words))
        if overlap >= 2 and (directional >= 0.72 or jaccard >= 0.58):
            return True
        # Last resort for one-letter/typo differences in distinctive titles.
        if difflib.SequenceMatcher(None, found_n, wanted_n).ratio() >= 0.82:
            return True
        return False

    def _artist_match(self, found, wanted):
        return self._artist_match_strength(found, wanted) > 0

    def _year_match(self, found_year, wanted_year):
        if not wanted_year or not found_year:
            return True
        try:
            return abs(int(str(found_year)[:4]) - int(str(wanted_year)[:4])) <= 2
        except Exception:
            return True

    def _result_matches_identity(self, item, artist, album, year=''):
        title_ok = self._title_match(item.get('title', ''), album)
        artist_strength = self._artist_match_strength(self._album_artist(item), artist)
        if not (title_ok and artist_strength):
            return False
        found_year = (item.get('release_date') or item.get('release_year') or '')[:4]
        if self._year_match(found_year, year):
            return True
        # Treat the library year as useful evidence, not a hard failure, when
        # the artist/title match is strong.  Streaming services often expose a
        # digital/reissue date that differs from the original tag year.
        title_exact = normalize_for_match(clean_album_name(item.get('title', ''))) == normalize_for_match(clean_album_name(album))
        return bool(title_exact and artist_strength >= 2)

    def _score_result(self, item, artist, album, year=''):
        score = 0
        found_artist = normalize_for_match(self._album_artist(item))
        found_title = normalize_for_match(clean_album_name(item.get('title', '')))
        artist_variants = [normalize_for_match(v) for v in self._artist_variants(artist)] or [normalize_for_match(artist)]
        want_artist = artist_variants[0] if artist_variants else normalize_for_match(artist)
        want_title = normalize_for_match(clean_album_name(album))
        if found_title == want_title:
            score += 85
        elif self._title_match(found_title, want_title):
            score += 60
        elif want_title and want_title in found_title:
            score += 40
        artist_strength = self._artist_match_strength(self._album_artist(item), artist)
        if artist_strength >= 3:
            score += 65
        elif artist_strength >= 2:
            score += 42
        elif artist_strength >= 1:
            score += 25
        found_year = (item.get('release_date') or item.get('release_year') or '')[:4]
        if year and found_year:
            if found_year == str(year):
                score += 18
            elif found_year[:3] == str(year)[:3]:
                score += 6
            else:
                score -= 8
        if item.get('cover_xl'):
            score += 8
        elif item.get('cover_big'):
            score += 4
        return score

    def search_albums(self, artist, album, year='', limit=20, log=None, stop_event=None):
        clean = clean_album_name(album)
        queries = []
        def add_query(q):
            q = ' '.join((q or '').split())
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
        if artist and clean:
            artist_variants = self._artist_variants(artist) or [artist]
            title_variants = self._title_variants(artist, clean)
            for artist_variant in artist_variants:
                add_query(f'artist:"{artist_variant}" album:"{clean}"')
                add_query(f'{artist_variant} {clean}')
                for variant in title_variants:
                    add_query(f'{artist_variant} {variant}')
            # Broad but still verifiable: Deezer can find the album by title-only;
            # we then require the returned album artist to match before accepting it.
            for variant in title_variants:
                add_query(variant)
        elif clean:
            for variant in self._title_variants('', clean):
                add_query(variant)
        elif artist:
            add_query(artist)

        results = []
        seen_ids = set()
        for q in queries:
            if stop_event and stop_event.is_set():
                break
            url = f'https://api.deezer.com/search/album?q={quote_plus(q)}&limit={int(limit)}'
            if log:
                log(f'  Deezer API search: {q}')
            data = self._get_json(url, timeout=6 if stop_event and stop_event.is_set() else 12)
            if not isinstance(data, dict):
                continue
            for item in data.get('data') or []:
                album_id = item.get('id')
                if not album_id or album_id in seen_ids:
                    continue
                # Fetch album detail before matching; detail often contains better
                # release_date, track count and canonical title/artist fields.
                detail = self.fetch_album(album_id) or {}
                merged = dict(item)
                merged.update({k: v for k, v in detail.items() if v not in (None, '', [])})
                if not self._result_matches_identity(merged, artist, clean, year=year):
                    continue
                merged['_local_score'] = self._score_result(merged, artist, clean, year=year)
                results.append(merged)
                seen_ids.add(album_id)
            time.sleep(REQUEST_DELAY_SECONDS)
        results.sort(key=lambda x: x.get('_local_score', 0), reverse=True)
        if log:
            log(f'  Deezer API accepted {len(results)} album match(es).')
        return results[:limit]

    def fetch_album(self, album_id):
        if not album_id:
            return None
        return self._get_json(f'https://api.deezer.com/album/{album_id}')

    def release_label(self, item):
        artist = self._album_artist(item)
        title = item.get('title', '')
        date = item.get('release_date') or ''
        record_type = item.get('record_type') or ''
        bits = [b for b in (artist, title, date, record_type) if b]
        return ' — '.join(bits)

    def _derive_sized_cover_url(self, item, size):
        """Build a Deezer CDN cover URL at a requested square size when possible."""
        try:
            size = int(size)
        except Exception:
            size = 0
        if size <= 0:
            return None
        desired = f'{size}x{size}'
        for key in ('cover_xl', 'cover_big', 'cover_medium', 'cover'):
            url = item.get(key)
            if not url:
                continue
            for known in ('2000x2000', '1600x1600', '1500x1500', '1400x1400', '1200x1200', '1000x1000', '800x800', '500x500', '250x250', '120x120', '56x56'):
                token = f'/{known}-'
                if token in url:
                    return url.replace(token, f'/{desired}-', 1)
            if '/images/cover/' in url and '-' in url.rsplit('/', 1)[-1]:
                prefix, last = url.rsplit('/', 1)
                suffix = last.split('-', 1)[1]
                return f'{prefix}/{desired}-{suffix}'
        return None

    def _url_options(self, item):
        target_size = get_preferred_artwork_size()
        preferred_sizes = []
        for size in (target_size, 1400, 1200, 1000):
            try:
                size = int(size)
            except Exception:
                continue
            if size > 0 and size not in preferred_sizes:
                preferred_sizes.append(size)
        options = []
        for size in preferred_sizes:
            options.append((f'{size}px cover', self._derive_sized_cover_url(item, size)))
        options.extend([
            ('XL cover', item.get('cover_xl')),
            ('Big cover', item.get('cover_big')),
            ('Medium cover', item.get('cover_medium')),
            ('Original cover URL', item.get('cover')),
        ])
        # Keep ordering but remove duplicate URLs, since Deezer can return the
        # same CDN image through several aliases.
        deduped = []
        seen = set()
        for label, url in options:
            if url and url not in seen:
                deduped.append((label, url))
                seen.add(url)
        return deduped

    def get_candidates_from_release(self, artist, album, album_key, item, max_candidates=5, log=None, stop_event=None):
        candidates = []
        fetch_min_size = get_fetch_min_artwork_size()
        target_size = get_preferred_artwork_size()
        if log:
            log(f'  Trying selected Deezer album: {self.release_label(item)}')
        title = item.get('title', '')
        release_date = item.get('release_date') or item.get('release_year') or ''
        display_title = title + (f' ({str(release_date)[:4]})' if release_date else '')
        album_id = item.get('id')
        seen_urls = set()
        urls = []
        for detail, url in self._url_options(item):
            if url and url not in seen_urls:
                urls.append((detail, url))
                seen_urls.add(url)
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
                    log(f'    Skipped Deezer {source_detail} below fetch minimum ({w}x{h}; minimum {fetch_min_size}px)')
                continue
            ctype = img_r.headers.get('Content-Type', '').lower()
            ext = '.png' if 'png' in ctype else '.webp' if 'webp' in ctype else '.jpg'
            base = f'{sanitize_filename(artist)} - {sanitize_filename(clean_album_name(album))} - Deezer - option - {len(candidates) + 1}'
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
                'release_title': display_title,
                'release_mbid': f'deezer:{album_id}' if album_id else '',
                'source_meta': {
                    'source_artist': self._album_artist(item),
                    'source_title': title,
                    'source_year': str(release_date)[:4] if release_date else '',
                    'release_date': release_date,
                    'track_count': item.get('nb_tracks') or item.get('track_count') or '',
                    'record_type': item.get('record_type') or '',
                    'source_page': f'https://www.deezer.com/album/{album_id}' if album_id else '',
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
        results = self.search_albums(artist, album, year=year, limit=20, log=log, stop_event=stop_event)
        candidates = []
        for item in results:
            if stop_event and stop_event.is_set():
                break
            remaining = max_candidates - len(candidates)
            if remaining <= 0:
                break
            candidates.extend(self.get_candidates_from_release(artist, album, album_key, item, remaining, log=log, stop_event=stop_event))
        return candidates
