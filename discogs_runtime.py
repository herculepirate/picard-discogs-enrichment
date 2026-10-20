"""Late-bound access to the Picard 3 plugin API.

Picard 3 hands a plugin its `PluginApi` object once, as the argument to
`enable()`. Everything a plugin needs — settings, logging, the web service,
the tagger — hangs off that object, which means code that used to reach for
module-level `picard.config` and `picard.log` has nowhere to import from.

Threading the api object through every function would mean touching every
call site in the plugin, and every one of those edits is a chance to
introduce the kind of NameError that only shows up when a user clicks the
button that runs it. Instead `enable()` binds the api here once, and the
rest of the plugin imports these two proxies:

    from .discogs_runtime import log, settings

`settings['cover_art']` reads from the plugin's own private config section,
falling back to DEFAULTS. `log.debug(...)` forwards to the api's logger. Both
degrade to something harmless rather than raising when the api is not bound,
so importing any module in this package outside Picard — which is how the
test suite exercises the pure logic — still works.
"""

from __future__ import annotations

import logging
from typing import Any

# Defaults for every setting the plugin understands. Picard 3 has no
# module-level option declaration step (the 2.x `config.TextOption(...)`
# calls), so this dict IS the schema: a key absent from the user's config
# falls back to its value here.
#
# Keys are unprefixed. In Picard 2.x every option name needed a
# `discogs_enrichment_` prefix because all plugins shared one global
# namespace; `api.plugin_config` is already private to this plugin, so the
# prefix would just be noise in the config file.
DEFAULTS: dict[str, Any] = {
    'token': '',
    'import_notes': False,
    'merge_mode': 'combine',
    'reverse_match': True,
    'style_mode': 'separate',
    'protected_tags': '',
    'marketplace': False,
    'performers': True,
    'companies': True,
    'identifiers': True,
    'strictness': 'normal',
    'skip_preview': False,
    'auto_disc_map': False,
    'cover_art': True,
    'cover_art_scope': 'front',
    'use_cache': True,
    # Write a collaboration as one artist name per MusicBrainz artist ID
    # ("Max Corbacho", "Bruno Sanfilippo") instead of Picard's joined credit,
    # on every album, with or without a Discogs match. See README.
    'split_collaborations': True,
    'log_level': 'basic',
    'log_max_kb': '2048',
    'prefer_original_date': False,
    # Write track title/number/artist from the Discogs tracklist. Required
    # for "Discogs only" to produce usable files, since that mode clears the
    # MusicBrainz title along with everything else.
    'track_tags': True,
}


class _Settings:
    """Dict-style view over the plugin's private config section.

    Reads fall back to DEFAULTS, so a setting the user has never touched
    behaves as documented instead of raising KeyError. Writes go to
    `api.plugin_config`, which participates in Picard's option profiles.
    """

    def __init__(self) -> None:
        self._api = None
        self._fallback: dict[str, Any] = {}

    def bind(self, api) -> None:
        self._api = api
        # Every option MUST be registered before it can be read back.
        # ConfigSection.__getitem__() looks the name up in Picard's Option
        # registry and returns None for anything it does not find — so an
        # unregistered setting reads as empty no matter what was saved to
        # it, which is exactly how a stored Discogs token comes back blank.
        for key, default in DEFAULTS.items():
            try:
                api.plugin_config.register_option(key, default)
            except Exception:
                # Already registered (a disable/enable cycle re-runs this),
                # or this build names it differently. Either way the read
                # below still falls back to DEFAULTS.
                pass

    def unbind(self) -> None:
        self._api = None

    def __getitem__(self, key: str) -> Any:
        default = DEFAULTS.get(key)
        if self._api is None:
            # Outside Picard (tests, static analysis): behave as defaults.
            return self._fallback.get(key, default)
        # Subscript, NOT .get(): Picard's ConfigSection has no get() method.
        # Calling one and catching the AttributeError silently turned every
        # setting into its default — the bug that made a saved token vanish.
        value = self._api.plugin_config[key]
        # A setting stored before a default changed type can come back as the
        # wrong type; coerce booleans so `if settings['cover_art']` is sound.
        if isinstance(default, bool) and not isinstance(value, bool) and value is not None:
            return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
        return default if value is None else value

    def __setitem__(self, key: str, value: Any) -> None:
        if self._api is None:
            self._fallback[key] = value
            return
        self._api.plugin_config[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        value = self[key]
        return default if value is None else value

    def __contains__(self, key: str) -> bool:
        return key in DEFAULTS


class _Log:
    """Forwards to the api's logger, or to a plain logger when unbound."""

    _FALLBACK = logging.getLogger('discogs_enrichment')

    def __init__(self) -> None:
        self._api = None

    def bind(self, api) -> None:
        self._api = api

    def unbind(self) -> None:
        self._api = None

    def _target(self):
        if self._api is not None:
            try:
                return self._api.logger
            except Exception:
                pass
        return self._FALLBACK

    def __getattr__(self, name: str):
        # Logging must never be the thing that breaks tagging, so an
        # unexpected logger shape degrades to a no-op rather than raising.
        target = self._target()
        method = getattr(target, name, None)
        if method is None:
            return lambda *args, **kwargs: None
        return method


settings = _Settings()
log = _Log()

# The api object itself, for the few places that genuinely need more than
# settings and logging (the web service, the tagger, the plugin directory).
_api = None


def bind_api(api) -> None:
    global _api
    _api = api
    settings.bind(api)
    log.bind(api)


def unbind_api() -> None:
    global _api
    _api = None
    settings.unbind()
    log.unbind()


def get_api():
    """The bound PluginApi, or None outside Picard."""
    return _api
