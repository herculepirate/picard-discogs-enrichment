"""Web sources for what MusicBrainz and Discogs leave empty: TagMender's web_tag (Deezer, iTunes,
Bandcamp, Wikipedia, Wikidata) and two of the plugin's own, VGMdb (through vgmdb.info) and
LRCLIB (lyrics).

A web album is used only on TagMender's proof: scored by its matcher against your files (artist,
title, track count and every track's length) and classified "auto". It then fills what is
empty: never a tag MusicBrainz or Discogs wrote, never a pressing's details (label, barcode,
catalogue number) for an album MusicBrainz matched - a shop's or encyclopedia's album is the
album, not your pressing. Lyrics are LRCLIB's for that very track (its artist, title, album and
length within two seconds).

`http`: get_json(url, host) / get_text(url, host), as for discogs_decide. Pure Python: no Qt.
No search engine's page and no site whose terms forbid it is read: Deezer, iTunes, Wikipedia,
Wikidata and LRCLIB are public APIs; Bandcamp's own search and the album data its pages publish
for search engines; VGMdb only through vgmdb.info's JSON.
"""
from __future__ import annotations

import re
from urllib.parse import quote

try:
    from . import tm_match_core as mc
    from . import tm_web as tw
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_web as tw

VGMDB = 'https://vgmdb.info'
LRCLIB = 'https://lrclib.net'
# how far LRCLIB's track may be from yours, in seconds
LYRICS_SLACK_S = 2.0

# tags a web album fills on an album MusicBrainz matched (only where empty)
FILL_TAGS = ('genre', 'composer', 'arranger', 'lyricist')


# ---- VGMdb (vgmdb.info) --------------------------------------------------------------------
def _vgm_name(names: dict | None) -> str:
    names = names or {}
    for k in ('en', 'English', 'ja-latn', 'Romaji', 'ja', 'Japanese'):
        if names.get(k):
            return str(names[k])
    return str(next(iter(names.values()), '')) if names else ''


def _mmss(v) -> float:
    m = re.fullmatch(r'(?:(\d+):)?(\d+):(\d{2})', str(v or '').strip())
    if not m:
        return 0.0
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def vgmdb_album(http, link: str) -> dict | None:
    """One vgmdb.info album ("album/79") as a TagMender candidate (a physical release: VGMdb
    lists pressings, with their catalogue numbers)."""
    link = link.strip('/')
    d = http.get_json(f'{VGMDB}/{link}?format=json', 'vgmdb')
    if not isinstance(d, dict) or not d.get('discs'):
        return None
    tracks = []
    for n_disc, disc in enumerate(d.get('discs') or (), 1):
        for n, t in enumerate(disc.get('tracks') or (), 1):
            tracks.append({'disc': n_disc, 'num': n, 'title': _vgm_name(t.get('names')),
                           'secs': _mmss(t.get('track_length')), 'artist': ''})

    def people(key):
        return [_vgm_name(p.get('names')) for p in d.get(key) or () if isinstance(p, dict)]
    pub = d.get('publisher') or {}
    labels = [o for o in d.get('organizations') or () if str(o.get('role')) == 'label']
    label = _vgm_name((labels[0] if labels else pub).get('names') if (labels or pub) else {})
    artist = ', '.join(people('composers')[:3]) or ', '.join(people('performers')[:3])
    cats = d.get('categories') or ([d['category']] if d.get('category') else [])
    c = tw._cand('vgmdb', link, f'https://vgmdb.net/{link}', artist,
                 _vgm_name(d.get('names')) or d.get('name'), d.get('release_date') or '', label,
                 str(d.get('barcode') or ''), [str(x) for x in cats], tracks, d.get('picture_full') or '')
    c['formats'] = {str(d.get('media_format') or 'CD').casefold()}
    # a soundtrack is credited to its composer or its performers: either may be the album artist
    c['artist_alt'] = ', '.join(people('performers')[:3]) if people('composers') else ''
    c['catnos'] = {mc.norm_code(d['catalog'])} if d.get('catalog') else set()
    c['catno_text'] = d.get('catalog') or ''
    c['names'] = d.get('names') or {}
    c['credits'] = {'composer': people('composers'), 'arranger': people('arrangers'),
                    'lyricist': people('lyricists'), 'performer': people('performers')}
    c['covers'] = [x for x in d.get('covers') or () if isinstance(x, dict) and x.get('full')]
    return c


def vgmdb(http, artist, title, n_tracks=None):
    """Albums vgmdb.info finds by title (VGMdb is searched by album title; the artist of a game
    or anime soundtrack is rarely its composer)."""
    data = http.get_json(f'{VGMDB}/search/albums/{quote(title)}?format=json', 'vgmdb') or {}
    hits = ((data.get('results') or {}).get('albums') or [])[:4]
    return [c for c in (vgmdb_album(http, h.get('link') or '') for h in hits if h.get('link')) if c]


SOURCES = tuple(tw.SOURCES) + (('VGMdb', vgmdb),)
SOURCE_NAMES = tuple(n for n, _ in SOURCES)


def chosen(names) -> list:
    by = {n.casefold(): (n, f) for n, f in SOURCES}
    return [by[k] for k in dict.fromkeys(str(x).strip().casefold() for x in names or ()) if k in by]


