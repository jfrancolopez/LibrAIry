# Phase 14 — Screen Redesigns: Search-first Dashboard, Evidence-rich Review, Browse, History, Health (v1.1.x)

**Status:** NOT STARTED
**Depends on:** Phase 13 (theme system — build on tokens, not on pipboy literals)
**Size:** L (five independent screen tasks; each shippable alone)

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY lives in docs/plan/README.md. Do not edit here; if decisions change, the canonical copy is updated and propagated. -->

**LibrAIry** is a self-hosted, privacy-first, AI-assisted file organizer and library manager. It ships as a single Docker container for NAS systems (UNRAID is the primary target) and desktop workstations. The user drops messy files into an **inbox** folder; LibrAIry analyzes them continuously in the background and proposes clean names and destinations inside an organized **library**; the user reviews proposals in batches from a lightweight LAN web portal (approve / edit / reject / postpone); only then does LibrAIry move files. It is an *orchestrator*, leaning on proven external tools (ffprobe, exiftool, fpcalc/Chromaprint, rmlint, czkawka) and free catalog APIs (MusicBrainz, AcoustID, TMDB), using AI only when deterministic evidence is insufficient — local AI (Ollama) by default, cloud AI strictly opt-in.

**Safety invariants (non-negotiable; enforced in code and by tests):** LibrAIry NEVER deletes user files; NEVER overwrites (deterministic collision renames); the existing library is READ-ONLY; analysis never mutates the filesystem — only the commit engine moves files, executing exactly an approved, immutable, hash-verified plan; every destination is containment-validated (traversal/absolute/symlink escapes fail closed); quarantine is reversible; v1 renames/moves only; every operation is journaled and undoable; privacy is local-first with structural redaction and per-provider cloud opt-in.

**Locked product decisions (do not reopen):** one container, web + worker under a Python supervisor; single-admin LAN portal (scrypt, SQLite sessions, CSRF, rate limiting); taxonomy `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/ Misc/`; SQLite WAL + FTS5 (no Postgres, no Elasticsearch); Python 3.11+, FastAPI + uvicorn + Jinja2 + HTMX, vanilla CSS/JS (no Node build), raw stdlib sqlite3 (no ORM), Pydantic, pytest, ruff, GitHub Actions; Ollama default with per-provider cloud opt-in; duplicates → reversible quarantine only; portal is "a lightweight dashboard and review tool, NOT a file manager"; no microservices/queues/plugin system/Kubernetes.

<!-- end context-boilerplate -->

---

## Phase goal

Owner acceptance feedback (2026-07-23): the screens work but feel clunky and demand too much digging. Five redesigns, all **presentation-layer**: reuse the existing data functions (`web/dashboard.py`, `web/review.py`, `web/commit.py`, `web/browse.py`, `web/history.py`, `web/quarantine.py`, `web/health.py`, `search.py`) — new UI, same engine. No schema changes except where a task explicitly says otherwise.

## Out of scope (tempting, but NO)

- Opening/playing files from the browser; editing files; any write operation from Browse (locked: NOT a file manager, NOT a media player).
- JS frameworks, charting libraries (bars/meters are styled divs), metrics daemons or time-series tables.
- New AI features. New search backends (FTS5 stands).

## Backlog items

### P14-01 Search-first dashboard
**Story:** As the admin, opening LibrAIry feels like opening a search engine for my library: a big centered search box, then my status at a glance below.
**Depends on:** — | **Size:** S
**Description:** Add a prominent centered search form at the top of `dashboard.html` (plain `<form action="/search" method="get">` with one large input, autofocus). `/search` (route app.py, template `search.html`) accepts the `q` query param, prefills the filter form, and **auto-runs** the results (the htmx machinery `hx-get /search/results` already exists in `search.html:5` — trigger it on load when `q` is present). Stats tiles (`partials/dashboard_stats.html`) move below the search box. Nav link stays.
**Acceptance criteria:**
- [ ] Dashboard search → enter lands on `/search?q=…` with results already rendered.
- [ ] Web test: GET `/search?q=x` response contains the results container with hits for seeded data.

