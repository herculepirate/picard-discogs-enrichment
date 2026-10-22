"""Maps a Discogs release JSON object onto Picard tag names.

Discogs' `/releases/{id}` response shape (relevant fields only):

    {
        "title": "...",
        "artists": [{"name": "...", ...}, ...],
        "labels": [{"name": "...", "catno": "..."}, ...],
        "genres": ["Rock", ...],
        "styles": ["Indie Rock", "Emo", ...],
        "identifiers": [{"type": "Barcode", "value": "..."}, ...],
        "country": "...",
        "released": "2021-02-26",
        "formats": [{"name": "Vinyl", "descriptions": ["LP", "Album"], ...}],
        "notes": "...",
        "tracklist": [{"position": "1", "title": "...", "duration": "3:12"}, ...],
    }

We deliberately keep this mapping shallow — enough to fill common gaps
(genre/style, catalog number, label, pressing/format notes) without trying
to reproduce Discogs' full data model.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Fields Picard/MusicBrainz already owns by default. Whether Discogs is
# allowed to touch them (and how) is controlled by the merge mode — see
# resolve_field_value() below. Includes the real Picard/MusicBrainz credit
# tags Discogs' extraartists can supply (producer, engineer, etc.) — these
# have genuine MB-native meaning, so they follow the user's chosen merge
# mode like any other field MB might already have a value for, rather than
# being unconditionally supplementary.
BACKFILL_ONLY_FIELDS = (
    'artist',
    'albumartist',
    'artistsort',
    'genre',
    'label',
    'catalognumber',
    'barcode',
    'date',
    'releasecountry',
    'media',
    'albumartistsort',
    'originalyear',
    'originaldate',
    'discnumber',
    'totaldiscs',
    'producer',
    'engineer',
    'mixer',
    'remixer',
    'arranger',
    'conductor',
    'lyricist',
    'composer',
    'writer',
    'director',
    'djmixer',
)
# 'album' is deliberately NOT in here — it gets special handling via
# resolve_album_and_subtitle() below instead of the generic combine/
# override logic, since joining two different album titles into one
# string (as generic combine would) is meaningless for box sets.

# Fields that are Discogs-specific concepts with no direct MB equivalent.
# These are always added (never overwrite anything, since MB has no
# opinion on them), prefixed to make their source obvious in a tag editor.
# Also includes dynamically-named fields (performer:<instrument>,
# discogs_credit_<role>, discogs_<company role>) — see
# is_supplementary_field() below, since those can't be listed exhaustively
# up front.
SUPPLEMENTARY_FIELDS = (
    'discogs_style',
    'discogs_catalognumber',
    'discogs_release_id',
    'discogs_notes',
)


def is_supplementary_field(field: str) -> bool:
    """True for any field that should always be added (never gated by the
    merge mode), including dynamically-named ones that can't be listed in
    the static SUPPLEMENTARY_FIELDS tuple: performer:<instrument> credits
    and discogs_<company role>/discogs_credit_<role> fields.
    """
    return (
        field in SUPPLEMENTARY_FIELDS
        or field.startswith('performer:')
        or field.startswith('discogs_credit_')
        or field.startswith('discogs_company_')
        or field.startswith('discogs_identifier_')
        or field.startswith('discogs_')
    )


def _slugify(text: str) -> str:
    """Turn a free-text Discogs role/company-type string into a safe tag
    name suffix, e.g. "Mastered At" -> "mastered_at".
    """
    slug = re.sub(r'[^a-z0-9]+', '_', text.strip().lower())
    return slug.strip('_')


# Discogs `extraartists[].role` strings (lowercased, trimmed) that map onto
# real Picard/MusicBrainz credit tags. Not exhaustive — anything not found
# here is assumed to be a performance/instrument credit (see
# extract_credits() below) and goes to performer:<role>, since that's the
# most common remaining category (Bass Guitar, Drums, Vocals, ...).
CREDIT_ROLE_TO_TAG = {
    'producer': 'producer',
    'co-producer': 'producer',
    'executive producer': 'producer',
    'engineer': 'engineer',
    'recorded by': 'engineer',
    'recording engineer': 'engineer',
    'mastered by': 'engineer',
    'mastering engineer': 'engineer',
    'mixed by': 'mixer',
    'remix': 'remixer',
    'remixed by': 'remixer',
    'arranged by': 'arranger',
    'orchestrated by': 'arranger',
    'conductor': 'conductor',
    'conducted by': 'conductor',
    'lyrics by': 'lyricist',
    'words by': 'lyricist',
    'music by': 'composer',
    'composed by': 'composer',
    'written-by': 'writer',
    'written by': 'writer',
    'directed by': 'director',
    'dj mix': 'djmixer',
}

# Roles that are real credits but not performances and have no Picard/MB
# tag equivalent — these go to discogs_credit_<slug> instead of
# performer:<role>, since "performer: design" would be misleading.
NON_PERFORMANCE_ROLES = {
    'design', 'artwork', 'artwork by', 'photography', 'photography by',
    'liner notes', 'package', 'coordinator', 'layout', 'illustration',
    'illustrations', 'sleeve notes', 'art direction', 'graphic design',
    'lacquer cut by',
}

# Discogs `companies[].entity_type_name` -> discogs_company_<slug> suffix.
# Falls back to a slugified version of the raw entity_type_name for
# anything not explicitly listed, so nothing is silently dropped.
COMPANY_ROLE_TO_SLUG = {
    'pressed by': 'pressed_by',
    'mastered at': 'mastered_at',
    'lacquer cut at': 'mastered_at',
    'distributed by': 'distributed_by',
    'published by': 'published_by',
    'licensed to': 'licensed_to',
    'phonographic copyright (p)': 'phonographic_copyright',
    'copyright (c)': 'copyright',
    'marketed by': 'marketed_by',
    'record company': 'record_company',
    'manufactured by': 'manufactured_by',
}


def extract_credits(extraartists: list[dict]) -> dict[str, list[str]]:
    """Map Discogs extraartists credits onto Picard tags.

    Per explicit instruction, track-scoped credits are imported the same
    as release-wide ones (i.e. the `tracks` field on each credit is
    ignored) — a credit that only applied to a few tracks may end up
    attributed to the whole album. A Discogs role string can itself list
    several roles ("Producer, Written-By") and is split on commas — but
    only OUTSIDE any "[...]" qualifier, since those can contain their own
    commas (e.g. "Engineer [Brooklyn, NY]", "Guitar [Rhythm, Lead]").

    Returns a dict of tag_name -> list of names, ready to merge into the
    same shape extract_discogs_fields() produces. Recognized production/
    writing roles use real Picard tags (producer, engineer, mixer, ...);
    known non-performance roles (Design, Artwork, ...) use
    discogs_credit_<slug>; everything else is assumed to be a performance/
    instrument credit and uses performer:<role>.
    """
    out: dict[str, list[str]] = {}
    for credit in extraartists or []:
        name = clean_artist_name(credit.get('anv') or credit.get('name'))
        role_text = (credit.get('role') or '').strip()
        if not name or not role_text:
            continue
        # Strip "[...]" qualifiers before splitting, so a comma inside one
        # (a location, an instrument variant, ...) doesn't get mistaken
        # for a role separator.
        role_text_no_brackets = re.sub(r'\[[^\]]*\]', '', role_text)
        for role in role_text_no_brackets.split(','):
            role = role.strip()
            if not role:
                continue
            role_lower = role.lower()
            if role_lower in CREDIT_ROLE_TO_TAG:
                tag = CREDIT_ROLE_TO_TAG[role_lower]
            elif role_lower in NON_PERFORMANCE_ROLES:
                tag = f'discogs_credit_{_slugify(role)}'
            else:
                tag = f'performer:{role_lower}'
            out.setdefault(tag, [])
            for index, existing in enumerate(out[tag]):
                if fold_for_compare(existing) == fold_for_compare(name):
                    out[tag][index] = better_variant(existing, name)
                    break
            else:
                out[tag].append(name)
    return out


def extract_company_credits(companies: list[dict]) -> dict[str, list[str]]:
    """Map Discogs companies (pressing plant, mastering studio, distributor,
    etc.) onto discogs_company_<slug> supplementary fields.
    """
    out: dict[str, list[str]] = {}
    for company in companies or []:
        name = clean_artist_name(company.get('name'))
        role_text = (company.get('entity_type_name') or '').strip()
        if not name or not role_text:
            continue
        slug = COMPANY_ROLE_TO_SLUG.get(role_text.lower(), _slugify(role_text))
        field = f'discogs_company_{slug}'
        out.setdefault(field, [])
        for index, existing in enumerate(out[field]):
            if fold_for_compare(existing) == fold_for_compare(name):
                out[field][index] = better_variant(existing, name)
                break
        else:
            out[field].append(name)
    return out


def extract_original_year(master: dict[str, Any] | None) -> dict[str, str]:
    """Pull the earliest known release year from a Discogs master release
    JSON (fetched separately via GET /masters/{master_id}) — distinct from
    the specific pressing's own 'released'/'year', which may be a much
    later reissue date.
    """
    if not master:
        return {}
    year = master.get('year')
    if not year:
        return {}
    # 'originalyear' and 'originaldate' are real Picard tags (see
    # picard/util/tags.py) — use them rather than a custom name, so players
    # and Picard's own scripting understand the value.
    return {'originalyear': str(year), 'originaldate': str(year)}


# Discogs disambiguates same-named artists with a numeric suffix, e.g.
# "Nirvana (2)". That is a database artefact, not part of the name.
_DISCOGS_ARTIST_SUFFIX_RE = re.compile(r'\s*\(\d+\)\s*$')


# Tags that describe the SAME artist as `artist` / `albumartist`. When Discogs
# replaces a name, these still describe the OLD credit and have to go,
# otherwise a player that keys artists by MusicBrainz ID (Lyrion, Navidrome)
# shows the new name against the old artist's ID.
ARTIST_COMPANION_TAGS = {
    'artist': ('musicbrainz_artistid', 'artistsort', 'artists'),
    'albumartist': ('musicbrainz_albumartistid', 'albumartistsort', 'albumartists'),
}


def same_artist_name(a, b) -> bool:
    """True if two artist-name values are the same once case, spacing and
    Unicode form are ignored (a list is compared as its "; " join)."""
    def norm(v):
        if isinstance(v, (list, tuple)):
            v = '; '.join(str(x) for x in v)
        return ' '.join(unicodedata.normalize('NFKC', str(v or '')).casefold().split())
    return norm(a) == norm(b)


def snapshot_artist_names(metadata) -> dict:
    """The current artist / albumartist values, taken BEFORE Discogs writes."""
    return {field: list(metadata.getall(field)) for field in ARTIST_COMPANION_TAGS}


def drop_stale_artist_companions(metadata, before: dict, supplied) -> list[str]:
    """Delete MusicBrainz artist IDs / name lists that belong to an artist
    name Discogs has just replaced.

    `before` comes from snapshot_artist_names(). A companion tag is removed
    only when the name really changed and Discogs did not supply that tag
    itself. Release and recording IDs are never touched, so the files stay
    re-matchable in Picard. Returns the tags removed.
    """
    removed = []
    for field, companions in ARTIST_COMPANION_TAGS.items():
        if same_artist_name(before.get(field), metadata.getall(field)):
            continue
        for tag in companions:
            if tag in supplied or not metadata.getall(tag):
                continue
            metadata.delete(tag)
            removed.append(tag)
    return removed


# How each artist field relates to MusicBrainz's per-artist data. Picard
# writes a collaboration as ONE joined credit ("A & B") next to SEVERAL
# artist IDs. Players that key artists by MusicBrainz ID (Lyrion, Navidrome)
# then give the whole joined credit to the FIRST ID, so artist A's page is
# renamed "A & B". Writing one name per ID, in the same order, avoids that and
# lists the album under every collaborator.
ARTIST_CREDIT_TAGS = {
    'albumartist': {
        'ids': 'musicbrainz_albumartistid',
        'names': ('albumartists', '~albumartists'),
        'plural': 'albumartists',
        'sort': 'albumartistsort',
        'sorts': ('~albumartists_sort',),
    },
    'artist': {
        'ids': 'musicbrainz_artistid',
        'names': ('artists', '~artists'),
        'plural': 'artists',
        'sort': 'artistsort',
        'sorts': ('~artists_sort',),
    },
}

# Words and symbols that may sit BETWEEN collaborator names in a credit.
_CREDIT_JOIN_WORDS = {
    'and', 'with', 'feat', 'featuring', 'ft', 'vs', 'x', 'meets', 'presents',
    'pres', 'e', 'y', 'und', 'et', 'og', 'och', 'en',
}
_CREDIT_JOIN_SYMBOLS_RE = re.compile(r'[&/,;+×·•|~\-–—]')


def _norm_name(value) -> str:
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def _is_credit_join(gap: str) -> bool:
    """True if the text between two names is only joining words/symbols."""
    words = _CREDIT_JOIN_SYMBOLS_RE.sub(' ', gap).replace('.', ' ').split()
    return all(word in _CREDIT_JOIN_WORDS for word in words)


def credit_matches_names(value, names) -> bool:
    """True if `value` (a credit string, or a list of names) names exactly
    the artists in `names`, in that order, joined only by "&", "/", ",",
    "and", "feat." and the like. Case, spacing and Unicode form are ignored.

    "Max Corbacho & Bruno Sanfilippo", "Max Corbacho / Bruno Sanfilippo" and
    ["Max Corbacho", "Bruno Sanfilippo"] all match
    ["Max Corbacho", "Bruno Sanfilippo"]; "Bruno Sanfilippo & Max Corbacho"
    does not (wrong order), nor does a credit naming someone else.
    """
    names = [n for n in (names or []) if _norm_name(n)]
    if not names:
        return False
    values = value if isinstance(value, (list, tuple)) else [value]
    text = _norm_name('; '.join(str(v) for v in values))
    pos = 0
    for i, name in enumerate(names):
        wanted = _norm_name(name)
        idx = text.find(wanted, pos)
        if idx < 0:
            return False
        gap = text[pos:idx]
        if (i == 0 and gap.strip()) or not _is_credit_join(gap):
            return False
        pos = idx + len(wanted)
    return not text[pos:].strip()


def credit_names_same_artists(value, names) -> bool:
    """Like credit_matches_names(), but the credit may list the artists in
    any order (Discogs and MusicBrainz sometimes order a duo differently).
    Only tried for up to five names."""
    names = [n for n in (names or []) if _norm_name(n)]
    if credit_matches_names(value, names):
        return True
    if not 1 < len(names) <= 5:
        return False
    from itertools import permutations
    return any(credit_matches_names(value, list(p)) for p in permutations(names))


def _first_nonempty(metadata, keys) -> list[str]:
    for key in keys:
        values = [str(v) for v in metadata.getall(key) if str(v).strip()]
        if values:
            return values
    return []


def snapshot_artist_credits(metadata) -> dict:
    """Artist names, MusicBrainz per-artist names, IDs and sort names,
    taken BEFORE Discogs writes anything."""
    snap = {}
    for field, spec in ARTIST_CREDIT_TAGS.items():
        value = list(metadata.getall(field))
        names = _first_nonempty(metadata, spec['names'])
        if not names and len(value) > 1:
            names = [str(v) for v in value]
        snap[field] = {
            'value': value,
            'names': names,
            'ids': [str(v) for v in metadata.getall(spec['ids']) if str(v).strip()],
            'sorts': _first_nonempty(metadata, spec['sorts']),
        }
    return snap


def settle_artist_credits(metadata, before: dict, supplied=(), prefer_mb_names: bool = True) -> list[str]:
    """Make each artist field safe for players that key artists by
    MusicBrainz ID. Returns a short description of every change made.

    - The credit names the same artists MusicBrainz lists, in the same order
      (whether Discogs rewrote it or not): a collaboration is written as one
      name per artist, and the MusicBrainz IDs are kept in that same order.
      A single artist is left as it is.
    - The credit names the artists differently (Discogs' "credited as" short
      names such as "Adams / Reich", or other people) and MusicBrainz has an
      ID for every artist: MusicBrainz's names win, one per artist, with
      their IDs, per-artist lists and sort names. Artist pages in players
      that key by ID then stay correct and complete.
    - Same, but `prefer_mb_names` is False ("Discogs only"), or MusicBrainz
      has no ID for each artist: the credit stays as written and the
      MusicBrainz IDs, per-artist names and sort name of the old credit are
      removed, unless Discogs supplied that tag itself.
    """
    supplied = set(supplied or ())
    changes = []
    for field, spec in ARTIST_CREDIT_TAGS.items():
        snap = (before or {}).get(field) or {}
        value = list(metadata.getall(field))
        if not value:
            continue
        names = snap.get('names') or []
        ids = snap.get('ids') or []
        sorts = snap.get('sorts') or []

        if credit_names_same_artists(value, names):
            if len(names) < 2:
                continue                     # single artist: nothing to do
            if [str(v) for v in value] != names:
                metadata[field] = list(names)
                changes.append(f'{field}: {value} -> {names}')
            if list(metadata.getall(spec['plural'])) != names:
                metadata[spec['plural']] = list(names)
            if len(ids) == len(names):
                if list(metadata.getall(spec['ids'])) != ids:
                    metadata[spec['ids']] = list(ids)
                    changes.append(f'{spec["ids"]}: restored {len(ids)} ID(s)')
            elif metadata.getall(spec['ids']):
                metadata.delete(spec['ids'])
                changes.append(f'{spec["ids"]}: removed ({len(ids)} ID(s) for {len(names)} names)')
            if len(sorts) == len(names):
                if list(metadata.getall(spec['sort'])) != sorts:
                    metadata[spec['sort']] = list(sorts)
            elif metadata.getall(spec['sort']):
                metadata.delete(spec['sort'])
            continue

        if same_artist_name(snap.get('value'), value):
            continue                         # untouched and not a known collaboration

        if prefer_mb_names and names and len(ids) == len(names):
            # MusicBrainz knows exactly who these artists are; keep its names.
            metadata[field] = list(names)
            if len(names) > 1 or metadata.getall(spec['plural']):
                metadata[spec['plural']] = list(names)
            metadata[spec['ids']] = list(ids)
            if len(sorts) == len(names):
                metadata[spec['sort']] = list(sorts)
            elif spec['sort'] not in supplied and metadata.getall(spec['sort']):
                metadata.delete(spec['sort'])
            changes.append(f'{field}: kept MusicBrainz names {names} with their IDs '
                           f'(Discogs credited {value})')
            continue
        for tag in (spec['ids'], spec['plural'], spec['sort']):
            if tag in supplied or not metadata.getall(tag):
                continue
            metadata.delete(tag)
            changes.append(f'{tag}: removed (credit now names different artists)')
    return changes


def clean_artist_name(name: str) -> str:
    """Strip Discogs' numeric disambiguation suffix from an artist name."""
    return _DISCOGS_ARTIST_SUFFIX_RE.sub('', str(name or '')).strip()


