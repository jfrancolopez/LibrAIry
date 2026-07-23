# LibrAIry Completion Plan — Master Overview

This directory contains the complete, phased backlog to take LibrAIry from its current CLI prototype (v2.2) to a dependable v1.0: a privacy-first, Docker-shipped, web-managed file organizer for NAS systems.

**Each phase document is fully self-contained.** An AI agent (or human) given ONE phase file plus a checkout of this repository has everything needed to execute that phase: product context, glossary, entry checks, backlog items with acceptance criteria, verification steps, and an exit gate. No access to any prior conversation is required.

## How to execute a phase

1. Pick the lowest-numbered phase whose status line is `NOT STARTED` and whose dependencies are `DONE`.
2. Open [EXECUTION-PROMPT.md](EXECUTION-PROMPT.md), fill in the phase file path, and give that prompt to the executing agent.
3. The agent verifies the phase's Entry Criteria, implements the backlog items in order, updates the checkboxes and status line inside the phase doc as it goes, and flips the status to `DONE` only when every Exit Gate item passes.
4. Phases must be executed in numeric order unless a phase doc's "Depends on" header says otherwise.

## Phase map

| # | File | Title | Depends on | Status |
|---|------|-------|-----------|--------|
| 1 | [phase-1-core-engine.md](phase-1-core-engine.md) | Core safety engine + project foundation | — | NOT STARTED |
| 2 | [phase-2-classification.md](phase-2-classification.md) | Classification engine (catalog + heuristics, no AI) | 1 | NOT STARTED |
| 3 | [phase-3-ai-providers.md](phase-3-ai-providers.md) | AI provider layer + privacy redaction | 2 | NOT STARTED |
| 4 | [phase-4-dedup-worker.md](phase-4-dedup-worker.md) | Dedup, background worker, bash retirement | 3 | NOT STARTED |
| 5 | [phase-5-web-foundation.md](phase-5-web-foundation.md) | Web shell: auth, Pip-Boy theme, dashboard, health | 4 | NOT STARTED |
| 6 | [phase-6-review-commit-ui.md](phase-6-review-commit-ui.md) | Review queue, commit, quarantine, history UI | 5 | NOT STARTED |
| 7 | [phase-7-search-settings.md](phase-7-search-settings.md) | Search/browse, settings, AI provider selector | 6 | NOT STARTED |
| 8 | [phase-8-release.md](phase-8-release.md) | Release hardening, UNRAID packaging, v1.0 | 7 | NOT STARTED |
| 9 | [phase-9-fast-follows.md](phase-9-fast-follows.md) | Post-1.0: document text search, rclone backup | 8 | NOT STARTED |
| 10 | [phase-10-release-acceptance.md](phase-10-release-acceptance.md) | Release acceptance & v1.0.0 publish | 9 | NOT STARTED |
| 11 | [phase-11-tui.md](phase-11-tui.md) | Terminal UI (`librairy tui`) | 10, 14 | NOT STARTED |
| 12 | [phase-12-portal-fixes.md](phase-12-portal-fixes.md) | Portal defect fixes (pre-v1.0-tag blockers) | 10 (P10-01..05) | NOT STARTED |
| 13 | [phase-13-theme-system.md](phase-13-theme-system.md) | Theme system + settings UX (v1.1) | 12 | NOT STARTED |
| 14 | [phase-14-screen-redesigns.md](phase-14-screen-redesigns.md) | Screen redesigns: dashboard/review/browse/history/health | 13 | NOT STARTED |
| 15 | [phase-15-catalog-expansion.md](phase-15-catalog-expansion.md) | Catalog expansion + web API key entry (v1.2) | 14 | NOT STARTED |

