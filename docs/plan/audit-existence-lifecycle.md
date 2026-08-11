# Audit: six ways to say a file exists

Phase 1 of the Search consistency work. Written before touching a query, by
tracing call paths and by reading the author's real database through a private
copy. No AI provider, catalog or model was involved in producing any of it.

Browse now answers "what is on disk". This note is about the other side: after
that fix, LibrAIry holds **six separate representations of a file existing**,
and their relationships were never written down. Search returning a file that
was deleted a week ago is one symptom of that, not a bug on its own.

## The six

| Representation | Table / source | What it really claims |
|---|---|---|
| physical file | filesystem | It is there right now. |
| item row | `items` | LibrAIry has seen it, and knows its size, mtime and fingerprint. |
| search entry | `search_fts` | Its words are in the search index. |
| `missing_since` | `items` column | A scan of that root looked and did not find it. |
| proposal | `proposals` | Somebody or something decided where it should go. |
| history | `history` | An operation happened to this path. |

## The pipeline, with the actual call sites

```
filesystem
   │  scan_root()           the only thing that creates an items row
   ▼
items ──sync_search_item()──▶ search_fts
   │
   │  analyze_items()       reads items, writes proposals
   ▼
proposals ──plan, approve──▶ executor.commit
                                │  moves the file, UPDATEs the item row's
                                │  root+relpath, clears missing_since
                                ▼
                             library
                                │  a later scan_root('library')
                                ▼
                             missing_since set, if it is not there any more
```

### When an items row is created

`scanner.scan_root()`, and nowhere else — one `INSERT … ON CONFLICT(root,
relpath) DO UPDATE`. Every other stage *moves* a row: the executor, quarantine
restore and history undo all `UPDATE items SET root=?, relpath=?`, keeping the
same id. That is why an item id survives a commit, and why History can still
point at it.

### When a search entry is created, updated, removed

`search.sync_search_item(conn, item_id)` — `DELETE` the row for that id, then
`INSERT` it again from a join across `items`, the live proposal, its group and
any vision result. It is called from six places: `scan_root` (every file it
touches), `upsert_proposal` / `supersede_proposal` / `reject_proposal`,
`executor._move_item_row`, quarantine send and restore, and Review's edit path.

**Nothing ever deletes a search entry permanently, and nothing deletes an items
row at all.** `rebuild_search_index` clears the whole table and refills it from
`items`. So in a settled database, `search_fts` has exactly one row per item,
and the real database confirms it: **243 items, 243 search entries.**

### What sets `missing_since`

One place: `scanner._mark_missing`, at the end of `scan_root`. It lists every
row of that root that currently has `missing_since IS NULL`, and stamps the
ones the walk did not see. It never deletes.

Note the scope: **a scan only reconciles the root it scanned.** `scan --root
library` cannot mark an inbox row missing, and the background worker only ever
scans the inbox.

### What clears it

Four places, all of them "the file is here, I just touched it":

- `scan_root`'s upsert (`missing_since=NULL` in the DO UPDATE)
- `executor._move_item_row` after a commit
- `quarantine.restore_entry`
- history undo

So a returning file needs no repair — the next scan of its root clears the flag
as a side effect of finding it.

### Who filters on it

| Surface | Filters `missing_since IS NULL`? | Where |
|---|---|---|
| Review queue | ✅ | `web/review.py` ×3 |
| Commit / plan | ✅ | `web/commit.py` ×4, `planner.py` ×2, `web/app.py` |
| Dedup, duplicates, catalog probe, content extract, backup, indexer | ✅ | each in its own query |
| Companions / sidecars | ✅ | `classify/companions.py` ×4 |
| Dashboard counts | ✅ | `lifecycle.vanished_count` counts them *deliberately*, so the totals add up |
| **Search** | ❌ **nothing** | `search.py` has no reference to the column at all |
| Browse | n/a | existence comes from disk; a stale row simply fails to join |
| History | n/a, by design | it records what happened, not what is |

Search is the only surface that was never told. That is the whole bug.

## The bug, on the author's real data

Five files were dropped into the inbox on 2026-08-05 for a drill and removed
afterwards. A scan stamped them `missing_since`. Today, live:

```
GET /browse?q=Test.Show&root=all   →  6 results

   inbox/_drop/Test.Show.S01E02.1080p.mkv      missing since 2026-08-05
   inbox/_drop/Test.Show.S01E04.1080p.mkv      missing since 2026-08-05
   inbox/_drop/Test.Show.S01E03.1080p.mkv      missing since 2026-08-05
   inbox/_drop/Test.Show.S01E01.1080p.mkv      missing since 2026-08-05
   inbox/_drop/Test.Show.S01E05.1080p.mkv      missing since 2026-08-05
   inbox/_librairy-duplicate-test/half-size.png   ← the only one that exists
```