def format_artist_credit(artists) -> str:
    """Build a display artist credit from a Discogs artists array.

    Honours each entry's `anv` (artist name variation — how the artist was
    actually credited on this release) in preference to the canonical
    name, and each entry's `join` string ("&", ",", "Feat.") so
    collaborations read the way the release presents them rather than
    being glued together with a hardcoded separator.
    """
    parts: list[str] = []
    for entry in artists or []:
        if not isinstance(entry, dict):
            continue
        name = clean_artist_name(entry.get('anv') or entry.get('name'))
        if not name:
            continue
        if parts:
            join = str(parts.pop()).strip()
            parts.append(join if join else '')
        parts.append(name)
        join = str(entry.get('join') or '').strip()
        parts.append(join)
    # Drop a trailing join token, then stitch with sensible spacing.
    while parts and not parts[-1]:
        parts.pop()
    if parts and parts[-1] in (',', '&', '/'):
        parts.pop()
    text = ''
    for token in parts:
        if not token:
            continue
        if token == ',':
            text = text.rstrip() + ', '
        elif text:
            text = f'{text.rstrip()} {token} '
        else:
            text = f'{token} '
    return text.strip()


def _normalize_discogs_date(value) -> str:
    """Normalise a Discogs date into a valid tag value.

    Discogs writes unknown month/day components as 00 ("1987-07-00",
    "2020-00-00"). Those are not valid dates, so trailing unknown parts are
    dropped: "1987-07-00" -> "1987-07", "2020-00-00" -> "2020". Returns ''
    if there is no usable year.
    """
    text = str(value or '').strip()
    if not text:
        return ''
    parts = text.split('-')
    year = parts[0].strip()
    if not (year.isdigit() and len(year) == 4):
        return ''
    out = [year]
    for part in parts[1:3]:
        part = part.strip()
        if not part.isdigit() or int(part) == 0:
            break
        out.append(f'{int(part):02d}')
    return '-'.join(out)


