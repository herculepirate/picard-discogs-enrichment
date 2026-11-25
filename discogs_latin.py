"""English names only: names written in another script (Japanese, Cyrillic, Greek, Chinese, ...)
are replaced by their English / Latin form when one is known.

What is replaced: names and titles - album, album artist(s), artist(s), track title, disc
subtitle, label, and the people credited (composer, lyricist, performer, ...).

What is never touched:
- Latin names with accents or other letters of their language (Björk, Sigur Rós, Motörhead,
  "L'Été indien"): they are in the Latin script already.
- Lyrics, notes and comments: the words themselves, not names.
- Language: the language sung in is a fact about the music.
- A name with no English form known: it stays as it is (and is logged), never emptied.

Where the English form comes from, first found wins:
1. the name itself, when it carries both ("金田一耕助の冒険 = The Adventures Of Kindaichi Kosuke");
2. what Discogs writes for the same tag (release title, artist credit, track title);
3. the sort name MusicBrainz gives the artist, turned back ("Mystery Kindaichi Band, The" ->
   "The Mystery Kindaichi Band"; "Sakamoto, Ryuichi" -> "Ryuichi Sakamoto").

Pure Python: no Qt, no Picard.
"""
from __future__ import annotations

import re
import unicodedata

NAME_TAGS = ('album', 'albumartist', 'artist', 'title', 'discsubtitle', 'label', 'albumartists',
             'artists', 'composer', 'lyricist', 'writer', 'conductor', 'arranger', 'producer',
             'engineer', 'mixer', 'remixer', 'djmixer', 'director', 'originalartist', 'work',
             'movement', 'grouping', '~artists', '~albumartists')
# each name tag's sort tag, from which the name's Latin form can be turned back
SORT_OF = {'albumartist': 'albumartistsort', 'artist': 'artistsort', 'composer': 'composersort'}


def is_name_tag(tag: str) -> bool:
    return tag in NAME_TAGS or tag.startswith('performer:')


def is_latin(text) -> bool:
    """Every letter in TEXT is a Latin one (accents and all); a text without letters is too."""
    for ch in str(text or ''):
        if ch.isalpha() and not unicodedata.name(ch, '').startswith('LATIN'):
            return False
    return True


def latin_side(text) -> str | None:
    """The Latin half of a name written both ways ("ミステリー = Mystery", "Mystery (ミステリー)")."""
    text = str(text or '')
    for part in re.split(r'\s+=\s+|\s+/\s+', text):
        part = part.strip()
        if part and part != text and is_latin(part) and any(c.isalpha() for c in part):
            return part
    m = re.match(r'^(.*?)\s*[(\[]([^()\[\]]+)[)\]]\s*$', text)
    if m:
        for part in (m.group(2), m.group(1)):
            part = part.strip()
            if part and is_latin(part) and any(c.isalpha() for c in part):
                return part
    return None


def unsort(sort) -> str | None:
    """A sort name turned back into a name, when it is Latin ("Mystery Kindaichi Band, The")."""
    sort = str(sort or '').strip()
    if not sort or not is_latin(sort) or not any(c.isalpha() for c in sort):
        return None
    names = []
    for one in re.split(r'\s+&\s+|;\s*', sort):
        head, sep, tail = one.partition(', ')
        names.append(f'{tail} {head}'.strip() if sep and ', ' not in tail else one.strip())
    return ' & '.join(n for n in names if n)


def _values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if str(value).strip() else []


def name_map(metadata, alternatives: dict) -> dict:
    """{name in another script: its Latin form}, from the sort names and the Discogs tags."""
    out = {}
    for tag, sort_tag in SORT_OF.items():
        names, sorts = _values(_getall(metadata, tag)), _values(_getall(metadata, sort_tag))
        if len(names) == 1 and len(sorts) == 1 and not is_latin(names[0]):
            back = unsort(sorts[0])
            if back:
                out.setdefault(names[0], back)
    for tag, value in (alternatives or {}).items():
        theirs = _values(value)
        ours = _values(_getall(metadata, tag))
        if len(theirs) == len(ours):
            for mine, alt in zip(ours, theirs):
                if not is_latin(mine) and is_latin(alt):
                    out[mine] = alt          # (Discogs' name before the turned-back sort name)
    return out


def _getall(metadata, tag) -> list:
    try:
        return list(metadata.getall(tag))
    except AttributeError:
        return _values(metadata.get(tag))


def english_names(metadata, alternatives: dict | None = None) -> tuple[dict, list]:
    """Replace, in METADATA (a Picard Metadata or a dict), every name in another script by its
    Latin form. ALTERNATIVES: what Discogs writes for the same tags. Returns ({tag: (was,
    now)}, [names left as they are: no English form known])."""
    alternatives = alternatives or {}
    names = name_map(metadata, alternatives)
    changed, left = {}, []
    for tag in [t for t in list(metadata.keys()) if is_name_tag(t)]:
        was = _values(_getall(metadata, tag))
        if not was or all(is_latin(v) for v in was):
            continue
        alt = _values(alternatives.get(tag))
        now = []
        for i, value in enumerate(was):
            if is_latin(value):
                now.append(value)
                continue
            new = latin_side(value) or names.get(value)
            if not new and len(alt) == len(was) and is_latin(alt[i]):
                new = alt[i]
            if new:
                now.append(new)
            else:
                now.append(value)
                left.append(f'{tag}: {value}')
        if now != was:
            metadata[tag] = now if len(now) > 1 else now[0]
            changed[tag] = (was, now)
    if changed and str(_first(metadata, 'script')) not in ('', 'Latn') and all(
            is_latin(v) for t in ('album', 'title') for v in _values(_getall(metadata, t))):
        changed['script'] = ([_first(metadata, 'script')], ['Latn'])
        metadata['script'] = 'Latn'
    return changed, left


def _first(metadata, tag) -> str:
    values = _values(_getall(metadata, tag))
    return values[0] if values else ''
