"""Web sources (discogs_web): TagMender's proof over Deezer, iTunes, VGMdb; LRCLIB lyrics.

Run: python3 tests/test_web.py

Albums are real folders (test_evidence's silent WAVs: 4, 6 and 5 seconds); the web is a fake
serving the answers each API gives."""
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import discogs_evidence as de  # noqa: E402
import discogs_web as dw  # noqa: E402
from test_evidence import _album  # noqa: E402

SECS = (4, 6, 5)                       # test_evidence.LENGTHS in seconds


def deezer_album(aid=11, secs=SECS, title='Nightfall', artist='Candlemass'):
    return {
        'search': {'data': [{'id': aid, 'nb_tracks': len(secs)}]},
        'album': {'id': aid, 'title': title, 'artist': {'name': artist}, 'release_date': '1987-11-01',
                  'label': 'Active', 'upc': '7321234567897', 'cover_xl': f'https://cdn/{aid}.jpg',
                  'genres': {'data': [{'name': 'Metal'}]}, 'link': f'https://www.deezer.com/album/{aid}'},
        'tracks': {'data': [{'disk_number': 1, 'track_position': n, 'title': f'Track {n}', 'duration': s,
                             'artist': {'name': artist}} for n, s in enumerate(secs, 1)]},
    }


VGM = {
    'names': {'en': 'Nightfall', 'ja': 'ナイトフォール'}, 'name': 'Nightfall', 'catalog': 'SSCX-10037',
    'release_date': '1999-11-20', 'media_format': 'CD', 'category': 'Game',
    'publisher': {'link': 'org/54', 'names': {'en': 'DigiCube'}},
    'organizations': [{'link': 'org/1', 'names': {'en': 'Active'}, 'role': 'label'}],
    'composers': [{'link': 'artist/1', 'names': {'en': 'Nobuo Uematsu'}}],
    'arrangers': [{'names': {'en': 'Shiro Hamaguchi'}}],
    'lyricists': [], 'performers': [{'names': {'en': 'Candlemass'}}],
    'discs': [{'name': 'Disc 1', 'disc_length': '0:15',
               'tracks': [{'names': {'English': f'Track {n}', 'Japanese': f'トラック{n}'},
                           'track_length': f'0:{s:02d}'} for n, s in enumerate(SECS, 1)]}],
    'picture_full': 'https://media.vgm.io/albums/97/79/79.png',
    'covers': [{'name': 'Front', 'full': 'https://media.vgm.io/f.png', 'thumb': '', 'medium': ''},
               {'name': 'Back', 'full': 'https://media.vgm.io/b.png', 'thumb': '', 'medium': ''}],
}


class FakeWeb:
    def __init__(self, deezer=None, itunes=None, vgmdb=None, lrclib=None, down=()):
        self.deezer, self.itunes, self.vgmdb, self.lrclib = deezer, itunes, vgmdb, lrclib or {}
        self.down = set(down)
        self.asked = []

    def get_json(self, url, host):
        self.asked.append((host, url))
        if host in self.down:
            raise OSError(f'{host} is down')
        u = urlparse(url)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if host == 'deezer' and self.deezer:
            if u.path.startswith('/search/'):
                return self.deezer['search']
            if u.path.endswith('/tracks'):
                return self.deezer['tracks']
            return self.deezer['album']
        if host == 'itunes' and self.itunes:
            return self.itunes['search'] if u.path == '/search' else self.itunes['lookup']
        if host == 'vgmdb' and self.vgmdb:
            if u.path.startswith('/search/'):
                return {'results': {'albums': [{'link': 'album/79', 'titles': {'en': 'Nightfall'}}]}}
            return self.vgmdb
        if host == 'lrclib':
            if u.path == '/api/get':
                return self.lrclib.get('get', {'statusCode': 404})
            return self.lrclib.get('search', [])
        return None

    def get_text(self, url, host):
        return ''