def _casing_rank(text: str) -> int:
    """Rank how well-cased a string is; higher is better.

    Discogs contributors enter the same credit in different cases, and
    deduplication has to pick one. Keeping whichever happened to come first
    means an ALL CAPS entry can win and end up in the tags, so prefer
    normal mixed case, then lower case, and treat shouting as the worst.
    """
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0
    if len(letters) > 3 and all(ch.isupper() for ch in letters):
        return 0                      # ALL CAPS
    if all(ch.islower() for ch in letters):
        return 1                      # all lower case
    return 2                          # Mixed Case


def better_variant(current: str, candidate: str) -> str:
    """Pick the more presentable of two equivalent spellings."""
    if _casing_rank(candidate) > _casing_rank(current):
        return candidate
    return current


def dedupe(values) -> list:
    """Remove repeats from a list Discogs itself supplied, keeping order.

    A single release routinely credits the same catalogue number under more
    than one label entry (label plus series, or two label credits), and the
    same genre or style can appear twice. Left alone that produced tags
    like "UICY-77820; UICY-77820" from one source, with nothing to merge.
    Comparison uses fold_for_compare, so punctuation and case variants
    count as the same value.
    """
    order, chosen = [], {}
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        key = fold_for_compare(text)
        if key not in chosen:
            order.append(key)
            chosen[key] = text
        else:
            chosen[key] = better_variant(chosen[key], text)
    return [chosen[key] for key in order]


def extract_discogs_fields(release: dict[str, Any]) -> dict[str, str | list[str]]:
    """Pull the fields we care about out of a Discogs release JSON blob."""
    out: dict[str, str | list[str]] = {}

    if artist_credit := format_artist_credit(release.get('artists')):
        # Discogs has no separate album-artist concept: the release-level
        # `artists` array IS the album artist. Populate both, since Picard
        # and every player use albumartist for grouping.
        out['artist'] = artist_credit
        out['albumartist'] = artist_credit

    if title := release.get('title'):
        out['album'] = title

    if genres := dedupe(release.get('genres')):
        out['genre'] = genres

    if styles := dedupe(release.get('styles')):
        out['discogs_style'] = styles

    if labels := release.get('labels'):
        label_names = dedupe(clean_artist_name(entry.get('name')) for entry in labels)
        # Discogs writes the literal word "none" when a release has no
        # catalogue number; that is not a catalogue number.
        catnos = dedupe(entry.get('catno', '') for entry in labels
                        if str(entry.get('catno') or '').strip().casefold() != 'none')
        if label_names:
            out['label'] = label_names
        if catnos:
            out['catalognumber'] = catnos
            out['discogs_catalognumber'] = catnos

    for identifier in release.get('identifiers') or []:
        if identifier.get('type') == 'Barcode' and identifier.get('value'):
            out['barcode'] = identifier['value'].replace(' ', '')
            break

    # Discogs dates need normalising and a fallback. `released` is often
    # partial ("1987-07-00", "2020-00-00") because Discogs uses 00 for an
    # unknown month/day, and some releases have no `released` at all but do
    # carry a `year`. Writing "2020-00-00" as a date tag is invalid, and
    # emitting nothing at all leaves whatever junk date the file already
    # had in place.
    date_value = _normalize_discogs_date(release.get('released'))
    if not date_value and release.get('year'):
        date_value = _normalize_discogs_date(str(release['year']))
    if date_value:
        out['date'] = date_value
        # Always keep the pressing's own date under its own name. If the
        # user prefers the original release date in `date`, this is where
        # the specific pressing's date remains visible.
        out['discogs_released'] = date_value
        # Baseline original date, so that ONE Discogs release always yields
        # ONE original year across every disc it contains. Without this,
        # a release with no Discogs master (common for compilations) left
        # MusicBrainz's per-disc original years in place — each disc of a
        # box keeping the year of the original album it reissues. A naming
        # script that folders by original year then scatters a single box
        # across one folder per year. extract_original_year() overwrites
        # this with the master's true original year when there is a master.
        out['originaldate'] = date_value
        out['originalyear'] = date_value[:4]

    if country := release.get('country'):
        # Picard's tag is 'releasecountry', not 'country' — see
        # picard/util/tags.py and mbjson.py's own country->releasecountry map.
        out['releasecountry'] = country

    if formats := release.get('formats'):
        format_descriptions = []
        for fmt in formats:
            name = fmt.get('name', '')
            descriptions = fmt.get('descriptions', [])
            format_descriptions.append(', '.join(filter(None, [name, *descriptions])))
        if format_descriptions:
            out['media'] = '; '.join(format_descriptions)

    if release_id := release.get('id'):
        out['discogs_release_id'] = str(release_id)

    if notes := release.get('notes'):
        # Discogs notes are free text and can be long — keep them, but this
        # is exactly the kind of field a user may want to disable via the
        # options page, since it can be noisy.
        out['discogs_notes'] = notes.strip()

    out.update(extract_credits(release.get('extraartists', [])))
    out.update(extract_company_credits(release.get('companies', [])))
    out.update(extract_remaining_release_fields(release))

    return out


# Top-level Discogs release keys already consumed by extract_discogs_fields()
# above, or deliberately excluded (binary/large/irrelevant for tagging).
_HANDLED_RELEASE_KEYS = {
    'artists', 'title', 'genres', 'styles', 'labels', 'identifiers',
    'released', 'country', 'formats', 'id', 'notes', 'extraartists',
    'companies',
}
_EXCLUDED_RELEASE_KEYS = {
    'images', 'thumb', 'videos', 'tracklist', 'resource_url', 'master_url',
    'artists_sort',  # handled explicitly below as albumartistsort
}

# Discogs identifier types that map onto real Picard tags rather than a
# generic discogs_identifier_* field.
_IDENTIFIER_TYPE_TO_TAG = {
    'barcode': 'barcode',
    'asin': 'asin',
}


def extract_remaining_release_fields(release: dict[str, Any]) -> dict[str, str | list[str]]:
    """Capture everything else the Discogs release JSON offers, so nothing
    useful is silently dropped.

    Real Picard tags are used wherever one exists (albumartistsort,
    releasestatus, asin, ...); everything else lands under a
    discogs_<key> name. Marketplace/community counters (have/want/rating,
    lowest_price, num_for_sale) change constantly and are captured too,
    but they're a snapshot at tagging time, not stable facts.
    """
    out: dict[str, str | list[str]] = {}

    if artists_sort := release.get('artists_sort'):
        sort_name = clean_artist_name(artists_sort)
        out['albumartistsort'] = sort_name
        out['artistsort'] = sort_name

    if status := release.get('status'):
        # Discogs 'Accepted'/'Draft' is a database-moderation status, not
        # MusicBrainz's official/promotion/bootleg releasestatus — keep it
        # under its own name rather than colliding with a different concept.
        out['discogs_status'] = str(status).strip()

    # Every identifier type, not just Barcode (Label Code, Matrix / Runout,
    # Rights Society, Mastering SID Code, ASIN, ...).
    for identifier in release.get('identifiers', []) or []:
        itype = (identifier.get('type') or '').strip()
        value = (identifier.get('value') or '').strip()
        if not itype or not value:
            continue
        description = (identifier.get('description') or '').strip()
        tag = _IDENTIFIER_TYPE_TO_TAG.get(itype.lower())
        if tag == 'barcode':
            continue  # already set (normalized) by extract_discogs_fields
        if not tag:
            tag = f'discogs_identifier_{_slugify(itype)}'
        display = f'{value} ({description})' if description else value
        existing = out.get(tag)
        if isinstance(existing, list):
            if not any(fold_for_compare(e) == fold_for_compare(display) for e in existing):
                existing.append(display)
        elif existing:
            if fold_for_compare(display) != fold_for_compare(existing):
                out[tag] = [existing, display]
        else:
            out[tag] = display

    if series := release.get('series'):
        names = dedupe(clean_artist_name(s.get('name')) for s in series)
        if names:
            out['discogs_series'] = names

    community = release.get('community') or {}
    if isinstance(community, dict):
        rating = community.get('rating') or {}
        if isinstance(rating, dict):
            if rating.get('average'):
                out['discogs_community_rating'] = str(rating['average'])
            if rating.get('count'):
                out['discogs_community_rating_count'] = str(rating['count'])
        for key in ('have', 'want'):
            if community.get(key) is not None:
                out[f'discogs_community_{key}'] = str(community[key])

    # Remaining simple scalars, captured generically so future Discogs
    # additions aren't silently lost.
    for key, value in release.items():
        if key in _HANDLED_RELEASE_KEYS or key in _EXCLUDED_RELEASE_KEYS:
            continue
        if key in ('status', 'series', 'community'):
            continue  # handled explicitly above
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            tag = f'discogs_{_slugify(key)}'
            if tag not in out:
                out[tag] = str(value).strip()

    return out


