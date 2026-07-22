# Phase 5 — Web Foundation: Auth, Pip-Boy Theme, Dashboard, Health

**Status:** IN PROGRESS
**Depends on:** Phase 4 (worker + bash retirement) DONE
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

- **Supervisor**: the container entrypoint `python -m librairy run` — a small (~50-line) process manager spawning uvicorn (web) and the worker as subprocesses, restarting either on crash with backoff, forwarding SIGTERM.
- **Session**: server-side login session — random 256-bit token in a cookie (`HttpOnly`, `SameSite=Lax`; `Secure` when behind TLS), token *hash* stored in the `sessions` table (created in schema v1).
- **CSRF token**: per-session random token required on every state-changing request (double-submit: hidden form field / `X-CSRF-Token` header checked against the session row).
- **First-run setup**: when no admin password hash exists in `settings`, the portal shows a one-time create-password screen; after that, login only. Single admin user — no roles, no user table.
- **Pip-Boy theme**: the project's visual identity: dark background, phosphor green primary (amber accent option), monospace type, subtle CRT scanline/glow effects, chunky bordered panels — implemented in plain CSS custom properties, no frameworks, no build step.
- **HTMX partial**: a server-rendered HTML fragment swapped into the page by HTMX attributes (used for live dashboard updates via polling).

## Entry criteria

```bash
# 1. Phase 4 exit gate holds
pip install -e ".[dev]" && ruff check src tests && pytest

# 2. Worker importable and CLI-runnable; bash gone
python3 -c "from librairy.worker import main; print('OK')"
test ! -d inbox-processor && echo GONE

# 3. worker_state + lifecycle counts queryable (dashboard data source)
librairy worker --once --help
```

If any check fails, STOP and report.

## Phase goal

