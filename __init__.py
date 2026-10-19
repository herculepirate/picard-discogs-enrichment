# -*- coding: utf-8 -*-
"""Discogs Enrichment plugin for Picard 2.x (legacy plugin API).

Ported from a Picard 3.x plugin3 build after discovering the target
install (flatpak org.musicbrainz.Picard 2.13.3) predates the plugin3 API
entirely — 3.0 is still release-candidate, not the stable channel. This
version targets the actual legacy API confirmed against Picard 2.13.3's
real source (picard/plugin.py, picard/metadata.py, picard/config.py,
picard/webservice/__init__.py).

MusicBrainz stays the primary source of truth. After Picard loads an
album's MusicBrainz metadata, this plugin fetches the matching Discogs
release and fills in only the fields MB left empty, plus always adds
Discogs-only supplementary fields (style, catalog number, notes).

Known limitation vs. the 3.x version: the legacy API has no equivalent of
add_album_task/complete_album_task, so there is no way for a plugin to
hold an album in a "loading" state until an async fetch completes. This
version fires the Discogs lookup asynchronously inside the metadata
processor and calls album.update() to refresh the UI once it resolves —
enrichment typically lands within a second or two after the album shows
as loaded, not strictly before. If you save immediately after an album
appears loaded (e.g. via a batch "save all"), the Discogs fields may not
have landed yet. This plugin does not (yet) include the manual "Choose
Discogs release..." picker or the change-preview dialog from the 3.x
version — those relied on plugin3-only UI registration hooks.
"""

import os
import re
import weakref

from PyQt6 import QtCore, QtWidgets

from .discogs_runtime import bind_api, get_api, log, settings, unbind_api

# Picard 3 serves every plugin base class from one module, so a plugin no
# longer reaches into picard.ui.* or picard.metadata for them. Registration
# is done through the api object handed to enable(), not by importing
# register_* functions.
from picard.plugin3.api import BaseAction, OptionsPage

from .discogs_client import DiscogsClient, clear_cache
from .discogs_logging import (LEVEL_BASIC, LEVEL_DETAILED, LEVEL_OFF,
                              log_path, plugin_log)
from .discogs_coverart import ART_ALL, ART_FRONT, ART_FRONT_BACK
from .discogs_mapping import (
    drop_stale_artist_companions,
    snapshot_artist_names,
    MODE_COMBINE,
    MODE_DISCOGS_ONLY,
    MODE_FILL_GAPS,
    MODE_MB_ONLY,
    MODE_OVERRIDE,
    PROTECTED_IN_BOXSET,
    compute_field_diff,
    discogs_release_id_from_mb,
    align_tracks_to_files,
    extract_disc_fields,
    extract_track_fields,
    extract_discogs_fields,
    extract_original_year,
    is_probable_boxset,
    is_supplementary_field,
    STRICTNESS_LENIENT,
    STRICTNESS_NORMAL,
    STRICTNESS_STRICT,
    STYLE_BOTH,
    STYLE_GENRE,
    STYLE_SEPARATE,
    apply_style_mode,
    filter_fields,
    parse_protected_tags,
    parse_discs,
    rank_release_candidates,
    rank_release_candidates_detailed,
    release_disc_count,
    resolve_album_and_subtitle,
    threshold_for_strictness,
    resolve_field_value,
    stale_discogs_tags,
    tags_to_clear,
)
from .discogs_disc_dialog import CONFIDENT_DISC_SCORE, DiscMappingDialog, auto_map
from .discogs_picker_dialog import DiscogsPickerDialog
from .discogs_preview_dialog import DiscogsPreviewDialog
from .discogs_search_dialog import DiscogsSearchDialog

# Matches a Discogs release code bare ("R1234567", "r1234567"), wrapped in
# the square brackets Discogs' own "Copy Release Code" button produces
# ("[r1234567]"), or as a plain number ("1234567") — the last being what
# people naturally copy out of a Discogs URL.
_DISCOGS_RELEASE_ID_RE = re.compile(r'^\[?[Rr]?(\d+)\]?$')
# Matches discogs.com/release/1234567-... or discogs.com/de/release/1234567-...
_DISCOGS_RELEASE_URL_RE = re.compile(r'discogs\.com/(?:[a-zA-Z-]+/)?release/(\d+)')
# A MASTER release code/URL ("[m3816174]", discogs.com/master/3816174-...).
# A master is not a release: it groups every pressing of a work and has no
# per-pressing data (no barcode, catalog number, country, credits). This
# plugin fetches releases, so a master reference is detected only to give
# a clear message rather than silently looking up the wrong ID number.
_DISCOGS_MASTER_ID_RE = re.compile(r'^\[?[Mm](\d+)\]?$')
_DISCOGS_MASTER_URL_RE = re.compile(r'discogs\.com/(?:[a-zA-Z-]+/)?master/(\d+)')


def _extract_release_id(text: str) -> str | None:
    """Pull a Discogs RELEASE id out of whatever was pasted — a bare number
    ("1234567"), a bare or bracketed release code ("R1234567",
    "[r1234567]"), or a full release URL. Returns None if nothing
    recognizable is found.
    """
    text = (text or '').strip()
    if not text:
        return None
    url_match = _DISCOGS_RELEASE_URL_RE.search(text)
    if url_match:
        return url_match.group(1)
    id_match = _DISCOGS_RELEASE_ID_RE.match(text)
    if id_match:
        return id_match.group(1)
    return None


def _pair_label(owner, metadata) -> str:
    """Human-readable label for one selected album/cluster/file, used in the
    disc-mapping dialog so the user can tell their items apart.
    """
    album = metadata.get('album') or ''
    disc = metadata.get('discnumber') or ''
    artist = metadata.get('albumartist') or metadata.get('artist') or ''
    parts = [p for p in (album, artist) if p]
    label = ' — '.join(parts) if parts else str(owner)
    if disc:
        label = f'{label}  [disc {disc}]'
    return label


def _extract_master_id(text: str) -> str | None:
    """Detect a Discogs MASTER reference ("[m3816174]" or a /master/ URL).
    Used only to warn the user, since this plugin applies release-level
    data and a master has none of the per-pressing fields.
    """
    text = (text or '').strip()
    if not text:
        return None
    url_match = _DISCOGS_MASTER_URL_RE.search(text)
    if url_match:
        return url_match.group(1)
    id_match = _DISCOGS_MASTER_ID_RE.match(text)
    if id_match:
        return id_match.group(1)
    return None


# Unmissable marker, logged at ERROR level (always visible regardless of the
# log viewer's verbosity filter) the instant this module is imported. If
# this line doesn't appear after a fresh Picard start, the plugin code is
# not being imported at all — a loading/registration problem, not a Discogs
# API problem. If it DOES appear but process_album() below still logs
# nothing when an album loads, that's a different, more specific bug.
log.debug('Discogs Enrichment: module imported')

# Picard 3 reads a plugin's name, version, authors, licence and supported
# API from MANIFEST.toml beside this file. The PLUGIN_* module constants
# that Picard 2.x used were removed in 3.0 and are no longer read.

# Config options live in a single flat 'setting' namespace shared with core
# Picard and every other plugin — prefixed to avoid collisions.
# One of MODE_FILL_GAPS / MODE_OVERRIDE / MODE_COMBINE (see discogs_mapping).
# Default is combine: keeps both sources' data as a multi-value tag with
# Discogs listed first (most readers treat the first value as primary),
# rather than silently discarding whichever value doesn't get used.
# After the user picks a Discogs release for unmatched files, look up which
# MusicBrainz release corresponds to it and offer to match to that, so the
# files end up with BOTH sources' tags instead of Discogs data alone.
# Output filtering
# Matching / workflow
# Cover art
# Caching
# Troubleshooting log


def _configure_log() -> None:
    """Push the current settings into the logger. Called before any work so
    a level change takes effect without restarting Picard."""
    try:
        max_kb = int(settings['log_max_kb'] or 2048)
    except (TypeError, ValueError):
        max_kb = 2048
    # One rollover file is kept, so the bytes-per-file limit is half the
    # figure shown in the settings — that way "2 MB" means 2 MB on disk in
    # total, not 2 MB plus a 2 MB backup.
    plugin_log.configure(
        settings['log_level'] or LEVEL_BASIC,
        max(64, max_kb) * 1024 // 2,
        settings['token'] or '',
    )


def _log_settings_snapshot() -> None:
    plugin_log.settings_snapshot({
        'tag_source': settings['merge_mode'],
        'strictness': settings['strictness'],
        'styles': settings['style_mode'],
        'protected': settings['protected_tags'] or '(none)',
        'marketplace': settings['marketplace'],
        'performers': settings['performers'],
        'companies': settings['companies'],
        'identifiers': settings['identifiers'],
        'original_date': settings['prefer_original_date'],
        'reverse_match': settings['reverse_match'],
        'cover_art': settings['cover_art'],
        'cover_scope': settings['cover_art_scope'],
        'cache': settings['use_cache'],
        'token_set': bool(settings['token']),
    })


def _shape_fields(fields: dict) -> dict:
    """Apply the user's output preferences to a freshly extracted field set.

    Done in one place so the automatic and manual paths cannot drift apart.
    """
    fields = apply_style_mode(fields, settings['style_mode'] or STYLE_SEPARATE)
    return filter_fields(
        fields,
        marketplace=bool(settings['marketplace']),
        performers=bool(settings['performers']),
        companies=bool(settings['companies']),
        identifiers=bool(settings['identifiers']),
        protected=parse_protected_tags(settings['protected_tags']),
    )
# Off by default, which matches MusicBrainz/Picard convention: `date` is
# the date of the release you actually own (a 2017 box, a 2023 repress),
# and `originaldate`/`originalyear` carry the work's original date. Both
# are always populated. Turn this on only if you specifically want the
# original date in `date` itself — note it will then also override a box
# set's own year, so files get filed under the original album year.


# Search-term names as the user saw them on the search dialog, so a message
# about a widened search names the field they actually filled in rather than
# the API parameter behind it.
_SEARCH_TERM_LABELS = {
    'query': 'free text',
    'artist': 'artist',
    'release_title': 'album title',
    'year': 'year',
    'catalog_number': 'catalogue number',
    'barcode': 'barcode',
    'label': 'label',
    'country': 'country',
    'format': 'format',
    'genre': 'genre',
    'style': 'style',
    'track': 'track title',
    'credit': 'credited person',
}


def _mb_match_facts(metadata) -> dict:
    """The MusicBrainz release facts used to identify the same pressing on
    Discogs when no explicit MusicBrainz->Discogs link exists.
    """
    return {
        'barcode': metadata.get('barcode') or '',
        'catalognumber': metadata.get('catalognumber') or '',
        'date': metadata.get('date') or '',
        'releasecountry': metadata.get('releasecountry') or '',
        'media': metadata.get('media') or '',
    }


