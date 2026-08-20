# Changelog

## Unreleased

Review becomes usable on a real queue, and four detectors that had never worked
start working. Most of what follows was found by pointing the new comparison
panel at a real library and reading what it said.

### Added — Library Audit

A second question, kept firmly apart from the first. Review asks *where should
this new file go?*; Library Audit asks *is this file, which I already own, in
the right place?* See [the guide](docs/using-librairy.md#library-audit).

- **Manual only.** A button in Browse — *Audit this folder* / *Audit the whole
  library* — or `librairy audit run [--scope Music/Pop]`. Nothing runs on a
  timer, deliberately: an audit that reorganises in the background is the
  opposite of what this program is for.
- **Analysis never writes to the library.** It reads the filesystem, the index
  and embedded tags, and produces findings. No rename, no move, no delete, no
  re-index, not even for a finding it is sure about. Every test that builds a
  tree asserts the tree is byte-identical afterwards.
- **Findings live in their own table and their own section of Review**, outside
  the inbox form. That is what makes it structurally impossible for *Approve
  all confident* to sweep up a change to a file you already own — a guarantee
  from the shape of the thing rather than from a filter someone might forget.
  Every row is labelled **LIBRARY AUDIT** in words; the colour only reinforces.
- **Eight detectors**, all deterministic: unexpected file type, loose file,
  naming inconsistency, tags disagreeing with the folder, exact duplicate,
  missing artwork, unindexed, system junk. An observation renders no
  *Suggested* line at all, because not every finding is a move.
- **Silence is the target.** Your layout is evidence, not a mistake: genre
  disagreement alone never moves anything, a library with no consistent depth
  gets no *loose file* findings, and a library that shouts throughout is a
  style rather than four hundred naming problems. Against the author's real
  140-file library a full audit reports **three** things.
- **Missing artwork is per album, not per folder.** Written per-folder first,
  and the real library rejected it: a 45-track compilation filed
  one-artist-per-folder produced twenty-eight identical rows for one missing
  cover. Found by running it, not by a test.
- **Keep as it is** is remembered. The next audit leaves that finding alone
  unless the file itself changes.

**Corrections are not executable yet, and that is deliberate.** The immutable
plan and undo turn out to handle library-to-library moves already — the
executor never asked which root it was working in — and `test_library_to_library.py`
pins down containment, source fingerprint, collision, journal and exact-path
undo for them. Two things are still missing before a button may act on a
finding: companions (an album's cue, artwork and playlist) must move together,
and the source must be re-checked against the fingerprint the finding was made
against. Until both exist, a suggested destination is advice.

### Changed — the CLI is now something you can script against

Once maintenance work has a command, its exit code is an API. Three habits made
that unreliable, and all three are fixed. See [the command line](docs/cli.md).

- **`librairy --json ai status` printed plain text.** `ai status` and `ai test`
  declared their own `--json`; a subparser copies every key it sets back over
  the parent's namespace, so their default of `False` overwrote the global
  `True` you had just typed. `--json` is now inherited by every subcommand with
  `default=SUPPRESS`, so it works before or after the command and cannot be
  shadowed. A test walks the parser tree and fails on *any* subcommand flag that
  shadows a global one, rather than on this one instance.
- **A refusal exited `0`.** `commit`, `undo` and `vanished clear` without
  `--yes`, and `quarantine restore` without an entry, all printed a complaint
  and reported success. Now `2`. Nothing-to-do still exits `0`, because
  `cleared: 0` is an answer and not a fault, and a partly finished commit still
  exits `1`.
- **`error` is a machine code now** — `confirmation_required`,
  `argument_required`, `provider_not_found`, `internal_error` — with the English
  in a new `message` field, so a script never has to match on a sentence.
- **In `--json` mode, stdout is exactly one JSON document**, errors included.
  Errors used to go to stderr, which left a pipe holding nothing. Human mode is
  unchanged: results on stdout, diagnostics on stderr.
- **`ai status` and `ai test` fail differently, on purpose.** Status exits `0`
  while reporting every provider offline — you asked what the state was and got
  told. `ai test` exits `1` when the round trip does not complete, because the
  round trip was the request.
- **Grouped commands require a subcommand.** `librairy vanished`, `librairy ai`
  and five others used to print nothing and exit `0`.
- **Nested values no longer print as Python reprs.** `ai test` rendered its
  health block as `{'ok': True, ...}`; dicts, tuples and values containing
  newlines all render line-oriented now, the way list values already did.

### Fixed — three things a browser found and no assertion had

Found by driving the new workflows in a real browser rather than by reading the
markup, which is the only way any of them would have surfaced.

- **Commit named a folder rename after one of its files.** A fourteen-file
  rename is one decision, and the card was titled `01 - Funkytown.flac` with
  that file's before and after under it — one fourteenth of what pressing
  Commit would do, and no sign that a folder was involved at all. The card is
  named by the finding now: *Lipps Inc.*, `Music/Pop/Lipps Inc.` →
  `Music/Pop/Lipps Inc`, *3 files*.
- **Setting a duplicate aside was filed under "Library corrections".** Both
  come from a Library Review finding and they are not the same act: a
  correction moves a file to a better place in the library, and this takes one
  *out* of it. Commit has a sixth group — **Duplicates set aside**, badge
  `SET ASIDE` — so the page never tells anybody that a quarantine is a rename.
- **And the commit screen said "Applying correction … 1 file moved".** It now
  says *Setting aside — foo.jpg · 1 file going to Quarantine. Nothing is
  deleted; it can be restored from the Quarantine page.*

### Added — the duplicate the audit found can now be set aside

*Possible duplicate* has been in the audit since the first release and has never
had a button. The reason recorded in the code was right the whole time —
quarantining a copy is a different action class with its own safety rules, not a
move — but the consequence was that the finding with the clearest answer in the
whole audit was the one you could do nothing about.
See [the guide](docs/using-librairy.md#identical-files-your-choice-not-a-rule).

- **LibrAIry does not choose which copy you keep.** The bytes are identical, so
  there is nothing measurable to appeal to; the difference is what the folders
  mean to you. Every deterministic rule anyone could write — keep the deeper
  one, keep the first alphabetically — is a preference wearing a rule's clothes,
  and it would be applied to a whole library at once. The row lists the copies
  and you press one.
- **A new row state, *Your choice*.** It has a real action and is still not
  approvable, which is the first time those two have come apart: *Approve all
  confident* has no answer to "which one", so bulk can never reach this row.
- **Nothing is deleted.** The copy moves to Quarantine, where it can be looked
  at and restored. It is one approved plan, so it waits for Commit, appears
  there beside every other decision, is journalled, and Undo puts it back.
- **The last copy is never set aside.** Checked against the index at the moment
  you press, not against the audit that found it — so a copy deleted by hand
  since, or one set aside in another tab, leaves the row saying there is only
  one left rather than offering to quarantine it. A protected copy, a changed
  copy and one already waiting for Commit each say so on their own line instead
  of quietly losing a button.

### Added — Music Videos is a category, not a design document

The destination templates, the DJ filename parser, the version-identity rules
and the "no album layer" guarantee were all written and all proven. Nothing was
connected to any of it. No classifier ever produced the category, and
`proposals.category` was a CHECK constraint that had never heard the word
`music_videos` — so the one INSERT that would have made the rest reachable could
not have succeeded. Nobody found out, because nothing tried.
See [the guide](docs/using-librairy.md#music-videos).

- **Two signals, and neither is the extension.** A `Music Videos/` folder
  somebody put the file in, or a version marker only a video can carry —
  `(Official Video)`, `(Lyric Video)`, `(Visualizer)`. `(Live)` and
  `(Remastered)` are deliberately not in that set: both are as true of an audio
  release, and one word should not file a concert recording as a video.
- **`Artist - Title` alone changes nothing.** `50 Cent - In Da Club.mp4` loose in
  the inbox classifies exactly as it did before, because a good deal of cinema is
  named `Director - Title` by somebody's ripping script. No film that was
  classified correctly yesterday moves today.
- **Order of precedence, stated once:** an `S01E02` is an episode, always; a
  catalogued film beats a version marker; nothing beats the folder. A film in
  your `Music Videos/` directory means the folder is wrong about one file, not
  that a catalog overrules you about your own files.
- **Phone clips stay personal.** `IMG_4021.MOV` is filed with the photographs it
  was taken beside even when it is sitting inside a Music Videos folder, and
  nothing a model saw in a frame is allowed to decide this category at all.
- **No artist is invented.** A name that cannot be read gets `Unknown Artist`
  and lands below the confidence threshold for a person to place, rather than a
  guess that becomes a directory and outlives itself.
- **Versions are identity.** `(Clean)` and `(Dirty)`, official and lyric, live
  and studio are four different files with four destinations and one group, so
  Review shows them together and nothing calls either a duplicate.
- **Library Audit learned three findings.** *Music video in the wrong place* is
  a correction — one file, one move, undoable. *Personal clip under Music
  Videos* and *Music video nobody can be sure about* are observations, because
  choosing a year and an event for somebody's holiday clip is not a filing rule
  and there is no artist to invent for an unreadable name.
- **The audit never restyles a filename.** A check that compared every file
  against its canonical name would report a hand-made collection as one problem
  per file. A correction changes the folder and keeps the name it found, and it
  files into an artist folder you already have — spelled your way — in
  preference to inventing one.
- **Schema 29** rebuilds `proposals` so the category constraint admits
  `music_videos`, and Search's category list is taken from the taxonomy instead
  of being a second hand-kept copy. That copy had already drifted, which is why
  every music video already in a library counted as *misc* and the category
  filter could not find one.

### Added — a folder can now be renamed, as every file move it actually is

`Music/Pop/Lipps Inc.` was the report that opened this and the one that kept it
shut. Library Review could see the folder was wrong and spell the corrected
name, and it had to show that with no button, because LibrAIry has one executor
and every guarantee it makes is stated per file — a fingerprint checked at
commit time, a collision that never overwrites, a plan that cannot be
recomputed, an undo that puts each file back. `mv folderA folderB` has none of
them.

So the correction is not a folder operation that moves files as a side effect.
It **is** the files; the folder disappearing afterwards is what is left when
they have all gone. See [the guide](docs/using-librairy.md#library-audit).

- **Approval expands the folder into concrete moves**, one operation per file,
  at any depth, each carrying the fingerprint it was approved against. The
  folder itself is never an operand — there would be nothing to fingerprint and
  nothing for Undo to check. Commit, History and Undo learned nothing new: it
  is the same plan, the same journal and the same reversal a single-file
  correction already used.
- **The row says how big the decision is before asking you to make it.**
  *Affects 14 files · 620 MB · everything in this folder*, on the row rather
  than inside a disclosure.
- **Seven refusals, each said out loud on the row.** A folder that would merge
  into one that already exists; a file inside that is not indexed, has changed,
  or has been deleted since the audit; a file already waiting for Commit as
  part of another correction; a protected folder; a disc structure; more than
  200 files, because a plan is a list somebody reads and past a couple of
  hundred rows approving it stops being a decision.
- **A rename that only changes capitalisation is refused where the filesystem
  cannot express it.** `JAMES BROWN` → `James Brown` is the naming detector's
  commonest output, and on APFS or NTFS the destination directory already
  exists — it *is* the source, so each file would move onto itself. There is a
  two-step dance through a temporary name that would work, and it is exactly
  the unjournalled folder operation this avoids. The same finding on a
  case-sensitive volume is executable.
- **Approval reads the bytes; the page reads `stat`.** Rendering fifty folder
  findings must not hash the library, so Review checks that every file is
  present, indexed and the length the index recorded, and `accept_correction`
  verifies fingerprints. A file rewritten to its old length between the two is
  refused at the door, not half moved.
- **Emptied folders are taken away, in both directions.** Committing a rename
  empties the old folder; undoing it empties the new one. Only directories the
  plan itself emptied, only when nothing at all is left in them — `rmdir` and
  never `rmtree`, so a `.DS_Store` nobody asked about is enough to keep a
  folder. Not journalled, because there is nothing in an empty directory to
  restore and every undo recreates its parents on the way back.
- **This expands what is actionable; it does not make everything actionable.**
  Only folder findings whose correction is a re-rooting are executable.
  *One album in several folders*, *artist filed in two places* and *folder name
  disagrees with the tags* propose merges and choices, not renames, and stay
  observations.

### Added — Library Audit corrections can be applied

The audit stopped at "this looks wrong" because two guarantees were missing.
Both now exist, so a finding can become an approved library → library move
through the plan/commit/history/undo machinery that already carries inbox
filing. See [the guide](docs/using-librairy.md#library-audit).

- **A finding is only true about the file it was made against.** It carries the
  fingerprint from the moment it was made, and a file that has been re-tagged,
  replaced, renamed or deleted since gets **Needs re-analysis** or **Not on
  disk** — never a correction. Not by timestamp: copying a library between
  disks rewrites every mtime without changing a byte, and a file can be
  replaced with its timestamps preserved. Stale findings are looked at again,
  never quietly rewritten to match the new file.
- **Companions travel with their media.** The old probe proved the executor was
  safe and proved the gap in the same breath — *"a companion only travels when
  the plan names it"*. A correction now resolves the whole group before the
  plan exists. Files named after the primary (`Song.lrc`,
  `Movie.en.forced.srt`, `Movie.nfo`) follow its final name and keep whatever
  their name adds; files named after the folder (`cover.jpg`, `playlist.m3u`,
  `Album.cue`) travel only when the folder is emptying, so moving one track out
  of a ten-track album never takes the album's cover. Proximity is never the
  evidence. DVD structures are refused outright.
- **Every move is in the plan, and visible before Commit.** Review lists each
  file with its role and why it is in the group. Nothing moves as a side
  effect, so everything is previewable, containment-checked, collision-safe,
  journalled and undoable.
- **A correction group is all or nothing.** Sources are re-verified immediately
  before execution; if any has changed, none move and the finding reopens.
  Inbox plans keep their per-file independence — one file changing under you
  must not stop the other forty being filed.
- **Distinct wording, distinct sections.** The inbox says *Approve*; a finding
  says *Accept correction*. Commit counts **new files** and **library
  corrections** apart. History says *Library correction · moved 4 files*
  instead of *Filed*, read off the journal's two roots rather than a new
  column.
- **Only kinds anyone has reasoned about are executable** — for now, just
  *tags disagree with the folder*. The allowlist is by kind, not by "has a
  destination", so a future detector cannot become executable by accident.
  *Missing artwork*, *possible duplicate* and the rest stay observations, and
  `naming-inconsistency` stays one deliberately: the corrected spelling of
  `JAMES BROWN` is a judgement this code will not make.

### Added — Library Review checks names against LibrAIry's own naming rules

The same module that names a file LibrAIry is filing, not a second standard —
but only half of it, and the line is written down in `naming.py`.

- **Hygiene is audited**: leading, trailing and repeated whitespace, tabs and
  invisible characters, emoji and symbols, typographic quotes, characters
  Windows rejects, reserved device names, trailing dots, decomposed Unicode,
  over-long names. All deterministic; none needs a tag or a model.
- **House style is not.** `slugify` also turns spaces into dashes and drops
  apostrophes, which is right for a name being invented and wrong as a verdict
  on an existing library: measured against the author's real library it would
  rewrite **118 of 140 files**, since 142 of 183 path components contain a
  space. The ASCII apostrophe is spared for the same reason — `Guns N' Roses`
  is a correct name.
- **Capitals are never a rule.** `ABBA`, `MF DOOM` and `NASA` are correct. A
  shouting folder is checked against the tags of the files inside it: tags
  saying `James Brown` under a `JAMES BROWN` folder is a correction, tags
  saying `ABBA` is nothing at all, and no tags is an observation with no
  suggestion. On the real library `JAMES BROWN` is now **not** reported — its
  own tags spell it that way, and the old sibling-only rule was a false
  positive.
- **One bad name is one finding.** A folder holding forty files produces one
  row, not forty.
- Disc structures are never audited for style.
- File-level naming fixes are executable; folder renames are observations
  until a subtree rename is proven safe.

### Changed — Library Audit reads like the rest of Review

It worked, and it still looked like a diagnostic panel bolted onto the page.
Same interaction quality as the inbox queue now, same different meaning.

- **Wording.** *"Your call"* was a badge on every row that was not high
  severity and said nothing the buttons below it did not already say — removed
  rather than renamed. *"Keep as it is"* → **No change**: shorter, and it names
  the decision instead of describing the file. Status chips are words —
  *Correction*, *Observation*, *Needs re-analysis*, *Not on disk*, *Waiting for
  Commit*, *Corrected* — never the stored `open`/`accepted`/`kept`.
- **Name first**, then the summary, then Current and Suggested, then where the
  information came from. A folder-level finding shows the album's own name, not
  an id.
- **Preview and fullscreen**, through `/preview/items/` — the same endpoint,
  card, lightbox and video teardown as the inbox row, with no audit-specific
  preview code. It resolves the *item*, which carries where the file is now, so
  a suggested destination can never become a preview source.
- **Evidence, at last.** The audit has always recorded it and it has never once
  rendered: `decode_evidence` rejected `filesystem` and `fingerprint` as
  sources, `humanize_evidence` swallowed the error, and every audit *Why* panel
  said "No evidence recorded". Now Why lists each signal with the weight the
  detector gave it, and the row summarises the sources actually used.
- **No invented confidence.** An audit finding has no aggregate score and the
  page does not manufacture one. The bar shows the *mix* of evidence kinds at a
  fixed width, the row says LibrAIry does not combine them into a single
  number, and *Correction* versus *Observation* carries the weight a percentage
  would carry elsewhere.
- **Its own selection and bulk actions.** One implementation in `review.js`,
  two configurations: the inbox selects `proposal_id` inside `#review-list`,
  the audit selects `finding_id` inside `#library-audit`, and neither handler
  has a parameter that could hold the other's ids. Bulk accept acts only on
  eligible corrections, says *"(1 eligible)"* before the press and *"Accepted 1
  of 3"* after. No "accept all above N%".
- **⋯** holds *Open in Browse* (the folder the file is in now) and *View
  details*, and only appears when at least one of them would work.
- Folder headings only where they group more than one thing.

### Changed — one definition of "companion file"

The Library Audit carried its own hand-written list of companion extensions,
and within a single release it had drifted in both directions: it called `.log`
a sidecar when the classifier treats it as extractable text, and it had never
heard of `.ass`, `.ssa`, `.vtt` or `.md5`, so a subtitle sitting under `Music`
was reported as an unexpected file type. It now derives its set from
`companions.SIDECAR_KINDS`, which is the only such list left in the codebase.

- **`.lrc` is a companion now.** It was not in the classifier's set at all, so
  a lyrics file got its own destination and voted on where the album belonged —
  a file with no opinion casting one. Like a subtitle, it is found by filename,
  so it follows its track's final name; lyrics that match no track fall back to
  the album folder rather than being attached to the nearest song.
- **The three extension registries are documented as three.**
  `filetypes.REGISTRY` explains a format to a human, `mediakind` says what
  LibrAIry can do with it, `SIDECAR_KINDS` says whether a file belongs to
  another file. They disagree on purpose, in two places, both now written down
  in `mediakind.py` and asserted in `tests/test_taxonomy_boundary.py`. No
  classification behaviour changed: the office formats that render no preview
  are still filed as documents, and the real library's `.xlsx` files were
  correct all along.

### Fixed — "database is locked" while a scan was running

Loading Review during a scan could return a *System Fault* instead of a page.
It was not a slow query or a long transaction: reads never block under WAL, and
every connection is already in autocommit. The page was **writing**.

- **Drawing a page no longer writes to the database.** The site header asks for
  the AI provider chain, and asking used to mirror every provider into
  `provider_status` and seed default Ollama endpoints as a side effect. Two
  writes on every page view, competing with the worker for the single writer
  lock. The defaults are computed identically each time and Settings still
  writes the real row when you save one, so nothing is lost. GET paths that
  write dropped from 26 to 13 of 25 audited; the remaining ones are all the
  session INSERT on a first-ever visit.
- **The session `last_seen_at` refresh is now best-effort, and rare.** It
  happened on every single request; it now happens once the session is past
  halfway through its life, and if it cannot get the lock inside 250 ms it is
  skipped. It is a timestamp — nobody should be logged out, or made to wait,
  because a bookkeeping update lost a race. Only `database is locked` is
  swallowed; every other `OperationalError` still surfaces.
- **A first visit that cannot mint a session still renders.** It gets a
  transient in-memory session with a working CSRF token rather than a 500. Auth
  is untouched: in auth-required mode a transient session grants nothing, and
  the next request tries again.
- **It bit hardest exactly where LibrAIry is meant to run.** A bind mount on
  macOS or an UNRAID FUSE share reports a filesystem whose shared memory cannot
  be trusted, so the index runs in `DELETE` journal mode rather than WAL — and
  under `DELETE` a reader and a writer block each other, not just two writers.
  A page that wrote was therefore competing with the worker on precisely the
  storage where the fallback exists. No PRAGMA was changed to fix this; the
  page simply stopped writing.
- **No PRAGMA was widened.** `busy_timeout` stays at 5 s for real work. Raising
  it to 30 s would have converted an intermittent 500 into an intermittent
  thirty-second hang, which is worse — the first version of this fix took the
  test suite from 15 s to 8m28s and proved the point.

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

### Fixed — a backup that did not contain the index

- **`BACKUP_INCLUDE_DB_SNAPSHOT` did nothing.** `snapshot_database` was written,
  tested, given a default-on setting, a documented environment variable and a
  checkbox reading "Include SQLite snapshot" — and called by nothing. Every
  backup ever taken held the files and not the index, so restoring onto a new
  machine gave you a library with no history, no undo journal, no quarantine
  records and no record of where anything came from. The one case a backup
  exists for is the one where the original is gone. The index now goes up to
  `_librairy/librairy.db` behind the files, on runs that actually copied
  something — the worker polls on a timer, and re-sending the database every
  poll to say nothing changed is not a thing to do to a metered connection.
- **The thumbnail cache had no upper bound.** `prune_cache` was written with a
  byte budget and never called, so one JPEG per image and per video ever
  previewed accumulated forever, on the same volume as the index. The worker now
  holds it to 512 MB, oldest first; it only ever removes files LibrAIry
  generated under `appdata/thumbs`, and regenerating one costs a single ffmpeg
  call.
- Found by auditing every function in `src/` for callers, after `group_proposals`
  turned out to have sat dead for seven phases. Ten had none. `with_dest_base`
  and `enqueue_plan_outputs` were deleted — dead code that looks like a feature
  is exactly how the first one hid.

### Fixed — "fits your existing layout" had never once fitted anything

- **Both halves of the library-pattern feature were dead.** `index_library`
  builds the map of folders you already use; `apply_library_pattern` files a new
  record into them. Neither had a caller, so the `library-pattern` evidence
  source that Review renders as *"Fits your existing layout"* could never
  appear. `librairy scan --root library` now builds the map, and classification
  consults it.
- It was also **wrong in two ways** that only running it would show. It read
  `parts[1]` as the artist, so a genre-first library — `Music/Pop/Abba/` —
  registered exactly one artist called "Pop" and never Abba; both candidate
  depths are recorded now and the lookup by real name picks the right one. And
  it returned `dest_base + clean_name`, flattening the album:
  `Music/Rock/Queen/A-Night-at-the-Opera/01.mp3` came back as
  `Music/Queen/01.mp3`. Only the part above the artist is replaced now.
- Measured on a real 140-file library: 29 artists learned where the old code
  would have learned one genre. A new Abba record goes to the `Music/Pop/Abba/`
  that exists rather than starting a second `Music/Disco/Abba/`. An artist with
  no existing folder is untouched, and the map is empty until you scan your
  library, so nothing changes for anyone who has not.

### Fixed — home videos

- **A clip off a phone is no longer looked up as a film.** Seventeen `.MOV`
  files — the largest group of unfiled items in a real inbox — were being handed
  to TMDB as titles. A UUID matches nothing, so they came back at 0.65, under
  the threshold, proposing to file a home video as
  `Movies/General/255Bea56-53F5-4D71-B0F4-A2F78Cfd5667-(0)/`. `IMG_0585.MOV` and
  `IMG_0585.jpeg` left the same phone a second apart, and only one of them was
  going to Photos. A video named after a camera prefix or a UUID is now filed
  exactly like a photo. A bare number is deliberately not enough — `1917.mp4` is
  a film, and TMDB is the thing that can say so.
- **A UUID is no longer treated as the name of anything.** An iMessage export
  gave every attachment a folder named after one, which was appended to each
  filename (`IMG_1423-0373923B-123F-4ABF-9B6E-2229413CEED4.jpeg`) *and* used as
  the destination folder (`Photos/Unknown/01B583D3-1D28-…/`). The grouping had
  learned to ignore that noise; the naming and the destination had not. One
  `is_noise` in `naming.py` now answers for all three.

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
- **Previews open full screen.** Click the picture, or the expand control in its
  corner. Images fit the window with their aspect ratio intact and toggle to
  actual size, with the real pixel dimensions read off the file; video keeps the
  browser's own controls and never autoplays. Escape, the close button and the
  space around the media all close it, and clicking the media itself does not.
  It is a real `<dialog>`, so focus trapping and returning focus to the control
  you came from are the browser's job.
  - The row still shows the 320px thumbnail; full screen asks for a 1600px
    render through the same endpoint, so it is sharp without shipping a
    48-megapixel original to answer "is this the right picture?". `size` is a
    word looked up in a table of the two sizes that exist, not a number off a
    query string.
  - **The `close` event cannot be relied on.** Measured in this project's own
    browser: `showModal()` then `close()` opens and closes the dialog correctly
    and fires no `close` at all. Hanging the teardown off it left a video
    playing to nobody behind a shut viewer. Every exit runs one teardown now.
  - **In Browse too**, on all three surfaces that show a preview — search
    results, the explorer's detail panel and the full item page. They already
    rendered the same preview card, so they already had the expand control; only
    Review shipped the viewer it opened. The dialog and its script are one
    include now, so the markup cannot arrive without the behaviour. The
    explorer's keyboard navigation stands down while it is open: it listens on
    the document, so ↑↓ used to move the selection behind the viewer and Enter
    navigated the page out from under it.
- **Browse shows the library, not a page of the index.** It built its folder
  list out of fifty indexed files: select fifty rows ordered by path, take the
  second component of each, call that the directory listing. `Photos/` here
  holds 89 indexed files and the first 83 are inside `2022/`, so
  `Photos/Unknown/` — starting at row 83 — did not exist as far as the UI was
  concerned, while navigating straight into it worked. That is what made it
  look arbitrary rather than like a limit. Two more consequences of the same
  design: a file with no search row was invisible however few files there were,
  and an empty folder could never be shown at all.
  - Folders now come from the filesystem and are **never paginated** — no
    number of files can hide a sibling. Only the file list pages.
  - The index enriches rather than defines: an unindexed file is listed with a
    quiet *not indexed* badge, because a missing record means metadata is
    unavailable, not that the file is absent. A record whose file is gone is
    simply not listed.
  - Hidden files, ignore patterns and symlinks go through **one predicate
    shared with the indexer**, so Browse and the scanner cannot drift into
    disagreeing about what the library contains. Containment is unchanged.
- **The Browse home screen is a listing too.** It was the same bug one level
  up, left in place by the fix above: eight hard-coded classification
  categories, counted with a `GROUP BY` on the search index, linking to
  physical folders named after the slug. Five tiles read zero forever because
  `Movies/` and friends do not exist here — untidy. The half that mattered is
  the inverse: a folder created over SMB could never appear at all.
  - Tiles are now the top-level directories that are really there, **under
    their real names** — the breadcrumb used to capitalise the URL segment,
    renaming your folders in the one place whose job is saying where you are.
  - A URL segment names a real directory. `/browse/Music`, resolved against
    disk, case-insensitively so existing `/browse/music` links keep working;
    nothing else can be constructed there, so a traversal matches no folder and
    gets a 404.
  - **Every count means one thing**: visible files underneath, at any depth. The
    folder pane counted *direct entries*, subfolders included — a
    different-looking measure wearing the same clothes one screen away.
- **Browse says whether the index still describes the library.** One line under
  the heading: `140 files · index up to date`, or `140 files · 3 not indexed`,
  which opens into which files and what to run. Nothing rescans the library on
  a schedule — the worker watches the inbox, and library records are written by
  the commit engine — so a file copied straight in over SMB is browsable and
  unfindable, and you used to discover that by searching for something you were
  looking straight at.
  - It never claims to be synchronised: it is measured when the page renders
    and nothing watches it after.
  - It names `scan --root library`, not `index rebuild`, which only rebuilds
    the search index from records that already exist and would find none of
    these files. A stale record gets no command at all, because a scan marks it
    missing and keeps it — and inventing a delete here would be exactly the
    unasked-for repair this page exists to avoid.
  - **Not indexed never means unsupported.** The scanner has no extension
    filter, so anything Browse can see, it can index.
  - Reporting only. Opening Browse indexes nothing, deletes nothing, triggers
    no scan, and calls no catalog or AI provider.
- **Search no longer returns files that are not there.** `missing_since` is set
  by every scan and checked by Review, Commit, plan, dedup, duplicates, the
  catalog probe, content extraction, backup, the indexer and companions.
  `search.py` did not mention the column anywhere. Here that was five files
  deleted during a drill in August coming back beside one real result and
  rendered identically — same thumbnail slot, same size, same category badge,
  same "goes to" destination. Browse was finally trustworthy and Search was
  telling you the opposite.
  - The clause is in the `WHERE`, so the exclusion happens **before**
    `LIMIT`/`OFFSET`. Filtering the returned rows would have hidden the ghosts
    and quietly shortened every page — the Browse folder bug, one table over.
  - **Nothing is deleted.** The record keeps its classification, its evidence
    and any approval or rejection; History still points at it. Put the file
    back and the next scan makes it searchable again with no rebuild.
  - `/items/{id}` says *Not on disk*, when it was last seen and where, and
    stops offering a preview of nothing — that page used to attempt one, fail
    on the open, and print the errno where the photograph should be.
- **You can look at the vanished entries before resolving them.** Review has
  offered to clear proposals whose file is gone for a while; what it did not
  offer was any way to see them. One click, every root at once, from a notice
  counting only one of them, behind a label that reads destructive next to a
  warning triangle — for something that marks seven proposals superseded.
  - A disclosure per root now holds the list: filename, path, when it went,
    what it would have been filed as, and the one line of evidence saying why.
    Then what clearing does, then a button that names its own scope.
  - Three fixes in `forget_vanished`, found by running it rather than reading
    it: it cleared every root in one call, it wrote `items.state` directly (the
    only place in the codebase skipping the lifecycle check), and it superseded
    a proposal without re-syncing the search entry — leaving the index claiming
    a category no proposal made any more.
  - **Nothing is deleted**, and 23 tests hold the line: item, proposal,
    evidence, search entry and history counts are identical across a clear.
    `missing_since` stays set, a file that comes back leaves the list on its
    own with its decision intact, and running it twice does nothing.
  - **`librairy vanished list` / `librairy vanished clear --root inbox --yes`**,
    over the same function the button calls — a test asserts both names are the
    one object and that the CLI dispatch contains no `UPDATE` of its own.
    `list` is the preview; `clear` needs an explicit root and `--yes`, and
    reports `files_deleted: 0`.
  - **A missing record no longer describes itself as a zero-byte file.** The
    scanner keeps the last size it measured, so the number was right and the
    tense was wrong: the page says *last known size* now, humanised, or *not
    recorded*.
  - **A quarantine destination is not called filing.** Three intents can sit
    behind one `dest_relpath` and the path does not distinguish them, so the
    label does — *Would have been filed as*, *Set aside*, *Marked for
    deletion*. One vanished entry here points into quarantine and had been read
    as one more filing decision. Proposal states get written out too, rather
    than showing the word the database uses.
  - **The notice reconciles its own arithmetic**: seven entries to clear, and a
    line for the eighth record that is missing and already resolved.
- **Files in the library root are reachable.** A file sitting directly in
  `library/` was scanned, indexed, searchable and had a detail page, and there
  was nowhere in Browse it could appear: the root screen lists directories, and
  the explorer only opens one. They are listed under the tiles now, with the
  same rows, badges, previews and viewer, and the same rule as one level down —
  directories complete, files paged.
- **Companion files follow their media instead of inventing a release.** A
  `cover.jpg` inside a folder of FLACs was filed as a photograph; `00.Info.m3u`
  and `00.Info.nfo` came back as confident music under two *different* invented
  artists, which split the folder's consensus and cost its real cover a
  destination. The heuristic had declined correctly both times — it was the AI,
  which only runs because confidence is low, that was asked and answered.
  Sidecars are no longer asked: the extension already says a `.srt` is not a
  film. They take their destination from the media they describe, and subtitles
  match one specific video by name and keep its final stem, so
  `Movie.en.forced.srt` becomes `The-Matrix-(1999).en.forced.srt` and players
  can still find it. No anchor, no guess — it stays in Review.
  - **Proximity is not evidence.** Eight inbox folders here hold an image beside
    a video and seven are phone camera folders; a "picture next to a film is a
    poster" rule would have misfiled every family photograph in them.
  - Existing artwork always wins, and nothing is ever overwritten.
- **Quarantine shows the file.** Both lists get Review's preview and its
  fullscreen viewer — three of the entries here are named after UUIDs.
- **The filename editor moved above the preview.** It rendered last: you clicked
  a destination at the top of the row and a form appeared underneath a
  photograph, which reads as editing the picture. Clicking a destination also
  puts the cursor in the destination box rather than the category menu that
  happens to come first. Same form, endpoint, validation and containment checks.
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

### Added — image understanding

- **A local model can look at your pictures.** Optional, off by default, and
  switched on in Settings → Image understanding. It returns a caption, the
  subjects in the frame, tags, and any text it can read, kept separately from
  the caption. All of it appears under *Why* behind a **described** badge, and
  all of it goes into the search index — so `wifi` finds the screenshot of the
  Wi-Fi settings.
- **Images never reach a cloud provider.** Not an opt-in; there is no setting
  that permits it. Cloud AI keeps reading filenames and is never offered a
  picture.
- **The model does not file anything.** It never changes a category: a *receipt*
  the deterministic pass called a photo is shown in *Why* as a disagreement,
  four lines above the category dropdown that can settle it. It only ever adds
  to a filename, and only one that says nothing — `IMG_4821.jpg` becomes
  `IMG-4821-baby-with-cat.jpg`, while `IMG-20240612-101112.jpg` keeps the
  capture time that makes a photo folder sort. Agreement is worth 0.05 up to a
  0.92 ceiling; disagreement is worth nothing, so a file deliberately held below
  the threshold stays there.
- Works with LM Studio and Ollama, on whatever model you point it at — the test
  model was `gemma-4-e4b`, which is not a requirement and not a recommendation.
  A model with no vision has the server refuse the request, which is logged with
  the server's own words rather than becoming a silent absence of captions.
- **ffmpeg flattens transparency onto black**, so a screenshot with an alpha
  channel reached the model as a solid black rectangle and was accurately
  described as one. Found by looking at what was actually sent. Everything is
  composited onto white first, and EXIF rotation is applied explicitly rather
  than left to a build-dependent default.
- Re-analysing a file that has not changed replays the stored description
  instead of spending another pass of inference on the same picture.

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

### Added — Storage Optimization

Optional, secondary, and never on a timer. LibrAIry can convert a file to a
smaller representation, and the whole feature is built so that no step of it
loses you anything.

- **Opportunities** are advisory: a section in Review that names what could be
  converted, what it would save, and what it would cost in CPU. Nothing is
  queued without you saying so.
- **The queue** runs one job at a time, inside a maintenance window, at a
  measured share of the machine (`Low` is `pools=2:frame-threads=2`, and the
  number is measured rather than guessed). The worker never waits for the
  encoder; a restart marks a running job failed and never resumes it.
- **Nothing replaces anything until you commit.** A finished conversion is a
  result to review, verified against the original's running time and, for
  lossless audio, sample-for-sample.
- **Adoption preserves the original** in Quarantine and commits like every other
  decision. `Restore original` reverses it — both files, in order, preflighted
  before either moves.
- **Disposal** goes through the delete queue, which is a folder you empty
  yourself. LibrAIry still deletes no user file, ever.
- **The arithmetic is honest.** Nothing is called "saved" while both copies are
  on the disk; realized reduction counts only originals LibrAIry can see are
  gone, and that is never the same number as what the deletion freed.

### Changed — Commit is a list of decisions

- Every pending change now declares what kind it is — **new file**, **library
  correction**, **optimization**, **restore**, **delete queue** — and each one
  is one card. A correction to a twelve-track album is one decision and twelve
  operations, and the page counts decisions.
- **Each decision appears exactly once.** New files used to be rendered twice —
  as cards, and again as a "Ready to move" total with a five-row sample — and
  the plan behind them appeared a third time under "Started but never run".
- The headline, the filter tabs, the nav badge and the Dashboard tile all come
  out of one query. The badge used to read 2 above a page saying 5, because it
  could not see restores, delete-queue requests or adoptions.
- Approved inbox files still commit as **one batch**: one plan, one journal
  entry, one Undo. The page says so where the button is.

### Changed — Quarantine, History and the Dashboard

- **Quarantine** has states rather than a list: Held, Waiting for Commit,
  Delete queue, Removed, Put back. Preserved optimization originals are visibly
  distinct and carry their own two decisions — `Restore original` and
  `Keep original` — because "I have changed my mind about deleting this" is not
  "I want the old file back".
- **History** reads as a timeline: days, plans, and a sentence per plan. Undo is
  offered only where the files are still where the journal says, and says why
  when they are not. Its filters ask where a file went rather than naming one
  action, so quarantine, disposal and restore entries are findable at all.
- **The Dashboard** answers what needs attention, what is running and where the
  work is. It walks no filesystem, runs no tool and calls no provider.

### Changed — one word per idea

- `docs/ui-vocabulary.md` now holds the decision lifecycle and the words that
  belong to each step, and `tests/test_control_inventory.py` enforces it against
  every control on every populated page: no word may mean two things, and no
  thing may have two words.
- Fifteen labels were retired. Getting to Commit had five, re-running the
  analyser had two, and asking a provider whether it answers had three.
- `Undo` now means only "reverse files that moved". The pre-execution
  withdrawals are `Cancel decision`, `Cancel request`, `Send back to Review` and
  `Remove approval`.

### Fixed — controls that could not do anything

- Every `Preview` button on Quarantine was inert: the page never loaded the
  script that answers it.
- A failed optimization could not be removed from the queue by any control, and
  a stale one offered a control the update then refused.
- The withdrawal on a New file card carried no label and sent *every* approved
  inbox file back, not the one it was drawn on.
- The confirm screen stayed on screen after committing, question and button
  intact, above a panel saying the files had already moved.
- The Undone page printed the stored outcome code, which for the commonest
  refusal is two full hashes.

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
