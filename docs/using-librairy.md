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
does not apply. Eight missing records here, seven entries to clear — and the
disclosure says so, rather than leaving you to reconcile it.

The same thing from a terminal:

```bash
librairy vanished list --root inbox
```

```bash
librairy vanished clear --root inbox --yes
```

`list` is also the preview: it prints exactly the entries `clear` would resolve,
with relative paths only. `clear` needs an explicit root and `--yes` — without
it, it reports what it would do, changes nothing, and exits `2` — and running it
again when there is nothing left succeeds with `cleared: 0`. It is the same
function the Review button calls. See [the command line](cli.md) for the
`--json`, confirmation and exit-code rules every command follows.

### What a missing record's page shows

Open one directly and it says the file is gone rather than describing it as
though it were there:

- **Last known size**, not *size*. The scanner keeps the last size it measured;
  printing it plainly read as a claim about a file nobody can check. A record
  whose file was never measured says *not recorded*.
- No preview and no viewer control — there is nothing to open.
- The path it last had, relative to its root. No host path.
- Its category, evidence, siblings and history, all still there.

### Where a proposal was going, and what that meant

A destination is not always a filing decision, and the path alone does not say
which it is, so the label does:

| Label | Means |
|---|---|
| **Would have been filed as** `library/…` | Normal filing. |
| **Set aside** `quarantine/…` | Sent to quarantine — a duplicate, or something you shelved. |
| **Marked for deletion** `quarantine/_to-delete/…` | Staged for deletion. Still a quarantine move, still restorable. |

Proposal states are written out for the same reason: *Waiting for review*,
*Approved, not committed*, *Rejected* — rather than the words the database
uses for them.

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

### Finding the work on a busy page

Review holds two workloads: new files from your inbox, and suggestions about
files you already own. With 95 files waiting — measured on a real installation —
the page is fourteen screens tall and Library Review begins on the ninth. There
was nothing above it saying a second workload existed.

So the page opens with counts and links to each part of itself:

```text
New files 95    Existing library 13    Waiting for Commit 2    Dismissed 3
```

Deliberately links and not tabs. Tabs would hide one workload behind the other
and give Review a second kind of state to be in; this leaves both lists whole,
printable, linkable, and leaves each selection scope exactly where it was. A
workload with nothing in it gets no entry.

Named groups — an album, a season — fold away with the disclosure triangle on
their heading, and **Expand all groups** / **Collapse all groups** appear once
there are enough groups to be worth it. Collapsing is a view and never a
decision: a selection survives it, because a closed group still submits its
checkboxes. *Collapse group* and *Dismiss suggestion* are far apart in wording
for exactly that reason.

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
is behind **Evidence** on the row.

### Compare suggestions

"Why is it this?" and "what else could it be?" are the same moment, so the
second question lives at the bottom of the answer to the first. **Evidence →
Compare suggestions** asks every catalog and every AI provider you have switched on — each
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
*Compare suggestions* to choose an answer yourself, and *Re-analyse* to let
LibrAIry choose again.

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

### Video understanding

The same help for personal videos, and under one rule that does not bend: **the
model is shown images, never a video.** No clip is decoded for a model, no
container is uploaded, and the frame budget does not grow with duration.

Three tiers, cheapest first, and the first that answers wins:

1. **The photo beside it.** `IMG_0585.MOV` usually has `IMG_0585.jpeg` next to
   it, taken seconds apart, already described. That caption is free and often
   better evidence than a frame — the photo is the moment somebody chose. It is
   recorded as *context*, never as identity: *"paired phone photo"*, and a
   visual hint marked `from the paired photo, not from the video`. The still and
   the clip are not guaranteed to show the same thing.
2. **The thumbnail Browse already made.** If it is on disk, that is the frame.
   One inference, no extra decoding, nothing re-extracted.
3. **Three frames as one image.** Only when there is no thumbnail. 10%, 50% and
   90% of the duration, stacked into a single JPEG — one request with three
   frames costs far less on a local model than three requests with one. Three is
   the ceiling and it does not move; there is no scene detection and no "a few
   more for long videos".

Only phone-shaped clips are eligible: an `IMG_`/`VID_`/`PXL_` stem, and under
twenty minutes. A film has an identity a catalog should be answering for, and
decoding a frame out of a 40 GB remux to get a caption is the opposite of
lightweight.