def _clear_stale_discogs_tags(metadata, op: str = 'mb-only') -> int:
    """MusicBrainz-only mode: withdraw tags a previous Discogs run added."""
    protected = parse_protected_tags(settings['protected_tags'])
    try:
        existing = list(metadata.keys())
    except Exception:
        return 0
    doomed = stale_discogs_tags(existing, protected)
    for name in doomed:
        try:
            metadata.delete(name)
        except Exception:
            pass
    if doomed:
        plugin_log.event(op, f'MusicBrainz-only: withdrew {len(doomed)} tag(s) '
                             'written by an earlier Discogs run')
        plugin_log.detail(op, f'  withdrew: {doomed}')
        log.debug('Discogs Enrichment: removed %d stale Discogs tag(s): %s',
                  len(doomed), ', '.join(doomed[:12]))
    return len(doomed)


def _clear_non_discogs_tags(metadata, fields: dict, op: str = 'clear') -> int:
    """Discogs-only mode: delete everything Discogs did not supply.

    What survives is only what Discogs could never supply and the file
    could not recover: the MusicBrainz identifiers (so Picard can re-find
    the release), AcoustID/MusicIP fingerprints and ReplayGain values (both
    computed by scanning the audio), the user's rating, and the encoder
    tags. Everything else — title, artist, album, date, genre and the rest
    — is removed and rewritten from Discogs.

    The user's protected-tags list is honoured too, and supports a trailing
    wildcard so a whole family can be spared with one entry
    (for example "replaygain_*").
    """
    protected = parse_protected_tags(settings['protected_tags'])
    try:
        existing = list(metadata.keys())
    except Exception:
        return 0
    doomed = tags_to_clear(existing, fields.keys(), protected)
    for name in doomed:
        try:
            metadata.delete(name)
        except Exception:
            pass
    if doomed:
        plugin_log.event(op, f'Discogs-only: cleared {len(doomed)} tag(s) Discogs '
                             f'did not supply')
        plugin_log.detail(op, f'  cleared: {doomed}')
    return len(doomed)


def _prefer_original_date(fields: dict) -> None:
    """Point the Date tag at the album's original release rather than the
    specific pressing that was matched, when the user has asked for that.

    `originalyear`/`originaldate` come from the Discogs MASTER (the work),
    while `date` comes from the individual release (the pressing). Without
    this, tagging a 2023 repress of a 2020 album writes 2023 into `date`.
    """
    if not settings['prefer_original_date']:
        return
    original = fields.get('originaldate') or fields.get('originalyear')
    if original:
        fields['date'] = original


def _guard_async(fn):
    """Wrap an async network callback so an exception cannot escape into
    Picard's Qt reply handler.

    The automatic album-load path previously had NO error handling on any
    of its callbacks, so any failure inside them propagated into Picard
    itself instead of being contained to this plugin.
    """
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.error('Discogs Enrichment: error in background lookup', exc_info=True)
    return _wrapped


def _album_is_alive(album) -> bool:
    """True if `album` is still registered with the tagger.

    A background Discogs lookup can outlive the album that triggered it —
    the user may have removed it, or Picard may have replaced it on a
    re-match. Touching a dead album's metadata or calling update() on it
    is a crash risk, so every deferred write checks this first.
    """
    try:
        return album.id in album.tagger.albums
    except Exception:
        return False


def _collect_track_fields(tracks, indices, pairs, out_by_pair: dict) -> None:
    """Work out each selected file's track tags, keyed by pair index.

    Track identity is per-file, unlike every other field the plugin writes,
    so it cannot live in the shared `fields` dict. It is kept SEPARATE from
    the disc-mapping tags, which are applied directly: the user picking
    "this album is that Discogs disc" is an explicit instruction about the
    disc number, but it says nothing about whether they want the Discogs
    track title in place of the MusicBrainz one. Track tags therefore go
    through the merge mode and the preview like every other field.
    """
    if not settings['track_tags']:
        return
    indices = [i for i in indices if 0 <= i < len(pairs)]
    if not indices:
        return
    aligned = align_tracks_to_files(tracks, [pairs[i][1] for i in indices])
    total = len([t for t in (tracks or []) if isinstance(t, dict)])
    for position, (pair_index, track) in enumerate(zip(indices, aligned), start=1):
        if not track:
            continue
        fields = extract_track_fields(track, position, total)
        merged = dict(out_by_pair.get(pair_index) or {})
        merged.update(fields)
        out_by_pair[pair_index] = merged


# Discogs fields already applied to an album, kept so a file that attaches to
# a track LATER can still be given them.
#
# The automatic lookup is asynchronous and Picard links files to tracks on its
# own schedule, so the two race. When the reply wins, the files are not linked
# yet and get nothing — observed directly in the plugin log, where repeated
# loads of one 17-track album alternated between 35 targets (album + 17 tracks
# + 17 files) and 18 (no files). With "Clear existing tags" on, a file that
# misses the handover also loses the tags a previous run had written, so the
# result was Discogs tags appearing to be deleted.
#
# Keyed by id() of the album, with the album held weakly: this must not keep
# a removed album alive, and must not grow without bound over a long session.
_APPLIED_BY_ALBUM: dict = {}
_APPLIED_LIMIT = 64


def _remember_applied(album, fields: dict, track_fields: dict, disc_count: int) -> None:
    if len(_APPLIED_BY_ALBUM) >= _APPLIED_LIMIT:
        _APPLIED_BY_ALBUM.clear()
    try:
        ref = weakref.ref(album)
    except TypeError:
        return
    _APPLIED_BY_ALBUM[id(album)] = (ref, dict(fields), dict(track_fields), disc_count)


def _forget_applied(album) -> None:
    _APPLIED_BY_ALBUM.pop(id(album), None)


def apply_to_late_file(api, track, file) -> None:
    """Give a newly attached file the Discogs data its album already has.

    Registered as a file-post-addition-to-track processor. Without it, a file
    that joins its track after the Discogs reply has landed keeps whatever
    MusicBrainz left and never sees the Discogs fields.
    """
    album = getattr(track, 'album', None)
    entry = _APPLIED_BY_ALBUM.get(id(album)) if album is not None else None
    if not entry:
        return
    ref, fields, track_fields, disc_count = entry
    if ref() is not album:
        # id() was reused by a different object after the original was freed.
        _APPLIED_BY_ALBUM.pop(id(album), None)
        return
    metadata = getattr(file, 'metadata', None)
    if metadata is None:
        return

    index = None
    try:
        index = list(album.tracks).index(track)
    except (ValueError, TypeError, AttributeError):
        pass
    per_track = (track_fields.get(index) or {}) if index is not None else {}

    names_before = snapshot_artist_names(metadata)
    _apply_discogs_fields(metadata, fields, disc_count)
    mode = settings['merge_mode'] or MODE_COMBINE
    for field, value in per_track.items():
        if field.startswith('~'):
            metadata[field] = value
            continue
        resolved = resolve_field_value(metadata.getall(field), value, mode, field=field)
        if resolved is not None:
            metadata[field] = resolved
    if mode == MODE_DISCOGS_ONLY:
        _clear_non_discogs_tags(metadata, dict.fromkeys(set(fields) | set(per_track)), 'late-file')
    _drop_stale_artist_ids(metadata, names_before, set(fields) | set(per_track), 'late-file')
    log.debug('Discogs Enrichment: applied cached Discogs data to a late-arriving file')


def _album_metadata_targets(album, album_metadata):
    """Every metadata object that has to receive the Discogs fields.

    Returns (metadata, track_key) pairs. `track_key` identifies which track
    the object belongs to, so per-track tags reach the right rows; it is
    None for the album's own metadata, which has no single track.

    Three kinds of object matter and they are NOT the same object:
      * the album metadata — what the album row shows;
      * each Track's metadata — what the right-hand pane shows;
      * each File's metadata — what File.save() actually writes to disk.
    Picard copies between them at load time and never again, so an update
    arriving later has to be written to all three or it reaches none of the
    files.
    """
    targets = [(album_metadata, None)]
    seen = {id(album_metadata)}

    for index, track in enumerate(getattr(album, 'tracks', None) or []):
        track_metadata = getattr(track, 'metadata', None)
        if track_metadata is not None and id(track_metadata) not in seen:
            targets.append((track_metadata, index))
            seen.add(id(track_metadata))
        # A track's linked files carry their own metadata, and that is the
        # copy that gets saved.
        for linked in getattr(track, 'files', None) or []:
            file_metadata = getattr(linked, 'metadata', None)
            if file_metadata is not None and id(file_metadata) not in seen:
                targets.append((file_metadata, index))
                seen.add(id(file_metadata))

    # Files attached to the album but not to any track (unmatched extras).
    try:
        for linked in album.iterfiles():
            file_metadata = getattr(linked, 'metadata', None)
            if file_metadata is not None and id(file_metadata) not in seen:
                targets.append((file_metadata, None))
                seen.add(id(file_metadata))
    except Exception:
        log.debug('Discogs Enrichment: could not enumerate album files', exc_info=True)

    return targets


def _album_track_fields(album, release_data) -> dict:
    """Per-track tags for the automatic path, keyed by track index.

    Without this, "Discogs only" would clear the title on every track and
    put nothing back, because the album-level field set has no track
    identity in it.
    """
    if not settings['track_tags']:
        return {}
    tracks = getattr(album, 'tracks', None) or []
    if not tracks:
        return {}
    discs = parse_discs(release_data)
    # The automatic path has no disc-mapping dialog, so the tracklist is
    # taken in release order; align_tracks_to_files then prefers each
    # track's existing number over raw ordering.
    audio = []
    for disc in discs:
        audio.extend(disc.get('tracks') or [])
    if not audio:
        return {}
    metadatas = [getattr(t, 'metadata', None) for t in tracks]
    aligned = align_tracks_to_files(audio, metadatas)
    out = {}
    for index, track in enumerate(aligned):
        if track:
            out[index] = extract_track_fields(track, index + 1, len(audio))
    return out


def _refresh_album_ui(album) -> None:
    """Make the panes redraw after a late metadata change.

    album.update() alone refreshes the rows; the metadata box caches the
    current selection and keeps showing stale values until told the
    selection is dirty.
    """
    try:
        album.update()
    except Exception:
        log.debug('Discogs Enrichment: album.update() failed', exc_info=True)
    try:
        window = getattr(get_api().tagger, 'window', None) if get_api() else None
        box = getattr(window, 'metadata_box', None)
        if box is not None:
            box.selection_dirty = True
            box.update()
    except Exception:
        log.debug('Discogs Enrichment: metadata box refresh failed', exc_info=True)


