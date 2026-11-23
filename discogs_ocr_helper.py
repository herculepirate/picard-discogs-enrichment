#!/usr/bin/env python3
"""Read an album folder's scans and documents as TagMender reads them; print JSON.

Runs on your system's Python, never inside Picard: Picard's Flatpak has no PIL, pyzbar, numpy
or tesseract. discogs_ocr_bridge.py starts it (through flatpak-spawn --host when Picard is a
Flatpak) and reads what it prints.

    python3 discogs_ocr_helper.py --probe
        {"ok": true|false, "missing": [...], "optional_missing": [...], "langs": "eng+..."}
    python3 discogs_ocr_helper.py --album DIR [--max-images 15] [--cache FILE] [--have-barcode]
        {"ok": true, "images": n, "ocr_barcodes": [...], "docs_text": "...", "page_text": "...",
         "spines": "...", "digits": "...", "rings": "...", "complete": bool, ...}

Nothing is sent anywhere: it reads the folder, runs tesseract and zbar, and keeps what it read
in the cache file (sqlite) so a scan is read once.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))      # (run with -I: the script's folder is not on the path)

# what reading the scans needs; without the first group nothing is read
REQUIRED = {
    "tesseract": "the tesseract program (tesseract-ocr)",
    "PIL": "Pillow (python3-pil)",
}
OPTIONAL = {
    "pyzbar": "pyzbar and zbar (python3-pyzbar): barcodes decoded off the scans",
    "numpy": "numpy (python3-numpy): the barcode's digit band and disc rings",
    "pdftotext": "pdftotext (poppler-utils): PDF booklets' own text",
}


def _have(name: str) -> bool:
    if name in ("tesseract", "pdftotext"):
        return shutil.which(name) is not None
    try:
        if name == "pyzbar":
            from pyzbar import pyzbar  # noqa: F401  (loads the zbar library too)
        else:
            __import__(name)
        return True
    except Exception:
        return False


def probe() -> dict:
    missing = [desc for name, desc in REQUIRED.items() if not _have(name)]
    optional = [desc for name, desc in OPTIONAL.items() if not _have(name)]
    out = {"ok": not missing, "missing": missing, "optional_missing": optional,
           "python": sys.version.split()[0]}
    if not missing:
        import tm_ocr
        out["langs"] = tm_ocr.ocr_langs()
        out["version"] = tm_ocr.OCR_VERSION
    return out


def _joined(readings) -> str:
    return "\n".join(r for r in readings if isinstance(r, str) and r.strip())


# The small print: a page whose text tesseract read poorly (fewer than SMALL_PRINT_WORDS words)
# is read again in overlapping tiles, its print's halftone dots smoothed out and enlarged. A
# busy back cover (a barcode, a grey panel, columns of credits) defeats tesseract's own page
# layout; a tile of it does not - the copyright line "(P) 1994 Intermusic S.A." at the foot of
# a scan tells a 1994 pressing from a 1999 one.
SMALL_PRINT_KIND = "small-print-1"
SMALL_PRINT_WORDS = 40
SMALL_PRINT_GRID = (4, 3)            # rows, columns
SMALL_PRINT_OVERLAP = 0.04           # of the page's size, so no line is cut in every tile
SMALL_PRINT_MAX_SIDE = 4000          # enlarged up to 2x, never past this
SMALL_PRINT_BUDGET_S = 120.0


def _words(text) -> int:
    import re
    return len(re.findall(r"[^\W\d_]{3,}", text or ""))


def small_print(path: str) -> str:
    """One scan's text read tile by tile (see SMALL_PRINT_KIND)."""
    from PIL import Image, ImageFilter, ImageOps
    import tm_ocr as to
    with Image.open(path) as im:
        g = ImageOps.grayscale(ImageOps.exif_transpose(im))
    g = g.filter(ImageFilter.MedianFilter(3))
    scale = min(2.0, SMALL_PRINT_MAX_SIDE / max(g.size))
    if scale > 1.05:
        g = g.resize((round(g.width * scale), round(g.height * scale)), Image.LANCZOS)
    rows, cols = SMALL_PRINT_GRID
    w, h = g.size
    ox, oy = round(w * SMALL_PRINT_OVERLAP), round(h * SMALL_PRINT_OVERLAP)
    out = []
    for r in range(rows):
        for c in range(cols):
            box = (max(0, w * c // cols - ox), max(0, h * r // rows - oy),
                   min(w, w * (c + 1) // cols + ox), min(h, h * (r + 1) // rows + oy))
            out.append(to._tess(g.crop(box), 6, to.ocr_langs()) or "")
    return "\n".join(t.strip() for t in out if t.strip())


def read_album(album_dir: str, max_images: int, cache: str | None, have_barcode: bool) -> dict:
    """TagMender's scan reading for one album (match_albums' _process, the OCR part): every
    scan's barcodes and page text, spines, the digit band when no barcode is known, disc rings,
    and the folder's documents (PDF pages OCR'd when they carry no text of their own)."""
    import tm_ocr as to
    started = time.time()
    base = Path(album_dir)
    db = None
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(cache, timeout=30)
    try:
        log = to.new_scan_log()
        inventory: dict = {}
        images, _key = to.list_images(base, max_images, inventory)
        found = set(to.decoded_barcodes(db, images, log)) if images and _have("pyzbar") else set()
        page = spines = digits = rings = ""
        if images:
            pages = to.each_scan(db, to.page_kind("text"), images, lambda p: to.ocr_text([p]), log)
            poor = [p for p, t in zip(images, pages) if _words(t) < SMALL_PRINT_WORDS]
            small = to.each_scan(db, SMALL_PRINT_KIND, poor, small_print, log,
                                 budget_s=SMALL_PRINT_BUDGET_S) if poor else []
            page = _joined(list(pages) + list(small))
            spines = _joined(to.each_scan(db, to.page_kind("spines"), images,
                                          lambda p: to.ocr_spines([p]), log))
            if not (found or have_barcode) and _have("numpy"):
                digits = _joined(to.each_scan(db, "barcode-digits", images[:15],
                                              lambda p: to.ocr_barcode_digits([p]), log, budget_s=120.0))
            if _have("numpy"):
                rings = _joined(to.each_scan(db, "disc-rings-4", images[:40],
                                             lambda p: to.ocr_disc_rings([p]), log, budget_s=60.0))
        docs = to.documents_text(base, ocr=True, db=db)
        unread = sorted(str(x) for x in log.get("unread") or ())
        return {
            "ok": True, "version": to.OCR_VERSION, "langs": to.ocr_langs(),
            "images": len(images), "image_names": [Path(p).name for p in images],
            "inventory": {k: v for k, v in inventory.items() if isinstance(v, (int, str))},
            "ocr_barcodes": sorted(found), "docs_text": docs,
            "documents": len(to.album_documents(base)) if docs.strip() else 0,
            "page_text": page, "spines": spines, "digits": digits, "rings": rings,
            "unread": unread,
            # TagMender's scans_complete: every image read, none left out or unreadable
            "complete": bool(images and not unread and not inventory.get("left_out")
                             and not inventory.get("unreadable")),
            "read_now": len(log.get("read") or ()), "seconds": round(time.time() - started, 1),
        }
    finally:
        if db is not None:
            db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--album")
    ap.add_argument("--max-images", type=int, default=15)
    ap.add_argument("--cache")
    ap.add_argument("--have-barcode", action="store_true")
    args = ap.parse_args(argv)
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    try:
        if args.probe:
            out = probe()
        elif args.album:
            p = probe()
            out = read_album(args.album, args.max_images, args.cache, args.have_barcode) if p["ok"] else p
        else:
            ap.error("--probe or --album")
    except Exception as exc:                    # (the plugin reads why; it never sees a traceback)
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
