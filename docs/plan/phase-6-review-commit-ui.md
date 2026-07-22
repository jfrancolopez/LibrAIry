# Phase 6 — Review Queue, Commit, Quarantine, History UI

**Status:** IN PROGRESS
**Depends on:** Phase 5 (web foundation) DONE
**Size:** M/L (this is the product's core loop in the browser)

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

- **Review queue**: the screen listing `proposed` items (grouped by relationship group) with evidence, confidence, source→destination, and per-item/batch actions.
- **Batch actions**: approve / reject / postpone applied to a selection (checkboxes + "select all matching current filter").
- **Edit**: user-modified destination or clean name on a proposal before approval. Edits are server-revalidated (containment, collision, template sanity); the browser's value is never trusted.
- **Commit flow**: freezing the current approved set into an immutable Phase-1 plan (hash and all), showing a confirmation summary, executing via the engine in a background task, and streaming progress from `plan_ops`.
- **Preview**: a lightweight decision aid — image thumbnail, video poster frame + probe facts, audio tag card — served with strict path validation, cached under `<appdata>/thumbs/`. NOT a media player, NOT a file manager.
- **Duplicate review**: side-by-side comparison for quarantine proposals and czkawka `similar_media` flags (both files' facts; for images, both thumbnails).

## Entry criteria

```bash
# 1. Phase 5 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Portal runs: login + dashboard live
python -m librairy run &  # then browse http://localhost:8080 (manual) — or run the P5 supervisor smoke test

# 3. Engine loop works headless (the UI wraps exactly this)
librairy proposals --help && librairy propose-plan --help && librairy quarantine --help
```

If any check fails, STOP and report.

## Phase goal

Put the product's core loop in the browser: review accumulated proposals in batches (with previews and evidence), edit safely, approve, commit exactly the approved plan with live progress, review/restore quarantine, and browse/undo history. After this phase the terminal is optional for daily use.

## In scope

- Review queue screen with grouping, filtering, pagination, batch actions, inline edit.
- Commit flow (freeze → confirm → execute in background → progress → results).
- Preview generation (image thumbs, video poster + facts, audio tag card) with caching.
- Quarantine screen (evidence, side-by-side duplicates, restore). Similar-media review list.
- History screen (journal browsing, plan/op undo).
- Server-side revalidation of every user edit.

## Out of scope (tempting, but NO)

- Search, browse-the-library, settings screens (Phase 7).
- Any in-browser file deletion — the UI must not even offer it (invariant 1).
- Media playback/streaming, EXIF editors, bulk rename tools — not a file manager.
- Editing committed history (journal is append-only; undo is the only mutation).
- Auto-approve rules (post-1.0).
- Renaming/moving anything already in the library (invariant 3) — edits apply to *proposals* only.

## Design constraints binding this phase

- **Routes** (`web/routes/`): `review.py`, `commit.py`, `quarantine.py`, `history.py`, `preview.py`. All behind auth+CSRF from Phase 5.
- **Review queue**: default sort = confidence desc within group, groups by kind then label; filters: category, state (`proposed`/`pending`/`postponed`), min/max confidence, has-destination; pagination (50 rows/page, HTMX partial swaps). Pending items (no destination) are visible with their evidence and an edit box (giving them a destination = human classification, revalidated like any edit). Evidence rendered as source-labeled lines (`[MB] artist match 0.93`, `[AI:ollama/qwen3:4b] category=photos`, `[#tag] italy`); cloud-AI-derived evidence gets a distinct `[CLOUD]` marker — the user can always see when cloud AI touched a file.
- **Edits**: PATCH-style HTMX post per proposal (clean name, dest relpath, category); server pipeline: sanitize components → template/containment validation (`paths.validate_dest`) → collision precheck against filesystem AND other live proposals (warn + auto-suffix preview) → save + re-render row. Invalid input → inline `[FAIL]` message, value unsaved. A grep/route test proves no filesystem write occurs in any review/edit handler.
- **Commit flow**: POST `commit/create` compiles all `approved` proposals into a Phase-1 draft plan → `plan approve` (hash) → confirmation screen (op count, per-category summary, quarantine count, full op table paginated, plan hash displayed) → POST `commit/execute/<plan_id>` starts a background thread (daemon) running the executor under the flock; UI polls a progress partial (`done/skipped/failed` per `plan_ops.result` counts + last N ops); terminal state shows the results table with skips explained. Locked-by-worker contention → friendly retrying banner (the executor's non-blocking lock attempt surfaces it). One in-flight commit at a time (guard in DB/app state).
- **Previews** (`preview.py` + `thumbs.py` helper): image → Pillow **or** ffmpeg-scaled JPEG thumbnail (~320px, choose one implementation and document; Pillow is an acceptable new dependency, decide by image-format coverage); video → ffmpeg single-frame poster at 10% duration + duration/resolution/codec facts; audio → tag card (no waveform). Thumbs cached by fingerprint under `<appdata>/thumbs/`, size-capped (LRU prune of the *cache only* — cache files are LibrAIry-generated, not user data, so pruning is permitted; state this in code comments). Preview route takes item IDs, never paths; resolves via DB; validates the resolved path is inside a data root before reading; streams with correct content-type and immutable cache headers.
- **Quarantine screen**: staged (proposals) and executed (entries) sections; each row: reason, evidence, keeper link, original path, age; side-by-side panel for a selected pair (both files' facts + thumbnails when images/videos); actions: restore (executed entries; engine-backed), un-stage (staged proposals → back to `proposed`), and approve-stage (into next commit). NO delete button exists.
- **History screen**: paginated journal (time, action, src→dest, plan link, outcome); plan detail view (ops table + hashes); undo buttons (op / whole plan) with confirmation modal; undo runs like commit (background + progress); refused undos (changed fingerprint) rendered with both fingerprints and plain-language explanation.
- **Accessibility**: whole review flow keyboard-operable (tab order, space to select, enter to open); status never color-only (`[OK]/[WARN]/[FAIL]` idiom); labels on all controls; tables use proper `<th>`; focus states visible in the Pip-Boy theme.

## Backlog items

### P6-01 Review queue screen
**Story:** As a user, I open Review and see everything LibrAIry proposes, grouped sensibly, with the evidence to judge it fast.
**Depends on:** Phase 5
**Description:** Queue route/templates per Design Constraints: grouping, filters, pagination, evidence rendering with source markers (incl. `[CLOUD]`), confidence bars, pending-item visibility.
**Acceptance criteria:**
- [x] Seeded sandbox renders groups (album/season/event/project) with member counts; filters and pagination work via HTMX (no full reloads).
- [x] Evidence lines show source labels; cloud-derived evidence shows `[CLOUD]` (fixture test).
- [x] Pending items visible with no destination and an edit affordance.
- [x] 5,000-proposal seed renders a page < 1s (pagination enforced; no unbounded queries — test).
**Size:** M

### P6-02 Batch + per-item actions
**Story:** As a user, I clear a day's inbox in a few keystrokes: select, approve, done.
**Depends on:** P6-01
**Description:** Approve/reject/postpone endpoints (single + batch with select-all-matching-filter), state transitions via `lifecycle.py`, optimistic HTMX row updates, undo-last-action toast (state-level revert of the batch — not filesystem).
**Acceptance criteria:**
- [x] Batch approve of a filtered set transitions exactly the matching proposals (boundary test: filter excludes one item → it is untouched).
- [x] Rejected items return to `pending` with proposal kept for reference (status `rejected`); postponed items leave the default queue view.
- [x] All actions CSRF-protected; keyboard-only operation verified (integration test drives the DOM sequence).
**Size:** M

### P6-03 Safe edit pipeline
**Story:** As a user, I can fix LibrAIry's guess before approving — and I cannot break anything by typing garbage.
**Depends on:** P6-01
**Description:** Per Design Constraints: edit endpoints with the sanitize→validate→collision-precheck→save pipeline; category change re-renders the destination via templates; inline errors.
**Acceptance criteria:**
- [x] `../../etc/x`, absolute paths, backslashes, control chars, `{token}` remnants all rejected inline with a reason (parametrized test).
- [x] Edit colliding with an existing library file or another proposal shows the auto-suffix preview; saved value is the suffixed one on approval.
- [x] Server revalidation proven independent of the browser: raw httpx POST with hostile values → 422, nothing saved.
- [x] Editing never touches the filesystem (mutation-sweep test on edit handlers).
**Size:** M

### P6-04 Previews
**Story:** As a user deciding photo and video fates, I see what the file IS without opening a file manager.
**Depends on:** P6-01
**Description:** Per Design Constraints: thumbs.py generation + cache, preview routes by item ID, image/video/audio cards in queue rows and detail panes.
**Acceptance criteria:**
- [x] Image and video fixtures render thumbnails; audio renders a tag card; unsupported types render a typed placeholder icon.
- [x] Cache hit on second request (no regeneration; call-count test); cache pruning removes only `<appdata>/thumbs/` files (path assertion in the pruner + test).
- [x] Preview route with a forged/unknown ID → 404; DB path escaping a data root (crafted row) → 403 and logged.
- [x] Thumbnail generation failures (corrupt file fixture) degrade to placeholder without breaking the row.
**Size:** M

### P6-05 Quarantine + duplicate review screen
**Story:** As a user, I see exactly why something was flagged as a duplicate, compare both copies, and restore with one click — deletion stays my job, outside LibrAIry.
**Depends on:** P6-04
**Description:** Per Design Constraints: staged/executed sections, side-by-side comparison, restore/un-stage/approve-stage actions, similar-media flag list feeding the same comparison panel.
**Acceptance criteria:**
- [x] Executed quarantine rows show reason, keeper link, original path; restore round-trips the file (filesystem-verified test) and journals.
- [x] Side-by-side shows both files' size/dates/facts and thumbnails for an image pair fixture.
- [x] Similar-media flags listed separately, clearly marked "needs human judgment", with compare; no quarantine action is auto-applied to them.
- [x] No delete affordance anywhere on the screen (template test greps rendered HTML).
**Size:** M

### P6-06 Commit flow with live progress
**Story:** As a user, I press COMMIT, watch the ops tick by Pip-Boy style, and get an honest report — including anything skipped and why.
**Depends on:** P6-02, P6-03
**Description:** Per Design Constraints: create→confirm→execute-in-background→progress-poll→results; single-in-flight guard; lock-contention banner; plan hash surfaced at confirm and results.
**Acceptance criteria:**
- [ ] Full loop on the sandbox: approve batch → confirm screen op table matches approved proposals exactly (count + spot rows) → execute → progress advances → results table matches `plan_ops` terminal states.
- [ ] Committed plan hash shown at confirmation == hash recorded at execution (test asserts equality end-to-end): the UI provably commits what it showed.
- [ ] A source modified between confirm and execute → op reported `skipped_changed` in results with explanation.
- [ ] Second concurrent commit attempt → blocked with friendly message (test).
- [ ] Web process stays responsive during a large commit (progress endpoint answers while executor runs — threaded execution test).
**Size:** L

### P6-07 History + undo screen
**Story:** As a user, I can always answer "what did LibrAIry do to my files?" — and take any of it back.
**Depends on:** P6-06
**Description:** Per Design Constraints: journal browser, plan detail, op/plan undo with confirm modal + background execution + progress, refused-undo explanations.
**Acceptance criteria:**
- [ ] History lists the sandbox commit; plan detail shows ops + hash; single-op undo restores that file (filesystem-verified).
- [ ] Whole-plan undo after the P6-06 commit restores the pre-commit tree (fixture comparison) and appears in history as undo actions.
- [ ] Fingerprint-mismatch refusal path rendered with both fingerprints and explanation (fixture: modify a committed file, attempt undo).
- [ ] Journal rows are read-only in the UI (no edit affordances; append-only respected).
**Size:** M

## Verification steps

1. `ruff check src tests && pytest` green (all suites).
2. Full browser walkthrough on a compose-up sandbox: drop corpus → dashboard shows proposals → Review: inspect an album group's evidence, view an image preview, fix one destination (try `../` first — watch it refuse), reject one, postpone one, batch-approve the rest → Commit: confirm (note the hash), execute, watch progress, read results → verify files landed via the host filesystem → Quarantine: compare the duplicate pair, restore one → History: undo a single op, then the whole plan → verify the tree restored.
3. Keyboard-only pass through the same flow (no mouse).
4. Load check: seed 5,000 proposals → queue pages snappily; commit of 1,000 ops keeps the UI responsive.

## Exit gate checklist

- [ ] The complete loop — drop → review → edit → approve → commit → verify → quarantine-restore → undo — works entirely in the browser.
- [ ] Commit executes exactly what confirmation displayed: hash equality asserted by test; skips honestly reported.
- [ ] Every user edit is server-revalidated (containment, collision, sanitization); hostile-input tests green.
- [ ] Previews are safe (ID-based, root-validated, cached) and degrade gracefully.
- [ ] No delete affordance exists anywhere; mutation-sweep proves review/edit handlers touch no files.
- [ ] Undo (op and plan) works from the UI with honest refusals.
- [ ] Review flow fully keyboard-operable; status never color-only.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 7's search links into the item-detail/preview components built here — keep the preview card a reusable partial.
- Thumbnail cache and its pruner are reused by Phase 7's browse view.
- The `[CLOUD]` evidence marker becomes a filterable facet if ever requested (not planned).
- Commit-in-a-thread is sufficient for v1; if Phase 8 load tests show starvation, move execution into the worker process via a DB-flagged handoff (seam exists: plans in `approved` status).

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
