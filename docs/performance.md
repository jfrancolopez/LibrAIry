# Performance and scale

**M1-01, measured 2026-09-01 against `v1.3.1` / schema 47.**

Two of LibrAIry's eight surfaces do not work at the scale it is designed for.
Five do, comfortably. One is heading the wrong way and has time.

That is the whole result. Everything below is how it was measured and what
exactly is wrong, because [ROADMAP.md](ROADMAP.md) M1-01 says a measured
bottleneck is a *result*, not a work order — none of it is fixed here.

    Review      was unusable at every population; now pages decisions rather
                than files, on a flat 40 statements at a million
    Health      never finished; now 2.1 s at a million — usable, not yet good
    Search      2.6 s at a million; degrading linearly, cause known, not fixed
    Browse      1.2 s at a million; one unbounded query, not fixed
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
