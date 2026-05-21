# LibrAIry — Complete Setup & Operations Guide

This document covers everything needed to install, configure, run, and maintain LibrAIry from scratch. For a high-level overview see [README.md](README.md).

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Getting the Code](#2-getting-the-code)
3. [API Keys Setup](#3-api-keys-setup)
4. [Configuration (.env)](#4-configuration-env)
5. [Docker Setup](#5-docker-setup)
6. [First Run](#6-first-run)
7. [Running the Pipeline](#7-running-the-pipeline)
8. [Step-by-Step Reference](#8-step-by-step-reference)
9. [AI Provider Configuration](#9-ai-provider-configuration)
10. [Directory Layout Reference](#10-directory-layout-reference)
11. [Optional: Build czkawka from Source](#11-optional-build-czkawka-from-source)
12. [NAS Deployment Notes](#12-nas-deployment-notes)
13. [Troubleshooting](#13-troubleshooting)
14. [Environment Variable Reference](#14-environment-variable-reference)

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

LibrAIry works without any GPU. Ollama runs on CPU — slower but fully functional. Catalog API lookups (TMDB, AcoustID) run entirely in the container and require no GPU.

### Cloud AI (optional)

No hardware requirements. Requires internet access and an API key from the provider.

---

## 2. Getting the Code

```bash
git clone https://github.com/solosoyfranco/LibrAIry.git
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
    ├── catalog/                  ← Free catalog API lookup layer
    │   ├── catalog_main.py       ← Entry point (called by step3)
    │   ├── music_lookup.py       ← Tags + AcoustID + MusicBrainz
    │   ├── video_lookup.py       ← TMDB movie/TV search
    │   └── utils.py              ← Genre maps, shared helpers
    └── scripts/                  ← Pipeline scripts
        ├── main.sh               ← Orchestrator (runs all steps)
        ├── step1_scan.sh         ← rmlint duplicate detection
        ├── step2_hash_audio_video.sh  ← czkawka perceptual dups
        ├── step3_classify.sh     ← Classification (catalog + AI)
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
**Used for:** AI classification when catalog APIs don't match  

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
- `python3` — catalog scripts
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

### 5.5 Ollama connectivity

If Ollama is running on the same machine as Docker:

```env
OLLAMA_HOST=http://host.docker.internal:11434
```

`host.docker.internal` resolves to the Docker host's IP. This is configured in `docker-compose.yml` via `extra_hosts`.

If Ollama runs on a different machine (e.g., a separate server or NAS):

```env
OLLAMA_HOST=http://192.168.1.100:11434
```

---

## 6. First Run

### 6.1 Prepare your inbox

Copy or move files into `HOST_INBOX_DIR`. The pipeline supports:

- Single files: `movie.mkv`, `song.mp3`, `photo.jpg`
- Folders: `Album Name/` with multiple tracks inside
- Deeply nested chaos: mixed folders with no naming convention
- Archives: `.zip`, `.rar` (classified, not extracted)
- Any combination of the above

### 6.2 Run the pipeline

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

▶ Step 3 — Classify inbox (catalog + AI)
  Processing: Pink Floyd - Dark Side of the Moon/
    📖 Catalog matched (confidence: 0.944) — skipping AI
    → Pink_Floyd_Dark_Side_Of_The_Moon_1973 (MusicAlbum, confidence: 0.944)

  Processing: The.Matrix.1999.BluRay.mkv
    📖 Catalog matched (confidence: 0.912) — skipping AI
    → The_Matrix_1999 (VideoBundle, confidence: 0.912)
  ...

▶ Step 4 — Dry-run preview
  [preview of all planned moves]

Dry run complete.
Review the output above, then run step 5 to commit moves:

  ./step5_commit.sh
```

### 6.3 Review and commit

Read the dry-run output carefully. If the moves look correct:

```bash
./step5_commit.sh
```

Files are moved to their classified destinations. Low-confidence items (< 0.5) go to `/data/inbox/_review_pending/` instead of being moved.

---

## 7. Running the Pipeline

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

If you edit classification rules or fix an issue with the AI prompt, re-run from step 3:

```bash
./step3_classify.sh && ./step4_dryrun.sh
# Review, then:
./step5_commit.sh
```

Step 3 overwrites `step3_summary.json`. Steps 4 and 5 read from this file.

### Processing a specific file only

Run step 3 with `INBOX_DIR` pointing to a single directory:

```bash
INBOX_DIR=/data/inbox/single_album ./step3_classify.sh
```

---

## 8. Step-by-Step Reference

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

**Output JSON:**
```json
{
  "timestamp": "2026-05-21T...",
  "duplicates_found": 12,
  "files_quarantined": 10,
  "library_duplicates": [],
  "quarantine_dir": "/data/quarantine/2026-05-21",
  "quarantine_size": "2.3G"
}
```

---

### Step 2 — czkawka Perceptual Duplicate Scan

**Script:** `step2_hash_audio_video.sh`  
**Tool:** czkawka_cli  
**Reads:** `/data/inbox`, `/data/library`  
**Writes:** `step2_summary.json`

Finds perceptually similar files — images that look the same even if re-saved at different quality, near-duplicate videos.

**Note:** czkawka_cli is NOT included in the Docker image due to its long build time (~10 min). See [Section 11](#11-optional-build-czkawka-from-source) for the optional build. Without it, Step 2 exits cleanly with a "not found" message and the pipeline continues.

---

### Step 3 — Classification

**Script:** `step3_classify.sh`  
**Tools:** Python 3, ffprobe, fpcalc, curl  
**Reads:** `/data/inbox`  
**Writes:** `step3_summary.json`, `step3_ai.log`

This is the core step. For each item in the inbox:

**Phase A — Analysis** (Python, no network):
- Walks the file/folder structure
- Collects: file types, extensions, sizes, dates, track numbers, subfolder names
- Extracts embedded metadata (ffprobe for audio/video, exiftool for images)
- Scores bundle coherence (what fraction of files share the dominant type)

**Phase B — Catalog lookup** (Python, free APIs):

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

**Phase C — AI fallback** (only if catalog fails):
- Builds a structured prompt with full analysis JSON
- Tries providers in `AI_PROVIDER_ORDER` order
- Accepts first response that validates and meets confidence threshold
- Falls back to rule-based classification if all AI providers fail

**Phase D — Path construction:**
- Normalizes genre (e.g., "hip-hop" → "HipHop")
- Builds destination path from bundle type + genre + name
- Generates per-file rename suggestions (track numbers, sanitized names)

**Output** — `step3_summary.json` is a JSON array:

```json
[
  {
    "bundle_type": "MusicAlbum",
    "suggested_name": "Pink_Floyd_Dark_Side_Of_The_Moon_1973",
    "recommended_path": "/data/library/RAM/Music/Rock/Albums/Pink_Floyd_Dark_Side_Of_The_Moon_1973/",
    "confidence": 0.944,
    "reasoning": "Folder scan: 10 audio files, artist='Pink Floyd' (100%), album='The Dark Side of the Moon'",
    "genre": "Rock",
    "category": "Music",
    "storage_zone": "RAM",
    "files": [
      {
        "original_name": "01 Speak To Me.flac",
        "rename_to": "01_Speak_To_Me.flac",
        "recommended_path": "/data/library/RAM/Music/Rock/Albums/Pink_Floyd_Dark_Side_Of_The_Moon_1973/",
        "track_number": 1,
        "category": "Audio"
      }
    ],
    "source_path": "/data/inbox/Pink Floyd - Dark Side of the Moon",
    "is_folder": true
  }
]
```

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
- Empty source folders removed after all files are moved
- Detailed `step5_summary.json` with every action taken

**After commit:** `step3_summary.json` still exists as an audit trail. Delete it or archive it before running the next batch.

---

## 9. AI Provider Configuration

### Priority chain

Classification always tries in this order:

```
1. Embedded metadata tags (no network, instant)
2. Catalog APIs (TMDB, AcoustID, MusicBrainz — free, fast)
3. AI providers (configured order in AI_PROVIDER_ORDER)
4. Rule-based fallback (extension → default path)
```

AI is only invoked when steps 1 and 2 return nothing useful.

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

**Maximum accuracy (don't care about cost):**
```env
AI_PROVIDER_ORDER=anthropic,openai
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
OPENAI_MODEL=gpt-4o
```

**Best free setup:**
```env
AI_PROVIDER_ORDER=ollama,gemini
OLLAMA_MODEL_PRIMARY=llama3.1:8b
GEMINI_MODEL=gemini-1.5-flash
```

**Best balance of cost/accuracy:**
```env
AI_PROVIDER_ORDER=ollama,openai
OLLAMA_MODEL_PRIMARY=llama3.1:8b
OPENAI_MODEL=gpt-4o-mini
```

**Low-RAM NAS (4GB RAM, no GPU):**
```env
AI_PROVIDER_ORDER=gemini,openai
# Skip Ollama entirely — GPU-free CPU inference is too slow for batches
```

### Confidence threshold

```env
CONFIDENCE_THRESHOLD=0.80
```

Any AI result below this threshold is rejected and the next provider is tried. If all providers fail to meet the threshold, the highest-confidence result above 0.5 is used. Below 0.5, the item goes to the review queue.

---

## 10. Directory Layout Reference

### Inside the container

```
/data/
├── inbox/                    ← SOURCE: files to process
│   └── _review_pending/      ← Low-confidence items awaiting manual review
├── library/                  ← DESTINATION: organized library
│   ├── RAM/                  ← Active media
│   └── ROM/                  ← Archive storage
├── quarantine/
│   └── YYYY-MM-DD/           ← Duplicate files by date
└── reports/
    ├── step1_summary.json
    ├── step2_summary.json
    ├── step3_summary.json    ← Main classification output
    ├── step3_ai.log          ← Per-file AI decision log
    ├── step4_summary.json
    ├── step5_summary.json
    └── pipeline.log          ← Combined run log

/workspace/
└── inbox-processor/
    ├── catalog/              ← Python catalog package
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
| Photo album | `/library/ROM/Photos/{Subcategory}/{Name}/` |
| Screenshot | `/library/ROM/Images/Screenshots/` (standalone) |
| Document set | `/library/ROM/Documents/{Subcategory}/{Name}/` |
| Archive | `/library/ROM/Archives/{Name}/` |
| Tagged project | `/library/ROM/Tags/{#tag}/` |
| Unsorted | `/library/RAM/Misc/Unsorted/{Name}` |

---

## 11. Optional: Build czkawka from Source

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

## 12. NAS Deployment Notes

### Synology DSM

1. Install **Docker** from Synology Package Center
2. Open **Container Manager** → **Project** → **Create**
3. Point to the `LibrAIry` folder as the project path
4. Set up a scheduled task in **Control Panel → Task Scheduler**:
   ```bash
   docker compose -f /volume1/docker/LibrAIry/docker-compose.yml \
     run --rm librairy bash -c "./main.sh && ./step5_commit.sh"
   ```

### QNAP Container Station

1. Install **Container Station** from App Center
2. Import `docker-compose.yml`
3. Edit the compose file to use absolute paths for volumes (QNAP requires this)

### Unraid

Add the `LibrAIry` project folder to a share. Use the **User Scripts** plugin to schedule pipeline runs. The `docker compose run` command works natively in Unraid's terminal.

### Folder path examples by NAS

| NAS | Typical library path |
|---|---|
| Synology | `/volume1/Media/library` |
| QNAP | `/share/Media/library` |
| Unraid | `/mnt/user/Media/library` |
| Generic Linux | `/mnt/data/library` |
| macOS | `/Volumes/NAS/library` |

---

## 13. Troubleshooting

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

This usually means catalog lookups are failing and every item falls through to Ollama AI.

Check the log:
```bash
cat /data/reports/step3_ai.log | grep "📖\|🤖"
```

If you see many `🤖 Catalog miss`, verify your TMDB and AcoustID keys are set correctly.

For audio files, ensure `fpcalc` is working:
```bash
fpcalc /data/inbox/some_song.mp3
```

### Ollama not reachable

```bash
# Test from inside the container
curl http://host.docker.internal:11434/api/tags
```

If this fails:
- Check Ollama is running: `ollama list` on the host
- Verify the port: `OLLAMA_HOST=http://host.docker.internal:11434`
- On Linux, `host.docker.internal` may not work — use the host's actual IP instead:
  ```env
  OLLAMA_HOST=http://172.17.0.1:11434
  ```

### Paths look wrong in dry run

Check `step3_ai.log` for the `confidence` value and `reasoning` field of each item. If AI is returning garbage:
- Try a different model: `OLLAMA_MODEL_PRIMARY=qwen2.5:7b`
- Lower the confidence threshold temporarily: `CONFIDENCE_THRESHOLD=0.70`
- Check if the item is ending up in fallback mode (confidence ~0.60)

### Files not moving in step 5

If `step5_commit.sh` runs but nothing moves:
- Check permissions: the Docker user must have write access to `HOST_LIBRARY_DIR`
- Check `step5_summary.json` for the error log
- Run step 4 again and check for `MISSING` warnings in the dry-run output

### Items going to `_review_pending/` unexpectedly

These items had confidence below 0.5. Check why:
```bash
cat /data/reports/step3_summary.json | jq '.[] | select(.confidence < 0.5) | {source: .source_path, confidence: .confidence, reasoning: .reasoning}'
```

---

## 14. Environment Variable Reference

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
| `DASHBOARD_PORT` | `8080` | Web dashboard port (Phase 3) |
| `CZKAWKA_EXTENSIONS` | `jpg,png,...` | Extensions scanned by czkawka |
| `IGNORE_PATTERNS` | `` | Extra patterns to skip (colon-separated) |
