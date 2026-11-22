"""Folder evidence (discogs_evidence). Run: python3 tests/test_evidence.py

Albums are built in a temporary folder: WAV files exactly as long as CD sectors say, an EAC log
whose TOC fits them, folder names carrying catalogue numbers."""
import sys
import tempfile
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import discogs_evidence as de  # noqa: E402
import discogs_mapping as dm  # noqa: E402
import tm_readers as tr  # noqa: E402

SECTOR_FRAMES = 588                     # 44.1 kHz frames in one CD sector (1/75 s)
LENGTHS = (300, 450, 375)               # sectors per track


def _wav(path: Path, sectors: int, extra_frames: int = 0) -> None:
    """A silent CD-format WAV SECTORS long (EXTRA_FRAMES more: no longer whole CD sectors,
    as a lossy or edited copy)."""
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b'\0' * 4 * (SECTOR_FRAMES * sectors + extra_frames))


def _eac_log(artist, title, lengths=LENGTHS) -> str:
    rows, start = [], 0
    for n, length in enumerate(lengths, 1):
        rows.append(f'       {n}  |  0:00.00 |  0:04.00 |    {start:>6}    |   {start + length - 1:>6}')
        start += length
    return ('Exact Audio Copy V1.6 from 23. October 2020\n\n'
            'EAC extraction logfile from 4. March 2019, 21:10\n\n'
            f'{artist} / {title}\n\n'
            'TOC of the extracted CD\n\n'
            '     Track |   Start  |  Length  | Start sector | End sector\n'
            '    ---------------------------------------------------------\n'
            + '\n'.join(rows) + '\n')


def _album(root: Path, folder: str, log=True, lengths=LENGTHS, disc_dirs=(), extra_frames=0) -> list[str]:
    """An album folder under ROOT/Artist/FOLDER; returns its audio files' paths."""
    album = root / 'Candlemass' / folder
    paths = []
    for disc in disc_dirs or ('',):
        d = album / disc
        d.mkdir(parents=True, exist_ok=True)
        for n, length in enumerate(lengths, 1):
            p = d / f'{n:02d} - Track {n}.wav'
            _wav(p, length, extra_frames)
            paths.append(str(p))
        if log:
            (d / 'Candlemass - Nightfall.log').write_text(_eac_log('Candlemass', 'Nightfall', lengths))
    return paths


