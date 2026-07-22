# Phase 11 — Pip-Boy Terminal UI (`librairy tui`) → v1.1.0

**Status:** NOT STARTED
**Depends on:** Phase 10 (v1.0.0 published)
**Size:** M (eight small sequential tasks; heavy reuse, no new data layer)

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY lives in docs/plan/README.md. Do not edit here; if decisions change, the canonical copy is updated and propagated. -->

**LibrAIry** is a self-hosted, privacy-first, AI-assisted file organizer and library manager. It ships as a single Docker container for NAS systems (UNRAID is the primary target) and desktop workstations. The user drops messy files into an **inbox** folder; LibrAIry analyzes them continuously in the background and proposes clean names and destinations inside an organized **library**; the user reviews proposals in batches from a lightweight LAN web portal (approve / edit / reject / postpone); only then does LibrAIry move files. It is an *orchestrator*, leaning on proven external tools (ffprobe, exiftool, fpcalc/Chromaprint, rmlint, czkawka) and free catalog APIs (MusicBrainz, AcoustID, TMDB), using AI only when deterministic evidence is insufficient — local AI (Ollama) by default, cloud AI strictly opt-in.

**Safety invariants (non-negotiable; enforced in code and by tests):**

1. LibrAIry NEVER deletes user files. Deletion is always a manual human act outside the system — including for duplicates.
2. LibrAIry NEVER overwrites existing files. Destination collisions produce deterministic alternative names.
3. The existing library is READ-ONLY: indexed and searched, never renamed, moved, or modified.
4. Analysis never mutates the filesystem. Only the commit engine moves files, and it executes exactly an approved, immutable, hash-verified plan — never a recomputation.
5. Every destination path is containment-validated: it must resolve inside the library (or quarantine) root; traversal (`../`), absolute paths, and symlink escapes fail closed.
6. Quarantine is reversible storage with recorded history and a restore path — not deletion.
7. v1 renames and moves files only. It never rewrites file contents or embedded metadata.
8. Every filesystem operation is journaled with enough information to undo it.
9. Privacy is local-first. Cloud AI prompts are structurally redacted (no absolute paths, no GPS/location data) and each cloud provider is individually opt-in.

**Locked product decisions (do not reopen in any phase):**

- **Deployment**: one Docker container running two processes (web + worker) under a small Python supervisor. LAN web portal with single-admin login (scrypt password hashing, server-side sessions in SQLite, CSRF protection, login rate limiting). No public internet exposure assumed or advertised.
- **Workflow**: inbox drop → continuous background analysis that never stops to ask questions → proposals accumulate → batch review in web UI → commit executes exactly the approved plan → committed files are indexed and searchable. Uncertain files stay physically in the inbox with a pending state.
- **Taxonomy**: library top level is `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/ Misc/` (replaces the legacy RAM/ROM zones). Destination templates are user-selectable per category: conventional (`Music/<Artist>/<Album>/`) or genre-first (`Music/<Genre>/<Artist>/<Album>/`).
- **Items & grouping**: every file is an independently tracked item. Relationship groups (album, TV season, photo event, project) influence shared destinations and naming, but a file belongs to exactly one plan operation — never two.
- **Hashtag hints**: a `#tag` suffix on an inbox folder name (e.g. `Vacation 2026 #italy`, `Assets #projectone`) is a routing hint: used during classification, recorded as evidence, then stripped from clean destination names.
- **Database**: SQLite (WAL mode, FTS5) embedded in the container — decided; no PostgreSQL, no benchmarking phase. Files remain the source of truth; the index is always rebuildable from the filesystem plus the history journal.
- **Tech stack**: Python 3.11+ under `src/librairy/`; FastAPI + uvicorn + Jinja2 + HTMX; vanilla CSS/JS (no Node build step); raw stdlib `sqlite3` (no ORM); Pydantic for settings and models; pytest + httpx TestClient; ruff; GitHub Actions CI.
- **AI**: Ollama is the default provider; the user typically runs it on a separate LAN machine. Multiple named Ollama endpoints are supported. Default model recommendations: `qwen3:4b` (CPU-only hosts) / `qwen3:8b` (GPU hosts). Cloud providers (OpenAI, Anthropic, Gemini) are individually opt-in. The web UI has a quick provider selector with test-connection, effective without restart, degrading gracefully to heuristics-only when nothing is reachable.
- **Duplicates**: exact duplicates detected by core BLAKE2b content fingerprints AND an rmlint cross-check (both enabled by default; each tool can be toggled off in settings); near-identical media detected by czkawka. Duplicates are proposed for reversible quarantine — never deleted.
- **Search**: v1 searches names, metadata, and tags via SQLite FTS5. Text-inside-documents search (pdftotext → FTS5) is a post-1.0 fast-follow. Never Elasticsearch. Media file content is never indexed.
- **UI style**: Fallout Pip-Boy retro-terminal aesthetic — phosphor green/amber on dark, monospace type, subtle scanlines. A lightweight dashboard and review tool, NOT a file manager.
- **UX north star**: pull container → set ENV variables (paths, keys, Ollama host) → run → open web portal. Non-nagging, minimal setup, keeps working in the background.
- **No over-engineering**: no microservices, no message queues or brokers, no Elasticsearch, no multi-user roles, no plugin system, no Kubernetes.

