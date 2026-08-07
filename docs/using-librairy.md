# Using LibrAIry

1. Open the portal. It goes straight to the dashboard; setting a password is optional (Settings -> Portal Security).
2. Drop files or folders into the inbox host path. Nothing to press — the
   worker notices within a couple of seconds.
3. Watch the activity pill in the header, on any page, for what it is doing.
4. Open Review, approve/edit/reject/postpone proposals.
5. Open Commit, create a plan, inspect it, then execute.
6. Use Search and Browse to find indexed files.
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

## Duplicates

Exact duplicates are staged for reversible quarantine review. Similar media flags
are informational and require human judgment. LibrAIry never deletes duplicate
files.

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
