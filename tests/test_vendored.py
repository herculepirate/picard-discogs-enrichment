"""The code copied from TagMender. Run: python3 tests/test_vendored.py"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import tm_match_core as mc  # noqa: E402
import tm_readers as tr  # noqa: E402

TAGMENDER = Path.home() / "Documents/Github/tagmender"

EAC_LOG = """Exact Audio Copy V1.6 from 23. October 2020

EAC extraction logfile from 4. March 2022, 21:10

Testament / Demonic

Used drive  : ASUS    DRW-24F1ST   b

TOC of the extracted CD

     Track |   Start  |  Length  | Start sector | End sector
    ---------------------------------------------------------
        1  |  0:00.00 |  5:24.20 |         0    |    24319
        2  |  5:24.20 |  5:15.37 |     24320    |    47981
        3  | 10:39.57 |  4:17.25 |     47982    |    67281
"""


class UpToDate(unittest.TestCase):
    @unittest.skipUnless((TAGMENDER / "match_core.py").exists(), "TagMender checkout not found")
    def test_generated_files_match_tagmender(self):
        run = subprocess.run([sys.executable, str(ROOT / "tools/vendor_tagmender.py"), "--check",
                              "--tagmender", str(TAGMENDER)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)


class RipLogs(unittest.TestCase):
    def test_eac_log_toc_header_year(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Testament - Demonic.log").write_text(EAC_LOG)
            [log] = tr.rip_logs(d)
        # MusicBrainz TOC form: first, last, lead-out, then each start, all +150 sectors
        self.assertEqual(log["toc"], "1 3 67432 150 24470 48132")
        self.assertEqual(log["header"], ("Testament", "Demonic"))
        self.assertEqual(log["year"], 2022)       # the extraction date, never EAC's build date

    def test_disc_id_form(self):
        did = tr.mb_discid("1 3 67432 150 24470 48132")
        self.assertEqual(len(did), 28)
        self.assertTrue(did.endswith("-"))
        self.assertNotRegex(did, r"[+/=]")


class FolderNames(unittest.TestCase):
    def test_artist_album_year(self):
        self.assertEqual(tr.folder_reading("Candlemass - 1987 - Nightfall"),
                         ("Candlemass", "Nightfall", 1987))

    def test_catalogue_number_in_brackets(self):
        self.assertIn("DOMO 71063-2", mc.catno_hints("Nightfall [DOMO 71063-2]"))

    def test_year_alone_is_no_catalogue_number(self):
        self.assertEqual(mc.catno_hints("Nightfall (1987)"), [])


if __name__ == "__main__":
    unittest.main()
