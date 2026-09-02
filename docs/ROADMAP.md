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

Two findings worth keeping. **Documents and books never form a group** — a
group is an album, a season, a disc, a camera event or a project, and a book is
none of them — so a document grid would have been a template no page could
reach. The medium got its presentation on the row instead: the first page
beside the title, author and type it already showed. And **the harness could
not see any of this**: `scripts/ui_check.py` renders over `file://`, where
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

**Do not.** Let a learned pattern reach the deterministic tier — it is
authority level 4, permanently. Build this as a second automation system beside
Decision Memory; they are one model with one explanation (see M2-04).

**Acceptance.** Each tier's rule is written down and tested; every
deterministic item can answer "why am I here"; disabling the tier restores
today's behaviour exactly.

**Scale.** Tens of thousands of pending decisions.

---

## M1-06 · The pages that do not survive their own tables

**P1 · M · Low risk** — added 2026-09-01 from M1-01's measurements.

**Problem.** Three surfaces have a specific, small, non-architectural fault that
makes them degrade or fail with the size of a table they read. All three are
named with file and line in [performance.md](performance.md).

- ~~**Health is quadratic and unusable at 100,000 files.**~~ **DONE
  2026-09-02 — 22.3 s to 2.1 s at a million**, and it completes at every
  population. Three causes, all measured: the quadratic `unindexed`, an
  `OR`-joined dependency query at 17 s, and `PRAGMA quick_check` verifying the
  whole database on every render. What is left is ~300 ms of FTS counting asked
  three times per page; asking once is the next step and needs a shared count,
  not a faster query. The original text follows because the shape is worth
  remembering.
  `search_health.py:156` counts unindexed items with a `NOT EXISTS` against
  `search_fts`, whose `item_id` is declared `UNINDEXED` — so it scans the whole
  FTS table once per row of `items`. The query planner says so without any
  timing. `health_data` then asks for it **twice** per render. Health is the
  page that exists to say something is wrong, and it is the most broken page in
  the program.
- **Search counts history per result row.** `search.py:343`, fifty full scans of
  `history` per page, which has no index on `(dest_root, dest_relpath)`. 3.1 s
  at a million.
- **Browse home reads the whole library in one statement.** Bounded in
  statements, unbounded in rows. 1.2 s at a million; headroom, but not another
  order of magnitude.

**Desired outcome.** Every surface's cost is bounded by what it displays, not by
what the database holds.

**Work.** A different shape for the unindexed count, and asked once. A batched
history count, or an index, or neither if the number turns out not to be worth
a query. An index or a bound for Browse home.

**Do not.** Fix these by removing what they tell people — the unindexed count is
one of the few genuinely important numbers on Health. Add an index without
measuring that it is used. Treat this as architecture: it is three queries.

**Acceptance.** Health completes in well under a second at 1M — **not yet met:
2.1 s**, with the remaining cause named and cheap to fix. The `xfail(strict=True)`
test on the correlated scan is gone, replaced by a real assertion on what the
function actually executes. Search and Browse are **untouched** and still
degrade with the library: 2.6 s and 1.2 s at a million.

**Scale.** 1M items, 100k history rows, 50k findings.

---

**M1 gate.** Run it against the real inbox on the NAS. Refine what real use
finds. Then decide whether to publish.

---

# M2 — Intelligence and processing

## M2-01 · Waiting for AI

**P1 · M · Medium risk**

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

**P1 · L · Medium risk**

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

**P1 · M · Low risk**

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

**P2 · L · Medium risk**

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

**P2 · M · Low risk**

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

# M3 — Visibility and distribution

## M3-01 · A small durable metrics model

**P1 · M · Low risk**

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
