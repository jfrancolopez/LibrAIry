# Phase 10 — Release Acceptance & v1.0.0 Publish

**Status:** NOT STARTED
**Depends on:** Phase 9 (code complete; CI green after P9-07)
**Size:** M (mostly verification and packaging; one Dockerfile rework)

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

## State of the world at planning time (2026-07-22 — trust but re-verify cheaply)

- All phases 1–9 are implemented and merged; 336 tests green; ruff clean. Every remaining unchecked box in phases 1/4/5/6/8/9 is gated on Docker builds, real UNRAID hardware, or publishing — nothing is gated on missing code.
- The repo was **transferred to `github.com/jfrancolopez/LibrAIry`**. Workflows use `github.repository_owner`, and `packaging/unraid/librairy.xml` + install docs already point at `ghcr.io/jfrancolopez/librairy` — the namespace is now consistent. Old `solosoyfranco` URLs redirect.
- **No git tag exists**; `release.yml` has never run. The GHCR image has never been built or published.
- The product reports **version 0.1.0** while all docs describe v1.0.
- CI is green again after P9-07 (pdftotext fix). Known non-blocking warnings: Node 20 deprecation on `checkout@v4`/`setup-python@v5`; Starlette/httpx deprecation in pytest output.
- Local dev machine is an Apple Silicon Mac with Docker Desktop **installed but not running**; Python 3.14 in `.venv` (CI runs 3.11/3.12).

## Owner-manual actions (agents MUST NOT attempt these)

| ID | Action | Needed before |
|----|--------|---------------|
| O1 | Start Docker Desktop and wait for `docker info` to succeed | P10-05 |
| O2 | Push the release tag (`git push origin v1.0.0`, optionally `v1.0.0-rc1` first) | P10-06 |
| O3 | If GHCR publishes the package as private: flip package visibility to public (GitHub → Packages → librairy → settings) so UNRAID can pull anonymously | P10-06 verify |
| O4 | UNRAID drill on real hardware per `docs/install-unraid.md` | P10-06 close-out |

## Phase goal

Turn the finished codebase into a published, verified v1.0.0: green CI without deprecation warnings, a release workflow that can actually complete a multi-arch build, truthful version strings and planning docs, a Docker deployment verified end-to-end locally, and a tagged release publishing `ghcr.io/jfrancolopez/librairy` — followed by the real-hardware UNRAID drill.

## Out of scope (tempting, but NO)

- Any new product features (the TUI is Phase 11; everything else stays in the phase-9 "Notes for future phases" parking lot).
- Community Apps feed submission, Docker Hub mirror, Synology/QNAP templates (post-1.0, unchanged).
- Rewriting plan-history docs; reconciliation is additive status corrections only.
- CI perf-threshold tuning unless a run actually flakes (`tests/test_performance_smoke.py` bounds are generous; leave them until they bite).

## Backlog items

### P10-01 Actions hygiene: Node-24 majors + lint scope
**Story:** As a maintainer, CI runs warning-free on supported action majors, and CI lints exactly what release lints.
**Depends on:** —
**Description:** In both `.github/workflows/ci.yml` and `.github/workflows/release.yml`: bump `actions/checkout@v4` → `@v5` and `actions/setup-python@v5` → `@v6` (the Node-24 drop-in majors; do NOT chase v7 — newer majors change defaults such as credential persistence, unnecessary risk). In `ci.yml`, change the Lint step to `ruff check src tests scripts` to match `release.yml` and README.
**Acceptance criteria:**
- [ ] Both workflows reference `checkout@v5` and `setup-python@v6`; CI run shows no `node20` deprecation annotations.
- [ ] `ci.yml` lint command includes `scripts`.
- [ ] Both CI matrix legs green.
**Size:** XS

