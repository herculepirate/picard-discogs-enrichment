"""discogs_decide: TagMender's verdict, and the plugin's rules on top of it.

Run: python3 tests/test_decide.py

Each album is a real folder (silent WAVs as long as CD sectors say, test_evidence's builder);
Discogs and MusicBrainz are a fake http serving releases made here."""
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import discogs_decide as dd  # noqa: E402
import discogs_evidence as de  # noqa: E402
import discogs_mapping as dm  # noqa: E402
import tm_readers as tr  # noqa: E402
from test_evidence import LENGTHS, _album  # noqa: E402


def _secs(sectors):
    s = round(sectors / 75)
    return f'{s // 60}:{s % 60:02d}'


def release(rid, catno, barcode='', year=1987, country='Sweden', fmt='CD', lengths=LENGTHS):
    return {
        'id': rid, 'title': 'Nightfall', 'year': year, 'country': country, 'master_id': 0,
        'artists': [{'name': 'Candlemass', 'id': 1}],
        'formats': [{'name': fmt, 'qty': '1', 'descriptions': ['Album']}],
        'labels': [{'name': 'Active', 'catno': catno}],
        'identifiers': [{'type': 'Barcode', 'value': barcode}] if barcode else [],
        'tracklist': [{'position': str(n), 'type_': 'track', 'title': f'Track {n}',
                       'duration': _secs(length)} for n, length in enumerate(lengths, 1)],
        'resource_url': f'https://api.discogs.com/releases/{rid}',
    }


class FakeHttp:
    """Discogs search finds every release whose catalogue number or barcode is asked, or all of
    them by title; MusicBrainz knows the disc IDs in `discids`."""

    def __init__(self, releases, discids=None, mb_links=None):
        self.releases = {str(r['id']): r for r in releases}
        self.discids = discids or {}            # disc ID -> [MusicBrainz release ids]
        self.mb_links = mb_links or {}          # MusicBrainz release id -> [Discogs ids]
        self.asked = []

    def _row(self, r):
        return {'id': r['id'], 'title': f'Candlemass - {r["title"]}', 'year': str(r['year']),
                'catno': r['labels'][0]['catno'], 'country': r['country'],
                'barcode': [i['value'] for i in r['identifiers']], 'format': [r['formats'][0]['name']],
                'resource_url': r['resource_url'], 'type': 'release'}

    def get_json(self, url, host):
        self.asked.append(url)
        u = urlparse(url)
        if host == 'discogs' and u.path == '/database/search':
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            hits = list(self.releases.values())
            if 'catno' in qs:
                hits = [r for r in hits if dm._norm_code(r['labels'][0]['catno']) == dm._norm_code(qs['catno'])]
            if 'barcode' in qs:
                hits = [r for r in hits if any(dm._norm_code(i['value']) == dm._norm_code(qs['barcode'])
                                               for i in r['identifiers'])]
            return {'results': [self._row(r) for r in hits]}
        if host == 'discogs' and u.path.startswith('/releases/'):
            return self.releases.get(u.path.rsplit('/', 1)[1])
        if host == 'discogs':
            return None
        if host == 'mb' and '/discid/' in u.path:
            did = u.path.rsplit('/', 1)[1]
            rels = self.discids.get(did)
            return {'id': did, 'releases': [{'id': x, 'media': [{'discs': [{'id': did}]}]} for x in rels]} \
                if rels else {'releases': []}
        if host == 'mb' and '/release/' in u.path:
            rid = u.path.rsplit('/', 1)[1]
            return {'id': rid, 'relations': [
                {'target-type': 'url', 'url': {'resource': f'https://www.discogs.com/release/{x}'}}
                for x in self.mb_links.get(rid, ())]}
        return None

    def get_text(self, url, host):
        return ''


