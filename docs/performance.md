# Performance and scale

**Measured 2026-09-01 against `v1.3.1` / schema 47, again 2026-09-02 at the
close of M1 against schema 50, again 2026-09-03 after M2-01 and M2-03 against
schema 51, and again 2026-09-04 for M3-01 against schema 55.**

M1-01 found two of LibrAIry's eight surfaces unusable at the scale it is
designed for. This document is the measurement first and the work second,
because M1-01's own rule is that a measured bottleneck is a *result* and not
automatically a work order — the sections below are in the order they were
found, and the summary of where everything landed is
[M1 close](#m1-close-2026-09-02).

    Review      was unusable at every population; now 728 ms at a million on
                a flat 42 statements, paging decisions rather than files
    Health      never finished; now 1.8 s at a million — usable, not yet good
    Search      under 2 ms for a real query; 2.3 s for one matching the whole
                library, which is what the old headline measured
    Browse      1.2 s and a million-row read; now 0.2 ms and one statement
    Dashboard   349 ms at a million; bounded
    Commit      10 ms at a million; bounded
    Quarantine  21 ms at a million; bounded

Every surface completes at every population. The two that did not are fixed;
the two that are merely slow are M1-06 and untouched.

## How it was measured

`scripts/scale_bench.py`, run at three populations:

```bash
.venv/bin/python scripts/scale_bench.py --library 1000000 --inbox 50000 --budget 60
```

It synthesizes **database populations**, not files. A million real files
measures the filesystem and proves nothing about a query, and
`docs/ROADMAP.md` rules it out explicitly. Every row it writes is one the real
queries match — same tables, same states, same constraints — and every surface
is measured through the same data function the web route calls.

Two numbers per surface, and the second one matters more. A slow query is a bad
afternoon; a query *per row* is an architecture that stops working, and it looks
healthy until the table is large. Only the count separates them.

The 100k column carries an arrow where the work below changed it: the second
figure is after batching the plan lookup *and* bounding `audit_view`, same
harness, same population. 300k and 1M are the original measurements and have
not been re-run; Review's cost no longer depends on either number, so there is
nothing there left to re-measure.

A surface that will not finish is interrupted at sixty seconds through SQLite's
progress handler. **A query count recorded under `EXCEEDED` is truncated, not
total** — Review shows *fewer* statements at a million than at 300,000 because
the interrupt arrives earlier in the same unbounded loop. It is not improving.

Raw results: [measurements/](measurements/).

### The population

Proportioned like a personal library rather than a uniform table: 55%
photographs, 25% music, 10% documents, the rest video, books and oddments.
Arrivals gather into the shapes those categories actually arrive in — a camera
card of ~150, an album of ~12, a season of ~10 — because a uniform mix flatters
the grouping code.

| | 100k | 300k | 1M |
|---|---|---|---|
| library items | 100,000 | 300,000 | 1,000,000 |
| inbox items / proposals | 5,000 | 15,000 | 50,000 |
| groups | 139 | 413 | 1,376 |
| audit findings | 5,000 | 15,000 | 50,000 |
| quarantine entries | 1,000 | 3,000 | 10,000 |
| history rows | 10,000 | 30,000 | 100,000 |
| database | 59 MB | 176 MB | 587 MB |
| build time | 17 s | 63 s | 175 s |
| peak RSS | 65 MB | 97 MB | 198 MB |

## Results

Milliseconds and (statements) behind one page. Bold marks a surface that did
not finish; an arrow marks a figure this pass changed.

| Surface | 100k | 300k | 1M |
|---|---|---|---|
| Review page 1 | 79 (186) | 190 (186) | 543 (186) |
| Review page 50 | 61 (186) | 150 (186) | 466 (186) |
| Review, ungrouped sort | 55 (186) | 146 (186) | 457 (186) |
| Health | 336 (42) | 927 (42) | 2,120 (42) |
| Health (attention only) | 75 (18) | 192 (18) | 600 (18) |
| Search `Album` | 243 (153) | 779 (153) | 2,572 (153) |
| Browse home | 82 (1) | 297 (1) | 1,157 (1) |
| Dashboard | 36 (22) | 137 (22) | 349 (22) |
| Quarantine page 1 | 7 (24) | 10 (23) | 21 (24) |
| Commit page 1 | 2 (2) | 3 (2) | 10 (2) |

Review's statement count is **186 at every population** — the bounded-page rule
holding, which is the thing worth checking. Where it was, on 2026-09-01:

| Surface | 100k | 300k | 1M |
|---|---|---|---|
| Review page 1 | >60,000 (3,969) | >60,000 (3,133) | >60,000 (1,613) |
| Health | >60,000 (10) | >60,000 (10) | >60,000 (10) |

Counts under a `>60,000` are truncated by the interrupt, not totals.

Worker throughput, unchanged by population within noise: the search index
rebuilds at **3,400–5,200 items per second**, so re-indexing a million-file
library is three to five minutes.

## The bottlenecks, in the order they matter

### 1. Review renders the entire findings table, every time

> **FIXED, 2026-09-02.** Four changes, and it needed all four. Each was found by
> profiling after the previous one landed — none of them was visible by reading.
>
> 1. **The plan lookup was batched** (`active_plans_for`): 3,969 statements to
>    940, and the bottleneck moved rather than went away.
> 2. **`audit_view` became a bounded page.** The bucket rule and `subject_key`
>    are both expressible in SQL — `subject_key` reads only `kind` and
>    `relpath` — so which findings matter is decided without building a row for
>    any of them. Twenty-five *subjects*, whole cards, counts from SQL.
> 3. **The bucket rule stopped asking per finding.** Bounding it made the
>    bounding itself the bottleneck: `EXISTS (... p.audit_finding_id = f.id OR
>    p.id = f.plan_id)` spans two columns, so no index satisfies both arms and
>    SQLite scanned `plans` once per finding — **53.7 s for the counts alone at
>    a million**, slower than the unbounded page it replaced. Driven from
>    `plans` instead, which is the small side, as a CTE computed once.
> 4. **`destination_folders` dropped a redundant `DISTINCT`.** `UNIQUE (root,
>    relpath)` already guaranteed it, and it made SQLite build a sorted
>    temporary B-tree over the whole library before yielding a row — quietly
>    defeating the 200-folder early return the function already had.
>
> **Review at a million: 543 ms on 186 statements**, and the count is 186 at
> 100k and 300k too.

### 2. Health asks a quadratic question, twice

> **FIXED, 2026-09-02**, in two places.
>
> `unindexed` is two counts and a subtraction — live items, minus index rows
> belonging to a live item — instead of a `NOT EXISTS` against an FTS table
> whose `item_id` is `UNINDEXED`. Exact, because `search_fts` is written with
> `rowid = item_id`, so there is at most one index row per file.
>
> `undo_sequence._later_decisions` was the larger half at 17 s, and it was the
> **same shape as (1) and (3)**: one join whose `ON` clause had an `OR` between
> two different column pairs — the same file, or a later operation reading
> where an earlier one wrote. Neither arm can use an index that way, and both
> sides are derived tables with no indexes at all, so SQLite compared every row
> against every row. Split into two equality joins with `UNION ALL`, each arm
> gets an automatic index, and `COUNT(DISTINCT ...)` over the same key makes the
> union exact rather than approximate.
>
> `PRAGMA quick_check` was the third: a full verification of the database file
> on every render, 3.2 s on a 587 MB index and growing forever. It now runs on
> an idle worker cycle and Health reports the recorded verdict and its age —
> the arrangement `search_health.check_search_index` already used, for the same
> reason.
>
> **Health at a million: 22.3 s → 2.1 s**, `attention.report` alone 600 ms.
> Usable, not yet good: roughly a third of what is left is the FTS count, asked
> three times per render because `attention._search` and `search_index_panel`
> each ask independently. Counting a million FTS rows is ~300 ms and no cheaper
> shape exists — measured — so the fix is to ask once, not to ask faster.

### 3. Search counts history per result row

`search.py:343` runs `SELECT COUNT(*) FROM history WHERE dest_root=? AND
dest_relpath=?` once for each of the fifty rows, and `history` has no index on
those columns — only `plan_id` and `op_id`. Fifty full scans of the history
table per page: 3.1 seconds at 100,000 history rows.

The comment three lines above, explaining why `relationship_context` is batched,
reads: *asking per row would make the page slower the more the library knows.*
The next statement asks per row.

### 4. Browse home is one query, and it reads everything

94 ms → 294 ms → 1,196 ms across the three populations, on a single statement.
Bounded in *statements*, unbounded in *rows*. It has headroom and a clear cause,
so it is not urgent — but it will not survive another order of magnitude.

### 5. Review scans the whole library, once per candidate row

> **FIXED, 2026-09-02.** Two shapes, because the question has a common case and
> a real one. Nearly always the folder is spelled exactly as the name says, so
> that is asked first as a half-open range on `relpath`, which the existing
> index answers without reading the section at all. Only a genuine spelling
> difference falls through, and then it asks for the **distinct child folder
> names** rather than every file beneath them: 250,000 rows crossing into
> Python became 2,000.
>
> The sections on a page repeat, so a `ChildFolders` object computes each one
> once for the whole render. It is not a cache — it is created by the caller,
> lives for one render, and has no invalidation to get wrong.
>
> 1,006 ms a call, eight calls a page, to one 589 ms scan for the page; then to
> nothing measurable once the seek covers the common case.

### 6. Dashboard is fine and worth watching

22 statements at every population, which is the rule working. 52 → 132 → 620 ms
is the per-statement cost of larger tables, not a structural fault. It polls
every five seconds, so 620 ms at a million is worth a look eventually.

## Review pages decisions, 2026-09-02

Review now pages *decisions* rather than files: twenty-five units a page, where
a unit is a group with more than one member or a single loose file, and each
group shows five members with an honest count and a bounded expansion.

At a million files, the page still completes and its cost still does not grow
with the library:

| | before (files) | after (decisions) |
|---|---|---|
| Review page 1 | 543 ms (186 queries) | 737 ms (414) |
| Review page 50 | 466 ms (186) | 621 ms (34) |
| Review, sorted view | 457 ms (186) | 534 ms (186) |

The statement count varied with **how many rows the page drew** — 34 on a page
of large groups, 414 on a page of loose files — because three queries per row
(`is_duplicate_proposal`, and `similar_arrival` twice) had always been per-row
and a decision page can draw up to 125 rows instead of 50.

### The three per-row queries, batched — 2026-09-02

| | paging decisions | after batching |
|---|---|---|
| Review page 1 | 737 ms (414 queries) | 1,138 ms (**40**) |
| Review page 50 | 621 ms (34) | 835 ms (34) |
| Review, sorted view | 534 ms (186) | 585 ms (**37**) |

Read the statement counts, not the milliseconds: this run shared the machine
with the test suite, and every timing in this table is inflated by it. The
counts are exact and are the thing that was changed.

Building a page of rows now costs **7 statements whether it draws five rows or
fifty**, which is what `test_building_rows_costs_the_same_for_five_as_for_fifty`
pins. Three things did it:

- **`is_duplicate_proposal` re-read the proposal's own evidence.** There is at
  most one live proposal per item, so the row the caller is already holding
  *is* the row that query fetched. It is now a string test on data in hand.
- **`twins_of` asked one fingerprint at a time.** One `IN` for the page, and
  only for the rows whose evidence says the duplicate finder staged them —
  which on an ordinary page is none, so it runs no query at all.
- **`similar_arrival` opened with a lookup of the item, then asked about the
  pair.** The first is the row in hand. The second is one statement for the
  page — and it is the fourth appearance of the shape in this document.

That fourth appearance is worth naming. A `similar_media_flags` row is one
pair, and either file may be `item_id`, so finding a file's twin means asking
about two columns — `WHERE f.item_id IN (…) OR f.similar_item_id IN (…)`, which
defeats both indexes and scans. Two indexed halves and a `UNION ALL` instead,
exactly as `_later_decisions` was fixed. And the second column **had no index
at all**: `UNIQUE (item_id, similar_item_id, kind)` indexes the first, and
nothing indexed the second. That is **migration 048**, and the first schema
change since 1.3.1 shipped.

## M2-03, 2026-09-03 — what each processing mode costs

200 files in the inbox, one worker cycle per mode, no AI provider configured.
`scripts/measure_worker_load.py` reproduces it. CPU seconds per wall-clock
second, which is what a busy worker takes away from everything else on the box.

| | files a cycle | in the cycle | sustained | CPU s per file |
|---|---|---|---|---|
| Quiet | 10 | 0.72 | **0.34** | 0.317 |
| Balanced | 50 | 0.76 | **0.70** | 0.086 |
| Full Power | 50 | 0.76 | **0.76** | 0.087 |

**The batch cap is not what makes Quiet quiet, and the measurement is what says
so.** A Quiet cycle costs 0.72 CPU seconds per wall second and a Balanced one
0.76 — near enough identical, because a cycle's fixed costs do not shrink with
its batch: the inbox scan, the duplicate pass and the companion pass run either
way. What the cap buys is a *shorter* cycle, and the pause after it is where the
difference actually lives. Sustained, Quiet takes less than half the machine
Balanced does.

The per-file column is the price of that, and it is worth saying out loud:
Quiet costs 3.7× as much CPU per file, because it pays the same fixed cycle
cost for a fifth of the progress. That is the trade the mode is: the same work,
spread out, at a higher total cost. It is the right trade when something else
needs the machine and the wrong one when nothing does, which is why Balanced is
the default.

**What this does not measure.** A NAS serving video off the same disks, which is
the situation the mode exists for and cannot be reproduced on a build machine.
These are the reproducible numbers; the judgement is made with them in hand.

## M3-01, 2026-09-04 — what a measurement costs, and what reading one costs

The whole bargain of the metrics table in two columns. A rollup is allowed to
be expensive because it happens once an hour; the read it pays for has to be
flat, or the Dashboard would have swapped one slow page for another.

| library | rollup | 90 days of every metric |
|---|---|---|
| 100,000 | **73 ms** | 0.15 ms |
| 300,000 | **219 ms** | 0.14 ms |
| 1,000,000 | **821 ms** | 0.18 ms |

The rollup grows with the library because two of its measures have to read the
`items` table, and those two are the whole cost:

    library.files + library.bytes        157 ms at 1M   (SCAN items)
    the spread across top-level folders  580 ms at 1M   (SCAN items + GROUP BY)
    everything else                      under 1 ms each, on an index

Hourly, 821 ms is **0.02%** of a worker's time — which is why the rollup sits
after the inbox work on every cycle rather than behind the idle gate. Putting
it in the idle tier would have saved that 0.02% and cost a busy installation
its entire history.

The read does not grow at all: it is a few hundred rows keyed by day, and 90
points cost the same on a library of four files and one of four million. That
is the contract, and `test_scale_surfaces` asserts it as a statement count
rather than as a duration.

**Table growth.** 29 rows a day at a million items — twelve named measures plus
two per top-level folder. Two years is about 21,000 rows, well under a
megabyte, and `KEEP_DAYS` prunes past that.

### M3-02, the Dashboard, at three populations

The point of the whole exercise, in one table. The history band reads
`metrics_daily` and never `items`, so it costs the same on a library of forty
thousand files and one of a million:

| library | history band, 30 days | 365 days | whole page |
|---|---|---|---|
| 100,000 | **1.9 ms** · 9 statements | 8.9 ms | 37 ms · 35 statements |
| 300,000 | **1.8 ms** · 9 statements | 8.9 ms | 85 ms · 35 statements |
| 1,000,000 | **2.0 ms** · 9 statements | 8.7 ms | 282 ms · 35 statements |

Two things worth reading off it. The band is **flat** — its cost follows the
number of days asked for and nothing else, which is the contract M3-01 was
built to provide. And the whole page still grows with the library, because the
top two bands are live aggregates and must stay that way: replacing "what needs
me now" with yesterday's rollup would buy a better number here at the cost of a
page that can be wrong about the present.

Adding the third band cost the page about **2 ms**. The 349 ms recorded for
Dashboard at M1 close is now 282 ms, from unrelated indexing since.

### Health, measured again, and why the metrics layer does not help it

M1-06 carries Health at 1.8 s. Re-measured at a million on schema 55: **1,487
ms across 40 statements**, of which the index-integrity counts are the largest
single share:

    SELECT COUNT(*) FROM search_fts                              241 ms
    the same joined to items, for rows whose file has gone        314 ms
    SELECT COUNT(*) FROM items WHERE missing_since IS NULL         63 ms

M3-01 was checked against this deliberately and **is the wrong tool for it**.
Those counts are current operational state — is the search index consistent
with the library *right now* — and answering them from a historical rollup
would make a record of the past into the source of truth for the present,
which is the one thing this table must never become. If they are to get
cheaper it is by the recorded-verdict pattern `search_health` already uses:
measure on an idle cycle, show the verdict and its age. That is M1-06's work
and it stays there.

## M2-01, 2026-09-03 — what holding files costs

One million library rows, 20,000 inbox proposals, same machine and same harness
as the table below. The question is whether a section of Review that has to
account for an unbounded held backlog can be drawn for the price of a count.

| | M1 close | after M2-01 |
|---|---|---|
| Review page 1 | 728 ms (42) | **542 ms (43)** |
| Health | 1,773 ms (39) | **1,407 ms (40)** |
| Browse home | 0.2 ms (1) | 0.3 ms (1) |
| Search, matching everything | 2,260 ms (6) | 2,194 ms (6) |

**One statement each.** Review gained the `GROUP BY reason` that produces the
section's counts; Health gained the same one for its concern. Neither runs a
second query when nothing is held, and neither builds a row per held file — the
listing is one `LIMIT 25` page, and `test_scale_surfaces.py` pins that six times
as many held files cost the same number of statements and produce the same
number of rows.

The millisecond columns moved down rather than up, which is machine noise on an
otherwise unchanged page and is exactly why the statement counts are the part
worth reading.

## M1 close, 2026-09-02

Every surface, at every population, after M1-02 through M1-06. Milliseconds
first, statements in brackets. Idle machine; the caveat about machine load
applies to the numbers and never to the statement counts.

| | 100k | 300k | 1M |
|---|---|---|---|
| Review page 1 | 103 (42) | 236 (42) | **728 (42)** |
| Review page 50 | 67 (34) | 192 (36) | 624 (36) |
| Review, sorted | 59 (38) | 147 (38) | 477 (38) |
| Dashboard | 32 (22) | 125 (22) | 368 (22) |
| Health | 248 (39) | 561 (39) | **1,773 (39)** |
| Health, attention | 91 (19) | 255 (19) | 991 (19) |
| Commit summary | 0.3 (2) | 0.7 (2) | 3.6 (2) |
| Commit page 1 | 1.5 (2) | 3.4 (2) | 10.6 (2) |
| Quarantine page 1 | 6 (24) | 35 (23) | 38 (24) |
| Search, selective | — | — | **< 2 (6)** |
| Search, matching everything | 206 (6) | 632 (6) | 2,260 (6) |
| Search unfiltered | 41 (6) | 105 (6) | 367 (6) |
| Browse home | 0.1 (1) | 0.1 (1) | **0.2 (1)** |

Against where M1-01 found them: Review never finished at any population and is
728 ms on a statement count that does not move; Health never finished and is
1.8 s; Browse read a million rows to draw a line and reads none.

**Statement counts are flat in the library and flat in the queue.** Review's 42
is the shape of a page — twenty-five decisions, five members previewed each —
and the only thing that moves it is how many rows the page draws. Search was
153 and is 6.

### What the "Search 2.6 s" headline actually was

`Album*` matches **all one million rows** of the synthetic library, because the
generator names every file after its category. That is not a search; it is a
query for the whole library, and it is the number this report has been quoting.

A selective query — `IMG_004221*`, `Bee*`, anything that names a file — is
**under two milliseconds at a million**, and always was.

The real finding underneath it is still worth having. `ORDER BY bm25()` over a
join cannot be answered until every match has been joined and scored, so a
query matching a large share of a real library pays for that share. Ranking
inside the index first and joining the survivors is 4.7× faster — and it is
**not taken**, because it moves the liveness filter after the `LIMIT`, and
`search._where` explains why that is wrong: a stale row excluded after paging
shortens the page and pushes a real result onto the next one.
`tests/test_search_stale.py` catches it. Recorded, not done — see M1-06.

### Health, and what is left in it

1,373 ms of one render went on asking the same questions repeatedly: the FTS
join twice, the item count twice, from two parts of the page that could not see
each other. Counted once now, and `current` derived as `total - missing` rather
than paying 325 ms for a join that returns the same number.

What remains is irreducible with the current index shape: counting an FTS5
table means reading it (224 ms), and relating its rows to `items` means joining
it (321 ms). Health is 1.8 s at a million and roughly 1 s of that is those two
questions. Making them cheap needs either a recorded verdict — the arrangement
`PRAGMA quick_check` already has — or a different index shape. Neither is done.

## Human decision scale

The question M1-01 exists to answer: *how many decisions does a library of this
size ask a person for?*

| library | pending proposals | one per file | one per group (ideal) | as Review presents them today | groups split |
|---|---|---|---|---|---|
| 100k | 4,500 | 4,500 | 891 | 943 | 36 of 127 |
| 1M | 45,000 | 45,000 | 8,889 | 9,405 | 352 of 1,239 |

**Grouping already does almost all of the work.** A million files reach a person
as 9,405 decisions rather than 45,000, and the coherent answer is 8,889 — so the
page boundary costs **516 decisions, about 6%**, not the bulk of them.

> **Correction, 2026-09-02.** The first version of this section reported 16,930
> decisions and claimed *every* group was split, blaming the confidence sort for
> scattering group members across the ordering. That was wrong, and it was wrong
> about this harness rather than about LibrAIry: `decision_scale` replayed
> `ORDER BY confidence DESC`, which is the raw sort clause, while
> `review._order_by` already puts the group first whenever grouping is on —
> `COALESCE(g.kind, 'ungrouped'), COALESCE(g.label, 'Ungrouped')`, and then
> confidence *inside* the group. Members of a group are adjacent already. The
> table above is the corrected replay. The 300k row is not re-run and is
> omitted rather than left wrong.

A group is split only when it is genuinely larger than a page or straddles one —
36 of 127 at 100k. A twelve-track album is one decision. A 150-photo camera card
is three or four.

**What this changes.** M1-02's case does not rest on decision *count*: paging by
group saves something like 6%. It rests on the experience — a thumbnail grid
instead of 150 rows, outliers surfaced rather than hunted, one action instead of
a select-all — which is what the roadmap asks for and what 900 pages of rows
cannot give.

The lever that moves the *count* is **M1-05**: confidence tiers, and
deterministic decisions arriving in Ready for Commit. 9,405 decisions is still
9,405, and no amount of grouping makes it 500.

## What this does not measure

Stated so the numbers are not read as more than they are.

- **Filesystem cost.** Browse's directory walks and `_subtree_counts` touch
  real directories; this harness populates a database. Browse's numbers above
  are its SQL only. A million real files was ruled out deliberately.
- **Template rendering.** Every measurement is the data function, not the HTML.
- **Concurrency.** One reader, no worker running. SQLite has one writer, and a
  page competing with a live worker is a different measurement.
- **The worker end to end.** Scan, hash, classify and commit throughput at
  these populations is not covered; only search indexing is.
- **This machine, and it was busy.** A 2026 Mac, not the NAS — and during
  these runs it carried a load average near 11 from unrelated software
  (OneDrive at 90% of a core, a VM at 70%). Every millisecond below is
  therefore an upper bound on a contended machine, and should be re-taken on a
  quiet one before being quoted as a target.

  **The findings do not depend on it.** Statement counts are deterministic and
  unaffected by load; so is the query plan that proves the correlated scan, and
  so is every number in the decision-scale table, which is counting rather than
  timing. What load affects is how bad the two broken surfaces look, not that
  they are broken: Health is quadratic by construction, and Review's cost is
  one query per finding whatever the machine is doing.

## Regression tests

`tests/test_scale_surfaces.py` extends the bounded-page rule from Quarantine and
Commit to Review, Health and Search. Three of its tests are `xfail(strict=True)`
— they are the defects above, written as the invariant they violate, so that
fixing one turns an unexpected pass into a failure and the marker has to be
removed on purpose.

Expensive fixtures are opt-in:

```bash
.venv/bin/python -m pytest -m scale
```

## Previously

### 50k smoke, 2026-07-22

Run from the source checkout with `scripts/perf_smoke.py --count 50000
--commit-count 10000`, before Collections, relationships, findings, photo
similarity, Storage Optimization and Decision Memory existed. Kept as a record
of the pipeline's end-to-end cost, which the harness above does not measure:

- generate 4.97 s, scan 18.57 s, analyze 81.17 s, commit 11.82 s
- dashboard 5 ms, search 84 ms
- database 59 MB, peak RSS 90 MB

### CI smoke

`tests/test_performance_smoke.py` runs a 120-file version of the same pipeline
on every CI run: dashboard and search under a second, worker RSS under 500 MB.
