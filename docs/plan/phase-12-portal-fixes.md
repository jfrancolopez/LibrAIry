# Phase 12 — Portal Defect Fixes (pre-v1.0-tag)

**Status:** NOT STARTED
**Depends on:** Phase 10 tasks P10-01..P10-05 (execute this phase BEFORE P10-06 tags v1.0.0 — these are release blockers found in owner acceptance testing)
**Size:** S–M (six small tasks; root causes already diagnosed)

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY lives in docs/plan/README.md. Do not edit here; if decisions change, the canonical copy is updated and propagated. -->

**LibrAIry** is a self-hosted, privacy-first, AI-assisted file organizer and library manager. It ships as a single Docker container for NAS systems (UNRAID is the primary target) and desktop workstations. The user drops messy files into an **inbox** folder; LibrAIry analyzes them continuously in the background and proposes clean names and destinations inside an organized **library**; the user reviews proposals in batches from a lightweight LAN web portal (approve / edit / reject / postpone); only then does LibrAIry move files. It is an *orchestrator*, leaning on proven external tools (ffprobe, exiftool, fpcalc/Chromaprint, rmlint, czkawka) and free catalog APIs (MusicBrainz, AcoustID, TMDB), using AI only when deterministic evidence is insufficient — local AI (Ollama) by default, cloud AI strictly opt-in.

**Safety invariants (non-negotiable; enforced in code and by tests):** LibrAIry NEVER deletes user files; NEVER overwrites (deterministic collision renames); the existing library is READ-ONLY; analysis never mutates the filesystem — only the commit engine moves files, executing exactly an approved, immutable, hash-verified plan; every destination is containment-validated (traversal/absolute/symlink escapes fail closed); quarantine is reversible; v1 renames/moves only; every operation is journaled and undoable; privacy is local-first with structural redaction and per-provider cloud opt-in.

**Locked product decisions (do not reopen):** one container, web + worker under a Python supervisor; single-admin LAN portal (scrypt, SQLite sessions, CSRF, rate limiting); taxonomy `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/ Misc/`; SQLite WAL + FTS5 (no Postgres, no Elasticsearch); Python 3.11+, FastAPI + uvicorn + Jinja2 + HTMX, vanilla CSS/JS (no Node build), raw stdlib sqlite3 (no ORM), Pydantic, pytest, ruff, GitHub Actions; Ollama default with per-provider cloud opt-in; duplicates → reversible quarantine only; portal is "a lightweight dashboard and review tool, NOT a file manager"; no microservices/queues/plugin system/Kubernetes.

<!-- end context-boilerplate -->

---

## Why this phase exists

On 2026-07-23 the owner ran the first real user-acceptance session against the running container (Docker drill, phase 10) and found defects that must not ship in v1.0.0. Each was root-caused by code inspection on the same day — **the mechanisms below are diagnoses to verify by reproduction, then fix; do not re-investigate from scratch.**

Key architecture notes for this phase: templates live in `src/librairy/web/templates/` (base layout `base.html`, htmx vendored at `web/static/htmx.min.js`, styling `web/static/pipboy.css`); routes in `src/librairy/web/app.py`; settings persistence via `src/librairy/settings_service.py` (autocommit SQLite — writes persist immediately); CSRF is enforced on non-GET by middleware, tokens come from `request.state.session.csrf_token` (htmx sends them via `hx-headers`; plain forms need a hidden `csrf_token` input — `dashboard.html` shows the pattern).

## In scope

The six defects below. Cosmetic-only polish beyond them is Phase 13/14 — resist scope creep.

## Out of scope (tempting, but NO)

- Theme system, settings-screen redesign (Phase 13). Screen redesigns (Phase 14). New catalogs/keys (Phase 15).
- Making host paths web-editable — impossible with Docker bind mounts; P12-06 makes them *visible* and documents the `.env` workflow instead.

## Backlog items