def parse_discs(release: dict[str, Any]) -> list[dict]:
    """Split a Discogs tracklist into discs.

    Handles both real Discogs conventions (per their Database Guidelines
    12.2.9 and the type_='heading' convention):

      1. Position prefixes — "1-1", "1-2", "2-1", or "CD1-1", "DVD1-1".
      2. Heading rows — entries with type_='heading' whose title names the
         section (used on box sets where each disc is an original album).

    Returns a list of {'number': int, 'title': str, 'tracks': [...]}
    ordered by disc number. A single-disc release returns one entry with
    title '' — callers can treat len(...) <= 1 as "not multi-disc".
    """
    # Keep only dict entries. A malformed or unexpected tracklist shape must
    # not raise here: this runs on the main tagging path, where an
    # AttributeError is swallowed by the reply guard and silently abandons
    # the whole enrichment instead of degrading to single-disc.
    tracklist = _flatten_index_entries(
        [e for e in (release.get('tracklist') or []) if isinstance(e, dict)])
    discs: list[dict] = []

    # --- convention 1: position prefixes -------------------------------
    position_re = re.compile(r'^\s*(?:[A-Za-z]{1,4})?(\d+)\s*[-.]\s*\d+', re.IGNORECASE)
    by_number: dict[int, dict] = {}
    for entry in tracklist:
        if (entry.get('type_') or 'track') != 'track':
            continue
        if '_parent_position' in entry:
            # A movement: its work's position decides the disc. Its own number counts within
            # the work ("1.1", "2a"), so only a hyphenated disc prefix ("1-2.1") is trusted.
            match = (position_re.match(entry['_parent_position'])
                     or _SUB_DISC_RE.match(entry.get('position') or ''))
        else:
            match = position_re.match(entry.get('position') or '')
        if not match:
            by_number = {}
            break
        number = int(match.group(1))
        by_number.setdefault(number, {'number': number, 'title': '', 'tracks': []})
        by_number[number]['tracks'].append(entry)

    if len(by_number) > 1:
        discs = [by_number[n] for n in sorted(by_number)]
        # Headings, when present alongside numbered positions, name the discs.
        headings = [e.get('title', '').strip() for e in tracklist
                    if (e.get('type_') or '') == 'heading' and e.get('title')]
        if len(headings) == len(discs):
            for disc, heading in zip(discs, headings):
                disc['title'] = heading
        return discs

    # --- convention 2: heading rows ------------------------------------
    current: dict | None = None
    for entry in tracklist:
        etype = entry.get('type_') or 'track'
        if etype == 'heading':
            current = {
                'number': len(discs) + 1,
                'title': (entry.get('title') or '').strip(),
                'tracks': [],
            }
            discs.append(current)
        elif etype == 'track':
            if current is None:
                current = {'number': 1, 'title': '', 'tracks': []}
                discs.append(current)
            current['tracks'].append(entry)

    discs = [d for d in discs if d['tracks']]
    # Headings also mark SECTIONS within one disc ("Women", "Side A", a suite). When the
    # release's formats declare a single disc, the headings cannot be disc boundaries.
    declared = _declared_disc_total(release)
    if len(discs) > 1 and declared == 1:
        discs = [{'number': 1, 'title': '',
                  'tracks': [track for disc in discs for track in disc['tracks']]}]
    for index, disc in enumerate(discs, start=1):
        disc['number'] = index

    if not discs:
        return []
    return discs


_SUB_DISC_RE = re.compile(r'^\s*(?:[A-Za-z]{1,4})?(\d+)\s*-\s*\d+', re.IGNORECASE)


def _flatten_index_entries(tracklist: list[dict]) -> list[dict]:
    """Replace each "index" entry (a work split into movements) with its sub_tracks, which
    hold the audio. A movement's own position ("2a", "1-2.1", or blank) numbers it within
    its work, so its parent's position is kept for disc numbering. Matches
    count_audio_tracks(): an index entry without sub_tracks has no audio."""
    out: list[dict] = []
    for entry in tracklist:
        kind = str(entry.get('type_') or entry.get('type') or 'track').strip().casefold()
        if kind != 'index':
            out.append(entry)
            continue
        for sub in entry.get('sub_tracks') or []:
            if not isinstance(sub, dict):
                continue
            sub_kind = str(sub.get('type_') or sub.get('type') or 'track').strip().casefold()
            if sub_kind != 'track':
                continue
            flat = dict(sub)
            flat['type_'] = 'track'
            flat['_parent_position'] = entry.get('position') or ''
            out.append(flat)
    return out


def _declared_disc_total(release: dict[str, Any]) -> int | None:
    """Discs declared by the release's formats (CD qty 2 + DVD qty 1 = 3), or None when the
    release lists no formats. Digital media count as one disc (qty counts files there)."""
    formats = [f for f in (release.get('formats') or []) if f]
    if not formats:
        return None
    total = 0
    for fmt in formats:
        if not isinstance(fmt, dict):
            total += 1
            continue
        if str(fmt.get('name') or '').strip().casefold() in _NON_DISC_MEDIA:
            total += 1
            continue
        try:
            total += int(str(fmt.get('qty') or '1').strip())
        except (TypeError, ValueError):
            total += 1
    return max(total, 1)


def extract_disc_fields(disc: dict, total_discs: int) -> dict[str, str]:
    """Per-disc tags for one entry from parse_discs(): disc number, total
    disc count, and the disc's own title as discsubtitle when the box set
    names its discs (e.g. each original album inside a compilation).
    """
    out: dict[str, str] = {
        'discnumber': str(disc.get('number') or 1),
        'totaldiscs': str(total_discs),
    }
    title = (disc.get('title') or '').strip()
    if title:
        out['discsubtitle'] = title
    return out


# ---------------------------------------------------------------------------
# Track-level tags
# ---------------------------------------------------------------------------
# Everything above is album- or disc-level. These three functions are what
# make "Discogs only" a usable mode rather than a way to erase your titles:
# with MusicBrainz data cleared, the track identity has to come from the
# Discogs tracklist or it comes from nowhere.


def extract_track_fields(track: dict, position: int, total: int) -> dict[str, str]:
    """Tags for one audio track from a Discogs tracklist.

    `position` is the 1-based position within its disc, used rather than
    Discogs' own `position` string because that is free text: vinyl uses
    "A1"/"B2", box sets use "3-04", and some releases leave it blank. The
    numeric position the caller derived from ordering is the only form a
    `tracknumber` tag can hold.
    """
    out: dict[str, str] = {}
    title = str(track.get('title') or '').strip()
    if title:
        out['title'] = title
    out['tracknumber'] = str(position)
    out['totaltracks'] = str(total)

    # A compilation credits each track to its own artist; an album usually
    # credits none, in which case the release artist already applies and
    # writing a track artist would be wrong.
    artists = track.get('artists')
    if artists:
        credit = format_artist_credit(artists)
        if credit:
            out['artist'] = credit
            out['artistsort'] = credit

    duration = str(track.get('duration') or '').strip()
    if duration:
        out['~discogs_track_length'] = duration
    return out


def _track_number_of(metadata) -> int | None:
    """The existing tracknumber on a file, as an int, or None.

    Tolerates "4/12" and zero-padded forms, both of which appear in files
    tagged by other software.
    """
    try:
        raw = metadata.get('tracknumber')
    except Exception:
        return None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ''
    text = str(raw or '').strip()
    if '/' in text:
        text = text.split('/', 1)[0].strip()
    return int(text) if text.isdigit() else None


def align_tracks_to_files(tracks: list, existing: list) -> list:
    """Line Discogs' tracks up against the files, best-effort.

    `existing` is the list of per-file metadata objects, in whatever order
    the caller holds them. Returns a list the same length as `existing`,
    each entry the matching Discogs track dict or None.

    Position is trusted over order whenever the files carry a complete,
    non-duplicated set of track numbers — that survives files being listed
    alphabetically, which is exactly when "01, 10, 11, 2" would otherwise
    misalign every track after the first. Failing that it falls back to the
    order given, which is correct for a freshly-loaded album.
    """
    audio = [t for t in (tracks or []) if isinstance(t, dict)]
    if not audio or not existing:
        return [None] * len(existing or [])

    numbers = [_track_number_of(m) for m in existing]
    usable = [n for n in numbers if n is not None and 1 <= n <= len(audio)]
    if len(usable) == len(existing) and len(set(usable)) == len(usable):
        return [audio[n - 1] for n in numbers]

    # Fall back to positional pairing, which only lines up as far as the
    # shorter of the two lists.
    return [audio[i] if i < len(audio) else None for i in range(len(existing))]


# Characters that look like a hyphen but are different codepoints.
# MusicBrainz and Discogs disagree on which to use in catalogue numbers and
# titles, so "UICY-77821" (U+002D) and "UICY‐77821" (U+2010) are the same
# value typed two ways. NFKC does not fold these, so map them explicitly.
_DASHES = {
    '\u2010': '-',  # HYPHEN
    '\u2011': '-',  # NON-BREAKING HYPHEN
    '\u2012': '-',  # FIGURE DASH
    '\u2013': '-',  # EN DASH
    '\u2014': '-',  # EM DASH
    '\u2015': '-',  # HORIZONTAL BAR
    '\u2212': '-',  # MINUS SIGN
    '\uff0d': '-',  # FULLWIDTH HYPHEN-MINUS
}
_SPACES = {
    '\u00a0': ' ',  # NO-BREAK SPACE
    '\u2007': ' ',  # FIGURE SPACE
    '\u202f': ' ',  # NARROW NO-BREAK SPACE
    '\u3000': ' ',  # IDEOGRAPHIC SPACE
}
_CONFUSABLES = str.maketrans({**_DASHES, **_SPACES})


def fold_for_compare(value: str) -> str:
    """Normalise a tag value for equality comparison only.

    Used to decide whether two values are the same, never to decide what
    gets written — the original text is always what is stored. Folds
    Unicode dash and space variants, applies NFKC, collapses runs of
    whitespace, and casefolds.
    """
    text = unicodedata.normalize('NFKC', str(value or ''))
    text = text.translate(_CONFUSABLES)
    return ' '.join(text.split()).casefold()