def _drop_stale_artist_ids(metadata, names_before: dict, supplied, op: str = 'ids') -> int:
    """Remove MusicBrainz artist IDs that no longer match the artist name.

    Discogs replaces `artist` / `albumartist` with its own credit string,
    for example "Steve Roach / Kevin Braheny / Michael Stearns". The
    MusicBrainz artist IDs written alongside the old credit would then
    describe different people than the new name, and a player that keys
    artists by MusicBrainz ID shows the wrong name on the wrong artist.
    They are removed whenever the name actually changed.
    """
    try:
        removed = drop_stale_artist_companions(metadata, names_before, supplied)
    except Exception:
        log.debug('Discogs Enrichment: could not drop stale artist IDs', exc_info=True)
        return 0
    if removed:
        plugin_log.event(op, f'artist name replaced by Discogs: removed {len(removed)} '
                             f'MusicBrainz artist tag(s) that no longer match')
        plugin_log.detail(op, f'  removed: {sorted(removed)}')
    return len(removed)


def _apply_discogs_fields(metadata, discogs_fields: dict, disc_count: int = 1) -> None:
    mode = settings['merge_mode'] or MODE_COMBINE
    if mode == MODE_MB_ONLY:
        # Nothing new is written, but tags a PREVIOUS run added are withdrawn
        # — otherwise switching to "MusicBrainz only" leaves the old Discogs
        # values on the file and appears to have done nothing.
        _clear_stale_discogs_tags(metadata)
        return
    boxset = is_probable_boxset(metadata.getall('album'), discogs_fields.get('album'), disc_count)
    for field, value in discogs_fields.items():
        if is_supplementary_field(field):
            metadata[field] = value
            continue
        if field == 'album':
            resolved = resolve_album_and_subtitle(
                metadata.getall('album'), value, metadata.getall('discsubtitle'), mode,
            )
            for target_field, target_value in resolved.items():
                metadata[target_field] = target_value
            continue
        existing = metadata.getall(field)
        effective_mode = MODE_FILL_GAPS if (boxset and field in PROTECTED_IN_BOXSET) else mode
        resolved = resolve_field_value(existing, value, effective_mode, field=field)
        if resolved is not None:
            metadata[field] = resolved


def process_album(api, album, metadata, release) -> None:
    """Registered as an album metadata processor in enable().

    Picard 3 injects the PluginApi as the first argument — see
    PluginApi.register_album_metadata_processor(), which binds it with
    functools.partial before handing the function to Picard. The api is
    already bound module-wide by enable(), so this parameter is accepted
    and ignored rather than threaded through the call tree.
    """
    # Automatic lookup is governed by the tag source setting, not a second
    # switch: if the user asked for Discogs data, fetching it when an album
    # loads is exactly what they asked for. "MusicBrainz only" is the way
    # to turn Discogs off entirely.
    _configure_log()
    _log_settings_snapshot()
    op = plugin_log.new_operation('album')
    plugin_log.event(op, f'MusicBrainz matched: {metadata.get("albumartist") or metadata.get("artist")} '
                         f'\u2014 {metadata.get("album")} '
                         f'[date={metadata.get("date")} barcode={metadata.get("barcode")} '
                         f'catno={metadata.get("catalognumber")} country={metadata.get("releasecountry")}]')

    if (settings['merge_mode'] or MODE_COMBINE) == MODE_MB_ONLY:
        plugin_log.decision(op, 'tag source is MusicBrainz only \u2014 no Discogs lookup')
        return

    token = settings['token']
    if not token:
        plugin_log.decision(op, 'no Discogs token configured \u2014 nothing can be fetched')
        log.debug('Discogs Enrichment: no token configured, skipping album %r', album)
        return

    client = DiscogsClient(album.tagger.webservice, token)
    client.op = op

    # MusicBrainz often records the exact Discogs release this release
    # corresponds to, as a url-rel. Using it means the pressing Picard
    # matched and the pressing Discogs data comes from are the SAME
    # object — catalogue number, country, pressing credits and date all
    # line up. Searching by artist/title instead returns whichever
    # pressing Discogs ranks first, which for a heavily-repressed album
    # is essentially arbitrary.
    linked_id = discogs_release_id_from_mb(release)
    if linked_id:
        plugin_log.decision(op, f'MusicBrainz links this release to Discogs '
                                f'release {linked_id} \u2014 using it directly, no search')
        log.info(
            'Discogs Enrichment: MusicBrainz links this release to Discogs '
            'release %s — using it directly instead of searching', linked_id,
        )

    barcode = metadata.get('barcode') or None
    catalog_number = metadata.get('catalognumber') or None
    artist = metadata.get('albumartist') or metadata.get('artist') or None
    album_title = metadata.get('album') or None

    if not linked_id and not any([barcode, (catalog_number and artist), (artist and album_title)]):
        log.debug('Discogs Enrichment: not enough metadata to search Discogs for album %r', album)
        return

    def _on_release_detail(release_data):
        """Shared by both routes: the MusicBrainz-linked release fetch and
        the fallback search. Applies the release to the album."""
        if not release_data:
            log.warning('Discogs Enrichment: release detail fetch returned nothing')
            return
        fields = _shape_fields(extract_discogs_fields(release_data))
        if not settings['import_notes']:
            fields.pop('discogs_notes', None)

        def _finish():
            if not _album_is_alive(album):
                log.debug('Discogs Enrichment: album %r went away before the '
                          'Discogs result arrived, discarding it', album)
                return
            disc_count = release_disc_count(release_data)
            before = {k: list(metadata.getall(k)) for k in fields}

            # Writing to the album metadata alone is not enough. Picard builds
            # every track by COPYING the album metadata (Album._load_tracks),
            # and it does that during _finalize_loading — which has already
            # happened by the time this asynchronous Discogs reply arrives.
            # Fields written here would land in an object nothing reads again,
            # which is exactly why automatic lookups produced no discogs_*
            # tags while the manual search did.
            targets = _album_metadata_targets(album, metadata)
            track_fields = _album_track_fields(album, release_data)
            discogs_only = (settings['merge_mode'] or MODE_COMBINE) == MODE_DISCOGS_ONLY

            for target, track_key in targets:
                names_before = snapshot_artist_names(target)
                _apply_discogs_fields(target, fields, disc_count)
                per_track = track_fields.get(track_key) or {}
                for field, value in per_track.items():
                    if field.startswith('~'):
                        target[field] = value
                        continue
                    resolved = resolve_field_value(
                        target.getall(field), value,
                        settings['merge_mode'] or MODE_COMBINE, field=field)
                    if resolved is not None:
                        target[field] = resolved
                if discogs_only:
                    # The supplied set must include the track tags, or the
                    # clear would delete the very title just written.
                    supplied = set(fields) | set(per_track)
                    _clear_non_discogs_tags(target, dict.fromkeys(supplied), op)
                _drop_stale_artist_ids(target, names_before, set(fields) | set(per_track), op)

            changed = {k: (before.get(k), list(metadata.getall(k)))
                       for k in fields if list(metadata.getall(k)) != before.get(k)}
            plugin_log.event(op, f'applied {len(changed)} of {len(fields)} field(s) '
                                 f'to {len(targets)} target(s)')
            for name, (was, now) in sorted(changed.items()):
                plugin_log.detail(op, f'  {name}: {was} -> {now}')
            unchanged = [k for k in fields if k not in changed]
            if unchanged:
                plugin_log.detail(op, f'  unchanged ({len(unchanged)}): {sorted(unchanged)[:20]}')
            # Files that attach to their track after this point are handled
            # by apply_to_late_file(), which needs the same field set.
            _remember_applied(album, fields, track_fields, disc_count)
            _refresh_album_ui(album)
            log.info('Discogs Enrichment: applied fields %r to album %r (%d target(s))',
                     list(fields.keys()), album, len(targets))

        master_id = release_data.get('master_id')
        if not master_id:
            _finish()
            return

        def _on_master(master_data):
            fields.update(extract_original_year(master_data))
            _prefer_original_date(fields)
            _finish()

        client.get_master(master_id, _guard_async(_on_master))

    def _on_search_results(results):
        """No MusicBrainz->Discogs link, so identify the pressing ourselves.

        Discogs' own relevance ranking is not pressing-aware — for a
        heavily-repressed album its top hit is essentially arbitrary. Score
        every candidate against the MusicBrainz release's barcode,
        catalogue number, year, country and format instead, and only tag
        automatically when one is a confident match. Guessing silently is
        worse than doing nothing here, because the automatic path has no
        user to confirm with.
        """
        if not results:
            log.debug('Discogs Enrichment: no Discogs results for album %r', album)
            return
        ranked = rank_release_candidates(results, _mb_match_facts(metadata))
        best_score, best = ranked[0]
        plugin_log.detail(op, 'candidate scores: ' + '; '.join(
            f"{c.get('id')}={s} [{c.get('year')} {c.get('country')} "
            f"{c.get('catno')} {','.join(c.get('format') or [])}]"
            for s, c in ranked[:8]))
        threshold = threshold_for_strictness(settings['strictness'])
        if best_score < threshold:
            plugin_log.decision(op, f'best candidate {best.get("id")} scored {best_score}, '
                                    f'below the {threshold} threshold \u2014 not tagging '
                                    f'automatically (use Search Discogs\u2026 to choose)')
            log.info(
                'Discogs Enrichment: best Discogs candidate for %r scored only %d '
                '(needs %d) against the MusicBrainz release, so it is probably a '
                'different pressing — skipping. Use right-click > Plugins > '
                '"Search Discogs..." to choose one yourself.',
                album, best_score, threshold,
            )
            return
        resource_url = best.get('resource_url') or best.get('id')
        plugin_log.decision(op, f'chose Discogs release {best.get("id")} '
                                f'(score {best_score} \u2265 {threshold}) from '
                                f'{len(results)} candidate(s)')
        log.info('Discogs Enrichment: matched Discogs release %r (score %d)',
                 resource_url, best_score)
        client.get_release(resource_url, _guard_async(_on_release_detail))

    if linked_id:
        client.get_release(linked_id, _guard_async(_on_release_detail))
        return

    log.debug(
        'Discogs Enrichment: no MusicBrainz->Discogs link, searching for album %r '
        '(barcode=%r, catno=%r, artist=%r, title=%r)',
        album, barcode, catalog_number, artist, album_title,
    )
    client.search_releases_cascading(
        barcode=barcode,
        catalog_number=catalog_number,
        artist=artist,
        release_title=album_title,
        handler=_guard_async(_on_search_results),
    )




