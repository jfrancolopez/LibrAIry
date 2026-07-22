# Phase 4 — Dedup, Background Worker, Bash Retirement

**Status:** IN PROGRESS
**Depends on:** Phase 3 (AI providers) DONE
**Size:** M/L

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

## Glossary (terms used in this phase)

- **Exact duplicate**: two files with identical content (identical BLAKE2b fingerprints; optionally cross-checked by rmlint).
- **Similar media**: near-identical images/videos/audio found by czkawka's perceptual methods — NOT byte-identical; always requires human review; never auto-quarantined.
- **Quarantine proposal**: a proposal whose action is `quarantine` instead of a library move — it enters the same review→approve→commit pipeline as everything else. Quarantined files land under `/data/quarantine/<YYYY-MM-DD>/…` preserving their inbox-relative path.
- **Keeper**: the copy of a duplicate set that stays. Rule: an existing library copy always wins over inbox copies; among inbox-only copies, the first-seen wins (deterministic tiebreak: earliest `first_seen_at`, then lexicographic relpath).
- **Item lifecycle**: `discovered → analyzed → proposed → approved → committed`, with branches `quarantine-proposed → quarantined`, `postponed`, `pending` (analyzed, no destination). Stored in `items.state`.
- **Worker**: the long-running background process that scans, dedups, analyzes, and stages proposals — never asks questions, never commits.

## Entry criteria

```bash
# 1. Phase 3 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Full headless classification works
librairy analyze --help && librairy ai status --help

# 3. Executor + undo present with quarantine op support
librairy plan --help | grep -i quarantine || echo "quarantine op_type exists in schema"
```

If any check fails, STOP and report.

## Phase goal

LibrAIry becomes a continuously running organizer: a worker daemon scans, dedups, and analyzes in the background, accumulating proposals for review — never interrupting, never committing on its own. Exact duplicates are proposed for reversible quarantine (core fingerprints + rmlint cross-check, both toggleable), similar media flagged by czkawka for review. With headless parity proven end-to-end, the legacy bash pipeline is deleted.

## In scope

- Exact-duplicate detection (fingerprints + rmlint cross-check, settings toggles); czkawka wrapper; quarantine proposals + restore; worker daemon; item lifecycle state machine; headless E2E test; deletion of `inbox-processor/scripts/`, watcher compose service, and the `/data/reports` mount; Dockerfile gains czkawka.

## Out of scope (tempting, but NO)

- Any web UI (Phases 5–7). Auto-commit/auto-approve policies (post-1.0 at earliest; fresh installs stay batch-review).
- Deleting duplicate files — NEVER. Quarantine only, user deletes manually.
- Audio-similarity dedup via AcoustID and quality-comparison ranking (post-1.0 ideas; czkawka similar-media flagging is the v1 ceiling).
- inotify/fanotify watching — polling with stability detection is sufficient and portable across NAS mounts (document this choice).
- Removing `inbox-processor/catalog/` ports' originals is IN scope (whole `inbox-processor/` tree goes); re-architecting anything from Phases 1–3 is OUT.

## Design constraints binding this phase

- **Migration 004**:

```sql
CREATE TABLE quarantine_entries (
  id               INTEGER PRIMARY KEY,
  item_id          INTEGER NOT NULL REFERENCES items(id),
  reason           TEXT NOT NULL,          -- 'exact_duplicate' | 'similar_media' | 'user'
  duplicate_of     INTEGER REFERENCES items(id),   -- the keeper
  original_root    TEXT NOT NULL,
  original_relpath TEXT NOT NULL,
  quarantined_at   TEXT,
  restored_at      TEXT,
  plan_id          TEXT
);
CREATE TABLE worker_state (
  key   TEXT PRIMARY KEY,                  -- 'last_cycle_at', 'current_phase', 'items_pending', ...
  value TEXT NOT NULL
);
```

