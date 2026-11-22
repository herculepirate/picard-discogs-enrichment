"""What your copy of an album says about its pressing, read from its folder.

The reading itself is TagMender's (tm_readers.read_album: generated, never edited here): the
folder and file names, rip logs, cue sheets and CDTOC tags, notes, SACD and vinyl marks. This
module only

* finds the album's folder from the files Picard holds (album_location), refusing anything
  that could be a library root;
* turns TagMender's album reading into the facts this plugin searches and ranks with
  (copy_evidence), each with where it was read;
* compares your copy with the release MusicBrainz matched: its disc IDs (no network) and the
  barcodes of a Discogs release MusicBrainz links to.

Pure Python: no Qt and no Picard imports, so it can be tested directly.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

try:
    from . import tm_match_core as mc
    from . import tm_readers as tr
except ImportError:  # run outside Picard (tests)
    import tm_match_core as mc
    import tm_readers as tr

AUDIO_EXT = frozenset({'.flac', '.mp3', '.m4a', '.mp4', '.ogg', '.oga', '.opus', '.wv', '.ape',
                       '.wav', '.aiff', '.aif', '.dsf', '.dff', '.wma', '.mpc', '.tak'})

# The album folder may hold the album's files at most this far down ("CD1/", "Disc 2/Audio/").
MAX_DEPTH = 2
# ... and at most this many audio files in all, its own and others': a folder holding many more
# than the album has is an artist's or a library's folder, never read whole.
MAX_AUDIO = 400


class Location(dict):
    """album_dir, artist_folder, album_folder (the names read_album takes) and exclude: the
    audio files under album_dir that are not this album's."""


def _audio_under(top: Path, limit: int) -> list[Path] | None:
    """Every audio file under TOP, or None once more than LIMIT are found."""
    found = []
    for dirpath, _dirs, names in os.walk(top):
        for n in names:
            if os.path.splitext(n)[1].lower() in AUDIO_EXT:
                found.append(Path(dirpath) / n)
                if len(found) > limit:
                    return None
    return found


def album_location(paths: Iterable[str]) -> Location | None:
    """The folder your copy of the album is in, from its files' paths, or None.

    The common folder of the files; when that is a disc folder ("CD1") of a set whose other
    discs sit beside it, the set's folder, since a set's scans and logs are often kept in one
    disc's folder (TagMender's DSC rules read a set as one album). Refused (None) when the files
    lie deeper than MAX_DEPTH below it, or the folder holds over twice as many audio files as
    the album (and at most MAX_AUDIO): an artist's or library folder, whose reading would be
    slow and say nothing of this copy."""
    files = sorted({str(Path(p)) for p in paths if p})
    if not files:
        return None
    folders = {Path(f).parent for f in files}
    try:
        top = Path(os.path.commonpath([str(d) for d in folders]))
    except ValueError:                       # (different drives)
        return None
    if any(len(d.relative_to(top).parts) > MAX_DEPTH for d in folders):
        return None
    if tr.is_disc_dir(top) and top.parent != top:
        top = top.parent                     # (one disc of a set, loaded on its own)
    limit = min(MAX_AUDIO, max(2 * len(files), len(files) + 4))
    audio = _audio_under(top, limit)
    if audio is None:
        return None
    own = set(files)
    return Location(album_dir=str(top), artist_folder=top.parent.name, album_folder=top.name,
                    exclude=sorted(str(a) for a in audio if str(a) not in own))


def read_copy(loc: Location) -> dict[str, Any]:
    """TagMender's reading of the album folder (read_album), its other files left out."""
    return tr.read_album(Path(loc['album_dir']), loc['artist_folder'], loc['album_folder'],
                         exclude=loc.get('exclude') or ())


# media_hint values -> what MusicBrainz's media tag says for them (for ranking)
_MEDIA_NAMES = {'cd': 'CD', 'vinyl': 'Vinyl', 'sacd': 'SACD', 'cassette': 'Cassette',
                'dvd': 'DVD', 'bluray': 'Blu-ray', 'digital': 'Digital Media'}


# A disc ID names a disc only when its TOC has this many tracks (TagMender: file_clues).
MIN_DISCID_TRACKS = 3


def _toc_tracks(toc: str) -> int:
    parts = str(toc or '').split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def _barcode_form(code) -> str:
    """A barcode TagMender normalised (leading zeros dropped) in the form it is printed: the
    shortest of its 12-digit UPC / 13-digit EAN forms whose check digit holds, or ""."""
    if len(mc.barcode_key(code) or "") < 10:
        return ""                             # (a short number zero-padded is no barcode)
    forms = [v for v in mc.barcode_variants(code) if mc.valid_barcode(v)]
    return min(forms, key=len) if forms else ""


