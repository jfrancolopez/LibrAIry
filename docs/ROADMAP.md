# Roadmap

The current plan of work, and the only one. What LibrAIry *is* lives in
[PRODUCT.md](PRODUCT.md); superseded planning material is archived under
[history/](history/README.md) and describes decisions that were, not decisions
that are.

Written 2026-09-01, against `v1.3.1` / schema 47.

---

## How this roadmap works

**Four milestones, rolling up to one polished major release.** Each milestone
delivers a coherent capability that can stand on its own, be exercised with
real data, refined and stabilized before the next one starts:

```
build → test → real use → refine → stabilize → next milestone
```

Each milestone is **release-quality and independently shippable**. That is not
the same as a mandatory public release: publish an intermediate version when it
is useful to, and skip it when it is not. The major version is declared
finished only after every milestone has been through integration testing, scale
testing, UI refinement, regression testing and real-world burn-in. **"It
works" does not finish it.**

**Priorities.**

| | |
|---|---|
| **P0** | must be known or fixed before scale-dependent work can be designed |
| **P1** | high-impact capability that fundamentally improves daily use |
| **P2** | important expansion |
| **P3** | polish, maintainability, cleanup |
| **DEFERRED** | valuable, deliberately later |

Effort is S / M / L / XL. Risk is Low / Medium / High.

---

# M1 — Scale truth and decision-first Review

The milestone that changes daily use the most, and the one everything else is
sized against.

## M1 at close — 2026-09-02

| | |
|---|---|
| **M1-01** Scale measured | COMPLETE |
| **M1-02** The group is a real object | COMPLETE |
| **M1-03** Media-specific group previews | COMPLETE — documents reached in M2-06 |
| **M1-04** Outliers found for you | COMPLETE |
| **M1-05** Confidence tiers | COMPLETE |
| **M1-06** Pages that do not survive their tables | **PARTIAL** — Browse and Search complete; Health at 1.8 s against "well under a second", carried to M2 |

Nothing here is called complete because a template exists. M1-03 built four
faces and for a milestone only three of them could be reached, and it said so
until the fourth had a real workflow behind it — see M2-06.

---

## M1-01 · Measure the current program at scale

**P0 · M · Low risk · DONE 2026-09-01** — results in
[performance.md](performance.md), raw data in [measurements/](measurements/),
harness in `scripts/scale_bench.py`.

> **What it found**, and what has since been done about it: Review and Health
> did not work at 100,000 files, let alone a million. **Both are fixed** —
> Review 543 ms and Health 2.1 s at a million, measured 2026-09-02. Commit, Quarantine and Dashboard hold. Search and Browse degrade
> linearly with clear, small causes. Review is now fixed (see M1-02); Health is
> M1-06.
>
> And the human-decision number, **corrected 2026-09-02**: a million files reach
> a person as **9,405 decisions** where the coherent answer is **8,889**. The
> first figure published here — 16,930, every group split — was a bug in the
> measurement, not a fact about the program: it replayed the raw confidence sort
> while Review already orders by group. Grouping is doing its job; the page
> boundary costs about 6%.

**Problem.** The only end-to-end evidence is a 50,000-file run from
2026-07-22, recorded in [performance.md](performance.md). It predates
Collections, relationships, findings, photo similarity, Storage Optimization
and Decision Memory. Every claim about a million files is currently a guess,
and designing against a guess is how subsystems get rebuilt twice.

**Desired outcome.** You know, with numbers, which surface breaks first and at
what population — and, critically, **how many human decisions a library of a
given size actually generates**. A library that produces 90,000 decisions has
failed its human-scale objective however fast the SQL is.

**Existing foundation.** `scripts/perf_smoke.py`. `tests/test_scale.py`
already states the right rule — bounded SQL queries, server-side filters,
pagination, aggregate summaries — and holds it at 10,000 rows for Quarantine
and Commit.

**Work.**
- A synthetic database fixture at 100k / 300k / 1M rows. Database populations,
  not real files.
- Every paged surface measured at each population: Review, Browse, Search,
  Health, Dashboard, Commit, Quarantine, Audit. Response time *and* query
  count.
- Findings at 1k / 10k / 50k / 100k.
- Worker throughput per stage: scan, hash, dedup, analyze, content, companions,
  audit.
- A **decisions-per-100k-files** figure, from a realistic mix.
- A written report committed alongside the numbers.

**Do not.** Generate a million real files. Change architecture during
measurement — measuring and fixing in one pass produces neither. Replace SQLite
because a number looks large.

**Acceptance.** A committed report with per-surface timings and query counts at
each population; `test_scale.py` extended so the bounded-page rule is held on
*every* paged surface; a named, ordered list of what breaks first.

**Scale.** 1,000,000 rows / 20+ TB.

---

## M1-02 · The decision group becomes a real object

**P0 · L · Medium risk**

**Problem.** Measured by M1-01, and the honest version is narrower than this
item first claimed.

Review pages first and groups second: `_proposal_rows` applies `LIMIT 50 OFFSET
n` and `_group_rows` groups whatever landed, so a group larger than a page is
split by construction and `_fold_singletons` — seeing one page — concludes the
fragments are not groups. That is real: 352 of 1,239 groups at a million.

But it is **not** most of the cost. Ordering already puts a group together
(`_order_by` sorts by `g.kind, g.label` before confidence), so the page boundary
costs about 6% of decisions — 9,405 against an ideal 8,889 — and not the 16,930
this item was written against. That earlier number was a measurement bug.

**So the case for this item is the experience, not the arithmetic.** 150
photographs as a thumbnail grid with the three odd ones flagged, answered once,
is worth building. It is not worth building because it saves 516 decisions. The
lever on the count is M1-05.

The precondition — `audit_view` materializing the entire findings table on every
render, 50,000 statements before a proposal was looked at, a page that never
finished at any population — is **done**.

**Desired outcome.** Review pages *groups*. A group knows its own size, its own
confidence and its own membership, whether it holds three files or three
thousand, and answering it is one action.

**Existing foundation.** The `groups` table; `group_kind` / `group_label`
already joined into the Review query; and `inbox_collections.py`, which already
models "a folder is one arrival and one decision" with its own page and its own
bounded counts. That is the shape — this generalizes it.

**Work.**
- ~~Bound `audit_view` first.~~ **Done 2026-09-02**, and with it the whole of
  Review's scale problem: **543 ms at a million on 186 statements**, and 186 at
  100k and 300k as well. Four fixes, each found by profiling after the previous
  one landed — see [performance.md](performance.md). Review is no longer a
  scale item; what remains here is the interaction design.
- ~~Group identity, counts and confidence resolved in SQL, not in Python after a
  `LIMIT`.~~ **Done 2026-09-02.** `_unit_select` aggregates the existing
  `groups`/`proposals` tables into one row per decision — total members,
  best/worst/mean confidence, how many are doubtful, how many have no
  destination, category — and the page is a `LIMIT` over *that*. No new table:
  `groups` already says what belongs together, and a materialised copy would be
  a second account of the same fact.
- ~~An ordering that keeps a group together.~~ **Already true**, which the
  M1-01 correction established: `_order_by` sorts by `g.kind, g.label` before
  confidence.
- ~~Group-level paging, with member paging inside a group.~~ **Done
  2026-09-02.** The page is 25 *decisions*; each shows five members from one
  `ROW_NUMBER() OVER (PARTITION BY unit)`, and the rest arrives 25 at a time
  from `GET /review/group/{unit}`. At a million: **40 statements for page 1**,
  34 for page 50, and neither moves with the library or the queue.