**A frame is a hint, never an identity.** It is labelled that way in the
evidence — `read from 3 frames, which may not describe the whole clip` — and it
cannot decide a category. A frame showing a performer is equally consistent with
a family video, a concert bootleg and a DJ music video, and Music Video
classification stays where it belongs: filenames, tags, version parsing and
catalogs. What this does fix is the old failure where a `.MOV` from a phone went
to Movies because of its extension.

Names go through the same policy photos use — `IMG_0585.MOV` can become
`IMG_0585-dog-yard.MOV`, keeping the camera's sequence number, and a name you
chose is never overwritten. Answers cache against the file's fingerprint, the
provider and model, and the frame strategy, so an unchanged video is never
looked at twice and changing the strategy correctly invalidates the old answer.
Local providers only, exactly as for photos.

## Quarantine and the delete queue

Quarantine is not a bin. It is the out-tray: LibrAIry moved these files aside
because a decision is needed, or because an earlier operation put them there
safely. They are whole, unchanged, and still yours.

Every held file says four things: **why it is here**, where it is **now**, where
it **came from**, and — once you have decided — where it is going **after
Commit**.

Two decisions are available:

- **Restore** puts it back where it came from.
- **Delete queue** gathers it into one folder so you can empty that folder
  yourself.

Neither happens when you press it. Both write down a decision that waits for
Commit, exactly like a library correction, and the row immediately moves to
**Waiting for Commit** with **Cancel request** beside it. Cancelling is not
called Undo, because nothing has moved.

**Nothing in the delete queue is deleted.** LibrAIry has never deleted a file
and does not start here. Committing a delete-queue decision moves the file into
`quarantine/_to-delete/`, where it sits until you delete it yourself, in your
own file manager, deliberately. It can be restored from there too.

The tabs across the top — Held, Waiting for Commit, Delete queue, Put back —
are server-side filters and live in the URL, so reload and Back work, and the
counts are true however large the quarantine is.

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

## Music vs Music Videos

These are two different things and they get two different shapes on disk.

```text
Music         Music/Genre/Artist/Album/Track
Music Videos  Music Videos/Genre/Primary Artist/Video
```

**Album is the whole difference, and it is deliberate.** For an album of
FLACs, `Artist/Album/Track` is how the music was released and how you look for
it. For a DJ video collection it is a layer you fight: remixes, intro edits,
pool releases, mashups and bootlegs either belong to no album at all or to one
that has nothing to do with the file in front of you. Half the folders would
be a real release and half would be an invention, and nothing on screen would
tell you which.

So a music video is a **track, not an album**. Three levels, and that is
enough:

```text
Music Videos/
  Disco/
    Bee Gees/
      Bee-Gees-Night-Fever.mp4
  House/
    David Guetta/
      David-Guetta-feat.-Sia-Titanium-(Extended-Mix).mp4
      David-Guetta-feat.-Sia-Titanium-(DJ-Intro-Edit).mp4
```

`Music Videos/House/David Guetta/` should make sense over SSH with nothing
running. Everything richer than that lives in the index, where it can be
searched and filtered without becoming a directory:

```text
album           secondary genres     remixer
year            featured artists     version
BPM             clean / dirty        DJ pool source
catalog id      duration             original artist
```

**A music video is not a movie.** `.mp4` alone decides nothing — that is the
mistake that files phone clips as feature films. It is evidence, alongside the
source folder, a filename shaped like `Artist - Title`, a duration in the
right range, embedded tags, and a catalog match.

### Versions are identity, not noise

These are eight different files, and LibrAIry must not call them duplicates:

```text
Artist - Song.mp4
Artist - Song (Clean).mp4
Artist - Song (Dirty).mp4
Artist - Song (Extended Mix).mp4
Artist - Song (DJ Intro Edit).mp4
Artist - Song (Tiesto Remix).mp4
Artist - Song (Acapella).mp4
Artist - Song (Instrumental).mp4
```

Three distinct relationships:

| | Means |
|---|---|
| **Duplicate** | The same bytes. One hash, two paths. |
| **Possible duplicate** | Different bytes, near-identical content — a second download or a re-encode. |
| **Related version** | The same song, deliberately different. Never quarantined. |

Version markers survive the naming policy — the parentheses are kept — because
`(Clean)` and `(Dirty)` are the two things a DJ most needs to tell apart at a
glance.

### One primary genre, one folder

A track can be House *and* Dance *and* Electro House. It is filed **once**,
under one primary genre, and the rest are kept as metadata. Copying it into
three genre folders would be three files to keep in step forever.

