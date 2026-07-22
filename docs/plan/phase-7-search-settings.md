# Phase 7 — Search/Browse, Settings, AI Provider Selector

**Status:** IN PROGRESS
**Depends on:** Phase 6 (review/commit UI) DONE
**Size:** M

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

- **FTS index**: SQLite FTS5 virtual table indexing names, clean names, tags/hashtags, artists/albums/titles/shows/genres/event labels, and selected metadata strings — for items in the library, inbox, and quarantine. Media *content* is never indexed; document *text* content arrives in Phase 9 as a separate table.
- **Facet filters**: structured narrowing alongside text search: category, root (library/inbox/quarantine), year, genre, group kind, has-preview.
- **Browse**: navigating the library index by category → subfolders → item detail. Read-only over the index, with previews. NOT a file manager: no move/rename/delete from browse.
- **Settings screen**: DB-backed runtime settings (templates, thresholds, dedup toggles, AI providers) — distinct from boot-time env (paths, port). Precedence: **settings DB > environment > built-in default**, uniformly.
- **Provider selector**: the quick control (header widget + settings section) to switch the active AI endpoint/provider, test connections, and reorder the chain — effective on the next analysis batch without restart.
- **Access pointers page**: a static help screen explaining how to reach the library over the user's own SMB/FTP/WebDAV (UNRAID-flavored instructions + generic guidance). Documentation only — LibrAIry implements none of these protocols.

## Entry criteria

```bash
# 1. Phase 6 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Portal serves review/commit/quarantine/history behind auth
# (run the Phase 6 web suite specifically if labeled)
pytest -k "web" -q

# 3. Provider status data exists for the selector
librairy ai status --json | python3 -c "import json,sys; json.load(sys.stdin); print('OK')"
```

If any check fails, STOP and report.

## Phase goal

Make the library *findable* and the system *tunable*: instant search with facets across everything indexed, category browsing with previews, a settings screen for templates/thresholds/dedup/AI, and the quick AI-provider selector. This completes the v1 feature surface; Phase 8 only hardens and packages.

## In scope

- Migration 005: FTS5 tables + sync triggers/functions; index backfill and incremental maintenance; rebuild command.
- Search screen (query + facets + pagination + result actions: open detail, show-in-history, copy path).
- Browse screen (category tree → items → detail with preview partial from Phase 6).
- Settings screen (templates per category, confidence threshold, worker pacing, dedup toggles, masked API-key status).
- Provider selector (header widget + settings section).
- Access pointers page.

## Out of scope (tempting, but NO)

- Document text-content extraction/search (Phase 9 — the "PDF containing the word coding" case lands there).
- Search-driven bulk operations on library files (rename/move/delete from search results) — browse/search stay read-only (invariants 1, 3).
- Editing env-only settings (paths, port) from the web — boot-time config stays in `.env` (a web UI writing its own mount paths is a footgun).
- Entering/changing API keys via the web UI in v1: keys live in env; the UI shows presence/absence only (`set`/`not set`), never values. (Avoids key-handling in session-cookie territory; revisit post-1.0.)
- Any new search backend. FTS5 only.

## Design constraints binding this phase

