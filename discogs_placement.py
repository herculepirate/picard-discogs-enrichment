"""Which track of a MusicBrainz release each of your files is, by its disc and track number.

A hybrid SACD box is listed on MusicBrainz with each disc's layers as media of their own
(Jacqueline du Pre - The Great Cello Concertos: 4 discs, 8 media - "Hybrid SACD (CD layer)",
"Hybrid SACD (SACD layer, 2 channels)", ...). Your "Disc 2" is then media 3 and 4, not medium 2,
which is disc 1's SACD layer: Picard's own matching, reading disc 2 as medium 2 and finding only
"Track 1".."Track 7" for titles, cannot tell. TagMender's MED-22 (tm_layers.layer_groups) makes
physical discs of the media; your audio says which layer it is: finer than a CD (more than 16
bits or 44.1 kHz) the SACD layer of its channel count, else the CD layer.

Pure Python: no Qt, no Picard.
"""
from __future__ import annotations

import os
import re

try:
    from . import tm_layers as tl
except ImportError:  # run outside Picard (tests)
    import tm_layers as tl

_DISC_DIR = re.compile(r'(?:^|[\s_-])(?:cd|disc|disk|dvd|sacd)\s*[-_.#]?\s*(\d{1,3})\b', re.I)
_TRACK_NAME = re.compile(r'^\s*(?:\d{1,2}[-.]\s*)?(\d{1,3})(?=\D)')


def disc_of(facts: dict) -> int:
    """The file's disc: its tag, else its folder's name ("Disc 2", "CD02"); 0 unknown."""
    if facts.get('disc'):
        return int(facts['disc'])
    m = _DISC_DIR.search(os.path.basename(os.path.dirname(str(facts.get('path') or ''))))
    return int(m.group(1)) if m else 0


def track_of(facts: dict) -> int:
    """The file's track number: its tag, else its name's leading number ("07 ...", "7. Track 7")."""
    if facts.get('track'):
        return int(facts['track'])
    m = _TRACK_NAME.match(os.path.basename(str(facts.get('path') or '')))
    return int(m.group(1)) if m else 0


def wanted_layer(facts: dict) -> str:
    """The layer your audio is: "stereo" / "multichannel" (finer than a CD), else "cd"."""
    hd = (facts.get('bits') or 0) > 16 or (facts.get('rate') or 0) > 44100
    if not hd:
        return 'cd'
    return 'multichannel' if (facts.get('channels') or 0) > 2 else 'stereo'


def plan_placement(tracks, files) -> dict | None:
    """TRACKS: [(disc number, medium format, track index)] of the release's tracks in order;
    FILES: [{"path", "disc", "track", "bits", "rate", "channels"}]. Returns {"tracks": [track
    index per file], "summary"}, or None when any file cannot be placed for sure: its disc or
    track unknown, no such disc or track, two files on one track, or a disc of yours holding
    another number of files than the medium it goes to has tracks."""
    media, at = [], {}
    for disc, fmt, index in tracks:
        if disc not in at:
            at[disc] = len(media)
            media.append({'position': disc, 'format': fmt, 'track-count': 0, '~idx': []})
        m = media[at[disc]]
        m['track-count'] += 1
        m['~idx'].append(index)
    if not media or not files:
        return None
    groups = tl.layer_groups({'media': media})
    ones = len({disc_of(f) for f in files}) == 1
    out, used, per_medium, said = [], set(), {}, {}
    for f in files:
        disc = disc_of(f) or (1 if ones and len(groups) == 1 else 0)
        num = track_of(f)
        if not disc or not num or disc > len(groups):
            return None
        g = groups[disc - 1]
        medium = tl._pick_layer(g, wanted_layer(f)) if len(g) > 1 else g[0]
        if num > len(medium['~idx']):
            return None
        index = medium['~idx'][num - 1]
        if index in used:
            return None
        used.add(index)
        out.append(index)
        per_medium[id(medium)] = per_medium.get(id(medium), 0) + 1
        said[disc] = (medium['position'], medium['format'] or 'medium')
    for g in groups:
        for m in g:
            if id(m) in per_medium and per_medium[id(m)] != m['track-count']:
                return None
    summary = ', '.join(f'disc {d} -> medium {pos} ({fmt})' for d, (pos, fmt) in sorted(said.items()))
    return {'tracks': out, 'summary': summary}
