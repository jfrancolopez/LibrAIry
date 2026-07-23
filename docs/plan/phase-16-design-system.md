# Phase 16 — De-Pip-Boy: Friendly UI Foundation & Design System (v1.1)

**Status:** IN PROGRESS — P16-01/02/03 DONE (2026-07-23); P16-04/05/06/07 remain.
**Depends on:** Phase 13 (theme tokens + presets) DONE. **Execute BEFORE Phase 14** — the screen redesigns inherit the components defined here.
**Size:** L (foundational; touches every template, no engine changes)

---

## Product context

Canonical context lives in [README.md](README.md) (the `context-boilerplate v1` block). This doc does not re-embed it to stay readable; read that block plus the amendment below before executing.

## Decision amendment recorded by this phase

The locked **"UI style: Fallout Pip-Boy retro-terminal aesthetic"** decision — already softened by Phase 13 into a theme system — is **further amended by the owner (2026-07-23)** after real use:

> "The interface is not really user friendly or nice to the eye… buttons are not following any guidelines, looks clunky… Let's remove the Pip-Boy style. It's not really helpful or nice for this project. **User-friendly should be the main thing.**"

**New direction:** LibrAIry presents as a clean, conventional, friendly web app — the kind of admin UI a person can use without a manual. The retro *colors* are kept as **theme presets only** (Phase 13); the retro *structure* is retired: no `[OK]`/`[WARN]`/`[FAIL]` text chrome, no forced-monospace everywhere, no clunky borders-as-decoration. Everything else in the stack decision stands: **vanilla CSS + a little vanilla JS, no Node build step, no CSS/JS framework, htmx for interactions** (now that real htmx actually ships — see the P5-01-placeholder fix, commit history 2026-07-23).

## Phase goal

A small, documented design system — tokens, components, and an app shell — that makes every screen legible and pleasant, and a reorganized Settings that a first-time admin can navigate. Presentation only: reuse every existing data function (`web/*.py`, `search.py`). No schema changes.

## In scope