class Find(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        loc = de.album_location(_album(Path(self.tmp.name), 'Nightfall (1987)', log=False))
        self.album = de.read_copy(loc)
        self.album['artist'], self.album['title'] = 'Candlemass', 'Nightfall'

    def tearDown(self):
        self.tmp.cleanup()

    def test_deezer_album_whose_lengths_fit(self):
        r = dw.find(FakeWeb(deezer=deezer_album()), self.album, ['Deezer'])
        self.assertEqual(r['source'], 'Deezer', r)
        self.assertEqual(dw.fill_tags(r['best']['cand']), {'genre': ['Metal']})
        self.assertEqual(dw.cover_urls(r['best']['cand']), [('https://cdn/11.jpg', 'front')])

    def test_lengths_that_do_not_fit_prove_nothing(self):
        r = dw.find(FakeWeb(deezer=deezer_album(secs=(240, 300, 200))), self.album, ['Deezer'])
        self.assertIsNone(r['best'])
        self.assertEqual(r['near'][0], 'Deezer')

    def test_a_source_down_is_skipped(self):
        logged = []
        r = dw.find(FakeWeb(vgmdb=VGM, down={'deezer'}), self.album, ['Deezer', 'VGMdb'], log=logged.append)
        self.assertEqual(r['failed'], ['Deezer'])
        self.assertIn('Deezer lookup failed', logged[0])
        self.assertEqual(r['source'], 'VGMdb', r)

    def test_sources_asked_in_your_order_only(self):
        web = FakeWeb(deezer=deezer_album(), vgmdb=VGM)
        r = dw.find(web, self.album, ['vgmdb', 'deezer'])
        self.assertEqual(r['source'], 'VGMdb')
        self.assertFalse([h for h, _ in web.asked if h == 'deezer'])

    def test_no_name_no_search(self):
        r = dw.find(FakeWeb(), {'artist': '', 'title': '', 'tracks': []}, ['Deezer'])
        self.assertEqual(r['why'], ['no artist or title to search by'])


class Vgmdb(unittest.TestCase):
    def test_album_as_candidate(self):
        c = dw.vgmdb_album(FakeWeb(vgmdb=VGM), 'album/79')
        self.assertEqual([t['secs'] for t in c['tracks']], [4, 6, 5])
        self.assertEqual(c['tracks'][0]['title'], 'Track 1')            # (English first)
        self.assertEqual(c['label'], 'Active')
        self.assertEqual(c['catno_text'], 'SSCX-10037')
        self.assertEqual(c['url'], 'https://vgmdb.net/album/79')
        fill = dw.fill_tags(c)
        self.assertEqual(fill['composer'], ['Nobuo Uematsu'])
        self.assertEqual(fill['arranger'], ['Shiro Hamaguchi'])
        self.assertNotIn('lyricist', fill)
        self.assertNotIn('label', fill)                        # (a pressing's: never filled)
        kinds = dict((k, u) for u, k in dw.cover_urls(c))
        self.assertEqual(kinds['back'], 'https://media.vgm.io/b.png')

    def test_lengths(self):
        self.assertEqual(dw._mmss('3:09'), 189)
        self.assertEqual(dw._mmss('1:02:03'), 3723)
        self.assertEqual(dw._mmss('Unknown'), 0)


class Lyrics(unittest.TestCase):
    HIT = {'id': 5, 'trackName': 'Mirror Mirror', 'artistName': 'Candlemass', 'duration': 274.0,
           'plainLyrics': 'Mirror, mirror upon the wall', 'syncedLyrics': '[00:01.00] Mirror', 'instrumental': False}

    def test_the_very_track(self):
        hit = dw.lyrics(FakeWeb(lrclib={'get': self.HIT}), 'Candlemass', 'Mirror Mirror', 'Nightfall', 275)
        self.assertEqual(hit['plain'], 'Mirror, mirror upon the wall')
        self.assertEqual(hit['synced'], '[00:01.00] Mirror')

    def test_another_length_is_another_recording(self):
        live = dict(self.HIT, id=6, duration=333.0)
        hit = dw.lyrics(FakeWeb(lrclib={'get': {'statusCode': 404}, 'search': [live]}),
                        'Candlemass', 'Mirror Mirror', 'Nightfall', 275)
        self.assertIsNone(hit)

    def test_search_finds_the_fitting_one(self):
        live = dict(self.HIT, id=6, duration=333.0)
        hit = dw.lyrics(FakeWeb(lrclib={'search': [live, self.HIT]}), 'Candlemass', 'Mirror Mirror', '', 273)
        self.assertEqual(hit['id'], 5)

    def test_other_artist(self):
        other = dict(self.HIT, artistName='Snow White Tribute Band')
        self.assertIsNone(dw.lyrics(FakeWeb(lrclib={'get': other}), 'Candlemass', 'Mirror Mirror', '', 274))


class Albums(unittest.TestCase):
    def test_release_album(self):
        rel = {'media': [{'tracks': [{'title': 'A', 'length': 4000,
                                      'artist-credit': [{'name': 'X', 'joinphrase': ' & '}, {'name': 'Y'}]},
                                     {'title': 'B', 'recording': {'length': 6000}}]}]}
        a = dw.release_album(rel, 'X & Y', 'Album')
        self.assertEqual([t['secs'] for t in a['tracks']], [4.0, 6.0])
        self.assertEqual(a['tracks'][0]['artist'], 'X & Y')
        self.assertEqual(a['discs'], 1)

    def test_tags_for_an_album_no_database_has(self):
        with tempfile.TemporaryDirectory() as d:
            loc = de.album_location(_album(Path(d), 'Nightfall (1987)', log=False))
            album = de.read_copy(loc)
            album['artist'], album['title'] = 'Candlemass', 'Nightfall'
            web = FakeWeb(deezer=deezer_album())
            r = dw.find(web, album, ['Deezer'])
            rows = dw.album_tags(album, r['best']['cand'], web, 'Deezer')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['title'], 'Track 2')
        self.assertEqual(rows[0]['album'], 'Nightfall')
        self.assertEqual(rows[0]['tracknumber'], '1')
        self.assertFalse([k for k in rows[0] if k.startswith('tagmender')])


if __name__ == '__main__':
    unittest.main()
