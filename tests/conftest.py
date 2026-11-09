"""Shared test setup.

The app writes its database and settings at import time, so point it at a
throwaway data folder before any app module is imported.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_DATA_DIR = tempfile.mkdtemp(prefix='artwork-manager-tests-')
os.environ['ARTWORK_MANAGER_DATA_DIR'] = _DATA_DIR
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_track(path: Path, title: str, freq: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', f'sine=frequency={freq}:duration=1',
         '-metadata', 'artist=Test Artist', '-metadata', 'album=Test Album', '-metadata', f'title={title}',
         str(path)],
        check=True,
    )


@pytest.fixture
def multidisc_album(tmp_path):
    """Library/Test Artist/Test Album/{CD1,CD2}/{01,02}.flac, plus a macOS ._ sidecar."""
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg is needed to generate test audio files')
    album = tmp_path / 'Library' / 'Test Artist' / 'Test Album'
    for disc, freq in (('CD1', 220), ('CD2', 880)):
        for track in ('01.flac', '02.flac'):
            _make_track(album / disc / track, f'{disc} {track}', freq)
    (album / 'CD1' / '._01.flac').write_bytes(b'\x00\x05\x16\x07 AppleDouble junk')
    return album


@pytest.fixture
def cover_image(tmp_path):
    from PIL import Image
    path = tmp_path / 'cover.png'
    Image.new('RGB', (1400, 1400), (180, 40, 40)).save(path)
    return path
