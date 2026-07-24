# Changelog

## v1.0.0 - 2026-07-23

LibrAIry v1 is a self-hosted, privacy-first file organizer for NAS and desktop Docker hosts.

### Ships In v1

- One-container deployment with web portal and worker supervisor.
- Optional portal password (open on a trusted LAN by default; set `AUTH_REQUIRED=true` to require one), server-side sessions, CSRF protection, and rate-limited login.
- Inbox scanning, classification, duplicate review, proposals, safe edits, commit plans, undo history, quarantine restore, search, browse, settings, provider selector, and access pointers.
- SQLite + FTS5, local-first AI through Ollama, and explicit cloud opt-in.
- Metadata catalogs consulted before AI: embedded audio tags, TMDB (movies/TV, free key), and Open Library (books, keyless).
- Friendly web UI with six retro colour themes (beige-box default), contrast-checked to WCAG AA.
- Document text search and one-way rclone backup, both opt-in.
- UNRAID template and Docker install docs.

### Never Does

- Never deletes user files.
- Never overwrites existing destinations.
- Never mutates the existing library during indexing/search/browse.
- Never commits recomputed analysis; commits execute approved immutable plans.
- Never sends cloud AI prompts unless the provider is explicitly enabled.

### Known Gaps

- AcoustID and MusicBrainz lookups are not yet wired into the analyze pipeline; audio is identified from embedded tags and filename heuristics.
- The UNRAID template has not yet been drilled on real UNRAID hardware.
