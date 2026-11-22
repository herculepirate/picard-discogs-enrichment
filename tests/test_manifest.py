"""MANIFEST.toml as Picard checks it. Run: python3 tests/test_manifest.py"""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Manifest(unittest.TestCase):
    def test_picard_limits(self):
        m = tomllib.loads((ROOT / 'MANIFEST.toml').read_text())
        # Picard refuses (and rolls back) an update whose long description is longer
        self.assertLessEqual(len(m['long_description']), 2000)
        self.assertTrue(m['version'])

    def test_user_agent_names_the_version(self):
        m = tomllib.loads((ROOT / 'MANIFEST.toml').read_text())
        self.assertIn(f'PicardDiscogsEnrichment/{m["version"]} ', (ROOT / 'discogs_client.py').read_text())


if __name__ == '__main__':
    unittest.main()
