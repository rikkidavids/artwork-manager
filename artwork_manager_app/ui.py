import os, sys, queue, threading, webbrowser, json, hashlib, time, re, shutil, tempfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from PIL import Image, ImageTk, ImageFilter, ImageOps
from .config import APP_DIR, BUILD_VERSION, MIN_ARTWORK_SIZE, APPROVED_DIR, REPORT_DIR, BACKUP_DIR, PREVIEW_CACHE_DIR, TEMP_DIR, IMPORT_DIR, DATA_DIR, DB_PATH, load_settings, save_settings, get_scan_min_artwork_size, get_fetch_min_artwork_size, get_preferred_artwork_size, get_target_size_match_mode, get_deep_scan_all_files, get_scan_worker_threads, get_max_candidates_per_album, get_batch_search_count, get_max_embedded_artwork_size, get_nas_worker_enabled
from .utils import clean_input_path, open_path, normalize_for_match, clean_album_name, artwork_meets_target_size, prepare_jpeg_bytes, image_dimensions_from_bytes
from .scanner import scan_library, write_low_res_csv, count_album_folders, embedded_artwork, inspect_album_identity, analyze_album_folder, _album_folder_cover_status, deep_check_album_problem_files, _deep_check_album_files
from .review_queue import build_candidates, google_images_url, manual_import
from .embedder import embed_album, archive_approved, save_approved_artwork_to_album_folder, undo_last_embed, iter_music_files, list_embed_backups, restore_embed_history
from .state import evaluate_album_state, evaluate_album_record, workflow_bucket_for_status, good_reason_from_notes, needs_convert_reason, folder_cover_required, album_has_not_square_artwork, not_square_reason, effective_deep_file_check, deep_check_resolved_note
from .providers.discogs import DiscogsProvider
from .providers.deezer import DeezerProvider
from .providers.itunes import ITunesProvider
from .providers.musicbrainz import MusicBrainzProvider
from . import database as db
from .remote_worker import worker_enabled_for_path, embed_album_remote, deep_check_album_remote, check_worker, worker_status, worker_path_check, map_album_path_to_worker, RemoteWorkerError, worker_update_hint


def _pil_resample_lanczos():
    try:
        return Image.Resampling.LANCZOS
    except Exception:
        return getattr(Image, 'LANCZOS', getattr(Image, 'BICUBIC', 3))


def _high_quality_fit_image(img, max_size, *, allow_upscale=False, sharpen=True):
    """Return a crisp Pillow image fitted into max_size for Tk display.

    Tk's default thumbnail scaling can look soft compared with Finder/Quick Look.
    Use explicit Lanczos resampling, respect EXIF orientation, and add a very
    small post-resize sharpen only when downscaling.
    """
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    max_w, max_h = max(1, int(max_size[0])), max(1, int(max_size[1]))
    w, h = max(1, int(img.width)), max(1, int(img.height))
    scale = min(max_w / w, max_h / h)
    if not allow_upscale:
        scale = min(1.0, scale)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    out = img.copy()
    if (new_w, new_h) != (w, h):
        out = out.resize((new_w, new_h), _pil_resample_lanczos())
        if sharpen and scale < 0.95:
            try:
                out = out.filter(ImageFilter.UnsharpMask(radius=0.55, percent=115, threshold=2))
            except Exception:
                pass
    return out


def _scroll_units_from_event(event):
    """Return small scroll units for mouse wheels and Mac trackpad gestures."""
    try:
        delta = getattr(event, 'delta', 0)
        if delta:
            units = -int(delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
            return units
        num = getattr(event, 'num', None)
        if num == 4:
            return -1
        if num == 5:
            return 1
    except Exception:
        pass
    return 0


def bind_vertical_scroll(widget, scroll_target=None, *, horizontal_target=None):
    """Bind mouse-wheel / two-finger trackpad scrolling to Tk widgets.

    Tk's default wheel handling is inconsistent across Text, Canvas and
    Treeview on macOS, especially with two-finger gestures. Binding the widget
    itself keeps scrolling tied to the pane under the pointer.
    """
    target = scroll_target or widget
    htarget = horizontal_target

    def on_wheel(event):
        units = _scroll_units_from_event(event)
        if not units:
            return None
        try:
            if (getattr(event, 'state', 0) & 0x0001) and htarget is not None:
                htarget.xview_scroll(units, 'units')
            else:
                target.yview_scroll(units, 'units')
            return 'break'
        except Exception:
            return None

    for seq in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
        widget.bind(seq, on_wheel, add='+')
    if horizontal_target is not None:
        widget.bind('<Shift-MouseWheel>', on_wheel, add='+')


class QueueWriter:
    def __init__(self, q):
        self.q = q
    def write(self, s):
        if s:
            self.q.put(('LOG', s.replace('\r', '\n')))
    def flush(self):
        pass


def kb(size):
    try:
        return f'{int(size) // 1024} KB'
    except Exception:
        return 'unknown KB'


def file_kb(path):
    try:
        return kb(os.path.getsize(path))
    except Exception:
        return 'unknown KB'


def folder_size_text(path):
    try:
        total = 0
        root = Path(path)
        if root.exists():
            for item in root.rglob('*'):
                try:
                    if item.is_file():
                        total += item.stat().st_size
                except Exception:
                    pass
        if total >= 1024 * 1024 * 1024:
            return f'{total / (1024*1024*1024):.1f} GB'
        if total >= 1024 * 1024:
            return f'{total / (1024*1024):.1f} MB'
        return f'{total // 1024} KB'
    except Exception:
        return 'unknown'



def bytes_text(total):
    try:
        total = int(total or 0)
        if total >= 1024 * 1024 * 1024:
            return f'{total / (1024*1024*1024):.1f} GB'
        if total >= 1024 * 1024:
            return f'{total / (1024*1024):.1f} MB'
        return f'{total // 1024} KB'
    except Exception:
        return 'unknown'

class SettingsWindow(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master.root)
        self.title('Settings')
        self.geometry('640x520')
        self.minsize(600, 480)
        self.resizable(True, True)
        self.master_app = master
        self.settings = load_settings()
        self.discogs_token = tk.StringVar(value=self.settings.get('discogs_token', ''))
        self.discogs_enabled = tk.BooleanVar(value=bool(self.settings.get('discogs_enabled', True)))
        self.mb_enabled = tk.BooleanVar(value=bool(self.settings.get('musicbrainz_enabled', True)))
        self.deezer_enabled = tk.BooleanVar(value=bool(self.settings.get('deezer_enabled', True)))
        self.itunes_enabled = tk.BooleanVar(value=bool(self.settings.get('itunes_enabled', True)))
        self.fanart_enabled = tk.BooleanVar(value=bool(self.settings.get('fanarttv_enabled', False)))
        requested_order = self.settings.get('provider_order') or ['deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv']
        self.provider_order_keys = []
        for key in requested_order:
            if key not in self.provider_order_keys and key in ('deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv'):
                self.provider_order_keys.append(key)
        for fallback in ('deezer', 'itunes', 'musicbrainz', 'discogs', 'fanarttv'):
            if fallback not in self.provider_order_keys:
                self.provider_order_keys.append(fallback)
        self.scan_min_var = tk.StringVar(value=str(get_scan_min_artwork_size(self.settings)))
        self.fetch_min_var = tk.StringVar(value=str(get_fetch_min_artwork_size(self.settings)))
        self.preferred_size_var = tk.StringVar(value=str(get_preferred_artwork_size(self.settings)))
        self.target_size_mode_var = tk.StringVar(value=get_target_size_match_mode(self.settings))
        self.deep_scan_all_files = tk.BooleanVar(value=get_deep_scan_all_files(self.settings))
        self.scan_worker_threads_var = tk.StringVar(value=str(get_scan_worker_threads(self.settings)))
        self.max_candidates_var = tk.StringVar(value=str(get_max_candidates_per_album(self.settings)))
        self.batch_count_var = tk.StringVar(value=str(get_batch_search_count(self.settings)))
        self.save_album_artwork_file = tk.BooleanVar(value=bool(self.settings.get('save_approved_artwork_to_album_folder', False)))
        self.warn_before_low_confidence_embed = tk.BooleanVar(value=bool(self.settings.get('warn_before_low_confidence_embed', True)))
        self.resize_approved_artwork = tk.BooleanVar(value=bool(self.settings.get('resize_approved_artwork', True)))
        self.verify_after_embed = tk.BooleanVar(value=bool(self.settings.get('verify_after_embed_before_good', True)))
        self.ui_density_var = tk.StringVar(value=self.settings.get('ui_density', 'Comfortable') if self.settings.get('ui_density') in ('Comfortable', 'Compact') else 'Comfortable')
        self.nas_worker_enabled = tk.BooleanVar(value=bool(self.settings.get('nas_worker_enabled', False)))
        self.nas_worker_url = tk.StringVar(value=self.settings.get('nas_worker_url', 'http://nas.local:8765'))
        self.nas_worker_token = tk.StringVar(value=self.settings.get('nas_worker_token', ''))
        self.nas_worker_local_prefix = tk.StringVar(value=self.settings.get('nas_worker_local_prefix', ''))
        self.nas_worker_remote_prefix = tk.StringVar(value=self.settings.get('nas_worker_remote_prefix', '/music'))
        self.nas_worker_timeout = tk.StringVar(value=str(self.settings.get('nas_worker_timeout', 900)))

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill='both', expand=True)

        canvas = tk.Canvas(frm, highlightthickness=0)
        canvas.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(frm, orient='vertical', command=canvas.yview)
        sb.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=sb.set)
        body = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window_id, width=e.width))
        bind_vertical_scroll(canvas, canvas)
        bind_vertical_scroll(body, canvas)

        ttk.Label(body, text='Artwork Providers', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Checkbutton(body, text='Use MusicBrainz / Cover Art Archive', variable=self.mb_enabled).pack(anchor='w', pady=(10, 0))
        ttk.Checkbutton(body, text='Use Deezer album artwork', variable=self.deezer_enabled).pack(anchor='w')
        ttk.Checkbutton(body, text='Use iTunes / Apple artwork', variable=self.itunes_enabled).pack(anchor='w')
        ttk.Checkbutton(body, text='Use Discogs fallback', variable=self.discogs_enabled).pack(anchor='w')
        ttk.Checkbutton(body, text='Use fanart.tv placeholder provider', variable=self.fanart_enabled).pack(anchor='w')

        order_wrap = ttk.Frame(body)
        order_wrap.pack(fill='x', pady=(10, 0))
        ttk.Label(order_wrap, text='Provider search order:').pack(anchor='w')
        order_body = ttk.Frame(order_wrap)
        order_body.pack(fill='x', pady=(6, 0))
        self.provider_order_list = tk.Listbox(order_body, height=5, exportselection=False)
        self.provider_order_list.pack(side='left', fill='x', expand=True)
        order_btns = ttk.Frame(order_body)
        order_btns.pack(side='left', padx=(8, 0), anchor='n')
        ttk.Button(order_btns, text='Move Up', command=self.move_provider_up).pack(fill='x')
        ttk.Button(order_btns, text='Move Down', command=self.move_provider_down).pack(fill='x', pady=(6, 0))
        ttk.Label(body, text='Enabled providers are searched in this order. Use Search More to keep digging without discarding saved candidates.', foreground='gray', wraplength=560).pack(anchor='w', pady=(4, 0))
        self.refresh_provider_order_list()

        token_row = ttk.Frame(body)
        token_row.pack(fill='x', pady=(14, 4))
        ttk.Label(token_row, text='Discogs token:').pack(side='left')
        ttk.Entry(token_row, textvariable=self.discogs_token, show='•').pack(side='left', fill='x', expand=True, padx=(8, 0))
        ttk.Label(body, text='Used only for Discogs fallback searches.', foreground='gray').pack(anchor='w')

        ttk.Separator(body).pack(fill='x', pady=(16, 12))
        ttk.Label(body, text='Artwork Size Rules', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Label(
            body,
            text='These settings control which albums enter the queue and what fetched artwork is saved for review.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 8))

        grid = ttk.Frame(body)
        grid.pack(fill='x')
        grid.columnconfigure(1, weight=1)

        def add_setting(row, label, var, help_text):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky='w', pady=(4, 0), padx=(0, 10))
            entry = ttk.Entry(grid, textvariable=var, width=8)
            entry.grid(row=row, column=1, sticky='w', pady=(4, 0))
            ttk.Label(grid, text='px' if 'size' in label.lower() or 'artwork' in label.lower() else '', foreground='gray').grid(row=row, column=1, sticky='w', padx=(70, 0), pady=(4, 0))
            ttk.Label(grid, text=help_text, foreground='gray', wraplength=420).grid(row=row + 1, column=0, columnspan=2, sticky='w', pady=(0, 6))

        add_setting(0, 'Queue if embedded artwork below:', self.scan_min_var, 'Scan cutoff. Albums with missing artwork or artwork below this size are added to the queue.')
        add_setting(2, 'Only save fetched artwork at least:', self.fetch_min_var, 'Fetch cutoff. Smaller MusicBrainz/Deezer/Discogs images are skipped instead of saved as candidates.')
        add_setting(4, 'Preferred / target artwork size:', self.preferred_size_var, 'Used for scoring, warnings, badges, and approved-artwork resize/conversion.')

        ttk.Label(grid, text='Target size matching:').grid(row=6, column=0, sticky='w', pady=(4, 0), padx=(0, 10))
        ttk.Combobox(grid, textvariable=self.target_size_mode_var, values=('Relaxed', 'Strict'), state='readonly', width=14).grid(row=6, column=1, sticky='w', pady=(4, 0))
        ttk.Label(
            grid,
            text='Relaxed accepts near-target images such as 1200×1190 or 1400×1388. Strict requires both edges to meet the configured size.',
            foreground='gray', wraplength=420,
        ).grid(row=7, column=0, columnspan=2, sticky='w', pady=(0, 6))

        ttk.Checkbutton(
            grid,
            text='Deep check every music file during scan/re-evaluate',
            variable=self.deep_scan_all_files,
        ).grid(row=8, column=0, columnspan=2, sticky='w', pady=(4, 0))
        ttk.Label(
            grid,
            text='Slower, but checks every supported file in each album against the Preferred / target size and baseline-JPEG compatibility rules. This catches albums where one track differs from the rest.',
            foreground='gray', wraplength=420,
        ).grid(row=9, column=0, columnspan=2, sticky='w', pady=(0, 6))

        add_setting(10, 'Scan album checks at once:', self.scan_worker_threads_var, 'How many album folders can be inspected in parallel during Scan / Resume. For most NAS shares, 4–12 is the useful range.')
        add_setting(12, 'Max options saved per album:', self.max_candidates_var, 'Limits how many artwork candidates a single Find Artwork run saves per album.')
        add_setting(14, 'Batch search count:', self.batch_count_var, 'How many albums are searched by the Search Next N Albums button.')

        ttk.Label(
            body,
            text='Example: set scan cutoff to 800 if 800px covers are acceptable, but keep fetched artwork at 1000 or 1200px so replacements are better.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(12, 0))

        ttk.Separator(body).pack(fill='x', pady=(16, 12))
        ttk.Label(body, text='Approval / Artwork File', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Checkbutton(
            body,
            text='Save a copy of approved artwork in the album folder',
            variable=self.save_album_artwork_file,
        ).pack(anchor='w', pady=(8, 0))
        ttk.Label(
            body,
            text='When enabled, Approve + Embed also writes a target-size baseline JPEG such as cover.jpg beside the music files.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 10))
        ttk.Checkbutton(
            body,
            text='Show warning before embedding lower-confidence artwork',
            variable=self.warn_before_low_confidence_embed,
        ).pack(anchor='w')
        ttk.Label(
            body,
            text='When enabled, the app asks before embedding candidates marked Weak or with quality warnings. Turn this off for faster reviewing.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 10))

        ttk.Checkbutton(
            body,
            text='Convert approved artwork to target-size non-progressive JPEG',
            variable=self.resize_approved_artwork,
        ).pack(anchor='w')
        ttk.Label(
            body,
            text='When enabled, approved artwork is converted to a standard non-progressive JPEG and resized to the Preferred / target artwork size above before embedding or saving. Smaller artwork is converted but not enlarged.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 10))
        ttk.Checkbutton(
            body,
            text='After embedding, verify all files before marking Good',
            variable=self.verify_after_embed,
        ).pack(anchor='w')
        ttk.Label(
            body,
            text='When enabled, Approve + Embed re-checks every supported track. Albums are marked Good only when every file has target-size, square, baseline-JPEG artwork.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 0))

        ttk.Separator(body).pack(fill='x', pady=(16, 12))
        ttk.Label(body, text='Review Layout', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        density_row = ttk.Frame(body)
        density_row.pack(fill='x', pady=(8, 0))
        ttk.Label(density_row, text='Density:').pack(side='left')
        ttk.Combobox(density_row, textvariable=self.ui_density_var, values=('Comfortable', 'Compact'), state='readonly', width=14).pack(side='left', padx=(8, 0))
        ttk.Label(body, text='Compact reduces candidate row height and details spacing. Comfortable keeps more breathing room.', foreground='gray', wraplength=560).pack(anchor='w', pady=(2, 0))

        ttk.Separator(body).pack(fill='x', pady=(16, 12))
        ttk.Label(body, text='NAS / Synology Worker', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Checkbutton(
            body,
            text='Use NAS worker for embed / convert / deep-check jobs when path mapping matches',
            variable=self.nas_worker_enabled,
        ).pack(anchor='w', pady=(8, 0))
        ttk.Label(
            body,
            text='Runs heavy file writes on the Synology via Docker/Container Manager, avoiding slow tag rewrites over SMB/VPN. Leave disabled until the worker container is installed.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(2, 8))
        nas_grid = ttk.Frame(body)
        nas_grid.pack(fill='x')
        nas_grid.columnconfigure(1, weight=1)
        def add_nas_row(row, label, var, show=None):
            ttk.Label(nas_grid, text=label).grid(row=row, column=0, sticky='w', pady=2, padx=(0, 8))
            ttk.Entry(nas_grid, textvariable=var, show=show or '').grid(row=row, column=1, sticky='ew', pady=2)
        add_nas_row(0, 'Worker URL:', self.nas_worker_url)
        add_nas_row(1, 'API token:', self.nas_worker_token, show='•')
        add_nas_row(2, 'Mac path prefix:', self.nas_worker_local_prefix)
        add_nas_row(3, 'Worker path prefix:', self.nas_worker_remote_prefix)
        add_nas_row(4, 'Timeout seconds:', self.nas_worker_timeout)
        ttk.Label(
            body,
            text='Example mapping: /Volumes/Music on the Mac → /music inside the container. After replacing worker files on Synology, rebuild/recreate the Docker project; do not only restart it. Worker files are included in app Resources/app/artwork_manager_app/nas_worker.',
            foreground='gray', wraplength=560,
        ).pack(anchor='w', pady=(6, 0))
        nas_btns = ttk.Frame(body)
        nas_btns.pack(fill='x', pady=(8, 0))
        ttk.Button(nas_btns, text='Test NAS Worker', command=self.test_nas_worker).pack(side='left')
        ttk.Button(nas_btns, text='Worker Status', command=self.show_nas_worker_status).pack(side='left', padx=(8, 0))
        ttk.Button(nas_btns, text='Open Worker Files', command=lambda: open_path(APP_DIR / 'nas_worker')).pack(side='left', padx=(8, 0))

        ttk.Separator(body).pack(fill='x', pady=(16, 12))
        ttk.Label(body, text='Storage / Cleanup', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        self.storage_var = tk.StringVar(value=self.storage_summary_text())
        ttk.Label(body, textvariable=self.storage_var, foreground='#555555', wraplength=560).pack(anchor='w', pady=(4, 8))
        cleanup_grid = ttk.Frame(body)
        cleanup_grid.pack(fill='x')
        cleanup_grid.columnconfigure(0, weight=1)
        cleanup_grid.columnconfigure(1, weight=1)
        ttk.Button(cleanup_grid, text='Trash temporary artwork', command=self.trash_temp_artwork).grid(row=0, column=0, sticky='ew', padx=(0, 4), pady=2)
        ttk.Button(cleanup_grid, text='Trash handled temp artwork', command=self.trash_handled_temp).grid(row=0, column=1, sticky='ew', padx=(4, 0), pady=2)
        ttk.Button(cleanup_grid, text='Trash approved copies', command=self.trash_approved_copies).grid(row=1, column=0, sticky='ew', padx=(0, 4), pady=2)
        ttk.Button(cleanup_grid, text='Open app data folder', command=lambda: open_path(DATA_DIR)).grid(row=1, column=1, sticky='ew', padx=(4, 0), pady=2)
        ttk.Label(body, text='Cleanup moves app-managed artwork files to the macOS Trash. It does not remove embedded artwork from music files.', foreground='gray', wraplength=560).pack(anchor='w', pady=(6, 0))

        self.result = tk.StringVar(value='')
        ttk.Label(body, textvariable=self.result).pack(anchor='w', pady=(12, 0))
        buttons = ttk.Frame(body)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text='Save', command=self.save).pack(side='right', padx=4)
        ttk.Button(buttons, text='Test Discogs Connection', command=self.test).pack(side='right', padx=4)
        ttk.Button(buttons, text='Cancel', command=self.destroy).pack(side='right', padx=4)
        self.transient(master.root)
        self.grab_set()

    def storage_summary_text(self):
        return (
            f'Temporary candidates: {folder_size_text(TEMP_DIR)}   ·   '
            f'Manual imports: {folder_size_text(IMPORT_DIR)}   ·   '
            f'Approved copies: {folder_size_text(APPROVED_DIR)}'
        )

    def refresh_storage_summary(self):
        try:
            self.storage_var.set(self.storage_summary_text())
        except Exception:
            pass

    def trash_temp_artwork(self):
        if messagebox.askyesno('Trash temporary artwork?', 'Move all app-managed temporary candidate artwork and copied manual imports to the Trash?\n\nThis does not remove embedded artwork from music files.', parent=self):
            removed = self.master_app.trash_all_temporary_artwork(confirm=False)
            self.result.set(f'Trashed {removed} temporary/import artwork file(s).')
            self.refresh_storage_summary()

    def trash_handled_temp(self):
        removed = self.master_app.clear_handled_temporary_artwork(confirm=False)
        self.result.set(f'Trashed {removed} handled temporary/import artwork file(s).')
        self.refresh_storage_summary()

    def trash_approved_copies(self):
        removed = self.master_app.trash_approved_artwork_copies(confirm=False)
        self.result.set(f'Trashed {removed} approved artwork copy file(s).')
        self.refresh_storage_summary()

    def test_nas_worker(self):
        try:
            settings = self.current()
            result = check_worker(settings)
        except Exception as exc:
            self.result.set(f'NAS worker test failed: {exc}')
            messagebox.showerror('NAS worker test failed', str(exc), parent=self)
            return
        roots = ', '.join(result.get('music_roots') or [])
        version = result.get('version') or 'unknown'
        build = result.get('worker_build') or 'unknown'
        api = result.get('api') or 'unknown'
        compat = result.get('compatibility') or {}
        timing = result.get('_request_duration_seconds')
        timing_txt = f' · {timing:.2f}s' if isinstance(timing, (int, float)) else ''
        busy_txt = ' · busy' if result.get('busy') else ' · idle'
        lines = [f'NAS worker OK: {version}{busy_txt}{timing_txt}', f'Build/API: {build} / {api}']
        if roots:
            lines.append(f'Music roots: {roots}')
        if not compat.get('ok', False):
            warn = f'NAS worker needs update: {compat.get("message", "incompatible worker")}.\n\n{worker_update_hint()}'
            self.result.set(warn)
            messagebox.showerror('NAS worker needs update', warn, parent=self)
            return

        # Optional exact path self-test for the album currently selected in the main window.
        active = None
        try:
            active = self.master_app.active_album_info()
        except Exception:
            active = None
        album_path = (active or {}).get('album_path') or ''
        if album_path:
            lines.append('')
            lines.append('Selected album path test:')
            try:
                mapped = map_album_path_to_worker(album_path, settings)
            except Exception:
                mapped = ''
            lines.append(f'Mac path: {album_path}')
            lines.append(f'Worker path: {mapped or "not mapped"}')
            if mapped:
                try:
                    path_info = worker_path_check(album_path, settings)
                    exists = 'exists' if path_info.get('exists') else 'missing'
                    readable = 'readable' if path_info.get('readable') else 'not readable'
                    writable = 'writable' if path_info.get('writable') else 'not writable'
                    music_n = path_info.get('supported_music_file_count')
                    lines.append(f'Result: {exists}, {readable}, {writable} · {music_n} supported music file(s)')
                except Exception as exc:
                    lines.append(f'Result: exact worker path check unavailable or failed: {exc}')
                    lines.append('Core worker compatibility is OK; rebuild the bundled worker if you want the new exact album path self-test.')
        msg = '\n'.join(lines)
        self.result.set(lines[0])
        messagebox.showinfo('NAS worker OK', msg, parent=self)

    def show_nas_worker_status(self):
        try:
            settings = self.current()
            result = worker_status(settings)
        except Exception as exc:
            self.result.set(f'NAS worker status failed: {exc}')
            messagebox.showerror('NAS worker status failed', str(exc), parent=self)
            return
        version = result.get('version') or 'unknown'
        build = result.get('worker_build') or 'unknown'
        api = result.get('api') or 'unknown'
        compat = result.get('compatibility') or {}
        uptime = int(result.get('uptime_seconds') or 0)
        busy = 'busy' if result.get('busy') else 'idle'
        active = result.get('active_jobs') or []
        recent = result.get('recent_jobs') or []
        lines = [f'{version}', f'Build/API: {build} / {api}', f'Status: {busy}', f'Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s']
        if not compat.get('ok', True):
            lines.append('')
            lines.append(f'Warning: {compat.get("message", "incompatible worker")}')
            lines.append(worker_update_hint())
        fs = result.get('filesystem') if isinstance(result.get('filesystem'), dict) else None
        if fs:
            lines.append('')
            lines.append('Filesystem:')
            for label, item in fs.items():
                items = item if isinstance(item, list) else [item]
                for idx, entry in enumerate(items, 1):
                    if isinstance(entry, dict):
                        exists = 'ok' if entry.get('exists') else 'missing'
                        writable = 'writable' if entry.get('writable') else 'not writable'
                        path = entry.get('path') or label
                        item_label = f'{label} {idx}' if isinstance(item, list) and len(item) > 1 else label
                        lines.append(f'• {item_label}: {exists}, {writable} — {path}')
        if active:
            lines.append('')
            lines.append('Active job(s):')
            for job in active[:5]:
                lines.append(f'• {job.get("kind", "job")} · {job.get("label", "album")} · started {job.get("started_at", "")}')
        if recent:
            lines.append('')
            lines.append('Recent job(s):')
            for job in recent[:8]:
                ok = 'OK' if job.get('ok') else 'Failed'
                dur = job.get('duration_seconds')
                dur_txt = f'{dur:.1f}s' if isinstance(dur, (int, float)) else '?s'
                extra = ''
                if job.get('updated') is not None and job.get('total') is not None:
                    extra = f' · {job.get("updated")}/{job.get("total")} files'
                elif job.get('checked_files') is not None:
                    extra = f' · {job.get("checked_files")} checked'
                lines.append(f'• {ok} · {job.get("kind", "job")} · {dur_txt}{extra} · {job.get("label", "album")}')
        if not active and not recent:
            lines.append('')
            lines.append('No worker jobs have been recorded since the container started.')
        msg = '\n'.join(lines)
        self.result.set(f'NAS worker status: {busy} · {len(recent)} recent job(s)')
        messagebox.showinfo('NAS worker status', msg, parent=self)

    def provider_label(self, key):
        return {
            'deezer': 'Deezer',
            'itunes': 'iTunes / Apple Artwork',
            'musicbrainz': 'MusicBrainz / Cover Art Archive',
            'discogs': 'Discogs',
            'fanarttv': 'fanart.tv',
        }.get(key, key)

    def refresh_provider_order_list(self):
        self.provider_order_list.delete(0, 'end')
        for key in self.provider_order_keys:
            self.provider_order_list.insert('end', self.provider_label(key))
        if self.provider_order_keys:
            self.provider_order_list.selection_clear(0, 'end')
            self.provider_order_list.selection_set(0)

    def move_provider_up(self):
        sel = self.provider_order_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx <= 0:
            return
        self.provider_order_keys[idx - 1], self.provider_order_keys[idx] = self.provider_order_keys[idx], self.provider_order_keys[idx - 1]
        self.refresh_provider_order_list()
        self.provider_order_list.selection_set(idx - 1)

    def move_provider_down(self):
        sel = self.provider_order_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.provider_order_keys) - 1:
            return
        self.provider_order_keys[idx + 1], self.provider_order_keys[idx] = self.provider_order_keys[idx], self.provider_order_keys[idx + 1]
        self.refresh_provider_order_list()
        self.provider_order_list.selection_set(idx + 1)

    def _int_value(self, var, name, minimum, maximum):
        raw = var.get().strip()
        try:
            value = int(raw)
        except Exception:
            raise ValueError(f'{name} must be a whole number.')
        if value < minimum or value > maximum:
            raise ValueError(f'{name} must be between {minimum} and {maximum}.')
        return value

    def current(self):
        scan_min = self._int_value(self.scan_min_var, 'Queue cutoff', 1, 10000)
        fetch_min = self._int_value(self.fetch_min_var, 'Fetch cutoff', 1, 10000)
        preferred = self._int_value(self.preferred_size_var, 'Preferred size', 1, 10000)
        max_candidates = self._int_value(self.max_candidates_var, 'Max options per album', 1, 25)
        batch_count = self._int_value(self.batch_count_var, 'Batch search count', 1, 50)
        scan_worker_threads = self._int_value(self.scan_worker_threads_var, 'Scan album checks at once', 1, 32)
        nas_timeout = self._int_value(self.nas_worker_timeout, 'NAS worker timeout', 5, 7200)
        return {
            'discogs_token': self.discogs_token.get().strip(),
            'discogs_enabled': self.discogs_enabled.get(),
            'musicbrainz_enabled': self.mb_enabled.get(),
            'deezer_enabled': self.deezer_enabled.get(),
            'itunes_enabled': self.itunes_enabled.get(),
            'fanarttv_enabled': self.fanart_enabled.get(),
            'provider_order': list(self.provider_order_keys),
            'scan_min_artwork_size': scan_min,
            'fetch_min_artwork_size': fetch_min,
            'preferred_artwork_size': preferred,
            'target_size_match_mode': self.target_size_mode_var.get() if self.target_size_mode_var.get() in ('Relaxed', 'Strict') else 'Relaxed',
            'deep_scan_all_files': self.deep_scan_all_files.get(),
            'scan_worker_threads': scan_worker_threads,
            'max_candidates_per_album': max_candidates,
            'batch_search_count': batch_count,
            'save_approved_artwork_to_album_folder': self.save_album_artwork_file.get(),
            'warn_before_low_confidence_embed': self.warn_before_low_confidence_embed.get(),
            'resize_approved_artwork': self.resize_approved_artwork.get(),
            'verify_after_embed_before_good': self.verify_after_embed.get(),
            'nas_worker_enabled': self.nas_worker_enabled.get(),
            'nas_worker_url': self.nas_worker_url.get().strip(),
            'nas_worker_token': self.nas_worker_token.get().strip(),
            'nas_worker_local_prefix': self.nas_worker_local_prefix.get().strip(),
            'nas_worker_remote_prefix': self.nas_worker_remote_prefix.get().strip() or '/music',
            'nas_worker_timeout': nas_timeout,
            'ui_density': self.ui_density_var.get(),
        }

    def save(self):
        try:
            settings = self.current()
        except ValueError as exc:
            messagebox.showerror('Invalid settings', str(exc), parent=self)
            return
        old_density = (self.master_app.settings or {}).get('ui_density', 'Comfortable')
        save_settings(settings)
        self.master_app.settings = load_settings()
        new_density = self.master_app.settings.get('ui_density', 'Comfortable')
        self.result.set('Saved. Layout density applied.' if new_density != old_density else 'Saved. New scans/searches will use these settings.')
        self.master_app.log_msg('\nSettings saved. Layout density is now %s. New scans/searches will use the updated artwork size rules.\n' % new_density)
        if hasattr(self.master_app, 'apply_density_setting'):
            self.master_app.apply_density_setting(new_density)
        self.master_app.refresh_footer()
        if hasattr(self.master_app, 'refresh_size_sensitive_labels'):
            self.master_app.refresh_size_sensitive_labels()

    def test(self):
        try:
            save_settings(self.current())
        except ValueError as exc:
            messagebox.showerror('Invalid settings', str(exc), parent=self)
            return
        ok, msg = DiscogsProvider().test_connection()
        self.result.set(msg)
        self.master_app.discogs_last_test_ok = ok
        self.master_app.refresh_footer()
        if ok:
            messagebox.showinfo('Discogs connection', msg, parent=self)
        else:
            messagebox.showerror('Discogs connection failed', msg, parent=self)

class ReleaseSelectorWindow(tk.Toplevel):
    """Lets the user choose the exact MusicBrainz/Discogs release before artwork is pulled."""
    def __init__(self, master, album_info):
        super().__init__(master.root)
        self.title('Choose Correct Album / Release')
        self.geometry('760x520')
        self.minsize(640, 420)
        self.master_app = master
        self.album_info = album_info or {}
        self.results = []
        self.result_q = queue.Queue()
        self.artist_var = tk.StringVar(value=self.album_info.get('artist', ''))
        self.album_var = tk.StringVar(value=self.album_info.get('album', ''))
        self.status_var = tk.StringVar(value='Search for the exact album/release, then choose the one that matches your copy.')

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text='Choose the correct album/release', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Label(frm, text='Use this when automatic searching picked the wrong album artwork.', foreground='gray').pack(anchor='w', pady=(2, 10))

        fields = ttk.Frame(frm)
        fields.pack(fill='x')
        ttk.Label(fields, text='Artist:').grid(row=0, column=0, sticky='w')
        ttk.Entry(fields, textvariable=self.artist_var).grid(row=0, column=1, sticky='ew', padx=(8, 14))
        ttk.Label(fields, text='Album:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(fields, textvariable=self.album_var).grid(row=1, column=1, sticky='ew', padx=(8, 14), pady=(6, 0))
        fields.columnconfigure(1, weight=1)
        self.search_btn = ttk.Button(fields, text='Search Releases', command=self.search)
        self.search_btn.grid(row=0, column=2, rowspan=2, sticky='ns')

        ttk.Label(frm, textvariable=self.status_var).pack(anchor='w', pady=(10, 6))

        cols = ('provider', 'title', 'artist', 'date', 'country', 'extra')
        table_wrap = ttk.Frame(frm)
        table_wrap.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(table_wrap, columns=cols, show='headings', height=12)
        headings = [
            ('provider', 'Source', 90),
            ('title', 'Release / Album', 230),
            ('artist', 'Artist', 150),
            ('date', 'Year', 70),
            ('country', 'Country', 75),
            ('extra', 'Format / Score', 130),
        ]
        for col, title, width in headings:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor='w', stretch=(col in ('title', 'artist', 'extra')))
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(table_wrap, command=self.tree.yview)
        sb.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=sb.set)
        bind_vertical_scroll(self.tree)
        self.tree.bind('<Double-1>', lambda e: self.use_selected())

        bottom = ttk.Frame(frm)
        bottom.pack(fill='x', pady=(10, 0))
        ttk.Button(bottom, text='Cancel', command=self.destroy).pack(side='right')
        self.use_btn = ttk.Button(bottom, text='Use Selected Release', command=self.use_selected, state='disabled')
        self.use_btn.pack(side='right', padx=(0, 8))
        ttk.Button(bottom, text='Open Search in Browser', command=self.open_browser_search).pack(side='left')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.use_btn.configure(state='normal' if self.tree.selection() else 'disabled'))

        self.transient(master.root)
        self.lift()
        self.after(150, self.search)
        self.after(100, self._poll)

    def open_browser_search(self):
        artist = self.artist_var.get().strip()
        album = self.album_var.get().strip()
        if artist or album:
            webbrowser.open(f'https://www.google.com/search?q={quote(f"{artist} {album} deezer itunes apple discogs musicbrainz release")}')

    def _artist_from_mb(self, rel):
        try:
            return ' / '.join(ac.get('artist', {}).get('name', '') for ac in rel.get('artist-credit', []) if ac.get('artist'))
        except Exception:
            return ''

    def search(self):
        artist = self.artist_var.get().strip()
        album = self.album_var.get().strip()
        if not artist and not album:
            self.status_var.set('Enter an artist or album name first.')
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.results = []
        self.search_btn.configure(state='disabled')
        self.use_btn.configure(state='disabled')
        self.status_var.set('Searching MusicBrainz, Deezer, iTunes and Discogs releases…')
        settings = load_settings()

        def work():
            out = []
            try:
                if settings.get('musicbrainz_enabled', True):
                    mb = MusicBrainzProvider()
                    for rel in mb.search_releases(artist, album, limit=20):
                        out.append({'source': 'MusicBrainz', 'provider': mb, 'raw': rel})
                if settings.get('deezer_enabled', True):
                    dz = DeezerProvider()
                    for res in dz.search_albums(artist, album, limit=20):
                        out.append({'source': 'Deezer', 'provider': dz, 'raw': res})
                if settings.get('itunes_enabled', True):
                    it = ITunesProvider()
                    for res in it.search_albums(artist, album, limit=20):
                        out.append({'source': 'iTunes', 'provider': it, 'raw': res})
                if settings.get('discogs_enabled', True):
                    dg = DiscogsProvider()
                    if dg.token:
                        for res in dg.search(artist, album, limit=20):
                            out.append({'source': 'Discogs', 'provider': dg, 'raw': res})
                    else:
                        self.result_q.put(('LOG', 'Discogs skipped: no token saved.'))
                self.result_q.put(('RESULTS', out))
            except Exception as exc:
                self.result_q.put(('ERROR', str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _insert_result(self, idx, item):
        source = item['source']
        raw = item['raw']
        if source == 'MusicBrainz':
            title = raw.get('title', '')
            artist = self._artist_from_mb(raw)
            date = (raw.get('date') or '')[:4]
            country = raw.get('country') or ''
            extra = f'score {raw.get("score", "")}'.strip()
        elif source == 'Deezer':
            title = raw.get('title', '')
            artist = (raw.get('artist') or {}).get('name', '') if isinstance(raw.get('artist'), dict) else ''
            date = (raw.get('release_date') or '')[:4]
            country = ''
            extra = f'Deezer ID {raw.get("id", "")}'.strip()
        elif source == 'iTunes':
            title = raw.get('collectionName', '')
            artist = raw.get('artistName', '')
            date = (raw.get('releaseDate') or '')[:4]
            country = raw.get('country') or ''
            extra = f'iTunes ID {raw.get("collectionId", "")}'.strip()
        else:
            title = raw.get('title', '')
            artist = title.split(' - ', 1)[0] if ' - ' in title else ''
            date = str(raw.get('year') or '')
            country = raw.get('country') or ''
            fmt = ', '.join(raw.get('format') or [])
            extra = fmt
        self.tree.insert('', 'end', iid=f'rel_{idx}', values=(source, title, artist, date, country, extra))

    def _poll(self):
        try:
            while True:
                kind, payload = self.result_q.get_nowait()
                if kind == 'LOG':
                    self.master_app.log_msg('\n' + payload + '\n')
                elif kind == 'ERROR':
                    self.search_btn.configure(state='normal')
                    self.status_var.set(f'Error: {payload}')
                elif kind == 'RESULTS':
                    self.search_btn.configure(state='normal')
                    self.results = payload
                    for i, item in enumerate(self.results):
                        self._insert_result(i, item)
                    self.status_var.set(f'Found {len(self.results)} release match(es). Select the correct album, then use it.')
                elif kind == 'DOWNLOADED':
                    self.search_btn.configure(state='normal')
                    self.use_btn.configure(state='normal')
                    n = payload.get('count', 0)
                    if n:
                        self.status_var.set(f'Added {n} artwork option(s) from the selected release.')
                        self.master_app._pin_album_in_current_filter(self.album_info.get('album_key'), reason='selected release artwork')
                        self.master_app.load_album_for_review(self.album_info.get('album_key'))
                        self.master_app.refresh_queue_tab()
                        self.master_app.refresh_footer()
                    else:
                        self.status_var.set(f'Selected release had no artwork at or above the fetch minimum ({get_fetch_min_artwork_size()}px). Try another release or use Google/manual import.')
                elif kind == 'DOWNLOAD_ERROR':
                    self.search_btn.configure(state='normal')
                    self.use_btn.configure(state='normal')
                    self.status_var.set(f'Artwork download failed: {payload}')
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def use_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0].split('_', 1)[1])
            item = self.results[idx]
        except Exception:
            return
        artist = self.artist_var.get().strip() or self.album_info.get('artist', '')
        album = self.album_var.get().strip() or self.album_info.get('album', '')
        album_key = self.album_info.get('album_key')
        album_path = self.album_info.get('album_path')
        if not album_key:
            return
        self.search_btn.configure(state='disabled')
        self.use_btn.configure(state='disabled')
        self.status_var.set('Downloading artwork from the selected release…')
        provider = item['provider']
        raw = item['raw']

        def work():
            try:
                cands = provider.get_candidates_from_release(
                    artist, album, album_key, raw, max_candidates=8,
                    log=lambda s: self.master_app.q.put(('LOG', s + '\n')),
                )
                added = 0
                for c in cands:
                    c.update({'album_folder': album_path})
                    c['candidate_id'] = db.add_candidate(album_key, c)
                    self.master_app.q.put(('CAND', c))
                    added += 1
                if added:
                    db.set_album_status(album_key, 'candidate_found')
                    db.update_album_notes(album_key, {'state_evaluation': {'status': 'candidate_found', 'reason': f'{added} release artwork option(s) imported'}})
                self.result_q.put(('DOWNLOADED', {'count': added}))
            except Exception as exc:
                self.result_q.put(('DOWNLOAD_ERROR', str(exc)))

        threading.Thread(target=work, daemon=True).start()


class ImagePreviewWindow(tk.Toplevel):
    """In-app artwork preview with lightweight Quick Look-style navigation."""
    def __init__(self, master, *, image_path=None, image_bytes=None, title='Artwork Preview', candidate_navigation=False, album_navigation=False, preview_target='current'):
        super().__init__(master.root)
        self.master_app = master
        self.image_path = image_path
        self.image_bytes = image_bytes
        self.candidate_navigation = bool(candidate_navigation)
        self.album_navigation = bool(album_navigation)
        self.preview_target = preview_target or 'current'
        self.photo = None
        self.original = None
        self._load_token = 0
        self._loading = False
        self._global_preview_bind_ids = []
        self.title(title)
        self.geometry('820x760')
        self.minsize(520, 480)
        self.configure(background='#f2f2f2')
        if self.candidate_navigation:
            try:
                self.master_app.candidate_preview_windows.append(self)
            except Exception:
                pass
        self.protocol('WM_DELETE_WINDOW', self.close)

        # Keep preview chrome very quiet: album title on the left, only
        # essential controls on the right. Candidate/source/size details live in
        # the footer so the toolbar does not feel cramped or duplicated.
        toolbar = ttk.Frame(self, padding=(10, 8, 10, 4))
        toolbar.pack(fill='x')
        self.title_var = tk.StringVar(value=self._display_title(title))
        self.title_label = ttk.Label(toolbar, textvariable=self.title_var, style='Section.TLabel')
        self.title_label.pack(side='left', fill='x', expand=True)

        self.info_var = tk.StringVar(value='')
        self.info_label = ttk.Label(toolbar, textvariable=self.info_var, foreground='#666666')

        self.finder_btn = ttk.Button(toolbar, text='Finder', width=8, command=self.open_current_file)
        self.finder_btn.pack(side='right', padx=(6, 0))
        self.close_btn = ttk.Button(toolbar, text='Close', width=7, command=self.close)
        self.close_btn.pack(side='right', padx=(6, 0))
        if self.candidate_navigation:
            self.next_btn = ttk.Button(toolbar, text='›', width=3, command=lambda: self.navigate_candidate(1))
            self.prev_btn = ttk.Button(toolbar, text='‹', width=3, command=lambda: self.navigate_candidate(-1))
            # Packed on demand in _update_nav_buttons only when there is more
            # than one candidate. Showing disabled arrows for 1/1 adds clutter.
        else:
            self.prev_btn = self.next_btn = None

        self.canvas = tk.Canvas(self, background='white', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True, padx=10, pady=(0, 4))
        self.canvas.bind('<Configure>', lambda e: self.render())

        bottom = ttk.Frame(self, padding=(10, 0, 10, 8))
        bottom.pack(fill='x')
        self.footer_var = tk.StringVar(value='')
        ttk.Label(bottom, textvariable=self.footer_var, foreground='#555555').pack(side='left', fill='x', expand=True)

        # Bind keys on every preview widget, not only the Toplevel.  On macOS
        # a toolbar button often keeps keyboard focus after being clicked, and
        # plain Toplevel bindings do not see Left/Right in that case.  Widget
        # bindings return ``break`` so the button/focus class bindings do not
        # swallow the arrow-key navigation.
        self._bind_preview_keys(self)
        for widget in (toolbar, self.title_label, self.finder_btn, self.close_btn, self.canvas, bottom):
            self._bind_preview_keys(widget)
        if self.candidate_navigation:
            for widget in (self.prev_btn, self.next_btn):
                self._bind_preview_keys(widget)
        self._install_global_preview_key_bindings()
        self.refresh_header()
        self.load_image_async()
        self.transient(master.root)
        self.lift()
        self._keep_preview_active()

    def _bind_preview_keys(self, widget):
        if widget is None:
            return
        def close_preview(event=None):
            self.close()
            return 'break'
        widget.bind('<Escape>', close_preview, add='+')
        widget.bind('<space>', close_preview, add='+')
        widget.bind('<KeyPress-space>', close_preview, add='+')
        if self.candidate_navigation:
            widget.bind('<Left>', lambda e: self.navigate_candidate(-1), add='+')
            widget.bind('<Right>', lambda e: self.navigate_candidate(1), add='+')
            widget.bind('<Prior>', lambda e: self.navigate_candidate(-1), add='+')
            widget.bind('<Next>', lambda e: self.navigate_candidate(1), add='+')
        if self.album_navigation:
            widget.bind('<Up>', lambda e: self.navigate_album(-1), add='+')
            widget.bind('<Down>', lambda e: self.navigate_album(1), add='+')

    def _display_title(self, title, limit=82):
        text = str(title or 'Artwork Preview').replace('\n', ' ').strip()
        if len(text) <= limit:
            return text
        return text[:max(1, limit - 1)].rstrip() + '…'

    def _candidate_index_text(self):
        if not self.candidate_navigation:
            return ''
        total = len(self.master_app.current_candidates())
        if total:
            return f'{self.master_app.candidate_index + 1}/{total}'
        return ''

    def _candidate_info_text(self):
        c = self.master_app.current_candidate() if self.candidate_navigation else None
        if not c:
            return ''
        source = self.master_app._short_source_label(c) if hasattr(self.master_app, '_short_source_label') else (c.get('source') or '')
        bits = []
        if source:
            bits.append(source)
        w, h = c.get('width'), c.get('height')
        if w and h:
            bits.append(f'{w}×{h}')
        score = c.get('score')
        if score not in (None, ''):
            bits.append(f'{score}/100')
        if hasattr(self.master_app, '_candidate_target_result'):
            try:
                bits.append(self.master_app._candidate_target_result(c).replace('Meets target', 'Target met'))
            except Exception:
                pass
        idx = self._candidate_index_text()
        if idx:
            bits.insert(0, f'Candidate {idx}')
        return ' · '.join(str(x) for x in bits if x)

    def _preview_footer_text(self, file_size=None):
        parts = []
        info = self._candidate_info_text() if self.candidate_navigation else ''
        if info:
            parts.append(info)
        elif self.original is not None:
            parts.append(f'{self.original.width}×{self.original.height}')
        if file_size:
            parts.append(file_size)
        help_bits = []
        if self.candidate_navigation:
            help_bits.append('←/→ options')
        if self.album_navigation:
            help_bits.append('↑/↓ albums')
        help_bits.append('Space/Esc close')
        parts.append(' · '.join(help_bits))
        return ' · '.join(x for x in parts if x)

    def refresh_header(self):
        if self.candidate_navigation:
            c = self.master_app.current_candidate()
            if c:
                total = len(self.master_app.current_candidates())
                idx = self.master_app.candidate_index + 1
                album = f'{c.get("artist", "")} — {c.get("album", "")}'.strip(' —')
                title = f'{album} ({idx}/{total})' if album else f'Artwork option ({idx}/{total})'
                self.title(self._display_title(title, 120))
                self.title_var.set(self._display_title(title, 70))
            self.footer_var.set(self._preview_footer_text())
        else:
            self.title_var.set(self._display_title(self.title(), 70))
            self.footer_var.set(self._preview_footer_text())
        self.info_var.set('')
        try:
            self.finder_btn.configure(state='normal' if self.image_path else 'disabled')
        except Exception:
            pass
        self._update_nav_buttons()

    def _update_nav_buttons(self):
        if not self.candidate_navigation:
            return
        show = len(self.master_app.current_candidates()) > 1
        for btn in (self.prev_btn, self.next_btn):
            try:
                if show and not btn.winfo_ismapped():
                    btn.pack(side='right', padx=(6, 0), before=self.close_btn)
                elif not show and btn.winfo_ismapped():
                    btn.pack_forget()
                btn.configure(state='normal' if show else 'disabled')
            except Exception:
                pass

    def _preview_is_active(self):
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _keep_preview_active(self):
        """Keep keyboard focus on the preview while it is open.

        Tk/Aqua can return focus to the main window after a toolbar button is
        clicked or after the main review pane is refreshed.  The preview needs
        to behave like Quick Look: while it is visible, left/right should keep
        controlling the artwork in this window.
        """
        try:
            self.lift()
            try:
                self.grab_set()
            except Exception:
                pass
            self.focus_force()
            self.canvas.focus_set()
        except Exception:
            try:
                self.focus_set()
            except Exception:
                pass

    def _install_global_preview_key_bindings(self):
        """Capture preview navigation even if focus slips to the main app.

        Widget-level bindings are not enough on macOS because focus can move to
        the underlying Tk window during candidate refresh.  Use temporary
        bind_all handlers while the preview exists, then remove only our own
        handlers when it closes.
        """
        def bind(sequence, handler):
            try:
                bid = self.bind_all(sequence, handler, add='+')
                self._global_preview_bind_ids.append((sequence, bid))
            except Exception:
                pass
        bind('<Left>', lambda e: self.navigate_candidate(-1) if self.candidate_navigation else 'break')
        bind('<Right>', lambda e: self.navigate_candidate(1) if self.candidate_navigation else 'break')
        bind('<Prior>', lambda e: self.navigate_candidate(-1) if self.candidate_navigation else 'break')
        bind('<Next>', lambda e: self.navigate_candidate(1) if self.candidate_navigation else 'break')
        if self.album_navigation:
            bind('<Up>', lambda e: self.navigate_album(-1))
            bind('<Down>', lambda e: self.navigate_album(1))
        bind('<Escape>', lambda e: (self.close(), 'break')[1])
        bind('<space>', lambda e: (self.close(), 'break')[1])
        bind('<KeyPress-space>', lambda e: (self.close(), 'break')[1])

    def _remove_global_preview_key_bindings(self):
        for sequence, bid in list(getattr(self, '_global_preview_bind_ids', []) or []):
            try:
                # tkinter.unbind_all removes every handler for the sequence.
                # Use the private helper with funcid so we only remove the
                # temporary preview handler and leave app-wide shortcuts intact.
                self._root()._unbind(('bind', 'all', sequence), bid)
            except Exception:
                pass
        self._global_preview_bind_ids = []

    def close(self):
        self._remove_global_preview_key_bindings()
        try:
            self.grab_release()
        except Exception:
            pass
        self._load_token += 1
        if self.candidate_navigation:
            try:
                if self in self.master_app.candidate_preview_windows:
                    self.master_app.candidate_preview_windows.remove(self)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

    def open_current_file(self):
        if self.image_path:
            open_path(self.image_path)

    def navigate_candidate(self, delta):
        """Move through candidates from inside the open preview window.

        The preview should behave like Quick Look: left/right keeps cycling
        through every artwork option while the window stays open.  Do the
        wraparound explicitly here so navigation from the last item returns to
        the first item, and navigation from the first item returns to the last.
        """
        if not self.candidate_navigation:
            return 'break'
        cands = self.master_app.current_candidates()
        total = len(cands)
        if total <= 1:
            return 'break'
        try:
            current = int(getattr(self.master_app, 'candidate_index', 0) or 0)
        except Exception:
            current = 0
        self.master_app.candidate_index = (current + int(delta)) % total
        # Keep the main review surface and candidate list in sync with the
        # preview, but keep this preview window open and refresh its image
        # immediately afterwards.
        self.master_app.show_current_album()
        c = self.master_app.current_candidate()
        if c and c.get('image_path'):
            self.image_path = c.get('image_path')
            self.image_bytes = None
            self.refresh_header()
            self.load_image_async()
        self.after_idle(self._keep_preview_active)
        return 'break'

    def _active_album_preview_title(self):
        info = self.master_app.active_album_info() if hasattr(self.master_app, 'active_album_info') else None
        info = info or {}
        artist = str(info.get('artist') or '').strip()
        album = str(info.get('album') or '').strip()
        base = f'{artist} - {album}'.strip(' -')
        return f'{base} current embedded artwork' if base else 'Current embedded artwork'

    def _show_preview_message(self, message):
        self._load_token += 1
        self._loading = False
        self.original = None
        self.photo = None
        self.canvas.delete('all')
        self.canvas.create_text(
            max(20, self.canvas.winfo_width() // 2),
            max(20, self.canvas.winfo_height() // 2),
            anchor='center',
            text=message,
            fill='#555555',
            width=max(280, self.canvas.winfo_width() - 80),
        )
        self.footer_var.set(self._preview_footer_text())
        self.refresh_header()
        self.after_idle(self._keep_preview_active)

    def refresh_from_selected_album(self):
        """Refresh a current-art preview after queue selection changes."""
        if self.preview_target != 'current':
            return 'break'
        title = self._active_album_preview_title()
        try:
            self.title(self._display_title(title, 120))
            self.title_var.set(self._display_title(title, 70))
        except Exception:
            pass
        art = getattr(self.master_app, 'current_old_art_info', None) or {}
        path = art.get('image_path')
        data = art.get('bytes')
        if path or data:
            self.image_path = path
            self.image_bytes = data if not path else None
            self.refresh_header()
            self.load_image_async()
        else:
            self.image_path = None
            self.image_bytes = None
            self._show_preview_message('No embedded artwork for this album')
        return 'break'

    def navigate_album(self, delta):
        """Move the main queue selection while keeping the preview open.

        This lets the preview behave like a larger queue browser: in a Convert
        or Not Square filter, Up/Down can flick through each album's current
        artwork without closing the large preview window.
        """
        if not self.album_navigation:
            return 'break'
        try:
            self.master_app.move_queue_selection(int(delta or 0))
        except Exception:
            return 'break'
        return self.refresh_from_selected_album()

    def load_image_async(self):
        """Load/decode images off the Tk event loop so candidate switching stays responsive."""
        self._load_token += 1
        token = self._load_token
        path = self.image_path
        data = self.image_bytes
        self._loading = True
        self.canvas.delete('all')
        self.canvas.create_text(
            max(20, self.canvas.winfo_width() // 2),
            max(20, self.canvas.winfo_height() // 2),
            anchor='center',
            text='Loading preview…',
            fill='#555555',
        )
        self.footer_var.set('Loading…')
        self.info_var.set('')
        self._update_nav_buttons()

        def work():
            try:
                if data:
                    from io import BytesIO
                    img = Image.open(BytesIO(data))
                    size_text = f'{len(data) / 1024:.0f} KB'
                else:
                    img = Image.open(path)
                    size_text = file_kb(path) if path else ''
                img.load()
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                original = img.convert('RGBA') if img.mode not in ('RGB', 'RGBA') else img.copy()
                self.after(0, lambda: self._image_loaded(token, original, size_text))
            except Exception as exc:
                self.after(0, lambda: self._image_failed(token, exc))

        threading.Thread(target=work, daemon=True).start()

    def _image_loaded(self, token, img, size_text):
        if token != self._load_token or not self.winfo_exists():
            return
        self._loading = False
        self.original = img
        self.info_var.set('')
        self.footer_var.set(self._preview_footer_text(size_text))
        self._update_nav_buttons()
        self.render()
        self.after_idle(self._keep_preview_active)

    def _image_failed(self, token, exc):
        if token != self._load_token or not self.winfo_exists():
            return
        self._loading = False
        self.original = None
        self.info_var.set('')
        self.footer_var.set('Preview unavailable')
        self._update_nav_buttons()
        self.canvas.delete('all')
        self.canvas.create_text(20, 20, anchor='nw', text=f'Preview unavailable:\n{exc}', fill='#444444')

    # Kept for compatibility with any older internal calls.
    def load_image(self):
        self.load_image_async()

    def render(self):
        if self.original is None:
            return
        w = max(100, self.canvas.winfo_width() - 24)
        h = max(100, self.canvas.winfo_height() - 24)
        img = _high_quality_fit_image(self.original, (w, h), allow_upscale=False)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo, anchor='center')


class BackupRestoreWindow(tk.Toplevel):
    """Browse saved embed backups and restore a chosen approval."""
    def __init__(self, master):
        super().__init__(master.root)
        self.master_app = master
        self.title('Backup / Restore Browser')
        self.geometry('900x520')
        self.minsize(760, 420)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill='both', expand=True)
        ttk.Label(frm, text='Backup / Restore Browser', style='Section.TLabel').pack(anchor='w')
        ttk.Label(frm, text='Select a previous approval to restore the backed-up music files. Backups are created only when Backup was enabled.', foreground='#555555', wraplength=820).pack(anchor='w', pady=(2, 10))

        cols = ('date', 'album', 'files', 'backup_dir')
        body = ttk.Frame(frm)
        body.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(body, columns=cols, show='headings', height=14)
        for col, title, width in [
            ('date', 'Date', 150), ('album', 'Album key / folder', 260),
            ('files', 'Files', 70), ('backup_dir', 'Backup folder', 360)
        ]:
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=50, stretch=(col in ('album', 'backup_dir')))
        self.tree.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(body, command=self.tree.yview)
        sb.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=sb.set)
        bind_vertical_scroll(self.tree, self.tree)

        self.rows = []
        self.row_by_iid = {}
        self.load_rows()

        self.status_var = tk.StringVar(value=f'{len(self.rows)} backup approval(s) found.')
        ttk.Label(frm, textvariable=self.status_var, foreground='#555555').pack(anchor='w', pady=(8, 4))
        buttons = ttk.Frame(frm)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Restore Selected', command=self.restore_selected).pack(side='left')
        ttk.Button(buttons, text='Open Backup Folder', command=self.open_selected_backup).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.load_rows).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text='Close', command=self.destroy).pack(side='right')
        self.transient(master.root)
        self.lift()

    def load_rows(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows = list_embed_backups()
        self.row_by_iid = {}
        for i, row in enumerate(self.rows):
            iid = f'backup_{i}'
            self.row_by_iid[iid] = row
            album_text = row.get('album_key') or row.get('album_folder') or 'Unknown album'
            files_txt = f'{row.get("backup_count", 0)} / {row.get("updated", 0)}'
            self.tree.insert('', 'end', iid=iid, values=(row.get('created_at', ''), album_text, files_txt, row.get('backup_dir', '')))
        if hasattr(self, 'status_var'):
            self.status_var.set(f'{len(self.rows)} backup approval(s) found.')

    def selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.row_by_iid.get(sel[0])

    def open_selected_backup(self):
        row = self.selected_row()
        if row and row.get('backup_dir'):
            open_path(row.get('backup_dir'))

    def restore_selected(self):
        row = self.selected_row()
        if not row:
            messagebox.showinfo('Restore backup', 'Select a backup entry first.', parent=self)
            return
        if not messagebox.askyesno('Restore selected backup?', 'Restore backed-up music files for the selected approval? This replaces the current files with the saved backup copies.', parent=self):
            return
        res = restore_embed_history(row.get('history_id'))
        failed = res.get('failed') or []
        self.status_var.set(f'Restored {res.get("restored", 0)} file(s). Failed: {len(failed)}')
        self.master_app.log_msg(f'\nBackup restore: restored {res.get("restored", 0)} file(s), failed {len(failed)}.\n')
        messagebox.showinfo('Restore complete', f'Restored: {res.get("restored", 0)}\nFailed: {len(failed)}', parent=self)


class App:
    def __init__(self, root):
        self.root = root
        root.title(f'Artwork Review Manager — {BUILD_VERSION.split("—")[0].strip().replace("Build ", "v")}')
        try:
            icon_path = APP_DIR / 'assets' / 'app_icon.png'
            if icon_path.exists():
                icon_img = Image.open(icon_path).convert('RGBA')
                icon_img.thumbnail((256, 256))
                self.app_icon_photo = ImageTk.PhotoImage(icon_img)
                root.iconphoto(True, self.app_icon_photo)
        except Exception:
            pass
        self.settings = load_settings()
        layout = self.settings.get('layout') if isinstance(self.settings.get('layout'), dict) else {}
        self.layout_settings = layout
        # 3.81: allow the queue/work list to sit on the left like a native sidebar,
        # with the review/artwork pane on the right. Default to the new layout for
        # this build, while keeping a Tools toggle to switch back.
        self.queue_left_layout = bool(layout.get('queue_left_layout', True)) if isinstance(layout, dict) else True

        # Fit comfortably on the built-in Mac display, but remember the user's
        # preferred geometry/column sizes so HDMI displays can use the extra width.
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        default_w = min(max(1180, int(screen_w * 0.94)), 1700)
        default_h = min(max(680, screen_h - 85), 980)
        saved_geometry = layout.get('geometry') or ''
        saved_match = re.match(r'^(\d+)x(\d+)([+-]\d+)([+-]\d+)$', saved_geometry)
        if saved_match:
            win_w = max(1180, min(int(saved_match.group(1)), max(screen_w + 600, 1180)))
            win_h = max(640, min(int(saved_match.group(2)), max(screen_h + 300, 640)))
            x = max(-50, min(int(saved_match.group(3)), max(screen_w - 120, 20)))
            y = max(0, min(int(saved_match.group(4)), max(screen_h - 80, 35)))
            root.geometry(f'{win_w}x{win_h}+{x}+{y}')
        else:
            win_w, win_h = default_w, default_h
            root.geometry(f'{win_w}x{win_h}+20+35')
        root.minsize(1180, 640)
        density_pref = self.settings.get('ui_density', 'Comfortable')
        # Density is an explicit user preference. Earlier builds also forced
        # compact mode on smaller windows, which made the Settings control feel
        # like it did nothing. Honour the saved setting directly, then apply
        # it live when Settings is saved.
        self.compact_ui = (density_pref == 'Compact')

        saved_right = int(layout.get('right_panel_w') or 0) if str(layout.get('right_panel_w') or '').isdigit() else 0
        # 4.55: modern MacBook Pro displays have enough pixels for a larger
        # review surface. Keep the queue readable, but bias default queue-left
        # layouts toward bigger current/candidate artwork previews.
        if self.queue_left_layout:
            target_right = min(760, max(600, int(win_w * 0.44)))
            right_cap = min(860, int(win_w * 0.54))
            # Older builds often saved a queue width near 52% of the window,
            # which makes the covers feel tiny on a 14-inch MacBook Pro. Treat
            # that old default as migratable while still preserving narrower or
            # deliberately custom queue widths.
            if saved_right and saved_right >= int(win_w * 0.50):
                saved_right = target_right
        else:
            target_right = min(840, max(620, int(win_w * 0.52)))
            right_cap = min(940, int(win_w * 0.60))
        if saved_right:
            self.right_panel_w = max(target_right, min(saved_right, right_cap))
        else:
            self.right_panel_w = target_right
        # 4.14: keep the two artwork canvases square without letting the
        # candidate-options column run off the right edge.  4.13 protected the
        # artwork a bit too aggressively and underestimated the real width of
        # the mini navigation buttons, scrollbars, paddings, and divider.  Use a
        # more conservative width budget in queue-left layout and cap preview
        # size so the option list remains visible on built-in displays.
        left_floor = (600 if self.compact_ui else 680) if self.queue_left_layout else (540 if self.compact_ui else 585)
        self.right_panel_w = max(560, min(self.right_panel_w, max(560, win_w - left_floor)))
        if self.queue_left_layout:
            self.candidate_list_w = 180 if self.compact_ui else 198
            review_gutter_w = 108
            min_art_px = 170 if self.compact_ui else 184
            max_art_cap = 285 if self.compact_ui else 320
        else:
            self.candidate_list_w = 165 if self.compact_ui else 185
            review_gutter_w = 96
            min_art_px = 165 if self.compact_ui else 178
            max_art_cap = 250 if self.compact_ui else 290
        height_based = max(min_art_px, min(max_art_cap, win_h - (465 if self.compact_ui else 475)))
        review_available_w = max(left_floor, win_w - self.right_panel_w - (42 if self.queue_left_layout else 24))
        width_based = int((review_available_w - self.candidate_list_w - review_gutter_w) / 2)
        self.art_px = int(max(145, min(height_based, max(min_art_px, width_based))))
        self.win_w = win_w
        self.win_h = win_h
        self._layout_save_after = None
        self._queue_search_after = None
        self._queue_refresh_after = None
        self._queue_refresh_preserve = False
        self._queue_eval_cache = {}
        self._log_trim_counter = 0
        self._max_visible_log_lines = 1200
        self.thumbnail_photo_cache = {}
        self.preview_photo_cache = {}

        self.q = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        restored_library = self.settings.get('last_library_path') or ''
        if not restored_library:
            try:
                st = db.get_scan_state()
                restored_library = st.get('library_root') or '' if st else ''
            except Exception:
                restored_library = ''
        self.folder_var = tk.StringVar(value=restored_library)
        self.status_var = tk.StringVar(value='Choose your music folder, scan to build the queue, then find artwork options one album at a time.')
        self.status_display_var = tk.StringVar(value='')
        self.action_result_var = tk.StringVar(value='')
        try:
            self.status_var.trace_add('write', lambda *_: self._sync_top_status_label())
        except Exception:
            pass
        self.progress_text = tk.StringVar(value='')
        self.progress_var = tk.DoubleVar(value=0)
        saved_filter = layout.get('queue_filter') if isinstance(layout, dict) else None
        self.queue_filter_options = ('All', 'Needs Attention', 'Review', 'Missing', 'Needs Search', 'Not Square', 'Convert', 'Good')
        legacy_filter_map = {
            'All Active': 'Needs Attention',
            'Below Cutoff': 'Needs Search',
            'Review Artwork': 'Review',
            'Artwork Missing': 'Missing',
            'No Options Found': 'Needs Search',
            'Done / Good': 'Good',
            'All Okay': 'Good',
            'To Review': 'Review',
            'Needs Convert': 'Convert',
            'Needs Conversion': 'Convert',
            'No Options': 'Needs Search',
            'Ready to Review': 'Review',
            'Approved': 'Good',
            'Skipped': 'All',
            'Ignored': 'All',
            'Handled': 'All',
            'Searching': 'Needs Search',
            'Non Square': 'Not Square',
            'Non-square': 'Not Square',
            'Not square': 'Not Square',
        }
        saved_filter = legacy_filter_map.get(saved_filter, saved_filter)
        self.filter_var = tk.StringVar(value=saved_filter if saved_filter in self.queue_filter_options else 'All')
        self.queue_sort_options = ('Workflow Priority', 'Smallest Current Artwork', 'Largest Current Artwork', 'Most Options', 'Fewest Options', 'Artist A-Z', 'Album A-Z', 'Status')
        saved_sort = layout.get('queue_sort') if isinstance(layout, dict) else None
        self.queue_sort_var = tk.StringVar(value=saved_sort if saved_sort in self.queue_sort_options else 'Workflow Priority')
        self.queue_search_var = tk.StringVar(value=(layout.get('queue_search') or '') if isinstance(layout, dict) else '')
        self.dry_run = tk.BooleanVar(value=False)
        self.verbose_log_var = tk.BooleanVar(value=bool(self.settings.get('verbose_log', False)))
        self.backup = tk.BooleanVar(value=bool(self.settings.get('backup_before_embedding', False)))
        try:
            self.backup.trace_add('write', lambda *_: self._save_backup_preference())
        except Exception:
            pass
        self.scan_processed = 0
        self.scan_total = 0
        self.scan_active = False
        self.low_res_csv = None
        self.discogs_last_test_ok = None

        self.candidates = []
        self.groups = OrderedDict()
        self.album_keys = []
        self.current_album_key = None
        # A row is only kept outside the current filter after a deliberate
        # workflow action (for example Find Artwork changing Needs Search to
        # Review).  Plain filter changes must not pin the previously selected
        # album, otherwise the first visible row can appear to ignore the
        # selected filter.
        self.queue_sticky_album_key = None
        self.queue_sticky_filter = None
        self.queue_sticky_reason = ''
        self.candidate_index = 0
        self.current_img = None
        self.current_old_img = None
        self.current_old_art_info = None
        self.current_album_info = None
        self.current_art_cache = {}
        self.candidate_preview_windows = []
        self.thumb_refs = []
        self.queue_album_keys = {}
        self.find_worker = None
        self.find_stop_event = None
        self.find_job_counter = 0
        self.active_find_job_id = None
        self.active_find_album_key = None
        self.active_find_mode = None
        self.active_search_album_keys = set()
        self.active_search_album_status = {}
        self.active_search_album_labels = {}
        self.active_search_album_original_buckets = {}
        self.active_search_batch_total = 0
        self.canceled_find_jobs = set()
        # Guard against accidental action switching on macOS/Tk. A button click
        # can be pressed while it says Stop Search, then released after the
        # search finishes and the same primary-action button has redrawn as
        # Embed Selected. Keep action buttons briefly inert across search
        # start/stop/done transitions so one click can only perform one kind of
        # action.
        self.action_transition_until = 0.0
        self.action_transition_reason = ''
        self.embed_worker = None
        self.embed_job_counter = 0
        self.active_embed_job_id = None
        self.active_embed_album_key = None
        self.current_operation = 'idle'
        self.current_operation_label = ''
        self.convert_batch_active = False
        self.convert_batch_stop_after_current = False

        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.bind('<Configure>', self._on_root_configure, add='+')

        self._build()
        self.load_saved_queue(silent=True)
        self.refresh_queue_tab()
        self.restore_scan_progress()
        self._bind_shortcuts()
        self.log_msg(f'\nArtwork Review Manager — {BUILD_VERSION}\n')
        self._poll()


    def _on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._schedule_layout_save()

    def _schedule_layout_save(self):
        try:
            if self._layout_save_after:
                self.root.after_cancel(self._layout_save_after)
        except Exception:
            pass
        try:
            self._layout_save_after = self.root.after(900, self._save_layout_settings)
        except Exception:
            self._layout_save_after = None

    def _on_queue_search_changed(self):
        self._clear_queue_sticky_album()
        # Debounce search typing so large queues do not redraw/re-query on every
        # keystroke while the user is still entering text.
        try:
            if self._queue_search_after:
                self.root.after_cancel(self._queue_search_after)
        except Exception:
            pass
        try:
            self._queue_search_after = self.root.after(180, self._apply_queue_search_change)
        except Exception:
            self._queue_search_after = None
            self._apply_queue_search_change()

    def _apply_queue_search_change(self):
        self._queue_search_after = None
        self.refresh_queue_tab()
        self._load_focused_queue_album()
        self._schedule_layout_save()

    def _save_layout_settings(self):
        self._layout_save_after = None
        try:
            queue_columns = {}
            if hasattr(self, 'queue_tree'):
                for col in self.queue_tree['columns']:
                    queue_columns[col] = int(self.queue_tree.column(col, 'width'))
            save_settings({
                'last_library_path': clean_input_path(self.folder_var.get()) if hasattr(self, 'folder_var') and self.folder_var.get().strip() else '',
                'layout': {
                    'geometry': self.root.geometry(),
                    'queue_filter': self.filter_var.get() if hasattr(self, 'filter_var') else 'All',
                    'queue_sort': self.queue_sort_var.get() if hasattr(self, 'queue_sort_var') else 'Workflow Priority',
                    'queue_columns': queue_columns,
                    'right_panel_w': int(getattr(self, 'right_panel_w', 0) or 0),
                    'queue_search': self.queue_search_var.get() if hasattr(self, 'queue_search_var') else '',
                    'queue_left_layout': bool(getattr(self, 'queue_left_layout', True)),
                }
            })
        except Exception:
            pass

    def on_close(self):
        self._save_layout_settings()
        self.root.destroy()

    def _shortcut_allowed(self, event=None):
        widget = getattr(event, 'widget', None)
        try:
            cls = widget.winfo_class() if widget is not None else ''
        except Exception:
            cls = ''
        if cls in {'Entry', 'TEntry', 'Text', 'TCombobox', 'Combobox', 'Treeview'}:
            return False
        # Avoid hijacking Command/Control/Option-modified shortcuts.
        state = int(getattr(event, 'state', 0) or 0)
        if state & 0x0004 or state & 0x0008 or state & 0x0010 or state & 0x0080:
            return False
        return True

    def _widget_is_descendant_of(self, widget, parent):
        try:
            while widget is not None:
                if widget == parent:
                    return True
                widget = getattr(widget, 'master', None)
        except Exception:
            pass
        return False

    def _close_popups_on_outside_click(self, event=None):
        widget = getattr(event, 'widget', None)
        for attr, close_func in (
            ('queue_actions_popup', self.close_queue_actions_popup),
            ('tools_popup', self.close_tools_popup),
            ('queue_context_popup', self.close_queue_context_popup),
        ):
            popup = getattr(self, attr, None)
            if popup is None:
                continue
            try:
                exists = bool(popup.winfo_exists())
            except Exception:
                exists = False
            if not exists:
                continue
            if self._widget_is_descendant_of(widget, popup):
                continue
            close_func()
        return None

    def _begin_action_transition_guard(self, milliseconds=650, reason='action transition'):
        """Briefly freeze workflow actions while buttons redraw between states.

        Tk invokes button commands on mouse release. If a search completes while
        the mouse is down, the primary-action button can redraw from "Stop" to
        "Embed" before the release is delivered. This guard makes that release
        harmless and also blocks single-key action shortcuts during the same
        transition window.
        """
        try:
            until = time.monotonic() + max(0.05, float(milliseconds) / 1000.0)
            self.action_transition_until = max(float(getattr(self, 'action_transition_until', 0.0) or 0.0), until)
            self.action_transition_reason = reason or 'action transition'
        except Exception:
            return

        def refresh_when_clear(expected_until=until):
            try:
                if time.monotonic() >= float(getattr(self, 'action_transition_until', 0.0) or 0.0):
                    self.action_transition_reason = ''
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
            except Exception:
                pass

        try:
            self.root.after(int(milliseconds) + 80, refresh_when_clear)
        except Exception:
            pass

    def _action_transition_active(self):
        try:
            return time.monotonic() < float(getattr(self, 'action_transition_until', 0.0) or 0.0)
        except Exception:
            return False

    def _block_if_action_transition(self, action='Action'):
        if self._action_transition_active():
            try:
                reason = getattr(self, 'action_transition_reason', '') or 'the previous action'
                self.status_var.set(f'{action} paused: finishing {reason}.')
                self._set_action_result(f'{action} paused while the controls settle. Try again in a moment.')
            except Exception:
                pass
            return True
        return False

    def _run_shortcut(self, event, action):
        if not self._shortcut_allowed(event):
            return None
        if self._action_transition_active():
            return 'break'
        action_name = getattr(action, '__name__', '')
        if self.is_artwork_search_active() and action_name in {'approve', 'reject', 'skip', 'mark_current_good', 'ignore_album'}:
            try:
                self.status_var.set('Finish or stop the artwork search before using album action shortcuts.')
            except Exception:
                pass
            return 'break'
        action()
        return 'break'

    def _save_backup_preference(self):
        """Persist the main-window Backup checkbox without enabling it by default."""
        try:
            current = bool(self.backup.get())
            if isinstance(self.settings, dict):
                self.settings['backup_before_embedding'] = current
            save_settings({'backup_before_embedding': current})
        except Exception:
            pass

    def _bind_shortcuts(self):
        bindings = [
            ('<KeyPress-a>', self.approve), ('<KeyPress-A>', self.approve),
            ('<KeyPress-r>', self.reject), ('<KeyPress-R>', self.reject),
            ('<KeyPress-s>', self.skip), ('<KeyPress-S>', self.skip),
            ('<KeyPress-f>', self.find_more), ('<KeyPress-F>', self.find_more),
            ('<KeyPress-n>', self.next_work_item), ('<KeyPress-N>', self.next_work_item),
            ('<Return>', self.enlarge_candidate),
            ('<Up>', lambda: self.shift_candidate(-1)),
            ('<Down>', lambda: self.shift_candidate(1)),
        ]
        for seq, action in bindings:
            self.root.bind_all(seq, lambda e, a=action: self._run_shortcut(e, a), add='+')
        self.root.bind_all('<Tab>', self._toggle_review_keyboard_focus, add='+')
        # Some Tk/macOS builds do not define ISO_Left_Tab. Bind it only when
        # supported so the app still launches everywhere; Shift-Tab remains the
        # normal reverse-tab fallback.
        for seq in ('<ISO_Left_Tab>', '<Shift-Tab>'):
            try:
                self.root.bind_all(seq, self._toggle_review_keyboard_focus, add='+')
            except Exception:
                pass
        self.root.bind_all('<Command-f>', lambda e: self._focus_queue_search(), add='+')
        self.root.bind_all('<Control-f>', lambda e: self._focus_queue_search(), add='+')
        self.root.bind_all('<ButtonPress-1>', self._close_popups_on_outside_click, add='+')
        self.root.bind_all('<ButtonPress-2>', self._close_popups_on_outside_click, add='+')
        self.root.bind_all('<ButtonPress-3>', self._close_popups_on_outside_click, add='+')

    def _review_focus_context(self, widget=None):
        """Return 'queue', 'candidate', or None for keyboard navigation focus."""
        widget = widget or self.root.focus_get()
        if widget is None:
            return None
        try:
            if hasattr(self, 'queue_tree') and (widget == self.queue_tree or self._widget_is_descendant_of(widget, self.queue_tree)):
                return 'queue'
            if hasattr(self, 'cand_canvas') and (
                widget == self.cand_canvas
                or widget == getattr(self, 'cand_inner', None)
                or self._widget_is_descendant_of(widget, self.cand_canvas)
                or self._widget_is_descendant_of(widget, getattr(self, 'cand_inner', None))
            ):
                return 'candidate'
        except Exception:
            pass
        return None

    def _toggle_review_keyboard_focus(self, event=None):
        """Tab between the queue work list and candidate artwork option list."""
        widget = getattr(event, 'widget', None)
        try:
            cls = widget.winfo_class() if widget is not None else ''
        except Exception:
            cls = ''
        # Let text entry fields keep their normal Tab behaviour.
        if cls in {'Entry', 'TEntry', 'Text', 'TCombobox', 'Combobox'}:
            return None
        current = self._review_focus_context(widget)
        if current == 'candidate':
            self._focus_queue_list()
        else:
            self._focus_candidate_list()
        return 'break'

    def _focus_queue_list(self):
        try:
            self.notebook.select(self.queue_tab)
        except Exception:
            pass
        if not hasattr(self, 'queue_tree'):
            return 'break'
        try:
            children = list(self.queue_tree.get_children())
            if children:
                iid = self.queue_tree.focus() or (self.queue_tree.selection()[0] if self.queue_tree.selection() else children[0])
                if iid not in children:
                    iid = children[0]
                self.queue_tree.selection_set(iid)
                self.queue_tree.focus(iid)
                self.queue_tree.see(iid)
            self.queue_tree.focus_set()
        except Exception:
            pass
        return 'break'

    def _focus_candidate_list(self):
        if hasattr(self, 'cand_canvas'):
            try:
                if not getattr(self, 'candidate_row_widgets', None):
                    self.render_candidate_list()
                self._focus_selected_candidate_option()
                rows = getattr(self, 'candidate_row_widgets', [])
                if rows:
                    rows[max(0, min(self.candidate_index, len(rows) - 1))].focus_set()
                else:
                    self.cand_canvas.focus_set()
            except Exception:
                try:
                    self.cand_canvas.focus_set()
                except Exception:
                    pass
        return 'break'

    def _candidate_key_move(self, delta):
        self.shift_candidate(delta)
        self._focus_candidate_list()
        return 'break'

    def toggle_candidate_preview(self, event=None):
        """Spacebar Quick Look for the focused candidate: open once, press again to close."""
        # Close any existing candidate preview first.
        for win in list(getattr(self, 'candidate_preview_windows', []) or []):
            try:
                if bool(win.winfo_exists()):
                    win.close()
                    return 'break'
            except Exception:
                try:
                    win.close()
                    return 'break'
                except Exception:
                    pass
        self.enlarge_candidate()
        return 'break'

    def _focus_queue_search(self):
        try:
            self.notebook.select(self.queue_tab)
            self.queue_search_entry.focus_set()
            self.queue_search_entry.selection_range(0, 'end')
            return 'break'
        except Exception:
            return None

    # ---------- UI layout ----------
    def _build(self):
        self.style = ttk.Style()
        try:
            self.style.theme_use('aqua')
        except Exception:
            pass
        self.style.configure('Title.TLabel', font=('TkDefaultFont', 17, 'bold'))
        self.style.configure('Section.TLabel', font=('TkDefaultFont', 12, 'bold'))
        self.style.configure('Tiny.TLabel', foreground='#555555')
        self.style.configure('Good.TLabel', foreground='#149c31')
        self.style.configure('Danger.TLabel', foreground='#d00000')
        self.style.configure('ReviewTitle.TLabel', font=('TkDefaultFont', 12, 'bold'))
        self.style.configure('ReviewMeta.TLabel', foreground='#555555')
        # Small native-looking candidate navigation buttons between the preview
        # and option list. On Aqua this trims padding where supported.
        try:
            self.style.configure('MiniNav.TButton', padding=(0, 0), font=('TkDefaultFont', 9))
        except Exception:
            pass

        outer = ttk.Frame(self.root, padding=(12, 7, 12, 6))
        outer.pack(fill='both', expand=True)
        self.outer = outer

        top_path = ttk.Frame(outer)
        top_path.pack(fill='x')
        ttk.Label(top_path, text='1. Library folder', font=('TkDefaultFont', 11, 'bold')).pack(side='left', padx=(0, 8))
        ttk.Entry(top_path, textvariable=self.folder_var).pack(side='left', fill='x', expand=True)
        ttk.Button(top_path, text='Choose Folder…', width=18, command=self.choose).pack(side='left', padx=(10, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill='x', pady=(8, 4))
        ttk.Label(buttons, text='2. Build queue', font=('TkDefaultFont', 11, 'bold')).pack(side='left', padx=(0, 8))
        self.start_btn = ttk.Button(buttons, text='Scan / Resume', width=16, command=self.start)
        self.start_btn.pack(side='left', padx=(0, 8))
        self.stop_btn = ttk.Button(buttons, text='Stop Scan', width=12, command=self.stop_scan, state='disabled')
        self.stop_btn.pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='Refresh Queue', width=14, command=self.load_saved_queue).pack(side='left', padx=(0, 8))
        self.tools_btn = ttk.Button(buttons, text='Tools ▼', width=11, command=self.show_tools_popup)
        self.tools_btn.pack(side='left', padx=(0, 8))
        ttk.Checkbutton(buttons, text='Backup', variable=self.backup, command=self._save_backup_preference).pack(side='right')
        ttk.Checkbutton(buttons, text='Dry run', variable=self.dry_run).pack(side='right', padx=(0, 12))

        opts = ttk.Frame(outer)
        opts.pack(fill='x', pady=(0, 4))
        self.workflow_hint = ttk.Label(
            opts,
            text='3. Select an album, then choose an action. Selecting albums never downloads artwork.',
            foreground='#555555',
            wraplength=max(760, self.win_w - 120),
        )
        self.workflow_hint.pack(side='left', fill='x', expand=True)

        ttk.Separator(outer).pack(fill='x', pady=(0, 5))
        status_line = ttk.Frame(outer, height=58)
        status_line.pack(fill='x', pady=(0, 2))
        # Keep the top review/status row a fixed height. The selected-album
        # summary now lives here instead of inside the left review pane, so long
        # album/candidate text cannot push the artwork previews down or squeeze
        # the bottom details box.
        status_line.pack_propagate(False)

        status_left = ttk.Frame(status_line)
        status_left.pack(side='left', fill='both', expand=True)
        # Keep the status-dot object for existing status code, but do not show it.
        # The simplified review header reads cleaner without the extra coloured dot.
        self.status_dot = tk.Canvas(status_left, width=1, height=1, highlightthickness=0)
        self.status_dot_id = self.status_dot.create_oval(0, 0, 1, 1, fill='#26b53f', outline='')

        # Compact album summary strip. Keep this native and quiet: it gives the
        # currently selected album and workflow status only. Keep metadata in
        # the artwork captions/details box to avoid duplicating information.
        review_header = ttk.Frame(status_left, padding=(6, 0, 6, 0), height=54)
        review_header.pack(side='left', fill='both', expand=True, padx=(6, 8))
        review_header.pack_propagate(False)
        self.review_header = review_header
        self.review_title_raw = 'No album selected'
        self.review_meta_raw = 'Select an album in the queue to review.'
        self.review_title_var = tk.StringVar(value=self.review_title_raw)
        self.review_meta_var = tk.StringVar(value=self.review_meta_raw)
        # Use native ttk labels rather than tk.Label backgrounds. The previous
        # fixed single-line patch used explicit Tk backgrounds, which made the
        # review strip look like two white boxes on macOS. We still ellipsize
        # the text ourselves so the labels do not wrap or resize the layout.
        self.review_title_label = ttk.Label(
            review_header,
            textvariable=self.review_title_var,
            anchor='w',
            justify='left',
            font=('TkDefaultFont', 12, 'bold'),
            wraplength=640,
        )
        # Let the album title use the full two-line strip. The frame is fixed
        # height, so long album names can wrap without pushing the rest of the
        # UI down.
        self.review_title_label.pack(fill='both', expand=True, pady=(1, 0))
        self.review_meta_label = ttk.Label(
            review_header,
            textvariable=self.review_meta_var,
            anchor='w',
            justify='left',
            foreground='#555555',
            font=('TkDefaultFont', 11),
        )
        # Metadata/status lives elsewhere now; keep this label available for
        # code compatibility, but do not pack it into the visible strip.
        review_header.bind('<Configure>', self._on_review_header_configure)

        # Top-right app status: show one overall activity state (Idle, Searching,
        # Embedding, Scanning) rather than repeating the selected album title.
        status_right = ttk.Frame(status_line, width=max(440, min(720, self.right_panel_w - 24)), height=54)
        status_right.pack(side='right', fill='y', padx=(8, 0))
        status_right.pack_propagate(False)
        self.status_label = ttk.Label(
            status_right,
            textvariable=self.status_display_var,
            wraplength=0,
            justify='left',
            anchor='w',
            font=('TkDefaultFont', 12),
        )
        # Let the app status occupy the same two-line header height as the
        # album title on the left. When active progress is visible the status
        # sits above the progress bar; when idle it uses the whole strip.
        self.status_label.pack(fill='both', expand=True, pady=(0, 0))
        self.status_label.bind('<Configure>', lambda e: self._sync_top_status_label())
        self.progress_row = ttk.Frame(status_right)
        self.progress_text_label = ttk.Label(self.progress_row, textvariable=self.progress_text)
        self.progress_text_label.pack(side='left', padx=(0, 8))
        self.progress = ttk.Progressbar(self.progress_row, mode='determinate', variable=self.progress_var, maximum=100, length=220)
        self.progress.pack(side='left', fill='x', expand=True)
        self._set_progress_row_visible(False)

        main = ttk.Frame(outer)
        main.pack(fill='both', expand=True)
        self.main_pane = main
        main.rowconfigure(0, weight=1)

        # Review pane and queue pane can be swapped. Queue-left is now the
        # default trial layout: the queue becomes the navigation/sidebar, and
        # the artwork/review pane becomes the detail area on the right.
        if self.queue_left_layout:
            main.columnconfigure(0, weight=0, minsize=self.right_panel_w)
            main.columnconfigure(1, weight=0)
            main.columnconfigure(2, weight=1)
            left = ttk.Frame(main, padding=(8, 2, 0, 0))
            divider = ttk.Separator(main, orient='vertical')
            right = ttk.Frame(main, padding=(0, 2, 8, 0), width=self.right_panel_w)
            right.grid(row=0, column=0, sticky='ns')
            divider.grid(row=0, column=1, sticky='ns', padx=(6, 8))
            left.grid(row=0, column=2, sticky='nsew')
        else:
            main.columnconfigure(0, weight=1)
            main.columnconfigure(1, weight=0)
            main.columnconfigure(2, weight=0, minsize=self.right_panel_w)
            left = ttk.Frame(main, padding=(0, 2, 8, 0))
            divider = ttk.Separator(main, orient='vertical')
            right = ttk.Frame(main, padding=(8, 2, 0, 0), width=self.right_panel_w)
            left.grid(row=0, column=0, sticky='nsew')
            divider.grid(row=0, column=1, sticky='ns', padx=(2, 6))
            right.grid(row=0, column=2, sticky='ns')
        right.grid_propagate(False)
        right.pack_propagate(False)
        self.right_frame = right
        self.review_frame = left

        left_grid = ttk.Frame(left)
        # Keep artwork previews at their natural height so the details/status box
        # can use the remaining vertical space instead of being squeezed into a
        # short strip at the bottom of the window.
        left_grid.pack(fill='x', expand=False)
        # Three-column review area: current artwork, candidate artwork, candidate selector.
        # Current and candidate previews are equal square canvases so album art is not squashed.
        left_grid.columnconfigure(0, weight=1, uniform='art', minsize=self.art_px)
        left_grid.columnconfigure(1, weight=1, uniform='art', minsize=self.art_px)
        left_grid.columnconfigure(2, weight=0, minsize=8)
        left_grid.columnconfigure(3, weight=0, minsize=self.candidate_list_w)
        left_grid.rowconfigure(1, weight=1)

        current_head = ttk.Frame(left_grid)
        current_head.grid(row=0, column=0, sticky='n', pady=(0, 5))
        ttk.Label(current_head, text='Current', style='Section.TLabel').pack()

        candidate_head = ttk.Frame(left_grid)
        candidate_head.grid(row=0, column=1, sticky='n', pady=(0, 5))
        ttk.Label(candidate_head, text='Candidate', style='Section.TLabel').pack()
        # Option count is shown above the Artwork options list only; avoid
        # repeating it above the candidate preview.
        self.cand_nav_label = ttk.Label(candidate_head, text='', anchor='center')

        candidate_list_head = ttk.Frame(left_grid)
        candidate_list_head.grid(row=0, column=3, sticky='n', pady=(0, 5))
        ttk.Label(candidate_list_head, text='Options', style='Section.TLabel').pack()
        self.cand_count_label = ttk.Label(candidate_list_head, text='0 options', anchor='center')
        self.cand_count_label.pack()

        current_area = ttk.Frame(left_grid)
        current_area.grid(row=1, column=0, sticky='n', padx=(0, 10))
        self.old_label = tk.Canvas(current_area, width=self.art_px, height=self.art_px, bg='white', highlightthickness=1, highlightbackground='#cfcfcf')
        self.old_label.pack()
        self.old_label.bind('<Button-1>', lambda e: self.enlarge_current())
        self.old_label.bind('<Double-1>', lambda e: self.enlarge_current())
        self.old_size_var = tk.StringVar(value='—')
        ttk.Label(current_area, textvariable=self.old_size_var, font=('TkDefaultFont', 10)).pack(pady=(4, 0))
        self.reload_current_btn = ttk.Button(current_area, text='Reload Current Artwork', command=self.reload_current_artwork_from_disk)
        self.reload_current_btn.pack(fill='x', pady=(4, 0))

        candidate_area = ttk.Frame(left_grid)
        candidate_area.grid(row=1, column=1, sticky='n', padx=(0, 8))
        self.new_label = tk.Canvas(candidate_area, width=self.art_px, height=self.art_px, bg='white', highlightthickness=1, highlightbackground='#cfcfcf')
        self.new_label.pack()
        # Clicking the displayed candidate artwork is equivalent to pressing the + zoom button.
        self.new_label.bind('<Button-1>', lambda e: self.enlarge_candidate())
        self.new_label.bind('<Double-1>', lambda e: self.enlarge_candidate())
        self.new_size_var = tk.StringVar(value='—')
        ttk.Label(candidate_area, textvariable=self.new_size_var, font=('TkDefaultFont', 10)).pack(pady=(4, 0))

        # Compact secondary navigation sits in its own narrow column between the
        # candidate image and the option list, so the artwork preview and the
        # main action buttons do not lose width.
        option_col = ttk.Frame(left_grid, width=8, height=self.art_px)
        option_col.grid(row=1, column=2, sticky='n', padx=(0, 0))
        option_col.grid_propagate(False)
        self.prev_btn = ttk.Button(option_col, text='▲', width=1, style='MiniNav.TButton', command=lambda: self.shift_candidate(-1), state='disabled')
        self.prev_btn.pack(fill='x', pady=(0, 3))
        self.next_btn = ttk.Button(option_col, text='▼', width=1, style='MiniNav.TButton', command=lambda: self.shift_candidate(1), state='disabled')
        self.next_btn.pack(fill='x', pady=(0, 3))
        self.enlarge_btn = ttk.Button(option_col, text='+', width=1, style='MiniNav.TButton', command=self.enlarge_candidate)
        self.enlarge_btn.pack(fill='x')

        cand_list_box = ttk.Frame(left_grid)
        cand_list_box.grid(row=1, column=3, sticky='n', padx=(0, 0))
        self.cand_canvas = tk.Canvas(
            cand_list_box,
            width=self.candidate_list_w,
            height=self.art_px,
            bg='#f7f7f7',
            highlightthickness=1,
            highlightbackground='#d8d8d8'
        )
        self.cand_canvas.pack(side='left', fill='y', expand=False)
        self.cand_scroll = ttk.Scrollbar(cand_list_box, orient='vertical', command=self.cand_canvas.yview)
        self.cand_scroll.pack(side='right', fill='y')
        self.cand_canvas.configure(yscrollcommand=self.cand_scroll.set)
        self.cand_inner = tk.Frame(self.cand_canvas, bg='#f7f7f7')
        self.cand_window = self.cand_canvas.create_window((0, 0), window=self.cand_inner, anchor='nw')
        self.cand_inner.bind('<Configure>', lambda e: self.cand_canvas.configure(scrollregion=self.cand_canvas.bbox('all')))
        self.cand_canvas.bind('<Configure>', lambda e: self.cand_canvas.itemconfigure(self.cand_window, width=e.width))
        self._bind_option_scrolling(self.cand_canvas)
        self._bind_option_scrolling(self.cand_inner)
        for widget in (self.cand_canvas, self.cand_inner):
            widget.bind('<Up>', lambda e: self._candidate_key_move(-1), add='+')
            widget.bind('<Down>', lambda e: self._candidate_key_move(1), add='+')
            widget.bind('<space>', self.toggle_candidate_preview, add='+')
            widget.bind('<KeyPress-space>', self.toggle_candidate_preview, add='+')

        action_bar = ttk.Frame(left)
        action_bar.pack(fill='x', pady=(7, 5))
        # 4.10: one state-aware primary action keeps the common workflow out of
        # the increasingly capable Actions menu.  The button text/command is
        # refreshed whenever the selected album/candidate state changes.
        self.primary_action_btn = ttk.Button(action_bar, text='Find Artwork', command=self.run_primary_action, state='disabled')
        self.primary_action_btn.pack(fill='x', pady=(0, 5))
        action_row1 = ttk.Frame(action_bar)
        action_row1.pack(fill='x')
        for i in range(5):
            action_row1.columnconfigure(i, weight=1, uniform='review_actions')
        self.approve_btn = ttk.Button(action_row1, text='Embed (A)', command=self.approve, state='disabled')
        self.approve_btn.grid(row=0, column=0, sticky='ew', padx=(0, 3))
        self.reject_btn = ttk.Button(action_row1, text='Reject (R)', command=self.reject, state='disabled')
        self.reject_btn.grid(row=0, column=1, sticky='ew', padx=3)
        self.skip_btn = ttk.Button(action_row1, text='Skip (S)', command=self.skip, state='disabled')
        self.skip_btn.grid(row=0, column=2, sticky='ew', padx=3)
        self.mark_good_btn = ttk.Button(action_row1, text='Good', command=self.mark_current_good, state='disabled')
        self.mark_good_btn.grid(row=0, column=3, sticky='ew', padx=3)
        self.ignore_btn = ttk.Button(action_row1, text='Ignore', command=self.ignore_album, state='disabled')
        self.ignore_btn.grid(row=0, column=4, sticky='ew', padx=(3, 0))

        # Secondary artwork actions live in the Queue panel on the right; the
        # primary review decisions sit immediately beneath the artwork previews.
        self.more_btn = ttk.Menubutton(action_bar, text='More ▼')
        self.more_menu = tk.Menu(self.more_btn, tearoff=False)
        self.more_menu.add_command(label='Open Album Folder', command=self.open_album_folder)
        self.more_menu.add_command(label='Open New Artwork Option Preview', command=self.enlarge_candidate)
        self.more_btn.configure(menu=self.more_menu)

        # 4.00: one quiet action-result line keeps completion messages in a
        # consistent place, separate from the live top-right activity status.
        self.action_result_label = ttk.Label(
            left,
            textvariable=self.action_result_var,
            foreground='#555555',
            anchor='w',
            justify='left',
            font=('TkDefaultFont', 11),
        )
        # Keep this row hidden until there is an actual result to show. This
        # gives the details area back a little vertical room during ordinary
        # browsing, while still providing a consistent place for completed
        # action feedback.
        self.action_result_visible = False

        # 3.60 simplified review layout: remove the extra summary strip.
        # Current/candidate size is shown directly under the artwork, while
        # expanded source/match detail lives in the details box below.
        self.detail_summary_vars = {}

        self.details = tk.Text(
            left,
            height=(7 if self.compact_ui else 9),
            wrap='word',
            relief='flat',
            bd=0,
            highlightthickness=1,
            highlightbackground='#d5d5d5',
            font=('TkDefaultFont', 12),
            padx=8,
            pady=7,
            spacing1=1,
            spacing3=3,
        )
        self.details.tag_configure('section_heading', font=('TkDefaultFont', 12, 'bold'), spacing1=4, spacing3=2)
        self.details.tag_configure('section_body', font=('TkDefaultFont', 12), spacing1=1, spacing3=3)
        self.details.tag_configure('muted', foreground='#555555')
        # Grow this status/details area into the remaining free vertical space.
        # With the decision buttons above it, the bottom of the window is now a
        # dedicated status/details area rather than a mixed control bar.
        self.details.pack(fill='both', expand=True, pady=(0, 0))
        bind_vertical_scroll(self.details)

        self.notebook = ttk.Notebook(right, width=max(570, self.right_panel_w - 18))
        self.notebook.pack(fill='both', expand=True)
        queue_tab = ttk.Frame(self.notebook, padding=(5, 4, 5, 5))
        log_tab = ttk.Frame(self.notebook, padding=6)
        self.queue_tab = queue_tab
        self.log_tab = log_tab
        self.notebook.add(queue_tab, text='Queue')
        self.notebook.add(log_tab, text='Progress / History')

        queue_header = ttk.Frame(queue_tab)
        queue_header.pack(fill='x', pady=(0, 2))
        ttk.Label(queue_header, text='Queue', style='Section.TLabel').pack(side='left')
        ttk.Button(queue_header, text='Top', width=4, command=lambda: self.move_queue_selection_to_edge(first=True)).pack(side='left', padx=(8, 2))
        ttk.Button(queue_header, text='Bottom', width=6, command=lambda: self.move_queue_selection_to_edge(first=False)).pack(side='left', padx=(0, 8))
        self.queue_filter = ttk.Combobox(queue_header, textvariable=self.filter_var, values=self.queue_filter_options, state='readonly', width=16)
        self.queue_filter.pack(side='right')
        ttk.Label(queue_header, text='Show:').pack(side='right', padx=(8, 4))
        self.queue_filter.bind('<<ComboboxSelected>>', self._on_queue_filter_changed)

        search_row = ttk.Frame(queue_tab)
        search_row.pack(fill='x', pady=(0, 2))
        ttk.Label(search_row, text='Search:').pack(side='left')
        self.queue_search_entry = ttk.Entry(search_row, textvariable=self.queue_search_var)
        self.queue_search_entry.pack(side='left', fill='x', expand=True, padx=(6, 6))
        ttk.Button(search_row, text='Clear', width=5, command=lambda: self.queue_search_var.set('')).pack(side='left')
        self.queue_search_var.trace_add('write', lambda *_: self._on_queue_search_changed())

        sort_row = ttk.Frame(queue_tab)
        sort_row.pack(fill='x', pady=(0, 2))
        self.task_summary_var = tk.StringVar(value='Active: —')
        # Pack the sort controls on the right first so the summary text cannot
        # squeeze the combobox when counts or labels get longer.
        self.queue_sort = ttk.Combobox(sort_row, textvariable=self.queue_sort_var, values=self.queue_sort_options, state='readonly', width=15)
        self.queue_sort.pack(side='right')
        ttk.Label(sort_row, text='Sort:').pack(side='right', padx=(8, 4))
        ttk.Label(sort_row, textvariable=self.task_summary_var, foreground='#666666', style='Tiny.TLabel').pack(side='left', fill='x', expand=True)
        self.queue_sort.bind('<<ComboboxSelected>>', lambda e: (self._clear_queue_sticky_album(), self.refresh_queue_tab(), self._load_focused_queue_album(), self.refresh_queue_tab(), self._save_layout_settings()))

        self.queue_summary_var = tk.StringVar(value='Select an album to review. Searches run only when you ask.')
        ttk.Label(queue_tab, textvariable=self.queue_summary_var, foreground='#555555', style='Tiny.TLabel').pack(fill='x', pady=(0, 2))

        qcols = ('status', 'artist', 'album', 'current', 'candidates')
        queue_table_wrap = ttk.Frame(queue_tab)
        queue_table_wrap.pack(fill='both', expand=True)
        qxsb = ttk.Scrollbar(queue_table_wrap, orient='horizontal')
        qxsb.pack(side='bottom', fill='x')
        tree_area = ttk.Frame(queue_table_wrap)
        tree_area.pack(side='top', fill='both', expand=True)
        self.queue_tree = ttk.Treeview(tree_area, columns=qcols, show='headings', selectmode='extended', height=(10 if self.compact_ui else 12), xscrollcommand=qxsb.set)
        qxsb.configure(command=self.queue_tree.xview)
        saved_cols = self.layout_settings.get('queue_columns') if isinstance(self.layout_settings.get('queue_columns'), dict) else {}
        # Keep minimums small so columns can still be hand-compressed, but
        # start with a roomier Album column now that the queue owns slightly more
        # of the window.  Existing saved layouts are gently lifted toward these
        # defaults so upgrading does not preserve an unnecessarily cramped queue.
        min_widths = {'status': 62, 'artist': 55, 'album': 90, 'current': 64, 'candidates': 24}
        restore_caps = {'status': 132, 'artist': 175, 'album': 520, 'current': 120, 'candidates': 36}
        queue_available_w = max(540, int(self.right_panel_w) - 58)
        default_status_w = 106
        default_current_w = 104
        default_candidates_w = 30
        default_artist_w = max(118, min(160, int(queue_available_w * 0.21)))
        default_album_w = max(260, queue_available_w - default_status_w - default_artist_w - default_current_w - default_candidates_w)
        default_widths = {
            'status': default_status_w,
            'artist': default_artist_w,
            'album': default_album_w,
            'current': default_current_w,
            'candidates': default_candidates_w,
        }
        for col, title in [
            ('status', 'Status'), ('artist', 'Artist'), ('album', 'Album'),
            ('current', 'Size'), ('candidates', 'Opts')
        ]:
            width = default_widths.get(col, 80)
            saved_width = saved_cols.get(col) if isinstance(saved_cols, dict) else None
            try:
                if saved_width is not None:
                    saved_width = int(saved_width)
                    # If an older saved layout is narrower than the new default,
                    # lift it most of the way toward the new size.  If the user had
                    # made it wider, keep that preference up to the safety cap.
                    width = max(saved_width, int(width * 0.9))
                    width = min(width, restore_caps.get(col, width))
                width = max(int(width), min_widths.get(col, 24))
            except Exception:
                width = max(int(width), min_widths.get(col, 24))
            self.queue_tree.heading(col, text=title)
            self.queue_tree.column(col, width=width, minwidth=min_widths.get(col, 24), anchor='e' if col in ('current', 'candidates') else 'w', stretch=(col in ('artist', 'album')))
        self.queue_tree.tag_configure('review', background='#eef7ff')
        self.queue_tree.tag_configure('needs', background='#ffffff')
        self.queue_tree.tag_configure('noopts', background='#fff7e6')
        self.queue_tree.tag_configure('searching', background='#eaf3ff')
        self.queue_tree.tag_configure('done', foreground='#666666')
        # Keep the vertical scrollbar permanently visible. The previous pack order
        # let the expanding Treeview consume the whole row before the scrollbar was
        # allocated space, so on macOS it could disappear even though it existed.
        tree_area.columnconfigure(0, weight=1)
        tree_area.columnconfigure(1, weight=0, minsize=16)
        tree_area.rowconfigure(0, weight=1)
        self.queue_tree.grid(row=0, column=0, sticky='nsew')
        # Use a themed/native scrollbar here. The old classic Tk scrollbar was
        # always visible, but on macOS its thumb could feel unlike the rest of
        # the app and was unreliable to drag on some Tk builds. ttk.Scrollbar
        # follows the Aqua theme and handles normal click/drag/moveto behavior.
        qsb = ttk.Scrollbar(tree_area, orient='vertical', command=self.queue_tree.yview)
        qsb.grid(row=0, column=1, sticky='ns')
        self.queue_vscroll = qsb
        self.queue_tree.configure(yscrollcommand=qsb.set)
        bind_vertical_scroll(self.queue_tree, self.queue_tree, horizontal_target=self.queue_tree)
        self.queue_tree.bind('<<TreeviewSelect>>', self.select_queue_album)
        self.queue_tree.bind('<Double-1>', self.open_selected_queue_album)
        self.queue_tree.bind('<ButtonRelease-2>', self.show_queue_context_menu)
        self.queue_tree.bind('<ButtonRelease-3>', self.show_queue_context_menu)
        self.queue_tree.bind('<ButtonRelease-1>', lambda e: self._schedule_layout_save(), add='+')
        self.queue_tree.bind('<Up>', lambda e: self.move_queue_selection(-1), add='+')
        self.queue_tree.bind('<Down>', lambda e: self.move_queue_selection(1), add='+')
        self.queue_tree.bind('<Prior>', lambda e: self.move_queue_selection(-8), add='+')
        self.queue_tree.bind('<Next>', lambda e: self.move_queue_selection(8), add='+')
        self.queue_tree.bind('<Home>', lambda e: self.move_queue_selection_to_edge(first=True), add='+')
        self.queue_tree.bind('<End>', lambda e: self.move_queue_selection_to_edge(first=False), add='+')
        # Queue has focus during keyboard browsing; bind F here directly because
        # global single-letter shortcuts intentionally ignore Treeview widgets.
        self.queue_tree.bind('<KeyPress-f>', lambda e: (self.find_more(), 'break')[1], add='+')
        self.queue_tree.bind('<KeyPress-F>', lambda e: (self.find_more(), 'break')[1], add='+')
        self.queue_tree.bind('<KeyPress-n>', lambda e: self.next_work_item(), add='+')
        self.queue_tree.bind('<KeyPress-N>', lambda e: self.next_work_item(), add='+')

        queue_buttons = ttk.Frame(queue_tab)
        queue_buttons.pack(fill='x', pady=(5, 0))

        queue_row1 = ttk.Frame(queue_buttons)
        queue_row1.pack(fill='x')
        for i in range(3):
            queue_row1.columnconfigure(i, weight=1, uniform='queue_btns')
        self.find_btn = self.queue_download_btn = ttk.Button(queue_row1, text='Find Artwork (F)', command=self.find_more, state='disabled')
        self.queue_download_btn.grid(row=0, column=0, sticky='ew', padx=(0, 4))
        self.search_more_btn = ttk.Button(queue_row1, text='Search More', command=self.search_more, state='disabled')
        self.search_more_btn.grid(row=0, column=1, sticky='ew', padx=4)
        self.stop_find_btn = self.queue_stop_find_btn = ttk.Button(queue_row1, text='Stop Search', command=self.stop_artwork_search, state='disabled')
        self.queue_stop_find_btn.grid(row=0, column=2, sticky='ew', padx=(4, 0))

        queue_row2 = ttk.Frame(queue_buttons)
        queue_row2.pack(fill='x', pady=(4, 0))
        for i in range(3):
            queue_row2.columnconfigure(i, weight=1, uniform='queue_btns')
        self.import_btn = self.queue_import_btn = ttk.Button(queue_row2, text='Import Image…', command=self.manual, state='disabled')
        self.queue_import_btn.grid(row=0, column=0, sticky='ew', padx=(0, 4))
        self.google_btn = ttk.Button(queue_row2, text='Google Images', command=self.google, state='disabled')
        self.google_btn.grid(row=0, column=1, sticky='ew', padx=4)
        self.queue_actions_btn = ttk.Button(queue_row2, text='Actions ▼', command=self.show_queue_actions_menu, state='disabled')
        self.queue_actions_popup = None
        self.queue_actions_btn.grid(row=0, column=2, sticky='ew', padx=(4, 0))

        # Keep these attribute names for existing state-management code. The
        # actual commands now live in Actions so the queue panel stays two rows tall.
        self.release_btn = self.queue_release_btn = self.queue_actions_btn
        self.open_source_btn = self.queue_actions_btn
        self.find_next_btn = self.queue_actions_btn
        self.open_folder_btn = self.queue_actions_btn

        log_header = ttk.Frame(log_tab)
        log_header.pack(fill='x', pady=(0, 4))
        ttk.Label(log_header, text='Workflow Log', font=('TkDefaultFont', 11, 'bold')).pack(side='left')
        ttk.Label(log_header, text='Normal view shows completed actions only.', foreground='#666666', style='Tiny.TLabel').pack(side='left', padx=(8, 0))
        ttk.Checkbutton(log_header, text='Verbose', variable=self.verbose_log_var, command=self._toggle_verbose_log).pack(side='right')

        log_body = ttk.Frame(log_tab)
        log_body.pack(fill='both', expand=True)
        self.log = tk.Text(log_body, wrap='word', font=('Menlo', 10), relief='solid', bd=1, width=32)
        self.log.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(log_body, command=self.log.yview)
        sb.pack(side='right', fill='y')
        self.log.configure(yscrollcommand=sb.set)
        bind_vertical_scroll(self.log)

        footer = ttk.Frame(outer)
        self.provider_var = tk.StringVar(value='Providers: —')
        self.queue_var = tk.StringVar(value='Queue: —')
        self.last_saved_var = tk.StringVar(value='Saved: —')
        if not self.compact_ui:
            footer.pack(fill='x', pady=(6, 0))
            ttk.Label(footer, textvariable=self.provider_var).pack(side='left')
            ttk.Label(footer, textvariable=self.queue_var).pack(side='right', padx=(12, 0))
            ttk.Label(footer, textvariable=self.last_saved_var).pack(side='right', padx=(12, 0))

        self.toggle_review_controls(False)
        self.refresh_footer()

    def apply_density_setting(self, density=None):
        """Apply the Review Layout density preference without restarting.

        Density mostly affects row heights, details spacing, and review-list
        font sizing. It intentionally avoids moving/sizing the whole main
        window, but the visible controls update immediately when Settings is
        saved.
        """
        density = density or (self.settings or {}).get('ui_density', 'Comfortable')
        self.compact_ui = (density == 'Compact')
        try:
            if hasattr(self, 'details'):
                detail_font = ('TkDefaultFont', 11 if self.compact_ui else 12)
                self.details.configure(
                    height=(6 if self.compact_ui else 8),
                    font=detail_font,
                    padx=(6 if self.compact_ui else 8),
                    pady=(5 if self.compact_ui else 7),
                    spacing1=(0 if self.compact_ui else 1),
                    spacing3=(2 if self.compact_ui else 3),
                )
                self.details.tag_configure('section_heading', font=('TkDefaultFont', 11 if self.compact_ui else 12, 'bold'), spacing1=(2 if self.compact_ui else 4), spacing3=2)
                self.details.tag_configure('section_body', font=detail_font, spacing1=(0 if self.compact_ui else 1), spacing3=(2 if self.compact_ui else 3))
            if hasattr(self, 'queue_tree'):
                self.queue_tree.configure(height=(10 if self.compact_ui else 12))
            if hasattr(self, 'cand_canvas'):
                self.cand_canvas.configure(width=self.candidate_list_w, height=max(180, self.art_px))
            if hasattr(self, 'render_candidate_list'):
                self.render_candidate_list()
            if hasattr(self, 'display_candidate'):
                self.display_candidate()
        except Exception as exc:
            try:
                self.log_msg(f'\nCould not fully apply density setting immediately: {exc}\n')
            except Exception:
                pass


    def _primary_action_info(self, *, has_candidate=None, has_album=None):
        """Return (label, command, enabled) for the selected album's main next action."""
        try:
            if has_candidate is None:
                has_candidate = bool(self.current_candidate())
            if has_album is None:
                has_album = bool(self.active_album_info())
        except Exception:
            has_candidate = False
            has_album = False
        if self.is_artwork_search_active():
            try:
                stopping = self.active_find_job_id in getattr(self, 'canceled_find_jobs', set())
            except Exception:
                stopping = False
            if stopping:
                return 'Stopping…', lambda: None, False
            return 'Stop Search', self.stop_artwork_search, True
        if self._action_transition_active():
            return 'Please wait…', lambda: None, False
        if self.is_write_action_active():
            return 'Writing…', lambda: None, False
        if not has_album:
            return 'Select Album', lambda: None, False
        album = self.current_album_info or (db.get_album(self.current_album_key) if getattr(self, 'current_album_key', None) else None) or {}
        status, _reason = self._evaluate_queue_album(album)
        status = status or album.get('status') or ''
        if status == 'not_square_artwork':
            return 'Fix: Square Artwork', self.convert_embedded_artwork_to_baseline, True
        if status == 'incompatible_artwork':
            return 'Fix: Convert/Save', self.convert_embedded_artwork_to_baseline, True
        if has_candidate:
            return 'Fix: Embed Selected', self.approve, True
        if status in ('already_good', 'approved'):
            return 'Rework Album', self.rework_album, True
        if status in ('reviewed_skipped', 'ignored'):
            return 'Rework Album', self.rework_album, True
        return 'Fix: Find Artwork', self.find_more, True

    def update_primary_action_button(self, *, has_candidate=False, has_album=False):
        if not hasattr(self, 'primary_action_btn'):
            return
        label, _command, enabled = self._primary_action_info(has_candidate=has_candidate, has_album=has_album)
        try:
            self.primary_action_btn.configure(text=label, state=('normal' if enabled else 'disabled'))
        except Exception:
            pass

    def run_primary_action(self):
        if self._block_if_action_transition('Primary action'):
            return
        label, command, enabled = self._primary_action_info()
        if not enabled:
            return
        try:
            command()
        except Exception as exc:
            try:
                self.status_var.set(f'{label} failed: {exc}')
                self._set_action_result(f'{label} failed: {exc}')
            except Exception:
                pass

    # ---------- Data loading / rendering ----------
    def restore_scan_progress(self):
        state = db.get_scan_state()
        if state:
            self.scan_processed = int(state.get('processed_albums') or 0)
            self.scan_total = int(state.get('total_albums') or 0)
            self.update_progress_label()

    def refresh_footer(self):
        settings = load_settings()
        mb = 'MBZ ✓' if settings.get('musicbrainz_enabled', True) else 'MBZ off'
        deezer = 'Deezer ✓' if settings.get('deezer_enabled', True) else 'Deezer off'
        itunes = 'Apple ✓' if settings.get('itunes_enabled', True) else 'Apple off'
        discogs_enabled = settings.get('discogs_enabled', True)
        token = settings.get('discogs_token', '')
        if not discogs_enabled:
            discogs = 'Discogs off'
            token_txt = ''
        else:
            discogs = 'Discogs ✓'
            token_txt = ' · token missing' if not token else ''
        self.provider_var.set(f'Providers: {mb} · {deezer} · {itunes} · {discogs}{token_txt}')

        action_count = db.active_album_count()
        option_word = 'option' if len(self.candidates) == 1 else 'options'
        self.queue_var.set(f'Queue: {action_count} need action · {len(self.candidates)} {option_word}')
        st = db.get_scan_state()
        if st and st.get('updated_at'):
            stamp = str(st.get('updated_at') or '').replace('T', ' ')
            short = stamp[:16] if len(stamp) > 16 else stamp
            self.last_saved_var.set(f'Saved: {short}')

    def refresh_size_sensitive_labels(self):
        self.settings = load_settings()
        # The queue Actions popup is built on demand so its Search Next label
        # always reflects the current batch-search setting.
        self.refresh_queue_tab()
        if self.current_candidate():
            self.show_current_album()
        else:
            self.refresh_review_header()


    def friendly_status(self, status):
        mapping = {
            'candidate_found': 'Review Artwork',
            'needs_review': 'Needs Search',
            'missing_artwork': 'Artwork Missing',
            'incompatible_artwork': 'Convert',
            'not_square_artwork': 'Not Square',
            'no_candidate': 'No Options Found',
            'approved': 'Good',
            'reviewed_skipped': 'Skipped',
            'already_good': 'Good',
            'ignored': 'Ignored',
            'pending': 'Pending',
            'searching': 'Searching',
        }
        return mapping.get(status or '', (status or '').replace('_', ' ').title())

    def queue_status_label(self, status):
        mapping = {
            'candidate_found': 'Review',
            'needs_review': 'Needs Search',
            'missing_artwork': 'Missing',
            'incompatible_artwork': 'Convert',
            'not_square_artwork': 'Not Square',
            'no_candidate': 'No Options',
            'approved': 'Good',
            'reviewed_skipped': 'Skipped',
            'already_good': 'Good',
            'ignored': 'Ignored',
            'pending': 'Pending',
            'searching': 'Searching…',
        }
        return mapping.get(status or '', self.friendly_status(status))

    def display_queue_status(self, album):
        if album and album.get('album_key') in getattr(self, 'active_search_album_keys', set()):
            return 'searching'
        return album.get('status', '') if album else ''

    def _evaluate_queue_album(self, album):
        """Cached status/reason evaluation for one queue refresh.

        Queue drawing asks for the same album's bucket, label and tag several
        times. Cache the shared evaluator result by the row facts so large queue
        filters do not repeatedly parse notes JSON and recompute hierarchy.
        """
        if not album:
            return '', ''
        key = (
            album.get('album_key'),
            album.get('status'),
            album.get('width'),
            album.get('height'),
            album.get('candidate_count'),
            album.get('notes') or '',
        )
        cache = getattr(self, '_queue_eval_cache', None)
        if cache is None:
            cache = {}
            self._queue_eval_cache = cache
        if key in cache:
            return cache[key]
        try:
            value = evaluate_album_record(album, settings=getattr(self, 'settings', None))
        except Exception:
            value = (album.get('status', ''), '')
        cache[key] = value
        return value

    def display_queue_status_label(self, album):
        if not album:
            return ''
        status = self.display_queue_status(album)
        if status == 'searching':
            key = album.get('album_key')
            return getattr(self, 'active_search_album_status', {}).get(key) or self.queue_status_label('searching')
        evaluated_status, evaluated_reason = self._evaluate_queue_album(album)
        if evaluated_status and evaluated_status != status:
            status = evaluated_status
        label = self.queue_status_label(status)
        if status == 'not_square_artwork':
            return 'Not Square'
        if status == 'incompatible_artwork':
            # Keep the queue status column scannable.  The exact conversion
            # reason is shown in the details pane/action result when the row is
            # selected; putting it in every row made this column clip constantly.
            return 'Convert'
        if status == 'candidate_found':
            return 'Review'
        if status == 'already_good':
            return 'Good'
        if status == 'approved':
            return 'Good'
        return label

    def _compact_search_status(self, text, album_key=None):
        text = (text or '').strip()
        if not text:
            return 'Searching…'
        m = re.match(r'^Searching\s+.+?\s+\((\d+)/(\d+)\)…?$', text)
        if m:
            return f'Searching {m.group(1)}/{m.group(2)}'
        m = re.match(r'^Searching\s+(.+?)\s+for\s+', text)
        if m:
            provider = m.group(1).strip()
            return f'Searching {provider}…' if provider else 'Searching…'
        m = re.match(r'^(\d+)\s+artwork option\(s\) saved', text)
        if m:
            return f'Searching… {m.group(1)} saved'
        if text.startswith('No suitable artwork options found'):
            return 'No options…'
        if text.startswith('Finished '):
            return 'Finishing…'
        if text.startswith('Artwork search stopped'):
            return 'Stopping…'
        return 'Searching…'

    def _partial_search_status_finished_album(self, text):
        text = (text or '').strip()
        return (
            text.startswith('Finished ')
            or text.startswith('No suitable artwork options found for ')
            or (' already has ' in text and ' saved option' in text)
        )

    def _active_provider_status(self):
        """Return the current provider search status, if a search is active."""
        if not getattr(self, 'active_search_album_keys', set()):
            return ''
        statuses = list((getattr(self, 'active_search_album_status', {}) or {}).values())
        # Prefer a real provider line over generic queued/searching/saved notes.
        for status in statuses:
            status = str(status or '').strip()
            if status.startswith('Searching ') and status != 'Searching…' and ' saved' not in status and not re.match(r'^Searching \d+/\d+$', status):
                return status
        for status in statuses:
            status = str(status or '').strip()
            if status.startswith('Finishing'):
                return 'Finishing search…'
        for status in statuses:
            status = str(status or '').strip()
            if status == 'Searching…':
                return 'Searching…'
        for status in statuses:
            status = str(status or '').strip()
            if status == 'Search Queued' or status.startswith('Queued'):
                return 'Queued for search…'
        return 'Searching…'

    def _brief_current_size(self, width, height):
        """Short current-art size label for the review header."""
        if width is None or height is None:
            return 'Missing'
        scan_min = get_scan_min_artwork_size()
        try:
            w, h = int(width or 0), int(height or 0)
        except Exception:
            return '—'
        quality = 'OK' if artwork_meets_target_size(w, h, scan_min) else ('LOW' if min(w, h) < max(1, int(scan_min * 0.6)) else 'MID')
        return f'{w}×{h} {quality}'

    def _size_text(self, width, height):
        if width is None or height is None:
            return 'Missing'
        try:
            return f'{int(width or 0)}×{int(height or 0)}'
        except Exception:
            return '—'

    def _artwork_comparison_label(self, old=None, candidate=None):
        """Current → candidate comparison for details/log text."""
        candidate = candidate or {}
        old = old or {}
        target = get_preferred_artwork_size()
        new_w, new_h = candidate.get('width'), candidate.get('height')
        new_txt = self._size_text(new_w, new_h)
        old_txt = self._size_text(old.get('width'), old.get('height')) if old else 'Missing'
        try:
            nw, nh = int(new_w or 0), int(new_h or 0)
            ow, oh = int(old.get('width') or 0), int(old.get('height') or 0) if old else 0
        except Exception:
            nw = nh = ow = oh = 0
        if artwork_meets_target_size(nw, nh, target):
            verdict = 'Meets target'
        elif old and min(nw, nh) > min(ow, oh):
            verdict = 'Improves size'
        elif old and nw == ow and nh == oh:
            verdict = 'Same size'
        elif old and nw and nh and min(nw, nh) < min(ow, oh):
            verdict = 'Smaller'
        else:
            verdict = 'Below target'
        return f'{old_txt} → {new_txt} · {verdict}'

    def _artwork_comparison_surface_label(self, old=None, candidate=None):
        """Short candidate-size label for the preview surface.

        The under-artwork area is narrow. Show only the candidate resolution
        and whether it meets the configured target; the detailed current →
        candidate comparison stays in the details pane.
        """
        candidate = candidate or {}
        target = get_preferred_artwork_size()
        new_txt = self._size_text(candidate.get('width'), candidate.get('height'))
        try:
            nw, nh = int(candidate.get('width') or 0), int(candidate.get('height') or 0)
        except Exception:
            nw = nh = 0
        verdict = 'Target met' if artwork_meets_target_size(nw, nh, target) else 'Below'
        return f'{new_txt} · {verdict}'

    def _ellipsize_middle(self, text, limit):
        text = str(text or '')
        if len(text) <= limit:
            return text
        limit = max(12, int(limit or 12))
        keep_left = max(6, int(limit * 0.62))
        keep_right = max(4, limit - keep_left - 1)
        return text[:keep_left].rstrip() + '…' + text[-keep_right:].lstrip()

    def _ellipsize_end(self, text, limit):
        text = str(text or '')
        limit = max(8, int(limit or 8))
        if len(text) <= limit:
            return text
        return text[:max(1, limit - 1)].rstrip() + '…'

    def _overall_app_status(self, text):
        """Return a calm, short app-wide status for the top-right strip.

        Album identity/status now lives on the left and in the queue rows.  This
        area should only say what the app itself is doing, so it does not repeat
        selected/reviewing album names or duplicate embed/search progress text.
        While a provider search is active, prefer the live provider overlay over
        incidental messages such as candidates being saved or album selection.
        """
        if getattr(self, 'scan_active', False):
            raw_for_scan = str(text or '').strip().lower()
            if 'stopping after' in raw_for_scan or raw_for_scan.startswith('stopping scan'):
                return 'Stopping scan…'
            return 'Searching Library'
        provider_status = self._active_provider_status()
        if provider_status:
            return provider_status
        raw = str(text or '').strip()
        low = raw.lower()
        if not raw:
            return ''
        if low == 'idle':
            return ''
        if raw.startswith('Selected:') or raw.startswith('Reviewing:'):
            return ''
        if 'embedding artwork' in low or low.startswith('embedding '):
            m = re.search(r'(\d+)\s*/\s*(\d+)(?:\s*[—-]\s*(.+))?$', raw)
            if m:
                done, total = m.group(1), m.group(2)
                return f'Embedding {done}/{total}'
            return 'Embedding artwork…'
        if low.startswith('converting/saving') or low.startswith('convert/save'):
            m = re.search(r'(\d+)\s*/\s*(\d+)', raw)
            if m:
                return f'Convert/Save {m.group(1)}/{m.group(2)}'
            return 'Convert/Save…'
        if low.startswith('batch convert/save'):
            return 'Batch Convert/Save…'
        if 'scanning embedded artwork' in low or low.startswith('scan '):
            return 'Scanning library…'
        if 'stopping after' in low:
            return 'Stopping scan…'
        if 'searching musicbrainz' in low:
            return 'Searching MusicBrainz…'
        if 'searching discogs' in low:
            return 'Searching Discogs…'
        if 'searching itunes' in low or 'searching apple' in low:
            return 'Searching Apple Music…'
        if 'searching deezer' in low:
            return 'Searching Deezer…'
        if low.startswith('finishing'):
            return 'Finishing search…'
        if low.startswith('queued'):
            return 'Queued for search…'
        if ('finding artwork' in low or 'searching next' in low or
                'searching for more artwork' in low or 'artwork search' in low):
            return 'Searching artwork…'
        if 'search more finished' in low or 'search finished' in low or 'found ' in low or 'no artwork options found' in low:
            return ''
        if low.startswith('approval') or low.startswith('added ') or low.startswith('cleanup complete'):
            return ''
        if low.startswith('saved queue loaded') or low.startswith('choose your music folder'):
            return ''
        return raw

    def _set_progress_row_visible(self, visible):
        row = getattr(self, 'progress_row', None)
        if row is None:
            return
        try:
            mapped = bool(row.winfo_ismapped())
        except Exception:
            mapped = False
        if visible and not mapped:
            try:
                self.status_label.pack_configure(fill='x', expand=False)
            except Exception:
                pass
            row.pack(fill='x', pady=(3, 0))
        elif not visible and mapped:
            row.pack_forget()
            try:
                self.status_label.pack_configure(fill='both', expand=True)
            except Exception:
                pass

    def _sync_top_status_label(self):
        """Show a short overall app state in the top-right status area."""
        if not hasattr(self, 'status_display_var'):
            return
        text = self._overall_app_status(self.status_var.get() if hasattr(self, 'status_var') else '')
        try:
            width = int(getattr(self, 'status_label', None).winfo_width() or 0)
        except Exception:
            width = 0
        limit = max(24, min(86, int((width or 500) / 7)))
        shown = self._ellipsize_end(text, limit) if text else ''
        self.status_display_var.set(shown)
        # The progress bar is helpful while scanning/embedding, but it looks
        # like a stray empty rule when the app is idle. Hide it unless there is
        # active progress to show.
        try:
            pct = float(self.progress_var.get() or 0)
        except Exception:
            pct = 0
        progress_txt = self.progress_text.get().strip() if hasattr(self, 'progress_text') else ''
        # Search provider names should remain visible without needing a progress
        # bar. Only show the bar while there is meaningful scan/embed progress.
        self._set_progress_row_visible(bool(shown) and (progress_txt or pct > 0) and not shown.startswith('Searching '))

    def _sync_review_header_labels(self):
        """Keep the top-left review strip to two fixed single-line labels.

        Long artist/album/candidate metadata used to wrap and push the artwork
        preview area down. Store the full raw text, but display an ellipsized
        single-line version sized to the current review-strip width.
        """
        if not hasattr(self, 'review_title_var'):
            return
        try:
            width = int(getattr(self, 'review_header', None).winfo_width() or 0)
        except Exception:
            width = 0
        width = max(width, 360)
        title_limit = max(48, min(150, int(width / 5.2)))
        title = getattr(self, 'review_title_raw', self.review_title_var.get())
        meta = getattr(self, 'review_meta_raw', self.review_meta_var.get())
        try:
            self.review_title_label.configure(wraplength=max(260, width - 12))
        except Exception:
            pass
        # Let the album title occupy the two-line strip. Trim only very long
        # names so the label does not spill beyond the fixed-height area.
        self.review_title_var.set(self._ellipsize_end(title, title_limit))
        self.review_meta_var.set(self._ellipsize_middle(meta, max(36, min(100, int(width / 8.0)))))

    def _on_review_header_configure(self, event=None):
        """Keep the review strip fixed-height and single-line."""
        try:
            self._sync_review_header_labels()
        except Exception:
            pass

    def _update_review_header(self, album=None, *, candidate=None, candidate_index=None, candidate_total=None):
        """Update the quiet selected-album summary strip.

        3.60 keeps this area to album identity plus workflow status only.
        Artwork dimensions, candidate count, source, and match details are
        deliberately shown in their own dedicated areas so the review panel
        does not repeat the same information four times.
        """
        if not hasattr(self, 'review_title_var'):
            return
        if not album:
            self.review_title_raw = 'No album selected'
            self.review_meta_raw = 'Select an album in the queue to review.'
            self._sync_review_header_labels()
            return
        artist = album.get('artist') or (candidate.get('artist') if candidate else '')
        title = album.get('album') or (candidate.get('album') if candidate else '')
        artist = artist or 'Unknown artist'
        title = title or 'Unknown album'
        self.review_title_raw = f'{artist} — {title}'

        # Keep the left strip to album identity only.  The exact workflow
        # status is already visible in the queue/details; the top-right strip
        # now shows the overall app activity.
        self.review_meta_raw = ''
        self._sync_review_header_labels()

    def refresh_review_header(self):
        """Refresh the summary strip without changing the selected album/candidate."""
        if not getattr(self, 'current_album_key', None):
            self._update_review_header(None)
            return
        album = db.get_album(self.current_album_key) or self.current_album_info
        if not album:
            self._update_review_header(None)
            return
        cands = self.current_candidates()
        cand = self.current_candidate() if cands else None
        self._update_review_header(album, candidate=cand, candidate_index=self.candidate_index, candidate_total=len(cands))

    def _clear_album_search_overlay(self, album_key):
        if not album_key:
            return False
        changed = False
        keys = getattr(self, 'active_search_album_keys', set())
        if album_key in keys:
            keys.discard(album_key)
            changed = True
        status_map = getattr(self, 'active_search_album_status', {})
        if album_key in status_map:
            status_map.pop(album_key, None)
            changed = True
        labels = getattr(self, 'active_search_album_labels', {})
        if album_key in labels:
            labels.pop(album_key, None)
            changed = True
        original_buckets = getattr(self, 'active_search_album_original_buckets', {})
        if album_key in original_buckets:
            original_buckets.pop(album_key, None)
            changed = True
        return changed

    def _note_partial_search_status(self, text):
        if not text or not getattr(self, 'active_search_album_keys', set()):
            return False
        labels = getattr(self, 'active_search_album_labels', {}) or {}
        changed = False
        matched = False
        finished_album = self._partial_search_status_finished_album(text)
        for key, label in list(labels.items()):
            if label and label in text:
                matched = True
                if finished_album:
                    # build_candidates has already written candidate_found/no_candidate
                    # before it sends the final per-album status.  Stop showing the
                    # temporary Searching/Finishing overlay immediately so the queue
                    # row switches to Review or No Options while the rest of a
                    # Search Next batch continues.
                    if self._clear_album_search_overlay(key):
                        changed = True
                    continue
                compact = self._compact_search_status(text, key)
                if getattr(self, 'active_search_album_status', {}).get(key) != compact:
                    self.active_search_album_status[key] = compact
                    changed = True
        if not matched and len(getattr(self, 'active_search_album_keys', set())) == 1:
            key = next(iter(self.active_search_album_keys))
            if finished_album:
                if self._clear_album_search_overlay(key):
                    changed = True
            else:
                compact = self._compact_search_status(text, key)
                if getattr(self, 'active_search_album_status', {}).get(key) != compact:
                    self.active_search_album_status[key] = compact
                    changed = True
        if changed:
            self.schedule_queue_refresh(delay=90)
            self.refresh_review_header()
        if getattr(self, 'active_find_mode', None) == 'batch':
            summary = self._batch_search_progress_text()
            if summary:
                self._set_action_result(summary)
        return changed

    def _batch_search_progress_text(self):
        total = int(getattr(self, 'active_search_batch_total', 0) or 0)
        if not total:
            return ''
        remaining = len(getattr(self, 'active_search_album_keys', set()) or [])
        complete = max(0, total - remaining)
        queued = 0
        searching = 0
        saved = 0
        for status in (getattr(self, 'active_search_album_status', {}) or {}).values():
            label = str(status or '').strip()
            if label == 'Search Queued' or label.startswith('Queued'):
                queued += 1
            elif 'saved' in label.lower() or label.startswith('Finishing'):
                saved += 1
            elif label.startswith('Searching'):
                searching += 1
        bits = [f'Batch search: {complete}/{total} complete']
        if searching:
            bits.append(f'{searching} searching')
        if queued:
            bits.append(f'{queued} queued')
        if saved:
            bits.append(f'{saved} saving')
        return ' • '.join(bits)

    def current_size_label(self, width, height, *, prefix=''):
        label = 'Missing' if width is None or height is None else self._queue_current_label(width, height)
        return f'{prefix}: {label}' if prefix else label

    def _queue_current_label(self, width, height):
        """Consistent compact current-art label for queue/review UI."""
        if width is None or height is None:
            return 'Missing'
        try:
            w, h = int(width or 0), int(height or 0)
        except Exception:
            return '—'
        return f'{w}×{h}'

    def _deep_check_dimension_label_for_album(self, album):
        """Return the most relevant deep-check dimensions for display, if known."""
        album = album or {}
        notes = album.get('notes_json') or album.get('notes') or {}
        if isinstance(notes, str):
            try:
                notes = json.loads(notes)
            except Exception:
                notes = {}
        deep = effective_deep_file_check(notes)
        if not isinstance(deep, dict) or not deep.get('enabled'):
            return ''
        issue_counts = 0
        for key in ('missing_count', 'below_target_count', 'non_square_count', 'incompatible_count', 'unreadable_count'):
            try:
                issue_counts += int(deep.get(key) or 0)
            except Exception:
                pass
        if not issue_counts:
            return ''
        dims = deep.get('first_non_square_dimensions') or ''
        if not dims:
            try:
                w = int(deep.get('example_width') or 0)
                h = int(deep.get('example_height') or 0)
                if w > 0 and h > 0:
                    dims = f'{w}×{h}'
            except Exception:
                dims = ''
        return str(dims).replace('x', '×') if dims else ''

    def _queue_current_label_for_album(self, album):
        """Readable Current column label.

        When Deep Check has found a different per-file size than the stored
        representative row size, show the deep-check size so the queue and
        details explain the same problem.
        """
        album = album or {}
        deep_dims = self._deep_check_dimension_label_for_album(album)
        if deep_dims:
            return deep_dims
        return self._queue_current_label(album.get('width'), album.get('height'))

    def _candidate_target_result(self, candidate):
        candidate = candidate or {}
        target = get_preferred_artwork_size()
        try:
            w, h = int(candidate.get('width') or 0), int(candidate.get('height') or 0)
        except Exception:
            w = h = 0
        if artwork_meets_target_size(w, h, target):
            return 'Target met'
        if min(w, h) >= max(1, int(target * 0.6)):
            return 'Below target'
        return 'Low-res'


    def _candidate_embed_size_label(self, candidate):
        """Return the artwork size that will be embedded after target resizing."""
        candidate = candidate or {}
        try:
            w, h = int(candidate.get('width') or 0), int(candidate.get('height') or 0)
        except Exception:
            w = h = 0
        if not w or not h:
            return 'unknown size'
        try:
            settings = load_settings()
            resize_enabled = bool(settings.get('resize_approved_artwork', True))
            target = get_preferred_artwork_size(settings)
        except Exception:
            resize_enabled = True
            target = get_preferred_artwork_size()
        if resize_enabled:
            if max(w, h) > target:
                scale = float(target) / float(max(w, h))
                ew = max(1, int(round(w * scale)))
                eh = max(1, int(round(h * scale)))
            else:
                ew, eh = w, h
            if ew != eh:
                side = max(ew, eh)
                if target > 0 and side >= int(round(target * 0.98)):
                    side = target
                return f'{side}×{side}'
            return f'{ew}×{eh}'
        if max(w, h) <= target:
            return f'{w}×{h}'
        scale = float(target) / float(max(w, h))
        ew = max(1, int(round(w * scale)))
        eh = max(1, int(round(h * scale)))
        return f'{ew}×{eh}'

    def _candidate_will_embed_line(self, candidate):
        size = self._candidate_embed_size_label(candidate)
        source = candidate or {}
        try:
            w, h = int(source.get('width') or 0), int(source.get('height') or 0)
        except Exception:
            w = h = 0
        original = f'{w}×{h}' if w and h else ''
        if original and original != size:
            return f'Will embed as {size}'
        return f'Will embed as {size}' if size != 'unknown size' else 'Will embed at original size'

    def _update_detail_summary(self, *, current='—', candidate='—', source='—', match='—'):
        if not hasattr(self, 'detail_summary_vars'):
            return
        values = {
            'current': current or '—',
            'candidate': candidate or '—',
            'source': source or '—',
            'match': match or '—',
        }
        for key, value in values.items():
            var = self.detail_summary_vars.get(key)
            if var is not None:
                var.set(self._ellipsize_end(str(value), 32))

    def active_queue_keys(self):
        return [a['album_key'] for a in db.load_albums(actionable_only=True)]

    def queue_navigation_keys(self):
        """Return the queue row order the user is currently working through."""
        keys = []
        try:
            keys = self.visible_queue_keys()
        except Exception:
            keys = []
        if keys:
            return keys
        try:
            return [a.get('album_key') for a in self._queue_albums_for_current_view() if a.get('album_key')]
        except Exception:
            return self.active_queue_keys()

    def select_next_album_after(self, old_key, previous_keys=None, message=None):
        """Advance to the next row in the user's visible queue view.

        Older builds advanced through db.load_albums(actionable_only=True). That
        was surprising in Show: All because approving/skipping a row could jump
        past many visible albums to the next hidden workflow item.  The review
        workflow should follow the filtered/sorted queue the user can see: after
        an action, choose the next key from the pre-action visible row order that
        still exists in the refreshed visible row set.
        """
        self._clear_queue_sticky_album()
        previous_keys = [k for k in (previous_keys or []) if k]
        if not previous_keys:
            previous_keys = self.queue_navigation_keys()
        try:
            visible_albums = self._queue_albums_for_current_view()
        except Exception:
            visible_albums = []
        visible_keys = [a.get('album_key') for a in visible_albums if a.get('album_key')]
        visible_set = set(visible_keys)

        if not visible_keys:
            # Keep the review pane grounded in the selected album if it still
            # exists, but do not jump to a hidden actionable item outside the
            # current filter/search view.
            if old_key and db.get_album(old_key):
                try:
                    self.load_album_for_review(old_key)
                except Exception:
                    pass
            else:
                self.current_album_key = None
                self.clear(message or 'No albums remain in the current queue view.')
            self.refresh_queue_tab()
            self.refresh_footer()
            if message:
                self.status_var.set(message)
            return

        ordered = list(previous_keys)
        if old_key in ordered:
            start = ordered.index(old_key) + 1
            ordered = ordered[start:] + ordered[:start]
        ordered += [k for k in visible_keys if k not in ordered]

        next_key = next((k for k in ordered if k in visible_set and k != old_key), None)
        if next_key is None:
            # Single-row views such as Show: All with one album should simply
            # stay on that row after the action completes.
            next_key = old_key if old_key in visible_set else visible_keys[0]
        self.load_album_for_review(next_key)
        self.refresh_queue_tab()
        if message:
            self.status_var.set(message)

    def _rebuild_groups(self):
        groups = OrderedDict()
        for c in self.candidates:
            groups.setdefault(c['album_key'], []).append(c)
        for key in list(groups.keys()):
            groups[key] = sorted(groups[key], key=lambda item: (-int(item.get('score') or 0), item.get('candidate_id') or 0))
        self.groups = groups
        self.album_keys = list(groups.keys())
        if self.current_album_key not in groups:
            if self.current_album_key and db.get_album(self.current_album_key):
                # Preserve the album the user is browsing, even if it has no
                # saved candidates yet. Background searches should update the
                # queue quietly, not steal focus.
                self.candidate_index = 0
            else:
                self.current_album_key = self.album_keys[0] if self.album_keys else None
                self.candidate_index = 0
        elif self.candidate_index >= len(groups[self.current_album_key]):
            self.candidate_index = max(0, len(groups[self.current_album_key]) - 1)

    def load_saved_queue(self, silent=False):
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self._queue_consistency_check(repair=True, context='load queue')
        self.refresh_queue_tab()

        albums = db.load_albums(actionable_only=True)
        current_album = db.get_album(self.current_album_key) if self.current_album_key else None
        current_active = current_album and current_album.get('status') not in ('already_good', 'approved', 'reviewed_skipped', 'ignored')
        if current_active:
            self.load_album_for_review(self.current_album_key)
        elif albums:
            self.load_album_for_review(albums[0]['album_key'])
        else:
            self.clear('No albums requiring action are saved yet. Scan your library to build the queue first.')

        counts = db.album_counts()
        total_actionable = len(albums)
        self.status_var.set(f'Saved queue loaded: {total_actionable} album(s) need action, {len(self.candidates)} artwork option(s) saved.')
        if not silent:
            self.log_msg(f'\nQueue loaded: {total_actionable} album(s) need attention, {len(self.candidates)} saved option(s).\n')
            self.log_verbose(f'  Status counts: {counts}\n')
        self.refresh_footer()

    def _current_queue_filter_label(self):
        try:
            return self.filter_var.get() if hasattr(self, 'filter_var') else 'All'
        except Exception:
            return 'All'

    def _clear_queue_sticky_album(self):
        self.queue_sticky_album_key = None
        self.queue_sticky_filter = None
        self.queue_sticky_reason = ''

    def _pin_album_in_current_filter(self, album_key=None, reason=''):
        """Temporarily keep an actioned album visible in the active filter.

        This is intentionally narrow.  It is used after a user action changes an
        album's real state while the user is still working on it, such as Find
        Artwork turning a Needs Search row into Review.  It is cleared on manual
        filter changes, search text changes, and when the user moves to another
        album.
        """
        key = album_key or getattr(self, 'current_album_key', None)
        label = self._current_queue_filter_label()
        if key and label != 'All':
            self.queue_sticky_album_key = key
            self.queue_sticky_filter = label
            self.queue_sticky_reason = reason or 'workflow action'
        else:
            self._clear_queue_sticky_album()

    def _queue_sticky_applies_to_album(self, album, *, selected_filter=None):
        if not album:
            return False
        key = album.get('album_key')
        label = selected_filter or self._current_queue_filter_label()
        return bool(
            key
            and key == getattr(self, 'queue_sticky_album_key', None)
            and label
            and label == getattr(self, 'queue_sticky_filter', None)
            and label != 'All'
        )

    def _load_focused_queue_album(self):
        """Load the row currently selected/focused in the visible queue."""
        if not hasattr(self, 'queue_tree'):
            return False
        sel = list(self.queue_tree.selection())
        focus = self.queue_tree.focus()
        iid = focus if focus in sel else (sel[-1] if sel else None)
        if not iid:
            children = list(self.queue_tree.get_children(''))
            iid = children[0] if children else None
        key = self.queue_album_keys.get(iid) if iid and hasattr(self, 'queue_album_keys') else None
        if key:
            self.load_album_for_review(key)
            return True
        return False

    def _on_queue_filter_changed(self, event=None):
        # Changing filters is browsing, not an action on the current album.  Do
        # not carry the temporary "keep current row visible" rule across filter
        # changes; load the first real row in the selected filter instead.
        self._clear_queue_sticky_album()
        self.refresh_queue_tab()
        self._load_focused_queue_album()
        self._save_layout_settings()
        return 'break'

    def queue_workflow_bucket(self, album):
        """Return the queue filter bucket for an album.

        The bucket is a workflow/filter concept; it is derived from the shared
        evaluator instead of being another place that guesses album status.
        """
        if not album:
            return ''
        key = album.get('album_key')
        if key and key in getattr(self, 'active_search_album_keys', set()):
            return workflow_bucket_for_status(album.get('status', ''), active_search=True)
        status, _reason = self._evaluate_queue_album(album)
        return workflow_bucket_for_status(status)

    def queue_filter_matches_album(self, album):
        """Return True when an album belongs in the selected queue filter."""
        label = self.filter_var.get() if hasattr(self, 'filter_var') else 'All'
        if label == 'All':
            return True
        bucket = self.queue_workflow_bucket(album)
        key = album.get('album_key') if album else None
        original_bucket = ''
        if key and key in getattr(self, 'active_search_album_keys', set()):
            original_bucket = (getattr(self, 'active_search_album_original_buckets', {}) or {}).get(key, '')

        def bucket_matches(target):
            return bucket == target or original_bucket == target

        if label == 'Needs Attention':
            return bucket in {'Needs Search', 'Missing', 'Not Square', 'Convert'} or original_bucket in {'Needs Search', 'Missing', 'Not Square', 'Convert'}
        if label in {'Needs Search', 'Convert', 'Missing', 'Review', 'Good', 'Not Square'}:
            return bucket_matches(label)
        return True

    def queue_filter_actionable_only(self):
        # The simplified filters need access to completed albums as well as active
        # ones. The visible rows are filtered below.
        return False

    def _album_min_dim(self, album):
        w, h = album.get('width'), album.get('height')
        if w is None or h is None:
            return 0
        try:
            return min(int(w or 0), int(h or 0))
        except Exception:
            return 0

    def _sort_queue_albums(self, albums):
        sort = self.queue_sort_var.get() if hasattr(self, 'queue_sort_var') else 'Workflow Priority'
        if sort == 'Workflow Priority':
            order = {'Review': 0, 'Missing': 1, 'Needs Search': 2, 'Not Square': 3, 'Convert': 4, 'Good': 5, 'Handled': 6}
            return sorted(albums, key=lambda a: (order.get(self.queue_workflow_bucket(a), 99), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Smallest Current Artwork':
            return sorted(albums, key=lambda a: (self._album_min_dim(a), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Largest Current Artwork':
            return sorted(albums, key=lambda a: (-self._album_min_dim(a), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Most Options':
            return sorted(albums, key=lambda a: (-int(a.get('candidate_count') or 0), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Fewest Options':
            return sorted(albums, key=lambda a: (int(a.get('candidate_count') or 0), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Artist A-Z':
            return sorted(albums, key=lambda a: ((a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        if sort == 'Album A-Z':
            return sorted(albums, key=lambda a: ((a.get('album') or '').lower(), (a.get('artist') or '').lower()))
        if sort == 'Status':
            return sorted(albums, key=lambda a: (self.queue_workflow_bucket(a), (a.get('artist') or '').lower(), (a.get('album') or '').lower()))
        return albums

    def _queue_albums_for_current_view(self, albums=None):
        """Return albums exactly as the queue tab should display them.

        Keep the current album visible while the user is actively working on it.
        This matters in filtered views: for example, in Show: Needs Search, a
        successful Find Artwork changes the album to Review. Hiding that row
        immediately makes the user lose the album just as they need to inspect
        and embed the downloaded options. Once the user moves to another row, the
        normal filter rules take over again.
        """
        if albums is None:
            albums = db.load_albums(actionable_only=self.queue_filter_actionable_only())
        query = (self.queue_search_var.get() if hasattr(self, 'queue_search_var') else '').strip().lower()
        selected_filter = self.filter_var.get() if hasattr(self, 'filter_var') else 'All'
        shown = []
        for album in albums:
            display_status = self.display_queue_status(album)
            matches_filter = self.queue_filter_matches_album(album)
            keep_current_visible = bool(
                self._queue_sticky_applies_to_album(album, selected_filter=selected_filter)
                and not matches_filter
            )
            if not matches_filter and not keep_current_visible:
                continue
            display_label = self.display_queue_status_label(album)
            haystack = f'{album.get("artist", "")} {album.get("album", "")} {album.get("album_path", "")} {self.friendly_status(display_status)} {display_label}'.lower()
            if query and query not in haystack:
                continue
            shown.append(album)
        return self._sort_queue_albums(shown)

    def _queue_bucket_counts(self, albums=None):
        counts = {'Needs Search': 0, 'Convert': 0, 'Missing': 0, 'Review': 0, 'Good': 0, 'Handled': 0, 'Not Square': 0}
        if albums is None:
            try:
                albums = db.load_albums(actionable_only=False)
            except Exception:
                albums = []
        for album in albums:
            bucket = self.queue_workflow_bucket(album)
            if bucket in counts:
                counts[bucket] += 1
        counts['Needs Attention'] = counts['Needs Search'] + counts['Missing'] + counts['Not Square'] + counts['Convert']
        counts['All'] = len(albums)
        return counts

    def schedule_queue_refresh(self, delay=120, preserve_selection=False):
        """Coalesce frequent queue redraws from scans/search progress.

        Scans and provider searches can emit many small events. Redrawing the
        full queue for every event makes the UI feel slower on large libraries,
        so non-critical callers can request one near-future refresh instead.
        """
        if not hasattr(self, 'queue_tree'):
            return
        self._queue_refresh_preserve = bool(getattr(self, '_queue_refresh_preserve', False) or preserve_selection)
        if getattr(self, '_queue_refresh_after', None):
            return
        try:
            self._queue_refresh_after = self.root.after(int(delay or 120), self._run_scheduled_queue_refresh)
        except Exception:
            self._queue_refresh_after = None
            self.refresh_queue_tab(preserve_selection=preserve_selection)

    def _run_scheduled_queue_refresh(self):
        preserve = bool(getattr(self, '_queue_refresh_preserve', False))
        self._queue_refresh_after = None
        self._queue_refresh_preserve = False
        self.refresh_queue_tab(preserve_selection=preserve)

    def refresh_queue_tab(self, preserve_selection=False):
        if not hasattr(self, 'queue_tree'):
            return
        self._queue_refresh_after = None
        self._queue_eval_cache = {}
        selected_key = self.current_album_key
        selected_iid = None
        for iid in self.queue_tree.get_children():
            self.queue_tree.delete(iid)
        self.queue_album_keys = {}
        query = (self.queue_search_var.get() if hasattr(self, 'queue_search_var') else '').strip().lower()
        try:
            all_albums = db.load_albums(actionable_only=False)
        except Exception:
            all_albums = []
        shown = self._queue_albums_for_current_view(all_albums)
        label = self.filter_var.get() if hasattr(self, 'filter_var') else 'All'
        for idx, album in enumerate(shown):
            w, h = album.get('width'), album.get('height')
            cur = self._queue_current_label_for_album(album)
            iid = f'album_{idx}'
            self.queue_album_keys[iid] = album['album_key']
            if selected_key and album['album_key'] == selected_key:
                selected_iid = iid
            status = self.display_queue_status(album)
            actual_status, _actual_reason = self._evaluate_queue_album(album)
            if status == 'searching':
                tags = ('searching',)
            elif actual_status == 'candidate_found':
                tags = ('review',)
            elif actual_status == 'no_candidate':
                tags = ('noopts',)
            elif actual_status == 'not_square_artwork':
                tags = ('needs',)
            elif actual_status in ('approved', 'reviewed_skipped', 'already_good', 'ignored'):
                tags = ('done',)
            else:
                tags = ('needs',)
            self.queue_tree.insert('', 'end', iid=iid, tags=tags, values=(self.display_queue_status_label(album), album.get('artist', ''), album.get('album', ''), cur, album.get('candidate_count', 0)))
        if selected_iid:
            try:
                self.queue_tree.selection_set(selected_iid)
                self.queue_tree.focus(selected_iid)
                self.queue_tree.see(selected_iid)
            except Exception:
                pass
        elif shown:
            # Keep keyboard navigation alive after changing filters/search text,
            # even when the previously selected album is no longer visible.
            try:
                first_iid = 'album_0'
                if first_iid in self.queue_tree.get_children(''):
                    self.queue_tree.selection_set(first_iid)
                    self.queue_tree.focus(first_iid)
                    self.queue_tree.see(first_iid)
            except Exception:
                pass
        if hasattr(self, 'queue_summary_var'):
            all_active = sum(1 for a in all_albums if (a.get('status') or '') not in ('already_good', 'approved', 'reviewed_skipped', 'ignored'))
            counts = self._queue_bucket_counts(all_albums)
            review_n = counts.get('Review', 0)
            needs_attention_n = counts.get('Needs Attention', 0)
            good_n = counts.get('Good', 0)
            not_square_n = counts.get('Not Square', 0)
            if hasattr(self, 'task_summary_var'):
                # Match the visible filter buckets, not raw database status names.
                extra = f' · Not Square {not_square_n}' if not_square_n else ''
                self.task_summary_var.set(f'{len(shown)} shown · Attention {needs_attention_n} · Review {review_n} · Good {good_n}{extra}')
            label = self.filter_var.get() if hasattr(self, 'filter_var') else 'All'
            qtxt = f' matching “{self.queue_search_var.get().strip()}”' if query else ''
            sort_txt = self.queue_sort_var.get() if hasattr(self, 'queue_sort_var') else 'Workflow Priority'
            if shown:
                self.queue_summary_var.set(f'{label}{qtxt} · {sort_txt}')
            else:
                self.queue_summary_var.set(f'No albums · {label}{qtxt} · {all_active} active')

    def _queue_current_index(self, children=None):
        children = list(children or self.queue_tree.get_children())
        if not children:
            return -1
        sel = self.queue_tree.selection()
        current = sel[0] if sel else self.queue_tree.focus()
        if current in children:
            return children.index(current)
        selected_key = self.current_album_key
        if selected_key:
            for idx, iid in enumerate(children):
                if self.queue_album_keys.get(iid) == selected_key:
                    return idx
        return 0

    def move_queue_selection(self, delta):
        if not hasattr(self, 'queue_tree'):
            return 'break'
        children = list(self.queue_tree.get_children())
        if not children:
            return 'break'
        idx = self._queue_current_index(children)
        if idx < 0:
            idx = 0
        new_idx = max(0, min(len(children) - 1, idx + int(delta or 0)))
        iid = children[new_idx]
        try:
            self.queue_tree.selection_set(iid)
            self.queue_tree.focus(iid)
            self.queue_tree.see(iid)
            key = self.queue_album_keys.get(iid)
            if key:
                if key != getattr(self, 'queue_sticky_album_key', None):
                    self._clear_queue_sticky_album()
                self.load_album_for_review(key)
            self._schedule_layout_save()
        except Exception:
            pass
        return 'break'

    def move_queue_selection_to_edge(self, first=True):
        children = list(self.queue_tree.get_children()) if hasattr(self, 'queue_tree') else []
        if not children:
            return 'break'
        iid = children[0] if first else children[-1]
        try:
            self.queue_tree.selection_set(iid)
            self.queue_tree.focus(iid)
            self.queue_tree.see(iid)
            key = self.queue_album_keys.get(iid)
            if key:
                if key != getattr(self, 'queue_sticky_album_key', None):
                    self._clear_queue_sticky_album()
                self.load_album_for_review(key)
            self._schedule_layout_save()
        except Exception:
            pass
        return 'break'

    def select_queue_album(self, event=None):
        sel = self.queue_tree.selection()
        if not sel:
            return
        focus = self.queue_tree.focus()
        iid = focus if focus in sel else sel[-1]
        key = self.queue_album_keys.get(iid)
        if not key:
            return
        if key != getattr(self, 'queue_sticky_album_key', None):
            self._clear_queue_sticky_album()
        self.load_album_for_review(key)

    def selected_queue_keys(self):
        keys = []
        if hasattr(self, 'queue_tree'):
            for iid in self.queue_tree.selection():
                key = self.queue_album_keys.get(iid) if hasattr(self, 'queue_album_keys') else None
                if key and key not in keys:
                    keys.append(key)
        if not keys and self.current_album_key:
            keys.append(self.current_album_key)
        return keys

    def selected_queue_albums(self):
        albums = []
        for key in self.selected_queue_keys():
            album = db.get_album(key)
            if album:
                albums.append(album)
        return albums

    def visible_queue_keys(self):
        """Return album keys in the queue exactly as currently filtered/sorted/displayed."""
        keys = []
        if hasattr(self, 'queue_tree') and hasattr(self, 'queue_album_keys'):
            for iid in self.queue_tree.get_children(''):
                key = self.queue_album_keys.get(iid)
                if key and key not in keys:
                    keys.append(key)
        return keys

    def visible_queue_albums(self):
        albums = []
        for key in self.visible_queue_keys():
            album = db.get_album(key)
            if album:
                albums.append(album)
        return albums

    def open_selected_queue_album(self, event=None):
        sel = self.queue_tree.selection()
        focus = self.queue_tree.focus()
        iid = focus if focus in sel else (sel[0] if sel else None)
        key = self.queue_album_keys.get(iid) if iid else self.current_album_key
        if not key:
            return
        album = db.get_album(key)
        if album and album.get('album_path'):
            open_path(album.get('album_path'))

    def close_queue_context_popup(self, event=None):
        popup = getattr(self, 'queue_context_popup', None)
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
        self.queue_context_popup = None

    def show_queue_context_menu(self, event):
        try:
            iid = self.queue_tree.identify_row(event.y)
            if iid:
                if iid not in self.queue_tree.selection():
                    self.queue_tree.selection_set(iid)
                self.queue_tree.focus(iid)
                key = self.queue_album_keys.get(iid)
                if key:
                    self.load_album_for_review(key)
        except Exception:
            pass
        self.close_queue_context_popup()
        popup = tk.Toplevel(self.root)
        self.queue_context_popup = popup
        popup.overrideredirect(True)
        popup.transient(self.root)
        try:
            popup.attributes('-topmost', True)
        except Exception:
            pass
        frame = ttk.Frame(popup, padding=6, relief='solid', borderwidth=1)
        frame.pack(fill='both', expand=True)
        selected_n = len(self.selected_queue_keys())
        visible_n = len(self.visible_queue_keys()) if hasattr(self, 'queue_tree') else 0
        has_album = selected_n > 0
        has_candidate = bool(self.current_candidate())
        searching = self.is_artwork_search_active()
        transitioning = self._action_transition_active()
        writing = self.is_write_action_active()
        album = self.current_album_info or (db.get_album(self.current_album_key) if self.current_album_key else None)
        status, _reason = self._evaluate_queue_album(album or {})
        status = status or (album or {}).get('status') or ''

        def add_heading(label):
            ttk.Label(frame, text=label, foreground='#666666', font=('TkDefaultFont', 10, 'bold')).pack(anchor='w', pady=(5, 1))

        def add(label, command, state='normal'):
            def run():
                self.close_queue_context_popup()
                if state != 'disabled':
                    command()
            b = ttk.Button(frame, text=label, command=run, state=state)
            b.pack(fill='x', pady=1)
            return b

        def sep():
            ttk.Separator(frame).pack(fill='x', pady=4)

        search_state = 'normal' if has_album and not searching and not writing and not transitioning else 'disabled'
        album_state = 'normal' if has_album and not searching and not writing and not transitioning else 'disabled'
        multi_state = 'normal' if selected_n > 1 and not searching and not writing and not transitioning else 'disabled'

        add_heading('Primary')
        if searching:
            add('Stop Search', self.stop_artwork_search, 'normal')
        elif writing:
            add('Write action in progress…', lambda: None, 'disabled')
        elif status in ('not_square_artwork', 'incompatible_artwork'):
            add('Convert/Save Embedded Artwork', self.convert_embedded_artwork_to_baseline, album_state)
        elif has_candidate:
            add('Embed Selected Candidate', self.approve, album_state)
        elif status in ('already_good', 'approved', 'reviewed_skipped', 'ignored'):
            add('Rework Album', self.rework_album, album_state)
        else:
            add('Find Artwork', self.find_more, search_state)

        sep()
        add_heading('Search')
        add('Find Artwork', self.find_more, search_state)
        add('Search More', self.search_more, search_state)
        if selected_n > 1:
            add(f'Search Selected ({selected_n})', self.search_selected_albums, search_state)
        add(f'Search Next {get_batch_search_count(self.settings)}', self.find_next_five, 'disabled' if searching or writing or transitioning else 'normal')

        sep()
        add_heading('Artwork')
        add('Preview Embedded Artwork', self.preview_embedded_artwork, album_state)
        add(('Convert/Save Selected' if selected_n > 1 else 'Convert/Save Embedded Artwork'), self.convert_save_selected_embedded_artwork if selected_n > 1 else self.convert_embedded_artwork_to_baseline, album_state)
        add('Show Problem Files…', self.show_problem_files, album_state)
        add('Audit Album…', self.audit_selected_album, album_state)
        add(f'Convert/Save All Visible ({visible_n})', self.convert_save_visible_embedded_artwork, 'normal' if visible_n and not searching and not writing and not transitioning else 'disabled')
        add('Convert/Save Next', self.convert_save_next_visible_embedded_artwork, 'normal' if visible_n and not searching and not writing and not transitioning else 'disabled')
        add('Open Source Page', self.open_source_page, 'normal' if has_candidate else 'disabled')
        add('Reject All Candidates', self.reject_all_candidates, 'normal' if has_candidate and not writing else 'disabled')
        if getattr(self, 'convert_batch_active', False):
            add('Stop Batch After Current', self.request_stop_convert_batch_after_current, 'normal')

        sep()
        add_heading('Album')
        add('Open Album Folder', self.open_selected_queue_album, album_state)
        add('Locate Album Folder…', self.locate_album_folder, album_state)
        add('Refresh from Disk', self.rescan_selected_album, album_state)
        add('Deep Rescan Selected', self.deep_rescan_selected_album, album_state)
        add('Rework Album', self.rework_album, 'disabled' if (not has_album or searching or writing) else 'normal')

        sep()
        add_heading('Decisions')
        add(f'Ignore Selected ({selected_n})', self.ignore_selected_albums, album_state)
        add(f'Mark Selected Good ({selected_n})', self.mark_selected_good, album_state)
        if selected_n > 1:
            add(f'Reject Candidates for Selected ({selected_n})', self.reject_candidates_for_selected, multi_state)
            add(f'Rescan Selected ({selected_n})', self.rescan_selected_albums, multi_state)

        popup.update_idletasks()
        width = popup.winfo_reqwidth()
        height = popup.winfo_reqheight()
        screen_w = popup.winfo_screenwidth()
        screen_h = popup.winfo_screenheight()
        x = max(0, min(event.x_root, screen_w - width - 8))
        y = max(0, min(event.y_root, screen_h - height - 8))
        popup.geometry(f'{width}x{height}+{x}+{y}')
        popup.focus_force()
        popup.bind('<FocusOut>', self.close_queue_context_popup)
        popup.bind('<Escape>', self.close_queue_context_popup)
        return 'break'


    def load_album_for_review(self, key):
        album = db.get_album(key)
        if not album:
            return
        self.current_album_info = album
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.current_album_key = key
        self.candidate_index = 0
        if key in self.groups and self.groups[key]:
            self.show_current_album()
        else:
            self.show_album_without_candidate(album)

    def current_candidates(self):
        if not self.current_album_key:
            return []
        return self.groups.get(self.current_album_key, [])

    def current_candidate(self):
        cands = self.current_candidates()
        if not cands:
            return None
        self.candidate_index = max(0, min(self.candidate_index, len(cands) - 1))
        return cands[self.candidate_index]

    def clear(self, msg='No artwork option selected.'):
        self.current_img = self.current_old_img = None
        self.current_old_art_info = None
        self.current_album_info = None
        self.current_album_key = None
        self.set_preview_text(self.old_label, 'Current embedded artwork preview')
        self.set_preview_text(self.new_label, 'New artwork option preview')
        self.old_size_var.set('Current: —')
        self.new_size_var.set('—')
        self.cand_count_label.configure(text='0 options')
        self.cand_nav_label.configure(text='')
        for w in self.cand_inner.winfo_children():
            w.destroy()
        self.details.delete('1.0', 'end')
        self.details.insert('end', msg, ('section_body',))
        self._update_detail_summary()
        self._update_review_header(None)
        self.toggle_review_controls(False)

    def toggle_review_controls(self, enabled):
        state = 'normal' if enabled else 'disabled'
        widgets = [self.approve_btn, self.reject_btn, self.skip_btn, getattr(self, 'mark_good_btn', None), getattr(self, 'ignore_btn', None), self.google_btn, self.find_btn, self.prev_btn, self.next_btn, getattr(self, 'release_btn', None), getattr(self, 'import_btn', None), getattr(self, 'stop_find_btn', None), getattr(self, 'open_folder_btn', None), getattr(self, 'search_more_btn', None), getattr(self, 'open_source_btn', None)]
        if hasattr(self, 'queue_download_btn'):
            widgets.append(self.queue_download_btn)
        if hasattr(self, 'queue_release_btn'):
            widgets.append(self.queue_release_btn)
        if hasattr(self, 'queue_stop_find_btn'):
            widgets.append(self.queue_stop_find_btn)
        if hasattr(self, 'queue_import_btn'):
            widgets.append(self.queue_import_btn)
        for x in [w for w in widgets if w is not None]:
            try:
                x.configure(state=state)
            except Exception:
                pass

    def set_review_button_states(self, *, has_candidate=False, has_album=False):
        finding = self.is_artwork_search_active()
        embedding = self.is_embed_active()
        transitioning = self._action_transition_active()
        try:
            stopping_search = bool(self.active_find_job_id in getattr(self, 'canceled_find_jobs', set()))
        except Exception:
            stopping_search = False
        action_locked = bool(finding or embedding or transitioning)
        decision_state = 'normal' if has_candidate and not action_locked else 'disabled'
        album_decision_state = 'normal' if has_album and not action_locked else 'disabled'
        self.approve_btn.configure(state=decision_state)
        self.reject_btn.configure(state=decision_state)
        self.prev_btn.configure(state='normal' if has_candidate and len(self.current_candidates()) > 1 and not transitioning else 'disabled')
        self.next_btn.configure(state='normal' if has_candidate and len(self.current_candidates()) > 1 and not transitioning else 'disabled')
        self.enlarge_btn.configure(state='normal' if has_candidate and not transitioning else 'disabled')
        self.skip_btn.configure(state=album_decision_state)
        if hasattr(self, 'mark_good_btn'):
            self.mark_good_btn.configure(state=album_decision_state)
        if hasattr(self, 'ignore_btn'):
            self.ignore_btn.configure(state=album_decision_state)
        self.google_btn.configure(state='normal' if has_album and not transitioning else 'disabled')
        import_state = 'normal' if has_album and not finding and not embedding and not transitioning else 'disabled'
        if hasattr(self, 'import_btn'):
            self.import_btn.configure(state=import_state)
        if hasattr(self, 'queue_import_btn'):
            self.queue_import_btn.configure(state=import_state)
        download_state = 'normal' if has_album and not finding and not embedding and not transitioning else 'disabled'
        self.find_btn.configure(state=download_state)
        if hasattr(self, 'queue_download_btn'):
            self.queue_download_btn.configure(state=download_state)
        if hasattr(self, 'search_more_btn'):
            can_search_more = has_album and not finding and not embedding and not transitioning and (has_candidate or bool(self.current_album_key))
            self.search_more_btn.configure(state='normal' if can_search_more else 'disabled')
        stop_state = 'normal' if finding and not stopping_search else 'disabled'
        if hasattr(self, 'stop_find_btn'):
            self.stop_find_btn.configure(state=stop_state)
        if hasattr(self, 'queue_stop_find_btn'):
            self.queue_stop_find_btn.configure(state=stop_state)
        if hasattr(self, 'update_primary_action_button'):
            self.update_primary_action_button(has_candidate=has_candidate, has_album=has_album)
        if hasattr(self, 'more_btn'):
            self.more_btn.configure(state='normal' if has_album else 'disabled')
        if hasattr(self, 'release_btn'):
            self.release_btn.configure(state='normal' if has_album else 'disabled')
        if hasattr(self, 'queue_release_btn'):
            self.queue_release_btn.configure(state='normal' if has_album else 'disabled')
        if hasattr(self, 'open_folder_btn'):
            self.open_folder_btn.configure(state='normal' if has_album else 'disabled')
        if hasattr(self, 'open_source_btn'):
            self.open_source_btn.configure(state='normal' if has_candidate and not transitioning else 'disabled')
        if hasattr(self, 'find_next_btn'):
            self.find_next_btn.configure(state='disabled' if finding or embedding or transitioning else 'normal')

    def is_embed_active(self):
        return self.active_embed_job_id is not None

    def is_artwork_search_active(self):
        return self.active_find_job_id is not None

    def _cancel_active_artwork_search(self, *, status_message=None, log_message=None, update_controls=True):
        job_id = self.active_find_job_id
        if job_id is None:
            return False
        already_requested = job_id in getattr(self, 'canceled_find_jobs', set())
        active_key = self.active_find_album_key
        if active_key == 'BATCH':
            active_key = self.current_album_key
        self.canceled_find_jobs.add(job_id)
        if self.find_stop_event:
            self.find_stop_event.set()

        # Stop is a UI command, so the UI must stop immediately.  Older builds
        # kept the search marked active until the provider thread returned; that
        # made Stop feel slow and could leave the row to fall out of filtered
        # views after the delayed FIND_DONE event.  We now detach the UI from
        # the job at once, while the worker cooperatively notices the stop flag
        # and any late events are ignored because the job id remains in
        # canceled_find_jobs.
        self._begin_action_transition_guard(900, 'artwork search stop')
        self._pin_album_in_current_filter(active_key, reason='stopped artwork search')
        self.active_find_job_id = None
        self.active_find_album_key = None
        self.active_find_mode = None
        self.active_search_album_keys.clear()
        self.active_search_album_status.clear()
        self.active_search_album_labels.clear()
        self.active_search_album_original_buckets.clear()
        self.active_search_batch_total = 0

        try:
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            if active_key and active_key == self.current_album_key:
                self.load_album_for_review(active_key)
        except Exception:
            pass

        if status_message:
            self.status_var.set(status_message if not already_requested else 'Artwork search is already stopped. Late provider results will be ignored.')
        if log_message and not already_requested:
            self.log_msg(log_message)
        self.set_status_dot('#26b53f')
        if update_controls:
            has_candidate = bool(self.current_candidate())
            has_album = bool(self.active_album_info())
            self.set_review_button_states(has_candidate=has_candidate, has_album=has_album)
            self.refresh_queue_tab()
            self.refresh_footer()
        return True

    def stop_artwork_search(self):
        self._cancel_active_artwork_search(
            status_message='Artwork search stopped. Late provider results will be ignored.',
            log_message='\nArtwork search stop requested. Any already saved options remain in the queue.\n',
            update_controls=True,
        )

    def active_album_info(self):
        c = self.current_candidate()
        if c:
            return {
                'artist': c.get('artist', ''),
                'album': c.get('album', ''),
                'album_key': c.get('album_key'),
                'album_path': c.get('album_folder'),
            }
        if self.current_album_key:
            album = self.current_album_info or db.get_album(self.current_album_key)
            if album:
                return {
                    'artist': album.get('artist', ''),
                    'album': album.get('album', ''),
                    'album_key': album.get('album_key'),
                    'album_path': album.get('album_path'),
                }
        return None

    def _art_cache_filebase(self, album):
        raw = album.get('album_key') or album.get('album_path') or ''
        digest = hashlib.sha1(raw.encode('utf-8', errors='ignore')).hexdigest()
        return PREVIEW_CACHE_DIR / digest

    def _clear_current_art_cache(self, album_or_key, *, remove_files=False):
        """Clear cached embedded-art previews for one album.

        Current-art previews are cached because NAS/SMB tag reads are slow.
        However, after an embed/convert or an explicit Refresh from Disk the
        cache must be treated as stale even if the NAS/SMB mtime visible to the
        Mac has not changed yet.  Removing both the memory entry and the on-disk
        preview avoids showing a previous cover for the selected album.
        """
        if not album_or_key:
            return
        if isinstance(album_or_key, dict):
            keys = [album_or_key.get('album_key'), album_or_key.get('album_path')]
        else:
            keys = [str(album_or_key)]
        for key in [k for k in keys if k]:
            try:
                self._clear_current_art_cache(key, remove_files=True)
            except Exception:
                pass
            if remove_files:
                try:
                    base = self._art_cache_filebase({'album_key': key})
                    for suffix in ('.json', '.jpg'):
                        fp = base.with_suffix(suffix)
                        if fp.exists():
                            fp.unlink()
                except Exception:
                    pass
        if remove_files:
            try:
                self.preview_photo_cache.clear()
            except Exception:
                pass

    def _current_art_source_file(self, album):
        album_path = album.get('album_path') or ''
        if not album_path:
            return None
        example = album.get('example_file') or ''
        if example:
            fp = os.path.join(album_path, example)
            if os.path.exists(fp):
                return fp
        for fp in iter_music_files(album_path):
            return fp
        return None

    def current_art_info(self, album, *, force_refresh=False):
        """Return embedded-current-art preview info, cached after first read.

        Network/NAS albums can be slow to re-open. This stores a preview JPEG and
        metadata under Application Support so selecting the same album again does
        not repeatedly parse the music file just to redraw the current cover.
        """
        if not album:
            return None
        album_key = album.get('album_key') or album.get('album_path') or ''
        if not force_refresh and album_key in self.current_art_cache:
            info = self.current_art_cache[album_key]
            # Even when the preview image is cached, still sync the cached
            # embedded-art metadata back into the selected album/queue row.
            # Otherwise a previously cached album can display real artwork in
            # the preview while the queue/details still say Missing.
            self._sync_album_current_artwork_metadata(album, info)
            return info
        source_file = self._current_art_source_file(album)
        if not source_file or not os.path.exists(source_file):
            return None
        try:
            source_stat = os.stat(source_file)
            base = self._art_cache_filebase(album)
            meta_path = base.with_suffix('.json')
            image_path = base.with_suffix('.jpg')
            if not force_refresh and meta_path.exists() and image_path.exists():
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                # Cache schema v2 requires both size and nanosecond mtime.  Older
                # cache files only stored coarse mtime, which was not reliable on
                # NAS/SMB immediately after embedding artwork and could show the
                # previous cover.  Force one rebuild for old cache records.
                same_file = meta.get('source_file') == source_file
                same_size = int(meta.get('source_size') or -1) == int(source_stat.st_size)
                same_mtime_ns = int(meta.get('source_mtime_ns') or -1) == int(getattr(source_stat, 'st_mtime_ns', 0) or 0)
                if int(meta.get('cache_version') or 0) >= 2 and same_file and same_size and same_mtime_ns:
                    info = {
                        'width': meta.get('width'), 'height': meta.get('height'),
                        'image_path': str(image_path), 'size_bytes': meta.get('size_bytes'),
                        'source_file': source_file,
                    }
                    self._sync_album_current_artwork_metadata(album, info)
                    self.current_art_cache[album_key] = info
                    return info
        except Exception:
            pass
        arts = embedded_artwork(source_file)
        if not arts:
            self._clear_current_art_cache(album_key, remove_files=force_refresh)
            if force_refresh:
                self._record_current_artwork_read(album, None, source='disk refresh')
            return None
        art = arts[0]
        info = {
            'width': art.get('width'), 'height': art.get('height'),
            'bytes': art.get('bytes'), 'source_file': source_file,
            'format': art.get('format') or '',
            'compatible': bool(art.get('compatible')),
            'compatibility_issue': art.get('compatibility_issue') or '',
        }
        try:
            PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            base = self._art_cache_filebase(album)
            meta_path = base.with_suffix('.json')
            image_path = base.with_suffix('.jpg')
            from io import BytesIO
            img = Image.open(BytesIO(art.get('bytes') or b''))
            img.load()
            preview = img.convert('RGB')
            preview.thumbnail((1200, 1200))
            preview.save(image_path, 'JPEG', quality=90)
            st = os.stat(source_file)
            meta = {
                'cache_version': 2,
                'source_file': source_file,
                'source_mtime_ns': int(getattr(st, 'st_mtime_ns', 0) or 0),
                'source_size': int(st.st_size),
                # Keep old mtime field only for human/debug readability.
                'mtime': st.st_mtime,
                'width': art.get('width'), 'height': art.get('height'),
                'size_bytes': len(art.get('bytes') or b''),
            }
            meta_path.write_text(json.dumps(meta, indent=2), encoding='utf-8')
            info = {'width': art.get('width'), 'height': art.get('height'), 'image_path': str(image_path), 'size_bytes': meta['size_bytes'], 'source_file': source_file, 'format': art.get('format') or '', 'compatible': bool(art.get('compatible')), 'compatibility_issue': art.get('compatibility_issue') or ''}
        except Exception:
            pass
        self._sync_album_current_artwork_metadata(album, info)
        self._record_current_artwork_read(album, info, source='disk refresh' if force_refresh else 'disk read')
        self.current_art_cache[album_key] = info
        return info

    def _record_current_artwork_read(self, album, info=None, *, source='disk read'):
        """Store a small timestamped note showing when current artwork was read."""
        if not album:
            return
        key = album.get('album_key')
        if not key:
            return
        now_txt = db.now()
        if not info:
            note = {
                'checked_at': now_txt,
                'source': source or 'disk read',
                'status': 'missing',
                'dimensions': 'Missing',
            }
        else:
            w = info.get('width') or ''
            h = info.get('height') or ''
            dims = f'{w}×{h}' if w and h else 'Missing'
            note = {
                'checked_at': now_txt,
                'source': source or 'disk read',
                'status': 'ok' if w and h else 'missing',
                'dimensions': dims,
                'source_file': info.get('source_file') or '',
                'format': info.get('format') or '',
                'compatible': bool(info.get('compatible')) if 'compatible' in info else None,
                'compatibility_issue': info.get('compatibility_issue') or '',
            }
        try:
            db.update_album_notes(key, {'current_artwork_disk_read': note})
        except Exception:
            pass


    def reload_current_artwork_from_disk(self):
        """Visible one-click refresh for the current embedded-art preview."""
        info = self.active_album_info()
        if not info or not info.get('album_key'):
            return
        key = info.get('album_key')
        album = db.get_album(key) or {
            'album_key': key,
            'artist': info.get('artist', ''),
            'album': info.get('album', ''),
            'album_path': info.get('album_path') or '',
        }
        self._clear_current_art_cache(album, remove_files=True)
        fresh = self.current_art_info(album, force_refresh=True)
        if fresh:
            self.current_old_art_info = fresh
            self.current_old_img = self.show_image(self.old_label, fresh.get('image_path') or fresh.get('bytes'), maxsize=(self.art_px, self.art_px))
            self.old_size_var.set(self.current_size_label(fresh.get('width'), fresh.get('height')))
            msg = f'Reloaded current artwork from disk: {fresh.get("width", "?")}×{fresh.get("height", "?")}.'
        else:
            self.current_old_art_info = None
            self.set_preview_text(self.old_label, 'No embedded artwork')
            self.old_size_var.set('Missing')
            msg = 'Reloaded current artwork from disk: no embedded artwork found.'
        try:
            self.load_album_for_review(key)
        except Exception:
            self.refresh_queue_tab()
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.refresh_footer()

    def _sync_album_current_artwork_metadata(self, album, info):
        """Repair queue Current size from embedded artwork seen during review.

        Albums can be marked Good/Approved after artwork is embedded or changed
        outside a full scan.  In that case the review pane may read the real
        embedded artwork, while the queue still shows stale database values such
        as Missing.  When we have just read valid embedded artwork, quietly sync
        the queue metadata so the Current column matches what is actually in the
        music file.
        """
        if not album or not info:
            return
        album_key = album.get('album_key')
        if not album_key:
            return
        try:
            width = int(info.get('width') or 0)
            height = int(info.get('height') or 0)
        except Exception:
            return
        if width <= 0 or height <= 0:
            return
        try:
            old_w = album.get('width')
            old_h = album.get('height')
            if old_w is not None and old_h is not None and int(old_w) == width and int(old_h) == height:
                return
        except Exception:
            pass
        changed = False
        try:
            old_w = album.get('width')
            old_h = album.get('height')
            changed = old_w is None or old_h is None or int(old_w or 0) != width or int(old_h or 0) != height
        except Exception:
            changed = True
        try:
            db.update_album_path(
                album_key,
                album.get('album_path') or '',
                example_file=info.get('source_file') or album.get('example_file'),
                width=width,
                height=height,
            )
            album['width'] = width
            album['height'] = height
            if info.get('source_file'):
                album['example_file'] = info.get('source_file')
            if changed:
                # Refresh the visible queue row after repairing stale Current
                # metadata.  Defer with after_idle so this can be called safely
                # while the review pane is still being redrawn.
                try:
                    self.root.after_idle(self.refresh_queue_tab)
                except Exception:
                    pass
        except Exception:
            pass

    def _canvas_visible_size(self, canvas, fallback=None):
        """Return the actual drawable canvas size, not just the requested size.

        Tk can squeeze a Canvas inside a tight grid. If the preview image is
        scaled to the requested size instead of the visible size, the image can
        be clipped. This helper keeps artwork previews scaled to whatever the
        user can actually see.
        """
        fallback = fallback or (self.art_px, self.art_px)
        try:
            canvas.update_idletasks()
        except Exception:
            pass
        try:
            w = int(canvas.winfo_width() or 0)
            h = int(canvas.winfo_height() or 0)
        except Exception:
            w = h = 0
        if w <= 2:
            try:
                w = int(float(canvas.cget('width')))
            except Exception:
                w = int(fallback[0])
        if h <= 2:
            try:
                h = int(float(canvas.cget('height')))
            except Exception:
                h = int(fallback[1])
        return max(24, w), max(24, h)

    def set_preview_text(self, canvas, text):
        canvas.delete('all')
        w, h = self._canvas_visible_size(canvas)
        canvas.create_text(w // 2, h // 2, text=text, fill='#444444', anchor='center', width=max(80, w - 16))

    def _reset_review_canvas_style(self, canvas):
        try:
            canvas.configure(bg='white', highlightthickness=1, highlightbackground='#cfcfcf')
        except Exception:
            pass

    def _set_candidate_placeholder(self, text='No candidate yet'):
        try:
            # Empty candidate state should fade into the review area instead of
            # looking like a second bordered artwork panel. Real artwork restores
            # the normal canvas style when displayed.
            self.new_label.configure(bg='#f2f2f2', highlightthickness=0)
        except Exception:
            pass
        self.set_preview_text(self.new_label, text)

    def _set_options_empty_state(self, primary='No options yet', secondary='Find Artwork to search'):
        """Show a lighter empty state in the options panel instead of a heavy blank list."""
        try:
            self.cand_canvas.configure(bg='#f2f2f2', highlightthickness=0)
            self.cand_inner.configure(bg='#f2f2f2')
            self.cand_canvas.delete('empty_state')
            self.cand_canvas.update_idletasks()
            w = max(120, self.cand_canvas.winfo_width() or int(self.cand_canvas.cget('width')))
            h = max(90, self.cand_canvas.winfo_height() or int(self.cand_canvas.cget('height')))
            self.cand_canvas.create_text(
                w // 2,
                max(42, h // 2 - 10),
                text=primary,
                fill='#666666',
                anchor='center',
                width=max(100, w - 28),
                font=('TkDefaultFont', 10, 'bold'),
                tags=('empty_state',),
            )
            self.cand_canvas.create_text(
                w // 2,
                max(66, h // 2 + 14),
                text=secondary,
                fill='#777777',
                anchor='center',
                width=max(100, w - 28),
                font=('TkDefaultFont', 9),
                tags=('empty_state',),
            )
            self.cand_canvas.configure(scrollregion=(0, 0, w, h))
            try:
                self.cand_canvas.yview_moveto(0)
            except Exception:
                pass
        except Exception:
            pass

    def _reset_options_panel_style(self):
        try:
            self.cand_canvas.delete('empty_state')
            self.cand_canvas.configure(bg='#f7f7f7', highlightthickness=1, highlightbackground='#d8d8d8')
            self.cand_inner.configure(bg='#f7f7f7')
        except Exception:
            pass

    def show_image(self, canvas, path_or_bytes, maxsize=None):
        try:
            if not path_or_bytes:
                raise ValueError('No image')
            view_w, view_h = self._canvas_visible_size(canvas, maxsize or (self.art_px, self.art_px))
            draw_w = max(20, view_w - 4)
            draw_h = max(20, view_h - 4)
            cache_key = None
            if isinstance(path_or_bytes, (str, os.PathLike)):
                try:
                    st = os.stat(path_or_bytes)
                    cache_key = (str(path_or_bytes), int(st.st_mtime_ns), int(st.st_size), draw_w, draw_h)
                    photo = getattr(self, 'preview_photo_cache', {}).get(cache_key)
                    if photo is not None:
                        self._reset_review_canvas_style(canvas)
                        canvas.delete('all')
                        canvas.create_image(view_w // 2, view_h // 2, image=photo, anchor='center')
                        return photo
                except Exception:
                    cache_key = None
            if isinstance(path_or_bytes, (bytes, bytearray)):
                from io import BytesIO
                img = Image.open(BytesIO(path_or_bytes))
            else:
                img = Image.open(path_or_bytes)
            img.load()
            preview = _high_quality_fit_image(img, (draw_w, draw_h), allow_upscale=False)
            photo = ImageTk.PhotoImage(preview)
            if cache_key is not None:
                cache = getattr(self, 'preview_photo_cache', None)
                if cache is not None:
                    if len(cache) > 80:
                        cache.clear()
                    cache[cache_key] = photo
            self._reset_review_canvas_style(canvas)
            canvas.delete('all')
            canvas.create_image(view_w // 2, view_h // 2, image=photo, anchor='center')
            return photo
        except Exception:
            self.set_preview_text(canvas, 'Preview unavailable')
            return None

    def thumbnail_photo(self, path, size=(64, 64)):
        try:
            st = os.stat(path)
            key = (str(path), int(st.st_mtime_ns), int(st.st_size), int(size[0]), int(size[1]))
            cache = getattr(self, 'thumbnail_photo_cache', None)
            if cache is not None and key in cache:
                return cache[key]
            img = Image.open(path)
            img.load()
            img = _high_quality_fit_image(img, size, allow_upscale=False)
            canvas = Image.new('RGB', size, 'white')
            x = (size[0] - img.width) // 2
            y = (size[1] - img.height) // 2
            if img.mode in ('RGBA', 'LA'):
                canvas.paste(img.convert('RGBA'), (x, y), img.convert('RGBA'))
            else:
                canvas.paste(img.convert('RGB'), (x, y))
            photo = ImageTk.PhotoImage(canvas)
            if cache is not None:
                if len(cache) > 400:
                    cache.clear()
                cache[key] = photo
            return photo
        except Exception:
            return None

    def _short_source_label(self, candidate):
        """Compact source-only label for the artwork option list.

        Size/source-detail belongs on the second line, so the first line stays
        readable in the narrow selector panel. Keep provider names short enough
        for the row width, especially MusicBrainz.
        """
        source = candidate.get('source', '') or 'Source'
        if source == 'MusicBrainz':
            return 'MB'
        if source == 'iTunes':
            return 'Apple Music'
        if source == 'Manual Import':
            return 'Manual'
        if source == 'fanart.tv':
            return 'fanart.tv'
        return source

    def _candidate_score_label(self, score):
        try:
            score = int(score or 0)
        except Exception:
            score = 0
        if score >= 85:
            return 'Good'
        if score >= 60:
            return 'Usable'
        return 'Weak'

    def _short_warning_text(self, candidate):
        """Short third-line result for candidate cards.

        Keep the card clean and stable: source, dimensions/score, then a brief
        target/result phrase.  Detailed warnings remain in the details pane.
        """
        result = self._candidate_target_result(candidate)
        try:
            score = int((candidate or {}).get('score') or 0)
        except Exception:
            score = 0
        warnings = ' '.join(str(w).lower() for w in ((candidate or {}).get('warnings') or []))
        if result in ('Meets target', 'Target met') and score >= 85 and not warnings:
            return 'Excellent'
        if 'not square' in warnings or 'stretched' in warnings or 'aspect' in warnings:
            return f'{result} · check shape'
        if 'small file' in warnings or 'small size' in warnings:
            return f'{result} · check quality'
        return result

    def render_candidate_list(self):
        for w in self.cand_inner.winfo_children():
            w.destroy()
        self.thumb_refs.clear()
        cands = self.current_candidates()
        self.cand_count_label.configure(text=f'{len(cands)} option' + ('' if len(cands) == 1 else 's'))
        best_idx = 0 if cands else -1
        self.candidate_row_widgets = []
        if not cands:
            self._set_options_empty_state()
            return
        self._reset_options_panel_style()
        for i, c in enumerate(cands):
            selected = i == self.candidate_index
            bg = '#d7e9ff' if selected else '#ffffff'
            row_height = 58 if self.compact_ui else 66
            row = tk.Frame(self.cand_inner, bg=bg, bd=1, relief='solid' if selected else 'groove', takefocus=1 if selected else 0, height=row_height)
            row.pack(fill='x', padx=2, pady=3)
            row.pack_propagate(False)
            row.bind('<Button-1>', lambda e, ix=i: self.select_candidate(ix))
            row.bind('<Up>', lambda e: self._candidate_key_move(-1), add='+')
            row.bind('<Down>', lambda e: self._candidate_key_move(1), add='+')
            row.bind('<space>', self.toggle_candidate_preview, add='+')
            row.bind('<KeyPress-space>', self.toggle_candidate_preview, add='+')
            self._bind_option_scrolling(row)
            self.candidate_row_widgets.append(row)

            thumb_size = 40 if self.compact_ui else 46
            thumb = self.thumbnail_photo(c.get('image_path'), size=(thumb_size, thumb_size))
            self.thumb_refs.append(thumb)
            img = tk.Label(row, image=thumb, bg=bg, width=thumb_size, height=thumb_size)
            img.pack(side='left', padx=3 if self.compact_ui else 4, pady=3 if self.compact_ui else 4)
            img.bind('<Button-1>', lambda e, ix=i: self.select_candidate(ix))
            img.bind('<space>', self.toggle_candidate_preview, add='+')
            img.bind('<KeyPress-space>', self.toggle_candidate_preview, add='+')
            img.bind('<Up>', lambda e: self._candidate_key_move(-1), add='+')
            img.bind('<Down>', lambda e: self._candidate_key_move(1), add='+')
            self._bind_option_scrolling(img)

            score = int(c.get('score') or 0)
            source_label = self._short_source_label(c)
            best_txt = ' · Best' if i == best_idx and len(cands) > 1 else ''
            source_line = f'{i+1}. {source_label}{best_txt}'
            size_line = f'{c.get("width", "?")}×{c.get("height", "?")}'
            embed_size = self._candidate_embed_size_label(c)
            original_size = f'{c.get("width", "?")}×{c.get("height", "?")}'
            warning_line = f'Embeds {embed_size}' if embed_size and embed_size != 'unknown size' and embed_size != original_size else self._short_warning_text(c)

            # Keep cards fixed-height and single-line: provider, size/score,
            # target result.  Full warning text stays in the details pane.
            text_box = tk.Frame(row, bg=bg)
            text_box.pack(side='left', fill='x', expand=True, padx=(2, 3), pady=(1 if self.compact_ui else 2, 1 if self.compact_ui else 2))
            text_width_px = max(120, self.candidate_list_w - 64)
            char_limit = max(22, min(42, int(text_width_px / 6.2)))
            for child_text, font in (
                (self._ellipsize_end(source_line, char_limit), ('TkDefaultFont', 8 if self.compact_ui else 9, 'bold')),
                (self._ellipsize_end(size_line, char_limit), ('TkDefaultFont', 8 if self.compact_ui else 9)),
                (self._ellipsize_end(warning_line, char_limit), ('TkDefaultFont', 8 if self.compact_ui else 9)),
            ):
                lab = tk.Label(
                    text_box,
                    text=child_text,
                    justify='left',
                    anchor='w',
                    bg=bg,
                    font=font,
                    wraplength=0,
                    height=1,
                )
                lab.pack(fill='x', anchor='w')
                lab.bind('<Button-1>', lambda e, ix=i: self.select_candidate(ix))
                lab.bind('<space>', self.toggle_candidate_preview, add='+')
                lab.bind('<KeyPress-space>', self.toggle_candidate_preview, add='+')
                lab.bind('<Up>', lambda e: self._candidate_key_move(-1), add='+')
                lab.bind('<Down>', lambda e: self._candidate_key_move(1), add='+')
                self._bind_option_scrolling(lab)
            text_box.bind('<Button-1>', lambda e, ix=i: self.select_candidate(ix))
            text_box.bind('<space>', self.toggle_candidate_preview, add='+')
            text_box.bind('<KeyPress-space>', self.toggle_candidate_preview, add='+')
            text_box.bind('<Up>', lambda e: self._candidate_key_move(-1), add='+')
            text_box.bind('<Down>', lambda e: self._candidate_key_move(1), add='+')
            self._bind_option_scrolling(text_box)
        self.cand_canvas.configure(scrollregion=self.cand_canvas.bbox('all'))
        # Re-rendering the option cards used to leave the canvas scrolled at
        # the top, so moving to option 4/7 could still show option 1 as the
        # visual focus. After Tk has measured the rebuilt rows, keep the
        # selected artwork option highlighted, keyboard-focused, and visible.
        self.cand_canvas.after_idle(self._focus_selected_candidate_option)

    def _focus_selected_candidate_option(self):
        """Keep the selected candidate card visible inside the option list.

        The candidate selector is a Canvas containing normal Tk frames, not a
        Treeview/Listbox, so Tk does not automatically call see() for us.
        Whenever the selected candidate changes and the cards are rebuilt,
        scroll just enough to keep the active row in view.
        """
        rows = getattr(self, 'candidate_row_widgets', [])
        if not rows or not hasattr(self, 'cand_canvas'):
            return
        idx = max(0, min(int(getattr(self, 'candidate_index', 0) or 0), len(rows) - 1))
        row = rows[idx]
        try:
            self.cand_inner.update_idletasks()
            self.cand_canvas.update_idletasks()
            row.focus_set()
            row_y = row.winfo_y()
            row_h = max(1, row.winfo_height())
            row_bottom = row_y + row_h
            visible_top = self.cand_canvas.canvasy(0)
            visible_h = max(1, self.cand_canvas.winfo_height())
            visible_bottom = visible_top + visible_h
            scroll_bbox = self.cand_canvas.bbox('all')
            if not scroll_bbox:
                return
            content_h = max(1, scroll_bbox[3] - scroll_bbox[1])
            max_top = max(0, content_h - visible_h)
            new_top = visible_top
            if row_y < visible_top:
                new_top = row_y
            elif row_bottom > visible_bottom:
                new_top = row_bottom - visible_h
            new_top = max(0, min(max_top, new_top))
            if content_h > visible_h:
                self.cand_canvas.yview_moveto(new_top / content_h)
        except Exception:
            pass



    def _set_action_result(self, text='', *, log=False):
        """Show the final result of the last user action in one calm place."""
        text = str(text or '').strip()
        try:
            self.action_result_var.set(text)
            if hasattr(self, 'action_result_label'):
                if text and not getattr(self, 'action_result_visible', False):
                    self.action_result_label.pack(fill='x', pady=(4, 4), before=self.details)
                    self.action_result_visible = True
                elif not text and getattr(self, 'action_result_visible', False):
                    self.action_result_label.pack_forget()
                    self.action_result_visible = False
        except Exception:
            pass
        if log and text:
            try:
                self.log_msg(text + '\n')
            except Exception:
                pass

    def _queue_consistency_check(self, album_key=None, *, repair=False, context=''):
        """Check that stored status, candidates, and shared evaluator agree.

        This is intentionally quiet in normal use; it logs to Verbose only and
        repairs the small class of stale states that have caused queue/filter
        bugs: Review with no candidates, unneeded Convert prompts, or active
        candidates while an album is not in Review.
        """
        issues = []
        try:
            albums = [db.get_album(album_key)] if album_key else db.load_albums(actionable_only=False)
            albums = [a for a in albums if a]
        except Exception as exc:
            self.log_verbose(f'  Queue consistency check skipped: {exc}\n')
            return []
        try:
            candidate_counts = db.active_candidate_counts([a.get('album_key') for a in albums])
        except Exception:
            candidate_counts = {}
        for album in albums:
            key = album.get('album_key')
            stored = album.get('status') or ''
            try:
                active_candidates = int(candidate_counts.get(key, album.get('candidate_count') or 0) or 0)
            except Exception:
                active_candidates = int(album.get('candidate_count') or 0)
            try:
                evaluated, reason = evaluate_album_record(album, candidate_count=active_candidates, settings=getattr(self, 'settings', None))
            except Exception as exc:
                issues.append((key, stored, 'evaluator error', str(exc)))
                continue
            mismatch = False
            if stored == 'candidate_found' and active_candidates <= 0:
                mismatch = True
                issues.append((key, stored, evaluated, 'Review status has no active candidates'))
            elif stored == 'not_square_artwork' and evaluated != 'not_square_artwork':
                mismatch = True
                issues.append((key, stored, evaluated, reason or 'Not Square status no longer required'))
            elif stored == 'incompatible_artwork' and evaluated != 'incompatible_artwork':
                mismatch = True
                issues.append((key, stored, evaluated, reason or 'Convert status no longer required'))
            elif active_candidates > 0 and stored not in ('candidate_found', 'approved', 'already_good', 'reviewed_skipped', 'ignored'):
                mismatch = True
                issues.append((key, stored, evaluated, f'{active_candidates} active candidate(s) exist'))
            elif stored in ('already_good', 'approved') and evaluated not in ('already_good', 'approved'):
                # Do not silently rewrite Good in normal refreshes. Log it so the
                # cause is visible during bug reports; scan/rescan will reopen it
                # when disk facts really disagree.
                issues.append((key, stored, evaluated, reason or 'Good state disagrees with stored facts'))
            if repair and mismatch:
                try:
                    db.evaluate_and_set_album_state(key, candidate_count=active_candidates, settings=getattr(self, 'settings', None))
                except Exception as exc:
                    issues.append((key, stored, 'repair failed', str(exc)))
        if issues:
            label = f' ({context})' if context else ''
            self.log_verbose(f'  Queue consistency{label}: {len(issues)} issue(s) noted.\n')
            for key, stored, evaluated, reason in issues[:12]:
                self.log_verbose(f'    {key}: stored={stored or "—"}, evaluated={evaluated or "—"}; {reason}\n')
            if len(issues) > 12:
                self.log_verbose(f'    …and {len(issues) - 12} more.\n')
        return issues

    def _needs_convert_reason(self, album):
        if not album:
            return ''
        try:
            reason = needs_convert_reason(album.get('notes_json') or album.get('notes'))
            if reason:
                return reason
        except Exception:
            pass
        try:
            status, reason = evaluate_album_state(
                album.get('width'), album.get('height'), album.get('notes_json') or album.get('notes'),
                current_status=album.get('status') or '',
                candidate_count=album.get('candidate_count') or 0,
            )
            if status == 'incompatible_artwork':
                return reason or ''
        except Exception:
            pass
        return ''

    def _scan_summary_text(self):
        try:
            counts = db.album_counts()
        except Exception:
            counts = {}
        good = int(counts.get('already_good', 0) or 0)
        approved = int(counts.get('approved', 0) or 0)
        convert = int(counts.get('incompatible_artwork', 0) or 0)
        not_square = int(counts.get('not_square_artwork', 0) or 0)
        review = int(counts.get('candidate_found', 0) or 0)
        needs = int(counts.get('needs_review', 0) or 0) + int(counts.get('missing_artwork', 0) or 0) + int(counts.get('no_candidate', 0) or 0)
        handled = approved + int(counts.get('reviewed_skipped', 0) or 0) + int(counts.get('ignored', 0) or 0)
        total = sum(int(v or 0) for v in counts.values()) if counts else 0
        return f'Scan complete: {total} albums · {good} good · {approved} approved · {review} to review · {needs} need search · {not_square} not square · {convert} need convert · {handled} handled'

    def _details_section(self, title, lines):
        if isinstance(lines, str):
            lines = [lines]
        clean = [str(line) for line in (lines or []) if str(line).strip()]
        if not clean:
            return ''
        return title + '\n' + '\n'.join(clean)

    def _set_details_sections(self, sections):
        """Render the details pane as quiet native sections.

        Older builds used one monospaced report-style block.  The review pane is
        calmer if headings are bold and values use the normal system font, while
        still keeping the information copyable in a plain Text widget.
        """
        self.details.delete('1.0', 'end')
        first = True
        for title, lines in sections:
            if isinstance(lines, str):
                lines = [lines]
            clean = [str(line) for line in (lines or []) if str(line).strip()]
            if not clean:
                continue
            if not first:
                self.details.insert('end', '\n')
            first = False
            self.details.insert('end', str(title).strip() + '\n', ('section_heading',))
            for line in clean:
                self.details.insert('end', line + '\n', ('section_body',))

    def search_summary_text(self, album):
        notes = (album or {}).get('notes_json') or {}
        lines = notes.get('last_search_summary') or []
        if not lines:
            return ''
        stamp = notes.get('last_search_at') or ''
        out = ['Last provider search' + (f' ({stamp})' if stamp else '') + ':']
        out.extend(f'- {line}' for line in lines[:8])
        return '\n'.join(out)

    def _no_options_reason_lines(self, album, limit=6):
        notes = (album or {}).get('notes_json') or {}
        lines = notes.get('last_search_summary') or []
        if not lines:
            return ['No provider result details were saved for the last search.']
        return [str(line) for line in lines[:limit] if str(line).strip()]

    def _brief_no_options_reason(self, album_key):
        album = db.get_album(album_key) if album_key else None
        lines = self._no_options_reason_lines(album, limit=3)
        if not lines:
            return ''
        # Keep the status bar short; the full provider breakdown is in details.
        return '; '.join(lines)[:220]

    def _short_display_path(self, path, limit=92):
        path = str(path or '').strip()
        if not path or len(path) <= limit:
            return path
        try:
            parts = Path(path).parts
            if len(parts) >= 3:
                tail = os.path.join(*parts[-3:])
                short = '…/' + tail
                if len(short) <= limit:
                    return short
        except Exception:
            pass
        keep = max(12, (limit - 1) // 2)
        return path[:keep] + '…' + path[-keep:]

    def _album_folder_lines(self, album):
        """Return a compact album folder path for the details pane."""
        path = ''
        if isinstance(album, dict):
            path = album.get('album_path') or album.get('album_folder') or ''
        path = self._short_display_path(path)
        return [path] if path else ['—']

    def _status_reason_lines(self, album, status=None, reason=None):
        """Return consistent status/why/next-action lines for details pane."""
        album = album or {}
        if not status:
            try:
                status, reason = self._evaluate_queue_album(album)
            except Exception:
                status, reason = album.get('status') or '', reason or ''
        if not reason:
            try:
                notes = album.get('notes_json') or {}
                state_eval = (notes.get('state_evaluation') or {}) if isinstance(notes, dict) else {}
                reason = state_eval.get('reason') or ''
            except Exception:
                reason = ''
        lines = [f'Status: {self.queue_status_label(status)}']
        if reason:
            lines.append(f'Why: {reason}')
        else:
            lines.append(f'Why: {self.friendly_status(status)}')
        next_text = self._next_action_text(album, status, has_candidate=bool(self.current_candidate()))
        if next_text:
            lines.append(f'Next action: {next_text}')
        return lines

    def _next_action_text(self, album, status=None, *, has_candidate=False):
        """Short human next-action text that mirrors the primary button."""
        album = album or {}
        status = status or album.get('status') or ''
        if has_candidate or status == 'candidate_found':
            return 'review the downloaded option, then embed it or reject it.'
        if status == 'not_square_artwork':
            return 'Convert/Save to square the current artwork, or search/import a replacement.'
        if status == 'incompatible_artwork':
            return 'Convert/Save to make the artwork baseline JPEG and update cover.jpg if needed.'
        if status in ('missing_artwork', 'needs_review', 'no_candidate', 'pending'):
            return 'find artwork, choose a release, use Google Images, or import an image.'
        if status in ('already_good', 'approved'):
            return 'no action needed; use Rework Album only if you want to replace it.'
        if status == 'reviewed_skipped':
            return 'skipped; use Rework Album if you want to process it again.'
        if status == 'ignored':
            return 'ignored; use Rework Album if you want it back in the workflow.'
        return 'choose an action from the Actions menu.'

    def _deep_check_detail_lines(self, album):
        """Return consistent Deep Check detail lines for the selected album."""
        album = album or {}
        deep = effective_deep_file_check(album.get('notes_json') or album.get('notes'))
        if not isinstance(deep, dict) or not deep.get('enabled'):
            return []
        def count(key):
            try:
                return int(deep.get(key) or 0)
            except Exception:
                return 0
        checked = count('checked_files')
        target = deep.get('target_size') or get_preferred_artwork_size(self.settings)
        bits = []
        for key, label in (
            ('ok_count', 'OK'),
            ('missing_count', 'missing'),
            ('below_target_count', 'below target'),
            ('non_square_count', 'not square'),
            ('incompatible_count', 'not baseline'),
            ('unreadable_count', 'unreadable'),
        ):
            n = count(key)
            if n:
                bits.append(f'{n} {label}')
        lines = [f'{checked} file(s) checked at {target}px · ' + (', '.join(bits) if bits else 'all OK')]
        first_file = deep.get('first_issue_file') or deep.get('first_non_square_file') or ''
        first_issue = deep.get('first_issue') or ''
        first_dims = deep.get('first_non_square_dimensions') or ''
        if first_file:
            detail = f'First problem: {first_file}'
            extras = []
            if first_issue:
                extras.append(first_issue)
            if first_dims:
                extras.append(first_dims)
            if extras:
                detail += ' · ' + ' · '.join(extras)
            lines.append(detail)
        return lines


    def _last_action_lines(self, album):
        notes = (album or {}).get('notes_json') or {}
        if not isinstance(notes, dict):
            return []
        compat = notes.get('artwork_compatibility') or {}
        approved = notes.get('approved_artwork') or {}
        folder = notes.get('album_folder_cover') or {}
        state_eval = notes.get('state_evaluation') or {}
        lines = []
        if isinstance(approved, dict) and approved.get('approved_at'):
            src = approved.get('source') or 'unknown source'
            dims = approved.get('embedded_dimensions') or approved.get('dimensions') or ''
            lines.append(f'Approved from {src}' + (f' · {dims}' if dims else '') + f' · {approved.get("approved_at")}')
        elif isinstance(compat, dict) and compat.get('converted_at'):
            converted = compat.get('converted_to') or 'baseline JPEG'
            lines.append(f'Converted/Saved embedded artwork · {converted} · {compat.get("converted_at")}')
        elif isinstance(folder, dict) and folder.get('saved_at'):
            lines.append(f'Saved folder cover · {folder.get("saved_at")}')
        partial = notes.get('partial_failure') or {}
        if isinstance(partial, dict) and partial.get('reason'):
            lines.append(f'Needs attention: {partial.get("reason")}')
        workflow = notes.get('workflow_state') or {}
        if isinstance(workflow, dict) and workflow.get('state'):
            lines.append(f'Workflow: {workflow.get("state")} — {workflow.get("reason") or "temporary"}')
        if isinstance(state_eval, dict) and state_eval.get('reason'):
            lines.append(f'State reason: {state_eval.get("reason")}')
        return lines

    def _approved_artwork_lines(self, album):
        notes = (album or {}).get('notes_json') or {}
        approved = notes.get('approved_artwork') or {}
        if not isinstance(approved, dict) or not approved:
            return []
        dims = approved.get('embedded_dimensions') or approved.get('dimensions') or ''
        source = approved.get('source') or 'unknown source'
        score = approved.get('score')
        when = approved.get('approved_at') or ''
        updated = approved.get('updated_files')
        total = approved.get('total_files')
        if dims and updated not in (None, '') and total not in (None, ''):
            parts = [f'Embedded {dims} into {updated}/{total} file(s)']
            parts.append(f'Approved from {source}')
        else:
            parts = [f'Approved from {source}' + (f' · {dims}' if dims else '')]
        if score not in (None, ''):
            parts.append(f'Score: {score}/100')
        if approved.get('verify_required'):
            verify_txt = 'verified' if approved.get('verified') else 'verification needed attention'
            summary = approved.get('verification_summary') or ''
            parts.append('Post-embed verification: ' + verify_txt + (f' — {summary}' if summary else ''))
        if when:
            parts.append(f'Date: {when}')
        return parts

    def show_album_without_candidate(self, album):
        self.current_album_info = album
        self.current_old_art_info = self.current_art_info(album)
        if self.current_old_art_info:
            self.current_old_img = self.show_image(self.old_label, self.current_old_art_info.get('image_path') or self.current_old_art_info.get('bytes'), maxsize=(self.art_px, self.art_px))
            self.old_size_var.set(self.current_size_label(self.current_old_art_info.get('width'), self.current_old_art_info.get('height')))
        else:
            self.current_old_img = None
            self.set_preview_text(self.old_label, 'No embedded artwork')
            self.old_size_var.set('Missing')
        self.current_img = None
        self._set_candidate_placeholder('No candidate yet')
        self.new_size_var.set('—')
        self.cand_count_label.configure(text='0 options')
        self.cand_nav_label.configure(text='')
        for w in self.cand_inner.winfo_children():
            w.destroy()
        self._set_options_empty_state()
        stored_status = album.get('status', '')
        status = stored_status
        try:
            status, _reason = evaluate_album_record(album, settings=getattr(self, 'settings', None))
        except Exception:
            status = stored_status
        current_txt = self._queue_current_label_for_album(album)
        if status == 'no_candidate':
            reason_lines = self._no_options_reason_lines(album, limit=2)
            next_lines = ['No saved provider options from the last search.']
            next_lines.extend(reason_lines[:2])
            next_lines.append('Try Search More, Choose Release, Google Images, or Import Image.')
        elif status == 'not_square_artwork':
            next_lines = ['Convert/Save the current artwork to make it square, or search/import a better cover.']
        elif status == 'incompatible_artwork':
            next_lines = ['Convert/Save the current artwork, or search for a better cover.']
        elif status == 'ignored':
            next_lines = ['Ignored. Use Actions to rework it later.']
        else:
            next_lines = ['Find Artwork, Choose Release, Google Images, or Import Image.']
        self._update_detail_summary(
            current=self._queue_current_label_for_album(album),
            candidate='No options',
            source='—',
            match=self.friendly_status(status),
        )
        current_lines = [current_txt]
        if album_has_not_square_artwork(album):
            ns_reason = not_square_reason(album)
            current_lines.append('Shape: not square' + (f' — {ns_reason}' if ns_reason else ''))
        if status in ('already_good', 'approved'):
            current_lines.append('Good because: ' + good_reason_from_notes(album))
        compat = ((album.get('notes_json') or {}).get('artwork_compatibility') or {}) if isinstance(album.get('notes_json'), dict) else {}
        if compat.get('needs_conversion'):
            current_lines.append(f'Compatibility: convert to baseline JPEG ({compat.get("issue") or "not baseline JPEG"})')
        folder_cover = ((album.get('notes_json') or {}).get('album_folder_cover') or {}) if isinstance(album.get('notes_json'), dict) else {}
        if folder_cover.get('needs_save') and folder_cover_required(self.settings):
            try:
                reason_for_convert = needs_convert_reason(album.get('notes_json') or album.get('notes'), self.settings)
            except Exception:
                reason_for_convert = folder_cover.get('issue') or 'missing'
            if reason_for_convert:
                current_lines.append(f'Folder cover: save/replace cover.jpg ({folder_cover.get("issue") or "missing"})')
        sections = [
            ('Folder', self._album_folder_lines(album)),
            ('Status', self._status_reason_lines(album, status)),
            ('Current Artwork', current_lines),
        ]
        verification_lines = self._verification_lines(album)
        if verification_lines:
            sections.append(('Verification', verification_lines))
        problem_lines = self._problem_file_detail_lines(album)
        if problem_lines:
            sections.append(('Problem Files', problem_lines))
        deep_lines = self._deep_check_detail_lines(album)
        if deep_lines:
            sections.append(('Deep Check', deep_lines))
        approved_lines = self._approved_artwork_lines(album)
        if approved_lines:
            sections.append(('Approved Artwork', approved_lines))
        last_lines = self._last_action_lines(album)
        if last_lines:
            sections.append(('Last Action', last_lines))
        if status == 'no_candidate':
            search_lines = self._no_options_reason_lines(album, limit=4)
            if search_lines:
                sections.append(('Last Search', search_lines))
        self._set_details_sections(sections)
        self._update_review_header(album, candidate=None, candidate_total=int(album.get('candidate_count') or 0))
        self.status_var.set(f'Selected: {album.get("artist", "")} — {album.get("album", "")}')
        if status == 'not_square_artwork':
            reason = not_square_reason(album)
            self._set_action_result(('Not Square' + (f': {reason}' if reason else '') + ' · Use Actions → Convert/Save Embedded Artwork.'))
        elif status == 'incompatible_artwork':
            reason = self._needs_convert_reason(album)
            self._set_action_result(('Needs Convert' + (f': {reason}' if reason else '') + ' · Use Actions → Convert/Save Embedded Artwork.'))
        else:
            self._set_action_result('')
        self.set_review_button_states(has_candidate=False, has_album=True)
        self.refresh_footer()

    def select_candidate(self, index):
        self.candidate_index = index
        self.show_current_album()
        self._focus_candidate_list()

    def shift_candidate(self, delta):
        cands = self.current_candidates()
        if not cands:
            return
        self.candidate_index = (self.candidate_index + delta) % len(cands)
        self.show_current_album()

    def show_current_album(self):
        c = self.current_candidate()
        if not c:
            self.clear('All available artwork options reviewed. You can resume scanning or load the saved queue later.')
            return
        cands = self.current_candidates()
        self.current_album_info = {
            'album_key': c.get('album_key'),
            'artist': c.get('artist', ''),
            'album': c.get('album', ''),
            'album_path': c.get('album_folder'),
        }
        self.set_review_button_states(has_candidate=True, has_album=True)
        self.cand_nav_label.configure(text='')
        self.render_candidate_list()

        album_rec = db.get_album(c.get('album_key')) or {'album_key': c.get('album_key'), 'album_path': c.get('album_folder')}
        old = self.current_art_info(album_rec)
        self.current_old_art_info = old
        if old:
            self.current_old_img = self.show_image(self.old_label, old.get('image_path') or old.get('bytes'), maxsize=(self.art_px, self.art_px))
            self.old_size_var.set(self.current_size_label(old.get('width'), old.get('height')))
        else:
            self.current_old_img = None
            self.set_preview_text(self.old_label, 'No embedded artwork')
            self.old_size_var.set('Missing')
        self.current_img = self.show_image(self.new_label, c.get('image_path'), maxsize=(self.art_px, self.art_px))
        surface_comparison = self._artwork_comparison_surface_label(old, c)
        details_comparison = self._artwork_comparison_label(old, c)
        self.new_size_var.set(self._ellipsize_end(surface_comparison, 28))

        target_size = get_preferred_artwork_size()
        mid_size = max(1, int(target_size * 0.6))
        if artwork_meets_target_size(c.get('width', 0), c.get('height', 0), target_size):
            badge = f'GREEN: {target_size}px+'
        elif c.get('width', 0) >= mid_size and c.get('height', 0) >= mid_size:
            badge = f'AMBER: {mid_size}px+'
        else:
            badge = 'RED: low-res'
        # Do not show same-cover/higher-res matches as warnings. That is
        # the common improvement workflow; the details pane still notes it
        # under Source metadata when available. Also filters older database
        # candidates saved by previous builds.
        warns = [w for w in (c.get('warnings') or []) if 'same image' not in str(w).lower()]
        score = int(c.get('score') or 0)
        score_summary = c.get('score_summary') or 'Artwork option'
        source = c.get('source', '')
        detail = c.get('source_detail') or ''
        source_label = f'{source} ({detail})' if detail else source
        is_best_match = self.candidate_index == 0
        source_page = self.source_page_url_from_candidate(c)
        album_rec = db.get_album(c.get('album_key')) or {}
        match_details = self.candidate_match_details(c, album_rec)
        match_head = 'High' if 'Match confidence: High' in match_details else ('Medium' if 'Match confidence: Medium' in match_details else ('Weak' if 'Match confidence: Weak' in match_details else '—'))
        self._update_detail_summary(
            current=self._queue_current_label_for_album(album_rec) if album_rec else (self._queue_current_label(old.get('width'), old.get('height')) if old else 'Missing'),
            candidate=f'{c.get("width", "?")}×{c.get("height", "?")} · {self._candidate_target_result(c)}',
            source=source_label,
            match=match_head,
        )
        match_lines = []
        if match_head and match_head != '—':
            match_lines.append(f'Match: {match_head}')
        if details_comparison:
            match_lines.append(f'Compare: {details_comparison}')
        if score_summary and score_summary != 'Artwork option':
            match_lines.append(f'Score: {score}/100 — {score_summary}')
        candidate_lines = [
            f'{c.get("width")}×{c.get("height")} · {self._candidate_target_result(c)} · {source_label}',
            self._candidate_will_embed_line(c) + ' baseline JPEG',
        ]
        lifecycle = c.get('candidate_state') or 'available'
        lifecycle_reason = c.get('state_reason') or ''
        if lifecycle and lifecycle != 'available':
            candidate_lines.append('Candidate state: ' + lifecycle.replace('_', ' ').title() + (f' · {lifecycle_reason}' if lifecycle_reason else ''))
        release_title = str(c.get('release_title') or '').strip()
        if release_title:
            candidate_lines.append('Release: ' + self._ellipsize_end(release_title, 90))
        if warns:
            candidate_lines.append('Warnings: ' + self._ellipsize_end(', '.join(warns), 90))
        current_lines = [
            self._queue_current_label_for_album(album_rec) if album_rec else ('Missing' if not old else f'{old.get("width", "?")}×{old.get("height", "?")}'),
        ]
        if album_has_not_square_artwork(album_rec):
            ns_reason = not_square_reason(album_rec)
            current_lines.append('Shape: not square' + (f' — {ns_reason}' if ns_reason else ''))
        try:
            status, status_reason = self._evaluate_queue_album(album_rec or {})
        except Exception:
            status, status_reason = (album_rec or {}).get('status') or 'candidate_found', ''
        sections = [
            ('Folder', self._album_folder_lines(album_rec)),
            ('Status', self._status_reason_lines(album_rec, status, status_reason)),
            ('Current Artwork', current_lines),
            ('Candidate', candidate_lines),
        ]
        if match_lines:
            sections.append(('Match', match_lines))
        verification_lines = self._verification_lines(album_rec)
        if verification_lines:
            sections.append(('Verification', verification_lines))
        problem_lines = self._problem_file_detail_lines(album_rec)
        if problem_lines:
            sections.append(('Problem Files', problem_lines))
        deep_lines = self._deep_check_detail_lines(album_rec)
        if deep_lines:
            sections.append(('Deep Check', deep_lines))
        self._set_details_sections(sections)
        self._update_review_header(album_rec, candidate=c, candidate_index=self.candidate_index, candidate_total=len(cands))
        self.status_var.set(f'Reviewing: {c.get("artist")} — {c.get("album")} ({self.candidate_index + 1}/{len(cands)})')
        self._set_action_result('')
        self.refresh_footer()


    def next_work_item(self):
        """Jump to the next visible queue row."""
        keys = [k for k in self.queue_navigation_keys() if k]
        if not keys:
            self.status_var.set('No albums are visible in the current queue view.')
            return 'break'
        try:
            start = keys.index(self.current_album_key) + 1 if self.current_album_key in keys else 0
        except Exception:
            start = 0
        key = keys[start % len(keys)]
        if key != getattr(self, 'queue_sticky_album_key', None):
            self._clear_queue_sticky_album()
        self.load_album_for_review(key)
        self.refresh_queue_tab()
        self._focus_queue_list()
        return 'break'

    def _on_option_mousewheel(self, event):
        try:
            if getattr(event, 'delta', 0):
                step = -1 * int(event.delta / 120) if event.delta else 0
                if step == 0:
                    step = -1 if event.delta > 0 else 1
                self.cand_canvas.yview_scroll(step, 'units')
            elif getattr(event, 'num', None) == 4:
                self.cand_canvas.yview_scroll(-1, 'units')
            elif getattr(event, 'num', None) == 5:
                self.cand_canvas.yview_scroll(1, 'units')
        except Exception:
            pass
        return 'break'

    def _bind_option_scrolling(self, widget):
        for seq in ('<MouseWheel>', '<Shift-MouseWheel>', '<Button-4>', '<Button-5>'):
            widget.bind(seq, self._on_option_mousewheel, add='+')

    def _year_match_label(self, file_year, source_year):
        if not file_year or not source_year:
            return 'Unknown'
        try:
            fy = int(str(file_year)[:4])
            sy = int(str(source_year)[:4])
        except Exception:
            return 'Unknown'
        if fy == sy:
            return f'Exact ({fy})'
        if abs(fy - sy) <= 2:
            return f'Close (file {fy} / source {sy})'
        return f'Warning (file {fy} / source {sy})'

    def _text_match_label(self, wanted, found, *, clean_album=False):
        w = normalize_for_match(clean_album_name(wanted) if clean_album else wanted)
        f = normalize_for_match(clean_album_name(found) if clean_album else found)
        if not w or not f:
            return 'Unknown'
        if w == f:
            return 'Exact'
        if len(w) >= 6 and (w in f or f in w):
            return 'Close'
        w_words = set(w.split())
        f_words = set(f.split())
        if w_words and w_words.issubset(f_words):
            return 'Close'
        return 'Check manually'

    def candidate_match_details(self, candidate, album):
        meta = candidate.get('source_meta_json') or {}
        source_artist = meta.get('source_artist') or candidate.get('artist') or ''
        source_title = meta.get('source_title') or candidate.get('release_title') or ''
        source_year = meta.get('source_year') or meta.get('release_date') or ''
        artist_label = self._text_match_label(album.get('search_artist') or album.get('artist'), source_artist)
        title_label = self._text_match_label(album.get('search_album') or album.get('album'), source_title, clean_album=True)
        year_label = self._year_match_label(album.get('year') or '', source_year)
        points = 0
        if artist_label == 'Exact': points += 2
        elif artist_label == 'Close': points += 1
        if title_label == 'Exact': points += 2
        elif title_label == 'Close': points += 1
        if year_label.startswith('Exact'): points += 1
        elif year_label.startswith('Close'): points += 0.5
        if points >= 4.5:
            overall = 'High'
        elif points >= 3:
            overall = 'Medium'
        else:
            overall = 'Check manually'
        extras = []
        if meta.get('country'):
            extras.append(f'Country: {meta.get("country")}')
        if meta.get('status'):
            extras.append(f'Status: {meta.get("status")}')
        if meta.get('format'):
            extras.append(f'Format: {meta.get("format")}')
        if meta.get('record_type'):
            extras.append(f'Type: {meta.get("record_type")}')
        if meta.get('track_count'):
            extras.append(f'Tracks: {meta.get("track_count")}')
        if meta.get('same_as_current'):
            extras.append(f'Same image as current, higher-res ({meta.get("current_width")}×{meta.get("current_height")} → {candidate.get("width")}×{candidate.get("height")})')
        lines = [
            f'Match confidence: {overall}',
            f'Artist match: {artist_label}' + (f' ({source_artist})' if source_artist else ''),
            f'Album title match: {title_label}' + (f' ({source_title})' if source_title else ''),
            f'Year match: {year_label}',
        ]
        if extras:
            lines.append('Source metadata: ' + ' | '.join(extras))
        return '\n'.join(lines)

    def identity_summary(self, album):
        if not album:
            return ''
        src = album.get('notes_json') or {}
        summary = src.get('source_summary') or 'metadata inference'
        return (
            f'Searching with: {album.get("search_artist") or album.get("artist") or ""} — ' 
            f'{album.get("search_album") or album.get("album") or ""}'
            + (f' ({album.get("year")})' if album.get('year') else '')
            + f'  |  Confidence: {album.get("identity_confidence") or "unknown"}  |  Source: {summary}'
        )

    # ---------- Commands ----------
    def choose(self):
        f = filedialog.askdirectory(title='Choose your main music folder')
        if f:
            f = clean_input_path(f)
            self.folder_var.set(f)
            save_settings({'last_library_path': f})
            self.settings = load_settings()

    def open_settings(self):
        SettingsWindow(self)


    def open_keyboard_shortcuts(self):
        win = tk.Toplevel(self.root)
        win.title('Keyboard Shortcuts')
        win.geometry('560x560')
        win.minsize(480, 420)
        body = ttk.Frame(win, padding=16)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Keyboard Shortcuts', font=('TkDefaultFont', 18, 'bold')).pack(anchor='w', pady=(0, 8))
        ttk.Label(body, text='Shortcuts work when you are not typing in a search box, text box, or dropdown.', foreground='#555555', wraplength=520).pack(anchor='w', pady=(0, 12))

        text_box = tk.Text(body, wrap='word', height=20, relief='solid', bd=1)
        text_box.pack(fill='both', expand=True)
        shortcuts = [
            ('Review actions', [
                ('A', 'Approve + Embed selected candidate'),
                ('R', 'Reject selected candidate'),
                ('S', 'Skip Album'),
                ('Actions row', 'Embed (A) · Reject (R) · Skip (S) · Good · Ignore'),
                ('F', 'Find Artwork for selected album'),
                ('Return / Space', 'Open candidate artwork zoom; Space again closes preview'),
                ('N', 'Jump to next visible queue row'),
            ]),
            ('Candidate navigation', [
                ('Up / Down', 'Previous / next downloaded candidate, unless the queue is focused'),
                ('Click artwork preview', 'Open current or candidate artwork zoom'),
                ('Left / Right in zoom', 'Previous / next candidate in the zoom window'),
                ('Page Up / Page Down in zoom', 'Previous / next candidate in the zoom window'),
                ('Escape / Space in zoom', 'Close zoom window'),
            ]),
            ('Queue navigation', [
                ('Click the queue first', 'Give the queue keyboard focus'),
                ('Up / Down', 'Previous / next visible queue row and load that album'),
                ('Page Up / Page Down', 'Jump several visible queue rows'),
                ('Home / End', 'First / last visible queue row'),
                ('Double-click queue row', 'Open album folder'),
                ('Right-click queue row', 'Open queue context actions'),
            ]),
            ('Search and menus', [
                ('Cmd-F / Ctrl-F', 'Focus queue search'),
                ('Esc', 'Close Actions / Tools popup when open'),
            ]),
        ]
        for section, rows in shortcuts:
            text_box.insert('end', f'{section}\n', 'heading')
            for key, desc in rows:
                text_box.insert('end', f'  {key:<22} {desc}\n')
            text_box.insert('end', '\n')
        text_box.tag_configure('heading', font=('TkDefaultFont', 12, 'bold'))
        text_box.configure(state='disabled')
        bind_vertical_scroll(text_box)
        btns = ttk.Frame(body)
        btns.pack(fill='x', pady=(10, 0))
        ttk.Label(btns, text=BUILD_VERSION, foreground='#666666').pack(side='left')
        ttk.Button(btns, text='Close', command=win.destroy).pack(side='right')
        win.bind('<Escape>', lambda e: win.destroy())
        win.transient(self.root)
        win.focus_set()

    def open_about_logo(self):
        win = tk.Toplevel(self.root)
        win.title('Artwork Review Manager')
        win.geometry('560x620')
        win.minsize(460, 420)
        body = ttk.Frame(win, padding=18)
        body.pack(fill='both', expand=True)
        logo_path = APP_DIR / 'assets' / 'logo_full_large.png'
        if logo_path.exists():
            try:
                img = Image.open(logo_path).convert('RGBA')
                img.thumbnail((500, 430))
                win.logo_photo = ImageTk.PhotoImage(img)
                ttk.Label(body, image=win.logo_photo).pack(pady=(0, 12))
            except Exception:
                ttk.Label(body, text='Artwork Review Manager', font=('TkDefaultFont', 20, 'bold')).pack(pady=(0, 12))
        else:
            ttk.Label(body, text='Artwork Review Manager', font=('TkDefaultFont', 20, 'bold')).pack(pady=(0, 12))
        ttk.Label(body, text='Review, choose, and embed better album artwork.', foreground='#555555').pack(pady=(0, 6))
        ttk.Label(body, text=BUILD_VERSION, foreground='#555555').pack(pady=(0, 10))
        ttk.Label(body, text=f'App data: {DATA_DIR}', foreground='#666666', wraplength=500).pack(pady=(0, 14))
        ttk.Button(body, text='Close', command=win.destroy).pack()
        win.transient(self.root)
        win.focus_set()


    def toggle_queue_left_layout(self):
        self.queue_left_layout = not bool(getattr(self, 'queue_left_layout', True))
        try:
            save_settings({'layout': {'queue_left_layout': self.queue_left_layout}})
        except Exception:
            pass
        where = 'left' if self.queue_left_layout else 'right'
        messagebox.showinfo(
            'Layout changed',
            f'The queue will appear on the {where} after restarting Artwork Review Manager.'
        )

    def close_tools_popup(self, event=None):
        popup = getattr(self, 'tools_popup', None)
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
        self.tools_popup = None

    def show_tools_popup(self):
        self.close_tools_popup()
        btn = self.tools_btn
        popup = tk.Toplevel(self.root)
        self.tools_popup = popup
        popup.overrideredirect(True)
        popup.transient(self.root)
        try:
            popup.attributes('-topmost', True)
        except Exception:
            pass
        frame = ttk.Frame(popup, padding=6, relief='solid', borderwidth=1)
        frame.pack(fill='both', expand=True)

        def add(label, command):
            def run():
                self.close_tools_popup()
                command()
            b = ttk.Button(frame, text=label, command=run)
            b.pack(fill='x', pady=2)
            return b

        add('Settings…', self.open_settings)
        add('About / Logo…', self.open_about_logo)
        add('Keyboard Shortcuts…', self.open_keyboard_shortcuts)
        add(('Use Queue on Right' if getattr(self, 'queue_left_layout', True) else 'Use Queue on Left'), self.toggle_queue_left_layout)
        ttk.Separator(frame).pack(fill='x', pady=4)
        add('Open Approved Folder', lambda: open_path(APPROVED_DIR))
        add('Open Reports Folder', lambda: open_path(REPORT_DIR))
        add('Open App Data Folder', lambda: open_path(DATA_DIR))
        add('Open Backup Folder', lambda: open_path(BACKUP_DIR))
        add('Export Diagnostics…', self.export_diagnostics)
        add('Save Log…', self.save_log)
        add('Clear Log', self.clear_log)
        ttk.Separator(frame).pack(fill='x', pady=4)
        add('Clear Rejected Candidate Files', self.clear_rejected_candidate_files)
        add('Trash All Temporary Artwork', self.trash_all_temporary_artwork)
        add('Clear Handled Album Temporary Artwork', self.clear_handled_temporary_artwork)
        add('Trash Approved Artwork Copies', self.trash_approved_artwork_copies)
        add('Clear Orphan Temporary Images', self.clear_orphan_temporary_images)
        add('Refresh / Rebuild Queue Counts', self.rebuild_queue_counts)
        add('Re-evaluate Queue Statuses', self.reevaluate_queue_statuses)
        add('Repair Stale Candidate Rows', self.repair_stale_candidate_rows)
        add('Find / Repair Inconsistent Queue Rows', self.find_repair_inconsistent_queue_rows)
        if getattr(self, 'convert_batch_active', False):
            add('Stop Batch Convert/Save After Current', self.request_stop_convert_batch_after_current)
        ttk.Separator(frame).pack(fill='x', pady=4)
        add('Backup / Restore Browser…', self.open_restore_browser)
        add('Undo Last Approval', self.undo)
        add('Clear Saved Queue…', self.clear_saved_queue)

        popup.update_idletasks()
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 2
        width = max(btn.winfo_width(), popup.winfo_reqwidth())
        height = popup.winfo_reqheight()
        screen_w = popup.winfo_screenwidth()
        screen_h = popup.winfo_screenheight()
        x = max(0, min(x, screen_w - width - 8))
        y = max(0, min(y, screen_h - height - 8))
        popup.geometry(f'{width}x{height}+{x}+{y}')
        popup.focus_force()
        popup.bind('<FocusOut>', self.close_tools_popup)
        popup.bind('<Escape>', self.close_tools_popup)

    def close_queue_actions_popup(self, event=None):
        popup = getattr(self, 'queue_actions_popup', None)
        if popup is not None:
            try:
                popup.destroy()
            except Exception:
                pass
        self.queue_actions_popup = None

    def show_queue_actions_menu(self):
        if 'disabled' in self.queue_actions_btn.state():
            return
        # Avoid macOS/Tk native-menu placement oddities by using a small in-app
        # popup panel anchored under the button, rather than tk.Menu/tk_popup.
        # 4.09 keeps this menu grouped by workflow area so it does not become a
        # single long "everything drawer" as more actions are added.
        self.close_queue_actions_popup()
        btn = self.queue_actions_btn
        popup = tk.Toplevel(self.root)
        self.queue_actions_popup = popup
        popup.overrideredirect(True)
        popup.transient(self.root)
        try:
            popup.attributes('-topmost', True)
        except Exception:
            pass

        frame = ttk.Frame(popup, padding=6, relief='solid', borderwidth=1)
        frame.pack(fill='both', expand=True)

        selected_keys = self.selected_queue_keys()
        selected_n = len(selected_keys)
        visible_n = len(self.visible_queue_keys()) if hasattr(self, 'queue_tree') else 0
        album = self.current_album_info or (db.get_album(self.current_album_key) if self.current_album_key else None)
        status, _reason = self._evaluate_queue_album(album or {})
        status = status or (album or {}).get('status') or ''
        has_album = bool(self.active_album_info())
        has_candidate = bool(self.current_candidate())
        searching = self.is_artwork_search_active()
        transitioning = self._action_transition_active()
        writing = self.is_write_action_active()
        album_state = 'normal' if has_album and not searching and not writing and not transitioning else 'disabled'
        search_state = 'normal' if has_album and not searching and not writing and not transitioning else 'disabled'
        multi_state = 'normal' if selected_n > 1 and not searching and not writing and not transitioning else 'disabled'

        def add_heading(label):
            ttk.Label(frame, text=label, foreground='#666666', font=('TkDefaultFont', 10, 'bold')).pack(anchor='w', pady=(5, 1))

        def add_action(label, command, state='normal'):
            def run():
                self.close_queue_actions_popup()
                if state != 'disabled':
                    command()
            b = ttk.Button(frame, text=label, command=run, state=state)
            b.pack(fill='x', pady=1)
            return b

        def add_sep():
            ttk.Separator(frame).pack(fill='x', pady=4)

        # Primary action: show only the action most likely to be useful for the
        # currently selected album/state, then keep the rest grouped below.
        add_heading('Primary')
        if searching:
            add_action('Stop Search', self.stop_artwork_search, 'normal')
        elif writing:
            add_action('Write action in progress…', lambda: None, 'disabled')
        elif status in ('not_square_artwork', 'incompatible_artwork'):
            add_action('Convert/Save Embedded Artwork', self.convert_embedded_artwork_to_baseline, album_state)
        elif has_candidate:
            add_action('Embed Selected Candidate', self.approve, album_state)
        elif status in ('already_good', 'approved', 'reviewed_skipped', 'ignored'):
            add_action('Rework Album', self.rework_album, album_state)
        else:
            add_action('Find Artwork', self.find_more, search_state)

        add_sep()
        add_heading('Search')
        add_action('Find Artwork', self.find_more, search_state)
        add_action('Search More', self.search_more, search_state)
        add_action(f'Search Next {get_batch_search_count(self.settings)}', self.find_next_five, 'disabled' if searching or writing or transitioning else 'normal')
        if selected_n > 1:
            add_action(f'Search Selected ({selected_n})', self.search_selected_albums, 'disabled' if searching or writing or transitioning else 'normal')
        add_action('Choose Release…', self.choose_release, album_state)

        add_sep()
        add_heading('Artwork')
        add_action('Preview Embedded Artwork', self.preview_embedded_artwork, album_state)
        add_action('Open Source Page', self.open_source_page, 'normal' if has_candidate else 'disabled')
        add_action('Convert/Save Embedded Artwork', self.convert_embedded_artwork_to_baseline, album_state)
        add_action('Show Problem Files…', self.show_problem_files, album_state)
        add_action('Audit Album…', self.audit_selected_album, album_state)
        if selected_n > 1:
            add_action(f'Convert/Save Selected ({selected_n})', self.convert_save_selected_embedded_artwork, multi_state)
        add_action(f'Convert/Save All Visible ({visible_n})', self.convert_save_visible_embedded_artwork, 'normal' if visible_n and not searching and not writing and not transitioning else 'disabled')
        add_action('Convert/Save Next', self.convert_save_next_visible_embedded_artwork, 'normal' if visible_n and not searching and not writing and not transitioning else 'disabled')
        if getattr(self, 'convert_batch_active', False):
            add_action('Stop Batch After Current', self.request_stop_convert_batch_after_current, 'normal')
        add_action('Add Artwork from Source URL…', self.add_artwork_from_source_url, album_state)

        add_sep()
        add_heading('Album')
        add_action('Open Album Folder', self.open_album_folder, album_state)
        add_action('Locate Album Folder…', self.locate_album_folder, album_state)
        add_action('Refresh from Disk', self.rescan_selected_album, album_state)
        add_action('Deep Rescan Selected', self.deep_rescan_selected_album, album_state)
        add_action('Rework Album', self.rework_album, 'disabled' if (not has_album or searching or writing) else 'normal')
        add_action('Rescan Artist Folder', self.rescan_artist_folder, album_state)
        add_action('Rescan Active Queue', self.rescan_active_queue, album_state)

        add_sep()
        add_heading('Decisions')
        add_action('Reject All Candidates', self.reject_all_candidates, 'normal' if has_candidate and not writing else 'disabled')
        add_action('Mark Current Artwork as Good', self.mark_current_good, album_state)
        add_action('Ignore Album', self.ignore_album, album_state)
        if selected_n > 1:
            add_action(f'Mark Selected Good ({selected_n})', self.mark_selected_good, multi_state)
            add_action(f'Ignore Selected ({selected_n})', self.ignore_selected_albums, multi_state)
            add_action(f'Reject Candidates for Selected ({selected_n})', self.reject_candidates_for_selected, multi_state)
            add_action(f'Rescan Selected ({selected_n})', self.rescan_selected_albums, multi_state)

        popup.update_idletasks()
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 2
        width = max(btn.winfo_width(), popup.winfo_reqwidth())
        height = popup.winfo_reqheight()
        screen_w = popup.winfo_screenwidth()
        screen_h = popup.winfo_screenheight()
        x = max(0, min(x, screen_w - width - 8))
        y = max(0, min(y, screen_h - height - 8))
        popup.geometry(f'{width}x{height}+{x}+{y}')
        popup.focus_force()
        popup.bind('<FocusOut>', self.close_queue_actions_popup)
        popup.bind('<Escape>', self.close_queue_actions_popup)


    def _set_current_operation(self, operation='idle', label=''):
        """Track the current high-level operation for consistent action locks.

        This is intentionally lightweight: it centralises UI/action guard state
        without changing the existing worker implementation.
        """
        self.current_operation = operation or 'idle'
        self.current_operation_label = label or ''
        if label:
            try:
                self.status_var.set(label)
            except Exception:
                pass

    def _clear_current_operation(self, operation=None):
        if operation is None or getattr(self, 'current_operation', 'idle') == operation:
            self.current_operation = 'idle'
            self.current_operation_label = ''

    def _operation_allows_read_action(self):
        return getattr(self, 'current_operation', 'idle') in ('idle', 'searching')

    def is_write_action_active(self):
        """Return True while a file-writing action is running.

        This protects music files and queue rows from overlapping operations such
        as Approve/Embed, Convert/Save, batch Convert/Save, and backup restore.
        Provider searches are allowed to continue separately; finalized albums
        still ignore late search results through the existing guards.
        """
        try:
            embed_active = self.active_embed_job_id is not None
            worker_active = self.embed_worker is not None and self.embed_worker.is_alive()
            batch_active = bool(getattr(self, 'convert_batch_active', False))
            op = getattr(self, 'current_operation', 'idle')

            # Build 4.17: be defensive about stale high-level operation state.
            # Approve/Embed uses both active_embed_job_id and current_operation;
            # if a completion/error path clears the job but misses the operation,
            # the UI can otherwise stay permanently locked as "Writing files".
            if op in {'embedding', 'converting', 'batch_converting'} and not (embed_active or worker_active or batch_active):
                self._clear_current_operation(op)
                op = 'idle'
            if op in {'re_evaluating', 'repairing'} and not (embed_active or worker_active or batch_active):
                # These maintenance actions are synchronous. If their operation
                # label survives a return/cancel path, it is stale.
                self._clear_current_operation(op)
                op = 'idle'

            if op in {'embedding', 'converting', 'batch_converting', 're_evaluating', 'repairing'}:
                return True
            if embed_active or worker_active or batch_active:
                return True
        except Exception:
            pass
        return False

    def _block_if_write_action_active(self, action='That action'):
        if self.is_write_action_active():
            self.status_var.set(f'{action} blocked: another write action is still running.')
            self._set_action_result(f'{action} blocked because another write action is still running. Wait for it to finish first.')
            return True
        return False

    def request_stop_convert_batch_after_current(self):
        if not getattr(self, 'convert_batch_active', False):
            self.status_var.set('No batch Convert/Save is running.')
            return
        self.convert_batch_stop_after_current = True
        self.status_var.set('Batch Convert/Save will stop after the current album.')
        self._set_action_result('Batch Convert/Save: stop requested. Current album will finish, then the batch will stop.')
        self.log_msg('\nBatch Convert/Save stop requested. The current album will finish before stopping.\n')

    def _is_verbose_log_enabled(self):
        try:
            return bool(self.verbose_log_var.get())
        except Exception:
            return False

    def _toggle_verbose_log(self):
        enabled = self._is_verbose_log_enabled()
        try:
            save_settings({'verbose_log': enabled})
            self.settings = load_settings()
        except Exception:
            pass
        try:
            self.log_msg(f'\nVerbose log {"on" if enabled else "off"}.\n')
        except Exception:
            pass

    def log_verbose(self, msg):
        if self._is_verbose_log_enabled():
            self.log_msg(msg)

    def _workflow_album_label(self, artist='', album='', *, album_key=None):
        if album_key:
            try:
                rec = db.get_album(album_key) or {}
                artist = artist or rec.get('artist') or ''
                album = album or rec.get('album') or ''
            except Exception:
                pass
        artist = str(artist or '').strip()
        album = str(album or '').strip()
        if artist and album:
            return f'{artist} — {album}'
        return artist or album or 'Selected album'

    def log_msg(self, msg):
        self.log.insert('end', msg)
        self.log.see('end')
        # Tk Text widgets get noticeably slower when thousands of scan/search
        # progress lines accumulate. Keep the visible history bounded; full
        # durable event history remains in the database where applicable.
        try:
            self._log_trim_counter = int(getattr(self, '_log_trim_counter', 0)) + 1
            if self._log_trim_counter >= 25:
                self._log_trim_counter = 0
                max_lines = int(getattr(self, '_max_visible_log_lines', 1200) or 1200)
                total_lines = int(float(self.log.index('end-1c').split('.')[0]))
                if total_lines > max_lines + 150:
                    self.log.delete('1.0', f'{total_lines - max_lines}.0')
        except Exception:
            pass

    def set_status_dot(self, color):
        try:
            self.status_dot.itemconfigure(self.status_dot_id, fill=color)
        except Exception:
            pass

    def update_progress_label(self):
        if self.scan_total:
            pct = min(100.0, (self.scan_processed / self.scan_total) * 100.0) if self.scan_total else 0
            self.progress_var.set(pct)
            self.progress_text.set(f'{self.scan_processed} of {self.scan_total} albums ({pct:.1f}%)')
        elif self.scan_processed:
            self.progress_var.set(0)
            self.progress_text.set(f'{self.scan_processed} albums checked')
        else:
            self.progress_var.set(0)
            self.progress_text.set('')

    def start(self):
        folder = clean_input_path(self.folder_var.get())
        if not os.path.isdir(folder):
            messagebox.showerror('Folder not found', 'Choose a valid music folder.')
            return
        save_settings({'last_library_path': folder})
        self.settings = load_settings()
        if self.worker and self.worker.is_alive():
            return
        if self._block_if_write_action_active('Library scan'):
            return
        self.stop_event.clear()
        if get_deep_scan_all_files(self.settings):
            self.log_msg(f'\nDeep check started: checking every music file for {get_preferred_artwork_size()}px target artwork and baseline JPEG compatibility.\n')
        else:
            self.log_msg(f'\nScan started: queueing albums with missing or below-{get_scan_min_artwork_size()}px artwork.\n')
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.scan_active = True
        self.set_status_dot('#007aff')
        if get_deep_scan_all_files(self.settings):
            self.status_var.set(f'Deep checking every file. Queueing missing, below {get_preferred_artwork_size()}px, or non-baseline artwork.')
        else:
            self.status_var.set(f'Scanning embedded artwork only. Queueing missing or below {get_scan_min_artwork_size()}px; artwork options download only when requested.')
        self._sync_top_status_label()
        self.worker = threading.Thread(target=self._work, args=(folder,), daemon=True)
        self.worker.start()

    def stop_scan(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set('Stopping after the current album scan finishes…')
            self.set_status_dot('#f5a623')
            self.log_msg('\nStop requested. Albums already scanned are saved in the queue.\n')

    def _work(self, folder):
        oldout, olderr = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = QueueWriter(self.q)
        rows = []
        try:
            total_albums = 0
            self.q.put(('PROGRESS', {'processed': 0, 'total': total_albums, 'path': folder}))
            db.start_scan(folder, total_albums)

            def prog(i, total, path):
                self.q.put(('PROGRESS', {'processed': i, 'total': total_albums or total or 0, 'path': path}))
                if i % 25 == 0:
                    print(f'Checked {i} album folders… {path}\n')

            def on_album(row, info, n):
                rows.append(row)
                self.q.put(('ALBUM_QUEUED', {
                    'artist': info.get('artist', ''),
                    'album': info.get('album', ''),
                    'album_key': info.get('album_key'),
                }))
                print(f'Queued for review: {info["artist"]} - {info["album"]}\n')

            scan_library(folder, include_missing=True, progress=prog, stop_event=self.stop_event, on_album=on_album, total_albums=total_albums, resume=True)
            self.low_res_csv = write_low_res_csv(rows)
            print(f'CSV report saved: {self.low_res_csv}\n')
            stopped = self.stop_event.is_set()
            db.finish_scan(stopped=stopped)
            self.q.put(('DONE', {'stopped': stopped, 'rows': rows, 'csv': self.low_res_csv}))
        except Exception as exc:
            db.finish_scan(stopped=self.stop_event.is_set())
            self.q.put(('ERROR', str(exc)))
        finally:
            sys.stdout, sys.stderr = oldout, olderr

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'LOG':
                    self.log_verbose(payload)
                elif kind == 'PROGRESS':
                    self.scan_processed = int(payload.get('processed') or 0)
                    self.scan_total = int(payload.get('total') or 0)
                    self.update_progress_label()
                    if getattr(self, 'scan_active', False):
                        self._sync_top_status_label()
                elif kind == 'ALBUM_QUEUED':
                    self.schedule_queue_refresh(delay=180)
                    self.status_var.set(f'Queued for review: {payload.get("artist", "")} - {payload.get("album", "")}')
                    if getattr(self, 'scan_active', False):
                        self._sync_top_status_label()
                    self.refresh_footer()
                elif kind == 'CAND':
                    job_id = payload.get('job_id') if isinstance(payload, dict) and 'candidate' in payload else None
                    cand = payload.get('candidate') if isinstance(payload, dict) and 'candidate' in payload else payload
                    silent = bool(payload.get('silent')) if isinstance(payload, dict) else False
                    if job_id is not None and job_id in self.canceled_find_jobs:
                        continue
                    self.candidates = db.load_candidates(include_rejected=False)
                    self._rebuild_groups()
                    cand_key = cand.get('album_key')
                    if cand_key and cand_key == self.current_album_key:
                        self.candidate_index = max(0, len(self.groups.get(self.current_album_key, [])) - 1)
                        self.show_current_album()
                        self.status_var.set(f'Artwork option saved for {cand.get("artist", "")} - {cand.get("album", "")}.')
                    else:
                        self.refresh_queue_tab()
                        if silent:
                            self.status_var.set(f'Background search saved artwork for {cand.get("artist", "")} - {cand.get("album", "")}.')
                        else:
                            self.status_var.set(f'Artwork saved in background for {cand.get("artist", "")} - {cand.get("album", "")}. Your current album selection was left unchanged.')
                    self.refresh_footer()
                elif kind == 'FIND_DONE':
                    job_id = payload.get('job_id')
                    if job_id in self.canceled_find_jobs or payload.get('stopped'):
                        self.canceled_find_jobs.discard(job_id)
                        # If Stop already detached the UI from this job, keep
                        # this late completion calm, but still reload candidates in
                        # case the worker saved an option just before seeing the stop
                        # flag.  The sticky row remains in place for filtered views.
                        try:
                            self.candidates = db.load_candidates(include_rejected=False)
                            self._rebuild_groups()
                            cur_key = getattr(self, 'current_album_key', None)
                            if cur_key:
                                self.load_album_for_review(cur_key)
                            else:
                                self.refresh_queue_tab()
                        except Exception:
                            pass
                        if self.active_find_job_id == job_id:
                            self._begin_action_transition_guard(650, 'artwork search stop')
                            self.active_find_job_id = None
                            self.active_find_album_key = None
                            self.active_find_mode = None
                            self.active_search_album_keys.clear()
                            self.active_search_album_status.clear()
                            self.active_search_album_labels.clear()
                            self.active_search_album_original_buckets.clear()
                            self.active_search_batch_total = 0
                            self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                            self.status_var.set('Artwork search stopped. Saved options, if any, are still available.')
                            self.refresh_queue_tab()
                            self.refresh_footer()
                        continue
                    if job_id != self.active_find_job_id:
                        continue
                    self.active_find_job_id = None
                    self._begin_action_transition_guard(450, 'artwork search completion')
                    self.active_find_album_key = None
                    self.active_find_mode = None
                    self.active_search_album_keys.clear()
                    self.active_search_album_status.clear()
                    self.active_search_album_labels.clear()
                    self.active_search_album_original_buckets.clear()
                    self.active_search_batch_total = 0
                    key = payload.get('album_key')
                    count = payload.get('count', 0)
                    mode = payload.get('mode') or 'single'
                    try:
                        for check_key in (payload.get('album_keys') or ([key] if key else [])):
                            self._queue_consistency_check(check_key, repair=True, context='search')
                    except Exception:
                        pass
                    self.candidates = db.load_candidates(include_rejected=False)
                    self._rebuild_groups()
                    current_matches_finished = bool(key and key == self.current_album_key)
                    if current_matches_finished and mode in ('single', 'more'):
                        self._pin_album_in_current_filter(key, reason='artwork search result')
                    if current_matches_finished:
                        self.load_album_for_review(key)
                    self.refresh_queue_tab()
                    self.set_status_dot('#26b53f')
                    if mode == 'batch':
                        sc = payload.get('status_counts') or {}
                        review_n = int(sc.get('candidate_found') or 0)
                        noopts_n = int(sc.get('no_candidate') or 0)
                        searched_n = int(payload.get('album_count', 0) or 0)
                        self.status_var.set(f'Batch search finished: {searched_n} searched · {review_n} to review · {noopts_n} no options · {count} new option(s).')
                    elif mode == 'more':
                        total_now = len(db.load_candidates_for_album(key, include_rejected=False)) if key else count
                        if current_matches_finished:
                            self.status_var.set(f'Search More finished: {count} new artwork option(s) saved. {total_now} total option(s) are now available for this album.' if count else 'Search More finished. No additional distinct artwork options were found.')
                        else:
                            self.status_var.set(f'Background Search More finished: {count} new option(s) saved. Your current album selection was left unchanged.')
                    else:
                        if current_matches_finished:
                            if count:
                                self.status_var.set(f'Found {count} artwork option(s) for the selected album.')
                            else:
                                reason = self._brief_no_options_reason(key)
                                suffix = f' Reason: {reason}' if reason else ''
                                self.status_var.set('No artwork options found for the selected album.' + suffix + ' Try Choose Release, Search More, or Google Images.')
                        else:
                            self.status_var.set(f'Background artwork search finished: {count} option(s) saved. Your current album selection was left unchanged.')
                    try:
                        if mode == 'batch':
                            self._set_action_result(f'Batch search finished: {count} new option(s) saved.')
                            self.log_msg(f'\nSearch finished: {searched_n} album(s) · {count} new option(s) · {review_n} to review · {noopts_n} no options.\n')
                        elif count:
                            label = self._workflow_album_label(album_key=key)
                            self._set_action_result(f'Artwork search finished: {count} option(s) saved.')
                            self.log_msg(f'\nSearch finished: {label} · {count} option(s) saved.\n')
                        else:
                            label = self._workflow_album_label(album_key=key)
                            self._set_action_result('Artwork search finished: no usable options found.')
                            self.log_msg(f'\nSearch finished: {label} · no usable options found.\n')
                    except Exception:
                        pass
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                    self.focus_queue_table()
                    self.refresh_footer()
                elif kind == 'FIND_ERROR':
                    job_id = payload.get('job_id')
                    if job_id in self.canceled_find_jobs or payload.get('stopped'):
                        self.canceled_find_jobs.discard(job_id)
                        if self.active_find_job_id == job_id:
                            self.active_find_job_id = None
                            self.active_find_album_key = None
                            self.active_find_mode = None
                            self.active_search_album_keys.clear()
                            self.active_search_album_status.clear()
                            self.active_search_album_labels.clear()
                            self.active_search_album_original_buckets.clear()
                            self.active_search_batch_total = 0
                            self._begin_action_transition_guard(650, 'artwork search stop')
                            self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                            self.status_var.set('Artwork search stopped.')
                            self.refresh_queue_tab()
                            self.refresh_footer()
                        continue
                    if job_id != self.active_find_job_id:
                        continue
                    self.active_find_job_id = None
                    self._begin_action_transition_guard(650, 'artwork search error')
                    self.active_find_album_key = None
                    self.active_find_mode = None
                    self.active_search_album_keys.clear()
                    self.active_search_album_status.clear()
                    self.active_search_album_labels.clear()
                    self.active_search_album_original_buckets.clear()
                    self.active_search_batch_total = 0
                    self.set_status_dot('#d00000')
                    self.status_var.set('Artwork search failed. Saved progress is still in the queue database.')
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                    messagebox.showerror('Artwork search failed', payload.get('error', 'Unknown error'))
                elif kind == 'PARTIAL_STATUS':
                    if isinstance(payload, dict):
                        job_id = payload.get('job_id')
                        if job_id is not None and job_id in self.canceled_find_jobs:
                            continue
                        if job_id is not None and self.active_find_job_id is not None and job_id != self.active_find_job_id:
                            continue
                        text = payload.get('text', '')
                        self.status_var.set(text)
                        self._note_partial_search_status(text)
                    else:
                        self.status_var.set(payload)
                        self._note_partial_search_status(payload)
                elif kind == 'AUDIT_DONE':
                    self._clear_current_operation('auditing')
                    info = payload.get('info') or {}
                    key = info.get('album_key')
                    try:
                        self._clear_current_art_cache(key, remove_files=True)
                    except Exception:
                        pass
                    self.candidates = db.load_candidates(include_rejected=False)
                    self._rebuild_groups()
                    if key and db.get_album(key):
                        try:
                            self.load_album_for_review(key)
                        except Exception:
                            self.refresh_queue_tab()
                    else:
                        self.refresh_queue_tab()
                    verification = payload.get('verification') or {}
                    if verification.get('ok'):
                        msg = 'Album audit complete: all checked embedded artwork passed.'
                        self.set_status_dot('#26b53f')
                    else:
                        msg = 'Album audit complete: problem files found.'
                        self.set_status_dot('#ff9f0a')
                    self.status_var.set(msg)
                    self._set_action_result((verification.get('summary') or msg))
                    self.refresh_footer()
                    self._show_album_audit_window(payload)
                elif kind == 'AUDIT_ERROR':
                    self._clear_current_operation('auditing')
                    self.set_status_dot('#d00000')
                    self.status_var.set('Album audit failed.')
                    self._set_action_result(f'Album audit failed: {payload.get("error", "Unknown error")}')
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                    messagebox.showerror('Album audit failed', payload.get('error', 'Unknown error'))
                elif kind == 'EMBED_PROGRESS':
                    if payload.get('job_id') != self.active_embed_job_id:
                        continue
                    done = int(payload.get('done') or 0)
                    total = int(payload.get('total') or 0)
                    raw_file = payload.get('file') or ''
                    name = os.path.basename(raw_file)
                    self.progress_text.set('')
                    if total:
                        self.progress_var.set(min(100.0, (done / total) * 100.0))
                    self.status_var.set(f'Embedding {done}/{total}' if total else ('NAS worker processing…' if raw_file == 'NAS worker' else 'Embedding artwork…'))
                    if name:
                        self.log_verbose(f'  Embedding file {done}/{total}: {name}\n')
                    self._set_action_result('')
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                elif kind == 'CONVERT_EMBEDDED_DONE':
                    if payload.get('job_id') != self.active_embed_job_id:
                        continue
                    self.active_embed_job_id = None
                    self.active_embed_album_key = None
                    batch_convert_active = bool(getattr(self, 'convert_batch_active', False))
                    try:
                        self._finish_convert_embedded_artwork(payload.get('album_key'), payload.get('result') or {})
                    finally:
                        if not batch_convert_active:
                            self._clear_current_operation('converting')
                    if getattr(self, 'convert_batch_active', False):
                        try:
                            album = db.get_album(payload.get('album_key')) or {}
                            status = album.get('status') or ''
                            failed = (payload.get('result') or {}).get('failed') or []
                            if status == 'already_good':
                                self.convert_batch_good = getattr(self, 'convert_batch_good', 0) + 1
                            else:
                                self.convert_batch_needs = getattr(self, 'convert_batch_needs', 0) + 1
                            if failed:
                                self.convert_batch_failed = getattr(self, 'convert_batch_failed', 0) + 1
                            self.convert_batch_done = getattr(self, 'convert_batch_done', 0) + 1
                            self._set_action_result(f'Batch Convert/Save: {self.convert_batch_done}/{self.convert_batch_total} complete · {self.convert_batch_good} good · {self.convert_batch_needs} still need attention.')
                        except Exception:
                            pass
                        self.root.after(50, self._continue_convert_save_batch)
                elif kind == 'EMBED_DONE':
                    if payload.get('job_id') != self.active_embed_job_id:
                        continue
                    self.active_embed_job_id = None
                    self.active_embed_album_key = None
                    self._clear_current_operation('embedding')
                    c = payload.get('candidate') or {}
                    old_key = payload.get('album_key')
                    result = payload.get('result') or {}
                    approved_copy = payload.get('approved_copy') or ''
                    album_artwork_copy = payload.get('album_artwork_copy') or ''
                    previous_keys = payload.get('previous_keys') or []
                    extra_copy = f'\n  Album folder artwork file: {album_artwork_copy}' if album_artwork_copy else ''
                    temp_removed = int(payload.get('temp_removed') or 0)
                    cleanup_txt = f'\n  Trashed {temp_removed} temporary/import artwork file(s).' if temp_removed else ''
                    approved_txt = f'\n  Approved artwork copy: {approved_copy}' if approved_copy else '\n  Approved artwork copy: not saved (to save Application Support space)'
                    embedded_w = result.get('image_width') or result.get('width') or ''
                    embedded_h = result.get('image_height') or result.get('height') or ''
                    embedded_size_txt = f'{embedded_w}×{embedded_h}' if embedded_w and embedded_h else 'artwork'
                    embedded_summary = f'Embedded {embedded_size_txt} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).'
                    verification = payload.get('verification') or result.get('post_embed_verification') or {}
                    verify_summary = verification.get('summary') if isinstance(verification, dict) else ''
                    verify_note = f' Verified: {verify_summary}' if verify_summary else ''
                    approval_complete = bool(payload.get('approval_complete'))
                    self._clear_current_art_cache(old_key, remove_files=True)
                    self._queue_consistency_check(old_key, repair=True, context='approval')
                    self.candidates = db.load_candidates(include_rejected=False)
                    self._rebuild_groups()
                    self.progress_var.set(0)
                    self.progress_text.set('')
                    album_label = self._workflow_album_label(c.get('artist', ''), c.get('album', ''), album_key=old_key)
                    no_audio_files = bool(result.get('no_audio_files') or int(result.get('total') or 0) <= 0)
                    if result.get('dry_run'):
                        cover_note = ' Would also save cover.jpg.' if result.get('would_save_folder_cover') else ''
                        self.log_msg(f'\nDry run: {album_label} · would embed {embedded_size_txt} into {result.get("total", 0)} file(s).{cover_note} No files changed.\n')
                        self._set_action_result(f'Dry run complete — would embed {embedded_size_txt} into {result.get("total", 0)} file(s). No files changed.')
                        try:
                            self.load_album_for_review(old_key)
                        except Exception:
                            self.refresh_queue_tab()
                        self.set_status_dot('#26b53f')
                        self.status_var.set('Dry run complete. No files were changed and the candidate is still available.')
                    elif approval_complete:
                        cover_note = ' · cover.jpg saved' if album_artwork_copy else ''
                        worker_sec = result.get('remote_worker_duration_seconds')
                        worker_note = f' · NAS {float(worker_sec):.1f}s' if worker_sec not in (None, '') else ''
                        self.log_msg(f'\nArtwork embedded: {album_label} · {embedded_size_txt} · {result.get("updated", 0)}/{result.get("total", 0)} files · Good{cover_note}{worker_note}.\n')
                        self.log_verbose(f'  Details: {embedded_summary}{verify_note}{approved_txt}{extra_copy}{cleanup_txt}\n')
                        worker_msg = f' Via NAS worker in {float(worker_sec):.1f}s.' if worker_sec not in (None, '') else ''
                        self._set_action_result(f'Artwork embedded — album is now Good. {embedded_summary}{verify_note}' + (f' Saved cover file.' if album_artwork_copy else '') + worker_msg)
                        self.select_next_album_after(old_key, previous_keys, message=f'Artwork embedded — album is now Good. {embedded_summary}{verify_note}{worker_msg} Advanced to the next visible album.')
                        self.set_status_dot('#26b53f')
                    elif no_audio_files:
                        self.log_msg(f'\nApproval blocked: {album_label} · no supported audio files found · option kept for review.\n')
                        self.log_verbose(f'  Details: album folder={c.get("album_folder", "")} · supported formats are MP3, FLAC, M4A and MP4.\n')
                        self._set_action_result('Cannot embed: no supported audio files found. Candidate kept for review.')
                        try:
                            self.load_album_for_review(old_key)
                        except Exception:
                            self.refresh_queue_tab()
                        self.set_status_dot('#ff9f0a')
                        self.status_var.set('Approval blocked: no supported audio files found in the album folder.')
                        messagebox.showwarning(
                            'Cannot embed artwork',
                            'No supported audio files were found in this album folder, so the artwork was not embedded.\n\n'
                            'If the album folder moved or the drive is still loading, use Actions → Locate Album Folder… and try again.'
                        )
                    else:
                        fail_count = len(result.get('failed') or [])
                        reason = 'Some files were not updated.' if fail_count else 'The approval could not be finalized.'
                        if album_artwork_copy:
                            reason += ' Cover file was saved.'
                        self.log_msg(f'\nEmbedding incomplete: {album_label} · {result.get("updated", 0)}/{result.get("total", 0)} files updated · option kept for retry.\n')
                        self.log_verbose(f'  Details: {embedded_summary} {reason}{verify_note}\n')
                        extra_verify = f' {verify_summary}' if verify_summary else ''
                        self._set_action_result(f'Approval incomplete. {embedded_summary}{extra_verify} Artwork option kept for retry.')
                        try:
                            self.load_album_for_review(old_key)
                        except Exception:
                            self.refresh_queue_tab()
                        self.set_status_dot('#ff9f0a')
                        self.status_var.set('Approval incomplete. The artwork option was kept so you can retry.')
                    self.refresh_footer()
                    if result.get('failed') and not no_audio_files:
                        warning_lines = []
                        for item in (result.get('failed') or [])[:3]:
                            err = str(item.get('error') or item)
                            name = os.path.basename(str(item.get('file') or ''))
                            warning_lines.append(f'• {name + ": " if name else ""}{err}')
                        more = len(result.get('failed') or []) - len(warning_lines)
                        if more > 0:
                            warning_lines.append(f'• {more} more warning(s) in the log')
                        messagebox.showwarning('Artwork embedded with warnings', f'{embedded_summary}\n\n' + '\n'.join(warning_lines) + '\n\nThe artwork option was kept if approval did not fully complete.')
                elif kind == 'EMBED_ERROR':
                    if payload.get('job_id') != self.active_embed_job_id:
                        continue
                    self.active_embed_job_id = None
                    self.active_embed_album_key = None
                    self._clear_current_operation('embedding')
                    self.progress_var.set(0)
                    self.progress_text.set('')
                    self.set_status_dot('#d00000')
                    self.status_var.set('Approval failed while embedding artwork.')
                    self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
                    messagebox.showerror('Approval failed', payload.get('error', 'Unknown error'))
                elif kind == 'ERROR':
                    self.scan_active = False
                    self.start_btn.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    self.find_btn.configure(state='normal')
                    if hasattr(self, 'queue_download_btn'):
                        self.queue_download_btn.configure(state='normal')
                    self.set_status_dot('#d00000')
                    self.status_var.set('Error. Saved progress is still in the queue database.')
                    messagebox.showerror('Error', payload)
                elif kind == 'DONE':
                    self.scan_active = False
                    self.start_btn.configure(state='normal')
                    self.stop_btn.configure(state='disabled')
                    self.load_saved_queue(silent=True)
                    self.set_status_dot('#26b53f' if not payload['stopped'] else '#f5a623')
                    if payload['stopped']:
                        msg = 'Scan stopped. Albums already scanned are saved in the queue; select any album to find artwork.'
                        self.status_var.set(msg)
                        self._set_action_result(msg)
                        self.log_msg('\nScan stopped by user. Partial album queue saved.\n')
                    else:
                        summary = self._scan_summary_text()
                        self.status_var.set(summary)
                        self._set_action_result(summary)
                        self.log_msg('\n' + summary + f'\nCSV: {payload["csv"]}\n')
                    self.refresh_queue_tab()
                    self.focus_queue_table()
                    self.refresh_footer()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def close_candidate_preview_windows(self):
        for win in list(getattr(self, 'candidate_preview_windows', [])):
            try:
                if win.winfo_exists():
                    win.close()
            except Exception:
                try:
                    win.destroy()
                except Exception:
                    pass
        self.candidate_preview_windows = []

    def focus_queue_table(self):
        try:
            self.notebook.select(self.queue_tab)
            self.queue_tree.focus_set()
            sel = self.queue_tree.selection()
            if sel:
                self.queue_tree.focus(sel[0])
                self.queue_tree.see(sel[0])
            elif self.queue_tree.get_children():
                first = self.queue_tree.get_children()[0]
                self.queue_tree.selection_set(first)
                self.queue_tree.focus(first)
                self.queue_tree.see(first)
            return True
        except Exception:
            return False

    def candidate_quality_label(self, candidate=None):
        candidate = candidate or self.current_candidate() or {}
        score = int(candidate.get('score') or 0)
        if score >= 80:
            return 'Good'
        if score >= 60:
            return 'Usable'
        return 'Weak'

    def candidate_has_risky_warnings(self, candidate=None):
        candidate = candidate or self.current_candidate() or {}
        warnings = [str(w).lower() for w in (candidate.get('warnings') or [])]
        risky_terms = ('below target', 'blurry', 'soft', 'scan', 'photo', 'not square', 'upscaled', 'watermark', 'small file', 'small size')
        if int(candidate.get('score') or 0) < 60:
            return True
        return any(any(term in w for term in risky_terms) for w in warnings)

    def confirm_weak_candidate_if_needed(self, candidate=None):
        candidate = candidate or self.current_candidate()
        if not candidate:
            return False
        try:
            settings = self.settings if isinstance(getattr(self, 'settings', None), dict) else load_settings()
            if not bool(settings.get('warn_before_low_confidence_embed', True)):
                return True
        except Exception:
            pass
        quality = self.candidate_quality_label(candidate)
        if quality != 'Weak' and not self.candidate_has_risky_warnings(candidate):
            return True
        warnings = [w for w in (candidate.get('warnings') or []) if 'same image' not in str(w).lower()]
        warn_txt = ', '.join(str(w) for w in warnings[:3]) if warnings else 'low quality score'
        return messagebox.askyesno(
            'Embed lower-confidence artwork?',
            f'This candidate is marked {quality} and may need review.\n\n'
            f'Score: {int(candidate.get("score") or 0)}/100\n'
            f'Warnings: {warn_txt}\n\n'
            'Embed it anyway?'
        )

    def approve(self):
        c = self.current_candidate()
        if not c:
            return
        if self._block_if_action_transition('Approve + Embed'):
            return
        if self.is_artwork_search_active():
            self.status_var.set('Finish or stop the artwork search before embedding.')
            self._set_action_result('Embedding is disabled while artwork search is running or stopping, to prevent accidental action switching.')
            return
        if self._block_if_write_action_active('Approve + Embed'):
            return
        album_folder = c.get('album_folder') or ''
        settings_for_precheck = load_settings()
        use_nas_worker = worker_enabled_for_path(album_folder, settings_for_precheck)
        if not use_nas_worker and (not album_folder or not os.path.isdir(album_folder)):
            messagebox.showerror(
                'Cannot approve artwork',
                'Album folder is unavailable. Reconnect the drive/NAS or use Actions → Locate Album Folder… then try again.'
            )
            self.status_var.set('Approval blocked: album folder unavailable. Use Locate Album Folder if it moved.')
            return
        image_path = c.get('image_path') or ''
        if not image_path or not os.path.exists(image_path):
            messagebox.showerror('Cannot approve artwork', 'The selected artwork image file is missing. Search or import artwork again.')
            self.status_var.set('Approval blocked: selected artwork image is missing.')
            return
        if not use_nas_worker:
            try:
                embeddable_files = list(iter_music_files(album_folder))
            except Exception as exc:
                embeddable_files = []
                self.status_var.set('Approval blocked: could not read album files.')
                self._set_action_result(f'Cannot embed: could not read the album folder. {exc}')
                messagebox.showerror(
                    'Cannot embed artwork',
                    f'The album folder could not be read. Reconnect the drive/NAS or use Actions → Locate Album Folder… then try again.\n\n{exc}'
                )
                return
            if not embeddable_files:
                label = self._workflow_album_label(c.get('artist', ''), c.get('album', ''), album_key=c.get('album_key'))
                self.status_var.set('Approval blocked: no supported audio files were found in the album folder.')
                self._set_action_result('Cannot embed: no supported audio files found. Candidate kept for review.')
                self.log_msg(f'\nApproval blocked: {label} · no supported audio files found in album folder. Candidate kept for review.\n')
                try:
                    self._pin_album_in_current_filter(c.get('album_key'), reason='embed blocked: no audio files')
                    self.refresh_queue_tab()
                except Exception:
                    pass
                messagebox.showwarning(
                    'Cannot embed artwork',
                    'No supported audio files were found in this album folder, so the artwork was not embedded.\n\n'
                    'Supported embedded formats are MP3, FLAC, M4A and MP4. If the album folder moved or the drive is still loading, use Actions → Locate Album Folder… and try again.'
                )
                return
        if not self.confirm_weak_candidate_if_needed(c):
            self.status_var.set('Approval cancelled. Candidate was left unchanged.')
            return
        self.close_candidate_preview_windows()
        old_key = c['album_key']
        batch_search_continues = bool(
            self.active_find_job_id is not None
            and self.active_find_mode == 'batch'
            and old_key in getattr(self, 'active_search_album_keys', set())
        )
        if batch_search_continues:
            # Do not stop Search Next / batch searches when approving one album.
            # The search worker checks finalized album status before saving late
            # results for this album, so the approved album stays protected while
            # the remaining batch items continue to finish normally.
            self.active_search_album_keys.discard(old_key)
            self.active_search_album_status.pop(old_key, None)
            self.active_search_album_labels.pop(old_key, None)
            self.log_verbose('\nApproved one album while Search Next is running; the rest of the batch will continue. Late results for the approved album will be discarded.\n')
        else:
            self._cancel_active_artwork_search(
                log_message='\nApproving an image stopped the active artwork search. Late background results will be ignored.\n',
                update_controls=False,
            )
        previous_keys = self.queue_navigation_keys()
        if not self.dry_run.get():
            try:
                db.set_candidate_state(c.get('candidate_id'), 'selected', 'selected for approval/embed')
            except Exception:
                pass
        self.embed_job_counter += 1
        job_id = self.embed_job_counter
        self.active_embed_job_id = job_id
        self.active_embed_album_key = old_key
        self.set_status_dot('#007aff')
        self._set_current_operation('embedding', f'Embedding artwork for {c.get("artist", "")} - {c.get("album", "")}…')
        self.progress_text.set('Preparing files…')
        self.progress_var.set(0)
        self.set_review_button_states(has_candidate=True, has_album=True)

        def work(job_id=job_id, cand=dict(c), previous_keys=previous_keys):
            try:
                settings = load_settings()
                resize_enabled = bool(settings.get('resize_approved_artwork', True))
                max_artwork_size = get_max_embedded_artwork_size(settings) if resize_enabled else None
                folder_copy_enabled = bool(settings.get('save_approved_artwork_to_album_folder', False))
                folder_copy_error = ''
                approved_copy = ''
                album_artwork_copy = ''
                album_candidates = [cand]

                if self.dry_run.get():
                    # Dry run should never rewrite tags, move candidate files, or
                    # mutate queue/candidate state.  Older builds hit an unbound
                    # local here and could turn a dry run into an embed error.
                    total = len(list(iter_music_files(cand['album_folder'])))
                    try:
                        preview_bytes, _mime = prepare_jpeg_bytes(cand['image_path'], max_size=max_artwork_size, make_square=resize_enabled)
                        embedded_w, embedded_h = image_dimensions_from_bytes(preview_bytes) or (cand.get('width'), cand.get('height'))
                    except Exception:
                        embedded_w, embedded_h = cand.get('width'), cand.get('height')
                    result = {
                        'dry_run': True,
                        'updated': 0,
                        'total': total,
                        'failed': [],
                        'image_width': embedded_w,
                        'image_height': embedded_h,
                        'would_save_folder_cover': bool(folder_copy_enabled),
                    }
                    approval_complete = False
                    final_status = (db.get_album(cand['album_key']) or {}).get('status') or 'candidate_found'
                    temp_removed = 0
                else:
                    def progress(done, total, fp):
                        self.q.put(('EMBED_PROGRESS', {'job_id': job_id, 'done': done, 'total': total, 'file': fp}))
                    use_remote_worker = worker_enabled_for_path(cand.get('album_folder') or '', settings)
                    if use_remote_worker:
                        self.q.put(('EMBED_PROGRESS', {'job_id': job_id, 'done': 0, 'total': 0, 'file': 'NAS worker'}))
                        result = embed_album_remote(
                            cand['album_folder'],
                            cand['image_path'],
                            cand['album_key'],
                            artist=cand.get('artist') or '',
                            album=cand.get('album') or '',
                            backup=self.backup.get(),
                            max_artwork_size=max_artwork_size,
                            make_square=resize_enabled,
                            save_folder_cover=folder_copy_enabled,
                            embed=True,
                            settings=settings,
                        )
                        album_artwork_copy = result.get('album_artwork_copy') or ''
                    else:
                        result = embed_album(
                            cand['album_folder'],
                            cand['image_path'],
                            cand['album_key'],
                            backup=self.backup.get(),
                            progress=progress,
                            max_artwork_size=max_artwork_size,
                            make_square=resize_enabled,
                        )
                        # Do not keep a second permanent copy in approved_artwork by
                        # default. The chosen image is embedded into the tracks, and
                        # temporary/import source files are moved to Trash only after
                        # the whole approval has completed successfully.
                        if folder_copy_enabled:
                            try:
                                album_artwork_copy = save_approved_artwork_to_album_folder(
                                    cand['image_path'], cand['artist'], cand['album'], cand['album_folder'], max_artwork_size=max_artwork_size, make_square=resize_enabled
                                )
                            except Exception as copy_exc:
                                folder_copy_error = str(copy_exc)
                                result.setdefault('failed', []).append({
                                    'file': cand.get('album_folder') or '',
                                    'error': f'Folder cover copy failed: {copy_exc}',
                                })

                    try:
                        album_candidates = db.load_candidates_for_album(cand['album_key'], include_rejected=True)
                    except Exception:
                        album_candidates = [cand]

                    try:
                        embedded_w = result.get('image_width') or result.get('width') or cand.get('width')
                        embedded_h = result.get('image_height') or result.get('height') or cand.get('height')
                        embedded_dims = f"{embedded_w or '?'}×{embedded_h or '?'}"
                        approved_at = db.now()
                        failed_items = list(result.get('failed') or [])
                        embed_failures = [f for f in failed_items if 'Folder cover copy failed' not in str(f.get('error', ''))]
                        try:
                            updated_files = int(result.get('updated') or 0)
                            total_files = int(result.get('total') or 0)
                        except Exception:
                            updated_files = total_files = 0
                        no_audio_files = bool(result.get('no_audio_files') or total_files <= 0)
                        embed_ok = bool(total_files > 0 and updated_files == total_files and not embed_failures)
                        verify_required = bool(settings.get('verify_after_embed_before_good', True))
                        verification = {}
                        verified_ok = True
                        if embed_ok and verify_required:
                            verification_info = {
                                'album_key': cand.get('album_key'),
                                'album_path': cand.get('album_folder') or '',
                                'artist': cand.get('artist') or '',
                                'album': cand.get('album') or '',
                            }
                            try:
                                verification = self._verify_album_after_write(
                                    verification_info,
                                    settings=settings,
                                    target_size=max_artwork_size or get_preferred_artwork_size(settings),
                                    expected_dimensions=embedded_dims,
                                )
                                verified_ok = bool(verification.get('ok'))
                            except Exception as verify_exc:
                                verified_ok = False
                                verification = {'ok': False, 'summary': f'post-embed verification failed: {verify_exc}', 'checked_at': db.now(), 'problem_files': []}
                                try:
                                    db.update_album_notes(cand['album_key'], {'last_verification': verification})
                                except Exception:
                                    pass
                            result['post_embed_verification'] = verification
                        folder_needs_save = bool(folder_copy_enabled and not album_artwork_copy)
                        approval_complete = bool(embed_ok and not folder_needs_save and verified_ok)
                        final_status = 'approved' if approval_complete else 'candidate_found'
                        if approval_complete:
                            final_reason = 'approved artwork embedded and verified' if verify_required else 'approved artwork embedded'
                        elif no_audio_files:
                            final_reason = 'no supported audio files found; artwork option kept for review'
                        elif embed_ok and verify_required and not verified_ok:
                            final_reason = 'post-embed verification found problem files; artwork option kept for review'
                        else:
                            final_reason = 'approval incomplete; artwork option kept for retry'

                        if approval_complete:
                            db.mark_candidate(cand.get('candidate_id'), approved=True, state_reason='approved and embedded successfully')
                            db.mark_album_candidates(cand['album_key'], rejected=True, except_candidate_id=cand.get('candidate_id'), state_reason='superseded by approved artwork')
                            db.set_album_status(cand['album_key'], 'approved')
                        else:
                            # Do not hide/reject the selected candidate when the write
                            # only partially completed.  Keeping it visible lets the
                            # user retry after reconnecting storage or fixing cover.jpg.
                            db.set_album_status(cand['album_key'], final_status)
                            try:
                                db.set_candidate_state(cand.get('candidate_id'), 'failed_embed', final_reason)
                            except Exception:
                                pass

                        db.update_album_notes(cand['album_key'], {
                            'approved_artwork': {
                                'source': cand.get('source') or '',
                                'source_detail': cand.get('source_detail') or '',
                                'dimensions': f"{cand.get('width') or '?'}×{cand.get('height') or '?'}",
                                'embedded_dimensions': embedded_dims,
                                'updated_files': updated_files,
                                'total_files': total_files,
                                'score': int(cand.get('score') or 0),
                                'source_url': cand.get('source_url') or '',
                                'approved_at': approved_at if approval_complete else '',
                                'attempted_at': approved_at,
                                'resized_to_target': max_artwork_size or '',
                                'complete': approval_complete,
                                'verify_required': bool(locals().get('verify_required', False)),
                                'verified': bool(locals().get('verified_ok', False)) if locals().get('verify_required', False) else False,
                                'verification_summary': (locals().get('verification') or {}).get('summary', ''),
                            },
                            # Approval embeds a prepared baseline JPEG. Clear stale
                            # conversion notes only when the embed actually completed.
                            'artwork_compatibility': {
                                'needs_conversion': bool((not embed_ok) and not no_audio_files),
                                'issue': '' if (embed_ok or no_audio_files) else 'embedding failed or incomplete',
                                'converted_to': f'{embedded_dims} baseline JPEG' if embed_ok else '',
                                'converted_at': approved_at if embed_ok else '',
                            },
                            'album_folder_cover': {
                                'needs_save': folder_needs_save,
                                'issue': (folder_copy_error or 'folder cover missing') if folder_needs_save else '',
                                'saved_path': album_artwork_copy or '',
                                'saved_at': approved_at if album_artwork_copy else '',
                                'checked_at': approved_at,
                            },
                            'state_evaluation': {
                                'status': final_status,
                                'reason': final_reason,
                            },
                            'partial_failure': {
                                'reason': '' if approval_complete else final_reason,
                                'failed_items': len(failed_items),
                                'updated_files': updated_files,
                                'total_files': total_files,
                                'checked_at': approved_at,
                            },
                        })
                        if approval_complete:
                            try:
                                db.update_album_notes(cand['album_key'], deep_check_resolved_note(
                                    updated_files,
                                    max_artwork_size or get_preferred_artwork_size(settings),
                                    embedded_dims,
                                    source='approve/embed',
                                ))
                            except Exception:
                                pass
                        if embedded_w and embedded_h:
                            db.update_album_path(cand['album_key'], cand.get('album_folder') or '', width=embedded_w, height=embedded_h)
                    except Exception as state_exc:
                        approval_complete = False
                        final_status = 'candidate_found'
                        db.set_album_status(cand['album_key'], final_status)
                        try:
                            db.set_candidate_state(cand.get('candidate_id'), 'failed_embed', f'approval state update failed: {state_exc}')
                            db.update_album_notes(cand['album_key'], {
                                'partial_failure': {
                                    'reason': f'approval state update failed: {state_exc}',
                                    'checked_at': db.now(),
                                },
                                'state_evaluation': {
                                    'status': final_status,
                                    'reason': 'approval incomplete; artwork option kept for retry',
                                },
                            })
                        except Exception:
                            pass

                    temp_removed = self._remove_candidate_files(album_candidates) if approval_complete else 0
                self.q.put(('EMBED_DONE', {
                    'job_id': job_id,
                    'album_key': cand['album_key'],
                    'candidate': cand,
                    'previous_keys': previous_keys,
                    'result': result,
                    'approved_copy': approved_copy,
                    'album_artwork_copy': album_artwork_copy,
                    'temp_removed': locals().get('temp_removed', 0),
                    'approval_complete': bool(locals().get('approval_complete', False)),
                    'final_status': locals().get('final_status', ''),
                    'verification': locals().get('verification', {}),
                }))
            except Exception as exc:
                self.q.put(('EMBED_ERROR', {'job_id': job_id, 'error': str(exc)}))

        self.embed_worker = threading.Thread(target=work, daemon=True)
        self.embed_worker.start()

    def reject(self):
        c = self.current_candidate()
        if not c:
            return
        self.close_candidate_preview_windows()
        old_key = c['album_key']
        previous_keys = self.queue_navigation_keys()
        db.mark_candidate(c.get('candidate_id'), rejected=True)
        removed = self._remove_candidate_files([c])
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nRejected candidate: {self._workflow_album_label(c.get("artist", ""), c.get("album", ""))}.{cleanup_txt}\n')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        remaining = self.groups.get(old_key, [])
        if remaining:
            self.current_album_key = old_key
            self.candidate_index = min(self.candidate_index, len(remaining) - 1)
            self.show_current_album()
            self.refresh_queue_tab()
        else:
            if self.active_find_album_key == old_key:
                self._cancel_active_artwork_search(
                    log_message='\nRejecting the last saved image stopped the active search for this album.\n',
                    update_controls=False,
                )
            self._reclassify_after_candidate_rejection(old_key)
            self.select_next_album_after(old_key, previous_keys, message=f'Rejected the last saved image for {c["artist"]} - {c["album"]}. Advanced to the next visible album.')
        self.refresh_footer()

    def skip(self):
        info = self.active_album_info()
        if not info:
            return
        self.close_candidate_preview_windows()
        old_key = info['album_key']
        batch_search_continues = bool(
            self.active_find_job_id is not None
            and self.active_find_mode == 'batch'
            and old_key in getattr(self, 'active_search_album_keys', set())
        )
        if batch_search_continues:
            self.active_search_album_keys.discard(old_key)
            self.active_search_album_status.pop(old_key, None)
            self.active_search_album_labels.pop(old_key, None)
            self.log_verbose('\nSkipped one album while Search Next is running; the rest of the batch will continue. Late results for the skipped album will be discarded.\n')
        elif self.active_find_album_key == old_key:
            self._cancel_active_artwork_search(
                log_message='\nSkipping the album stopped the active artwork search. Late background results will be ignored.\n',
                update_controls=False,
            )
        previous_keys = self.queue_navigation_keys()
        db.set_album_status(old_key, 'reviewed_skipped')
        db.mark_album_candidates(old_key, rejected=True)
        removed = self._remove_temporary_artwork_for_album(old_key)
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nSkipped: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.{cleanup_txt}\n')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.select_next_album_after(old_key, previous_keys, message=f'Skipped {info["artist"]} - {info["album"]}. Advanced to the next visible album.')
        self.refresh_footer()

    def _is_path_inside_roots(self, path, roots):
        if not path:
            return False
        try:
            candidate_path = Path(path).expanduser().resolve()
            for root in roots:
                try:
                    root_path = Path(root).expanduser().resolve()
                    common = os.path.commonpath([str(candidate_path), str(root_path)])
                    if common == str(root_path):
                        return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _is_app_temporary_artwork_path(self, path):
        """Only auto-trash artwork files managed by temp/import areas.

        Approval and cleanup should not touch arbitrary files the user may have
        chosen elsewhere on disk. Provider downloads live in temporary_candidates
        and manual imports are copied into manual_imports, so those are safe to
        trash once an album is handled.
        """
        return self._is_path_inside_roots(path, (TEMP_DIR, IMPORT_DIR))

    def _is_app_approved_artwork_path(self, path):
        return self._is_path_inside_roots(path, (APPROVED_DIR,))

    def _trash_managed_file_path(self, path, roots):
        """Move an app-managed file to ~/.Trash, never arbitrary user files."""
        try:
            if not path or not os.path.exists(path) or not self._is_path_inside_roots(path, roots):
                return 0
            src = Path(path).expanduser().resolve()
            trash_dir = Path.home() / '.Trash'
            trash_dir.mkdir(parents=True, exist_ok=True)
            dest = trash_dir / src.name
            if dest.exists():
                stamp = time.strftime('%Y%m%d_%H%M%S')
                dest = trash_dir / f'{src.stem} {stamp}{src.suffix}'
                i = 2
                while dest.exists():
                    dest = trash_dir / f'{src.stem} {stamp} {i}{src.suffix}'
                    i += 1
            shutil.move(str(src), str(dest))
            return 1
        except Exception:
            return 0

    def _remove_candidate_file_path(self, path):
        return self._trash_managed_file_path(path, (TEMP_DIR, IMPORT_DIR))

    def _remove_candidate_files(self, candidates):
        removed = 0
        seen = set()
        for cand in candidates or []:
            try:
                path = cand.get('image_path') if isinstance(cand, dict) else cand
                if not path or path in seen:
                    continue
                seen.add(path)
                removed += self._remove_candidate_file_path(path)
            except Exception:
                pass
        return removed

    def _remove_temporary_artwork_for_album(self, album_key):
        if not album_key:
            return 0
        try:
            candidates = db.load_candidates_for_album(album_key, include_rejected=True)
        except Exception:
            candidates = []
        return self._remove_candidate_files(candidates)

    def _reclassify_after_candidate_rejection(self, album_key):
        """Re-evaluate the album after its active candidates are rejected.

        Rejecting the last option should not blindly turn every album into
        No Options. If the current embedded artwork is Not Square or Convert,
        the best next action is still Convert/Save; if it is truly below target
        after a search, the evaluator preserves No Options/Needs Search.
        """
        if not album_key:
            return {'status': '', 'reason': ''}
        try:
            db.set_album_status(album_key, 'no_candidate', reason='all saved artwork options rejected')
            return db.evaluate_and_set_album_state(
                album_key,
                candidate_count=0,
                preserve_user_terminal=False,
                settings=getattr(self, 'settings', None),
            )
        except Exception:
            try:
                db.set_album_status(album_key, 'no_candidate', reason='all saved artwork options rejected')
            except Exception:
                pass
            return {'status': 'no_candidate', 'reason': 'all saved artwork options rejected'}

    def reject_all_candidates(self):
        info = self.active_album_info()
        if not info:
            return
        album_key = info.get('album_key')
        candidates = db.load_candidates_for_album(album_key, include_rejected=False)
        if not candidates:
            self.status_var.set('No downloaded candidates to reject for the selected album.')
            return
        if not messagebox.askyesno('Reject all candidates?', f'Reject all {len(candidates)} downloaded artwork candidate(s) for this album?'):
            return
        self.close_candidate_preview_windows()
        if self.active_find_album_key in (album_key, 'BATCH'):
            self._cancel_active_artwork_search(
                log_message='\nRejecting all candidates stopped the active artwork search. Late background results will be ignored.\n',
                update_controls=False,
            )
        previous_keys = self.queue_navigation_keys()
        self._remove_candidate_files(candidates)
        db.mark_album_candidates(album_key, rejected=True)
        self._reclassify_after_candidate_rejection(album_key)
        self.log_msg(f'\nRejected all candidates: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.\n')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.select_next_album_after(album_key, previous_keys, message=f'Rejected all candidates for {info["artist"]} - {info["album"]}.')
        self.refresh_footer()

    def search_again_from_scratch(self):
        info = self.active_album_info()
        if not info:
            return
        if self.is_artwork_search_active():
            self.status_var.set('Artwork search is already running. Press Stop Search before starting another one.')
            return
        album_key = info.get('album_key')
        candidates = db.load_candidates_for_album(album_key, include_rejected=False)
        if candidates and not messagebox.askyesno('Search again from scratch?', f'This will reject the {len(candidates)} saved candidate(s) for this album, then search again. Continue?'):
            return
        self.close_candidate_preview_windows()
        self._remove_candidate_files(candidates)
        db.mark_album_candidates(album_key, rejected=True)
        db.set_album_status(album_key, 'needs_review')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.refresh_queue_tab()
        album_rec = db.get_album(album_key) or {}
        search_info = self._album_to_search_info({**info, **album_rec, 'album_path': info.get('album_path') or album_rec.get('album_path')})
        self.log_msg(f'\nRework started: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.\n')
        self._start_artwork_search([search_info], mode='single', total_limit=get_max_candidates_per_album())

    def _album_needs_convert_save_work(self, album):
        try:
            return self.queue_workflow_bucket(album) in {'Not Square', 'Convert'}
        except Exception:
            return bool((album or {}).get('status') in ('not_square_artwork', 'incompatible_artwork'))

    def _album_has_conversion_or_folder_cover_work(self, album):
        """Return True when Good would bypass compatibility/folder-cover work."""
        if not album:
            return False
        notes = album.get('notes_json')
        if notes is None and album.get('album_key'):
            try:
                fresh = db.get_album(album.get('album_key')) or {}
                notes = fresh.get('notes_json')
                album = {**fresh, **album}
            except Exception:
                notes = None
        notes = notes or {}
        try:
            return bool(album.get('status') == 'incompatible_artwork' or needs_convert_reason(notes, self.settings))
        except Exception:
            return bool(album.get('status') == 'incompatible_artwork')

    def _mark_album_good_in_db(self, album_key, reason='marked good by user'):
        """Finalize an album as Good and clear stale conversion prompts.

        The Good action is an explicit user decision. It should not immediately
        redraw as Convert because old compatibility/folder-cover notes are still
        present; a future rescan can reintroduce requirements if the current
        settings really demand them.
        """
        if not album_key:
            return
        marked_at = db.now()
        db.set_album_status(album_key, 'already_good')
        db.update_album_notes(album_key, {
            'artwork_compatibility': {
                'needs_conversion': False,
                'issue': '',
                'accepted_as_is_at': marked_at,
            },
            'album_folder_cover': {
                'needs_save': False,
                'issue': '',
                'accepted_as_is_at': marked_at,
            },
            'state_evaluation': {
                'status': 'already_good',
                'reason': reason,
            },
        })

    def _confirm_mark_good_with_pending_conversion(self, album, count=1):
        if self._album_has_conversion_or_folder_cover_work(album):
            msg = (
                'This album still needs compatibility/folder-cover work.\n\n'
                'Use Actions → Convert/Save Embedded Artwork to convert embedded artwork to baseline JPEG '
                'and/or write cover.jpg.\n\n'
            )
            if count > 1:
                msg = (
                    f'{count} selected album(s) include items that still need compatibility/folder-cover work.\n\n'
                    'Use the Needs Convert filter and Actions → Convert/Save Embedded Artwork first where needed.\n\n'
                )
            msg += 'Mark as Good anyway?'
            if not messagebox.askyesno('Mark Good anyway?', msg):
                return False

        # For a single album, verify the actual embedded files before accepting
        # Good. This prevents stale queue metadata or partial NAS/SMB writes from
        # being hidden by a manual Good decision without an explicit warning.
        if int(count or 1) == 1 and (album or {}).get('album_path') and (album or {}).get('album_key'):
            try:
                self.status_var.set('Verifying embedded artwork before marking Good…')
                self.root.update_idletasks()
            except Exception:
                pass
            try:
                target = get_preferred_artwork_size(self.settings)
                result = self._run_album_deep_check(album, target_size=target, problem_files=True, settings=self.settings)
                verification = self._persist_deep_check_and_verification(album, result, verification_source='mark-good verification', problem_files=True)
                if verification.get('ok'):
                    return True
                problem_lines = []
                for row in (verification.get('problem_files') or [])[:6]:
                    if isinstance(row, dict):
                        dims = f' · {row.get("dimensions")}' if row.get('dimensions') else ''
                        issues = ', '.join(row.get('issues') or [])
                        problem_lines.append(f'• {row.get("file")}{dims}: {issues}')
                    else:
                        problem_lines.append(f'• {row}')
                more = len(verification.get('problem_files') or []) - len(problem_lines)
                if more > 0:
                    problem_lines.append(f'• …and {more} more')
                detail = '\n'.join(problem_lines) if problem_lines else verification.get('summary', '')
                return messagebox.askyesno(
                    'Mark Good despite verification problems?',
                    'Verification found that this album still needs attention.\n\n'
                    f'{verification.get("summary") or "Problem files found."}\n\n'
                    f'{detail}\n\n'
                    'Mark as Good anyway?'
                )
            except Exception as exc:
                return messagebox.askyesno(
                    'Could not verify album',
                    f'The app could not verify the embedded artwork before marking Good:\n\n{exc}\n\nMark as Good anyway?'
                )
        return True

    def mark_current_good(self):
        info = self.active_album_info()
        if not info:
            return
        if not self._confirm_mark_good_with_pending_conversion(info):
            self.status_var.set('Mark Good cancelled. Convert/save embedded artwork first if this album needs compatibility work.')
            return
        self.close_candidate_preview_windows()
        old_key = info['album_key']
        batch_search_continues = bool(
            self.active_find_job_id is not None
            and self.active_find_mode == 'batch'
            and old_key in getattr(self, 'active_search_album_keys', set())
        )
        if batch_search_continues:
            self.active_search_album_keys.discard(old_key)
            self.active_search_album_status.pop(old_key, None)
            self.active_search_album_labels.pop(old_key, None)
            self.log_verbose('\nMarked one album good while Search Next is running; the rest of the batch will continue. Late results for this album will be discarded.\n')
        previous_keys = self.queue_navigation_keys()
        self._mark_album_good_in_db(old_key)
        db.mark_album_candidates(old_key, rejected=True)
        removed = self._remove_temporary_artwork_for_album(old_key)
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nMarked Good: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.{cleanup_txt}\n')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.select_next_album_after(old_key, previous_keys, message=f'Marked current artwork as good for {info["artist"]} - {info["album"]}.')
        self.refresh_footer()

    def ignore_album(self):
        info = self.active_album_info()
        if not info:
            return
        self.close_candidate_preview_windows()
        old_key = info['album_key']
        batch_search_continues = bool(
            self.active_find_job_id is not None
            and self.active_find_mode == 'batch'
            and old_key in getattr(self, 'active_search_album_keys', set())
        )
        if batch_search_continues:
            self.active_search_album_keys.discard(old_key)
            self.active_search_album_status.pop(old_key, None)
            self.active_search_album_labels.pop(old_key, None)
            self.log_verbose('\nIgnored one album while Search Next is running; the rest of the batch will continue. Late results for this album will be discarded.\n')
        previous_keys = self.queue_navigation_keys()
        db.set_album_status(old_key, 'ignored')
        db.mark_album_candidates(old_key, rejected=True)
        removed = self._remove_temporary_artwork_for_album(old_key)
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nIgnored: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.{cleanup_txt}\n')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.select_next_album_after(old_key, previous_keys, message=f'Ignored {info["artist"]} - {info["album"]}.')
        self.refresh_footer()

    def _refresh_after_multi_queue_action(self, message='Queue updated.'):
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.refresh_queue_tab()
        self.refresh_footer()
        if self.current_album_key and db.get_album(self.current_album_key):
            self.load_album_for_review(self.current_album_key)
        else:
            self.load_saved_queue(silent=True)
        self.status_var.set(message)

    def ignore_selected_albums(self):
        albums = self.selected_queue_albums()
        if not albums:
            return
        if len(albums) > 1 and not messagebox.askyesno('Ignore selected albums?', f'Ignore {len(albums)} selected album(s)?'):
            return
        removed = 0
        for album in albums:
            db.set_album_status(album['album_key'], 'ignored')
            db.mark_album_candidates(album['album_key'], rejected=True)
            removed += self._remove_temporary_artwork_for_album(album['album_key'])
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nIgnored {len(albums)} selected album(s).{cleanup_txt}\n')
        self._refresh_after_multi_queue_action(f'Ignored {len(albums)} selected album(s).{cleanup_txt}')

    def mark_selected_good(self):
        albums = self.selected_queue_albums()
        if not albums:
            return
        pending_conversion = [a for a in albums if self._album_has_conversion_or_folder_cover_work(a)]
        if pending_conversion:
            if not self._confirm_mark_good_with_pending_conversion(pending_conversion[0], count=len(pending_conversion)):
                self.status_var.set('Mark selected Good cancelled. Convert/save embedded artwork first for Needs Convert albums.')
                return
        elif len(albums) > 1 and not messagebox.askyesno('Mark selected good?', f'Mark current artwork as good for {len(albums)} selected album(s)?'):
            return
        removed = 0
        for album in albums:
            self._mark_album_good_in_db(album['album_key'])
            db.mark_album_candidates(album['album_key'], rejected=True)
            removed += self._remove_temporary_artwork_for_album(album['album_key'])
        cleanup_txt = f' Trashed {removed} temporary/import artwork file(s).' if removed else ''
        self.log_msg(f'\nMarked {len(albums)} selected album(s) as good.{cleanup_txt}\n')
        self._refresh_after_multi_queue_action(f'Marked {len(albums)} selected album(s) as good.{cleanup_txt}')

    def reject_candidates_for_selected(self):
        albums = self.selected_queue_albums()
        if not albums:
            return
        total = 0
        for album in albums:
            total += len(db.load_candidates_for_album(album['album_key'], include_rejected=False))
        if total <= 0:
            self.status_var.set('No downloaded candidates to reject for the selected album(s).')
            return
        if not messagebox.askyesno('Reject candidates for selected?', f'Reject/delete {total} candidate image(s) across {len(albums)} selected album(s)?'):
            return
        for album in albums:
            candidates = db.load_candidates_for_album(album['album_key'], include_rejected=False)
            self._remove_candidate_files(candidates)
            db.mark_album_candidates(album['album_key'], rejected=True)
            self._reclassify_after_candidate_rejection(album['album_key'])
        self.log_msg(f'\nRejected {total} candidate image(s) for {len(albums)} selected album(s).\n')
        self._refresh_after_multi_queue_action(f'Rejected {total} candidate image(s) for selected album(s).')

    def rescan_selected_albums(self):
        albums = self.selected_queue_albums()
        if not albums:
            return
        count = 0
        for album in albums:
            if album.get('album_path'):
                self._rescan_album_path(album.get('album_path'))
                count += 1
        self.load_saved_queue(silent=True)
        self.status_var.set(f'Rescanned {count} selected album folder(s).')
        self._set_action_result(f'Rescanned {count} selected album folder(s).')

    def search_selected_albums(self):
        if self.is_artwork_search_active():
            self.status_var.set('Artwork search is already running. Press Stop Search before starting another one.')
            return
        selected = [a for a in self.selected_queue_albums() if a.get('status') not in ('approved', 'reviewed_skipped', 'already_good', 'ignored')]
        convert_only = [a for a in selected if self._album_needs_convert_save_work(a)]
        albums = [a for a in selected if not self._album_needs_convert_save_work(a)]
        if not albums:
            if convert_only:
                self.status_var.set('Selected album(s) need Not Square/Convert Save work, not provider search. Use the Actions menu.')
            else:
                self.status_var.set('No selected albums need artwork search.')
            return
        if convert_only and not messagebox.askyesno('Skip Needs Convert albums?', f'{len(convert_only)} selected album(s) need Not Square/Convert Save work rather than provider search. Search the other {len(albums)} selected album(s)?'):
            self.status_var.set('Search selected cancelled.')
            return
        preview = '\n'.join(f'{i+1}. {a.get("artist", "")} — {a.get("album", "")}' for i, a in enumerate(albums[:12]))
        if len(albums) > 12:
            preview += f'\n… and {len(albums) - 12} more'
        if not messagebox.askyesno('Search selected albums?', f'Search artwork for {len(albums)} selected album(s)?\n\n{preview}'):
            return
        self.log_msg(f'\nSearch selected started: {len(albums)} album(s).\n')
        self.log_verbose(f'  Albums:\n{preview}\n')
        infos = [self._album_to_search_info(a) for a in albums]
        self._start_artwork_search(infos, mode='batch', total_limit=get_max_candidates_per_album())

    def _library_root_for_album_path(self, album_path):
        p = Path(album_path).expanduser()
        try:
            return str(p.parent.parent) if p.parent and p.parent.parent else str(p.parent)
        except Exception:
            return str(p.parent)

    def _rescan_album_path(self, album_path, *, force_deep_check=False):
        if not album_path or not os.path.isdir(album_path):
            return None
        try:
            existing = db.find_album_by_path(album_path) if hasattr(db, 'find_album_by_path') else None
        except Exception:
            existing = None
        if existing:
            self._clear_current_art_cache(existing, remove_files=True)
        try:
            names = os.listdir(album_path)
        except Exception:
            return None
        music = [n for n in names if os.path.isfile(os.path.join(album_path, n)) and n.lower().endswith(('.mp3', '.flac', '.m4a', '.mp4'))]
        if not music:
            return None
        library_root = self._library_root_for_album_path(album_path)
        identity = inspect_album_identity(album_path, library_root, music)
        row = analyze_album_folder(album_path, library_root, include_missing=True, music_files=music, identity=identity, force_deep_check=force_deep_check)
        if row:
            artist, album, w, h, example, album_path2, key, identity = row
            width_value = None if w == 'Missing' else w
            height_value = None if h == 'Missing' else h
            candidate_count = 0
            try:
                candidate_count = len(db.load_candidates_for_album(key, include_rejected=False))
            except Exception:
                candidate_count = 0
            status, status_reason = evaluate_album_state(
                width_value,
                height_value,
                identity.get('notes') or {},
                current_status=(db.get_album(key) or {}).get('status') or 'needs_review',
                candidate_count=candidate_count,
                preserve_user_terminal=False,
                settings=self.settings,
            )
            try:
                identity = dict(identity)
                merged_notes = dict(identity.get('notes') or {})
                merged_notes['state_evaluation'] = {'status': status, 'reason': status_reason}
                identity['notes'] = merged_notes
            except Exception:
                pass
            db.upsert_album(
                key, artist, album, album_path2, status=status,
                width=width_value,
                height=height_value,
                example_file=example,
                meta=identity,
            )
            self._clear_current_art_cache(key, remove_files=True)
            return key
        # analyze_album_folder already marks already_good when nothing needs action.
        return None

    def rescan_selected_album(self):
        info = self.active_album_info()
        if not info or not info.get('album_path'):
            return
        # Explicit refresh means "trust disk/tags now", not the previous
        # Application Support preview.  Clear the cache before and after the
        # scan because the album key can be recomputed from file metadata.
        self._clear_current_art_cache(info, remove_files=True)
        key = self._rescan_album_path(info['album_path'])
        if key:
            self._clear_current_art_cache(key, remove_files=True)
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        if key:
            self.load_album_for_review(key)
            # Force a fresh embedded-art read for the review pane immediately
            # after Refresh from Disk.  This prevents a stale cached current
            # artwork image from surviving when NAS/SMB metadata lags.
            try:
                album = db.get_album(key) or self.current_album_info
                fresh = self.current_art_info(album, force_refresh=True)
                self.current_old_art_info = fresh
                if fresh:
                    self.current_old_img = self.show_image(self.old_label, fresh.get('image_path') or fresh.get('bytes'), maxsize=(self.art_px, self.art_px))
                    self.old_size_var.set(self.current_size_label(fresh.get('width'), fresh.get('height')))
            except Exception:
                pass
            msg = f'Refreshed selected album from disk: {info["artist"]} - {info["album"]}.'
            self.status_var.set(msg)
            self._set_action_result(msg)
        else:
            self.load_saved_queue(silent=True)
            msg = 'Refreshed selected album from disk. It now appears to meet the current cutoff or has no readable music files.'
            self.status_var.set(msg)
            self._set_action_result(msg)
        self.refresh_queue_tab()
        self.refresh_footer()

    def _album_folders_under(self, root_path):
        folders = []
        for root, _, files in os.walk(root_path):
            if any(n.lower().endswith(('.mp3', '.flac', '.m4a', '.mp4')) for n in files):
                folders.append(root)
        return folders

    def _local_deep_check_result(self, album_path, target_size, *, problem_files=False):
        names = []
        try:
            names = sorted(os.listdir(album_path), key=lambda x: x.lower())
        except Exception:
            names = []
        music = [n for n in names if os.path.isfile(os.path.join(album_path, n)) and n.lower().endswith(('.mp3', '.flac', '.m4a', '.mp4'))]
        deep = _deep_check_album_files(album_path, music, target_size)
        deep['source'] = 'mac-local'
        rows = deep_check_album_problem_files(album_path, target_size=target_size, limit=500) if problem_files else []
        return {'deep_file_check': deep, 'problem_files': rows}

    def _run_album_deep_check(self, info, *, target_size=None, problem_files=False, settings=None):
        settings = settings or load_settings()
        album_path = (info or {}).get('album_path') or (info or {}).get('album_folder') or ''
        target = int(target_size or get_preferred_artwork_size(settings))
        if not album_path:
            raise ValueError('Album path is missing.')
        if worker_enabled_for_path(album_path, settings):
            return deep_check_album_remote(album_path, target_size=target, settings=settings, problem_files=problem_files)
        if not os.path.isdir(album_path):
            raise ValueError('Album folder could not be found. Reconnect the drive/NAS or use Locate Album Folder first.')
        return self._local_deep_check_result(album_path, target, problem_files=problem_files)

    def _deep_check_summary_text(self, deep):
        deep = deep or {}
        def count(key):
            try:
                return int(deep.get(key) or 0)
            except Exception:
                return 0
        checked = count('checked_files')
        target = deep.get('target_size') or get_preferred_artwork_size(self.settings)
        if not checked:
            return 'No supported music files checked.'
        bad = []
        for key, label in (
            ('missing_count', 'missing'),
            ('below_target_count', 'below target'),
            ('non_square_count', 'not square'),
            ('incompatible_count', 'not baseline JPEG'),
            ('unreadable_count', 'unreadable'),
        ):
            n = count(key)
            if n:
                bad.append(f'{n} {label}')
        if bad:
            return f'{checked} file(s) checked at {target}px · ' + ', '.join(bad)
        return f'Verified: {checked}/{checked} file(s) have target-size square baseline JPEG artwork at {target}px.'

    def _persist_deep_check_and_verification(self, info, result, *, verification_source='manual check', problem_files=False, expected_dimensions=''):
        key = (info or {}).get('album_key') or ''
        if not key:
            return {'ok': False, 'summary': 'Album key missing.'}
        deep = result.get('deep_file_check') if isinstance(result, dict) else {}
        if not isinstance(deep, dict):
            deep = {}
        rows = list((result or {}).get('problem_files') or []) if isinstance(result, dict) else []
        checked = int(deep.get('checked_files') or 0)
        ok = bool(checked > 0 and not deep.get('requires_action'))
        checked_at = deep.get('checked_at') or db.now()
        summary = self._deep_check_summary_text(deep)
        verification = {
            'ok': ok,
            'summary': summary,
            'checked_files': checked,
            'target_size': deep.get('target_size') or get_preferred_artwork_size(self.settings),
            'checked_at': checked_at,
            'source': verification_source,
            'expected_dimensions': expected_dimensions or '',
            'problem_count': len(rows),
            'problem_files': rows[:50],
        }
        updates = {
            'deep_file_check': deep,
            'last_verification': verification,
            'last_problem_files': {
                'checked_at': checked_at,
                'target_size': verification.get('target_size'),
                'rows': rows[:50],
                'problem_count': len(rows),
                'source': verification_source,
            },
        }
        try:
            db.update_album_notes(key, updates)
        except Exception:
            pass
        try:
            width = deep.get('example_width') or deep.get('min_width')
            height = deep.get('example_height') or deep.get('min_height')
            example = deep.get('first_issue_file') or deep.get('example_file') or None
            width = int(width) if width not in (None, '') else None
            height = int(height) if height not in (None, '') else None
            if width and height:
                db.update_album_path(key, (info or {}).get('album_path') or '', example_file=example, width=width, height=height)
        except Exception:
            pass
        return {'ok': ok, 'summary': summary, 'deep_file_check': deep, 'problem_files': rows, 'checked_files': checked}

    def _verify_album_after_write(self, info, *, settings=None, target_size=None, expected_dimensions='', attempts=4, delay=0.6):
        """Re-read every file after a write, with brief NAS/SMB-staleness retries."""
        settings = settings or load_settings()
        last_result = None
        last_persisted = None
        for attempt in range(max(1, int(attempts or 1))):
            if attempt:
                time.sleep(float(delay or 0.6))
            result = self._run_album_deep_check(info, target_size=target_size, problem_files=True, settings=settings)
            last_result = result
            persisted = self._persist_deep_check_and_verification(
                info,
                result,
                verification_source='post-embed verification' if attempt == 0 else f'post-embed verification retry {attempt + 1}',
                problem_files=True,
                expected_dimensions=expected_dimensions,
            )
            last_persisted = persisted
            if persisted.get('ok'):
                return persisted
        return last_persisted or {'ok': False, 'summary': 'Verification did not complete.', 'deep_file_check': {}, 'problem_files': []}

    def _verification_lines(self, album):
        notes = (album or {}).get('notes_json') or {}
        if not isinstance(notes, dict):
            return []
        lines = []
        verification = notes.get('last_verification') or {}
        if isinstance(verification, dict) and verification:
            prefix = 'Verified' if verification.get('ok') else 'Verify failed'
            summary = verification.get('summary') or ''
            checked_at = verification.get('checked_at') or ''
            source = verification.get('source') or ''
            line = prefix
            if summary:
                line += f': {summary}'
            if checked_at:
                line += f' · {checked_at}'
            if source:
                line += f' · {source}'
            lines.append(line)
        read = notes.get('current_artwork_disk_read') or {}
        if isinstance(read, dict) and read:
            dims = read.get('dimensions') or read.get('status') or ''
            when = read.get('checked_at') or ''
            src = read.get('source') or 'disk read'
            line = f'Current artwork read from disk: {dims}' if dims else 'Current artwork read from disk'
            if when:
                line += f' · {when}'
            if src:
                line += f' · {src}'
            lines.append(line)
        return lines

    def _problem_file_detail_lines(self, album, limit=8):
        notes = (album or {}).get('notes_json') or {}
        if not isinstance(notes, dict):
            return []
        problem = notes.get('last_problem_files') or {}
        rows = problem.get('rows') if isinstance(problem, dict) else None
        if not rows:
            verification = notes.get('last_verification') or {}
            rows = verification.get('problem_files') if isinstance(verification, dict) else []
        rows = list(rows or [])
        if not rows:
            return []
        lines = []
        for row in rows[:int(limit or 8)]:
            dims = f' · {row.get("dimensions")}' if isinstance(row, dict) and row.get('dimensions') else ''
            issues = ', '.join(row.get('issues') or []) if isinstance(row, dict) else str(row)
            name = row.get('file') if isinstance(row, dict) else ''
            lines.append(f'{name}{dims} — {issues}'.strip(' —'))
        extra = len(rows) - len(lines)
        if extra > 0:
            lines.append(f'…and {extra} more. Use Actions → Show Problem Files for the full list.')
        return lines

    def audit_selected_album(self):
        info = self.active_album_info()
        if not info or not info.get('album_path'):
            return
        if self._block_if_write_action_active('Audit Album'):
            return
        self._set_current_operation('auditing', 'Auditing selected album artwork…')
        self._set_action_result('Auditing cover.jpg and embedded artwork for every supported file…')
        self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=True)
        info = dict(info)

        def work(info=info):
            try:
                settings = load_settings()
                target = get_preferred_artwork_size(settings)
                result = self._run_album_deep_check(info, target_size=target, problem_files=True, settings=settings)
                verification = self._persist_deep_check_and_verification(info, result, verification_source='album audit', problem_files=True)
                folder_status = None
                album_path = info.get('album_path') or ''
                if os.path.isdir(album_path):
                    try:
                        folder_status = _album_folder_cover_status(album_path, target)
                    except Exception as exc:
                        folder_status = {'ok': False, 'issue': f'folder cover check failed: {exc}', 'path': ''}
                else:
                    folder_status = {'ok': False, 'issue': 'album folder unavailable on Mac', 'path': ''}
                self.q.put(('AUDIT_DONE', {'info': info, 'result': result, 'verification': verification, 'folder_status': folder_status}))
            except Exception as exc:
                self.q.put(('AUDIT_ERROR', {'info': info, 'error': str(exc)}))

        threading.Thread(target=work, daemon=True).start()

    def _show_album_audit_window(self, payload):
        info = payload.get('info') or {}
        result = payload.get('result') or {}
        verification = payload.get('verification') or {}
        folder_status = payload.get('folder_status') or {}
        deep = result.get('deep_file_check') or {}
        rows = result.get('problem_files') or []
        target = deep.get('target_size') or get_preferred_artwork_size(self.settings)
        title = f'{info.get("artist", "")} — {info.get("album", "")}'
        win = tk.Toplevel(self.root)
        win.title('Album Audit')
        win.geometry('820x560')
        win.minsize(620, 420)
        body = ttk.Frame(win, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Album Audit', font=('TkDefaultFont', 16, 'bold')).pack(anchor='w')
        ttk.Label(body, text=title, foreground='#555555', wraplength=780).pack(anchor='w', pady=(2, 8))
        text = tk.Text(body, wrap='word', relief='solid', bd=1, padx=8, pady=8)
        text.pack(fill='both', expand=True)
        text.tag_configure('heading', font=('TkDefaultFont', 12, 'bold'))
        text.insert('end', 'Folder\n', 'heading')
        text.insert('end', f'{info.get("album_path") or ""}\n\n')
        text.insert('end', 'cover.jpg / folder artwork\n', 'heading')
        if folder_status.get('path'):
            dims = folder_status.get('dimensions') or ''
            dims_txt = f' · {dims[0]}×{dims[1]}' if isinstance(dims, (list, tuple)) and len(dims) >= 2 else ''
            ok_txt = 'OK' if folder_status.get('ok') else 'Needs attention'
            issue = folder_status.get('issue') or ''
            text.insert('end', f'{ok_txt}: {folder_status.get("path")}{dims_txt}' + (f' · {issue}' if issue else '') + '\n\n')
        else:
            text.insert('end', f"Needs attention: {folder_status.get('issue') or 'cover.jpg missing'}\n\n")
        text.insert('end', f'Embedded artwork verification\n', 'heading')
        text.insert('end', f'{verification.get("summary") or self._deep_check_summary_text(deep)}\n')
        text.insert('end', f'Target: {target}px · square · baseline JPEG\n')
        source = deep.get('source') or result.get('remote_worker') and 'nas-worker' or 'mac-local'
        text.insert('end', f'Source: {source}\n\n')
        if rows:
            text.insert('end', f'Problem files ({len(rows)})\n', 'heading')
            for row in rows:
                dims = f' · {row.get("dimensions")}' if row.get('dimensions') else ''
                issues = ', '.join(row.get('issues') or [])
                text.insert('end', f'• {row.get("file")}{dims}\n  {issues}\n')
        else:
            text.insert('end', 'Problem files\n', 'heading')
            text.insert('end', 'None found. All checked files passed.\n')
        text.configure(state='disabled')
        btns = ttk.Frame(body)
        btns.pack(fill='x', pady=(8, 0))
        ttk.Button(btns, text='Show Problem Files…', command=lambda: (win.destroy(), self.show_problem_files())).pack(side='left')
        ttk.Button(btns, text='Close', command=win.destroy).pack(side='right')

    def _apply_remote_deep_check_result(self, info, result):
        """Persist Deep Check facts returned by the NAS worker."""
        key = (info or {}).get('album_key') or ''
        if not key:
            return ''
        deep = result.get('deep_file_check') or {}
        if not isinstance(deep, dict):
            deep = {}
        width = deep.get('example_width') or deep.get('min_width')
        height = deep.get('example_height') or deep.get('min_height')
        example = deep.get('first_issue_file') or deep.get('example_file') or ''
        try:
            width = int(width) if width not in (None, '') else None
            height = int(height) if height not in (None, '') else None
        except Exception:
            width = height = None
        try:
            note_updates = {'deep_file_check': deep}
            rows = list((result or {}).get('problem_files') or []) if isinstance(result, dict) else []
            if rows:
                note_updates['last_problem_files'] = {
                    'checked_at': deep.get('checked_at') or db.now(),
                    'target_size': deep.get('target_size') or get_preferred_artwork_size(self.settings),
                    'rows': rows[:50],
                    'problem_count': len(rows),
                    'source': deep.get('source') or 'nas-worker',
                }
                note_updates['last_verification'] = {
                    'ok': False,
                    'summary': self._deep_check_summary_text(deep),
                    'checked_files': deep.get('checked_files') or 0,
                    'target_size': deep.get('target_size') or get_preferred_artwork_size(self.settings),
                    'checked_at': deep.get('checked_at') or db.now(),
                    'source': deep.get('source') or 'nas-worker',
                    'problem_count': len(rows),
                    'problem_files': rows[:50],
                }
            db.update_album_notes(key, note_updates)
            db.update_album_path(key, info.get('album_path') or '', example_file=example or None, width=width, height=height)
            state = db.evaluate_and_set_album_state(
                key,
                target_size=get_preferred_artwork_size(self.settings),
                preserve_user_terminal=False,
                settings=self.settings,
            )
            db.update_album_notes(key, {'state_evaluation': state})
            self._clear_current_art_cache(key, remove_files=True)
        except Exception:
            pass
        return key

    def deep_rescan_selected_album(self):
        """Force a full per-track artwork check for the selected album."""
        info = self.active_album_info()
        if not info or not info.get('album_path'):
            return
        album_path = info.get('album_path')
        target = get_preferred_artwork_size(self.settings)
        if worker_enabled_for_path(album_path, self.settings):
            self.status_var.set('NAS worker deep-checking selected album…')
            self._set_action_result('NAS worker is checking every supported music file in the selected album…')
            try:
                result = deep_check_album_remote(album_path, target_size=target, settings=self.settings)
                key = self._apply_remote_deep_check_result(info, result) or info.get('album_key')
            except Exception as exc:
                messagebox.showerror('NAS worker deep check failed', str(exc))
                self.status_var.set('NAS worker deep check failed.')
                self._set_action_result(f'NAS worker deep check failed: {exc}')
                return
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            if key and db.get_album(key):
                self.load_album_for_review(key)
            else:
                self.load_saved_queue(silent=True)
            worker_sec = result.get('remote_worker_duration_seconds')
            timing_txt = f' in {float(worker_sec):.1f}s' if worker_sec not in (None, '') else ''
            msg = f'NAS deep check complete{timing_txt}: {info.get("artist", "")} - {info.get("album", "")}. Every supported music file was checked on the NAS.'
            self.status_var.set(msg)
            self._set_action_result(msg)
            self.refresh_queue_tab()
            self.refresh_footer()
            return
        if not os.path.isdir(album_path):
            messagebox.showwarning('Deep rescan unavailable', 'The selected album folder could not be found. Use Locate Album Folder first.')
            return
        self.status_var.set('Deep rescanning selected album…')
        self._set_action_result('Deep rescanning every supported music file in the selected album…')
        key = self._rescan_album_path(album_path, force_deep_check=True) or info.get('album_key')
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        if key and db.get_album(key):
            self.load_album_for_review(key)
        else:
            self.load_saved_queue(silent=True)
        msg = f'Deep rescan complete: {info.get("artist", "")} - {info.get("album", "")}. Every supported music file was checked.'
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.refresh_queue_tab()
        self.refresh_footer()

    def show_problem_files(self):
        """Show the exact tracks currently failing Deep Check rules."""
        info = self.active_album_info()
        if not info or not info.get('album_path'):
            return
        album_path = info.get('album_path')
        try:
            target = get_preferred_artwork_size(self.settings)
            if worker_enabled_for_path(album_path, self.settings):
                result = deep_check_album_remote(album_path, target_size=target, settings=self.settings, problem_files=True)
                rows = result.get('problem_files') or []
                try:
                    self._apply_remote_deep_check_result(info, result)
                except Exception:
                    pass
            else:
                if not os.path.isdir(album_path):
                    messagebox.showwarning('Problem files unavailable', 'The selected album folder could not be found. Use Locate Album Folder first.')
                    return
                result = self._local_deep_check_result(album_path, target, problem_files=True)
                rows = result.get('problem_files') or []
                try:
                    self._persist_deep_check_and_verification(info, result, verification_source='show problem files', problem_files=True)
                except Exception:
                    pass
        except Exception as exc:
            messagebox.showerror('Problem files failed', str(exc))
            return
        win = tk.Toplevel(self.root)
        win.title('Problem Files')
        win.geometry('760x520')
        win.minsize(560, 360)
        body = ttk.Frame(win, padding=12)
        body.pack(fill='both', expand=True)
        title = f'{info.get("artist", "")} — {info.get("album", "")}'
        ttk.Label(body, text='Problem Files', font=('TkDefaultFont', 16, 'bold')).pack(anchor='w')
        ttk.Label(body, text=title, foreground='#555555', wraplength=720).pack(anchor='w', pady=(2, 8))
        text = tk.Text(body, wrap='word', relief='solid', bd=1, padx=8, pady=8)
        text.pack(fill='both', expand=True)
        text.tag_configure('heading', font=('TkDefaultFont', 12, 'bold'))
        text.insert('end', 'Folder\n', 'heading')
        text.insert('end', f'{album_path}\n\n')
        text.insert('end', f'Target: {target}px · square · baseline JPEG\n\n', 'heading')
        if not rows:
            text.insert('end', 'No problem files found by the on-demand deep check.\n')
        else:
            text.insert('end', f'{len(rows)} problem file(s) found:\n\n', 'heading')
            for row in rows:
                dims = f' · {row.get("dimensions")}' if row.get('dimensions') else ''
                issues = ', '.join(row.get('issues') or [])
                text.insert('end', f'• {row.get("file")}{dims}\n  {issues}\n')
        text.configure(state='disabled')
        btns = ttk.Frame(body)
        btns.pack(fill='x', pady=(8, 0))
        ttk.Button(btns, text='Deep Rescan Selected', command=lambda: (win.destroy(), self.deep_rescan_selected_album())).pack(side='left')
        ttk.Button(btns, text='Close', command=win.destroy).pack(side='right')


    def rescan_artist_folder(self):
        info = self.active_album_info()
        if not info or not info.get('album_path'):
            return
        artist_folder = str(Path(info['album_path']).parent)
        count = 0
        for folder in self._album_folders_under(artist_folder):
            self._rescan_album_path(folder)
            count += 1
        self.load_saved_queue(silent=True)
        self.status_var.set(f'Rescanned {count} album folder(s) under the selected artist folder.')

    def rescan_active_queue(self):
        albums = db.load_albums(actionable_only=True)
        count = 0
        for album in albums:
            if album.get('album_path'):
                self._rescan_album_path(album.get('album_path'))
                count += 1
        self.load_saved_queue(silent=True)
        self.status_var.set(f'Rescanned {count} active queue album(s).')

    def google(self):
        info = self.active_album_info()
        if info:
            # Use the scan/search identity where available, not just the display
            # artist/album. This avoids folder/container names such as “Music”
            # leaking into the Google Images query when metadata was inferred
            # from the library path.
            album_rec = db.get_album(info.get('album_key')) if info.get('album_key') else None
            artist = (album_rec or {}).get('search_artist') or (album_rec or {}).get('artist') or info.get('artist') or ''
            album = (album_rec or {}).get('search_album') or (album_rec or {}).get('album') or info.get('album') or ''
            webbrowser.open(google_images_url(artist, album))

    def manual(self):
        info = self.active_album_info()
        if not info:
            return
        p = filedialog.askopenfilename(title='Choose manual artwork image', filetypes=[('Images', '*.jpg *.jpeg *.png *.webp'), ('All files', '*.*')])
        if not p:
            return
        try:
            if self.active_find_album_key == info['album_key']:
                self._cancel_active_artwork_search(
                    log_message='\nManual import stopped the active search for this album.\n',
                    update_controls=False,
                )
            cand = manual_import(p, info['artist'], info['album'], info['album_key'], info['album_path'])
            db.set_album_status(info['album_key'], 'candidate_found')
            db.update_album_notes(info['album_key'], {'state_evaluation': {'status': 'candidate_found', 'reason': 'manual artwork option imported'}})
            self._queue_consistency_check(info['album_key'], repair=True, context='manual import')
            self.log_msg(f'\nImported artwork option: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.\n')
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            self.current_album_key = cand['album_key']
            self.candidate_index = len(self.groups.get(cand['album_key'], [])) - 1
            self._pin_album_in_current_filter(cand['album_key'], reason='manual artwork import')
            self.show_current_album()
            self.refresh_queue_tab()
            self.refresh_footer()
        except Exception as exc:
            messagebox.showerror('Import failed', str(exc))

    def _album_to_search_info(self, album):
        if not album:
            return None
        return {
            'artist': album.get('artist', ''),
            'album': album.get('album', ''),
            'album_key': album.get('album_key'),
            'album_path': album.get('album_path') or album.get('album_folder'),
            'search_artist': album.get('search_artist') or album.get('artist', ''),
            'search_album': album.get('search_album') or album.get('album', ''),
            'year': album.get('year') or '',
            'mb_release_id': album.get('mb_release_id') or '',
            'mb_releasegroup_id': album.get('mb_releasegroup_id') or '',
            'identity_confidence': album.get('identity_confidence') or '',
        }

    def _start_artwork_search(self, infos, *, mode='single', total_limit=None):
        if self._block_if_action_transition('Find Artwork'):
            return
        infos = [i for i in infos if i and i.get('album_key') and i.get('album_path')]
        if not infos:
            return
        if self.is_artwork_search_active():
            self.status_var.set('Artwork search is already running. Press Stop Search before starting another one.')
            return
        self.find_job_counter += 1
        job_id = self.find_job_counter
        stop_event = threading.Event()
        self.find_stop_event = stop_event
        self.active_find_job_id = job_id
        self.active_find_album_key = infos[0]['album_key'] if mode in ('single', 'more') else 'BATCH'
        self.active_find_mode = mode
        original_buckets = {}
        for info in infos:
            key = info.get('album_key')
            if not key:
                continue
            try:
                album = db.get_album(key) or {}
                evaluated_status, _reason = self._evaluate_queue_album(album)
                original_buckets[key] = workflow_bucket_for_status(evaluated_status)
            except Exception:
                original_buckets[key] = ''
        self.active_search_album_keys = {i['album_key'] for i in infos if i.get('album_key')}
        self.active_search_album_original_buckets = original_buckets
        # Make Search Next visible immediately in the queue: the first album is
        # actively searching, and the rest are explicitly marked as queued for
        # search until provider progress reaches each row.
        self.active_search_album_status = {}
        total_searching = len(infos)
        for pos, i in enumerate(infos):
            key = i.get('album_key')
            if key:
                if mode == 'batch' and total_searching > 1:
                    self.active_search_album_status[key] = 'Searching 1/%d' % total_searching if pos == 0 else 'Search Queued'
                else:
                    self.active_search_album_status[key] = 'Searching…'
        self.active_search_album_labels = {i['album_key']: f'{i.get("artist", "")} - {i.get("album", "")}' for i in infos if i.get('album_key')}
        self.active_search_batch_total = len(infos) if mode == 'batch' else 0
        if self.active_search_batch_total:
            self._set_action_result(self._batch_search_progress_text())
        self.set_status_dot('#007aff')
        if mode in ('single', 'more'):
            self._pin_album_in_current_filter(infos[0].get('album_key'), reason='artwork search in progress')
        self.set_review_button_states(has_candidate=bool(self.current_candidate()), has_album=bool(self.active_album_info()))
        self.refresh_queue_tab()
        self.refresh_review_header()
        fetch_min = get_fetch_min_artwork_size()
        default_cap = get_max_candidates_per_album()
        limit = int(total_limit or default_cap)

        if mode == 'batch':
            self.log_msg(f'\nSearch started: {len(infos)} album(s), up to {limit} option(s) each.\n')
            self.log_verbose('  Albums:\n' + '\n'.join(f'    {i+1}. {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}' for i, info in enumerate(infos)) + '\n')
            self.status_var.set(f'Searching {len(infos)} album(s)…')
        elif mode == 'more':
            info = infos[0]
            self.log_msg(f'\nSearch More started: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.\n')
            self.log_verbose(f'  Target: up to {limit} total option(s) at {fetch_min}px+.\n')
            self.log_verbose(f'  Search identity → artist="{info["search_artist"]}" | album="{info["search_album"]}" | year="{info["year"]}" | confidence={info["identity_confidence"] or "unknown"}\n')
            self.status_var.set('Searching for more artwork options…')
        else:
            info = infos[0]
            self.log_msg(f'\nSearch started: {self._workflow_album_label(info.get("artist", ""), info.get("album", ""))}.\n')
            self.log_verbose(f'  Target: up to {limit} option(s) at {fetch_min}px+.\n')
            self.log_verbose(f'  Search identity → artist="{info["search_artist"]}" | album="{info["search_album"]}" | year="{info["year"]}" | confidence={info["identity_confidence"] or "unknown"}\n')
            self.status_var.set('Finding artwork for the selected album…')

        def work(job_id=job_id, stop_event=stop_event, infos=infos, mode=mode, total_limit=limit):
            try:
                before = {i['album_key']: len(db.load_candidates_for_album(i['album_key'], include_rejected=False)) for i in infos}
                build_candidates(
                    infos,
                    max_per_album=total_limit,
                    include_fallbacks=True,
                    stop_event=stop_event,
                    log=lambda s: self.q.put(('LOG', s + '\n')),
                    status=lambda s: self.q.put(('PARTIAL_STATUS', {'job_id': job_id, 'text': s})),
                    on_candidate=lambda cand: self.q.put(('CAND', {'job_id': job_id, 'candidate': cand, 'silent': mode == 'batch'})),
                )
                after = {i['album_key']: len(db.load_candidates_for_album(i['album_key'], include_rejected=False)) for i in infos}
                count = sum(max(0, after.get(k, 0) - before.get(k, 0)) for k in before)
                status_counts = {}
                for i in infos:
                    album = db.get_album(i.get('album_key')) or {}
                    st = album.get('status') or 'unknown'
                    status_counts[st] = status_counts.get(st, 0) + 1
                self.q.put(('FIND_DONE', {
                    'job_id': job_id,
                    'album_key': infos[0]['album_key'] if mode in ('single', 'more') else (self.current_album_key or infos[0]['album_key']),
                    'album_keys': [i.get('album_key') for i in infos if i.get('album_key')],
                    'status_counts': status_counts,
                    'count': count,
                    'album_count': len(infos),
                    'mode': mode,
                    'target_total': total_limit,
                    'stopped': stop_event.is_set(),
                }))
            except Exception as exc:
                self.q.put(('FIND_ERROR', {'job_id': job_id, 'error': str(exc), 'stopped': stop_event.is_set()}))

        self.find_worker = threading.Thread(target=work, daemon=True)
        self.find_worker.start()


    def _reopened_status_for_album(self, album):
        if not album:
            return 'needs_review'
        try:
            status, _reason = evaluate_album_record(
                album,
                candidate_count=0,
                preserve_user_terminal=False,
                settings=getattr(self, 'settings', None),
            )
            if status in ('missing_artwork', 'needs_review', 'not_square_artwork', 'incompatible_artwork'):
                return status
        except Exception:
            pass
        try:
            if int(album.get('width') or 0) <= 0 or int(album.get('height') or 0) <= 0:
                return 'missing_artwork'
        except Exception:
            return 'needs_review'
        return 'needs_review'

    def _prepare_finalized_album_for_new_search(self, album_key, *, clear_old_candidates=True):
        """Reopen an approved/handled album so Find Artwork can search again.

        Approved/skipped/good/ignored albums are normally finalized so late
        background search results cannot change them. If the user deliberately
        presses Find Artwork on one of those albums, treat that as a fresh
        review cycle: trash app-managed candidate files, drop stale candidate
        rows whose files may already have been trashed, clear the finalized
        status, and let the provider search save new options.
        """
        album = db.get_album(album_key) or {}
        if album.get('status') not in ('approved', 'reviewed_skipped', 'already_good', 'ignored'):
            return False
        removed_files = self._remove_temporary_artwork_for_album(album_key) if clear_old_candidates else 0
        removed_rows = 0
        if clear_old_candidates:
            try:
                removed_rows = db.delete_candidates_for_album(album_key)
            except Exception:
                removed_rows = 0
        try:
            reopened_status = self._reopened_status_for_album(album)
            db.set_album_status(album_key, reopened_status)
            db.update_album_notes(album_key, {'reworked_at': db.now(), 'reworked_reason': 'Fresh artwork search requested'})
        except Exception:
            pass
        cleanup_bits = []
        if removed_rows:
            cleanup_bits.append(f'cleared {removed_rows} old option row(s)')
        if removed_files:
            cleanup_bits.append(f'trashed {removed_files} temporary/import file(s)')
        suffix = '; ' + ', '.join(cleanup_bits) if cleanup_bits else ''
        self.log_verbose(f'  Reopened handled album for a fresh artwork search{suffix}.\n')
        return True

    def rework_album(self):
        """Explicitly return a handled album to the Needs Attention workflow."""
        info = self.active_album_info()
        if not info:
            messagebox.showinfo('Rework Album', 'Select an album first.')
            return
        if self.is_artwork_search_active():
            self.status_var.set('Artwork search is already running. Press Stop Search before reworking an album.')
            return
        album_key = info.get('album_key')
        album = db.get_album(album_key) or {}
        status = album.get('status') or ''
        if status not in ('approved', 'reviewed_skipped', 'already_good', 'ignored', 'no_candidate', 'candidate_found'):
            self.status_var.set('This album is already in the work queue. Use Find Artwork or Search Again from Scratch if needed.')
            return
        if not messagebox.askyesno(
            'Rework album?',
            'Return this album to Needs Attention and clear saved candidate options?\n\n'
            'This does not remove embedded artwork from the music files.'
        ):
            return
        candidates = db.load_candidates_for_album(album_key, include_rejected=True)
        removed_files = self._remove_candidate_files(candidates)
        try:
            removed_rows = db.delete_candidates_for_album(album_key)
        except Exception:
            removed_rows = 0
        new_status = self._reopened_status_for_album(album)
        db.set_album_status(album_key, new_status)
        db.update_album_notes(album_key, {'reworked_at': db.now(), 'reworked_reason': 'Manual Rework Album action'})
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.load_album_for_review(album_key)
        self.refresh_queue_tab()
        self.refresh_footer()
        self.log_msg(f'\nRework Album: {info.get("artist", "")} - {info.get("album", "")} returned to Needs Attention. Cleared {removed_rows} option row(s); trashed {removed_files} temporary/import file(s).\n')
        self.status_var.set('Album returned to Needs Attention. Press Find Artwork to search again, or use Choose Release / Import Image.')

    def find_more(self):
        active = self.active_album_info()
        if not active:
            messagebox.showinfo('Find artwork', 'Select an album from the queue first.')
            return
        album_key = active.get('album_key')
        reopened = self._prepare_finalized_album_for_new_search(album_key, clear_old_candidates=True)
        album_rec = db.get_album(album_key) or {}
        info = self._album_to_search_info({**active, **album_rec, 'album_path': active.get('album_path') or album_rec.get('album_path')})
        if reopened:
            self.status_var.set('Reopened approved/handled album and starting a fresh artwork search…')
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            self.refresh_queue_tab()
            self.refresh_review_header()
        self._start_artwork_search([info], mode='single', total_limit=get_max_candidates_per_album())

    def search_more(self):
        active = self.active_album_info()
        if not active:
            messagebox.showinfo('Search more artwork', 'Select an album from the queue first.')
            return
        album_key = active.get('album_key')
        reopened = self._prepare_finalized_album_for_new_search(album_key, clear_old_candidates=True)
        album_rec = db.get_album(album_key) or {}
        info = self._album_to_search_info({**active, **album_rec, 'album_path': active.get('album_path') or album_rec.get('album_path')})
        existing = 0 if reopened else len(db.load_candidates_for_album(info['album_key'], include_rejected=False))
        extra = get_max_candidates_per_album()
        if reopened:
            self.status_var.set('Reopened approved/handled album and starting a fresh artwork search…')
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            self.refresh_queue_tab()
            self.refresh_review_header()
        self._start_artwork_search([info], mode='more' if not reopened else 'single', total_limit=existing + extra)

    def _visible_queue_albums_in_order(self):
        albums = []
        if not hasattr(self, 'queue_tree'):
            return albums
        for iid in self.queue_tree.get_children():
            key = self.queue_album_keys.get(iid) if hasattr(self, 'queue_album_keys') else None
            if not key:
                continue
            album = db.get_album(key)
            if album:
                albums.append(album)
        return albums

    def find_next_five(self):
        if self.is_artwork_search_active():
            self.status_var.set('Artwork search is already running. Press Stop Search before starting another one.')
            return
        # Follow the visible queue order, including current filter and sort. This
        # matches what the user sees on screen instead of using hidden database order.
        albums = self._visible_queue_albums_in_order() or db.load_albums(actionable_only=True)
        current = self.current_album_key
        ordered = list(albums)
        after_label = ''
        if current:
            all_keys = [a.get('album_key') for a in ordered]
            if current in all_keys:
                # Search Next should start with the selected album, not the row
                # after it. This keeps a batch like "Search Next 5" aligned
                # with what the user is looking at: selected album + next 4.
                start = all_keys.index(current)
                ordered = ordered[start:] + ordered[:start]
                current_album = db.get_album(current) or self.current_album_info or {}
                after_label = f' starting at {current_album.get("artist", "")} — {current_album.get("album", "")}'
        def should_batch_search(album):
            try:
                bucket = self.queue_workflow_bucket(album)
            except Exception:
                bucket = workflow_bucket_for_status(album.get('status') or '')
            return bucket in {'Needs Search', 'Missing'}
        needs = [a for a in ordered if should_batch_search(a)]
        batch_count = get_batch_search_count()
        batch = needs[:batch_count]
        if not batch:
            self.status_var.set('No Needs Search / Missing albums are waiting for batch search in the visible queue.')
            return
        preview = '\n'.join(f'{i+1}. {a.get("artist", "")} — {a.get("album", "")}' for i, a in enumerate(batch))
        self.log_verbose(f'\nSearch Next {len(batch)} starting{after_label}:\n{preview}\n')
        if not messagebox.askyesno('Search next albums?', f'Search the next {len(batch)} album(s)?\n\n{preview}'):
            self.status_var.set('Search Next cancelled.')
            return
        first = batch[0]
        self.status_var.set(f'Search Next {batch_count} starting{after_label}. First: {first.get("artist", "")} — {first.get("album", "")}')
        infos = [self._album_to_search_info(a) for a in batch]
        self._start_artwork_search(infos, mode='batch', total_limit=get_max_candidates_per_album())


    def preview_embedded_artwork(self):
        """Open a Quick Look-style preview of the currently embedded artwork."""
        info = self.active_album_info()
        if not info:
            return
        try:
            if not getattr(self, 'current_old_art_info', None) or info.get('album_key') != getattr(self, 'current_album_key', None):
                self.current_old_art_info = self.current_art_info(info)
        except Exception:
            pass
        if not getattr(self, 'current_old_art_info', None):
            messagebox.showinfo('Embedded artwork', 'No embedded artwork was found for the selected album.')
            return
        self.enlarge_current()

    def convert_embedded_artwork_to_baseline(self):
        """Convert current embedded artwork or save it as album-folder cover.jpg."""
        selected = self.selected_queue_albums()
        if len(selected) > 1:
            return self.convert_save_selected_embedded_artwork()
        if self._block_if_write_action_active('Convert/Save'):
            return
        album = self.active_album_info()
        if not album:
            return
        self._start_convert_embedded_artwork_for_album(album, confirm=True, batch=False)

    def _begin_convert_save_batch(self, albums, title='Convert/save albums?', source_label='selected'):
        """Start a one-at-a-time Convert/Save batch for the supplied album rows."""
        # De-duplicate by album key while preserving the visible/selected order.
        deduped = []
        seen = set()
        for album in albums or []:
            key = album.get('album_key') if album else None
            if key and key not in seen:
                seen.add(key)
                deduped.append(album)
        skipped_non_convert = 0
        if source_label in {'visible', 'selected', 'next visible'}:
            before = len(deduped)
            deduped = [a for a in deduped if self._album_needs_convert_save_work(a)]
            skipped_non_convert = before - len(deduped)
        albums = deduped
        if not albums:
            self.status_var.set(f'No {source_label} Not Square/Convert albums for Convert/Save.')
            self._set_action_result(f'Convert/Save: no {source_label} Not Square/Convert albums.')
            return
        preview = '\n'.join(f'{i+1}. {a.get("artist", "")} — {a.get("album", "")}' for i, a in enumerate(albums[:12]))
        more = '' if len(albums) <= 12 else f'\n…plus {len(albums) - 12} more'
        filter_name = self.filter_var.get() if hasattr(self, 'filter_var') else 'current filter'
        extra = ''
        if source_label == 'visible':
            extra = f'\n\nCurrent filter: {filter_name}\nOnly Not Square/Convert albums currently visible in the queue will be processed.'
        if skipped_non_convert:
            extra += f'\n\nSkipped {skipped_non_convert} selected/visible album(s) that do not need Convert/Save.'
        if not messagebox.askyesno(title, f'Convert/save embedded artwork for {len(albums)} {source_label} album(s), one at a time?{extra}\n\n{preview}{more}'):
            self.status_var.set('Convert/Save batch cancelled.')
            return
        self.convert_batch_active = True
        self._set_current_operation('batch_converting', f'Batch Convert/Save starting for {len(albums)} album(s)…')
        self.convert_batch_source_label = source_label
        self.convert_batch_filter_name = filter_name
        self.convert_batch_stop_after_current = False
        self.convert_batch_queue = list(albums)
        self.convert_batch_total = len(albums)
        self.convert_batch_done = 0
        self.convert_batch_started = 0
        self.convert_batch_good = 0
        self.convert_batch_needs = 0
        self.convert_batch_skipped = 0
        self.convert_batch_failed = 0
        self.log_msg(f'\nBatch Convert/Save started: {len(albums)} {source_label} album(s).\n')
        self.log_verbose(f'  Albums:\n{preview}{more}\n')
        self._set_action_result(f'Batch Convert/Save: 0/{len(albums)} complete.')
        self._continue_convert_save_batch()

    def convert_save_visible_embedded_artwork(self):
        """Convert/save every album currently visible in the queue filter/search."""
        if self._block_if_write_action_active('Batch Convert/Save'):
            return
        albums = self.visible_queue_albums()
        if not albums:
            self.status_var.set('No visible queue albums for Convert/Save.')
            return
        return self._begin_convert_save_batch(albums, title='Convert/save all visible albums?', source_label='visible')

    def convert_save_next_visible_embedded_artwork(self):
        """Convert/save the current visible Needs Convert album, then the following visible ones."""
        if self._block_if_write_action_active('Convert/Save Next'):
            return
        visible = self.visible_queue_albums()
        if not visible:
            self.status_var.set('No visible queue albums for Convert/Save Next.')
            return
        current_key = getattr(self, 'current_album_key', None)
        start_index = 0
        if current_key:
            for idx, album in enumerate(visible):
                if album.get('album_key') == current_key:
                    start_index = idx
                    break
        albums = [a for a in visible[start_index:] if self._album_needs_convert_save_work(a)]
        if not albums:
            self.status_var.set('No Not Square/Convert albums from the current row onward.')
            self._set_action_result('Convert/Save Next: no Not Square/Convert albums from the current row onward.')
            return
        return self._begin_convert_save_batch(albums, title='Convert/save from current album onward?', source_label='next visible')

    def convert_save_selected_embedded_artwork(self):
        """Convert/save embedded artwork for selected queue albums, one after another."""
        if self._block_if_write_action_active('Batch Convert/Save'):
            return
        albums = self.selected_queue_albums()
        if not albums:
            self.status_var.set('No queue albums selected for Convert/Save.')
            return
        return self._begin_convert_save_batch(albums, title='Convert/save selected albums?', source_label='selected')

    def _continue_convert_save_batch(self):
        if not getattr(self, 'convert_batch_active', False):
            return
        if getattr(self, 'convert_batch_stop_after_current', False):
            remaining = len(getattr(self, 'convert_batch_queue', []) or [])
            self.convert_batch_active = False
            self.convert_batch_stop_after_current = False
            self._clear_current_operation('batch_converting')
            msg = f'Batch Convert/Save stopped after current album. {remaining} album(s) left unprocessed.'
            self.status_var.set(msg)
            self._set_action_result(msg)
            self.log_msg(msg + '\n')
            try:
                self.refresh_queue_tab(preserve_selection=True)
            except TypeError:
                self.refresh_queue_tab()
            return
        queue = getattr(self, 'convert_batch_queue', []) or []
        while queue:
            album = queue.pop(0)
            self.convert_batch_queue = queue
            self.convert_batch_started = getattr(self, 'convert_batch_started', 0) + 1
            started = self._start_convert_embedded_artwork_for_album(album, confirm=False, batch=True)
            if started:
                return
            self.convert_batch_done = getattr(self, 'convert_batch_done', 0) + 1
            self.convert_batch_skipped = getattr(self, 'convert_batch_skipped', 0) + 1
            self._set_action_result(f'Batch Convert/Save: {self.convert_batch_done}/{self.convert_batch_total} complete · {self.convert_batch_skipped} skipped.')
        total = getattr(self, 'convert_batch_total', 0)
        good = getattr(self, 'convert_batch_good', 0)
        needs = getattr(self, 'convert_batch_needs', 0)
        skipped = getattr(self, 'convert_batch_skipped', 0)
        failed = getattr(self, 'convert_batch_failed', 0)
        self.convert_batch_active = False
        self.convert_batch_stop_after_current = False
        self._clear_current_operation('batch_converting')
        msg = f'Batch Convert/Save complete: {total} album(s) processed · {good} good · {needs} still need attention'
        if skipped:
            msg += f' · {skipped} skipped'
        if failed:
            msg += f' · {failed} warning(s)'
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.log_msg(f'{msg}\n')
        try:
            self.refresh_queue_tab(preserve_selection=True)
        except TypeError:
            self.refresh_queue_tab()

    def _start_convert_embedded_artwork_for_album(self, album, confirm=True, batch=False):
        """Start a convert/save job for a specific album. Returns True when queued."""
        if not album:
            return False
        album_key = album.get('album_key')
        album_folder = album.get('album_path') or album.get('album_folder')
        artist = album.get('artist') or ''
        album_name = album.get('album') or ''
        display_name = f'{artist} — {album_name}'.strip(' —')
        if not album_folder or not os.path.isdir(album_folder):
            msg = 'Album folder is unavailable. Reconnect the drive/NAS or locate the album folder, then try again.'
            if confirm:
                messagebox.showerror('Album folder unavailable', msg)
            else:
                self.log_verbose(f'  Convert/Save skipped: {display_name}: {msg}\n')
            return False
        source_file = self._current_art_source_file(album)
        if not source_file:
            msg = 'No music file with embedded artwork was found for this album.'
            if confirm:
                messagebox.showinfo('Convert/save embedded artwork', msg)
            else:
                self.log_verbose(f'  Convert/Save skipped: {display_name}: {msg}\n')
            return False
        try:
            arts = embedded_artwork(source_file)
        except Exception as exc:
            arts = []
            msg = f'Could not read embedded artwork: {exc}'
            if confirm:
                messagebox.showerror('Convert/save embedded artwork', msg)
            else:
                self.log_verbose(f'  Convert/Save skipped: {display_name}: {msg}\n')
            return False
        if not arts:
            msg = 'No embedded artwork was found for this album.'
            if confirm:
                messagebox.showinfo('Convert/save embedded artwork', msg)
            else:
                self.log_verbose(f'  Convert/Save skipped: {display_name}: {msg}\n')
            return False
        art = arts[0]
        settings = load_settings()
        target = get_max_embedded_artwork_size(settings)
        original_format = art.get('format') or 'unknown'
        original_issue = art.get('compatibility_issue') or ('already baseline JPEG' if art.get('compatible') else 'not baseline JPEG')
        original_compatible = bool(art.get('compatible'))
        try:
            longest = max(int(art.get('width') or 0), int(art.get('height') or 0))
        except Exception:
            longest = 0
        target_int = int(target or 0)
        needs_resize = bool(target_int > 0 and longest > target_int)
        needs_format_conversion = not original_compatible
        needs_square_conversion = bool(
            target_int > 0
            and int(art.get('width') or 0) > 0
            and int(art.get('height') or 0) > 0
            and int(art.get('width') or 0) != int(art.get('height') or 0)
            and max(int(art.get('width') or 0), int(art.get('height') or 0)) >= int(round(target_int * 0.98))
        )
        needs_embed_conversion = needs_format_conversion or needs_resize or needs_square_conversion
        if needs_format_conversion and needs_resize and needs_square_conversion:
            conversion_reason = f'convert {original_issue}, resize to target, and square artwork'
        elif needs_format_conversion and needs_resize:
            conversion_reason = f'convert {original_issue} and resize to target'
        elif needs_format_conversion and needs_square_conversion:
            conversion_reason = f'convert {original_issue} and square artwork'
        elif needs_resize and needs_square_conversion:
            conversion_reason = 'resize to target and square artwork'
        elif needs_format_conversion:
            conversion_reason = f'convert {original_issue} to baseline JPEG'
        elif needs_resize:
            conversion_reason = 'resize baseline JPEG to target size'
        elif needs_square_conversion:
            conversion_reason = 'square artwork to target canvas'
        else:
            conversion_reason = 'embedded artwork already baseline JPEG at target size'
        folder_copy_enabled = bool(settings.get('save_approved_artwork_to_album_folder', False))
        if not needs_embed_conversion and not folder_copy_enabled:
            msg = 'The embedded artwork is already a target-size baseline JPEG, and album-folder cover saving is not enabled.'
            if confirm:
                messagebox.showinfo('Convert/save embedded artwork', msg)
            else:
                self.log_verbose(f'  Convert/Save skipped: {display_name}: {msg}\n')
            return False
        action_label = 'convert/re-embed the current embedded artwork' if needs_embed_conversion else 'save the current embedded artwork as cover.jpg'
        if folder_copy_enabled and needs_embed_conversion:
            action_label += ' and save cover.jpg'
        if confirm and not messagebox.askyesno(
            'Convert/save embedded artwork?',
            f'This will {action_label}.\n\n'
            f'Current: {art.get("width", "?")}×{art.get("height", "?")} · {original_issue}\n'
            f'Action: {conversion_reason}\n'
            f'Target: {target}px longest side\n\n'
            'If re-embedding is needed, Backup follows the main Backup checkbox.'
        ):
            return False

        fd, tmp_path = tempfile.mkstemp(prefix='embedded_artwork_', suffix='.img')
        os.close(fd)
        Path(tmp_path).write_bytes(art.get('bytes') or b'')
        self.embed_job_counter += 1
        job_id = self.embed_job_counter
        self.active_embed_job_id = job_id
        self.active_embed_album_key = album_key
        if batch:
            total = getattr(self, 'convert_batch_total', 0) or 0
            done = getattr(self, 'convert_batch_done', 0) or 0
            self._set_current_operation('batch_converting', f'Convert/Save {done + 1}/{total} — {display_name}')
        else:
            self._set_current_operation('converting', f'Converting/saving embedded artwork for {display_name}…')
        self.progress_var.set(0)
        self.progress_text.set('Preparing artwork…')

        def progress(done, total, fp):
            self.q.put(('EMBED_PROGRESS', {'job_id': job_id, 'done': done, 'total': total, 'file': fp}))

        def work():
            try:
                use_remote_worker = worker_enabled_for_path(album_folder, settings)
                if use_remote_worker:
                    self.q.put(('EMBED_PROGRESS', {'job_id': job_id, 'done': 0, 'total': 0, 'file': 'NAS worker'}))
                    result = embed_album_remote(
                        album_folder,
                        tmp_path,
                        album_key,
                        artist=artist,
                        album=album_name,
                        backup=self.backup.get(),
                        max_artwork_size=target,
                        make_square=needs_square_conversion,
                        save_folder_cover=folder_copy_enabled,
                        embed=needs_embed_conversion,
                        settings=settings,
                    )
                    result['reembedded'] = bool(needs_embed_conversion)
                    result['export_only'] = not bool(needs_embed_conversion)
                elif needs_embed_conversion:
                    result = embed_album(album_folder, tmp_path, album_key, backup=self.backup.get(), progress=progress, max_artwork_size=target, make_square=needs_square_conversion)
                    result['reembedded'] = True
                else:
                    result = {
                        'updated': 0,
                        'total': 0,
                        'failed': [],
                        'image_width': art.get('width'),
                        'image_height': art.get('height'),
                        'export_only': True,
                        'reembedded': False,
                    }
                result['original_width'] = art.get('width')
                result['original_height'] = art.get('height')
                result['original_format'] = original_format
                result['original_issue'] = original_issue
                result['original_compatible'] = original_compatible
                result['needed_format_conversion'] = needs_format_conversion
                result['needed_resize'] = needs_resize
                result['needed_square_conversion'] = needs_square_conversion
                result['conversion_reason'] = conversion_reason
                album_artwork_copy = result.get('album_artwork_copy') or ''
                if not use_remote_worker:
                    try:
                        if folder_copy_enabled:
                            album_artwork_copy = save_approved_artwork_to_album_folder(
                                tmp_path,
                                artist,
                                album_name,
                                album_folder,
                                max_artwork_size=target,
                                make_square=needs_square_conversion,
                            )
                    except Exception as copy_exc:
                        result.setdefault('failed', []).append({'file': album_folder, 'error': f'Folder cover copy failed: {copy_exc}'})
                result['album_artwork_copy'] = album_artwork_copy
                self.q.put(('CONVERT_EMBEDDED_DONE', {'job_id': job_id, 'album_key': album_key, 'result': result}))
            except Exception as exc:
                self.q.put(('CONVERT_EMBEDDED_DONE', {'job_id': job_id, 'album_key': album_key, 'result': {'failed': [{'file': album_folder, 'error': str(exc)}], 'image_width': '?', 'image_height': '?', 'fatal_error': str(exc)}}))
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()
        return True

    def _finish_convert_embedded_artwork(self, album_key, result):
        w = result.get('image_width') or '?'
        h = result.get('image_height') or '?'
        folder_copy = result.get('album_artwork_copy') or ''
        failed = result.get('failed') or []
        folder_failures = [f for f in failed if 'Folder cover copy failed' in str(f.get('error', ''))]
        embed_failures = [f for f in failed if f not in folder_failures]

        # Always resolve the completed album from the database, not from the
        # current UI selection. The user can select another album while the
        # background conversion/export is running; using active_album_info()
        # here could write the wrong folder path onto the completed album.
        completed_album = db.get_album(album_key) or {}
        album_path = completed_album.get('album_path') or completed_album.get('album_folder') or ''

        settings = load_settings()
        folder_copy_enabled = bool(settings.get('save_approved_artwork_to_album_folder', False))
        target = int(get_max_embedded_artwork_size(settings) or 0)

        # Verify what is embedded now, after the convert/save action. Relying
        # only on the planned result can leave the queue/details stale when the
        # action was an export-only cover.jpg write, or when a partial failure
        # happened. This also lets us move albums out of Needs Convert as soon
        # as the embedded art is confirmed baseline JPEG.
        verified_art = None
        verified_issue = ''
        verified_source_file = ''
        try:
            verified_source_file = self._current_art_source_file(completed_album) or ''
            if verified_source_file:
                arts = embedded_artwork(verified_source_file)
                if arts:
                    verified_art = arts[0]
        except Exception:
            verified_art = None
        if verified_art:
            w = verified_art.get('width') or w
            h = verified_art.get('height') or h
            verified_compatible = bool(verified_art.get('compatible'))
            verified_issue = verified_art.get('compatibility_issue') or ('' if verified_compatible else 'not baseline JPEG')
        else:
            verified_compatible = bool(result.get('original_compatible') and not result.get('needed_format_conversion'))
            verified_issue = 'embedded artwork could not be re-checked'

        try:
            wi, hi = int(w), int(h)
        except Exception:
            wi = hi = 0
        size_ok = artwork_meets_target_size(wi, hi, target)

        folder_status = {'ok': not folder_copy_enabled, 'issue': '', 'path': folder_copy}
        if folder_copy_enabled:
            try:
                folder_status = _album_folder_cover_status(album_path, target)
            except Exception as exc:
                folder_status = {'ok': False, 'issue': f'folder cover check failed: {exc}', 'path': folder_copy}
        folder_ok = bool(folder_status.get('ok'))
        if not folder_copy and folder_status.get('path'):
            folder_copy = folder_status.get('path') or ''

        if result.get('export_only'):
            embed_ok = bool(verified_art and verified_compatible)
        else:
            embed_ok = bool((not embed_failures) and int(result.get('updated') or 0) == int(result.get('total') or 0))

        try:
            db.update_album_path(album_key, album_path, width=(None if w == '?' else w), height=(None if h == '?' else h))
        except Exception:
            pass

        needs_conversion = bool((not verified_compatible) or (folder_copy_enabled and not folder_ok) or embed_failures or not embed_ok)
        if needs_conversion:
            issue_parts = []
            if not verified_compatible:
                issue_parts.append(verified_issue or 'not baseline JPEG')
            if embed_failures or not embed_ok:
                issue_parts.append('conversion failed or incomplete')
            if folder_copy_enabled and not folder_ok:
                issue_parts.append(folder_status.get('issue') or 'folder cover missing')
            compat_issue = '; '.join(x for x in issue_parts if x) or 'conversion still required'
        else:
            compat_issue = ''

        partial_reason = ''
        if failed:
            partial_reason = f'Convert/Save finished with {len(failed)} warning(s)'
        elif needs_conversion:
            partial_reason = compat_issue or 'conversion still required'
        notes_update = {
            'artwork_compatibility': {
                'needs_conversion': needs_conversion,
                'issue': compat_issue,
                'converted_to': f'{w}×{h} baseline JPEG' if verified_compatible and not embed_failures else '',
                'converted_at': db.now(),
            },
            'album_folder_cover': {
                'needs_save': bool(folder_copy_enabled and not folder_ok),
                'issue': (folder_status.get('issue') or '') if folder_copy_enabled and not folder_ok else '',
                'saved_path': folder_copy if folder_ok or folder_copy else '',
                'saved_at': db.now() if folder_copy and folder_ok else '',
                'checked_at': db.now(),
            },
            'partial_failure': {
                'reason': partial_reason,
                'failed_items': len(failed),
                'checked_at': db.now(),
            },
        }
        if not needs_conversion and verified_art:
            try:
                checked_files = int(result.get('total') or 0)
                if checked_files <= 0:
                    checked_files = int((completed_album.get('notes_json') or {}).get('approved_artwork', {}).get('total_files') or 0)
                if checked_files <= 0:
                    checked_files = len(list(iter_music_files(album_path)))
                notes_update.update(deep_check_resolved_note(
                    checked_files,
                    target or get_preferred_artwork_size(settings),
                    f'{w}×{h}',
                    source='convert/save',
                ))
            except Exception:
                pass
        db.update_album_notes(album_key, notes_update)

        # Reclassify through the shared evaluator so Convert/Good/Needs Search
        # follows the same rules as scans, search results, and queue filters.
        try:
            final_state = db.evaluate_and_set_album_state(
                album_key,
                target_size=target,
                preserve_user_terminal=False,
                settings=settings,
            )
            final_status = final_state.get('status') or ''
        except Exception:
            final_status = 'incompatible_artwork' if needs_conversion else ('needs_review' if not size_ok else 'already_good')
            try:
                db.set_album_status(album_key, final_status)
            except Exception:
                pass
        marked_good = final_status in ('already_good', 'approved')
        left_needs_search = final_status in ('needs_review', 'no_candidate', 'missing_artwork')
        still_not_square = final_status == 'not_square_artwork'
        needs_conversion = final_status == 'incompatible_artwork'

        # The conversion/export action has finished. Clear active progress
        # immediately so the top activity area cannot look like it is still
        # preparing/working after the final log message has been written.
        try:
            self.progress_var.set(0)
            self.progress_text.set('')
        except Exception:
            pass

        self._clear_current_art_cache(album_key, remove_files=True)
        self._queue_consistency_check(album_key, repair=True, context='convert/save')
        copy_text = f' Saved album-folder copy: {folder_copy}' if folder_copy else ''
        good_text = ' Marked Good.' if marked_good else (' Left in Needs Search because embedded artwork is still below target.' if left_needs_search else (' Still Not Square.' if still_not_square else ''))
        reason = result.get('conversion_reason') or 'convert/save embedded artwork'
        original_dims = f'{result.get("original_width") or "?"}×{result.get("original_height") or "?"}'
        original_issue = result.get('original_issue') or ''
        if result.get('export_only'):
            log_line = f'Embedded artwork already compatible: {original_dims} baseline JPEG. Saved cover.jpg only.{good_text}'
        elif result.get('needed_format_conversion') and result.get('needed_resize') and result.get('needed_square_conversion'):
            log_line = f'Converted, resized, and squared embedded artwork: {original_dims} → {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_format_conversion') and result.get('needed_resize'):
            log_line = f'Converted {original_issue} and resized to target baseline JPEG: {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_format_conversion') and result.get('needed_square_conversion'):
            log_line = f'Converted and squared embedded artwork from {original_issue}: {original_dims} → {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_resize') and result.get('needed_square_conversion'):
            log_line = f'Resized and squared embedded artwork: {original_dims} → {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_format_conversion'):
            log_line = f'Converted embedded artwork from {original_issue} to baseline JPEG: {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_resize'):
            log_line = f'Resized baseline JPEG to target size: {original_dims} → {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        elif result.get('needed_square_conversion'):
            log_line = f'Squared embedded artwork: {original_dims} → {w}×{h} into {result.get("updated", 0)}/{result.get("total", 0)} file(s).{good_text}'
        else:
            log_line = f'Convert/Save completed: {reason}: {w}×{h}.{good_text}'
        completed_label = self._workflow_album_label(album_key=album_key)
        if marked_good:
            outcome = 'Good'
        elif left_needs_search:
            outcome = 'Needs Search'
        elif still_not_square:
            outcome = 'Still Not Square'
        elif needs_conversion:
            outcome = 'Still Convert'
        else:
            outcome = 'Done'
        cover_note = ' · cover.jpg saved' if folder_copy else ''
        warning_note = f' · {len(failed)} warning(s)' if failed else ''
        worker_sec = result.get('remote_worker_duration_seconds')
        worker_note = f' · NAS {float(worker_sec):.1f}s' if worker_sec not in (None, '') else ''
        concise_line = f'Convert/Save: {completed_label} · {w}×{h} · {outcome}{cover_note}{warning_note}{worker_note}.'
        self.log_msg(f'\n{concise_line}\n')
        self.log_verbose(f'  Details: {log_line}\n')
        self.log_verbose(f'  Compatibility checked after action: embedded artwork is {"baseline JPEG" if verified_compatible else (verified_issue or "not baseline JPEG")} ({w}×{h}).\n')
        if folder_copy:
            self.log_verbose(f'  Saved album-folder copy: {folder_copy}\n')
        if folder_copy_enabled and not folder_ok:
            self.log_verbose(f'  Folder cover still needs attention: {folder_status.get("issue") or "cover.jpg missing"}\n')
        result_msg = concise_line.strip()
        if folder_copy:
            result_msg += ' Saved cover.jpg.'
        if marked_good:
            result_msg += ' Removed from Needs Convert / marked Good.'
        elif left_needs_search:
            result_msg += ' Now needs a larger/better cover search.'
        elif needs_conversion:
            result_msg += ' Still needs compatibility attention.'
        self._set_action_result(result_msg)
        if failed:
            self.log_verbose(f'  Failed/warning items: {len(failed)}\n')
            self.status_var.set(f'Convert/Save finished with {len(failed)} warning(s).')
            self.set_status_dot('#ff9f0a')
        else:
            if marked_good:
                self.status_var.set(('Convert/Save complete. Marked Good.' + copy_text).strip())
                self.set_status_dot('#26b53f')
            elif left_needs_search:
                self.status_var.set(('Converted/saved, but artwork is still below target. Needs Search.' + copy_text).strip())
                self.set_status_dot('#ff9f0a')
            elif needs_conversion:
                self.status_var.set(('Convert/Save finished, but compatibility still needs attention.' + copy_text).strip())
                self.set_status_dot('#ff9f0a')
            elif result.get('export_only'):
                self.status_var.set(('Already baseline JPEG. Saved cover.jpg.' + copy_text).strip())
                self.set_status_dot('#26b53f')
            else:
                self.status_var.set(('Convert/Save complete.' + copy_text).strip())
                self.set_status_dot('#26b53f')

        # Refresh the queue row. Only reload the review pane if the user is
        # still looking at the album that finished; otherwise do not pull focus
        # away from whatever they selected while the background task ran.
        selected_key = getattr(self, 'current_album_key', None)
        try:
            self.refresh_queue_tab(preserve_selection=True)
        except TypeError:
            self.refresh_queue_tab()
        if selected_key == album_key:
            try:
                fresh_album = db.get_album(album_key)
                if fresh_album:
                    self.current_album_info = fresh_album
                self.load_album_for_review(album_key)
            except Exception:
                pass
        try:
            self._sync_top_status_label()
        except Exception:
            pass

    def choose_release(self):
        active = self.active_album_info()
        if not active:
            messagebox.showinfo('Choose release', 'Select an album from the queue or review area first.')
            return
        ReleaseSelectorWindow(self, active)

    def enlarge_candidate(self):
        c = self.current_candidate()
        if c and c.get('image_path'):
            title = f'{c.get("artist", "")} - {c.get("album", "")} artwork option'
            ImagePreviewWindow(self, image_path=c.get('image_path'), title=title, candidate_navigation=True)

    def enlarge_current(self):
        if self.current_old_art_info and (self.current_old_art_info.get('image_path') or self.current_old_art_info.get('bytes')):
            info = self.active_album_info() or {}
            title = f'{info.get("artist", "")} - {info.get("album", "")} current embedded artwork'
            if self.current_old_art_info.get('image_path'):
                ImagePreviewWindow(self, image_path=self.current_old_art_info.get('image_path'), title=title, album_navigation=True, preview_target='current')
            else:
                ImagePreviewWindow(self, image_bytes=self.current_old_art_info.get('bytes'), title=title, album_navigation=True, preview_target='current')

    def source_page_url_from_candidate(self, cand):
        if not cand:
            return ''
        meta = cand.get('source_meta_json') or {}
        if meta.get('source_page'):
            return meta.get('source_page')
        source = (cand.get('source') or '').lower()
        rel = (cand.get('release_mbid') or '').strip()
        if source == 'musicbrainz' and rel:
            return f'https://musicbrainz.org/release/{rel}'
        if source == 'discogs' and rel:
            return f'https://www.discogs.com/release/{rel}'
        if source == 'itunes':
            itunes_id = rel.split(':', 1)[1] if rel.startswith('itunes:') else rel
            if itunes_id:
                return f'https://music.apple.com/album/{itunes_id}'
        if source == 'deezer':
            deezer_id = rel.split(':', 1)[1] if rel.startswith('deezer:') else rel
            if deezer_id:
                return f'https://www.deezer.com/album/{deezer_id}'
        return cand.get('source_url') or ''

    def open_source_page(self):
        c = self.current_candidate()
        if not c:
            return
        url = self.source_page_url_from_candidate(c)
        if not url:
            messagebox.showinfo('Open source page', 'No source page is available for the selected artwork option.')
            return
        webbrowser.open(url)

    def open_restore_browser(self):
        BackupRestoreWindow(self)

    def _save_direct_source_candidates(self, candidates, info):
        saved = 0
        for cand in candidates or []:
            try:
                cand.update({'album_folder': info.get('album_path') or ''})
                cand['candidate_id'] = db.add_candidate(info['album_key'], cand)
                saved += 1
            except Exception as exc:
                self.log_verbose(f'  Could not save direct-source candidate: {exc}\n')
        if saved:
            db.set_album_status(info['album_key'], 'candidate_found')
            db.update_album_notes(info['album_key'], {
                'last_search_summary': [f'Direct source URL: {saved} saved'],
                'last_search_at': db.now(),
                'last_search_saved': saved,
            })
            self.candidates = db.load_candidates(include_rejected=False)
            self._rebuild_groups()
            self.current_album_key = info['album_key']
            self.candidate_index = 0
            self._pin_album_in_current_filter(info['album_key'], reason='direct source artwork import')
            self.show_current_album()
            self.refresh_queue_tab()
            self.refresh_footer()
        return saved

    def add_artwork_from_source_url(self):
        info = self.active_album_info()
        if not info:
            messagebox.showinfo('Add artwork from source URL', 'Select an album first.')
            return
        raw = simpledialog.askstring(
            'Add artwork from source URL / ID',
            'Paste a Deezer album URL/ID, Google result link to Deezer, Apple Music/iTunes album URL/ID, or MusicBrainz release URL:',
            parent=self.root,
        )
        if not raw:
            return
        value = raw.strip()
        album_rec = db.get_album(info['album_key']) or {}
        search_info = self._album_to_search_info({**info, **album_rec, 'album_path': info.get('album_path') or album_rec.get('album_path')})
        artist = search_info.get('search_artist') or search_info.get('artist') or info.get('artist') or ''
        album = search_info.get('search_album') or search_info.get('album') or info.get('album') or ''
        max_candidates = get_max_candidates_per_album()
        saved = 0
        try:
            unwrapped_values = DeezerProvider.unwrap_source_url(value)
            decoded_value = ' '.join(unwrapped_values)
            deezer_album_id = DeezerProvider.extract_album_id(value)
            apple_match = re.search(r'(?:id|/)(\d{5,})(?:\D|$)', decoded_value, re.I)
            mb_match = re.search(r'musicbrainz\.org/release/([0-9a-f-]{36})', decoded_value, re.I) or re.search(r'\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b', decoded_value, re.I)
            if deezer_album_id and not ('music.apple' in decoded_value.lower() or 'itunes.apple' in decoded_value.lower()):
                album_id = deezer_album_id
                provider = DeezerProvider()
                item = provider.fetch_album(album_id)
                if not item:
                    raise ValueError('Deezer did not return album details for that ID.')
                cands = provider.get_candidates_from_release(artist, album, info['album_key'], item, max_candidates=max_candidates, log=lambda s: self.log_verbose(s + '\n'))
                saved = self._save_direct_source_candidates(cands, info)
            elif 'music.apple' in decoded_value.lower() or 'itunes.apple' in decoded_value.lower() or (apple_match and 'deezer' not in decoded_value.lower()):
                album_id = apple_match.group(1) if apple_match else value
                provider = ITunesProvider()
                data = provider._get_json(f'https://itunes.apple.com/lookup?id={album_id}&entity=album')
                results = (data or {}).get('results') or []
                item = next((r for r in results if r.get('collectionId')), results[0] if results else None)
                if not item:
                    raise ValueError('Apple/iTunes did not return album details for that ID.')
                cands = provider.get_candidates_from_release(artist, album, info['album_key'], item, max_candidates=max_candidates, log=lambda s: self.log_verbose(s + '\n'))
                saved = self._save_direct_source_candidates(cands, info)
            elif mb_match:
                mbid = mb_match.group(1)
                provider = MusicBrainzProvider()
                rel = provider.fetch_release(mbid)
                if not rel:
                    raise ValueError('MusicBrainz did not return that release.')
                cands = provider.get_candidates_from_release(artist, album, info['album_key'], rel, max_candidates=max_candidates, log=lambda s: self.log_verbose(s + '\n'))
                saved = self._save_direct_source_candidates(cands, info)
            else:
                messagebox.showerror('Unsupported source URL', 'I could not recognise that URL/ID. Try a Deezer album URL/ID, a Google result link to Deezer, an Apple/iTunes album URL/ID, or a MusicBrainz release URL.')
                return
        except Exception as exc:
            messagebox.showerror('Source URL import failed', str(exc))
            return
        self.status_var.set(f'Added {saved} artwork option(s) from source URL.' if saved else 'Source URL was recognised, but no suitable artwork met your size rules.')

    def locate_album_folder(self):
        info = self.active_album_info()
        if not info:
            return
        folder = filedialog.askdirectory(title='Locate album folder')
        if not folder:
            return
        if not os.path.isdir(folder):
            messagebox.showerror('Folder unavailable', 'That folder is not available.')
            return
        db.update_album_path(info['album_key'], folder)
        db.update_album_notes(info['album_key'], {'path_repaired_at': db.now(), 'path_repaired_to': folder})
        self.current_album_info = db.get_album(info['album_key'])
        self.status_var.set(f'Updated album folder path: {folder}')
        self.load_album_for_review(info['album_key'])
        self.refresh_queue_tab()
        self.refresh_footer()

    def _candidate_paths_from_db(self):
        paths = set()
        try:
            with db.connect() as c:
                for row in c.execute('SELECT image_path FROM candidates WHERE image_path IS NOT NULL AND image_path<>""'):
                    paths.add(str(row['image_path']))
        except Exception:
            pass
        return paths

    def clear_log(self):
        try:
            self.log.delete('1.0', 'end')
            self.log_msg(f'{BUILD_VERSION}\nLog cleared.\n')
            self.status_var.set('Progress / History log cleared.')
        except Exception:
            pass

    def save_log(self):
        try:
            default = f'artwork_manager_log_{time.strftime("%Y%m%d_%H%M%S")}.txt'
            path = filedialog.asksaveasfilename(
                title='Save Progress / History Log',
                defaultextension='.txt',
                initialfile=default,
                filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            )
            if not path:
                return
            content = self.log.get('1.0', 'end').strip() + '\n'
            Path(path).write_text(content, encoding='utf-8')
            self.status_var.set(f'Log saved: {path}')
        except Exception as exc:
            messagebox.showerror('Save log failed', str(exc))

    def clear_rejected_candidate_files(self):
        removed = 0
        missing = 0
        try:
            with db.connect() as c:
                rows = c.execute('SELECT image_path FROM candidates WHERE rejected=1 AND image_path IS NOT NULL AND image_path<>""').fetchall()
            for row in rows:
                path = row['image_path']
                try:
                    if path and os.path.exists(path):
                        removed += self._remove_candidate_file_path(path)
                    else:
                        missing += 1
                except Exception:
                    pass
        except Exception as exc:
            messagebox.showerror('Cleanup failed', str(exc))
            return
        self.status_var.set(f'Cleanup complete: trashed {removed} rejected candidate file(s).')
        self.log_msg(f'\nCleanup: trashed {removed} rejected candidate file(s); {missing} were already missing.\n')

    def clear_handled_temporary_artwork(self, confirm=True):
        """Backfill cleanup for albums that were handled in earlier builds."""
        if confirm and not messagebox.askyesno('Trash handled temporary artwork?', 'Move temporary/import artwork for handled albums to the Trash?'):
            return 0
        removed = 0
        skipped = 0
        try:
            with db.connect() as c:
                rows = c.execute('''
                    SELECT c.image_path
                    FROM candidates c
                    JOIN albums a ON a.album_key=c.album_key
                    WHERE a.status IN ('approved','reviewed_skipped','already_good','ignored')
                      AND c.image_path IS NOT NULL AND c.image_path<>''
                ''').fetchall()
            freed = 0
            for row in rows:
                path = row['image_path']
                if self._is_app_temporary_artwork_path(path):
                    try:
                        if path and os.path.exists(path):
                            freed += os.path.getsize(path)
                    except Exception:
                        pass
                    removed += self._remove_candidate_file_path(path)
                else:
                    skipped += 1
        except Exception as exc:
            messagebox.showerror('Cleanup failed', str(exc))
            return 0
        freed_txt = bytes_text(locals().get('freed', 0))
        self.status_var.set(f'Cleanup complete: trashed {removed} temporary/import artwork file(s), freeing about {freed_txt}.')
        extra = f'; skipped {skipped} non-temporary path(s)' if skipped else ''
        self.log_msg(f'\nCleanup: trashed {removed} temporary/import artwork file(s) for handled albums; freed about {freed_txt}{extra}.\n')
        return removed


    def trash_approved_artwork_copies(self, confirm=True):
        """Move old approved_artwork copies to Trash to reclaim Application Support space."""
        if not APPROVED_DIR.exists():
            self.status_var.set('No approved artwork folder found.')
            return 0
        try:
            files = [p for p in APPROVED_DIR.rglob('*') if p.is_file()]
        except Exception as exc:
            messagebox.showerror('Cleanup failed', str(exc))
            return 0
        if not files:
            self.status_var.set('No approved artwork copies to trash.')
            return 0
        if confirm and not messagebox.askyesno(
            'Trash approved artwork copies?',
            f'Move {len(files)} saved approved artwork file(s) from Application Support to the Trash?\n\n'
            'This will not remove embedded artwork from your music files or album-folder cover files.'
        ):
            return 0
        freed = 0
        trashed = 0
        for path in files:
            try:
                freed += path.stat().st_size
            except Exception:
                pass
            trashed += self._trash_managed_file_path(path, (APPROVED_DIR,))
        freed_txt = bytes_text(freed)
        self.status_var.set(f'Cleanup complete: trashed {trashed} approved artwork copy file(s), freeing about {freed_txt}.')
        self.log_msg(f'\nCleanup: trashed {trashed} approved artwork copy file(s) from approved_artwork; freed about {freed_txt}.\n')
        return trashed

    def trash_all_temporary_artwork(self, confirm=True):
        if confirm and not messagebox.askyesno('Trash temporary artwork?', 'Move all app-managed temporary candidate artwork and copied manual imports to the Trash?'):
            return 0
        removed = 0
        freed = 0
        try:
            for root in (TEMP_DIR, IMPORT_DIR):
                root.mkdir(parents=True, exist_ok=True)
                for path in root.rglob('*'):
                    if path.is_file():
                        try:
                            freed += path.stat().st_size
                        except Exception:
                            pass
                        removed += self._trash_managed_file_path(path, (TEMP_DIR, IMPORT_DIR))
        except Exception as exc:
            messagebox.showerror('Cleanup failed', str(exc))
            return 0
        freed_txt = bytes_text(freed)
        self.status_var.set(f'Cleanup complete: trashed {removed} temporary/import artwork file(s), freeing about {freed_txt}.')
        self.log_msg(f'\nCleanup: trashed {removed} temporary/import artwork file(s); freed about {freed_txt}.\n')
        return removed

    def clear_orphan_temporary_images(self):
        referenced = self._candidate_paths_from_db()
        removed = 0
        try:
            for root in (TEMP_DIR, IMPORT_DIR):
                root.mkdir(parents=True, exist_ok=True)
                for path in root.rglob('*'):
                    if path.is_file() and str(path) not in referenced:
                        try:
                            path.unlink()
                            removed += 1
                        except Exception:
                            pass
        except Exception as exc:
            messagebox.showerror('Cleanup failed', str(exc))
            return
        self.status_var.set(f'Cleanup complete: trashed {removed} orphan temporary image(s).')
        self.log_msg(f'\nCleanup: trashed {removed} orphan temporary image(s).\n')



    def repair_stale_candidate_rows(self):
        """Remove candidate rows whose temporary/import image files no longer exist.

        Cleanup actions intentionally move candidate artwork out of Application
        Support after albums are handled. If a database row survives that cleanup,
        the queue can show Review even though no candidate can be previewed. This
        maintenance action removes those stale rows and reclassifies affected
        albums through the same disk refresh path used elsewhere.
        """
        if self._block_if_write_action_active('Repair stale candidates'):
            return
        self._set_current_operation('repairing', 'Checking for stale candidate rows…')
        rows = db.all_candidate_file_rows(include_rejected=True)
        stale_ids = []
        affected = set()
        for row in rows:
            path = row.get('image_path') or ''
            if path and not os.path.exists(path):
                stale_ids.append(row.get('id'))
                if row.get('album_key'):
                    affected.add(row.get('album_key'))
        removed = db.delete_candidate_ids(stale_ids)
        rechecked = 0
        for key in affected:
            try:
                album = db.get_album(key) or {}
                album_path = album.get('album_path') or ''
                if album_path and os.path.isdir(album_path):
                    self._rescan_album_path(album_path)
                    rechecked += 1
                else:
                    # If the folder is unavailable and there are no candidates left,
                    # avoid leaving a phantom Review state.
                    refreshed = db.get_album(key) or album
                    if (refreshed.get('status') == 'candidate_found') and not db.load_candidates_for_album(key):
                        db.set_album_status(key, 'needs_review')
            except Exception as exc:
                self.log_msg(f'Stale candidate repair warning for {key}: {exc}\n')
        self._clear_current_operation('repairing')
        self.load_saved_queue(silent=True)
        msg = f'Removed {removed} stale candidate row(s).'
        if rechecked:
            msg += f' Re-evaluated {rechecked} affected album(s).'
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.log_msg('\n' + msg + '\n')

    def find_repair_inconsistent_queue_rows(self):
        """Find obviously inconsistent database rows and optionally repair them from disk."""
        if self._block_if_write_action_active('Find inconsistent queue rows'):
            return
        self._set_current_operation('repairing', 'Checking queue for inconsistent rows…')
        albums = db.load_albums(actionable_only=False)
        issues = []
        for album in albums:
            status = album.get('status') or ''
            w, h = album.get('width'), album.get('height')
            reasons = []
            if status in ('already_good', 'approved') and (w is None or h is None):
                reasons.append(f'{self.queue_status_label(status)} but current artwork is recorded as Missing')
            if status == 'candidate_found' and int(album.get('candidate_count') or 0) <= 0:
                reasons.append('Review status but no saved candidate rows')
            if status == 'not_square_artwork' and not album_has_not_square_artwork(album):
                reasons.append('Not Square status but current artwork is recorded as square')
            if status == 'incompatible_artwork' and not self._needs_convert_reason(album):
                reasons.append('Needs Convert without a current conversion requirement')
            if status in ('already_good', 'approved') and self._needs_convert_reason(album):
                reasons.append(f'{self.queue_status_label(status)} but compatibility/folder-cover notes still say conversion is needed')
            try:
                evaluated_status, evaluated_reason = evaluate_album_record(album, settings=getattr(self, 'settings', None))
                if status not in ('reviewed_skipped', 'ignored') and evaluated_status != status:
                    reasons.append(f'stored {self.queue_status_label(status)} but disk/notes evaluate as {self.queue_status_label(evaluated_status)} ({evaluated_reason})')
            except Exception:
                pass
            if reasons:
                issues.append((album, reasons))
        if not issues:
            msg = 'No obvious inconsistent queue rows found.'
            self.status_var.set(msg)
            self._set_action_result(msg)
            self.log_msg('\n' + msg + '\n')
            return
        preview = '\n'.join(f'- {(a.get("artist") or "Unknown")} — {(a.get("album") or "Unknown")}: {", ".join(r)}' for a, r in issues[:12])
        more = '' if len(issues) <= 12 else f'\n…plus {len(issues) - 12} more'
        if not messagebox.askyesno('Repair inconsistent queue rows?', f'Found {len(issues)} potentially inconsistent queue row(s). Re-read those album folders from disk now?\n\n{preview}{more}'):
            msg = f'Found {len(issues)} potentially inconsistent queue row(s); no repair was run.'
            self.status_var.set(msg)
            self._set_action_result(msg)
            return
        repaired = 0
        unavailable = 0
        for album, _reasons in issues:
            path = album.get('album_path') or ''
            if not path or not os.path.isdir(path):
                unavailable += 1
                continue
            try:
                self._rescan_album_path(path)
                repaired += 1
            except Exception as exc:
                self.log_msg(f'Could not repair {album.get("artist", "")} — {album.get("album", "")}: {exc}\n')
        self._clear_current_operation()
        self.load_saved_queue(silent=True)
        msg = f'Repaired/re-evaluated {repaired} inconsistent row(s).'
        if unavailable:
            msg += f' {unavailable} folder(s) unavailable.'
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.log_msg('\n' + msg + '\n')

    def reevaluate_queue_statuses(self):
        """Re-read known albums from disk and update queue state without provider search."""
        if self._block_if_write_action_active('Re-evaluate queue'):
            return
        self._set_current_operation('re_evaluating', 'Re-evaluating queue statuses…')
        albums = db.load_albums(actionable_only=False)
        count = 0
        unavailable = 0
        for album in albums:
            path = album.get('album_path') or ''
            if not path or not os.path.isdir(path):
                unavailable += 1
                continue
            self._rescan_album_path(path)
            count += 1
        self._clear_current_operation()
        self.load_saved_queue(silent=True)
        msg = f'Re-evaluated {count} album(s) from disk.'
        if unavailable:
            msg += f' {unavailable} folder(s) unavailable.'
        self.status_var.set(msg)
        self._set_action_result(msg)
        self.log_msg('\n' + msg + '\n')

    def rebuild_queue_counts(self):
        self.candidates = db.load_candidates(include_rejected=False)
        self._rebuild_groups()
        self.refresh_queue_tab()
        self.refresh_footer()
        self.status_var.set('Queue counts refreshed.')
        self.log_msg('\nQueue counts refreshed from the database.\n')

    def export_diagnostics(self):
        try:
            info = self.active_album_info() or {}
            counts = db.album_counts()
            settings = load_settings().copy()
            if settings.get('discogs_token'):
                settings['discogs_token'] = 'set (hidden)'
            recent_log = self.log.get('end-80l', 'end') if hasattr(self, 'log') else ''
            details = self.details.get('1.0', 'end') if hasattr(self, 'details') else ''
            album = db.get_album(info.get('album_key')) if info.get('album_key') else None
            payload = [
                f'Artwork Review Manager diagnostics',
                f'Build: {BUILD_VERSION}',
                f'Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}',
                '',
                'Queue counts:',
                json.dumps(counts, indent=2),
                '',
                'Selected album:',
                json.dumps(album or info, indent=2, default=str),
                '',
                'Settings:',
                json.dumps(settings, indent=2, default=str),
                '',
                'Details box:',
                details.strip(),
                '',
                'Recent log:',
                recent_log.strip(),
            ]
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            default = REPORT_DIR / f'artwork_manager_diagnostics_{time.strftime("%Y%m%d_%H%M%S")}.txt'
            path = filedialog.asksaveasfilename(
                title='Export diagnostics',
                initialfile=default.name,
                initialdir=str(REPORT_DIR),
                defaultextension='.txt',
                filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            )
            if not path:
                return
            Path(path).write_text('\n'.join(payload), encoding='utf-8')
            self.status_var.set(f'Diagnostics exported: {path}')
            messagebox.showinfo('Diagnostics exported', f'Saved diagnostics to:\n{path}')
        except Exception as exc:
            messagebox.showerror('Diagnostics export failed', str(exc))

    def open_album_folder(self):
        info = self.active_album_info()
        path = info.get('album_path') if info else ''
        if path and os.path.isdir(path):
            open_path(path)
        else:
            messagebox.showerror('Album folder unavailable', 'Album folder is unavailable. Reconnect the drive/NAS or use Actions → Locate Album Folder…')

    def undo(self):
        res = undo_last_embed()
        messagebox.showinfo('Undo result', f'Restored: {res.get("restored", 0)}\nFailed: {len(res.get("failed", []))}')

    def clear_saved_queue(self):
        if messagebox.askyesno('Clear saved queue?', 'This clears the saved album queue and artwork option records. It will not delete your music files. Continue?'):
            db.clear_queue()
            self.candidates = []
            self.groups = OrderedDict()
            self.album_keys = []
            self.current_album_key = None
            self.candidate_index = 0
            self.clear('Saved queue cleared.')
            self.refresh_queue_tab()
            self.refresh_footer()
            self.status_var.set('Saved queue cleared.')
            self.progress_var.set(0)
            self.progress_text.set('')


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
