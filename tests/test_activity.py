"""discogs_activity: an album is busy while any of its tasks is open, through a chain of
requests, and waits for you while its picker is queued.

Run: python3 tests/test_activity.py"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import discogs_activity as a  # noqa: E402


class Album:
    pass


class Activity(unittest.TestCase):
    def setUp(self):
        a._ALBUMS.clear()
        self.calls = []
        a.LISTENERS[:] = [lambda: self.calls.append(1)]

    def tearDown(self):
        a.LISTENERS[:] = []
        a._ALBUMS.clear()

    def test_busy_until_the_last_task_closes(self):
        album = Album()
        t = a.start(album, 'reading your folder', 'Tool — Lateralus')
        self.assertEqual(a.state_of(album), 'busy')
        t.stage('reading your scans (OCR)')
        self.assertEqual(a.summary(), (1, 0, 'Tool — Lateralus: reading your scans (OCR)'))
        t.done()
        t.done()                                  # (once only)
        self.assertEqual(a.state_of(album), '')
        self.assertEqual(a.summary()[:2], (0, 0))
        self.assertTrue(self.calls)

    def test_a_chain_of_requests_stays_busy(self):
        album, seen, out = Album(), [], []

        def second(*_):
            seen.append(a.state_of(album))

        def first(*_):
            out.append(a.wrap(album, 'asking Discogs', second))   # (the next request, sent from the answer)
        a.wrap(album, 'asking Discogs', first)()
        self.assertEqual(a.state_of(album), 'busy')     # (the second request still out)
        out[0]()                                       # (its answer)
        self.assertEqual(seen, ['busy'])
        self.assertEqual(a.state_of(album), '')

    def test_a_failing_callback_still_closes(self):
        album = Album()

        def boom(*_):
            raise ValueError
        with self.assertRaises(ValueError):
            a.wrap(album, 'asking Discogs', boom)()
        self.assertEqual(a.state_of(album), '')

    def test_waiting_for_you(self):
        album = Album()
        a.waiting(album, True, 'The Holy Flesh — Emissary and Vessel')
        self.assertEqual(a.state_of(album), 'waiting')
        t = a.start(album, 'asking Discogs')
        self.assertEqual(a.state_of(album), 'busy')
        t.done()
        self.assertEqual(a.state_of(album), 'waiting')
        self.assertEqual(a.summary()[:2], (0, 1))
        a.waiting(album, False)
        self.assertEqual(a.state_of(album), '')

    def test_gone_and_stale_are_forgotten(self):
        gone, stale = Album(), Album()
        a.start(gone, 'asking Discogs')
        t = a.start(stale, 'asking Discogs')
        a._ALBUMS[id(stale)]['tasks'][t._n][1] -= a.STALE_S + 1
        self.assertTrue(a.prune(lambda album: album is not gone))
        self.assertEqual((a.state_of(gone), a.state_of(stale)), ('', ''))

    def test_no_album_nothing_shown(self):
        self.assertIsNone(a.start(None, 'x'))
        self.assertEqual(a.wrap(None, 'x', lambda: 5)(), 5)


if __name__ == '__main__':
    unittest.main()
