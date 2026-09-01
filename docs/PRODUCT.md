# LibrAIry — what it is

**An intelligent, highly polished orchestrator for one permanent personal
library.**

Everything enters through the Inbox. LibrAIry understands what arrived, groups
the decisions it implies, preselects the answers the evidence is strong enough
to preselect and holds back the ones it is not — and changes nothing on disk
until you Commit. When the Inbox is quiet it maintains the library it already
holds. It orchestrates copies outward — Backup, Mirror, Offline Backup — from a
Library that stays the source of truth.

It is finished not when the features are present, but when it is trustworthy
enough to use instead of redesign.

It is **not** a project manager, a two-way sync product, an autonomous agent
that changes files without you, an external-drive migration appliance, or an
enterprise document-management platform.

---

## Core principles

1. **Analyze aggressively, change nothing without Commit.** The intelligence
   may grow without limit. The filesystem boundary does not move.
2. **The Inbox is the only front door.** SMB, FTP, a script, a copy from an
   external disk — how a file arrived is not LibrAIry's concern, and no
   source-drive provenance model exists to make it one.
3. **Inbox work outranks everything.** Library maintenance runs on cycles that
   changed nothing, and yields the moment new work arrives.
4. **Decisions are the unit; files are the detail.** A coherent group is one
   decision with an expandable membership — not eighty rows to answer
   separately.
5. **Confidence decides how much work you do**, never whether a person is
   involved.
6. **Evidence is compared, not ranked once.** Deterministic, catalog, model and
   learned evidence each keep their own weight, and disagreement between them
   is shown rather than silently resolved.
7. **Not knowing is a real answer.** Waiting beats guessing — and waiting has
   to be visible, counted, and escapable by hand.
8. **Learned behaviour accelerates Review; it never bypasses it.** A pattern
   becomes a rule because a person promoted it.
9. **The Library is authoritative; copies outward follow it.** LibrAIry never
   deletes at a destination. It reports the divergence and you decide.
10. **Bounded pages, SQL counts, aggregate summaries.** No screen's size grows
    with the database.
11. **Every screen explains, and the page that owns a fix owns the remedy.**
    No `Fix all` anywhere.

---

## Safety invariants

Non-negotiable, enforced in code and by tests.

1. LibrAIry **never deletes user files.** Deletion is a manual human act
   outside the system — including for duplicates. See
   [the delete queue](delete-queue.md).
2. LibrAIry **never overwrites** an existing file. Destination collisions
   produce deterministic alternative names.
3. **Nothing moves without Commit.** Analysis, audit, measurement, relationship
   discovery and decision memory never touch the filesystem; only the commit
   engine moves files, and it executes exactly an approved, immutable,
   hash-verified plan — never a recomputation.

   > This replaces the older invariant "the existing library is READ-ONLY",
   > which stopped being true and was still being quoted. Adoption moves files
   > inside the library, audits propose re-filing, and normalization renames.
   > What was always *meant* by the old sentence is this one: the library is
   > never rearranged behind your back. Say that instead.

4. Every destination path is **containment-validated** — it must resolve inside
   the library or quarantine root. Traversal, absolute paths and symlink
   escapes fail closed.
5. **Quarantine is reversible storage** with recorded history and a restore
   path. It is not deletion.
6. LibrAIry **never rewrites file contents or embedded metadata** in place. A
   representation change produces a new file and is adopted as an explicit,
   reversible decision.
7. Every filesystem operation is **journaled** with enough information to undo
   it, and undo understands the order decisions were taken in.
8. **Privacy is local-first.** Cloud prompts are structurally redacted — no
   absolute paths, no GPS — and every cloud provider is individually opt-in.
9. **Ambiguity is refused, never guessed.** Where the program cannot tell, it
   says so and asks.

---

## Authority — who gets to have an opinion about a file

Five kinds, in a fixed order. Nothing at a lower level may answer a higher
level's question.

