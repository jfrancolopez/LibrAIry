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
| `MB_RATE_LIMIT` | Minimum seconds between MusicBrainz requests. |
| `AI_PROVIDER_ORDER` | Default AI provider kind order. |
| `CONFIDENCE_THRESHOLD` | Default proposal confidence threshold. DB setting can override. |
| `USE_MULTI_AI` | Whether AI tries multiple providers until threshold is met. |
| `OLLAMA_HOST` | Default Ollama endpoint URL. |
| `OLLAMA_MODEL` | Legacy alias for `OLLAMA_MODEL_PRIMARY`. |
| `OLLAMA_MODEL_PRIMARY` | Default primary Ollama model. |
| `OLLAMA_MODEL_SECONDARY` | Default secondary Ollama model. |
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
| `BACKUP_SCHEDULE` | Backup schedule setting; `after_commit` is the default. |
| `BACKUP_INCLUDE_DB_SNAPSHOT` | Whether backup includes a SQLite appdata snapshot. |
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
| `backup.schedule` | Backup schedule mode. |
| `backup.include_db_snapshot` | Include a SQLite snapshot in backups. |

API keys are environment-only in v1. The settings page shows only `set` or `not set`.
