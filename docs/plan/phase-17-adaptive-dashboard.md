# Phase 17 — Adaptive Dashboard UI: Responsive Shell, Explorer Browse, Decision-First Review

**Status:** IN PROGRESS — P17-01/02/03/04 landed (2026-07-24); P17-05 (polish + screenshots) next
**Depends on:** Phase 16 (design system) and Phase 14 (screen content) — this reshapes *layout and density*, not the data.
**Size:** L

---

## Product context

Canonical context lives in [README.md](README.md). Read the `context-boilerplate v1` block there plus the amendment below.

## Decision amendment recorded by this phase

Owner feedback (2026-07-24), after using the real inbox on macOS:

> "I would like to use the most of the screen… add more intelligence, and adaptability, and some of the best design ideas when there is multiple screens, from iPad, to laptop, to 32 inch monitor. Rarely will be open on my phone, but I would like the option."
> "I like the idea of more like a full operational dashboard… something like [glance](https://github.com/glanceapp/glance) — please use it as context to understand what I'm saying. Don't copy paste, I'm just not happy with the style and design."
> "BROWSE… I would like a better experience, more like a regular explorer… columns to move across the files with the arrows… then a small preview."
> "REVIEW… more friendly and easy to make decisions. Maybe using colors, alerts, simple useful things."

**Direction:** LibrAIry becomes an **operational dashboard**, not a centred document. Glance's *ideas* to borrow — density without clutter, a column-based widget layout that reflows by breakpoint, everything important visible without scrolling, quiet borders and strong hierarchy. Its *implementation* is not copied; LibrAIry stays vanilla CSS + htmx with the Phase-13 theme tokens.

## In scope

Layout/shell responsiveness across four breakpoints; a widget-based dashboard; a Miller-column explorer for Browse; a decision-first Review; density and colour semantics tuned for scanning.

## Out of scope (tempting, but NO)

- Any change to the engine, safety invariants, or data functions. Presentation only.
- JS frameworks, CSS frameworks, build steps, drag-and-drop dashboard editing, user-configurable widget layouts (that is glance's job, not v1's).
- Opening or playing files from Browse; Browse stays read-only.

## Design constraints binding this phase

- **Breakpoints (four, named):** `compact` < 640px (phone — usable, single column, nav collapses), `medium` 640–1023px (iPad portrait — two columns), `wide` 1024–1599px (laptop — three columns, sidebars appear), `ultra` ≥ 1600px (32" — content grows to a readable cap, extra column, no infinite line lengths). Replace the fixed `.shell { width: min(1040px, …) }` with a fluid container that caps *text* measure while letting *grids* use the width.
- **Density:** add a `--density` scale so `ultra` shows more rows per screen, not just bigger whitespace. Never shrink hit targets below 40px.
- **Dashboard as widgets:** `dashboard.html` becomes a widget grid — search hero (full width), then cards: Review queue (with a primary CTA), Worker/activity, Inbox lifecycle, Disk, AI providers, Recent operations, Backup. Each widget is a `.card` that can span 1–2 columns via a class. Widgets reflow by breakpoint, in priority order (queue and search first on `compact`).
- **Explorer Browse (Miller columns):** category → folder → file panes side by side on `wide`/`ultra`, collapsing to a single pane with breadcrumbs on `compact`/`medium`. Arrow keys move within a pane, Left/Right move between panes, Enter opens, Backspace goes up. Keep the Phase-14 detail panel as the right-most pane with its preview. Still zero write affordances.
- **Decision-first Review:** the point is deciding fast. Each row gets a **confidence colour band** (green ≥0.85 / amber 0.6–0.85 / red <0.6 or no destination), an at-a-glance "what changes" (from → to), and per-row **Approve/Reject** buttons in addition to the batch bar. Sort/filter by confidence so the safe bulk can be approved in one click and the risky remainder reviewed individually. Colour is never the only signal — always pair with text or an icon (accessibility).
- **Accessibility:** the Phase-13 AA contrast test must stay green; every colour-coded state carries a text label; keyboard paths keep visible focus.

## Backlog items

### P17-01 Fluid responsive shell + density scale
**Depends on:** — | **Size:** M
- [x] Four named breakpoints implemented; `.shell` fluid with a text-measure cap; no horizontal scroll at 320px or 2560px.
- [ ] Header/nav collapse cleanly on `compact` (CSS-only) and spread out on `ultra`.
- [x] Density scale applied to tables/lists so `ultra` shows more rows, not just more padding.

### P17-02 Widget dashboard
**Depends on:** P17-01 | **Size:** M
- [x] Dashboard renders as a reflowing widget grid with search hero first and the review queue prominent.
- [ ] Widget order/priority verified at all four breakpoints; the most important widgets come first on `compact`.
- [x] Existing `dashboard_data` reused unchanged; auto-refresh still works.

### P17-03 Explorer Browse (Miller columns)
**Depends on:** P17-01 | **Size:** L
- [x] Multi-pane explorer on `wide`/`ultra`, single pane + breadcrumbs on smaller; the Phase-14 detail panel is the last pane.
- [x] Arrow keys move within a pane, Left/Right across panes, Enter opens, Backspace up; visible focus throughout.
- [x] Read-only invariant test still passes (no `<form>`, `hx-post`, or `<button>` in browse markup).

### P17-04 Decision-first Review
**Depends on:** P17-01 | **Size:** M
- [x] Confidence colour band + text label per row; per-row Approve/Reject alongside the batch bar.
- [x] Filter/sort by confidence; "approve everything above X" works in one action (Approve all confident ≥0.85, with a confirm), plus a "show only the ones needing a look" filter link.
- [x] Contrast test green; no state signalled by colour alone.

### P17-05 Screenshot + polish pass
**Depends on:** P17-02, P17-03, P17-04 | **Size:** S
- [ ] Walk every screen at all four breakpoints; fix overflow/cramping found.
- [ ] Refresh `docs/images/` (also closes the P13-04 / P16-07 screenshot debt).

## Verification steps

1. Per item: `ruff check src tests scripts && pytest` green, plus a manual pass in the browser at 375px / 820px / 1440px / 2560px.
2. After P17-03: drive Browse with the keyboard only, on the owner's real 6.4 GB inbox corpus.
3. After P17-04: review a real batch — the safe bulk approved in one action, the rest decided individually.

## Exit gate checklist

- [ ] No horizontal scrolling and no cramped/empty-feeling screens at any of the four breakpoints.
- [ ] Dashboard reads as an operational overview; Browse feels like an explorer; Review makes decisions fast.
- [ ] No new dependencies, no build step, one stylesheet; suite green; AA contrast maintained.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
- 2026-07-24: created from owner feedback. Glance is a **reference for density and layout ideas only** — no code, markup, or CSS is taken from it, and LibrAIry does not gain a YAML widget config.

- 2026-07-24: **P17-03 exposed a real Browse bug.** The category counts came from `search_fts` over *all* items, but the panes only list committed **library** files — so the owner's inbox of 174 items showed "music 48" next to "No files at this level". `browse_home` and `browse_category` now both filter `items.root = 'library'`, so counts match what Browse can actually show (all zeros until a commit). Regression test: `test_browse_counts_only_committed_library_files`.
- 2026-07-24: the explorer starts focus on the **Folders** pane rather than Categories, since that is where navigation usually begins; the Categories pane is hidden between 1024–1599px to give folders/files/details room, and returns at `ultra`.
