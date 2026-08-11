# Using LibrAIry

1. Open the portal. It goes straight to the dashboard; setting a password is optional (Settings -> Portal Security).
2. Drop files or folders into the inbox host path. Nothing to press — the
   worker notices within a couple of seconds.
3. Watch the activity pill in the header, on any page, for what it is doing.
4. Open Review, approve/edit/reject/postpone proposals.
5. Open Commit, create a plan, inspect it, then execute.
6. Use Search and Browse to find files.

## What Browse shows, and how it differs from Search

**Browse reflects the physical contents of your library directory.** It lists
the real direct children of each folder and then enriches them with whatever
LibrAIry knows — category, size, thumbnail, preview, evidence. A file with no
database record still appears, marked *not indexed*: a missing record means
metadata is unavailable, not that the file does not exist.

That applies to the front page as much as to the folder view. **The tiles on
the Browse home screen are the top-level directories that are really there**,
under their real names, each showing how many visible files lie beneath it at
any depth. A folder you created yourself over SMB gets a tile; a category
LibrAIry knows about but has never filed anything into does not.

Files lying **directly in the library root** are listed under the tiles, in the
same rows the explorer uses. Nothing has to be put in a folder before Browse
will admit it is there.

The consequences worth knowing:

- **Folders are never truncated.** However many files a folder holds, every
  sibling folder is listed. Only the file list pages.
- **Empty folders appear**, because they exist.
- **A file deleted outside LibrAIry stops appearing**, because Browse lists the
  disk rather than the index.
- **Every count means the same thing**: visible files underneath, at any depth.
  A tile, and the number beside a folder in the explorer, are the same measure.
- **Search is index-backed**, so a file that exists but has not been scanned is
  visible in Browse before it is findable in Search. Run a library scan
  (`librairy scan --root library`) to close the gap.

Browse deliberately does not answer *where should this file go* — that is
Review's job. It answers *what is in my library right now*, so a folder whose
name the classifier would not have chosen is still shown exactly as it is.

### The consistency line

Under the Browse heading is one sentence saying whether the index still
describes the library — `140 files · index up to date`, or
`140 files · 3 not indexed`, which opens into which files and what to do.

It is measured when the page renders and nothing watches it afterwards, which
is why it never claims to be synchronised. Two things can be reported:

| Reading | What happened | What fixes it |
|---|---|---|
| *not indexed* | A file is on disk that nothing has scanned — usually copied straight in over SMB. It is browsable but not searchable. | `librairy scan --root library` |
| *missing on disk* | A record points at a file that is no longer there. Search will not return it and Browse never showed it; the record is kept for its history and its evidence. | Nothing automatic. LibrAIry does not delete your records. Put the file back and a scan picks it up again. |

**Not indexed never means unsupported.** The scanner has no extension filter —
anything Browse can see, it can index — so the only reason a visible file has
no record is that no scan has reached it yet.

The reading only reports. Opening Browse does not index a file, delete a
record, trigger a scan, classify anything, or call a catalog or an AI provider.

### Why drift happens at all

Nothing rescans the library on a schedule. The background worker watches the
inbox; library records are written by the commit engine as it moves files in.
So a file that arrives in the library any other way stays unknown until you
scan. That is by design — the library is not touched unless you ask — and the
consistency line exists so the consequence is visible rather than surprising.

### Three questions, three answers

| | Answers | Source |
|---|---|---|
| **Browse** | What is physically in my library? | The filesystem. |
| **Search** | Which files that are here match this? | The index, minus anything a scan looked for and did not find. |
| **History** | What happened, including to things that are gone? | The journal, which is never pruned. |

A file you deleted outside LibrAIry disappears from Browse immediately, drops
out of Search at the next scan, and stays in History forever. Its record stays
too: open `/items/{id}` from a History entry and the page says the file is not
on disk, when it was last seen and where, with no preview offered. Nothing is
deleted to achieve that — the record still carries the classification, the
evidence and any approval or rejection made about it.

Put the file back and the next scan clears the flag; it is searchable again
with no repair and no rebuild.

**A rename outside LibrAIry** looks like a deletion and a new file, because
LibrAIry does not track renames. You get two records and one healthy result:
the old path stops being searchable, the new one appears once scanned.

### What `librairy scan --root library` does

