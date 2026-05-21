# LibrAIry
**AI-Powered File Organization & Library Manager**

> Drop your chaos into an inbox. LibrAIry sorts, names, deduplicates, and indexes everything — using catalog APIs first, local AI as a fallback, cloud AI only as a last resort. No subscriptions required for the core workflow.

---

## Why LibrAIry?

Most file organizers either move things blindly by extension, or depend entirely on expensive cloud AI for every decision. LibrAIry uses a smarter priority chain:

```
Embedded metadata tags  →  Free catalog APIs  →  Local AI (Ollama)  →  Cloud AI
       (instant)              (MusicBrainz,          (private,            (OpenAI /
                              TMDB, AcoustID)         no cost)          Anthropic /
                                                                          Gemini)
```

Audio fingerprinting identifies music even without tags. TMDB matches movies from filenames. AI only runs when everything else fails. The result: fast, accurate, and cheap classification for the vast majority of files.

---

## Key Features

| Feature | How |
|---|---|
| Smart classification | File type, embedded tags, folder structure, and catalog APIs determine destination |
| Catalog-first | MusicBrainz, AcoustID, TMDB — free APIs handle most music and video without touching AI |
| Audio fingerprinting | Identifies unlabeled or untagged audio via AcoustID + Chromaprint |
| Dry-run safe | Preview every move before anything changes on disk |
| Duplicate detection | rmlint (hash) + czkawka (perceptual) — no AI needed |
| AI orchestration | Choose any combination of Ollama, OpenAI, Anthropic, Gemini — set the order |
| Non-destructive | Existing library structure is never touched — only inbox items are processed |
| Containerized | Runs in Docker, designed for NAS hardware (arm64 + amd64) |
| Portable config | All paths live in `.env` — move to a new drive by changing 4 lines |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         HOST SYSTEM / NAS                       │
│                                                                 │
│  /inbox          /library            /quarantine    /reports    │
│  (drop here)     (organized output)  (dupes/flags)  (JSON logs) │
│      │                 ▲                  ▲               ▲     │
└──────┼─────────────────┼──────────────────┼───────────────┼─────┘
       │   Docker Volume Mounts             │               │
┌──────▼─────────────────────────────────────────────────────────┐
│                    LibrAIry Container                           │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    Pipeline (main.sh)                     │  │
│  │                                                           │  │
│  │  Step 1 ──▶ rmlint          Hash-based duplicate scan    │  │
│  │  Step 2 ──▶ czkawka         Perceptual duplicate scan    │  │
│  │  Step 3 ──▶ Classifier      Catalog APIs → AI fallback   │  │
│  │  Step 4 ──▶ Dry-run         Preview all moves safely     │  │
│  │  Step 5 ──▶ Commit          Execute approved moves       │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │               Catalog Layer (Python)                      │  │
│  │                                                           │  │
│  │  music_lookup.py   → embedded tags → AcoustID → MB       │  │
│  │  video_lookup.py   → filename parse → TMDB search        │  │
│  │  catalog_main.py   → dispatcher (file or folder)         │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │               AI Fallback Chain                           │  │
│  │   (only runs when catalog APIs return no match)           │  │
│  │                                                           │  │
│  │  Ollama (local) → OpenAI → Anthropic → Gemini            │  │
│  │         order configured in .env                         │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
         │                              │
   External APIs                  Ollama host
   (TMDB, AcoustID,               (same machine or
    MusicBrainz)                   network address)