### P12-01 Settings save: fix black screen, show confirmation
**Story:** As the admin, I click SAVE on Settings and see my settings page again with a clear "[OK] SETTINGS SAVED" — never a broken black page.
**Depends on:** —
**Description:** Diagnosed root cause: `settings.html:8` form carries `hx-post="/settings" hx-target="body" hx-swap="outerHTML"`; the POST route (`app.py:190-234`) returns `RedirectResponse("/settings?saved=1", 302)`; htmx follows the redirect transparently and swaps the **entire full-page HTML document** into `<body>` with `outerHTML`, destroying the real body element so `pipboy.css` body rules stop applying → black/broken page. **Values DO persist** (autocommit) — the "not saving" impression was the broken render plus no confirmation. Fix (pick the simplest, apply consistently): when the request has the `HX-Request` header, return `204` with an `HX-Redirect: /settings?saved=1` response header (htmx then performs a full browser navigation); alternatively drop htmx attributes from this form entirely and add the hidden `csrf_token` input (pattern in `dashboard.html`). On GET with `saved=1`, render a visible confirmation banner. Rename the button to `SAVE SETTINGS`. Audit the repo for the same `hx-target="body"` + redirect pattern on other forms and fix any siblings the same way.
**Acceptance criteria:**
- [ ] Reproduced the black screen in the running container before fixing (screenshot or note in log).
- [ ] Saving settings lands back on a fully styled settings page showing "[OK] SETTINGS SAVED".
- [ ] Regression test: TestClient POST `/settings` with header `HX-Request: true` → response is not a 200 full-document (assert `HX-Redirect` header or non-htmx form behavior).
- [ ] No other form in `templates/` uses full-document-into-body swaps (grep check recorded).
**Size:** S

### P12-02 Live template-style example + genre-first defaults
**Story:** As the admin, switching a category to "genre-first" instantly updates the example path so I can see what I'm choosing — and music/movies/shows arrive genre-first out of the box.
**Depends on:** P12-01 (same template/form)
**Description:** Diagnosed: the example line (`settings.html:20-32`) is server-rendered from the **persisted** style (`settings_service.settings_page_data` → `example_path` (settings_service.py:246) → `taxonomy.render_destination`); the `<select>` has no change handler, so changing it does nothing until save (and P12-01 then hid the result). Fix: add a small GET endpoint (e.g. `/settings/template-example?category=music&style=genre-first`) returning just the example line; wire the select with `hx-get` + `hx-target` on the sibling example element — `example_path`/`render_destination` already accept a `style=` argument, no logic changes. Defaults: replace the single `DEFAULT_STYLE = "conventional"` (`taxonomy.py:14`) with a per-category default map — **genre-first for music, movies, shows**; conventional for books/photos/documents/projects/misc (genre-first only exists for music/movies/shows/books per `taxonomy.py:16-37`). `template_style(conn, category)` (taxonomy.py:67-77) falls back to the map. Existing installs with saved `templates.<category>.style` rows keep their values; only fresh DBs see the new defaults — state this in the doc/commit.
**Acceptance criteria:**
- [ ] Changing the style dropdown updates the example path without saving; saving persists it.
- [ ] Fresh DB: `template_style` returns genre-first for music/movies/shows, conventional otherwise (unit test updated).
- [ ] Persisted style still wins over defaults (test).
**Size:** S

### P12-03 Item detail / history detail black screens
**Story:** As the admin, clicking a file's detail or a history entry always shows a real page — and if something ever does fail, the error page says so instead of rendering near-blank.
**Depends on:** —
**Description:** Diagnosed mechanism: browse item links (`browse_category.html:11` → `/items/{id}`, route `app.py:657`) and history plan links (`history.html:8` → `/history/plans/{id}`, route `app.py:525`) catch only `ValueError`; any other exception hits the 500 handler (`app.py:678`) which renders `error.html` — a nearly empty dark page indistinguishable from a blackout. Most likely throwers: `browse.item_detail:84` → `preview_for_item` → `thumbs.get_thumbnail` raising an uncaught `OSError` on cache-dir mkdir/write (`thumbs.py:60-66`), or `decode_evidence` on unexpected evidence JSON (`browse.py:90`). **First reproduce with real data in the running container and capture the actual traceback (`docker logs librairy`)** — then: wrap preview generation and evidence decoding so failures degrade (detail page renders without preview / with "evidence unavailable" instead of 500-ing); make `error.html` a properly styled page (status code, plain-language message, back link); add a regression test where `get_thumbnail` is monkeypatched to raise `OSError` and `/items/{id}` still returns 200 without a preview.
**Acceptance criteria:**
- [ ] Actual traceback captured and recorded in the open-questions log before fixing.
- [ ] Item detail and history detail render for every item/plan in the drill DB.
- [ ] Preview failure degrades gracefully (test), evidence-decode failure degrades gracefully (test).
- [ ] `error.html` visibly identifies itself as an error page.
**Size:** M