Genre comes from your existing layout first, then reliable tags, then a
catalog, then the way the artist is already filed — and only then a model. A
catalog's genre vocabulary never reorganises the collection on its own:
catalogs disagree constantly about House, Dance, EDM, Electro House and Pop,
and the filesystem needs to sit still.

### Featured artists do not get folders

```text
David Guetta feat. Sia - Titanium.mp4
  → Music Videos/House/David Guetta/…      the primary artist
  ✗ Music Videos/House/David Guetta feat. Sia/
```

The second shape is how a collection grows a thousand one-off collaboration
folders. The complete credit stays in the filename, where it is searchable and
costs nothing.

If no artist can be established, the folder says `Unknown Artist` rather than
carrying a low-confidence guess that will outlive the guess. Library Review
surfaces those for correction.

### What Library Audit does not say about a music video

**A missing album is not a finding here.** Nor is a genre folder full of
videos by many artists a compilation — that is just the genre. The
compilation policy from Music does not apply; the videos organise under their
own artists.

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

## File type info

Beside every filename is a small **?**. It explains what that file extension
is and what such files are normally for — useful when a folder turns out to
contain `VTS_01_1.VOB`, `.BUP`, `.LRC` or something nobody recognises.

```
.IFO — DVD information file
Stores navigation and playback metadata for DVD-Video.
Part of a VIDEO_TS folder, alongside .VOB and .BUP files.
Structural DVD filenames are meaningful to players and are preserved
rather than tidied.
```

It appears in Review, Library Audit, Browse, Search, Quarantine, History,
item detail and the commit confirmation, and it says the same thing in all of
them because they read from one list.

**It is reference information only.** It describes the *format*, never the
contents: `.mp4` is a video container, which may be a family clip or a film,
and `.jpg` is an image rather than a photograph. It never changes a category,
a confidence, a destination, a filename or a proposal, and it never tells you
a file is safe to delete — including `.DS_Store`, which it describes and
leaves to you.

An extension it does not know says so plainly rather than guessing. Roughly 90
formats are covered; the list exists to remove the "what is this?" pause while
you organise, not to catalogue every format there is.

## Library Audit

Review asks *where should this new file go?* Library Audit asks a different
question about files you already own: **is this in the right place?**

They are kept apart on purpose. Filing a file you just dropped in is routine;
changing one you filed two years ago is not, so audit findings sit in their own
section of Review, every row is labelled **LIBRARY AUDIT** in words, and none of
the inbox bulk actions can reach them.

### Running one

Nothing runs on a timer. You press a button:

- **Browse → Audit this folder** on any folder you are looking at.
- **Browse → Audit the whole library** from the top.

or from a terminal:

```bash
librairy audit run --scope Music/Pop
```

```bash
librairy audit list
```

Reading embedded tags costs roughly 30 ms a file, so a folder answers in about a
second and a whole large library belongs on the command line. `--no-tags` skips
the tag reading and runs on filesystem and index evidence alone.

### What it will never do

An audit **reads**. It writes findings and nothing else — it does not rename,
move, delete, re-index, add artwork to your library, or quarantine anything,
and that holds for a finding it is completely certain about. Your library is
read-only input while it runs, exactly as it is during Browse.

### It runs in the background, behind your inbox

Pressing **Audit** queues the work and returns immediately; the worker that
already files your inbox picks it up. The ordering is the guarantee: the
worker does its own cycle first — scan, duplicates, analyse — and only spends
time on the library if that cycle found nothing to do. **A file dropped in
your inbox is never behind a library reconciliation.**

The work happens in short slices, so the inbox gets a look in between each
one, and Review shows how far it has got:

```text
Library audit          Reading metadata                 64%
████████████████░░░░░░░░
89 of 140 files read

Music        48 / 48       Photos    31 / 89
Albums       28            Catalog requests  2
Issues found 5            Sent to AI        0 / 1

Runs in the background. New inbox work takes priority.
```

The percentage is whatever the current stage is actually counting through —
files read, collections checked, albums checked for artwork — and where a
stage counts nothing (scanning is one directory walk) there is no bar at all
and the panel says which step it is on. It deliberately does not show a single
overall number: the stages cost wildly different amounts, so "38%" derived
from being three stages into eight would sit unchanged through the entire slow
half.

If the panel stops moving it says why — *Waiting while the inbox is
processed* — and that is recorded by the worker when it chooses the inbox,
never guessed from how long it has been.

Two audits can be waiting, and the more specific one goes first:

