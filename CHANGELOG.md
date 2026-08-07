# Changelog

## Unreleased

Review becomes usable on a real queue, and four detectors that had never worked
start working. Most of what follows was found by pointing the new comparison
panel at a real library and reading what it said.

### Fixed — three duplicate detectors had never once run

- **rmlint wrote its JSON to a file literally named `-`.** The flag was
  `-o json:-`, which rmlint reads as a filename, not stdout. Every fingerprint
  match was therefore recorded as "rmlint disagrees", and because a disagreement
  blocks staging, **no exact duplicate had ever been staged for quarantine**.
- **czkawka never ran.** It was passed `-d dirA dirB`; czkawka 11 wants one `-d`
  per root and refused the whole command. Fixed, it then exited 101 with empty
  stderr — a panic on a cache directory it could not create, because the
  container drops privileges with `HOME` still pointing at root's home. Its cache
  now lives on the appdata volume, which also keeps the perceptual hashes (the
  entire cost of a scan) across restarts. Any czkawka failure is now recorded
  instead of swallowed.
- **AcoustID had never identified a file.** `fpcalc -plain` prints the
  fingerprint and nothing else, so the duration came back empty and AcoustID
  refuses a lookup without one. The whole fingerprint path was dead while every
  mocked test passed; the guard is now an assertion on the argv, since nothing
  mocked can catch this.
- **Three catalog clients were called wrongly** — Discogs without its token,
  Last.fm without its key, AcoustID handed a dict where it wanted a fingerprint.
  Each looked like "no match". The wiring tests now inject an opener and make a
  real request shape, because `lambda *a, **k` mocks accept any call at all.
- **Grouping had never run.** `group_proposals` was written in phase 2 and
  nothing ever called it, so every proposal carried a NULL group_id and Review's
  default sort — whose premise is "keeps albums and seasons together" — put
  everything in Ungrouped.

### Added — Review

- **Compare duplicates.** Any row that may already be in your library opens the
  two copies side by side, with a preview of each, what all three detectors
  concluded and every property that differs — duration, bitrate, resolution,
  camera, date taken. A detector switched off says *not asked*, which is
  deliberately not the same as *agrees*.
- **Undo.** After any decision an Undo bar names what it will take back
  ("Approved 12 files") and one press restores the whole batch, destinations
  included. Distinct from History's undo, which moves files back on disk;
  anything already committed is refused rather than described wrongly.
- **Other options** (behind *Why*). Asks every catalog and every AI provider you
  have switched on, each separately, about that one file, and lists what each
  said with the destination it would give the file and a *Use this* button.
  Nothing is stored: a provider or key added five minutes ago is included, with
  no re-analysis. An answer below the confidence threshold still shows its
  destination — during a scan that gets stripped, but choosing one by hand is a
  different act. Anything that fails or finds nothing is listed with its reason,
  and a catalog that drew a blank is never credited with the filename fallback.
- **Re-analyse**, replacing *Not this*. The old button set the guess aside and
  never guessed again — a dead end in one click, escapable only from the command
  line. This hands the file back for a fresh pass over tags, catalogs, duplicate
  detectors and AI.
- **Mark for deletion**, gathering files into `quarantine/_to-delete` — one
  folder to empty yourself, in one deliberate gesture. Available on a Review row,
  on a held quarantine file, and on a staged duplicate that has not moved yet.
  **Nothing is deleted by LibrAIry, at any point**; *Put it back* still works
  from the pile.
- **A Test button on every catalog**, making one real request, because a rejected
  key and no match look identical from the outside. It reports "Key rejected",
  "Rate limited", "Service is down" or "Reachable, no match".
- **What do these buttons do?** — the three-step flow and every button's effect,
  next to the Review heading and in `docs/using-librairy.md`.

### Changed — Review

- **Confidence is one bar, coloured by the score** — green from 85%, amber from
  60%, red below or with no destination — with the sources it is made of kept as
  shading within that hue. It replaces a decimal, a badge word and a colour band
  that were three encodings of one number, none of which answered *is this safe
  to wave through?*
- **The rows are half the height.** Worst case 496px → 123px, median 136px → 98px,
  and the furniture above the first file 489px → 345px. Long names and paths clamp
  at two lines with the full value in the title.
- Bulk approve names its threshold and its count — *Approve 40 at 85%+* — and is
  absent when it would do nothing. *Forget them* became *Clear these entries*,
  which is what it does. Each action now says what it did rather than "n
  proposal(s) updated".
- **Review says how many files are approved and waiting**, with a link to Commit.
  Approving is a decision; nothing on the page said the move was one more press.
- A group of one is no longer a group: a heading, a select-all and a section
  margin over a decision you were making one row at a time anyway. A folder named
  after a UUID is no longer a photo event — iMessage attachments arrived as
  thirty-two separate "events".

### Added — discs

- **A ripped DVD files as one thing.** Its nine files were nine unanswerable
  questions at 0.30 apiece; the title is on the folder above `VIDEO_TS`, and the
  whole structure files under it. **The names inside a disc directory are never
  rewritten** — a player looks for `VTS_01_1.VOB` by that exact name — so a tidied
  disc folder is one that no longer plays.
- A disc's byte-identical `.BUP` files are no longer treated as duplicates of
  their `.IFO`s. The pairing is the format, not clutter; quarantining one damages
  the disc.

### Added — configuration

- `CZKAWKA_SIMILARITY`: `strict` (default), `balanced` or `loose`. czkawka scores
  0-40 for images and 0-20 for videos, on scales nobody can calibrate without
  running the tool twice — measured on a real library, 5 finds only what is
  visually identical and 20 groups eleven unrelated photographs.

### Fixed — portal

- **"System Fault" on any page during a scan.** Drawing the site header mirrored
  every AI provider into `provider_status` as a side effect of being asked which
  providers exist — 25 writes per five page views — and a page view that collides
  with the worker holding the write lock is a 500 on whatever you were reading.
  Rendering is read-only now, with a test that traces the connection across five
  pages.
- **Static assets carry `Cache-Control: no-cache`.** `StaticFiles` sent an ETag
  and no Cache-Control, leaving the browser to invent a lifetime: a container
  upgrade kept the old stylesheet against the new HTML, an update that appears to
  do nothing and then fixes itself hours later.
- Saying "no" to a duplicate was always a 500 — `quarantine-proposed → pending`
  was not a legal transition.
- The header no longer overflows on a phone.

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
