#!/usr/bin/env python3
"""Copy TagMender's folder reader, evidence readers and pressing scorer into this plugin.

    python3 tools/vendor_tagmender.py [--tagmender ~/Documents/Github/tagmender] [--check]

Writes the generated files below. Never edit one by hand: change TagMender, then run this again.

* tm_match_core.py - TagMender's match_core.py, unchanged apart from a header. It is pure
  (standard library only): the candidate scorer (score_candidate, classify), clue weights,
  catalogue-number and barcode rules, folder-name readers and the pressing choice.
* tm_readers.py    - the functions named in ROOTS (TagMender's read_album and the readers
  beside it) and every module-level name they reach, from match_albums.py and the few other
  modules it calls into (tagio, exclusions, engine), in one namespace. "mc.fold" stays a call
  into tm_match_core; "ma.rip_logs", "tagio.read_only_mutagen_file", "ex.read_meta" and a
  function's own "from engine import is_disc_dir" become plain names of tm_readers.
  A name the closure reaches in a module not listed in MODULES stops the run, unless STUBS
  stands in for it.
* tm_ocr.py        - the same, from OCR_ROOTS: TagMender's scan and document reading (barcodes
  decoded off the scans, tesseract's page text, spines, the barcode's digit band, disc rings,
  PDF booklets). It never runs inside Picard: discogs_ocr_helper.py runs it on your system's
  Python, where PIL, pyzbar, numpy and tesseract are installed.
* tm_ocr_order.json - TagMender's shipped scan order (assets/ocr_order.json), which tm_ocr reads.

--check exits 1 when either file would change (tests/test_vendored.py runs it).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# alias used in TagMender's sources -> file. Output order is this order: a module-level
# statement may only use names of modules above it.
MODULES = {
    "ex": "exclusions.py",
    "tagio": "tagio.py",
    "companions": "companions.py",
    "ocr_order": "ocr_order.py",
    "ma": "match_albums.py",
    "engine": "engine.py",
    "web": "web_tag.py",
    "comp": "compilations.py",
    "cls": "classical.py",
    "naming": "naming.py",
    "vinylrip": "vinylrip.py",
    "marks": "marks.py",
    "plan": "plan.py",
}
# other spellings of the same modules in TagMender's imports
ALIASES = {"exclusions": "ex", "match_albums": "ma", "engine": "engine", "tagio": "tagio",
           "companions": "companions", "ocr_order": "ocr_order", "web_tag": "web",
           "compilations": "comp", "classical": "cls", "naming": "naming", "vinylrip": "vinylrip",
           "marks": "marks", "plan": "plan"}

# what the plugin calls (through discogs_evidence.py); everything they reach comes along
ROOTS = {
    "ma": [
        "read_album", "with_reading",                   # the folder read as TagMender reads it
        "folder_reading", "rip_logs", "log_tocs", "cue_tocs", "cdtoc_tag_toc", "mb_discid",
        "ctdb_toc", "rip_log_year", "sacd_marks", "vinyl_marks", "enhanced_marks",
        "scan_evidence", "packaging_evidence", "notes_text", "codes_from_notes",
        # the folder's documents as text, without OCR (notes, saved pages, a PDF's own text when
        # pdftotext is there); with OCR the helper reads them (tm_ocr)
        "documents_text", "album_documents",
        "discogs_candidate", "discogs_format_names", "mb_linked_discogs_ids",
        # finding and fetching Discogs candidates; disc IDs (over the plugin's http bridge)
        "discogs_search", "with_discogs_ids", "toc_releases", "toc_discogs_order", "_discid_url",
        "AuthError",
        # merging what the OCR helper read (discogs_ocr.merge_scans): pure text work
        "split_documents", "drop_file_name_lines", "scan_catnos", "labels_from_text",
    ],
}

# the web sources (discogs_web): TagMender's web_tag.py - Deezer, iTunes, Bandcamp, Wikipedia,
# Wikidata - and the tags it writes from a proven web album
WEB_ROOTS = {
    "web": [
        "deezer", "itunes", "bandcamp", "bandcamp_album", "wikipedia", "SOURCES", "chosen_sources",
        "album_candidates", "tags_for", "genre_list", "physical_copy", "track_artists",
        "wikidata_artist_ids", "artist_ids",
    ],
}

# what TagMender's album_artist.py (copied whole as tm_album_artist.py) takes from its neighbours
AA_DEP_ROOTS = {"comp": ["is_various", "is_single_compilation", "discogs_hints", "FREE_FORM", "EXTRA_FLAGS",
                         "HIDDEN_VA", "HIDDEN_COMPILATION"],
                "cls": ["genre_kind"]}
# the edition in the album title (naming.edition, ED-1..7) and ED-6's generic version mark
EDITION_ROOTS = {"naming": ["edition", "edition_name", "edition_parts", "EDITIONS"],
                 "plan": ["_GENERIC_VERSION_MARK"]}
# a vinyl rip's own comment line (VIN-4)
VINYLRIP_ROOTS = {"vinylrip": ["describe", "comment_line", "merged_values", "resolution_of", "resolution_name",
                               "own_line", "PREFIX"]}
# Grouping marks: Vinyl Rip, Tape Rip, HD Audio (MED-6, MED-8)
MARKS_ROOTS = {"marks": ["of_album", "is_hd", "HD", "VINYL", "TAPE"]}
# album_artist.py's own imports of them, and what they become
AA_IMPORTS = (
    ("import compilations\n",
     "try:\n    from . import tm_album_artist_deps as compilations\n"
     "except ImportError:  # run outside Picard (tests)\n    import tm_album_artist_deps as compilations\n"),
    ("    import classical as _cls\n", "    _cls = compilations  # (vendored: genre_kind is in tm_album_artist_deps)\n"),
)


def _album_artist(root: Path, hdr: str) -> str:
    """TagMender's album_artist.py as it is, its two imports pointed at tm_album_artist_deps."""
    text = (root / "album_artist.py").read_text()
    for old, new in AA_IMPORTS:
        if old not in text:
            raise SystemExit(f"vendor_tagmender: album_artist.py no longer has {old.strip()!r}")
        text = text.replace(old, new)
    future = "from __future__ import annotations\n"
    if future not in text:
        raise SystemExit("vendor_tagmender: album_artist.py no longer imports annotations from __future__")
    # (one docstring may come before a __future__ import: the header's; TagMender's own follows)
    return hdr + "# flake8: noqa\n" + future + text.replace(future, "", 1)