- ~~One approve action per group, with per-member removal before it.~~ **Done
  2026-09-02**, and the approve half needed a correction the same day. A group
  has two counts — how many files it holds, and how many the current filters
  are about — and the button said "all 120" while `hx-vals` posted a view in
  which 150 matched, so the server resolved the group honestly under the wrong
  filters. Now `ReviewFilters.form` is posted whole, the heading says both
  numbers, and the button names the one it will act on: *Approve 73 matching*,
  never *all 120*. Per-member removal is M1-04 — splitting a member out of a
  group is the outlier decision, and inventing half of it here would have been
  a control that could not finish its sentence.
- ~~An ordering that keeps a group together.~~ **Already true** (above). What
  did need fixing is the order *inside* one: members were seated by confidence,
  so an album read 7, 6, 5, 4, 2. They are seated by destination now, which
  spells track order, episode order and shutter order without a rule per medium.
- The existing flat and sorted views kept for the cases where a list is right.

**Do not.** Materialize a groups-of-proposals table before M1-01 says a
windowed query will not do. Let a group become a plan operation — a file
belongs to exactly one operation, and that does not change. Break the existing
Collections page; absorb it.

**Acceptance.** A 3,000-file group renders in one bounded page with an exact
count; approving it produces one decision; paging its members neither repeats
nor drops a row; the query count does not grow with group size, with the
findings table, or with the library. The remaining `xfail(strict=True)` on
unbounded library scans passes and its marker is removed. The decision count
moves from 9,405 toward 8,889 — a small number, and not the reason to do this.

**Scale.** Thousands of groups, hundreds of thousands of members.

---

## M1-03 · Media-specific group previews

**P1 · L · Low risk**

**Problem.** Every category is reviewed through the same row. A hundred
photographs and a hundred invoices are the same list of paths, and the fastest
way to check either one — look at them — is behind a per-row expander.

**Desired outcome.** A group is reviewed in the idiom of what it holds.
Photographs: a thumbnail grid, click to zoom, easy to remove one. Documents and
books: a cover or first-page grid with title, author, type and the evidence
behind them. Video: poster frame, identity, metadata. Music: artist, album,
track, catalog evidence, cover art, format and quality.

**Existing foundation.** `web/thumbs.py`, `preview_card.html`, `lightbox.js`,
`previews.js` and `photo_group.html`. The thumbnail machinery exists and is
wired per-row instead of per-grid.

**Work.** A grid renderer per media kind, chosen from the group's category;
bounded thumbnail loading; zoom and remove-from-group in the grid; the row view
kept for mixed groups.

**Done 2026-09-02**, except remove-from-group, which is M1-04's decision — see
the note under M1-02. Four faces over one foundation: `LAYOUTS` maps a group's
category to `partials/members/{photos,music,video,rows}.html`, and the heading,
the two counts, the whole-group buttons, the five-member preview and the paged
expansion are shared by all of them. A cell is a *face*, not a smaller row: it
carries the picture or the identity and the same `proposal_id` checkbox the
ordinary row carries, and `GET /review/proposals/{id}/row` fetches the real row
when somebody wants the destination, the evidence or the edit panel. There is
one row implementation and none of this is a second.

**PARTIAL, and the open half is named below.** Photographs, music and video
are complete. **Documents and books never form a group** — a group is an album,
a season, a disc, a camera event or a project, and a book is none of them — so
a document *grid* would have been a template no page could reach, and it is not
built. The medium got its row presentation instead: the first page beside the
title, author and type it already showed. That is a real improvement and it is
not what this item asked for, so:

    document row presentation                COMPLETE
    document/book grouping + group grid      COMPLETE — M2-06, 2026-09-04

The grid was always cheap. What did not exist was a *defensible reason* for two
documents to be one decision, and inventing one to satisfy a checkbox would
have produced exactly the wrongly-grouped headings M1-04 says are worse than
none. M2-06 wrote the reason first and the grid second, which is the order this
item was held open for.

And **the harness could not see any of this**: `scripts/ui_check.py` renders over `file://`, where
`/preview/items/…/thumb` resolves to nothing, so a grid of photographs
photographed as a grid of alt text. It inlines previews as `data:` URIs now.
The dev fixture had no groups at all, and now has three.

**Do not.** Force one generic row interface on every category. Load a grid
unbounded — a 4,000-photo group is one bounded page like everything else.

**Acceptance.** Each category's group renders in its own idiom; a 200-photo
group scrolls one bounded page of thumbnails; the thumbnail cache stays inside
its byte budget. A picture is offered only where one can be rendered — images,
video, and the PDFs a first page can come from — so an `.epub` or a `.txt`
never draws a broken image where its face should be.

**Met for photographs, music and video from M1-03, and for documents and books
from M2-06**, which built the groups this face had nothing to draw.

**Scale.** Groups of thousands.

---

## M1-04 · Outliers found for you

**P1 · M · Medium risk**

**Problem.** Finding the three wrong things among a hundred and fifty means
reading a hundred and fifty rows.

**Desired outcome.** A very strong mismatch is automatically split into its own
Review group. A possible mismatch stays in the group and is visibly flagged.
The system helps you find the exceptions instead of making you inspect equals.

**Existing foundation.** Confidence is already on every proposal;
`similar_media`, `photo_group`, `album_identity` and `consistency` already
compute the cohesion signals a mismatch would contradict.

**Work.** A cohesion measure per group; a split threshold and a flag threshold,
both stated and both settable; every split carries its reason in the UI.

**Done 2026-09-02**, and the shape of it is the finding. The split is
**derived, never stored** — `groups` still says what belongs together, and who
currently disagrees with it is computed from where each member is going — which
is what makes "turning both thresholds off returns exactly today's behaviour"
true by construction rather than by a second code path.

One signal splits, and it is structural: `groups.dest_base` is the folder the
group was formed around, so a member whose destination is not under it is not a
doubt about that file, it is a statement that the file belongs somewhere else.
Everything weaker flags in place. Deliberately row-local: deciding this by
comparing each member against its group's *most common* destination would need
a window over every group in the library on every page render, which is the
unbounded shape M1-01 spent a day removing.

An outlier is a unit in every sense — `_UNIT_SPLIT` is the one expression the
page, the preview, the expansion, the per-unit totals and `unit_proposal_ids`
all key off — so it has its own count, its own heading and its own action, and
the group it came out of excludes it from all three. That is what lets a
derived split coexist with the action-scope rule from M1-02 instead of
undermining it. It sorts immediately after its parent, and an exception of one
is never folded into the loose pile: a group of one is not a group, but one
file that is not going where its group is going is the entire case.

The flag half needed one more thing than a count. "3 to look at" over a hundred
and fifty files names a number and gives no way to reach it, which leaves
reading a hundred and fifty rows as the way to find three — so the badge is a
control: it replaces the members shown with exactly those three, and offers the
way back.

**Do not.** Split on a single weak signal. A wrongly split group is worse than
a flagged one, because a flag is read and a split is trusted.

**Acceptance.** The "three wrong among a hundred and fifty" case is findable
without opening a hundred and fifty rows; every automatic split names why;
turning both thresholds off returns exactly today's behaviour. All three are
pinned by `tests/test_web_review.py`, the last one by `review.outliers.split`.

**Still open:** removing a member from a group by hand. The split answers the
machine-findable case; the case where a person looks at a grid and says *that
one does not belong* has no control yet. It was deliberately not invented
alongside the group actions in M1-02 — a control that could not finish its
sentence would have been worse than none.

**Scale.** Thousands of groups.

---

## M1-05 · Confidence tiers, including deterministic Ready for Commit

**P1 · M · Medium risk**