def copy_evidence(album: dict[str, Any] | None) -> dict[str, Any]:
    """The facts of your copy the plugin searches and ranks with, from TagMender's reading.

    barcodes   - your copy's own (tags no other tagger wrote, else decoded or read whole off
                 your scans: discogs_ocr.merge_scans); proof of the release
    scan_codes - barcodes and catalogue numbers in your scans and documents, searched
    note_barcodes - typed in the folder's notes: searched, never proof
    catnos     - as written, best first: the folder name's, then notes', rip logs', tags'
    year       - the pressing's (a remaster/reissue year in the folder name), else None
    media      - what the copy was made from ("CD", "Vinyl", "SACD"...), else ""
    discids    - {disc: MusicBrainz disc ID} of the discs whose TOC fits their files
    discid_from - {disc: where its TOC came from: a rip log or cue sheet, or the files' lengths}
    rip_log_year, physical (scans of the physical copy), packaging, sacd
    sources    - {fact: where it was read}, for the log and the Match explanation
    """
    if not album:
        return {}
    by_tagger = bool(album.get('tags_by_tagger'))
    tag_barcodes = sorted(album.get('tag_barcodes') or ())
    barcodes = [] if by_tagger else [f for f in dict.fromkeys(map(_barcode_form, tag_barcodes)) if f]
    from_scans = False
    if not barcodes:
        barcodes = [f for f in dict.fromkeys(map(_barcode_form, sorted(album.get('scan_barcodes') or ()))) if f]
        from_scans = bool(barcodes)
    note_barcodes = [f for f in dict.fromkeys(map(_barcode_form, album.get('note_barcodes') or ()))
                     if f and f not in barcodes]
    folder_own = set(album.get('catnos_folder_own') or ())
    typed = set(album.get('catnos_typed') or ())
    tag_catnos = set(album.get('tag_catnos') or ())
    catnos, sources = [], {}
    for c in album.get('catno_search') or ():
        n = mc.norm_code(c)
        if by_tagger and n in tag_catnos and n not in folder_own and n not in typed:
            continue                          # (another tagger's catalogue number: not the copy's)
        if c not in catnos:
            catnos.append(c)
    if catnos:
        first = mc.norm_code(catnos[0])
        sources['catalogue number'] = ('the folder name' if first in folder_own
                                       else "the folder's notes or rip log" if first in typed
                                       else "your files' tags")
    if barcodes:
        sources['barcode'] = 'your scans' if from_scans else "your files' tags"
    year = album.get('pressing_year') or album.get('edition_year') or None
    if year:
        sources['year'] = 'the folder name (pressing year)'
    media = _MEDIA_NAMES.get(str(album.get('media_hint') or ''), '')
    if media:
        sources['medium'] = album.get('media_hint_from') or 'the folder'
    # every disc whose TOC fits its files to the sector: read from a rip log or cue sheet
    # (tocs_read), or rebuilt from the files' own lengths when they are whole CD sectors (a
    # lossless rip of the CD: its disc ID all the same)
    discids, discid_from = {}, {}
    read = album.get('tocs_read') or {}
    for disc in album.get('tocs_fit') or ():
        toc = (album.get('tocs') or {}).get(disc)
        # (TagMender's rule, match_core.file_clues: a one- or two-track TOC is just "a CD this
        # long", which unrelated CDs share - and a single-file image read as one track is no
        # disc's TOC at all)
        if toc and _toc_tracks(toc) >= MIN_DISCID_TRACKS:
            discids[disc] = tr.mb_discid(toc)
            discid_from[disc] = ('the rip log or cue sheet' if read.get(disc) == toc
                                 else "your files' lengths (whole CD sectors)")
    if discids:
        sources['disc ID'] = '; '.join(sorted(set(discid_from.values())))
    return {
        'barcodes': barcodes, 'note_barcodes': note_barcodes, 'catnos': catnos,
        'year': year, 'media': media, 'discids': discids, 'discid_from': discid_from,
        'rip_log_year': album.get('rip_log_year'),
        'physical': bool((album.get('scans') or {}).get('physical')),
        'packaging': sorted(album.get('packaging') or ()),
        'sacd': album.get('sacd_hint') or '',
        'tags_by_tagger': by_tagger,
        'scan_codes': list(album.get('scan_codes') or ()),
        'sources': sources,
    }


def with_matched_names(album: dict | None, artist: str, title: str) -> bool:
    """Files with no artist tag (a fresh rip, a cue image): read_album named the album after its
    folders, which may be anything ("Drummer X / Band - Album"). Picard has matched it, so the
    matched release's artist and title are searched and scored instead - names, never evidence
    of the pressing. Returns whether they were used."""
    if not album or not artist or not title:
        return False
    if album.get('track_artists') or any(t.get('artist') for t in album.get('tracks') or ()):
        return False
    album['artist'], album['title'] = artist, title
    return True


def has_copy_evidence(ev: dict[str, Any] | None) -> bool:
    """Does your copy say anything about which pressing it is?"""
    return bool(ev and (ev.get('barcodes') or ev.get('catnos') or ev.get('discids')
                        or ev.get('note_barcodes') or ev.get('scan_codes')))


