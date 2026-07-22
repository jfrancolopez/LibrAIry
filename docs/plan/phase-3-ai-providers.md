# Phase 3 — AI Provider Layer + Privacy Redaction

**Status:** DONE
**Depends on:** Phase 2 (classification engine) DONE
**Size:** M (sharply bounded: providers plug into the existing cascade)

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

- **Provider**: one configured AI endpoint: kind (`ollama`, `openai`, `anthropic`, `gemini`), name, endpoint URL (Ollama), model, enabled flag.
- **Provider chain**: the ordered list of enabled providers tried for one classification query (from `AI_PROVIDER_ORDER` + settings), stopping at the first confident, valid answer.
- **Redacted item view**: the ONLY data structure the prompt builder can see — constructed from an allowlist of safe fields. Fields that never enter it: absolute paths, GPS coordinates, city/country, precise timestamps beyond year, camera serial data, user/host names.
- **AI evidence**: a proposal evidence entry with source `ai`, recording provider, model, and whether the provider was local or cloud.
- **Structured response contract**: the strict JSON schema an AI answer must satisfy (category, name fields, group hint, confidence, rationale) — anything else is discarded.

## Entry criteria

```bash
# 1. Phase 2 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Classification works headless without AI
librairy analyze --help && librairy proposals --help

# 3. The cascade is an ordered evidence-source list with an insertion point
grep -rn "library-pattern\|library_pattern" src/librairy/classify/ | head -3
```

If any check fails, STOP and report.

## Phase goal

Add AI as one more evidence source in the classification cascade — behind the same proposal contract — with: multiple named Ollama endpoints (LAN-first), individually opt-in cloud providers, one structural redaction choke point that makes leaking paths/GPS impossible, honest config handling (`CONFIDENCE_THRESHOLD`, `AI_TIMEOUT`, retries actually honored), and graceful heuristics-only degradation when no provider is reachable. AI never authors paths: it suggests category/fields; templates render destinations; containment validates them (defense in depth).

## In scope

- Provider abstraction + Ollama (multi-endpoint) + OpenAI/Anthropic/Gemini adapters.
- Redaction layer + prompt builder + strict response validation.
- Cascade integration, orchestration (order, timeout, retry, thresholds), degradation.
- Provider status persistence (reachability, latency, last use) for the Phase-7 selector.
- `librairy ai status|test [provider]` CLI.

## Out of scope (tempting, but NO)

- The web provider-selector UI (Phase 7 — this phase only persists the status data it will read).
- Duplicate detection, worker daemon (Phase 4). Web anything (Phase 5+).
- Sending file *content* to any AI (names/metadata fields only — content never leaves, not even to local models, in v1).
- Vision/multimodal classification (post-1.0 idea at best; not planned).
- New Python SDK dependencies for cloud providers: use plain HTTP via stdlib `urllib` like the catalog code (keeps the image slim; the APIs are simple JSON POSTs).

## Design constraints binding this phase