**Problem.** Every decision costs the same amount of attention regardless of
how certain it is.

**Desired outcome.** Three tiers. **Uncertain** asks, and preselects nothing
where evidence conflicts. **Strong or learned** preselects the recommended
answer and labels why. **Deterministic** arrives in Ready for Commit already.
Hundreds of decisions become a handful of meaningful confirmations — and
**nothing executes without Commit**, which this does not touch. This changes
where a decision waits, never whether you take it.

**Existing foundation.** `CONFIDENT` and "approve all confident ≥ 0.85" already
exist in Review; `decisions.suggest` already preselects and explains.

**Work.** A stated evidence rule per tier; the deterministic tier wired to the
existing Commit queue; an audit trail for why any item reached it; a setting to
disable the deterministic tier entirely.

**Done 2026-09-02.** The rule is in `librairy/confidence_tiers.py` and it is
about **evidence, not the score**: `0.92` off a filename heuristic and `0.92`
off an AcoustID match are the same number and are not the same claim. What
settles a decision is an identity — a catalog match on this recording, an ISBN
or a DOI printed in the file — read from the same `STRONG_SOURCES` /
`STRONG_FIELDS` that already keep a learned habit quiet, so the tier and the
authority order cannot drift into two opinions. A learned pattern, an AI cue
and a filename guess can reach `suggested` and never `settled`.

Stored on the proposal (**migration 049**) rather than derived per render, for
the same reason `confidence` is: "24 settled by identity" is a number above a
list, not a number found by reading twenty-four rows, and the evidence it comes
from is a JSON blob no index can answer a question about. Written by
`upsert_proposal` from the evidence it already validates, so it is recomputed
exactly when the evidence changes.

The column is a fact about the evidence; `settled_now` is a fact about the
moment. An arrival that turns out to be a second copy of something filed, a
cross-root comparison nobody has answered, a model that disagrees about what a
picture is — none of those is knowable when a proposal is written, and each one
makes a settled filing a question again. Both the button and the automatic
approval ask.

**The deterministic tier ships in two halves, both on.** The button — *Approve
24 settled* — is the manual one, and every row under it can say what identified
it. `review.settled.auto_approve` is the automatic one and is **on by default**:
a deterministic answer needs no asking, which is the product decision this item
was written for.

What it must never do is reach *backwards*. Deciding for somebody is the
product decision; deciding for them retroactively is a different one, and it is
the one an upgrade would make by accident — a queue of four hundred files
somebody has been working through for a fortnight, answered while they read the
release notes. So the boundary is durable and stamped at migration time, and it
is two numbers because one cannot be exact: `review.settled.activated_at` and
`review.settled.activated_after_id`. A proposal is eligible if it is newer than
the boundary **or has been re-analysed since it** — the id because `utc_now()`
has no sub-second part and a fresh install writes its first proposals in the
same second it is created, the timestamp because an id cannot see a reprocessed
file, and re-analysing an old file is a new decision about it.

`librairy/settled_queue.py` runs on an idle cycle, takes an Undo snapshot
first, and deliberately does **not** call `remember_approvals`: a program that
learns from its own automatic decisions is citing itself as evidence.

Sweeping the pre-existing backlog on request — *Apply deterministic rules to
existing backlog* — is deliberately not built. It is one call with the boundary
lifted, and it is a decision about four hundred files that nobody has asked for
yet.

**Do not.** Let a learned pattern reach the deterministic tier — it is
authority level 4, permanently. Build this as a second automation system beside
Decision Memory; they are one model with one explanation (see M2-04).

**Acceptance.** Each tier's rule is written down and tested; every
deterministic item can answer "why am I here"; disabling the tier restores
today's behaviour exactly. All three in `tests/test_confidence_tiers.py` and
`tests/test_web_review.py` — the second one derived from the evidence rather
than stored beside it, because two records of why can disagree and one cannot.

The fourth thing tested is the one that is not in the acceptance criteria and
matters more than the default: **upgrading with a backlog must not silently
answer it**, pinned by `test_upgrading_never_answers_the_queue_somebody_was_
working_through`.

**Scale.** Tens of thousands of pending decisions.

---

## M1-06 · The pages that do not survive their own tables

**P1 · M · Low risk** — added 2026-09-01 from M1-01's measurements.

**Problem.** Three surfaces have a specific, small, non-architectural fault that
makes them degrade or fail with the size of a table they read. All three are
named with file and line in [performance.md](performance.md).

- ~~**Health is quadratic and unusable at 100,000 files.**~~ **DONE
  2026-09-02 — 22.3 s to 1.8 s at a million**, and it completes at every
  population. Four causes, all measured: the quadratic `unindexed`, an
  `OR`-joined dependency query at 17 s, `PRAGMA quick_check` verifying the
  whole database on every render, and — the last of them — the same three
  counts asked twice from two parts of the page that could not see each other,
  1,373 ms of one render. Counted once now and `current` derived by subtraction
  rather than paying 325 ms for a join that returns the same number. The
  original text follows because the shape is worth remembering.
  `search_health.py:156` counts unindexed items with a `NOT EXISTS` against
  `search_fts`, whose `item_id` is declared `UNINDEXED` — so it scans the whole
  FTS table once per row of `items`. The query planner says so without any
  timing. `health_data` then asks for it **twice** per render. Health is the
  page that exists to say something is wrong, and it is the most broken page in
  the program.
- ~~**Search counts history per result row.**~~ **DONE 2026-09-02 — 153
  statements to 6.** Three queries per result, not one: the item, its proposal,
  and the history count. Batched to three for the page, and `history` gained
  the index it had never had (**migration 050**). The latency did not move,
  because the per-row queries were never the latency — 31 ms of a 2.2 s page —
  and the real finding is below.
- ~~**Browse home reads the whole library in one statement.**~~ **DONE
  2026-09-02 — 1,188 ms and a million-row read, to 0.2 ms and one statement.**
  Comparing the library against the index is maintenance, not a render: the
  worker does it on an idle cycle and Browse reports the verdict and its age.
  The same arrangement `PRAGMA quick_check` already has, and it also fixes the
  half this harness never measured — the filesystem walk, which has no files to
  walk here and would have every one of a million in a real library.

**Desired outcome.** Every surface's cost is bounded by what it displays, not by
what the database holds.

**Work.** A different shape for the unindexed count, and asked once. A batched
history count, or an index, or neither if the number turns out not to be worth
a query. An index or a bound for Browse home.

**Do not.** Fix these by removing what they tell people — the unindexed count is
one of the few genuinely important numbers on Health. Add an index without
measuring that it is used. Treat this as architecture: it is three queries.

**Acceptance.** Every surface's cost bounded by what it displays.

    Browse      COMPLETE   0.2 ms, one statement, at any population
    Search      COMPLETE   6 statements; under 2 ms for a real query
    Health      PARTIAL    1.8 s at 1M against a target of "well under a second"

**Health is the one that did not fully land, and the reason is worth keeping.**
What remains is not a mistake anywhere: counting an FTS5 table means reading it
(224 ms at a million) and relating its rows to `items` means joining it
(321 ms). Roughly a second of Health's 1.8 s is those two questions, asked once
each, with no cheaper shape found. Making them cheap needs a *recorded* verdict
— the arrangement `PRAGMA quick_check` and the FTS integrity check already have,
and which Browse's consistency line just joined — or a different index shape.
Neither is speculative and neither is done. **Carried into M2** rather than
called complete.

