# Phase 1 — Core Safety Engine + Project Foundation

**Status:** IN PROGRESS
**Depends on:** none (first phase)
**Size:** L (largest phase — deliberately: a scaffold-only phase would leave nothing testable as a system)

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

- **Item**: one tracked file, identified by (root, relative path) with a content fingerprint. Roots are `inbox`, `library`, `quarantine`.
- **Fingerprint**: `blake2b` hex digest of file content (with size and mtime recorded alongside). Used to prove a file has not changed between plan approval and execution.
- **Plan**: an immutable, uniquely identified set of operations (moves/quarantines). Once approved, it is content-hashed; the executor runs exactly what the hash covers.
- **Plan operation (op)**: one `move` or `quarantine` of one item: source (root, relpath, fingerprint) → destination (root, relpath).
- **Containment**: the property that a destination path, fully resolved (symlinks and `..` collapsed), is inside its declared root directory.
- **Journal / history**: append-only record of every executed operation with before/after paths and outcome, sufficient to undo.
- **Undo**: reversing a previously executed operation (or whole plan) by moving files back to their journaled original paths, itself journaled.
- **Data roots**: `/data/inbox`, `/data/library`, `/data/quarantine`, `/data/appdata` — configurable via env; tests always use temporary directories, never real data.

## Entry criteria

First phase — only sanity checks on the environment:

```bash
# 1. Repo is at the expected starting point (legacy layout present, no src/ yet)
test -d inbox-processor/scripts && test ! -d src && echo OK

# 2. Python 3.11+ available
python3 -c 'import sys; assert sys.version_info >= (3,11); print("OK")'

# 3. Git worktree clean enough to work (no conflicting src/ or docs changes)
git status --porcelain
```

## Phase goal

Build the tested Python package `src/librairy/` whose plan/commit engine makes the legacy pipeline's safety defects *structurally impossible*: analysis cannot move files, commits execute exactly an approved hash-verified plan, destinations cannot escape their roots, nothing is ever overwritten or deleted, every operation is journaled and undoable, and concurrent runs are excluded by a lock. Driveable end-to-end via CLI. The legacy bash pipeline is frozen (documentation warning only) and remains the interim working path.

## In scope

- Python package scaffold, test suite, lint, CI.
- Typed configuration honoring every documented env var.
- SQLite store with migrations.
- Inbox scanner with content fingerprints.
- Immutable plan model; containment and collision safety; atomic executor; locking; history + undo.
- CLI: `scan`, `plan`, `approve`, `commit`, `history`, `undo`.
- Deprecation warning for the legacy bash pipeline (documentation edit only).
- Adversarial tests (traversal fuzzing, crash mid-commit, concurrent runs).

## Out of scope (tempting, but NO)

- Classification of any kind (Phase 2). In this phase, plans are built from explicit operation specs (JSON), not from analysis.
- AI anything (Phase 3). Duplicate detection (Phase 4). Web server (Phase 5).
- Fixing bugs inside the bash scripts — they are frozen and die in Phase 4. The ONLY permitted legacy change is the documentation warning in P1-11.
- Modifying `inbox-processor/catalog/*.py` (ported in Phase 2, untouched here).
- Deleting or restructuring anything under `inbox-processor/`.

## Design constraints binding this phase

- Layout: `pyproject.toml` at repo root; package in `src/librairy/`; tests in `tests/`. Runtime dependencies for this phase: `pydantic` (+ `pydantic-settings`) only. Dev dependencies: `pytest`, `ruff`. Do NOT add FastAPI yet.
- Database file: `<appdata>/librairy.db`. Open with WAL (`PRAGMA journal_mode=WAL`), `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`. Migrations tracked via `PRAGMA user_version`, applied in `db.py` at connection time, each migration in a transaction.
- **Schema v1** (later phases add tables via new migrations; do not pre-create their tables):

