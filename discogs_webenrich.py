"""The web sources inside Picard: filling what MusicBrainz and Discogs leave empty, lyrics, web
cover art, and tagging an album no database has (discogs_web holds the pure logic).

Everything web runs on its own worker ("web"), its requests through Picard's web service (one a
second per site), so it never waits behind a scan reading or a Discogs look-up.
"""
from __future__ import annotations

import threading
import weakref

from PyQt6 import QtWidgets

from picard.plugin3.api import BaseAction, CoverArtImage, CoverArtProvider

from . import discogs_web as dw
from .discogs_client import DiscogsClient
from .discogs_evidence import album_location, read_copy
from .discogs_http_bridge import PicardHttp, Worker
from .discogs_logging import plugin_log
from .discogs_runtime import log, settings

_QUEUE_STATE = getattr(CoverArtProvider, 'QueueState', CoverArtProvider)
FINISHED = _QUEUE_STATE.FINISHED
WAIT = _QUEUE_STATE.WAIT

# what the web filled on each album: {id(album): (weakref, {tag: values}, {track index: {tag: v}})}
_FILLED: dict = {}
_FILLED_LIMIT = 500
# one web look-up per album, shared by the metadata processor and the cover provider
_FOUND: dict = {}
_FOUND_LOCK = threading.Lock()


def web_on() -> bool:
    return bool(settings.get('web_sources'))


def sources() -> list[str]:
    raw = str(settings.get('web_source_list') or '')
    names = [x.strip() for x in raw.replace(';', ',').split(',') if x.strip()]
    return names or list(dw.SOURCE_NAMES)


def _paths(album) -> list[str]:
    try:
        return [f.filename for f in album.iterfiles() if getattr(f, 'filename', None)]
    except Exception:
        return []


def _client(album):
    token = settings.get('token')
    return DiscogsClient(album.tagger.webservice, token) if token else None


def _album_for_proof(paths, release, artist, title) -> dict:
    """Your files as TagMender reads them, named as Picard matched them; else the matched
    release's own tracks and lengths."""
    loc = album_location(paths) if paths else None
    if loc is not None:
        a = read_copy(loc)
        if a.get('tracks'):
            if artist and title:
                a['artist'], a['title'] = artist, title
            return a
    return dw.release_album(release, artist, title)


def found_for(key, compute) -> dict:
    """compute() once per KEY (on the web worker; the processor and the cover provider share it)."""
    with _FOUND_LOCK:
        if key in _FOUND:
            return _FOUND[key]
    value = compute()
    with _FOUND_LOCK:
        if len(_FOUND) > 200:
            _FOUND.clear()
        _FOUND[key] = value
    return value


def _find(album, release, artist, title, paths, op):
    http = PicardHttp(_client(album), album.tagger.webservice)
    a = _album_for_proof(paths, release, artist, title)
    return found_for((getattr(album, 'id', ''), artist, title),
                     lambda: dw.find(http, a, sources(), log=lambda m: log.info('Discogs Enrichment: %s', m)))


def _alive(album) -> bool:
    """Still registered with the tagger (as the plugin's _album_is_alive)."""
    try:
        return album.id in album.tagger.albums
    except Exception:
        return False


# ---- the album metadata processor's part -------------------------------------------------
def enrich_album(album, metadata, release, op, targets_of, refresh) -> None:
    """Start the web look-ups for an album Picard matched (when switched on). targets_of(album,
    metadata) -> [(metadata, track index or None)], refresh(album): the plugin's own helpers."""
    want_fill = web_on() and bool(settings.get('web_fill_tags', True))
    want_lyrics = bool(settings.get('web_lyrics'))
    if not (want_fill or want_lyrics):
        return
    artist = metadata.get('albumartist') or metadata.get('artist') or ''
    title = metadata.get('album') or ''
    paths = _paths(album)
    tracks = []
    for i, t in enumerate(getattr(album, 'tracks', None) or []):
        m = getattr(t, 'metadata', None)
        if m is None:
            continue
        secs = (getattr(m, 'length', 0) or 0) / 1000.0
        tracks.append((i, m.get('artist') or artist, m.get('title') or '', secs, bool(m.get('lyrics'))))
    if not tracks:
        rel_tracks = dw.release_album(release, artist, title)['tracks']
        tracks = [(i, t['artist'] or artist, t['title'], t['secs'], False) for i, t in enumerate(rel_tracks)]

    def job():
        out = {'found': None, 'lyrics': {}}
        if want_fill:
            out['found'] = _find(album, release, artist, title, paths, op)
        if want_lyrics:
            http = PicardHttp(None, album.tagger.webservice)
            for i, a, t, secs, has in tracks:
                if has or not t or not secs:
                    continue
                try:
                    hit = dw.lyrics(http, a, t, title, secs)
                except Exception:
                    log.debug('Discogs Enrichment: LRCLIB look-up failed', exc_info=True)
                    hit = None
                if hit:
                    out['lyrics'][i] = hit
        return out

    def done(result=None, error=None):
        if error is not None or not _alive(album):
            if error is not None:
                plugin_log.event(op, f'web sources failed: {error!r}')
            return
        try:
            _apply(album, metadata, op, result, targets_of, refresh)
        except Exception:
            log.error('Discogs Enrichment: applying web data failed', exc_info=True)

    Worker.shared('web').submit(job, done)


