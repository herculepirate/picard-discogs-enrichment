# Discogs Enrichment

A [MusicBrainz Picard](https://picard.musicbrainz.org/) 3 plugin that adds
Discogs as a metadata source alongside MusicBrainz — for the releases
MusicBrainz covers thinly: specific pressings, small labels, reissues, box
sets.

**Requires Picard 3.0 or later.** It will not load in Picard 2.x; the plugin
system changed completely in 3.0.

---

## What it does

**Picks the right pressing, rather than the first hit.** Searching Discogs
opens a sortable table: artist, title, year, country, medium, disc and track
counts, edition markers (Reissue, Remastered, Deluxe, Promo, Mono), label,
catalogue number and community have/want counts. A **Match** column scores
each candidate against what Picard already knows — barcode, catalogue
number, year, country and medium — and hovering it says which facts agreed:
*"Exact: agrees on barcode, catalogue number, year, country and medium."*

**Searches by anything Discogs supports** — barcode, catalogue number,
artist, title, year, label, country, format, track title, credited person,
genre, style, or free text — or by pasting a release ID or URL. A search
that finds nothing widens itself by dropping the most over-specific terms
and tells you which ones it dropped, instead of reporting "no matches" and
leaving you to guess.

**Four tag sources**, each exact about what it overwrites. See
[Tag sources](#tag-sources).

**Box sets.** Maps the discs Picard has onto the discs Discogs lists, with a
confidence score per pairing and manual override.

**Cover art** — front, front and back, or every image.

**A troubleshooting log**, switchable between basic and detailed,
size-capped, and saved from the settings page.

---

## Installing

Picard 3 installs plugins with its own plugin manager, which keeps each one as
a **git checkout** so its **Update** button can bring it up to date. Install it
through Picard (or `picard-cli`, or `install.sh`, which uses `picard-cli`) and
it stays updatable.

> **Never copy or edit files inside Picard's `plugins3` folder by hand.** Picard
> refuses to update a checkout with local edits ("Plugin … has uncommitted
> changes"). If that has already happened, run `./install.sh --repair`.

> Picard 2.x installed plugins as `.zip` files. Picard 3 does not read those
> at all — if you have a `plugins` folder full of zips, it is the old one and
> is now ignored.

### Option 1 — from inside Picard (easiest, works everywhere)

**Options → Plugins → Install Plugin…**, then paste
`https://github.com/herculepirate/picard-discogs-enrichment`.

### Option 2 — the install script (Linux: apt/.deb or Flatpak)

```bash
git clone https://github.com/herculepirate/picard-discogs-enrichment.git
cd picard-discogs-enrichment
./install.sh
```

It finds your Picard (including the Flatpak) and installs through Picard's own
`picard-cli`, so Picard manages the result.

```bash
./install.sh --status      # installed commit, commits behind, any hand edits
./install.sh --update      # update to the latest commit
./install.sh --repair      # back up hand edits in the installed copy, discard them, update
./install.sh --local       # install from this folder instead of GitHub (development)
./install.sh --uninstall
./install.sh --flavour flatpak   # choose when both a .deb and a Flatpak Picard exist
```

`--repair` copies any edited files to `~/discogs-enrichment-local-edits-<date>/`
(with a `changes.diff`) before discarding them, so nothing is lost. An install
made by an older version of this script (a plain copy) is replaced with a
Picard-managed one automatically.

### Option 3 — `picard-cli` directly

```bash
picard-cli plugins install https://github.com/herculepirate/picard-discogs-enrichment
picard-cli plugins update "Discogs Enrichment"
# Flatpak:
flatpak run --command=picard-cli org.musicbrainz.Picard plugins install https://github.com/herculepirate/picard-discogs-enrichment
```

### After installing

1. Restart Picard.
2. **Options → Plugins** and enable **Discogs Enrichment**.
3. **Options → Plugins → Discogs Enrichment**, paste your Discogs token.

Generate a token at
[discogs.com/settings/developers](https://www.discogs.com/settings/developers)
— "Generate token" is enough; this plugin does not need a full OAuth app.
Discogs only returns cover images to authenticated requests, so art will not
download without one.

---

## Tag sources

Set under **Options → Plugins → Discogs Enrichment**. The four modes differ
in exactly what gets overwritten:

| Mode | Behaviour |
| --- | --- |
| **Discogs only** | Every tag is removed and rewritten from Discogs — including title, track and disc numbers, artist and date. See [what survives](#what-discogs-only-keeps). |
| **MusicBrainz only** | No Discogs data is written, and any Discogs tags a previous run left behind are removed. |
| **Discogs preferred** | Every field Discogs supplies replaces the MusicBrainz value. MusicBrainz keeps the fields Discogs has no opinion on. |
| **MusicBrainz preferred** | MusicBrainz wins wherever it has a value; Discogs fills only what MusicBrainz left blank. |
| **Keep both** | Both values are kept as a multi-value tag, Discogs first. |

Single-valued fields — album, artist, albumartist, date, barcode, country,
media, disc numbers and the sort fields — never become multi-valued, in any
mode. Two different album titles joined into one string is not useful data.

### Artist names and MusicBrainz artist IDs

When Discogs replaces `artist` or `albumartist` with its own credit (for
example `Steve Roach / Kevin Braheny / Michael Stearns`), the MusicBrainz
artist IDs that came with the old credit would describe different people than
the new name. Players that group artists by MusicBrainz ID (Lyrion, Navidrome)
then show the new name against the wrong artist — one joined credit can end up
as the name of a single artist's page.

So whenever the plugin actually changes an artist name it also removes the
tags that belonged to the old one: `musicbrainz_artistid` /
`musicbrainz_albumartistid`, and the `artists` / `albumartists` lists unless
Discogs supplied them. A name that did not change keeps all of its tags.
Release and recording IDs are never touched, so the file stays re-matchable
in Picard. Use **MusicBrainz preferred** if you want MusicBrainz's artist
names and IDs left exactly as they are.

### What "Discogs only" keeps

Everything a release database could know is replaced. What survives is only
what Discogs cannot supply and the file could not recover:

- **`musicbrainz_*`** (except the artist IDs, see above) — so the file stays re-matchable. With the release MBID
  intact Picard finds the release again in one lookup. Every MusicBrainz URL
  derives from these: the release page is
  `musicbrainz.org/release/<musicbrainz_albumid>`. (Picard has no separate
  URL tag — of its 163 registered tags the only URL-valued ones are
  `website`, the artist's homepage, and `podcasturl`.)
- **`acoustid_*`, `musicip_fingerprint`** — computed by scanning the audio.
  Wiping them means re-fingerprinting the library.
- **`replaygain_*`** — loudness analysis, hours of CPU across a library.
- **`rating`** — your own listening data; nothing can reconstruct it.
- **`encodedby`, `encodersettings`** — describe the file, not the release.
- **`~`-prefixed names** — Picard's runtime variables. These are never
  written to files in the first place.

`bpm` and `key` are deliberately **not** kept: either can come from a release
database or from a DJ tool's own analysis, and there is no way to tell which
at tagging time. If you analyse your library, protect them with the
**protected tags** setting, which takes a comma-separated list and supports a
trailing wildcard (`replaygain_*`).

Because this mode clears the MusicBrainz title, the plugin writes track
title, number and artist from the Discogs tracklist. Tracks are lined up
against your files by existing track number where available, falling back to
file order — so files listed alphabetically (`01, 10, 11, 2…`) still align
correctly.

---

## Troubleshooting

**"Failed to update the plugin … has uncommitted changes".** Files in the
installed copy were edited or replaced by hand. Run `./install.sh --repair`
(it backs the edits up first), or in Picard choose Update and accept
"Discard changes" if `picard-cli` asks. `./install.sh --status` shows what was
changed.

**The plugin does not appear in Options → Plugins.** Install it again through
Picard (Option 1) or `./install.sh`, then enable it and restart Picard.

**No Discogs data appears.** Check the token is set, and that the tag source
is not "MusicBrainz only".

**No cover art.** Discogs serves images only to authenticated requests, so a
token is required. Also check **Options → Cover Art**: newly registered
providers are added disabled, at the bottom of the list.

**Anything else.** Turn the log up to detailed in the plugin settings,
reproduce the problem, then save the log from the same page. The token is
redacted from it.

---

## Development

Install from your working copy once, then commit and update; never edit the
installed copy:

```bash
./install.sh --local      # Picard now tracks this folder's git history
# edit, then:
git commit -am "…"
./install.sh --update     # Picard fast-forwards to your commit; restart Picard
git push                  # when you are happy with it
```

Only committed work is installed. `./install.sh` (without `--local`) switches
back to the GitHub source; settings are kept either way.

Pure logic lives in `discogs_mapping.py` with no Qt or Picard imports, so it
can be tested directly. `discogs_runtime.py` holds the late-bound `api`
object: `enable(api)` binds it once and the rest of the plugin imports `log`
and `settings` from there, rather than threading the api through every call.

```
MANIFEST.toml            plugin metadata (uuid, name, api version)
__init__.py              enable()/disable(), actions, options page
discogs_runtime.py       late-bound api: settings + logging
discogs_mapping.py       pure logic — field mapping, merge modes, scoring
discogs_client.py        Discogs API over Picard's web service
discogs_*_dialog.py      release picker, search, disc mapping, preview
discogs_coverart.py      cover art provider
discogs_logging.py       size-capped troubleshooting log
```

## Licence

GPL-2.0-or-later. See `MANIFEST.toml`.