| | | overridden by |
|---|---|---|
| **1. Safety invariant** | never overwrite, never delete, revalidate hashes | nothing |
| **2. Explicit user policy** | `music.preferred_format = mp3` | the owner changing it |
| **3. Strong current evidence** | a catalog identity, an ISBN, a DOI | better evidence about the same file |
| **4. Learned suggestion** | "you filed six Honda manuals here" | all of the above |
| **5. Weak heuristic or model cue** | a filename that looks like a year | all of the above |

A learned pattern sits at (4) deliberately: it is a statement about files that
*resembled* this one, where (3) is a statement about *this* one.

---

## Locked product decisions

Settled. Do not reopen without evidence.

- **Deployment.** One Docker container, two processes (web + worker) under a
  small Python supervisor. LAN portal, single admin, no public exposure
  assumed. UNRAID is the primary target; desktop workstations work too.
- **Workflow.** Inbox → continuous background analysis that never stops to ask
  → decisions accumulate → grouped review → Commit executes exactly what was
  approved → committed files are indexed and searchable. Uncertain files stay
  physically in the Inbox.
- **Taxonomy.** `Music/ Movies/ Shows/ Photos/ Documents/ Books/ Projects/
  Misc/`, with user-selectable destination templates per category.
- **Database.** SQLite, WAL, FTS5, embedded. No PostgreSQL. Files are the
  source of truth; the index is rebuildable from the filesystem plus the
  journal.
- **Tech stack.** Python 3.11+ under `src/librairy/`; FastAPI + uvicorn +
  Jinja2 + htmx; vanilla CSS/JS with no build step; raw `sqlite3`; Pydantic for
  settings; pytest + httpx; ruff; GitHub Actions.
- **AI.** A configured local provider is the default — Ollama and LM Studio are
  both first-class, and neither is hard-coded anywhere the other would work.
  Multiple named endpoints supported. Cloud providers are individually opt-in.
  AI is complementary evidence, not a last resort and not an authority.
- **Duplicates.** Exact by BLAKE2b plus an rmlint cross-check; near-identical
  media by czkawka. Proposed for reversible quarantine, never deleted.
- **Search.** Names, metadata, tags and document text via FTS5, entirely local.
  Never Elasticsearch. Media file content is never indexed.
- **Transfers.** rclone performs them. LibrAIry owns policy, configuration,
  state, comparison and orchestration. `sync`, `--delete`, `purge` and `move`
  are never used.
- **UI.** A clean, friendly, conventional web app. Retro looks survive as
  optional colour themes, never as the structural idiom. A dashboard and review
  tool, not a file manager. Friendliness wins every tie.
- **No over-engineering.** No microservices, no brokers or message queues, no
  Elasticsearch, no multi-user roles, no plugin system, no Kubernetes, no
  front-end framework.

**Container layout**, bind-mounted from host paths: `/data/inbox`,
`/data/library`, `/data/quarantine`, `/data/appdata`.

---

## The architecture that must not drift

Each of these is a permanent principle with its own document. They are not
feature notes; they are the shape of the program.

| | |
|---|---|
| [Relationships](architecture/relationships.md) | evidence about files: it explains and warns, and never adds an operation |
| [Format Policy](architecture/format-policy.md) | what you prefer, permit and protect, asked in one place |
| [Decision memory](architecture/decision-memory.md) | learned from completed decisions; suggests, never acts |
| [Undo sequencing](architecture/undo-sequencing.md) | reversing an old decision must not quietly reverse a newer one |
| [Waiting-decision conflicts](architecture/plan-conflicts.md) | two decisions that cannot both be right are refused, never auto-cancelled |
| [Health](architecture/health.md) | derives what needs attention; reads only, repairs nothing, never says "overdue" |
| [Restore reconciliation](architecture/restore-reconciliation.md) | only exact bytes may say a file moved |
| [Withdrawn decisions](architecture/withdrawn-decisions.md) | a withdrawal moved nothing, so it is not History |
| [Adoption](architecture/adoption-architecture.md) | what a representation change inherits, and what it must not |

Shared vocabulary lives in [ui-vocabulary.md](ui-vocabulary.md). The current
plan of work is [ROADMAP.md](ROADMAP.md). Superseded planning material is
archived under [history/](history/README.md) and is not current.
