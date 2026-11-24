"""discogs_albumartist: TagMender's album-artist rule (tm_album_artist) on Picard's tags.

Run: python3 tests/test_albumartist.py

MusicBrainz is a fake serving artist pages with their "member of band" relationships."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import discogs_albumartist as da  # noqa: E402
import tm_album_artist as aa  # noqa: E402

JIMI, EXP = 'jimi-0000', 'exp-0000'
MITCH = 'mitch-0000'
MORSE, PURPLE = 'morse-0000', 'purple-0000'
STRONEN, TIABG = 'stronen-0000', 'tiabg-0000'
CALEXICO, IRON = 'calexico-0000', 'iron-0000'
METHENY, PMG = 'metheny-0000', 'pmg-0000'


def person(mbid, name):
    return {'id': mbid, 'name': name, 'type': 'Person', 'relations': []}


def group(mbid, name, members):
    return {'id': mbid, 'name': name, 'type': 'Group', 'relations': [
        {'type': 'member of band', 'direction': 'backward',
         'artist': {'id': m, 'name': n, 'sort-name': s}} for m, n, s in members]}


ARTISTS = {
    JIMI: person(JIMI, 'Jimi Hendrix'), MITCH: person(MITCH, 'Mitch Mitchell'),
    EXP: group(EXP, 'The Jimi Hendrix Experience', [(JIMI, 'Jimi Hendrix', 'Hendrix, Jimi'),
                                                   (MITCH, 'Mitch Mitchell', 'Mitchell, Mitch')]),
    MORSE: person(MORSE, 'Steve Morse'),
    PURPLE: group(PURPLE, 'Deep Purple', [(MORSE, 'Steve Morse', 'Morse, Steve')]),
    STRONEN: person(STRONEN, 'Thomas Strønen'),
    TIABG: group(TIABG, 'Time Is a Blind Guide', [(STRONEN, 'Thomas Strønen', 'Strønen, Thomas')]),
    CALEXICO: group(CALEXICO, 'Calexico', [('jb', 'Joey Burns', 'Burns, Joey')]),
    IRON: group(IRON, 'Iron & Wine', [('sb', 'Sam Beam', 'Beam, Sam')]),
    METHENY: person(METHENY, 'Pat Metheny'),
    PMG: group(PMG, 'Pat Metheny Group', [(METHENY, 'Pat Metheny', 'Metheny, Pat')]),
}


class FakeHttp:
    def __init__(self, down=False):
        self.down = down
        self.asked = []

    def get_json_or_raise(self, url):
        self.asked.append(url)
        if self.down:
            raise OSError('MusicBrainz is down')
        mbid = url.split('/artist/', 1)[1].split('?', 1)[0] if '/artist/' in url else ''
        return ARTISTS.get(mbid)

    def get_json(self, url, host):
        return None                              # (Discogs not asked: no token)


class Meta(dict):
    """Picard's Metadata, as far as the plugin uses it."""

    def getall(self, key):
        v = self.get(key, [])
        return list(v) if isinstance(v, list) else [v]

    def __setitem__(self, key, value):
        super().__setitem__(key, value if isinstance(value, list) else [value])

    def delete(self, key):
        self.pop(key, None)


PROFILE = da.profile({'aa_lead': True, 'aa_bands': True, 'aa_file_under': ''})


def choose(meta, credit='', prof=PROFILE, down=False):
    da.Lookups._cache.clear()
    return da.choose(da.tags_of(meta, credit), prof, da.Lookups(FakeHttp(down)))


