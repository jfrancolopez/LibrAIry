# Phase 2 — Classification Engine (Catalog + Heuristics, no AI)

**Status:** IN PROGRESS
**Depends on:** Phase 1 (core safety engine) DONE
**Size:** L (largest port: replaces the 1,676-line `step3_classify.sh` and adapts the `catalog/` Python modules)

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

- **Proposal**: the classification result for one item: category, clean name, destination relpath (or none), confidence, evidence list, optional group membership. A proposal is a *suggestion*; it moves nothing.
- **Evidence**: a recorded reason behind a proposal field, with source (`heuristic`, `tags`, `acoustid`, `musicbrainz`, `tmdb`, `library-pattern`, `hashtag`, `ai`) and detail. Users see evidence in the review UI (Phase 6).
- **Confidence**: 0.0–1.0. Deterministic sources (embedded tags, catalog match) score higher than guesses. Below the configured threshold → proposal has NO destination and the item stays pending in the inbox.
- **Category**: one of `music, movies, shows, photos, documents, books, projects, misc` — mapping 1:1 to the library top-level folders.
- **Relationship group**: a set of items sharing a destination context (album, TV season, photo event, project). Groups influence destinations; they never own files (each file keeps exactly one proposal).
- **Destination template**: a per-category pattern (conventional or genre-first) that renders a relative library path from proposal fields.
- **Hashtag hint**: `#tag` suffix on an inbox folder name; extracted as evidence/routing hint, stripped from output names.
- **Golden fixture corpus**: a committed set of synthetic inbox trees plus expected-proposal snapshots used as regression tests.

## Entry criteria

```bash
# 1. Phase 1 exit gate holds: install, lint, tests green
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Core engine CLI exists
librairy scan --help && librairy commit --help && librairy undo --help

# 3. Schema v1 present
python3 - <<'EOF'
from librairy.db import connect_default  # adjust to actual API
# expect user_version >= 1
EOF

# 4. Legacy untouched
git log --oneline -- inbox-processor/ | head -1   # only pre-plan commits
```

If any check fails, STOP and report — do not begin this phase.

## Phase goal

`librairy analyze` turns scanned inbox items into proposals — category, clean name, validated destination, evidence, confidence, grouping — for all eight categories, using only local heuristics, embedded metadata, and free catalog APIs (MusicBrainz/AcoustID/TMDB). No AI (Phase 3 plugs AI in as one more evidence source behind the same contract). Uncertain items get a proposal with evidence but no destination and stay pending.

## In scope

- Proposal contract + DB migration; taxonomy + template registry; port of `heuristics.py`, `music_lookup.py`, `video_lookup.py`, `utils.py`; subprocess adapters (ffprobe/exiftool/fpcalc); hashtag hints; relationship grouping; documents/books/projects/misc heuristics; read-only library indexer replacing `library_index.py`; golden fixture corpus; `librairy analyze` + `librairy propose-plan` CLI.

## Out of scope (tempting, but NO)

- AI providers, prompts, redaction (Phase 3).
- Duplicate detection and quarantine proposals (Phase 4).
- Any web UI (Phases 5–7). Document text extraction (Phase 9).
- Deleting the legacy bash (Phase 4). Do not modify `inbox-processor/scripts/`.
- The legacy `inbox-processor/catalog/*.py` files: port their logic into `src/librairy/`, leave the originals untouched (deleted with the rest in Phase 4).
- Writing to any file inside the library (read-only indexing only).

## Design constraints binding this phase

- **DB migration 002** adds:

```sql
CREATE TABLE proposals (
  id            INTEGER PRIMARY KEY,
  item_id       INTEGER NOT NULL REFERENCES items(id),
  category      TEXT NOT NULL CHECK (category IN
                  ('music','movies','shows','photos','documents','books','projects','misc')),
  clean_name    TEXT NOT NULL,             -- final filename (sanitized, hashtags stripped)
  dest_relpath  TEXT,                      -- relative to library root; NULL = pending/uncertain
  confidence    REAL NOT NULL,
  group_id      INTEGER REFERENCES groups(id),
  status        TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','approved','rejected','postponed','committed','superseded')),
  evidence      TEXT NOT NULL,             -- JSON array [{source, field, detail, weight}]
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  UNIQUE (item_id)                          -- one live proposal per item; re-analysis supersedes
);
CREATE TABLE groups (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL CHECK (kind IN ('album','season','photo_event','project','archive')),
  label      TEXT NOT NULL,                 -- e.g. "Queen — A Night at the Opera", "Vacation 2026 Italy"
  dest_base  TEXT,                          -- shared destination folder (relative), if resolved
  created_at TEXT NOT NULL
);
```

