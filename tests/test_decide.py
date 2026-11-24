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

    def test_disc_id_on_one_other_musicbrainz_release_moves_your_files_there(self):
        # Picard matched the vinyl; your rip log's disc ID is on the CD's MusicBrainz release
        album = self._copy('Nightfall (1987)')
        did = tr.mb_discid(album['tocs'][1])
        http = FakeHttp([release(1, 'ACTLP 1')], discids={did: ['mbid-other']},
                        mb_links={'mbid-other': ['1']})
        r = self._decide(album, http, mb_release_id='mbid-matched', mb_discids=set(), linked=[])
        self.assertTrue(r['discid_on'].startswith('other:'), r['discid_on'])
        self.assertEqual(r['status'], 'move', r['why'])
        self.assertEqual(r['move_to'], 'mbid-other')

    def test_disc_id_on_several_other_musicbrainz_releases_asks_you(self):
        album = self._copy('Nightfall (1987)')
        did = tr.mb_discid(album['tocs'][1])
        http = FakeHttp([release(1, 'ACTLP 1')], discids={did: ['mbid-a', 'mbid-b']},
                        mb_links={'mbid-a': ['1']})
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
        self.assertEqual(label, 'Proven (cat. no.)')
        self.assertIn("TagMender's pick", text)


if __name__ == '__main__':
    unittest.main()


class NoInterventionRules(unittest.TestCase):
    """0.9.0: the rules that decide without asking you, each with the case it must not decide."""

    CFG = dd._cfg()

    def _s(self, rid, conf=90.0, clues=None, code='', against=None, **kw):
        return dict({'cand': {'id': rid}, 'confidence': conf, 'clues': clues or {}, 'code': code,
                     'clues_against': against or [], 'tracks_ok': True,
                     'artist_sim': 1.0, 'title_sim': 1.0}, **kw)

    def test_compound_discogs_countries(self):
        self.assertEqual(dd.mc.country_code('UK & Europe'), 'XE')
        self.assertEqual(dd.mc.country_code('Germany, Austria, & Switzerland'), 'DE')
        self.assertEqual(dd.mc.country_code('UK'), 'GB')
        self.assertEqual(dd.mc.country_code(''), '')

    def test_barcode_misread_is_no_catalogue_number(self):
        album = {'barcodes': {'5052498647958'}, 'catnos': {'5053498647958', 'R2 647958'}}
        self.assertEqual(dd.drop_misread_numbers(album), ['5053498647958'])
        self.assertEqual(album['catnos'], {'R2 647958'})
        # a valid barcode, or one two digits off, is left as it is
        album = {'barcodes': {'5052498647958'}, 'catnos': {'5053498647959', '5052498647958'}}
        self.assertEqual(dd.drop_misread_numbers(album), [])

    def test_disc_id_on_two_releases_the_cd_of_the_matched_number(self):
        album = {'discid_mb': {
            'a': {'catnos': ['PEL 349'], 'formats': ['CD'], 'barcode': ''},
            'b': {'catnos': ['PEL 350CD'], 'formats': ['CD'], 'barcode': ''}}}
        mbid, _ = dd.pick_discid_release(album, ['a', 'b'], {'catalognumber': 'PEL 350'}, likely=True)
        self.assertEqual(mbid, 'b')
        # "most likely" off: you are asked
        self.assertIsNone(dd.pick_discid_release(album, ['a', 'b'], {'catalognumber': 'PEL 350'}, likely=False)[0])
        # both agree with the matched number: you are asked
        album['discid_mb']['a']['catnos'] = ['PEL 350']
        self.assertIsNone(dd.pick_discid_release(album, ['a', 'b'], {'catalognumber': 'PEL 350'}, likely=True)[0])

    def test_disc_id_on_two_releases_your_barcode_decides(self):
        album = {'barcodes': {'0123456789012'}, 'discid_mb': {
            'a': {'catnos': [], 'formats': ['CD'], 'barcode': '0123456789012'},
            'b': {'catnos': [], 'formats': ['CD'], 'barcode': '0999999999999'}}}
        self.assertEqual(dd.pick_discid_release(album, ['a', 'b'], {}, likely=False)[0], 'a')

    def test_proof_first_takes_the_one_disc_id_pressing(self):
        disc = self._s(2, 82, clues={'disc TOC': 'disc ID matches'})
        rest = [self._s(i, 82) for i in (1, 3, 4)]
        self.assertIs(dd.proof_first(rest + [disc], self.CFG), disc)

    def test_proof_first_not_when_another_is_proven_as_strongly(self):
        a = self._s(1, 82, clues={'disc TOC': 'x'})
        b = self._s(2, 82, clues={'disc TOC': 'x'})
        self.assertIsNone(dd.proof_first([a, b], self.CFG))

    def test_proof_first_not_with_anything_against_or_another_title(self):
        a = self._s(1, 82, clues={'disc TOC': 'x'}, against=['scans say made in DE'])
        self.assertIsNone(dd.proof_first([a], self.CFG))
        b = self._s(1, 82, clues={'disc TOC': 'x'}, title_sim=0.5)
        self.assertIsNone(dd.proof_first([b], self.CFG))
        c = self._s(1, 82, clues={'disc TOC': 'x'})
        self.assertIsNone(dd.proof_first([c], self.CFG, held={'1'}))

    def test_proof_first_never_on_a_catalogue_number_alone(self):
        a = self._s(1, 82, code='catalogue number')
        self.assertIsNone(dd.proof_first([a], self.CFG))

    def test_only_unopposed_with_its_country_printed(self):
        eu = self._s(1, 99, clues={'country': 'made in the EU on the scans (a European pressing)'})
        others = [self._s(i, 99, against=['scans say made in XE, this pressing is from Taiwan']) for i in (2, 3)]
        self.assertIs(dd.only_unopposed([eu] + others), eu)
        # nothing printed for it, but the country your copy names rules out every other: taken
        bare = self._s(1, 99)
        self.assertIs(dd.only_unopposed([bare] + others), bare)
        # the others ruled out for another reason (not a country your copy names): not enough
        other_reason = [self._s(i, 99, against=['your rip log is dated 2010; this pressing came out in 2015'])
                        for i in (2, 3)]
        self.assertIsNone(dd.only_unopposed([bare] + other_reason))

    def test_only_unopposed_by_the_folder_names_country(self):
        europe = self._s(3203195, 100)
        russia = self._s(12837163, 100, against=['folder name says DE, this pressing is from Russia'])
        self.assertIs(dd.only_unopposed([europe, russia]), europe)
        # two unopposed: not decided
        self.assertIsNone(dd.only_unopposed([europe, self._s(4, 100), russia]))


