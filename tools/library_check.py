#!/usr/bin/env python3
"""What the plugin reads of each album in a music folder, and what it would decide.

    python3 tools/library_check.py ~/Music                 # the folders' evidence only
    python3 tools/library_check.py ~/Music --ocr           # and the scans (OCR helper)
    python3 tools/library_check.py ~/Music --decide        # and the Discogs verdict (network)

Run outside Picard, on your system's Python, with the plugin's own code (discogs_evidence,
discogs_ocr, discogs_decide). Nothing in the folders is changed and nothing is tagged.

--decide asks Discogs and MusicBrainz as the plugin does, one request a second per site, and
keeps every answer in --cache, so a second run makes no requests. The Discogs token is read
from $DISCOGS_TOKEN, else from Picard's settings for this plugin; it is never printed.
There is no matched MusicBrainz release here (Picard has not matched one), so "linked" and
"musicbrainz" never appear: what is counted is what your copy proves on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discogs_decide as dd  # noqa: E402
import discogs_evidence as de  # noqa: E402
import discogs_mapping as dm  # noqa: E402
import discogs_ocr as do  # noqa: E402

UA = 'PicardDiscogsEnrichment-library-check +https://github.com/herculepirate/picard-discogs-enrichment'
PICARD_INI = (Path.home() / '.var/app/org.musicbrainz.Picard/config/MusicBrainz/Picard.ini',
              Path.home() / '.config/MusicBrainz/Picard.ini')


def _plugin_uuid() -> str:
    m = re.search(r'uuid\s*=\s*"([0-9a-f-]+)"', (ROOT / 'MANIFEST.toml').read_text())
    return m.group(1) if m else ''


def discogs_token() -> str:
    if os.environ.get('DISCOGS_TOKEN'):
        return os.environ['DISCOGS_TOKEN']
    uuid = _plugin_uuid()[:8]
    for ini in PICARD_INI:
        if not ini.exists() or not uuid:
            continue
        section = False
        for line in ini.read_text(errors='ignore').splitlines():
            if line.startswith('['):
                section = line.startswith(f'[plugin.{uuid}')
            elif section and line.startswith('token='):
                return line.split('=', 1)[1].strip()
    return ''


class Http:
    """get_json / get_text for discogs_decide: real requests, one a second per site, every
    answer cached on disk."""

    def __init__(self, cache: Path, token: str) -> None:
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.token = token
        self.last: dict[str, float] = {}
        self.sent: Counter = Counter()

    def _fetch(self, url: str, host: str):
        f = self.cache / (hashlib.sha1(url.encode()).hexdigest() + '.json')
        if f.exists():
            return json.loads(f.read_text())['body']
        wait = 1.05 - (time.time() - self.last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        headers = {'User-Agent': UA}
        if host == 'discogs' and self.token:
            headers['Authorization'] = f'Discogs token={self.token}'
        body = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    body = r.read().decode('utf-8', 'replace')
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(5)
                    continue
                body = '' if e.code == 404 else None
                break
            except Exception:
                break
        self.last[host] = time.time()
        self.sent[host] += 1
        if body is not None:
            f.write_text(json.dumps({'url': url, 'body': body}))
        return body

    def get_json(self, url, host):
        body = self._fetch(url, host)
        try:
            return json.loads(body) if body else None
        except ValueError:
            return None

    def get_text(self, url, host):
        return self._fetch(url, host) or ''


def albums(root: Path):
    """Each album folder's audio files, as Picard would load them (one folder, or a set of
    disc folders read together)."""
    seen = set()
    for folder, _dirs, names in sorted(os.walk(root)):
        paths = [os.path.join(folder, n) for n in sorted(names)
                 if os.path.splitext(n)[1].lower() in de.AUDIO_EXT]
        if not paths:
            continue
        loc = de.album_location(paths)
        if loc is None or loc['album_dir'] in seen:
            continue
        seen.add(loc['album_dir'])
        yield loc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('root', help='a music folder')
    ap.add_argument('--ocr', action='store_true', help="read the scans (the OCR helper)")
    ap.add_argument('--decide', action='store_true', help='ask Discogs what the plugin would decide')
    ap.add_argument('--limit', type=int, default=0, help='albums at most (0: all)')
    ap.add_argument('--only-evidence', action='store_true',
                    help='decide only albums whose copy says something of its pressing')
    ap.add_argument('--cache', default=str(Path.home() / '.cache/discogs-enrichment/library-check'))
    args = ap.parse_args(argv)

    ocr = probe = None
    if args.ocr:
        from discogs_ocr_bridge import OcrBridge, partial_message, unavailable_message
        ocr = OcrBridge()
        probe = ocr.probe()
        if not probe.get('ok'):
            print(unavailable_message(probe), file=sys.stderr)
            ocr = None
        elif partial_message(probe):
            print(partial_message(probe), file=sys.stderr)
    http = None
    if args.decide:
        token = discogs_token()
        if not token:
            print('no Discogs token ($DISCOGS_TOKEN or the plugin settings): searching unauthenticated',
                  file=sys.stderr)
        http = Http(Path(args.cache).expanduser(), token)

    counts, kinds = Counter(), Counter()
    started = time.time()
    for n, loc in enumerate(albums(Path(args.root).expanduser()), 1):
        if args.limit and n > args.limit:
            break
        t = time.time()
        try:
            album = de.read_copy(loc)
            scans = ocr.read(loc['album_dir'], have_barcode=bool(album.get('barcodes'))) if ocr else None
            merged = do.merge_scans(album, loc['album_dir'], scans)
            ev = de.copy_evidence(album)
        except Exception as exc:
            counts['error'] += 1
            print(f'[error  ] {loc["album_folder"]}: {type(exc).__name__}: {exc}')
            continue
        has = de.has_copy_evidence(ev)
        counts['evidence' if has else 'no evidence'] += 1
        for k in ('barcodes', 'catnos', 'discids', 'note_barcodes', 'scan_codes'):
            if ev.get(k):
                kinds[k] += 1
        line = f'{loc["artist_folder"]} / {loc["album_folder"]}'
        status = ''
        if http and (has or not args.only_evidence):
            r = dd.decide(album, http, dm, ocr_text=merged.get('text') or '', scan_probe=merged.get('probe'))
            status = r['status']
            counts[status] += 1
            c = (r.get('best') or {}).get('cand') or {}
            pick = f' -> {c.get("id")} {c.get("year")} {c.get("country")}' if c else ''
            print(f'[{status:7}] {line}{pick}')
            if status == 'auto':
                print(f'          {dd.explain(r["best"])[:200]}')
            elif r.get('why'):
                print(f'          why: {"; ".join(r["why"][:2])[:200]}')
        else:
            print(f'[{"copy" if has else "-":7}] {line}')
        if has:
            print('          ' + '; '.join(de.evidence_summary(ev)))
        scanned = do.scans_summary(scans, merged)
        if scanned:
            print(f'          scans: {scanned}')
        sys.stdout.flush()
        counts['seconds'] += round(time.time() - t)

    total = counts['evidence'] + counts['no evidence'] + counts['error']
    print(f'\n{total} album(s) in {time.time() - started:.0f} s: {counts["evidence"]} with evidence of the '
          f'pressing, {counts["no evidence"]} without, {counts["error"]} unreadable')
    print('evidence: ' + ', '.join(f'{k} {v}' for k, v in sorted(kinds.items())))
    if http:
        print('verdicts: ' + ', '.join(f'{k} {counts[k]}' for k in ('auto', 'review', 'nomatch') if counts[k])
              + f'; requests sent: {dict(http.sent)}')
    return 1 if counts['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