**Container data layout** (bind-mounted from host paths via env vars): `/data/inbox` (user drops files here), `/data/library` (organized output), `/data/quarantine` (reversible duplicate/review storage), `/data/appdata` (SQLite database, settings, thumbnails, logs). The legacy `/data/reports` JSON-report mount exists only while the legacy bash pipeline survives (through Phase 3) and is retired in Phase 4.

<!-- end context-boilerplate -->

---

## Decision amendment recorded by this phase

The locked tech stack gains ONE optional dependency: **Textual** (`textual`, textual.textualize.io — the mature, widely used Python TUI framework), shipped as the `librairy[tui]` extra. Rationale: the owner wants the portal's core operations available over SSH when no browser is reachable (`docker exec -it librairy librairy tui`). This does not reopen "NOT a file manager" — the TUI is the same dashboard-and-review tool in a terminal skin, with the same single code path for every mutation. No other stack change; no plugin system; no remote/HTTP mode.

## Phase goal

`librairy tui`: a keyboard-driven Pip-Boy terminal UI mirroring the web portal's daily loop — dashboard, health, review (approve/reject/postpone/edit), commit with live progress, search, then history+undo, quarantine restore, and browse — reusing the web layer's existing data functions verbatim, so web and TUI can never disagree.

## In scope

