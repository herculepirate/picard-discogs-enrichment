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

from typing import Any

try:
    from . import tm_match_core as mc
    from . import tm_readers as tr
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_readers as tr

# MusicBrainz releases (carrying your exact disc ID) whose Discogs links are read
MAX_DISCID_RELEASES = 3
# Discogs releases MusicBrainz links to that are fetched beside the search's own
MAX_LINKED = 4


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
    for rid in exact[:MAX_DISCID_RELEASES]:
        rel = http.get_json(f'https://musicbrainz.org/ws/2/release/{rid}?inc=url-rels&fmt=json', 'mb')
        links |= {x for x in tr.mb_linked_discogs_ids(rel or {}) if str(x).isdigit()}
    if links:
        album['toc_discogs'] = set(album.get('toc_discogs') or ()) | {str(x) for x in links}
        album['toc_discogs_fuzzy'] = {k: v for k, v in (album.get('toc_discogs_fuzzy') or {}).items()
                                      if k not in links}
    if exact and mb_release_id not in exact:
        return 'other:' + ','.join(exact)
    return 'matched' if exact else ''


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


def decide(album: dict, http, dm, *, mb_release_id: str = '', mb_discids=(), linked=(),
           mb_facts: dict | None = None, cfg: dict | None = None, ocr_text: str = '') -> dict[str, Any]:
    """Which Discogs release ALBUM (read_album's dict) is.

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
    "review"      you choose (the picker); "nomatch" nothing found
    """
    cfg = _cfg(cfg)
    linked = [str(x) for x in linked or () if str(x).isdigit()]
    discid_on = _disc_ids(album, http, mb_release_id, set(mb_discids or ()), linked)

    cands = tr.discogs_search(http, album, cfg, dm)
    cands = tr.with_discogs_ids(http, dm, cands, linked, limit=MAX_LINKED)
    if not cands:
        return {'status': 'nomatch', 'best': None, 'scored': [], 'why': ['no Discogs release found'],
                'discid_on': discid_on, 'classified': 'nomatch'}

    # TagMender's run_source
    mc.note_titles(album, cands)
    mc.settle_title_numbers(album, cands)
    scored = mc.code_twins(album, [mc.score_candidate(album, c, cfg, ocr_text) for c in cands], cfg, ocr_text)
    res = mc.classify(scored, cfg)
    status, best = res.get('status'), res.get('best')
    out = {'status': status, 'best': best, 'scored': scored, 'why': list(res.get('why') or ()),
           'discid_on': discid_on, 'classified': status, 'runner_up': res.get('runner_up'),
           'margin': res.get('margin')}

    if discid_on.startswith('other:'):
        # your disc ID is on another MusicBrainz release than the one Picard matched: the
        # MusicBrainz match and your copy disagree - you decide
        out['status'] = 'review'
        out['why'].insert(0, f'your disc ID is on MusicBrainz release {discid_on[6:]}, '
                             f'not the one Picard matched')
        return out

    proven = [x for x in scored if proven_by_copy(x) and x.get('tracks_ok')]
    rank = proof_rank(best)
    if status == 'auto' and best is not None and rank and best.get('tracks_ok') \
            and all(x is best or proof_rank(x) < rank for x in proven):
        return out              # proven by your copy, and no other pressing as strongly

    def clean(x):
        return (x is not None and not x.get('clues_against') and not x.get('conflict')
                and x.get('tracks_ok') and not [y for y in proven if y is not x])

    if status == 'auto':
        out['status'] = 'review'
        out['why'].insert(0, 'TagMender found the album, but nothing of your copy proves the '
                             'pressing' if not proven else
                             'more than one pressing carries what your copy shows')
    link = next((x for x in scored if str(x['cand'].get('id')) in linked), None)
    if clean(link):
        out.update(status='linked', best=link)
        out['why'].insert(0, 'MusicBrainz links this Discogs release, and nothing of your copy '
                             'is against it')
        return out
    by_mb = [(agrees_with_musicbrainz(x, mb_facts), x) for x in scored]
    by_mb = [(w, x) for w, x in by_mb if w and clean(x)]
    if len(by_mb) == 1:
        what, x = by_mb[0]
        out.update(status='musicbrainz', best=x)
        out['why'].insert(0, f'it carries the {what} of the MusicBrainz release Picard matched, '
                             f'and nothing of your copy is against it')
        return out
    if status == 'nomatch' or not scored:
        out['status'] = 'nomatch'
    else:
        out['status'] = 'review'
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
    if proven_by_copy(s):
        label = 'Proven'
    elif conf >= 90:
        label = 'Strong'
    elif conf >= 60:
        label = 'Likely'
    elif conf > 0:
        label = 'Weak'
    else:
        label = 'No match'
    return {'score': round(conf), 'label': label,
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