- **Analysis never mutates the filesystem** (invariant 4): the analyzer reads files, runs subprocess tools, writes only to the DB. A test greps `classify/` and `taxonomy.py` for move/delete primitives.
- **Module layout**: `taxonomy.py` (categories, template registry, rendering); `classify/__init__.py` (pipeline orchestration `analyze_items(...)`); `classify/heuristics.py` (port); `classify/music.py`, `classify/video.py` (ports); `classify/documents.py` (documents/books/projects/misc rules); `classify/grouping.py`; `classify/hashtags.py`; `tools/ffprobe.py`, `tools/exiftool.py`, `tools/fpcalc.py`; `indexer.py` (read-only library index).
- **Classification cascade** (per item, ordered; stop when confident): 1) heuristics (project/screenshot/camera-roll/season/album rules, ported); 2) embedded metadata (ffprobe tags, EXIF); 3) catalog lookups (AcoustID+MusicBrainz for audio, TMDB for video) — only when the category calls for them; 4) library-pattern consistency (existing artist/show placement wins over genre guesses, ported from `_apply_consistency`); 5) fallback: extension-based category with LOW confidence and no destination unless ≥ threshold. AI slots in between 4 and 5 in Phase 3 — design the cascade as an ordered list of evidence sources so that insertion is one line.
- **Templates** (`taxonomy.py`): token-based, per category, two built-in styles. Tokens: `{artist} {album} {year} {genre} {title} {show} {season:02d} {episode:02d} {event} {author} {project} {ext}`. Conventional and genre-first presets for music/movies/shows/books; photos use `Photos/{year}/{event}/`; documents `Documents/{year}/` or `Documents/{topic}/` (keep simple); projects `Projects/{project}/` (project folder moved intact). Rendered output is a *relative* path passed through Phase-1 `paths.validate_dest` — template rendering itself must be incapable of emitting `..` or absolute paths (tokens are sanitized components).
- **Confidence discipline** (ported values may be tuned but must remain honest): embedded complete tags 0.85–0.92; AcoustID score > 0.65 + MusicBrainz 0.85–0.95; TMDB strong title/year match 0.8–0.9; heuristic structural rules 0.78–0.9; extension-only fallback ≤ 0.5. Threshold from `CONFIDENCE_THRESHOLD` (settings), default 0.80: below it, `dest_relpath` stays NULL.
- **Catalog etiquette**: MusicBrainz rate limit from `MB_RATE_LIMIT` (module-level limiter as in the legacy `_mb_get`); descriptive `User-Agent: LibrAIry/<version>`; timeouts on every request; on network failure return no evidence (never crash the cascade, never fabricate). API keys from settings (`ACOUSTID_KEY`, `TMDB_KEY`); missing key = skip that source silently. HTTP via stdlib `urllib` (keep the zero-HTTP-deps property of the legacy catalog code) — tests use recorded JSON fixtures, never the network.
- **Groups**: built before destination rendering. Album = audio files sharing a folder + consistent tags/track numbers; season = SxxEyy pattern under one show; photo_event = image/video sets in one folder (folder name + hashtag becomes the event label); project = folder matched by the project heuristic (`.git`, `package.json`, etc.) — a project folder is proposed as ONE unit (its internal structure is preserved verbatim; children get no individual proposals).
- **Hashtags** (`classify/hashtags.py`): extract ALL `#tag` tokens from the item's folder chain within the inbox; expose as evidence and as template context (e.g. photo event label); strip from every rendered name/path component. Tags are hints, not commands — they bias category/labels, they cannot inject path segments (they pass through component sanitization).
- **Library indexer** (`indexer.py`): read-only walk of `/data/library` upserting `items` rows with `root='library'` (reusing the Phase-1 scanner) + a lightweight pattern map (artist→path, show→path) persisted in the DB (replaces the legacy `library_index.json` TTL cache; delete no legacy files). Never opens library files for writing; fingerprinting of library files is optional/deferred (size+mtime suffice here; Phase 4 hashes what dedup needs).

