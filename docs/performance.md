# Performance and scale

**M1-01, measured 2026-09-01 against `v1.3.1` / schema 47.**

Two of LibrAIry's eight surfaces do not work at the scale it is designed for.
Five do, comfortably. One is heading the wrong way and has time.

That is the whole result. Everything below is how it was measured and what
exactly is wrong, because [ROADMAP.md](ROADMAP.md) M1-01 says a measured
bottleneck is a *result*, not a work order — none of it is fixed here.

    Review      was unusable everywhere; now 1.4 s at 100,000, still over
                budget at a million — one unbounded scan left, named below
    Health      was quadratic and never finished; now completes at a million,
                but takes 22 s and is not yet usable there
    Search      3.1 s at a million; degrading linearly, and fixable
    Browse      1.2 s at a million; one unbounded query
    Dashboard   620 ms at a million; bounded, worth watching
    Commit      11 ms at a million; bounded
    Quarantine  36 ms at a million; bounded

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
| Review page 1 | >60,000 (3,969) → **1,357 (185)** | >60,000 (3,133) | >60,000 (1,613) → **>60,000 (159)** |
| Review page 50 | >60,000 (3,906) → **1,193 (185)** | >60,000 (3,212) | >60,000 (1,663) → **>60,000 (159)** |
| Review, ungrouped sort | >60,000 (3,985) → **1,181 (185)** | >60,000 (3,212) | >60,000 (1,598) → **>60,000 (159)** |
| Health | >60,000 (10) | >60,000 (10) | >60,000 (10) → **22,264 (41)** |
| Health (attention only) | >60,000 (8) | >60,000 (8) | >60,000 (8) → **17,362 (18)** |
| Search `Album` | 248 (153) | 793 (153) | 3,108 (153) |
| Search unfiltered | 67 (153) | 276 (153) | 919 (153) |
| Browse home | 94 (1) | 294 (1) | 1,196 (1) |
| Dashboard | 52 (22) | 132 (22) | 620 (22) |
| Quarantine page 1 | 10 (24) | 12 (23) | 37 (24) |
| Quarantine page 50 | 2 (6) | 6 (24) | 17 (24) |
| Commit page 1 | 2 (2) | 4 (2) | 12 (2) |
| Commit summary | 1 (2) | 1 (2) | 6 (2) |

Worker throughput, unchanged by population within noise: the search index
rebuilds at **3,400–5,200 items per second**, so re-indexing a million-file
library is three to five minutes.

## The bottlenecks, in the order they matter

### 1. Review renders the entire findings table, every time

> **FIXED, 2026-09-02.** Two changes, and it needed both.
>
> The per-finding plan lookup is batched — `correction_state.active_plans_for`
> answers for a whole set in one statement — which took Review from 3,969
> statements to 940 and moved the bottleneck to (5) without making the page
> usable.
>
> Then `audit_view` became a bounded page. The bucket rule and the subject key
> are both expressible in SQL (`subject_key` reads only `kind` and `relpath`),
> so which findings matter can be decided *without building a row for any of
> them*: three counts over the whole table, then rows built only for a page of
> twenty-five subjects, whole cards never cut off mid-card.
>
> **Review at 100k: 1,357 ms and 185 statements, and it finishes.** The count
> is now flat — the same 185 on page 1, page 50, and with grouping off — and
> `_artist_folder_under` has left the hot list because it is called for a page
> of subjects rather than for every finding in the database.
>
> **At a million it is still over budget**, on 159 statements. That is no longer
> a counting problem: a page runs a fixed, small number of queries and one of
> them reads the whole library. See (5), which is now the only thing between
> Review and a million files.


`web/review.py:1213` — `audit_view` builds a row for **every** finding with
status `open`, `accepted` or `kept`. No `LIMIT`, no paging. Each of those rows
then calls `correction_state.active_plan` (`web/review.py:1954`), which is one
query carrying two correlated subqueries, plus a drift measurement when a plan
exists.

So a Review page costs one query per audit finding in the database, and it is
`review_data`'s unconditional first act. At 50,000 findings that is 50,000
statements before a single proposal has been looked at. The measured page never
got past 1,200 of them inside sixty seconds.

This is a straight violation of the rule `tests/test_scale.py` already states
for Quarantine and Commit: *the row count in the response does not grow with
the table*. Review's proposal list obeys it. The audit list embedded beside it
never did.

### 2. Health asks a quadratic question, twice

> **Quadratic part fixed, 2026-09-02.** `unindexed` is two counts and a
> subtraction — live items, minus index rows belonging to a live item — which
> is the same question without the correlated scan, and safe because
> `search_fts` is written with `rowid = item_id` so there is at most one index
> row per file. `index_counts` derives its copy from the join it had already
> run instead of asking again.
>
> **Health at a million: from never finishing to 22.3 s**, and it completes.
> That is not yet usable. What is left is not quadratic, just heavy — chiefly
> `undo_sequence._later_decisions` at 17 s of the 22 inside
> `attention.report`. M1-06 continues.

`search_health.py:156`:

```sql
SELECT COUNT(*) FROM items i
WHERE i.missing_since IS NULL
  AND NOT EXISTS (SELECT 1 FROM search_fts s WHERE s.item_id = i.id)
```

`search_fts` declares `item_id UNINDEXED`, so there is no index to satisfy that
subquery. The query planner confirms it without any timing:

```
SCAN i
CORRELATED SCALAR SUBQUERY 1
  SCAN s VIRTUAL TABLE INDEX 0:
```

A full scan of the FTS table for every row of `items`. 3.8 seconds at 5,000
items; the shape is N×M. And `health_data` asks for it **twice** in one render
— which is the same duplication the function's own docstring says was removed
once already.

Second contributor, far behind: `undo_sequence.py:378` `_later_decisions`, 940 ms
at 2,000 history rows.

Health is the page that exists to tell you something is wrong. It is currently
the most broken page in the program.

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

`destination_choice.py:155` `_artist_folder_under` walks every library row under
a section prefix in Python, comparing normalised folder keys, once per candidate
finding. `LIKE 'Music/%'` on a music library is most of the library.

It was dwarfed by (1), briefly became the top cost when (1) was half-fixed, and
is now **bounded but not cured**: it is called for the findings on the page
rather than for every finding in the database, which is why Review completes.
Each call still walks the library.

Looking a folder up by *normalised* name is not something the current schema can
index, so the cure is a design question rather than a query rewrite, and it is
not urgent now that the call count is bounded. The `xfail(strict=True)` on
unbounded library scans in `tests/test_scale_surfaces.py` stays until it is.

### 6. Dashboard is fine and worth watching

22 statements at every population, which is the rule working. 52 → 132 → 620 ms
is the per-statement cost of larger tables, not a structural fault. It polls
every five seconds, so 620 ms at a million is worth a look eventually.

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
