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

Picard 3 finds plugins by scanning its `plugins3` folder for directories
containing a `MANIFEST.toml`. Installing is therefore a directory copy, and
the only thing that differs between a `.deb`, a Flatpak and a Snap is where
that folder lives.

> Picard 2.x installed plugins as `.zip` files. Picard 3 does not read those
> at all — if you have a `plugins` folder full of zips, it is the old one and
> is now ignored.

### Option 1 — from inside Picard (easiest, works everywhere)

**Options → Plugins → Install**, then either:

- paste `https://github.com/herculepirate/picard-discogs-enrichment` into the
  URL field, or
- download/clone this repository and point the **Local path** field at the
  folder.

Picard validates that the folder has a `MANIFEST.toml` before accepting it.

### Option 2 — the install script

```bash
git clone https://github.com/herculepirate/picard-discogs-enrichment.git
cd picard-discogs-enrichment
./install.sh
```

It detects which Picard you have and copies the plugin into the right place.

```bash
./install.sh --list                 # show detected installs and their paths
./install.sh --flavour flatpak      # force one: deb | flatpak | snap
./install.sh --dir /custom/plugins3 # install somewhere specific
./install.sh --uninstall            # remove it again
```

If you have more than one Picard installed it will say so rather than
guessing, and you run it once per flavour.

### Option 3 — copy the folder yourself

Copy this repository's contents into your `plugins3` directory, in a folder
named exactly:

```
discogs_enrichment_dd789f69-332f-498a-af94-d8e3273cc435
```

That is `<sanitized plugin name>_<uuid>`, which is how Picard names plugin
folders itself. Any folder name loads, but using a different one means a
later install from the plugin registry creates a **second** folder with the
same uuid, and both load at once.

The `plugins3` directory is:

| Picard install | `plugins3` directory |
| --- | --- |
| **Debian/Ubuntu (`.deb`, apt)** | `~/.local/share/MusicBrainz/Picard/plugins3/` |
| **Flatpak** | `~/.var/app/org.musicbrainz.Picard/data/MusicBrainz/Picard/plugins3/` |
| **Snap** | `~/snap/picard/current/.local/share/MusicBrainz/Picard/plugins3/` |
| **Windows** | `%APPDATA%\MusicBrainz\Picard\plugins3\` |
| **macOS** | `~/Library/Application Support/MusicBrainz/Picard/plugins3/` |

Flatpak and Snap each confine Picard to their own data directory — a Snap in
particular gives Picard a `$HOME` of `~/snap/picard/current`, so the plugin
folder is **not** under your real home. That is the single most common reason
a manually-copied plugin does not appear.

If you have set `PICARD_PLUGIN_DIR`, that overrides all of the above.

```bash
# Flatpak example
DEST=~/.var/app/org.musicbrainz.Picard/data/MusicBrainz/Picard/plugins3
mkdir -p "$DEST"
cp -R picard-discogs-enrichment \
      "$DEST/discogs_enrichment_dd789f69-332f-498a-af94-d8e3273cc435"
```

### Option 4 — the command line

If your build ships `picard-cli`:

```bash
picard-cli plugins install https://github.com/herculepirate/picard-discogs-enrichment
picard-cli plugins install /path/to/picard-discogs-enrichment   # local folder
picard-cli plugins list
```

Inside a Flatpak, run it through the sandbox:

```bash
flatpak run --command=picard-cli org.musicbrainz.Picard plugins install <url-or-path>
```

`picard-cli` is not shipped by every package. If the command is missing, use
one of the options above — they do the same thing.

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

### What "Discogs only" keeps

Everything a release database could know is replaced. What survives is only
what Discogs cannot supply and the file could not recover:

- **`musicbrainz_*`** — so the file stays re-matchable. With the release MBID
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

**The plugin does not appear in Options → Plugins.** Almost always the wrong
directory — see the table above, and note that Flatpak and Snap do not use
your real home directory. `./install.sh --list` prints the path it would use.

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