### P12-04 Durable dashboard banner dismissal
**Story:** As the admin, dismissing the welcome banner makes it stay gone.
**Depends on:** —
**Description:** Diagnosed: dismiss button exists (`base.html:35`, `hx-post="/welcome/dismiss"` → `app.py:139-142` → `auth.dismiss_welcome_banner`, auth.py:130) but the dismissal key is **per-session** (`ux.welcome_dismissed.<token_hash>`, auth.py:126-133), so the banner returns after re-login — and if htmx failed to load, the button does nothing. Fix: store one durable global key (e.g. `ux.welcome_dismissed = true`) instead of per-session; verify the button works live in the container; keep the banner shown on `/dashboard` only until first dismissal ever.
**Acceptance criteria:**
- [ ] Dismiss survives logout/login and container restart (manual drill + test on the settings key).
**Size:** XS

### P12-05 Logout and account controls in the header
**Story:** As the admin, logout is where every app puts it — top right, on every page.
**Depends on:** —
**Description:** Today logout is a plain form at the **bottom of the dashboard only** (`dashboard.html:8-11` → POST `/logout`, `app.py:131-137`); the global header (`base.html:12-26`) has the wordmark + nav links but no logout. Move logout into the header's right side on every authenticated page (simple POST form styled as a nav item — no dropdown component), alongside a compact account area: the version string (already in the footer) may stay in the footer; if a change-password flow exists, link it here (if none exists, do NOT build one in this phase — note it for later). Remove the dashboard-bottom form.
**Acceptance criteria:**
- [ ] Logout visible top-right on every authenticated page; works (CSRF intact); dashboard-bottom form gone.
- [ ] Web test asserting the logout control renders in the base layout for authenticated pages.
**Size:** XS

### P12-06 Storage-paths visibility + macOS test-folder walkthrough
**Story:** As the owner, I can see exactly which host folders LibrAIry is using from the Settings screen, and the docs tell me how to point them at test folders on my Mac desktop.
**Depends on:** —
**Description:** Host paths are boot-time env only (`config.py:23-31`, `HOST_*_DIR` + container `/data/*`) and **cannot be changed at runtime** — Docker bind mounts are host-level; say this honestly in the UI. Add a read-only "STORAGE PATHS" section to Settings showing the four host paths and their container mappings (the `/access` page, `app.py:630-641` + `access.html:9-11`, already displays them — reuse that data source), with an inline note: "Set in `.env`, applied by `docker compose up -d`." Docs: add a short "Using test folders" walkthrough to `docs/install-docker.md` — e.g. `HOST_INBOX_DIR=/Users/<you>/Desktop/test-inbox`, `HOST_LIBRARY_DIR=/Users/<you>/Desktop/test-library`, etc., then `docker compose up -d` recreates the container with the new mounts (macOS: folder must be within Docker Desktop file-sharing scope; `~/Desktop` is by default).
**Acceptance criteria:**
- [ ] Settings shows the four host paths read-only with the env note; test asserts the section renders.
- [ ] `docs/install-docker.md` contains the test-folders walkthrough.
**Size:** XS

## Verification steps

1. Every task: reproduce in the running drill container first, fix, then re-drill the exact flow manually.
2. `ruff check src tests scripts && pytest` green after each task's commit (one task = one commit).
3. Final pass: owner walks Settings → save, style switch, item detail, history detail, banner dismiss, logout — all clean — then Phase 10's P10-06 (tag v1.0.0) may proceed.

## Exit gate checklist

- [ ] All six defects fixed, regression-tested, and manually re-verified in the container.
- [ ] Suite green; no new runtime dependencies; no Node/JS frameworks introduced.
- [ ] Owner sign-off recorded here; v1.0.0 tagging unblocked.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