The supported way to reconcile the library with the index. Audited, so this
list is exact:

- **Marks** records whose file it did not find, by setting `missing_since`.
- **Clears** `missing_since` for a file that has come back.
- **Indexes** files it has not seen before, and updates size, mtime and
  fingerprint for ones that changed.
- **Rebuilds the layout map** — the folder conventions used to file a new
  album into the artist folder you already keep.
- **Deletes nothing.** No record, no search entry, no proposal, no history.
- **Classifies nothing**, and calls no AI provider and no catalog.

It only reconciles the root you scanned. `--root library` cannot mark an inbox
record missing, and the background worker only ever scans the inbox — which is
why inbox records can sit missing while the library is perfectly clean.

Scanning never deletes a record. Resolving the workflow entries left behind is
a separate, manual decision — see below.

### Clearing entries whose file is gone

When a file disappears while LibrAIry was still waiting for you to decide about
it, the proposal is left over: not in Review (it is filtered out), not
committable, but still counted. Review says so, and offers to resolve them:

> **7 files moved or deleted outside LibrAIry**, so they are not listed below.
> Usually an unmounted disk — plug it back in and the next scan picks them up
> with the decisions intact.
>
> ▸ 7 inbox entries — waiting to be filed

Open the disclosure and you get the list — filename, path, when it went, what
it would have been filed as, and why LibrAIry thought so — then the button.

**Clearing does not delete anything.** The proposal is marked superseded and
the item goes back to being an unclassified file. Specifically:

| | |
|---|---|
| The file | Already gone. Nothing on disk is touched, then or ever. |
| The item record | Kept. |
| The proposal row and its evidence | Kept, marked superseded. |
| The search entry | Kept, re-synced. |
| History | Untouched. |
| `missing_since` | Left set — clearing is not finding. |
| What you lose | The item's page stops showing a category and a *why here?*. Put the file back and a scan works it out again. |

It is scoped to one root: the button beside a count of inbox entries clears
inbox entries. Running it twice does nothing the second time.

**Nothing does this on a timer.** A missing file is usually an unmounted disk,
and discarding a volume's worth of decisions the moment it drops offline would
be worse than a stale row. If the file comes back before you clear anything,
the next scan takes it off the list by itself, decision intact.

One count worth understanding: **records whose file is missing** and **entries
worth clearing** are different numbers. A proposal you already rejected is not
waiting on anybody, so its file vanishing changes nothing about it and clearing
does not apply. Eight missing records here, seven entries to clear.

### The words, and what they mean here

| Term | Meaning |
|---|---|
| **scanned** / **indexed** | Has a record in `items`. The scanner takes every visible file, whatever its type. |
| **searchable** | Has a row in the search index, which every record gets — so in practice, the same as scanned. |
| **classified** | Has a live proposal carrying a category and a destination. Browse does not require this. |
| **committed** | Moved into the library by an approved plan. |
| **missing** | A scan of that root looked for the file and did not find it. The record is kept; it is excluded from Search, Review, Commit and everything else that assumes the file is there. |

**Intentionally excluded** from both Browse and the indexer, by one shared
rule: dot-files and dot-directories, anything matching `IGNORE_PATTERNS`, and
symlinks (never followed, so they cannot escape the library root or loop).
Directories that cannot be read are skipped rather than crashing the page.
7. Use History to inspect or undo committed filesystem operations.

LibrAIry never commits automatically. Analysis only writes database proposals. File moves happen only through an approved immutable plan.

## Knowing something is happening

LibrAIry watches the inbox for you. The worker polls a cheap fingerprint of the
inbox's folder timestamps every couple of seconds while idle, so a file you just
dropped in is picked up almost immediately rather than waiting out the idle
backoff. There is no filesystem-watcher dependency and nothing to configure.

The header carries a small activity pill on every page:

| What you see | What it means |
| --- | --- |
| *nothing* | Idle, and no backlog. The pill takes up no room until it has something to say. |
| *scanning the inbox — 5 to go* | Working, with 5 inbox files found but not yet identified. |
| *identifying files — 12 to go* | Running classification, including any AI step. |
| *7 new files found* | Files are queued and the next cycle will take them. |
| *worker stopped — 7 files waiting, nothing running* | There is a backlog and no heartbeat for three minutes. Check the container logs. |