def _apply(album, metadata, op, result, targets_of, refresh) -> None:
    found = result.get('found')
    fills = {}
    if found is not None:
        if found.get('failed'):
            plugin_log.detail(op, f'web: not reached: {", ".join(found["failed"])}')
        best = found.get('best')
        if best:
            c = best['cand']
            fills = dw.fill_tags(c)
            plugin_log.event(op, f'web: {found["source"]} has this album ({c.get("url")}, '
                                 f'{best["confidence"]:.0f}%): may fill {sorted(fills) or "nothing"}')
        elif found.get('near'):
            name, near, why = found['near']
            plugin_log.decision(op, f'web: no source proves the album (nearest: {name} '
                                    f'{near["confidence"]:.0f}%, {"; ".join(why[:2])})')
        else:
            plugin_log.decision(op, 'web: no source has the album')
    synced = bool(settings.get('web_synced_lyrics'))
    per_track = {}
    for i, hit in (result.get('lyrics') or {}).items():
        text = (hit.get('synced') if synced and hit.get('synced') else hit.get('plain')) or ''
        if text.strip():
            per_track[i] = {'lyrics': text}
            if hit.get('synced'):
                # (never saved: a hidden variable; the .lrc beside the track is written from it -
                # discogs_copy.write_lrc)
                per_track[i]['~lyrics_synced'] = hit['synced']
    if result.get('lyrics') is not None and settings.get('web_lyrics'):
        plugin_log.event(op, f'lyrics (LRCLIB): {len(per_track)} track(s)')
    album_done, track_done = {}, {}
    for target, index in targets_of(album, metadata):
        for tag, values in fills.items():
            if not target.getall(tag):
                target[tag] = values
                album_done[tag] = values
        for tag, value in (per_track.get(index) or {}).items() if index is not None else ():
            if not target.getall(tag):
                target[tag] = value
                track_done.setdefault(index, {})[tag] = value
    if album_done or track_done:
        for tag, values in sorted(album_done.items()):
            plugin_log.detail(op, f'  web filled {tag}: {values}')
        _remember(album, album_done, track_done)
        refresh(album)


def _remember(album, fills, per_track) -> None:
    if len(_FILLED) >= _FILLED_LIMIT:
        _FILLED.clear()
    try:
        _FILLED[id(album)] = (weakref.ref(album), dict(fills), dict(per_track))
    except TypeError:
        pass


def _filled(album):
    entry = _FILLED.get(id(album))
    if not entry or entry[0]() is not album:
        return None
    return entry


def drop_web_fills(album, targets, supplied) -> None:
    """Before Discogs writes FIELDS: a tag the web filled and Discogs supplies is Discogs' now
    (the web only fills what nothing else gives)."""
    entry = _filled(album)
    if not entry:
        return
    _ref, fills, _per = entry
    for tag in [t for t in fills if t in supplied]:
        for target, _i in targets:
            if list(target.getall(tag)) == list(fills[tag]):
                del target[tag]
        fills.pop(tag, None)


def apply_to_late_file(track, file) -> None:
    """A file joining its track later gets what the web filled too."""
    album = getattr(track, 'album', None)
    entry = _filled(album) if album is not None else None
    m = getattr(file, 'metadata', None)
    if not entry or m is None:
        return
    _ref, fills, per_track = entry
    try:
        index = list(album.tracks).index(track)
    except Exception:
        index = None
    for tag, values in list(fills.items()) + list((per_track.get(index) or {}).items()):
        if not m.getall(tag):
            m[tag] = values