Five ghosts and one file, rendered identically — same thumbnail slot, same
size, same category badge, same "goes to" destination.

## The eight stale inbox rows

All eight, read-only, unaltered:

| id | path | missing since | search entry | state | proposal | history |
|---|---|---|---|---|---|---|
| 239 | `_drop/Test.Show.S01E01.1080p.mkv` | 2026-08-05 19:56 | yes | proposed | proposed | 0 |
| 237 | `_drop/Test.Show.S01E02.1080p.mkv` | 2026-08-05 19:56 | yes | proposed | proposed | 0 |
| 235 | `_drop/Test.Show.S01E03.1080p.mkv` | 2026-08-05 19:56 | yes | proposed | proposed | 0 |
| 238 | `_drop/Test.Show.S01E04.1080p.mkv` | 2026-08-05 19:56 | yes | pending | rejected | 0 |
| 236 | `_drop/Test.Show.S01E05.1080p.mkv` | 2026-08-05 19:56 | yes | approved | approved | 0 |
| 232 | `_p15drill/Breaking.Bad.S05E14…mkv` | 2026-08-05 18:48 | yes | proposed | proposed | 0 |
| 233 | `_p15drill/The.Expanse.S02E06.720p.mkv` | 2026-08-05 18:48 | yes | proposed | proposed | 0 |
| 234 | `_p15drill/dq.mp3` | 2026-08-05 18:48 | yes | proposed | proposed | 0 |

They are **drill fixtures**: `_p15drill/` from the phase-15 catalog work at
18:48, `_drop/` from a review-flow exercise at 19:56, both deleted from the
inbox by hand the same evening. Zero history rows, because none was ever
committed — nothing moved, so there was nothing to journal.

Why they remain is not an accident and not a leak. `_mark_missing` stamps and
keeps, on purpose: a missing file is usually an unmounted disk, and the same
comment appears on `lifecycle.forget_vanished`, which drops the *proposals* for
vanished files and is deliberately manual for exactly that reason. Those eight
rows still carry a rejection, an approval and six classification results — the
audit trail for decisions the owner made. Deleting them because a file is gone
would throw that away to tidy a number.

So: **retain the record, exclude it from anything that claims the file is
here.** Review and Commit already do. Search has to.

## The item with no search entry

Item 171, `Queen … DVD5/VIDEO_TS/VIDEO_TS.IFO`, was reported in the last audit
as the only item lacking a `search_fts` row. **It is not a bug, and there is
nothing to fix.**

Checked again on a fresh copy: item 171 has a search entry, indexed as
`category=movies`, and so do all nine VIDEO_TS files in that disc — `.IFO`,
`.BUP` and `.VOB` alike. `items` 243, `search_fts` 243. An independent snapshot
from 2026-08-07 also reads 243/243.

The copy that showed 242 was taken minutes after the container had been killed
mid-write by a full Docker disk, with a rollback journal still on the file, and
`cp` of a live 1 MB database is not atomic. `sync_search_item` is a `DELETE`
followed by an `INSERT`, so a torn copy landing between the two produces
exactly one item with no search entry. The artefact was in my snapshot, not in
the database.

Worth stating positively, since the question was whether FTS is optional for
structural files: **it is not.** `sync_search_item` has no extension filter and
no early return other than "the item does not exist". Every item gets an entry,
`.IFO` included, and the disc's filenames are preserved verbatim in it. A
regression test now covers that so the contract stops being folklore.

## The design choice for stale search entries

Keep the entry, filter at query time — **Option A**, and the alternative is
worse on every axis that was checked:

- **Reappearance.** Clearing the entry would need something to put it back.
  The only writer is `sync_search_item`, called by `scan_root`, so a returning
  file would work — but only through a scan, and only if that new call site
  were correct. Filtering needs no new lifecycle at all: the row was never
  wrong, only the query.
- **Rebuild.** `rebuild_search_index` refills from `items`. If missing items
  were meant to have no entry, rebuild would have to learn that too, and a
  rebuild would then silently change what a stale row means.
- **Cost.** `items` is already joined in both search queries. The filter is one
  clause on a column with 243 rows.
- **Blast radius.** One clause in `_where` plus one in the content query,
  against a new deletion path touching a table five modules write to.

## What this task changes

1. Search excludes items with `missing_since` set — in the `WHERE`, so the
   exclusion happens before `LIMIT`/`OFFSET` and not after.
2. `/items/{id}` says so plainly when the file is gone, instead of offering a
   preview of nothing.
3. Browse's root screen learns to show files lying directly in the library
   root, which are indexed today but unreachable.
4. Nothing is deleted. Not an item, not a proposal, not a history row, not a
   search entry, not a quarantine record.
