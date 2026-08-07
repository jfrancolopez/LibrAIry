# Configuration

Configuration has two layers. Boot-time environment variables define paths, ports, keys, and default models. Runtime web settings live in SQLite and take precedence for supported knobs on the next worker batch.

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `HOST_INBOX_DIR` | Host path where you drop files to organize. |
| `HOST_LIBRARY_DIR` | Host path for organized output and existing read-only library indexing. |
| `HOST_QUARANTINE_DIR` | Host path for reversible duplicate/review quarantine storage. |
| `HOST_APPDATA_DIR` | Host path for SQLite database, settings, thumbnails, and logs. |
| `INBOX_DIR` | Container path for inbox, normally `/data/inbox`. |
| `LIBRARY_DIR` | Container path for library, normally `/data/library`. |
| `QUARANTINE_DIR` | Container path for quarantine, normally `/data/quarantine`. |
| `APPDATA_DIR` | Container path for appdata, normally `/data/appdata`. |
| `TMDB_KEY` | Optional TMDB key for movie/TV metadata. |
| `ACOUSTID_KEY` | Optional AcoustID key for audio fingerprint lookup. |
| `DISCOGS_TOKEN` | Optional Discogs personal token; names untagged audio from its filename. |
| `LASTFM_KEY` | Optional Last.fm key; supplies a genre when the file has none. |
| `MB_RATE_LIMIT` | Minimum seconds between MusicBrainz requests. |
| `AI_PROVIDER_ORDER` | Default AI provider kind order. |
| `CONFIDENCE_THRESHOLD` | Default proposal confidence threshold. DB setting can override. |
| `USE_MULTI_AI` | Whether AI tries multiple providers until threshold is met. |
| `OLLAMA_HOST` | Default Ollama endpoint URL. |
| `OLLAMA_MODEL` | Legacy alias for `OLLAMA_MODEL_PRIMARY`. |
| `OLLAMA_MODEL_PRIMARY` | Default primary Ollama model. |
| `OLLAMA_MODEL_SECONDARY` | Default secondary Ollama model. |
| `LMSTUDIO_HOST` | IP or URL of a machine running LM Studio on your LAN. An IP is enough — `http://` and `:1234` are filled in. Empty disables it. |
| `LMSTUDIO_MODEL` | Model identifier as shown in LM Studio (e.g. `qwen2.5-7b-instruct`). |
| `OPENAI_API_KEY` | Optional OpenAI key. Never rendered in HTML. |
| `OPENAI_MODEL` | OpenAI model name. |
| `ANTHROPIC_API_KEY` | Optional Anthropic key. Never rendered in HTML. |
| `ANTHROPIC_MODEL` | Anthropic model name. |
| `GEMINI_API_KEY` | Optional Gemini key. Never rendered in HTML. |
| `GEMINI_MODEL` | Gemini model name. |
| `MAX_FILES_TO_ANALYZE` | Legacy cap, `0` means unlimited. |
| `AI_TIMEOUT` | AI request timeout seconds. |
| `MAX_AI_RETRIES` | Retry count per AI provider. |
| `BATCH_SIZE` | Default files per worker analysis batch. DB setting can override. |
| `IGNORE_PATTERNS` | Extra ignored filename/path patterns. |
| `CZKAWKA_EXTENSIONS` | Extensions scanned by czkawka. |
| `LIBRARY_INDEX_TTL` | Legacy index TTL, safe to leave default. |
| `DASHBOARD_PORT` | Web portal port inside the app and host mapping default. |
| `FILE_STABILITY_SECONDS` | How long files must stop changing before scanning. |
| `LOG_LEVEL` | Structured log level. Use `DEBUG` only while diagnosing. |
| `LOG_MAX_BYTES` | Rotating log file max size in bytes. |
| `LOG_BACKUP_COUNT` | Number of rotated log files to keep. |
| `CONTENT_SEARCH_ENABLED` | Default for local document text extraction, usually changed in Settings. |
| `CONTENT_EXTRACT_MAX_CHARS` | Maximum extracted characters per document. |
| `BACKUP_ENABLED` | Default one-way rclone backup toggle, usually changed in Settings. |
| `BACKUP_REMOTE` | Default rclone remote destination, e.g. `b2:librairy-backup`. |
| `BACKUP_BANDWIDTH_LIMIT` | Optional rclone bandwidth limit. |
| `BACKUP_SCHEDULE` | When the worker drains the backup queue: `after_commit` (default), `hourly`, `daily`, or `manual`. "Back up now" in Settings overrides all four. |
| `BACKUP_DAILY_AT` | Time of day for the `daily` schedule, in UTC — the container's clock. Default `02:00`. |
| `AUTH_REQUIRED` | `false` (default) leaves the portal open on your LAN with no password. `true` forces first-run password setup and blocks password removal. |
| `BACKUP_INCLUDE_DB_SNAPSHOT` | Whether backup includes a SQLite appdata snapshot. |
| `NORMALIZE_ATTRIBUTES` | `true` (default) clears the macOS hidden flag and settles permissions on each file as it is placed in the library. Runs at the move, never during a scan. |
| `FILE_MODE` | Octal permissions for placed files, default `644`. Empty keeps whatever arrived — right for exFAT and NTFS, where a chmod either fails or lies. |
| `DIR_MODE` | Octal permissions for folders LibrAIry creates, default `755`. Folders that already have this mode are left alone, so a tree you set up by hand is untouched. |
| `PUID` | Container file-owner UID, default `99`. |
| `PGID` | Container file-owner GID, default `100`. |