def _section_label(parent, text):
    """A bold heading used to break the options page into sections."""
    label = QtWidgets.QLabel(f'<b>{text}</b>', parent)
    label.setTextFormat(QtCore.Qt.TextFormat.RichText)
    return label


def _select_by_data(combo, value):
    """Select the combo entry whose userData equals `value`, if present."""
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


class DiscogsOptionsPage(OptionsPage):
    NAME = 'discogs_enrichment'
    TITLE = 'Discogs Enrichment'
    PARENT = 'plugins'

    def __init__(self, parent=None):
        super().__init__(parent=parent)

        # This page is long. Without a scroll area the content is clipped by
        # the options dialog and the "Make It So" button becomes unreachable,
        # so the page itself scrolls and the dialog keeps its own chrome.
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QtWidgets.QWidget(scroll)
        scroll.setWidget(container)
        outer.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(container)

        # --- account ----------------------------------------------------
        layout.addWidget(_section_label(self, 'Discogs account'))
        token_row = QtWidgets.QHBoxLayout()
        token_row.addWidget(QtWidgets.QLabel('Personal access token:'))
        self.token_field = QtWidgets.QLineEdit(self)
        self.token_field.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.token_field.setToolTip(
            'Required. Generate one at discogs.com \u2192 Settings \u2192 Developers.')
        token_row.addWidget(self.token_field, 1)
        layout.addLayout(token_row)

        # --- tag source -------------------------------------------------
        layout.addWidget(_section_label(self, 'Tag source'))
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mb_only_radio = QtWidgets.QRadioButton('MusicBrainz only', self)
        self.mb_only_radio.setToolTip('Write no Discogs data at all.')
        self.fill_gaps_radio = QtWidgets.QRadioButton('MusicBrainz preferred', self)
        self.fill_gaps_radio.setToolTip(
            'Discogs fills only the fields MusicBrainz left empty. One value per field.')
        self.override_radio = QtWidgets.QRadioButton('Discogs preferred (recommended)', self)
        self.override_radio.setToolTip(
            'Discogs wins where it has a value; MusicBrainz is kept where Discogs has '
            'none. One value per field \u2014 never concatenated.')
        self.discogs_only_radio = QtWidgets.QRadioButton(
            'Discogs only \u2014 remove tags Discogs does not supply', self)
        self.discogs_only_radio.setToolTip(
            'Deletes every tag Discogs did not provide. Track title, track and disc '
            'numbers, length, and the MusicBrainz / AcoustID identifiers are always '
            'kept, since without them a file has no title and can never be '
            're-matched.\n\n'
            'Everything else goes, including ReplayGain, comments, lyrics, ratings '
            'and custom tags. Use the "Never change these tags" box to spare any of '
            'them \u2014 a trailing * works, e.g. replaygain_*')
        self.combine_radio = QtWidgets.QRadioButton('Keep both values (multi-value tags)', self)
        self.combine_radio.setToolTip(
            'The only option that concatenates, e.g. "Jazz; Rock". Single-value fields '
            'such as date, artist and barcode still take the Discogs value alone.')
        for radio in (self.mb_only_radio, self.fill_gaps_radio,
                      self.override_radio, self.combine_radio,
                      self.discogs_only_radio):
            self.mode_group.addButton(radio)
            layout.addWidget(radio)
        self.discogs_only_warning = QtWidgets.QLabel('')
        self.discogs_only_warning.setWordWrap(True)
        self.discogs_only_warning.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(self.discogs_only_warning)
        self.discogs_only_radio.toggled.connect(self._update_discogs_only_warning)

        hint = QtWidgets.QLabel(
            '<i>Anything but "MusicBrainz only" also fetches Discogs automatically '
            'on album load.</i>')
        hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(hint)

        # --- what to write ----------------------------------------------
        layout.addWidget(_section_label(self, 'What to write'))
        style_row = QtWidgets.QHBoxLayout()
        style_row.addWidget(QtWidgets.QLabel('Discogs styles:'))
        self.style_combo = QtWidgets.QComboBox(self)
        self.style_combo.addItem('Keep in discogs_style only', STYLE_SEPARATE)
        self.style_combo.addItem('Use as Genre', STYLE_GENRE)
        self.style_combo.addItem('Both', STYLE_BOTH)
        self.style_combo.setToolTip(
            'Discogs genres are broad ("Rock"); styles carry the detail '
            '("Prog Rock"). By default styles go only to discogs_style, which most '
            'players ignore.')
        style_row.addWidget(self.style_combo, 1)
        layout.addLayout(style_row)

        self.performers_checkbox = QtWidgets.QCheckBox('Performer credits', self)
        self.performers_checkbox.setToolTip('performer:bass, performer:drums, \u2026')
        self.companies_checkbox = QtWidgets.QCheckBox('Company credits', self)
        self.companies_checkbox.setToolTip('Pressed by, mastered at, distributed by, \u2026')
        self.identifiers_checkbox = QtWidgets.QCheckBox('Other identifiers', self)
        self.identifiers_checkbox.setToolTip('Matrix / runout, label code, rights society, \u2026')
        self.marketplace_checkbox = QtWidgets.QCheckBox('Marketplace / community stats', self)
        self.marketplace_checkbox.setToolTip(
            'Have, want, rating, price. A snapshot at tagging time that goes stale '
            'immediately, so off by default.')
        self.import_notes_checkbox = QtWidgets.QCheckBox('Release notes', self)
        self.import_notes_checkbox.setToolTip('Free text; sometimes several paragraphs.')
        for box in (self.performers_checkbox, self.companies_checkbox,
                    self.identifiers_checkbox, self.marketplace_checkbox,
                    self.import_notes_checkbox):
            layout.addWidget(box)

        protected_row = QtWidgets.QHBoxLayout()
        protected_row.addWidget(QtWidgets.QLabel('Never change these tags:'))
        self.protected_field = QtWidgets.QLineEdit(self)
        self.protected_field.setPlaceholderText('date, genre, albumartist')
        self.protected_field.setToolTip(
            'Discogs will never touch these, whatever the tag source is set to. '
            'Commas or spaces; case-insensitive.')
        protected_row.addWidget(self.protected_field, 1)
        layout.addLayout(protected_row)

        # --- track tags -------------------------------------------------
        layout.addWidget(_section_label(self, 'Track tags'))
        self.track_tags_checkbox = QtWidgets.QCheckBox(
            'Write track title, number and artist from the Discogs tracklist', self)
        self.track_tags_checkbox.setToolTip(
            'On (recommended): track identity comes from Discogs. Required for '
            '"Discogs only" to produce usable files, since that mode clears the '
            'MusicBrainz title along with everything else.\n'
            'Off: track titles and numbers are left exactly as they are.')
        layout.addWidget(self.track_tags_checkbox)

        # --- dates ------------------------------------------------------
        layout.addWidget(_section_label(self, 'Dates'))
        self.prefer_original_date_checkbox = QtWidgets.QCheckBox(
            'Use the original release date as the Date tag', self)
        self.prefer_original_date_checkbox.setToolTip(
            'Off (recommended): Date is the release you own, so a 2017 box is dated '
            '2017. The original is still written to Original Year.\n'
            'On: Date becomes the original date instead.')
        layout.addWidget(self.prefer_original_date_checkbox)

        # --- cover art --------------------------------------------------
        layout.addWidget(_section_label(self, 'Cover art'))
        self.cover_art_checkbox = QtWidgets.QCheckBox('Provide Discogs cover art', self)
        self.cover_art_checkbox.setToolTip(
            'Adds Discogs as a cover art source. You must also enable it in '
            'Options \u2192 Cover Art, where new providers start switched off.')
        layout.addWidget(self.cover_art_checkbox)
        art_row = QtWidgets.QHBoxLayout()
        art_row.addWidget(QtWidgets.QLabel('Download:'))
        self.cover_art_scope_combo = QtWidgets.QComboBox(self)
        self.cover_art_scope_combo.addItem('Front cover only', ART_FRONT)
        self.cover_art_scope_combo.addItem('Front and back', ART_FRONT_BACK)
        self.cover_art_scope_combo.addItem('All artwork', ART_ALL)
        self.cover_art_scope_combo.setToolTip(
            'Discogs labels images only primary or secondary, so the first '
            'secondary is taken as the back cover and the rest as "other".')
        art_row.addWidget(self.cover_art_scope_combo, 1)
        layout.addLayout(art_row)
        self.art_warning = QtWidgets.QLabel('')
        self.art_warning.setWordWrap(True)
        self.art_warning.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(self.art_warning)
        self.cover_art_scope_combo.currentIndexChanged.connect(self._update_art_warning)
        cover_hint = QtWidgets.QLabel(
            '<i>Also tick <b>Discogs</b> in Options \u2192 Cover Art.</i>')
        cover_hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
        cover_hint.setWordWrap(True)
        cover_hint.setToolTip(
            'Picard adds newly registered cover art providers at the bottom of '
            'that list, switched off. Drag Discogs above Cover Art Archive if you '
            'want it to take precedence rather than only fill gaps.')
        layout.addWidget(cover_hint)

        # --- matching & workflow ----------------------------------------
        layout.addWidget(_section_label(self, 'Matching and workflow'))
        strict_row = QtWidgets.QHBoxLayout()
        strict_row.addWidget(QtWidgets.QLabel('Auto-match strictness:'))
        self.strictness_combo = QtWidgets.QComboBox(self)
        self.strictness_combo.addItem('Strict', STRICTNESS_STRICT)
        self.strictness_combo.addItem('Normal', STRICTNESS_NORMAL)
        self.strictness_combo.addItem('Lenient', STRICTNESS_LENIENT)
        self.strictness_combo.setToolTip(
            'Used only when MusicBrainz has no Discogs link and a pressing must be '
            'identified by scoring candidates. Strict demands more than a barcode '
            'match. Below the threshold nothing is tagged automatically.')
        strict_row.addWidget(self.strictness_combo, 1)
        layout.addLayout(strict_row)

        self.reverse_match_checkbox = QtWidgets.QCheckBox(
            'Offer the matching MusicBrainz release after picking a Discogs one', self)
        self.reverse_match_checkbox.setToolTip(
            'For unmatched files only. Accepting matches them to that MusicBrainz '
            'release, so you get both sources.')
        self.skip_preview_checkbox = QtWidgets.QCheckBox('Skip the change preview', self)
        self.skip_preview_checkbox.setToolTip('Applies changes without asking.')
        self.auto_disc_map_checkbox = QtWidgets.QCheckBox(
            'Skip disc matching when unambiguous', self)
        self.auto_disc_map_checkbox.setToolTip(
            'Only when there is one group per disc, every disc matched, and no group '
            'claimed twice.')
        self.use_cache_checkbox = QtWidgets.QCheckBox('Reuse Discogs responses', self)
        self.use_cache_checkbox.setToolTip(
            'Avoids refetching the same release. Discogs allows 60 requests a minute.')
        for box in (self.reverse_match_checkbox, self.skip_preview_checkbox,
                    self.auto_disc_map_checkbox, self.use_cache_checkbox):
            layout.addWidget(box)

        # --- troubleshooting log -----------------------------------------
        layout.addWidget(_section_label(self, 'Troubleshooting log'))
        log_row = QtWidgets.QHBoxLayout()
        log_row.addWidget(QtWidgets.QLabel('Detail:'))
        self.log_level_combo = QtWidgets.QComboBox(self)
        self.log_level_combo.addItem('Off', LEVEL_OFF)
        self.log_level_combo.addItem('Basic \u2014 what it decided and why', LEVEL_BASIC)
        self.log_level_combo.addItem('Detailed \u2014 plus scores and field changes', LEVEL_DETAILED)
        self.log_level_combo.setToolTip(
            'Records Discogs and MusicBrainz requests, timings, which release was '
            'chosen and why, and which tags changed. Your token is never written.')
        log_row.addWidget(self.log_level_combo, 1)
        log_row.addWidget(QtWidgets.QLabel('Max size:'))
        self.log_size_combo = QtWidgets.QComboBox(self)
        for kb, label in ((512, '512 KB'), (2048, '2 MB'), (8192, '8 MB')):
            self.log_size_combo.addItem(label, str(kb))
        self.log_size_combo.setToolTip(
            'Total size on disk. Oldest entries are overwritten once the limit is '
            'reached, so the log never grows without bound.')
        log_row.addWidget(self.log_size_combo)
        layout.addLayout(log_row)

        self.log_status = QtWidgets.QLabel('')
        self.log_status.setWordWrap(True)
        layout.addWidget(self.log_status)

        log_buttons = QtWidgets.QHBoxLayout()
        self.save_log_button = QtWidgets.QPushButton('Save log as\u2026', self)
        self.save_log_button.clicked.connect(self._save_log)
        self.clear_log_button = QtWidgets.QPushButton('Clear log', self)
        self.clear_log_button.clicked.connect(self._clear_log)
        log_buttons.addWidget(self.save_log_button)
        log_buttons.addWidget(self.clear_log_button)
        log_buttons.addStretch(1)
        layout.addLayout(log_buttons)

        button_row = QtWidgets.QHBoxLayout()
        self.clear_cache_button = QtWidgets.QPushButton('Clear cache', self)
        self.clear_cache_button.clicked.connect(self._clear_cache)
        self.reset_button = QtWidgets.QPushButton('Reset to recommended', self)
        self.reset_button.clicked.connect(self._reset_defaults)
        button_row.addWidget(self.clear_cache_button)
        button_row.addWidget(self.reset_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        layout.addStretch(1)

    def _refresh_log_status(self):
        size = plugin_log.size_bytes()
        if size:
            self.log_status.setText(
                f'<i>{log_path()} \u2014 {size / 1024:.0f} KB</i>')
        else:
            self.log_status.setText(f'<i>{log_path()} \u2014 empty</i>')
        self.log_status.setTextFormat(QtCore.Qt.TextFormat.RichText)

    def _save_log(self):
        """Copy the log somewhere the user chooses, so it can be attached to
        a bug report without hunting for Picard's config directory."""
        import shutil
        source = log_path()
        if not os.path.exists(source):
            QtWidgets.QMessageBox.information(
                self, 'Discogs Enrichment',
                'There is no log yet. Set Detail to Basic or Detailed, reproduce '
                'the problem, then save the log.')
            return
        target, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, 'Save Discogs Enrichment log',
            os.path.join(os.path.expanduser('~'), 'discogs_enrichment.log'),
            'Log files (*.log);;All files (*)')
        if not target:
            return
        try:
            # Include the rolled-over half, oldest first, so the saved file
            # reads chronologically rather than starting mid-session.
            with open(target, 'w', encoding='utf-8') as out:
                for part in (source + '.1', source):
                    if os.path.exists(part):
                        with open(part, encoding='utf-8', errors='replace') as handle:
                            shutil.copyfileobj(handle, out)
            QtWidgets.QMessageBox.information(
                self, 'Discogs Enrichment', f'Log saved to:\n{target}')
        except OSError as error:
            QtWidgets.QMessageBox.warning(
                self, 'Discogs Enrichment', f'Could not save the log:\n{error}')

    def _clear_log(self):
        if plugin_log.clear():
            self._refresh_log_status()
            QtWidgets.QMessageBox.information(
                self, 'Discogs Enrichment', 'Log cleared.')
        else:
            QtWidgets.QMessageBox.warning(
                self, 'Discogs Enrichment', 'Could not clear the log.')

    def _update_discogs_only_warning(self):
        if self.discogs_only_radio.isChecked():
            self.discogs_only_warning.setText(
                '<b>Destructive.</b> ReplayGain, comments, lyrics, ratings and custom '
                'tags will be deleted on save. ReplayGain is calculated from the audio '
                'and can only be restored by re-analysing your files. Add '
                '<tt>replaygain_*</tt> to "Never change these tags" below to keep it.')
        else:
            self.discogs_only_warning.setText('')

    def _update_art_warning(self):
        """Warn when Picard's own cover art options would discard the extra
        images this plugin is about to fetch.

        Asking for all artwork is pointless if "embed only one front image"
        or "save only one front image" is still on, and those live on a
        different options page, so the connection is easy to miss.
        """
        if self.cover_art_scope_combo.currentData() == ART_FRONT:
            self.art_warning.setText('')
            return
        s = settings
        # Picard abandons the WHOLE cover art pipeline as soon as a front
        # image is found, when embedding one front image is all that is
        # asked of it (picard/coverart/__init__.py). Under Picard's stock
        # settings that happens before this plugin is ever consulted, so no
        # amount of configuring here produces extra artwork.
        hard_stop = (s['save_images_to_tags']
                     and not s['save_images_to_files']
                     and s['embed_only_one_front_image'])
        if hard_stop:
            self.art_warning.setText(
                '<b>This will not work yet.</b> Picard stops fetching artwork the '
                'moment any provider supplies a front cover. Under Options \u2192 '
                'Cover Art, either tick <b>Save cover images as separate files</b> '
                'or untick <b>Embed only one front image</b>.')
            return

        blockers = []
        if s['save_images_to_tags'] and s['embed_only_one_front_image']:
            blockers.append('only one image will be embedded '
                            '("Embed only one front image")')
        if s['save_images_to_files'] and s['save_only_one_front_image']:
            blockers.append('only one image will be saved to disk '
                            '("Save only one front image")')
        if not s['save_images_to_tags'] and not s['save_images_to_files']:
            blockers.append('nothing is being written at all \u2014 enable embedding '
                            'or saving to files')
        if blockers:
            self.art_warning.setText(
                '<i>Note: ' + '; '.join(blockers)
                + '. Change under Options \u2192 Cover Art.</i>')
        else:
            self.art_warning.setText('')

    def _clear_cache(self):
        clear_cache()
        QtWidgets.QMessageBox.information(
            self, 'Discogs Enrichment', 'Cached Discogs responses cleared.')

    def _reset_defaults(self):
        """Restore recommended settings, leaving the token alone."""
        self.override_radio.setChecked(True)
        self.import_notes_checkbox.setChecked(False)
        self.reverse_match_checkbox.setChecked(True)
        self.track_tags_checkbox.setChecked(True)
        self.prefer_original_date_checkbox.setChecked(False)
        self.performers_checkbox.setChecked(True)
        self.companies_checkbox.setChecked(True)
        self.identifiers_checkbox.setChecked(True)
        self.marketplace_checkbox.setChecked(False)
        self.protected_field.setText('')
        self.skip_preview_checkbox.setChecked(False)
        self.auto_disc_map_checkbox.setChecked(False)
        self.cover_art_checkbox.setChecked(True)
        _select_by_data(self.cover_art_scope_combo, ART_FRONT)
        self.use_cache_checkbox.setChecked(True)
        _select_by_data(self.style_combo, STYLE_SEPARATE)
        _select_by_data(self.strictness_combo, STRICTNESS_NORMAL)
        _select_by_data(self.log_level_combo, LEVEL_BASIC)
        _select_by_data(self.log_size_combo, '2048')

    def load(self):
        s = settings
        self.token_field.setText(s['token'])
        mode = s['merge_mode'] or MODE_COMBINE
        if mode == MODE_DISCOGS_ONLY:
            self.discogs_only_radio.setChecked(True)
        elif mode == MODE_MB_ONLY:
            self.mb_only_radio.setChecked(True)
        elif mode == MODE_FILL_GAPS:
            self.fill_gaps_radio.setChecked(True)
        elif mode == MODE_OVERRIDE:
            self.override_radio.setChecked(True)
        else:
            self.combine_radio.setChecked(True)
        self.import_notes_checkbox.setChecked(bool(s['import_notes']))
        self.reverse_match_checkbox.setChecked(bool(s['reverse_match']))
        self.track_tags_checkbox.setChecked(bool(s['track_tags']))
        self.prefer_original_date_checkbox.setChecked(bool(s['prefer_original_date']))
        self.performers_checkbox.setChecked(bool(s['performers']))
        self.companies_checkbox.setChecked(bool(s['companies']))
        self.identifiers_checkbox.setChecked(bool(s['identifiers']))
        self.marketplace_checkbox.setChecked(bool(s['marketplace']))
        self.protected_field.setText(s['protected_tags'] or '')
        self.skip_preview_checkbox.setChecked(bool(s['skip_preview']))
        self.auto_disc_map_checkbox.setChecked(bool(s['auto_disc_map']))
        self.cover_art_checkbox.setChecked(bool(s['cover_art']))
        _select_by_data(self.cover_art_scope_combo, s['cover_art_scope'] or ART_FRONT)
        self._update_art_warning()
        self.use_cache_checkbox.setChecked(bool(s['use_cache']))
        _select_by_data(self.style_combo, s['style_mode'] or STYLE_SEPARATE)
        _select_by_data(self.strictness_combo, s['strictness'] or STRICTNESS_NORMAL)
        _select_by_data(self.log_level_combo, s['log_level'] or LEVEL_BASIC)
        _select_by_data(self.log_size_combo, s['log_max_kb'] or '2048')
        self._refresh_log_status()
        self._update_discogs_only_warning()

    def save(self):
        s = settings
        s['token'] = self.token_field.text().strip()
        if self.discogs_only_radio.isChecked():
            s['merge_mode'] = MODE_DISCOGS_ONLY
        elif self.mb_only_radio.isChecked():
            s['merge_mode'] = MODE_MB_ONLY
        elif self.fill_gaps_radio.isChecked():
            s['merge_mode'] = MODE_FILL_GAPS
        elif self.override_radio.isChecked():
            s['merge_mode'] = MODE_OVERRIDE
        else:
            s['merge_mode'] = MODE_COMBINE
        s['import_notes'] = self.import_notes_checkbox.isChecked()
        s['reverse_match'] = self.reverse_match_checkbox.isChecked()
        s['track_tags'] = self.track_tags_checkbox.isChecked()
        s['prefer_original_date'] = self.prefer_original_date_checkbox.isChecked()
        s['performers'] = self.performers_checkbox.isChecked()
        s['companies'] = self.companies_checkbox.isChecked()
        s['identifiers'] = self.identifiers_checkbox.isChecked()
        s['marketplace'] = self.marketplace_checkbox.isChecked()
        s['protected_tags'] = self.protected_field.text().strip()
        s['skip_preview'] = self.skip_preview_checkbox.isChecked()
        s['auto_disc_map'] = self.auto_disc_map_checkbox.isChecked()
        s['cover_art'] = self.cover_art_checkbox.isChecked()
        s['cover_art_scope'] = self.cover_art_scope_combo.currentData()
        s['use_cache'] = self.use_cache_checkbox.isChecked()
        s['style_mode'] = self.style_combo.currentData()
        s['strictness'] = self.strictness_combo.currentData()
        s['log_level'] = self.log_level_combo.currentData()
        s['log_max_kb'] = self.log_size_combo.currentData()
        _configure_log()




