# Phase 13 — Theme System + Settings UX (v1.1)

**Status:** NOT STARTED
**Depends on:** Phase 12 (portal fixes) and Phase 10 complete (v1.0.0 tagged)
**Size:** M

---

## Product Context

<!-- context-boilerplate v1 -->
<!-- CANONICAL COPY lives in docs/plan/README.md. Do not edit here; if decisions change, the canonical copy is updated and propagated. -->

**LibrAIry** is a self-hosted, privacy-first, AI-assisted file organizer and library manager. It ships as a single Docker container for NAS systems (UNRAID is the primary target) and desktop workstations. The user drops messy files into an **inbox** folder; LibrAIry analyzes them continuously in the background and proposes clean names and destinations inside an organized **library**; the user reviews proposals in batches from a lightweight LAN web portal (approve / edit / reject / postpone); only then does LibrAIry move files. It is an *orchestrator*, leaning on proven external tools (ffprobe, exiftool, fpcalc/Chromaprint, rmlint, czkawka) and free catalog APIs (MusicBrainz, AcoustID, TMDB), using AI only when deterministic evidence is insufficient — local AI (Ollama) by default, cloud AI strictly opt-in.

**Safety invariants (non-negotiable; enforced in code and by tests):** LibrAIry NEVER deletes user files; NEVER overwrites (deterministic collision renames); the existing library is READ-ONLY; analysis never mutates the filesystem — only the commit engine moves files, executing exactly an approved, immutable, hash-verified plan; every destination is containment-validated (traversal/absolute/symlink escapes fail closed); quarantine is reversible; v1 renames/moves only; every operation is journaled and undoable; privacy is local-first with structural redaction and per-provider cloud opt-in.

**Locked product decisions (do not reopen):** one container, web + worker under a Python supervisor; single-admin LAN portal (scrypt, SQLite sessions, CSRF, rate limiting); taxonomy `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/ Misc/`; SQLite WAL + FTS5 (no Postgres, no Elasticsearch); Python 3.11+, FastAPI + uvicorn + Jinja2 + HTMX, vanilla CSS/JS (no Node build), raw stdlib sqlite3 (no ORM), Pydantic, pytest, ruff, GitHub Actions; Ollama default with per-provider cloud opt-in; duplicates → reversible quarantine only; portal is "a lightweight dashboard and review tool, NOT a file manager"; no microservices/queues/plugin system/Kubernetes.

<!-- end context-boilerplate -->

---

## Decision amendment recorded by this phase

