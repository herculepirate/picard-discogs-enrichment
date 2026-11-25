"""English names only.

Run: python3 tests/test_latin.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discogs_latin as dl  # noqa: E402


class Md(dict):
    """The part of Picard's Metadata the module uses."""
    def getall(self, k):
        v = self.get(k)
        return [] if v is None else (list(v) if isinstance(v, list) else [v])


class EnglishNames(unittest.TestCase):
    def test_latin(self):
        for name in ('Björk', 'Sigur Rós', 'Motörhead', "L'Été indien", 'AC/DC', '1977', ''):
            self.assertTrue(dl.is_latin(name), name)
        for name in ('ミステリー金田一バンド', 'Кино', 'Βαγγέλης', '坂本龍一', 'Mystery 金田一'):
            self.assertFalse(dl.is_latin(name), name)

    def test_unsort(self):
        self.assertEqual(dl.unsort('Mystery Kindaichi Band, The'), 'The Mystery Kindaichi Band')
        self.assertEqual(dl.unsort('Sakamoto, Ryuichi'), 'Ryuichi Sakamoto')
        self.assertEqual(dl.unsort('Hosono, Haruomi & Sakamoto, Ryuichi'), 'Haruomi Hosono & Ryuichi Sakamoto')
        self.assertIsNone(dl.unsort('坂本, 龍一'))

    def test_both_ways(self):
        self.assertEqual(dl.latin_side('金田一耕助の冒険 = The Adventures Of Kindaichi Kosuke'),
                         'The Adventures Of Kindaichi Kosuke')
        self.assertEqual(dl.latin_side('ミステリー金田一バンド (The Mystery Kindaichi Band)'),
                         'The Mystery Kindaichi Band')
        self.assertIsNone(dl.latin_side('ミステリー金田一バンド'))

    def test_the_mystery_kindaichi_band(self):
        """MusicBrainz names the band in Japanese; its sort name and Discogs are English."""
        m = Md(albumartist='ミステリー金田一バンド', albumartistsort='Mystery Kindaichi Band, The',
               artist='ミステリー金田一バンド', artistsort='Mystery Kindaichi Band, The',
               albumartists=['ミステリー金田一バンド'], artists=['ミステリー金田一バンド'],
               album='The Adventures Of Kohsuke Kindaichi', title='金田一耕助のテーマ',
               language='eng', lyrics='金田一', script='Jpan')
        changed, left = dl.english_names(m, {'title': 'Kindaichi Kosuke No Theme'})
        self.assertEqual(m['albumartist'], 'The Mystery Kindaichi Band')
        self.assertEqual(m['artists'], 'The Mystery Kindaichi Band')
        self.assertEqual(m['title'], 'Kindaichi Kosuke No Theme')
        self.assertEqual(m['albumartistsort'], 'Mystery Kindaichi Band, The')
        self.assertEqual(m['lyrics'], '金田一')               # the words themselves stay
        self.assertEqual(m['script'], 'Latn')
        self.assertEqual(left, [])

    def test_no_english_form_stays(self):
        m = Md(title='金田一耕助のテーマ', album='Björk')
        changed, left = dl.english_names(m, {})
        self.assertEqual(m['title'], '金田一耕助のテーマ')
        self.assertEqual(left, ['title: 金田一耕助のテーマ'])
        self.assertEqual(changed, {})

    def test_discogs_name_first(self):
        m = Md(albumartist='Кино', albumartistsort='Kino')
        dl.english_names(m, {'albumartist': 'Kino (2)'.replace(' (2)', '')})
        self.assertEqual(m['albumartist'], 'Kino')

    def test_performers(self):
        m = Md(**{'performer:guitar': ['坂本龍一', 'Steve Howe']})
        dl.english_names(m, {'performer:guitar': ['Ryuichi Sakamoto', 'Steve Howe']})
        self.assertEqual(m['performer:guitar'], ['Ryuichi Sakamoto', 'Steve Howe'])


if __name__ == '__main__':
    unittest.main()
