# LibrAIry — Complete Setup & Operations Guide

This document covers everything needed to install, configure, run, and maintain LibrAIry from scratch. For a high-level overview see [README.md](README.md).

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Getting the Code](#2-getting-the-code)
3. [API Keys Setup](#3-api-keys-setup)
4. [Configuration (.env)](#4-configuration-env)
5. [Docker Setup](#5-docker-setup)
6. [Mac M-Series Local AI Setup (Ollama)](#6-mac-m-series-local-ai-setup-ollama)
7. [First Run](#7-first-run)
8. [Running the Pipeline](#8-running-the-pipeline)
9. [Step-by-Step Reference](#9-step-by-step-reference)
10. [Classification Intelligence](#10-classification-intelligence)
11. [AI Provider Configuration](#11-ai-provider-configuration)
12. [Directory Layout Reference](#12-directory-layout-reference)
13. [Optional: Build czkawka from Source](#13-optional-build-czkawka-from-source)
14. [NAS Deployment Notes](#14-nas-deployment-notes)
15. [Troubleshooting](#15-troubleshooting)
16. [Environment Variable Reference](#16-environment-variable-reference)

---

## 1. System Requirements

### Host machine

| Requirement | Minimum | Recommended |
|---|---|---|
| Docker | 20.10+ | Latest |
| Docker Compose | v2 (plugin) | Latest |
| RAM | 2 GB | 4 GB+ |
| Architecture | amd64 or arm64 | — |
| Storage | Enough for your library | — |

### For local AI (Ollama)

| Requirement | Minimum | Recommended |
|---|---|---|
| RAM | 8 GB | 16 GB+ |
| VRAM (GPU) | None (CPU only) | 8 GB+ VRAM |
| CPU | Any modern x86-64 or ARM | — |

LibrAIry works without any GPU. Ollama runs on CPU — slower but fully functional. Catalog API lookups (TMDB, AcoustID, MusicBrainz) and the heuristics engine run entirely in the container and require no GPU at all.

### Cloud AI (optional)

No hardware requirements. Requires internet access and an API key from the provider.

---

## 2. Getting the Code

```bash
git clone https://github.com/jfrancolopez/LibrAIry.git
cd LibrAIry
```

Project structure after cloning:

```
LibrAIry/
├── .env.example                  ← Config template (copy to .env)
├── Dockerfile                    ← Container image definition
├── docker-compose.yml            ← Service definitions
├── setup.sh                      ← Interactive first-run wizard
├── README.md
├── Instructions.md               ← This file
└── inbox-processor/
    ├── catalog/                  ← Classification intelligence layer
    │   ├── catalog_main.py       ← Entry point (called by step3)
    │   ├── heuristics.py         ← Rule-based pre-classifier (no AI needed)
    │   ├── library_index.py      ← Library consistency index
    │   ├── music_lookup.py       ← Tags + AcoustID + MusicBrainz
    │   ├── video_lookup.py       ← TMDB movie/TV search
    │   └── utils.py              ← Genre maps, shared helpers
    └── scripts/                  ← Pipeline scripts
        ├── main.sh               ← Orchestrator (runs all steps)
        ├── step1_scan.sh         ← rmlint duplicate detection
        ├── step2_hash_audio_video.sh  ← czkawka perceptual dups
        ├── step3_classify.sh     ← Classification (heuristics + catalog + AI)
        ├── step4_dryrun.sh       ← Move preview (no changes)
        └── step5_commit.sh       ← Execute real moves
```

---

## 3. API Keys Setup

### 3.1 TMDB — Movie & TV Metadata

**Cost:** Free  
**Used for:** Identifying movies and TV shows from filenames  
**Rate limit:** 50 req/s (effectively unlimited)

Steps:
1. Create a free account at **[themoviedb.org/signup](https://www.themoviedb.org/signup)**
2. Verify your email
3. Go to **your profile → Settings → API**
4. Click **"Request an API Key"** → choose **"Developer"**
5. Fill in the application form (any reasonable values work for personal use)
6. Copy the **"API Key (v3 auth)"** — it's a 32-character hex string
7. Add to `.env`: `TMDB_KEY=your_key_here`

> The v3 key is a simple query string parameter — no OAuth needed.

---

### 3.2 AcoustID — Audio Fingerprint Lookup

**Cost:** Free  
**Used for:** Identifying audio files by acoustic fingerprint (works even with no tags)  
**Rate limit:** 3 req/s

Steps:
1. Create a free account at **[acoustid.org/login](https://acoustid.org/login)**
2. Once logged in, go to **[acoustid.org/applications](https://acoustid.org/applications)**
3. Click **"Register a new application"**
4. Name it anything (e.g., "LibrAIry"), leave the website blank if you don't have one
5. Click Submit — you'll see a generated **API Key**
6. Add to `.env`: `ACOUSTID_KEY=your_key_here`

> `fpcalc` (Chromaprint) generates the fingerprint locally. AcoustID only receives the fingerprint hash, never the audio file.

---

### 3.3 MusicBrainz — Music Metadata

**Cost:** Free, no registration  
**Used for:** Artist, album, year, genre, track listing after AcoustID lookup  
**Rate limit:** 1 req/s (enforced by `MB_RATE_LIMIT` in `.env`)

No setup required. MusicBrainz is an open database. LibrAIry respects their rate limit automatically. The only configuration is:

```env
MB_RATE_LIMIT=1.1   # seconds between requests — do not set below 1.0
```

---

### 3.4 Google Gemini — AI Fallback (optional, free tier)

**Cost:** Free tier: 15 req/min, 1,500 req/day  
**Used for:** AI classification when heuristics + catalog APIs don't match  

Steps:
1. Go to **[aistudio.google.com/apikey](https://aistudio.google.com/apikey)**
2. Click **"Create API key"**
3. Select or create a Google Cloud project
4. Copy the key
5. Add to `.env`:
   ```env
   GEMINI_API_KEY=your_key_here
   GEMINI_MODEL=gemini-1.5-flash
   ```

---

### 3.5 OpenAI (optional)

**Cost:** Pay-per-use (~$0.001–0.005 per file classified)  
**Used for:** AI fallback, more accurate than most local models  

1. Create account at **[platform.openai.com](https://platform.openai.com)**
2. Go to **API Keys** → **Create new secret key**
3. Add to `.env`:
   ```env
   OPENAI_API_KEY=sk-...
   OPENAI_MODEL=gpt-4o-mini
   ```

---

### 3.6 Anthropic / Claude (optional)

**Cost:** Pay-per-use (~$0.001–0.003 per file classified)  
**Used for:** AI fallback, excellent JSON schema adherence  

1. Create account at **[console.anthropic.com](https://console.anthropic.com)**
2. Go to **API Keys** → **Create Key**
3. Add to `.env`:
   ```env
   ANTHROPIC_API_KEY=sk-ant-...
   ANTHROPIC_MODEL=claude-3-5-haiku-20241022
   ```

---

## 4. Configuration (.env)

The `.env` file is the single source of truth for all configuration. It is read by `docker-compose.yml` and exported into the container at startup.

### Creating .env

**Option A — Interactive wizard (recommended):**

```bash
chmod +x setup.sh
./setup.sh
```

The wizard prompts for every setting, shows defaults, tests API connectivity, and writes `.env` automatically.

**Option B — Manual:**

```bash
cp .env.example .env
# Edit with your preferred editor
nano .env
```

### Critical settings

```env
# ── Where your files actually live on the host ────────────────────
HOST_INBOX_DIR=/mnt/nas/inbox          # Drop files here to process
HOST_LIBRARY_DIR=/mnt/nas/library      # Organized destination
HOST_QUARANTINE_DIR=/mnt/nas/quarantine
HOST_REPORTS_DIR=/mnt/nas/reports

# ── Free catalog APIs ─────────────────────────────────────────────
TMDB_KEY=                              # From themoviedb.org
ACOUSTID_KEY=                          # From acoustid.org

# ── AI fallback order ─────────────────────────────────────────────
AI_PROVIDER_ORDER=ollama,gemini,openai,anthropic
OLLAMA_HOST=http://host.docker.internal:11434
```

> `.env` is listed in `.gitignore` — it will never be committed to git. Your API keys are safe.

For the complete list of every configurable option, see [`.env.example`](.env.example).

---

## 5. Docker Setup

### 5.1 Build the image

```bash
docker compose build
```

This builds a Debian Bookworm image with:
- `ffmpeg` + `ffprobe` — media analysis
- `fpcalc` (chromaprint-utils) — audio fingerprinting
- `rmlint` — hash-based duplicate detection
- `exiftool` — image EXIF metadata
- `python3` — catalog and heuristics scripts
- `jq`, `curl`, `bc`, `coreutils` — shell utilities

Build takes ~2 minutes on first run (package downloads). Subsequent builds use the Docker cache.

### 5.2 Verify the image

```bash
docker compose run --rm librairy bash -c "
  echo '=== Tool versions ===' &&
  ffprobe -version 2>&1 | head -1 &&
  fpcalc -version &&
  rmlint --version &&
  python3 --version &&
  jq --version
"
```

### 5.3 Services defined in docker-compose.yml

| Service | Profile | Purpose |
|---|---|---|
| `librairy` | (default) | Interactive pipeline — run on demand |
| `watcher` | `watch` | Polls inbox every 60s, auto-runs pipeline |
| `dashboard` | `dashboard` | Web UI — Phase 3 placeholder |

```bash
# Default interactive service
docker compose run --rm librairy

# Start watcher in background
docker compose --profile watch up -d

# Check watcher logs
docker compose logs -f watcher

# Stop watcher
docker compose --profile watch down
```

### 5.4 Volume mounts explained

The container always sees these internal paths:

| Internal | Maps to (from .env) | Purpose |
|---|---|---|
| `/data/inbox` | `HOST_INBOX_DIR` | Source files to process |
| `/data/library` | `HOST_LIBRARY_DIR` | Organized output — never reorganized |
| `/data/quarantine` | `HOST_QUARANTINE_DIR` | Duplicates and flagged files |
| `/data/reports` | `HOST_REPORTS_DIR` | JSON reports and logs |
| `/workspace/inbox-processor` | `./inbox-processor/` (read-only) | Pipeline scripts |

The scripts mount is read-only, so edits to `.sh` or `.py` files on the host are immediately reflected in the container without rebuilding.

---

## 6. Mac M-Series Local AI Setup (Ollama)

This section covers running Ollama natively on Apple Silicon (M1/M2/M3/M4) and connecting the LibrAIry container to it. Apple Silicon runs local AI faster than most x86 servers because the unified memory architecture lets the GPU and CPU share RAM — no VRAM ceiling.

### 6.1 Install Ollama on macOS

```bash
# Option A — Homebrew (recommended)
brew install ollama

# Option B — Direct download
# Download from https://ollama.com/download/mac
# Drag Ollama.app to /Applications, then launch it
```

After installation, Ollama runs as a background service at `http://localhost:11434`.

Verify it is running:

```bash
ollama list        # shows installed models
curl http://localhost:11434/api/tags   # should return JSON
```

### 6.2 Best models for a 16 GB M-series Mac

With 16 GB unified memory you can run 7–8 billion parameter models at full precision, or quantized 13B models. These are the recommended choices for LibrAIry's classification task (JSON output, structured reasoning):

| Model | RAM usage | Quality | Speed | Command |
|---|---|---|---|---|
| `llama3.1:8b` | ~5 GB | Excellent | Fast | `ollama pull llama3.1:8b` |
| `qwen2.5:7b` | ~5 GB | Excellent | Fast | `ollama pull qwen2.5:7b` |
| `mistral:7b` | ~5 GB | Very good | Fast | `ollama pull mistral:7b` |
| `gemma3:4b` | ~3 GB | Good | Very fast | `ollama pull gemma3:4b` |
| `phi4-mini:3.8b` | ~2.5 GB | Good | Very fast | `ollama pull phi4-mini:3.8b` |
| `llama3.1:70b-q4` | ~40 GB | Best | Slow (too large) | — |

**Recommended configuration for 16 GB M-series:**

```env
OLLAMA_MODEL_PRIMARY=llama3.1:8b
OLLAMA_MODEL_SECONDARY=qwen2.5:7b
```

> **Why two models?** The pipeline tries the primary first. If it gives a low-confidence result, it automatically tries the secondary. Both fit in 16 GB simultaneously.

Pull both models before your first run:

```bash
ollama pull llama3.1:8b
ollama pull qwen2.5:7b
```

### 6.3 Connect LibrAIry container to Ollama via IP

The Docker container cannot reach `localhost` on your Mac — it needs the host machine's actual network address.

**Option A — Automatic (recommended for most users):**

Docker Desktop for Mac automatically provides `host.docker.internal`:

```env
OLLAMA_HOST=http://host.docker.internal:11434
```

This resolves to your Mac's IP from inside the container. It is configured automatically in `docker-compose.yml` via `extra_hosts`.

**Option B — Use your Mac's LAN IP:**

If `host.docker.internal` doesn't work (older Docker versions, or running Docker Engine without Desktop):

1. Find your Mac's LAN IP:
   ```bash
   ipconfig getifaddr en0       # WiFi
   # or
   ipconfig getifaddr en1       # Ethernet
   # or
   ifconfig | grep "inet " | grep -v 127.0.0.1
   ```

2. Set it in `.env`:
   ```env
   OLLAMA_HOST=http://192.168.1.XX:11434
   ```

**Option C — Ollama on a different machine (e.g., your NAS or a server):**

If Ollama runs on a separate machine on your network:

```env
OLLAMA_HOST=http://192.168.1.100:11434
```

Replace `192.168.1.100` with that machine's IP. No other changes needed.

### 6.4 Allow network access to Ollama

By default Ollama only listens on `localhost`. To reach it from the container, allow it to bind to all interfaces:

```bash
# One-time: set environment variable before starting Ollama
export OLLAMA_HOST=0.0.0.0

# Or permanently: add to your shell profile (~/.zshrc or ~/.bash_profile)
echo 'export OLLAMA_HOST=0.0.0.0' >> ~/.zshrc
source ~/.zshrc

# Then restart Ollama
pkill ollama
ollama serve &
```

Or if using the Ollama macOS app, set it in System Settings → Privacy → Ollama preferences.

### 6.5 Verify connectivity from inside the container

```bash
docker compose run --rm librairy bash -c "
  curl -s http://host.docker.internal:11434/api/tags | jq '.models[].name'
"
```

You should see the names of your installed models. If the command hangs or returns an error, check the `OLLAMA_HOST` setting and that Ollama is listening on `0.0.0.0`.

### 6.6 macOS-specific .env example

```env
# ── Mac M-series + Ollama setup ───────────────────────────────────
HOST_INBOX_DIR=/Users/yourname/librairy/inbox
HOST_LIBRARY_DIR=/Volumes/NAS/library       # or any local folder
HOST_QUARANTINE_DIR=/Users/yourname/librairy/quarantine
HOST_REPORTS_DIR=/Users/yourname/librairy/reports

# Ollama — running natively on this Mac
OLLAMA_HOST=http://host.docker.internal:11434
OLLAMA_MODEL_PRIMARY=llama3.1:8b
OLLAMA_MODEL_SECONDARY=qwen2.5:7b

# AI order: local Ollama first, Gemini (free) as cloud fallback
AI_PROVIDER_ORDER=ollama,gemini

# Free catalog APIs (get keys — see Section 3)
TMDB_KEY=your_tmdb_key_here
ACOUSTID_KEY=your_acoustid_key_here
```

---

## 7. First Run

### 7.1 Prepare your inbox

Copy or move files into `HOST_INBOX_DIR`. The pipeline supports:

- Single files: `movie.mkv`, `song.mp3`, `photo.jpg`
- Folders: `Album Name/` with multiple tracks inside
- Deeply nested chaos: mixed folders with no naming convention
- Hidden files: files starting with `.` are automatically un-hidden during moves
- Archives: `.zip`, `.rar` (classified, not extracted)
- Any combination of the above

### 7.2 Run the pipeline

```bash
docker compose run --rm librairy ./main.sh
```

You'll see:

```
╔══════════════════════════════════════════════╗
║              LibrAIry v2.0                   ║
║     AI-Powered Library Organizer             ║
╚══════════════════════════════════════════════╝

Configuration:
  Inbox     : /data/inbox
  Library   : /data/library
  ...

Catalog APIs:
  ✓ TMDB (movies/TV)
  ✓ AcoustID (audio fingerprint)
  ✓ MusicBrainz (free, always on)

AI Providers (fallback order):
  ✓ Ollama @ http://host.docker.internal:11434
  ✓ Gemini (gemini-1.5-flash)
  – OpenAI (no key)
  – Anthropic (no key)

Starting pipeline at Thu May 21 00:00:00 2026
─────────────────────────────────────────────────

▶ Step 1 — Duplicate scan (rmlint)
  ✓ Done in 3s

▶ Step 2 — Deep duplicate scan (czkawka)
  ✓ Done in 8s

▶ Step 3 — Classify inbox (heuristics + catalog + AI)
  Processing: my-react-app/
    [heuristic] ✓ SoftwareProject — Project markers found: .git, package.json
  
  Processing: Screenshots/
    [heuristic] ✓ Screenshot — 100% of files match screenshot pattern
  
  Processing: Windows Backup 2023/
    [heuristic] ✓ Archive — Folder name matches backup pattern

  Processing: Pink Floyd - Dark Side of the Moon/
    [catalog] ✓ Matched: MusicAlbum → .../Music/Rock/Albums/Pink_Floyd_Dark_Side_Of_The_Moon_1973/

  Processing: The.Matrix.1999.BluRay.mkv
    [catalog] ✓ Matched: VideoBundle → .../Movies/Action/The_Matrix_1999/

  Processing: Unknown_Album_2019/
    🤖 Catalog miss — AI chain: [ollama_primary, ollama_secondary]
    🤖 Trying Ollama (llama3.1:8b)
    ✓ ollama_primary succeeded (confidence: 0.84)
  ...

▶ Step 4 — Dry-run preview
  [preview of all planned moves]

Dry run complete.
Review the output above, then run step 5 to commit moves:

  ./step5_commit.sh
```

### 7.3 Review and commit

Read the dry-run output carefully. If the moves look correct:

```bash
./step5_commit.sh
```

Files are moved to their classified destinations. Low-confidence items (< 0.5) go to `/data/inbox/_review_pending/` instead of being moved.

---

## 8. Running the Pipeline

### Run all steps (recommended)

```bash
./main.sh            # Steps 1–4 (dry run)
./step5_commit.sh    # Step 5 (real moves — run after reviewing dry run)
```

`main.sh` intentionally stops before committing so you can review. This is the safety mechanism.

### Run individual steps

Each step can be run independently:

```bash
./step1_scan.sh           # Duplicate scan only
./step2_hash_audio_video.sh  # Perceptual duplicate scan only
./step3_classify.sh       # Classify inbox items only
./step4_dryrun.sh         # Preview planned moves
./step5_commit.sh         # Execute moves
```

### Re-running after changes

If you edit classification rules or fix an issue, re-run from step 3:

```bash
./step3_classify.sh && ./step4_dryrun.sh
# Review, then:
./step5_commit.sh
```

Step 3 overwrites `step3_summary.json`. Steps 4 and 5 read from this file.

### Processing a specific folder only

```bash
INBOX_DIR=/data/inbox/single_album ./step3_classify.sh
```

### Running for days (large libraries)

LibrAIry is designed for accuracy over speed. For very large inboxes (10,000+ files), the pipeline can run for days. It is safe to interrupt and resume:

- Step 3 uses a checkpoint file. Processed items are skipped on restart.
- The Docker container can be stopped and restarted without losing progress.
- Use the watcher profile to run continuously:
  ```bash
  docker compose --profile watch up -d
  docker compose logs -f watcher
  ```

---

## 9. Step-by-Step Reference

### Step 1 — rmlint Duplicate Scan

**Script:** `step1_scan.sh`  
**Tool:** rmlint  
**Reads:** `/data/inbox`, `/data/library`  
**Writes:** `step1_summary.json`, `rmlint.json`

Scans both inbox and library for exact duplicates (SHA1 hash comparison).

**Logic:**
- If a file in inbox matches a file already in library → inbox copy → quarantine (library version kept)
- If two identical files exist only in inbox → keep one, move others → quarantine
- Reports duplicate groups within the library itself (for visibility, not auto-removed)
- **The existing library is never modified.** It is read-only for comparison.

---

### Step 2 — czkawka Perceptual Duplicate Scan

**Script:** `step2_hash_audio_video.sh`  
**Tool:** czkawka_cli  
**Reads:** `/data/inbox`, `/data/library`  
**Writes:** `step2_summary.json`

Finds perceptually similar files — images that look the same even if re-saved at different quality, near-duplicate videos.

**Note:** czkawka_cli is NOT included in the Docker image due to its long build time (~10 min). See [Section 13](#13-optional-build-czkawka-from-source) for the optional build. Without it, Step 2 exits cleanly with a "not found" message and the pipeline continues.

---

### Step 3 — Classification

**Script:** `step3_classify.sh`  
**Tools:** Python 3, ffprobe, fpcalc, curl  
**Reads:** `/data/inbox`  
**Writes:** `step3_summary.json`, `step3_ai.log`

This is the core step. For each item in the inbox it runs four phases in order, stopping as soon as a confident match is found.

**Phase A — Heuristics** (local, instant, no network, no AI cost):

Analyzes folder names, file names, file-type distributions, and directory structure markers. Classifies obvious cases immediately:

| Pattern | Classification |
|---|---|
| Folder named `Screenshots/` or files named `Screenshot_*.png` | Screenshots collection |
| Folder contains `.git`, `package.json`, `Makefile` | Software project / Code |
| Folder named `*Backup*` or `*Time Machine*` | Archive / Backup |
| Files named `IMG_XXXX.jpg`, `DSC_XXXX.jpg` in `DCIM/` folder | Camera roll / Photos |
| Mostly `.stl` + `.gcode` files | 3D model project |
| Mostly `.epub`/`.mobi` files | Ebook collection |
| Mostly `.ttf`/`.otf` files | Font collection |
| Folder named `Season XX` or `S01` | TV show season |
| Audio files with sequential numbering, no catalog match | Music album (untagged) |

> **Key insight:** If a folder called "Windows Backup 2023" contains thousands of screenshots, LibrAIry understands it's a system backup — not a photo album. The heuristics layer uses the *combination* of folder name + file patterns to make this judgment without any AI.

**Phase B — Library Index** (local, instant, no network):

Before any network call, the library index is consulted. If this artist, movie, or TV show already exists in your library under a specific genre, that genre is enforced — even if the new item would be classified differently.

This is the consistency guarantee: **the same artist always goes to the same genre folder, forever.**

**Phase C — Catalog APIs** (free, fast network):

*For audio files/folders:*
1. Extract ID3/FLAC/AAC tags via ffprobe
2. If `artist` + (`album` or `title`) found → classify with confidence ~0.92
3. If tags incomplete → generate acoustic fingerprint with `fpcalc`
4. Submit fingerprint to AcoustID API → get MusicBrainz recording ID
5. Fetch full metadata from MusicBrainz (artist, album, year, genre)

*For video files/folders:*
1. Parse filename: strip quality tags (`BluRay`, `1080p`, etc.), extract year
2. Detect S01E02 pattern → try TMDB TV search first
3. Otherwise → try TMDB movie search
4. On ambiguous result → try TMDB TV search as fallback

**Phase D — AI fallback** (only if all above phases fail):

- Builds a structured prompt with full analysis JSON
- Tries providers in `AI_PROVIDER_ORDER` order
- Accepts first response that validates and meets confidence threshold
- Falls back to rule-based classification if all AI providers fail

**Hidden file handling:**

Files whose names begin with `.` (hidden files on Unix/macOS) are automatically un-hidden during classification. They are included in the analysis and the resulting file entries have `rename_to` set to the name without the leading dot. The actual rename happens in step 5.

---

### Step 4 — Dry Run

**Script:** `step4_dryrun.sh`  
**Reads:** `step3_summary.json`  
**Writes:** `step4_summary.json`  
**Changes on disk:** None

Simulates every move from the classification results. Outputs a human-readable preview:

```
📦 Processing: /data/inbox/Pink Floyd - Dark Side of the Moon
   Type: MusicAlbum (conf: 0.944)
   FOLDER DEST: /data/library/RAM/Music/Rock/Albums/Pink_Floyd_Dark_Side_Of_The_Moon_1973
   MULTI-FILE MODE (12 files)
     mv 01 Speak To Me.flac → .../01_Speak_To_Me.flac
     mv 02 On The Run.flac  → .../02_On_The_Run.flac
     ...
```

Items with confidence below 0.5 are flagged for review instead of being moved.

---

### Step 5 — Commit

**Script:** `step5_commit.sh`  
**Reads:** `step3_summary.json`  
**Changes on disk:** Yes — moves real files

Executes the moves planned in step 3/4.

**Safety features:**
- `mv -n` (no-clobber) — never overwrites existing files
- Collision handling — appends `_1`, `_2` if destination exists
- Low-confidence items (< 0.5) → `_review_pending/` instead of classified path
- **Hidden files are renamed** (leading `.` stripped) as they are moved
- Empty source folders removed after all files are moved
- Detailed `step5_summary.json` with every action taken
- **The existing library is never touched.** Step 5 only adds new files.

**After commit:** `step3_summary.json` still exists as an audit trail. Delete it or archive it before running the next batch.

---

## 10. Classification Intelligence

### How LibrAIry avoids AI mistakes

LibrAIry is designed so that **AI is a last resort, not the foundation.** Most files are classified without any AI call:

```
Priority chain:
  1. Heuristics       — instant, local, rule-based (handles ~40% of files)
  2. Embedded tags    — instant, local (handles ~30% of audio files)
  3. Catalog APIs     — fast, free network calls (handles ~20% of files)
  4. AI (Ollama)      — only for the remaining ~10%
  5. Cloud AI         — only if Ollama also fails
  6. Fallback rules   — extension-based default path as last resort
```

This means most files never touch AI at all. For a library of 10,000 files you might invoke AI for only ~1,000. This saves time, cost, and avoids the hallucination errors that AI makes on structured classification tasks.

### Context-aware reasoning examples

The heuristics engine uses human-like reasoning based on context:

| Folder | Files inside | Classification |
|---|---|---|
| `Windows Backup 2023/` | Mixed: `.jpg`, `.xml`, `.log`, `.dll` | Archive → `ROM/Archives/Windows_Backup_2023/` |
| `Screenshots/` | `Screenshot_2024-01-15.png` × 200 | Screenshots → `ROM/Images/Screenshots/` |
| `my-website/` | `.git/`, `package.json`, `index.js` | Code → `ROM/Misc/Code/my-website/` |
| `DCIM/` | `IMG_1234.jpg` × 500 | Camera roll → `ROM/Photos/Camera/DCIM/` |
| `Season 3/` | `.mkv` files | TV show season → `RAM/Shows/General/.../Season_03/` |
| `Cool_Prints/` | `part1.stl`, `base.stl`, `job.gcode` | 3D project → `RAM/3dModels/Projects/Cool_Prints/` |
| `My Books/` | `.epub` × 30, `.mobi` × 5 | Ebooks → `ROM/Documents/Books/My_Books/` |

### Library consistency guarantee

The library index (`library_index.json`) tracks every artist, movie, and show that has been organized. On each run:

1. The index is loaded from cache (rebuilt after 24h or if missing)
2. Before classifying, the artist/title is looked up in the index
3. If found → the existing genre folder is **enforced**, confidence is raised to 0.99
4. If not found → normal classification proceeds, result written to index after commit

Example: If Pink Floyd was organized into `Music/Rock/` last month, and a new Pink Floyd album arrives today — even if TMDB or Ollama would classify it as "Alternative" — the index overrides to `Rock`. The structure never drifts.

The index is automatically invalidated and rebuilt from a full library scan if it is more than 24 hours old (configurable via `LIBRARY_INDEX_TTL`).

### Hidden files

Files starting with `.` (dotfiles) are hidden on Unix/macOS systems. LibrAIry automatically detects and un-hides them:

- During step 3, any hidden file gets `unhide: true` in its classification entry
- During step 5, hidden files are renamed (leading `.` removed) when moved
- Example: `.hidden_song.mp3` → `hidden_song.mp3` in the library

---

## 11. AI Provider Configuration

### Priority chain

Classification always tries in this order:

```
1. Heuristics (folder/filename patterns — no network, instant)
2. Embedded metadata tags (no network, instant)
3. Catalog APIs (TMDB, AcoustID, MusicBrainz — free, fast)
4. AI providers (configured order in AI_PROVIDER_ORDER)
5. Rule-based fallback (extension → default path)
```

AI is only invoked when steps 1–3 return nothing useful.

### Configuring the AI order

```env
# Try Ollama first (free, local), then Gemini (free tier), skip the rest
AI_PROVIDER_ORDER=ollama,gemini

# Prefer Anthropic for accuracy, Ollama as cheap fallback
AI_PROVIDER_ORDER=anthropic,ollama

# Cloud-only (no local Ollama)
AI_PROVIDER_ORDER=openai,anthropic,gemini
```

Providers without a configured key are silently skipped. The pipeline never fails because an AI provider is unavailable — it moves to the next one.

### Recommended AI models by use case

**Mac M-series 16 GB (recommended):**
```env
AI_PROVIDER_ORDER=ollama,gemini
OLLAMA_MODEL_PRIMARY=llama3.1:8b
OLLAMA_MODEL_SECONDARY=qwen2.5:7b
GEMINI_MODEL=gemini-1.5-flash
```

**Maximum accuracy (don't care about cost):**
```env
AI_PROVIDER_ORDER=anthropic,openai
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
OPENAI_MODEL=gpt-4o
```

**Best free setup (no paid keys):**
```env
AI_PROVIDER_ORDER=ollama,gemini
OLLAMA_MODEL_PRIMARY=llama3.1:8b
GEMINI_MODEL=gemini-1.5-flash
```

**Low-RAM NAS (4GB RAM, no GPU):**
```env
AI_PROVIDER_ORDER=gemini,openai
# Skip Ollama entirely — CPU inference is too slow for batches on 4GB RAM
```

**NAS with 8–12 GB RAM:**
```env
AI_PROVIDER_ORDER=ollama,gemini
OLLAMA_MODEL_PRIMARY=gemma3:4b     # Small, fast, 3GB
OLLAMA_MODEL_SECONDARY=phi4-mini:3.8b
```

### Confidence threshold

```env
CONFIDENCE_THRESHOLD=0.80
```

Any AI result below this threshold is rejected and the next provider is tried. If all providers fail to meet the threshold, the highest-confidence result above 0.5 is used. Below 0.5, the item goes to the review queue.

---

## 12. Directory Layout Reference

### Inside the container

```
/data/
├── inbox/                    ← SOURCE: files to process
│   └── _review_pending/      ← Low-confidence items awaiting manual review
├── library/                  ← DESTINATION: organized library (never modified)
│   ├── RAM/                  ← Active media
│   └── ROM/                  ← Archive storage
├── quarantine/
│   └── YYYY-MM-DD/           ← Duplicate files by date
└── reports/
    ├── step1_summary.json
    ├── step2_summary.json
    ├── step3_summary.json    ← Main classification output
    ├── step3_ai.log          ← Per-file decision log
    ├── step4_summary.json
    ├── step5_summary.json
    ├── library_index.json    ← Consistency index (auto-generated)
    └── pipeline.log          ← Combined run log

/workspace/
└── inbox-processor/
    ├── catalog/              ← Python intelligence layer
    └── scripts/              ← Pipeline shell scripts
```

### Library path patterns

| Bundle type | Path pattern |
|---|---|
| Music album | `/library/RAM/Music/{Genre}/Albums/{Artist}_{Album}_{Year}/` |
| Music single | `/library/RAM/Music/{Genre}/Singles/{Artist}_{Title}_{Year}/` |
| Music video | `/library/RAM/MusicVideos/{Genre}/Official/{Artist}_{Title}/` |
| Live performance | `/library/RAM/MusicVideos/{Genre}/LivePerformances/{Artist}_{Year}/` |
| Movie | `/library/RAM/Movies/{Genre}/{Title}_{Year}/` |
| TV show episode | `/library/RAM/Shows/{Genre}/{Show}/Season_{NN}/` |
| 3D model | `/library/RAM/3dModels/Projects/{Name}/` |
| Tutorial | `/library/RAM/Tutorials/{Genre}/{Name}/` |
| Software (macOS) | `/library/RAM/Software/macos/{UseCase}/{Name}/` |
| Software project / Code | `/library/ROM/Misc/Code/{Name}/` |
| Photo album | `/library/ROM/Photos/{Subcategory}/{Name}/` |
| Camera roll | `/library/ROM/Photos/Camera/{Name}/` |
| Screenshot | `/library/ROM/Images/Screenshots/` |
| Ebook collection | `/library/ROM/Documents/Books/{Name}/` |
| Font collection | `/library/ROM/Misc/Fonts/{Name}/` |
| Document set | `/library/ROM/Documents/{Subcategory}/{Name}/` |
| Archive / Backup | `/library/ROM/Archives/{Name}/` |
| Unsorted | `/library/RAM/Misc/Unsorted/{Name}` |

---

## 13. Optional: Build czkawka from Source

czkawka provides perceptual duplicate detection (similar images, near-duplicate videos). It requires Rust and takes ~10 minutes to build.

Run this inside a running LibrAIry container or a Debian-based system:

```bash
# Install Rust and build dependencies
apt install -y \
  build-essential pkg-config cmake nasm yasm clang g++ gcc \
  libjpeg-dev libpng-dev libtiff-dev libtag1-dev \
  libaom-dev libdav1d-dev libavif-dev libheif-dev \
  libx264-dev libx265-dev

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source $HOME/.cargo/env

# Clone and build
git clone https://github.com/qarmin/czkawka.git /opt/czkawka
cd /opt/czkawka

# For ARM (NAS, Apple Silicon): set PKG_CONFIG_PATH accordingly
export PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig   # ARM
# export PKG_CONFIG_PATH=/usr/lib/x86_64-linux-gnu/pkgconfig  # x86_64

cargo build --release --bin czkawka_cli -p czkawka_cli

cp target/release/czkawka_cli /usr/local/bin/
chmod +x /usr/local/bin/czkawka_cli
czkawka_cli --version
```

To persist czkawka across container restarts, commit the container to a new image:

```bash
# After building inside the container:
docker commit librairy librairy:with-czkawka

# Update docker-compose.yml to use this image:
# image: librairy:with-czkawka
```

---

## 14. NAS Deployment Notes

### Unraid (primary target)

1. Open the **Apps** tab and install the **Docker Compose Manager** plugin
2. Create a new compose stack, point it to the `LibrAIry` folder
3. Create a share for your library (e.g., `Media`) with the path `/mnt/user/Media/`
4. Edit `.env`:
   ```env
   HOST_INBOX_DIR=/mnt/user/Media/inbox
   HOST_LIBRARY_DIR=/mnt/user/Media/library
   HOST_QUARANTINE_DIR=/mnt/user/Media/quarantine
   HOST_REPORTS_DIR=/mnt/user/appdata/librairy/reports
   ```
5. Schedule runs with the **User Scripts** plugin:
   ```bash
   # Run dry-run nightly
   docker compose -f /mnt/user/appdata/librairy/docker-compose.yml \
     run --rm librairy bash -c "./main.sh"
   ```
6. If Ollama is running on the same Unraid server, find its Docker bridge IP:
   ```bash
   ip route | grep docker
   # Use that IP: OLLAMA_HOST=http://172.17.0.1:11434
   ```

### TrueNAS Scale

1. Go to **Apps → Discover Apps → Custom App**
2. Use the **docker-compose.yml** from this project
3. Set volume paths to your TrueNAS datasets:
   ```env
   HOST_INBOX_DIR=/mnt/pool/media/inbox
   HOST_LIBRARY_DIR=/mnt/pool/media/library
   ```
4. TrueNAS Scale uses K3s (Kubernetes), so Docker Compose runs through a compatibility layer. For best results, use the native **App** interface or run LibrAIry from a VM/jail with Docker installed.

### Ubuntu Server / Generic Linux

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# Clone and configure
git clone https://github.com/jfrancolopez/LibrAIry.git
cd LibrAIry
./setup.sh

# Run
docker compose run --rm librairy ./main.sh
```

Set up a cron job for scheduled runs:
```bash
crontab -e
# Run classification every night at 2am:
0 2 * * * cd /opt/LibrAIry && docker compose run --rm librairy bash -c "./main.sh" >> /var/log/librairy.log 2>&1
```

### Synology DSM

1. Install **Docker** from Synology Package Center
2. Open **Container Manager** → **Project** → **Create**
3. Point to the `LibrAIry` folder as the project path
4. Set up a scheduled task in **Control Panel → Task Scheduler**:
   ```bash
   docker compose -f /volume1/docker/LibrAIry/docker-compose.yml \
     run --rm librairy bash -c "./main.sh && ./step5_commit.sh"
   ```

### Folder path examples by platform

| Platform | Typical library path |
|---|---|
| Unraid | `/mnt/user/Media/library` |
| TrueNAS Scale | `/mnt/pool/media/library` |
| Ubuntu Server | `/mnt/data/library` |
| Synology | `/volume1/Media/library` |
| QNAP | `/share/Media/library` |
| macOS | `/Volumes/NAS/library` or `~/librairy/library` |

---

## 15. Troubleshooting

### "fpcalc not found"

Audio fingerprinting is disabled but classification continues via embedded tags. Install inside the container:

```bash
apt install -y chromaprint-utils
fpcalc -version
```

Or rebuild the Docker image — `chromaprint-utils` is already in the Dockerfile.

### "TMDB key empty — video classification will use AI only"

Set `TMDB_KEY` in `.env`. Re-run `./setup.sh` to verify connectivity.

### Step 3 is very slow

This usually means heuristics + catalog lookups are failing and every item falls through to Ollama AI.

Check the log:
```bash
cat /data/reports/step3_ai.log | grep "heuristic\|📖\|🤖"
```

If you see many `🤖 Catalog miss`, verify your TMDB and AcoustID keys are set correctly.

For audio files, ensure `fpcalc` is working:
```bash
fpcalc /data/inbox/some_song.mp3
```

### Ollama not reachable from container

```bash
# Test from inside the container
curl http://host.docker.internal:11434/api/tags

# If that fails, try the host IP directly
curl http://192.168.1.XX:11434/api/tags
```

If this fails:
- Check Ollama is running: `ollama list` on the host
- Check Ollama is listening on `0.0.0.0` (not just localhost):
  ```bash
  OLLAMA_HOST=0.0.0.0 ollama serve
  ```
- Verify the port: `OLLAMA_HOST=http://host.docker.internal:11434`
- On Linux, `host.docker.internal` may not work — use the host's actual IP instead:
  ```env
  OLLAMA_HOST=http://172.17.0.1:11434
  ```

### Paths look wrong in dry run

Check `step3_ai.log` for the `confidence` value and `reasoning` field. If AI is returning garbage:
- Try a different model: `OLLAMA_MODEL_PRIMARY=qwen2.5:7b`
- Lower the confidence threshold temporarily: `CONFIDENCE_THRESHOLD=0.70`
- Check if the item is ending up in fallback mode (confidence ~0.60)

### Library index not updating after commit

The library index cache (`library_index.json`) refreshes automatically after 24 hours. To force an immediate rebuild:

```bash
rm /data/reports/library_index.json
# Re-run step 3 — the index will be rebuilt from scratch
```

### Artist/show ending up in wrong genre despite library index

The index only overrides when an exact normalized name match is found. Check if the new item's artist name differs slightly (e.g., "The Beatles" vs "Beatles"). The index uses alphanumeric normalization, so "The Beatles" and "Beatles" map to different keys. In this case, add the artist manually to the index or check the existing library folder name.

### Files not moving in step 5

If `step5_commit.sh` runs but nothing moves:
- Check permissions: the Docker user must have write access to `HOST_LIBRARY_DIR`
- Check `step5_summary.json` for the error log
- Run step 4 again and check for `MISSING` warnings in the dry-run output

### Items going to `_review_pending/` unexpectedly

These items had confidence below 0.5. Check why:
```bash
cat /data/reports/step3_summary.json | \
  jq '.[] | select(.confidence < 0.5) | {source: .source_path, confidence: .confidence, reasoning: .reasoning}'
```

### Hidden files not being un-hidden

Hidden file detection runs in the catalog layer. If a hidden file is going through the AI path (not heuristics or catalog), the AI prompt does not explicitly flag it. To force un-hiding for all files starting with `.`, run:

```bash
# Preview hidden files in your inbox
find /data/inbox -name '.*' -not -name '.DS_Store' -not -name '.git'
```

Hidden files classified by heuristics or catalog APIs are always un-hidden automatically.

---

## 16. Environment Variable Reference

| Variable | Default | Description |
|---|---|---|
| `HOST_INBOX_DIR` | (required) | Host path for inbox folder |
| `HOST_LIBRARY_DIR` | (required) | Host path for organized library |
| `HOST_QUARANTINE_DIR` | (required) | Host path for quarantine folder |
| `HOST_REPORTS_DIR` | (required) | Host path for reports/logs |
| `INBOX_DIR` | `/data/inbox` | Container path (do not change) |
| `LIBRARY_DIR` | `/data/library` | Container path (do not change) |
| `QUARANTINE_DIR` | `/data/quarantine` | Container path (do not change) |
| `REPORTS_DIR` | `/data/reports` | Container path (do not change) |
| `TMDB_KEY` | `` | TMDB API key (free) |
| `ACOUSTID_KEY` | `` | AcoustID API key (free) |
| `MB_RATE_LIMIT` | `1.1` | Seconds between MusicBrainz requests |
| `AI_PROVIDER_ORDER` | `ollama,openai,anthropic,gemini` | AI fallback order |
| `CONFIDENCE_THRESHOLD` | `0.80` | Minimum acceptable AI confidence |
| `USE_MULTI_AI` | `true` | Try multiple providers until threshold met |
| `OLLAMA_HOST` | `http://host.docker.internal:11434` | Ollama server URL |
| `OLLAMA_MODEL_PRIMARY` | `llama3.1:8b` | Primary Ollama model |
| `OLLAMA_MODEL_SECONDARY` | `qwen2.5:7b` | Secondary Ollama model (fallback) |
| `OPENAI_API_KEY` | `` | OpenAI key (optional) |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `ANTHROPIC_API_KEY` | `` | Anthropic key (optional) |
| `ANTHROPIC_MODEL` | `claude-3-5-haiku-20241022` | Anthropic model |
| `GEMINI_API_KEY` | `` | Gemini key (optional, free tier) |
| `GEMINI_MODEL` | `gemini-1.5-flash` | Gemini model |
| `AI_TIMEOUT` | `120` | Seconds before AI call times out |
| `MAX_AI_RETRIES` | `2` | Max retries per provider |
| `MAX_FILES_TO_ANALYZE` | `0` | Files analyzed per item (0 = unlimited) |
| `BATCH_SIZE` | `50` | Processing batch size |
| `CATALOG_DIR` | `/workspace/inbox-processor/catalog` | Catalog Python package path |
| `LIBRARY_INDEX_TTL` | `86400` | Seconds before library index is rebuilt (24h) |
| `DASHBOARD_PORT` | `8080` | Web dashboard port (Phase 3) |
| `CZKAWKA_EXTENSIONS` | `jpg,png,...` | Extensions scanned by czkawka |
| `IGNORE_PATTERNS` | `` | Extra patterns to skip (colon-separated) |