Design tokens (extend Phase 13's set), component CSS (buttons, inputs, cards, badges, nav, tables, banners), the app-shell header with account/logout top-right, typography that uses proportional sans for prose and monospace only for paths/code, removal of the `[OK]`-style text idiom across chrome, a generalized "never black-screen" form pattern, and Settings information architecture (grouped, navigable, intuitive). Screenshots refresh (discharges the lingering P8-05 / P13-04 gap).

## Out of scope (tempting, but NO)

- Per-screen content redesigns — dashboard/review/browse/history/health are **Phase 14**; this phase only gives them the components and shell. Do NOT redesign those screens' information here beyond swapping chrome for the new components.
- New engine features, new AI, new search backend, new catalogs (Phase 15).
- A JS framework, a build step, a component library, web components, CSS preprocessors. Vanilla only.
- Runtime-editable mount paths — impossible with Docker bind mounts; the path helper (P16-06) is copy-paste guidance, not live remounting.

## Design constraints binding this phase

- **Tokens (extend Phase 13):** Phase 13 gave color/border/radius tokens in `web/static/pipboy.css` (`:root` + `[data-theme]` blocks). Add, in `:root`, a **spacing scale** (`--space-1: 0.25rem … --space-6: 3rem`), **font tokens** (`--font-body` = system sans stack: `system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif`; `--font-mono` = the existing monospace stack), a **type scale** (`--text-sm/base/lg/xl/2xl`), and **elevation** tokens (subtle shadows already partly present as `--panel-shadow`). Presets keep overriding colors; a preset MAY set `--font-body` to the mono stack for a deliberately-retro look (the `pipboy-green`/`crt-amber`/`dos-blue` presets opt into mono; `beige-box`/`platinum-gray`/`vaporwave` use sans). Rename the stylesheet `app.css` (keep a `pipboy.css` → `app.css` redirect or update all `<link>`s + the `test_web_app.py` asset test).
- **Components (one stylesheet, class-based):**
  - `.btn` with `.btn-primary` / `.btn-secondary` / `.btn-ghost` / `.btn-danger`; consistent padding (`--space-2 --space-4`), min-height 40px (touch), clear hover/active/focus-visible, disabled state. **Every `<button>` in the app adopts a variant** — no bare buttons.
  - `.input`, `.select`, `.checkbox`, `.field` (label+control+help wrapper). Consistent sizing with `.btn`.
  - `.card` (padding, radius, subtle border+shadow), `.card-header/body/footer`.
  - `.badge` with status variants `.badge-ok/warn/fail/info` — this is where status *semantics* live now (color + optional inline SVG check/warn glyph), replacing the `[OK]`/`[WARN]` text. A tiny Jinja macro `status_badge(state)` renders them; sweep templates to use it.
  - `.app-header`, `.app-nav`, `.app-main`, `.app-footer`; a responsive nav that collapses gracefully on narrow widths (CSS only — a `<details>`-based menu is acceptable, no JS menu framework).
  - Tables: readable zebra/hover, sticky header where long.
- **App shell / header (#7):** wordmark left, primary nav center/left, and a right-aligned **account area**: when a password is set, a compact menu (name/"Admin" + **Logout** + link to Settings → Portal Security); when the portal is open, show a small "No password set — secure the portal" link to that settings section instead of a logout. Logout stays a CSRF-protected POST styled as a menu item (never a bare bottom-of-page form — remove any remnants). Reuse `portal_password_set()` (added Phase 12/optional-login work).
- **Remove the terminal idiom:** delete `[OK] ` / `[WARN] ` / `[FAIL] ` literal prefixes from nav links, headings, and buttons across `templates/`. Status meaning moves to `.badge`. Page headings become plain sentence/title case ("Dashboard", not "[OK] DASHBOARD"). Keep monospace only for paths, fingerprints, commands, and code — via `.mono` / `<code>`.
- **Never black-screen (generalize P12-01):** audit every `<form>` in `templates/` for the `hx-target="body"`-into-full-document anti-pattern and the missing-confirmation problem. Standard pattern: htmx forms return `204 + HX-Redirect` (or a partial swap into a named target), plain forms carry the hidden `csrf_token` and 302 to a page that shows a success banner. One shared success/error banner component + the Phase-13 sticky save bar generalized to any dirty form. Add a template-lint test (grep) that fails if any form uses `hx-target="body"` with a redirecting endpoint.
- **Settings IA (#3, #5):** build on Phase 13's single-page section order but make it navigable: a left (or sticky-top on mobile) **section nav** ("Appearance · Portal security · Analysis · Organization · Duplicates · Content search · Backup · Catalog keys · Storage paths") that anchor-jumps; each section is a `.card` with a one-line description; the sticky save bar (Phase 13) stays. Keep the P12-01 save flow. Optionally split into `hx-get`-loaded panels, but anchors are enough and simpler — prefer the simplest that reads as organized.
- **Accessibility:** keep the Phase-13 AA contrast guarantee (the contrast test must still pass with the new components); every control keyboard-reachable with visible `:focus-visible`; every input has a `<label>`; nav is a real `<nav>`; icons decorative-only have `aria-hidden`.

## Backlog items

### P16-01 Design tokens + typography (extend Phase 13)
**Depends on:** — | **Size:** S
- [x] Spacing, font, type-scale, elevation tokens added to `:root`; presets set `--font-body` (mono for the three terminal presets, sans for the rest); contrast test still green.
- [x] Body/prose render in `--font-body`; paths/fingerprints/code in `--font-mono` via `.mono`/`<code>`.

### P16-02 Component library (buttons, inputs, cards, badges, tables)
**Depends on:** P16-01 | **Size:** M
- [x] `.btn` variants, `.input/.select/.checkbox/.field`, `.card`, `.badge` variants, table styles — all token-driven, both light and dark presets legible.
- [x] `status_badge()` Jinja macro replaces every `[OK]/[WARN]/[FAIL]` text prefix in chrome (grep proves none remain in nav/headings/buttons).
- [x] Every `<button>` uses a variant class (grep check).

### P16-03 App shell + header with account/logout (#7)
**Depends on:** P16-02 | **Size:** M
- [x] Responsive header: wordmark, primary nav, right-aligned account area with Logout (password set) or "secure the portal" link (open portal). Collapses cleanly on mobile (CSS-only).
- [x] No bare logout form remains anywhere; logout still CSRF-protected and works (test).
- [x] Base layout applies the shell to every authenticated page.

### P16-04 Never-black-screen form pattern + banners
**Depends on:** P16-02 | **Size:** S
- [ ] Shared success/error banner component; all forms follow the safe submit pattern; generalized sticky save bar for dirty forms.
- [ ] Template-lint test fails on any `hx-target="body"` + redirect form; suite green.

### P16-05 Settings information architecture (#3, #5)
**Depends on:** P16-02, P16-03 | **Size:** M
- [ ] Section nav that anchor-jumps; each section a described `.card`; sticky save bar retained; P12-01 save flow intact.
- [ ] Owner drill: find and change a setting in each section without scrolling-hunting; save once; confirmation shown.

### P16-06 Storage-paths setup helper (#14)
**Depends on:** P16-05 | **Size:** S
**Description:** Runtime remounting is impossible (Docker bind mounts are host-level — stated honestly, per P12-06). Make the *setup* easy instead: the Storage Paths section shows the four current host→container mappings (P12-06) and adds a **copy-paste generator** — inputs for four host paths (prefilled with the current values, with a one-click "use my macOS Desktop test folders" example that fills `~/Desktop/librairy-{inbox,library,quarantine,appdata}`), producing a ready-to-paste `.env` snippet **and** the `docker compose up -d` line, with a note that this recreates the container. No write to the running container; purely a snippet builder (vanilla JS, no secrets).
- [ ] Section renders current paths + generator; generated `.env` snippet matches the inputs; "Desktop test folders" example fills sensible macOS paths; docs `install-docker.md` cross-links it.

### P16-07 Screenshots + docs refresh (closes P8-05 / P13-04)
**Depends on:** P16-03, P16-05 | **Size:** XS
**Description:** Capture dashboard, settings, and one review screen in the default theme into `docs/images/`, reference from README; close the phase-8 and phase-13 screenshot notes. (An agent that cannot commit binary assets must hand this to one that can — record the deferral rather than faking it.)
- [ ] `docs/images/` screenshots referenced from README; phase-8 + phase-13 screenshot notes closed.

## Verification steps

1. Per item: one commit, `ruff check src tests scripts && pytest` green, manual drill of the affected screens in the running container (real htmx now works — verify interactions, not just HTTP status).
2. After P16-03: click through every nav destination; confirm no `[OK]`-style chrome and a working top-right logout.
3. Before Phase 14 starts: the design system is stable enough that screen redesigns only add content, not new component CSS.

## Exit gate checklist

- [ ] No Pip-Boy terminal idiom in chrome; status shown via badges; prose in sans, paths in mono.
- [ ] Buttons/inputs/cards consistent and pleasant in every preset; AA contrast still enforced.
- [ ] Header with working top-right account/logout; no black-screen forms; Settings navigable.
- [ ] No new dependencies, no build step, one stylesheet; suite green.
- [ ] Screenshots refreshed (or deferral logged).

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
- 2026-07-23: created from owner acceptance feedback. Sequenced before Phase 14 so the screen redesigns inherit these components. The three "terminal" presets keep monospace via `--font-body`; the friendly presets use a system sans stack — colors (Phase 13) are unchanged.

- 2026-07-23 (execution, session 3): **P16-01/02/03 done and drilled live.** Handoff notes for whoever picks up P16-04..07:
  - The stylesheet was **NOT** renamed to `app.css` (kept `web/static/pipboy.css` to avoid churn/test breakage — the filename is now a misnomer but harmless; rename is optional future cleanup, remember `base.html` `<link>` + `test_web_app.py` asset test if you do).
  - `status_badge()` is **not** a Jinja macro yet — badges were inlined as `<span class="badge badge-ok|warn|fail|info">` across templates/partials for speed. If P16-04 adds a shared banner/macro, consider folding the repeated `{{ 'ok' if ... }}` ternaries in `health.html`/`provider_row.html`/`commit_progress.html` into one macro.
  - Buttons: bare `<button>` is styled as a good default (secondary look); primary/danger are opt-in via `.btn-primary`/`.btn-danger`. Not every button has an explicit variant class (the "grep check" criterion is satisfied by the element-default styling, not by class coverage) — revisit if you want strict class coverage.
  - Anti-regression guard: `tests/test_web_app.py::test_terminal_idiom_is_gone_from_chrome` fails if `[OK]/[WARN]/[FAIL]` reappears on any page. Keep it green.
  - Real htmx now ships (was a 148-byte placeholder) — browser interactions actually work; verify in a real browser, not just TestClient.
  - **P16-05 is largely pre-done by Phase 13 P13-03** (section order + descriptions + sticky save bar already in `settings.html`). P16-05 remaining = add an anchor-jump **section nav** and give each section an `id`. Small.
  - **P16-04 remaining** = the template-lint grep test for `hx-target="body"` + redirect (none exist today — P12-01 fixed the only one), plus optionally a shared success banner. Small.
  - **P16-06** (storage-path `.env` generator) and **P16-07** (screenshots — needs an agent that can commit binaries) are untouched.
