"""The tag source decides which database identifies the pressing first: "Discogs preferred" -
Discogs, from your copy, MusicBrainz breaking ties and filling only what Discogs did not write;
"MusicBrainz preferred" (and the others) - the release Picard matched, Discogs filling.

Run: python3 tests/test_sources.py"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import discogs_decide as dd  # noqa: E402
import discogs_evidence as de  # noqa: E402
import discogs_mapping as dm  # noqa: E402
from test_decide import FakeHttp, release  # noqa: E402
from test_evidence import LENGTHS, _album  # noqa: E402

OFF = list(LENGTHS[:2]) + [LENGTHS[2] + 450]      # the last track 6 s longer than yours


class Order(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.album = de.read_copy(de.album_location(_album(Path(self.tmp.name), 'Nightfall (1987)', log=False)))

    def tearDown(self):
        self.tmp.cleanup()

    def _decide(self, http, first, **kw):
        return dd.decide(self.album, http, dm, first=first, **kw)

    def test_musicbrainz_first_keeps_its_link(self):
        http = FakeHttp([release(1, 'ACT 1'), release(2, 'ACT 2', year=2005, country='Poland', lengths=OFF)])
        r = self._decide(http, 'mb', linked=['2'], mb_facts={'catalognumber': 'ACT 2'})
        self.assertEqual((r['status'], str(r['best']['cand']['id']), r['same_as_mb']), ('linked', '2', True))

    def test_discogs_first_ranks_by_your_copy(self):
        # Discogs ranks 1 clearly first (every track fits); MusicBrainz's link (2) does not decide
        http = FakeHttp([release(1, 'ACT 1'), release(2, 'ACT 2', year=2005, country='Poland', lengths=OFF)])
        r = self._decide(http, 'discogs', linked=['2'], mb_facts={'catalognumber': 'ACT 2'})
        self.assertEqual((r['status'], str(r['best']['cand']['id'])), ('discogs', '1'), r['why'])
        self.assertIs(r['same_as_mb'], False)      # (MusicBrainz is another pressing: album-wide only)

    def test_discogs_first_tie_broken_by_musicbrainz(self):
        http = FakeHttp([release(1, 'ACT 1'), release(2, 'ACT 1', country='Germany')])
        r = self._decide(http, 'discogs', linked=['2'])
        self.assertEqual((r['status'], str(r['best']['cand']['id'])), ('linked', '2'), r['why'])
        r = self._decide(http, 'discogs', mb_facts={'catalognumber': 'ACT 1'})
        self.assertEqual(r['status'], 'review', r['why'])   # (both carry it: no tie broken)

    def test_a_download_is_no_cassette(self):
        # The Holy Flesh - Emissary and Vessel: Discogs lists the cassette and an LP; the files are
        # a download
        http = FakeHttp([release(1, 'CAL 161', fmt='Cassette'), release(2, 'MAVET 14', fmt='Vinyl')])
        for first in ('mb', 'discogs'):
            r = self._decide(http, first, kind=('digital', "a shop's tags"),
                             mb_facts={'catalognumber': 'CAL 161', 'date': '1987'})
            self.assertEqual(r['status'], 'nomatch', (first, r['why']))
            self.assertTrue(r.get('ruled_out'))

    def test_a_vinyl_rip_is_no_cd(self):
        # Tool - Lateralus: MusicBrainz links the CD; the folder is a vinyl rip
        http = FakeHttp([release(1, 'CD 1'), release(2, 'LP 1', fmt='Vinyl')])
        for first in ('mb', 'discogs'):
            r = self._decide(http, first, linked=['1'], kind=('vinyl', 'your rip notes'))
            if r['status'] in ('linked', 'musicbrainz', 'discogs', 'auto', 'likely'):
                self.assertEqual(str(r['best']['cand']['id']), '2', (first, r['why']))


class SamePressing(unittest.TestCase):
    def _s(self, rid, barcodes=(), catnos=()):
        return {'cand': {'id': rid, 'barcodes': list(barcodes), 'catnos': list(catnos)}}

    def test_rules(self):
        self.assertTrue(dd.same_pressing(self._s(1), ['1'], {}))
        self.assertTrue(dd.same_pressing(self._s(1, barcodes=['614223116020']), [], {'barcode': '614223116020'}))
        self.assertTrue(dd.same_pressing(self._s(1, catnos=['CAL-161']), [], {'catalognumber': 'CAL 161'}))
        self.assertIs(dd.same_pressing(self._s(1, catnos=['MAVET14']), [], {'catalognumber': 'CAL 161'}), False)
        self.assertIs(dd.same_pressing(self._s(1), ['2'], {}), False)
        self.assertIsNone(dd.same_pressing(self._s(1), [], {}))


class AlbumWide(unittest.TestCase):
    def test_no_pressing_details(self):
        fields = {'genre': ['Rock'], 'discogs_style': ['Prog Rock'], 'discogs_master_id': '1517',
                  'originalyear': '2001', 'performer:bass': 'Justin Chancellor', 'media': 'CD',
                  'catalognumber': '61422-31160-2', 'label': 'Volcano', 'barcode': '614223116020',
                  'releasecountry': 'US', 'date': '2003', 'discogs_release_id': '7304432',
                  'discogs_company_pressed_by': 'Sonopress USA', 'discogs_notes': 'Jewel case'}
        self.assertEqual(sorted(dm.album_wide_only(fields)),
                         ['discogs_master_id', 'discogs_style', 'genre', 'originalyear', 'performer:bass'])


if __name__ == '__main__':
    unittest.main()