### P10-02 Release build: prebuilt czkawka per-arch + workflow hardening
**Story:** As a maintainer, the first-ever release workflow run completes: no Rust compile under QEMU, cached layers, and an RC tag can't hijack `latest`.
**Depends on:** —
**Description:** The Dockerfile builder stage runs `cargo install czkawka_cli --locked --version "8.0.0"`; under `docker/build-push-action` with `platforms: linux/amd64,linux/arm64` the arm64 leg compiles Rust inside QEMU — the classic multi-hour timeout/OOM. czkawka ≥10 publishes prebuilt `linux_czkawka_cli_x86_64` / `linux_czkawka_cli_arm64` release assets (8.0.0 has no arm64 CLI asset, which is why compile-from-source existed). Changes:
1. `Dockerfile`: `ARG CZKAWKA_CLI_VERSION=11.0.1` (patch-stabilized; 12.x is too fresh). Replace builder apt deps `build-essential cargo pkg-config` with `curl ca-certificates` (the wheel is pure Python; pydantic-core ships manylinux wheels for both arches). Replace `cargo install` with a `TARGETARCH`-mapped download of the matching release asset + `sha256sum -c` against pinned checksums (czkawka publishes no checksum files — the implementing agent downloads both assets once, computes SHA-256, hardcodes them as `ARG` defaults).
2. `Dockerfile` runtime stage: change `command -v czkawka_cli >/dev/null` to `czkawka_cli --version >/dev/null` so a glibc mismatch fails at build time on both arches. Fallback ladder if it fails on `python:3.12-slim-bookworm` (glibc 2.36): (a) pin czkawka 10.0.0; (b) switch base images to `python:3.12-slim-trixie`. Pick one, don't mix.
3. `release.yml` build step: add `cache-from: type=gha` / `cache-to: type=gha,mode=max`; change `type=raw,value=latest` to `type=raw,value=latest,enable=${{ !contains(github.ref_name, '-') }}` so pre-release tags don't move `latest`.
4. `tests/test_release.py`: assert `cargo install` absent from Dockerfile, `releases/download` + `sha256sum -c` present, `cache-from` present in the workflow.
**Acceptance criteria:**
- [ ] No `cargo`/`build-essential` in the Dockerfile; czkawka fetched per-arch with pinned checksum verification.
- [ ] `czkawka_cli --version` executes in the build on both arches (local: `docker buildx build --platform linux/amd64,linux/arm64 .` at least reaching past that step; full proof in P10-05/06).
- [ ] `latest` tag guarded against pre-release refs; GHA layer cache configured.
- [ ] New/extended release tests green.
**Size:** S

### P10-03 Version bump 0.1.0 → 1.0.0
**Story:** As a user, `librairy --version`, the web footer, and the image label all say 1.0.0 when I install v1.0.0.
**Depends on:** —
**Description:** Exactly three version strings exist (verified by grep): `pyproject.toml` `version = "0.1.0"`, `Dockerfile` label `org.opencontainers.image.version="0.1.0"`, `src/librairy/__init__.py` metadata-fallback `"0.1.0"`. Bump all three to `1.0.0`. Existing tests already pin behavior (`tests/test_release.py::test_version_is_sourced_from_package_metadata`, `test_web_footer_shows_version`). **Gotcha:** after editing, re-run `pip install -e ".[dev]"` locally or the metadata test fails confusingly with stale 0.1.0 metadata (CI installs fresh and is immune).
**Acceptance criteria:**
- [ ] `librairy --version` prints `librairy 1.0.0` after reinstall; no `0.1.0` remains in the three files.
- [ ] Full suite green.
**Size:** XS

### P10-04 Docs/plan truth reconciliation (light touch)
**Story:** As a future contributor, the planning docs tell the truth about what happened.
**Depends on:** P10-03
**Description:** Additive corrections only, one commit: (1) `docs/plan/README.md` phase map: rows 1–9 `NOT STARTED` → `DONE` (9 already reads its own status; keep map consistent), plus one line under the table: "All phases executed 2026-07-21/22. Migration numbers inside phase-doc Design Constraints are planning-time estimates; `src/librairy/db.py` (migrations 001–010, `SCHEMA_VERSION = 10`) is authoritative. Publish-gated leftovers are tracked in `CHANGELOG.md`." (2) Phase docs 1/4/5/6/9 status lines → `DONE — remaining boxes publish-gated, see phase-10`; leave phase-8 `IN PROGRESS` (it genuinely is, until P10-05/06). Do NOT tick any unchecked verification boxes here — they get ticked when actually verified. (3) `Instructions.md`: add the three doc links README has that it lacks (one-way backup, content search, performance). (4) `CHANGELOG.md` "Pending Before Tagging": drop the 50k-perf bullet (recorded in `docs/performance.md` 2026-07-22); keep Docker-verify and UNRAID bullets. Keep the four safety phrases intact (test-enforced by `tests/test_release.py`).
**Acceptance criteria:**
- [ ] Phase map and status lines truthful; migration-number disclaimer present.
- [ ] `Instructions.md` links match README's doc set.
- [ ] `pytest tests/test_release.py tests/test_docs.py -q` green.
**Size:** XS

