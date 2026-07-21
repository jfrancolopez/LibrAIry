# Phase 9 — Post-1.0 Fast-Follows: Document Text Search, rclone One-Way Backup

**Status:** NOT STARTED
**Depends on:** Phase 8 (v1.0 released) DONE
**Size:** M (two independent features; may be executed as two separate runs, P9-01..02 then P9-03..05)

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

- **Content extraction**: pulling plain text OUT of a document file (PDF via `pdftotext`, EPUB via its XHTML, plain/markdown/office text) without modifying the file. Read-only by nature; OCR of scanned images is explicitly NOT included.
- **`content_fts`**: a second FTS5 table holding extracted document text, separate from the v1 `search_fts` (names/metadata), so content search can be toggled, rebuilt, and size-capped independently.
- **One-way backup**: copying organized library files to a remote (cloud/other NAS) via `rclone copy`. One-way means: local → remote only; remote state NEVER causes local changes; LibrAIry never issues remote deletes (`copy`, never `sync`).
- **Backup remote**: an rclone remote name configured by the user in a mounted `rclone.conf`. LibrAIry does not manage rclone credentials; it consumes a remote the user already configured with rclone's own tooling.
- **Backup run**: one post-commit backup pass: the set of newly committed files queued for copy, executed with verification and per-file status.

## Entry criteria

```bash
# 1. v1.0 shipped: tag exists and full suite is green
git tag --list 'v1.0.0' | grep v1.0.0
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Search screen + FTS infra present (P9 extends them)
librairy index rebuild --help

# 3. Release workflow functional (this phase ships as v1.1+)
test -f .github/workflows/release.yml && echo OK
```

If any check fails, STOP and report.

## Phase goal

Two opt-in features that round out the v1 vision: (1) find documents by what's INSIDE them — "which PDF contains the word coding" — via lightweight extraction into FTS5; (2) protect the organized library with verified one-way rclone backup that runs after successful commits and can never delete anything anywhere. Both ship disabled by default, both are settings-toggleable, and neither complicates the core loop.

## In scope

- Text extraction pipeline (pdftotext/poppler for PDF, EPUB text, plain/markdown, DOCX body text) with size caps and incremental processing; `content_fts` + search-screen `content` facet + snippet display; rebuild + toggle.
- rclone wrapper, backup queue fed by successful commits, verified copies, per-file status, retry with backoff, bandwidth/schedule limits, dashboard/status surfaces, settings section.
- Docker image additions: `poppler-utils`, `rclone` (pinned).
- Docs for both features (incl. privacy note: extracted text stays local, never enters AI prompts).

## Out of scope (tempting, but NO)

- OCR of scanned documents/images (heavy dependency, post-1.x if ever).
- Extracting text from media files (subtitle/lyric mining — never planned; media content is never indexed).
- Two-way sync, remote reconciliation, restore-from-remote automation (backup is copy-out only; restore is documented as a manual rclone operation).
- Sending extracted document text to ANY AI provider, local or cloud (content search is search infrastructure, not classification input — keeps the redaction story simple and true).
- Backing up the inbox or quarantine (organized library + appdata DB snapshot only).
- rclone credential management UI (users configure remotes with rclone directly; LibrAIry lists what it finds).

## Design constraints binding this phase

