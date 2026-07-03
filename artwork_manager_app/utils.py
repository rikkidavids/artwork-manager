import os, re, shutil, hashlib, platform, webbrowser, subprocess
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps, ImageChops, ImageStat, ImageFilter

INVALID_CHARS = r'<>:"/\\|?*'

_IMAGE_DIMENSIONS_CACHE = {}
_ARTWORK_COMPAT_PATH_CACHE = {}


def _file_cache_key(path):
    try:
        p = Path(path)
        st = p.stat()
        return (str(p), int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        return None

def clean_input_path(path: str) -> str:
    return os.path.abspath(path.strip().strip('\'"').replace('\\ ', ' '))

def sanitize_filename(name: str) -> str:
    name = name or ''
    for ch in INVALID_CHARS:
        name = name.replace(ch, '_')
    return name.strip() or 'Unknown'

def clean_album_name(name: str) -> str:
    name = name or ''
    name = re.sub(r'^\(\d{4}\)\s*-\s*', '', name)
    name = re.sub(r'^\d{4}\s*-\s*', '', name)
    return name.strip()

def normalize_for_match(name: str) -> str:
    name = sanitize_filename(clean_album_name(name)).lower()
    name = re.sub(r'[^a-z0-9]+', ' ', name)
    return ' '.join(name.split())

def image_dimensions_from_bytes(data: bytes):
    try:
        with Image.open(BytesIO(data)) as img:
            img.load(); return img.size
    except Exception:
        return None

def image_dimensions(path):
    key = _file_cache_key(path)
    if key is not None and key in _IMAGE_DIMENSIONS_CACHE:
        return _IMAGE_DIMENSIONS_CACHE[key]
    try:
        with Image.open(path) as img:
            img.load(); result = img.size
        if key is not None:
            if len(_IMAGE_DIMENSIONS_CACHE) > 1000:
                _IMAGE_DIMENSIONS_CACHE.clear()
            _IMAGE_DIMENSIONS_CACHE[key] = result
        return result
    except Exception:
        return None

def image_sha256_from_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()



def image_perceptual_hash(image_path, hash_size=8):
    try:
        with Image.open(image_path) as img:
            img.load()
            resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.LANCZOS)
            gray = img.convert('L').resize((hash_size, hash_size), resample)
            pixels = list(gray.getdata())
        avg = sum(pixels) / float(len(pixels) or 1)
        return ''.join('1' if px >= avg else '0' for px in pixels)
    except Exception:
        return ''


def image_perceptual_hash_from_bytes(data, hash_size=8):
    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.LANCZOS)
            gray = img.convert('L').resize((hash_size, hash_size), resample)
            pixels = list(gray.getdata())
        avg = sum(pixels) / float(len(pixels) or 1)
        return ''.join('1' if px >= avg else '0' for px in pixels)
    except Exception:
        return ''


def hamming_distance(a, b):
    if not a or not b or len(a) != len(b):
        return 999
    return sum(1 for x, y in zip(a, b) if x != y)

def _corner_average_rgb(img):
    """Return a quiet background colour from the image corners."""
    try:
        w, h = img.size
        pts = [
            img.getpixel((0, 0)),
            img.getpixel((max(0, w - 1), 0)),
            img.getpixel((0, max(0, h - 1))),
            img.getpixel((max(0, w - 1), max(0, h - 1))),
        ]
        return tuple(max(0, min(255, int(round(sum(p[i] for p in pts) / len(pts))))) for i in range(3))
    except Exception:
        return (255, 255, 255)