# what discogs_ocr_helper.py calls, on your system's Python (not Picard's)
OCR_ROOTS = {
    "ma": [
        "list_images", "images_key", "scan_key", "new_scan_log", "each_scan", "decoded_barcodes",
        "ocr_text", "ocr_spines", "ocr_barcode_digits", "ocr_disc_rings", "ocr_langs", "page_kind",
        "documents_text", "album_documents", "OCR_VERSION",
    ],
}

# modules TagMender's code reaches that the plugin must not (or cannot) load, and what stands in
STUBS = {
    "stagetimes": (
        "class stagetimes:  # stand-in: TagMender's per-stage timing, not kept here\n"
        "    import contextlib as _c\n"
        "    stage = staticmethod(lambda *_a, **_k: stagetimes._c.nullcontext())\n"),
    "requirements": (
        "class requirements:  # stand-in: TagMender's optional-tool checks\n"
        "    @staticmethod\n"
        "    def have(name):\n"
        "        return shutil.which(name) is not None\n"),
}
# module-level names of a vendored module that are replaced, not copied
OVERRIDES = {
    # never TagMender's own state folder: the plugin reads no TagMender job history
    # (tagmender_originals / tagmender_decided then find nothing, as for an album it never tagged)
    ("ex", "STATE_DIR"): 'STATE_DIR = Path("/nonexistent/tagmender-state-not-read-by-the-plugin")',
    # TagMender's shipped scan order, copied beside the generated module
    ("ocr_order", "SHIPPED"): 'SHIPPED = Path(__file__).resolve().parent / "tm_ocr_order.json"',
    # a vinyl rip's notes and scans: the folder's notes as tm_readers reads them; the scans' text
    # comes from the OCR helper (discogs_ocr), passed to describe() - never read here
    ("vinylrip", "notes_of"): (
        'def notes_of(folder) -> str:\n'
        '    """The rip notes beside the music (tm_readers.notes_text: .txt, .nfo, .info, .md)."""\n'
        '    try:\n'
        '        try:\n'
        '            from . import tm_readers as _tr\n'
        '        except ImportError:  # run outside Picard (tests)\n'
        '            import tm_readers as _tr\n'
        '        return _tr.notes_text(Path(str(folder)))\n'
        '    except Exception:\n'
        '        return ""'),
    ("vinylrip", "scan_text"): (
        'def scan_text(paths) -> str:\n'
        '    """The scans\' text comes from the OCR helper (passed to describe as scans=)."""\n'
        '    return ""'),
}

