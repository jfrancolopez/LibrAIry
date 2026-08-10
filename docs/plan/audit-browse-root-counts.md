# Audit: what a Browse tile counts

Phase 1 of closing the last Browse inconsistency. Written before any behaviour
changed, by tracing the call paths and by measuring the author's real library
(140 committed files, 103 inbox items) through a private copy of the database.

The previous fix made the *explorer* answer "what physically exists". The root
screen was left alone. This note establishes what it actually answers today.

## How a tile is built

`GET /browse` → `_browse_context` → `browse_home(conn)` → one query:

```sql
SELECT s.category, COUNT(*) AS count
FROM search_fts s JOIN items i ON i.id = s.item_id
WHERE i.root = 'library'
GROUP BY s.category
```

and the tile list itself is a **hard-coded tuple**:

```python
CATEGORIES = ("music", "movies", "shows", "photos", "documents",
              "books", "projects", "misc")
```

So, point by point:

| Question | Answer today |
|---|---|
| Where does the category list come from? | A constant. Not the filesystem, not the database. |
| Where does the count come from? | `search_fts.category`, which is copied from the **live proposal** — a classification, not a folder. |
| Recursive or direct-child? | Neither. It counts rows, at any depth, that carry that classification. |
| Do folders count? | No. |
| Do ignored/system files count? | No — they were never scanned, so they have no row. |
| Do unsupported-but-visible files count? | Yes, if scanned. The scanner has no extension filter. |
| Do unindexed physical files count? | **No.** This is the drift. |
| Do stale rows count? | **Yes.** A row whose file is gone still counts until a scan clears it. |
| FTS or items? | FTS, joined to items for the root filter. |
| Does the category exist only because rows exist? | The reverse: the tile exists whether or not anything does. |

Two separate problems fall out of that.

### 1. The count is classification truth, the link is filesystem truth

The tile links to `/browse/{category}`, and `_category_prefix` turns that slug
into a physical folder by capitalising it — `music` → `Music/`. So the number
and the destination are derived from two different things that happen to agree
only because the destination templates in `taxonomy.py` file each category into
its matching top folder. Nothing enforces it. A row whose proposal changed
category after the file was committed would be counted under one tile and found
under another.

Measured on the real library: **0 rows disagree.** The bug is latent, not live.

### 2. The tile list is not the library

The library physically contains three top-level directories. Browse shows eight
tiles, five of them permanently reading zero:

```
music      48   Music/     exists
movies      0   Movies/    does not exist
shows       0   Shows/     does not exist
photos     89   Photos/    exists
documents   0   Documents/ does not exist
books       0   Books/     does not exist
projects    3   Projects/  exists
misc        0   Misc/      does not exist
```

The inverse is the real damage: a directory the user creates over SMB — say
`Archives/` — can never appear on the Browse home screen, because the screen is
a list of classifications, not a listing. That is the same invariant the last
fix established for the explorer, unfixed one level up.

So Browse root is currently **option C, a mixture**: logical categories for
existence and counting, physical folders for navigation.

## Real counts, read-only

Compared against the filesystem using `is_visible_entry`, the predicate the
scanner and the explorer already share:

| folder | physical visible files | items rows | searchable rows | unindexed | stale |
|---|---|---|---|---|---|
| Music | 48 | 48 | 48 | 0 | 0 |
| Photos | 89 | 89 | 89 | 0 | 0 |
| Projects | 3 | 3 | 3 | 0 | 0 |

Everything agrees today. That is worth stating plainly: this is not a bug the
author is currently looking at, it is a bug the author cannot currently *see*,
which is the point of adding a consistency reading rather than only a fix.

## Timings

| Operation | Measured |
|---|---|
| `browse_home()` as it stands (one GROUP BY) | 0.10 ms |
| recursive visible walk, `Music/` (48 files) | 1.9 ms |
| recursive visible walk, `Photos/` (89 files) | 1.0 ms |
| recursive visible walk, `Projects/` (3 files) | 0.1 ms |
| recursive visible walk, whole library (140 files) | 2.8 ms |

Roughly 20 µs per file with `Path.iterdir`. A filesystem-backed root is
affordable here by three orders of magnitude, so it gets the simple
implementation and no cache.

## Terminology, as the code actually uses it

This has to be settled before the UI can say anything honest.

```
filesystem  --librairy scan-->  items  --sync_search_item-->  search_fts
                                  |
                                  +--analyze--> proposals (category, destination)
                                                    |
                                                    +--plan, approve, commit--> library
```

| Word | What it means here |
|---|---|
| **scanned** | Has an `items` row. `scan_root` walks every visible file with no extension filter. |
| **indexed** | Same thing. There is no separate inventory. |
| **searchable** | Has a `search_fts` row. `sync_search_item` is called for every item unconditionally, so this equals scanned. |
| **classified** | Has a live proposal carrying a category and a destination. |
| **committed** | Moved into the library by an approved plan; the executor moves the item row's root to `library`. |
| **library item** | An `items` row with `root='library'`. |

Two consequences worth being exact about:

- **There is no "visible but unsupported" tier.** Because the scanner takes
  every visible file, anything Browse shows is scannable. So "not indexed"
  always means "not scanned yet" and never "this type cannot be indexed". The
  UI may say so.
- **`librairy index rebuild` cannot fix it.** That command rebuilds `search_fts`
  from the item rows that already exist; it discovers nothing. The remedy for an
  unindexed file is `librairy scan --root library`, which is also what builds
  the layout map. This distinction matters because pointing the user at the
  wrong command would be worse than saying nothing.

## Why drift is reachable at all

The worker scans **inbox only** (`worker.py`, `run_once`). Library rows are
created by the commit executor when it moves a file in. Nothing periodically
rescans the library. So a file copied straight into the library over SMB stays
unindexed indefinitely, and Browse — correctly, since the last fix — shows it
while Search cannot find it. That is the drift the status reading is for.

## What this task will change

1. Browse root lists **physical top-level directories**, with recursive counts
   of visible files. Classification stops deciding what exists.
2. A folder's real name is its label and its URL. No capitalising a real
   directory into a category label.
3. A small consistency reading near the heading, calculated per request:
   physical vs indexed, unindexed count, indexed-but-missing count.
4. It reports. It does not repair — no row deletion, no scan trigger, no
   indexing because someone looked at a folder.

## Noted, not chased

- One inbox item has no `search_fts` row: item 171,
  `Queen - .../VIDEO_TS/VIDEO_TS.IFO`. Every other item has one. Inbox, not
  library, so it does not affect Browse; worth a look on its own.
- Eight inbox rows carry `missing_since`. Expected — those files were consumed
  or removed — but nothing ever prunes them.
- `settings.ignore_patterns` is empty on this install, so the ignore path is
  exercised by tests rather than by the live data.