def prepare_jpeg_bytes(image_path, max_size=None, make_square=False):
    """Return baseline (non-progressive) JPEG bytes for embedding/display.

    All source formats supported by Pillow, including PNG and WebP, are
    converted to RGB JPEG output. If max_size is supplied, artwork larger than
    that on either edge is downscaled using high-quality Lanczos resampling.
    Smaller artwork content is never enlarged. When make_square is true,
    non-square artwork is centred on a square canvas after any downscale; this
    fixes near-square covers such as 1200×1184 without cropping artwork. The
    saved JPEG is always encoded as a standard non-progressive/baseline JPEG for
    better compatibility with music players, tag readers, and simple cover-file
    consumers.
    """
    with Image.open(image_path) as img:
        img.load()
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        # Preserve transparent PNG/WebP artwork more predictably. Pillow's plain
        # RGBA->RGB conversion uses black for transparent pixels, which can create
        # ugly dark borders. Composite transparency over white first.
        if img.mode in ('RGBA', 'LA') or ('transparency' in getattr(img, 'info', {})):
            try:
                rgba = img.convert('RGBA')
                bg = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
                bg.alpha_composite(rgba)
                img = bg.convert('RGB')
            except Exception:
                img = img.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        max_size_i = 0
        if max_size:
            try:
                max_size_i = int(max_size)
            except Exception:
                max_size_i = 0
            if max_size_i > 0:
                w, h = img.size
                longest = max(w, h)
                if longest > max_size_i:
                    scale = max_size_i / float(longest)
                    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
                    resample = getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', Image.LANCZOS)
                    img = img.resize(new_size, resample)

        if make_square:
            w, h = img.size
            if w > 0 and h > 0 and w != h:
                # Use the target canvas when the artwork already reaches that target
                # on one side; otherwise use the image's longest side so low-res art
                # is not misreported as target-size simply because it was padded.
                side = max(w, h)
                if max_size_i > 0 and max(w, h) >= int(round(max_size_i * 0.98)):
                    side = max_size_i
                bg = Image.new('RGB', (side, side), _corner_average_rgb(img))
                bg.paste(img, ((side - w) // 2, (side - h) // 2))
                img = bg

        buf = BytesIO()
        img.save(buf, format='JPEG', quality=92, optimize=True, progressive=False)
        return buf.getvalue(), 'image/jpeg'



def artwork_compatibility_from_bytes(data: bytes):
    """Return basic embedded-artwork compatibility details.

    Older players are happiest with baseline/non-progressive JPEG artwork.
    This helper intentionally labels PNG/WebP/progressive JPEG as needing
    conversion even if their dimensions are otherwise acceptable.
    """
    result = {
        'format': '',
        'is_jpeg': False,
        'is_progressive_jpeg': False,
        'is_baseline_jpeg': False,
        'compatible': False,
        'issue': 'unknown format',
    }
    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            fmt = (img.format or '').upper()
            info = getattr(img, 'info', {}) or {}
            progressive = bool(info.get('progressive') or info.get('progression'))
    except Exception as exc:
        result['issue'] = f'cannot read artwork: {exc}'
        return result

    result['format'] = fmt or 'UNKNOWN'
    result['is_jpeg'] = fmt in {'JPEG', 'JPG'}
    result['is_progressive_jpeg'] = bool(result['is_jpeg'] and progressive)
    result['is_baseline_jpeg'] = bool(result['is_jpeg'] and not progressive)
    result['compatible'] = bool(result['is_baseline_jpeg'])
    if result['compatible']:
        result['issue'] = ''
    elif result['is_progressive_jpeg']:
        result['issue'] = 'progressive JPEG'
    elif fmt:
        result['issue'] = f'{fmt} artwork'
    else:
        result['issue'] = 'non-baseline artwork'
    return result



def artwork_compatibility_from_path(path):
    """Return baseline-JPEG compatibility details for an image file.

    Used for album-folder cover files as well as embedded artwork checks. Folder
    covers are often checked repeatedly while browsing/filtering, so cache by
    path + modified time + file size and invalidate automatically when the file
    changes.
    """
    key = _file_cache_key(path)
    if key is not None and key in _ARTWORK_COMPAT_PATH_CACHE:
        return dict(_ARTWORK_COMPAT_PATH_CACHE[key])
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        return {
            'format': '',
            'is_jpeg': False,
            'is_progressive_jpeg': False,
            'is_baseline_jpeg': False,
            'compatible': False,
            'issue': f'cannot read file: {exc}',
        }
    result = artwork_compatibility_from_bytes(data)
    if key is not None:
        if len(_ARTWORK_COMPAT_PATH_CACHE) > 1000:
            _ARTWORK_COMPAT_PATH_CACHE.clear()
        _ARTWORK_COMPAT_PATH_CACHE[key] = dict(result)
    return result



def artwork_meets_target_size(width, height, target_size, tolerance=None):
    """Return True when artwork satisfies the configured target-size mode.

    Strict mode requires both edges to reach the target. Relaxed mode accepts a
    small near miss on the shorter edge when the longer edge reaches the target
    so provider images such as 1200×1190 or 1400×1388 do not get rejected just
    for being a few pixels short.
    """
    try:
        w = int(width or 0)
        h = int(height or 0)
        target = int(target_size or 0)
    except Exception:
        return False
    if target <= 0:
        return True
    if w <= 0 or h <= 0:
        return False
    if w >= target and h >= target:
        return True
    if tolerance is None:
        try:
            from .config import get_target_size_tolerance
            tolerance = get_target_size_tolerance()
        except Exception:
            tolerance = 0.98
    try:
        tolerance = float(tolerance)
    except Exception:
        tolerance = 0.98
    tolerance = max(0.0, min(1.0, tolerance))
    if max(w, h) >= target and min(w, h) >= int(round(target * tolerance)):
        return True
    return False

def quality_warnings(image_path, min_size=1000):
    warnings = []
    scored = score_artwork(image_path, min_size=min_size)
    try:
        with Image.open(image_path) as img:
            img.load()
            w, h = img.size
            if not artwork_meets_target_size(w, h, min_size):
                warnings.append(f'below target ({w}x{h})')
            ratio = max(w, h) / max(1, min(w, h))
            if ratio > 1.08:
                warnings.append('not square / possibly stretched')
            size_kb = os.path.getsize(image_path) / 1024
            if artwork_meets_target_size(w, h, min_size) and size_kb < 120:
                warnings.append('small file size for resolution')
    except Exception as exc:
        warnings.append(f'quality check failed: {exc}')
        return warnings

    reasons = scored.get('reasons') or []
    for reason in reasons:
        if 'blur' in reason or 'soft' in reason:
            warnings.append('possibly blurry/soft')
        elif 'photo/scan' in reason or 'photographed sleeve' in reason:
            warnings.append('looks like photo/scan of physical cover')
        elif 'border/background' in reason:
            warnings.append('possible plain border')
        elif 'odd aspect ratio' in reason and 'not square / possibly stretched' not in warnings:
            warnings.append('not square / possibly stretched')
        elif 'low resolution' in reason and not any('below target' in w for w in warnings):
            warnings.append('below target')
        elif 'possible upscaled' in reason:
            warnings.append('possibly upscaled')
        elif 'possible watermark' in reason or 'web overlay' in reason:
            warnings.append('possible watermark/overlay')
    # Deduplicate while preserving order
    out = []
    for item in warnings:
        if item not in out:
            out.append(item)
    return out

def open_path(path):
    if not path:
        return False
    try:
        p = Path(path).expanduser()
        # macOS Finder opens local folders/files much more reliably with the
        # native `open` command than through webbrowser/file URLs, especially
        # for external drives and mounted NAS paths.
        if platform.system() == 'Darwin':
            subprocess.Popen(['open', str(p)])
            return True
        if platform.system() == 'Windows':
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True
        subprocess.Popen(['xdg-open', str(p)])
        return True
    except Exception:
        try:
            webbrowser.open(str(Path(path).resolve()))
            return True
        except Exception:
            return False


def _safe_open_rgb(image_path, resize_to=300):
    img = Image.open(image_path)
    img.load()
    rgb = img.convert('RGB')
    if resize_to:
        rgb = rgb.resize((resize_to, resize_to))
    return rgb


def _edge_variance(gray_img):
    shifted_x = ImageChops.offset(gray_img, 1, 0)
    shifted_y = ImageChops.offset(gray_img, 0, 1)
    diff = ImageChops.difference(gray_img, shifted_x)
    diff2 = ImageChops.difference(gray_img, shifted_y)
    return (ImageStat.Stat(diff).var[0] + ImageStat.Stat(diff2).var[0]) / 2.0


def score_artwork(image_path, min_size=1000):
    """Return a quality score and explanation for a cover image.

    Higher scores prefer clean, sharp, square digital artwork and penalise
    blurry/soft images, photographed physical copies, heavy borders, and odd
    aspect ratios.
    """
    result = {
        'score': 0,
        'summary': 'Unable to score image',
        'reasons': [],
        'metrics': {},
    }
    try:
        with Image.open(image_path) as img:
            img.load()
            w, h = img.size
        result['metrics'].update({'width': w, 'height': h})
        score = 50
        reasons = []

        min_dim = min(w, h)
        max_dim = max(w, h)
        ratio = max_dim / max(1, min_dim)
        try:
            target_size = max(1, int(min_size or 1000))
        except Exception:
            target_size = 1000

        # Prefer artwork that is clean and close to the user's configured
        # target size. Very large provider images are useful, but because the
        # app now resizes on approval, a crisp 1200px cover should usually rank
        # ahead of an equally good 2500px/3000px cover when the user's target is
        # 1200px. Use the shorter edge so odd-aspect candidates still need to be
        # genuinely usable after resize.
        try:
            from .config import get_target_size_tolerance
            near_target_floor = int(round(target_size * get_target_size_tolerance()))
        except Exception:
            near_target_floor = int(round(target_size * 0.98))
        if min_dim >= target_size:
            over_ratio = min_dim / float(target_size)
            if over_ratio <= 1.10:
                score += 22; reasons.append(f'closest to {target_size}px target')
            elif over_ratio <= 1.25:
                score += 17; reasons.append(f'slightly over {target_size}px target')
            elif over_ratio <= 1.75:
                score += 10; reasons.append(f'larger than {target_size}px target')
            else:
                score += 4; reasons.append(f'much larger than {target_size}px target')
        elif min_dim >= near_target_floor:
            score += 14; reasons.append(f'near {target_size}px target')
        elif min_dim >= int(round(target_size * 0.75)):
            score += 5; reasons.append(f'usable but under {target_size}px target')
        elif min_dim >= 800:
            score += 1; reasons.append(f'under {target_size}px target')
        else:
            score -= 18; reasons.append('low resolution')

        # Mildly demote enormous artwork when it is far beyond the user's target.
        # It may still win if it is sharper/cleaner, but it no longer wins just
        # for being huge.
        if min_dim >= int(round(target_size * 2.5)):
            score -= 8; reasons.append('oversized versus target')
        elif min_dim >= int(round(target_size * 1.75)):
            score -= 4; reasons.append('above target; will be resized')

        if ratio <= 1.02:
            score += 16; reasons.append('very square cover')
        elif ratio <= 1.05:
            score += 10; reasons.append('square-ish cover')
        elif ratio <= 1.10:
            score += 2
        else:
            score -= 14; reasons.append('odd aspect ratio')

        size_kb = os.path.getsize(image_path) / 1024.0
        megapixels = max(0.01, (w * h) / 1_000_000.0)
        kb_per_mp = size_kb / megapixels
        result['metrics'].update({'size_kb': round(size_kb, 1), 'kb_per_mp': round(kb_per_mp, 1), 'ratio': round(ratio, 3)})
        if kb_per_mp >= 350:
            score += 8; reasons.append('healthy file quality')
        elif kb_per_mp >= 180:
            score += 3
        elif kb_per_mp < 55:
            score -= 10; reasons.append('small file size for resolution')
        elif kb_per_mp < 90:
            score -= 4

        rgb = _safe_open_rgb(image_path, resize_to=320)
        gray = rgb.convert('L')
        sharpness = _edge_variance(gray)
        result['metrics']['sharpness_var'] = round(sharpness, 2)
        if sharpness >= 180:
            score += 16; reasons.append('crisp details')
        elif sharpness >= 110:
            score += 10; reasons.append('fairly sharp')
        elif sharpness >= 60:
            score += 3
        else:
            score -= 18; reasons.append('possibly blurry/soft')

        # Border / photographed copy heuristic. Compare edges to the centre and
        # look for flat background strips that often appear in scans/photos.
        top = rgb.crop((0, 0, 320, 16))
        bottom = rgb.crop((0, 304, 320, 320))
        left = rgb.crop((0, 0, 16, 320))
        right = rgb.crop((304, 0, 320, 320))
        centre = rgb.crop((64, 64, 256, 256))
        edge_mean = sum(sum(ImageStat.Stat(x).mean) for x in (top, bottom, left, right)) / 12.0
        edge_var = sum(sum(ImageStat.Stat(x).var) for x in (top, bottom, left, right)) / 12.0
        centre_var = sum(ImageStat.Stat(centre).var) / 3.0
        result['metrics'].update({'edge_mean': round(edge_mean,1), 'edge_var': round(edge_var,1), 'centre_var': round(centre_var,1)})

        plain_edge = edge_mean > 238 or edge_mean < 12
        if plain_edge:
            score -= 8; reasons.append('plain border/background at edges')
        if plain_edge and edge_var < 45 and centre_var > max(20, edge_var * 2.0):
            score -= 12; reasons.append('looks like photo/scan of physical cover')
        elif edge_var < 25 and plain_edge:
            score -= 4

        # Slight penalty for heavy shadows / photographed sleeves: blur edge bands
        # and compare with original edge mean to spot consistent dark framing.
        edge_strip = Image.new('RGB', (320, 64), 'white')
        edge_strip.paste(top, (0, 0)); edge_strip.paste(bottom, (0, 16));
        edge_strip.paste(left.resize((160, 16)), (0, 32)); edge_strip.paste(right.resize((160, 16)), (160, 32))
        blur = edge_strip.filter(ImageFilter.GaussianBlur(radius=4))
        shadow_var = sum(ImageStat.Stat(ImageChops.difference(edge_strip, blur)).var) / 3.0
        result['metrics']['shadow_var'] = round(shadow_var, 1)
        if shadow_var < 20 and edge_mean < 60:
            score -= 8; reasons.append('dark frame / likely photographed sleeve')

        # Upscale heuristic: very large dimensions but little edge/detail
        # usually indicate a smaller source stretched to a bigger canvas.
        if min_dim >= min_size and sharpness < 80 and kb_per_mp < 120:
            score -= 14; reasons.append('possible upscaled low-detail image')

        # Watermark / web overlay heuristic: small high-contrast marks in corners
        # are not always watermarks, so keep this as a mild warning/penalty.
        corners = [rgb.crop((0, 0, 56, 56)), rgb.crop((264, 0, 320, 56)), rgb.crop((0, 264, 56, 320)), rgb.crop((264, 264, 320, 320))]
        corner_edge = sum(_edge_variance(c.convert('L')) for c in corners) / 4.0
        result['metrics']['corner_edge_var'] = round(corner_edge, 1)
        if corner_edge > max(260, sharpness * 1.8):
            score -= 4; reasons.append('possible watermark or web overlay')

        # Friendly summary
        final = max(0, min(100, int(round(score))))
        if final >= 90:
            summary = 'Excellent clean digital cover'
        elif final >= 80:
            summary = 'Strong artwork option'
        elif final >= 65:
            summary = 'Decent option with minor issues'
        elif final >= 50:
            summary = 'Usable but review carefully'
        else:
            summary = 'Weak option – likely scan/photo or soft image'

        result.update({'score': final, 'summary': summary, 'reasons': reasons})
        return result
    except Exception as exc:
        result['summary'] = f'Quality scoring failed: {exc}'
        result['reasons'] = ['quality scoring failed']
        return result