STDLIB = {
    "__future__", "argparse", "ast", "base64", "bisect", "collections", "contextlib", "contextvars",
    "copy", "csv", "dataclasses", "datetime", "difflib", "fnmatch", "functools", "glob", "hashlib",
    "heapq", "html", "http", "io", "itertools", "json", "logging", "marshal", "math", "mimetypes",
    "os", "pathlib", "queue", "random", "re", "shlex", "shutil", "signal", "sqlite3", "statistics",
    "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "traceback", "typing", "unicodedata", "urllib", "uuid", "zipfile", "zlib",
    "Counter", "Path", "contextmanager", "dataclass", "field", "OrderedDict", "defaultdict",
    "mutagen",          # (Picard ships it)
    "concurrent",
}
# imports TagMender guards (try/except ImportError, or behind a guarded entry point): allowed,
# and absent inside Picard's Flatpak - the code that needs them returns nothing there
OPTIONAL = {"PIL", "numpy", "pypdf", "pyzbar"}
STDLIB |= OPTIONAL

HEADER = '''"""GENERATED by tools/vendor_tagmender.py from TagMender {version} (git {head}).
Sources: {sources}.
Do not edit: change TagMender and run the tool again.

Copyright (C) the TagMender authors. GPL-2.0-or-later, as this plugin.
"""
'''