- **Module layout**: `ai/base.py` (Provider protocol: `name`, `kind`, `is_local`, `health() -> HealthResult`, `classify(view: RedactedItemView, timeout) -> AIAnswer | None`), `ai/ollama.py`, `ai/openai.py`, `ai/anthropic.py`, `ai/gemini.py`, `ai/redact.py`, `ai/orchestrator.py`.
- **Redaction is structural, not a filter**: `redact.py` defines `RedactedItemView` (Pydantic model) with an explicit allowlist: relative-to-inbox display path (e.g. `Vacation 2026 #italy/IMG_001.jpg` — never `/data/inbox/...` or host paths), file name, extension, size bucket, media kind, duration, resolution, codec, embedded title/artist/album/genre tags, track number, year (from tags/EXIF, year only), sibling file names (bounded, same redaction), folder chain (relative), hashtag hints, and prior deterministic evidence summaries. There is NO constructor path that accepts a full metadata dict: the builder copies field-by-field. GPS/city/country/camera-serial fields do not exist on the model — leaking them is a type error, not a policy hope.
- **Config truthfulness**: `AI_TIMEOUT`, `MAX_AI_RETRIES` (per provider, on transport errors only — not on low confidence), `CONFIDENCE_THRESHOLD`, `AI_PROVIDER_ORDER`, `USE_MULTI_AI` are read from settings and demonstrably change behavior (tests). This closes legacy defect #13 for the AI path.
- **Providers config**: Ollama endpoints are a named list in the settings DB (`ai.ollama.endpoints = [{name, url, model}]`), seeded on first run from `OLLAMA_HOST`/`OLLAMA_MODEL_PRIMARY`(alias `OLLAMA_MODEL`)/`OLLAMA_MODEL_SECONDARY`. Cloud providers are disabled unless BOTH an API key exists AND the per-provider enable flag (`ai.<provider>.enabled`) is true — a key alone must not activate cloud calls (opt-in means opt-in).
- **Response contract**: providers must return JSON conforming to a strict Pydantic schema: `{category: <enum of 8>, name_fields: {artist? album? title? show? season? episode? year? event? project? author?}, group_hint?: str, confidence: float 0..1, rationale: str}`. Extraction tolerates code fences; validation is unforgiving: wrong enum, extra path-like strings in name fields containing `/` or `\\`, or missing keys → answer discarded, next provider tried. **The AI never returns a path and no AI string is ever concatenated into a path without passing component sanitization.**
- **Cascade placement**: after library-pattern, before the extension fallback. Only items still below `CONFIDENCE_THRESHOLD` reach AI. AI evidence merges like any other source; an AI-only proposal is capped (e.g. ≤0.85) so catalog-backed evidence always outranks it (document exact cap in code).
- **Degradation**: all providers unreachable/disabled → cascade completes with deterministic sources only; a single WARNING per analysis batch (not per item); items stay pending as usual. Nothing blocks, nothing crashes, no retry storms (per-provider circuit-break for the rest of the batch after N consecutive transport failures).
- **Status persistence**: migration 003 adds `provider_status` (`name, kind, endpoint, model, enabled, last_ok_at, last_error, latency_ms, last_used_at`), updated by health checks and real calls. Health check for Ollama: `GET /api/tags` (also yields the model list for Phase 7's selector); cloud: cheap models/list-style endpoint or a documented no-network "configured" state (avoid burning tokens on health checks).
- **Logging hygiene**: prompts/responses logged only at DEBUG, and even then through the redacted view; API keys never logged (SecretStr).

## Backlog items

### P3-01 Provider abstraction + status persistence
**Story:** As the system, AI backends are interchangeable plugins with observable health.
**Depends on:** Phase 2
**Description:** `ai/base.py` protocol + `AIAnswer`/`HealthResult` models; migration 003 (`provider_status`); settings seeding from env on first run; registry that builds the enabled provider list from settings + `AI_PROVIDER_ORDER`.
**Acceptance criteria:**
- [x] Registry yields the configured chain; disabled/keyless providers excluded.
- [x] Cloud provider with key but `enabled=false` is NOT in the chain (opt-in test).
- [x] Status rows persist health/latency/last-use updates.
**Size:** S

### P3-02 Ollama provider (multi-endpoint, qwen3 defaults)
**Story:** As a privacy-focused user, my LAN Ollama box does the AI work, and I can have more than one.
**Depends on:** P3-01
**Description:** `ai/ollama.py`: `POST /api/generate` (or `/api/chat`) with `format: "json"`, per-endpoint model, timeout, retries; health via `GET /api/tags` including available-model capture; multi-endpoint iteration in configured order. Documentation strings recommend `qwen3:4b` (CPU) / `qwen3:8b` (GPU); seed default model = `qwen3:4b` when env gives none.
**Acceptance criteria:**
- [x] Mocked-server tests: success, malformed JSON, timeout, connection refused → typed results; retries only on transport errors.
- [x] Two configured endpoints: first down → second used; status rows reflect both.
- [x] Model list captured on health check and persisted for the selector.
**Size:** M

### P3-03 Cloud providers (OpenAI, Anthropic, Gemini) — opt-in
**Story:** As a user who explicitly opts in, cloud models can rescue hard cases — under the same redaction and contract.
**Depends on:** P3-01
**Description:** Three thin adapters using stdlib HTTP: OpenAI chat-completions (JSON mode), Anthropic messages, Gemini generateContent. Keys via SecretStr settings; models from settings with sane defaults; identical response-contract validation; per-provider enable flags enforced in the registry (double-checked in the adapter).
**Acceptance criteria:**
- [x] Mocked-endpoint tests per provider: request shape correct, response parsed, contract enforced.
- [x] No adapter can be invoked when its enable flag is false (test).
- [x] Keys never appear in logs or exceptions (test captures logging).
**Size:** M

### P3-04 Redaction layer
**Story:** As a privacy-focused user, nothing I would not shout across the internet can even reach the prompt builder.
**Depends on:** —
**Description:** `ai/redact.py`: `RedactedItemView` + `build_view(item, metadata, evidence) -> RedactedItemView` per Design Constraints; the ONLY prompt-input type accepted by `base.Provider.classify`.
**Acceptance criteria:**
- [x] Hostile-metadata test: item with GPS, city/country, camera serial, absolute paths in every field → serialized view contains none of them (string-scan assertions for coordinates, `/data/`, city names from fixture).
- [x] View has no field capable of carrying GPS/location (model-schema test).
- [x] Display paths are inbox-relative in every case, including nested and unicode paths.
- [x] Sibling list is bounded (e.g. ≤20 names) so prompts cannot balloon.
**Size:** M

### P3-05 Prompt builder + response validation
**Story:** As the system, AI answers are structured, validated, and template-safe — or discarded.
**Depends on:** P3-04
**Description:** One prompt template (system+user) describing the 8 categories and the JSON contract, taking only a `RedactedItemView`; response extraction (fence-tolerant) + strict Pydantic validation + name-field sanitization (reject path separators, length caps).
**Acceptance criteria:**
- [x] Valid mocked answers become `AIAnswer`s; each malformation class (bad enum, missing key, path in a name field, confidence out of range) is discarded with a typed reason.
- [x] Prompt snapshot test: rendering the prompt for the hostile fixture contains no redacted material.
- [x] Property test: no validated `AIAnswer` field can render a template output that fails containment.
**Size:** M

### P3-06 Orchestrator + cascade integration
**Story:** As a user, AI quietly fills the gaps deterministic sources leave — and my threshold/timeout settings actually work.
**Depends on:** P3-02, P3-05
**Description:** `ai/orchestrator.py`: chain iteration, per-provider timeout/retries, circuit-breaking, confidence cap for AI-only results, merge into the Phase-2 cascade at the defined insertion point; batch-level degradation warning.
**Acceptance criteria:**
- [x] Corpus item unresolved by Phase-2 sources gets a correct proposal from a mocked local provider; evidence records provider+model+local/cloud.
- [x] `CONFIDENCE_THRESHOLD` and `AI_TIMEOUT` changes demonstrably alter behavior (tests with different settings).
- [x] All providers down → analysis completes, one warning, deterministic results unchanged (diff-test vs AI-disabled run).
- [x] AI-influenced destinations pass containment (reuse the Phase-2 property test over AI-shaped inputs).
- [x] Circuit breaker: after N transport failures a provider is skipped for the batch (call-count test).
**Size:** M

### P3-07 AI CLI
**Story:** As a user/agent, I can see and test my AI setup from the terminal.
**Depends on:** P3-06
**Description:** `librairy ai status` (table: provider, kind, endpoint/model, enabled, last ok, latency), `librairy ai test [name]` (live health check + tiny classification round-trip against a synthetic redacted view), `--json` variants.
**Acceptance criteria:**
- [x] Status reads persisted rows; test updates them.
- [x] `ai test` against a mocked endpoint round-trips; against a down endpoint reports failure cleanly (exit code 1).
**Size:** S

## Verification steps

1. `ruff check src tests && pytest` green (Phases 1–2 suites still green).
2. With a real or mocked Ollama on the LAN: `librairy ai status`, `librairy ai test` — verify health, latency, model list.
3. Sandbox: add corpus items that deterministic sources cannot resolve; run `librairy analyze` with AI enabled (mock or real local model) — verify AI-sourced proposals appear with capped confidence and provider evidence.
4. Set every provider disabled → re-run analyze → identical deterministic results, single warning.
5. Grep the DEBUG logs from step 3 for `/data/`, GPS coordinates, city names from fixtures → zero hits.
6. Toggle `ai.openai.enabled=false` with a key present → confirm via logs/mocks that no OpenAI call is attempted.

## Exit gate checklist

- [x] System fully functional with AI disabled (diff-test proves deterministic parity).
- [x] Redaction proven: hostile-metadata serialization scan + schema-level impossibility of GPS/paths + prompt snapshot.
- [x] Cloud is strictly opt-in: key alone never activates a provider.
- [x] AI-influenced destinations pass containment; AI never emits paths.
- [x] `CONFIDENCE_THRESHOLD`, `AI_TIMEOUT`, `MAX_AI_RETRIES`, `AI_PROVIDER_ORDER` all demonstrably honored.
- [x] Multi-endpoint Ollama with failover works; provider status persisted for Phase 7.
- [x] All mocked-provider integration tests green; no new HTTP SDK dependencies.
- [x] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 7's provider selector is a thin UI over `provider_status` + the settings keys defined here (`ai.ollama.endpoints`, `ai.<provider>.enabled`, active order) — no new backend concepts should be needed.
- Phase 4's worker calls the same orchestrated cascade; circuit-breaker state should be per-batch, not global, so a recovered endpoint is retried on the next cycle.
- The legacy `/data/reports` mount and bash pipeline are still present; Phase 4 removes both.

## Open questions log

2026-07-21: P3-02 needs model-list persistence, but P3-01's provider_status schema was already committed as migration 003 without a model-list column. Safest default: keep history append-only and add migration 004 with `available_models` rather than amending/reusing migration 003.