```sql
CREATE TABLE items (
  id            INTEGER PRIMARY KEY,
  root          TEXT NOT NULL CHECK (root IN ('inbox','library','quarantine')),
  relpath       TEXT NOT NULL,             -- POSIX-style, relative to root, no leading slash
  size          INTEGER NOT NULL,
  mtime_ns      INTEGER NOT NULL,
  fingerprint   TEXT,                      -- blake2b hex; NULL until hashed
  state         TEXT NOT NULL DEFAULT 'discovered',
  first_seen_at TEXT NOT NULL,             -- ISO-8601 UTC
  last_seen_at  TEXT NOT NULL,
  missing_since TEXT,                      -- set when a rescan no longer finds it
  UNIQUE (root, relpath)
);
CREATE TABLE plans (
  id          TEXT PRIMARY KEY,            -- uuid4
  status      TEXT NOT NULL CHECK (status IN ('draft','approved','executing','done','failed')),
  plan_hash   TEXT,                        -- sha256 of canonical op serialization; set at approval
  created_at  TEXT NOT NULL,
  approved_at TEXT,
  finished_at TEXT
);
CREATE TABLE plan_ops (
  id              INTEGER PRIMARY KEY,
  plan_id         TEXT NOT NULL REFERENCES plans(id),
  seq             INTEGER NOT NULL,        -- execution order within plan
  op_type         TEXT NOT NULL CHECK (op_type IN ('move','quarantine')),
  item_id         INTEGER REFERENCES items(id),
  src_root        TEXT NOT NULL,
  src_relpath     TEXT NOT NULL,
  src_fingerprint TEXT NOT NULL,
  dest_root       TEXT NOT NULL,
  dest_relpath    TEXT NOT NULL,
  result          TEXT,                    -- NULL | 'done' | 'skipped_changed' | 'skipped_missing' | 'renamed_collision' | 'failed'
  final_relpath   TEXT,                    -- actual destination after collision renaming
  executed_at     TEXT,
  UNIQUE (plan_id, seq),
  UNIQUE (plan_id, src_root, src_relpath) -- one owner per source per plan
);
CREATE TABLE history (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  plan_id     TEXT,
  op_id       INTEGER,
  action      TEXT NOT NULL,               -- 'move','quarantine','undo_move','undo_quarantine'
  src_root    TEXT NOT NULL, src_relpath TEXT NOT NULL,
  dest_root   TEXT NOT NULL, dest_relpath TEXT NOT NULL,
  fingerprint TEXT,
  outcome     TEXT NOT NULL                -- 'ok' | error string
);
CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                      -- JSON-encoded
);
CREATE TABLE sessions (                     -- used from Phase 5; created now so schema v1 is complete
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  csrf_token TEXT NOT NULL
);
```

- **Executor rules** (the heart of the phase):
  - Only `executor.py` may move files. Enforced by convention + a test that greps the package for `shutil.move|os.rename|os.replace|Path.rename|Path.replace` outside `executor.py`/`fingerprint.py` test helpers.
  - No code path in the package may call `os.remove`, `os.unlink`, `shutil.rmtree`, `Path.unlink`, or `send2trash` on user data. The only permitted removal is the source half of a verified cross-device copy (copy → fingerprint-verify destination → remove source), and `os.rmdir` on now-empty directories (which cannot destroy content by definition).
  - Same-filesystem moves use atomic rename. Cross-device: copy to `dest.part-<planid>` temp name in the destination directory, fingerprint-verify, rename into place, then remove source.
  - Before each op: re-fingerprint the source; on mismatch record `skipped_changed` and continue (never move a changed file). Missing source → `skipped_missing`.
  - Collisions: if destination exists, derive `name (2).ext`, `name (3).ext`, … deterministically; record `renamed_collision` and the `final_relpath`. Never overwrite (`O_EXCL` semantics / `os.link`+unlink-source or rename onto checked-absent target under the lock).
  - Idempotent re-run: re-committing a partially executed plan skips ops already `done` (journal is the source of truth).
- **Containment** (`paths.py`): destination validation resolves the candidate against its root with `Path.resolve()` and verifies the result `is_relative_to(root.resolve())`; rejects absolute inputs, `..` segments, empty/`.` components, and paths whose parent traverses a symlink pointing outside the root. Component sanitization strips path separators and control characters from *names* (not from the relpath structure, which is validated as a whole).
- **Locking** (`locks.py`): one `flock`-based exclusive lock file at `<appdata>/librairy.lock` taken by any mutating entrypoint (executor, later worker/web commit). Second acquirer fails fast with a clear message.
- **Atomic file writes** anywhere the package writes non-DB files: write `path.tmp` then `os.replace`.
- **Config** (`config.py`, Pydantic settings): every variable documented in `.env.example` / `Instructions.md` §16 must exist as a typed field, including the ones the legacy code ignores. Accept BOTH `OLLAMA_MODEL_PRIMARY` (canonical) and `OLLAMA_MODEL` (legacy alias) for the primary model. New variables: `APPDATA_DIR` (default `/data/appdata`), `HOST_APPDATA_DIR` for compose. Remove nothing from `.env.example` yet; add the new vars and regenerate it from the settings model so docs and code cannot drift.
- Timestamps: UTC ISO-8601 everywhere. Paths stored POSIX-style relative; roots stored separately.
- Tests must never touch real data paths: every filesystem test builds its roots under `tmp_path`.