```text
1.  Your inbox, and anything you are doing        always wins
2.  Audit this folder                             you asked about something
3.  Audit the whole library                       maintenance
```

**Stop the audit** ends it wherever it stands. That is safe by construction
rather than by care: an audit only reads, so there is nothing half-done to
unwind.

Nothing is scheduled. There is no audit daemon, no timer and no second
process — an audit exists because you asked for one. Re-running is cheap:
what a catalog said is written down and read back rather than asked again, so
a second audit of an unchanged library makes **no catalog requests at all**.

### What it looks for

| Finding | Means |
|---|---|
| **Unexpected file type** | A PDF under Music, a spreadsheet under Movies. |
| **Loose file** | Filed shallower than the rest of your library files things. |
| **Naming inconsistency** | A folder capitalised unlike the ones beside it. |
| **Tags disagree with the folder** | Embedded artist tags point at an artist folder you already have elsewhere. |
| **Possible duplicate** | Identical bytes in two places. |
| **Missing artwork** | An album with tracks, no cover file and no picture inside the files — reported once per album, not once per folder. |
| **Artwork is embedded but not on disk** | The album does have a picture; it is inside the tracks rather than beside them. Some players show it, some do not. |
| **Not indexed** | On disk but never scanned, so Search cannot see it. |
| **System file** | `.DS_Store` and friends. Reported, never deleted. |
| **One album in several folders** | One artist's release split across two folders — usually a half-finished copy or a second rip. |
| **Recognized compilation** | Many artists, and a catalog names the release. Keep it together. |
| **Custom compilation** | Many artists, the files describe one release, no catalog has heard of it. Keep it together, or organise the tracks individually. |
| **Loose collection** | Many artists and no reliable release identity. The tracks belong under their own artists. |
| **Artist filed in two places** | The same artist has folders under two different sections. |
| **Folder name disagrees with the tags** | Every track says one album name; the folder says another. |
| **Tracks missing from an album** | A numbered album with a hole in the middle. |
| **Named unlike its neighbours** | One file in a folder where every other file follows a pattern. |
| **Loose tracks beside album folders** | Tracks directly in an artist folder that otherwise uses albums. |
| **A catalog spells this differently** | The tags *and* an outside catalog agree on a spelling the folder does not use. |

### Folders with many artists in them

This is the one case where "tidy this up" has two opposite right answers.
*Now That's What I Call Music 42* is a release: it has a catalogue number, a
cover and a running order, and splitting it into forty artist folders is
vandalism. `Stuff for the car` is not a release: it is a folder somebody
filled once, and keeping it whole hides forty artists from where they belong.

Nothing about the folder tells you which one you have — both are a directory
with tracks by many people in it. So LibrAIry asks three questions, in order
of what each one proves:

1. **Does a catalog know this release?** MusicBrainz and Discogs are both
   asked, by barcode first and then by exact title. A release id is external
   and checkable, and it beats everything below.
2. **Do the files agree with each other?** Forty-five tracks that all name the
   same album, number themselves 1 to 45 with no gaps and no repeats, and
   carry one barcode and one cover between them are describing a release, even
   if no catalog has heard of it. This is weaker, because the files wrote it
   about themselves — so a contradiction cancels it outright. Ten tracks all
   numbered 1 look coherent until you count them.
3. **Have you already decided?** **Leave current layout** on this folder is
   remembered,
   and it is the strongest evidence there is, because it is the only kind that
   knows what the folder is *for*.

Three answers, and only the last one takes anything apart:

```text
Recognized compilation   45 tracks · 1.4 GB · 27 artists
                         MusicBrainz and Discogs identify this as one release.
                         Suggested: Music/Pop/Various Artists/<the release>/

Custom compilation       45 tracks · 1.4 GB · 27 artists
                         The files consistently describe one collection, but no
                         configured catalog recognises the release.
                         Keep it together, or organise the tracks individually.

Loose collection         45 tracks · 1.4 GB · 27 artists
                         No reliable album identity was found. The tracks belong
                         under their own artist and album.
```

**A collection folder is never inherited into every artist's hierarchy.** That
shape — `Abba/Best Road Trip Disco Fever Classics/`, `Bee Gees/Best Road Trip
Disco Fever Classics/`, twenty-seven times — is the worst of both structures:
the album is not together, *and* every artist folder now claims a release that
does not exist. Either the collection is real and lives in one folder, or it is
not and the tracks go to their own albums. There is no third shape.