- **Migration 006**: `content_fts` FTS5 table (`text`, `item_id UNINDEXED`, same unicode61 tokenizer) + `content_extractions` bookkeeping table (`item_id`, `fingerprint`, `extractor`, `chars`, `truncated`, `extracted_at`, `error`) so extraction is incremental by fingerprint and failures are visible, not retried forever (max 3 attempts, then marked failed until fingerprint changes).
- **Extractors** (`content/extract.py` + `tools/pdftotext.py`): PDF → `pdftotext -layout -q` (poppler, subprocess, timeout); EPUB → stdlib zipfile + HTML tag-strip of content documents; `.txt/.md` → direct read with encoding detection; DOCX → stdlib zipfile + `word/document.xml` text nodes. Per-file cap (default 2 MB of text, truncation flagged); per-cycle batch bound; worker runs extraction as a low-priority step after analysis (settings: `content_search.enabled` default **false**; scope: library items in categories documents/books/projects — inbox extraction only after commit, keeping the pre-commit loop fast).
- **Search integration**: `content` facet/toggle on the Phase-7 search screen; results from `content_fts` render with `snippet()` context lines and a `[CONTENT]` source marker distinguishing them from name/metadata hits; combined queries (name OR content) supported; `librairy index rebuild --content` rebuilds the content table alone. Perf guard: content queries paginated and snippet length bounded.
- **Backup engine** (`backup.py` + `tools/rclone.py`): settings (`backup.enabled` default **false**, `backup.remote` e.g. `b2:librairy-backup`, `backup.bandwidth_limit`, `backup.schedule` = `after_commit` | `daily@HH:MM`, `backup.include_db_snapshot` default true). Queue: successful commit → insert `backup_queue` rows (migration 006b: `item_id`, `relpath`, `fingerprint`, `state queued|copying|done|failed`, `attempts`, `last_error`, timestamps). Runner (worker step or schedule): `rclone copy` in relpath batches with `--bwlimit`, `--immutable`-style safety; **the literal strings `sync`, `delete`, `purge`, `move` never appear in constructed rclone argv — enforced by a unit test on the command builder and a runtime assertion**. Verification: `rclone check` (or size/hash listing comparison) per batch before marking `done`. DB snapshot: `sqlite3 .backup`-style consistent copy to appdata then rclone copy of the snapshot. Retry failed rows with exponential backoff; give up after N attempts with `[WARN]` surfaced on dashboard/health. Remote unreachable → backup pauses and reports; commits are NEVER blocked or rolled back by backup state.
- **UI surfaces**: settings section (enable, remote picker from `rclone listremotes`, schedule, bandwidth), dashboard tile (last run, queued/done/failed counts), health row (remote reachability), history entries (`backup_run` summaries). No per-file backup browsing UI (queue table + logs suffice — not a backup manager).
- **Image**: add pinned `poppler-utils` and `rclone` to the runtime stage; `rclone.conf` expected at `<appdata>/rclone/rclone.conf` (documented; rclone invoked with `--config` pointing there).
- **Privacy**: extracted text lives only in the local DB; it is never passed to `ai/` (grep/import test: `content/` modules are not imported by `ai/`, and `RedactedItemView` gains no content field). Backup docs state plainly that library file contents leave the machine ONLY via the user's configured backup remote.

## Backlog items

### P9-01 Content extraction pipeline
**Story:** As a user, my PDFs, ebooks, and notes become searchable by their words — locally, without my documents going anywhere.
**Depends on:** Phase 8
**Description:** Per Design Constraints: migration 006, extractors, caps, incremental-by-fingerprint processing, failure bookkeeping, worker low-priority step, off-by-default setting, image gains poppler-utils.
**Acceptance criteria:**
- [ ] Fixture PDF/EPUB/TXT/MD/DOCX extract correct text (golden snippets); corrupt fixtures record errors and stop retrying after 3 attempts.
- [ ] Extraction is incremental: unchanged fingerprints skip; changed files re-extract (call-count test).
- [ ] Cap + truncation flag honored on an oversized fixture.
- [ ] Disabled setting = zero extraction work in a worker cycle (call test).
- [ ] Extraction runs only on post-commit library items in the scoped categories.
- [ ] Files' mtimes untouched by extraction (read-only proof, mtime snapshot).
**Size:** M

### P9-02 Content search UI + rebuild
**Story:** As a user, I type `coding`, flip on "search inside documents", and the scanned-named `doc_0042.pdf` that mentions coding shows up with the matching line.
**Depends on:** P9-01
**Description:** Per Design Constraints: `content` facet, `[CONTENT]` marker + snippet rendering, combined ranking, `index rebuild --content`, perf bounds.
**Acceptance criteria:**
- [ ] The headline case passes as an integration test: a PDF whose *name* lacks the term but whose *text* contains it is found only when the content facet is on, with a snippet showing the term.
- [ ] Name hits and content hits are visually distinct; combined query returns both.
- [ ] `index rebuild --content` reproduces identical content-search results.
- [ ] Content queries stay under the Phase-7 latency budget on the seeded corpus (perf-marked test).
**Size:** S

### P9-03 rclone wrapper + backup queue
**Story:** As a user, every successful commit quietly queues my newly organized files for off-NAS copies.
**Depends on:** Phase 8
**Description:** Per Design Constraints: migration 006b, `tools/rclone.py` (version probe, `listremotes`, `copy`, `check`, `--config`/`--bwlimit` handling), command-builder safety test (no destructive verbs), commit hook inserting queue rows, DB snapshot helper. Image gains rclone.
**Acceptance criteria:**
- [ ] Successful commit inserts queue rows for exactly the committed files (test).
- [ ] Command builder can produce only `copy`/`check`/`listremotes`/`version` invocations; destructive-verb assertion test passes.
- [ ] Missing rclone binary/config → feature reports unavailable in health; nothing crashes; commits unaffected.
- [ ] DB snapshot is consistent (taken via SQLite backup API, not file copy of a live WAL DB).
**Size:** M

