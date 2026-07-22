# LibrAIry Operations Guide

## Setup

1. Copy `.env.example` to `.env`.
2. Set the host paths for inbox, library, quarantine, and appdata.
3. Optionally configure catalog API keys and AI providers.
4. Run `docker compose build`.
5. Run `docker compose up -d`.

The container starts `librairy worker`, which continuously scans and stages proposals. It never commits changes automatically.

## Required Host Paths

```text
HOST_INBOX_DIR=/mnt/nas/inbox
HOST_LIBRARY_DIR=/mnt/nas/library
HOST_QUARANTINE_DIR=/mnt/nas/quarantine
HOST_APPDATA_DIR=/mnt/nas/appdata
```

Inside the container these are always mounted at:

```text
/data/inbox
/data/library
/data/quarantine
/data/appdata
```

## Common Commands

Run one worker cycle:

```bash
docker compose run --rm librairy librairy worker --once
```

List proposals:

```bash
docker compose run --rm librairy librairy proposals list
```

Create a plan from proposals:

```bash
docker compose run --rm librairy librairy propose-plan
```

Approve and commit:

```bash
docker compose run --rm librairy librairy plan approve <plan-id>
docker compose run --rm librairy librairy commit <plan-id> --yes
```

Undo a committed plan:

```bash
docker compose run --rm librairy librairy undo --plan <plan-id> --yes
```

List and restore quarantine entries:

```bash
docker compose run --rm librairy librairy quarantine list
docker compose run --rm librairy librairy quarantine restore <entry-id>
```

Inspect AI providers:

```bash
docker compose run --rm librairy librairy ai status
docker compose run --rm librairy librairy ai test ollama-primary
```

## Worker Behavior

- Scans the inbox and skips unstable files.
- Hashes changed files with BLAKE2b.
- Detects exact duplicates using fingerprints and rmlint when enabled.
- Uses czkawka to flag similar media for review when available.
- Analyzes discovered files up to `BATCH_SIZE` per cycle.
- Persists progress in SQLite `worker_state`.
- Handles SIGTERM/SIGINT by finishing the current cycle and exiting cleanly.

## AI Configuration

Ollama defaults to `http://host.docker.internal:11434` with `qwen3:4b` and `qwen3:8b` endpoint entries. Cloud providers are disabled unless both an API key exists and the provider is explicitly enabled in the settings DB.

## Development Verification

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/pytest
docker compose config
```

If Docker is available, also run:

```bash
docker build .
docker compose run --rm librairy librairy --help
```