### P14-02 Evidence-rich review / commit / quarantine cards
**Story:** As the admin, each item under review shows me at a glance WHAT it is (preview), WHERE it goes (from → to), and WHY (the system's evidence) — approving takes seconds, digging is optional.
**Depends on:** — | **Size:** L
**Description:** One shared card partial used by review, commit-confirm, and quarantine so the three screens speak the same language. Card contents, all from existing data: thumbnail/preview (`web/thumbs.py` pipeline; graceful "no preview" state per P12-03), original path → proposed destination rendered as a visual from→to, category + confidence, and a collapsed **"WHY?" expander** that renders the proposal's `EvidenceEntry` list (`proposals.evidence`, decode via `proposals.decode_evidence`) in plain language — e.g. "Folder heuristic: looks like a camera roll", "TMDB match: 'Movie (1995)' 97%", "AI (ollama/qwen3:4b): category=documents, confidence 0.86". Map evidence `source`/`kind` fields to human sentences in one small template filter or helper (put it beside the web code, reuse from all three screens). Batch actions (approve/reject/postpone selected/all) stay; make the primary action per card one obvious button.
**Acceptance criteria:**
- [ ] Review, commit-confirm, and quarantine all render the shared card with preview, from→to, confidence, and working WHY expander.
- [ ] Every evidence kind present in the drill DB renders as a human-readable sentence (no raw JSON on screen).
- [ ] Existing review/commit/quarantine web tests updated, still green; approve→commit→undo drill unchanged functionally.

### P14-03 Browse: breadcrumbs, keyboard navigation, detail panel
**Story:** As the admin, I can walk my library like a tidy file listing — breadcrumbs up top, arrow/j-k keys to move, Enter to open a folder, and a detail panel showing what a file is, with a small preview for images/PDFs.
**Depends on:** — | **Size:** M
**Description:** yazi-INSPIRED, read-only (locked: not a file manager). Enhance `browse_category.html` + `web/browse.py` (`browse_home`, `browse_category(conn, category, folder, page)` already do folder walking): (1) breadcrumb trail of the current path, each segment a link; (2) keyboard navigation — a few lines of vanilla JS: j/k or arrows move a highlighted row, Enter follows the row's existing link, Backspace goes up one level; (3) selecting a file row loads a right-side detail panel via htmx (`hx-get` the existing item-detail data as a partial) showing name, size, category, dates, fingerprint, evidence summary, and an image thumbnail or PDF-first-page preview where the preview pipeline supports it (reuse `preview_for_item`/`thumbs.py`; no new preview types). No file opening, no operations.
**Acceptance criteria:**
- [ ] Breadcrumbs, keyboard navigation, and detail panel work in the drill container across a nested folder tree.
- [ ] Detail partial reuses item-detail data (no duplicated query logic); web test for the partial route.
- [ ] Zero write operations exist on the screen (review check recorded).

### P14-04 History as a readable timeline
**Story:** As the admin, history reads like a `git log` of my library: commits grouped by plan, one line per file, undo right there, and clicking a file jumps me to where it lives in Browse.
**Depends on:** P14-03 (deep-links target the improved Browse) | **Size:** S
**Description:** Rework `history.html` using existing `web/history.py` data (`history_data`, `plan_detail_data`): group entries by plan — header line "PLAN #12 · 14 files → Documents/… · 2026-07-23 · [UNDO PLAN]" — expandable to per-file rows "moved  inbox/foo.txt → Documents/Notes/foo.txt  [UNDO]". Each destination deep-links to Browse at the containing folder (`/browse/<category>?folder=…` — served by `browse_category`). Undo entry/plan flows unchanged (`undo_history_entry`, `undo_history_plan`), just surfaced inline with confirmation.
**Acceptance criteria:**
- [ ] Timeline grouped by plan with expandable file rows; deep-links land on the right Browse folder (test).
- [ ] Undo from the timeline works in the drill (file physically returns; entry marked undone).

### P14-05 Health with system insight and recommendations
**Story:** As the admin, Health tells me how the system is doing, where it's struggling, and what to do about it — not just a wall of [OK].
**Depends on:** — | **Size:** M
**Description:** Keep the six existing sections (`web/health.py:39-56`: tools, providers, db, disks, worker, backup). Add, from data already on hand or one cheap query away: worker cycle stats (last cycle duration + items processed — extend the `worker_state` `last_summary` the worker already writes; add a duration field there if absent), DB file + WAL sizes (already computed), per-root disk usage as styled meter bars (styled divs, no chart lib), tool versions (probe `--version` where cheap, cache per boot), and a **RECOMMENDATIONS block**: plain if/else rules over existing signals, each with a one-line action, e.g. "Ollama unreachable — running heuristics-only; check OLLAMA_HOST", "library disk below 10% free", "N items stuck pending review for >7 days", "backup enabled but remote unreachable", "content search enabled but pdftotext missing". Rules live in one function with a unit test each; no metrics store, no history.
**Acceptance criteria:**
- [ ] New rows render with meter bars; recommendations appear/disappear according to seeded conditions (unit tests per rule).
- [ ] Health page still loads fast (<100ms in the perf smoke's terms) — no slow probes on request path (cache tool versions).

## Verification steps

1. Each task: one commit, `ruff check src tests scripts && pytest` green, manual drill of that screen in the running container.
2. After P14-02: full inbox→review→commit→undo drill using only the new cards.
3. Ship as v1.1.x releases (or fold into v1.1.0 with Phase 13 — owner's call at release time).

## Exit gate checklist

- [ ] All five screens redesigned, tests green, drills passed, no new dependencies, read-only invariants intact.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
