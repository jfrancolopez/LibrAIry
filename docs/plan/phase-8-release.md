# Phase 8 — Release Hardening, UNRAID Packaging, v1.0

**Status:** IN PROGRESS
**Depends on:** Phase 7 (search/settings) DONE
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

- **Multi-arch image**: one published Docker image manifest covering `linux/amd64` and `linux/arm64` (Intel/AMD NAS boxes and ARM boxes/Apple-silicon Docker hosts), built with `docker buildx`.
- **UNRAID Community Apps template**: the XML file UNRAID's Apps tab consumes — defines the container, its port, path mappings, env vars, icon, and descriptions so installation is form-filling, not compose-editing.
- **Rootless / PUID-PGID**: the container runs its processes as a non-root user; `PUID`/`PGID` env vars (the NAS-community convention) map that user to the host owner of the data shares so created files belong to the user, not root.
- **Smoke test (50k)**: a scripted load run — 50,000 generated files through scan→analyze(no-AI)→propose→commit — asserting completion, bounded memory, and responsive UI during the run.
- **RAM/ROM migration note**: guidance for pre-v1 users whose library uses the legacy `RAM/`/`ROM/` zones. LibrAIry never restructures an existing library (invariant 3), so migration is the user's manual choice.

## Entry criteria

```bash
# 1. Phase 7 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Full product surface exists behind auth
# dashboard/health (P5), review/commit/quarantine/history (P6), search/browse/settings/providers (P7)
pytest -k "web" -q

# 3. Container runs the supervisor with czkawka present
docker build -t librairy:test . && docker run --rm librairy:test sh -c "command -v czkawka_cli && librairy --help"
```

If any check fails, STOP and report.

## Phase goal

Turn a feature-complete system into something a stranger installs on their UNRAID box from documentation alone and trusts with their files: hardened container (non-root, pinned, healthchecked, multi-arch), friendly boot-time validation, UNRAID template, polished first-run, rewritten docs, a 50k-file performance pass, and a tagged v1.0.

## In scope

- Final Dockerfile (multi-stage, non-root + PUID/PGID, pinned versions, `.dockerignore`, amd64+arm64) and published versioned images.
- Boot-time env validation with plain-language errors; final single-service compose.
- UNRAID Community Apps template + install guide; generic Docker/desktop install guide.
- First-run UX polish (empty states, guided "drop files" hints, setup screen copy).
- Documentation rewrite (README, Instructions → install/configure/use/troubleshoot/security/FAQ), RAM/ROM migration note.
- Performance pass + 50k smoke test; structured logs with rotation; DB backup/restore documentation.
- v1.0 tag, changelog, GitHub release with image publishing workflow.

## Out of scope (tempting, but NO)

- New features of any kind (search depth, backup, enrichment — Phase 9 or later).
- TLS termination inside the container (document reverse-proxy patterns instead).
- Synology/QNAP-specific packaging (generic Docker docs cover them; dedicated templates post-1.0 if demand exists).
- Docker Hub vs GHCR debates — pick GHCR (free, integrated with the repo) and move on; a Docker Hub mirror is post-1.0.
- Auto-update mechanisms (users update containers with their NAS tooling).

## Design constraints binding this phase