class DeepReading(unittest.TestCase):
    def test_only_words_tesseract_is_sure_of(self):
        import discogs_ocr_helper as h
        head = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext'
        rows = [head,
                '5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t96.1\tPEL',
                '5\t1\t1\t1\t1\t2\t0\t0\t9\t9\t91.0\t350CD',
                '5\t1\t1\t1\t1\t3\t0\t0\t9\t9\t12.0\tqzx',
                '5\t1\t1\t1\t2\t1\t0\t0\t9\t9\t30.0\tAS',
                '5\t1\t1\t1\t2\t2\t0\t0\t9\t9\t22.0\t274']
        self.assertEqual(h._sure_words('\n'.join(rows)), 'PEL 350CD')


class CodelessCopy(unittest.TestCase):
    CFG = dd._cfg()

    def _s(self, rid, barcodes=(), catnos=(), **kw):
        return dict({'cand': {'id': rid, 'barcodes': list(barcodes), 'catnos': list(catnos)},
                     'confidence': 100.0, 'clues': {}, 'code': '', 'clues_against': [],
                     'tracks_ok': True, 'artist_sim': 1.0, 'title_sim': 1.0}, **kw)

    def _album(self, **kw):
        return dict({'scans_complete': True, 'scan_barcodes': set(), 'barcodes': set(),
                     'scan_names': ['Back.png', 'Front.png'], 'printed_text': 'NEW DAY\nHOME'}, **kw)

    def test_the_one_pressing_that_lists_no_code(self):
        bare = self._s(5220713)
        scored = [bare, self._s(8876415, catnos=['pdip6537']), self._s(36205219, catnos=['rbdc12302'])]
        self.assertIs(dd.codeless_pick(scored, self._album(), self.CFG), bare)

    def test_not_without_a_back_cover_read_whole(self):
        scored = [self._s(1), self._s(2, catnos=['abc123'])]
        self.assertIsNone(dd.codeless_pick(scored, self._album(scan_names=['Front.png']), self.CFG))
        self.assertIsNone(dd.codeless_pick(scored, self._album(scans_complete=False), self.CFG))
        self.assertIsNone(dd.codeless_pick(scored, self._album(barcodes={'0123'}), self.CFG))

    def test_not_when_two_list_no_code_or_another_code_is_printed(self):
        self.assertIsNone(dd.codeless_pick([self._s(1), self._s(2)], self._album(), self.CFG))
        scored = [self._s(1), self._s(2, catnos=['PDIP-6537'])]
        self.assertIsNone(dd.codeless_pick(scored, self._album(printed_text='PDIP-6537'), self.CFG))


