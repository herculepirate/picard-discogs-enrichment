"""What your copy is, written where a player sees it - TagMender's rules, on Picard's tags:

  Edition in the Album tag   "Nightfall [2004 Remaster]", "Lateralus [2005 Vinyl Rip]",
                             "Brave [1999 SACD]", "[2015 Remaster HD]" (tm_editions: naming.edition,
                             ED-1..7). The original CD or download has none; a bootleg is no remaster
  Version tags               ALBUMVERSION (the edition) and RELEASECOMMENT (MusicBrainz's
                             disambiguation), read by LMS and other media servers
  Grouping                   "Vinyl Rip", "Tape Rip" or "HD Audio" where the file has no Grouping
                             (tm_marks: MED-6, MED-8)
  Vinyl rip comment          "Vinyl rip: PBTHAL · 24-bit/96 kHz · ripped 2012 · <turntable>" beside
                             what the comment says (tm_vinylrip: VIN-4)
  Comments                   keep, or remove a ripper's or shop's comment (TagMender's
                             cleanup.comments)
  Compilations               COMPILATION on a single artist's best-of too; ITUNESCOMPILATION and COMP
                             in Vorbis/APE files; Grouping "Compilation" (tm_album_artist_deps:
                             compilations.py)
  Genres                     your own names ("funeral doom metal = Doom Metal") and at most N
                             (TagMender's enrich.map_genres)
  Lyrics file                a track's time-synced lyrics saved as "<track>.lrc" beside it, never
                             over one that is there (TagMender's enrich --lrc)

The pure part needs no Picard; refresh() and the hooks run inside it: the files are measured
(bit depth, rate) and the folder's rip notes read on the plugin's "copy" worker, and the tags
written to the album, its tracks and their files - again whenever Discogs writes its tags or a
file joins its track, so the edition follows the release finally chosen.
"""
from __future__ import annotations

import os
import re
import threading
import weakref

try:
    from . import tm_album_artist_deps as comp
    from . import tm_editions as ted
    from . import tm_marks as tmarks
    from . import tm_match_core as mc
    from . import tm_readers as tr
    from . import tm_vinylrip as tvr
except ImportError:  # run outside Picard (tests)
    import tm_album_artist_deps as comp
    import tm_editions as ted
    import tm_marks as tmarks
    import tm_match_core as mc
    import tm_readers as tr
    import tm_vinylrip as tvr

EDITION_MODES = ('off', 'edition', 'edition_year')
COMMENT_MODES = ('keep', 'remove')
# how many files are measured for HD (ED-5: the first tracks say)
HD_FILES = 3


# ---- settings --------------------------------------------------------------------------------
def prefs(settings) -> dict:
    g = settings.get if hasattr(settings, 'get') else (lambda k, d=None: settings[k] if k in settings else d)
    mode = str(g('ed_album', 'off') or 'off')
    return {
        'edition': mode if mode in EDITION_MODES else 'off',
        'version_tags': bool(g('ed_version_tags', True)),
        'grouping': bool(g('mark_grouping', True)),
        'vinyl_comment': bool(g('vinyl_comment', True)),
        'comments': str(g('comments', 'keep') or 'keep'),
        'comp_extra_flags': bool(g('comp_extra_flags', False)),
        'comp_grouping': bool(g('comp_grouping', False)),
        'comp_single_artist': bool(g('comp_single_artist', False)),
        'genre_max': int(g('genre_max', 5) or 0),
        'genre_map': parse_map(g('genre_map', '') or ''),
        'lrc': bool(g('lyrics_lrc', False)),
    }


def parse_map(text: str) -> dict:
    """"from = to" lines (also "from -> to") as {folded from: to}."""
    out = {}
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        for sep in ('->', '=>', '='):
            if sep in line:
                k, v = (x.strip() for x in line.split(sep, 1))
                if k and v:
                    out[mc.fold(k)] = v
                break
    return out


# ---- the pure rules --------------------------------------------------------------------------
def _get(m, key) -> list[str]:
    try:
        values = m.getall(key)
    except AttributeError:
        values = m.get(key) or []
        values = values if isinstance(values, list) else [values]
    return [str(v) for v in values if str(v).strip()]


def edition_tags(m, hd: bool = False) -> dict:
    """The tags naming.edition reads, from one metadata object (and the copy's HD, ED-5)."""
    t = {}
    for key in ('media', 'date', 'originaldate', 'originalyear', 'releasestatus', 'album'):
        v = _get(m, key)
        if v:
            t[key] = v
    comment = _get(m, '~releasecomment') or _get(m, 'releasecomment')
    if comment:
        t['releasecomment'] = comment
    if hd:
        t['~edition_hd'] = ['1']
    return t


