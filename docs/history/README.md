# History

**Superseded planning material, kept intact.**

Nothing in this directory is current. It is here because the decisions in it
are why LibrAIry is shaped the way it is, and losing that is worse than the
mild risk of somebody quoting a stale line. Read it as a record of what was
decided and when — never as instructions.

The current documents are:

- [../PRODUCT.md](../PRODUCT.md) — what LibrAIry is, its principles and its
  safety invariants
- [../ROADMAP.md](../ROADMAP.md) — the current plan of work
- [../architecture/](../architecture/) — the permanent technical principles

---

## What is here

### [plan/](plan/) — the completion plan, 2026-07-21 → 2026-08-31

Seventeen numbered phases that took LibrAIry from a bash prototype to a
published `v1.3.1`, plus the execution prompt that drove them and three
investigation write-ups.

| | |
|---|---|
| [plan/README.md](plan/README.md) | the master overview and phase map |
| `plan/phase-1` … `phase-9` | the original v1.0 build: engine, classification, AI, dedup, web, review, search, release, fast-follows |
| `plan/phase-10` … `phase-17` | post-1.0: release acceptance, portal fixes, themes, screen redesigns, catalogs, design system, adaptive dashboard |
| `plan/phase-11-tui.md` | **deferred, not cancelled** — see ROADMAP.md |
| [plan/EXECUTION-PROMPT.md](plan/EXECUTION-PROMPT.md) | how a phase was handed to an agent |
| `plan/audit-*.md` | three investigations: browse root counts, catalogs and UI, existence lifecycle |

### What was promoted out of it

The lower half of `plan/README.md` — the permanent principles added between
2026-08-25 and 2026-08-31 — was the most valuable planning text in the
repository and did not belong in an archive. It now lives in
[../PRODUCT.md](../PRODUCT.md) and [../architecture/](../architecture/). The
original copies remain below, unedited.

`adoption-architecture.md` was moved out of `plan/` to
[../architecture/adoption-architecture.md](../architecture/adoption-architecture.md),
because live code points at it.

---

## Known-stale statements in here

Listed so that nobody has to discover them the hard way. These are **not**
corrected in place — correcting an archive makes it a worse record.

- **`SCHEMA_VERSION = 10`** appears throughout `plan/README.md` and several
  phase docs as the authoritative schema. It was, in July 2026. The schema is
  47, and `src/librairy/db.py` is the only authority.
- **The phase map disagrees with the phase docs.** `plan/README.md` lists
  phase 17 as `NOT STARTED` while `plan/phase-17-adaptive-dashboard.md` records
  P17-01 through P17-04 as landed. The phase docs were maintained; the map was
  not.
- **"The existing library is READ-ONLY"** — safety invariant 3 in the
  `context-boilerplate v1` block, embedded identically in every phase doc.
  It stopped being true: adoption moves files inside the library, audits
  propose re-filing, normalization renames. The invariant that was always
  *meant* is in [../PRODUCT.md](../PRODUCT.md): nothing moves without Commit.
- **The 2026-07-21 repository audit** in `plan/README.md` describes a bash
  pipeline, a `sleep infinity` dashboard stub, and "no web UI, no database, no
  tests, no CI". All of that is four hundred commits ago.
- **Phase status lines** stopped being updated once work moved past the phase
  model. Five phases read `IN PROGRESS` with nothing outstanding but
  screenshots.