**Search's headline was a harness artefact, and that is the finding.** `Album*`
matches all one million rows of the synthetic library, because the generator
names every file after its category; a query that names a file has always been
under two milliseconds. The genuine improvement underneath — ranking inside the
index before joining, 4.7× — was implemented, measured and **reverted**: it
moves the liveness filter after the `LIMIT`, which shortens a page and pushes a
real result onto the next one, exactly as `search._where` warns and
`tests/test_search_stale.py` catches. Carried into M2 with the constraint
attached.

**Scale.** 1M items, 100k history rows, 50k findings.

---

**M1 gate.** Run it against the real inbox on the NAS. Refine what real use
finds. Then decide whether to publish.

---

# M2 — Intelligence and processing

## M2 at close — 2026-09-04

| | |
|---|---|
| **M2-01** Waiting for AI | COMPLETE |
| **M2-02** Documents that disagree | COMPLETE |
| **M2-03** Resource modes and a separate AI limiter | COMPLETE — two partials accepted: NAS-under-load belongs to production validation, and the Settings 375px overflow to the UI track |
| **M2-04** Decision Memory, context-aware | COMPLETE |
| **M2-05** Tags, and Projects | COMPLETE — one semantic correction after review, see the entry |
| **M2-06** Documents that belong together | COMPLETE — and M1-03 with it |

### The gate

Six features passing is not six features holding together, and the difference
is where the defects were. `tests/test_m2_integration.py` makes only the claims
no single feature can make, and it exists because every cross-feature bug this
milestone produced was invisible from inside the feature that caused it:

* a tagged file **lost its tag** when nothing could classify it — recording sat
  after the branch a held file takes
* a **companion was held**, because the pass that associates artwork looks for
  undecided *proposals* and a held file has none
* a document set that grew **forked into two identical headings**, because its
  base is a majority and can move, and the group was found by its base

What the gate holds: nothing M2 added can move a file; a resource mode changes
*when* work happens and never what the answer is; a held file blocks nothing
and keeps its tag; the three waiting reasons still mean three different things;
and a tag and a rule still reach a destination by the same single path.

### Carried out of M2

* **M1-06 PARTIAL** — Health at 1.8 s against "well under a second". The
  residual is FTS counting, and it is a measurement pass of its own.
* **The Settings page at 375px** overflows by 84px, from the
  `<label>sentence <select></label>` pattern used on every field. A UI pass,
  not a resource-modes pass.
* **NAS responsiveness under load** cannot be measured on the test rig. It
  belongs to production validation.
* **An organization on a financial document.** M2-06's `document_set` needs
  one and nothing reads a bank's name off a statement, so a year of statements
  groups only if somebody tags them.

## M2-01 · Waiting for AI

**P1 · M · Medium risk · DONE 2026-09-03** — `librairy/waiting.py`, schema 51,
`tests/test_waiting.py`.

> **What shipped.** A durable lifecycle state (`items.state = 'waiting'`) and a
> row saying why, in three reasons that are different questions: nothing could
> be asked, something was reached and broke, everything was asked and it was
> still not enough. The first two resume by themselves when a provider answers
> again; the third resumes for nobody and says so. Held files appear as a
> section of Review and a concern on Health, are answerable by hand at any
> moment, and never block the rest of the inbox.
>
> **What it needed that was not planned for.** Ollama and LM Studio both
> swallowed a refused connection into the same `None` they return for "I have
> nothing to say", so "the provider is unavailable" and "the provider had
> nothing to add" were the same fact from outside. `ProviderUnreachable` is
> what makes the three reasons distinguishable at all.
>
> **Carried to M2-03, and done there.** The recovery probe's interval is the
> Local AI mode's — never for Off, five minutes for Limited, thirty seconds for
> Full Power — and switching AI off stops the worker knocking on a door the
> owner has closed.

**Problem.** When deterministic and catalog evidence are not enough and the
configured AI provider is disabled or unreachable, `ai/orchestrator.py` logs
"providers unavailable … continuing with deterministic results" and classifies
from heuristics anyway. Weak evidence silently becomes a proposal.

**Desired outcome.** The item is held out of the normal Review queue and marked
**Waiting for AI**, with a count and a link in Review and Health. You can open
that list and decide any of them by hand at any time. When the configured
provider becomes available again, they resume automatically. Nothing is ever
invisible, and nothing is ever stuck.

**Existing foundation.** `lifecycle.py` already owns item states and their
legal transitions; `ai/status.py` and `provider_status` already know whether a
provider is answering.

**Work.** The state and its transitions; the held list, reviewable by hand;
automatic resume on provider recovery; manual pause and resume; AI status
surfaced in Review and Health — provider online or offline, waiting count,
what is in flight, failures, processing mode.

**Do not.** Hard-code any one provider. LM Studio, Ollama and anything else
supported are equal citizens: the terminology throughout is *configured AI
provider*, *local AI provider*, *provider unavailable*. Build a separate AI
Queue page. Let held items age out or be discarded.

**Acceptance.** With a real batch in flight, stop the **configured** provider —
whichever it is: nothing is guessed, nothing is lost, the waiting count is
visible in Review and Health, every held item can still be decided by hand, and
the batch finishes itself when the provider returns. The test runs against
whatever provider the configuration names.

**Scale.** Tens of thousands held.

---

## M2-02 · Documents that disagree

**P1 · L · Medium risk · DONE 2026-09-03** — `librairy/document_identity.py`,
`librairy/ocr.py`, `tests/test_document_identity.py`.

> **What shipped.** Documents are no longer identified by a ladder that stops at
> the first source that answers. Every source that names one — the filename, the
> embedded metadata, the first page, OCR where there is nothing to extract, and
> a catalog — is compared, and what they *add up to* decides. Two independent
> sources naming the same work is what earns a preselection; sources that
> disagree produce a recommendation, a reason, and a question on the row.
>
> **Why comparison and not a better ordering.** Authority alone re-creates the
> bug. The embedded title outranks the first page under every ordering anybody
> would write down, and in the `CRACKING` case the embedded title is the one
> that is wrong. What distinguishes the right answer is that three sources name
> a version of "Programming Rust" and one does not — a fact about the set, not
> about any member of it. Authority still picks the *wording* among the sources
> that agree, so a resolved ISBN's "2nd Edition" beats a running header.
>
> **OCR is off by default and mostly a list of reasons not to run.** Not a PDF,
> has a text layer, switched off, no tesseract, the mode says not now, the cycle
> has read enough — six gates, and what passes all six gets two pages rather
> than a document. It is governed by the **processing** mode and not the AI one:
> tesseract makes no judgement, and switching Local AI off must not stop a
> scanner's output being readable. Quiet rations it to two documents a cycle
> rather than refusing it, so a mode still changes the rate and never the answer.
>
> **Two false conflicts the fixture found**, both fixed and both worth naming: an
> arXiv identifier is not a title (a name with no word in it names nothing), and
> a filename that loses to two agreeing sources *inside* the document is an old
> name rather than a disagreement.
>
> **Document grouping is untouched, and M1-03 stays PARTIAL.** Nothing here
> creates a document group, so the group grid still has no workflow that reaches
> it. That remains M2-06, and the distinction stands: document *row*
> presentation works and is now richer; document *group* presentation is
> complete when a real workflow can reach it and not before.

**Problem.** A PDF whose embedded Title reads `CRACKING` becomes a file called
that. Found in real use.

**Desired outcome.** Filename, embedded metadata, content-derived title and
catalog identity are compared. Strong agreement preselects a clean, meaningful
title. Disagreement reaches Review as a disagreement, showing all four and
asking.

**Existing foundation.** `docmeta.py` already implements exactly the right
ladder — embedded metadata, then ISBN/DOI, then front-matter text, then the
filename — and already refuses to guess, reporting a scanned PDF as scanned
rather than inventing a title for it. `document_works.py` already recognises
editions and revisions of one work. This is not "add OCR"; it is finishing what
is there.