def edition_of(m, mode: str, hd: bool = False) -> str:
    """TagMender's edition for this release ("2004 Remaster", "Vinyl Rip", "HD"), "" for none."""
    return ted.edition(edition_tags(m, hd), mode) if mode in ted.EDITIONS and mode != 'off' else ''


def titled(album: str, ed: str, previous: str = '') -> str:
    """ALBUM with " [ED]" after it, once - TagMender's ED-6 (a "[Remastered]" the title carries
    gives way to the edition) and ED-3b (the edition's own name, "(Deluxe Edition)", is not said
    twice). PREVIOUS: the edition this plugin added before, taken off first."""
    album = str(album or '')
    if previous and album.endswith(f' [{previous}]'):
        album = album[:-len(f' [{previous}]')]
    if not ed or not album:
        return album
    if album.endswith(f' [{ed}]'):
        return album
    if re.search(r'(?:Remaster|Reissue)(?: HD)?$', ed):
        album = ted._GENERIC_VERSION_MARK.sub('', album) or album
    named = ted.edition_name({'media': [ed]})
    if named:
        album = re.sub(r'\s*[\(\[]' + re.escape(named) + r'[\)\]]', '', album, flags=re.I).strip() or album
    return f'{album} [{ed}]'


def map_genres(genres, mapping: dict, most: int) -> list[str]:
    """Your names for genres ("funeral doom metal = Doom Metal"), each once, at most MOST (0: all)."""
    out = []
    for g in genres or ():
        g = mapping.get(mc.fold(g), str(g).strip())
        if g and mc.fold(g) not in {mc.fold(x) for x in out}:
            out.append(g)
    return out[:most] if most > 0 else out


def compilation_tags(m, path: str, p: dict) -> dict:
    """TagMender's compilation marks for one file: {tag: values} to write. COMPILATION on a single
    artist's best-of (comp_single_artist), ITUNESCOMPILATION/COMP in Vorbis and APE files
    (comp_extra_flags), Grouping "Compilation" where empty (comp_grouping)."""
    t = {k: _get(m, k) for k in ('musicbrainz_albumartistid', 'albumartist', 'releasetype', 'compilation',
                                 'grouping')}
    if any('compilation' in x.casefold() for x in _get(m, 'media')):
        t[comp.HIDDEN_COMPILATION] = ['1']
    va = comp.is_various(t)
    single = p['comp_single_artist'] and not va and comp.is_single_compilation(t)
    if not (va or single or (t.get('compilation') == ['1'])):
        return {}
    out = {}
    if t.get('compilation') != ['1'] and (va or single):
        out['compilation'] = ['1']
    if p['comp_extra_flags'] and os.path.splitext(str(path or ''))[1].lower() in comp.FREE_FORM:
        for k in comp.EXTRA_FLAGS:
            if _get(m, k) != ['1']:
                out[k] = ['1']
    if p['comp_grouping'] and not t.get('grouping'):
        out['grouping'] = ['Compilation']
    return out


def lrc_text(m) -> str:
    """A track's time-synced lyrics ("[00:12.34] ..."): LRCLIB's kept by the plugin, or the
    lyrics tag when it is in LRC form; "" for none."""
    synced = ''.join(_get(m, '~lyrics_synced'))
    if synced.strip():
        return synced
    text = '\n'.join(_get(m, 'lyrics'))
    return text if re.search(r'^\s*\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\]', text, re.M) else ''


# ---- reading the copy (worker thread) --------------------------------------------------------
def copy_facts(paths: list[str], folder: str, media: list[str], p: dict, release: dict) -> dict:
    """What the files and the folder say: {"hd": bool, "mark": "Vinyl Rip"|"Tape Rip"|"HD Audio"|"",
    "rip_line": the vinyl rip's comment line or ""}. Reads the files (bit depth, rate) and the
    folder's notes - on a worker, never Picard's main thread."""
    lossless = [x for x in paths if os.path.splitext(x)[1].lower() in tmarks.LOSSLESS]
    hd = any(tmarks.is_hd(x) for x in lossless[:HD_FILES])
    media, ignored = media_of_copy(media, folder)
    mark = tmarks.of_album([{'media': media}], [], folder) if paths or media else ''
    if hd and not mark:
        mark = tmarks.HD
    rip_line = ''
    if mark == tmarks.VINYL and p['vinyl_comment'] and folder:
        try:
            d = tvr.describe(folder, paths[:HD_FILES], catno=release.get('catno', ''),
                             comments=release.get('comments') or (), pressing_year=release.get('year', ''),
                             original_year=release.get('original_year', ''), artist=release.get('artist', ''),
                             scans='')
            rip_line = tvr.comment_line(d)
        except Exception:
            rip_line = ''
    return {'hd': hd, 'mark': mark, 'rip_line': rip_line, 'media_ignored': ignored}


