"""Discogs API client for Picard 3's webservice module.

Verified against picard/webservice/__init__.py in 3.0.2: WSRequest.__init__()
accepts a `headers` dict, so the Authorization and User-Agent headers go in
directly. (Picard 2.x had no such argument and needed the inherited
QNetworkRequest.setRawHeader() instead — that workaround is gone.)

One 2.x hazard has NOT gone away: WebService._handle_reply() still calls
request.handler(...) with no try/except, from a Qt slot. See _never_raise().
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtNetwork import QNetworkRequest

from .discogs_runtime import log, settings

from .discogs_logging import plugin_log, timed
from .discogs_mapping import (
    artist_from_mb_url_lookup,
    discogs_release_artists,
    format_artist_credit,
    release_artists_for,
    remember_release_artists,
)
from picard.webservice import WSRequest

DISCOGS_HOST = 'api.discogs.com'
MUSICBRAINZ_HOST = 'musicbrainz.org'
USER_AGENT = 'PicardDiscogsEnrichment/0.3.6 +https://github.com/herculepirate/picard-discogs-enrichment'


# Process-wide response cache. Tagging a box set, re-running a lookup, or
# fetching cover art for a release already tagged all hit the same few
# endpoints repeatedly. Discogs allows 60 requests/minute, so avoiding
# repeats is the single cheapest way to stay under the limit.
_RESPONSE_CACHE: dict = {}
_CACHE_LIMIT = 256


# Discogs artist id -> {'mbid', 'name', 'sort'} or None (no MusicBrainz artist links to it)
_ARTIST_MBID_CACHE: dict = {}
_ARTIST_LOOKUP_TIMEOUT_MS = 30000


def clear_cache() -> None:
    """Drop everything cached. Exposed so a user can force fresh data."""
    _RESPONSE_CACHE.clear()
    _ARTIST_MBID_CACHE.clear()


class DiscogsClient:
    """Thin wrapper around a Picard WebService instance for Discogs calls."""

    def __init__(self, webservice, token: str) -> None:
        self._webservice = webservice
        self._token = token

    def find_musicbrainz_release(self, discogs_release_id, handler) -> None:
        """Reverse lookup: which MusicBrainz release links to this Discogs release?

        MusicBrainz indexes external URLs, so asking its /url endpoint for
        the Discogs release page returns the release(s) related to it. This
        is the same relationship used in the forward direction, read the
        other way round, so it is exact rather than a fuzzy title match.

        Calls handler(mbid_or_None). Note this queries musicbrainz.org, not
        Discogs, so it carries no Discogs auth and no Discogs rate cost.
        """
        op = plugin_log.new_operation('mb-reverse')
        resource = f'https://www.discogs.com/release/{discogs_release_id}'
        path = ('/ws/2/url?resource=' + quote(resource, safe='')
                + '&inc=release-rels&fmt=json')
        url = QUrl(f'https://{MUSICBRAINZ_HOST}{path}')

        def _on_response(document, reply, error):
            if error or not isinstance(document, dict):
                # A 404 here is normal and expected: it just means nobody has
                # linked this Discogs release to MusicBrainz.
                log.debug('Discogs Enrichment: no MusicBrainz URL entry for Discogs '
                          'release %s', discogs_release_id)
                handler(None)
                return
            for relation in document.get('relations') or []:
                if relation.get('target-type') != 'release':
                    continue
                release = relation.get('release') or {}
                if release.get('id'):
                    plugin_log.response(op, 'MusicBrainz', ok=True,
                                        detail=f"linked release {release['id']}")
                    handler(release['id'])
                    return
            plugin_log.response(op, 'MusicBrainz', ok=True,
                                detail='no release linked to this Discogs release')
            handler(None)

        request = WSRequest(
            method='GET',
            url=url,
            handler=self._never_raise(_on_response),
            parse_response_type='json',
            priority=True,
            important=False,
        )
        plugin_log.request(op, 'MusicBrainz', path)
        log.debug('Discogs Enrichment: MusicBrainz reverse lookup %s', url.toString())
        self._webservice.add_request(request)

    @staticmethod
    def _never_raise(handler: Callable) -> Callable:
        """Guarantee that nothing this plugin does can escape into Qt.

        Picard's WebService._handle_reply() invokes request.handler(...)
        with NO try/except, and it runs as a Qt slot. PyQt6 calls qFatal()
        on an unhandled Python exception inside a slot, which aborts the
        whole process — so a plain bug in a callback here does not raise a
        catchable error, it hard-crashes Picard. Every handler this client
        hands to WSRequest therefore goes through this wrapper.
        """
        def _inner(document, reply, error):
            try:
                handler(document, reply, error)
            except Exception:
                log.error('Discogs Enrichment: error handling Discogs reply',
                          exc_info=True)
        return _inner

    def _get(self, path: str, handler: Callable, use_cache: bool = True) -> None:
        # Honour the user's setting; the parameter stays so callers can force
        # a fresh fetch regardless.
        try:
            use_cache = use_cache and bool(settings['use_cache'])
        except Exception:
            pass
        op = getattr(self, 'op', None) or plugin_log.new_operation('discogs')
        if use_cache and path in _RESPONSE_CACHE:
            plugin_log.request(op, 'Discogs', path, cached=True)
            log.debug('Discogs Enrichment: cache hit for %s', path)
            # Deliver on the next event-loop turn, exactly like a live reply.
            # Calling back synchronously breaks callers that return WAIT and
            # expect the answer later: Picard's cover art queue is a running
            # generator, and a provider that calls next_in_queue() from
            # inside queue_images() gets "generator already executing" and
            # stalls the album's whole cover art pipeline.
            document = _RESPONSE_CACHE[path]
            QTimer.singleShot(0, lambda: self._never_raise(handler)(document, None, None))
            return

        url = QUrl(f'https://{DISCOGS_HOST}{path}')
        clock = timed()
        clock.__enter__()

        def _caching_handler(document, reply, error):
            clock.__exit__()
            if error:
                status = ''
                try:
                    status = reply.attribute(
                        QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else ''
                except Exception:
                    pass
                plugin_log.response(op, 'Discogs', ok=False,
                                    detail=f'HTTP {status}: {error}',
                                    seconds=clock.seconds)
            else:
                shape = ''
                if isinstance(document, dict):
                    if 'results' in document:
                        shape = f"{len(document.get('results') or [])} result(s)"
                    elif 'id' in document:
                        shape = f"release {document.get('id')}"
                    if plugin_log.detailed():
                        shape += f' | keys: {sorted(document)[:12]}'
                plugin_log.response(op, 'Discogs', ok=True, detail=shape,
                                    seconds=clock.seconds)
            if use_cache and not error and isinstance(document, dict):
                if len(_RESPONSE_CACHE) >= _CACHE_LIMIT:
                    _RESPONSE_CACHE.clear()
                _RESPONSE_CACHE[path] = document
            handler(document, reply, error)

        request = WSRequest(
            method='GET',
            url=url,
            handler=self._never_raise(_caching_handler),
            parse_response_type='json',
            priority=True,
            important=False,
            # Discogs requires a descriptive User-Agent and returns images
            # only to authenticated requests, so both headers matter.
            headers={
                'User-Agent': USER_AGENT,
                'Authorization': f'Discogs token={self._token}',
            },
        )
        plugin_log.request(op, 'Discogs', path)
        log.debug('Discogs Enrichment: GET %s', url.toString())
        self._webservice.add_request(request)

    # Every /database/search parameter this plugin exposes, mapped from the
    # name used in the search dialog to the name Discogs expects. Confirmed
    # against Discogs' API reference for /database/search; anything not in
    # this map is silently ignored rather than sent, so a typo in a caller
    # cannot turn into an HTTP 500 from Discogs.
    SEARCH_PARAMS: dict[str, str] = {
        'query': 'q',
        'artist': 'artist',
        'release_title': 'release_title',
        'year': 'year',
        'catalog_number': 'catno',
        'barcode': 'barcode',
        'label': 'label',
        'format': 'format',
        'country': 'country',
        'genre': 'genre',
        'style': 'style',
        'track': 'track',
        'credit': 'credit',
    }

    def search_advanced(
        self,
        *,
        handler: Callable[[list[dict[str, Any]]], None],
        per_page: int = 50,
        release_type: str = 'release',
        **terms: str | None,
    ) -> None:
        """Search Discogs using any combination of its real search fields
        at once \u2014 unlike search_release()'s barcode > catno+artist > title
        cascade (which tries one strategy at a time for the automatic
        backfill), this combines whatever the user filled in on the
        manual search dialog into a single query.

        Accepts any key in SEARCH_PARAMS as a keyword argument; unknown keys
        and blank values are dropped. `release_type` is 'release' (specific
        pressings, the default and what tagging needs) or 'master' (one entry
        per work, for browsing which pressings exist).
        """
        params: dict[str, str] = {
            'type': release_type if release_type in ('release', 'master') else 'release',
            'per_page': str(per_page),
        }
        # Insertion order here is the order they appear in the query string,
        # which keeps the logged URL readable and the response cache key
        # stable for the same set of terms.
        for name, discogs_name in self.SEARCH_PARAMS.items():
            value = terms.get(name)
            if value is None:
                continue
            value = str(value).strip()
            if value:
                params[discogs_name] = value

        unknown = set(terms) - set(self.SEARCH_PARAMS)
        if unknown:
            log.warning('Discogs Enrichment: ignoring unsupported search '
                        'term(s) %s', sorted(unknown))

        if len(params) <= 2:  # only 'type' and 'per_page', nothing real to search on
            handler([])
            return

        query_string = '&'.join(f'{k}={quote(v, safe="")}' for k, v in params.items())

        def _on_response(document, reply, error):
            if error:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else '?'
                log.warning('Discogs Enrichment: advanced search failed (HTTP %s): %s', status, error)
                handler([])
                return
            if not isinstance(document, dict):
                log.warning('Discogs Enrichment: advanced search response was not JSON: %r', document)
                handler([])
                return
            results = document.get('results') or []
            log.debug('Discogs Enrichment: advanced search returned %d result(s)', len(results))
            handler(results)

        self._get(f'/database/search?{query_string}', _on_response)

    def search_by_query(
        self,
        *,
        query: str,
        handler: Callable[[list[dict[str, Any]]], None],
        per_page: int = 25,
    ) -> None:
        """Free-text Discogs search, used by the manual picker when the user
        types a search term instead of relying on MusicBrainz's tags.
        """
        params = {'q': query, 'type': 'release', 'per_page': str(per_page)}
        query_string = '&'.join(f'{k}={quote(v, safe="")}' for k, v in params.items())

        def _on_response(document, reply, error):
            if error:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else '?'
                log.warning('Discogs Enrichment: free-text search failed (HTTP %s): %s', status, error)
                handler([])
                return
            if not isinstance(document, dict):
                log.warning('Discogs Enrichment: free-text search response was not JSON: %r', document)
                handler([])
                return
            results = document.get('results') or []
            log.debug('Discogs Enrichment: free-text search returned %d result(s)', len(results))
            handler(results)

        self._get(f'/database/search?{query_string}', _on_response)

    def search_releases(
        self,
        *,
        barcode: str | None,
        catalog_number: str | None,
        artist: str | None,
        release_title: str | None,
        handler: Callable[[list[dict[str, Any]]], None],
        per_page: int = 25,
    ) -> None:
        """Search Discogs for candidate releases.

        Query preference: barcode (highest confidence) > catalog number +
        artist > artist + title. Calls handler(results_list); list may be
        empty.
        """
        params: dict[str, str] = {'type': 'release', 'per_page': str(per_page)}
        if barcode:
            params['barcode'] = barcode
        elif catalog_number and artist:
            params['catno'] = catalog_number
            params['artist'] = artist
        elif artist and release_title:
            params['artist'] = artist
            params['release_title'] = release_title
        else:
            handler([])
            return

        query_string = '&'.join(f'{k}={quote(v, safe="")}' for k, v in params.items())

        def _on_response(document, reply, error):
            if error:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else '?'
                log.warning('Discogs Enrichment: search request failed (HTTP %s): %s', status, error)
                handler([])
                return
            if not isinstance(document, dict):
                log.warning('Discogs Enrichment: search response was not JSON as expected: %r', document)
                handler([])
                return
            results = document.get('results') or []
            log.debug('Discogs Enrichment: search returned %d result(s)', len(results))
            handler(results)

        self._get(f'/database/search?{query_string}', _on_response)

    def search_releases_cascading(
        self,
        *,
        barcode: str | None,
        catalog_number: str | None,
        artist: str | None,
        release_title: str | None,
        handler: Callable[[list[dict[str, Any]]], None],
    ) -> None:
        """Try each search strategy in turn and hand back the FULL result
        list from the first one that returns anything.

        Same cascade as search_release() (barcode, then catalogue number +
        artist, then artist + title, since Discogs' barcode field is
        community-entered and often absent), but the caller gets every
        candidate rather than just the top hit — needed so candidates can
        be scored against the MusicBrainz release instead of trusting
        Discogs' relevance ranking.
        """
        strategies = []
        if barcode:
            strategies.append({'barcode': barcode})
        if catalog_number and artist:
            strategies.append({'catalog_number': catalog_number, 'artist': artist})
        if artist and release_title:
            strategies.append({'artist': artist, 'release_title': release_title})

        if not strategies:
            handler([])
            return

        def _try(index: int):
            if index >= len(strategies):
                handler([])
                return
            kwargs = {'barcode': None, 'catalog_number': None,
                      'artist': None, 'release_title': None}
            kwargs.update(strategies[index])

            def _on_results(results):
                if results:
                    handler(results)
                else:
                    log.debug('Discogs Enrichment: strategy %r found nothing, '
                              'trying next fallback', strategies[index])
                    _try(index + 1)

            self.search_releases(**kwargs, handler=_on_results)

        _try(0)

    # Order in which search terms are given up when a combined query returns
    # nothing, least useful first. Discogs ANDs every field, so one term the
    # database spells differently ("Europe" where the user typed "Germany",
    # a label credited under a subsidiary name) sinks the whole query. Year
    # goes early because a reissue's year is the single most common mismatch;
    # barcode and catalogue number go last because they are the terms most
    # likely to be right and most likely to pin the exact pressing.
    _BROADENING_ORDER: tuple[str, ...] = (
        'query', 'style', 'genre', 'credit', 'track',
        'country', 'format', 'label', 'year', 'artist',
        'release_title', 'catalog_number', 'barcode',
    )

    def search_broadening(
        self,
        *,
        handler: Callable[[list[dict[str, Any]]], None],
        on_relaxed: Callable[[list[str]], None] | None = None,
        per_page: int = 50,
        release_type: str = 'release',
        **terms: str | None,
    ) -> None:
        """Search, then progressively drop terms until something comes back.

        A search that returns nothing is the most common way the manual
        lookup fails, and the cause is almost always one over-specific term
        rather than the release being absent. Instead of reporting "no
        matches" and making the user guess which field to clear, this drops
        terms one at a time in _BROADENING_ORDER and retries.

        The retries are sequential, each one only firing after the previous
        came back empty, so a successful first query costs exactly one
        request — the same as search_advanced().

        `on_relaxed` is called with the list of dropped term names before
        the results are handed over, so the caller can tell the user the
        search was widened and why its results may look loose. It is not
        called when the original query succeeded.
        """
        active = {name: str(value).strip()
                  for name, value in terms.items()
                  if name in self.SEARCH_PARAMS and value and str(value).strip()}
        if not active:
            handler([])
            return

        # Only terms actually supplied can be dropped, and at least one must
        # survive — an empty query would match the whole database.
        droppable = [name for name in self._BROADENING_ORDER if name in active]
        droppable = droppable[:-1] if len(droppable) == len(active) else droppable
        dropped: list[str] = []

        def _attempt(current: dict) -> None:
            def _on_results(results):
                if results:
                    if dropped and on_relaxed is not None:
                        try:
                            on_relaxed(list(dropped))
                        except Exception:
                            log.warning('Discogs Enrichment: on_relaxed callback '
                                        'failed', exc_info=True)
                    handler(results)
                    return
                while droppable:
                    name = droppable.pop(0)
                    if name in current:
                        dropped.append(name)
                        relaxed = {k: v for k, v in current.items() if k != name}
                        log.debug('Discogs Enrichment: nothing found, dropping '
                                  '%r and retrying with %s', name, sorted(relaxed))
                        _attempt(relaxed)
                        return
                handler([])

            self.search_advanced(handler=_on_results, per_page=per_page,
                                 release_type=release_type, **current)

        _attempt(active)

    def search_release(
        self,
        *,
        barcode: str | None,
        catalog_number: str | None,
        artist: str | None,
        release_title: str | None,
        handler: Callable[[dict[str, Any] | None], None],
    ) -> None:
        """Try barcode first; if that finds nothing, fall back to catalog
        number + artist, then artist + title — rather than giving up after
        one strategy. Discogs' barcode field is community-entered and often
        incomplete even when the release itself is catalogued, so a barcode
        miss does not mean the release isn't there.
        """
        strategies = []
        if barcode:
            strategies.append({'barcode': barcode})
        if catalog_number and artist:
            strategies.append({'catalog_number': catalog_number, 'artist': artist})
        if artist and release_title:
            strategies.append({'artist': artist, 'release_title': release_title})

        if not strategies:
            handler(None)
            return

        def _try_next(index: int):
            if index >= len(strategies):
                handler(None)
                return
            kwargs = {'barcode': None, 'catalog_number': None, 'artist': None, 'release_title': None}
            kwargs.update(strategies[index])

            def _on_results(results: list[dict[str, Any]]):
                if results:
                    handler(results[0])
                else:
                    log.debug(
                        'Discogs Enrichment: strategy %r found nothing, trying next fallback',
                        strategies[index],
                    )
                    _try_next(index + 1)

            self.search_releases(**kwargs, handler=_on_results)

        _try_next(0)

    def get_release(self, resource_url_or_id, handler: Callable[[dict[str, Any] | None], None]) -> None:
        """Fetch full release detail (search results only give a summary)."""
        if isinstance(resource_url_or_id, str) and resource_url_or_id.startswith('http'):
            path = resource_url_or_id.split(DISCOGS_HOST, 1)[-1]
        else:
            path = f'/releases/{resource_url_or_id}'

        def _on_response(document, reply, error):
            if error:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else '?'
                log.warning('Discogs Enrichment: release fetch failed (HTTP %s): %s', status, error)
                handler(None)
                return
            if not isinstance(document, dict):
                log.warning('Discogs Enrichment: release response was not JSON as expected: %r', document)
                handler(None)
                return
            # Before handing the release over, find the MusicBrainz ID of each of its
            # artists, so a collaborator MusicBrainz's credit is missing can be added
            # with the right ID (see settle_artist_credits()).
            self.resolve_release_artists(document, lambda: handler(document))

        self._get(path, _on_response)

    def resolve_release_artists(self, release: dict, done: Callable[[], None]) -> None:
        """Look up the MusicBrainz artist linked to each Discogs release artist (by the
        artist's Discogs page, exact), remember the result for the release, then call
        done() exactly once. Releases with one artist need nothing. Any failure, or no
        answer within 30 s, still calls done(): the lookup can only add information."""
        state = {'called': False}

        def finish():
            if not state['called']:
                state['called'] = True
                done()

        try:
            release_id = release.get('id')
            artists = discogs_release_artists(release)
            if len(artists) < 2 or release_artists_for(release_id) is not None:
                finish()
                return
            results: list = [None] * len(artists)
            pending = {'n': len(artists)}

            def store(index, found):
                results[index] = found
                pending['n'] -= 1
                if pending['n'] == 0 and not state['called']:
                    resolved = [{'mbid': (r or {}).get('mbid', ''),
                                 'name': (r or {}).get('name') or name,
                                 'sort': (r or {}).get('sort', '')}
                                for r, (_aid, name) in zip(results, artists)]
                    remember_release_artists(release_id, {
                        'credit': format_artist_credit(release.get('artists')),
                        'artists': resolved})
                    missing = [name for r, (_aid, name) in zip(results, artists) if not r]
                    log.debug('Discogs Enrichment: MusicBrainz artists for Discogs release %s: '
                              '%d of %d found%s', release_id, len(artists) - len(missing),
                              len(artists), f' (not linked: {missing})' if missing else '')
                    finish()

            QTimer.singleShot(_ARTIST_LOOKUP_TIMEOUT_MS, finish)
            for index, (artist_id, _name) in enumerate(artists):
                self.find_musicbrainz_artist(artist_id, lambda found, i=index: store(i, found))
        except Exception:
            log.debug('Discogs Enrichment: artist lookup failed', exc_info=True)
            finish()

    def find_musicbrainz_artist(self, discogs_artist_id, handler) -> None:
        """Which MusicBrainz artist links to this Discogs artist page? handler(dict|None).
        Exact (MusicBrainz's own URL relationship), cached, and costs no Discogs requests."""
        if discogs_artist_id in _ARTIST_MBID_CACHE:
            found = _ARTIST_MBID_CACHE[discogs_artist_id]
            QTimer.singleShot(0, lambda: handler(found))
            return
        resource = f'https://www.discogs.com/artist/{discogs_artist_id}'
        path = '/ws/2/url?resource=' + quote(resource, safe='') + '&inc=artist-rels&fmt=json'

        def _on_response(document, reply, error):
            found = None if error else artist_from_mb_url_lookup(document)
            _ARTIST_MBID_CACHE[discogs_artist_id] = found
            handler(found)

        request = WSRequest(
            method='GET',
            url=QUrl(f'https://{MUSICBRAINZ_HOST}{path}'),
            handler=self._never_raise(_on_response),
            parse_response_type='json',
            priority=True,
            important=False,
        )
        log.debug('Discogs Enrichment: MusicBrainz artist lookup for Discogs artist %s', discogs_artist_id)
        self._webservice.add_request(request)

    def get_master(self, master_id, handler: Callable[[dict[str, Any] | None], None]) -> None:
        """Fetch a Discogs master release — used to find the earliest known
        release year for a work, distinct from a specific pressing's own
        'released' date (which may be a much later reissue).
        """
        def _on_response(document, reply, error):
            if error:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) if reply else '?'
                log.warning('Discogs Enrichment: master fetch failed (HTTP %s): %s', status, error)
                handler(None)
                return
            if not isinstance(document, dict):
                log.warning('Discogs Enrichment: master response was not JSON as expected: %r', document)
                handler(None)
                return
            handler(document)

        self._get(f'/masters/{master_id}', _on_response)
