"""discogs_copy: TagMender's edition, Grouping marks, vinyl rip comment, comments, compilations,
genres and .lrc files on Picard's tags.

Run: python3 tests/test_copy.py"""
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import discogs_copy as dc  # noqa: E402
from test_albumartist import Meta  # noqa: E402

VA = '89ad4ac3-39f7-470e-963a-56509c546377'


def prefs(**kw):
    base = {'ed_album': 'edition_year', 'ed_version_tags': True, 'mark_grouping': True, 'vinyl_comment': True,
            'comments': 'keep', 'comp_extra_flags': False, 'comp_grouping': False, 'comp_single_artist': False,
            'genre_max': 0, 'genre_map': '', 'lyrics_lrc': False}
    base.update(kw)
    return dc.prefs(base)


NO_FACTS = {'hd': False, 'mark': '', 'rip_line': ''}


def wav(path, bits=16, rate=44100):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes(bytes((bits // 8) * 2 * (rate // 10)))


class Editions(unittest.TestCase):
    def test_remaster(self):
        m = Meta(album=['Nightfall'], media=['CD'], date=['2004-05-01'], originaldate=['1987-11-01'])
        changed, ed = dc.apply_to(m, '', prefs(), NO_FACTS)
        self.assertEqual(ed, '2004 Remaster')
        self.assertEqual(m.getall('album'), ['Nightfall [2004 Remaster]'])
        self.assertEqual(m.getall('albumversion'), ['2004 Remaster'])
        # again (Discogs wrote its tags, a file joined): once
        dc.apply_to(m, '', prefs(), NO_FACTS, previous_edition=ed)
        self.assertEqual(m.getall('album'), ['Nightfall [2004 Remaster]'])

    def test_kind_only(self):
        m = Meta(album=['Nightfall'], media=['CD'], date=['2004'], originaldate=['1987'])
        dc.apply_to(m, '', prefs(ed_album='edition'), NO_FACTS)
        self.assertEqual(m.getall('album'), ['Nightfall [Remaster]'])

    def test_original_cd_has_none(self):
        m = Meta(album=['Nightfall'], media=['CD'], date=['1987'], originaldate=['1987'])
        changed, ed = dc.apply_to(m, '', prefs(), NO_FACTS)
        self.assertEqual((ed, m.getall('album')), ('', ['Nightfall']))
        self.assertNotIn('albumversion', m)

    def test_vinyl_and_sacd_always(self):
        m = Meta(album=['A Farewell to Kings'], media=['12" Vinyl'], date=['1977'], originaldate=['1977'])
        self.assertEqual(dc.apply_to(m, '', prefs(), NO_FACTS)[1], '1977 Vinyl Rip')
        m = Meta(album=['Brave'], media=['Hybrid SACD (SACD layer)'], date=['1999'], originaldate=['1994'])
        self.assertEqual(dc.apply_to(m, '', prefs(), NO_FACTS)[1], '1999 SACD')

    def test_hd(self):
        m = Meta(album=['Nightfall'], media=['Digital Media'], date=['1987'], originaldate=['1987'])
        self.assertEqual(dc.apply_to(m, '', prefs(), dict(NO_FACTS, hd=True))[1], 'HD')

    def test_bootleg_is_no_remaster(self):
        # Warsaw: a 1994 bootleg of 1978 recordings
        m = Meta(album=['Warsaw'], media=['CD, Album, Unofficial Release'], date=['1994'], originaldate=['1978'])
        self.assertEqual(dc.apply_to(m, '', prefs(), NO_FACTS)[1], '')

    def test_generic_mark_gives_way(self):
        m = Meta(album=['Bridge of Sighs (Remastered)'], media=['CD'], date=['1999'], originaldate=['1974'])
        dc.apply_to(m, '', prefs(), NO_FACTS)
        self.assertEqual(m.getall('album'), ['Bridge of Sighs [1999 Remaster]'])

    def test_named_edition(self):
        m = Meta(album=['Echoes (Deluxe Edition)'], media=['CD, Album, Deluxe Edition'], date=['2023'],
                 originaldate=['1999'])
        dc.apply_to(m, '', prefs(), NO_FACTS)
        self.assertEqual(m.getall('album'), ['Echoes [2023 Deluxe Edition]'])

    def test_off(self):
        m = Meta(album=['Nightfall'], media=['CD'], date=['2004'], originaldate=['1987'])
        dc.apply_to(m, '', prefs(ed_album='off'), NO_FACTS)
        self.assertEqual(m.getall('album'), ['Nightfall'])


class Marks(unittest.TestCase):
    def test_grouping_only_when_empty(self):
        m = Meta(album=['X'])
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, mark='Vinyl Rip'))
        self.assertEqual(m.getall('grouping'), ['Vinyl Rip'])
        m = Meta(album=['X'], grouping=['Mine'])
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, mark='HD Audio'))
        self.assertEqual(m.getall('grouping'), ['Mine'])

    def test_hd_and_vinyl_from_the_copy(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / '1977 - A Farewell to Kings [Vinyl Rip 24-96]'
            folder.mkdir()
            wav(folder / '01.wav', 24, 96000)
            (folder / 'notes.txt').write_text('Vinyl rip by PBTHAL, ripped 2012. 24-bit/96 kHz.')
            facts = dc.copy_facts([str(folder / '01.wav')], str(folder), ['12" Vinyl'], prefs(),
                                  {'catno': 'SRM-1-1184', 'year': '1977'})
            self.assertTrue(facts['hd'])
            self.assertEqual(facts['mark'], 'Vinyl Rip')
            self.assertIn('Vinyl rip:', facts['rip_line'])
            self.assertIn('24-bit/96 kHz', facts['rip_line'])
            cd = Path(d) / 'cd'
            cd.mkdir()
            wav(cd / '01.wav', 16, 44100)
            facts = dc.copy_facts([str(cd / '01.wav')], str(cd), ['CD'], prefs(), {})
            self.assertEqual((facts['hd'], facts['mark']), (False, ''))


class GroupingFollowsTheCopy(unittest.TestCase):
    """TagMender's marks.py: a mark only where the file has no Grouping of its own, judged on the
    file on disk (Picard's metadata never carries its Grouping); a mark the plugin wrote follows
    what the copy turns out to be."""

    def test_a_mark_written_before_is_replaced(self):
        m = Meta(album=['X'])
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, hd=True, mark='HD Audio'))
        self.assertEqual(m.getall('grouping'), ['HD Audio'])
        # Discogs then says the release is an LP: a 24-bit vinyl rip
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, hd=True, mark='Vinyl Rip'))
        self.assertEqual(m.getall('grouping'), ['Vinyl Rip'])
        # and when the copy is no longer HD, vinyl or tape, the mark goes
        dc.apply_to(m, '', prefs(ed_album='off'), NO_FACTS)
        self.assertEqual(m.getall('grouping'), [])

    def test_your_grouping_on_disk_is_never_replaced(self):
        m = Meta(album=['X'])                   # (Picard's file metadata: no Grouping in it)
        dc.apply_to(m, '/m/01.flac', prefs(ed_album='off'), dict(NO_FACTS, mark='Vinyl Rip'),
                    on_disk_grouping=['Favourites'])
        self.assertEqual(m.getall('grouping'), ['Favourites'])
        m = Meta(album=['X'], grouping=['HD Audio'])      # (written before this fix)
        dc.apply_to(m, '/m/01.flac', prefs(ed_album='off'), dict(NO_FACTS, mark='HD Audio'),
                    on_disk_grouping=['Favourites'])
        self.assertEqual(m.getall('grouping'), ['Favourites'])

    def test_a_mark_on_disk_follows_the_copy(self):
        m = Meta(album=['X'])
        dc.apply_to(m, '/m/01.flac', prefs(ed_album='off'), dict(NO_FACTS, mark='Vinyl Rip'),
                    on_disk_grouping=['HD Audio'])
        self.assertEqual(m.getall('grouping'), ['Vinyl Rip'])

    def test_compilation_before_the_mark(self):
        m = Meta(album=['X'], albumartist=['Various Artists'], musicbrainz_albumartistid=[VA],
                 grouping=['HD Audio'])
        dc.apply_to(m, '/m/01.flac', prefs(ed_album='off', comp_grouping=True), dict(NO_FACTS, mark='HD Audio'))
        self.assertEqual(m.getall('grouping'), ['Compilation'])

    def test_vinyl_from_the_copy_itself(self):
        """VIN-10: a needle-drop picture makes a vinyl rip of a release MusicBrainz lists as CD."""
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / '1977 - A Farewell to Kings'
            folder.mkdir()
            for name in ('01 Xanadu.wav', '02 Closer to the Heart.wav'):
                wav(folder / name, 24, 96000)
            (folder / 'needle drop.jpg').write_bytes(b'x')
            audio = [str(p) for p in sorted(folder.glob('*.wav'))]
            facts = dc.copy_facts(audio, str(folder), ['CD'], prefs(), {})
            self.assertTrue(facts['vinyl_copy'])
            self.assertEqual(facts['mark'], 'Vinyl Rip')