### P9-04 Backup runner: verified, retrying, never-blocking
**Story:** As a user, backups happen after my commits, verify themselves, retry when my link flaps — and can never touch my local files or block my organizing.
**Depends on:** P9-03
**Description:** Per Design Constraints: runner as worker step/schedule, batch `rclone copy` + verification before `done`, exponential-backoff retries with give-up surfacing, bandwidth/schedule settings, pause-and-report on unreachable remote, `backup_run` history entries.
**Acceptance criteria:**
- [ ] Mock-remote (local-dir rclone remote) round-trip: queue → copy → verify → `done`; remote files match fingerprints.
- [ ] Simulated copy failure → retries with backoff → give-up after N → `[WARN]` on dashboard/health (test at small N).
- [ ] Remote deletion of an already-backed-up file NEVER causes any local action, and the next run re-copies it only per the re-queue rules (explicit test: local tree untouched).
- [ ] A running backup never holds the executor lock; a commit during backup proceeds (concurrency test).
- [ ] Backup failure does not alter commit results or item states (isolation test).
**Size:** M

### P9-05 Backup + content-search UI, settings, docs
**Story:** As a user, both features are a toggle, a status tile, and an honest doc page away.
**Depends on:** P9-02, P9-04
**Description:** Settings sections (per Design Constraints), dashboard tile + health rows, `docs/backup.md` (rclone remote setup walkthrough incl. `rclone.conf` mount path, restore-is-manual note with example commands, "what leaves the machine" privacy note) and `docs/content-search.md` (what is extracted, local-only guarantee, OCR non-goal), release notes for v1.1.
**Acceptance criteria:**
- [ ] Enabling either feature from settings starts it on the next cycle without restart; disabling stops it.
- [ ] Remote picker lists remotes from the mounted config; no credential fields exist anywhere in the UI.
- [ ] Dashboard/health surfaces reflect a live mock-remote run.
- [ ] Docs drills: configure a scratch rclone remote and enable backup following only `docs/backup.md`; enable content search following only `docs/content-search.md`.
- [ ] Privacy assertions in docs match code (extracted text not in AI imports — sync/grep test).
**Size:** S

## Verification steps

1. `ruff check src tests && pytest` green (full suite incl. new perf marks).
2. Content drill: drop three PDFs (one named after its topic, one opaquely named `doc_0042.pdf` containing "coding", one scanned/imageonly) → commit → enable content search → search `coding` → opaque PDF found with snippet; scanned one absent (documented OCR limitation); toggle off → only name hits remain.
3. Backup drill with a local-dir rclone remote: enable → commit a batch → watch queue drain on the dashboard → verify remote tree + DB snapshot; delete a file on the remote → confirm nothing happens locally; yank the "remote" (rename dir) mid-run → watch retry/backoff → restore it → queue completes.
4. Confirm a commit executed during an active backup completes normally.
5. Ship rehearsal: tag `v1.1.0-rc`, workflow publishes, pull-and-run, both features work from the published image.

## Exit gate checklist

- [ ] Content queries return document hits by inner text (headline integration test green); extraction is local-only, read-only, incremental, capped, and off by default.
- [ ] `content_fts` rebuildable independently; search UI distinguishes content hits.
- [ ] Backup provably never issues destructive remote verbs (builder test + runtime assertion) and never mutates local state from remote state (test).
- [ ] Backups verify before `done`, retry with backoff, surface give-ups, and never block or alter commits.
- [ ] Both features toggle at runtime without restart; docs drills pass as written.
- [ ] v1.1 published via the release workflow.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

This is the last planned phase. Post-1.1 candidates the project owner may schedule later (deliberately unplanned, listed to park scope creep): OCR for scanned documents; audio-similarity dedup (AcoustID cross-file) with quality-aware keeper ranking; embedded-metadata writing (ID3/EXIF) as an explicit opt-in policy; artwork/subtitle sidecar fetching; notifications (ntfy/Pushover); Synology/QNAP templates + Community Apps feed submission; web-based API key entry (needs its own security review).

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
