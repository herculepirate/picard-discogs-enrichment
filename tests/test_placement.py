"""discogs_placement: your files on their own tracks of a MusicBrainz release.

Run: python3 tests/test_placement.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discogs_placement as dp  # noqa: E402

CD, SACD = 'Hybrid SACD (CD layer)', 'Hybrid SACD (SACD layer, 2 channels)'
BOX = '/m/Jacqueline du Pré - The Great Cello Concertos (2025) [SACD]'


def release(*media):
    """[(disc, format, index)] for media given as (format, track count)."""
    out, i = [], 0
    for disc, (fmt, n) in enumerate(media, 1):
        for _ in range(n):
            out.append((disc, fmt, i))
            i += 1
    return out


def files(counts, bits=24, rate=88200, channels=2, tags=True):
    out = []
    for disc, n in enumerate(counts, 1):
        for t in range(1, n + 1):
            out.append({'path': f'{BOX}/Disc {disc}/{t}. Track {t}.flac', 'disc': disc if tags else 0,
                        'track': t if tags else 0, 'bits': bits, 'rate': rate, 'channels': channels})
    return out


class Placement(unittest.TestCase):
    GREAT_CELLO = release((CD, 9), (SACD, 9), (CD, 7), (SACD, 7), (CD, 9), (SACD, 9), (CD, 9), (SACD, 9))

    def test_hi_res_files_go_to_the_sacd_layers(self):
        plan = dp.plan_placement(self.GREAT_CELLO, files([9, 7, 9, 9]))
        self.assertIsNotNone(plan)
        self.assertIn('disc 2 -> medium 4', plan['summary'])
        # disc 2 track 1 is the first track of medium 4 (after 9 + 9 + 7 tracks)
        self.assertEqual(plan['tracks'][9], 25)
        self.assertEqual(plan['tracks'][0], 9)

    def test_cd_rips_go_to_the_cd_layers(self):
        plan = dp.plan_placement(self.GREAT_CELLO, files([9, 7, 9, 9], bits=16, rate=44100))
        self.assertEqual(plan['tracks'][0], 0)
        self.assertIn('disc 2 -> medium 3', plan['summary'])

    def test_from_folder_and_file_names(self):
        plan = dp.plan_placement(self.GREAT_CELLO, files([9, 7, 9, 9], tags=False))
        self.assertIn('disc 4 -> medium 8', plan['summary'])

    def test_not_when_a_disc_does_not_fit(self):
        self.assertIsNone(dp.plan_placement(self.GREAT_CELLO, files([9, 9, 9, 9])))
        self.assertIsNone(dp.plan_placement(self.GREAT_CELLO, files([9, 7, 9, 9, 3])))

    def test_plain_cds(self):
        plan = dp.plan_placement(release(('CD', 3), ('CD', 2)), files([3, 2], bits=16, rate=44100))
        self.assertEqual(plan['tracks'], [0, 1, 2, 3, 4])

    def test_names(self):
        self.assertEqual(dp.track_of({'path': '/x/7. Track 7.flac'}), 7)
        self.assertEqual(dp.track_of({'path': '/x/1-07 Title.flac'}), 7)
        self.assertEqual(dp.disc_of({'path': '/x/CD02/01.flac'}), 2)


if __name__ == '__main__':
    unittest.main()