def match_facts(mb_facts: dict[str, str], ev: dict[str, Any] | None) -> dict[str, Any]:
    """The facts Discogs candidates are ranked against: your copy's where it has them, the
    MusicBrainz release's elsewhere. Your copy is the pressing you own; the MusicBrainz release
    is only the one Picard matched, which may be another pressing of the album."""
    facts = dict(mb_facts or {})
    if not ev:
        return facts
    if ev.get('barcodes'):
        facts['barcode'] = ev['barcodes'][0]
    if ev.get('catnos'):
        facts['catalognumber'] = '; '.join(ev['catnos'])
    if ev.get('year'):
        facts['date'] = str(ev['year'])
    if ev.get('media'):
        facts['media'] = ev['media']
    facts['_from'] = {k: v for k, v in (ev.get('sources') or {}).items()}
    return facts


def search_terms(mb_facts: dict[str, str], ev: dict[str, Any] | None) -> dict[str, str]:
    """(barcode, catalogue number) to search Discogs by: your copy's first, then the
    MusicBrainz release's."""
    ev = ev or {}
    barcode = (ev.get('barcodes') or ev.get('note_barcodes') or [None])[0] or mb_facts.get('barcode') or None
    catno = (ev.get('catnos') or [None])[0] or mb_facts.get('catalognumber') or None
    return {'barcode': barcode, 'catalog_number': catno}


def mb_release_discids(release_node: dict[str, Any] | None) -> set[str]:
    """The disc IDs MusicBrainz attaches to the release Picard matched (Picard asks for them)."""
    out = set()
    for medium in (release_node or {}).get('media') or ():
        for disc in medium.get('discs') or ():
            if disc.get('id'):
                out.add(disc['id'])
    return out


def discid_verdict(ev: dict[str, Any] | None, mb_discids: set[str]) -> str:
    """'agrees' when a disc ID of your copy is on the MusicBrainz release Picard matched,
    'unknown' when your copy has disc IDs but none is attached there (MusicBrainz lists few disc
    IDs, so this is no proof of another release), '' when your copy has none."""
    ids = set((ev or {}).get('discids', {}).values())
    if not ids:
        return ''
    return 'agrees' if ids & set(mb_discids or ()) else 'unknown'


def release_barcodes(release_data: dict[str, Any] | None) -> set[str]:
    """A Discogs release's barcodes, normalised."""
    out = set()
    for ident in (release_data or {}).get('identifiers') or ():
        if str(ident.get('type') or '').casefold() == 'barcode':
            code = mc.norm_code(ident.get('value'))
            if code:
                out.add(code)
    return out


def contradicts_release(ev: dict[str, Any] | None, release_data: dict[str, Any] | None) -> str:
    """Why your copy is not this Discogs release, or "": its own barcode is not among the
    release's barcodes (a release listing none is not contradicted)."""
    own = {mc.norm_code(b) for b in (ev or {}).get('barcodes') or ()}
    theirs = release_barcodes(release_data)
    if own and theirs and not (own & theirs) and not any(
            mc.norm_code(b).lstrip('0') == mc.norm_code(t).lstrip('0') for b in own for t in theirs):
        return (f'your copy\'s barcode {", ".join(sorted(own))} is not on Discogs release '
                f'{(release_data or {}).get("id")} ({", ".join(sorted(theirs))})')
    return ''


def evidence_summary(ev: dict[str, Any] | None) -> list[str]:
    """One line per fact, for the troubleshooting log."""
    if not ev:
        return ['no evidence read from the folder']
    src = ev.get('sources') or {}
    out = []
    if ev.get('barcodes'):
        out.append(f'barcode {", ".join(ev["barcodes"])} ({src.get("barcode")})')
    if ev.get('note_barcodes'):
        out.append(f'barcode typed in notes {", ".join(ev["note_barcodes"])} (searched, not proof)')
    if ev.get('catnos'):
        out.append(f'catalogue number {", ".join(ev["catnos"])} ({src.get("catalogue number")})')
    for disc, did in sorted((ev.get('discids') or {}).items()):
        out.append(f'disc {disc} ID {did} (from {(ev.get("discid_from") or {}).get(disc) or src.get("disc ID")})')
    if ev.get('year'):
        out.append(f'pressing year {ev["year"]}')
    if ev.get('media'):
        out.append(f'made from {ev["media"]} ({src.get("medium")})')
    if ev.get('rip_log_year'):
        out.append(f'ripped in {ev["rip_log_year"]} (rip log)')
    if ev.get('physical'):
        out.append('scans of the physical copy' + (f' ({", ".join(ev["packaging"])})' if ev.get('packaging') else ''))
    if ev.get('scan_codes'):
        out.append(f'codes in your scans or documents: {", ".join(ev["scan_codes"][:6])} (searched)')
    if ev.get('tags_by_tagger'):
        out.append("the files' release details came from a tagger, so they are not read as your copy's")
    return out or ['the folder says nothing about the pressing']