## Web Settings

These are stored in SQLite and apply without rebuilding the container:

| Setting | Purpose |
| --- | --- |
| `runtime.confidence_threshold` | Overrides `CONFIDENCE_THRESHOLD` for next analysis batch. |
| `runtime.batch_size` | Overrides `BATCH_SIZE` for next worker cycle. |
| `templates.<category>.style` | Destination template style per category. Categories: music, movies, shows, photos, documents, books, projects, misc. |
| `dedup.use_fingerprints` | Toggle exact duplicate detection by BLAKE2b fingerprints. |
| `dedup.use_rmlint` | Toggle rmlint exact duplicate cross-check. At least one exact method must stay enabled. |
| `dedup.use_czkawka` | Toggle near-identical media flagging through czkawka. |
| `ai.provider_order` | Provider kind order for next AI batch. |
| `ai.ollama.endpoints` | Named Ollama endpoints, URLs, models, and enabled flags. |
| `ai.openai.enabled` | Explicit cloud opt-in for OpenAI. Requires key and `CLOUD` confirmation. |
| `ai.anthropic.enabled` | Explicit cloud opt-in for Anthropic. Requires key and `CLOUD` confirmation. |
| `ai.gemini.enabled` | Explicit cloud opt-in for Gemini. Requires key and `CLOUD` confirmation. |
| `content_search.enabled` | Toggle local-only document text extraction for next worker cycle. |
| `backup.enabled` | Toggle one-way rclone copy-out backup. |
| `backup.remote` | rclone remote destination consumed from mounted `rclone.conf`. |
| `backup.bandwidth_limit` | Optional rclone bandwidth limit. |
| `backup.schedule` | Backup schedule mode: `after_commit`, `hourly`, `daily`, `manual`. |
| `backup.daily_at` | Time of day for the `daily` mode, UTC. |
| `backup.categories` | Which categories go off-site. Empty means all of them, including any added in a later release. |
| `backup.include_db_snapshot` | Include a SQLite snapshot in backups. |
| `appearance.theme` | Colour preset for the portal. |
| `appearance.background` | Optional background colour override; empty means the theme's own. |
| `catalog.<slug>.enabled` | Per-catalog on/off switch. Slugs: musicbrainz, acoustid, tmdb, discogs, lastfm, coverart, tvmaze, openlibrary. A catalog that is off makes no requests. |