class Comments(unittest.TestCase):
    LINE = 'Vinyl rip: PBTHAL · 24-bit/96 kHz · ripped 2012'

    def test_rip_line_added_once(self):
        m = Meta(album=['X'], comment=['Ripped with love'])
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, rip_line=self.LINE))
        dc.apply_to(m, '', prefs(ed_album='off'), dict(NO_FACTS, rip_line=self.LINE))
        self.assertEqual(m.getall('comment'), ['Ripped with love\n' + self.LINE])

    def test_remove_keeps_own_line(self):
        m = Meta(album=['X'], comment=['www.some-shop.example'], **{'comment:ripper': ['by someone']})
        dc.apply_to(m, '', prefs(ed_album='off', comments='remove'), NO_FACTS)
        self.assertNotIn('comment', m)
        self.assertNotIn('comment:ripper', m)
        m = Meta(album=['X'], comment=['shop note\n' + self.LINE])
        dc.apply_to(m, '', prefs(ed_album='off', comments='remove'), NO_FACTS)
        self.assertEqual(m.getall('comment'), [self.LINE])


class Compilations(unittest.TestCase):
    def test_various_artists_flags(self):
        m = Meta(album=['Hits'], musicbrainz_albumartistid=[VA], albumartist=['Various Artists'],
                 compilation=['1'])
        dc.apply_to(m, '/m/01.flac', prefs(ed_album='off', comp_extra_flags=True, comp_grouping=True), NO_FACTS)
        self.assertEqual(m.getall('itunescompilation'), ['1'])
        self.assertEqual(m.getall('comp'), ['1'])
        self.assertEqual(m.getall('grouping'), ['Compilation'])
        mp3 = Meta(album=['Hits'], musicbrainz_albumartistid=[VA], compilation=['1'])
        dc.apply_to(mp3, '/m/01.mp3', prefs(ed_album='off', comp_extra_flags=True), NO_FACTS)
        self.assertNotIn('itunescompilation', mp3)            # (ID3 has TCMP of its own)

    def test_single_artist_best_of(self):
        m = Meta(album=['Best Of'], albumartist=['Candlemass'], musicbrainz_albumartistid=['x'],
                 releasetype=['album', 'compilation'])
        dc.apply_to(m, '', prefs(ed_album='off', comp_single_artist=True), NO_FACTS)
        self.assertEqual(m.getall('compilation'), ['1'])
        self.assertEqual(m.getall('albumartist'), ['Candlemass'])
        plain = Meta(album=['Nightfall'], albumartist=['Candlemass'], musicbrainz_albumartistid=['x'],
                     releasetype=['album'])
        dc.apply_to(plain, '', prefs(ed_album='off', comp_single_artist=True), NO_FACTS)
        self.assertNotIn('compilation', plain)