- **Dockerfile**: multi-stage — builder stage (pip wheel build; czkawka_cli fetched as the official release binary per arch, checksum-verified) and slim runtime stage (`python:3.12-slim-bookworm` base; apt: ffmpeg, libimage-exiftool-perl, chromaprint-utils/fpcalc, rmlint; COPY czkawka from builder). All apt packages and pip dependencies version-pinned. Non-root `librairy` user; entrypoint script applies `PUID`/`PGID` (default 99/100 — UNRAID's `nobody:users`) via usermod/gosu-style drop before exec'ing the supervisor. `HEALTHCHECK CMD curl -fsS localhost:${DASHBOARD_PORT:-8080}/healthz`. `.dockerignore` excludes tests, docs, fixtures, git.
- **Boot validation**: before the supervisor starts children, run config validation (`validate_or_die` from P1-02) PLUS runtime checks: data roots exist and are writable by the effective UID; inbox ≠ library ≠ quarantine (no nesting either); DB openable/migratable; port free. Failures print a numbered, plain-language list ("`/data/library` is not writable by UID 99 — on UNRAID set the share's owner or pass PUID/PGID") and exit non-zero so `docker logs` tells the whole story.
- **UNRAID template**: XML per Community Apps schema: name, repository (GHCR image + tag), WebUI `http://[IP]:[PORT:8080]`, port mapping, four path mappings (inbox/library/quarantine/appdata) with UNRAID share defaults (`/mnt/user/...`), env fields for `OLLAMA_HOST`, `OLLAMA_MODEL_PRIMARY`, `TMDB_KEY`, `ACOUSTID_KEY`, PUID/PGID, an `Overview` description, category `MediaApp:Other Tools:`, support link, and a Pip-Boy-styled icon (PNG in `packaging/unraid/`). Template lives in `packaging/unraid/librairy.xml`; the install guide covers adding it via "Template repositories" until/unless it is submitted to Community Apps (submission itself is post-1.0 — it requires a public feed repo).
- **Docs structure**: `README.md` = product pitch + screenshot + 5-minute quickstart (compose) + links. `docs/install-unraid.md`, `docs/install-docker.md`, `docs/configuration.md` (every env var + every web setting, generated table from the settings model where practical), `docs/using-librairy.md` (inbox→review→commit walkthrough with screenshots), `docs/troubleshooting.md` (health-screen driven), `docs/security.md` (LAN posture, what redaction sends/withholds, reverse-proxy note, no-public-exposure warning), `docs/faq.md` (incl. RAM/ROM migration note verbatim below), `docs/backup-restore.md` (appdata backup = DB + settings; `librairy index rebuild` recovers the index; quarantine layout explanation). `Instructions.md` (legacy) is replaced by a pointer page to the new docs.
- **RAM/ROM migration note** (FAQ + install docs): *Fresh installs need nothing. If your library still uses the legacy `RAM/`/`ROM/` zones: LibrAIry will never restructure an existing library. Option A (recommended): manually move top-level content to the plain categories (`Music/`, `Movies/`, …) before first indexing — a one-time `mv` per folder on the NAS. Option B: leave it; the indexer is structure-agnostic, old paths remain searchable/browsable, and newly committed files land in the plain categories at the library root, so the zones fade into legacy corners over time.*
- **Logging**: structured (timestamp, level, component) to stdout (Docker-native) AND a rotating file in `<appdata>/logs/` (stdlib `RotatingFileHandler`, ~10 MB × 5); DEBUG gated by env; a redaction filter guarantees no secrets/API keys in any log line (test).
- **Performance pass**: generate 50k synthetic files (small; realistic name/type mix) → worker `--once` cycles to full analysis (AI mocked off) → propose→approve→commit 10k of them. Assert: completes; worker RSS bounded (< ~500 MB); dashboard and search respond < 1s during the run; DB size sane; no O(n²) hot spots (profile the scan/analyze loops once and record findings in the doc). Fix what fails; record numbers in `docs/performance.md`.
- **Release workflow**: `.github/workflows/release.yml` — on tag `v*`: run full test suite, buildx amd64+arm64, push `ghcr.io/<owner>/librairy:<version>` + `:latest`, attach changelog. `CHANGELOG.md` started with v1.0.0 (honest summary: what v1 does, what it never does, link to docs). Version single-sourced from `pyproject.toml` and surfaced in the web footer + `librairy --version`.

## Backlog items

### P8-01 Hardened multi-arch Dockerfile + entrypoint
**Story:** As a NAS user, the container runs as my user, on my architecture, with everything bundled — no manual tool builds, no root-owned files.
**Depends on:** Phase 7
**Description:** Per Design Constraints: multi-stage build, czkawka binary per arch (checksum-verified), pinned versions, non-root + PUID/PGID entrypoint, HEALTHCHECK, `.dockerignore`.
**Acceptance criteria:**
- [ ] `docker buildx build --platform linux/amd64,linux/arm64` succeeds; both images pass the CI smoke run (`librairy --help`, `czkawka_cli --version`, ffprobe/exiftool/fpcalc/rmlint present).
- [ ] Files created in mounted volumes are owned by PUID:PGID (test with a scratch mount).
- [ ] Container processes run non-root (`docker top`/test asserts UID ≠ 0).
- [ ] HEALTHCHECK transitions healthy after boot and unhealthy when the web child is killed.
**Size:** M

### P8-02 Boot-time validation + final compose
**Story:** As a first-time user, a misconfigured container tells me exactly what to fix in plain English — in `docker logs` — instead of half-working.
**Depends on:** P8-01
**Description:** Per Design Constraints: startup validation chain (config, writability, path-distinctness/nesting, DB, port) with numbered friendly errors; final `docker-compose.yml` (single service, four mounts, PUID/PGID, healthcheck, restart policy) + a commented `.env.example` refresh.
**Acceptance criteria:**
- [ ] Each failure class (unset path, unwritable dir, inbox inside library, bad port) produces its specific friendly error and non-zero exit (parametrized container tests).
- [ ] Valid config boots to healthy with zero warnings.
- [ ] `docker compose up` from the committed compose + example env works on a clean machine.
**Size:** S

### P8-03 UNRAID template + install guides
**Story:** As an UNRAID user, I add the template, fill four paths and an Ollama address, hit Apply, and open the WebUI.
**Depends on:** P8-01
**Description:** Per Design Constraints: `packaging/unraid/librairy.xml` + icon; `docs/install-unraid.md` (template-repo method, share setup, PUID/PGID explanation, first-run walkthrough); `docs/install-docker.md` (compose + plain `docker run` for desktop/other NAS).
**Acceptance criteria:**
- [ ] Template XML validates against the Community Apps schema conventions (fields render in UNRAID's Add Container form — verified on the user's UNRAID or via schema review).
- [ ] Both guides followed verbatim on a clean target reach a healthy portal (the UNRAID pass is the user's acceptance run; the docker pass is CI-scriptable).
- [ ] Icon renders; WebUI link lands on the portal.
**Size:** M

### P8-04 First-run UX polish
**Story:** As a brand-new user staring at an empty system, the portal itself teaches me the loop: drop files → watch → review → commit.
**Depends on:** —
**Description:** Empty-state passes over every screen (dashboard "inbox clear — drop files into <host inbox path> to begin", review "nothing to review yet", search/browse/quarantine/history equivalents), setup-screen copy (password guidance, what happens next), a dismissible first-visit banner linking the walkthrough doc, favicon + page titles.
**Acceptance criteria:**
- [ ] Every screen renders a purposeful empty state on a fresh install (template sweep test).
- [ ] Dashboard empty state shows the real host inbox path from env.
- [ ] First-visit banner appears once, dismisses persistently (per session store).
**Size:** S

### P8-05 Documentation rewrite
**Story:** As any user, the docs answer install, configure, use, break-fix, and "is my data safe?" without reading code.
**Depends on:** P8-02, P8-03
**Description:** Per Design Constraints: new docs set, README rewrite with screenshots (Pip-Boy dashboard + review screen), configuration reference generated/synced from the settings model, security page (incl. exactly what a cloud prompt contains after redaction), backup/restore, FAQ with the RAM/ROM note, legacy `Instructions.md` → pointer. Remove `setup.sh` if the docs no longer reference it (its wizard role is superseded by env + web settings) — or keep and update it; decide, document, be consistent.
**Acceptance criteria:**
- [ ] Every env var and web setting appears in `docs/configuration.md` (sync test against the settings model).
- [ ] README quickstart verified end-to-end on a clean machine.
- [ ] Security page lists the redaction allowlist verbatim from `ai/redact.py` (sync test).
- [ ] No doc references deleted artifacts (bash steps, RAM/ROM as current, `/data/reports`) except as historical/migration notes (link-and-grep check).
**Size:** M

### P8-06 Performance pass + 50k smoke test
**Story:** As a data hoarder, LibrAIry chews through a 50,000-file dump without choking my NAS or its own UI.
**Depends on:** —
**Description:** Per Design Constraints: generator script, scripted load run (CI-runnable at reduced scale, e.g. 10k in CI + 50k locally/tagged), assertions on completion/memory/latency, profile of hot loops, fixes for regressions found, `docs/performance.md` with measured numbers and the scaling story (SQLite page-cache behavior, WAL, what "millions of files" means operationally).
**Acceptance criteria:**
- [ ] Load run passes at 50k locally and its reduced form runs in CI (marked `slow`).
- [ ] Worker RSS stays under the documented bound; no unbounded in-memory listings (generators/batches everywhere — profile-verified).
- [ ] Dashboard + search latency assertions hold mid-run.
- [ ] Numbers recorded in `docs/performance.md`.
**Size:** M

### P8-07 Structured logging + rotation + secret redaction
**Story:** As a self-hoster, logs tell me what happened, rotate themselves, and never contain my keys.
**Depends on:** —
**Description:** Per Design Constraints: logging config module (stdout + rotating file), component loggers adopted across worker/web/executor, redaction filter, DEBUG gating.
**Acceptance criteria:**
- [ ] Log lines carry timestamp/level/component; rotation proven with a small max-size test config.
- [ ] Redaction filter test: logging records containing fixture API keys/session tokens emit masked output.
- [ ] Executor logs every op result at INFO (journal remains the authority; logs are convenience).
**Size:** S

### P8-08 v1.0 release: workflow, changelog, tag
**Story:** As the project, v1.0 is a reproducible, published artifact — not a branch state.
**Depends on:** P8-01..P8-07
**Description:** Per Design Constraints: release workflow (test → buildx → GHCR push → release notes), `CHANGELOG.md`, version single-sourcing (footer + `--version`), tag `v1.0.0` after the user's final acceptance pass.
**Acceptance criteria:**
- [ ] Tagging a release-candidate tag on a branch runs the full workflow and publishes a pullable multi-arch image (dry-run tag acceptable).
- [ ] `docker run ghcr.io/<owner>/librairy:<rc>` on a clean machine reaches a healthy portal.
- [ ] Changelog honestly states capabilities AND the never-list (no delete, no overwrite, library read-only).
- [ ] `v1.0.0` tagged only after every item in this phase's exit gate is checked.
**Size:** S

## Verification steps

1. Full suite: `ruff check src tests && pytest` (including `slow` marks) — green.
2. Clean-machine drill (the release rehearsal): new VM or pruned Docker host → follow `docs/install-docker.md` only → healthy portal → drop mixed sample files → review → commit → search finds them → undo works. No step may require knowledge outside the docs.
3. UNRAID drill on the user's NAS: add template, map shares, set Ollama host → healthy portal → verify created library files are owned by the share user (PUID/PGID) → run the same loop.
4. `docker buildx build --platform linux/amd64,linux/arm64` clean; pull-and-run the pushed RC image on both arches (arm64 via Apple-silicon Docker or QEMU).
5. 50k smoke run locally; record numbers; confirm UI responsiveness mid-run by hand.
6. Failure-mode spot checks: unwritable library mount → friendly boot error; kill web child → HEALTHCHECK flips unhealthy → supervisor restarts it.

## Exit gate checklist

- [ ] A stranger can install from docs alone (clean-machine drill passed exactly as written).
- [ ] UNRAID template drill passed on real UNRAID hardware; files owned by the mapped user; nothing runs as root.
- [ ] Multi-arch images build, publish, and run; czkawka + all tools present on both arches.
- [ ] Boot-time validation covers the failure classes with plain-language errors.
- [ ] 50k smoke test passes with documented resource bounds; perf numbers recorded.
- [ ] Docs complete and sync-tested (config reference, redaction allowlist, no stale references); RAM/ROM migration note published.
- [ ] Logs structured, rotated, secret-free (test-enforced).
- [ ] Zero known data-safety bugs open; the full invariant test suite green.
- [ ] `v1.0.0` tagged; release workflow produced the published image + changelog.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 9 features (document text search, rclone backup) ship as minor releases (v1.1+) using the release workflow built here.
- Community Apps feed submission, Docker Hub mirror, Synology/QNAP guides: post-1.0 backlog seeds, deliberately unscheduled.
- If multiple uvicorn workers are ever introduced, revisit the in-memory login rate limiter (noted in Phase 5) and the single-in-flight commit guard (Phase 6).

## Open questions log

- 2026-07-22: P8-01 local implementation added a multi-stage Dockerfile, PUID/PGID entrypoint, healthcheck, `.dockerignore`, and static packaging tests. Runtime acceptance checks remain blocked locally because Docker daemon is unavailable.
- 2026-07-22: P8-02 boot validation is locally implemented for missing/unwritable roots, nested roots, DB open/migration, and busy ports, with numbered friendly errors before supervisor startup. Container boot/healthy checks remain blocked locally because Docker daemon is unavailable.
