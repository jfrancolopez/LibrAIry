# Changelog

## v1.2.0 - 2026-08-04

Container hardening release. `docker scout quickview` goes from 7C/36H/55M/143L and a
health score of E (2 of 7 policies) to 2C/4H/12M/117L and B (5 of 7).

### Security

- Base image moved from `python:3.12-slim-bookworm` to `python:3.12-slim-trixie`.
  Bookworm had no fix for the critical/high CVEs in `perl`, `nss`, `mbedtls`, `jpeg-xl`,
  `libssh2`, and `tiff`. Python stays on 3.12.
- Replaced Debian's `gosu` with `setpriv` from `util-linux`, and Debian's `rclone` with
  upstream's static build (pinned by version + SHA256). Both Debian packages are Go 1.19
  binaries and were the sole source of all 4 critical and 29 high *fixable* CVEs.
- Both build stages now run `apt-get upgrade -y`.
- Release builds publish max-mode SLSA provenance and an SBOM.
- See `docs/security.md` for the two remaining, deliberate deviations.

### Breaking

- **The image now runs as the non-root `librairy` user (uid 1000) by default.**
  `PUID`/`PGID` remapping requires root, so hosts that rely on it must start the
  container with `--user 0:0` (compose: `user: "${LIBRAIRY_USER:-0:0}"`, already the
  default in `docker-compose.yml`; the UNRAID template passes it in `ExtraParams`).
  Privileges are still dropped to `PUID:PGID` before anything runs. Started non-root
  against directories it cannot write, the container now stops immediately with a
  message naming the directory instead of failing later with an opaque error.

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