## Backlog items

### P1-01 Package scaffold, test suite, lint, CI
**Story:** As a developer/agent, I can clone the repo, `pip install -e ".[dev]"`, and run `pytest` and `ruff check` locally and in CI.
**Depends on:** —
**Description:** Create `pyproject.toml` (project `librairy`, `src` layout, console script `librairy = librairy.cli:main`), `src/librairy/__init__.py`, `src/librairy/__main__.py`, empty module stubs per the architecture, `tests/` with a passing smoke test, `ruff` config, and `.github/workflows/ci.yml` running ruff + pytest on push/PR (Python 3.11 and 3.12). Extend `.gitignore` for `*.egg-info`, `.pytest_cache`, `.ruff_cache`, `dist/`.
**Acceptance criteria:**
- [x] `pip install -e ".[dev]"` succeeds on a clean checkout.
- [x] `pytest` runs and passes; `ruff check src tests` passes.
- [ ] CI workflow runs both on GitHub Actions and is green.
- [x] `librairy --help` (console script) and `python -m librairy --help` both work.
**Test notes:** smoke test imports the package and asserts version string.
**Size:** S

### P1-02 Typed settings honoring every env var
**Story:** As a NAS user, every variable I set in `.env` actually takes effect — unlike the legacy pipeline.
**Depends on:** P1-01
**Description:** Implement `config.py` with a Pydantic settings class covering ALL documented env vars: the four `HOST_*_DIR` + `APPDATA_DIR`/`HOST_APPDATA_DIR`, internal `INBOX_DIR`/`LIBRARY_DIR`/`QUARANTINE_DIR`/`REPORTS_DIR`, `TMDB_KEY`, `ACOUSTID_KEY`, `MB_RATE_LIMIT`, `AI_PROVIDER_ORDER`, `CONFIDENCE_THRESHOLD`, `USE_MULTI_AI`, `OLLAMA_HOST`, `OLLAMA_MODEL_PRIMARY` (with `OLLAMA_MODEL` alias), `OLLAMA_MODEL_SECONDARY`, `OPENAI_API_KEY`/`OPENAI_MODEL`, `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`, `GEMINI_API_KEY`/`GEMINI_MODEL`, `MAX_FILES_TO_ANALYZE`, `AI_TIMEOUT`, `MAX_AI_RETRIES`, `BATCH_SIZE`, `IGNORE_PATTERNS`, `CZKAWKA_EXTENSIONS`, `LIBRARY_INDEX_TTL`, `DASHBOARD_PORT`. Types, ranges, defaults; secrets typed as `SecretStr`; a `validate_or_die()` that prints friendly errors listing each bad variable. Regenerate `.env.example` from the model (script `scripts/gen_env_example.py` or equivalent) preserving grouping comments.
**Acceptance criteria:**
- [x] Every variable above exists as a typed field with the documented default.
- [x] `OLLAMA_MODEL` (legacy name) populates the primary model field; `OLLAMA_MODEL_PRIMARY` wins if both set.
- [x] Invalid values (e.g. `CONFIDENCE_THRESHOLD=2.0`) produce a one-line-per-error friendly report, not a traceback.
- [x] Regenerated `.env.example` is committed and a test asserts it stays in sync with the model.
- [x] No default anywhere is a private LAN IP (the legacy `192.168.1.94` default must not reappear).
**Test notes:** round-trip test env → settings; sync test model ↔ `.env.example`.
**Size:** M

### P1-03 SQLite store: schema v1, WAL, migrations
**Story:** As the system, I have one durable, crash-safe place for items, plans, history, settings.
**Depends on:** P1-02
**Description:** Implement `db.py` (connection factory applying pragmas, migration runner keyed on `PRAGMA user_version`) and migration 001 creating the schema in Design Constraints, plus indexes on `items(fingerprint)`, `items(state)`, `plan_ops(plan_id)`, `history(plan_id)`. Provide `models.py` dataclasses mirroring rows.
**Acceptance criteria:**
- [x] Fresh DB reaches `user_version=1` with all tables/indexes; re-opening is a no-op.
- [x] WAL mode and foreign keys verified active by test.
- [x] Migration runner rejects a DB whose `user_version` is newer than the code (clear error, no writes).
- [x] Two processes can read/write concurrently without `database is locked` errors under `busy_timeout` (test with threads/processes).
**Test notes:** migration idempotency; forward-version refusal; concurrent access smoke test.
**Size:** M