# ---- cover art -----------------------------------------------------------------------------
class CoverArtProviderWeb(CoverArtProvider):
    """The front cover (and VGMdb's scans) of the web album that proves to be this one."""
    NAME = 'Discogs Enrichment: web'
    TITLE = 'Web (Deezer, iTunes, Bandcamp, VGMdb)'

    def enabled(self):
        if not (web_on() and settings.get('web_cover_art', True)):
            return False
        return super().enabled()            # (not when an earlier provider gave a front cover)

    def queue_images(self):
        self.op = plugin_log.new_operation('art')
        album, release = self.album, self.release
        artist = self.metadata.get('albumartist') or self.metadata.get('artist') or ''
        title = self.metadata.get('album') or ''
        paths = _paths(album)
        Worker.shared('web').submit(lambda: _find(album, release, artist, title, paths, self.op), self._found)
        return WAIT

    def _found(self, result=None, error=None):
        try:
            best = (result or {}).get('best')
            if best:
                urls = dw.cover_urls(best['cand'])
                scope = settings.get('cover_art_scope') or 'front'
                if scope == 'front':
                    urls = [u for u in urls if u[1] == 'front'][:1]
                elif scope == 'front_back':
                    urls = [u for u in urls if u[1] in ('front', 'back')]
                for url, kind in urls:
                    self.queue_put(CoverArtImage(url=url, types=[kind], comment=result.get('source') or 'web'))
                plugin_log.event(self.op, f'cover art: {len(urls)} image(s) from {result.get("source")}')
        except Exception:
            log.error('Discogs Enrichment: web cover art failed', exc_info=True)
        finally:
            try:
                self.next_in_queue()
            except Exception:
                log.error('Discogs Enrichment: web cover art could not continue', exc_info=True)


# ---- albums no database has ----------------------------------------------------------------
class WebTagClusterAction(BaseAction):
    """Tag a cluster MusicBrainz has no release for from the first web source that proves it
    (artist, title, track count and every track's length): TagMender's web_tag."""
    TITLE = 'Tag from the web…'

    def callback(self, objs):
        files = []
        for obj in objs:
            files += list(getattr(obj, 'files', None) or ())
        by_path = {getattr(f, 'filename', None): f for f in files if getattr(f, 'filename', None)}
        parent = getattr(self.tagger, 'window', None)
        loc = album_location(list(by_path))
        if loc is None:
            QtWidgets.QMessageBox.information(parent, 'Discogs Enrichment',
                                              'These files are not one album folder: nothing to look up.')
            return
        op = plugin_log.new_operation('web')
        names = sources()
        tagger = self.tagger
        client = DiscogsClient(tagger.webservice, settings['token']) if settings.get('token') else None

        def job():
            album = read_copy(loc)
            http = PicardHttp(client, tagger.webservice)
            res = dw.find(http, album, names, log=lambda m: plugin_log.detail(op, m))
            rows = []
            if res.get('best'):
                rows = dw.album_tags(album, res['best']['cand'], http, res['source'])
            return album, res, rows

        def done(result=None, error=None):
            if error is not None:
                QtWidgets.QMessageBox.warning(parent, 'Discogs Enrichment', f'The web look-up failed: {error}')
                return
            album, res, rows = result
            best = res.get('best')
            if not best:
                near = res.get('near')
                why = (f'The nearest was {near[0]}’s “{near[1]["cand"].get("title")}” at '
                       f'{near[1]["confidence"]:.0f}%: {"; ".join(near[2][:2])}.') if near else 'None of them has it.'
                QtWidgets.QMessageBox.information(
                    parent, 'Discogs Enrichment',
                    f'No web source ({", ".join(names)}) proves this album.\n\n{why}')
                return
            c = best['cand']
            tracks = album.get('tracks') or []
            msg = (f'{res["source"]}: {c.get("artist")} — {c.get("title")} ({c.get("date") or c.get("year")})\n'
                   f'{len(c.get("tracks") or ())} tracks, {best["confidence"]:.0f}% — every track’s length fits.\n'
                   f'{c.get("url")}\n\nWrite its tags to these {len(tracks)} file(s)? (Save in Picard to keep them.)')
            if QtWidgets.QMessageBox.question(parent, 'Tag from the web', msg) != \
                    QtWidgets.QMessageBox.StandardButton.Yes:
                return
            written = 0
            for t, row in zip(tracks, rows):
                f = by_path.get(t.get('path'))
                if f is None:
                    continue
                for tag, value in row.items():
                    f.metadata[tag] = value
                try:
                    f.update()
                except Exception:
                    pass
                written += 1
            plugin_log.event(op, f'tagged {written} file(s) from {res["source"]} ({c.get("url")})')

        Worker.shared('web').submit(job, done)