class Genres(unittest.TestCase):
    def test_map_and_limit(self):
        m = Meta(album=['X'], genre=['funeral doom metal', 'Doom Metal', 'Rock', 'Metal'])
        dc.apply_to(m, '', prefs(ed_album='off', genre_max=2,
                                 genre_map='Funeral Doom Metal = Doom Metal\n# note'), NO_FACTS)
        self.assertEqual(m.getall('genre'), ['Doom Metal', 'Rock'])


class Lyrics(unittest.TestCase):
    SYNCED = '[00:01.00] Mirror\n[00:03.50] mirror'

    def test_lrc_text(self):
        self.assertEqual(dc.lrc_text(Meta(lyrics=['plain words'])), '')
        self.assertEqual(dc.lrc_text(Meta(lyrics=[self.SYNCED])), self.SYNCED)
        self.assertEqual(dc.lrc_text(Meta(lyrics=['plain'], **{'~lyrics_synced': [self.SYNCED]})), self.SYNCED)

    def test_written_once(self):
        class F:
            pass
        with tempfile.TemporaryDirectory() as d:
            f = F()
            f.filename = os.path.join(d, '01 Mirror.flac')
            f.metadata = Meta(lyrics=[self.SYNCED])
            on = {'lyrics_lrc': True}
            self.assertTrue(dc.write_lrc(f, on).endswith('01 Mirror.lrc'))
            self.assertEqual(Path(d, '01 Mirror.lrc').read_text(), self.SYNCED + '\n')
            self.assertEqual(dc.write_lrc(f, on), '')                   # (never over one there)
            self.assertEqual(dc.write_lrc(f, {'lyrics_lrc': False}), '')


if __name__ == '__main__':
    unittest.main()