def _display(value: str | list[str] | None) -> str:
    """Render a tag value (possibly multi-valued) for display in a diff row."""
    if not value:
        return '(empty)'
    if isinstance(value, list):
        return '; '.join(value) if value else '(empty)'
    return value


# Fields that can only ever hold ONE meaningful value. In "combine" mode
# these must NOT become multi-value tags: a file that already carried a
# bogus date (rippers sometimes write the folder's creation date) would
# otherwise end up with e.g. "2020-06-19; 2023-09-01" instead of the real
# release date. For these fields combine behaves like override — the
# Discogs value wins outright and the old one is dropped.
SINGLE_VALUE_FIELDS = frozenset({
    'album', 'artist', 'albumartist', 'albumartistsort', 'artistsort',
    'date', 'originaldate', 'originalyear', 'year',
    'barcode', 'asin', 'releasecountry', 'media', 'script',
    'discnumber', 'totaldiscs', 'tracknumber', 'totaltracks',
    'discsubtitle', 'compilation', 'releasestatus', 'releasetype',
    # A track has one title. Combining two sources would produce
    # "Discogs Title; MusicBrainz Title" in the one tag every player uses
    # to name the track.
    'title',
})

# Tags that describe the individual track rather than the release, and so
# have a different value for every selected file. They travel per-pair
# rather than in the shared field dict, but they are still subject to the
# merge mode and the preview exactly like any other field.
TRACK_LEVEL_FIELDS = frozenset({
    'title', 'tracknumber', 'totaltracks', 'artist', 'artistsort',
})

# MODE_MB_ONLY means "don't write Discogs data at all" — the plugin stays
# installed and its search action still works for looking things up, but
# nothing from Discogs is applied to tags.
MODE_MB_ONLY = 'mb_only'
MODE_DISCOGS_ONLY = 'discogs_only'
MODE_FILL_GAPS = 'fill_gaps'
MODE_OVERRIDE = 'override'
MODE_COMBINE = 'combine'
MERGE_MODES = (MODE_MB_ONLY, MODE_FILL_GAPS, MODE_OVERRIDE, MODE_COMBINE,
               MODE_DISCOGS_ONLY)


def resolve_field_value(
    existing: str | list[str] | None,
    new_value: str | list[str],
    mode: str,
    field: str | None = None,
) -> list[str] | None:
    """Decide what (if anything) should be written to a BACKFILL_ONLY_FIELDS
    tag, given the current MusicBrainz value, the Discogs value, and the
    configured merge mode. Returns None for "leave it alone", otherwise the
    full list of values to write (Metadata accepts a list for multi-value
    tags directly).

    - fill_gaps: only write if `existing` is empty.
    - override: Discogs' value replaces `existing` entirely.
    - combine: both are kept as a multi-value tag, Discogs' value(s) first
      (most tag readers/players treat the first value in a multi-value
      field as primary) followed by MB's original value(s). Values that
      are the same once trimmed and case-folded (e.g. "Metallica" and
      "metallica ", or a value already added on a previous combine pass)
      count as duplicates and only the first-seen copy is kept.
    """
    if mode == MODE_MB_ONLY:
        return None

    # A single-valued field must never be combined into a multi-value tag.
    if mode == MODE_DISCOGS_ONLY:
        mode = MODE_OVERRIDE

    if mode == MODE_COMBINE and field in SINGLE_VALUE_FIELDS:
        mode = MODE_OVERRIDE

    existing_list = existing if isinstance(existing, list) else ([existing] if existing else [])
    existing_list = [v for v in existing_list if v]
    new_list = new_value if isinstance(new_value, list) else [new_value]

    if not existing_list:
        return new_list  # nothing to preserve either way — just fill it

    if mode == MODE_FILL_GAPS:
        return None
    if mode == MODE_OVERRIDE:
        return new_list
    if mode == MODE_COMBINE:
        combined: list[str] = []
        seen: set[str] = set()
        for v in [*new_list, *existing_list]:
            key = fold_for_compare(v)
            if key and key not in seen:
                combined.append(v)
                seen.add(key)
        existing_normalized = [fold_for_compare(v) for v in existing_list]
        combined_normalized = [fold_for_compare(v) for v in combined]
        return combined if combined_normalized != existing_normalized else None
    return None


# A MusicBrainz release can carry an explicit link to the exact Discogs
# release it corresponds to, as a URL relation. Matching that is far more
# accurate than searching by artist/title, which for a heavily-repressed
# album returns an arbitrary pressing.
_MB_DISCOGS_RELEASE_URL_RE = re.compile(r'discogs\.com/(?:[a-zA-Z-]+/)?release/(\d+)', re.IGNORECASE)


def discogs_release_id_from_mb(release_node: dict[str, Any] | None) -> str | None:
    """Return the Discogs RELEASE id that MusicBrainz links this release to.

    MusicBrainz stores it as a url-type relation (`target-type` == 'url',
    `type` == 'discogs') whose resource points at discogs.com/release/NNN.
    Requires Picard to have requested url-rels, which it does by default.

    Master links (discogs.com/master/NNN) are deliberately ignored: a
    master is the work, not a pressing, and has none of the per-pressing
    data this plugin applies. Returns None when there is no release link.
    """
    if not release_node:
        return None
    for relation in release_node.get('relations') or []:
        if relation.get('target-type') != 'url':
            continue
        resource = ((relation.get('url') or {}).get('resource') or '')
        match = _MB_DISCOGS_RELEASE_URL_RE.search(resource)
        if match:
            return match.group(1)
    return None


# Media whose formats[].qty counts something other than discs — a file count
# for digital releases, a quantity of cartridges/reels for tape. Treated as a
# single logical disc so disc mapping never invents extra discs.
_NON_DISC_MEDIA = frozenset({'file', 'memory stick', 'umd'})


def release_disc_count(release: dict[str, Any]) -> int:
    """How many discs this Discogs release actually has.

    Uses the parsed tracklist first, falling back to the format quantity
    (`formats[].qty`, e.g. "5" on a 5-CD box) when the tracklist isn't
    segmented. Returns 1 when there's no evidence of more than one disc.
    """
    discs = parse_discs(release)
    if len(discs) > 1:
        return len(discs)
    total = 0
    for fmt in release.get('formats') or []:
        if not isinstance(fmt, dict):
            total += 1
            continue
        # qty on a digital release counts FILES, not discs: a 6-track MP3
        # album carries qty="6" and is one "disc". Counting it would split a
        # single digital album into six phantom discs during disc mapping.
        if str(fmt.get('name') or '').strip().casefold() in _NON_DISC_MEDIA:
            total += 1
            continue
        try:
            total += int(str(fmt.get('qty') or '1').strip())
        except (TypeError, ValueError):
            total += 1
    return max(total, 1)


def is_probable_boxset(
    existing_album: str | list[str] | None,
    discogs_album_title: str | None,
    disc_count: int = 1,
) -> bool:
    """True only when this really looks like a multi-disc compilation whose
    release-level facts must not be applied to an individual disc.

    BOTH conditions are required:

      1. The Discogs release actually spans more than one disc
         (`disc_count > 1`, from release_disc_count()).
      2. The existing album title genuinely differs from the Discogs
         release title.

    Requiring only condition 2 — as an earlier version did — was badly
    wrong: an ordinary single album whose title merely differs in
    punctuation or carries a "(Deluxe)"/"[2020]" suffix was treated as a
    box set, which force-protected `date` and `releasecountry` and so
    PRESERVED whatever junk date the file already had (rippers sometimes
    write the folder's creation date) instead of replacing it with the
    real release date.
    """
    if disc_count <= 1:
        return False
    existing_str = (_display(existing_album) if existing_album else '').strip()
    if existing_str == '(empty)':
        existing_str = ''
    discogs_str = (discogs_album_title or '').strip()
    if not existing_str or not discogs_str:
        return False
    return fold_for_compare(existing_str) != fold_for_compare(discogs_str)


# Formerly this forced `date`/`releasecountry` to fill-gaps-only whenever a
# box set was detected, on the theory that the box's own values shouldn't
# overwrite each disc's. That was wrong, and it produced the opposite of
# what people expect: tagging a 2017 five-album box left the files dated
# 1965 and filed under 1965.
#
# MusicBrainz/Picard convention is that `date` is the date of THIS release
# — the physical box or pressing you actually own — while `originaldate`/
# `originalyear` carry the original release date of the work. Both are
# populated, so nothing is lost and each means what it is supposed to mean.
# The right defence against a wrong date is matching the right pressing,
# which the MusicBrainz link and candidate scoring now handle.
PROTECTED_IN_BOXSET: tuple = ()


def resolve_album_and_subtitle(
    existing_album: str | list[str] | None,
    discogs_album_title: str | None,
    existing_discsubtitle: str | list[str] | None,
    mode: str,
) -> dict[str, str]:
    """Special-cased handling for 'album', since joining two different
    album titles into one string (what the generic combine logic would
    do) is meaningless \u2014 e.g. a Discogs box set titled "Original Album
    Series" containing several individually-titled MusicBrainz releases
    ("Joy of a Toy", "Shooting at the Moon", ...).

    Instead: if the two titles genuinely differ, Discogs' title becomes
    'album' (in override/combine modes), and MusicBrainz's original title
    is relocated into 'discsubtitle' \u2014 the tag that exists precisely for
    "which disc/edition within this release" \u2014 rather than being lost or
    mashed into the album name. Only relocates into discsubtitle if that
    field is currently empty, so real subtitle data already present is
    never overwritten.

    Returns a dict with whichever of {'album', 'discsubtitle'} should be
    written; a field absent from the dict should not be touched.
    """
    existing_album_str = (_display(existing_album) if existing_album else '').strip()
    if existing_album_str == '(empty)':
        existing_album_str = ''
    discogs_album_title = (discogs_album_title or '').strip()
    existing_discsubtitle_str = (_display(existing_discsubtitle) if existing_discsubtitle else '').strip()
    if existing_discsubtitle_str == '(empty)':
        existing_discsubtitle_str = ''

    result: dict[str, str] = {}

    if mode == MODE_MB_ONLY:
        return result

    if not existing_album_str:
        if discogs_album_title:
            result['album'] = discogs_album_title
        return result

    if fold_for_compare(existing_album_str) == fold_for_compare(discogs_album_title):
        return result  # already the same — nothing to change

    if mode == MODE_DISCOGS_ONLY:
        mode = MODE_OVERRIDE
    if mode in (MODE_FILL_GAPS, MODE_MB_ONLY):
        return result  # MB already has an album title; leave it alone

    # override or combine: both replace 'album' cleanly and relocate MB's
    # original title into discsubtitle, rather than concatenating strings.
    result['album'] = discogs_album_title
    if not existing_discsubtitle_str:
        result['discsubtitle'] = existing_album_str
    return result


