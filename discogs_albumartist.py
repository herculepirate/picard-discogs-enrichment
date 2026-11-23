"""The album artist as TagMender files it (its album_artist.py, copied as tm_album_artist.py):
the lead artist alone, while the Artist tag keeps the whole credit.

  Lead artist only       a joint credit's first artist is the album artist ("Thomas Strønen &
                         Time Is a Blind Guide" -> "Thomas Strønen"), unless it is a
                         collaboration of equals ("Calexico and Iron & Wine": kept whole)
  Bands named after      a band whose name is one of its members' name and more ("Pat Metheny
  a member               Group", "The Jimi Hendrix Experience", "Steve Morse Band") is filed
                         under that member - only when MusicBrainz (else Discogs) lists that
                         person as a member: "Deep Purple" and "The Marshall Tucker Band" stay
  Your own album         "Credit = Album artist" lines over both rules ("Dregs = Dixie Dregs";
  artists                "Ben Folds Five = Ben Folds Five" keeps one as credited)

Only the album artist (and its sort name and MusicBrainz ID) changes; the Artist tags keep the
credit as it is. Various-artists releases and classical albums are never changed. When
MusicBrainz cannot be asked, the album artist is left as it is.

The pure part (tags_of, profile, apply_choice) needs no Picard; settle() and the late-file hook
run inside Picard: the members are asked on the plugin's "artist" worker, through Picard's web
service, and the answer is written to the album, its tracks and their files.
"""
from __future__ import annotations

import threading
import weakref

try:
    from . import tm_album_artist as aa
except ImportError:  # run outside Picard (tests)
    import tm_album_artist as aa

# the tags written for the album artist (the Artist tags are never touched)
NAME_TAGS = ('albumartist',)
LIST_TAGS = ('albumartists', '~albumartists')
SORT_TAGS = ('albumartistsort',)
SORTS_TAGS = ('~albumartists_sort',)
ID_TAG = 'musicbrainz_albumartistid'


def parse_file_under(text: str) -> dict:
    """"Credit = Album artist" lines (also "Credit -> Album artist") as {credit: album artist}."""
    out = {}
    for line in str(text or '').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        for sep in ('->', '=>', '='):
            if sep in line:
                k, v = (x.strip() for x in line.split(sep, 1))
                if k and v:
                    out[k] = v
                break
    return out


def profile(settings) -> dict:
    """TagMender's profile group "album_artist" from the plugin's settings."""
    return {'album_artist': {'lead': bool(settings.get('aa_lead')),
                             'bands': bool(settings.get('aa_bands', True)),
                             'file_under': parse_file_under(settings.get('aa_file_under') or '')}}


def _getall(metadata, key) -> list[str]:
    try:
        values = metadata.getall(key)
    except AttributeError:
        values = metadata.get(key) or []
        values = values if isinstance(values, list) else [values]
    return [str(v) for v in values if str(v).strip()]


def credit_text(release) -> str:
    """MusicBrainz's album credit as written ("Pat Metheny & Lyle Mays with Naná Vasconcelos")."""
    credit = (release or {}).get('artist-credit') or []
    return ''.join(f"{c.get('name') or (c.get('artist') or {}).get('name') or ''}{c.get('joinphrase') or ''}"
                   for c in credit if isinstance(c, dict)).strip()


def tags_of(metadata, credit: str = '') -> dict:
    """The album's tags as TagMender's choose() reads them."""
    t = {}
    for key in ('albumartist', 'albumartistsort', ID_TAG, 'genre', 'composer', 'releasetype',
                'compilation'):
        v = _getall(metadata, key)
        if v:
            t[key] = v
    names = _getall(metadata, 'albumartists') or _getall(metadata, '~albumartists')
    if names:
        t['albumartists'] = names
    sorts = _getall(metadata, '~albumartists_sort')
    if sorts:
        t['~albumartists_sort'] = sorts
    if credit:
        t['~albumartist_credit'] = [credit]
    return t


def apply_choice(metadata, lead) -> dict:
    """Write the chosen album artist LEAD (name, sort, MusicBrainz ID, Discogs ID) to one
    metadata object. An unknown sort name or ID is taken off rather than left naming the whole
    credit. Returns {tag: (before, after)} of what changed."""
    name, sort, mbid = lead[0], lead[1] or '', lead[2] or ''
    changed = {}

    def put(key, values):
        before = _getall(metadata, key)
        if before == values:
            return
        if values:
            metadata[key] = values if len(values) > 1 else values[0]
        else:
            metadata.delete(key)
        changed[key] = (before, values)

    for key in NAME_TAGS:
        put(key, [name])
    for key in LIST_TAGS:
        if _getall(metadata, key):
            put(key, [name])
    for key in SORT_TAGS:
        put(key, [sort] if sort else [])
    for key in SORTS_TAGS:
        if _getall(metadata, key) or sort:
            put(key, [sort] if sort else [])
    put(ID_TAG, [mbid] if mbid else [])
    return changed


