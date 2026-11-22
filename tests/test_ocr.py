"""Scans and documents: the OCR helper, its bridge, and merging what it read.

Run: python3 tests/test_ocr.py

The helper tests run the real helper on this system's Python when tesseract, Pillow and pyzbar
are installed (skipped otherwise): an EAN-13 barcode drawn here is decoded off a "back cover".
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import discogs_decide as dd  # noqa: E402
import discogs_evidence as de  # noqa: E402
import discogs_mapping as dm  # noqa: E402
import discogs_ocr as do  # noqa: E402
import discogs_ocr_bridge as ob  # noqa: E402
from test_decide import FakeHttp, release  # noqa: E402
from test_evidence import _album  # noqa: E402

BARCODE = '5016583122102'


def scans(**kw):
    out = {'ok': True, 'images': 2, 'ocr_barcodes': [], 'docs_text': '', 'page_text': '',
           'spines': '', 'digits': '', 'rings': '', 'complete': True, 'documents': 0, 'seconds': 1.0}
    out.update(kw)
    return out


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _copy(self, folder='Nightfall (1987)'):
        loc = de.album_location(_album(self.root, folder, log=False))
        return loc, de.read_copy(loc)

    def test_decoded_barcode_is_your_copys(self):
        loc, album = self._copy()
        merged = do.merge_scans(album, loc['album_dir'], scans(ocr_barcodes=[BARCODE]))
        self.assertEqual(album['barcodes'], {BARCODE})
        self.assertEqual(merged['barcodes_from_scans'], [BARCODE])
        ev = de.copy_evidence(album)
        self.assertEqual(ev['barcodes'], [BARCODE])
        self.assertEqual(ev['sources']['barcode'], 'your scans')
        self.assertTrue(de.has_copy_evidence(ev))

    def test_catalogue_number_printed_is_searched(self):
        loc, album = self._copy()
        merged = do.merge_scans(album, loc['album_dir'], scans(
            page_text='CANDLEMASS\nNIGHTFALL\nACTIVE RECORDS\nCAT. NO. ACT 7702\nMade in Sweden'))
        self.assertIn('ACT 7702', merged['codes'])
        self.assertIn('ACT 7702', merged['probe']['catno_search'])
        self.assertIn('NIGHTFALL', merged['text'])
        ev = de.copy_evidence(album)
        self.assertIn('ACT 7702', ev['scan_codes'])
        self.assertTrue(de.has_copy_evidence(ev))

    def test_barcode_only_typed_in_notes_is_not_proof(self):
        loc, album = self._copy()
        notes = f'(info.txt)\nRipped by me. Barcode: {BARCODE}\n'
        do.merge_scans(album, loc['album_dir'], scans(images=0, docs_text=notes, complete=False))
        self.assertFalse(album['barcodes'])
        self.assertEqual(de.copy_evidence(album)['barcodes'], [])

    def test_without_the_helper_documents_are_still_read(self):
        loc, album = self._copy()
        (Path(loc['album_dir']) / 'info.txt').write_text('Label: Active\nCat# ACT 7702\n')
        merged = do.merge_scans(album, loc['album_dir'], None)
        self.assertIn('ACT 7702', merged['text'])
        self.assertNotIn('ocr_barcodes', album)

    def test_nothing_read(self):
        loc, album = self._copy()
        merged = do.merge_scans(album, loc['album_dir'], scans(images=0, complete=False))
        self.assertEqual(merged, {'text': '', 'probe': None, 'codes': [], 'barcodes_from_scans': []})


class DecideWithScans(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_barcode_proves_the_pressing(self):
        loc = de.album_location(_album(self.root, 'Nightfall (1987)', log=False))
        album = de.read_copy(loc)
        merged = do.merge_scans(album, loc['album_dir'], scans(ocr_barcodes=['7321234567890']))
        http = FakeHttp([release(1, 'ACTLP 1', barcode='7 321234 567890'),
                         release(2, 'ACTLP 1', barcode='4 001234 567893', country='Germany')])
        r = dd.decide(album, http, dm, ocr_text=merged['text'], scan_probe=merged['probe'])
        self.assertEqual(r['status'], 'auto', r['why'])
        self.assertEqual(str(r['best']['cand']['id']), '1')
        self.assertEqual(dd.proof_rank(r['best']), 2)

    def test_printed_catalogue_number_finds_a_release_the_title_search_missed(self):
        loc = de.album_location(_album(self.root, 'Nightfall (1987)', log=False))
        album = de.read_copy(loc)
        merged = do.merge_scans(album, loc['album_dir'], scans(page_text='CAT. NO. ACT 7702'))
        http = FakeHttp([release(5, 'ACT 7702')])
        r = dd.decide(album, http, dm, ocr_text=merged['text'], scan_probe=merged['probe'])
        self.assertTrue(any('catno=ACT' in u.replace('%20', ' ').replace('+', ' ') or 'ACT+7702' in u
                            for u in http.asked), http.asked)
        self.assertIn('5', [str(x['cand']['id']) for x in r['scored']])


class Bridge(unittest.TestCase):
    def _runner(self, stdout='', stderr='', code=0, exc=None):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if exc:
                raise exc
            return subprocess.CompletedProcess(cmd, code, stdout, stderr)
        return run, calls

    def test_probe_ok(self):
        run, calls = self._runner(json.dumps({'ok': True, 'missing': [], 'optional_missing': []}))
        b = ob.OcrBridge(runner=run)
        self.assertTrue(b.probe()['ok'])
        b.probe()
        self.assertEqual(len(calls), 1)                    # (kept a while)
        self.assertIn('--probe', calls[0])
        self.assertIn('-I', calls[0])

    def test_flatpak_goes_through_the_host(self):
        run, calls = self._runner(json.dumps({'ok': True}))
        old = ob.FLATPAK
        ob.FLATPAK = True
        try:
            ob.OcrBridge(runner=run).read('/music/x', max_images=4, have_barcode=True)
        finally:
            ob.FLATPAK = old
        self.assertEqual(calls[0][:3], ['flatpak-spawn', '--host', 'python3'])
        self.assertIn('--have-barcode', calls[0])
        self.assertEqual(calls[0][calls[0].index('--max-images') + 1], '4')

    def test_missing_packages_are_named(self):
        run, _ = self._runner(json.dumps({'ok': False, 'missing': ['the tesseract program (tesseract-ocr)'],
                                          'optional_missing': []}))
        p = ob.OcrBridge(runner=run).probe()
        msg = ob.unavailable_message(p)
        self.assertIn('scans are not read', msg)
        self.assertIn('tesseract-ocr', msg)
        self.assertIn('apt install', msg)

    def test_no_python_on_the_host(self):
        run, _ = self._runner(exc=FileNotFoundError(2, 'No such file', 'flatpak-spawn'))
        p = ob.OcrBridge(runner=run).probe()
        self.assertFalse(p['ok'])
        self.assertIn('cannot start flatpak-spawn', ob.unavailable_message(p))

    def test_helper_crash_is_no_answer(self):
        run, _ = self._runner('', 'Portal call failed: org.freedesktop.Flatpak not allowed', 1)
        old = ob.FLATPAK
        ob.FLATPAK = True
        try:
            p = ob.OcrBridge(runner=run).probe()
        finally:
            ob.FLATPAK = old
        self.assertFalse(p['ok'])
        self.assertIn('flatpak override', ob.unavailable_message(p))

    def test_optional_parts_missing(self):
        msg = ob.partial_message({'ok': True, 'optional_missing': ['numpy (python3-numpy): x']})
        self.assertIn('without numpy', msg)
        self.assertEqual(ob.partial_message({'ok': True, 'optional_missing': []}), '')


# EAN-13 drawn as bars (L, G and R codes; the first digit sets the left half's parity)
_L = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011']
_G = [''.join('1' if c == '0' else '0' for c in code)[::-1] for code in _L]
_R = [''.join('1' if c == '0' else '0' for c in code) for code in _L]
_PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG', 'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL']


def _ean13_bits(code: str) -> str:
    d = [int(c) for c in code]
    left = ''.join((_L if p == 'L' else _G)[x] for p, x in zip(_PARITY[d[0]], d[1:7]))
    return '101' + left + '01010' + ''.join(_R[x] for x in d[7:]) + '101'


def _back_cover(path: Path, code: str) -> None:
    from PIL import Image, ImageDraw
    bits = _ean13_bits(code)
    w, unit = 1000, 4
    im = Image.new('RGB', (w, 700), 'white')
    draw = ImageDraw.Draw(im)
    x0 = 300
    for i, b in enumerate(bits):
        if b == '1':
            draw.rectangle([x0 + i * unit, 400, x0 + (i + 1) * unit - 1, 600], fill='black')
    im.save(path, quality=95)


def _helper_ready() -> bool:
    try:
        from PIL import Image  # noqa: F401
        from pyzbar import pyzbar  # noqa: F401
    except Exception:
        return False
    return shutil.which('tesseract') is not None


@unittest.skipUnless(_helper_ready(), 'tesseract, Pillow or pyzbar not installed')
class Helper(unittest.TestCase):
    def _run(self, *args):
        p = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'discogs_ocr_helper.py'), *args],
                           capture_output=True, text=True, timeout=600, cwd='/')
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_probe(self):
        p = self._run('--probe')
        self.assertTrue(p['ok'], p)
        self.assertIn('eng', p['langs'])

    def test_barcode_decoded_off_a_back_cover(self):
        with tempfile.TemporaryDirectory() as d:
            album = Path(d) / 'Artist' / 'Album (1990)'
            album.mkdir(parents=True)
            _back_cover(album / 'Back.jpg', BARCODE)
            cache = Path(d) / 'cache' / 'ocr.sqlite'
            out = self._run('--album', str(album), '--max-images', '3', '--cache', str(cache))
            self.assertTrue(out['ok'], out)
            self.assertEqual(out['images'], 1)
            self.assertEqual(out['ocr_barcodes'], [BARCODE])
            self.assertTrue(out['complete'])
            again = self._run('--album', str(album), '--cache', str(cache))
            self.assertEqual(again['read_now'], 0)             # (the cache: read once)
            self.assertEqual(again['ocr_barcodes'], [BARCODE])

    def test_ean13_drawing(self):
        self.assertEqual(len(_ean13_bits(BARCODE)), 95)


if __name__ == '__main__':
    unittest.main()
