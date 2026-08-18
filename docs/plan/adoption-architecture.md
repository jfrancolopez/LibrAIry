# How a generated file becomes a library file

A decision record. It exists because the previous pass recommended one option
on paper, and writing the execution code turned out to answer the question
differently.

The question is precise:

> How does a verified file in `appdata/optimization/jobs/<job-id>/output.flac`
> become a legal source for an immutable plan, **without creating a second,
> unjournaled filesystem workflow?**

Everything else about adoption — the UI, the storage vocabulary, Undo — is
downstream of that one transition.

## The answer, first

**Option C.** The generated file **does not move before the plan exists.** It is
read in place, from a root only the executor can resolve, by a plan operation
whose `src_fingerprint` is the hash recorded when the output was verified.

```
plan_ops row:
    op_type          move
    src_root         optimization          <- resolves to appdata, executor only
    src_relpath      <job-id>/output.flac
    src_fingerprint  <hash of the verified output>
    dest_root        library
    dest_relpath     Music/Live/concert.flac
    item_id          NULL
```

There is no first move, so there is nothing to journal separately, nothing to
be interrupted half way, and no window in which a generated file exists
somewhere a user can see it but no plan describes it.

### The hash is integrity, not provenance

An earlier draft of this document said "the existing per-op hash check *is* the
provenance check". That was wrong, and the distinction matters.

`src_fingerprint` proves one thing: **these bytes are the bytes the operation
expected**. It does not prove they are the verified output of *this* job. A
different file with identical bytes satisfies the hash and is still not an
authorised source — and neither is a stale output left in a job directory by an
interrupted run, nor another job's output that happens to match.

Complete provenance needs the whole chain to agree:

    plan.optimization_job_id
      -> the job
      -> that job's recorded verified output path
      -> that job's recorded verified output fingerprint
      -> op.src_fingerprint
      -> the bytes actually on disk

The resolver must check every link. Hash equality is the last of six
conditions, not a substitute for the other five.

## Why the earlier recommendation was wrong

The previous pass recommended **B**, generated output under quarantine
semantics, and its reasoning was "reuse machinery that already exists". That
reasoning was sound and the conclusion was still wrong, for a reason only
visible once the execution code existed: **B has no honest answer to the
question above.**

Under B the generated file has to get from `appdata` into the quarantine root
before a plan can name it. Who moves it?

- **A request handler** — then a POST moves a file, which is precisely the thing
  the last four passes have been removing from this codebase.
- **The executor** — then you need a plan to create the plan.
- **A worker step** — then there is a second filesystem workflow with its own
  crash semantics, its own journal (or none), and its own reversal story.

Every branch is worse than the problem it solves. The only version of B that
avoids the move is *staging into the quarantine root from the beginning*, and
that trades one move for a permanent obligation: the scanner, Browse, Search,
the backup queue and all four Quarantine view predicates would each need an
exclusion for a subtree that is not user media. Five exclusions is five places
to leak, and an in-progress encode would sit inside a folder the user opens over
SMB — which is the exact problem that put staging under `appdata` in the first
place.

## Why A is not merely undesirable but blocked

Option A proposed a fourth root beside `library`, `inbox` and `quarantine`. A
*user* root needs `items` rows — the scanner, Browse and Search all key off
them. And:

```sql
items.root TEXT NOT NULL CHECK (root IN ('inbox','library','quarantine'))
```

Measured, not assumed:

```
items.root accepts 'optimization': NO — CHECK constraint failed
```

SQLite cannot alter a CHECK constraint, and rebuilding `items` means dropping a
table that ten others hold foreign keys into. This is the same wall that stopped
a `withdrawn` plan status two passes ago. **A is not available at the price it
was costed at.**

The same measurement is what makes C work, though:

```
plan_ops.src_root  TEXT NOT NULL          -- no CHECK at all
plan_ops.item_id                          -- nullable
plan_ops.op_type   CHECK IN ('move','quarantine')
```

A plan operation may name any root. It does not need an item row. And adoption
needs only `move` and `quarantine`, both of which already exist — so no new
`op_type`, which would have hit the CHECK wall as well.

## What C actually costs

Three touch points, measured by patching them in process and running the real
executor, planner, history and undo:

1. `executor._root_path` / `planner._root_path` — one branch each, resolving
   `optimization` to `appdata/optimization/jobs`. `history` imports the
   executor's, so it comes free. The root resolves **from settings**, never from
   anything a request supplies.
2. `planner.add_plan_op` — a source in that root carries its own fingerprint
   instead of reading one from `items`.
3. `planner._approval_errors` — the same exemption from the "source has an item
   row" check.

## The evidence

Real FFmpeg output, real plan, real executor, real undo:

```
plan hash          8174ea72f2b5d262
op 2 fingerprint   == hash of the verified output      true
execute            done 2 · failed 0 · skipped 0 · renamed_collision 0

after commit       library      Music/Live/concert.flac
                   quarantine   Music/Live/concert.wav
                   staging      (empty)
optimized file matches the verified output             true
original bytes preserved exactly                       true

after undo         library      Music/Live/concert.wav
                   quarantine   (empty)
                   staging      output.flac
undo restored the exact original bytes                 true
generated copy back in its own job staging             true
```

