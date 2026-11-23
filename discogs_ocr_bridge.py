"""Start discogs_ocr_helper.py on your system's Python and read what it prints.

Picard's Flatpak has no PIL, pyzbar, numpy or tesseract, so the scans are read outside it:
through `flatpak-spawn --host python3 ...` when Picard runs as a Flatpak (it needs
`flatpak override --user --talk-name=org.freedesktop.Flatpak org.musicbrainz.Picard`), else
with the system's python3. The helper's folder and the album's are the same paths on the host
(the plugin lives in Picard's data folder under ~/.var/app, the music in your home).

What the helper read is cached on the host (sqlite, in Picard's cache folder): a scan is read
once. No Qt here; called on the plugin's worker thread.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / 'discogs_ocr_helper.py'
FLATPAK = Path('/.flatpak-info').exists()
# one album's scans (15 by default, each read several ways): minutes the first time
ALBUM_TIMEOUT_S = 900
PROBE_TIMEOUT_S = 30
# a probe that failed is asked again after this long (a package installed meanwhile counts)
PROBE_AGAIN_S = 60
PROBE_KEEP_S = 600

INSTALL_HINT = ('install them on your system (not in Picard), e.g. on Debian/Ubuntu: '
                'sudo apt install tesseract-ocr python3-pil python3-pyzbar python3-numpy poppler-utils')
FLATPAK_HINT = ('Picard is a Flatpak: let it start programs on your system with '
                'flatpak override --user --talk-name=org.freedesktop.Flatpak org.musicbrainz.Picard '
                '(then restart Picard)')


def cache_file() -> Path:
    base = os.environ.get('XDG_CACHE_HOME') or str(Path.home() / '.cache')
    return Path(base) / 'discogs-enrichment' / 'ocr.sqlite'


def _command(args: list[str]) -> list[str]:
    run = ['python3', '-I', '-B', str(HELPER), *args]
    if FLATPAK:
        return ['flatpak-spawn', '--host', *run]
    return [shutil.which('python3') or 'python3', *run[1:]]


def _run(args: list[str], timeout: float, runner=subprocess.run) -> dict:
    try:
        proc = runner(_command(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return {'ok': False, 'error': f'cannot start {exc.filename or "python3"}',
                'hint': FLATPAK_HINT if FLATPAK else INSTALL_HINT}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'the OCR helper gave no answer within {int(timeout)} s'}
    lines = [l for l in (proc.stdout or '').splitlines() if l.strip()]
    try:
        out = json.loads(lines[-1]) if lines else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        err = (proc.stderr or '').strip().splitlines()
        why = err[-1] if err else f'exit status {proc.returncode}'
        out = {'ok': False, 'error': f'the OCR helper did not run ({why})'}
        if FLATPAK and ('org.freedesktop.Flatpak' in (proc.stderr or '') or proc.returncode in (1, 127)):
            out['hint'] = FLATPAK_HINT
    return out


class OcrBridge:
    """probe() once in a while (cached); read(album_dir) per album."""

    def __init__(self, runner=subprocess.run) -> None:
        self._runner = runner
        self._lock = threading.Lock()
        self._probe: dict | None = None
        self._probed = 0.0

    def probe(self) -> dict:
        """{"ok", "missing", "optional_missing", "error", "hint"}: can the scans be read?"""
        with self._lock:
            age = time.monotonic() - self._probed
            if self._probe is None or age > (PROBE_KEEP_S if self._probe.get('ok') else PROBE_AGAIN_S):
                p = _run(['--probe'], PROBE_TIMEOUT_S, self._runner)
                if not p.get('ok') and p.get('missing') and not p.get('hint'):
                    p['hint'] = INSTALL_HINT
                self._probe, self._probed = p, time.monotonic()
            return dict(self._probe)

    def read(self, album_dir: str, *, max_images: int = 15, have_barcode: bool = False,
             deep: bool = False, extra=()) -> dict:
        args = ['--album', str(album_dir), '--max-images', str(int(max_images)),
                '--cache', str(cache_file())]
        if have_barcode:
            args.append('--have-barcode')
        if deep:
            args.append('--deep')
        for d in extra or ():
            args += ['--extra', str(d)]
        return _run(args, ALBUM_TIMEOUT_S, self._runner)


def embedded_dir(key: str) -> Path:
    """Where an album's embedded cover art is saved for the helper (your home, which the helper
    and a Flatpak Picard both see)."""
    return cache_file().parent / 'embedded' / key


def unavailable_message(probe: dict) -> str:
    """The warning given on every look-up while the scans cannot be read."""
    why = probe.get('error') or ''
    if probe.get('missing'):
        why = 'missing on your system: ' + ', '.join(probe['missing'])
    msg = f'Discogs Enrichment: scans are not read (OCR unavailable: {why or "unknown reason"})'
    if probe.get('hint'):
        msg += f'; {probe["hint"]}'
    return msg


def partial_message(probe: dict) -> str:
    """What is read without the optional parts ("" when nothing is missing)."""
    opt = probe.get('optional_missing') or []
    return ('Discogs Enrichment: scans are read, but without ' + '; '.join(opt)) if opt else ''