The pill refreshes itself every three seconds and costs two indexed counts, so
leaving tabs open is cheap.

## The review flow

Three steps, and only the last one touches a file.

1. **You drop files in the inbox.** LibrAIry reads them, asks the catalogs and
   tools, and proposes a name and a place. It never stops to ask you anything.
2. **You decide in Review.** Nothing on disk has moved yet — every button here
   only changes what the plan says.
3. **You commit.** The only step that moves a file, and it executes exactly the
   plan you approved.

**Why two steps?** Deciding is fast and reversible; moving a hundred files is
neither. Approving builds a list, and Commit shows you that exact list before it
touches anything — so a mistake in Review costs one press of Undo instead of a
hunt through your library. Review tells you how many are approved and waiting,
with a link straight to Commit.

| Button | What happens | Where the file goes |
| --- | --- | --- |
| **Approve** | You agree with the name and the place. It joins the next commit. | Stays in the inbox until you commit, then moves to the library. |
| **Re-analyse** | The guess is wrong or thin. The file goes back in the queue for a fresh pass — tags, catalogs, the duplicate detectors and any AI you have switched on. | Stays in the inbox; a new guess lands within a cycle or two. |
| **Quarantine** | Not in the library. Set aside indefinitely, whole and unchanged, restorable from the Quarantine page. | Moves to the quarantine folder on commit. |
| **⋯ Later** | Not now. Drops out of *Waiting on you* so the queue is what you have not looked at yet. | Stays in the inbox. Filter State to *Put off for later*. |
| **⋯ Mark for deletion** | Done with it. The same move as Quarantine, into one folder inside it. | Moves to `quarantine/_to-delete` on commit. |

The same summary is on the Review page itself, behind **What do these buttons
do?** next to the heading.

**Re-analyse** replaced *Not this*, which set the guess aside and never guessed
again — reachable in one click and escapable only from the command line. If you
add catalog keys or an AI provider after a scan, re-analysing is how the files
already in the queue get the benefit. The whole queue at once is
`librairy analyze --reanalyze`.

**Nothing here deletes anything, including Mark for deletion.** It gathers the
files you have finished with into one folder so that emptying them is a single
deliberate gesture you make yourself, in your own file manager. The Quarantine
page has the same button for files already held there, and *Put it back* still
works from the pile.

### Undo

After any decision an **Undo** bar appears above the list, naming what it will
take back — "Approved 12 files". One press restores the whole batch, including
the destination that Quarantine overwrote.

This is not the same as History's undo. Undo here reverses a decision made
*before* anything moved, so it only touches database rows. Once a file has been
committed it is on disk somewhere new, and only History can move it back —
Undo refuses those and says so rather than flipping a status to describe a
library that does not exist.

### Reading the confidence bar

Each row carries a short bar and a percentage. **Length and colour are both the
score** — how sure LibrAIry is that this name and this place are right:

| Colour | Score |
| --- | --- |
| Green | 85% and up. A catalog or the file's own tags settled it — this is what *Approve all* takes. |
| Amber | 60–85%. Good evidence, worth a glance. |
| Red | Under 60%, or no destination yet. Read this one. |

Within a bar, the **shading** says what the score is made of: solid where a
public catalog or the file's own tags earned it, fading out through local AI and
cloud AI to a plain guess from the filename. 62% off a catalog match is a
different proposition from 62% assembled out of a name.

Hover any bar for the same thing in words. The full breakdown, entry by entry,
is behind **Why** on the row.

### Other options

"Why is it this?" and "what else could it be?" are the same moment, so the
second question lives at the bottom of the answer to the first. **Why → Other
options** asks every catalog and every AI provider you have switched on — each
one separately, about this one file, right now — and lists what each said, with
the destination it would give the file and a **Use this** button.

Analysis does not work this way, deliberately. During a scan the catalogs are
one cascade and the providers stop at the first answer good enough to act on,
because asking everything about fifty thousand files to discard most of the
answers is a lot of electricity for nothing. It also hides the disagreement
that makes the question worth asking: TMDB and your local model can name the
same film slightly differently, and MusicBrainz and Discogs can file the same
track under two different genres — and the genre is the first folder in the
path. Choosing between them is a different question, asked about one file at a
time, so it happens on demand and nothing is stored. A local model can take
twenty or thirty seconds to answer; the panel says *asking…* while it waits.

