# LibrAIry

Privacy-first file organization and library management for a NAS or workstation.

LibrAIry watches an inbox, analyzes stable files, stages reviewable proposals, and only moves files when an approved plan is committed. It never deletes user files, never overwrites existing destinations, and treats the existing library as read-only input.

## Current Engine

- Python package under `src/librairy/`.
- SQLite database in `/data/appdata`.
- Worker command: `librairy worker`.
- One-shot worker command for cron/tests: `librairy worker --once`.
- Review flow from CLI: `proposals list` -> `propose-plan` -> `plan approve` -> `commit --yes`.
- AI is optional and local-first. Ollama is enabled by default; cloud providers require both an API key and an explicit DB opt-in flag.

## Library Layout

Top-level destinations are:

```text
Music/
Movies/
Shows/
Photos/
Documents/
Books/
Projects/
Misc/
```

Duplicate files are proposed for reversible quarantine under `/data/quarantine/<date>/...`. Similar media is flagged for review only and is never auto-quarantined.

## Quick Start

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

Drop files into the configured inbox. The worker scans, deduplicates, analyzes, and stages proposals.

Inspect proposals:

```bash
docker compose run --rm librairy librairy proposals list
```

Create and approve a plan from proposals:

```bash
docker compose run --rm librairy librairy propose-plan
docker compose run --rm librairy librairy plan approve <plan-id>
```

Commit approved moves:

```bash
docker compose run --rm librairy librairy commit <plan-id> --yes
```

Undo a committed plan:

```bash
docker compose run --rm librairy librairy undo --plan <plan-id> --yes
```

Check AI providers:

```bash
docker compose run --rm librairy librairy ai status
docker compose run --rm librairy librairy ai test ollama-primary
```

## Local Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests scripts
.venv/bin/pytest
```

## Safety Guarantees

- No deletion path exists for user files.
- Destination collisions resolve to deterministic alternate names.
- Commit executes an approved immutable plan, not a recomputed analysis.
- Undo is journaled and hash-verified.
- Cloud AI prompts are structurally redacted and cloud providers are opt-in.
