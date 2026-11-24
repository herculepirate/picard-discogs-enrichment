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


class NoShadowedImports(unittest.TestCase):
    """A function assigning a name the module imports makes that name local to the whole
    function: an earlier call of the import fails (UnboundLocalError - Search Discogs, 0.9.3)."""

    def test_no_function_assigns_an_imported_name(self):
        import ast
        for name in ('__init__.py', 'discogs_decide.py', 'discogs_copy.py', 'discogs_albumartist.py',
                     'discogs_webenrich.py', 'discogs_http_bridge.py', 'discogs_ocr.py'):
            tree = ast.parse((ROOT / name).read_text())
            imported = {a.asname or a.name.split('.')[0] for n in tree.body
                        if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
            for f in ast.walk(tree):
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assigned = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
                    self.assertFalse(assigned & imported, f'{name}:{f.lineno} {f.name} assigns {assigned & imported}')