A real example, on a movie whose filename had defeated the heuristics:

| Asked | Said |
| --- | --- |
| TMDB | 86% — `Movies/General/An-American-Carol-(2008)/An-American-Carol-(2008).mp4` |
| Local AI | 85% — `Movies/General/An-American-Carol-(2008)/An-American-Carol.mp4` |
| TVmaze | no match |

Two consequences worth knowing:

- **It is always current.** A provider or catalog key you added five minutes ago
  is included, without re-analysing anything.
- **A low-scoring answer still shows its destination.** During a scan anything
  under the confidence threshold has its destination stripped, because nothing
  that unsure should file itself. Picking one by hand is a different act, and an
  option that cannot say where the file would land is not a choice.

Anything that fails or finds nothing is listed too, with the reason. A silently
shorter list would read as agreement. In particular, a catalog that drew a
blank is reported as *no match* rather than being credited with the guess the
classifier falls back to — a row labelled TMDB that TMDB did not produce would
undermine the one thing this panel is for.

The button next to it in the row, **Re-analyse**, is the same machinery aimed
the other way: it hands the file back to the worker to pick a new winner. Use
*Other options* to choose an answer yourself, and *Re-analyse* to let LibrAIry
choose again.

### Fitting your existing layout

Run this once, and again whenever your library changes shape:

```bash
docker exec librairy librairy scan --root library
```

It reads your library without writing to it and learns which folder you already
keep each artist, show and film in. After that, a new record by an artist you
already have joins that folder instead of the one a template would invent —
if your library is `Music/Pop/Abba/` and the genre-first template would produce
`Music/Disco/Abba/`, the proposal is for `Music/Pop/Abba/`, and *Why* says
**Fits your existing layout**. The album underneath is kept.

An artist with no folder of their own is left to the template. Until you run
that scan the map is empty and nothing changes.

### Image understanding

Off until you switch it on, in **Settings → Image understanding**. Once it is
on, a model running on your own hardware opens each picture and says what is in
it: a caption, the things it contains, some tags, and any text it can read. The
row grows a **described** badge and everything it found appears under *Why*.

```
What the picture shows
  A screenshot of a phone's Wi-Fi settings, listing nearby networks.
  In it     phone settings
  Tags      wifi, network, settings
  Kind      screenshot
  Read by   google/gemma-4-e4b, on your own hardware
  ▸ Text in the image
```

**Images never go to a cloud provider.** Not as an opt-in — there is no setting
that permits it. Cloud AI, if you have it enabled, carries on reading filenames
and is simply never offered a picture. A redacted filename is a few words; a
photograph of your kitchen is a photograph of your kitchen.

What it changes, and the list is deliberately short:

- **It never changes the category.** If the model says *receipt* about something
  filed under Photos, *Why* says so — `screenshot — not photos` — and stops
  there. The category dropdown is four lines below in the same panel, so the
  useful thing to do with a disagreement is show it to you. Nothing is filed on
  the strength of a caption.
- **It only ever adds to a filename, and only when the name says nothing.**
  `IMG_4821.jpg` becomes `IMG-4821-baby-with-cat.jpg`. A name with a real word
  in it is left alone, and `IMG-20240612-101112.jpg` keeps its capture time —
  that timestamp is what makes a photo folder sort. The words go through the
  same sanitising as every other name.
- **Agreement nudges the score up by 0.05, to a ceiling of 0.92.** Disagreement
  changes nothing, so a file deliberately held below the threshold — album art
  beside its album, say — stays there.
- **It all becomes searchable.** Caption, subjects, tags and the text out of the
  image go into the index, so searching `wifi` finds the screenshot of the Wi-Fi
  settings.

Two settings worth understanding:

- **Which images.** *Every image* is what you want it on for. *Only ones the
  scan was unsure about* sounds thriftier, but an ordinary photo already scores
  0.85 from its extension alone — comfortably over the threshold — so that mode
  skips almost every photo in a photo library.
- **Model.** Empty means the same model your local provider already uses. A
  model has to be able to *see*, and plenty of excellent text models cannot; one
  that cannot logs the server's own refusal and changes nothing else. Point this
  at a small vision model to keep it separate from the one reading filenames.

