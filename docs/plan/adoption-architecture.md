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

## The three questions C leaves open

Found by running it. All are shortcomings of the *proof*, not of the choice.

1. **The adopted file has no `items` row.** `item_id` is NULL, so
   `_move_item_row` does nothing and the optimized FLAC is unindexed —
   after adoption, Search still returns the *quarantined original* and knows
   nothing of the active file. Adoption must create the library item row,
   carrying the original's identity forward (a representation change is not a
   new identity).
2. **Collision must refuse, not renumber.** `resolve_collision` auto-numbers,
   which is right for an unrelated import and wrong here: `concert-2.flac`
   beside `concert.wav` is not what anybody asked for. Refusal belongs in
   preflight, before the plan is approved, and again before execution.
3. **`quarantine_entries.reason` is CHECK-constrained** to
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

## Decision

Implement **C**. The gate on `Use optimized` existing at all is the three open
questions above, in that order.