- **Dedup engine** (`dedup.py`): primary source = `items.fingerprint` equality (inbox↔inbox and inbox↔library; requires the indexer to fingerprint library files on demand for size-matched candidates — hash only size-colliding library files, not the whole library). Cross-check = `rmlint` run over the duplicate candidates (`--types=duplicates`, JSON output) — a candidate is confirmed when both sources agree; disagreement → flag for review, do not quarantine. Settings: `dedup.use_fingerprints` (default true), `dedup.use_rmlint` (default true), `dedup.use_czkawka` (default true) — at least one exact method must remain enabled (validation). czkawka (`tools/czkawka.py`): `czkawka_cli dup`/`image`/`video` with `CZKAWKA_EXTENSIONS` honored (closing legacy defect: the env var is finally read); results become `similar_media` review flags, never auto-quarantine.
- **Quarantine is a plan op**: dedup produces quarantine *proposals*; they compile into plan ops (`op_type='quarantine'`, dest_root `quarantine`, dest_relpath `<date>/<inbox-relpath>`) via the standard propose→approve→commit path. The executor already handles the move; this phase adds `quarantine_entries` bookkeeping + `librairy quarantine list/restore` (restore = journaled move back to `original_relpath`, collision-safe, clears `restored_at`).
- **Worker** (`worker.py`): single process, cycle loop: (1) scan inbox (Phase-1 scanner; unstable files skipped), (2) fingerprint new/changed, (3) dedup, (4) analyze un-analyzed stable items in bounded batches (`BATCH_SIZE` honored — closing another ignored-env defect), (5) update `worker_state`, (6) sleep with backoff (fast when work was found, slower when idle; bounds in settings, e.g. 5s–60s). Never commits. Never prompts. Ignores items in states `proposed/approved/quarantine-proposed/postponed/pending` unless their fingerprint changed (this structurally kills the legacy infinite-reprocessing loop). Graceful shutdown on SIGTERM/SIGINT (finish current item, persist state). Crash-safe: all progress is in the DB; restart resumes.
- **State machine** (`lifecycle.py`): explicit transition table; illegal transitions raise; every transition timestamped in the DB. States listed in Glossary; `items.state` is the single source of truth the worker and (later) the web UI read.
- **Bash retirement** (last item, after the E2E gate): `git rm -r inbox-processor/` (scripts AND catalog — ports are complete), remove the watcher and legacy service wiring from `docker-compose.yml` (interim compose: one service running `python -m librairy worker`; Phase 5 introduces the supervisor), drop `/data/reports` mounts and `REPORTS_DIR` config, update README/Instructions to describe the Python CLI flow, delete the P1-11 deprecation banner (nothing left to deprecate). Git history preserves the old code.
- **Dockerfile**: install the package (`pip install .`), add czkawka_cli (multi-stage: download the official release binary for the target arch, or build once — document choice; it must be IN the image, closing legacy defect #17), keep ffmpeg/exiftool/chromaprint/rmlint, drop tools nothing uses anymore.

## Backlog items

### P4-01 Exact-duplicate detection (fingerprints + rmlint cross-check)
**Story:** As a user, byte-identical copies are caught before they clutter my library — and two independent methods agree before anything is staged.
**Depends on:** Phase 1 scanner/fingerprints
**Description:** Per Design Constraints: fingerprint grouping, targeted library hashing for size-matches, rmlint cross-check subprocess (`tools/rmlint.py`), keeper selection rule, settings toggles with validation.
**Acceptance criteria:**
- [x] Inbox pair + inbox↔library pair detected; keeper chosen per rule (library wins; deterministic tiebreak).
- [x] Fingerprint/rmlint disagreement (crafted fixture) → review flag, no quarantine proposal.
- [x] Toggling `dedup.use_rmlint=false` skips the subprocess (call test); disabling both exact methods is rejected.
- [x] Only size-colliding library files get hashed (call-count test on a fixture library).
**Size:** M

### P4-02 czkawka similar-media detection
**Story:** As a photo/video hoarder, near-identical shots and re-encodes surface for my judgment — never auto-actioned.
**Depends on:** P4-01
**Description:** `tools/czkawka.py` wrapper (dup/image/video modes, JSON parsing, `CZKAWKA_EXTENSIONS` honored, timeout, missing-binary tolerated with a health warning); results stored as `similar_media` flags linking item pairs/groups for Phase-6 review display.
**Acceptance criteria:**
- [ ] Recorded czkawka output fixtures parse into similarity groups.
- [ ] Similar pairs produce review flags only — proposals remain `proposed` for normal organization, never auto-quarantine (test).
- [ ] Missing binary → warning once + feature marked unavailable in `worker_state`; nothing crashes.
- [ ] `CZKAWKA_EXTENSIONS` change alters the invocation (test).
**Size:** M

### P4-03 Quarantine engine + restore
**Story:** As a user, staged duplicates sit safely in dated quarantine folders, and one command puts any of them back exactly where they were.
**Depends on:** P4-01
**Description:** Quarantine proposals → plan ops per Design Constraints; `quarantine_entries` rows written on commit; `librairy quarantine list` (reason, duplicate-of, original path, age) and `librairy quarantine restore <id|--all>` via the executor (journaled, collision-safe).
**Acceptance criteria:**
- [ ] Commit of a quarantine op moves the file under `/data/quarantine/<date>/…` preserving relative structure; entry row complete.
- [ ] Restore returns it to the original path (or collision-renamed sibling), journaled, `restored_at` set.
- [ ] Quarantined items are excluded from re-analysis (state machine test).
- [ ] Nothing in the quarantine flow can delete a file (grep/invariant test extends to new modules).
**Size:** M

### P4-04 Item lifecycle state machine
**Story:** As the system, every item is in exactly one well-defined state, and nothing processes an item twice.
**Depends on:** —
**Description:** `lifecycle.py` transition table + helpers; retrofit scanner/analyzer/planner/executor to route state changes through it; migration of any ad-hoc state writes.
**Acceptance criteria:**
- [ ] All legal transitions covered by tests; illegal ones raise.
- [ ] Changed fingerprint on a `proposed` item returns it to `discovered` and supersedes its proposal.
- [ ] State counts queryable in one cheap query (dashboard-ready).
**Size:** S

### P4-05 Worker daemon
**Story:** As a user, I drop files in the inbox and walk away; LibrAIry quietly gets them ready for my next review session.
**Depends on:** P4-01..P4-04
**Description:** `worker.py` per Design Constraints: cycle loop, bounded batches, backoff, graceful shutdown, crash-resume, `worker_state` heartbeat; `librairy worker` CLI entry (foreground; `--once` flag runs one cycle for tests/cron).
**Acceptance criteria:**
- [ ] `--once` on the corpus: scan→dedup→analyze→proposals staged; second `--once` with no changes does near-zero work (call counters).
- [ ] Continuous mode: dropping new files mid-run gets them processed next cycle; unstable (growing) files wait.
- [ ] SIGTERM mid-cycle → clean exit; restart resumes without duplicate work or lost items.
- [ ] kill -9 mid-analysis → restart recovers (DB is the state; no corruption).
- [ ] Worker never calls the executor (grep + runtime test): staging only.
- [ ] Backoff verified: idle cycles sleep longer, capped.
**Size:** M

### P4-06 Headless end-to-end test
**Story:** As the project, the complete v1 engine loop is proven before any UI exists.
**Depends on:** P4-05
**Description:** One integration test: seed corpus inbox (including an exact dup of a library file, a similar-media pair, hashtag folders, a project, pending-worthy junk) → run worker `--once` (AI mocked) → assert staged proposals + quarantine proposals + review flags → approve via `librairy propose-plan`/`plan approve` → `commit` → assert library tree, quarantine tree, history, states → `undo --plan` → assert full restoration.
**Acceptance criteria:**
- [ ] The E2E test exists, runs in CI, and passes.
- [ ] Assertion coverage includes: pending junk untouched; project intact; dup quarantined with entry row; similar pair NOT quarantined; undo restores everything.
**Size:** M

### P4-07 Bash retirement + Docker/compose update
**Story:** As the project, one codebase remains — the tested one.
**Depends on:** P4-06 (gate: E2E must be green first)
**Description:** Per Design Constraints: remove `inbox-processor/`, rewrite `docker-compose.yml` (single `librairy` service, 4 data mounts incl. appdata, no reports, no watcher, dashboard stub gone — Phase 5 adds the web service command), Dockerfile per constraints (package install + czkawka in image), README/Instructions updated to the CLI flow, deprecation banner removed, `.env.example` regenerated (no orphan vars).
**Acceptance criteria:**
- [ ] `docker build` succeeds; `docker compose run librairy librairy --help` works; czkawka_cli present in image (`command -v` check in CI smoke build).
- [ ] `git grep -l 'step3_classify\|RAM/\|_review_pending'` returns only docs/plan history references.
- [ ] `.env.example` contains no variable the settings model doesn't define, and vice versa (existing sync test still green).
- [ ] README quickstart (env → compose up → CLI) verified by following it in a container.
**Size:** M

## Verification steps

1. `ruff check src tests && pytest` green (all prior suites).
2. Sandbox continuous run: start `librairy worker`, drop the corpus in stages (including a file copied in slowly via `rsync --bwlimit` or chunked append to prove stability detection), watch proposals accumulate; SIGTERM; restart; confirm resume.
3. Full headless loop by hand: worker → `proposals list` → `propose-plan` → `plan approve` → `commit` → inspect library + quarantine → `quarantine restore` one item → `undo --plan`.
4. `docker build . && docker compose config` — image builds with czkawka; compose is the single-service shape.
5. Repo hygiene: `test ! -d inbox-processor && echo GONE`; README quickstart accurate.

## Exit gate checklist

- [ ] Headless E2E test green in CI.
- [ ] No code path anywhere can delete a user file (invariant grep-test covers all new modules; quarantine/restore round-trip proven).
- [ ] Worker survives SIGTERM and kill -9 with resume; never asks questions; never commits; no reprocessing loops (idle cycle is near-no-op).
- [ ] Exact dupes: dual-method agreement staged for quarantine; disagreement and similar-media flagged for review only.
- [ ] Dedup tool toggles work; `CZKAWKA_EXTENSIONS` and `BATCH_SIZE` honored.
- [ ] `inbox-processor/` deleted; compose/Dockerfile/docs updated; czkawka in image; `.env.example` in sync.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 5's supervisor (`python -m librairy run`) will spawn this worker + uvicorn; keep `worker.main()` importable and signal-clean.
- `worker_state` and lifecycle counts are the Phase-5 dashboard's data source — no new instrumentation should be needed.
- Similar-media review flags render side-by-side in Phase 6 (P6-05 quarantine/duplicates screen).
- Auto-approve policies (per-category confidence rules) remain post-1.0; the state machine already has the seams (`proposed→approved` transition is policy-agnostic).

## Open questions log

2026-07-21: Phase 4 design names its schema migration "004", but Phase 3 already used migration 004 for AI provider model-list persistence. Safest default: preserve append-only migration history and use migration 005 for the next Phase 4 schema change.