class _DiscogsSearchBase(BaseAction):
    """Shared logic for searching Discogs and applying the result, used by
    both the album-level action (matched albums, right pane) and the
    cluster-level action (unmatched files still sitting on the left pane
    because no MusicBrainz release was found). Subclasses only need to
    implement _build_pairs() to say which (owner, metadata) pairs the
    current selection maps to \u2014 an Album/File already has .metadata
    directly, while a Cluster needs its contained files enumerated.
    """

    def _build_pairs(self, objs):
        raise NotImplementedError

    def _build_groups(self, objs, pairs):
        """Group the flat `pairs` list into units that map 1:1 onto Discogs
        discs. Default: one group per pair (correct for albums/files, where
        each selected item is its own disc). Clusters override this, since
        one cluster can contain several discs' worth of files.

        Returns a list of (label, [indices into pairs]).
        """
        return [(_pair_label(owner, metadata), [i])
                for i, (owner, metadata) in enumerate(pairs)]

    def _guard(self, fn):
        """Wrap an async network callback so an exception inside it is
        reported instead of vanishing.

        This matters because the search/fetch chain is asynchronous: the
        synchronous entry point returns as soon as the request is queued,
        so a plain try/except around it would guard almost nothing. Every
        handler that Picard's WebService calls back into is wrapped with
        this instead.
        """
        def _wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                log.error('Discogs Enrichment: unhandled error', exc_info=True)
                QtWidgets.QMessageBox.critical(
                    self.tagger.window, 'Discogs Enrichment',
                    'Something went wrong while applying Discogs data. '
                    'See Help \u2192 View Error/Debug Log for the traceback.',
                )
        return _wrapped

    def callback(self, objs):
        try:
            self._callback(objs)
        except Exception:
            log.error('Discogs Enrichment: unhandled error', exc_info=True)
            QtWidgets.QMessageBox.critical(
                self.tagger.window, 'Discogs Enrichment',
                'Something went wrong. See Help \u2192 View Error/Debug Log '
                'for the full traceback.',
            )

    def _callback(self, objs):
        _configure_log()
        _log_settings_snapshot()
        self.op = plugin_log.new_operation('search')
        token = settings['token']
        if not token:
            QtWidgets.QMessageBox.warning(
                self.tagger.window, 'Discogs Enrichment',
                'No Discogs API token is configured.\n\n'
                'Set one under Options \u2192 Plugins \u2192 Discogs Enrichment.',
            )
            return

        pairs = self._build_pairs(objs)
        if not pairs:
            QtWidgets.QMessageBox.information(
                self.tagger.window, 'Discogs Enrichment',
                'Nothing taggable was found in the current selection.',
            )
            return
        groups = self._build_groups(objs, pairs)

        client = DiscogsClient(self.tagger.webservice, token)
        client.op = self.op
        parent = self.tagger.window
        metadata = pairs[0][1]
        plugin_log.event(self.op, f'manual search on {len(pairs)} item(s): '
                                  f'{metadata.get("albumartist") or metadata.get("artist")} '
                                  f'\u2014 {metadata.get("album")}')

        # If MusicBrainz linked this release to a specific Discogs release,
        # offer that as the pre-filled direct lookup so the manual path
        # lands on the SAME pressing Picard matched, not an arbitrary one.
        linked_id = ''
        for owner, _md in pairs:
            node = getattr(owner, '_release_node', None) or getattr(
                getattr(owner, 'album', None), '_release_node', None)
            found = discogs_release_id_from_mb(node)
            if found:
                linked_id = found
                break

        date = metadata.get('date') or ''
        defaults = {
            'artist': metadata.get('albumartist') or metadata.get('artist') or '',
            'release_title': metadata.get('album') or '',
            'year': date[:4] if len(date) >= 4 and date[:4].isdigit() else '',
            'catalog_number': metadata.get('catalognumber') or '',
            'barcode': metadata.get('barcode') or '',
            # Label and country are deliberately NOT pre-filled. Discogs
            # treats every field as a requirement, and its label naming
            # rarely matches MusicBrainz's ("EMI" vs "EMI Records"), so
            # pre-filling them turns a good search into zero results.
            'direct': linked_id,
        }

        dialog = DiscogsSearchDialog(
            defaults,
            parent=parent,
            context_label=(
                f'Searching for {len(pairs)} selected items — the same result '
                'will be applied to all of them.'
            ) if len(pairs) > 1 else None,
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        values = dialog.values

        release_id = _extract_release_id(values['direct'])
        if release_id:
            self._fetch_and_apply(client, pairs, groups, release_id, parent)
            return

        master_id = _extract_master_id(values['direct'])
        if master_id:
            QtWidgets.QMessageBox.information(
                parent,
                'Discogs Master Release',
                'That looks like a Discogs MASTER release code, which groups '
                'every pressing of a work and has no per-pressing data '
                '(no barcode, catalogue number, country, or credits).\n\n'
                'Open the master page on Discogs, pick the specific pressing '
                'you have, and paste that release code or URL instead.',
            )
            return

        # Everything the dialog collected except the direct-ID box and the
        # result-scope radio, which are not search terms.
        search_terms = {name: value for name, value in values.items()
                        if name not in ('direct', 'release_type')}
        if not any(search_terms.values()):
            log.debug('Discogs Enrichment: search dialog closed with nothing to search on')
            return

        relaxed_terms: list[str] = []

        def _note_relaxed(dropped):
            relaxed_terms.extend(dropped)

        def _on_results(results):
            # Rank against the MusicBrainz release facts so the pressing
            # Picard actually matched floats to the top, rather than
            # whichever one Discogs happens to rank first. The scores go to
            # the picker as well, so the user can see WHY a row ranked where
            # it did instead of trusting an invisible ordering.
            match_details: list[dict] = []
            if results:
                ranked = rank_release_candidates_detailed(
                    results, _mb_match_facts(metadata))
                match_details = [detail for detail, _candidate in ranked]
                results = [candidate for _detail, candidate in ranked]
            if not results:
                QtWidgets.QMessageBox.information(
                    parent, 'Discogs Enrichment',
                    'Discogs has nothing matching those terms, even after '
                    'widening the search.\n\n'
                    'Discogs requires every field to match, so one term it '
                    'spells differently is enough to rule out every release. '
                    'Try the barcode or catalogue number on its own, or open '
                    'the release on discogs.com and paste its ID or URL into '
                    'the top field.',
                )
                return

            if relaxed_terms:
                labels = ', '.join(_SEARCH_TERM_LABELS.get(name, name)
                                   for name in relaxed_terms)
                QtWidgets.QMessageBox.information(
                    parent, 'Discogs Enrichment',
                    'Nothing matched everything you entered, so the search was '
                    f'widened by ignoring: {labels}.\n\n'
                    'The results are ranked best-first against what Picard '
                    'already knows, but check the pressing details before '
                    'applying one.',
                )

            picker = DiscogsPickerDialog(
                results, parent=parent, match_details=match_details,
                detail_fetcher=self._make_detail_fetcher(client),
            )
            if picker.exec() and picker.selected_result:
                resource_url = (picker.selected_result.get('resource_url')
                                or picker.selected_result.get('id'))
                self._fetch_and_apply(client, pairs, groups, resource_url, parent)

        client.search_broadening(
            handler=self._guard(_on_results),
            on_relaxed=_note_relaxed,
            release_type=values.get('release_type') or 'release',
            **search_terms,
        )

    def _make_detail_fetcher(self, client):
        """Build the callback the picker uses to fill in track counts and
        company credits.

        A /database/search response carries neither, so they need one
        /releases/{id} request each. Handing the picker a small function
        instead of the client keeps the dialog free of any network or
        Picard dependency, and routes the fetch through the same cache and
        reply guard as every other request.
        """
        def _fetch(release_id, callback):
            client.get_release(release_id, self._guard(callback))
        return _fetch

    def _try_musicbrainz_first(self, client, pairs, release_data, parent, then):
        """Reverse direction: the user picked a Discogs release, so find the
        MusicBrainz release that corresponds to it and offer to match to it.

        Only meaningful for files that are NOT already matched to a
        MusicBrainz release; if they are, MusicBrainz tags are already
        present and there is nothing to gain. When the user accepts, the
        files are moved onto that MusicBrainz album and we stop here: the
        automatic path then applies the Discogs data on top, because the
        MusicBrainz release links back to this very Discogs release. That
        yields both sources without applying anything twice.

        `then` is called to continue with plain Discogs-only tagging when
        no MusicBrainz release is found, the user declines, or the files
        are already matched.
        """
        if not settings['reverse_match']:
            then()
            return

        unmatched = [owner for owner, _md in pairs
                     if hasattr(owner, 'filename') and getattr(owner, 'parent', None) is not None
                     and getattr(getattr(owner, 'parent', None), 'album', None) is None]
        if not unmatched:
            then()  # already matched to MusicBrainz, or not files at all
            return

        discogs_id = release_data.get('id')
        if not discogs_id:
            then()
            return

        def _on_mbid(mbid):
            if not mbid:
                log.debug('Discogs Enrichment: no MusicBrainz release links to '
                          'Discogs release %s, tagging from Discogs alone', discogs_id)
                then()
                return
            answer = QtWidgets.QMessageBox.question(
                parent, 'Discogs Enrichment',
                'MusicBrainz has a release matching the Discogs release you chose.\n\n'
                'Match these files to it? You will get MusicBrainz tags and the '
                'Discogs data for this same release on top.\n\n'
                'Choose No to tag from Discogs only.',
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.Yes,
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                then()
                return
            log.info('Discogs Enrichment: matching %d file(s) to MusicBrainz release %s',
                     len(unmatched), mbid)
            self.tagger.move_files_to_album(unmatched, mbid)

        client.find_musicbrainz_release(discogs_id, self._guard(_on_mbid))

    def _fetch_and_apply(self, client, pairs, groups, resource_url, parent):
        def _on_release(release_data):
            if not release_data:
                QtWidgets.QMessageBox.warning(
                    parent, 'Discogs Enrichment',
                    'Could not fetch that release from Discogs. '
                    'Check Help \u2192 View Error/Debug Log for details.',
                )
                return

            fields = _shape_fields(extract_discogs_fields(release_data))
            if not settings['import_notes']:
                fields.pop('discogs_notes', None)
            mode = settings['merge_mode'] or MODE_COMBINE

            # Multi-disc handling: a Discogs box set is ONE release, but
            # Picard splits it up, and the order it lists things in need not
            # match Discogs' disc order. Ask which of the user's GROUPS
            # (one album, or one disc's worth of files in a cluster) each
            # Discogs disc is, rather than assuming positional order.
            disc_fields_by_pair = {}
            track_fields_by_pair = {}
            disc_count = release_disc_count(release_data)
            discs = parse_discs(release_data)
            if len(discs) > 1 and len(groups) > 1:
                # Hand the matcher the actual track titles — the strongest
                # signal available, since two discs of a box can share an
                # album title and a disc number but never a track listing.
                group_info = []
                for label, indices in groups:
                    group_info.append({
                        'label': label,
                        'count': len(indices),
                        'titles': [pairs[i][1].get('title') or '' for i in indices],
                    })
                auto = auto_map(discs, group_info)
                guess = [index for index, _score in auto]
                # Unambiguous means: one group per disc, every disc matched,
                # and no group claimed twice. Anything less is a judgement
                # call and still goes to the dialog.
                unambiguous = (
                    len(discs) == len(groups)
                    and all(value >= 0 for value in guess)
                    and len(set(guess)) == len(guess)
                )
                confident = all(score >= CONFIDENT_DISC_SCORE for _i, score in auto)
                if settings['auto_disc_map'] and unambiguous and confident:
                    log.info('Discogs Enrichment: every disc matched confidently, '
                             'accepting the mapping without asking')
                    mapping = guess
                else:
                    dialog = DiscMappingDialog(discs, group_info, parent=parent)
                    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                        log.debug('Discogs Enrichment: disc mapping cancelled')
                        return
                    mapping = dialog.mapping
                for disc_index, group_index in enumerate(mapping):
                    if group_index is None or group_index < 0:
                        continue
                    disc_tags = extract_disc_fields(discs[disc_index], len(discs))
                    indices = groups[group_index][1]
                    for pair_index in indices:
                        disc_fields_by_pair[pair_index] = disc_tags
                    _collect_track_fields(
                        discs[disc_index].get('tracks'), indices, pairs,
                        track_fields_by_pair)
            elif discs:
                # Single disc: every selected file belongs to the one
                # tracklist, so align against it directly.
                _collect_track_fields(
                    discs[0].get('tracks'), list(range(len(pairs))), pairs,
                    track_fields_by_pair)

            def _continue_to_master():
                master_id = release_data.get('master_id')
                if not master_id:
                    self._show_preview_and_apply(fields, mode, pairs, parent,
                                                 disc_fields_by_pair, disc_count,
                                                 track_fields_by_pair)
                    return

                def _continue_with_master(master_data):
                    fields.update(extract_original_year(master_data))
                    _prefer_original_date(fields)
                    self._show_preview_and_apply(fields, mode, pairs, parent, disc_fields_by_pair,
                                                 disc_count, track_fields_by_pair)

                client.get_master(master_id, self._guard(_continue_with_master))

            # Offer the corresponding MusicBrainz release first; only fall
            # through to Discogs-only tagging if there isn't one or the user
            # declines it.
            self._try_musicbrainz_first(client, pairs, release_data, parent,
                                        _continue_to_master)

        client.get_release(resource_url, self._guard(_on_release))

    @staticmethod
    def _union_diff(pairs, fields, mode, disc_count=1):
        """Build the preview across EVERY selected item, not just the first.

        The apply loop skips any field the user did not tick, and the tick
        list comes from this preview — so computing it from one item alone
        meant a field that needed changing on the other items but not on
        that one was silently skipped everywhere. Rows are unioned by field
        name; where the items disagree about the current value the row says
        "(varies)" rather than implying they all share the first one's.
        """
        rows = {}
        currents = {}
        for _owner, metadata in pairs:
            for row in compute_field_diff(metadata, fields, mode=mode, disc_count=disc_count):
                name = row['field']
                currents.setdefault(name, set()).add(row['current'])
                if name not in rows:
                    rows[name] = dict(row)
        for name, row in rows.items():
            values = currents.get(name) or set()
            if len(values) > 1:
                row['current'] = '(varies across selection)'
        return list(rows.values())

    @staticmethod
    def _track_diff(pairs, track_fields_by_pair, mode):
        """Preview rows for the per-file track tags.

        One row per field name across the whole selection, not one per
        track: eleven rows all saying "title" would be unreadable, and the
        choice the user is actually making is "take Discogs' track titles or
        keep the existing ones". Where the files disagree about the current
        value — which they almost always do, every track having its own
        title — the row says so rather than implying they share the first
        one's.
        """
        if not track_fields_by_pair or mode == MODE_MB_ONLY:
            return []
        effective = MODE_OVERRIDE if mode == MODE_DISCOGS_ONLY else mode

        currents: dict[str, set] = {}
        proposed: dict[str, set] = {}
        for pair_index, (_owner, metadata) in enumerate(pairs):
            for field, value in (track_fields_by_pair.get(pair_index) or {}).items():
                if field.startswith('~'):
                    continue
                existing = metadata.getall(field)
                resolved = resolve_field_value(existing, value, effective, field=field)
                if resolved is None or existing == resolved:
                    continue  # this mode leaves the field alone
                currents.setdefault(field, set()).add('; '.join(existing) if existing else '')
                proposed.setdefault(field, set()).add('; '.join(resolved))

        rows = []
        for field in sorted(proposed):
            current_values = currents.get(field) or set()
            new_values = proposed[field]
            rows.append({
                'field': field,
                'current': ('(varies across selection)' if len(current_values) > 1
                            else next(iter(current_values), '')),
                'new': ('(one per track, from the Discogs tracklist)'
                        if len(new_values) > 1 else next(iter(new_values), '')),
                'supplementary': False,
            })
        return rows

    def _show_preview_and_apply(self, fields, mode, pairs, parent, disc_fields_by_pair=None,
                                disc_count=1, track_fields_by_pair=None):
        disc_fields_by_pair = disc_fields_by_pair or {}
        track_fields_by_pair = track_fields_by_pair or {}
        if mode == MODE_MB_ONLY:
            QtWidgets.QMessageBox.information(
                parent, 'Discogs Enrichment',
                'Tag source is set to "MusicBrainz only", so no Discogs data '
                'was written.\n\nChange it under Options \u2192 Plugins \u2192 '
                'Discogs Enrichment if you want Discogs data applied.',
            )
            return
        context_label = None
        if len(pairs) > 1:
            context_label = f'This release will be applied to all {len(pairs)} selected item(s).'
        diff_rows = self._union_diff(pairs, fields, mode, disc_count)
        # Track tags differ per file, so a union row shows "(varies)" for the
        # current value — one tickable row per field name covering the whole
        # selection, rather than a row per track.
        diff_rows += self._track_diff(pairs, track_fields_by_pair, mode)

        if not diff_rows and not disc_fields_by_pair:
            QtWidgets.QMessageBox.information(
                parent, 'Discogs Enrichment',
                'Nothing would change — the current tags already match this '
                'Discogs release under your chosen merge mode.\n\n'
                'If you expected changes, check Options \u2192 Plugins \u2192 '
                'Discogs Enrichment: "Keep MusicBrainz\'s value" only fills '
                'fields that are currently empty.',
            )
            return

        if settings['skip_preview']:
            # User has opted out of confirming each change; accept everything
            # the diff proposed.
            class _AutoAccept:
                selected_fields = {row['field'] for row in diff_rows}
            preview = _AutoAccept()
        else:
            preview = DiscogsPreviewDialog(diff_rows, parent=parent, context_label=context_label)
            if not preview.exec():
                log.debug('Discogs Enrichment: preview cancelled')
                return
        if not preview.selected_fields and not disc_fields_by_pair:
            QtWidgets.QMessageBox.information(
                parent, 'Discogs Enrichment',
                'No fields were selected, so nothing was changed.',
            )
            return

        updated_count = 0
        for pair_index, (owner, metadata) in enumerate(pairs):
            changed = False
            names_before = snapshot_artist_names(metadata)
            # Per-disc tags from the disc-mapping dialog are authoritative:
            # the user explicitly said "this album is that Discogs disc", so
            # discnumber/totaldiscs/discsubtitle are written directly rather
            # than routed through the merge mode (which could otherwise
            # leave a wrong existing disc number in place).
            for disc_field, disc_value in (disc_fields_by_pair.get(pair_index) or {}).items():
                if metadata.getall(disc_field) != [disc_value]:
                    metadata[disc_field] = disc_value
                    changed = True

            # Track tags, unlike the disc tags above, go through the merge
            # mode: "MusicBrainz preferred" must keep the MusicBrainz title.
            for field, value in (track_fields_by_pair.get(pair_index) or {}).items():
                if field.startswith('~'):
                    metadata[field] = value      # hidden variable, not a file tag
                    continue
                if field not in preview.selected_fields:
                    continue
                resolved = resolve_field_value(metadata.getall(field), value, mode, field=field)
                if resolved is not None and metadata.getall(field) != resolved:
                    metadata[field] = resolved
                    changed = True

            boxset = is_probable_boxset(metadata.getall('album'), fields.get('album'), disc_count)
            for field, value in fields.items():
                if field == 'album':
                    # 'album' can resolve to updates on TWO tags
                    # ('album' and 'discsubtitle') — each is checked
                    # against the preview selection independently,
                    # since the user may have unchecked just one.
                    # A disc-mapping discsubtitle wins over this fallback,
                    # since it names the actual disc rather than guessing.
                    resolved = resolve_album_and_subtitle(
                        metadata.getall('album'), value, metadata.getall('discsubtitle'), mode,
                    )
                    mapped = disc_fields_by_pair.get(pair_index) or {}
                    for target_field, target_value in resolved.items():
                        if target_field == 'discsubtitle' and 'discsubtitle' in mapped:
                            continue
                        if target_field in preview.selected_fields:
                            metadata[target_field] = target_value
                            changed = True
                    continue
                if field not in preview.selected_fields:
                    continue
                if is_supplementary_field(field):
                    metadata[field] = value
                    changed = True
                else:
                    effective_mode = MODE_FILL_GAPS if (boxset and field in PROTECTED_IN_BOXSET) else mode
                    resolved = resolve_field_value(metadata.getall(field), value, effective_mode, field=field)
                    if resolved is not None:
                        metadata[field] = resolved
                        changed = True
            if mode == MODE_DISCOGS_ONLY:
                if _clear_non_discogs_tags(metadata, fields, getattr(self, 'op', 'search')):
                    changed = True
            supplied_now = set(fields) | set(track_fields_by_pair.get(pair_index) or {})
            if _drop_stale_artist_ids(metadata, names_before, supplied_now, getattr(self, 'op', 'search')):
                changed = True

            if changed:
                # One removed/closed file must not abort the whole batch.
                try:
                    owner.update()
                except Exception:
                    log.warning('Discogs Enrichment: could not refresh %r', owner, exc_info=True)
                updated_count += 1

        # Picard's metadata pane caches the selection's tags and will not
        # re-read them just because we mutated metadata objects. Without
        # this the tags ARE changed on disk-save but the pane still shows
        # the old values, which looks exactly like "nothing happened".
        try:
            window = self.tagger.window
            window.metadata_box.selection_dirty = True
            window.metadata_box.update()
            window.update_selection(new_selection=True)
        except Exception:
            log.warning('Discogs Enrichment: could not refresh the metadata pane', exc_info=True)

        log.info(
            'Discogs Enrichment: applied %d field(s) to %d of %d item(s)',
            len(preview.selected_fields), updated_count, len(pairs),
        )
        if updated_count:
            QtWidgets.QMessageBox.information(
                parent, 'Discogs Enrichment',
                f'Applied {len(preview.selected_fields)} field(s) to '
                f'{updated_count} of {len(pairs)} item(s).\n\n'
                'Remember to save (Ctrl+S) to write the tags to your files.',
            )
        else:
            QtWidgets.QMessageBox.information(
                parent, 'Discogs Enrichment',
                'No item ended up changing — the selected fields already held '
                'these values.',
            )


class DiscogsSearchAction(_DiscogsSearchBase):
    """For matched albums (right pane), tracks, and individual files.

    Crucially this targets the FILES, not the Album object. Picard's
    File.save() writes from file.metadata; an Album's own metadata is a
    separate object that is copied into tracks only while the release is
    being parsed. Writing to album.metadata after load therefore updates
    the album row in the UI but saves nothing to disk — which looked
    exactly like "the tags don't get saved".
    """

    TITLE = 'Search Discogs\u2026'

    def _build_pairs(self, objs):
        pairs = []
        seen = set()

        def _add(item):
            metadata = getattr(item, 'metadata', None)
            if metadata is None or id(item) in seen:
                return
            seen.add(id(item))
            pairs.append((item, metadata))

        for obj in objs:
            iterfiles = getattr(obj, 'iterfiles', None)
            if callable(iterfiles):
                # Album or Track: tag the files these contain.
                files = list(iterfiles())
                for f in files:
                    _add(f)
                # Also update the album/track object itself so the UI rows
                # reflect the change; it is not what gets saved, but a
                # stale row next to updated files is confusing.
                _add(obj)
            else:
                _add(obj)
        return pairs

    def _build_groups(self, objs, pairs):
        """Group the selected files into the units that map onto Discogs discs.

        The key is (parent album, discnumber), not discnumber alone. When
        MusicBrainz models a box set as SEPARATE albums — one per original
        record — every one of them calls itself disc 1, so keying on the
        disc number merged them all together and the mapping dialog offered
        two identical "Disc 1" entries. Including the parent album keeps
        them distinct, while the disc number still splits a genuine
        multi-disc album into its discs.
        """
        buckets = {}
        order = []
        for index, (owner, metadata) in enumerate(pairs):
            if not hasattr(owner, 'filename'):
                continue  # container row, not a file
            parent = getattr(owner, 'parent', None)
            album = getattr(parent, 'album', None) if parent is not None else None
            album_key = id(album) if album is not None else None
            disc = (metadata.get('discnumber') or '').strip()
            folder = os.path.dirname(getattr(owner, 'filename', '') or '')
            key = (album_key, disc or folder)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(index)

        if not buckets:
            return []

        def _distinguisher(index):
            """Something that actually differs between groups.

            When MusicBrainz matches each disc of a box as its own album,
            every group can share the same album title AND call itself
            disc 1, so a label built from those alone renders identically
            for every row and the dialog becomes unusable. Fall back to
            the containing folder, then to the first track title.
            """
            owner, metadata = pairs[index]
            folder = os.path.basename(os.path.dirname(getattr(owner, 'filename', '') or ''))
            if folder:
                return folder
            return metadata.get('title') or ''

        raw = []
        for key in order:
            indices = buckets[key]
            first = pairs[indices[0]][1]
            album_title = first.get('album') or ''
            disc = (first.get('discnumber') or '').strip()
            parts = []
            if album_title:
                parts.append(album_title)
            if disc:
                parts.append(f'disc {disc}')
            raw.append((' \u2014 '.join(parts), indices))

        # Any label shared by more than one group is useless as a choice,
        # so qualify every member of that set with what actually differs.
        counts = {}
        for label, _indices in raw:
            counts[label] = counts.get(label, 0) + 1

        groups = []
        for label, indices in raw:
            if counts.get(label, 0) > 1 or not label:
                extra = _distinguisher(indices[0])
                label = f'{label} \u2014 {extra}' if label and extra else (extra or label or 'Selected files')
            groups.append((f'{label}  ({len(indices)} tracks)', indices))

        # Last resort: if anything still collides, number it so the rows
        # are at least selectable.
        seen = {}
        final = []
        for label, indices in groups:
            if label in seen:
                seen[label] += 1
                label = f'{label}  [{seen[label]}]'
            else:
                seen[label] = 1
            final.append((label, indices))
        return final

class DiscogsSearchClusterAction(_DiscogsSearchBase):
    """For unmatched clusters on the left pane \u2014 files Picard couldn't
    find a MusicBrainz release for, so they never became an Album at all.
    A Cluster has no single metadata object that governs its files the way
    Album.metadata does; each contained File has its own, so the release
    chosen here gets applied to every file in the cluster individually.
    """

    TITLE = 'Search Discogs\u2026'

    def _build_pairs(self, objs):
        pairs = []
        for obj in objs:
            files = getattr(obj, 'files', None)
            if files is not None:
                for f in files:
                    if f.metadata is not None:
                        pairs.append((f, f.metadata))
            elif getattr(obj, 'metadata', None) is not None:
                # Defensive fallback in case this ever receives something
                # that isn't a Cluster but does expose .metadata directly.
                pairs.append((obj, obj.metadata))
        return pairs

    def _build_groups(self, objs, pairs):
        """A cluster holds FILES, not albums, so the default one-group-per-
        pair would list every track in the disc-mapping dialog. Group the
        files by the disc they already claim to belong to instead: by
        existing discnumber where present, otherwise by containing folder,
        otherwise the whole cluster as one group.
        """
        buckets = {}
        order = []
        for index, (owner, metadata) in enumerate(pairs):
            if not hasattr(owner, 'filename'):
                continue  # container row, not a file
            key = (metadata.get('discnumber') or '').strip()
            if not key:
                path = getattr(owner, 'filename', '') or ''
                key = os.path.dirname(path)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(index)

        groups = []
        for key in order:
            indices = buckets[key]
            first_metadata = pairs[indices[0]][1]
            album = first_metadata.get('album') or ''
            if key and not key.startswith(os.sep):
                label = f'Disc {key}'
            elif key:
                label = os.path.basename(key) or key
            else:
                label = album or 'Selected files'
            if album and album not in label:
                label = f'{label} — {album}'
            groups.append((f'{label}  ({len(indices)} files)', indices))
        return groups


def enable(api) -> None:
    """Picard 3 entry point, called once when the plugin is enabled.

    Everything the plugin needs comes from `api`, so the first thing to do
    is bind it for the modules that reach for settings and logging. Nothing
    above this point may touch the api at import time: Picard imports the
    module before calling enable().

    Note the two shape changes from the Picard 2.x API. Actions register as
    CLASSES, not instances — Picard sets `api` on the class and constructs
    it itself. And a metadata processor now receives the api as its first
    argument, so process_album's signature gained one parameter.
    """
    bind_api(api)
    log.debug('Discogs Enrichment: enabling')

    api.register_album_metadata_processor(process_album)
    # Closes the race between the asynchronous Discogs reply and Picard
    # linking files to tracks; see apply_to_late_file().
    api.register_file_post_addition_to_track_processor(apply_to_late_file)
    api.register_options_page(DiscogsOptionsPage)

    api.register_album_action(DiscogsSearchAction)
    api.register_file_action(DiscogsSearchAction)
    api.register_cluster_action(DiscogsSearchClusterAction)

    # Cover art is a bonus. If the provider API differs on this build,
    # tagging still has to work, so a failure here is logged, not raised.
    try:
        from .discogs_coverart import CoverArtProviderDiscogs
        api.register_cover_art_provider(CoverArtProviderDiscogs)
        log.debug('Discogs Enrichment: cover art provider registered')
    except Exception:
        log.warning('Discogs Enrichment: could not register the cover art provider',
                    exc_info=True)

    log.debug('Discogs Enrichment: enabled')


def disable() -> None:
    """Called when the plugin is disabled. Picard removes the registrations
    itself; this just drops the api reference and the response cache so a
    disable/enable cycle starts clean rather than serving stale releases.
    """
    try:
        clear_cache()
    except Exception:
        pass
    log.debug('Discogs Enrichment: disabled')
    unbind_api()