class CopyMedia(unittest.TestCase):
    def test_a_cd_rip_log_rules_out_the_matched_vinyl(self):
        import tempfile
        import discogs_copy as dc
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(dc.media_of_copy(['12" Vinyl'], d), (['12" Vinyl'], []))   # no log
            Path(d, 'rip.log').write_text(
                'Exact Audio Copy V1.0 beta 3\n\nTOC of the extracted CD\n\n'
                '     Track |   Start  |  Length  | Start sector | End sector \n'
                '    ---------------------------------------------------------\n'
                '        1  |  0:00.00 |  4:00.00 |         0    |    17999   \n'
                '        2  |  4:00.00 |  3:00.00 |     18000    |    31499   \n'
                '        3  |  7:00.00 |  5:00.00 |     31500    |    53999   \n', encoding='utf-8')
            kept, ignored = dc.media_of_copy(['12" Vinyl'], d)
            self.assertEqual((kept, ignored), ([], ['12" Vinyl']))
            self.assertEqual(dc.media_of_copy(['CD'], d), (['CD'], []))


class TwoOnOne(unittest.TestCase):
    REL = {'id': 8752298, 'title': 'Beginnings / Tomorrow',
           'tracklist': [{'type_': 'track', 'title': f't{i}'} for i in range(21)]}

    def test_one_album_of_a_two_on_one(self):
        self.assertTrue(dm.holds_more_than_album(self.REL, 'Beginnings', 9))
        self.assertTrue(dm.holds_more_than_album(self.REL, 'Tomorrow', 12))

    def test_not_the_whole_release_or_another_title(self):
        self.assertFalse(dm.holds_more_than_album(self.REL, 'Beginnings / Tomorrow', 21))
        self.assertFalse(dm.holds_more_than_album(self.REL, 'Beginnings', 21))
        self.assertFalse(dm.holds_more_than_album(self.REL, 'Relayer', 9))
        one = dict(self.REL, title='Beginnings (Deluxe Edition)')
        self.assertFalse(dm.holds_more_than_album(one, 'Beginnings', 9))   # bonus tracks: not two albums

    def test_only_the_pressing_is_written(self):
        fields = {'album': 'Beginnings / Tomorrow', 'artist': ['Steve Howe', 'Tomorrow'], 'date': '2002',
                  'originaldate': '2002', 'releasecountry': 'RU', 'catalognumber': 'SHWCD 14120201',
                  'label': 'Not On Label', 'media': 'CD', 'discogs_release_id': '8752298',
                  'performer:guitar': 'Steve Howe', '~discogs_track_length': '1:00'}
        self.assertEqual(set(dm.pressing_only(fields)),
                         {'date', 'releasecountry', 'catalognumber', 'label', 'media', 'discogs_release_id'})


