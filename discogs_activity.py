"""Which albums the plugin is still working on, and at what - for the busy bar in Picard's
status bar and the "Discogs..." marker on the album's row.

An album is busy while any of its tasks is open: a Discogs request waiting for its answer, the
folder or scans being read on the worker, the evidence job waiting its turn behind other albums.
A callback that starts the next request before its own task closes keeps the album busy through
the whole chain, so no branch has to say "finished". An album waiting for you to choose its
pressing is marked so until you choose (or close the picker).

Pure Python, thread-safe: tasks open and close on the worker thread too. LISTENERS are called
(on whatever thread changed the state) after every change; the UI side moves them to the main
thread.
"""
from __future__ import annotations

import threading
import time
import weakref
from collections import Counter

STALE_S = 30 * 60          # a task open this long is forgotten (a reply that never came)

_LOCK = threading.RLock()
_ALBUMS: dict[int, dict] = {}
LISTENERS: list = []


def _notify() -> None:
    for fn in list(LISTENERS):
        try:
            fn()
        except Exception:
            pass


def _entry(album, title: str = '') -> dict:
    key = id(album)
    e = _ALBUMS.get(key)
    if e is None or e['ref']() is not album:
        try:
            ref = weakref.ref(album)
        except TypeError:
            ref = (lambda a=album: a)
        e = _ALBUMS[key] = {'ref': ref, 'title': '', 'tasks': {}, 'waiting': False, 'next': 0}
    if title:
        e['title'] = title
    return e


class Task:
    """One open piece of work on an album. stage() renames it, done() closes it (once)."""

    def __init__(self, album, stage: str, title: str = '') -> None:
        self._album_ref = weakref.ref(album) if _weakrefable(album) else (lambda a=album: a)
        self._key = id(album)
        with _LOCK:
            e = _entry(album, title)
            e['next'] += 1
            self._n = e['next']
            e['tasks'][self._n] = [stage, time.monotonic()]
        _notify()

    def stage(self, stage: str) -> None:
        with _LOCK:
            e = _ALBUMS.get(self._key)
            if not e or self._n not in e['tasks'] or e['tasks'][self._n][0] == stage:
                return
            e['tasks'][self._n] = [stage, time.monotonic()]
        _notify()

    def done(self) -> None:
        with _LOCK:
            e = _ALBUMS.get(self._key)
            if not e or e['tasks'].pop(self._n, None) is None:
                return
            if not e['tasks'] and not e['waiting']:
                del _ALBUMS[self._key]
        _notify()


def _weakrefable(obj) -> bool:
    try:
        weakref.ref(obj)
        return True
    except TypeError:
        return False


def start(album, stage: str, title: str = '') -> Task | None:
    """Open a task on ALBUM (None when there is no album: nothing is shown)."""
    if album is None:
        return None
    return Task(album, stage, title)


def wrap(album, stage: str, fn, title: str = ''):
    """FN, with a task open from now until FN has run (FN's own requests opened first)."""
    task = start(album, stage, title)

    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            if task:
                task.done()
    return _wrapped


def waiting(album, on: bool, title: str = '') -> None:
    """ALBUM waits for you to choose its pressing (ON), or no longer does."""
    if album is None:
        return
    with _LOCK:
        if on:
            _entry(album, title)['waiting'] = True
        else:
            e = _ALBUMS.get(id(album))
            if not e:
                return
            e['waiting'] = False
            if not e['tasks']:
                del _ALBUMS[id(album)]
    _notify()


def forget(album) -> None:
    with _LOCK:
        if _ALBUMS.pop(id(album), None) is None:
            return
    _notify()


def state_of(album) -> str:
    """"busy", "waiting" (for you) or ""."""
    with _LOCK:
        e = _ALBUMS.get(id(album))
        if not e or e['ref']() is not album:
            return ''
        if e['tasks']:
            return 'busy'
        return 'waiting' if e['waiting'] else ''


def prune(alive=None) -> bool:
    """Forget albums gone (ALIVE(album) False, or collected) and tasks open past STALE_S.
    True when anything was forgotten."""
    now = time.monotonic()
    changed = False
    with _LOCK:
        for key, e in list(_ALBUMS.items()):
            album = e['ref']()
            if album is None or (alive is not None and not _safe(alive, album)):
                del _ALBUMS[key]
                changed = True
                continue
            for n, (_stage, since) in list(e['tasks'].items()):
                if now - since > STALE_S:
                    del e['tasks'][n]
                    changed = True
            if not e['tasks'] and not e['waiting']:
                del _ALBUMS[key]
                changed = True
    if changed:
        _notify()
    return changed


def _safe(fn, album) -> bool:
    try:
        return bool(fn(album))
    except Exception:
        return False


def snapshot() -> list[tuple]:
    """[(album, title, [stage, ...], waiting)] of every album worked on or waiting for you."""
    out = []
    with _LOCK:
        for e in _ALBUMS.values():
            album = e['ref']()
            if album is None:
                continue
            stages = list(Counter(s for s, _t in e['tasks'].values()))
            out.append((album, e['title'], stages, e['waiting']))
    return out


def summary() -> tuple[int, int, str]:
    """(albums busy, albums waiting for you, a tooltip listing them)."""
    busy = waiting_n = 0
    lines = []
    for _album, title, stages, wait in snapshot():
        name = title or 'an album'
        if stages:
            busy += 1
            lines.append(f'{name}: {", ".join(stages)}')
        elif wait:
            waiting_n += 1
            lines.append(f'{name}: waiting for you to choose the pressing')
    return busy, waiting_n, '\n'.join(lines)