# ---- finding the album ---------------------------------------------------------------------
def find(http, album: dict, sources, cfg: dict | None = None, ocr_text: str = '', log=None) -> dict:
    """The first web source (in SOURCES' order) whose album TagMender is sure of.

    Returns {"source", "best" (scored), "why", "near" (the best unsure one), "failed": [names]}."""
    cfg = dict(mc.DEFAULT_CFG, **(cfg or {}))
    artist = mc.artist_parts(album.get('artist') or '')[0]
    title = mc.search_title(album.get('title') or '')
    out = {'source': None, 'best': None, 'why': [], 'near': None, 'failed': []}
    if not artist and not title:
        out['why'] = ['no artist or title to search by']
        return out
    for name, fetch in chosen(sources):
        try:
            cands = fetch(http, artist, title) or []
        except Exception as exc:                    # one source down: the others still count
            out['failed'].append(name)
            if log:
                log(f'{name} lookup failed: {type(exc).__name__}: {exc}')
            continue
        cands = [c for c in cands if c.get('tracks')]
        if not cands:
            continue
        scored = [mc.score_candidate(album, c, cfg, ocr_text) for c in cands]
        res = mc.classify(scored, cfg)
        if res.get('status') == 'auto' and res.get('best'):
            out.update(source=name, best=res['best'], why=list(res.get('why') or ()))
            return out
        b = res.get('best')
        if b and (out['near'] is None or b['confidence'] > out['near'][1]['confidence']):
            out['near'] = (name, b, list(res.get('why') or ()))
    return out


def fill_tags(cand: dict) -> dict:
    """The tags a proven web album may fill on an album MusicBrainz matched: its genres, and
    VGMdb's composer, arranger and lyricist credits. Never label, barcode or catalogue number."""
    out = {}
    genres = tw.genre_list(cand.get('genre'))
    if genres:
        out['genre'] = genres
    for k, v in (cand.get('credits') or {}).items():
        if k in FILL_TAGS and v:
            out[k] = list(dict.fromkeys(v))
    return out


def cover_urls(cand: dict) -> list[tuple[str, str]]:
    """[(url, type)] of a proven web album's artwork: its front cover, and VGMdb's other scans."""
    out = []
    if cand.get('cover'):
        out.append((cand['cover'], 'front'))
    for x in cand.get('covers') or ():
        name = str(x.get('name') or '').casefold()
        kind = 'front' if 'front' in name else 'back' if 'back' in name else 'booklet' if 'booklet' in name \
            else 'medium' if ('disc' in name or 'cd' in name) else 'other'
        if x['full'] not in [u for u, _ in out]:
            out.append((x['full'], kind))
    return out


def album_tags(album: dict, cand: dict, http, source_name: str, text: str = '') -> list[dict]:
    """TagMender's tags for each file of an album no database has (web_tag.tags_for), in file
    order, without TagMender's own bookkeeping tags."""
    rows = tw.tags_for(album, cand, http, {}, source_name, text=text)
    out = []
    for row in rows:
        out.append({k: v for k, v in row.items()
                    if not k.startswith('tagmender') and v not in ('', [], None)})
    return out


# ---- lyrics (LRCLIB) -----------------------------------------------------------------------
def lyrics(http, artist: str, title: str, album: str, secs: float) -> dict | None:
    """LRCLIB's lyrics of one track: {"plain", "synced", "instrumental", "id"} or None. Only a
    track of that artist and title whose length is within LYRICS_SLACK_S of yours."""
    if not (artist and title and secs):
        return None
    url = (f'{LRCLIB}/api/get?artist_name={quote(artist)}&track_name={quote(title)}'
           f'&album_name={quote(album or "")}&duration={int(round(secs))}')
    hit = http.get_json(url, 'lrclib')
    if not _lyrics_fit(hit, artist, title, secs):
        hits = http.get_json(f'{LRCLIB}/api/search?artist_name={quote(artist)}&track_name={quote(title)}',
                             'lrclib')
        fits = [h for h in hits if _lyrics_fit(h, artist, title, secs)] if isinstance(hits, list) else []
        hit = min(fits, key=lambda h: abs(float(h.get('duration') or 0) - secs)) if fits else None
    if not hit:
        return None
    return {'plain': hit.get('plainLyrics') or '', 'synced': hit.get('syncedLyrics') or '',
            'instrumental': bool(hit.get('instrumental')), 'id': hit.get('id')}


def _lyrics_fit(hit, artist, title, secs) -> bool:
    if not isinstance(hit, dict) or not hit.get('id'):
        return False
    if abs(float(hit.get('duration') or 0) - secs) > LYRICS_SLACK_S:
        return False
    name = hit.get('trackName') or hit.get('name') or ''
    by = hit.get('artistName') or ''
    same_artist = mc.fold(artist) in mc.fold(by) or mc.title_sim(artist, by) >= 0.85
    return same_artist and mc.title_sim(title, name) >= 0.85


def release_album(release: dict | None, artist: str, title: str) -> dict:
    """An album in read_album's shape from the MusicBrainz release Picard matched (its tracks and
    lengths), for when the files cannot be read (not attached yet, or spread over a folder
    holding other albums). The proof is then against the matched release's lengths."""
    tracks = []
    for d, medium in enumerate((release or {}).get('media') or (), 1):
        for n, t in enumerate(medium.get('tracks') or (), 1):
            ms = t.get('length') or (t.get('recording') or {}).get('length') or 0
            credit = ''.join(f"{a.get('name') or ''}{a.get('joinphrase') or ''}"
                             for a in t.get('artist-credit') or ())
            tracks.append({'disc': d, 'num': n, 'disc_tag': d, 'title': t.get('title') or '',
                           'secs': (ms or 0) / 1000.0, 'artist': credit, 'path': '', 'ids': [],
                           'audio': {}})
    return {'artist': artist, 'title': title, 'tracks': tracks, 'discs': max([1] + [t['disc'] for t in tracks]),
            'tracktotal': len(tracks), 'barcodes': set(), 'catno_search': [], 'catnos': set(),
            'track_artists': [], 'tag_urls': [], 'folder': '', 'artist_folder': '', 'year': None}