### P1-04 Inbox scanner with content fingerprints
**Story:** As the system, I know exactly what is in the inbox, and I can tell if any file changed since I last looked.
**Depends on:** P1-03
**Description:** `scanner.py` + `fingerprint.py`: walk a root (skip hidden files/dirs and `IGNORE_PATTERNS` globs), upsert items by `(root, relpath)`, record size/mtime, compute `blake2b` fingerprints (streamed, 1 MiB chunks). Incremental: unchanged size+mtime skips re-hashing; changed files re-hash and reset state to `discovered`; vanished files get `missing_since`. Stability detection: a file whose size or mtime changed within the last `N` seconds (configurable, default 10) is recorded but flagged not-yet-stable (still copying) — callers exclude unstable items.
**Acceptance criteria:**
- [x] Scan of a fixture tree produces correct items with correct fingerprints (verified against `b2sum`-style reference).
- [x] Second scan with no changes re-hashes nothing (verified via hash-call counter/mock).
- [x] Modified file is detected by size/mtime and re-fingerprinted.
- [x] Deleted file gets `missing_since` set; reappearing file clears it.
- [x] A growing file (simulated) is flagged unstable and excluded from "ready" queries.
- [x] Symlinks are recorded as their own item type or skipped (documented choice) — never followed out of the root.
**Test notes:** all under `tmp_path`; include unicode names, deep nesting, 0-byte files, duplicate basenames in different dirs.
**Size:** M

### P1-05 Immutable plan model
**Story:** As a user, what I approve is exactly what runs — bit for bit.
**Depends on:** P1-03, P1-04
**Description:** `planner.py`: create a `draft` plan from a list of operation specs (this phase: loaded from a JSON file via CLI; Phase 2+ will generate specs from proposals). Each op captures the source item's current fingerprint. `approve(plan_id)` validates every op (containment via P1-06, source exists, no duplicate sources, no two ops sharing one destination), computes `plan_hash` = sha256 over the canonical JSON serialization of ordered ops, and flips status to `approved`. Any attempt to modify an approved plan's ops fails.
**Acceptance criteria:**
- [x] Draft → approved sets `plan_hash`; recomputing the hash from stored ops matches.
- [x] Approval rejects: duplicate source paths, duplicate destinations, missing sources, containment violations — with per-op error messages.
- [x] Ops of an approved plan cannot be inserted/updated/deleted (guarded in code; test proves it).
- [x] Plan serialization is canonical (stable key order, no float drift) so hashes are reproducible.
**Test notes:** golden hash test with a fixed fixture plan.
**Size:** M

### P1-06 Containment + collision safety
**Story:** As a user, no bug, no malicious filename, and no hallucinating AI can ever place a file outside my library — or on top of another file.
**Depends on:** P1-01
**Description:** `paths.py`: `validate_dest(root: Path, relpath: str) -> Path` implementing the containment rules in Design Constraints; `sanitize_component(name)` for single path components; `resolve_collision(dest: Path) -> Path` producing `name (2).ext` style alternatives (correct for extensionless names and dotfiles — no `name_1.name` corruption like the legacy code).
**Acceptance criteria:**
- [x] Rejects: `../x`, `a/../../x`, absolute paths, `~`, empty components, components of only dots, backslash separators, NUL and control chars.
- [x] Rejects a relpath whose parent directory is a symlink escaping the root (test builds one).
- [x] Property/fuzz test: for thousands of generated hostile strings, `validate_dest` either raises or returns a path strictly inside the root — never anything else.
- [x] Collision naming: `file.txt`→`file (2).txt`; `file`→`file (2)`; `.hidden`→`.hidden (2)`; `a.tar.gz`→`a.tar (2).gz` or documented alternative — deterministic and tested.
**Test notes:** use `hypothesis` if added as dev-dep (allowed), else a generated corpus; either way thousands of cases.
**Size:** M

