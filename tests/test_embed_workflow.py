import hashlib
import os

from artwork_manager_app import approval, database as db, embedder
from artwork_manager_app.config import load_settings
from artwork_manager_app.scanner import album_music_relpaths, embedded_artwork


def _md5(path):
    return hashlib.md5(open(path, 'rb').read()).hexdigest()


def test_music_listing_includes_disc_folders_and_skips_appledouble(multidisc_album):
    rel = album_music_relpaths(str(multidisc_album))
    assert rel == [os.path.join('CD1', '01.flac'), os.path.join('CD1', '02.flac'),
                   os.path.join('CD2', '01.flac'), os.path.join('CD2', '02.flac')]
    walked = [os.path.relpath(p, multidisc_album) for p in embedder.iter_music_files(str(multidisc_album))]
    assert walked == rel


def test_backups_keep_same_named_tracks_apart_and_undo_restores_each(multidisc_album, cover_image):
    before = {p: _md5(p) for p in embedder.iter_music_files(str(multidisc_album))}
    result = embedder.embed_album(str(multidisc_album), str(cover_image), 'backup-test', backup=True, max_artwork_size=1000)
    assert result['updated'] == result['total'] == 4
    backups = [b['backup'] for b in result['backups']]
    assert len(set(backups)) == 4, 'each track needs its own backup file'
    assert len({os.path.dirname(os.path.dirname(b)) for b in backups}) == 1, 'one embed shares one backup folder'

    for fp in before:
        assert embedded_artwork(fp), f'{fp} should now have artwork'

    undo = embedder.undo_last_embed()
    assert undo['restored'] == 4 and not undo['failed']
    for fp, digest in before.items():
        assert _md5(fp) == digest, f'{fp} was restored with the wrong content'


def test_backup_listing_points_at_embed_folder(multidisc_album, cover_image):
    embedder.embed_album(str(multidisc_album), str(cover_image), 'listing-test', backup=True, max_artwork_size=1000)
    row = next(r for r in embedder.list_embed_backups() if r['album_key'] == 'listing-test')
    assert row['backup_count'] == 4
    assert os.path.isdir(os.path.join(row['backup_dir'], 'CD1'))
    assert os.path.isdir(os.path.join(row['backup_dir'], 'CD2'))


def test_approve_multidisc_album_verifies_every_disc(multidisc_album, cover_image):
    key = 'approve-test'
    db.upsert_album(key, 'Test Artist', 'Test Album', str(multidisc_album), status='candidate_found')
    cid = db.add_candidate(key, {'source': 'test', 'image_path': str(cover_image), 'width': 1400, 'height': 1400, 'score': 95})
    candidate = {'id': cid, 'album_key': key, 'album_folder': str(multidisc_album), 'image_path': str(cover_image),
                 'artist': 'Test Artist', 'album': 'Test Album', 'width': 1400, 'height': 1400, 'score': 95}
    settings = load_settings()
    settings.update({'nas_worker_enabled': False, 'preferred_artwork_size': 1000, 'verify_after_embed_before_good': True})

    result = approval.approve_candidate(candidate, settings=settings)

    assert result['total_files'] == 4
    assert result['verification']['checked_files'] == 4
    assert result['approval_complete'], result['final_reason']
    assert db.get_album(key)['status'] == 'approved'