Undo needed **no new code and no invented location**. `undo_plan` reverses in
`id DESC` order, which is exactly right: the optimized file leaves the library
slot *before* the original comes back into it, so there is never a collision,
and the same ordering makes the same-path HEVC case safe for free.

### Undo policy, chosen and recorded

After Undo the generated copy returns to its job's staging directory and the
job returns to **Ready**. No `_optimization-undone` folder is invented, because
that state already has a name: it is exactly the state that existed before
adoption, and the user can adopt again or discard. Staging is only cleared by
cancel, failure and discard — all explicit — so the file is not at risk there.

## The result item's lifecycle — decided and proven

The subtlest part, because it can make Search wrong while every filesystem
operation succeeds.

Adoption creates a second `items` row for the generated file. Undo sends that
file back to internal staging, and **the row cannot follow it**:

    items.root TEXT NOT NULL CHECK (root IN ('inbox','library','quarantine'))

The schema catches this rather than allowing it. Undoing op 2 without a rule
raises, from the real undo path:

    CHECK constraint failed: root IN ('inbox','library','quarantine')

**Deleting the row is not available either.** Measured:

    DELETE FROM items WHERE id=<result>   ->  REFUSED: FOREIGN KEY constraint failed

Fourteen tables hold foreign keys into `items`, seven of those columns NOT NULL
(`proposals`, `similar_media_flags` twice, `quarantine_entries`, `backup_queue`,
`duplicate_reports` twice). A library file acquires a `backup_queue` row on
commit, so by the time Undo runs the result item is already referenced.

**Chosen: the row stays where it is and is marked missing.** Not a new
invention — `missing_since` already means "recorded, not at that path right
now" everywhere else here (an unmounted share produces exactly this), and
Search already filters on it via `LIVE_ONLY`.

Proven across adoption, Undo and re-adoption, with the real executor and the
real index:

    before adoption   items  1:library:concert.wav
                      search library:concert.wav

    adopted           items  1:quarantine:concert.wav · 2:library:concert.flac
                      search library:concert.flac · quarantine:concert.wav

    undone            items  1:library:concert.wav · 2:library:concert.flac:MISSING
                      search library:concert.wav          <- no ghost

    re-adopted        items  1:quarantine:concert.wav · 2:library:concert.flac
                      search library:concert.flac · quarantine:concert.wav

    result item count stayed 1 throughout

Re-adoption reuses the same row rather than creating a second, so lineage
survives and no foreign key churns. `scripts/prove_result_item_lifecycle.py`
reproduces it.

## What the result item inherits — every table, not a sample

The previous pass answered "nothing" from a partial list. Checked against every
table actually tied to an item, the answer is still nothing, and now it is a
finding rather than a shortcut. `scripts/inventory_item_tables.py` derives the
list from three sources, because no one of them is complete:

1. declared foreign keys into `items.id` — twelve tables, fifteen columns
2. tables created lazily at first use, which a PRAGMA on a fresh database does
   not show — **`item_metadata`** and `library_patterns`
3. `item_id` by convention with no FK possible — the two FTS shadows

| Table | Kind | Carry | Why |
|---|---|---|---|
| `vision_results` | byte | no | Keyed by fingerprint. A caption computed from the WAV's bytes attached to the FLAC's bytes asserts something looked at bytes nothing looked at. |
| `content_extractions` | byte | no | Keyed by fingerprint, same argument. |
| `content_fts` | byte | no | The shadow of the above. |
| `item_metadata` | byte | no | The ffprobe cache — codec, bitrate, duration, channels, sample format. Every field is a property of the encoding that just changed. |
| `audit_findings` | byte | no | Statements about a specific file at a specific path. |
| `duplicate_reports` | byte | no | A claim that two specific files are byte copies. |
| `similar_media_flags` | byte | no | A scored claim about a pair of files. |
| `optimization_opportunities` | byte | no | An offer to optimize specific bytes. The result is the output of one, not a candidate for another. |
| `backup_queue` | byte | no | A request to copy specific bytes. The executor makes one for whatever lands in the library. |
| `proposals` | neither | no | An inbox-review decision. The result is already filed at the destination it produced. |
| `groups` | neither | no | Reached only through `proposals.group_id`. |
| `plan_ops` | neither | historic | The journal. Adoption writes its own two rows. |
| `quarantine_entries` | neither | historic | Belongs to the original, which is what gets preserved. |
| `history` | neither | historic | Keyed by plan and op, not by item. |
| `review_undo` | neither | historic | A Review snapshot. |
| `optimization_jobs` | neither | **link** | Not inherited — created. `result_item_id` is the lineage Undo and re-adoption follow. |
| `search_fts` | derived | **recompute** | Rebuilt by `sync_search_item`. Category comes from the path. |
| `catalog_identity` | **identity** | automatic | See below. |
| `library_patterns` | identity | automatic | Keyed by artist or show name. Unaffected. |