- New package `src/librairy/tui/` + `tui` subcommand in `cli.py`; optional `[tui]` extra; textual included in the Docker image and the `dev` extra.
- Screens in three slices: (A) dashboard + health, (B) review + commit + search, (C) history/undo + quarantine + browse.
- Pip-Boy Textual stylesheet mirroring `web/static/pipboy.css` colors.
- Pilot-based tests (textual's built-in test harness) for every mutating flow.
- `docs/using-librairy.md` TUI section + README bullet; v1.1.0 release.

## Out of scope (tempting, but NO)

- **No settings mutation** (no provider toggles, no cloud opt-in, no backup/dedup config — the web's "type CLOUD" opt-in ceremony must not get a second implementation). Read-only settings display is skipped too.
- **No auth** — same trust model as the existing CLI: whoever can run `librairy` against the appdata already owns the DB file. Stated in docs.
- **No arbitrary-path file browsing**, no previews/thumbnails, no delete controls (none exist anywhere), no media playback.
- **No HTTP/remote mode** — the TUI opens SQLite locally, full stop. Remote use = SSH to the host, `docker exec`.
- **No new data-layer functions** unless a web function is genuinely unusable as-is; any needed helper goes INTO the existing web module so both surfaces share one code path.

## Design constraints binding this phase

- **Reuse inventory** (verified signatures; all take only `conn`/`settings`/plain values, no FastAPI types):
  | Screen | Functions |
  |---|---|
  | Dashboard | `web.dashboard.dashboard_data(conn, settings) -> dict` |
  | Health | `web.health.health_data(conn, settings)`; optional `test_provider(conn, settings, name)` |
  | Review | `web.review.ReviewFilters`, `review_data(conn, filters)`, `apply_review_action(conn, action, filters, *, proposal_ids=None, all_matching=False)`, `edit_proposal(conn, settings, proposal_id, *, category, clean_name, dest_relpath)` |
  | Commit | `web.commit.CommitState`, `create_commit_plan(conn, settings)`, `commit_confirm_data(conn, plan_id)`, `start_execution(conn, settings, state, plan_id)`, `progress_data(conn, plan_id)` |
  | Search | `librairy.search.SearchFilters`, `search_data(conn, settings, query, filters)` |
  | History | `web.history.history_data(conn, limit=50)`, `plan_detail_data`, `undo_history_entry`, `undo_history_plan` |
  | Quarantine | `web.quarantine.quarantine_data(conn)`, `restore_quarantine`, `unstage_proposal`, `approve_stage` |
  | Browse | `web.browse.browse_home(conn)`, `browse_category(conn, category, folder="", page=1)` (no item previews) |
- **Commit flow**: identical to web — `create_commit_plan` → confirm screen → `start_execution` (background thread, own DB connection, single-flight `CommitState`) → poll `progress_data` with `set_interval(1.0)`. NOT the CLI's synchronous `execute_plan`.
- **Concurrency**: `db.connect` already uses WAL, `check_same_thread=False`, `busy_timeout=5000`; the TUI opens its own connection beside web+worker exactly as the CLI does. Executor-lock contention (`locks.LockHeldError`) surfaces as a "LibrAIry is busy — retry shortly" toast, mirroring the web's message. `undo`/`restore` run inline (seconds-scale hashing acceptable); move to `App.run_worker(thread=True)` only if UX demands.
- **Layout**: `src/librairy/tui/{__init__,app,_app_impl}.py`, `pipboy.tcss`, `screens/{dashboard,health,review,commit,search,history,quarantine,browse}.py`. `app.run_tui(settings) -> int` catches `ModuleNotFoundError` for `textual` and prints `Install with: pip install 'librairy[tui]'` → exit 2 (textual-importing code lives only in `_app_impl.py` so the guard is testable).
- **CLI wiring**: `subparsers.add_parser("tui", ...)` after `run`; in `main()`, branch to `run_tui(settings)` before `configure_logging`/`connect` (the TUI owns the terminal, and creates its own connection).
- **Dependencies**: `[project.optional-dependencies] tui = ["textual>=8,<9"]`; `dev` gains `textual>=8,<9` + `pytest-asyncio>=0.24,<2`; pytest config gains `asyncio_mode = "auto"`. Docker builder installs `.[tui]` and runtime asserts `python -c "import textual"` (textual+rich are pure-Python, ~15 MB — noise next to ffmpeg).
- **Theme** (`pipboy.tcss`, mirror `web/static/pipboy.css` palette): screen `#061109`/`#7cff6b`, header/footer `#020703`/`#ffbf4d`, borders `#56d364`, cursor row `#2f8f43`, focus accents `#ffbf4d`. Status vocabulary `[OK]`/`[WARN]`/`[FAIL]` identical to web. No scanline gimmicks.
- **Bindings**: screens on single keys shown in `Footer` (d dashboard, r review, c commit, s search, h health; slice C adds y history, x quarantine, b browse); within review: a approve, j reject, p postpone, e edit, space select, enter detail; ctrl+q quit. Adjust only on collision.
- **Tests**: every TUI test module starts with `pytest.importorskip("textual")`; Pilot (`async with app.run_test()`) drives keys; DB/filesystem assertions reuse the seeding patterns from `tests/test_web_commit.py` / `tests/test_web_review.py`.

## Backlog items

### P11-01 Skeleton: extra, subcommand, optional-dep guard
**Depends on:** Phase 10
**Description:** pyproject extras + asyncio config; `cli.py` `tui` subcommand + branch; `tui/__init__.py`, `tui/app.py` with the guarded `run_tui`; `tests/test_tui_optional_dep.py` (monkeypatched missing textual → exit 2 + install hint; parser accepts `tui`).
**Acceptance criteria:**
- [ ] Without textual: `librairy tui` prints the `pip install 'librairy[tui]'` hint, exit 2. With textual: proceeds (empty app OK).
- [ ] `ruff check src tests scripts && pytest` green.
**Size:** S

### P11-02 App shell + Dashboard + Health (slice A)
**Depends on:** P11-01
**Description:** `_app_impl.py` (App, BINDINGS, screen registry), `pipboy.tcss`, `screens/dashboard.py` + `screens/health.py` fed by `dashboard_data`/`health_data`; auto-refresh via `set_interval` (dashboard ~5s). Pilot test: boots to dashboard, seeded counts render, d/h switch screens.
**Acceptance criteria:**
- [ ] `librairy tui` against a real appdata shows live dashboard + health in Pip-Boy colors.
- [ ] Pilot tests green.
**Size:** M

### P11-03 Review screen + edit modal
**Depends on:** P11-02
**Description:** `screens/review.py`: DataTable of proposals (`review_data`, paginated via `ReviewFilters.page`), multi-select, a/j/p actions through `apply_review_action`, e opens `EditDestinationModal` → `edit_proposal` (shows its collision warning when returned).
**Acceptance criteria:**
- [ ] Pilot: approve/reject/postpone/edit round-trip to the DB; pagination works.
**Size:** M

### P11-04 Commit screen with live progress
**Depends on:** P11-03
**Description:** `screens/commit.py`: create plan → confirm table (`commit_confirm_data`) → execute (`start_execution`) → progress poll → done/error states; `LockHeldError` → busy toast (test by pre-acquiring the flock).
**Acceptance criteria:**
- [ ] Pilot end-to-end: seeded approved proposal + real tmp file → committed → file physically moved → proposals marked committed.
- [ ] Busy-lock path shows the toast, no crash.
**Size:** M

### P11-05 Search screen
**Depends on:** P11-02
**Description:** `screens/search.py`: Input + results DataTable via `search_data`; content-facet toggle honoring `content_search_enabled`; `[CONTENT]` marker parity with web.
**Acceptance criteria:**
- [ ] Pilot: query returns seeded hits; content toggle changes result set.
**Size:** S

### P11-06 History + undo, Quarantine (slice C)
**Depends on:** P11-04
**Description:** `screens/history.py` (list, plan detail, undo entry/plan behind a confirm modal) and `screens/quarantine.py` (list, restore/unstage/approve).
**Acceptance criteria:**
- [ ] Pilot: undo restores the file on a tmp filesystem; quarantine actions mutate DB correctly.
**Size:** M

### P11-07 Browse + Docker + docs
**Depends on:** P11-06
**Description:** `screens/browse.py` (category → folder drill via `browse_home`/`browse_category`, read-only, no previews). Dockerfile: builder wheels `.[tui]`, runtime asserts `import textual`. Docs: `docs/using-librairy.md` gains "Terminal UI over SSH" (`docker exec -it librairy librairy tui`, note `-e COLORTERM=truecolor` if colors wash out, trust model note); README bullet.
**Acceptance criteria:**
- [ ] In-container `docker exec -it librairy librairy tui` verified manually in the next Docker session.
- [ ] Docs updated; suite green.
**Size:** S

### P11-08 Release v1.1.0
**Depends on:** P11-07
**Description:** CHANGELOG `## v1.1.0` ("Adds the Pip-Boy terminal UI (`librairy tui`) for SSH sessions…"), version bump to 1.1.0 in the same three files as P10-03 (+ reinstall gotcha), owner pushes `v1.1.0`, agent verifies the published image and that `docker exec … librairy tui` works from the pulled image.
**Acceptance criteria:**
- [ ] v1.1.0 published; TUI works from the published image; phase status DONE.
**Size:** XS

## Verification steps

1. Each task: `ruff check src tests scripts && pytest` green before commit (tasks are strictly sequential).
2. After P11-04: manual smoke on the dev Mac — `librairy tui` against a scratch appdata, full review→commit→undo loop from the keyboard only.
3. After P11-07: in-container manual smoke over `docker exec -it`.
4. After P11-08: pulled-image smoke.

## Exit gate checklist

- [ ] Web and TUI share every data/mutation function (no TUI-only business logic — grep review).
- [ ] All mutating flows Pilot-tested; optional-dep guard tested; suite green without textual installed (skips).
- [ ] Docs cover install, usage over SSH, and the trust model.
- [ ] v1.1.0 tagged and published; all boxes ticked; status DONE.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
