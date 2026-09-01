# Restoring, and agreeing about what you have

A backup tool puts bytes and a database back. Whether they still describe each
other is a different question, and it is LibrAIry's rather than the backup
tool's — no amount of teaching rclone about individual tables would answer it,
because the failure is that two snapshots of two moments have been placed side
by side.

So the responsibility splits:

```
what do I currently have?
      ↓
which of my persisted facts are still true?
      ↓
which derived facts can I safely rebuild?
      ↓
which ambiguities require a person?
```

## Three kinds of persisted state

Treating them alike is how a restore either destroys a decision somebody made
or trusts a cache describing bytes that are gone.

| | examples | what a restore may do |
| --- | --- | --- |
| **Authoritative** | History, Format Policy, Decision Memory, suppressions, withdrawals, recognised moves, committed plan provenance | nothing. Nobody can regenerate a record of what a person chose |
| **Derived** | the search index, audit findings, similarity findings, relationship discovery | rebuild freely. Losing it costs time, not information |
| **Fingerprint-bound** | `item_metadata`, `track_identity`, `content_extractions`, `vision_results` | check against the fingerprint they were measured from, and treat a mismatch as a miss |

The third is the dangerous one. Silently attaching last month's identity to
this month's bytes is worse than having no identity at all, so every reader of
those tables gates on the fingerprint first — which is what makes a stale row a
*miss* rather than a wrong answer.

## Validation

`Reconcile` reports; it never repairs. It opens no file, runs no subprocess,
calls no provider and writes nothing at all.

It is index-first, and says so. Hashing a whole library on a page load would
take hours on the libraries this matters most for, and the scanner already
re-hashes anything whose size or modification time moved. The honest order is
**scan, then validate**, and the page says *scan first* when the walk and the
index disagree about which files exist.

What it reports:

- files still where the index says, with the bytes it says
- files whose bytes are on disk **somewhere else** — a move, not a loss
- files whose bytes are in **several** places — nobody's to guess
- files nothing on disk holds
- measurements taken from bytes that have since changed
- held files quarantine's records no longer describe
- decisions that were waiting for Commit, classified and left alone
- what could not be regenerated, counted, so you can see it survived

## A path mismatch is not data loss

If the index expects `Music/Rock/Queen/Album/song.mp3` and exactly those bytes
are at `Music/Queen/Album/song.mp3`, nothing is lost. Reporting that as missing
is how a successful restore looks like a catastrophe.

**Only the fingerprint may say so.** Not a matching filename, not a matching
size, not a similar title — a reconciliation built on any of those would attach
one file's history to another file, which is the worst thing this could do.

Where identical bytes exist in more than one place, the answer is *ambiguous*
and stays that way. Picking the alphabetically first copy would be a guess
wearing a decision's clothes.

## Recognising a move changes an understanding, not a location

```
Music/Rock/Talking Heads/Remain in Light   ← what LibrAIry recorded
        → Music/Talking Heads/Remain in Light   ← where the files are
                                       [Recognize new location]
```

Pressing it moves **zero bytes**. One `items` row changes its path and stops
being missing, and everything referencing that row follows for free, because
identity was never the path:

| | after recognition |
| --- | --- |
| measured metadata, catalog identity | still valid — the bytes did not change |
| relationships | survive; they were never about the path |
| Search | that one row is refreshed, not the whole index |
| Format Policy | re-resolves from the **new** path. A file moved into a preserve-originals folder is protected afterwards |
| approved plans naming the old path | go **stale**. Never rewritten to point somewhere nobody approved |
| History | unchanged. It keeps the paths its operations actually used |
| Undo | answered by the existing preflight against what is on disk now. If the file is not where the journal left it, Undo declines and says so |

It also never moves the file back to where LibrAIry would have filed it. The
person put it there deliberately; a program that quietly undid that would be
enforcing a taxonomy rather than keeping an index. If a later audit thinks the
location is unusual, that is a separate observation.

A folder is offered as **one** decision only when the correspondence is
complete: every missing file in the old folder has an unambiguous partner, and
every partner landed in the same new folder under the same name. One member in
doubt and the folder is not offered at all — the rest are still there to
recognise one at a time, which is the version that cannot be wrong.

Recognition is refused outright when the file at the new path is not a
stranger. A row carrying an operation, a quarantine record, a remembered
decision or an optimization job is a second identity, and merging two would
destroy whichever lost.

## What the backup itself still does not do

Backup copies files to the remote **by path**, and takes a SQLite snapshot
after a run that copied something. Two consequences worth knowing:

- the database snapshot and the file copies are two moments, not one. Nothing
  claims otherwise, and reconciliation exists because they can differ.
- a file moved outside LibrAIry is copied again under its new path, and the old
  copy stays on the remote until you remove it.

Neither is changed by this pass. See [Backup and restore](../backup-restore.md)
for the procedure, and [Health](health.md) for where a pending reconciliation
is counted.