class Bands(unittest.TestCase):
    def test_band_named_after_a_member(self):
        m = Meta(albumartist=['The Jimi Hendrix Experience'], albumartistsort=['Jimi Hendrix Experience, The'],
                 musicbrainz_albumartistid=[EXP], artist=['The Jimi Hendrix Experience'])
        lead = choose(m)
        self.assertEqual(lead[:3], ('Jimi Hendrix', 'Hendrix, Jimi', JIMI))
        changed = da.apply_choice(m, lead)
        self.assertEqual(m.getall('albumartist'), ['Jimi Hendrix'])
        self.assertEqual(m.getall('albumartistsort'), ['Hendrix, Jimi'])
        self.assertEqual(m.getall('musicbrainz_albumartistid'), [JIMI])
        self.assertEqual(m.getall('artist'), ['The Jimi Hendrix Experience'])     # (as credited)
        self.assertIn('albumartist', changed)

    def test_group_suffix(self):
        m = Meta(albumartist=['Pat Metheny Group'], musicbrainz_albumartistid=[PMG])
        self.assertEqual(choose(m)[0], 'Pat Metheny')

    def test_band_not_named_after_a_member_stays(self):
        m = Meta(albumartist=['Deep Purple'], musicbrainz_albumartistid=[PURPLE])
        self.assertIsNone(choose(m))

    def test_bands_off(self):
        prof = da.profile({'aa_lead': True, 'aa_bands': False})
        m = Meta(albumartist=['The Jimi Hendrix Experience'], musicbrainz_albumartistid=[EXP])
        self.assertIsNone(choose(m, prof=prof))

    def test_musicbrainz_down_keeps_the_credit(self):
        m = Meta(albumartist=['The Jimi Hendrix Experience'], musicbrainz_albumartistid=[EXP])
        self.assertEqual(choose(m, down=True), aa.UNKNOWN)


class Credits(unittest.TestCase):
    def test_leader_with_their_band(self):
        m = Meta(albumartist=['Thomas Strønen & Time Is a Blind Guide'],
                 albumartists=['Thomas Strønen', 'Time Is a Blind Guide'],
                 musicbrainz_albumartistid=[STRONEN, TIABG])
        self.assertEqual(choose(m, 'Thomas Strønen & Time Is a Blind Guide')[0], 'Thomas Strønen')

    def test_collaboration_of_equals_kept(self):
        m = Meta(albumartist=['Calexico and Iron & Wine'], albumartists=['Calexico', 'Iron & Wine'],
                 musicbrainz_albumartistid=[CALEXICO, IRON])
        self.assertIsNone(choose(m, 'Calexico and Iron & Wine'))

    def test_various_artists_never(self):
        m = Meta(albumartist=['Various Artists'], musicbrainz_albumartistid=['89ad4ac3-39f7-470e-963a-56509c546377'])
        self.assertIsNone(choose(m))

    def test_lead_off(self):
        prof = da.profile({'aa_lead': False})
        m = Meta(albumartist=['Pat Metheny Group'], musicbrainz_albumartistid=[PMG])
        self.assertIsNone(choose(m, prof=prof))


class YourOwn(unittest.TestCase):
    def test_file_under(self):
        prof = da.profile({'aa_lead': True, 'aa_bands': True,
                           'aa_file_under': 'Dregs = Dixie Dregs\n# a comment\nPat Metheny Group = Pat Metheny Group'})
        self.assertEqual(choose(Meta(albumartist=['Dregs']), prof=prof)[0], 'Dixie Dregs')
        m = Meta(albumartist=['Pat Metheny Group'], musicbrainz_albumartistid=[PMG])
        self.assertIsNone(choose(m, prof=prof))                    # (mapped to itself: kept)

    def test_parse(self):
        self.assertEqual(da.parse_file_under('A = B\nC -> D\n\nbad line\n'), {'A': 'B', 'C': 'D'})


class Picard(unittest.TestCase):
    def test_credit_text(self):
        rel = {'artist-credit': [{'name': 'Pat Metheny', 'joinphrase': ' & '}, {'name': 'Lyle Mays'}]}
        self.assertEqual(da.credit_text(rel), 'Pat Metheny & Lyle Mays')

    def test_unknown_sort_and_id_are_taken_off(self):
        m = Meta(albumartist=['Dregs'], albumartistsort=['Dregs'], musicbrainz_albumartistid=['x'])
        da.apply_choice(m, ('Dixie Dregs', '', '', ''))
        self.assertEqual(m.getall('albumartist'), ['Dixie Dregs'])
        self.assertNotIn('albumartistsort', m)
        self.assertNotIn('musicbrainz_albumartistid', m)


    def test_sort_kept_from_its_place_in_the_credit(self):
        m = Meta(albumartist=['Steve Howe', 'Tomorrow'], albumartistsort=['Howe, Steve', 'Tomorrow'])
        da.apply_choice(m, ('Steve Howe', '', '', ''))
        self.assertEqual(m.getall('albumartist'), ['Steve Howe'])
        self.assertEqual(m.getall('albumartistsort'), ['Howe, Steve'])


if __name__ == '__main__':
    unittest.main()