If your library already keeps compilations somewhere — a `Compilations/` or a
`Various Artists/` folder — that name is used. If it does not, `Various
Artists` is suggested as a starting point you can decline.

Nothing here is executed for you. Gathering forty-five files out of
twenty-seven directories is a subtree restructure, and Review shows the answer
rather than offering a button that would perform it.

### How much it looks at

Stages, in the order they earn their cost, and each can be absent without
changing the answers of the ones before it. Nothing waits on MusicBrainz to
discover it has a `.DS_Store`, and nothing asks a language model a question a
hash already answered:

| Stage | Reads | Cost |
|---|---|---|
| **Scanning** | The filesystem and the index | Microseconds. Always. |
| **Reading metadata** | Embedded tags | ~30 ms a file. Skip with `--no-tags`. |
| **Structure and convention** | What the first two found | Free. |
| **Catalogs** | MusicBrainz and Discogs, and what they said last time | One request per album, once. |
| **Artwork** | Covers on disk, then pictures in the tags, then Cover Art Archive | Free — the tag read already saw them. |
| **Duplicates** | Hashes the index already holds | A group-by. Nothing is re-hashed. |
| **AI** | Only what nothing above could resolve | Usually nothing at all. |

A compilation has no artist to search by, and asking a catalog about an artist
called `V.A.` returns whatever happens to be named that — so those releases
are looked up by **barcode** and by **exact title with a matching track
count** instead. The verification is not optional: asked for *Best Road Trip
Disco Fever Classics*, MusicBrainz returns *Road Trip Classics* at full
relevance score, and taking that would invent an official identity for a
collection that has none.

The AI stage runs only when something is genuinely unresolved, and if nothing
is it does not run at all. What reaches it today is the custom compilation —
the case where the catalogs had no answer and the files are only speaking for
themselves. What comes back is **evidence, not a verdict**: a line in Why
beside the tags and the catalogs. A model cannot promote a collection to
*recognized*; only a release id does that. If the model is unreachable the
audit finishes and says so — `Sent to AI  0 / 3` is a successful audit telling
you which part of itself was missing.

The catalog tier runs **only when you press Audit**. Browsing never queries
anything, nothing polls on a timer, and there is no background audit service.
What a catalog answers is written down — the release id, the canonical names —
so the next audit reads it instead of asking again, and a failure to match is
remembered too, for a shorter time. A catalog that is switched off, unreachable
or slow degrades to *no answer*: the audit still succeeds, and every finding
above that tier is unaffected.

A catalog never wins an argument on its own. It gets a say only when the
embedded tags already agree with it **against** the folder — three witnesses,
and only the folder-alone case means anything:

```text
folder differs, tags and catalog agree   →  reported.  JAMES BROWN → James Brown
folder and tags agree, catalog differs   →  nothing.   ABBA stays ABBA
folder and catalog agree, tags differ    →  nothing.
```

The real library shows why the middle row matters. Its one non-compilation album
matches a MusicBrainz release whose canonical title is *Unplugged: 20th
Anniversary*, while the folder and every tag in it say *Unplugged (20th
Anniversary)*. The catalog is outvoted and nothing is suggested.

### Why it says so little

A audit that reports eight hundred harmless style differences gets ignored, and
then it is protecting nothing. So **your layout is treated as evidence, not as a
mistake**: `Music/Pop/Abba/` is your convention even though a catalog would call
Abba disco, and genre disagreement alone never moves anything. A library with no
consistent depth has no convention to be inconsistent with, so it gets no *loose
file* findings at all. A library that shouts throughout is a style, not 400
naming problems.

Against a real 140-file library, a full audit reports **six** things — and the
grouping is doing most of that work. Forty-five of its forty-eight music tracks
are one compilation filed as twenty-seven artist folders, which is **one** row
saying so, not twenty-seven and not forty-five. The same split would otherwise
have produced a *tracks missing from an album* row for every one of those
folders, since each is "missing" forty of the forty-five numbers; a finding that
explains another one suppresses it.

A row that speaks for several places says so, and lists them:

```text
▸ Spans 27 folders          ▸ 2 identical copies
```

### Reading a row

Every row answers five questions in this order:

```text
☐  LIBRARY AUDIT   Correction   Tags disagree with the folder   Worth acting on

   05 - Song.flac  [?]
   Tagged 'Queen' but filed under 'Pop'.

   CURRENT     Music/Pop/Queen/05 - Song.flac
   SUGGESTED   Music/Rock/Queen/A Night at the Opera/05 - Song.flac

   ▇▇▇▇▇▇▇▇   Embedded tags · On disk · Your library

   [Approve change] [Dismiss suggestion] [⋯]   Preview  Evidence
   ▸ Moves 2 files
```