- **Migration 005**: `CREATE VIRTUAL TABLE search_fts USING fts5(name, clean_name, tags, artist, album, title, show, genre, event, category UNINDEXED, root UNINDEXED, item_id UNINDEXED, tokenize='unicode61 remove_diacritics 2')` — fed from `items` + `proposals` + cached metadata. Sync strategy: explicit sync functions called at the natural write points (indexer upsert, proposal save, commit/state change) rather than a web of SQL triggers — simpler to test; a full `rebuild_search_index()` regenerates from scratch (exposed as `librairy index rebuild` + health-screen button).
- **Query semantics**: user query → FTS5 `MATCH` with prefix support (`term*`); facets compiled to indexed WHERE clauses; results ranked `bm25()`; snippet/highlight via `highlight()`; paginated 50/page. Malformed FTS syntax from users must not 500: sanitize/escape into a phrase query on parse failure.
- **Performance**: target on the seeded 10k-item sandbox: cold search < 200 ms server-side, warm < 50 ms (assert in a perf-marked test with generous CI margin, e.g. < 500 ms). No `LIKE '%…%'` scans anywhere in search paths.
- **Settings write path**: one `settings_service` module validates and persists DB settings (Pydantic models per section), used by web routes AND read by config accessors (the Phase-2/3/4 code already reads merged settings — verify the precedence rule holds uniformly and add the missing accessors). Every change journaled to `history` as a `settings_change` action (old→new, no secrets).
- **Provider selector**: header widget shows active chain summary (`AI: lan-beast (qwen3:8b) [OK]`); click → panel: reorder chain (drag or up/down buttons — keep it button-simple), enable/disable providers, pick per-endpoint model from the captured model list, add/remove named Ollama endpoints (name + URL + model), test button per row (live health check via Phase-3 code). All writes via `settings_service`; worker reads settings at each batch start → changes apply next batch, no restart (add a test proving a mid-run settings flip changes the next batch's chain). Cloud rows carry an explicit `[CLOUD — data leaves this machine]` caption; enabling one requires a confirm step naming what redaction still withholds.
- **Browse**: category cards (8) with counts → drill into the *index's* folder structure (derived from relpaths; no live `os.walk` per request) → item detail: preview partial (Phase 6), metadata card, evidence/provenance, history links, group siblings. "Copy path" buttons render the host-visible path hint using the configured `HOST_*_DIR` values so the user can find files over SMB/FTP.
- **Access pointers page**: static template, UNRAID-first: enabling SMB shares for the library path, typical `\\TOWER\library` / `smb://` URLs, FTP/WebDAV pointers to standard UNRAID plugins/apps, plus a generic non-UNRAID note. Explicitly states LibrAIry serves none of these itself.

## Backlog items

### P7-01 FTS index + sync + rebuild
**Story:** As a user with years of files, anything LibrAIry knows about is findable in milliseconds.
**Depends on:** Phase 6
**Description:** Migration 005 per Design Constraints; backfill on migration; sync calls at indexer/proposal/commit write points; `librairy index rebuild` + health-screen rebuild button; malformed-query hardening.
**Acceptance criteria:**
- [x] Backfill indexes the seeded sandbox (library+inbox+quarantine items with metadata fields populated).
- [x] New proposal, commit, and quarantine each update the FTS rows (three targeted tests).
- [x] `index rebuild` from an empty FTS table reproduces identical search results (checksum/count comparison).
- [x] Hostile queries (`"`, `AND OR`, `*`, unbalanced parens, emoji) return results or empty — never 500 (parametrized test).
- [x] Perf test: seeded 10k items, search < 500 ms in CI.
**Size:** M

### P7-02 Search screen
**Story:** As a user, I type `queen night opera` — or filter photos to 2026 — and get the right rows with paths I can act on.
**Depends on:** P7-01
**Description:** Search route/template: query box (HTMX live search, 300 ms debounce), facet sidebar (category/root/year/genre/group-kind), highlighted snippets, result rows (icon, name, path, category, confidence origin), actions per row: detail view, show-in-history, copy host path. Empty/first-visit state teaches query syntax by example.
**Acceptance criteria:**
- [x] Text + each facet + combinations return correct fixtures (parametrized).
- [x] Highlighting marks matched terms; pagination works via HTMX.
- [x] Copy-path renders the HOST-mapped path (env-derived), not the container path (test).
- [x] Row actions link correctly into detail/history; keyboard operable.
**Size:** M

### P7-03 Browse screen + item detail
**Story:** As a user, I wander my library by category like flipping through a well-labeled archive — previews included, nothing touchable.
**Depends on:** P7-01
**Description:** Per Design Constraints: category cards with counts, index-derived folder drill-down, item detail composing the Phase-6 preview partial + metadata + evidence + group siblings + history links.
**Acceptance criteria:**
- [x] All 8 categories navigable on the sandbox; counts correct; folder listing paginated.
- [x] Detail shows preview, metadata, evidence with source markers, siblings.
- [x] Zero mutating affordances (template grep test: no forms/buttons that write, beyond navigation).
- [x] No per-request filesystem walking (call test on a browse request — DB only, except preview streaming).
**Size:** M

### P7-04 Settings screen
**Story:** As a user, the knobs that matter — layout style, strictness, dedup tools, worker pace — are sliders and toggles, not env archaeology.
**Depends on:** —
**Description:** Settings route/templates + `settings_service` per Design Constraints: per-category template style (conventional/genre-first with live example path preview), `CONFIDENCE_THRESHOLD` slider, worker pacing bounds, dedup tool toggles (fingerprint/rmlint/czkawka with the ≥1-exact-method rule), API-key presence indicators (masked, read-only), change journaling.
**Acceptance criteria:**
- [ ] Template style change immediately alters the example preview and the next analysis batch's rendered destinations (integration test).
- [ ] Threshold change alters which proposals get destinations next batch (test).
- [ ] Disabling both exact dedup methods is rejected inline.
- [ ] Key fields show `set`/`not set` only; page source contains no key material (test greps rendered HTML against fixture keys).
- [ ] Every change lands in history as `settings_change` without secret values.
**Size:** M

### P7-05 AI provider quick-selector
**Story:** As a user with a Mac, a gaming PC, and a NAS, I flip LibrAIry between their Ollama servers — or a cloud fallback — from the header, and it just takes effect.
**Depends on:** P7-04
**Description:** Per Design Constraints: header widget + settings panel (chain reorder, enable/disable, endpoint CRUD, model pick from captured lists, per-row test button, cloud confirm step with redaction note).
**Acceptance criteria:**
- [ ] Adding a named endpoint + test button round-trips against a mock Ollama; status row updates live.
- [ ] Chain reorder/disable changes the next analysis batch's provider order without restart (mid-run flip test from Design Constraints).
- [ ] Enabling a cloud provider requires the confirm step; skipping it (raw POST) → 422 (server-enforced, not just UI).
- [ ] Header widget reflects active provider and health; degrades to `AI: heuristics-only` when chain is empty/down.
- [ ] Removing an endpoint mid-batch doesn't crash the worker (it finishes the batch on its snapshot; test).
**Size:** M

### P7-06 Access pointers page
**Story:** As a user, LibrAIry tells me how to open my organized files from my own machines — without pretending to be a file server.
**Depends on:** —
**Description:** Static help template per Design Constraints (UNRAID SMB walkthrough, FTP/WebDAV pointers, generic note), linked from browse/search "open on your computer" hints, using configured host paths in examples.
**Acceptance criteria:**
- [ ] Page renders with the user's actual `HOST_LIBRARY_DIR` substituted into examples.
- [ ] States explicitly that LibrAIry does not serve these protocols.
- [ ] Linked from item detail and browse.
**Size:** S

## Verification steps

1. `ruff check src tests && pytest` green (all suites, including perf-marked).
2. Sandbox walkthrough: search `queen` → find the committed album; facet to Photos/2026 → find the vacation event; hostile query strings → graceful results.
3. Browse Music → artist folder → track detail: preview, evidence, siblings; copy-path yields the host path.
4. Settings: flip Music to genre-first → run a new analysis batch → confirm new destinations use genre-first; flip back.
5. Provider selector: point at a real/mock LAN Ollama, test, reorder, disable → dashboard header reflects it; batch uses the new chain without restart.
6. `librairy index rebuild` → search results identical before/after.
7. Access pointers page shows your real host paths and reads sensibly for an UNRAID user.

## Exit gate checklist

- [ ] Seeded 10k-item search meets the perf target in CI; no unbounded scans.
- [ ] FTS stays in sync through analyze/commit/quarantine, and full rebuild reproduces it.
- [ ] Browse + search are provably read-only over user files.
- [ ] Settings changes (templates, threshold, dedup, pacing) take effect next batch, are validated, and are journaled without secrets.
- [ ] Provider switch is effective without restart; cloud enablement is a server-enforced explicit confirm; key material never reaches HTML.
- [ ] Access pointers page ships with host-path substitution.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 9's document-text search adds a second FTS table (`content_fts`) and a `content` facet to this search screen — the facet sidebar and result renderer were built to take one more source.
- Phase 8's 50k-file smoke test reuses the perf-marked tests at larger scale.
- If key entry via web is ever wanted, it needs its own security review — deliberately excluded from v1 (recorded here so it isn't "discovered" as an oversight).

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