## Backlog items

### P2-01 Proposal contract + migration 002
**Story:** As the system, every category speaks one language: the proposal.
**Depends on:** Phase 1
**Description:** Migration 002 (schema above), `models.py` additions (`Proposal`, `EvidenceEntry`, `Group`), persistence helpers, and supersede logic (re-analysis of a changed item replaces its proposal, old row marked `superseded` via status or archived copy — keep simple, document choice).
**Acceptance criteria:**
- [x] Migration applies on a v1 DB; fresh DB reaches user_version 2 in one go.
- [x] One live proposal per item enforced; re-analysis supersedes cleanly.
- [x] Evidence round-trips as typed entries (source enum validated).
**Size:** S

### P2-02 Taxonomy + destination template registry
**Story:** As a user, I choose how my library is laid out — conventional or genre-first — per category.
**Depends on:** P2-01
**Description:** `taxonomy.py` with the eight categories, template presets, `render_destination(proposal_fields, style) -> str (relpath)`, style selection read from the `settings` DB table (key `templates.<category>.style`, default `conventional`) falling back to config. Unknown/missing tokens → template unusable for that item → no destination (never a path with a literal `{artist}`).
**Acceptance criteria:**
- [x] Both styles render correctly for music/movies/shows/books; photos/documents/projects/misc render their single style.
- [x] Rendered paths always pass `paths.validate_dest`; property test over hostile token values (slashes, dots, unicode) proves components are sanitized.
- [x] Missing required token → explicit "no destination" result, not a broken path.
- [x] Style change in settings changes the next render (no restart needed).
**Size:** M

### P2-03 Port the heuristics engine
**Story:** As the system, obvious things (code projects, screenshots, camera rolls, season folders, untagged albums, fonts, ebooks collections) are recognized instantly without network or AI.
**Depends on:** P2-01
**Description:** Port `inbox-processor/catalog/heuristics.py` (454 lines) into `classify/heuristics.py`: keep its rules and "return None rather than guess" design; retarget outputs from RAM/ROM paths to categories + template fields; keep per-rule confidences; add unit tests per rule (the legacy code has none).
**Acceptance criteria:**
- [x] Every legacy rule has ≥1 positive and ≥1 negative unit test.
- [x] Outputs are proposals (category+fields+evidence), never raw paths.
- [x] Hidden-file flagging behavior preserved and documented.
- [x] Legacy file untouched.
**Size:** M