### The identity that is not lost, and not copied either

A trusted TMDB or MusicBrainz answer *should* survive MKV -> MP4, and throwing
one away because the container changed would be a real loss. It does survive,
for a reason worth stating exactly:

```sql
CREATE TABLE catalog_identity (
  scope_kind TEXT NOT NULL,   -- 'album' | 'movie' | 'show'
  scope_key  TEXT NOT NULL,   -- the library-relative FOLDER
  ...
  UNIQUE(scope_kind, scope_key, provider)
);
```

There is no `item_id` and no foreign key to `items` at all — measured, not
inferred from the name. Identity belongs to the album or movie folder, not to
each of its forty tracks. And `target_relpath` changes only the suffix, so the
optimized file lands in the same folder. `Movies/Fight Club (1999)` is still
TMDB 550 afterwards, without a line of carry-forward code.

`item_metadata` is the table that looks like it might hold identity and does
not. Despite the name it is one tool's cache of ffprobe output, and it is read
only on a fingerprint match — so it self-invalidates across a re-encode even
if some future change did copy it. That is what makes reusing `result_item_id`
across a re-run safe: every byte-specific fact is either absent or re-derived.

`CARRIED` is an empty tuple in `optimization_adopt`, and a test reads the
module's own SQL to prove it writes to nothing but `items` and
`optimization_jobs`.

## The revision the same-path case forced

This document said the dormant result row would keep the library path it used
to hold. Writing the HEVC case proved that cannot be true for all three shapes,
and the reason is a constraint rather than a preference.

`PRESET_SUFFIX` decides which shape an optimization is:

    flac-lossless    .flac
    mp4-stream-copy  .mp4
    hevc-1080p-low   .mp4

So an H.264 **MP4** re-encoded to HEVC comes back as an MP4 and lands on its
own path — `Movies/film.mp4` -> `Movies/film.mp4`. (An MKV source does not:
it comes out `.mp4`, an ordinary extension change. The same-path case is real,
and it is narrower than it first looks.)

On Undo the original returns to that path while the dormant result row is still
claiming it:

    UNIQUE constraint failed: items.root, items.relpath

`UNIQUE (root, relpath)` is a **table constraint** on `items`, so SQLite cannot
alter it, and rebuilding `items` means dropping a table fifteen foreign keys
point into — the third time this wall has decided a design here.

**The row yields the path.** `root` stays `library`, `missing_since` is set as
before, and `relpath` is parked at `_optimization/<job-id>/<former relpath>` —
the former path kept inside the parked one, so lineage still reads, and the
`_` prefix following the convention `_to-delete` and `_librairy` already use.

It is the more honest record anyway. There is no file at
`Music/Live/concert.flac` while the copy is in staging, and a row saying there
is one is exactly what this pass set out to prevent.

One consequence, and it is an improvement: `record_result_item` and
`retire_result_item` now find the row through `optimization_jobs.result_item_id`
rather than by path. The job is what knows which row its output became; a path
lookup agreed with it only by coincidence.

## The two questions C still leaves open
1. **Collision must refuse, not renumber.** `resolve_collision` auto-numbers,
   which is right for an unrelated import and wrong here: `concert-2.flac`
   beside `concert.wav` is not what anybody asked for. Refusal belongs in
   preflight, before the plan is approved, and again before execution.
2. **`quarantine_entries.reason` is CHECK-constrained** to
   `('exact_duplicate','similar_media','user')`, so a preserved original reads
   "you said you did not want it" — the opposite of the truth. The
   "PRESERVED ORIGINAL" label has to be derived from the linked optimization
   job rather than from a new reason value, because that CHECK cannot be
   widened either.

## And one bug this found in shipped code

Undoing **any** quarantine put the file back and left the item row reading
`quarantined`, with the search index still describing it as quarantined. Not
cosmetic: `quarantined` may legally only become `discovered`, so the row was
nearly frozen, and every count of "what is in quarantine" answered yes about a
file sitting in the inbox. Nothing to do with optimization; fixed and tested in
this pass.

## Storage vocabulary

Recorded here because it is part of the design, not decoration. With an 842 MB
original and a 504 MB optimized copy, there is no single honest number called
"reclaimable": deleting the preserved original frees **842 MB at that moment**,
and the library ends up **338 MB smaller than it started**. Those differ by more
than a factor of two.

`librairy.optimization_storage` is the only place these are computed:

    representation_reduction_bytes    338 MB   the new file is this much smaller
    current_extra_storage_bytes       504 MB   what the second copy costs today
    reclaimed_now_bytes                 0 B    freed so far — zero, always, until
                                               somebody removes the original
    bytes_freed_if_original_removed   842 MB   what that removal frees
    final_net_reduction_bytes         338 MB   where storage lands afterwards

`reclaimed_now_bytes` is the only quantity that may be described as saved or
reclaimed, and it is 0 for the entire life of this feature because LibrAIry
deletes nothing.

## Decision

Implement **C**. The gate on `Use optimized` existing at all is the two open
questions above, plus the partial-failure compensation proof.
