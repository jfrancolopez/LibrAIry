# Changelog

## v1.2.0 - 2026-08-04

Container hardening release. `docker scout quickview` goes from 7C/36H/55M/143L and a
health score of E (2 of 7 policies) to 2C/4H/12M/117L and B (5 of 7). Also closes the
last v1.0.0 known gap.

### Fixed — index corruption on FUSE and network storage

- **SQLite's journal mode is now chosen from the filesystem holding appdata.** WAL
  synchronises processes through an `mmap`'d shared-memory file; on FUSE and network
  filesystems each process gets its own view of it, so the web process and the worker
  both believed they owned the write-ahead log and the index rotted. This reproduced on
  Docker Desktop for macOS within a single analyze run, twice, and UNRAID's `/mnt/user`
  shares are the same class of filesystem. WAL is kept on recognised local disks
  (ext4/xfs/btrfs/zfs/overlay/…) and DELETE is used everywhere else — an allowlist,
  because Docker Desktop reports its bind mounts as `fakeowner`, a name no blocklist
  would have anticipated. Override with `SQLITE_JOURNAL_MODE`.
  See `docs/troubleshooting.md` for how to repair an already-corrupted index; no user
  files are ever at risk, the index is rebuildable by rescanning.

### Added

- `librairy analyze --reanalyze` re-proposes items already sitting in the review queue.
  Analysis only ever ran on newly discovered items, so a newly configured AI provider, a
  catalog key added after the first scan, or an upgrade with better classification never
  reached anything already proposed — the queue kept its first answer forever. Approved,
  committed, and quarantined items are never touched.

### Fixed

- **Loose image files were never classified as photos.** Only images matching the
  screenshot pattern had a rule; every other `.jpg`/`.png` fell through to the document
  classifier's unknown-extension branch and landed in `misc` at 0.30 — below the
  confidence threshold, so it never even got a destination. Images now file under
  `Photos/{year}/{event}/`, taking the year from a date in the filename and the event
  from the folder they came from, so your own grouping survives. Generic containers
  (`Pictures`, `DCIM`, `Downloads`) become `Unsorted` rather than pretending to be events.
- **Album art is no longer filed as a photo.** `cover.jpg` beside an album or film is
  recognised as artwork and deliberately held below the threshold, because v1 cannot move
  a sidecar along with its media. The name alone is not enough — there has to be media
  beside it, so a genuine photo called `cover.jpg` is still a photo.
- **Screenshots no longer land in a literal `Photos/0/` folder.** The year was hardcoded
  to `0`; it is now read from the filename, falling back to `Unknown`.
- Health reported "low on space" once per storage root, so a single full disk shared by
  inbox/library/quarantine/appdata looked like four separate problems. Warnings are now
  grouped by the underlying volume and name the tightest reading; roots on genuinely
  separate volumes (a NAS with the library on its own array) still warn independently.

### Catalogs

- **AcoustID and MusicBrainz are wired into the analyze pipeline.** Both were injection
  points that only tests ever passed, so a configured `ACOUSTID_KEY` did nothing to real
  proposals. Audio with no usable embedded tags is now fingerprinted with fpcalc, matched
  through AcoustID, and named from the MusicBrainz recording. Files that *do* carry tags
  are unaffected — tags remain the first and strongest signal, and nothing is
  fingerprinted without a key or with the catalog toggled off.
- When a recording appears on several releases, the earliest dated one is chosen, so a
  track lands under the original album rather than a later compilation.

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