### P2-04 Subprocess tool adapters
**Story:** As the system, metadata extraction is robust: tools time out, fail cleanly, and return typed results.
**Depends on:** —
**Description:** `tools/ffprobe.py` (format+streams JSON → typed audio/video metadata incl. tags), `tools/exiftool.py` (batch `-j` mode → typed image metadata incl. GPS, camera, dates — GPS is extracted and stored locally; Phase 3's redaction governs what ever leaves the machine), `tools/fpcalc.py` (Chromaprint fingerprint). Common wrapper: timeout (from settings), missing-binary detection with one warning (not per-file spam), structured errors, and a per-item metadata cache in the DB (JSON in a `item_metadata` table or `items` column — keep simple, document choice) so re-analysis doesn't re-run tools on unchanged files.
**Acceptance criteria:**
- [x] Each adapter parses recorded real-output fixtures correctly.
- [x] Timeout and missing-binary produce typed failures; cascade continues.
- [x] Unchanged file re-analysis hits the cache (call-counter test).
**Size:** M

### P2-05 Music classification (tags → AcoustID → MusicBrainz)
**Story:** As a music hoarder, a messy `01-track.mp3` becomes `Queen/A Night at the Opera/01 - Death on Two Legs.mp3` with real catalog evidence.
**Depends on:** P2-02, P2-04
**Description:** Port `music_lookup.py`: strategy 1 embedded tags; strategy 2 fpcalc→AcoustID (score>0.65)→MusicBrainz recording lookup + genre enrichment; MB rate limiting; evidence entries per source; album grouping fields (artist/album/year/track). Network calls mocked in tests via recorded responses.
**Acceptance criteria:**
- [x] Tagged file classifies from tags alone (no network) with confidence ≥0.85.
- [x] Untagged fixture with recorded AcoustID/MB responses resolves artist/title/genre; evidence lists both sources.
- [x] Missing `ACOUSTID_KEY` skips fingerprinting silently; MB limiter enforces spacing (timed test with mock clock).
- [x] All destinations render through the template registry (both styles tested).
**Size:** M

### P2-06 Video classification (TMDB movies/TV)
**Story:** As a movie/TV hoarder, `The.Matrix.1999.1080p.x264-GRP.mkv` becomes `Movies/The Matrix (1999)/…` and `show.s02e05.mkv` lands in the right season folder.
**Depends on:** P2-02, P2-04
**Description:** Port `video_lookup.py`: filename parser (title/year/SxxEyy), TMDB search movie/TV, genre mapping (port `utils.py` maps), vote-count-derived confidence, season grouping. Missing `TMDB_KEY` → heuristic-only naming from the parsed filename at reduced confidence.
**Acceptance criteria:**
- [x] Parser handles the fixture set: dotted scene names, year-in-title, SxxEyy variants, junk tokens.
- [x] Recorded TMDB fixtures produce correct movie and episode proposals with genre evidence.
- [x] No key → parsed-name proposal with confidence below catalog-backed levels; evidence says `heuristic`.
- [x] Episodes of one season share a group and a season folder destination.
**Size:** M

### P2-07 Hashtag hints
**Story:** As a user, naming a folder `Vacation 2026 #italy` routes its contents sensibly, and `#italy` never appears in my library paths.
**Depends on:** P2-01
**Description:** `classify/hashtags.py` per Design Constraints; integration into the cascade (hints bias photo-event labels, project names, and the `Misc`→category nudge) and into naming (strip everywhere).
**Acceptance criteria:**
- [x] Extraction from folder chains (nearest folder wins; multiple tags supported).
- [x] Tags appear in evidence, influence event/project labels, and are absent from every rendered path/name (regression test greps rendered output for `#`).
- [x] Hostile tags (`#../x`, `#a/b`) cannot affect path structure (sanitization test).
**Size:** S

### P2-08 Relationship grouping
**Story:** As a user, albums, seasons, photo events, and projects arrive as coherent sets, not scattered files.
**Depends on:** P2-05, P2-06
**Description:** `classify/grouping.py` building `groups` rows per Design Constraints; group-aware destination rendering (members share `dest_base`); the single-owner rule (a project folder is one proposal; members of other groups keep individual proposals sharing a base folder). This kills legacy defect #2 (file and parent folder both classified) by design: candidates are files, plus intact project folders — never both.
**Acceptance criteria:**
- [ ] Album fixture: N tracks → one group, one shared folder, per-track clean names.
- [ ] Season fixture: episodes share the season folder.
- [ ] Photo-event fixture: mixed photos+videos in `Trip #italy` group under one event label.
- [ ] Project fixture: folder proposed intact; no child proposals exist (test asserts).
- [ ] No item ever appears in two proposals (DB constraint + test).
**Size:** M

### P2-09 Documents, books, projects, misc rules
**Story:** As a user, PDFs, ebooks, code, and everything else get sensible homes too — or honestly stay pending.
**Depends on:** P2-02
**Description:** `classify/documents.py`: extension+name rules for documents (office/pdf/text), books (epub/mobi/azw + pdf-with-booklike-name), projects (delegates to the ported project heuristic), misc fallback. Clean-name normalization (strip release junk, normalize separators — port `sanitize_name` ideas from `utils.py`). Below-threshold results carry evidence but no destination.
**Acceptance criteria:**
- [x] Fixture set covering each rule classifies correctly.
- [x] Ambiguous fixture (e.g. bare `scan001.pdf`) yields a pending proposal: evidence present, `dest_relpath` NULL.
- [x] Clean-name function has its own unit tests (unicode, dots, release tags).
**Size:** M

### P2-10 Read-only library indexer + consistency source
**Story:** As a returning user, new files follow the conventions my library already has — same artist, same folder.
**Depends on:** P2-01
**Description:** `indexer.py` per Design Constraints (walk library into `items`, build artist/show/movie pattern map in DB) and a cascade evidence source using it (port `_apply_consistency`: existing placement overrides genre guesses, +confidence bump, evidence `library-pattern`). Delete nothing legacy; the dead `register_*` code simply never gets ported. Incremental: consistency map updates when the indexer re-runs and when commits land (executor already updates `items`; the pattern map derives from DB, not a TTL JSON cache).
**Acceptance criteria:**
- [ ] Indexing a fixture library creates `root='library'` items and the pattern map; zero writes inside the library tree (mtime snapshot test).
- [ ] New track by an already-indexed artist lands in that artist's existing path regardless of genre guess; evidence records the override.
- [ ] After a commit, the pattern map reflects the new placement without a full rescan.
**Size:** M

### P2-11 Golden fixture corpus + analyze/propose CLI
**Story:** As the project, classification quality is pinned by snapshots, and the whole flow runs headless.
**Depends on:** P2-03..P2-10
**Description:** Committed corpus under `tests/fixtures/corpus/` (tiny synthetic files; media headers faked or minimal real samples): tagged album, untagged album, compilation, movie, episode+season, photo event with `#tag`, camera roll, screenshots, PDFs (clear + ambiguous), epub, code project, fonts, generic unknowns, duplicate basenames, unicode names, hashtag folders. Expected-proposal snapshots checked by test. CLI: `librairy analyze` (scan+classify pending items → proposals; `--json`), `librairy proposals list/show`, `librairy propose-plan [--min-confidence X] [--ids ...]` compiling approved-able proposals into a Phase-1 plan spec (draft plan) — the bridge to `plan approve`/`commit`.
**Acceptance criteria:**
- [ ] Corpus snapshot test passes and is readable enough to review diffs.
- [ ] `librairy analyze` on the corpus inbox produces the snapshot proposals; runs with network mocked (no real API calls in tests).
- [ ] `propose-plan` → `plan approve` → `commit` moves confident corpus items into a correct fixture library tree (end-to-end test).
- [ ] Pending (no-destination) items remain physically untouched in the inbox.
**Size:** M

## Verification steps

1. `ruff check src tests && pytest` green (includes Phase 1 suite — no regressions).
2. Sandbox run: copy the corpus into a scratch inbox, `librairy scan && librairy analyze && librairy proposals list` — inspect categories, names, evidence, confidences.
3. `librairy propose-plan --min-confidence 0.8`, `plan approve`, `commit` — verify the resulting library tree matches expectations (album folder, movie folder, event folder, project intact).
4. Verify the ambiguous PDF and generic unknowns are still sitting untouched in the inbox with pending proposals.
5. Re-run `librairy analyze` — unchanged items are not re-processed (cache hit counters/log).
6. Confirm zero mutations during analysis: snapshot inbox tree before/after `analyze` (identical).

## Exit gate checklist

- [ ] Golden corpus yields correct proposals across all eight categories (snapshot test green).
- [ ] Every proposed destination passes Phase-1 containment; template property tests green.
- [ ] Uncertain items produce evidence-bearing proposals with NO destination and remain physically untouched.
- [ ] Catalog APIs fully mocked in tests; MB rate limiting enforced; missing keys degrade silently.
- [ ] Grouping enforces single ownership (no file in two proposals; project folders atomic).
- [ ] Hashtags influence classification and never appear in output paths.
- [ ] Analysis provably mutates nothing (tree-snapshot test + grep-test for move primitives in `classify/`).
- [ ] End-to-end analyze→propose-plan→approve→commit works headless on the corpus.
- [ ] Legacy `inbox-processor/` untouched.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- The cascade is an ordered evidence-source list; Phase 3 inserts the AI source between library-pattern and fallback.
- Proposal `status` values `approved/rejected/postponed` are set by Phase 6's UI (and `propose-plan --ids` meanwhile).
- Phase 4's worker calls the same `analyze_items` entrypoint on a schedule; keep it callable with a bounded item batch.
- Phase 7 builds FTS over `items` + `proposals` + metadata; keep metadata cached in queryable JSON.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