Dependency shape: linear 1 → … → 9 for the original v1.0 build (phases 1–4 headless, 5–7 web, 8 packages, 9 fast-follows). Post-completion order (owner-decided 2026-07-23): **P10-01..05 → phase 12 (defect fixes) → P10-06 (tag v1.0.0) → 13 (theme, v1.1) → 14 (redesigns) → 15 (catalogs, v1.2) → 11 (TUI last, so it inherits the final design language).**

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY. Every phase doc embeds an identical copy of this section. If a product decision ever changes: update it HERE, bump the version marker, and propagate to every phase doc. Drift is detectable with: grep -c 'context-boilerplate v1' docs/plan/*.md -->

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

## Current state of the repository (audited 2026-07-21, commit 8089d73)

**What exists and is kept/adapted:**

| Asset | Location | Fate |
|---|---|---|
| Heuristics rule engine (454 lines, good) | `inbox-processor/catalog/heuristics.py` | Ported into `src/librairy/` in Phase 2 |
| Music lookup: tags + AcoustID + MusicBrainz | `inbox-processor/catalog/music_lookup.py` | Ported in Phase 2 |
| Video lookup: TMDB movies/TV | `inbox-processor/catalog/video_lookup.py` | Ported in Phase 2 |
| Library consistency index | `inbox-processor/catalog/library_index.py` | Reworked into read-only SQLite indexer in Phase 2 |
| Genre maps, sanitization helpers | `inbox-processor/catalog/utils.py` | Ported in Phase 2 |
| Docker packaging (debian:bookworm-slim + ffmpeg, exiftool, fpcalc, rmlint) | `Dockerfile`, `docker-compose.yml` | Evolved through Phases 4–8 |
| Interactive setup wizard | `setup.sh` | Superseded by boot-time env validation + web settings (Phase 8 decides its fate) |
| Bash pipeline (~2,500 lines: main.sh, step1–step5) | `inbox-processor/scripts/` | Frozen in Phase 1, deleted in Phase 4 |

**What does not exist at all:** web UI (the `dashboard` compose service is a `sleep infinity` stub), HTTP server, database, search, tests, CI.

**Confirmed defects in the legacy pipeline** (verified with line references; they are listed so agents understand why the rewrite exists — they are NOT to be fixed in bash, because the bash dies in Phase 4):

1. `step4_dryrun.sh:75,84` — the "dry-run" physically moves low-confidence/bad-name items to `/data/inbox/_review_pending`.
2. `step3_classify.sh:1416-1471` — files AND their parent folders become competing classification candidates.
3. `step4_dryrun.sh:141` / `step5_commit.sh:136` — name sanitizer keeps `/` and `.`, allowing path traversal in `rename_to`.
4. `step5_commit.sh` — no canonical containment validation of destinations (no realpath check anywhere).
5. `((var++))` under `set -euo pipefail` aborts steps 2/4/5 on the first increment from 0 (e.g. `step5_commit.sh:139`).
6. step4 and step5 compute destinations differently (year suffix, nested lookup, per-file paths) — preview ≠ commit.
7. `step5_commit.sh:13,196` — commit re-reads step3 output; the reviewed step4 plan is never consumed.
8. `step5_commit.sh:156` — nested sources located by `find -name <basename> | head -1` (ambiguous).
9. All JSON reports written non-atomically; step3 streams an array open-bracket first (corruptible).
10. No run locks, plan IDs, source fingerprints, or stale-plan checks anywhere.
11. Watcher (`docker-compose.yml:83-97`) re-runs the pipeline forever: never commits, counts `_review_pending` as new files.
12. `step3_classify.sh:632-637` — cloud AI prompts include full absolute paths, every filename, and photo EXIF GPS latitude/longitude, city, country, camera model.
13. Ignored/hardcoded config: `CONFIDENCE_THRESHOLD`, `AI_TIMEOUT`, `MAX_FILES_TO_ANALYZE`, `CZKAWKA_EXTENSIONS` are hardcoded despite `.env` entries; step3 reads `OLLAMA_MODEL` but `.env` defines `OLLAMA_MODEL_PRIMARY`; `OLLAMA_HOST` default is a developer's private IP (`step3_classify.sh:24`); step1/step2 read `INBOX_DIRS`/`LIBRARY_DIRS` (plural) which `.env` never defines.
14. No tests, no CI.
15. `library_index.py:130-154` — `register_*` incremental-update methods are dead code (never called).
16. `mv -n` success masks silent no-ops in `safe_move` (`step5_commit.sh:75`).
17. czkawka_cli is not in the shipped image, so step2 always fails (`Dockerfile:5-6`).

## Target architecture (summary)

```
src/librairy/
  config.py        Pydantic settings — every env var, typed, validated
  db.py            sqlite3 connection factory, WAL, migrations via PRAGMA user_version
  models.py        Item, Plan, PlanOp, Proposal, Evidence, ...
  fingerprint.py   BLAKE2b content hashing
  scanner.py       inbox/library walking, stability detection, incremental rescan
  paths.py         containment validation, name sanitization, collision naming
  planner.py       proposals → immutable approved plans
  executor.py      the ONLY code that moves files; atomic, journaled, idempotent
  history.py       operation journal + undo
  locks.py         flock single-writer lock
  taxonomy.py      categories + destination template registry        (Phase 2)
  classify/        heuristics, music, video, docs, grouping, hashtags (Phase 2)
  tools/           subprocess adapters: ffprobe, exiftool, fpcalc,
                   rmlint, czkawka                                   (Phases 2, 4)
  ai/              provider abstraction, ollama, cloud, redaction    (Phase 3)
  dedup.py         exact + similar duplicate engine                  (Phase 4)
  worker.py        background analysis daemon                        (Phase 4)
  web/             FastAPI app, auth, routes, Jinja2 templates,
                   Pip-Boy static assets                             (Phases 5–7)
  supervisor.py    `python -m librairy run` → web + worker           (Phase 5)
  cli.py           scan / plan / commit / history / undo / worker
```

Processes communicate only through SQLite (WAL) and the filesystem. No queues, no brokers.

## Document conventions

- Backlog item IDs: `P<phase>-<nn>` (e.g. `P4-03`). Commit messages during execution: `P4-03: <title>`.
- Sizes: S (≤ half day), M (≈ a day), L (multi-day) — coarse, for sequencing only.
- Every phase doc follows the same section template (see any phase file): Header → Product Context → Glossary → Entry Criteria → Goal → In/Out of Scope → Design Constraints → Backlog Items → Verification → Exit Gate → Notes for Future Phases → Open Questions Log.
- Entry criteria are **runnable checks** (commands + expected results), not prose. Every entry criterion of phase N corresponds to an exit-gate item of an earlier phase.
- The executing agent updates its phase doc in place: item checkboxes, the status line, and the Open Questions Log. It never edits other phase docs.

## Decision-change protocol

If the project owner changes a locked decision: edit the canonical boilerplate in THIS file, bump the marker (e.g. `context-boilerplate v2`), copy the new block into every phase doc not yet `DONE`, and record the change in a `## Decision log` appended to this file. Docs already `DONE` are historical records — leave them.
