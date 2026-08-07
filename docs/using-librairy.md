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

Any Review row where the file may already be in your library is marked **you may
already have this** and gains a **Compare the two copies** button. It opens the
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