```

---

## Library Structure

Files are organized into two storage zones inside your library:

```
/library
├── RAM/                        ← Active media (frequently accessed)
│   ├── Music/
│   │   ├── Rock/
│   │   │   ├── Albums/         Artist_Album_Year/
│   │   │   └── Singles/        Artist_Title_Year/
│   │   ├── HipHop/
│   │   ├── Jazz/
│   │   └── ...
│   ├── MusicVideos/
│   │   ├── Rock/
│   │   │   ├── Official/       Artist_Title/
│   │   │   └── LivePerformances/
│   ├── Movies/
│   │   ├── Action/             Title_Year/
│   │   ├── Drama/
│   │   └── ...
│   ├── Shows/
│   │   ├── SciFi/              Show_Name/Season_01/
│   │   └── ...
│   ├── Games/                  Platform/Game_Name/
│   ├── Software/               OS/UseCase/App_Name/
│   ├── 3dModels/               Projects/Model_Name/
│   ├── Tutorials/              Topic/Course_Name/
│   └── Misc/
│       ├── Unsorted/
│       └── Mixed/
│
└── ROM/                        ← Archives (less frequently accessed)
    ├── Photos/
    │   ├── Travel/
    │   ├── Events/
    │   └── Personal/
    ├── Documents/              Topic/Document_Set/
    ├── Archives/
    ├── Backups/
    ├── Tags/                   ProjectName/ (#tag routing)
    └── Misc/
        ├── Code/
        └── Configs/
```

---

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/install/) (v2)
- Your media folders on a local drive or NAS
- Two free API keys (5 minutes to get both — links below)

### 1. Clone and configure

```bash
git clone https://github.com/solosoyfranco/LibrAIry.git
cd LibrAIry

# Run the interactive setup wizard
chmod +x setup.sh
./setup.sh
```

The wizard will prompt for your folder paths, API keys, and AI provider preferences, then write a `.env` file and create all required directories.

Alternatively, copy the template and edit manually:

```bash
cp .env.example .env
nano .env   # or your preferred editor
```

### 2. Build the container

```bash
docker compose build
```

### 3. Drop files into your inbox

Copy, move, or rsync anything into the folder you set as `HOST_INBOX_DIR`. Mixed file types, nested folders, random downloads — it handles all of it.

### 4. Run the pipeline

```bash
# Interactive shell inside the container
docker compose run --rm librairy

# Then inside:
./main.sh
```

Or non-interactively in one command:

```bash
docker compose run --rm librairy ./main.sh
```

### 5. Review the dry run, then commit

`main.sh` stops after the dry run (Step 4). Read the output, then if satisfied:

```bash
./step5_commit.sh
```

---

## API Keys — Where to Get Them

All required catalog APIs are completely free. No credit card, no subscription.

### TMDB — Movie & TV Metadata (required for video)

> Identifies movies and TV shows from filenames. Returns title, year, genre, original language.

1. Create a free account at [themoviedb.org](https://www.themoviedb.org/signup)
2. Go to **Settings → API** → Request an API key (choose "Developer")
3. Copy the **API Key (v3 auth)** value into `TMDB_KEY` in your `.env`

**Rate limit:** 50 requests / second — effectively unlimited for this use case.

---

### AcoustID — Audio Fingerprint Lookup (required for untagged audio)

> Submits an audio fingerprint and returns MusicBrainz recording IDs — identifies songs without relying on filename or tags.

1. Create a free account at [acoustid.org](https://acoustid.org/login)
2. Go to **My Applications → Register a new application**
3. Fill in name and description (anything works), copy the **API Key** into `ACOUSTID_KEY`

**Rate limit:** 3 requests / second — plenty for batch processing.

---

### MusicBrainz — Music Metadata (no key required)

> Open music encyclopedia. Returns artist, album, year, genre, track listings. Used automatically after AcoustID lookup and for tag enrichment.

No registration required. Rate limit is 1 request/second — enforced automatically by `MB_RATE_LIMIT` in `.env`.

**MusicBrainz docs:** [musicbrainz.org/doc/MusicBrainz_API](https://musicbrainz.org/doc/MusicBrainz_API)

---

### Google Gemini — AI Fallback (optional, free tier)

> Used as AI fallback when catalog APIs don't match. Free tier is generous enough for personal use.

1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Click **Create API Key**
3. Copy into `GEMINI_API_KEY` in your `.env`

**Free tier:** 15 requests/minute, 1,500 requests/day — more than enough.

---

## AI Provider Guide

LibrAIry treats AI as a fallback, not a crutch. But when it's needed, here's what works best for JSON classification tasks:

### Local — Ollama (recommended, zero cost)

Install Ollama from [ollama.ai](https://ollama.ai) and pull models:

```bash
ollama pull llama3.1:8b      # Best all-around for classification
ollama pull qwen2.5:7b       # Excellent at structured JSON output
ollama pull mistral:7b       # Fast, good for simpler classifications
```

| Model | Speed | Accuracy | Best for |
|---|---|---|---|
| `llama3.1:8b` | Medium | High | Primary — best balance |
| `qwen2.5:7b` | Medium | High | JSON schema adherence |
| `mistral:7b` | Fast | Good | High-volume quick pass |
| `llama3.2:3b` | Fast | OK | Low-VRAM systems |

Set `OLLAMA_HOST=http://host.docker.internal:11434` if Ollama runs on the same machine as Docker. Use the NAS IP if running on a separate device.

---

### Cloud AI (optional fallbacks)

| Provider | Model | Speed | Cost | Best for |
|---|---|---|---|---|
| OpenAI | `gpt-4o-mini` | Fast | ~$0.001/file | Best accuracy per dollar |
| OpenAI | `gpt-4o` | Fast | ~$0.005/file | Highest accuracy |
| Anthropic | `claude-3-5-haiku-20241022` | Very fast | ~$0.001/file | Most reliable JSON output |
| Anthropic | `claude-3-5-sonnet-20241022` | Fast | ~$0.003/file | Complex edge cases |
| Gemini | `gemini-1.5-flash` | Very fast | Free tier | Good free option |
| Gemini | `gemini-1.5-pro` | Medium | Low | Better than Flash |

**Recommendation:** `claude-3-5-haiku` or `gpt-4o-mini` if you want cloud AI — they follow the JSON schema most reliably. `gemini-1.5-flash` is the best free option.

Configure priority order in `.env`:

```env
# Tries Ollama first, then Gemini (free), skips OpenAI/Anthropic if no key
AI_PROVIDER_ORDER=ollama,gemini,openai,anthropic
```

---

## Running Modes

### Interactive pipeline

```bash
docker compose run --rm librairy
# inside container:
./main.sh          # runs steps 1–4 (dry run)
./step5_commit.sh  # commit after review
```

### Run individual steps

```bash
docker compose run --rm librairy ./step1_scan.sh        # duplicates only
docker compose run --rm librairy ./step3_classify.sh    # classify only
docker compose run --rm librairy ./step4_dryrun.sh      # preview moves
```

### Auto-watch inbox (runs on new files)

```bash
docker compose --profile watch up -d
docker compose logs -f watcher
```

### Non-interactive (cron / NAS scheduler)

```bash
docker compose run --rm librairy bash -c "./main.sh && ./step5_commit.sh"
```

---

## Configuration Reference

All settings live in `.env`. The `setup.sh` wizard generates this file interactively.

```env
# Folder paths (host machine)
HOST_INBOX_DIR=/mnt/nas/inbox
HOST_LIBRARY_DIR=/mnt/nas/library
HOST_QUARANTINE_DIR=/mnt/nas/quarantine
HOST_REPORTS_DIR=/mnt/nas/reports

# Free catalog APIs
TMDB_KEY=your_key_here
ACOUSTID_KEY=your_key_here

# AI provider order (tried left to right, skips missing keys)
AI_PROVIDER_ORDER=ollama,gemini,openai,anthropic

# Ollama
OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL_PRIMARY=llama3.1:8b

# Cloud AI (leave blank to skip)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
```

Full reference with every option: [`.env.example`](.env.example)

---

## Pipeline — Step by Step

| Step | Script | Tool | What it does |
|---|---|---|---|
| 1 | `step1_scan.sh` | rmlint | Hash-based duplicate scan across inbox + library. Exact duplicates → quarantine. |
| 2 | `step2_hash_audio_video.sh` | czkawka | Perceptual duplicate scan (similar images, near-duplicate video). |
| 3 | `step3_classify.sh` | Python + APIs | Analyzes each item: embedded tags → catalog APIs → AI. Writes `step3_summary.json`. |
| 4 | `step4_dryrun.sh` | bash | Simulates all moves. Shows exactly what would change. Writes `step4_summary.json`. |
| 5 | `step5_commit.sh` | bash | Executes real moves. Collision-safe. Low-confidence items → review queue. |

### Step 3 Classification — detailed flow

```
For each item in /inbox:
  │
  ├── Python metadata analysis (ffprobe, exiftool)
  │     file types, sizes, embedded tags, track numbers, folder structure
  │
  ├── Catalog lookup (catalog_main.py)
  │   ├── Audio file/folder?
  │   │   ├── Step A: Read embedded ID3/FLAC/AAC tags via ffprobe
  │   │   │           If artist + album/title found → classify (confidence ~0.92)
  │   │   └── Step B: AcoustID audio fingerprint (fpcalc) → MusicBrainz lookup
  │   │               Returns full artist/album/year/genre → classify (confidence ~0.87)
  │   │
  │   └── Video file/folder?
  │       └── Parse filename (strip quality tags, extract year)
  │           → TMDB movie search → TMDB TV search → classify (confidence ~0.85)
  │
  ├── If catalog matched (exit 0) → skip AI entirely ✓
  │
  └── If catalog failed (exit 1) → AI chain
      Try providers in AI_PROVIDER_ORDER order until confidence ≥ threshold
      Ollama → OpenAI → Anthropic → Gemini
      If all fail → rule-based fallback (extension-based routing)
```

### Reports and logs

| File | Contents |
|---|---|
| `/data/reports/step1_summary.json` | Duplicate groups found, files quarantined |
| `/data/reports/step2_summary.json` | czkawka perceptual duplicate results |
| `/data/reports/step3_summary.json` | Full classification results for every item |
| `/data/reports/step3_ai.log` | Per-item classification log with confidence scores |
| `/data/reports/step4_summary.json` | Simulated move plan |
| `/data/reports/step5_summary.json` | Actual move results, errors, skipped files |
| `/data/reports/pipeline.log` | Combined pipeline run log |
| `/data/inbox/_review_pending/` | Low-confidence items awaiting manual review |
| `/data/quarantine/YYYY-MM-DD/` | Detected duplicates |

---

## Portability — Moving to a New System

LibrAIry is designed so moving to a new NAS, new drive, or new machine takes under 5 minutes:

1. Copy the `LibrAIry/` project folder to the new machine
2. Edit `.env` — update the four `HOST_*_DIR` paths to match new mount points
3. `docker compose build`
4. Done — all reports, quarantine, and library structure are preserved exactly

The container has no persistent state. Everything lives in the mounted host folders.

---

## Contributing

Pull requests welcome. Focus areas:

- Catalog API modules for new file types (books via Open Library, comics via ComicVine)
- Better genre normalization and library path rules
- Web dashboard (Phase 3 — see TODO below)
- NAS platform integration guides (Synology, QNAP, Unraid)

Open issues or PRs at [github.com/solosoyfranco/LibrAIry](https://github.com/solosoyfranco/LibrAIry)

---

## TODO — Upcoming Features

### Phase 3 — Web Dashboard
- [ ] SQLite indexer: scan library on startup, index every file with path, type, size, tags, metadata
- [ ] Flask web server running inside the `dashboard` Docker service (port 8080)
- [ ] Library browser: visual grid/list view, filter by type/genre/year, click path to open in file manager
- [ ] Inbox queue viewer: see pending items, approve or reject AI classification before committing
- [ ] Classification decisions log: every file's reasoning, confidence score, and source (catalog/AI/fallback)
- [ ] Override UI: manually correct a classification before committing

### Phase 4 — Enrichment
- [ ] Cover art downloader: fetch missing album artwork from MusicBrainz Cover Art Archive
- [ ] Subtitle downloader: fetch .srt files from OpenSubtitles for movies/TV
- [ ] ID3 tag writer: write corrected metadata back into audio files after classification
- [ ] EXIF-based photo organization: sort photos by GPS location, camera model, date taken
- [ ] Book metadata: Open Library API for PDF/EPUB/MOBI classification

### Phase 5 — Duplicate Intelligence
- [ ] Audio duplicate detection: compare by AcoustID fingerprint, not just hash (catches re-encodes)
- [ ] Video duplicate detection: perceptual hash comparison via czkawka's video mode
- [ ] Image near-duplicate UI: side-by-side comparison in dashboard before quarantine
- [ ] Quality-aware deduplication: keep highest bitrate/resolution, quarantine lower quality copy

### Phase 6 — Automation
- [ ] NAS-native integration: Synology Task Scheduler and QNAP Container Station guides
- [ ] Webhook support: trigger pipeline via HTTP POST (Zapier, n8n, Home Assistant)
- [ ] Scheduled runs: cron-based auto-processing inside the watcher service
- [ ] Notification support: push notification on pipeline completion (ntfy, Pushover, Slack)
- [ ] Step 6 cleanup script: remove empty folders, fix permissions, update indexes

### Long-term
- [ ] Docker image published to Docker Hub (multi-arch: amd64 + arm64)
- [ ] TUI (terminal UI) mode using `rich` or `textual` for interactive review without a browser
- [ ] Plugin system: drop a Python file into `/catalog/plugins/` to add a new file type handler
- [ ] Sync integration: rsync or rclone post-processing to replicate library to cloud/remote

---

## Credits

| Tool | Role | Link |
|---|---|---|
| rmlint | Hash-based duplicate detection | [github.com/sahib/rmlint](https://github.com/sahib/rmlint) |
| czkawka | Perceptual duplicate detection | [github.com/qarmin/czkawka](https://github.com/qarmin/czkawka) |
| Chromaprint / fpcalc | Audio fingerprint generation | [acoustid.org/chromaprint](https://acoustid.org/chromaprint) |
| AcoustID | Fingerprint → MusicBrainz lookup | [acoustid.org](https://acoustid.org) |
| MusicBrainz | Open music encyclopedia | [musicbrainz.org](https://musicbrainz.org) |
| TMDB | Movie & TV metadata | [themoviedb.org](https://www.themoviedb.org) |
| Ollama | Local LLM runtime | [ollama.ai](https://ollama.ai) |
| ffmpeg / ffprobe | Media analysis | [ffmpeg.org](https://ffmpeg.org) |
| ExifTool | Image metadata | [exiftool.org](https://exiftool.org) |

---

## License

MIT — see [LICENSE](LICENSE)