Ship the portal shell: a login-protected, Pip-Boy-themed FastAPI web app served alongside the worker by one supervisor entrypoint, with a live dashboard (what's happening in the background) and a health screen (tools, providers, disk, DB). Later phases add screens to this shell; this phase makes the container's UX real: `docker compose up` → browse to the portal → watch LibrAIry work.

## In scope

- FastAPI app skeleton, Jinja2 templates, static assets, Pip-Boy CSS.
- Auth (first-run setup, login, logout, sessions, CSRF, rate limiting).
- Supervisor entrypoint; compose/Dockerfile updated to it.
- Dashboard + health screens with HTMX live updates.
- Web test suite.

## Out of scope (tempting, but NO)

- Review queue, commit, quarantine, history screens (Phase 6). Search, browse, settings screens (Phase 7).
- Any HTTPS/TLS termination (LAN deployment; a reverse proxy is the user's choice — Phase 8 documents it).
- Multi-user, roles, OAuth, TOTP (single admin, period).
- WebSockets (HTMX polling is enough; SSE optional only if trivial).
- JS frameworks, npm, bundlers — hand-written CSS/JS only.
- New file-moving capabilities: this phase's UI is read-only over engine state.

## Design constraints binding this phase

- **Module layout**: `web/app.py` (FastAPI factory), `web/auth.py`, `web/routes/dashboard.py`, `web/routes/health.py`, `web/templates/` (`base.html`, `login.html`, `setup.html`, `dashboard.html`, `health.html`, partials), `web/static/` (`pipboy.css`, `htmx.min.js` **vendored** — committed to the repo, no CDN), `supervisor.py`.
- **Dependencies added now**: `fastapi`, `uvicorn`, `jinja2`, `python-multipart` (forms), `httpx` (dev/test). Nothing else.
- **Auth rules**: scrypt via stdlib `hashlib.scrypt` (n=2**14, r=8, p=1 minimums; parameters stored beside the hash for future migration); constant-time compare; session tokens `secrets.token_urlsafe(32)`, stored hashed (sha256) with expiry (default 7 days, sliding); logout deletes the row; login rate limit: 5 failures / 5 minutes per source IP (in-memory is fine, document that a container restart resets it); every non-GET route requires a valid CSRF token; all routes except `/login`, `/setup`, `/static/*`, `/healthz` require a session. `/healthz` is an unauthenticated 200 JSON liveness endpoint for Docker healthchecks — no data beyond status.
- **Supervisor**: `python -m librairy run` = default container CMD. Spawns uvicorn (`web.app:create_app`, host `0.0.0.0`, port `DASHBOARD_PORT` default 8080) and `worker.main()` as subprocesses; restart on crash with exponential backoff (cap 60s); SIGTERM → forward, wait, exit; exit non-zero if a child flaps continuously (>N restarts in M minutes). The web process NEVER runs the executor in-request in this phase (nothing to commit yet); mutating anything later goes through the Phase-1 lock.
- **Dashboard content** (read-only queries, no new instrumentation): worker heartbeat/phase from `worker_state`; counts by lifecycle state (inbox pending, analyzing, proposed, quarantine-staged); recent history (last N ops); AI provider summary (from `provider_status`); disk free for the four data roots. HTMX `hx-get` polling every 5s on the stats partial. Numbers, sparkline-ish bars, status lamps — no charts library.
- **Pip-Boy theme**: `pipboy.css` custom properties (`--phosphor`, `--phosphor-dim`, `--amber`, `--bg`, `--panel-border`); monospace stack (`"JetBrains Mono", "Fira Mono", ui-monospace, monospace` — system fonts only, no webfont downloads); scanline overlay via `repeating-linear-gradient` + subtle `text-shadow` glow; visible focus states; status conveyed by text+symbol, never color alone (`[OK]`, `[WARN]`, `[FAIL]`); respects `prefers-reduced-motion` (no flicker/CRT animation). Keep it tasteful: readable first, aesthetic second.
- **Security hygiene**: Jinja2 autoescape on; security headers middleware (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, minimal CSP `default-src 'self'`); no secrets ever rendered; error pages leak no paths/tracebacks (DEBUG mode only via env flag).

## Backlog items

### P5-01 FastAPI skeleton + Pip-Boy base theme
**Story:** As a user, the portal loads instantly, looks like my Pip-Boy, and works on any modern browser with no build step.
**Depends on:** Phase 4
**Description:** App factory, base template (header with LibrAIry wordmark + nav slots, panel layout), `pipboy.css` per constraints, vendored `htmx.min.js`, 404/500 pages in-theme.
**Acceptance criteria:**
- [x] `uvicorn` serves `/` (redirects to login/setup); static assets load from the package; zero external network requests (test asserts no non-self URLs in rendered HTML).
- [x] Theme: monospace, phosphor-on-dark, scanline overlay, `[OK]/[WARN]/[FAIL]` status idiom, reduced-motion respected.
- [x] Autoescape + security headers verified by test.
**Size:** M

### P5-02 Auth: first-run setup, login, sessions, CSRF, rate limit
**Story:** As a NAS user, my portal is protected on the LAN with a password I set on first run — no config-file fiddling.
**Depends on:** P5-01
**Description:** Per Design Constraints: setup screen (only when no hash exists; sets password, creates session), login/logout, session middleware/dependency, CSRF enforcement on non-GET, rate limiting, `/healthz`.
**Acceptance criteria:**
- [x] Fresh DB → any route redirects to `/setup`; after setup, `/setup` is gone (404/redirect) forever.
- [x] Wrong password rejected; 6th failure within window → 429 with retry hint; correct login sets HttpOnly cookie.
- [x] Every non-GET without a valid CSRF token → 403 (tested on a sample route).
- [x] All protected routes 302→login without a session (route-table sweep test).
- [x] Session expiry honored; logout invalidates immediately; tokens stored hashed (DB row ≠ cookie value).
- [x] scrypt parameters stored; hash verifies after a simulated parameter upgrade.
**Size:** M

### P5-03 Supervisor entrypoint + container wiring
**Story:** As a user, `docker compose up` gives me both the portal and the background worker — one service, no orchestration homework.
**Depends on:** P5-01
**Description:** `supervisor.py` per constraints; compose service uses it as command with `DASHBOARD_PORT` published; Dockerfile CMD updated; Docker `HEALTHCHECK` hitting `/healthz`.
**Acceptance criteria:**
- [x] `python -m librairy run` starts both children; killing the worker child → auto-restart (logged, backoff); same for web.
- [x] SIGTERM to supervisor → both children exit cleanly ≤ 10s; supervisor exits 0.
- [ ] `docker compose up` → portal reachable on the mapped port; container healthcheck goes healthy.
- [x] Flapping child (forced crash loop) → supervisor gives up and exits non-zero (restart policy surfaces it).
**Size:** M

### P5-04 Dashboard screen
**Story:** As a user, one glance tells me what LibrAIry is doing right now and what's waiting for me.
**Depends on:** P5-02
**Description:** Dashboard route + template per Design Constraints: worker status lamp + current phase, lifecycle counts (with "N proposals awaiting review" as the visual centerpiece), recent operations feed, provider summary line, disk-free bars for the four roots; HTMX 5s polling partial; empty states ("inbox clear — drop files to begin") in Pip-Boy voice.
**Acceptance criteria:**
- [ ] With the worker running against a seeded sandbox, counts and phase update live (integration test polls the partial twice across a state change).
- [ ] All data read-only from existing tables (no new writes from web; test wraps requests and asserts no DB mutations besides sessions).
- [ ] Renders correctly with zero data (fresh install) — no division-by-zero, friendly empties.
- [ ] Page interactive < 1s on the seeded sandbox (no N+1 queries; count queries are single statements).
**Size:** M

### P5-05 Health screen
**Story:** As a user, when something's off — Ollama down, czkawka missing, disk filling — I see it in one place with plain-language hints.
**Depends on:** P5-02
**Description:** Health route: external tool availability (ffprobe/exiftool/fpcalc/rmlint/czkawka version probes, cached), AI provider table from `provider_status` with per-row test buttons (HTMX post → live check), DB status (size, WAL, integrity_check quick), disk space with warn thresholds, worker heartbeat age. Each failing row gets a one-line remedy hint (e.g. "czkawka missing — image was built without it; see install docs").
**Acceptance criteria:**
- [ ] Tool probes report present/missing correctly (test with PATH manipulation).
- [ ] Provider test button triggers a real health check and updates the row without page reload.
- [ ] Warn states render as `[WARN]` with hint text; all-green shows `[OK]` summary.
- [ ] Probes are cached (no exec storm on refresh; call-count test).
**Size:** M

### P5-06 Web test suite
**Story:** As the project, the web layer has the same safety net as the engine.
**Depends on:** P5-02..P5-05
**Description:** httpx TestClient fixtures (app + tmp sandbox + seeded DB), covering auth flows, CSRF, headers, route protection sweep, dashboard/health rendering, and a supervisor smoke test (subprocess, brief run, SIGTERM).
**Acceptance criteria:**
- [ ] All above green in CI; route-protection sweep auto-discovers routes (new unprotected routes fail the sweep by default).
- [ ] Coverage includes at least one full browser-flow simulation: setup → login → dashboard → logout → blocked.
**Size:** S

## Verification steps

1. `ruff check src tests && pytest` green (all suites).
2. `docker compose up --build` with a scratch `.env` → browse `http://localhost:8080` → first-run setup → login.
3. Drop corpus files into the mounted inbox → watch the dashboard counts move without reloading; verify "awaiting review" grows.
4. Visit health: all tools `[OK]` (czkawka included — Phase 4 put it in the image); stop your LAN Ollama (or point at a bad host) → provider row goes `[FAIL]` after test-button.
5. `docker stop` (SIGTERM) → container exits promptly and cleanly; `docker start` → sessions still valid (SQLite-backed).
6. From another device on the LAN: portal reachable, login required, wrong-password lockout works.

## Exit gate checklist

- [ ] `docker compose up` → portal on `DASHBOARD_PORT` → setup/login → live dashboard reflecting a real worker run.
- [ ] Unauthenticated access fully blocked (sweep test); CSRF on all non-GET; sessions hashed at rest; rate limiting works.
- [ ] Supervisor restarts crashed children, shuts down cleanly on SIGTERM, surfaces flapping.
- [ ] Container restart preserves sessions and all state.
- [ ] Zero external asset/network dependencies in the UI; security headers present; no path/traceback leaks.
- [ ] Health screen truthfully reports tools/providers/disk/DB with remedy hints.
- [ ] All backlog checkboxes ticked; status DONE.

## Notes for future phases

- Phase 6 adds the review/commit routes INTO this shell; commits from the web must take the Phase-1 flock (executor already enforces it) and should run in a background task/thread with progress polled from `plan_ops` — do not block request handlers.
- Phase 7 mounts search/browse/settings into the same nav; the nav slots in `base.html` are ready.
- Keep `/healthz` shape stable — Phase 8's UNRAID template and Docker HEALTHCHECK rely on it.
- The rate limiter is in-memory by design (single process); if Phase 8 ever adds multiple web workers, revisit (documented here to avoid surprise).

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
