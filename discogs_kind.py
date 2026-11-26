"""What your copy was made from - a record, a tape, a CD or a shop's download - and whether a
release of another format can be it.

Picard matches the files to a release by their track lengths; the release's format says nothing
of your copy. The Holy Flesh - Emissary and Vessel: MusicBrainz lists only the 150-copy cassette,
Picard matched it, and the album was marked "Tape Rip" and given the Discogs cassette - but the
files are a Deezer download (UPC, ISRC and EXPLICIT in every file's tags, no rip log). Tool -
Lateralus: Picard matched the 2003 CD, and the Discogs CD MusicBrainz links to it was written
over a picture-disc vinyl rip (a turntable in "rip log.txt", SOURCE=VINYL, side photos).

What says so, first found wins:
- vinyl: TagMender's VIN-10 (copy_vinyl: the folder name, the ripper's notes, sides, a source
  tag naming the record);
- cd: a CD rip log with the disc's table of contents beside the files;
- cassette: the folder name ("[Tape Rip]", "(Cassette)");
- digital: the folder name ("[WEB]", a shop named), or the tags a shop's downloader writes in
  every file: UPC and ISRC with the shop's explicit-content flag (EXPLICIT, ITUNESADVISORY) -
  tags no ripper and no tagger of a CD or record writes;
- cd, by the folder name ("[2CD]").

Pure Python: no Qt, no Picard (mutagen to read the tags).
"""
from __future__ import annotations

import os

try:
    from . import tm_match_core as mc
    from . import tm_readers as tr
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_readers as tr

NAMES = {'vinyl': 'a vinyl rip', 'cassette': 'a tape rip', 'cd': 'a CD rip', 'digital': 'a download'}
STORE_FILES = 4          # files read for the shop's tags
_FLAG_KEYS = ('explicit', 'itunesadvisory', 'rtng', 'contentadvisory')


def _keys(path) -> set:
    """The file's tag names, lower case, without the container's prefix ("TXXX:UPC" -> "upc")."""
    try:
        import mutagen
        f = mutagen.File(str(path))
    except Exception:
        return set()
    if f is None or not getattr(f, 'tags', None):
        return set()
    out = set()
    try:
        names = list(f.tags.keys())
    except Exception:
        return set()
    for k in names:
        k = str(k).casefold()
        for prefix in ('txxx:', '----:com.apple.itunes:', 'wm/'):
            if k.startswith(prefix):
                k = k[len(prefix):]
        out.add('isrc' if k == 'tsrc' else k)
    return out


def store_download(paths) -> str:
    """Why these files are a shop's download by their tags, or "": every file read carries UPC,
    ISRC and the explicit-content flag (deemix, streamrip, OrpheusDL and the shops' own apps)."""
    files = [p for p in paths or () if os.path.splitext(str(p))[1]][:STORE_FILES]
    if not files:
        return ''
    for p in files:
        keys = _keys(p)
        if not ('upc' in keys and 'isrc' in keys and any(k in keys for k in _FLAG_KEYS)):
            return ''
    return "a shop's tags in every file (UPC, ISRC, explicit flag)"


def _cd_log(folder) -> bool:
    try:
        return any(log.get('toc') for log in tr.rip_logs(folder))
    except Exception:
        return False


def copy_kind(paths, folder) -> tuple[str, str]:
    """("vinyl" | "cassette" | "cd" | "digital" | "", why) for the files PATHS in FOLDER."""
    paths = [str(p) for p in paths or ()]
    folder = str(folder or '')
    name = os.path.basename(folder.rstrip('/'))
    if not folder or not paths:
        return '', ''
    try:
        if tr.copy_vinyl(folder, paths) == 'vinyl':
            return 'vinyl', ('the folder name' if mc.folder_says_vinyl(name) else
                             'your rip notes and files')
    except Exception:
        pass
    if _cd_log(folder):
        return 'cd', 'a CD rip log with the disc\'s table of contents'
    if mc._CASSETTE_HINT.search(name):
        return 'cassette', 'the folder name'
    hint = mc.media_hint(name)
    if hint == 'digital':
        return 'digital', 'the folder name'
    shop = store_download(paths)
    if shop:
        return 'digital', shop
    if hint == 'cd':
        return 'cd', 'the folder name'
    return '', ''


def release_kinds(release_data) -> set:
    """The formats of a Discogs release ("formats": [{"name": "Vinyl"}, ...]) as copy kinds."""
    return {mc.media_kind(f.get('name')) for f in (release_data or {}).get('formats') or ()
            if isinstance(f, dict)} - {''}


def kind_against(kind: str, why: str, release_data) -> str:
    """Why a copy of KIND cannot be this Discogs release, or "" (nothing known, or it fits)."""
    theirs = release_kinds(release_data)
    if not kind or not theirs or kind in theirs:
        return ''
    names = ', '.join(sorted(f.get('name') for f in release_data.get('formats') or ()
                             if isinstance(f, dict) and f.get('name')))
    return (f'your copy is {NAMES.get(kind, kind)} ({why}), Discogs release '
            f'{release_data.get("id")} is {names}')