def _git_head(root: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "?"


def _version(root: Path) -> str:
    m = re.search(r"VERSION\s*=\s*[\"']([^\"']+)", (root / "version.py").read_text())
    return m.group(1) if m else "?"


class Module:
    def __init__(self, alias: str, path: Path):
        self.alias, self.path = alias, path
        self.src = path.read_text()
        self.lines = self.src.splitlines()
        self.tree = ast.parse(self.src)
        self.top: dict[str, list[ast.stmt]] = {}
        self.imports: dict[str, str] = {}        # local name -> module alias it stands for
        for node in self.tree.body:
            for n in self._binds(node):
                self.top.setdefault(n, []).append(node)
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.imports[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]

    @staticmethod
    def _binds(node) -> list[str]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [node.name]
        if isinstance(node, ast.Assign):
            return [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            return [node.target.id]
        return []

    def segment(self, node) -> tuple[int, int]:
        start = (node.decorator_list[0].lineno if getattr(node, "decorator_list", None) else node.lineno) - 1
        while start > 0 and self.lines[start - 1].lstrip().startswith("#"):
            start -= 1                           # (the comments right above a definition are its own)
        return start, node.end_lineno


def _locals(node) -> set[str]:
    """Names a function (or class) binds itself - its arguments, assignments, loop, with,
    except and comprehension targets, imports and nested definitions - which a load of the same
    name inside it means, not the module's. Empty for a module-level statement."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return set()
    out = set()
    for x in ast.walk(node):
        if x is node:
            continue
        if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
            out.add(x.id)
        elif isinstance(x, ast.arg):
            out.add(x.arg)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(x.name)
        elif isinstance(x, ast.ExceptHandler) and x.name:
            out.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in x.names}
    # (a name the function declares global is the module's)
    for x in ast.walk(node):
        if isinstance(x, ast.Global):
            out -= set(x.names)
    return out


def _canon(mod_name: str) -> str | None:
    """The MODULES alias a TagMender import name stands for, or None."""
    return ALIASES.get(mod_name) if ALIASES.get(mod_name) in MODULES else None


def build(root: Path) -> dict[str, str]:
    mods = {a: Module(a, root / f) for a, f in MODULES.items()}
    core = (root / "match_core.py").read_text()
    head, version = _git_head(root), _version(root)
    sources = ", ".join(f"{f} @ {hashlib.sha1((root / f).read_bytes()).hexdigest()[:10]}"
                        for f in ["match_core.py", *MODULES.values(), "album_artist.py"])
    hdr = HEADER.format(version=version, head=head, sources=sources)
    return {"tm_match_core.py": hdr + core,
            "tm_readers.py": _closure(mods, ROOTS, hdr),
            "tm_ocr.py": _closure(mods, OCR_ROOTS, hdr),
            "tm_web.py": _closure(mods, WEB_ROOTS, hdr),
            "tm_album_artist.py": _album_artist(root, hdr),
            "tm_album_artist_deps.py": _closure(mods, AA_DEP_ROOTS, hdr),
            "tm_editions.py": _closure(mods, EDITION_ROOTS, hdr),
            "tm_vinylrip.py": _closure(mods, VINYLRIP_ROOTS, hdr),
            "tm_marks.py": _closure(mods, MARKS_ROOTS, hdr),
            "tm_ocr_order.json": (root / "assets" / "ocr_order.json").read_text()}


def _closure(mods: dict, roots: dict, hdr: str) -> str:
    """One generated module: ROOTS and every module-level name they reach."""
    want = [(a, n) for a, names in roots.items() for n in names]
    seen: set[tuple[str, str]] = set()
    used_stubs: set[str] = set()
    problems: list[str] = []
    while want:
        alias, name = want.pop()
        if (alias, name) in seen:
            continue
        mod = mods[alias]
        if name not in mod.top:
            problems.append(f"{name!r} is not defined at the top of {MODULES[alias]}")
            continue
        seen.add((alias, name))
        if (alias, name) in OVERRIDES:
            continue
        for node in mod.top[name]:
            local = _locals(node)
            for x in ast.walk(node):
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                    if x.id in local:
                        continue                 # (a function's own variable: "marks = {}")
                    if x.id in mod.top:
                        want.append((alias, x.id))
                    elif x.id in mod.imports:
                        target = mod.imports[x.id]
                        if target in STUBS:
                            used_stubs.add(target)
                        elif _canon(target) is None and target not in STDLIB and target != "match_core":
                            problems.append(f"{MODULES[alias]}:{name} uses {target} (not vendored, no stub)")
                elif isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) \
                        and x.value.id not in local:          # (a variable, "marks = {}": not the module)
                    target = mod.imports.get(x.value.id)
                    other = _canon(target) if target else None
                    if other:
                        want.append((other, x.attr))
                elif isinstance(x, ast.ImportFrom) and x.level == 0 and x.module:
                    other = _canon(x.module)
                    if other:
                        want += [(other, a.name) for a in x.names]
                    elif x.module in STUBS:
                        used_stubs.add(x.module)
                    elif x.module.split(".")[0] not in STDLIB and x.module != "match_core":
                        problems.append(f"{MODULES[alias]}:{name} imports from {x.module} (not vendored)")
                elif isinstance(x, ast.Import):
                    for a in x.names:
                        top = a.name.split(".")[0]
                        if top in STUBS:
                            used_stubs.add(top)
                        elif _canon(top) is None and top not in STDLIB and top != "match_core":
                            problems.append(f"{MODULES[alias]}:{name} imports {top} (not vendored)")
    if problems:
        raise SystemExit("vendor_tagmender:\n  " + "\n  ".join(sorted(set(problems))))

    # one namespace: a name reached in two modules must be the same statement text
    owner: dict[str, str] = {}
    for alias, name in sorted(seen):
        if name in owner and owner[name] != alias:
            a, b = (mods[owner[name]], mods[alias])
            ta = "\n".join(ast.unparse(n) for n in a.top[name])
            tb = "\n".join(ast.unparse(n) for n in b.top[name])
            alias_stmt = re.fullmatch(rf"{name} = \w+\.{name}", tb) or re.fullmatch(rf"{name} = \w+\.{name}", ta)
            if ta != tb and not alias_stmt:
                raise SystemExit(f"vendor_tagmender: {name!r} is defined differently in "
                                 f"{MODULES[owner[name]]} and {MODULES[alias]}")
            # ("volume_number = ma.volume_number" in engine: the real definition is the one kept)
            if re.fullmatch(rf"{name} = \w+\.{name}", ta):
                owner[name] = alias
            continue
        owner[name] = alias

    vendored = {a for a in MODULES}
    blocks = []
    for alias in MODULES:
        mod = mods[alias]
        nodes = {id(n): n for (a, name) in seen if a == alias and owner.get(name) == alias
                 and (a, name) not in OVERRIDES for n in mod.top[name]}
        local_aliases = {k for k, v in mod.imports.items() if _canon(v) in vendored}
        for node in sorted(nodes.values(), key=lambda n: n.lineno):
            s, e = mod.segment(node)
            text = "\n".join(mod.lines[s:e])
            # "ma.rip_logs(" -> "rip_logs(": one namespace now
            for la in local_aliases - _locals(node):    # (never a variable of that name: "marks = []")
                text = re.sub(rf"(?<![\w.]){re.escape(la)}\.(?=[A-Za-z_])", "", text)
            # a function's own "from engine import a, b": those are names of this module now
            text = re.sub(r"(?m)^(\s*)from (?:" + "|".join(map(re.escape, ALIASES)) + r") import [^\n]+$",
                          r"\1pass  # (vendored: the names it imported are this module's)", text)
            text = re.sub(r"(?m)^(\s*)import (?:" + "|".join(map(re.escape, ALIASES)) + r")(?: as \w+)?\s*$",
                          r"\1pass  # (vendored)", text)
            # "volume_number = ma.volume_number" became "volume_number = volume_number": dropped
            if re.fullmatch(r"(\w+) = \1\s*(#.*)?", text.strip()):
                continue
            blocks.append(f"# --- {MODULES[alias]}:{node.lineno}\n{text}")
        for (a, name), stmt in OVERRIDES.items():
            if a == alias and (a, name) in seen:
                blocks.append(f"# --- {MODULES[alias]} {name} (overridden by vendor_tagmender.py)\n{stmt}")

    return (hdr + "# flake8: noqa\nfrom __future__ import annotations\n\n"
               + _imports_for("\n".join(blocks), mods, used_stubs) + "\n\n"
               "try:\n    from . import tm_match_core as mc\nexcept ImportError:  # run outside Picard (tests)\n"
               "    import tm_match_core as mc\n\n"
               + "".join(STUBS[m] + "\n" for m in sorted(used_stubs)) + "\n"
               + "\n\n\n".join(blocks) + "\n")


def _imports_for(body: str, mods: dict, used_stubs: set) -> str:
    """The module-level imports (standard library and mutagen only) of the vendored modules that
    BODY uses. Never a TagMender module: those are this module's own names, mc, or a stub."""
    loaded = {x.id for x in ast.walk(ast.parse(body)) if isinstance(x, ast.Name)}
    out = []
    for mod in mods.values():
        for node in mod.tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound = a.asname or a.name.split(".")[0]
                    if bound in loaded and a.name.split(".")[0] in STDLIB - OPTIONAL:
                        out.append(f"import {a.name}" + (f" as {a.asname}" if a.asname else ""))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                    and node.module.split(".")[0] in STDLIB - OPTIONAL:
                names = [a for a in node.names if (a.asname or a.name) in loaded]
                if names:
                    out.append(f"from {node.module} import " + ", ".join(
                        a.name + (f" as {a.asname}" if a.asname else "") for a in names))
    return "\n".join(dict.fromkeys(sorted(out, key=lambda l: (l.startswith("from"), l))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tagmender", default=str(Path.home() / "Documents/Github/tagmender"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    files = build(Path(args.tagmender).expanduser())
    changed = [n for n, text in files.items()
               if not (HERE / n).exists() or (HERE / n).read_text() != text]
    if args.check:
        for n in changed:
            print(f"{n} is out of date with TagMender (run tools/vendor_tagmender.py)", file=sys.stderr)
        return 1 if changed else 0
    for n in changed:
        (HERE / n).write_text(files[n])
        print(f"wrote {n} ({files[n].count(chr(10))} lines)")
    if not changed:
        print("already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
