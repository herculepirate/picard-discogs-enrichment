"""What the OCR helper read of your scans and documents, added to the album as TagMender adds it.

merge_scans() is match_albums' _process from "documents_text" to the scan search, minus the
searching itself (discogs_decide runs the probe it returns): the barcodes decoded off the scans
become the album's when its files carry none; the page text, spines, digit band and disc rings
are the OCR text every candidate is scored against; a document's typed text and the copy's
printed text are told apart (a barcode only typed in notes proves nothing); the barcodes and
catalogue numbers printed on the scans that the first search did not ask are searched.

Pure Python: no Qt, no Picard, no OCR (that ran in discogs_ocr_helper.py).
"""
from __future__ import annotations

try:
    from . import tm_match_core as mc
    from . import tm_readers as tr
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_readers as tr


def _txt(v) -> str:
    return v if isinstance(v, str) else ''


def merge_scans(album: dict, album_dir: str, scans: dict | None) -> dict:
    """Add SCANS (the helper's JSON; None or not ok: the documents are read here, without OCR)
    to ALBUM. Returns {"text": the OCR text to score with, "probe": the album dict to search the
    new codes with (or None), "codes": the new codes, "barcodes_from_scans": [...]}."""
    album.setdefault('barcodes', set())
    if not isinstance(album['barcodes'], set):
        album['barcodes'] = set(album['barcodes'])
    ok = bool(scans and scans.get('ok'))
    out = {'text': '', 'probe': None, 'codes': [], 'barcodes_from_scans': []}
    if ok:
        found = {str(b) for b in scans.get('ocr_barcodes') or ()}
        album['ocr_barcodes'] = found                    # decoded off the scans
        if found and not album['barcodes']:
            album['barcodes'] = set(found)
            out['barcodes_from_scans'] = sorted(found)
        album['scan_barcodes'] = set(found)
        album['scans_complete'] = bool(scans.get('complete'))
        docs_txt = _txt(scans.get('docs_text'))
    else:
        try:
            docs_txt = tr.documents_text(album_dir, ocr=False)
        except Exception:
            docs_txt = ''
    page = _txt(scans.get('page_text')) if ok else ''
    cover_txt = album.get('cover_text') or ''
    if not (page.strip() or docs_txt.strip() or cover_txt.strip()):
        return out

    typed_docs, printed_docs = tr.split_documents(docs_txt, album.get('foreign_cues'))
    album['typed_text'] = typed_docs
    album['printed_text'] = printed_docs + '\n' + page
    text = tr.drop_file_name_lines(docs_txt + '\n' + page, album_dir).strip()
    for key in ('spines', 'digits', 'rings'):
        part = _txt(scans.get(key)) if ok else ''
        if part.strip():
            text = text + '\n' + part
            album['printed_text'] += '\n' + part
            if key == 'rings':
                album['ring_text'] = part                # matrix codes: a conflict is told here
    out['text'] = text
    if not (text.strip() or cover_txt.strip()):
        return out

    codes = mc.codes_from_ocr_text((text + '\n' + cover_txt).strip())
    known_b = {mc.barcode_key(b) for b in album['barcodes']}
    known_c = {mc.norm_code(c) for c in album.get('catno_search', [])}
    new_b = [b for b in codes['barcodes'] if mc.barcode_key(b) not in known_b]
    new_c = [c for c in tr.scan_catnos(codes['catnos'], docs_txt) if mc.norm_code(c) not in known_c]
    album['scan_catnos_searched'] = list(new_c)
    labels = album.get('search_labels') or []
    new_l = [x for x in tr.labels_from_text(text) if x not in labels[:2]]
    labels = [x for x in labels if x not in new_l]
    if new_l and not new_c and not album.get('catno_search'):
        new_l = []                                       # nothing to search the label with
    if new_b or new_c or new_l:
        out['codes'] = new_b + new_c
        probe = dict(album, barcodes=set(new_b), catno_search=new_c, asins=[],
                     search_labels=(new_l + labels) if new_c else new_l, narrow_codes_first=True)
        if not new_c:
            probe['label_catnos'] = album.get('catno_search', [])
        out['probe'] = probe
        # a barcode printed whole on the scans (not one only typed in notes) is your copy's
        sure = [b for b in mc.barcodes_read_whole(text, new_b) if not mc.typed_only_barcode(album, b)]
        if not album['barcodes'] and sure:
            album['barcodes'] = set(sure)
            out['barcodes_from_scans'] = sorted(sure)
        album['scan_barcodes'] = set(album.get('scan_barcodes') or ()) | set(sure)
    album['scan_codes'] = list(out['codes'])
    return out


def scans_summary(scans: dict | None, merged: dict) -> str:
    """One log line on what the scans gave."""
    if not scans or not scans.get('ok'):
        return ''
    bits = [f"{scans.get('images', 0)} scan(s) read"
            + ('' if scans.get('complete') or not scans.get('images') else ' (not all)')]
    if scans.get('documents'):
        bits.append(f"{scans['documents']} document(s)")
    if scans.get('ocr_barcodes'):
        bits.append('barcode decoded: ' + ', '.join(scans['ocr_barcodes']))
    if merged.get('codes'):
        bits.append('codes printed: ' + ', '.join(merged['codes'][:6]))
    bits.append(f"{scans.get('seconds', 0)} s")
    return '; '.join(bits)
