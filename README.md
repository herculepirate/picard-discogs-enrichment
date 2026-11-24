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

**Reads your copy, not just MusicBrainz.** The album's folder says which
pressing you own: a catalogue number in its name (`[Relativity, 88561-1115-2,
U.S.A.]`), a rip log or cue sheet whose disc ID fits your files, a pressing
year, a vinyl or SACD rip, and the barcode and catalogue number printed on
your scans and PDF booklets (OCR). Discogs is searched by these first and its
releases ranked against them. See [Your copy's evidence](#your-copys-evidence).

**The web, where MusicBrainz and Discogs fall short** (off by default):
empty genres and credits, cover art, lyrics, and albums no database has, from
Deezer, iTunes, Bandcamp, Wikipedia, VGMdb and LRCLIB — each used only when
every track's length fits your files. See [Web sources](#web-sources).

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

Picard writes a collaboration as one joined credit
(`Max Corbacho & Bruno Sanfilippo`) next to several MusicBrainz artist IDs.
Players that group artists by MusicBrainz ID (Lyrion, Navidrome) give that
whole credit to the first ID, so Max Corbacho's artist page is renamed
`Max Corbacho & Bruno Sanfilippo` — and the album never appears under
Bruno Sanfilippo.

Whenever the plugin applies a Discogs release it settles each artist field
(`albumartist` and `artist`) against MusicBrainz's per-artist list:

- **Same artists as MusicBrainz** (in any order, joined by `&`, `/`, `,`,
  `and`, `feat.` …, whether Discogs rewrote the credit or not): a
  collaboration is written as **one name per artist**
  (`Max Corbacho`, `Bruno Sanfilippo`) in MusicBrainz's order, and the
  MusicBrainz artist IDs, the `artists` / `albumartists` lists and the sort
  names are kept in that same order. Each name now sits next to its own ID,
  so the album is listed under every collaborator and no artist page is
  renamed. A single artist, or a group that is one MusicBrainz artist
  (`Simon & Garfunkel`), is left as it is.
- **Discogs credits more artists than MusicBrainz** (MusicBrainz's credit is
  missing a collaborator, e.g. *Croatian Variations*: MusicBrainz credits
  `Blaine L. Reininger`, Discogs `Steven Brown & Blaine L. Reininger`): the
  plugin looks up each Discogs artist's MusicBrainz ID through the Discogs
  link on their MusicBrainz artist page — an exact match, not a name guess,
  and no Discogs requests. When every artist is found, and MusicBrainz's own
  artists are among them, all are written — `Steven Brown`,
  `Blaine L. Reininger` — each with its ID and sort name, in Discogs' order.
  If any artist cannot be found, the rules below apply unchanged.
- **Named differently** (Discogs' "credited as" short names such as
  `Adams / Reich - Solisti New York, Ransom Wilson`, a different spelling,
  or other people) **and MusicBrainz has an ID for every artist**:
  MusicBrainz's names win — `John Adams`, `Steve Reich`, `Solisti New York`,
  `Ransom Wilson`, each with its ID, per-artist list entry and sort name — so
  the album stays on every artist's page. Every other Discogs field (label,
  credits, genres, notes, …) is still applied.
- **"Discogs only" mode, or MusicBrainz lacks an ID for some artist**: the
  credit stays as Discogs wrote it, and the MusicBrainz IDs, per-artist lists
  and sort name of the old credit are removed (unless Discogs supplied that
  tag itself), so the joined name cannot take over a single artist's page.

Release and recording IDs are never touched, so the file stays re-matchable
in Picard.

Role credits (composer, arranger, producer, performer, …) use each artist's
own name, not the sleeve's "credited as" spelling (often a surname alone), so
they merge with MusicBrainz's names instead of listing the same person twice.

**Release country** is written as the code Picard and MusicBrainz use (`IT`,
`GB`, `XE` for Europe, `XW` for Worldwide), converted from Discogs' name
(`Italy`, `UK`, `Europe`). A Discogs country that spans several countries
(`UK & Europe`) has no code, so the existing value is left alone.