The "UI style: Fallout Pip-Boy retro-terminal" locked decision is **amended by the owner** (2026-07-23): after real use, green-on-black is not friendly enough for daily use. The product moves to a **theme system**: multiple retro presets selectable in Settings, default **`beige-box`** (warm 1990s desktop computing — the owner's nostalgia era; NOT pure black), Pip-Boy preserved as a preset. Readability and user-friendliness win every tie with nostalgia. Everything else in the stack decision stands: vanilla CSS, no Node build, no CSS framework.

## Phase goal

A tokenized stylesheet with six switchable retro presets + a background color picker, persisted in settings and applied without restart — plus a reorganized, self-explanatory Settings screen with an unmissable save flow.

## In scope

Stylesheet tokenization; `data-theme` presets; appearance settings (theme + background color) in `settings_service`; settings screen information architecture; screenshots refresh.

## Out of scope (tempting, but NO)

- Per-screen redesigns (Phase 14). Custom font loading (system monospace stack only). Dark/light auto-switching by OS preference (a preset choice is enough for a single-admin LAN tool). Theme editor/import (six presets + background picker is the whole surface). No CSS framework, no build step.

## Design constraints binding this phase

- **Tokens first (P13-01):** `web/static/pipboy.css` (225 lines) has 5 `:root` vars (`--phosphor`, `--phosphor-dim`, `--amber`, `--bg`, `--panel-border`) but many hardcoded values: body gradient `#12361a` (:15), input bg `#020703` (:82), assorted `rgba()` literals; thumbnail SVG colors are hardcoded in Python (`web/thumbs.py:127-134`). Define a full token set — suggested: `--bg`, `--bg-panel`, `--bg-input`, `--text`, `--text-dim`, `--accent`, `--accent-2`, `--border`, `--ok`, `--warn`, `--fail`, `--radius`, `--bevel` (border style token for the 90s look) — and replace every literal. Thumbs SVGs take their two colors from the active palette (simplest: pass current theme colors into the SVG generation via the settings lookup already available at request time). This task is a **zero-visual-change refactor**: pipboy values become the `:root` defaults, and a screenshot diff before/after should show nothing.
- **Presets (P13-02):** implemented purely as `[data-theme="<name>"]` variable-override blocks in the same CSS file; `<html data-theme="...">` rendered by `base.html` from a persisted setting. Six presets:
  | name | vibe | key colors (tune for AA contrast, don't treat as literal) |
  |---|---|---|
  | `beige-box` **(default)** | Win95/PC-beige era desktop, daylight-friendly | warm beige `#d8d0c0` bg, panel `#e8e2d4`, charcoal text `#26241f`, teal accent `#1f6f6b`, navy accent-2 `#2b3a67`, bevel borders |
  | `platinum-gray` | Mac OS 8 Platinum | cool grays `#d4d4d8`/`#e9e9ec`, near-black text, muted blue accent |
  | `crt-amber` | late-80s amber terminal (dark but NOT pure black) | warm charcoal `#171310` bg, amber `#ffb000` text, dim amber `#8a6a1f` |
  | `dos-blue` | Norton Commander / Turbo Pascal | deep blue `#0000a8`-family bg, white/cyan text, yellow accent |
  | `vaporwave` | 90s through retrowave lens | dark navy/purple bg, pink `#ff6ec7` + cyan `#4de0e0` accents |
  | `pipboy-green` | the original v1.0 look, verbatim | current values, preserved exactly |
  Every preset must pass **WCAG AA (4.5:1)** for body text and 3:1 for large/accent text — check with a contrast tool during implementation and record the ratios in the open-questions log; adjust colors to pass rather than shipping pretty-but-unreadable.
- **Background picker:** one `<input type="color">` overriding only `--bg` (rendered as an inline CSS variable on `<html>`); "Reset to theme default" control; help text warns that extreme choices can hurt contrast. No other per-token customization.
- **Persistence:** two settings keys via `settings_service` (pattern: `save_settings` / `RuntimeSettingsView`, settings_service.py:37-46,140-243): `appearance.theme` (enum of the six names; invalid values fall back to default) and `appearance.background` (hex string or empty = theme default). Applied on next page render — no restart, no htmx trickery needed.
- **Settings IA (P13-03):** single page, grouped sections in this order: **Appearance · Organization templates · AI providers · Duplicates · Content search · Backup · Storage paths (read-only, from P12-06) · Catalog keys status**. Each section: one-line plain-language description. A **sticky save bar** appears only when the form is dirty ("Unsaved changes — [SAVE SETTINGS] [discard]") — a few lines of vanilla JS listening for input/change events; no framework. Keep the P12-01 save flow (HX-Redirect + saved banner).
- **Screenshots (P13-04):** capture dashboard + review + settings in `beige-box` into `docs/images/`, reference from README (discharges the P8-05 promised-screenshots gap; update the phase-8 open-questions note).

## Backlog items

### P13-01 Tokenize the stylesheet (zero visual change)
**Depends on:** — | **Size:** S
- [ ] Every color/border/radius literal in `pipboy.css` replaced by a token; thumbs SVG colors driven by the palette.
- [ ] Before/after screenshots identical (manual check recorded); suite green.

### P13-02 Six presets + background picker, persisted
**Depends on:** P13-01 | **Size:** M
- [ ] `data-theme` blocks for all six presets; `<html>` attribute rendered from `appearance.theme`; picker overrides `--bg`; reset control works.
- [ ] AA contrast ratios recorded per preset in the open-questions log.
- [ ] Settings keys round-trip (tests via `settings_service`); invalid theme name falls back to `beige-box`.
- [ ] Switching theme in the running container changes every screen on next navigation; Pip-Boy preset is pixel-faithful to v1.0.

### P13-03 Settings information architecture + sticky save bar
**Depends on:** P13-02 | **Size:** M
- [ ] Sections/order as specified with descriptions; dirty-state save bar appears/disappears correctly (vanilla JS only).
- [ ] Owner drill: change one value in each section, save once, all persist, confirmation shown.

### P13-04 Screenshots + docs refresh
**Depends on:** P13-03 | **Size:** XS
- [ ] `docs/images/` screenshots in beige-box referenced from README; phase-8 screenshot note closed; release as part of v1.1.0.

## Verification steps

1. Per task: `ruff check src tests scripts && pytest` green; manual drill in the running container.
2. P13-02: cycle all six presets + a custom background live; verify `[OK]/[WARN]/[FAIL]` status colors stay distinguishable in every preset.

## Exit gate checklist

- [ ] Default install greets a new user with beige-box; all six presets + picker work; Pip-Boy preserved.
- [ ] No new dependencies, no build step, CSS still one file.
- [ ] Settings screen self-explanatory (owner sign-off) — v1.1.0 ships with Phase 14 or standalone, owner's call.

## Open questions log

*(Executing agent: record ambiguities and the safest-default decision taken, then continue.)*