**Work.** OCR for the case `docmeta` already identifies — pages present, no
extractable text. The comparison surface in Review. Catalog identity folded in
as a fourth source.

**Assumption, stated:** OCR ships **opt-in, default off**, runs under the
resource limiter from M2-03, and never outranks deterministic metadata. It adds
a dependency to the image and its size is called out in the release notes.

**Do not.** Trust embedded Title. Let OCR text overwrite a catalog identity —
OCR is content evidence, authority level 3 at best and often 5. Run OCR on
documents that already have extractable text.

**Acceptance.** The `CRACKING` case reaches Review as a disagreement with all
four sources shown; a scanned manual with an ISBN on page one is identified;
OCR off changes nothing about today's behaviour.

**Scale.** Tens of thousands of documents; OCR is bounded by the limiter, not
by the queue.

---

## M2-03 · Resource modes and a separate AI limiter

**P1 · M · Low risk · DONE 2026-09-03** — `librairy/resources.py`,
`tests/test_resources.py`, measurements in
[performance.md](performance.md#m2-03-2026-09-03--what-each-processing-mode-costs).

> **What shipped.** Two settings — *Overall processing* Quiet / Balanced / Full
> Power, and *Local AI* Off / Limited / Normal / Full Power — with the
> per-workload numbers derived from them and not exposed. `ResourcePolicy` is
> `resources.EncoderPolicy` now, unchanged in every field, and it is one of
> several workloads a mode governs rather than the only one there is. Balanced
> and Normal reproduce the previous behaviour value for value.
>
> **What the measurement corrected.** The batch cap was expected to be what
> makes Quiet quiet, and it is not: a Quiet cycle costs 0.72 CPU seconds per
> wall second against Balanced's 0.76, because a cycle's fixed costs do not
> shrink with its batch. The cap buys a *shorter* cycle and the pause after it
> is where the difference lives — sustained, 0.34 against 0.70. The per-file
> cost goes up 3.7× as a result, which is the trade the mode actually is, and
> it is written down rather than left to be discovered.
>
> **Not done, and deliberately.** "Quiet leaves the NAS responsive under a full
> inbox, measured" is measured on a laptop, not a NAS serving video off the
> same disks. That half of the acceptance needs the production machine and is
> the one thing here that a build agent cannot answer.
>
> **And it found something else.** Pointing `scripts/ui_check.py` at Settings
> for the first time reported the page **514px wide at a 375px viewport** — a
> pre-existing, page-wide overflow that no pass had measured because Settings
> was not one of the harness's pages. Three fixes landed because they are
> correct in their own right and verified against every other page: grid items
> in `.shell` and `.metric` may now shrink, and the four path boxes stack under
> their labels instead of sitting beside them. That took it to **459px**.
>
> The remaining 84px is the same pattern repeated: `<label>some sentence
> <select></label>` on nearly every field, whose min-content is the sentence
> plus a browser-default control. Fixing it properly restyles every field on
> the page, which is a UI pass and not a resource-modes pass, so Settings is
> **not** in the harness's page list yet — a check that always fails is a check
> that stops being read. Carried as M2 polish, with the number written down.

**Problem.** There is no way to tell LibrAIry to be quiet. The only resource
control in the program is `ResourcePolicy`, and it exists solely inside Storage
Optimization.

**Desired outcome.** Two simple settings. **Overall processing:** Quiet /
Balanced / Full Power. **Local AI:** Off / Limited / Normal / Full Power,
limited independently of everything else. Advanced controls may exist
underneath where they earn it.

**Existing foundation.** `ResourcePolicy`, `optimization_process.py` and
`optimization_exec.py` already know how to run something on a fraction of the
machine, including the measured `Low` policy. The worker's priority tiers
already work and are not in scope to change.

**Work.** Generalize `ResourcePolicy` out of Storage Optimization; a mode
setting per axis; per-workload interpretation — scanning, metadata, OCR,
similarity, AI and transcoding do not want identical throttling; the active
mode visible on the Dashboard.

**Do not.** Expose fifteen worker tuning knobs by default. Change the priority
tiering in `worker.run_once`, which already does what Direction 2 asks. Suspend
a running encode when a file lands in the Inbox — that trade is already
deliberately refused and the reasoning is in the code.

**Acceptance.** Quiet leaves the NAS responsive under a full inbox, measured;
the AI limiter bounds provider load independently and provably; the mode is
visible without opening Settings.

**Scale.** Whole-machine.

---

## M2-04 · Decision Memory, context-aware

**P2 · L · Medium risk · DONE 2026-09-03** — `librairy/rules.py`, schema 52,
`tests/test_rules.py`.

> **What was already true, and is now pinned.** Domain scoping did not need
> building: every cue `decision_cues.cues_for` produces carries the category, so
> a habit learned from books is a *different string* from anything a music row
> can match — not a filter applied afterwards. Negative learning did not need
> building either: an override is recorded as the ordinary decision it is, the
> history divides, and `_dominant` stops finding an answer. Both now have tests
> that fail if either stops being true.
>
> **What was built.** Promotion. A pattern that has been right often enough —
> eight decisions, and five times as many confirmations as departures — earns
> an *offer* on the Decision Memory page. A person presses the button, and there
> is a rule: named, listed, explainable, switchable, removable. Nothing else in
> the program can call `promote`, and a test asserts that by reading the source.
>
> **The line, stated once.** Repetition earns the offer and never the rule.
> "You have done this eighteen times" and "you have decided this is how it
> works" are different claims, and a program that turns the first into the
> second on a count has taken a decision that was not its to take.
>
> **One authority model, not four.** A rule is level four, exactly as a learned
> suggestion is. It fills an answer in; it cannot approve, commit or settle, and
> it loses to a catalog identity about the file in front of it. What promotion
> changes is durability — a rule keeps offering when the counting behind it has
> gone quiet, because it is the owner's statement rather than an observation
> about them.
>
> **Overriding a rule is counted and never acted on.** A suggestion weakens
> itself because it is a claim about behaviour and the behaviour changed. A rule
> is a claim its owner made, so the page says "you have filed four of these
> somewhere else since" and leaves the decision where it belongs. Switching off
> a policy somebody wrote down, on a count, is the same overreach as creating
> one on a count.
>
> **Widening is its own press.** A rule is created at the width it was learned
> at, category included. Making one apply everywhere is a separate, confirmed
> action, because a filing policy learned from invoices and applied to
> everything renames a photograph the first time it matches one.

**Problem.** A habit learned about invoices should not casually become a rule
about photographs.

**Desired outcome.** Learning scoped by domain first — music habits, book
habits, invoice habits, photo habits — with a small number of explicit global
rules where they genuinely apply. When a choice has repeated enough, LibrAIry
offers *"you have chosen this 18 times — save it as a rule?"* and **you**
promote it.

**Existing foundation.** `decisions.py` already records, settles, tallies,
generalizes, suggests and suppresses; `learned.html` already shows patterns;
the authority order is already documented and enforced.

**Work.** Domain scoping on patterns; a promotion action; promoted rules as a
first-class, listable, revocable thing; reconciliation with the M1-05 tiers so
there is one story about automation, not two.

**Do not.** Create a trusted rule silently. Let a promoted rule reach authority
level 3 — a rule is still the owner's habit written down, and a catalog
identity still outranks it. Let a rule approve or commit anything.

**Acceptance.** A rule promoted in one domain provably does not fire in
another; every promoted rule can be listed, explained and revoked; Review and
Decision Memory give the same explanation for the same preselection.

**Scale.** Tens of thousands of recorded decisions.

---

## M2-05 · Tags, and Projects

**P2 · M · Low risk · DONE 2026-09-03** — `librairy/tags.py`, schema 53,
`tests/test_tags.py`.

> **All five rows of the table below now pass.** A hashtag in a file's own name
> is read; every tag is kept with where it was written; `nearest` is a stated
> rule rather than `tags[0]`; the tag survives filing because it is stored
> against the *item*, not the path; and it is explicit evidence in the decision
> being made now, on the one existing authority path.
>
> **The store is keyed to the item, and that is the whole fix.**
> `items.relpath` changes when a file moves and `items.id` does not, so
> `#ProjectHouse` is still true a year later — where before the tag lived in a
> proposal's evidence, was stripped out of the clean name on the way to the
> library, and was gone by the time anything re-read the file. Migration 053
> backfills every tag already in existing evidence, so upgrading does not lose
> the ones somebody already wrote.
>
> **`nearest`, stated:** the most specific *place* wins — the file's own name,
> then the deepest folder outward — and within one name the first tag written.
> Nothing is discarded to get there; `#ProjectHouse` and `#Invoices` on one
> file are two true things.
>
> **What a tag is worth, corrected.** The first version of this entry said a
> tag reached a destination "as a Decision Memory cue and by no other route",
> which made an explicit hint *weaker* than a learned one until enough
> decisions had been watched to learn something about it. That is backwards.
> A hashtag and a habit about hashtags are two different facts:
>
>     #ProjectHouse                    what you are telling LibrAIry, now
>     "you file #ProjectHouse docs     what LibrAIry has learned you tend to
>      under Documents/House"           do with that kind of hint
>
> Both are kept, and the first does not wait for the second. A tag naming a
> promoted Project joins the file to it the moment it is read; every tag is
> explicit evidence on the proposal; and a tag rung is asked *before* an
> inferred one of the same width, which is where the ordering actually decides
> something — `suggest` breaks a tie by the order the rungs arrive in.
>
> Still one authority path, and still bounded on the other side: a tag names no
> destination and picks no category. `#ProjectHouse` on an installer leaves the
> installer alone, and where a file goes still comes from evidence about the
> file, a learned pattern, or a rule somebody promoted.
>
> **`nearest` is a tie-break, not a ranking.** It answers "which is *the*
> context" for the one caller that needs a single answer — a photo group has
> one heading. Every tag is stored, searchable, evidence, and its own rung of
> the ladder, so a rule about `#Taxes2026` is found on a file that also carries
> `#ProjectHouse`.
>
> **A tag can be given without renaming a file.** The item page takes one and
> records it as manual. Until then the only way to say `#ProjectHouse` was to
> rename the file, which is an odd thing to have to do to a document already
> filed — and it meant explicit evidence was something only the inbox could
> carry.
>
> **A Project is a promoted tag and nothing else.** Its members are the items
> carrying the tag, read back out of `item_tags`; there is no membership table,
> because a second copy of that would be free to disagree. Promotion is
> explicit, for the same reason a rule is: a tag on four hundred files is a tag
> on four hundred files.
>
> **The vocabulary collision is resolved and pinned.** A **Project** is a view
> across the library; a **Project folder** is `Projects/{project}/`, a filing
> destination. The category is labelled *Project folders* wherever a category
> name is shown, so a badge and a page heading cannot read as the same thing.
> The physical taxonomy is untouched. `tests/test_tags.py` holds both halves.
>
> **Not built, deliberately:** tasks, kanban, notes, calendars, deadlines. A
> Project answers what belongs to it, what kinds, what is filed and what is
> waiting — four aggregates and a bounded page of files.

**Problem.** Hashtags are half-built, and "Project" means two things.

Verified 2026-09-01 against the four behaviours that matter:

| | |
|---|---|
| Folder hashtags reach files beneath them | **Works.** `extract_hashtags` walks every ancestor folder, at any depth |
| A hashtag in a *file's* own name | **Missing.** It reads `parent.parts` only, so `IMG_4421 #Vacation2026.jpg` yields nothing |
| Evidence strength as intended | **Missing.** The 0.7 entry is consumed in exactly one place — a fallback for photo-event group labels. It influences no category, destination, naming or project association |
| Survives final renaming | **Partial.** Stripped from clean names and searchable, but only out of the proposal's frozen evidence JSON. There is no tag store, so re-analysis after filing reads the library path — where the tag is already gone — and loses it |
| Multiple hashtags | **Works**, with a wrinkle: all tags are captured and evidenced, but `nearest` takes `tags[0]` when a folder carries two |

**Desired outcome.** A hashtag anywhere in a file or folder name is strong
user-provided evidence that influences grouping, classification, destination,
naming and project association; it is removed from the final clean name; and it
remains yours — searchable and filterable — for as long as the file exists.
Promoting a tag makes a **Project**: a virtual, file-focused collection across
normal Library categories, with file counts, categories, recent activity,
storage used, unresolved items and backup status.

**Existing foundation.** `classify/hashtags.py`, `taxonomy._strip_hashtags`,
the `hashtag` evidence source, and the tags column already in the FTS index.

**Work.**
- Read hashtags from the filename as well as its ancestors.
- Give hashtag evidence real weight, in the places the authority order says it
  belongs — above heuristic and model cues, below catalog identity and explicit
  policy.
- A durable tag association that does not depend on a proposal row surviving.
- Sensible handling of several tags on one name.
- Promote-to-Project, and the Project view.
- **A vocabulary decision, taken during implementation, not deferred.**
  `Projects/` is and remains a filing destination for folders that genuinely
  arrive project-shaped. The virtual thing is a different concept and must not
  share its name in the UI. Resolve it in
  [ui-vocabulary.md](ui-vocabulary.md) before either name ships.

**Do not.** Move files when a tag is promoted. Make a Project a filesystem
silo. Add tasks, kanban, notes or calendars — a Project is about understanding
related files, and nothing else. Let a hashtag act: it is evidence, and Review
and Commit apply exactly as they always did.

**Acceptance.** Each of the five rows above passes; a promoted Project shows
its files across categories with nothing moved; the two meanings of "project"
have two names.

**Scale.** Thousands of tags, hundreds of thousands of tagged files.

---

## M2-06 · Documents that belong together

**P2 · M · Medium risk · DONE 2026-09-04** — `librairy/document_groups.py`,
schema 54, `partials/members/documents.html`, `tests/test_document_groups.py`.

> **M1-03 is now COMPLETE.** The fourth group face has a workflow that reaches
> it, which is the only thing it was ever waiting for.
>
> **Three reasons, and each one is a sentence on the group.**
>
>     book_series    one title in parts or editions — an explicit volume,
>                    part or edition marker over an identical stem
>     document_set   the same organization and the same kind of document
>     tagged_set     the same explicit tag and the same kind of document
>
> The heading says what the set is; the line under it says what makes it one
> decision, written when the reason was known rather than reconstructed by a
> page. That is the acceptance, and it is one stored column.
>
> **The rule earns its keep by what it refuses.** Not a category. Not a folder.
> Not an arrival: files dropped in together are related more often than not,
> and *more often than not* is the standard that writes a wrong heading — so
> arrival can strengthen a sentence a real relationship already earned and can
> never write one. Not a shared ISBN either: two files with one identifier are
> one work in two containers, which `document_works` already answers, and
> better — there, keeping both is a first-class outcome.
>
> **The guard that mattered most was not the one expected.** "The same kind of
> document" looked sufficient for a tagged set until it put a boiler manual and
> a novel under one heading: both `.epub`, so both typed `Book`, so "the same
> kind" was satisfied by a fact neither file had any choice about. A type earns
> a group only when it says **more than the category already did**. Two
> *invoices* tagged `#ProjectHouse` are a set; two *books* are two books.
>
> **A conflicted identity weakens rather than prevents — with one line drawn.**
> A source that actually read the document disagreeing stops a group: what the
> file *is* is the open question, and a heading is where a question goes to
> stop being noticed. The **filename** dissenting alone does not, because an
> abbreviated filename is the ordinary case for an ebook and treating it as a
> real conflict made every set of them ungroupable for the reason that says
> least about the files. Those members keep the lowered confidence M2-02 gave
> them and surface in the group's "to look at" count.
>
> **The outlier split is M1-04's, reused.** One correction was needed:
> `groups.dest_base` is now the folder a **majority** of members are going to,
> not the unanimous one. Unanimity let a single dissenting member erase the
> base for everybody — switching off the split in precisely the case it exists
> for. A book series still has no base, because each volume has its own folder
> and "not going where the group is going" is the design there rather than an
> anomaly.
>
> **A group is work still to be done.** One live document joining eight
> already-filed ones is one document, not a group of nine. Members keep their
> `group_id` after Commit, the way an album's tracks do, because what a group
> *was* is how Commit and History explain what happened.
>
> **What the integration gate found.** A set that grows moves its own base —
> the base is a majority — and the group was being found by `(kind, label,
> dest_base)` like every other kind. So a set of two that became a set of five
> filed somewhere else created a *second* group under an identical heading:
> the eleven-PDFs problem wearing the opposite hat. A document set is now found
> by its key, which is on its members and indexed, and its base moves with it.
> Only visible across two passes, which is why it took a gate rather than a
> feature test.
>
> **What did not come out as hoped.** The roadmap's own example — a year of
> bank statements — does not group yet. `document_set` needs an organization,
> and nothing reads a bank's name off a statement: the classifier extracts an
> organization for manuals only. Tagging them reaches the same place through
> `tagged_set`. Reading an organization off a financial document is its own
> piece of work and is not smuggled in here.

**Carried forward from M1-03**, which built the group presentation for every
medium that has groups and found that documents and books have none. A group is
an album, a season, a disc, a camera event or a project; a book is none of
those, so the document grid M1-03 asked for is a page nothing can reach.

**Problem.** Twenty scanned chapters of one manual, a three-volume set, a
year's bank statements and eleven unrelated PDFs are twenty-something separate
decisions with nothing to say they are related. The other media got "one
coherent thing is one decision" in M1-02; documents did not.

**Desired outcome.** Documents form a group **only when there is a defensible
reason** for two of them to be one decision, and then they are reviewed as one:
a cover or first-page grid, the identity and the evidence behind it, and a
single answer.

**Existing foundation.** Everything except the grouping rule. `classify/
grouping.py` takes a `GroupInput` and needs only a descriptor; the group
paging, the two counts, the whole-group actions, the bounded expansion and the
outlier split (M1-02, M1-04) are already medium-agnostic; the row already reads
a document's identity out of stored evidence; `partials/members/` already holds
three faces and a fourth is a template, not an architecture.
`document_works.py` already models *one work in several formats*, which is a
related question and not this one.

**Work.** A grouping rule with a stated reason per kind. Candidates, strongest
first:

- **a series or set** — a catalog identity that names one work in parts, or
  volume numbering under a shared title
- **a shared printed identity** — the same ISBN, the same DOI, the same
  organization and document type
- **an explicit hashtag or Project** (M2-05), which is the owner saying it
- **one arrival** — a folder dropped in together, which
  `inbox_collections.py` already models and deliberately treats as *a
  collection and nothing else*; promoting one to a decision-group is a real
  design question, not a default

Then the grid, which is small: `partials/members/documents.html` and one entry
in `LAYOUTS`.

**Do not.** Group documents by category, by folder, or by anything else that
would put eleven unrelated PDFs under one heading with one Approve button.
M1-04 states the rule this has to respect — a wrongly grouped set is worse than
an ungrouped one, because a heading is trusted — and a document group is the
easiest place in the program to get that wrong. Do not build the grid before
the rule: a face with nothing behind it was the reason this item exists.

**Acceptance.** Every document group can say what makes it one decision;
turning the rule off leaves today's rows exactly as they are; the grid renders
bounded like every other group. **All three met** — the third by using the same
member paging every other group uses, and the second is asserted rather than
argued: with the rule answering nothing, the documents are the rows they have
always been, no group, no heading, no key.

**Scale.** Hundreds of thousands of documents. One indexed column and one
aggregate per batch: the pass asks only about keys the batch produced, and a
group's base is two aggregated rows rather than a scan of its members.

---

# M3 — Visibility and distribution

## M3-01 · A small durable metrics model

**P1 · M · Low risk · DONE 2026-09-04** — `librairy/metrics.py`, schema 55,
`tests/test_metrics.py`.

> **Every metric is a recomputation, never an increment.** The one decision the
> rest follows from. A counter incremented as things happen has to be right
> about every retry, every crash and every half-executed plan, and can never be
> checked against anything. Each measure here is instead a query over data that
> is already authoritative — `history`, `quarantine_entries`, `proposals`, the
> `items` table — so re-running a day is the same answer again, and the primary
> key `(day, metric)` makes that a replacement rather than a second row.
> Idempotence is a property of the schema and the arithmetic, not of anybody
> remembering to check.
>
> **Current state is not history.** Nothing operational reads the table. The
> Dashboard's top and middle bands stay live aggregates over indexed columns,
> because a rollup that became the source of truth for the present would be a
> cache that can be wrong about it. This answers the bottom band only, and a
> test reads the source of `web/dashboard` and `attention` to keep it that way.
>
> **The one honest asymmetry**, which decides the repair story:
>
>     counts   recomputable for any past day whose source rows still exist
>     gauges   measurable only now — a snapshot nobody took is gone
>
> So `backfill` gives an upgrade months of commit history out of `history`, and
> cannot give it last March's library size. Inventing one is the single thing
> this table must never contain.
>
> **Days are UTC days**, chosen rather than inherited: every timestamp in the
> program is `utc_now()`, and a local date would put a metric's boundary
> somewhere else from the rows it is derived from.
>
> **Twelve measures and a category spread**, each naming the Dashboard question
> it answers — the dataclass requires it, and a test asserts it, because a
> metric with no question behind it is one nobody reads and everybody has to
> keep working. 29 rows a day at a million items; two years is ~21,000 rows and
> `KEEP_DAYS = 730` prunes past that.
>
> **What it costs** (`docs/performance.md`): the rollup is 73 ms at 100k, 219
> ms at 300k and **821 ms at 1M** — it has to read `items` twice — and a
> 90-day read is **0.18 ms at every population**, one statement. Hourly, 821 ms
> is 0.02% of a worker's time, which is why the rollup runs after the inbox
> work on *every* cycle rather than behind the idle gate: that 0.02% is worth
> less than a busy installation's entire history.
>
> **Health was measured against this and the answer was no.** 1,487 ms at a
> million, of which the FTS index-integrity counts are the largest share — and
> those are current operational state. Answering them from a rollup is exactly
> the mistake this design refuses. If they get cheaper it is by the
> recorded-verdict pattern `search_health` already uses, which is M1-06's work.

**Problem.** Of 41 tables, none records a measurement over time. `history` and
`audit_runs` can answer some questions retroactively; nothing can answer "was
the Review backlog smaller last Tuesday".

**Desired outcome.** Enough durable history to answer: how much was processed
this week, is the backlog shrinking, how fast is the library growing, how many
duplicates have been resolved, how is storage changing.

**Work.** One small append-only daily-rollup table, written by the worker on a
cycle it already runs. A stated retention.

**Do not.** Build a Prometheus replacement. Persist a metric with no product
question behind it. Write a second account of facts that already exist — where
`history` can answer, read `history`.

**Acceptance.** Every persisted metric is named on the Dashboard; the table's
growth is bounded and stated; deleting it degrades trends and breaks nothing.

**Scale.** Years of daily rows is kilobytes.

---

## M3-02 · Dashboard as command center

**P1 · L · Low risk**

**Problem.** The Dashboard answers "what needs me" and "what is happening"
well. It cannot answer "how is my library changing".

**Desired outcome.** Three bands. **Top:** what needs your attention now.
**Middle:** what LibrAIry is doing — processing, AI status, worker activity,
maintenance, throughput, active resource mode. **Bottom:** how the library is
evolving — growth, category distribution, processing history, backlog trend,
duplicates resolved, storage trend, maintenance progress. Useful graphs, real
trends, visual polish, some wow.

**Existing foundation.** The hero, the surface cards and `operations_overview`
already are the top and most of the middle, and every number there is already
an indexed aggregate that probes nothing.

**Do not.** Duplicate Health, which owns the detailed attention semantics —
summarize and link. Ship a decorative chart that changes no decision. Let the
page cost a filesystem traversal; it polls every five seconds.

**Acceptance.** Every graph answers a question you would otherwise have asked;
the page stays one bounded set of aggregates at 1M rows; Health and Dashboard
do not disagree.

**Scale.** 1M items.

---

## M3-03 · Backup, Mirror, Offline Backup

**P1 · XL · High risk**

**Problem.** One remote, the whole library, copy-only. Three different
intentions collapsed into one.

**Desired outcome.** Three named concepts that are not synonyms.

- **Backup** — recovery and retained history. A file disappearing from the
  Library is not a reason to remove it from a backup. Safety beats
  equivalence.
- **Mirror** — a location that should represent the *current* Library.
  Additions and changes propagate. Divergence is **reported**, never silently
  erased: "37 files here are no longer in your Library" and you decide.
- **Offline Backup** — a configured external drive that may be disconnected for
  months. Its actions appear only when it is configured **and** currently
  attached; when it is not, there is no UI clutter for something that cannot
  work. On reconnect: what to add, what to update, and what exists there but no
  longer in the Library — reported, never deleted.

**The Browse quick action is part of this item, not a later nicety.** Browsing
`Photos/Family/` with the configured drive attached offers **Send to Offline
Backup → WD-8TB**, using the existing transfer engine underneath. With the
drive disconnected the action is not rendered at all.

**Existing foundation.** `backup.py` is strong and should be reused wholesale:
rclone copy with four-step fingerprint verification, a queue keyed to exact
bytes, honest reporting when a remote could only compare sizes, and a schedule
that is read rather than ignored. `tools/rclone.py` owns the invocation.

**Work.** A `Category → Destination → Mode` policy model, designed so
folder-and-subtree rules can be added later without rebuilding it. Drive
presence detection. The three-way comparison and its report. The Browse action.
Backup status per destination, surfaced in Health and on the Dashboard.

**Do not.** Reinvent transfer — rclone performs it, LibrAIry owns policy,
state, comparison and orchestration. Use `sync`, `--delete`, `purge` or `move`,
ever. Delete anything at any destination for any reason. Show an action for an
absent drive. Build the advanced rule editor first.

**Acceptance.** A drive gone for three months reconnects and produces an
accurate three-way comparison with **zero** deletions; the Browse action is
absent when the drive is, and present within one poll of it being attached; a
mirror divergence is explained and actionable; a backup drill restores real
files.

**Scale.** 20+ TB, 1M files, a destination unreachable for months.

---

## M3-04 · Projects on the Dashboard

**P3 · S · Low risk**

A promoted Project earns a card: files, categories, recent activity, storage,
unresolved items, backup status. Only after M2-05 has been used enough to know
which of those you actually look at.

---

# M4 — Stabilize to the major version

Not a feature milestone. This is where "it works" becomes "it is finished".

- Full-population rehearsal on the NAS, not the test rig.
- Regression across every earlier milestone.
- Migration tested from **every released schema** to current, not just N−1.
- Accessibility and mobile pass; visible focus, contrast, 40px hit targets.
- Empty states, error states, copy, visual hierarchy, consistency.
- Documentation refresh; screenshots, which have been owed since Phase 13.
- Backup, restore, reconcile and roll-back drills, executed.
- Release acceptance extended and run.
- Real-world burn-in: a period of ordinary use with no redesign.

**Exit.** The major version is declared when this milestone's list is done and
LibrAIry has been *used* rather than worked on.

---

# Build order

**M1-01 before anything else.** The measurement decides whether M1-02 needs
materialized group rows or a windowed query. That is a load-bearing answer and
guessing it costs a rewrite.

Then M1-02, then M1-03 / M1-04 / M1-05 together — they are one experience and
testing them apart proves very little. M1 gate on the real inbox.

M2-01 and M2-03 pair naturally: both are about what the machine does when it
cannot, or should not, work hard. M2-02 follows, because its OCR wants the
limiter that M2-03 builds. M2-04 and M2-05 close the milestone.

M3-01 before M3-02, necessarily. M3-03 is the largest single item on this
roadmap and is safest last, while the safety habits are freshest.

M4 last, and not rushed.

---

# Test strategy

The target is software you trust. Tests pin invariants; none is written to
raise a count.

| | |
|---|---|
| **Unit / integration** | as today — 3,592 tests, ruff, CI on 3.11 and 3.12 |
| **Migration** | from every *released* schema to current, not only N−1. Schema 47 arrived from 10 in one release and that path must stay provable |
| **Filesystem safety** | the top gate: containment, collision, hash revalidation, journal completeness, undo sequencing. Never relaxed for a feature |
| **Scale** | `tests/test_scale.py` extended to hold bounded pages, SQL counts and deterministic paging on **every** paged surface; synthetic 1M fixtures built once and reused |
| **Real files** | fixtures for OCR, document identity, format decisions, companion pairing |
| **UI** | geometry and layout checks through `scripts/ui_check.py`. The in-app browser reports a 0×0 viewport and will lie to you about anything positional |
| **Backup drills** | backup, restore, reconcile and roll back executed as acceptance, not described as prose |
| **Long-run** | worker burn-in between milestones, on the NAS |
| **Release acceptance** | `scripts/release_acceptance.py`, extended per milestone |

---

# Polish strategy

Polish is not a phase that waits at the end. Every capability is built, tested,
visually inspected and refined before the next one starts — that is the
`build → test → use → refine` loop, and skipping the middle two is how a
roadmap accumulates debt it later calls a phase.

M4 then revisits what only shows up across the whole program: consistency,
visual hierarchy, responsiveness, copy, empty states, errors, accessibility and
performance. The target is not "technically works". It is "feels finished".

---

# What this roadmap replaced

The seventeen numbered phases in [history/plan/](history/plan/README.md) took
LibrAIry from a bash prototype to a published v1.3.1. They are archived intact,
not deleted: the decisions in them are why the program is shaped the way it is,
and several of them — the safety invariants, the authority order, the permanent
principles added in August 2026 — were promoted into
[PRODUCT.md](PRODUCT.md) and [architecture/](architecture/) rather than
retired.

Phase 11 (a terminal UI) is **DEFERRED**, not cancelled. No milestone here
depends on it, and it should inherit a finished design language rather than
chase one.
