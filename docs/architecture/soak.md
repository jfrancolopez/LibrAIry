# Soak: what repeated work leaves behind

Every other measurement in this program asks how long something takes. This one
asks a different question, and a component can pass either while failing the
other:

| | |
|---|---|
| **steady-state** | what one repetition costs, forever |
| **drift** | what one repetition *leaves behind*, forever |

Expensive and stable is fine. The Dashboard poll runs every five seconds and
costs 1.5 ms and fifteen statements, and that is a healthy number because it is
the same number on the hundred-thousandth poll. Cheap and growing is the
failure: half a millisecond and one row per idle cycle is invisible in any
measurement of a single cycle, and it is why an appliance that was pleasant in
week one is annoying in week four.

`scripts/soak.py` measures both. `tests/test_soak.py` holds the conclusions.

## The instrument has to be still

The first run of this harness reported the application leaking **4.6 MB per
thousand dashboard polls**, with a late-window slope higher than the whole-run
slope — the signature of a genuine, still-accelerating leak. Every byte of it
was `asyncio` event loops allocated by the test client, which starts a fresh one
per request unless it is entered as a context manager.

A soak instrument that drifts reports drift. Two rules came out of it, and both
are enforced in the script:

- the client is entered once and reused, as a browser is;
- every workload starts from a settled baseline, because a workload that follows
  an expensive one otherwise measures the previous one's memory being released
  and reports a **negative** 388 MB per thousand operations.

And a third, from the other direction: the watch list of tables is read from
`sqlite_master` rather than written by hand. The hand-written one carried
`transfer_runs`, which is not what the schema calls that table, so the count
silently failed and every backup soak reported "no row growth" while meaning "I
was not looking".

## Slope, not endpoints

`first` to `last` cannot tell *climbed once and settled* from *climbing
steadily*, and those are the two answers the whole exercise exists to
distinguish. So every figure is a least-squares slope per thousand operations,
reported twice: over the whole run, and over the second half alone. A cache that
fills and then holds still has a whole-run slope and no late slope. Latency is
reported in three windows for the same reason — a number that rises and comes
back is a page cache warming, and a before/after pair calls it a regression.

## What was found

Three drifts, and the cheapest-looking one was the worst.

**A file descriptor per idle worker cycle.** `check_database` opened its
connection with `with sqlite3.connect(...)`, which is a *transaction* manager:
it commits on the way out and closes nothing. One database and one WAL leaked
per cycle — twenty-three cycles held twenty-three databases open — and a few
hundred cycles would have reached the process limit and stopped the worker with
*too many open files*. Nothing was wrong with any single cycle.

`librairy/db.py` already documents the mirror image of this mistake on
`transaction()`: there, `with conn:` looks like a transaction and is not one.
Here it looks like opening a file and is not that either.

**A full database verification on every idle cycle.** `PRAGMA quick_check` reads
and verifies every page — 3.2 seconds on a 587 MB index, by the measurement in
`check_database`'s own docstring — and it ran whenever the worker had nothing
else to do, which on a settled installation is every cycle there is. The comment
claimed it was arranged like the FTS integrity check; it was not, because that
one runs when the index is rebuilt. It now has the gate it claimed to have,
through the same recorded interval the AI probe uses, so a worker restarted
hourly does not get a free verification each time.

**A search-index rewrite per file per scan.** The scanner had a fast path for
unchanged files that was only ever fast about *hashing*: it still wrote the row,
looked the id up and called `sync_search_item`, which is a `DELETE` and an
`INSERT` into an FTS5 table. A library nobody had touched grew its FTS segment
tables on every pass, and the scan got slower as they accumulated — 179 ms
rising to 221 ms across a single 200-cycle run at 800 files.

The index stays correct without it because every writer of the things it derives
from calls `sync_search_item` for itself, at twenty-odd call sites. The
scanner's copy was re-asserting what was already true.

| rescan of 800 unchanged files | before | after |
|---|---|---|
| steady | 212 ms | **51 ms** |
| database growth | 145 KB / 1,000 | 5 KB / 1,000 |
| FTS segment rows | grows every pass | **none** |

## What converged

Measured, not assumed. Each of these is a claim that repeating unchanged work
writes nothing new, and each is pinned in `tests/test_soak.py`.

| | |
|---|---|
| **Dashboard polling** | 12,000 polls: one session row, total. Latency 1.51 / 1.53 / 1.52 ms across the three windows |
| **Idle worker** | after the first cycles settle, 400 more produce zero row deltas and zero database growth |
| **Rescan** | unchanged filesystem, no new rows of any kind |
| **Review / Search / Browse / Health** | reads mutate nothing |
| **Document facts** | eight PDFs measured once; 119 further repetitions shell out to nothing |
| **Thumbnails** | eight cached files, bounded, across 120 repetitions |
| **Backup** | the second unchanged run transfers nothing — through real rclone, because what the binary decided is the question |
| **Mirror** | the divergence set is identical after eight further identical comparisons |
| **Run history** | `backup_runs` stops at its stated 200 per destination, exactly |

## The WAL

Bounded by SQLite's own autocheckpoint at 1,000 pages, and it stays there: 8,000
writes interleaved with reads on a second connection never moved it off
4,027 KB, and an explicit `TRUNCATE` checkpoint takes it to zero — which is the
proof that no reader is pinning an old snapshot. Every connection in this
program is opened in autocommit, so a read releases its snapshot when the
statement ends rather than holding one open across a page render.

## Mixed, not isolated

Isolated loops cannot show contention. The mixture — ten dashboard polls to two
worker cycles to one each of Review, Search, Browse, Health, a scan, a backup, a
mirror, thumbnails, document facts and a commit-and-undo round — runs as one
repeated operation, and across 150 rounds of it the descriptor count, the WAL
and the per-round latency are all flat. Everything that grew was work that had
actually been done: a journal row per operation, a run row per transfer, an item
row per arrival.