The chip after **LIBRARY AUDIT** says what kind of row it is: *Ready to
approve*, *Observation*, *Cannot be applied*, *Needs analysis again*, *Not on
disk*, *Waiting for Commit*, *Corrected* or *Dismissed*. **Worth acting on**
appears only where the audit thinks so.

That chip is not decoration and not a summary of which buttons rendered. It is
the single value every control on the row is derived from, and only *Ready to
approve* can be approved — the checkbox on any other row is disabled, and the
row says in a sentence why. This used to be inferred: a folder-naming
observation had an ordinary enabled checkbox, and the toolbar quietly disabled
its own Approve button when nothing eligible was selected. Pressing a disabled
button produces no request and no message, so "I approved it and nothing
happened" was a completely accurate description of the software.

The bar is **evidence composition, not confidence**. Unlike an inbox proposal,
an audit finding has no single score — see below.

### Answering a finding

Three answers, and one of them is always available.

**Approve change** approves one specific library → library move. Nothing
happens on approval except that a plan is written down — the files move when you
press Commit, and not before. The row leaves the undecided list and appears
under **Waiting for Commit**, where **Remove approval** takes the decision back.
That is deliberately not called *Undo*: nothing has moved, so there is nothing
to reverse, and one word for both is how somebody comes to believe Undo will
rescue a commit they never made.

**Dismiss suggestion** records that you do not want this suggestion. The next
audit leaves it alone unless the file itself changes, so the same question does
not come back every week — and it is **reversible**: the row moves to a
collapsed **Dismissed** group at the foot of Library Review with a **Restore
suggestion** button. Nothing is deleted.

The three structural choices for a multi-artist folder — **Keep together**,
**Organize individually**, **Leave current layout** — are *not* dismissals and
keep their own names. Those are three different libraries.

**Analyse again** appears when a finding has gone stale. See below.

Once a correction is approved, the approval is what LibrAIry believes — not the
row's own bookkeeping. A finding whose stored status says one thing while an
approved plan says another always reads as **Waiting for Commit**, and never
offers a second approval: approving twice would write a second plan over the
same files and leave the first one orphaned. If the two ever disagree,
`librairy db check` says so; see [Troubleshooting](troubleshooting.md).

An approval can also go **out of date**. The plan records exactly which bytes
were to move, so if one of those files is edited or moved by hand between
approval and Commit, the row says *Approval is outdated* and the Commit button
is withdrawn — the executor would refuse the whole correction anyway, rather
than apply half an album. **Remove old approval** puts it back in Review, where
a fresh audit can look at whatever the files are now.

Behind **⋯**: *Open in Browse*, which opens the folder the file is in **now**
rather than any suggested destination, and *View details* when the file is
indexed. Neither appears when it would not work.

**Preview** shows the file as it is today, through the same preview card,
fullscreen viewer and video handling as the inbox queue. There is no preview for
a folder-level finding or for a file that is not on disk.

### Selecting several at once

Tick the box on any row for a toolbar with **Approve selected changes**,
**Dismiss suggestions** and **Analyse again**. It is a separate selection from
the inbox queue's, and deliberately so: the two post different fields to
different endpoints, so an inbox bulk action cannot reach a library finding and
vice versa.

Only approvable rows have an enabled checkbox, so a selection of things that
cannot be approved is not expressible. Every bulk result names what happened to
each row rather than counting successes:

```text
Selected: 4 · Approved: 2 · Already waiting for Commit: 1 · Observation only: 1
```

There is no "approve everything above N%" — inbox files and existing-library
corrections do not carry the same risk.

### What the audit knows, and what it does not

An inbox proposal has a confidence: *how sure am I this is where this new file
belongs?* An audit finding has **no equivalent number**, and the page does not
invent one.

What is recorded is a weight per piece of evidence — how much that one signal is
trusted — and those are shown individually in **Why**. They are never added
together, because there is no scoring model behind the audit that would make the
total mean anything. The bar on the row shows the *mix* of evidence kinds at a
fixed width; its length is not a score. A finding is either a *Correction* or an
*Observation*, and that distinction carries the weight a percentage would carry
elsewhere.

### Stale findings