def compute_field_diff(metadata, discogs_fields: dict[str, str | list[str]], mode: str = MODE_OVERRIDE, disc_count: int = 1) -> list[dict]:
    """Return one row per field that WOULD actually change if applied,
    given the configured merge `mode` (fill_gaps / override / combine —
    see resolve_field_value). Fields BACKFILL_ONLY_FIELDS-gated where
    nothing would actually change are omitted, since there's nothing
    useful to preview for them.

    Each row: {'field': str, 'current': str, 'new': str, 'supplementary': bool}
    — 'current'/'new' are already display-formatted strings, not raw values.
    """
    if mode == MODE_MB_ONLY:
        return []  # nothing from Discogs is written in this mode
    if mode == MODE_DISCOGS_ONLY:
        mode = MODE_OVERRIDE

    boxset = is_probable_boxset(metadata.getall('album'), discogs_fields.get('album'), disc_count)

    rows: list[dict] = []
    for field, value in discogs_fields.items():
        if is_supplementary_field(field):
            current_display = _display(metadata.getall(field))
            new_display = _display(value)
            if current_display != new_display:
                rows.append({
                    'field': field,
                    'current': current_display,
                    'new': new_display,
                    'supplementary': True,
                })
            continue

        if field == 'album':
            resolved = resolve_album_and_subtitle(
                metadata.getall('album'), value, metadata.getall('discsubtitle'), mode,
            )
            if 'album' in resolved:
                rows.append({
                    'field': 'album',
                    'current': _display(metadata.getall('album')),
                    'new': resolved['album'],
                    'supplementary': False,
                })
            if 'discsubtitle' in resolved:
                rows.append({
                    'field': 'discsubtitle',
                    'current': _display(metadata.getall('discsubtitle')),
                    'new': resolved['discsubtitle'],
                    'supplementary': False,
                })
            continue

        if field not in BACKFILL_ONLY_FIELDS:
            continue
        existing = metadata.getall(field)
        effective_mode = MODE_FILL_GAPS if (boxset and field in PROTECTED_IN_BOXSET) else mode
        resolved = resolve_field_value(existing, value, effective_mode, field=field)
        if resolved is None:
            continue  # this mode leaves the field alone
        current_display = _display(existing)
        new_display = _display(resolved)
        if current_display == new_display:
            # Override mode always "resolves" to the Discogs value, even when
            # that value is what the field already holds. Listing those as
            # changes made the preview claim dozens of edits that were no-ops.
            continue
        rows.append({
            'field': field,
            'current': current_display,
            'new': new_display,
            'supplementary': False,
        })

    return rows


# ---------------------------------------------------------------------------
# Candidate scoring, used when MusicBrainz has no Discogs link for a release.
# ---------------------------------------------------------------------------

# Points per matching attribute. A barcode match alone is decisive: barcodes
# identify a specific commercial product, so two releases sharing one are the
# same pressing. Everything else is corroborating evidence.
_SCORE_BARCODE = 60
_SCORE_CATNO = 35
_SCORE_YEAR = 15
_SCORE_COUNTRY = 10
_SCORE_FORMAT = 10

# At or above this, the best candidate is treated as the right pressing and
# used without asking. Below it, the caller should let the user choose rather
# than silently tagging from an arbitrary pressing.
CONFIDENT_SCORE = 50


def _norm_code(value: str) -> str:
    """Normalise a barcode/catalogue number for comparison: strip spaces,
    dashes and dots, casefold. Discogs and MusicBrainz punctuate these
    inconsistently ("PB 41447" vs "PB-41447").
    """
    return re.sub(r'[\s\-.]', '', fold_for_compare(value))


def explain_release_match(candidate: dict[str, Any],
                          mb: dict[str, str]) -> dict[str, Any]:
    """Score one Discogs search result and say what the score is made of.

    `mb` is a plain dict of the MB values to compare: barcode,
    catalognumber, date, releasecountry, media. Missing values on either
    side simply score nothing rather than counting against the candidate.

    Returns the score plus `matched` and `missed` lists naming the facts
    that agreed and disagreed, and `comparable` — how many facts there were
    to compare at all. A bare number cannot be acted on ("why is 110 good?");
    "barcode and catalogue number agree" can.
    """
    score = 0
    matched: list[str] = []
    missed: list[str] = []
    available = 0

    def judge(label: str, agrees: bool, points: int) -> None:
        nonlocal score, available
        available += points
        if agrees:
            score += points
            matched.append(label)
        else:
            missed.append(label)

    mb_barcode = _norm_code(mb.get('barcode'))
    if mb_barcode:
        raw = candidate.get('barcode') or []
        candidate_barcodes = {_norm_code(b) for b in (raw if isinstance(raw, list) else [raw])}
        judge('barcode', mb_barcode in candidate_barcodes, _SCORE_BARCODE)

    mb_catno = _norm_code(mb.get('catalognumber'))
    if mb_catno:
        judge('catalogue number',
              _norm_code(candidate.get('catno')) == mb_catno, _SCORE_CATNO)

    mb_year = str(mb.get('date') or '')[:4]
    if mb_year.isdigit():
        judge('year', str(candidate.get('year') or '')[:4] == mb_year, _SCORE_YEAR)

    mb_country = str(mb.get('releasecountry') or '').strip().casefold()
    if mb_country:
        judge('country',
              str(candidate.get('country') or '').strip().casefold() == mb_country,
              _SCORE_COUNTRY)

    mb_media = str(mb.get('media') or '').strip().casefold()
    if mb_media:
        # Read both API shapes: `format` is the flat list, `formats[].name`
        # the structured one. Relying on `format` alone would silently stop
        # scoring medium if Discogs ever drops the flattened field.
        tokens = [str(f) for f in _as_list(candidate.get('format'))]
        for entry in _as_list(candidate.get('formats')):
            if isinstance(entry, dict):
                tokens.append(str(entry.get('name') or ''))
                tokens.extend(str(d) for d in _as_list(entry.get('descriptions')))
        joined = ' '.join(tokens).casefold()
        # MusicBrainz says "12\" Vinyl"/"CD"; Discogs says ["Vinyl","LP"]/["CD"].
        token = 'vinyl' if 'vinyl' in mb_media else ('cd' if 'cd' in mb_media else mb_media)
        judge('medium', bool(token) and token in joined, _SCORE_FORMAT)

    return {
        'score': score,
        'matched': matched,
        'missed': missed,
        'available': available,
        'comparable': len(matched) + len(missed),
    }


def score_release_candidate(candidate: dict[str, Any], mb: dict[str, str]) -> int:
    """Score one Discogs search result against the MusicBrainz release facts."""
    return explain_release_match(candidate, mb)['score']


# Confidence bands for display, as a share of the points actually available.
# Expressed as a proportion rather than an absolute, because the points on
# offer depend on how much MusicBrainz knew: 15 out of 15 when only the year
# is comparable is weak evidence, while 60 out of 60 on a barcode is not.
def describe_match(detail: dict[str, Any]) -> tuple[str, str]:
    """Turn an explain_release_match() result into (label, explanation).

    The label goes in the picker's Match column; the explanation is its
    tooltip and detail-pane line.
    """
    if not detail or not detail.get('comparable'):
        return '', ('Nothing to compare against — Picard has no barcode, '
                    'catalogue number, year, country or medium for these files.')

    matched = detail.get('matched') or []
    missed = detail.get('missed') or []
    score = detail.get('score') or 0
    available = detail.get('available') or 0
    share = (score / available) if available else 0.0

    if 'barcode' in matched:
        label = 'Exact'          # a shared barcode is the same product
    elif share >= 0.75:
        label = 'Strong'
    elif share >= 0.4:
        label = 'Likely'
    elif score > 0:
        label = 'Weak'
    else:
        label = 'No match'

    parts = []
    if matched:
        parts.append('agrees on ' + _human_list(matched))
    if missed:
        parts.append('differs on ' + _human_list(missed))
    return label, (label + ': ' + '; '.join(parts) if parts else label)


def _human_list(items) -> str:
    items = [str(i) for i in items if str(i)]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + ' and ' + items[-1]


def rank_release_candidates_detailed(
    results: list[dict], mb: dict[str, str]
) -> list[tuple[dict, dict]]:
    """Return (match_detail, candidate) pairs sorted best-first, preserving
    Discogs' own relevance order as the tie-breaker.

    The detail is explain_release_match()'s dict, so a caller can show WHY a
    candidate ranked where it did rather than only that it did.
    """
    scored = [(explain_release_match(c, mb), i, c) for i, c in enumerate(results or [])]
    scored.sort(key=lambda t: (-t[0]['score'], t[1]))
    return [(detail, c) for detail, _i, c in scored]


def rank_release_candidates(results: list[dict], mb: dict[str, str]) -> list[tuple[int, dict]]:
    """Return (score, candidate) pairs sorted best-first, preserving Discogs'
    own relevance order as the tie-breaker.
    """
    return [(detail['score'], candidate)
            for detail, candidate in rank_release_candidates_detailed(results, mb)]


# ---------------------------------------------------------------------------
# User-configurable output filtering
# ---------------------------------------------------------------------------

