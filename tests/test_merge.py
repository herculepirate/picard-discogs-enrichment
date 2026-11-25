"""discogs_mapping: what Discogs may replace of MusicBrainz's tags.

Run: python3 tests/test_merge.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discogs_mapping as dm  # noqa: E402


class MythologyMerge(unittest.TestCase):
    """Derek Sherinian - Mythology (Discogs 3144697)."""

    def test_sort_names_are_musicbrainz(self):
        for mode in (dm.MODE_OVERRIDE, dm.MODE_COMBINE, dm.MODE_DISCOGS_ONLY):
            self.assertIsNone(dm.resolve_field_value(['Sherinian, Derek'], 'Derek Sherinian', mode,
                                                     field='albumartistsort'), mode)
        self.assertEqual(dm.resolve_field_value([], 'Derek Sherinian', dm.MODE_OVERRIDE,
                                                field='artistsort'), ['Derek Sherinian'])

    def test_a_year_never_replaces_its_full_date(self):
        self.assertIsNone(dm.resolve_field_value(['2004-11-08'], '2004', dm.MODE_OVERRIDE, field='originaldate'))
        self.assertEqual(dm.resolve_field_value(['2004'], '2004-11-08', dm.MODE_OVERRIDE, field='originaldate'),
                         ['2004-11-08'])
        self.assertEqual(dm.resolve_field_value(['2004-11-08'], '1999', dm.MODE_OVERRIDE, field='date'), ['1999'])

    def test_the_barcode_is_no_catalogue_number(self):
        release = {'labels': [{'name': 'Inside Out Music', 'catno': 'IOMCD 191'},
                              {'name': 'Inside Out Music', 'catno': 'IOMACD 2099'},
                              {'name': 'Inside Out Music', 'catno': '6 93723 40582 7'}],
                   'identifiers': [{'type': 'Barcode', 'value': '693723405827'}]}
        fields = dm.extract_discogs_fields(release)
        self.assertEqual(fields['catalognumber'], ['IOMCD 191', 'IOMACD 2099'])


if __name__ == '__main__':
    unittest.main()