### P10-05 Local Docker verification drill
**Story:** As the owner, I've seen the container build, boot, organize, undo, and survive a restart on my own machine before anyone else can pull it.
**Depends on:** P10-02, P10-03, O1 (Docker Desktop running)
**Description:** Execute the runbook, then commit the box-ticking. From repo root: `docker compose down -v; rm -rf data && mkdir -p data/inbox data/library data/quarantine data/appdata` → `docker compose build --no-cache` (watch the czkawka version print) → `docker compose up -d` → healthy within ~60s → `curl -fsS localhost:8080/healthz` → in-container tool sweep `docker exec librairy sh -c 'librairy --version && czkawka_cli --version && pdftotext -v 2>&1 | head -1 && rclone version | head -1 && rmlint --version 2>&1 | head -1 && ffprobe -version | head -1 && fpcalc -version'` → `docker top librairy` shows no UID-0 app processes → `docker exec librairy sh -c 'id && ls -ln /data/appdata'` shows 99:100 (host-side `ls -ln data/` will show the Mac user — VirtioFS remaps; in-container is the gate; real host ownership is the UNRAID drill). First-run: browser `/setup` → password → dashboard. Full loop: drop a `.txt` in `data/inbox`, wait ~30s, review → approve → commit → execute → file lands under `data/library/Documents/…` → history → undo → file back in inbox. Duplicate leg: two identical files → quarantine proposal → commit → restore. Restart: `docker compose restart librairy` → healthz OK → browser session still valid without re-login. Optional: `docker buildx build --platform linux/arm64 .` cross-build smoke; capture `docs/images/dashboard.png` for the README (P8-05's promised screenshot) — if skipped, log the deferral in phase-8's open questions.
**Acceptance criteria:**
- [ ] Every runbook step above passes; failures fixed and re-run before ticking.
- [ ] Closing commit ticks the now-verified docker-gated boxes in phases 4/5/6/8 and removes the Docker-verify bullet from CHANGELOG.
**Size:** S (execution-heavy, code-light)

### P10-06 Tag, publish, and close v1.0.0
**Story:** As a user, I can `docker pull ghcr.io/jfrancolopez/librairy:v1.0.0` and it runs.
**Depends on:** P10-01, P10-02, P10-04, P10-05
**Description:** Final CHANGELOG commit: retitle `## v1.0.0 - pending final acceptance` → `## v1.0.0 - <date>`, delete the "Pending Before Tagging" section (UNRAID drill becomes O4, post-publish). Confirm main is green. Recommended rehearsal: owner pushes `v1.0.0-rc1` → workflow publishes the RC image **without** moving `latest` (P10-02 guard) → pull and boot it once → delete RC release/tag if desired. Then owner pushes `v1.0.0` (O2). Agent verifies: Actions run green end-to-end; `docker pull ghcr.io/jfrancolopez/librairy:v1.0.0` and `:latest`; `docker run --rm ghcr.io/jfrancolopez/librairy:v1.0.0 librairy --version` → `librairy 1.0.0`; GitHub Release exists with CHANGELOG body. If the package is private, O3. After the owner's UNRAID drill (O4): tick phase-8's UNRAID boxes, flip phase-8 and this phase to DONE.
**Tag strategy (decided):** tag **v1.0.0 only** — the P9 fast-follow features ship inside v1.0.0 since no earlier v1.0 was ever published; a same-commit v1.1.0 tag would be two releases with an empty delta. Phase-9's "v1.1 published" box gets the note "folded into v1.0.0". v1.1.0 is reserved for Phase 11 (TUI).
**Acceptance criteria:**
- [ ] `v1.0.0` tag exists; release workflow green; multi-arch image pullable; version prints 1.0.0 from the published image.
- [ ] GitHub Release page carries the CHANGELOG.
- [ ] UNRAID drill done (O4) and phase-8 closed.
**Size:** S

### P10-07 czkawka wrapper: fix real-binary invocation (v1.0.x fast-fix)
**Story:** As a user, similar-media detection actually produces flags when czkawka runs for real, not just in mocked tests.
**Depends on:** P10-05 (needs the container to verify against the real binary)
**Description:** `src/librairy/tools/czkawka.py` invokes `czkawka_cli image -f json` style arguments — but czkawka's `-f` writes a *text file* named as given (so this creates a file literally named `json`) and `-e` is not the extensions flag (`-x` is allowed-extensions). stdout is human text, so `run_json_tool` always returns `ToolResult(ok=False, "invalid JSON")` and similar-media detection silently no-ops (unit tests mock the subprocess, hiding it). Not a data-safety issue — it degrades to "no similar media found". Fix: invoke with the compact-JSON-output flag `-C <tmpfile.json>` (present in czkawka 8 and 11), read and parse the tmpfile, use `-x` for extensions; verify inside the P10-05 container against real image/video pairs; update unit tests to assert the new argv shape.
**Acceptance criteria:**
- [ ] Wrapper produces parsed groups from a real czkawka run in the container (manual drill: two resized copies of one photo in inbox → similar-media flag appears).
- [ ] Command-builder unit tests assert `-C`/`-x` argv; suite green.
**Size:** S

## Verification steps

1. After P10-01/02/03/04: `ruff check src tests scripts && pytest` green locally AND both CI legs green on GitHub with zero deprecation annotations.
2. P10-05 runbook executed top to bottom with all checks passing.
3. P10-06: published image pulled and booted on a machine that never built it.
4. P10-07: real-binary czkawka drill in the container.

## Exit gate checklist

- [ ] CI green, warning-free; release workflow proven by an actual published multi-arch image.
- [ ] Product reports 1.0.0 everywhere; planning docs truthful; CHANGELOG final.
- [ ] Docker drill and UNRAID drill both done; phase-8 closed as DONE.
- [ ] `ghcr.io/jfrancolopez/librairy:v1.0.0` + `:latest` public and pullable.
- [ ] All backlog checkboxes ticked; status DONE.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*

- 2026-07-23: first-ever Docker build (start of the P10-05 drill) found two blockers, both fixed ahead of the planned order:
  1. Runtime apt package `chromaprint-tools` does not exist in Debian bookworm — the fpcalc package is `libchromaprint-tools`. Fixed in the Dockerfile.
  2. `cargo install czkawka_cli --locked --version 8.0.0` fails: **that version was never published to crates.io**, so the image was never buildable on any arch (worse than the QEMU-slowness this task predicted). P10-02 executed early: prebuilt czkawka 11.0.1 per-arch download with pinned SHA-256 (amd64 `2f81d63f…`, arm64 `eb333e3b…`), builder stage dropped cargo/build-essential, runtime check upgraded to `czkawka_cli --version`, release workflow gained GHA cache + `latest`-tag pre-release guard. The 11.0.1 arm64 binary was verified to run on `python:3.12-slim-bookworm` (glibc OK) before pinning — no fallback needed.
  3. **Every documented CSV env var (`AI_PROVIDER_ORDER`, `IGNORE_PATTERNS`, `CZKAWKA_EXTENSIONS`) crashed Settings at boot** when set via real environment or a `.env` file: pydantic-settings JSON-decodes complex fields at the source layer *before* the model's CSV `field_validator` runs, raising `SettingsError`. Never caught because tests construct `Settings(**kwargs)`, which bypasses env sources — the documented configuration contract (README quickstart `cp .env.example .env`, compose `env_file`, UNRAID template fields) had never been exercised. Fixed with `Annotated[list[str], NoDecode]` on the three fields (pydantic-settings' official mechanism; floor bumped to `>=2.6`), plus regression tests that go through the real env and dotenv sources (`tests/test_config.py::test_csv_fields_parse_from_real_env_vars`, `::test_env_example_values_parse_through_dotenv_source`).