class Decide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _copy(self, folder, **kw):
        loc = de.album_location(_album(self.root, folder, **kw))
        return de.read_copy(loc)

    def _decide(self, album, http, **kw):
        return dd.decide(album, http, dm, **kw)

    def test_folder_catalogue_number_proves_the_pressing(self):
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'MASS CD 1491', year=2005, country='Poland')])
        r = self._decide(album, http)
        self.assertEqual(r['status'], 'auto', r['why'])
        self.assertEqual(str(r['best']['cand']['id']), '1')
        self.assertTrue(dd.proven_by_copy(r['best']))

    def test_number_two_pressings_share_proves_neither(self):
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'ACTLP 1', country='Germany')])
        r = self._decide(album, http)
        self.assertEqual(r['status'], 'review')

    def test_shared_number_most_likely_pressing(self):
        # both carry the folder's number; the other's last track is 6 s longer than yours
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        off = list(LENGTHS[:2]) + [LENGTHS[2] + 450]
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'ACTLP 1', year=2005, lengths=off)])
        r = self._decide(album, http)
        self.assertEqual(r['status'], 'likely', r['why'])
        self.assertEqual(str(r['best']['cand']['id']), '1')
        self.assertEqual([str(x['cand']['id']) for x in r['others']], ['2'])
        self.assertEqual(self._decide(album, http, likely=False)['status'], 'review')

    def test_pressings_nothing_tells_apart_ask_you(self):
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'ACTLP 1', year=2005)])
        self.assertEqual(self._decide(album, http)['status'], 'review')

    def test_printed_mark_only_one_pressing_lists(self):
        # Warsaw: one barcode on three Portuguese runs; the scan prints "(C) 1994 Movieplay"
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        a, b = release(1, 'ACTLP 1', year=1994), release(2, 'ACTLP 1', year=1999)
        a['notes'] = '\u24c1 SPA \u2117 1994 Intermusic S.A. \u00a9 1994 Movieplay S.A.'
        b['notes'] = '(L) SPA \u2117 1994 Intermusic S.A. \u00a9 1994 Intermusic S.A.'
        b['identifiers'] = [{'type': 'Rights Society', 'value': 'MCPS'}]
        http = FakeHttp([a, b])
        scan = 'ISSUED UNDER LICENSE TO INTERMUSIC S.A.\n|994 INTERMUSIC S.A. \u00a9 1994 MOVIEPLAY S.A.\nSPA'
        r = self._decide(album, http, ocr_text=scan, likely=False)
        self.assertEqual(r['status'], 'auto', r['why'])
        self.assertEqual(str(r['best']['cand']['id']), '1')
        self.assertIn('1994 Movieplay', r['why'][0])
        # the scans print nothing that tells them apart: you are asked
        self.assertEqual(self._decide(album, http, ocr_text='SPA 1994 INTERMUSIC', likely=False)['status'], 'review')

    def test_printed_marks(self):
        det = {'notes': '\u2117 1994 Intermusic S.A. \u00a9 1999 IMC Music Ltd.  Made in Portugal. GEMA',
               'identifiers': [{'type': 'Rights Society', 'value': 'SPA'},
                               {'type': 'Matrix / Runout', 'value': 'CDSNV MPG 74034 (94281205)'},
                               {'type': 'Barcode', 'value': '8712177021284'}]}
        self.assertEqual(dd.printed_marks(det), {('rights', 'SPA'), ('rights', 'GEMA'),
                                                 ('matrix', 'CDSNVMPG7403494281205'),
                                                 ('copyright', '1994 INTERMUSIC'), ('copyright', '1999 IMC')})

    def test_printed_marks_another_pressing_also_shows_ask(self):
        x = {'cand': {'id': 1}}
        y = {'cand': {'id': 2}}
        details = {'1': {'identifiers': [{'type': 'Rights Society', 'value': 'SPA'}]},
                   '2': {'identifiers': [{'type': 'Rights Society', 'value': 'MCPS'}]}}
        self.assertIs(dd.printed_tiebreak([x, y], details, 'SPA')[0], x)
        self.assertIsNone(dd.printed_tiebreak([x, y], details, 'SPA MCPS')[0])     # both printed
        self.assertIsNone(dd.printed_tiebreak([x, y], details, 'nothing')[0])

    def test_likely_needs_a_lead(self):
        tied = [{'cand': {'id': n}, 'confidence': 95.0, 'tracks_ok': True, 'clues': {'disc TOC': 'x'}}
                for n in (1, 2)]
        self.assertIsNone(dd.most_likely(tied, tied)[0])
        tied[0]['confidence'] = 97.0
        self.assertEqual(dd.most_likely(tied, tied)[0]['cand']['id'], 1)
        self.assertIsNone(dd.most_likely(tied, tied, held_unofficial={'1'})[0])
        tied[0]['clues_against'] = ['made in Germany']
        self.assertIsNone(dd.most_likely(tied, tied)[0])

    def test_nothing_of_the_copy_is_no_proof(self):
        album = self._copy('Nightfall (1987)', log=False)
        http = FakeHttp([release(1, 'ACTLP 1')])
        r = self._decide(album, http)
        self.assertNotEqual(r['status'], 'auto')

    def test_musicbrainz_link_kept_when_nothing_is_against_it(self):
        album = self._copy('Nightfall (1987)', log=False)
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'MASS 1', country='Poland')])
        r = self._decide(album, http, linked=['2'])
        self.assertEqual(r['status'], 'linked')
        self.assertEqual(str(r['best']['cand']['id']), '2')

    def test_musicbrainz_link_against_the_folder_is_not_kept(self):
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]', log=False)
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'MASS 1', country='Poland')])
        r = self._decide(album, http, linked=['2'])
        self.assertEqual(r['status'], 'auto')            # your copy's number wins
        self.assertEqual(str(r['best']['cand']['id']), '1')

    def test_barcode_of_the_matched_musicbrainz_release(self):
        album = self._copy('Nightfall (1987)', log=False)
        http = FakeHttp([release(1, 'ACTLP 1', barcode='7 321234 567890'),
                         release(2, 'MASS 1', barcode='5 907785 024919', country='Poland')])
        r = self._decide(album, http, mb_facts={'barcode': '5907785024919'})
        self.assertEqual(r['status'], 'musicbrainz')
        self.assertEqual(str(r['best']['cand']['id']), '2')

    def test_disc_id_on_the_matched_release_proves_its_discogs_link(self):
        album = self._copy('Nightfall (1987)')                  # an EAC log that fits
        did = tr.mb_discid(album['tocs'][1])
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'ACT 2', country='Germany')])
        r = self._decide(album, http, mb_release_id='mbid-1', mb_discids={did}, linked=['2'])
        self.assertEqual(r['discid_on'], 'matched')
        self.assertEqual(str(r['best']['cand']['id']), '2')
        self.assertIn('disc TOC', r['best']['clues'])
        self.assertIn(r['status'], ('auto', 'linked'))
        self.assertFalse([u for u in http.asked if '/discid/' in u])   # (no request needed)

    def test_disc_id_outranks_a_shared_catalogue_number(self):
        # both pressings carry the folder's number; only one is linked by the MusicBrainz
        # release your exact disc ID is on
        album = self._copy('Nightfall (1987) [Active, ACTLP 1]')
        did = tr.mb_discid(album['tocs'][1])
        http = FakeHttp([release(1, 'ACTLP 1'), release(2, 'ACTLP 1', country='Germany')])
        r = self._decide(album, http, mb_release_id='mbid-1', mb_discids={did}, linked=['2'])
        self.assertEqual(r['status'], 'auto', r['why'])
        self.assertEqual(str(r['best']['cand']['id']), '2')
        self.assertEqual(dd.proof_rank(r['best']), 3)

    def test_disc_id_on_another_musicbrainz_release_asks_you(self):
        album = self._copy('Nightfall (1987)')
        did = tr.mb_discid(album['tocs'][1])
        http = FakeHttp([release(1, 'ACTLP 1')], discids={did: ['mbid-other']},
                        mb_links={'mbid-other': ['1']})
        r = self._decide(album, http, mb_release_id='mbid-matched', mb_discids=set(), linked=[])
        self.assertTrue(r['discid_on'].startswith('other:'), r['discid_on'])
        self.assertEqual(r['status'], 'review')

    def test_nothing_found(self):
        album = self._copy('Nightfall (1987)', log=False)
        r = self._decide(album, FakeHttp([]))
        self.assertEqual(r['status'], 'nomatch')


class PickerRows(unittest.TestCase):
    def test_release_as_search_row(self):
        row = dd.release_row(release(9, 'ACTLP 1', barcode='7321234567890'))
        summary = dm.summarize_search_result(row)
        self.assertEqual(summary['artist'], 'Candlemass')
        self.assertEqual(summary['title'], 'Nightfall')
        self.assertEqual(summary['catno'], 'ACTLP 1')
        self.assertEqual(summary['barcode'], '7321234567890')

    def test_match_cell(self):
        s = {'confidence': 95.0, 'clues': {'catalogue number': 'x'}, 'code': 'catalogue number (folder name)',
             'clues_against': []}
        label, text = dm.describe_match(dd.match_detail(s, s))
        self.assertEqual(label, 'Proven')
        self.assertIn("TagMender's pick", text)


if __name__ == '__main__':
    unittest.main()