class Location(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_album_folder(self):
        paths = _album(self.root, 'Nightfall (1987)', log=False)
        loc = de.album_location(paths)
        self.assertEqual(loc['album_folder'], 'Nightfall (1987)')
        self.assertEqual(loc['artist_folder'], 'Candlemass')
        self.assertEqual(loc['exclude'], [])

    def test_disc_folder_reads_the_set(self):
        paths = _album(self.root, 'Box (1990)', log=False, disc_dirs=('CD1', 'CD2'))
        loc = de.album_location(paths[:3])          # one disc loaded on its own
        self.assertEqual(loc['album_folder'], 'Box (1990)')
        self.assertEqual(len(loc['exclude']), 3)     # the other disc's files: not this album's

    def test_artist_folder_refused(self):
        # two albums of an artist matched as one: their common folder is the artist's
        many = _album(self.root, 'A (1987)', log=False, lengths=(75,) * 3)
        for i in range(30):
            _album(self.root, f'Other {i} (1990)', log=False, lengths=(75,))
        other = _album(self.root, 'B (1988)', log=False, lengths=(75,))
        self.assertIsNone(de.album_location(many + other))

    def test_too_deep_refused(self):
        p = self.root / 'a' / 'b' / 'c' / 'd'
        p.mkdir(parents=True)
        _wav(p / '01.wav', 75)
        q = self.root / 'a' / 'x.wav'
        _wav(q, 75)
        self.assertIsNone(de.album_location([str(p / '01.wav'), str(q)]))

    def test_no_files(self):
        self.assertIsNone(de.album_location([]))


class CopyEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _read(self, folder, **kw):
        loc = de.album_location(_album(self.root, folder, **kw))
        return de.copy_evidence(de.read_copy(loc))

    def test_folder_catalogue_number_and_disc_id(self):
        ev = self._read('Nightfall (1987) [Active, ACTLP 1]')
        self.assertIn('ACTLP 1', ev['catnos'])
        self.assertEqual(ev['sources']['catalogue number'], 'the folder name')
        # the log's TOC fits the files to the sector: its disc ID is the copy's
        toc = tr.log_tocs(str(self.root / 'Candlemass' / 'Nightfall (1987) [Active, ACTLP 1]'))[1]
        self.assertEqual(ev['discids'], {1: tr.mb_discid(toc)})
        self.assertEqual(ev['discid_from'], {1: 'the rip log or cue sheet'})
        self.assertEqual(ev['rip_log_year'], 2019)
        self.assertTrue(de.has_copy_evidence(ev))

    def test_log_of_another_rip_is_not_read(self):
        # the log says 300/450/375 sectors; these files are other lengths (another rip's log):
        # its TOC is not the copy's. The files are whole CD sectors, so their own lengths give
        # the disc's TOC instead.
        album = self.root / 'Candlemass' / 'Nightfall (1987)'
        paths = _album(self.root, 'Nightfall (1987)', log=False, lengths=(310, 440, 380))
        (album / 'other.log').write_text(_eac_log('Candlemass', 'Nightfall'))
        ev = de.copy_evidence(de.read_copy(de.album_location(paths)))
        log_toc = tr.log_tocs(str(album))[1]
        self.assertNotIn(tr.mb_discid(log_toc), ev['discids'].values())
        self.assertEqual(ev['discid_from'], {1: "your files' lengths (whole CD sectors)"})
        self.assertIsNone(ev['rip_log_year'])        # (the log is not of these files)

    def test_image_file_gives_no_disc_id(self):
        # one FLAC (here WAV) image of the whole CD: one "track", no disc's TOC
        paths = _album(self.root, 'Nightfall (1987)', log=False, lengths=(1125,))
        ev = de.copy_evidence(de.read_copy(de.album_location(paths)))
        self.assertEqual(ev['discids'], {})

    def test_lossy_copy_has_no_disc_id(self):
        paths = _album(self.root, 'Nightfall (1987)', log=False, extra_frames=100)
        ev = de.copy_evidence(de.read_copy(de.album_location(paths)))
        self.assertEqual(ev['discids'], {})

    def test_nothing_said(self):
        ev = self._read('Nightfall', log=False, extra_frames=100)
        self.assertEqual(ev['catnos'], [])
        self.assertFalse(de.has_copy_evidence(ev))
        self.assertEqual(de.evidence_summary(ev), ['the folder says nothing about the pressing'])


class Facts(unittest.TestCase):
    MB = {'barcode': '5051083075114', 'catalognumber': 'MB 1', 'date': '1987-05-01',
          'releasecountry': 'GB', 'media': 'CD'}
    EV = {'barcodes': ['072435234565'], 'note_barcodes': [], 'catnos': ['ACTLP 1', 'ACT 1'],
          'year': 2001, 'media': 'Vinyl', 'discids': {},
          'sources': {'barcode': "your files' tags", 'catalogue number': 'the folder name'}}

    def test_copy_beats_musicbrainz(self):
        f = de.match_facts(self.MB, self.EV)
        self.assertEqual(f['barcode'], '072435234565')
        self.assertEqual(f['catalognumber'], 'ACTLP 1; ACT 1')
        self.assertEqual(f['date'], '2001')
        self.assertEqual(f['media'], 'Vinyl')
        self.assertEqual(f['releasecountry'], 'GB')          # the copy said nothing of it

    def test_no_evidence_keeps_musicbrainz(self):
        self.assertEqual(de.match_facts(self.MB, None), self.MB)
        self.assertEqual(de.search_terms(self.MB, None),
                         {'barcode': '5051083075114', 'catalog_number': 'MB 1'})

    def test_search_by_copy_first(self):
        self.assertEqual(de.search_terms(self.MB, self.EV),
                         {'barcode': '072435234565', 'catalog_number': 'ACTLP 1'})

    def test_match_explanation_names_the_source(self):
        cand = {'barcode': ['072435234565'], 'catno': 'ACTLP 1', 'year': '1987', 'format': ['Vinyl']}
        detail = dm.explain_release_match(cand, de.match_facts(self.MB, self.EV))
        label, text = dm.describe_match(detail)
        self.assertEqual(label, 'Exact')
        self.assertIn("barcode (from your files' tags)", text)
        self.assertIn('catalogue number (from the folder name)', text)


class AgainstTheRelease(unittest.TestCase):
    def test_disc_id_on_the_matched_release(self):
        node = {'media': [{'discs': [{'id': 'abc-'}]}, {'discs': []}]}
        ids = de.mb_release_discids(node)
        self.assertEqual(ids, {'abc-'})
        self.assertEqual(de.discid_verdict({'discids': {1: 'abc-'}}, ids), 'agrees')
        self.assertEqual(de.discid_verdict({'discids': {1: 'xyz-'}}, ids), 'unknown')
        self.assertEqual(de.discid_verdict({'discids': {}}, ids), '')

    def test_linked_release_without_your_barcode(self):
        rel = {'id': 42, 'identifiers': [{'type': 'Barcode', 'value': '5 051083 075114'}]}
        self.assertIn('not on Discogs release 42',
                      de.contradicts_release({'barcodes': ['072435234565']}, rel))
        self.assertEqual(de.contradicts_release({'barcodes': ['5051083075114']}, rel), '')
        self.assertEqual(de.contradicts_release({'barcodes': []}, rel), '')
        # a release listing no barcode is not contradicted
        self.assertEqual(de.contradicts_release({'barcodes': ['072435234565']}, {'id': 1}), '')

    def test_upc_and_its_ean_are_one_barcode(self):
        rel = {'id': 7, 'identifiers': [{'type': 'Barcode', 'value': '0072435234565'}]}
        self.assertEqual(de.contradicts_release({'barcodes': ['072435234565']}, rel), '')


class BarcodeForms(unittest.TestCase):
    def test_leading_zero_restored(self):
        self.assertEqual(de._barcode_form('72435234565'), '072435234565')

    def test_short_number_is_no_barcode(self):
        self.assertEqual(de._barcode_form('123'), '')


if __name__ == '__main__':
    unittest.main()
