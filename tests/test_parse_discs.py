"""Disc splitting from Discogs tracklists. Run: python3 tests/test_parse_discs.py"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import discogs_mapping as dm  # noqa: E402


def _heading(title):
    return {'type_': 'heading', 'position': '', 'title': title}


def _track(position, title='t'):
    return {'type_': 'track', 'position': str(position), 'title': title}


class TwoCdsWithSideHeadings(unittest.TestCase):
    """Feared - Reborn (Discogs 10162832): CD + CD, headings "CD 1 - Album", "SIDE A",
    "SIDE B", "CD 2 - Instrumentals", positions restarting at 1 with no disc prefix."""

    def setUp(self):
        self.release = json.loads(
            (HERE / 'data' / 'discogs_10162832_feared_reborn.json').read_text())

    def test_two_discs(self):
        discs = dm.parse_discs(self.release)
        self.assertEqual([len(d['tracks']) for d in discs], [12, 12])
        self.assertEqual([d['number'] for d in discs], [1, 2])
        self.assertEqual([d['title'] for d in discs], ['CD 1 - Album', 'CD 2 - Instrumentals'])
        self.assertEqual(dm.release_disc_count(self.release), 2)

    def test_disc_one_holds_both_sides_in_order(self):
        disc1 = dm.parse_discs(self.release)[0]['tracks']
        self.assertEqual(disc1[0]['title'], 'Album Side A Track 1')
        self.assertEqual(disc1[6]['title'], 'Album Side B Track 1')
        numbers = [dm.extract_track_fields(t, i, len(disc1))['tracknumber']
                   for i, t in enumerate(disc1, start=1)]
        self.assertEqual(numbers, [str(n) for n in range(1, 13)])


class DiscHeadingNames(unittest.TestCase):
    def test_names(self):
        for title in ('CD 1', 'CD2', 'Disc 2', 'Disc Two', 'CD 2 - Instrumentals', 'Bonus CD'):
            self.assertTrue(dm._names_a_disc(title), title)
        for title in ('SIDE A', 'Side B', 'Part I', 'Disco Inferno', 'Discography', 'Women'):
            self.assertFalse(dm._names_a_disc(title), title)


class UnchangedSplits(unittest.TestCase):
    def test_single_lp_sides_stay_one_disc(self):
        release = {'formats': [{'name': 'Vinyl', 'qty': '1'}],
                   'tracklist': [_heading('Side A'), _track('A1'), _track('A2'),
                                 _heading('Side B'), _track('B1'), _track('B2')]}
        self.assertEqual([len(d['tracks']) for d in dm.parse_discs(release)], [4])
        self.assertEqual(dm.release_disc_count(release), 1)

    def test_vinyl_side_positions(self):
        release = {'formats': [{'name': 'Vinyl', 'qty': '2'}],
                   'tracklist': [_track('A1'), _track('B1'), _track('C1'), _track('D1')]}
        self.assertEqual(len(dm.parse_discs(release)), 1)
        self.assertEqual(dm.release_disc_count(release), 2)

    def test_double_lp_side_headings_kept(self):
        # no heading names a disc: the side split is left as it was
        release = {'formats': [{'name': 'Vinyl', 'qty': '2'}],
                   'tracklist': [_heading('Side A'), _track('A1'), _heading('Side B'), _track('B1'),
                                 _heading('Side C'), _track('C1'), _heading('Side D'), _track('D1')]}
        self.assertEqual(len(dm.parse_discs(release)), 4)

    def test_disc_headings_matching_formats(self):
        release = {'formats': [{'name': 'CD', 'qty': '2'}],
                   'tracklist': [_heading('Album One'), _track(1), _track(2),
                                 _heading('Album Two'), _track(1)]}
        discs = dm.parse_discs(release)
        self.assertEqual([d['title'] for d in discs], ['Album One', 'Album Two'])
        self.assertEqual(dm.release_disc_count(release), 2)

    def test_position_prefixes(self):
        release = {'formats': [{'name': 'CD', 'qty': '2'}],
                   'tracklist': [_heading('CD 1'), _heading('Side A'), _track('1-1'),
                                 _track('1-2'), _track('2-1')]}
        self.assertEqual([len(d['tracks']) for d in dm.parse_discs(release)], [2, 1])

    def test_no_formats(self):
        release = {'tracklist': [_heading('CD 1'), _heading('Part I'), _track(1),
                                 _heading('Part II'), _track(2), _heading('CD 2'), _track(1)]}
        self.assertEqual(len(dm.parse_discs(release)), 3)


if __name__ == '__main__':
    unittest.main()
