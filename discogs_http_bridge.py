"""TagMender's synchronous http (get_json / get_text) over Picard's web service.

TagMender's look-up code (discogs_search, toc_releases ...) asks for a URL and waits for the
answer. Picard's web service is asynchronous and lives on the main thread. So that code runs on
the plugin's own worker thread (Worker), and each request it makes is handed to the main thread
(picard.util.thread.to_main), sent through Picard's web service - its rate limits (one request
a second per host), retries and the plugin's Discogs response cache - and waited for.

A Discogs budget caps the requests one album may make; once it is spent get_json returns
None, which TagMender's code reads as "nothing found" (it then decides with what it has).
Cached answers cost nothing.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from PyQt6.QtCore import QUrl

from picard.util.thread import to_main
from picard.webservice import WSRequest

from .discogs_client import _RESPONSE_CACHE, DISCOGS_HOST, USER_AGENT
from .discogs_runtime import log

# Discogs requests one automatic look-up may make (searches and release details); Picard
# sends them one a second, which is Discogs' limit of 60 a minute.
AUTO_BUDGET = 15
# How long one request is waited for before it counts as failed.
TIMEOUT_S = 90


class PicardHttp:
    """get_json(url, host) / get_text(url, host) for TagMender's code, on the worker thread.

    host "discogs": through the plugin's DiscogsClient (token, cache, log); "mb" and "ctdb":
    Picard's web service directly. `releases` keeps every Discogs release fetched, by id."""

    def __init__(self, client, webservice, budget: int = AUTO_BUDGET) -> None:
        self.client = client
        self.webservice = webservice
        self.budget = budget
        self.used = 0
        self.spent = False
        self.releases: dict[str, dict] = {}

    def _wait(self, start: Callable[[Callable], None]) -> tuple[Any, Any]:
        """Run START(done) on the main thread; block until done(document, reply, error)."""
        if threading.current_thread() is threading.main_thread():
            raise RuntimeError('PicardHttp must be used from a worker thread')
        finished = threading.Event()
        box: dict[str, Any] = {}

        def done(document, reply, error):
            box['document'], box['error'] = document, error
            finished.set()

        def begin():
            try:
                start(done)
            except Exception as exc:          # (never left waiting for a request never sent)
                box['document'], box['error'] = None, exc
                finished.set()

        to_main(begin)
        if not finished.wait(TIMEOUT_S):
            log.warning('Discogs Enrichment: no answer within %d s; carrying on without it', TIMEOUT_S)
            return None, 'timeout'
        return box.get('document'), box.get('error')

    def get_json(self, url: str, host: str) -> dict | None:
        if host == 'discogs' and self.client is None:
            return None                               # (no token: Discogs is not asked)
        if host == 'discogs':
            path = url.split(DISCOGS_HOST, 1)[1] if DISCOGS_HOST in url else url
            if path not in _RESPONSE_CACHE:
                if self.used >= self.budget:
                    self.spent = True
                    return None
                self.used += 1
            document, error = self._wait(lambda done: self.client._get(path, done))
            if error or not isinstance(document, dict):
                return None
            if path.startswith('/releases/') and document.get('id'):
                self.releases[str(document['id'])] = document
            return document
        document, error = self._wait(lambda done: self._send(url, 'json', done))
        return document if not error and isinstance(document, dict) else None

    def get_text(self, url: str, host: str) -> str:
        document, error = self._wait(lambda done: self._send(url, None, done))
        if error or document is None:
            return ''
        if isinstance(document, (bytes, bytearray)):
            return bytes(document).decode('utf-8', 'replace')
        return str(document)

    def _send(self, url: str, parse: str | None, done: Callable) -> None:
        self.webservice.add_request(WSRequest(
            method='GET', url=QUrl(url), handler=done, parse_response_type=parse,
            priority=False, important=False, headers={'User-Agent': USER_AGENT}))


class Worker:
    """One thread for the plugin's look-ups, albums in turn: one album's requests at a time
    keep Discogs' rate steady, and Picard's own thread pool (file loading) is never blocked
    by a look-up waiting on the network."""

    _instances: dict[str, 'Worker'] = {}

    def __init__(self, name: str = 'discogs') -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f'discogs-enrichment-{name}', daemon=True)
        self._thread.start()

    @classmethod
    def shared(cls, name: str = 'discogs') -> 'Worker':
        """The worker of that name: "discogs" (folder, scans, Discogs), "web" (web sources: never
        held up behind a long scan reading)."""
        w = cls._instances.get(name)
        if w is None or not w._thread.is_alive():
            w = cls._instances[name] = cls(name)
        return w

    def submit(self, job: Callable[[], Any], then: Callable[..., None]) -> None:
        """Run JOB on the worker; then THEN(result=...) or THEN(error=...) on the main thread."""
        self._jobs.put((job, then))

    def pending(self) -> int:
        return self._jobs.qsize()

    def _run(self) -> None:
        while True:
            job, then = self._jobs.get()
            try:
                result = job()
            except Exception as exc:
                log.error('Discogs Enrichment: look-up failed', exc_info=True)
                to_main(then, error=exc)
            else:
                to_main(then, result=result)
