"""Troubleshooting log for the Discogs Enrichment plugin.

Picard's own log is shared by everything and is cleared on restart, which
makes it poor for diagnosing an intermittent tagging problem. This writes a
dedicated, rotating file that survives restarts and can be handed over
whole when something goes wrong.

Design decisions, and why:

* **Separate file, not Picard's log.** Picard's log viewer filters by level
  and drops old lines; a user reporting "it tagged the wrong pressing an
  hour ago" has usually lost the evidence by the time they look.

* **Rotating, size-capped.** A log that grows without bound gets deleted by
  the user, or fills a disk. RotatingFileHandler overwrites the oldest data
  once the cap is reached, so the file is always bounded and always
  contains the most recent activity.

* **Correlation ids.** One album load fans out into several asynchronous
  requests whose replies interleave with other albums'. Without a shared id
  per operation the file is unreadable. Every line carries one.

* **Timing on every request.** "Discogs is slow" and "Discogs returned
  nothing" look identical in a log without durations.

* **The token is never written.** It is redacted from URLs and headers even
  at the most verbose level, so a log can be shared safely.

* **Two levels, not five.** BASIC answers "what did it decide and why";
  DETAILED adds payload shapes and full decision scoring. More levels would
  just mean picking the wrong one.
"""

from __future__ import annotations

import itertools
import logging
import os
import platform
import re
import time
from logging.handlers import RotatingFileHandler

LEVEL_OFF = 'off'
LEVEL_BASIC = 'basic'
LEVEL_DETAILED = 'detailed'
LEVELS = (LEVEL_OFF, LEVEL_BASIC, LEVEL_DETAILED)

LOG_FILENAME = 'discogs_enrichment.log'
_BACKUP_COUNT = 1  # one rollover file, so the cap is ~2x the configured size

_counter = itertools.count(1)


def _user_dir() -> str:
    try:
        from picard.const import USER_DIR
        return USER_DIR
    except Exception:
        return os.path.expanduser('~')


def log_path() -> str:
    return os.path.join(_user_dir(), LOG_FILENAME)


class PluginLog:
    """Thin wrapper over a rotating logger, shaped for troubleshooting."""

    def __init__(self):
        self._logger = None
        self._level = LEVEL_OFF
        self._max_bytes = 2 * 1024 * 1024
        self._token = ''
        self._session_written = False

    # -- configuration -------------------------------------------------

    def configure(self, level: str, max_bytes: int, token: str = '') -> None:
        """Apply settings. Rebuilds the handler only when something that
        affects the file itself has changed.
        """
        self._token = token or ''
        rebuild = (self._logger is None) or (max_bytes != self._max_bytes)
        self._level = level if level in LEVELS else LEVEL_OFF
        self._max_bytes = max_bytes
        if self._level == LEVEL_OFF:
            return
        if rebuild:
            self._build()

    def _build(self) -> None:
        try:
            logger = logging.getLogger('discogs_enrichment')
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
            handler = RotatingFileHandler(
                log_path(), maxBytes=self._max_bytes,
                backupCount=_BACKUP_COUNT, encoding='utf-8')
            handler.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)-5s %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'))
            logger.addHandler(handler)
            self._logger = logger
            self._session_written = False
        except Exception:
            # Logging must never break tagging.
            self._logger = None

    # -- helpers -------------------------------------------------------

    def enabled(self) -> bool:
        return self._level != LEVEL_OFF and self._logger is not None

    def detailed(self) -> bool:
        return self._level == LEVEL_DETAILED

    def new_operation(self, what: str) -> str:
        """An id to tie together every line of one logical operation."""
        return f'{what}#{next(_counter):04d}'

    def redact(self, text) -> str:
        """Strip the Discogs token from anything before it is written."""
        text = str(text)
        if self._token:
            text = text.replace(self._token, '<TOKEN>')
        return re.sub(r'(token=)[^\s&\'"]+', r'\1<TOKEN>', text)

    def _write(self, level, message) -> None:
        if not self.enabled():
            return
        try:
            self._ensure_session()
            self._logger.log(level, self.redact(message))
        except Exception:
            pass

    def _ensure_session(self) -> None:
        if self._session_written:
            return
        self._session_written = True
        try:
            from picard import PICARD_VERSION_STR
            picard_version = PICARD_VERSION_STR
        except Exception:
            picard_version = 'unknown'
        self._logger.info('=' * 72)
        self._logger.info('Session start | Picard %s | Python %s | %s',
                          picard_version, platform.python_version(),
                          platform.platform())
        self._logger.info('Log level: %s | max size: %d KiB',
                          self._level, self._max_bytes // 1024)

    # -- the API the plugin uses ---------------------------------------

    def settings_snapshot(self, settings: dict) -> None:
        """Record the configuration in force. Most "it did the wrong thing"
        reports are explained by a setting, so this is written once per
        session rather than being asked for later.
        """
        if not self.enabled():
            return
        pairs = ', '.join(f'{k}={v}' for k, v in sorted(settings.items()))
        self._write(logging.INFO, f'Settings: {pairs}')

    def event(self, op: str, message: str) -> None:
        self._write(logging.INFO, f'[{op}] {message}')

    def detail(self, op: str, message: str) -> None:
        """Only written at DETAILED level."""
        if self.detailed():
            self._write(logging.DEBUG, f'[{op}] {message}')

    def request(self, op: str, source: str, url: str, cached: bool = False) -> None:
        tag = 'CACHE' if cached else 'GET  '
        self._write(logging.INFO, f'[{op}] {tag} {source}: {url}')

    def response(self, op: str, source: str, *, ok: bool, detail: str = '',
                 seconds: float | None = None) -> None:
        status = 'OK  ' if ok else 'FAIL'
        timing = f' ({seconds:.2f}s)' if seconds is not None else ''
        self._write(logging.INFO if ok else logging.WARNING,
                    f'[{op}] {status} {source}{timing} {detail}'.rstrip())

    def decision(self, op: str, message: str) -> None:
        """Why a particular release/pressing/field outcome was chosen. These
        are the lines that actually answer "why did it do that".
        """
        self._write(logging.INFO, f'[{op}] DECIDE {message}')

    def failure(self, op: str, message: str, exc_info=True) -> None:
        if not self.enabled():
            return
        try:
            self._ensure_session()
            self._logger.error(self.redact(f'[{op}] {message}'), exc_info=exc_info)
        except Exception:
            pass

    # -- maintenance ---------------------------------------------------

    def clear(self) -> bool:
        try:
            if self._logger:
                for handler in self._logger.handlers:
                    try:
                        handler.close()
                    except Exception:
                        pass
                self._logger.handlers.clear()
            for path in (log_path(), log_path() + '.1'):
                if os.path.exists(path):
                    os.remove(path)
            self._logger = None
            if self._level != LEVEL_OFF:
                self._build()
            return True
        except Exception:
            return False

    def size_bytes(self) -> int:
        total = 0
        for path in (log_path(), log_path() + '.1'):
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total


plugin_log = PluginLog()


class timed:
    """Context manager measuring how long a request took.

    Used so the log can distinguish "Discogs was slow" from "Discogs
    returned nothing", which otherwise look the same.
    """

    def __init__(self):
        self.seconds = None
        self._start = None

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.seconds = time.monotonic() - self._start
        return False