# Volatile marketplace/community numbers. These are a snapshot at tagging
# time — have/want counts and prices change daily — so they are opt-in.
MARKETPLACE_FIELDS = frozenset({
    'discogs_community_have',
    'discogs_community_want',
    'discogs_community_rating',
    'discogs_community_rating_count',
    'discogs_num_for_sale',
    'discogs_lowest_price',
    'discogs_estimated_weight',
    'discogs_data_quality',
})

STYLE_SEPARATE = 'separate'   # styles stay in discogs_style only
STYLE_GENRE = 'genre'         # styles replace genre
STYLE_BOTH = 'both'           # styles go to both
STYLE_MODES = (STYLE_SEPARATE, STYLE_GENRE, STYLE_BOTH)

# Auto-match strictness. NORMAL lets a barcode match alone decide, which is
# usually right. STRICT demands corroboration beyond a barcode, for people
# who have been burned by a wrong pressing. LENIENT accepts weaker evidence.
STRICTNESS_STRICT = 'strict'
STRICTNESS_NORMAL = 'normal'
STRICTNESS_LENIENT = 'lenient'
STRICTNESS_THRESHOLDS = {
    STRICTNESS_STRICT: 80,
    STRICTNESS_NORMAL: CONFIDENT_SCORE,   # 50
    STRICTNESS_LENIENT: 25,
}


def threshold_for_strictness(strictness: str) -> int:
    return STRICTNESS_THRESHOLDS.get(strictness, CONFIDENT_SCORE)


def apply_style_mode(fields: dict, mode: str) -> dict:
    """Route Discogs `styles` according to the user's preference.

    Discogs' genres are coarse ("Rock") while its styles are the useful
    detail ("Prog Rock", "Symphonic"), but styles land in discogs_style
    where most players ignore them. Mutates and returns `fields`.
    """
    styles = fields.get('discogs_style')
    if not styles:
        return fields
    style_list = styles if isinstance(styles, list) else [styles]
    if mode == STYLE_GENRE:
        fields['genre'] = list(style_list)
        fields.pop('discogs_style', None)
    elif mode == STYLE_BOTH:
        existing = fields.get('genre') or []
        existing = existing if isinstance(existing, list) else [existing]
        merged = list(existing)
        for style in style_list:
            if style not in merged:
                merged.append(style)
        fields['genre'] = merged
    return fields


def filter_fields(
    fields: dict,
    *,
    marketplace: bool = False,
    performers: bool = True,
    companies: bool = True,
    identifiers: bool = True,
    protected: set | None = None,
) -> dict:
    """Drop field groups the user has switched off, and any tag they have
    protected from Discogs entirely. Returns a new dict.

    `protected` is compared case-insensitively so "Date" and "date" both
    work, since people will type these by hand.
    """
    protected_norm = {p.strip().lower() for p in (protected or set()) if p.strip()}
    out = {}
    for name, value in fields.items():
        if name.lower() in protected_norm:
            continue
        if not marketplace and name in MARKETPLACE_FIELDS:
            continue
        if not performers and name.startswith('performer:'):
            continue
        if not companies and name.startswith('discogs_company_'):
            continue
        if not identifiers and name.startswith('discogs_identifier_'):
            continue
        out[name] = value
    return out


def parse_protected_tags(text: str) -> set:
    """Parse the user's protected-tags text field. Accepts commas, spaces
    or newlines as separators so any reasonable typing works.
    """
    return {part.strip() for part in re.split(r'[,\s]+', text or '') if part.strip()}


# ---------------------------------------------------------------------------
# "Discogs only" — clear tags Discogs did not supply
# ---------------------------------------------------------------------------

# "Discogs only" means exactly that: every tag on the file is removed unless
# Discogs supplied it. The track title, the track and disc numbers, the
# artist, the date — all of it goes, and is rewritten from Discogs.
#
# This is only coherent because the plugin writes track identity from the
# Discogs tracklist itself (see extract_track_fields / align_tracks_to_files);
# without that, wiping `title` would leave files with no title at all.
# What survives follows one rule: Discogs replaces everything a release
# database could possibly know, and what is kept is either a measured
# property of the audio, the user's own data, or an identifier that makes
# the file findable again. None of it is something Discogs could supply, so
# nothing here competes with Discogs data — it would simply be destroyed,
# and in most cases could not be recovered without reprocessing the file.
ESSENTIAL_KEEP_EXACT = frozenset({
    # The user's own listening data. Nothing can reconstruct this.
    'rating',
    # Written by the encoder, describes this file rather than the release.
    'encodedby',
    'encodersettings',
    # An audio fingerprint, not release metadata.
    'musicip_fingerprint',
})

ESSENTIAL_KEEP_PREFIXES = (
    # Picard's runtime variables (~filename, ~format, ~bitrate). Every
    # format writer skips these, so they are never written to a file and
    # there is nothing there to wipe; dropping them from the in-memory
    # metadata would only break Picard's own file handling.
    '~',
    # The MusicBrainz identifiers, kept so the file stays re-matchable: with
    # the release MBID intact Picard finds the release again in one lookup
    # instead of re-identifying the file from scratch. Every MusicBrainz URL
    # derives from these — the release page is
    # musicbrainz.org/release/<musicbrainz_albumid> — and Picard has no
    # separate URL tag to keep: of its 163 registered tags the only
    # URL-valued ones are `website` (the artist's homepage) and `podcasturl`.
    'musicbrainz_',
    # Computed by scanning the audio. Wiping these means re-fingerprinting
    # the library to use "Scan" again.
    'acoustid_',
    # Loudness analysis — hours of CPU across a library, and Discogs has no
    # equivalent to replace it with.
    'replaygain_',
)

# Deliberately NOT kept, though a case could be made: `bpm` and `key`. Both
# can come either from a release database or from a DJ tool's own analysis,
# and there is no way to tell which at tagging time. They are wiped on the
# assumption that "Discogs only" means what it says; a user who analyses
# their library can protect them via the plugin's "protected tags" setting
# rather than having the decision made for them here.


def _matches_pattern(name: str, pattern: str) -> bool:
    """Case-insensitive match supporting a trailing '*' wildcard, so a user
    can protect a whole family such as 'replaygain_*' in one entry.
    """
    name = name.lower()
    pattern = pattern.strip().lower()
    if pattern.endswith('*'):
        return name.startswith(pattern[:-1])
    return name == pattern


def tags_to_clear(existing_names, supplied_names, protected=None) -> list:
    """Which tags to delete in Discogs-only mode.

    Everything currently on the file is removed EXCEPT: what Discogs just
    supplied, the essential keep-list above, and anything the user
    protected. Returned sorted so logs read predictably.
    """
    supplied = {n.lower() for n in supplied_names}
    patterns = [p for p in (protected or set()) if p.strip()]
    out = []
    for name in existing_names:
        lower = name.lower()
        if lower in supplied:
            continue
        if lower in ESSENTIAL_KEEP_EXACT:
            continue
        if any(lower.startswith(p) for p in ESSENTIAL_KEEP_PREFIXES):
            continue
        if any(_matches_pattern(name, p) for p in patterns):
            continue
        out.append(name)
    return sorted(out)


# ---------------------------------------------------------------------------
# Search-result presentation
# ---------------------------------------------------------------------------
# Everything below turns a raw Discogs object into display-ready strings for
# the release picker. It is pure: no Qt, no network, no Picard imports, so it
# can be tested directly against captured API responses.
#
# Two API shapes are handled deliberately. The /database/search response has
# changed over the years and Discogs does not version it:
#
#   older shape:  "label": "Svek",          "format": ["Vinyl", "12\""]
#   newer shape:  "label": ["Svek", "..."], "format": ["Vinyl", "LP", "Album"],
#                 "formats": [{"name": "Vinyl", "qty": "2", "text": "180g",
#                              "descriptions": ["LP", "Album", "Reissue"]}],
#                 "format_quantity": 2, "master_id": 5427,
#                 "community": {"have": 20, "want": 10}
#
# Reading `formats` when present and falling back to the flat `format` list
# means neither shape produces a blank column, and a future addition cannot
# raise. Nothing here assumes a key exists.

# Physical carriers, as Discogs names them in formats[].name. Everything
# else that turns up in a format list (LP, Album, Reissue, Deluxe Edition...)
# is an edition descriptor rather than a medium.
_MEDIA_NAMES = frozenset({
    'vinyl', 'cd', 'cdr', 'cassette', 'file', 'dvd', 'dvd-audio', 'dvd-video',
    'sacd', 'shellac', 'blu-ray', 'blu-ray audio', 'hybrid', 'box set',
    '8-track cartridge', 'reel-to-reel', 'minidisc', 'dat', 'vhs',
    'laserdisc', 'flexi-disc', 'acetate', 'lathe cut', 'memory stick',
    'microcassette', 'cd-rom', 'hdcd', 'all media', 'betamax', 'umd',
})

# Edition descriptors worth surfacing on their own, because they are what
# distinguishes two otherwise-identical pressings. Matched as substrings so
# "Deluxe Edition" and "Limited Edition, Numbered" both register.
_EDITION_MARKERS = (
    'remaster', 'reissue', 'repress', 'deluxe', 'limited', 'numbered',
    'anniversary', 'special edition', 'expanded', 'promo', 'test pressing',
    'unofficial', 'mispress', 'club edition', 'compilation', 'mono', 'stereo',
    'quadraphonic', 'remix', 'partially mixed', 'mixed', 'sampler',
)

# Lossless digital markers in a File release's `text`/`descriptions`, so a
# digital release's actual quality shows rather than a bare "File".
_LOSSLESS_MARKERS = ('flac', 'wav', 'aiff', 'alac', 'dsd', 'dsf', 'shn', 'ape')


def _as_list(value) -> list:
    """Coerce a field that Discogs returns as either a scalar or a list."""
    if value is None or value == '':
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, '')]
    return [value]