class Lookups:
    """TagMender's members_of / discogs_members_of / find_ids over one http (get_json_or_raise
    for MusicBrainz), each answer kept for the session: an artist is asked once."""

    _cache: dict = {}
    _lock = threading.Lock()

    def __init__(self, http) -> None:
        self.http = http

    def _kept(self, key, compute):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        value = compute()
        if value is not None:                      # (an outage is asked again next time)
            with self._lock:
                if len(self._cache) > 2000:
                    self._cache.clear()
                self._cache[key] = value
        return value

    def _mb(self):
        """TagMender's http for its MusicBrainz look-ups: raises when MusicBrainz is not reached."""
        http = self.http

        class Strict:
            @staticmethod
            def get_json(url, host):
                return http.get_json_or_raise(url) if host == 'mb' else http.get_json(url, host)
        return Strict()

    def members_of(self, mbid):
        return self._kept(('mb', mbid), lambda: aa.band_members(self._mb(), mbid))

    def discogs_members_of(self, dgid):
        return self._kept(('dg', dgid), lambda: aa.discogs_members(self.http, dgid))

    def find_ids(self, name, mbid):
        def compute():
            found = mbid or aa.artist_named(self._mb(), name)
            return (found or '', aa.linked_discogs(self._mb(), found) if found else '')
        return self._kept(('ids', aa.fold(name), mbid), compute)


def choose(t: dict, prof: dict, lookups: Lookups | None):
    """TagMender's choice for tags T: (name, sort, MusicBrainz ID, Discogs ID), aa.UNKNOWN, or
    None (the credit stays)."""
    if lookups is None:
        return aa.choose(t, prof, None, None, None, [t])
    return aa.choose(t, prof, lookups.members_of, lookups.discogs_members_of, lookups.find_ids, [t])


# ---- inside Picard ---------------------------------------------------------------------------
# the choice made for each album: {id(album): (weakref, lead)}, for files that attach later
_CHOSEN: dict = {}


def settle(album, metadata, release, op, targets_of, refresh, client=None, settings=None) -> None:
    """Choose the album artist on the "artist" worker and write it to the album, its tracks and
    their files. Called when MusicBrainz matched the album and again after Discogs wrote its
    tags (Discogs may write its own credit)."""
    from .discogs_http_bridge import PicardHttp, Worker
    from .discogs_logging import plugin_log
    from .discogs_runtime import log
    if settings is None:
        from .discogs_runtime import settings as _s
        settings = _s
    prof = profile(settings)
    if not prof['album_artist']['lead']:
        return
    t = tags_of(metadata, credit_text(release))
    if not t.get('albumartist'):
        return
    before = list(t['albumartist'])
    webservice = album.tagger.webservice

    def job():
        return choose(t, prof, Lookups(PicardHttp(client, webservice)))

    def done(result=None, error=None):
        if error is not None:
            plugin_log.event(op, f'album artist: not chosen ({error!r}); kept as credited')
            return
        if not _alive(album):
            return
        if _getall(metadata, 'albumartist') != before:
            return                    # (changed meanwhile - Discogs: its own settle follows)
        if result is None:
            plugin_log.detail(op, f'album artist: {before} kept (TagMender\'s rule keeps the credit)')
            return
        if result == aa.UNKNOWN:
            plugin_log.event(op, f'album artist: {before} kept - MusicBrainz could not be asked '
                                 f'for the band\'s members')
            return
        _CHOSEN[id(album)] = (weakref.ref(album), result)
        changed = {}
        for target, _index in targets_of(album, metadata):
            changed.update(apply_choice(target, result))
        plugin_log.decision(op, f'album artist: {before} -> {result[0]!r} (the Artist tags keep the credit)')
        for k, (was, now) in sorted(changed.items()):
            plugin_log.detail(op, f'  {k}: {was} -> {now}')
        try:
            refresh(album)
        except Exception:
            log.debug('Discogs Enrichment: album refresh failed', exc_info=True)

    Worker.shared('artist').submit(job, done)


def _alive(album) -> bool:
    try:
        return album.id in album.tagger.albums and album.tagger.albums[album.id] is album
    except Exception:
        return False


def apply_to_late_file(track, file) -> None:
    """A file that attaches to its track after the album artist was chosen gets it too."""
    album = getattr(track, 'album', None)
    entry = _CHOSEN.get(id(album)) if album is not None else None
    if not entry:
        return
    ref, lead = entry
    if ref() is not album:
        _CHOSEN.pop(id(album), None)
        return
    metadata = getattr(file, 'metadata', None)
    if metadata is not None:
        apply_choice(metadata, lead)