A finding is a statement about a specific file at a specific moment: *this file,
with this hash, is in the wrong place*. Between the audit and the button you may
have re-tagged it, replaced it, renamed it or deleted it — and a correction that
executed anyway would move a file nobody looked at.

So every finding carries the fingerprint it was made against, and the row says
which of three things is true:

| State | Means | Offered |
|---|---|---|
| **Ready to approve** | The file is exactly as audited. | Approve change, Dismiss suggestion |
| **Needs analysis again** | The file changed after the audit. | Analyse again, Dismiss suggestion |
| **Not on disk** | Nothing is at the audited path any more. | Dismiss suggestion |

Timestamps are not used for this. Copying a library between disks rewrites every
mtime without changing a byte, and a file can be replaced with its timestamps
preserved — so the test is the hash, the same one Commit uses. **Re-audit** looks
at the file again and records what is true now; it never quietly rewrites an old
finding to match a new file.

### Naming

Library Review checks every file and folder name against LibrAIry's own naming
rules — the same module that names files it is filing, not a second standard.

That policy has two halves, and only one of them is audited.

**Hygiene is audited.** These say a name is *damaged*, and none of them needs
tags or a catalog to be certain about:

| Rule | `  Queen` → `Queen` |
|---|---|
| Leading and trailing spaces | `Queen  ` → `Queen` |
| Repeated spaces | `Queen  Live` → `Queen Live` |
| A space before the extension | `Song .flac` → `Song.flac` |
| Tabs, newlines, unusual and invisible spaces | joined back into one space |
| Emoji and other symbols | `🔥 Song 🔥.mp3` → `Song.mp3` |
| Typographic quotes, `"` and backtick | `“Fancy”.pdf` → `Fancy.pdf` |
| Characters Windows and SMB reject — `< > : \| ? *` | replaced with `-` |
| Reserved device names | `CON.txt` → `CON_.txt` |
| Trailing dots | `Title...` → `Title` |
| Decomposed Unicode, over-long names | normalised, trimmed |

**House style is not audited.** When LibrAIry names a file it is filing, it
also turns spaces into dashes, drops apostrophes and spells `&` as `and`:
`A Night at the Opera` becomes `A-Night-at-the-Opera`. That is right for a
name being invented and wrong as a verdict on a library you already organised
— against a real 140-file library it would rewrite **118 of them**. Your
layout is evidence, not a mistake.

The ASCII apostrophe is the one place the audit is deliberately narrower than
the sanitizer. `Guns N' Roses` and `You're My Best Friend` are correct names,
legal on every filesystem LibrAIry runs on, and flagging them would turn right
names into wrong ones.

**Capitals are never a rule.** `str.isupper()` is not a defect — `ABBA`,
`MF DOOM`, `NASA` and `AC/DC` are all correct. A shouting folder is checked
against the tags of the files inside it:

```text
folder JAMES BROWN + tags say "James Brown"  → correction, with a suggestion
folder ABBA        + tags say "ABBA"          → nothing at all
folder SHOUTY BAND + no tags                  → observation, no suggestion
```

**Disc structures are never touched.** `VIDEO_TS`, `VTS_01_1.VOB` and their
siblings are a contract with a player, not a description of anything.

One bad name is one finding. A folder called `  Vacation 2022 🔥 ` holding
forty files produces a single row, not forty.

### Which findings can be corrected

Only kinds whose correction is a concrete, deterministic move:

| Executable | Observation only |
|---|---|
| Tags disagree with the folder, naming cleanup on a **file**, naming inconsistency on a **folder** | Possible duplicate, missing artwork, not indexed, system file, unexpected file type, loose file, one album in several folders, artist filed in two places, folder name disagrees with the tags |

The observations are all true and worth showing; none has a move that answers
it. *Missing artwork* describes a file that is exactly where it belongs.
*Possible duplicate* has a real answer, quarantining the copy, but that is a
different action with its own safety rules and it is not offered here yet.
*One album in several folders* and *artist filed in two places* propose a merge
or a choice, not a rename — which of two spellings is right is a judgement, and
merging asks a question no rule answers, such as which of two `cover.jpg` files
wins.

### Renaming a folder

A folder is not a thing that can be corrected. The directory entry is one name;
what you mean by "fix that folder" is the files underneath it. So approving one
builds the moves it actually is — one operation per file, at any depth — and the
folder disappearing afterwards is what is left when they have all gone.