### P1-07 Atomic, journaled, idempotent executor
**Story:** As a user, a commit either does exactly what was approved or tells me precisely what it skipped — even across crashes and power loss.
**Depends on:** P1-05, P1-06, P1-08, P1-09
**Description:** `executor.py::execute(plan_id)` under the exclusive lock: verify plan status `approved` and stored `plan_hash` matches recomputation; iterate ops in `seq` order; per op re-fingerprint source (skip `skipped_changed`/`skipped_missing` on mismatch/absence), validate destination containment again (defense in depth), resolve collisions, move atomically (same-fs rename; cross-device copy→verify→rename→remove-source), journal to `history`, update `plan_ops.result`/`final_relpath`, and update the `items` row to its new root/relpath. Mark plan `done` (all ops terminal) or `failed` (unexpected error; already-done ops stay done). Re-running `execute` on a partially executed plan continues from the first non-terminal op.
**Acceptance criteria:**
- [x] Executes a multi-op plan; filesystem end state matches the plan exactly; journal has one row per op.
- [x] Source changed between approval and execution → op `skipped_changed`, file untouched, execution continues.
- [x] Destination collision → deterministic rename, `renamed_collision` recorded with `final_relpath`.
- [x] Kill -9 mid-execution (test with subprocess) → re-run completes remaining ops; no file lost, none duplicated, no partial `.part-*` residue after completion.
- [x] Cross-device path exercised (bind/mock or forced-copy flag): destination fingerprint verified before source removal.
- [x] Executing a plan whose hash no longer matches its ops aborts before touching any file.
- [x] Grep-test: no `os.remove/unlink/rmtree` on user data outside the verified-copy source removal; no move primitives outside `executor.py`.
**Test notes:** the crash test spawns a real subprocess and kills it between ops (hook/env var to pause); assert invariants, then resume.
**Size:** L

### P1-08 Process locking
**Story:** As the system, two mutating processes can never interleave file operations.
**Depends on:** P1-02
**Description:** `locks.py`: `flock`-based exclusive lock on `<appdata>/librairy.lock` with context manager, non-blocking acquire + clear "another LibrAIry process holds the lock" error, and stale-proof semantics (flock releases on process death automatically).
**Acceptance criteria:**
- [x] Second acquirer in another *process* fails fast with the friendly message (test with `multiprocessing`/`subprocess`).
- [x] Lock is released on normal exit, exception, and SIGKILL of the holder.
**Test notes:** must test across processes, not threads (flock is per-fd).
**Size:** S

### P1-09 History journal + undo
**Story:** As a user, I can see everything LibrAIry ever did and reverse any of it.
**Depends on:** P1-03
**Description:** `history.py`: append-only journal writer (used by executor) and undo: `undo_op(op_id)` / `undo_plan(plan_id)` move files back from their journaled destination (`final_relpath`) to their journaled source, using the same executor safety machinery (fingerprint re-check, containment, collision handling if the original path is now occupied, journaling the undo as its own action). Undo never deletes; if the file at the destination no longer matches the journaled fingerprint, refuse that op with a clear message and continue with the rest.
**Acceptance criteria:**
- [x] `undo_plan` after a commit restores the exact prior tree (fixture comparison), journaled as `undo_move` rows.
- [x] Undo of an op whose file was modified post-commit is refused for that op (message includes both fingerprints) while others proceed.
- [x] Undo of an undo is possible (it is just another journaled move).
- [x] Undoing a quarantine op restores the file to its original path.
**Test notes:** round-trip commit→undo→commit tests; occupied-original-path collision case.
**Size:** M

### P1-10 CLI
**Story:** As a developer/agent (and power user), I can drive the whole engine headless.
**Depends on:** P1-04..P1-09
**Description:** `cli.py` (stdlib `argparse`): `librairy scan` (scan inbox, print summary), `librairy plan create --from-file ops.json` (spec: list of `{op_type, src_relpath, dest_root, dest_relpath}`), `librairy plan show <id>`, `librairy plan approve <id>`, `librairy commit <plan-id>`, `librairy history [--plan <id>] [-n N]`, `librairy undo --op <id> | --plan <id>`, `librairy db path|migrate`. Human-readable output + `--json` flag for machine output. Exit codes: 0 success, 1 partial (skips), 2 error.
**Acceptance criteria:**
- [x] Full lifecycle works end-to-end in a temp sandbox: scan → plan create → approve → commit → history → undo.
- [x] `--json` output is valid JSON for every command (test parses it).
- [x] Destructive-looking commands print what they will do and require `--yes` (or interactive confirm) before executing.
**Test notes:** invoke via `subprocess` against a tmp sandbox to test the real entrypoint.
**Size:** M

