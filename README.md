# LibrAIry

Privacy-first, AI-assisted file organization for a NAS or workstation.

LibrAIry watches an inbox, analyzes stable files, stages reviewable proposals, and moves files only after you approve and commit a plan in the web portal. It never deletes user files, never overwrites existing destinations, and treats the existing library as read-only input.

## 5-Minute Docker Quickstart

```bash
cp .env.example .env
mkdir -p data/inbox data/library data/quarantine data/appdata
docker compose up -d --build
```

Open `http://localhost:8080` and drop files into `data/inbox`. No password is required on a trusted LAN; set one any time in Settings -> Portal Security, or force it at boot with `AUTH_REQUIRED=true`.

## What You Get

- Dashboard, health, review, commit, quarantine, history, search, browse, settings, and provider selector screens.
- SQLite + FTS5 index in appdata, rebuildable with `librairy index rebuild`.
- Local-first AI through Ollama; cloud AI is disabled unless you set a key and explicitly enable the provider.
- Reversible quarantine for duplicates; no delete controls.
- Fallout/Pip-Boy-style lightweight LAN portal.

## Documentation

- [Docker install](docs/install-docker.md)
- [UNRAID install](docs/install-unraid.md)
- [Configuration](docs/configuration.md)
- [LM Studio on your LAN](docs/lm-studio.md)
- [Using LibrAIry](docs/using-librairy.md)
- [The command line](docs/cli.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Security](docs/security.md)
- [Backup and restore](docs/backup-restore.md)
- [One-way backup](docs/backup.md)
- [Content search](docs/content-search.md)
- [Files that belong together](docs/relationships.md)
- [Performance](docs/performance.md)
- [FAQ](docs/faq.md)

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests scripts
.venv/bin/pytest
docker compose config
```

### Looking at a page

DOM assertions pass happily while a page is visibly wrong, so there is a
harness that opens one in a real browser and measures it:

```bash
.venv/bin/python scripts/ui_check.py review
```

It builds a throwaway library containing one of every finding shape,
screenshots the page at 1280px and at a true 375px, and reports anything
sticking out past the edge. Screenshots land in `.dev/`, which is gitignored.

**Headless Chrome is a development validation tool only. It is not part of
LibrAIry production runtime.** There is no browser in the image, no browser
service in Compose, nothing in `src/librairy` that imports the harness, and
nothing that starts a browser on a timer or in the background. The harness
uses whatever Chrome the developer already has, in a temporary profile it
deletes on the way out, and it says so plainly and exits if there is none.
`tests/test_dev_tooling.py` asserts each of those rather than trusting this
paragraph.

## Safety Guarantees

- No deletion path exists for user files.
- Destination collisions resolve to deterministic alternate names.
- Commit executes an approved immutable plan, not a recomputed analysis.
- Undo is journaled and hash-verified.
- Cloud AI prompts are structurally redacted and cloud providers are opt-in.