def media_of_copy(media, folder: str) -> tuple[list, list]:
    """The release's media, less a vinyl or tape your copy cannot be: a folder with a CD rip
    log (one with the disc's table of contents: EAC, XLD, CUERipper ...) was ripped from a CD,
    whatever the release Picard matched says (Steve Howe - The Steve Howe Album: Picard matched
    the 1979 LP; the folder holds the 1995 CD and its EAC log). Not when the folder name says
    vinyl. Returns (media kept, media ignored)."""
    media = list(media or ())
    off = [m for m in media if mc.media_kind(m) in ('vinyl', 'cassette')]
    if not off or not folder or mc.folder_says_vinyl(os.path.basename(str(folder))):
        return media, []
    try:
        cd_log = any(log.get('toc') for log in tr.rip_logs(folder))
    except Exception:
        cd_log = False
    if not cd_log:
        return media, []
    return [m for m in media if m not in off], off


# ---- writing one metadata object -------------------------------------------------------------
def _put(m, key, values, changed) -> None:
    before = _get(m, key)
    if before == list(values):
        return
    if values:
        m[key] = list(values) if len(values) > 1 else values[0]
    else:
        m.delete(key)
    changed[key] = (before, list(values))


def apply_to(m, path: str, p: dict, facts: dict, previous_edition: str = '') -> tuple[dict, str]:
    """Every rule on one metadata object (the album's, a track's or a file's). Returns
    ({tag: (before, after)}, the edition now in its Album tag)."""
    changed: dict = {}
    # compilations first: Grouping "Compilation" before a copy's mark (TagMender's order)
    for k, v in compilation_tags(m, path, p).items():
        _put(m, k, v, changed)
    if p['grouping'] and facts.get('mark') and not _get(m, 'grouping'):
        _put(m, 'grouping', [facts['mark']], changed)
    ed = edition_of(m, p['edition'], facts.get('hd', False))
    album = ''.join(_get(m, 'album')[:1])
    if album:
        new = titled(album, ed, previous_edition)
        if new != album:
            _put(m, 'album', [new], changed)
    if p['version_tags'] and ed:
        _put(m, 'albumversion', [ed], changed)
        comment = _get(m, '~releasecomment')
        if comment:
            _put(m, 'releasecomment', comment, changed)
    # comments: removed (keeping TagMender's own vinyl-rip line), then the rip's line added
    keys = [k for k in _keys(m) if k == 'comment' or k.startswith('comment:')]
    if p['comments'] == 'remove':
        for k in keys:
            own = [ln for v in _get(m, k) for ln in re.split(r'\r?\n', v) if tvr.own_line(ln)]
            _put(m, k, own[:1], changed)
    line = facts.get('rip_line') or ''
    if line:
        _put(m, 'comment', tvr.merged_values(_get(m, 'comment'), line), changed)
    if p['genre_max'] or p['genre_map']:
        genres = _get(m, 'genre')
        if genres:
            _put(m, 'genre', map_genres(genres, p['genre_map'], p['genre_max']), changed)
    return changed, ed


def _keys(m) -> list[str]:
    try:
        return [str(k) for k in m.keys()]
    except Exception:
        return []


# ---- inside Picard ---------------------------------------------------------------------------
# per album: {"ref", "edition" (the Album tag's edition the plugin added), "busy", "again", "facts"}
_STATE: dict = {}
_LOCK = threading.Lock()


def _state(album) -> dict:
    s = _STATE.get(id(album))
    if s is None or s['ref']() is not album:
        if len(_STATE) > 1000:
            _STATE.clear()
        s = _STATE[id(album)] = {'ref': weakref.ref(album), 'edition': {}, 'busy': False, 'again': False,
                                 'facts': None}
    return s


def wanted(p: dict) -> bool:
    return (p['edition'] != 'off' or p['grouping'] or p['vinyl_comment'] or p['comments'] != 'keep'
            or p['comp_extra_flags'] or p['comp_grouping'] or p['comp_single_artist']
            or bool(p['genre_max']) or bool(p['genre_map']))


def refresh_late(track, targets_of, ui_refresh) -> None:
    """A file joined its track: the album's marks again (its bit depth, its comment)."""
    album = getattr(track, 'album', None)
    if album is None or getattr(album, 'metadata', None) is None:
        return
    st = _STATE.get(id(album))
    op = st.get('op') if st and st['ref']() is album else None
    if op is None:
        from .discogs_logging import plugin_log
        op = plugin_log.new_operation('copy')
    refresh(album, album.metadata, op, targets_of, ui_refresh)