### P1-11 Legacy bash freeze (documentation only)
**Story:** As a user, I am warned that the old pipeline is unsafe and unmaintained before I run it.
**Depends on:** —
**Description:** Add a prominent deprecation warning to `README.md` (top) and `Instructions.md` (top): the bash pipeline is frozen, will be replaced, and has known dangerous behaviors — explicitly: step4 "dry-run" moves files into `/data/inbox/_review_pending`; step5 executes a different plan than step4 previewed; the watcher loops indefinitely. Link to `docs/plan/README.md`. **No functional change to any `.sh` file — this is the only legacy edit this phase permits.**
**Acceptance criteria:**
- [x] Warning present at the top of both docs, naming the three dangerous behaviors.
- [x] `git diff` shows zero changes under `inbox-processor/`.
**Size:** S

### P1-12 Adversarial test suite
**Story:** As the project, the safety invariants are protected by tests hostile enough to catch regressions.
**Depends on:** P1-06, P1-07
**Description:** Consolidate the adversarial cases into a dedicated suite: containment fuzzing (P1-06), crash/kill matrix (before first op, between ops, mid-copy), double-execution race (two processes attempt `commit` concurrently → exactly one proceeds), stale plan (source tree replaced wholesale after approval → all ops skipped, nothing moved), unicode/emoji/255-byte filenames, read-only destination directory (clean failure, no partial state), disk-full simulation on cross-device copy (temp file cleaned up, source intact).
**Acceptance criteria:**
- [x] All scenarios above have explicit tests and pass.
- [x] Suite runs in CI within reasonable time (mark the slowest as `slow` but still run them in CI).
- [x] A "safety invariants" test module greps the package for forbidden calls (deletion primitives on user data, move primitives outside executor) and fails on violation.
**Size:** M

## Verification steps

1. `pip install -e ".[dev]" && ruff check src tests && pytest` — all green.
2. In a scratch sandbox (`/tmp/librairy-sandbox` with `inbox/ library/ quarantine/ appdata/`, a few nested files including a duplicate basename and a unicode name), run the full CLI lifecycle: scan → `plan create --from-file` (move two files, quarantine one) → approve → commit → verify tree → `history` shows 3 ops → `undo --plan` → verify original tree restored.
3. Re-run `commit` on the completed plan → reports nothing to do; no changes.
4. Attempt a plan containing `"dest_relpath": "../../escape.txt"` → approval fails, names the op.
5. Run two `librairy commit` processes simultaneously → one wins, one exits with the lock message.
6. `kill -9` a commit mid-run (use the test pause hook) → re-run completes; verify no file lost (count + fingerprints).
7. Confirm `git diff --stat inbox-processor/` shows only zero lines changed (P1-11 touches only README/Instructions).

## Exit gate checklist

- [ ] CI green on a clean clone (ruff + pytest, Python 3.11 & 3.12).
- [x] A committed plan executes byte-for-byte as approved: plan-hash verified before execution; executor never recomputes destinations.
- [x] Kill-mid-commit leaves a consistent journal, loses no file, and a re-run completes the plan.
- [x] Containment property tests pass (hostile-path fuzzing) and containment is re-checked at execution time.
- [x] No overwrite is possible: collision tests pass; no deletion primitive targets user data anywhere in the package (grep-test enforced).
- [x] Undo restores prior state and is itself journaled.
- [x] Every documented env var is honored by `config.py`; regenerated `.env.example` in sync (test-enforced).
- [x] Lock excludes concurrent mutators across processes.
- [x] Legacy bash byte-identical; deprecation warning present in README.md and Instructions.md.
- [ ] All backlog checkboxes above ticked; status line set to DONE.

## Notes for future phases

- Phase 2 builds proposals that compile into the P1-05 plan-spec format — the plan/commit engine must not need changes for classification to plug in.
- The `sessions` table is created now but first used in Phase 5.
- Scanner stability detection (P1-04) is reused by the Phase 4 worker; the worker adds scheduling, not new scanning logic.
- `REPORTS_DIR` remains in config only for legacy coexistence; Phase 4 removes its last use.

## Open questions log

*(Executing agent: when the spec is ambiguous, record the question and the safest-default decision you took, then continue.)*
