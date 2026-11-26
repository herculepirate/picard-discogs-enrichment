"""discogs_kind: what your copy was made from, against the release Picard or MusicBrainz chose.

Run: python3 tests/test_kind.py"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from mutagen.id3 import ID3, TSRC, TXXX  # noqa: E402
from mutagen.wave import WAVE  # noqa: E402

import discogs_copy as dc  # noqa: E402
import discogs_kind as k  # noqa: E402
from test_copy import prefs, wav  # noqa: E402


def shop_tags(path, flag=True):
    """The tags a shop's downloader writes (The Holy Flesh - Emissary and Vessel, from Deezer)."""
    f = WAVE(str(path))
    f.add_tags()
    f.tags.add(TXXX(desc='UPC', text=['859738283076']))
    f.tags.add(TSRC(text=['TCAET2014865']))
    if flag:
        f.tags.add(TXXX(desc='EXPLICIT', text=['0']))
    f.save()


CASSETTE = {'id': 15247631, 'formats': [{'name': 'Cassette', 'qty': '1', 'descriptions': ['Album']}]}
CD = {'id': 7304432, 'formats': [{'name': 'CD', 'qty': '1', 'descriptions': ['HDCD', 'Album']}]}
VINYL = {'id': 513594, 'formats': [{'name': 'Vinyl', 'qty': '2', 'descriptions': ['LP', 'Picture Disc']}]}


class ShopDownload(unittest.TestCase):
    """The Holy Flesh - Emissary and Vessel: MusicBrainz lists only the cassette; the files are a
    Deezer download. No "Tape Rip", and the Discogs cassette is not the copy."""

    def _folder(self, d, flag=True):
        folder = Path(d) / '2020 - Emissary and Vessel'
        folder.mkdir()
        paths = []
        for n in (1, 2, 3):
            p = folder / f'0{n} - Emissary {n}.wav'
            wav(p)
            shop_tags(p, flag)
            paths.append(str(p))
        return folder, paths

    def test_a_download_by_its_tags(self):
        with tempfile.TemporaryDirectory() as d:
            folder, paths = self._folder(d)
            kind, why = k.copy_kind(paths, str(folder))
            self.assertEqual(kind, 'digital')
            self.assertIn('UPC', why)
            self.assertIn('Cassette', k.kind_against(kind, why, CASSETTE))
            facts = dc.copy_facts(paths, str(folder), ['Cassette'], prefs(), {})
            self.assertEqual(facts['mark'], '')
            self.assertEqual(facts['media_ignored'], ['Cassette'])

    def test_no_flag_no_shop(self):
        # a barcode and ISRCs alone are what Picard writes from MusicBrainz: not a shop's
        with tempfile.TemporaryDirectory() as d:
            folder, paths = self._folder(d, flag=False)
            self.assertEqual(k.copy_kind(paths, str(folder)), ('', ''))
            facts = dc.copy_facts(paths, str(folder), ['Cassette'], prefs(), {})
            self.assertEqual(facts['mark'], 'Tape Rip')


class VinylRipMatchedToTheCd(unittest.TestCase):
    """Tool - Lateralus: Picard matched the 2003 CD, MusicBrainz links Discogs CD 7304432; the
    folder is a picture-disc rip (a turntable in "rip log.txt", sides A-D)."""

    def test_the_cd_is_not_the_copy(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / 'Tool - Lateralus'
            folder.mkdir()
            paths = []
            for n, name in ((1, 'The Grudge'), (2, 'Eon Blue Apocalypse')):
                p = folder / f'0{n} {name}.wav'
                wav(p, 24, 96000)
                paths.append(str(p))
            (folder / 'rip log.txt').write_text(
                'Ripping Info / Technical Log\n  Hardware:\n    TT: Clearaudio Emotion modifeyd\n'
                '    Cartridge: Clearaudio Maestro Wood MM\n  Recording:\n    Sony PCM-M10 - 24bit/96kHz\n'
                'Record shape\nSide A EX / NM\nSide B EX / NM\nSide C EX / NM\nSide D VG\n')
            kind, why = k.copy_kind(paths, str(folder))
            self.assertEqual(kind, 'vinyl')
            self.assertIn('CD', k.kind_against(kind, why, CD))
            self.assertEqual(k.kind_against(kind, why, VINYL), '')


class Against(unittest.TestCase):
    def test_nothing_known_is_never_against(self):
        self.assertEqual(k.kind_against('', '', CD), '')
        self.assertEqual(k.kind_against('vinyl', 'x', {'id': 1, 'formats': []}), '')
        self.assertEqual(k.kind_against('cd', 'x', {'id': 1, 'formats': [{'name': 'SACD'}]}), '')
        self.assertEqual(k.kind_against('vinyl', 'x', {'id': 1, 'formats': [{'name': 'CD'}, {'name': 'Vinyl'}]}), '')


if __name__ == '__main__':
    unittest.main()