```text
Naming inconsistency                      Ready to approve
Lipps Inc.
Ends in a dot, which Windows silently drops.
Music/Pop/ Lipps Inc. → Lipps Inc

Affects  14 files · 620 MB · everything in this folder
  Moves 14 files ▸

[Approve change] [Dismiss suggestion] [⋯]
```

It is refused, on the row and in words, when any of this is true:

| Refused | Because |
|---|---|
| The new name already exists as a folder | Merging two folders asks questions renaming one does not — which `cover.jpg` wins. |
| Only the capitalisation changes, on a Mac or Windows volume | The destination *is* the source there, so each file would move onto itself. On a case-sensitive volume the same rename works. |
| A file inside is not indexed, has changed, or is gone | The finding describes the folder as it was checked. If that is not what is there, the suggestion is out of date, not smaller. |
| A file inside is already waiting for Commit | Two approved plans would each believe they know where it is. |
| The folder is protected, or holds a disc structure | Neither moves, by rule. |
| More than 200 files | A plan is a list you read before approving it. Past a couple of hundred rows, approving stops being a decision. |

Commit, History and Undo treat it exactly like any other correction — one plan,
all-or-nothing, one History line, one Undo that puts every file back where it
was. Undoing also takes away the folder the commit created, so "put it back"
does not leave something behind that was not there before.

### Companions travel with their media

A correction that moved `05 - Song.flac` and left `05 - Song.lrc` behind would
break something you did not ask to have broken. So a correction resolves the
whole group first, and every file in it is listed in Review before you accept:

```text
Correction will move 4 files

  primary     05 - Song.flac
  companion   05 - Song.lrc      — named after 05 - Song.flac
  companion   album.nfo          — belongs to the folder, and nothing else is staying in it
  companion   cover.jpg          — belongs to the folder, and nothing else is staying in it
```

Two kinds of belonging, and LibrAIry only claims the ones it can prove:

- **Named after the file** — `Song.lrc`, `Movie.en.forced.srt`, `Movie.nfo`.
  A player finds these by filename, so they follow the primary's final name and
  keep whatever their name adds. `.en.forced` is the only thing telling two
  subtitles apart, so it survives.
- **Named after the folder** — `cover.jpg`, `playlist.m3u`, `Album.cue`. These
  describe the release, not the track, so they travel **only when the folder is
  emptying**. Moving one track out of a ten-track album never takes the album's
  cover with it.

Being nearby is never the evidence. A DVD or Blu-ray structure is refused
outright: `VIDEO_TS.IFO` points at its siblings by name and position, so a
correction that lifted one `.VOB` out would produce two broken things instead of
one tidy one.

### What happens at Commit

Corrections appear on the Commit page under **Library corrections**, counted and
listed separately from new files, with every move spelled out. Each commits on
its own. Each is headed by what it is about — the album, not a plan id — and
says where the file is now, where it will be after Commit, how many files and
how much data it affects, and how long ago you approved it, before the button
rather than one click behind it. **Send back to Review** returns the decision if
you change your mind; nothing has moved yet, so that is not called Undo. If you
have approved and withdrawn this same correction before, the card says so.

A correction whose files changed after you approved it shows *Approval is
outdated* and offers **Remove old approval** in place of Commit.

While it runs the page says what is happening in words — *1 file moved. Nothing
failed.* — and lists only the outcomes that actually occurred. There is no
percentage, because no honest one exists: files are copied and verified one at a
time, so a count and a stage is all there is to report. When it finishes the
page offers **View in Browse**, **View History** and **Undo**, which is the same
hash-verified reversal History uses. There is one undo.

Committing the last thing in the queue does not erase what you just did. The
Commit page keeps a **Last completed** line — what moved, how many, when — with
History and Undo beside it, and the Commit tab stays in the navigation whether
or not anything is waiting.

Immediately before anything moves, every file in the group is re-checked against
the fingerprint that was approved. If any of them has changed, **none of them
move** — a correction you approved as one action stays one action, and half an
album in its new home is worse than either outcome. The finding goes back to
open so the next audit can look at whatever state things are actually in. A
group that partly fails is never reported as a success.

History labels the result **Library correction · moved 4 files** rather than
*Filed*, and every file is undoable — back to its exact original path, with its
exact original bytes.

### What it will never do

Corrections do not run on a timer, are never approved automatically, and are
never included in an inbox bulk action. *Approve 40 at 85%+* acts on checkboxes
inside the inbox form; audit findings live in their own table and render outside
that form, so the separation is structural rather than a filter someone has to
remember.
