"""Which Discogs release your copy is, decided as TagMender decides it.

decide() runs TagMender's Discogs side on the album TagMender's reader read (discogs_evidence):

1. disc IDs - your copy's against the release Picard matched (no requests), else MusicBrainz's
   disc ID lookup and the CUETools database (toc_releases); a MusicBrainz release carrying your
   exact disc ID passes its Discogs links on as disc-ID proof (TagMender's disc_id_discogs)
2. candidates - discogs_search (your barcodes and catalogue numbers first, then titles, then
   the master's versions), plus the Discogs releases MusicBrainz links to (with_discogs_ids)
3. scoring - note_titles, settle_title_numbers, score_candidate, code_twins, classify:
   TagMender's run_source, unchanged

and returns TagMender's verdict: "auto" (proven), "review" or "nomatch", with the scored
candidates. One rule is the plugin's own: when TagMender is unsure but the best candidate is
the release MusicBrainz links to, nothing of your copy is against it, and no other candidate
is proven by your copy, that link is kept ("linked") - the 0.3.x behaviour, never against
your copy's evidence.

`http` is any object with get_json(url, host) -> dict | None (host "discogs" or "mb") and,
optionally, get_text(url, host) -> str (host "ctdb"); in Picard it is discogs_http_bridge's.
Runs on a worker thread. Pure Python: no Qt, no Picard.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from . import tm_match_core as mc
    from . import tm_readers as tr
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_readers as tr

# ---- Discogs' compound countries ("UK & Europe", "Germany, Austria, & Switzerland") -----------
# TagMender's country_code knows one country at a time, so a pressing filed under two got no
# country clue either way: "Made in the EU" on your scans neither for nor against "UK & Europe".
# One of Europe: Europe (XE, which TagMender treats as every European country); else the first.
_COUNTRY_CODE = mc.country_code


def country_code(name):
    code = _COUNTRY_CODE(name)
    raw = str(name or '')
    if code or not re.search(r'&|,|/|\band\b', raw):
        return code
    codes = [c for c in (_COUNTRY_CODE(p) for p in re.split(r'\s*(?:&|,|/|\band\b)\s*', raw) if p.strip()) if c]
    return 'XE' if 'XE' in codes else (codes[0] if codes else '')


mc.country_code = country_code

# MusicBrainz releases (carrying your exact disc ID) whose Discogs links are read
MAX_DISCID_RELEASES = 3
# Discogs releases MusicBrainz links to that are fetched beside the search's own
MAX_LINKED = 4
# "likely": how far (confidence points) the most likely of the pressings your copy proves
# equally must lead the next, and how sure TagMender must be of it
LIKELY_MIN_LEAD = 1.0
LIKELY_MIN_CONFIDENCE = 90.0


def _cfg(overrides: dict | None = None) -> dict:
    cfg = dict(mc.DEFAULT_CFG)
    cfg.update(overrides or {})
    return cfg


def _disc_ids(album: dict, http, mb_release_id: str, mb_discids: set, linked: list) -> str:
    """Set the album's disc-ID keys TagMender's scorer reads (toc_releases, exact_toc_releases,
    toc_discogs, toc_discogs_fuzzy). Returns where your disc ID is: "matched" (on the release
    Picard matched), "other:<mbid,...>" (on other MusicBrainz releases only), "" (nowhere known,
    or no disc ID)."""
    tocs = album.get('tocs') or {}
    fit = set(album.get('tocs_fit') or ())
    own = {tr.mb_discid(t) for d, t in tocs.items() if d in fit}
    if own and own & set(mb_discids or ()):
        # proof with no request: MusicBrainz attaches your disc ID to the very release Picard
        # matched, so the Discogs releases it links to inherit it (disc_id_discogs)
        album['toc_releases'] = {mb_release_id}
        album['exact_toc_releases'] = {mb_release_id}
        album['toc_discogs'] = set(linked)
        album['toc_discogs_fuzzy'] = {}
        return 'matched'
    if not tocs:
        return ''
    album['toc_releases'] = tr.toc_releases(http, album)
    exact = sorted(album.get('exact_toc_releases') or ())
    links = set()
    album['discid_mb'] = {}
    for rid in exact[:MAX_DISCID_RELEASES]:
        rel = http.get_json(f'https://musicbrainz.org/ws/2/release/{rid}?inc=url-rels+labels+media&fmt=json', 'mb')
        own = {str(x) for x in tr.mb_linked_discogs_ids(rel or {}) if str(x).isdigit()}
        links |= own
        rel = rel or {}
        album['discid_mb'][rid] = {
            'discogs': own, 'barcode': str(rel.get('barcode') or ''),
            'catnos': [str(li.get('catalog-number') or '') for li in rel.get('label-info') or ()
                       if isinstance(li, dict) and li.get('catalog-number')],
            'formats': [str(m.get('format') or '') for m in rel.get('media') or () if isinstance(m, dict)],
        }
    if links:
        album['toc_discogs'] = set(album.get('toc_discogs') or ()) | {str(x) for x in links}
        album['toc_discogs_fuzzy'] = {k: v for k, v in (album.get('toc_discogs_fuzzy') or {}).items()
                                      if k not in links}
    if exact and mb_release_id not in exact:
        return 'other:' + ','.join(exact)
    return 'matched' if exact else ''


def other_product(s: dict | None) -> bool:
    """Your copy shows it is another product: a barcode that differs (TagMender's "conflict") or
    a catalogue number another candidate carries (mark_code_against). Whatever else of your copy
    it carries proves nothing then - a disc ID is the same on every pressing cut from one master
    (Yuka & Chronoship - Ship: your disc ID on the UK CD, your barcode and number on the
    Japanese)."""
    return bool(s and (s.get('conflict') or s.get('code_against')))


def proven_by_copy(s: dict) -> bool:
    """Your copy proves this candidate is your pressing: its own barcode (never one typed in
    notes or written by another tagger), your disc ID (on a Discogs candidate the full "disc
    TOC" clue, given only through a MusicBrainz release carrying that exact disc ID - never
    "disc TOC (fuzzy)"), or your own catalogue number (the folder name, notes, rip log, tags
    as ripped: TagMender's _own_catno_on)."""
    clues = (s or {}).get('clues') or {}
    return bool(s and (mc.barcode_proves(s) or 'disc TOC' in clues or mc._own_catno_on(s)))


def proof_rank(s: dict | None) -> int:
    """How strongly your copy proves this candidate: 3 your disc ID (through a MusicBrainz
    release carrying it exactly), 2 your barcode, 1 your catalogue number, 0 nothing. A disc ID
    names one disc's exact layout; a barcode one product; a catalogue number is often shared
    by a label's pressings in several countries."""
    if not s:
        return 0
    if 'disc TOC' in (s.get('clues') or {}):
        return 3
    if mc.barcode_proves(s):
        return 2
    return 1 if mc._own_catno_on(s) else 0


def musicbrainz_release_of(http, verdict: dict, album: dict, mb_release_id: str = '', linked=()) -> str:
    """The MusicBrainz release of the Discogs pressing your copy proves, when it is not the one
    Picard matched: the one release MusicBrainz links to that Discogs release, else (proof by
    barcode) the one release with your barcode. "" when Picard's match is it, links to it, or
    MusicBrainz names none or several (Jacqueline du Pre - The Great Cello Concertos: Picard
    matched the 2024 download, 5021732413130; your scans' 5021732520296 is the 2025 SACD box,
    7389e942, linked to Discogs 33632313)."""
    best = verdict.get('best') or {}
    cid = str((best.get('cand') or {}).get('id') or '')
    if verdict.get('status') != 'auto' or not cid.isdigit() or cid in {str(x) for x in linked or ()}:
        return ''
    from urllib.parse import quote
    found = []
    doc = http.get_json('https://musicbrainz.org/ws/2/url?resource='
                        + quote(f'https://www.discogs.com/release/{cid}', safe='')
                        + '&inc=release-rels&fmt=json', 'mb') or {}
    for r in doc.get('relations') or ():
        rid = ((r or {}).get('release') or {}).get('id')
        if rid and rid not in found:
            found.append(rid)
    if not found and proof_rank(best) == 2:
        mine = [b for b in album.get('barcodes') or () if any(_same_code(b, c) for c in best['cand'].get('barcodes') or ())]
        if mine:
            doc = http.get_json(f'https://musicbrainz.org/ws/2/release/?query=barcode:{mine[0]}&fmt=json', 'mb') or {}
            found = [r['id'] for r in doc.get('releases') or ()
                     if r.get('id') and _same_code(r.get('barcode') or '', mine[0])]
    if len(found) == 1 and found[0] != mb_release_id:
        return found[0]
    return ''


def _same_code(a, b) -> bool:
    """Two barcodes or catalogue numbers are one: punctuation, case and leading zeros aside
    (a UPC is its EAN with a leading 0)."""
    na, nb = mc.norm_code(a).lstrip('0'), mc.norm_code(b).lstrip('0')
    return bool(na) and na == nb


def agrees_with_musicbrainz(s: dict, mb: dict) -> str:
    """What of the release Picard matched this candidate carries, as the 0.3.x plugin required
    to tag on its own: its barcode, or its catalogue number with its year. "" for neither."""
    c = (s or {}).get('cand') or {}
    bc = str((mb or {}).get('barcode') or '')
    if bc and any(_same_code(bc, x) for x in c.get('barcodes') or ()):
        return 'barcode'
    catnos = [x for x in str((mb or {}).get('catalognumber') or '').split(';') if x.strip()]
    year = str((mb or {}).get('date') or '')[:4]
    if catnos and year and str(c.get('year') or '')[:4] == year and any(
            _same_code(x, y) for x in catnos for y in c.get('catnos') or ()):
        return 'catalogue number and year'
    return ''


# ---- what is printed on your scans that only one pressing lists ------------------------------
# rights societies a sleeve or disc names (Discogs' "Rights Society" identifier, or its notes)
RIGHTS_SOCIETIES = ('SPA', 'MCPS', 'GEMA', 'SACEM', 'SDRM', 'BIEM', 'SGAE', 'JASRAC', 'STEMRA', 'SIAE',
                    'BUMA', 'NCB', 'AKM', 'AUME', 'APRA', 'AMCOS', 'ASCAP', 'BMI', 'SABAM', 'SUISA',
                    'ZAIKS', 'STIM', 'TEOSTO', 'KODA', 'TONO', 'ARTISJUS', 'OSA', 'ECAD', 'SOCAN', 'CMRRA')
# "(P) 1994 Intermusic", "© 1999 IMC Music": a copyright line's year and its owner's first word
_COPYRIGHT = re.compile(r"(?:\u2117|\u00a9|\u24c1|\u24c5|\u24b8|\((?:p|c|l)\)|copyright|phonographic)"
                        r"\s*(?:by\s+)?((?:19|20)\d\d)[\s,]+([A-Za-z][A-Za-z&'-]{2,})", re.I)
_MATRIX_TYPES = ('matrix / runout', 'mould sid code', 'mastering sid code')


def _ocr_upper(text: str) -> str:
    """Your scans' text for matching printed marks: upper case, "|994" read as "1994"."""
    return re.sub(r"[|Il!](?=\d{3}\b)", "1", text or "").upper()


def printed_marks(det: dict | None) -> set:
    """What a Discogs release says is printed on it and a scan can show: its rights societies,
    its copyright lines (year and owner) and its matrix and SID codes."""
    det = det or {}
    marks = set()
    notes = str(det.get('notes') or '')
    for i in det.get('identifiers') or ():
        kind, value = str(i.get('type') or '').casefold(), str(i.get('value') or '')
        if kind == 'rights society':
            marks |= {('rights', w) for w in re.findall(r'[A-Za-z]{2,}', value.upper()) if w in RIGHTS_SOCIETIES}
        elif kind in _MATRIX_TYPES:
            code = re.sub(r'[^A-Z0-9]', '', value.upper())
            if len(code) >= 6:
                marks.add(('matrix', code))
    marks |= {('rights', w) for w in re.findall(r'\b[A-Z]{2,7}\b', notes) if w in RIGHTS_SOCIETIES}
    marks |= {('copyright', f'{y} {o.upper()}') for y, o in _COPYRIGHT.findall(notes)}
    return marks


def mark_printed(mark, upper: str, compact: str) -> bool:
    kind, value = mark
    if kind == 'rights':
        return re.search(rf'(?<![A-Z]){value}(?![A-Z])', upper) is not None
    if kind == 'matrix':
        return value in compact
    year, owner = value.split(' ', 1)
    return re.search(rf'{year}\W{{0,3}}{re.escape(owner)}', upper) is not None


def printed_tiebreak(tied: list, details: dict, ocr_text: str) -> tuple[dict | None, list]:
    """Of pressings your copy proves equally, the one whose printed marks (printed_marks) on
    your scans include every other's - and at least one no other has. A mark all of them list
    tells none apart. Returns (that candidate or None, its marks found on your scans)."""
    if len(tied) < 2 or not (ocr_text or '').strip():
        return None, []
    upper = _ocr_upper(ocr_text)
    compact = re.sub(r'[^A-Z0-9]', '', upper)
    marks = {id(x): printed_marks(details.get(str(x['cand'].get('id')))) for x in tied}
    common = set.intersection(*marks.values())
    hits = {k: {m for m in v - common if mark_printed(m, upper, compact)} for k, v in marks.items()}
    best = max(tied, key=lambda x: len(hits[id(x)]))
    mine = hits[id(best)]
    if not mine or any(x is not best and not hits[id(x)] < mine for x in tied):
        return None, []
    return best, sorted(mine - set().union(*(hits[id(x)] for x in tied if x is not best)))


def _mark_text(mark) -> str:
    kind, value = mark
    return {'rights': f'rights society {value}', 'matrix': f'matrix {value}',
            'copyright': f'copyright "{value.title()}"'}[kind]


def most_likely(scored, proven, held_unofficial=()) -> tuple[dict | None, list]:
    """When several pressings carry what your copy proves as strongly (the same barcode on a
    1994 and a 1999 pressing), the one TagMender scores highest - year, country, label, track
    lengths, the text of your scans - if it leads the next by LIKELY_MIN_LEAD, TagMender is
    LIKELY_MIN_CONFIDENCE sure of it, every track fits and nothing of your copy is against it.
    Returns (that candidate or None, the others proven as strongly). HELD_UNOFFICIAL: ids of
    bootlegs TagMender's R9 still holds (an official release carries the same barcode)."""
    if not proven:
        return None, []
    top_rank = max(proof_rank(x) for x in proven)
    tied = sorted((x for x in proven if proof_rank(x) == top_rank), key=lambda x: -(x.get('confidence') or 0))
    top, others = tied[0], tied[1:]
    lead = (top.get('confidence') or 0) - ((others[0].get('confidence') or 0) if others else 0)
    if (lead < LIKELY_MIN_LEAD or (top.get('confidence') or 0) < LIKELY_MIN_CONFIDENCE
            or not top.get('tracks_ok') or top.get('clues_against') or top.get('conflict')
            or str(top['cand'].get('id')) in held_unofficial):
        return None, others
    return top, others


def _details(http, rid) -> dict | None:
    """A Discogs release's own page, as discogs_search fetched it (Picard's bridge keeps them)."""
    kept = getattr(http, 'releases', None)
    if isinstance(kept, dict) and str(rid) in kept:
        return kept[str(rid)]
    try:
        return http.get_json(f'https://api.discogs.com/releases/{rid}', 'discogs')
    except Exception:
        return None


def _one_digit_off(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def drop_misread_numbers(album: dict) -> list:
    """A "catalogue number" of 12-13 digits that is no valid barcode and one digit off your
    copy's decoded barcode is that barcode misread or mistyped (Joy Division - Total: MusicBrainz
    has 5053498647958 for 5052498647958): it is no number of yours that no pressing carries.
    Removed from album["catnos"]; returns those removed."""
    barcodes = {re.sub(r'\D', '', str(b)).lstrip('0') for b in album.get('barcodes') or ()}
    gone = []
    for c in list(album.get('catnos') or ()):
        d = re.sub(r'\D', '', str(c))
        if 12 <= len(d) <= 13 and re.fullmatch(r'[\d\s-]+', str(c).strip()) and not mc.valid_barcode(d) \
                and any(_one_digit_off(d.lstrip('0'), b) for b in barcodes):
            gone.append(c)
    if gone:
        album['catnos'] = type(album['catnos'])(c for c in album['catnos'] if c not in gone) \
            if isinstance(album['catnos'], (set, list, tuple)) else album['catnos']
    return gone


def pick_discid_release(album: dict, others: list, mb_facts: dict | None, likely: bool):
    """Your disc ID is on several MusicBrainz releases, none the one Picard matched (one CD in
    two editions - The Ocean - Solaris, PEL 349 and PEL 350CD). The one of them that is your
    copy: (1) its barcode is your copy's, or its catalogue number is printed on your scans;
    else, LIKELY on, (2) its catalogue number is the matched release's and it is a CD - when
    exactly one of them is. Returns (mbid, why) or (None, "")."""
    info = album.get('discid_mb') or {}
    rel = {m: info.get(m) or {} for m in others}
    own_bc = {re.sub(r'\D', '', str(b)).lstrip('0') for b in album.get('barcodes') or ()} - {''}
    by_copy = [m for m, r in rel.items()
               if (r.get('barcode') and re.sub(r'\D', '', r['barcode']).lstrip('0') in own_bc)
               or any(mc.catno_printed(album, c) for c in r.get('catnos') or ())]
    if len(by_copy) == 1:
        return by_copy[0], 'its barcode or catalogue number is on your copy'
    if by_copy or not likely:
        return None, ''
    matched = [x for x in str((mb_facts or {}).get('catalognumber') or '').split(';') if x.strip()]
    if not matched:
        return None, ''
    by_mb = [m for m, r in rel.items()
             if mc.catnos_agree(matched, r.get('catnos') or ())
             and r.get('formats') and all('CD' in f for f in r['formats'])]
    if len(by_mb) == 1:
        return by_mb[0], f'it is the CD with catalogue number {"; ".join(matched)} of the release Picard matched'
    return None, ''


def none_is_yours(album: dict, others: list) -> bool:
    """Your disc ID is on several MusicBrainz releases, and your barcode - read off your copy -
    is on none of them, each listing another: your pressing is not among them, only cut from
    the same master (Curved Air - Air Conditioning: the 2015 SHM-CD, 4943674205684, not on
    MusicBrainz; its disc ID is the 2007 Japanese CD's and the 1997 European CD's)."""
    info = album.get('discid_mb') or {}
    own = {re.sub(r'\D', '', str(b)).lstrip('0') for b in album.get('barcodes') or ()} - {''}
    theirs = [re.sub(r'\D', '', str((info.get(m) or {}).get('barcode') or '')).lstrip('0') for m in others]
    return bool(own) and bool(others) and all(t and t not in own for t in theirs)


def _bare_title(text) -> str:
    """An album title without its bracketed remarks ("Mothballs [2001 ReIssue]": "mothballs")."""
    return mc.fold(re.sub(r'\s*[\[(][^\])]*[\])]', ' ', str(text or '')))


def subtitled(copy_title, cand_title) -> bool:
    """The release's title is your album's with a subtitle ("Mothballs" - "Mothballs - Groups &
    Sessions 64-69", "Mothballs: The Early Years", "Mothballs (Groups & Sessions)")."""
    words = _bare_title(copy_title).split()
    if not words:
        return False
    lead = r'\W*'.join(re.escape(w) for w in words)
    return re.match(rf'\s*(?:(?:the|a|an|le|la|les|der|die|das|el|los)\s+)?{lead}\s*(?:[-:\u2013\u2014(/]|\[)\s*\w',
                    _ascii_lower(cand_title), re.I) is not None


def _ascii_lower(text) -> str:
    import unicodedata
    return unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().casefold()


def titles_agree(album_tracks, cand_tracks) -> float:
    """The share of your files whose title is the release's track title at the same place, a
    leading "Group - " in yours aside (a compilation of several groups: "Syndicats - Maybellene"
    for "Maybellene"). 0.0 when the counts differ or nothing can be compared."""
    mine = sorted(album_tracks or (), key=lambda t: (t.get('disc') or 1, t.get('num') or 0))
    theirs = list(cand_tracks or ())
    if not mine or len(mine) != len(theirs):
        return 0.0
    good = 0
    for m, t in zip(mine, theirs):
        a, b = mc.fold(str(m.get('title') or '')), mc.fold(str(t.get('title') or ''))
        if b and (a == b or a.endswith(' ' + b)):
            good += 1
    return good / len(mine)


def proof_first(scored: list, cfg: dict, held=(), album: dict | None = None) -> dict | None:
    """The one candidate your copy proves by disc ID or barcode, when no other is proven as
    strongly - whatever TagMender's confidence ranked first (Dustin O'Halloran - Piano Solos:
    five pressings at 82%, one with your exact disc ID). It must fit every track, carry the
    album's artist and title, and have nothing of your copy against it. A title that is your
    album's with a subtitle counts as the album's (Steve Howe - Mothballs: your barcode on
    "Mothballs - Groups & Sessions 64-69") when the album itself agrees: half its track titles
    or half its lengths."""
    proven = [x for x in scored if proven_by_copy(x) and x.get('tracks_ok') and not other_product(x)]
    if not proven:
        return None
    top = max(proof_rank(x) for x in proven)
    tied = [x for x in proven if proof_rank(x) == top]
    tied = official_over_bootlegs(tied)
    if top < 2 or len(tied) != 1:
        return None
    x = tied[0]
    if (x.get('clues_against') or x.get('conflict') or mc._ruled_out_by_copy(x)
            or str(x['cand'].get('id')) in held
            or (x.get('artist_sim') or 0) < cfg['auto_min_artist']):
        return None
    if (x.get('title_sim') or 0) >= cfg['auto_min_title']:
        return x
    if album and subtitled(album.get('title'), x['cand'].get('title')) and (
            (x.get('dur_frac') or 0) >= 0.5
            or titles_agree(album.get('tracks'), x['cand'].get('tracks')) >= 0.5):
        return x
    return None


def official_over_bootlegs(tied: list) -> list:
    """Of pressings your copy proves equally, the one official release when the others are
    unofficial copies of it (TagMender R9: a bootleg is never preferred over the official
    release with the same proof). A bootleg stays only when your copy shows something of its
    own - its matrix on your scans (Steve Howe - Homebrew 6: the 2016 US CD and a Russian
    bootleg with the same barcode and HSCD006; the scans print no country, no matrix)."""
    official = [x for x in tied if not x['cand'].get('unofficial')]
    bootlegs = [x for x in tied if x['cand'].get('unofficial')]
    if len(official) == 1 and bootlegs and not any((x.get('clues') or {}).get('matrix') for x in bootlegs):
        return official
    return tied


# a country your copy names, against a pressing: its folder name or its scans
_COUNTRY_AGAINST = ('folder name says', 'scans say made in')


def only_unopposed(tied: list) -> dict | None:
    """Of pressings your copy proves equally, the only one nothing of your copy is against, when
    the country your copy names is what tells them apart: printed on your scans for it (Joy
    Division - Total: one barcode on six CDs; "Made in the EU" on the scans; only "UK & Europe"
    is left), or against every other (Steve Howe - The Steve Howe Album: 7567-81559-2 on a
    "Europe" and a "Russia" CD; the folder name says Germany). None otherwise."""
    free = [x for x in tied if not x.get('clues_against') and not x.get('conflict') and x.get('tracks_ok')]
    if len(tied) < 2 or len(free) != 1:
        return None
    lone = free[0]
    if 'country' in (lone.get('clues') or {}):
        return lone
    if all(any(str(a).startswith(_COUNTRY_AGAINST) for a in x.get('clues_against') or ())
           for x in tied if x is not lone):
        return lone
    return None


_BACK_SCAN = re.compile(r'back|rear|tray|inlay|traseira|r\u00fcckseite|arri\u00e8re', re.I)


def codeless_pick(scored: list, album: dict, cfg: dict) -> dict | None:
    """Your copy prints no code at all - its back cover scanned and read whole, no barcode on
    it, none in your tags or notes - and of the pressings that fit, exactly one lists neither a
    barcode nor a catalogue number; every other lists one your scans do not show (Dustin
    O'Halloran - The Beauty Inside: the codeless 2013 US CD, not the Japanese PDIP-6537 nor
    the 2014 US reissue). None otherwise."""
    if not (album.get('scans_complete') and 'scan_barcodes' in album and not album.get('scan_barcodes')
            and not album.get('barcodes') and any(_BACK_SCAN.search(n) for n in album.get('scan_names') or ())):
        return None
    def fits(x):
        return (x.get('tracks_ok') and not x.get('clues_against') and not x.get('conflict')
                and not mc._ruled_out_by_copy(x) and (x.get('artist_sim') or 0) >= cfg['auto_min_artist']
                and (x.get('title_sim') or 0) >= cfg['auto_min_title'])
    def codes(x):
        c = x.get('cand') or {}
        return [b for b in c.get('barcodes') or () if str(b).strip()] + \
               [n for n in c.get('catnos') or () if str(n).strip() and mc.norm_code(n) not in ('none', '')]
    fitting = [x for x in scored if fits(x)]
    bare = [x for x in fitting if not codes(x)]
    if len(bare) != 1:
        return None
    for x in fitting:
        if x is not bare[0] and any(mc.catno_printed(album, n) for n in (x.get('cand') or {}).get('catnos') or ()):
            return None                       # a code of another IS printed: not codeless after all
    return bare[0]


def settled_without_scans(verdict: dict | None) -> bool:
    """A verdict nothing on the scans could change: your files belong to another MusicBrainz
    release (your disc ID is on it), or your exact disc ID proves the pressing (TagMender's
    strongest proof) - so the scans need not be read first."""
    v = verdict or {}
    return v.get('status') == 'move' or (v.get('status') == 'auto' and proof_rank(v.get('best')) == 3)


# how many candidates' own pages are read for their matrix codes (the best-scored first)
MATRIX_CANDIDATES = 8


def matrix_pick(scored: list, http, ocr_text: str, album: dict | None, cfg: dict) -> tuple[dict | None, str]:
    """The one candidate whose matrix / runout code (Discogs' "Matrix / Runout" identifiers) is
    printed on your scans - the code pressed into your own disc, proof of the pressing for a
    bootleg too (Steve Howe / Tomorrow - Beginnings / Tomorrow: "SHWCD 14120201" on the disc
    scan, the matrix of Discogs 8752298). It must fit your files (every track; half its track
    titles or lengths) with nothing of your copy against it, and no other candidate's matrix may
    be printed too. Returns (candidate or None, the code)."""
    if not (ocr_text or '').strip():
        return None, ''
    upper = _ocr_upper(ocr_text)
    compact = re.sub(r'[^A-Z0-9]', '', upper)
    hits = []
    for x in sorted(scored, key=lambda x: -(x.get('confidence') or 0))[:MATRIX_CANDIDATES]:
        det = _details(http, (x.get('cand') or {}).get('id'))
        codes = [m[1] for m in printed_marks(det) if m[0] == 'matrix' and mark_printed(m, upper, compact)]
        if codes:
            hits.append((x, codes[0]))
    if len(hits) != 1:
        return None, ''
    x, code = hits[0]
    fits = (x.get('tracks_ok') and not x.get('clues_against') and not x.get('conflict')
            and not mc._ruled_out_by_copy(x) and (x.get('artist_sim') or 0) >= cfg['auto_min_artist']
            and ((x.get('dur_frac') or 0) >= 0.5
                 or titles_agree((album or {}).get('tracks'), (x.get('cand') or {}).get('tracks')) >= 0.5))
    return (x, code) if fits else (None, '')


# ---- which database decides first (the tag source) ---------------------------------------------
# "Discogs preferred": Discogs decides the pressing from your copy; MusicBrainz's link and its
# barcode or catalogue number only break a tie. "MusicBrainz preferred" (and the others): the
# release Picard matched decides; its Discogs link first.
RANK_MIN_CONFIDENCE = 90.0      # (TagMender's auto_min_confidence)
RANK_MIN_LEAD = 8.0             # (TagMender's auto_min_margin)
ALBUM_MIN_CONFIDENCE = 50.0     # below: not the album at all


def mark_kind_against(scored: list, kind: tuple) -> None:
    """A candidate of a format your copy cannot be (a CD for a vinyl rip, a cassette for a
    download) has that against it: "kind_against", and in its clues_against."""
    k, why = (tuple(kind or ()) + ('', ''))[:2]
    if not k:
        return
    for x in scored:
        theirs = set((x.get('cand') or {}).get('formats') or ())
        if theirs and k not in theirs:
            what = (f'your copy is {_KIND_NAMES.get(k, k)} ({why}), this pressing is '
                    f'{"/".join(sorted(theirs))}')
            x['kind_against'] = what
            x['clues_against'] = list(x.get('clues_against') or ()) + [what]


def mark_code_against(scored: list, album: dict) -> None:
    """A candidate listing another catalogue number than your copy's own, when your number is on
    another candidate that fits your files: another pressing ("code_against", and in its
    clues_against). Yuka & Chronoship - Ship: the rip log names KICS-3687 (the Japanese CD,
    Discogs 11962139); your disc ID is on MusicBrainz only for the UK CD YCUK-004 (Discogs
    11988370), whose tracks lie on the disc exactly as the Japanese's - the disc ID cannot tell
    them apart, the number can. A barcode that differs is TagMender's own "conflict" already."""
    own = {mc.norm_code(c) for c in album.get('catnos') or ()} - {'', 'none'}
    own = {c for c in own if not mc.tagger_catno(album, {c})}
    holders = [x for x in scored if x.get('tracks_ok') and not x.get('conflict')
               and {mc.norm_code(n) for n in (x.get('cand') or {}).get('catnos') or ()} & own]
    if not own or not holders:
        return
    mine = sorted({str(n).upper() for x in holders for n in (x.get('cand') or {}).get('catnos') or ()
                   if mc.norm_code(n) in own}) or sorted(c.upper() for c in own)
    ids = ', '.join(str((x.get('cand') or {}).get('id')) for x in holders)
    for x in scored:
        if x in holders or mc.barcode_proves(x) or x.get('code_against'):
            continue
        theirs = [str(n) for n in (x.get('cand') or {}).get('catnos') or ()
                  if mc.norm_code(n) not in ('', 'none')]
        if not theirs or {mc.norm_code(n) for n in theirs} & own:
            continue
        what = (f'your catalogue number {", ".join(mine)} is on Discogs {ids}; this pressing is '
                f'{", ".join(n.upper() for n in theirs)}')
        x['code_against'] = what
        x['clues_against'] = list(x.get('clues_against') or ()) + [what]


_KIND_NAMES = {'vinyl': 'a vinyl rip', 'cassette': 'a tape rip', 'cd': 'a CD rip', 'digital': 'a download'}


def same_pressing(s: dict | None, linked, mb: dict | None):
    """Is candidate S the pressing of the MusicBrainz release Picard matched? True: MusicBrainz
    links it, or it carries that release's barcode or catalogue number. False: MusicBrainz names
    another (links others, or has a barcode or catalogue number it does not carry). None: nothing
    to tell by."""
    if not s:
        return False
    c = s.get('cand') or {}
    if str(c.get('id')) in {str(x) for x in linked or ()}:
        return True
    bc = str((mb or {}).get('barcode') or '')
    catnos = [x for x in str((mb or {}).get('catalognumber') or '').split(';') if x.strip()]
    if bc and any(_same_code(bc, x) for x in c.get('barcodes') or ()):
        return True
    if catnos and any(_same_code(x, y) for x in catnos for y in c.get('catnos') or ()):
        return True
    if bc or catnos or linked:
        return False
    return None


def ranked_pick(scored: list, linked, mb: dict | None) -> tuple[dict | None, str, str]:
    """Discogs first, when your copy proves nothing: the candidate TagMender ranks first (format,
    catalogue number, year, country, track lengths, your scans' text) among those nothing of your
    copy is against. Clearly first: it. Tied (within RANK_MIN_LEAD): MusicBrainz breaks the tie -
    the release it links, else the one carrying its barcode or catalogue number. Returns (pick,
    status "discogs" | "linked" | "musicbrainz", why) or (None, "", "")."""
    pool = sorted((x for x in scored if x.get('tracks_ok') and not x.get('clues_against')
                   and not x.get('conflict') and (x.get('confidence') or 0) >= ALBUM_MIN_CONFIDENCE),
                  key=lambda x: -(x.get('confidence') or 0))
    if not pool:
        return None, '', ''
    top = pool[0]
    conf = top.get('confidence') or 0
    nxt = pool[1].get('confidence') or 0 if len(pool) > 1 else 0
    if conf >= RANK_MIN_CONFIDENCE and conf - nxt >= RANK_MIN_LEAD:
        return top, 'discogs', (f'Discogs ranks it first for your copy ({conf:.0f}%'
                                + (f' against {nxt:.0f}% for {pool[1]["cand"].get("id")}' if len(pool) > 1 else '')
                                + '), and nothing of your copy is against it')
    tied = [x for x in pool if conf - (x.get('confidence') or 0) < RANK_MIN_LEAD]
    ids = ', '.join(str(x['cand'].get('id')) for x in tied)
    link = [x for x in tied if str(x['cand'].get('id')) in {str(i) for i in linked or ()}]
    if len(link) == 1:
        return link[0], 'linked', f'{len(tied)} pressings fit your copy equally ({ids}); MusicBrainz links this one'
    by_mb = [x for x in tied if same_pressing(x, (), mb)]
    if len(by_mb) == 1:
        return by_mb[0], 'musicbrainz', (f'{len(tied)} pressings fit your copy equally ({ids}); this one carries '
                                         f'the barcode or catalogue number of the MusicBrainz release Picard matched')
    return None, '', ''


def _decide(album: dict, http, dm, *, mb_release_id: str = '', mb_discids=(), linked=(),
           mb_facts: dict | None = None, cfg: dict | None = None, ocr_text: str = '',
           scan_probe: dict | None = None, likely: bool = True, first: str = 'mb',
           kind=('', '')) -> dict[str, Any]:
    """Which Discogs release ALBUM (read_album's dict) is.

    OCR_TEXT and SCAN_PROBE come from discogs_ocr.merge_scans: the text of your scans and
    documents every candidate is scored against, and the codes printed on them the first
    search did not ask (searched too: the release they name may not be among what it found).

    Returns {"status", "best" (a scored candidate or None), "scored", "why", "discid_on" (see
    _disc_ids), "classified" (TagMender's own classify status)}. The status, first that holds:

    "auto"        TagMender is sure (classify "auto") AND your copy proves the pick
                  (proven_by_copy), and no other candidate is proven as strongly (proof_rank:
                  disc ID > barcode > catalogue number; a number two pressings share proves
                  neither)
    "linked"      the Discogs release MusicBrainz links to: nothing of your copy is against it
                  and no other candidate is proven by your copy
    "musicbrainz" a candidate carrying the matched MusicBrainz release's barcode (or catalogue
                  number and year), when your copy proves nothing else and is not against it
    "likely"      (LIKELY) several pressings carry what your copy proves as strongly, and
                  TagMender scores one clearly highest (most_likely); "others" lists the rest
    "move"        your disc ID is on one other MusicBrainz release than Picard matched (and your
                  copy is one disc): "move_to" is that release, where your files belong
    "review"      you choose (the picker); "nomatch" nothing found

    FIRST "discogs" (the tag source "Discogs preferred"): after what your copy proves, Discogs'
    own ranking decides ("discogs", ranked_pick); MusicBrainz's link ("linked") and its barcode or
    catalogue number ("musicbrainz") only break a tie. KIND: what your copy was made from
    (discogs_kind.copy_kind); a candidate of another format has that against it.
    "same_as_mb": the pick is the pressing of the MusicBrainz release Picard matched (same_pressing).
    """
    cfg = _cfg(cfg)
    linked = [str(x) for x in linked or () if str(x).isdigit()]
    discid_on = _disc_ids(album, http, mb_release_id, set(mb_discids or ()), linked)
    others = [x for x in discid_on[6:].split(',') if x] if discid_on.startswith('other:') else []
    not_mine = len(others) > 1 and none_is_yours(album, others)
    if not_mine:
        # your disc ID names the master, not your pressing: the Discogs releases those MusicBrainz
        # releases link to are proven by nothing of yours
        album['toc_discogs'] = set()
        album['toc_discogs_fuzzy'] = {}

    misread = drop_misread_numbers(album)
    cands = tr.discogs_search(http, album, cfg, dm)
    if scan_probe:
        ids = {str(c.get('id')) for c in cands}
        cands += [c for c in tr.discogs_search(http, scan_probe, cfg, dm) if str(c.get('id')) not in ids]
    cands = tr.with_discogs_ids(http, dm, cands, linked, limit=MAX_LINKED)
    if not cands:
        return {'status': 'nomatch', 'best': None, 'scored': [], 'why': ['no Discogs release found'],
                'discid_on': discid_on, 'classified': 'nomatch'}

    # TagMender's run_source
    mc.note_titles(album, cands)
    mc.settle_title_numbers(album, cands)
    scored = mc.code_twins(album, [mc.score_candidate(album, c, cfg, ocr_text) for c in cands], cfg, ocr_text)
    mark_kind_against(scored, kind)
    mark_code_against(scored, album)
    res = mc.classify(scored, cfg)
    # TagMender's R9 against Discogs' whole list: a bootleg your copy proves is yours when no
    # official release carries that proof (classify alone never takes one)
    res = mc.unofficial_settled(res, [None, {'scored': scored}], album, cfg, ocr_text)
    status, best = res.get('status'), res.get('best')
    out = {'status': status, 'best': best, 'scored': scored, 'why': list(res.get('why') or ()),
           'discid_on': discid_on, 'classified': status, 'runner_up': res.get('runner_up'),
           'margin': res.get('margin')}
    if misread:
        out['why'].append(f'{", ".join(map(str, misread))}: your barcode misread or mistyped, not read as a '
                          f'catalogue number')

    if discid_on.startswith('other:'):
        # your disc ID is on another MusicBrainz release than the one Picard matched: the
        # MusicBrainz match and your copy disagree. On exactly one other release (and your copy
        # one disc), that release is yours: "move" - the files go there, as Picard's own disc
        # lookup would put them; on several, you decide
        others = [x for x in discid_on[6:].split(',') if x]
        if len(others) == 1 and len(album.get('tocs') or {}) == 1:
            out.update(status='move', move_to=others[0])
            out['why'].insert(0, f'your disc ID is on MusicBrainz release {others[0]}, not the one '
                                 f'Picard matched: your files belong there')
            return out
        pick, how = pick_discid_release(album, others, mb_facts, likely) if len(others) > 1 \
            and len(album.get('tocs') or {}) == 1 else (None, '')
        if pick:
            out.update(status='move', move_to=pick)
            out['why'].insert(0, f'your disc ID is on MusicBrainz releases {", ".join(others)}; '
                                 f'{pick} is yours: {how}')
            return out
        if not_mine:
            # (your pressing is none of them: what your copy proves on Discogs decides, below)
            out['why'].append(f'your disc ID is on MusicBrainz releases {", ".join(others)}, but your '
                              f'barcode is on none of them: your pressing is not on MusicBrainz')
        else:
            out['status'] = 'review'
            out['why'].insert(0, f'your disc ID is on MusicBrainz release {discid_on[6:]}, '
                                 f'not the one Picard matched')
            return out

    proven = [x for x in scored if proven_by_copy(x) and x.get('tracks_ok') and not other_product(x)]
    rank = proof_rank(best)
    if status == 'auto' and best is not None and rank and best.get('tracks_ok') \
            and all(x is best or proof_rank(x) < rank for x in proven):
        return out              # proven by your copy, and no other pressing as strongly
    held = {str(best['cand'].get('id'))} if best and mc.R9_HELD in out['why'] else set()
    pick, code = matrix_pick(scored, http, ocr_text, album, cfg)
    if pick is not None:
        out.update(status='auto', best=pick)
        out['why'].insert(0, f'its matrix {code} is printed on your scans: the code pressed into your '
                             f'own disc, on no other candidate')
        pick['notes'] = list(pick.get('notes') or []) + [f'matrix {code} printed on your scans']
        return out
    lone_proof = proof_first(scored, cfg, held, album)
    if lone_proof is not None:
        out.update(status='auto', best=lone_proof)
        out['why'].insert(0, f'only this pressing is proven by your copy '
                             f'({"disc ID" if proof_rank(lone_proof) == 3 else "barcode"}), and nothing of '
                             f'your copy is against it (TagMender\'s confidence {lone_proof.get("confidence", 0):.0f}%)')
        return out

    def clean(x):
        return (x is not None and not x.get('clues_against') and not x.get('conflict')
                and x.get('tracks_ok') and not [y for y in proven if y is not x])

    if status == 'auto':
        out['status'] = 'review'
        out['why'].insert(0, 'TagMender found the album, but nothing of your copy proves the '
                             'pressing' if not proven else
                             'more than one pressing carries what your copy shows')
    link = next((x for x in scored if str(x['cand'].get('id')) in linked), None)
    if first == 'discogs':
        link = None                 # (Discogs first: MusicBrainz only breaks a tie, below)
    if clean(link):
        out.update(status='linked', best=link)
        out['why'].insert(0, 'MusicBrainz links this Discogs release, and nothing of your copy '
                             'is against it')
        return out
    by_mb = [(agrees_with_musicbrainz(x, mb_facts), x) for x in scored] if first != 'discogs' else []
    by_mb = [(w, x) for w, x in by_mb if w and clean(x)]
    if len(by_mb) == 1:
        what, x = by_mb[0]
        out.update(status='musicbrainz', best=x)
        out['why'].insert(0, f'it carries the {what} of the MusicBrainz release Picard matched, '
                             f'and nothing of your copy is against it')
        return out
    if proven:
        top_rank = max(proof_rank(x) for x in proven)
        tied = [x for x in proven if proof_rank(x) == top_rank]
        details = {str(x['cand'].get('id')): _details(http, x['cand'].get('id')) for x in tied}
        pick, marks = printed_tiebreak(tied, details, ocr_text)
        if pick is not None and str(pick['cand'].get('id')) not in held and pick.get('tracks_ok') \
                and not pick.get('clues_against') and not pick.get('conflict'):
            what = ', '.join(_mark_text(m) for m in marks)
            out.update(status='auto', best=pick, others=[x for x in tied if x is not pick])
            out['why'] = [w for w in out['why'] if not w.startswith('more than one pressing carries')]
            out['why'].insert(0, f'{len(tied)} pressings carry what your copy proves; only this one '
                                 f'lists what your scans print: {what}')
            pick['notes'] = list(pick.get('notes') or []) + [f'printed on your scans, only this pressing: {what}']
            return out
        lone = only_unopposed(tied) if likely else None
        if lone is not None and str(lone['cand'].get('id')) not in held:
            out.update(status='likely', best=lone, others=[x for x in tied if x is not lone])
            where = (lone.get('clues') or {}).get('country') or 'the country your copy names rules out the others'
            out['why'].insert(0, f'{len(tied)} pressings carry what your copy proves; only this one has '
                                 f'nothing of your copy against it, and {where}')
            return out
    top, others = most_likely(scored, proven, held) if likely else (None, [])
    if top is not None and others:
        out.update(status='likely', best=top, others=others)
        out['why'].insert(0, f'{len(others) + 1} pressings carry what your copy proves; this one is the '
                             f'most likely ({top["confidence"]:.1f}% against '
                             f'{others[0]["confidence"]:.1f}% for {others[0]["cand"].get("id")})')
        return out
    lone = codeless_pick(scored, album, cfg) if likely and not proven else None
    if lone is not None:
        out.update(status='likely', best=lone, others=[x for x in scored if x is not lone and x.get('tracks_ok')])
        out['why'].insert(0, 'your copy prints no barcode or catalogue number (its back cover read '
                             'whole), and only this pressing lists none')
        return out
    if first == 'discogs' and scored:
        pick, how, why = ranked_pick(scored, linked, mb_facts)
        if pick is not None and str(pick['cand'].get('id')) not in held:
            out.update(status=how, best=pick)
            out['why'].insert(0, why)
            return out
        if not any(x.get('tracks_ok') and not x.get('kind_against') and not mc._ruled_out_by_copy(x)
                   and (x.get('confidence') or 0) >= ALBUM_MIN_CONFIDENCE for x in scored):
            out.update(status='nomatch', ruled_out=True)
            out['why'].insert(0, 'Discogs lists no release your copy can be (each is a format your '
                                 'copy is not, another pressing your copy rules out, or not this album)')
            return out
    if status == 'nomatch' or not scored:
        out['status'] = 'nomatch'
    elif not proven and all(mc._ruled_out_by_copy(x) or x.get('kind_against') for x in scored):
        out.update(status='nomatch', ruled_out=True)
        out['why'].insert(0, 'Discogs lists no release your copy can be (each is a format or '
                             'pressing your copy rules out)')
    else:
        out['status'] = 'review'
    return out


def decide(album: dict, http, dm, *, mb_release_id: str = '', mb_discids=(), linked=(),
           mb_facts: dict | None = None, cfg: dict | None = None, ocr_text: str = '',
           scan_probe: dict | None = None, likely: bool = True, first: str = 'mb',
           kind=('', '')) -> dict[str, Any]:
    """_decide (see there), and "same_as_mb": the pick is the pressing of the MusicBrainz release
    Picard matched (True), another (False) or nothing tells (None)."""
    out = _decide(album, http, dm, mb_release_id=mb_release_id, mb_discids=mb_discids, linked=linked,
                  mb_facts=mb_facts, cfg=cfg, ocr_text=ocr_text, scan_probe=scan_probe, likely=likely,
                  first=first, kind=kind)
    if out.get('status') in ('linked', 'musicbrainz'):
        out['same_as_mb'] = True
    else:
        out['same_as_mb'] = same_pressing(out.get('best'), linked, mb_facts) if out.get('best') else None
    out['first'] = first
    return out


def release_row(det: dict) -> dict:
    """A Discogs release (its /releases JSON) as a /database/search result, for the picker."""
    det = det or {}
    names = ' & '.join(str(a.get('name') or '') for a in det.get('artists') or () if isinstance(a, dict))
    fmt = []
    for f in det.get('formats') or ():
        fmt.append(str(f.get('name') or ''))
        fmt += [str(d) for d in f.get('descriptions') or ()]
    labels = [l for l in det.get('labels') or () if isinstance(l, dict)]
    images = det.get('images') or []
    return {
        'id': det.get('id'), 'type': 'release', 'master_id': det.get('master_id') or '',
        'uri': det.get('uri') or '', 'resource_url': det.get('resource_url') or '',
        'thumb': (images[0].get('uri150') if images else '') or det.get('thumb') or '',
        'cover_image': (images[0].get('uri') if images else '') or '',
        'title': f'{names} - {det.get("title") or ""}', 'year': det.get('year') or '',
        'country': det.get('country') or '', 'format': [x for x in fmt if x],
        'formats': det.get('formats') or [],
        'label': [str(l.get('name') or '') for l in labels],
        'catno': ', '.join(dict.fromkeys(str(l.get('catno') or '') for l in labels if l.get('catno'))),
        'barcode': [str(i.get('value') or '') for i in det.get('identifiers') or ()
                    if str(i.get('type') or '').casefold() == 'barcode'],
        'genre': det.get('genres') or [], 'style': det.get('styles') or [],
        'community': {k: (det.get('community') or {}).get(k) for k in ('have', 'want')},
    }


def match_detail(s: dict, best: dict | None = None) -> dict:
    """The picker's Match cell for one scored candidate: TagMender's confidence and clues."""
    conf = float((s or {}).get('confidence') or 0)
    rank = proof_rank(s) if proven_by_copy(s) else 0
    against = bool((s or {}).get('clues_against') or (s or {}).get('conflict'))
    if rank:
        label = 'Proven (' + {3: 'disc ID', 2: 'barcode', 1: 'cat. no.'}[rank] + ')'
    elif against:
        label = 'Doubtful'
    elif conf >= 90:
        label = 'Strong'
    elif conf >= 60:
        label = 'Likely'
    elif conf > 0:
        label = 'Weak'
    else:
        label = 'No match'
    # sorted on: what your copy proves (disc ID > barcode > catalogue number), then nothing of
    # your copy against it, then TagMender's confidence - never the label's alphabet
    order = rank * 1000 + (0 if against else 500) + round(conf)
    return {'score': order, 'label': label,
            'text': f'{label}: {explain(s)}' + (' (TagMender\'s pick)' if best is not None and s is best else '')}


def explain(s: dict | None) -> str:
    """One line on why a scored candidate scored as it did: the clues for it and against it."""
    if not s:
        return ''
    parts = [f"{s.get('confidence', 0):.0f}%"]
    clues = s.get('clues') or {}
    if clues:
        parts.append('for: ' + '; '.join(str(v) for v in clues.values()))
    if s.get('code'):
        parts.append(f"code: {s['code']}")
    against = s.get('clues_against') or []
    if against:
        parts.append('against: ' + '; '.join(map(str, against)))
    return ' | '.join(parts)