### Collaborations on every album

The same one-name-per-artist rewrite also runs on MusicBrainz's own data the
moment Picard loads an album — whether or not Discogs is used, finds a match,
or is switched to "MusicBrainz only" — so no tagging script is needed. It is
on by default: **Options → Plugins → Discogs Enrichment → "Write
collaborations as one artist name per artist"**. Turn it off to keep Picard's
usual joined credit.

If your file-naming script uses `%albumartist%`, a collaboration then expands
to `Max Corbacho; Bruno Sanfilippo`. To name the folder
`Bruno Sanfilippo & Max Corbacho` (sorted, so both orders share one folder)
use `$join($sortmulti(%albumartist%), & )` for the artist folder instead.

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

## Your copy's evidence

MusicBrainz's release is the one Picard *matched*, which for a much-reissued
album may be another pressing than yours. So the plugin also reads the
album's own folder, using TagMender's readers (see [Development](#development)):

| Read from | What it gives |
| --- | --- |
| Folder and file names | Catalogue numbers in brackets (`(Under One Flag, CD FLAG 54, France)`), a remaster or reissue year (`[2004 RM]`), vinyl and SACD rips |
| Rip logs (EAC, XLD) and cue sheets | The disc's table of contents and so its MusicBrainz disc ID, only when it fits your files to the sector; the year it was ripped; catalogue numbers in the paths the ripper wrote to |
| `CDTOC` tags, and lossless files that are whole CD sectors | The disc ID, rebuilt from the files' own lengths |
| Notes (`.txt`, `.nfo`), saved pages | Catalogue numbers and barcodes the ripper typed (searched, never proof) |
| Scans (`.jpg`, `.png` … in the folder and its subfolders) | Barcodes decoded off the back or a sticker (proof); the page text: catalogue numbers, labels, "Made in", matrix codes on the disc ring; the barcode's digit band read alone when it will not decode. Needs [OCR](#reading-your-scans-ocr) |
| PDF booklets | Their text (OCR'd when a page has none of its own) |
| Your files' tags | A barcode and catalogue number, unless another tagger wrote them (the files carry release IDs): those came *from* a release and prove nothing |

What it does with them, when your copy says anything of its pressing:

1. **Your disc ID.** When MusicBrainz attaches it to the release Picard matched
   (no request needed), the Discogs releases that MusicBrainz release links to
   are proven by it. Otherwise MusicBrainz's disc ID lookup and the CUETools
   database are asked.
2. **Discogs is searched as TagMender searches it**: your copy's barcodes, then
   its catalogue numbers (with the artist, then the label), then the title,
   then the master's other versions; plus every Discogs release MusicBrainz
   links to.
3. **Every candidate is scored by TagMender's own scorer** — track count and
   lengths, title and artist, your disc ID, barcode, catalogue number, year,
   country, label, format, packaging, rip date — and TagMender decides whether
   it is sure.
4. **The Discogs release is applied on its own only when:**

   | | |
   | --- | --- |
   | **Proven** | TagMender is sure *and* your copy proves the pick: your disc ID, your barcode or your catalogue number, with no other pressing proven as strongly (a disc ID beats a barcode, a barcode a catalogue number; a number two pressings share proves neither) |
   | **Only one proven** | Your disc ID or barcode is on exactly one candidate, it fits every track, its artist and title are the album's, and nothing of your copy is against it — even when TagMender's overall confidence is under 90% (five pressings of one album at 82%, one of them with your exact disc ID) |
   | **Linked** | MusicBrainz links that Discogs release to the release Picard matched, and nothing of your copy is against it |
   | **Same as MusicBrainz** | It carries the matched MusicBrainz release's barcode (or its catalogue number and year), and nothing of your copy is against it |
   | **Printed on your scans** | Several pressings carry what your copy proves (one barcode on a 1994 and a 1999 run), and only one of them lists a mark your scans print: a copyright line (*"© 1994 Movieplay S.A."*), a rights society (SPA, MCPS, GEMA …) or a matrix code. If a mark another pressing lists is printed too, you are asked |
   | **Only one unopposed** | Several pressings carry your barcode or catalogue number, and only one has nothing of your copy against it, the country your copy names telling them apart: printed on your scans for it (*"Made in the EU"* on a *UK & Europe* pressing; the Taiwan, Brazil and Mexico runs of the same barcode ruled out), or against every other (*Germany* in the folder name: the *Europe* CD, not the *Russia* one). Under **"…take the most likely"** |
   | **No code printed** | Your back cover was scanned and read whole, no barcode is on it or in your tags, and of the pressings that fit only one lists neither a barcode nor a catalogue number (a codeless promo-style CD, not the Japanese or reissue pressings that print theirs). Under **"…take the most likely"** |
   | **Most likely** | Several pressings carry what your copy proves and nothing printed tells them apart, but TagMender scores one clearly highest (at least 90%, a point or more ahead): its year, country, label and track lengths fit best. The others are named in the log. Turn off with **"When several pressings share my proof, take the most likely"** |

   An unofficial release (a bootleg) is taken only when no official release
   carries what proves it (TagMender's rule: bootlegs copy official barcodes).

   **Picard matched another pressing.** When the disc ID in your rip log or
   cue sheet is on one *other* MusicBrainz release than Picard matched (Picard
   took the vinyl, you ripped the CD), your files are moved to that release —
   as Picard's own disc lookup would put them — and the album left empty is
   removed; the right release then loads and is looked up afresh, so its
   media, barcode and Grouping are your copy's too. On several releases the one
   whose barcode is yours, or whose catalogue number is printed on your scans,
   is taken; failing that (under **"…take the most likely"**) the one CD among
   them with the matched release's catalogue number (PEL 350 → *PEL 350CD*, not
   *PEL 349*). Otherwise, or for a copy of more than one disc, you are asked. Turn off with **"Move my
   files to the MusicBrainz release my disc ID is on"**.

   **Read again, harder.** When the first reading leaves the pressing
   undecided, the scans — and the cover art embedded in your files — are read
   again: every page in all four turns, its small print in tiles, barcodes
   enlarged and thresholded, the barcode's digits even when your tags carry
   one. Then it is decided again.

   **Nothing your copy can be.** When every Discogs entry is one your copy
   rules out (only an MP3 download, for a CD rip), the album gets no Discogs
   data and no picker opens.

   **One album of several on a release.** When the Discogs release holds more
   than the album Picard matched — a "two on one" CD (*Beginnings / Tomorrow*)
   whose title has the album's as one of its parts, with more tracks — only
   its pressing is written (date, country, label, catalogue number, barcode,
   media, genre, the Discogs links). The album's own title, artists, original
   date, credits and track titles stay those of the album Picard matched.

   A folder with a CD rip log is never marked *Vinyl Rip* or *Tape Rip*
   because the MusicBrainz release Picard matched is an LP or cassette.

   A 12–13 digit "catalogue number" one digit off your decoded barcode, and no
   valid barcode itself, is a misread or mistyped barcode: it is not held
   against every pressing. Discogs' compound countries (*UK & Europe*) count as
   Europe.

5. **Otherwise you choose.** The release picker opens with TagMender's
   candidates, best first; its Match column shows TagMender's confidence and
   every clue for and against (*"Proven: 100% | for: disc ID (track positions
   of the CD) matches; label Century Media, 77219-2 in the tags"*). Pickers
   open one album at a time and do not block Picard. Turn this off with
   **"Ask me when the pressing is not proven"**; the album then gets no Discogs
   data until you use Search Discogs….

Your disc ID on a *different* MusicBrainz release than the one Picard matched
always asks you: the MusicBrainz match and your copy disagree.

When your copy says nothing of its pressing, the plugin behaves as before: the
MusicBrainz link, or a search by MusicBrainz's barcode and catalogue number.

An automatic look-up makes at most 15 Discogs requests (searches and
releases; repeats come from the cache). Picard sends them one a second, which
is Discogs' limit, and albums are looked up one at a time on the plugin's own
thread, so a large batch takes a while but never slows Picard down. The
troubleshooting log (detailed) lists every candidate with its score and clues.

**Search Discogs…** is pre-filled with your copy's barcode, catalogue number
and pressing year.

A disc ID counts only when its TOC has three tracks or more: a one- or
two-track disc ID, or a single-file image read as one track, is just "a CD
this long". The folder is never read when the album's files are spread over a
folder holding other albums (an artist's or library folder), and nothing in
it is changed. Switch it off under **Options → Plugins → Discogs Enrichment →
"Read the album folder for which pressing you own"**.

### Reading your scans (OCR)

The scans are read as TagMender reads them, by `discogs_ocr_helper.py` on
**your system's Python** — Picard's Flatpak has no Pillow, zbar or tesseract.
Install, on the system (not in Picard):

```bash
sudo apt install tesseract-ocr python3-pil python3-pyzbar python3-numpy poppler-utils
# more languages are read when their packs are installed, e.g. tesseract-ocr-jpn
```

`tesseract-ocr` and `python3-pil` are needed; without `python3-pyzbar` no
barcode is decoded, without `python3-numpy` the digit band and disc rings are
not read, without `poppler-utils` a PDF's own text is not.

**Flatpak Picard** must be allowed to start the helper on your system, once:

```bash
flatpak override --user --talk-name=org.freedesktop.Flatpak org.musicbrainz.Picard
```

then restart Picard. The plugin then runs `flatpak-spawn --host python3 …`.

Up to 15 scans an album are read, the likeliest to carry a code first
(the back, tray, disc and obi before the booklet). Each is read once — a full
set takes a minute or two the first time — and kept in a cache
(`~/.var/app/org.musicbrainz.Picard/cache/discogs-enrichment/ocr.sqlite` for
the Flatpak). A page tesseract reads poorly as a whole (a busy back cover:
a barcode, a grey panel, columns of credits) is read again in tiles, smoothed
and enlarged, for its small print — the copyright line at its foot. Cover
art embedded in your audio files is read as a scan. When that first reading
leaves the pressing undecided, everything is read again, harder (see above),
and cached too. A barcode
decoded off a scan is your copy's own, as a tag would be; the codes printed on the scans are searched too, and the page text is
scored against every candidate. Nothing is sent anywhere.

While OCR is unavailable, **every look-up says so** in the log and Picard's
status bar, naming what is missing. Switch it off under **"Read my scans and
PDF booklets (OCR)"**.

---

## Your copy and editions

What your copy is, written where a player shows it — TagMender's rules
(`tm_editions.py`, `tm_marks.py`, `tm_vinylrip.py`, generated from its
`naming.py`, `marks.py` and `vinylrip.py`). Under **Options → Plugins →
Discogs Enrichment → Your copy and editions**:

| Option | Default | What it does |
| --- | --- | --- |
| **Edition in the Album tag** | Off | *Nightfall [2004 Remaster]*, *25 [2015 Vinyl Rip]*, *Brave [1999 SACD]*, *[HD]*. SACD, DVD-Audio, Blu-ray and vinyl or tape rips always; a CD or download only as a newer remaster or reissue (its date later than the first release's) or by the name it calls itself (*[2023 Deluxe Edition]*). The original CD or download has none; a bootleg is no remaster. A *(Remastered)* the title carries gives way. "Edition" alone: *[Remaster]*, *[Vinyl Rip]* |
| **ALBUMVERSION and RELEASECOMMENT** | On | With the edition: ALBUMVERSION holds it, RELEASECOMMENT MusicBrainz's disambiguation — for LMS and other media servers |
| **Mark HD audio, vinyl and tape rips in Grouping** | On | *HD Audio* (above 16 bits or 44.1 kHz, DSD too), *Vinyl Rip*, *Tape Rip* — only where Grouping is empty |
| **A vinyl rip's details in the comment** | On | *Vinyl rip: PBTHAL · 24-bit/96 kHz · ripped 2012 · turntable*, from the folder name and its notes, beside what the comment says |
| **Remove the files' comments** | Off | A ripper's or shop's note is taken off (the vinyl rip line stays) |
| **Single-artist compilations** | Off | A best-of (release type Compilation) is flagged COMPILATION too |
| **ITUNESCOMPILATION and COMP** | Off | In FLAC, Ogg, Opus, APE and WavPack files of a compilation (ID3 and MP4 have their own flag) |
| **Grouping "Compilation"** | Off | Where Grouping is empty |
| **Genres: at most** | 5 | Per album; 0 for no limit. With **your own genre names** (`funeral doom metal = Doom Metal`) |
| **Time-synced lyrics as .lrc** | Off | `01 Track.lrc` beside the track when it is saved, never over one that is there |

The files are measured (bit depth, rate) and the folder's rip notes read off
Picard's main thread, and everything is written again when Discogs writes its
tags or a file joins its track, so the edition follows the release finally
chosen. Picard's own **Various artists** name (Options → Metadata) stays
Picard's.

## Album artist

The album artist is filed as TagMender files it (its `album_artist.py`,
copied as `tm_album_artist.py`); the **Artist** tags always keep the credit
as it is. Under **Options → Plugins → Discogs Enrichment → Album artist**:

| Option | What it does |
| --- | --- |
| **Lead artist only** (on) | A joint credit's first artist is the album artist: *Thomas Strønen & Time Is a Blind Guide* → *Thomas Strønen*. A collaboration of equals (*Calexico and Iron & Wine*), various artists and classical albums are kept whole |
| **Bands named after a member** (on) | *The Jimi Hendrix Experience*, *Pat Metheny Group*, *Steve Morse Band*, *Tom Petty and the Heartbreakers* → that member — only when MusicBrainz (else Discogs) lists that person as a member. *Deep Purple* and *The Marshall Tucker Band* stay |
| **Your own album artists** | `Credit = Album artist` lines over both rules (`Dregs = Dixie Dregs`); `Ben Folds Five = Ben Folds Five` keeps one as credited |

The album artist's sort name and MusicBrainz ID follow the member's; one not
known is removed rather than left naming the band. The band's members are
asked of MusicBrainz once a session; when MusicBrainz cannot be reached the
album artist is left as it is. It is chosen when MusicBrainz matches the
album and again after Discogs writes its own credit, so your file-naming
script (`%albumartist%`) files the album under the member.

## Web sources

Off by default: **Options → Plugins → Discogs Enrichment → Web sources**.
They are TagMender's (`web_tag`), plus VGMdb and LRCLIB:

| Source | Through | What it is good for |
| --- | --- | --- |
| Deezer, iTunes | their public catalogue APIs | genres, front covers (iTunes up to 3000 px) |
| Bandcamp | Bandcamp's own search, and the album data its pages publish for search engines | independent releases, their tags as genres |
| Wikipedia | the MediaWiki API (infobox and track listing) | genres of older and well-known albums |
| VGMdb | [vgmdb.info](https://vgmdb.info)'s JSON | game and anime soundtracks, Japanese releases: composer, arranger and lyricist credits, scans |
| LRCLIB | [lrclib.net](https://lrclib.net)'s API | lyrics |

**The proof.** A web album is used only when TagMender's matcher is sure of
it: artist, title, track count and every track's length fit your files (or,
before the files are attached, the matched MusicBrainz release). The sources
are asked in the order you list them, and the first that proves the album is
used.

**What it fills.** On an album MusicBrainz matched: only tags nothing else
gave — the genre, and VGMdb's composer, arranger and lyricist. Never a
pressing's label, barcode or catalogue number (a shop's album is the album,
not your pressing). When Discogs later supplies a tag the web filled, Discogs'
value replaces it.

**Cover art.** A provider named **"Web (Deezer, iTunes, Bandcamp, VGMdb)"**
offers the proven album's front cover (VGMdb's back and booklet scans too,
with the art scope set to more than the front). Enable it, and place it,
under **Options → Cover Art**; like every provider it runs only while no
earlier one found a front cover.

**Lyrics** ("Lyrics from LRCLIB, where a track has none"): LRCLIB's lyrics
for that very track — its artist and title, and its length within two seconds
of yours (a live version is another recording). Plain lyrics, or LRC
time-synced ones with "Prefer time-synced lyrics".

**Albums no database has.** Right-click a cluster MusicBrainz finds nothing
for → **Tag from the web…**: the first source that proves the album gives
album, album artist, track artists, titles, numbers, date and genre (a shop's
barcode only when your copy is that download), after you confirm. Save in
Picard to keep them.

No search engine's page is read, nor any site whose terms forbid automated
reading; every request goes through Picard's web service, one a second per
site. *vgmdb.info* depends on VGMdb.net, which now sits behind Cloudflare, so
it may be unreachable; it is then skipped and the troubleshooting log says so.

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

Pure logic lives in `discogs_mapping.py`, `discogs_evidence.py` and
`discogs_decide.py` with no Qt
or Picard imports, so it can be tested directly (`python3 tests/test_*.py`).

`tm_match_core.py`, `tm_readers.py`, `tm_ocr.py`, `tm_web.py`, `tm_album_artist.py`, `tm_album_artist_deps.py`, `tm_editions.py`, `tm_marks.py`, `tm_vinylrip.py` and `tm_ocr_order.json` are
**generated** from a
[TagMender](https://github.com/herculepirate/tagmender) checkout by
`tools/vendor_tagmender.py`: its pressing scorer verbatim, and the folder
reader (`read_album`) with every reader it reaches, and its scan reading
(run by the OCR helper, outside Picard). Never edit them; change
TagMender and run the tool again. `tests/test_vendored.py` fails when they are
behind TagMender. `tools/library_check.py ~/Music [--ocr] [--decide]` shows, outside Picard,
what the plugin reads of every album in a folder and (with `--decide`, over
the network, cached) what it would decide; nothing is changed or tagged.
`discogs_runtime.py` holds the late-bound `api`
object: `enable(api)` binds it once and the rest of the plugin imports `log`
and `settings` from there, rather than threading the api through every call.

```
MANIFEST.toml            plugin metadata (uuid, name, api version)
__init__.py              enable()/disable(), actions, options page
discogs_runtime.py       late-bound api: settings + logging
discogs_mapping.py       pure logic — field mapping, merge modes, scoring
discogs_evidence.py      pure logic — your copy's evidence from the album folder
discogs_decide.py        pure logic — TagMender's verdict on the Discogs release
discogs_http_bridge.py   TagMender's synchronous http over Picard's web service
tm_match_core.py         generated from TagMender: pressing scorer, code rules
tm_readers.py            generated from TagMender: folder, rip log, cue readers
tm_ocr.py, tm_ocr_order.json  generated from TagMender: scan and document reading
discogs_ocr_helper.py    reads the scans on your system's Python; prints JSON
discogs_ocr_bridge.py    starts the helper (flatpak-spawn --host in a Flatpak)
discogs_ocr.py           pure logic — what the scans read, added as TagMender adds it
tm_web.py                generated from TagMender: web sources (web_tag)
tm_album_artist*.py      generated from TagMender: album_artist.py, as it is
tm_editions.py           generated from TagMender: naming.edition (ED-1..7)
tm_marks.py, tm_vinylrip.py  generated from TagMender: marks.py, vinylrip.py
discogs_web.py           pure logic — web sources, VGMdb, LRCLIB, the proof
discogs_webenrich.py     web sources in Picard: fills, lyrics, cover art, cluster action
discogs_albumartist.py   the album artist as TagMender files it (tm_album_artist.py)
discogs_copy.py          edition, Grouping marks, vinyl rip comment, compilations, genres, .lrc
tools/vendor_tagmender.py  regenerates the tm_* files
tools/library_check.py   what the plugin reads and decides, over a music folder
discogs_client.py        Discogs API over Picard's web service
discogs_*_dialog.py      release picker, search, disc mapping, preview
discogs_coverart.py      cover art provider
discogs_logging.py       size-capped troubleshooting log
```

## Licence

GPL-2.0-or-later. See `MANIFEST.toml`.
