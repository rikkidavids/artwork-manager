import os, json, shutil
from pathlib import Path
from datetime import datetime
from io import BytesIO
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover
from PIL import Image
from .config import MUSIC_EXTENSIONS, BACKUP_DIR, APPROVED_DIR, load_settings, get_max_embedded_artwork_size
from .utils import prepare_jpeg_bytes, image_dimensions_from_bytes, sanitize_filename, clean_album_name
from . import database as db


def iter_music_files(album_folder):
    for root,_,files in os.walk(album_folder):
        for fn in files:
            if fn.lower().endswith(MUSIC_EXTENSIONS): yield os.path.join(root,fn)

def backup_file(file_path, album_key):
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    safe=sanitize_filename(album_key)[:120]
    dest_dir=BACKUP_DIR / safe / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest=dest_dir / Path(file_path).name
    shutil.copy2(file_path, dest)
    return str(dest)

def embed_file(file_path, image_bytes, mime='image/jpeg'):
    ext=os.path.splitext(file_path)[1].lower()
    if ext=='.mp3':
        try: audio=ID3(file_path)
        except ID3NoHeaderError: audio=ID3()
        audio.delall('APIC')
        audio.add(APIC(encoding=3,mime=mime,type=3,desc='Cover',data=image_bytes))
        audio.save(file_path, v2_version=3); return True
    if ext=='.flac':
        audio=FLAC(file_path); audio.clear_pictures(); pic=Picture(); pic.type=3; pic.mime=mime; pic.desc='Cover'; pic.data=image_bytes
        try:
            with Image.open(BytesIO(image_bytes)) as img:
                pic.width,pic.height=img.size; pic.depth=len(img.getbands())*8
        except Exception: pass
        audio.add_picture(pic); audio.save(); return True
    if ext in ('.m4a','.mp4'):
        audio=MP4(file_path)
        if audio.tags is None: audio.add_tags()
        audio.tags['covr']=[MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save(); return True
    return False

def embed_album(album_folder, image_path, album_key, backup=True, progress=None, stop_event=None, max_artwork_size=None, make_square=False):
    """Embed artwork into every music file for an album.

    progress(done, total, file_path) is called after each file. The UI runs this
    in a background thread so slow NAS/USB writes do not freeze the window.
    """
    if max_artwork_size is None:
        settings = load_settings()
        max_artwork_size = get_max_embedded_artwork_size(settings) if settings.get('resize_approved_artwork', True) else None
    image_bytes,mime=prepare_jpeg_bytes(image_path, max_size=max_artwork_size, make_square=make_square)
    embedded_size = image_dimensions_from_bytes(image_bytes) or (None, None)
    files=list(iter_music_files(album_folder)); backups=[]; updated=0; failed=[]
    total=len(files)
    if total == 0:
        image_width, image_height = embedded_size
        payload = {
            'album_folder': album_folder,
            'image_path': image_path,
            'backups': backups,
            'updated': 0,
            'failed': failed,
            'total': 0,
            'image_width': image_width,
            'image_height': image_height,
            'no_audio_files': True,
            'message': 'No supported audio files found in album folder',
        }
        db.add_history(album_key, 'embed_blocked', payload)
        return payload
    for idx, fp in enumerate(files, 1):
        if stop_event is not None and stop_event.is_set():
            failed.append({'file':fp,'error':'Embedding cancelled before this file was updated'})
            if progress:
                try: progress(idx, total, fp)
                except Exception: pass
            continue
        try:
            b=None
            if backup: b=backup_file(fp, album_key); backups.append({'file':fp,'backup':b})
            if embed_file(fp, image_bytes, mime): updated+=1
        except Exception as exc:
            failed.append({'file':fp,'error':str(exc)})
        if progress:
            try: progress(idx, total, fp)
            except Exception: pass
    image_width, image_height = embedded_size
    payload = {'album_folder':album_folder,'image_path':image_path,'backups':backups,'updated':updated,'failed':failed,'image_width':image_width,'image_height':image_height}
    db.add_history(album_key,'embed',payload)
    return {'updated':updated,'failed':failed,'total':len(files),'backups':backups,'image_width':image_width,'image_height':image_height}

def archive_approved(image_path, artist, album):
    safe_artist=sanitize_filename(artist); safe_album=sanitize_filename(clean_album_name(album)); ext=Path(image_path).suffix or '.jpg'
    out=APPROVED_DIR / f'{safe_artist} - {safe_album}{ext}'; i=1
    while out.exists():
        out=APPROVED_DIR / f'{safe_artist} - {safe_album}_{i}{ext}'; i+=1
    shutil.copy2(image_path,out); return str(out)


def save_approved_artwork_to_album_folder(image_path, artist, album, album_folder, max_artwork_size=None, make_square=False):
    if not album_folder or not os.path.isdir(album_folder):
        raise ValueError('Album folder is missing or unavailable.')

    # Album-folder artwork copies should use a predictable player-friendly
    # name instead of artist/album names, e.g. cover.jpg / cover.png / cover.webp.
    # When resize-to-target is enabled, the saved copy is encoded as JPEG, so the
    # filename is always cover.jpg. If resizing is disabled, preserve the source
    # file type where possible.
    if max_artwork_size is None:
        settings = load_settings()
        max_artwork_size = get_max_embedded_artwork_size(settings) if settings.get('resize_approved_artwork', True) else None

    album_dir = Path(album_folder)
    if max_artwork_size:
        out = album_dir / 'cover.jpg'
        data, _mime = prepare_jpeg_bytes(image_path, max_size=max_artwork_size, make_square=make_square)
        out.write_bytes(data)
    else:
        ext = Path(image_path).suffix.lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
            ext = '.jpg'
        out = album_dir / f'cover{ext}'
        shutil.copy2(image_path, out)

    # Avoid leaving stale app-written cover variants beside the newly saved one.
    # This only targets the standard cover.* names in the selected album folder.
    for stale_name in ('cover.jpg', 'cover.jpeg', 'cover.png', 'cover.webp'):
        stale = album_dir / stale_name
        if stale != out and stale.exists() and stale.is_file():
            try:
                stale.unlink()
            except Exception:
                pass
    return str(out)

def undo_last_embed():
    h=db.last_history('embed')
    if not h: return {'restored':0,'message':'No embed history found.'}
    payload=json.loads(h['payload']); restored=0; failed=[]
    for item in payload.get('backups',[]):
        try:
            shutil.copy2(item['backup'], item['file']); restored+=1
        except Exception as exc:
            failed.append({'file':item.get('file'),'error':str(exc)})
    db.add_history(h['album_key'],'undo_embed',{'restored':restored,'failed':failed,'from_history_id':h['id']})
    return {'restored':restored,'failed':failed}


def list_embed_backups(limit=500):
    rows = db.history_rows('embed', limit=limit)
    out = []
    for row in rows:
        try:
            payload = json.loads(row.get('payload') or '{}')
        except Exception:
            payload = {}
        backups = payload.get('backups') or []
        out.append({
            'history_id': row.get('id'),
            'album_key': row.get('album_key'),
            'created_at': row.get('created_at'),
            'album_folder': payload.get('album_folder') or '',
            'image_path': payload.get('image_path') or '',
            'updated': payload.get('updated') or 0,
            'failed_count': len(payload.get('failed') or []),
            'backup_count': len(backups),
            'backup_dir': str(Path(backups[0].get('backup')).parent) if backups and backups[0].get('backup') else '',
        })
    return out


def restore_embed_history(history_id):
    h = db.get_history(history_id)
    if not h:
        return {'restored': 0, 'failed': [{'file': '', 'error': 'Backup history entry not found'}]}
    try:
        payload = json.loads(h.get('payload') or '{}')
    except Exception:
        payload = {}
    restored = 0
    failed = []
    for item in payload.get('backups', []):
        try:
            shutil.copy2(item['backup'], item['file'])
            restored += 1
        except Exception as exc:
            failed.append({'file': item.get('file'), 'error': str(exc)})
    db.add_history(h.get('album_key'), 'restore_embed', {'restored': restored, 'failed': failed, 'from_history_id': h.get('id')})
    return {'restored': restored, 'failed': failed}