def refresh(album, metadata, op, targets_of, ui_refresh, settings=None) -> None:
    """Read the copy (worker) and write every rule to the album, its tracks and their files.
    Called when MusicBrainz matched, after Discogs wrote its tags and when a file joins its
    track; one reading at a time per album (another request while one runs runs once after)."""
    from .discogs_http_bridge import Worker
    from .discogs_logging import plugin_log
    from .discogs_runtime import log
    if settings is None:
        from .discogs_runtime import settings as _s
        settings = _s
    p = prefs(settings)
    if not wanted(p):
        return
    st = _state(album)
    st['op'] = op
    if st['busy']:
        st['again'] = True
        return
    st['busy'] = True
    paths = _paths(album)
    folder = _folder(paths)
    release = {'catno': metadata.get('catalognumber') or '', 'year': (metadata.get('date') or '')[:4],
               'original_year': (metadata.get('originaldate') or metadata.get('originalyear') or '')[:4],
               'artist': metadata.get('albumartist') or '', 'comments': _get(metadata, 'comment')}
    media = _get(metadata, 'media')

    def job():
        return copy_facts(paths, folder, media, p, release)

    def done(result=None, error=None):
        st['busy'] = False
        try:
            if error is not None:
                plugin_log.event(op, f'your copy: not read ({error!r})')
                result = {'hd': False, 'mark': '', 'rip_line': ''}
            if not _alive(album):
                return
            st['facts'] = result
            _write(album, metadata, op, targets_of, ui_refresh, p, result, st)
        except Exception:
            log.error('Discogs Enrichment: writing the copy\'s marks failed', exc_info=True)
        finally:
            if st['again'] and _alive(album):
                st['again'] = False
                refresh(album, metadata, op, targets_of, ui_refresh, settings)

    Worker.shared('copy').submit(job, done)


def _write(album, metadata, op, targets_of, ui_refresh, p, facts, st) -> None:
    from .discogs_logging import plugin_log
    changed_any, ed_now = {}, ''
    for target, _index in targets_of(album, metadata):
        path = _path_of(album, target)
        key = id(target)
        changed, ed = apply_to(target, path, p, facts, st['edition'].get(key, '') or st['edition'].get('*', ''))
        st['edition'][key] = ed
        if target is metadata:
            ed_now = ed
            changed_any.update(changed)
    st['edition']['*'] = ed_now
    if facts.get('media_ignored') and not st.get('told_ignored'):
        st['told_ignored'] = True
        plugin_log.decision(op, f'your copy: the matched release is {", ".join(facts["media_ignored"])}, '
                                f'but your folder has a CD rip log - no vinyl or tape marks')
    if changed_any:
        what = ', '.join(f'{k}: {now}' for k, (_was, now) in sorted(changed_any.items()))
        plugin_log.decision(op, f'your copy: {what[:300]}')
        ui_refresh(album)


def _paths(album) -> list[str]:
    try:
        return [f.filename for f in album.iterfiles() if getattr(f, 'filename', None)]
    except Exception:
        return []


def _folder(paths) -> str:
    dirs = {os.path.dirname(x) for x in paths}
    if len(dirs) == 1:
        return next(iter(dirs))
    common = os.path.commonpath(list(dirs)) if dirs else ''
    return common if common and common != os.path.sep else ''


def _path_of(album, target) -> str:
    try:
        for f in album.iterfiles():
            if getattr(f, 'metadata', None) is target:
                return f.filename
    except Exception:
        pass
    return ''


def _alive(album) -> bool:
    try:
        return album.id in album.tagger.albums and album.tagger.albums[album.id] is album
    except Exception:
        return False


def write_lrc(file, settings=None) -> str:
    """After a save: the track's time-synced lyrics as "<track>.lrc" beside it, when the option
    is on and no .lrc is there. Returns the path written, or ""."""
    if settings is None:
        from .discogs_runtime import settings as _s
        settings = _s
    if not prefs(settings)['lrc']:
        return ''
    m = getattr(file, 'metadata', None)
    name = getattr(file, 'filename', '') or ''
    text = lrc_text(m) if m is not None else ''
    if not (text and name):
        return ''
    target = os.path.splitext(name)[0] + '.lrc'
    if os.path.exists(target):
        return ''
    with open(target, 'w', encoding='utf-8') as fh:
        fh.write(text if text.endswith('\n') else text + '\n')
    return target