def _join(values, separator: str = ', ', limit: int | None = None) -> str:
    """Join after deduping, optionally truncating with a +N marker."""
    items = [str(v).strip() for v in _as_list(values) if str(v).strip()]
    items = dedupe(items)
    if limit is not None and len(items) > limit:
        hidden = len(items) - limit
        return separator.join(items[:limit]) + f' (+{hidden})'
    return separator.join(items)


def split_discogs_title(title: str) -> tuple[str, str]:
    """Split Discogs' combined "Artist - Title" search title.

    Discogs joins the two with a spaced hyphen. Only the FIRST such
    separator splits, because album titles contain hyphens far more often
    than artist names do ("Godspeed You! Black Emperor - Lift Your
    Skinny Fists... - EP"). A title with no separator is all title, with
    no artist — guessing would be worse than leaving the column blank.
    """
    text = str(title or '').strip()
    if ' - ' in text:
        artist, _sep, rest = text.partition(' - ')
        artist = clean_artist_name(artist)
        if artist and rest.strip():
            return artist, rest.strip()
    return '', text


def format_detail(result: dict[str, Any]) -> dict[str, Any]:
    """Break a search result's format information into named parts.

    Returns media (the carrier, e.g. "Vinyl" or "CD"), quality (a lossless
    codec or bitrate for digital releases), descriptors (every descriptor
    Discogs lists), editions (just the ones that distinguish pressings),
    discs (how many physical pieces) and text (the free-text note, e.g.
    "180g, Gatefold").
    """
    formats = [f for f in _as_list(result.get('formats')) if isinstance(f, dict)]

    media: list[str] = []
    descriptors: list[str] = []
    texts: list[str] = []
    quantity = 0
    pieces: list[str] = []

    if formats:
        for entry in formats:
            name = str(entry.get('name') or '').strip()
            if name:
                media.append(name)
            for desc in _as_list(entry.get('descriptions')):
                descriptors.append(str(desc).strip())
            text = str(entry.get('text') or '').strip()
            if text:
                texts.append(text)
            # qty is a string in the API; a missing or junk value counts as
            # one piece rather than zero, so a column never reads "0 discs".
            raw_qty = str(entry.get('qty') or '').strip()
            count = int(raw_qty) if raw_qty.isdigit() and int(raw_qty) > 0 else 1
            # On a digital release qty is a FILE count, not a disc count — a
            # 6-track MP3 album carries qty="6" and is one logical disc.
            if name.casefold() in _NON_DISC_MEDIA:
                quantity += 1
                pieces.append(f'{count} files' if count > 1 else name or 'file')
            else:
                quantity += count
                pieces.append(f'{count} × {name}' if count > 1 else name)
    else:
        # Older shape: one flat list mixing carrier and descriptors.
        for token in _as_list(result.get('format')):
            token = str(token).strip()
            if not token:
                continue
            if token.casefold() in _MEDIA_NAMES:
                media.append(token)
            else:
                descriptors.append(token)

    if not quantity:
        raw = str(result.get('format_quantity') or '').strip()
        quantity = int(raw) if raw.isdigit() and int(raw) > 0 else (len(media) or 1)

    haystack = ' '.join(descriptors + texts).casefold()
    quality = [token.upper() for token in _LOSSLESS_MARKERS if token in haystack]
    # A bitrate note ("320 kbps") is the lossy counterpart worth showing.
    for text in texts:
        lowered = text.casefold()
        if 'kbps' in lowered or 'khz' in lowered or 'bit' in lowered:
            quality.append(text)

    editions = [d for d in descriptors
                if any(marker in d.casefold() for marker in _EDITION_MARKERS)]

    return {
        'media': dedupe(media),
        'quality': dedupe(quality),
        'descriptors': dedupe(descriptors),
        'editions': dedupe(editions),
        'discs': quantity,
        'pieces': ' + '.join(pieces),
        'text': dedupe(texts),
    }


def summarize_search_result(result: dict[str, Any], score: int | None = None) -> dict[str, Any]:
    """Flatten one Discogs search result into display-ready strings.

    Every value is a string ready to put in a cell, except `discs`,
    `score`, `have`, `want` and `release_id`, which stay numeric so the
    table can sort them properly, and `is_master`, which is a bool.

    Only what /database/search actually returns is read here. Track counts
    and company credits are NOT in a search response; they need a
    per-release fetch and arrive via summarize_release_detail().
    """
    result = result if isinstance(result, dict) else {}
    fmt = format_detail(result)
    artist, title = split_discogs_title(result.get('title'))

    community = result.get('community')
    community = community if isinstance(community, dict) else {}

    def _count(key):
        raw = community.get(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    # The medium, with digital quality folded in so a FLAC release is
    # distinguishable from an MP3 one at a glance.
    media = _join(fmt['media'], ' + ')
    if fmt['quality']:
        media = f"{media} ({_join(fmt['quality'], ', ', limit=2)})" if media else _join(fmt['quality'])

    year = str(result.get('year') or '').strip()

    return {
        'release_id': result.get('id'),
        'is_master': str(result.get('type') or '').strip().casefold() == 'master',
        'master_id': result.get('master_id') or '',
        'uri': result.get('uri') or '',
        'thumb': result.get('thumb') or '',
        'cover_image': result.get('cover_image') or '',
        'resource_url': result.get('resource_url') or '',

        'title': title,
        'artist': artist,
        'year': year[:4] if year[:4].isdigit() else year,
        'country': str(result.get('country') or '').strip(),
        'media': media,
        'format': _join(fmt['descriptors'], ', ', limit=4),
        'edition': _join(fmt['editions'], ', ', limit=3),
        'notes': _join(fmt['text'], ', '),
        'discs': fmt['discs'],
        'pieces': fmt['pieces'],
        'label': _join(result.get('label'), ', ', limit=2),
        'catno': _join(result.get('catno'), ', '),
        'barcode': _join(result.get('barcode'), ', ', limit=2),
        'genre': _join(result.get('genre'), ', ', limit=3),
        'style': _join(result.get('style'), ', ', limit=3),
        'have': _count('have'),
        'want': _count('want'),
        'score': score,

        # Filled in later by summarize_release_detail() once the full
        # release has been fetched; blank until then so the table can show
        # a placeholder rather than a wrong zero.
        'tracks': None,
        'duration': '',
        'companies': '',
        'released': '',
        'quality_rating': '',
    }


def _parse_duration(text: str) -> int:
    """Convert a Discogs "m:ss" or "h:mm:ss" duration to whole seconds."""
    parts = [p for p in str(text or '').strip().split(':') if p != '']
    if not parts or not all(p.isdigit() for p in parts):
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return ''
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def count_audio_tracks(tracklist) -> int:
    """Count real audio tracks in a Discogs tracklist.

    A tracklist mixes three entry types: "track" (real audio), "heading"
    (a section label such as a side or an act, with no audio) and "index"
    (a grouping whose audio lives in nested sub_tracks). Counting entries
    blindly inflates the number on exactly the releases where an accurate
    count matters most — box sets and classical works. Entries with no
    explicit type are counted as tracks, which is how older API responses
    present an ordinary tracklist.
    """
    total = 0
    for entry in _as_list(tracklist):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get('type_') or entry.get('type') or 'track').strip().casefold()
        if kind == 'heading':
            continue
        if kind == 'index':
            total += count_audio_tracks(entry.get('sub_tracks'))
            continue
        total += 1
    return total


def total_tracklist_duration(tracklist) -> int:
    """Total playing time in seconds, counting nested sub-tracks once."""
    total = 0
    for entry in _as_list(tracklist):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get('type_') or entry.get('type') or 'track').strip().casefold()
        if kind == 'heading':
            continue
        if kind == 'index':
            nested = total_tracklist_duration(entry.get('sub_tracks'))
            # An index track may carry its own duration instead of listing
            # sub-track times; prefer the sub-tracks when they have them.
            total += nested or _parse_duration(entry.get('duration'))
            continue
        total += _parse_duration(entry.get('duration'))
    return total


def summarize_release_detail(release: dict[str, Any]) -> dict[str, Any]:
    """Pull the extras that only a full /releases/{id} response carries.

    Returns the same keys summarize_search_result() left blank, so a row
    dict can simply be updated with this. The publisher/company line uses
    Discogs' entity_type_name ("Manufactured By", "Distributed By",
    "Recorded At") because "Sony Music · Manufactured By" says something
    the bare company name does not.
    """
    release = release if isinstance(release, dict) else {}
    tracklist = release.get('tracklist')

    companies = []
    for entry in _as_list(release.get('companies')):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('name') or '').strip()
        if not name:
            continue
        role = str(entry.get('entity_type_name') or '').strip()
        companies.append(f'{name} — {role}' if role else name)

    discs = release_disc_count(release)

    return {
        'tracks': count_audio_tracks(tracklist),
        'duration': _format_duration(total_tracklist_duration(tracklist)),
        'companies': _join(companies, '; ', limit=4),
        'released': str(release.get('released_formatted')
                        or release.get('released') or '').strip(),
        'quality_rating': str(release.get('data_quality') or '').strip(),
        'discs': discs if discs else None,
    }


def stale_discogs_tags(existing_names, protected=None) -> list:
    """Discogs-written tags to remove in "MusicBrainz only" mode.

    Selecting MusicBrainz only stops the plugin writing anything new, but
    that on its own leaves whatever a previous run wrote sitting on the
    file — so switching to MusicBrainz only appeared to do nothing to
    already-tagged files, and the Discogs values stayed visible in the
    player. Everything the plugin writes is identifiable by name (the
    supplementary fields and the discogs_/performer: prefixes), so the
    tags it added can be withdrawn without touching anything else.

    Fields the plugin shares with MusicBrainz — artist, genre, date and the
    rest — are deliberately NOT removed: once written they are
    indistinguishable from a MusicBrainz value, and the next MusicBrainz
    lookup overwrites them anyway.
    """
    patterns = [p for p in (protected or set()) if p.strip()]
    out = []
    for name in existing_names:
        if not is_supplementary_field(name.lower()):
            continue
        if any(_matches_pattern(name, p) for p in patterns):
            continue
        out.append(name)
    return sorted(out)