JPEG, PNG, WEBP, GIF and BMP. HEIC is not supported — ffmpeg cannot open it, and
adding an image library to decode it is a bigger change than this feature is
worth. Expect a few seconds per image on a small model and considerably longer
on a large one; a re-analysis of an unchanged file reuses the stored answer
instead of looking again.

## Duplicates

Exact duplicates are staged for reversible quarantine review. Similar media flags
are informational and require human judgment. LibrAIry never deletes duplicate
files.

**How alike is "similar"?** `CZKAWKA_SIMILARITY` decides, and it defaults to
`strict` — only what is visually identical. `balanced` also catches resizes and
re-encodes. `loose` catches crops and heavy edits, and on a real library it will
also hand you eleven unrelated photographs as one group; it is worth having, but
only when you are going through the results yourself. Nothing acts on a
similarity flag at any setting.

## Ripped discs

A DVD folder arrives as `VIDEO_TS.IFO`, `VIDEO_TS.BUP`, a row of `VTS_01_n.VOB`
and so on. Read one file at a time these are unidentifiable — nothing about
`VTS_01_3.VOB` says what film it is — so LibrAIry reads the **folder above the
disc directory** instead, which is the only place anybody wrote the title down,
and files the whole structure under it:

```
Queen - 1979-12-26 - The Queen Special on TV - DVD5/VIDEO_TS/VTS_01_1.VOB
  → Movies/General/Queen-The-Queen-Special-on-TV-(1979)/VIDEO_TS/VTS_01_1.VOB
```

**The names inside the disc directory are never rewritten.** A player looks for
`VTS_01_1.VOB` by exactly that name and `VIDEO_TS.IFO` points at its siblings by
theirs, so a tidied disc folder is one that no longer plays. Everything above
the disc directory is tidied as usual; from it downwards the names are only made
safe, never changed. `DVD5`, `Disc 2` and similar are dropped from the title —
they say how it was written down, not what it is.

Only a disc *directory* counts. A stray `VIDEO_TS.IFO` in a downloads folder is
just a file.

Files inside a `VIDEO_TS`, `AUDIO_TS`, `BDMV` or `CERTIFICATE` folder are never
treated as duplicates of each other. A DVD keeps a byte-identical `.BUP` beside
every `.IFO` on purpose — a player falls back to it when the `.IFO` will not
read — so quarantining one damages the disc rather than tidying it. The folder
is what signals this, not the filename: a stray `VIDEO_TS.IFO` in a downloads
folder is just a file.

### Comparing the two copies

Any Review row where the file may already be in your library is marked
**duplicate** and gains a **Compare** button. It opens the
inbox copy and the library copy side by side, with a preview of each, and below
them two things:

**What each check found.** Three detectors run, each answering a different
question, and the panel keeps all three answers rather than collapsing them into
one verdict:

| Check | Answers |
| --- | --- |
| BLAKE2b fingerprint | Are these the same bytes? |
| rmlint | Does a second, independent implementation agree? |
| czkawka | Do they *look* the same, whatever the bytes say? |
| file size | Which is bigger — for one recording, usually which is better |
| ffprobe / exiftool | Duration, bitrate, resolution, codec, camera, date taken |

A detector that is switched off in Settings → Library says **not asked**, which
is deliberately not the same as **agrees**. When the fingerprint and rmlint
disagree, nothing is staged and the panel says so — that combination is rare
enough to be worth a human look.

**Side by side.** Every property measured on both copies, with the rows that
differ marked. This is what answers "which one do I keep?" — a 320 kbps rip
against a 128 kbps one, or a full-size photo against a resize, are not questions
a hash can settle.

The recommendation stops where v1 stops. When the copies are identical it says
to quarantine the inbox one. When the *inbox* copy looks better, it says to
reject the quarantine proposal and keep both, because nothing in LibrAIry
overwrites anything: the better copy is filed alongside and the older one stays
where it is until you remove it yourself.

Comparisons are built during the worker cycle and loaded when you press the
button, so a page of fifty rows does not fetch a hundred previews.

## Accessing Files

Use the Access page for SMB/FTP/WebDAV pointers. LibrAIry does not serve those protocols; your NAS or operating system does.
