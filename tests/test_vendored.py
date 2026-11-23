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


def _undefined_names(path: Path) -> list:
    """Names a module loads that nothing defines (a closure the vendoring tool cut short)."""
    import ast
    import builtins
    tree = ast.parse(path.read_text())
    top = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                top |= {x.id for x in ast.walk(t) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name):
            top.add(n.target.id)
        for x in ast.walk(n):
            if isinstance(x, (ast.Import, ast.ImportFrom)) and (n is x or isinstance(n, (ast.Try, ast.If))):
                top |= {(a.asname or a.name).split(".")[0] for a in x.names}
    bad = set()
    for n in tree.body:
        bound = set()
        for x in ast.walk(n):
            if isinstance(x, ast.Name) and not isinstance(x.ctx, ast.Load):
                bound.add(x.id)
            elif isinstance(x, ast.arg):
                bound.add(x.arg)
            elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(x.name)
            elif isinstance(x, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in x.names}
            elif isinstance(x, ast.ExceptHandler) and x.name:
                bound.add(x.name)
        for x in ast.walk(n):
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in top | bound \
                    and not hasattr(builtins, x.id) and x.id != "__file__":
                bad.add(x.id)
    return sorted(bad)


class Generated(unittest.TestCase):
    def test_no_undefined_names(self):
        for name in ("tm_readers.py", "tm_match_core.py", "tm_ocr.py", "tm_web.py",
                     "tm_album_artist.py", "tm_album_artist_deps.py", "tm_editions.py",
                     "tm_vinylrip.py", "tm_marks.py"):
            self.assertEqual(_undefined_names(ROOT / name), [], name)

    def test_tagmender_state_never_read(self):
        # the plugin reads no TagMender job history (TagMender's state folder is not used)
        self.assertFalse(tr.STATE_DIR.exists())
        self.assertIn("nonexistent", str(tr.STATE_DIR))


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