class FastFirst(unittest.TestCase):
    def test_settled_without_scans(self):
        disc = {'cand': {'id': 1}, 'clues': {'disc TOC': 'x'}, 'code': ''}
        barcode_only = {'cand': {'id': 2}, 'clues': {}, 'code': 'barcode'}
        self.assertTrue(dd.settled_without_scans({'status': 'move', 'move_to': 'x'}))
        self.assertTrue(dd.settled_without_scans({'status': 'auto', 'best': disc}))
        # a barcode or catalogue number, a likely pick, a review: the scans are read first
        self.assertFalse(dd.settled_without_scans({'status': 'auto', 'best': barcode_only}))
        self.assertFalse(dd.settled_without_scans({'status': 'likely', 'best': disc}))
        self.assertFalse(dd.settled_without_scans({'status': 'review'}))
        self.assertFalse(dd.settled_without_scans(None))


class Mothballs(unittest.TestCase):
    """Your barcode on "Mothballs - Groups & Sessions 64-69"; your files titled "Group - Title"."""
    CFG = dd._cfg()

    def test_subtitled(self):
        self.assertTrue(dd.subtitled('Mothballs [2001 ReIssue]', 'Mothballs - Groups & Sessions 64-69'))
        self.assertTrue(dd.subtitled('The Early Years', 'The Early Years - Live'))
        self.assertFalse(dd.subtitled('Mothballs', 'Mothballs'))
        self.assertFalse(dd.subtitled('Mothballs', 'Mothballs Two'))
        self.assertFalse(dd.subtitled('Moth', 'Mothballs - X'))

    def test_titles_agree_with_a_group_in_front(self):
        mine = [{'num': 1, 'title': 'Syndicats - Maybellene'}, {'num': 2, 'title': 'In Crowd - On The Horizon'},
                {'num': 3, 'title': 'Something Else'}]
        theirs = [{'title': 'Maybellene'}, {'title': 'On The Horizon'}, {'title': 'Other'}]
        self.assertAlmostEqual(dd.titles_agree(mine, theirs), 2 / 3)
        self.assertEqual(dd.titles_agree(mine, theirs[:2]), 0.0)          # counts differ

    def _s(self, title, **kw):
        tracks = [{'title': f'T{i}'} for i in range(4)]
        return dict({'cand': {'id': 9847398, 'title': title, 'tracks': tracks}, 'confidence': 75.0,
                     'clues': {}, 'code': 'barcode', 'clues_against': [], 'tracks_ok': True,
                     'artist_sim': 1.0, 'title_sim': 0.49, 'dur_frac': 0.2}, **kw)

    def test_barcode_on_a_subtitled_release_whose_tracks_agree(self):
        album = {'title': 'Mothballs [2001 ReIssue]',
                 'tracks': [{'num': i + 1, 'title': f'Group - T{i}'} for i in range(4)]}
        x = self._s('Mothballs - Groups & Sessions 64-69')
        self.assertIs(dd.proof_first([x], self.CFG, album=album), x)

    def test_not_when_neither_titles_nor_lengths_agree(self):
        album = {'title': 'Mothballs', 'tracks': [{'num': i + 1, 'title': f'Other {i}'} for i in range(4)]}
        self.assertIsNone(dd.proof_first([self._s('Mothballs - Groups & Sessions 64-69')], self.CFG, album=album))

    def test_not_for_another_title(self):
        album = {'title': 'Mothballs', 'tracks': [{'num': i + 1, 'title': f'T{i}'} for i in range(4)]}
        self.assertIsNone(dd.proof_first([self._s('Beginnings')], self.CFG, album=album))

    def test_picker_search_terms(self):
        self.assertEqual(dm.picker_search_terms('666496403123'), {'barcode': '666496403123'})
        self.assertEqual(dm.picker_search_terms('6 66496 40312 3'), {'barcode': '666496403123'})
        self.assertEqual(dm.picker_search_terms('BIG 4031-2'), {'catalog_number': 'BIG 4031-2'})
        self.assertEqual(dm.picker_search_terms('Steve Howe Mothballs'), {'query': 'Steve Howe Mothballs'})

    def test_match_order_proof_before_doubt(self):
        proven = dd.match_detail(self._s('Mothballs - X'))
        doubtful = dd.match_detail(dict(self._s('Mothballs'), code='', clues_against=['scans say made in US'],
                                        confidence=75.0))
        self.assertEqual(proven['label'], 'Proven (barcode)')
        self.assertEqual(doubtful['label'], 'Doubtful')
        self.assertGreater(proven['score'], doubtful['score'])