API keys can be set from the Settings page or from the environment. **The environment
always wins** — a variable in your compose file or UNRAID template is deliberate
configuration, so a key saved in the portal is kept but unused while the variable is
set, and the card says so. Keys are write-only either way: the page reports `set` or
`not set` and never shows a value back.

## Catalogs

Metadata sources consulted **before** AI. Each one is individually switchable on the
Settings page. A catalog that is unreachable, unconfigured, or switched off degrades
silently to the next evidence source.

| Catalog | Identifies | Key | Sends |
| --- | --- | --- | --- |
| MusicBrainz | Music releases, artists, albums | none | Track/album titles, artist names, durations |
| AcoustID | Music by audio fingerprint | `ACOUSTID_KEY` | A fingerprint and duration, not the audio |
| TMDB | Movies and TV shows | `TMDB_KEY` | Cleaned title guesses and years |
| TVmaze | TV shows, and each episode's title | none | Cleaned show titles, season and episode numbers |
| Discogs | Music releases, including vinyl and rare pressings | `DISCOGS_TOKEN` | A cleaned artist/title guess, for files with no readable tags |
| Last.fm | Genres for music that has none | `LASTFM_KEY` | Artist and album names |
| Cover Art Archive | Album art on review cards | none | A release ID, or an artist and album to find one |
| Open Library | Books by title, author or ISBN | none | Cleaned title and author guesses |

No catalog is ever sent a file path.

**Testing a key.** Each catalog card has a **Test it** button. It asks that
catalog one question with a known answer — TMDB about *The Matrix*, TVmaze about
*Breaking Bad*, MusicBrainz about *OK Computer* — and reports what came back.

It exists because a catalog swallows its own errors on purpose: a service being
down must degrade to the next evidence source, not stop an analysis batch. That
makes a rejected key indistinguishable from "never heard of that film". When the
test comes back empty it repeats the request at the HTTP level purely to say
which it was: **Key rejected** (401/403), **Rate limited** (429), **Service is
down** (5xx), **Cannot reach it** (network or DNS), or **Reachable, no match** —
which is a pass, because the connection and the key are both fine.

AcoustID is the exception: it answers questions about audio fingerprints and
nothing else, so its test fingerprints one of your own audio files and looks it
up. With no audio on hand it says so rather than guessing.

**TMDB and TVmaze together.** TMDB is asked first — it needs a key, so having one is a
deliberate choice — and its show name wins any disagreement. TVmaze is also asked, for
episodes only, because it answers something TMDB's search endpoint does not: the
episode's own title, which turns `S03E09.mkv` into
`S03E09 - The Rains of Castamere.mkv`. When both name the same show, confidence rises
above what either source earns alone. TVmaze also stands in for TMDB entirely when TMDB
is unkeyed, switched off, or draws a blank.

**How music evidence is ordered.** Strongest first: embedded tags, then an AcoustID
fingerprint resolved through MusicBrainz, then — with nothing left but the filename — a
Discogs text search. Discogs is only asked when the filename actually names something
beyond the track (`Radiohead - Karma Police.mp3`, not `track01.mp3`), and its answer is
**verified**: the artist it returns must appear in the text that was searched for. An
unverified text hit would confidently rename a file to whatever Discogs listed first,
which is worse than admitting the file is unknown.

Last.fm sits outside that cascade. It never identifies anything; it fills in the genre
once the release is known, and only when the file itself does not say. Genre is the
first path component under the genre-first template, so without it a perfectly
identified album still lands in `Music/General/`.

**Album art.** Audio is the one category with nothing to look at: an image or a video
gets a real thumbnail, a document gets its opening lines, and a song gets a filename.
Cover Art Archive fills that gap on review and browse cards.

It runs **only when you open a preview**, never during analysis. Fetching art for every
track in an inbox would cost a MusicBrainz request per album, at one request a second,
for pictures nobody may ever look at. One file at a time, on demand, costs nothing.
Covers are cached in `appdata/thumbs/` and **never written into your library** — v1
renames and moves files, it does not add them.
