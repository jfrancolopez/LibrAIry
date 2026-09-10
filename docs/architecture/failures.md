# When something goes wrong

Three questions, in this order, for every failure a person can meet:

    What happened?
    Is my Library safe?
    What can I do next?

`[Errno 28] No space left on device: '/library/Documents/big.bin.part-4f2…'`
answers none of them. It was, until this pass, the entire explanation somebody
got when a commit failed — printed raw on the plan page, and reduced to the
word **failed** on the page they were actually looking at.

Two modules and one rule.

## `librairy/failures.py` — what happened, and what next

One exception in, one `Failure` out: a `kind`, a stable `code`, a sentence, a
next step, and the raw text kept as `detail` for whoever is debugging.

Four kinds, because they want four different treatments and one of them is not
an error at all:

| Kind | Means |
|---|---|
| `unavailable` | an expected absent state — a drive in a drawer, a provider switched off. **Never coloured like a fault.** |
| `operational` | the operation failed; retrying converges once the condition changes |
| `action` | only a person can clear it: a permission, a read-only mount |
| `fault` | LibrAIry did not anticipate it. The only kind with a reference number |

**The middle question is deliberately not answered here.** Whether the Library
is safe is a fact about what the operation had already done when it failed, and
only the caller knows that: the same `ENOSPC` means "nothing moved" from the
first operation and "eleven files moved, this one did not" from the twelfth. A
classifier that guessed would be inventing the one sentence people act on. So
every surface supplies the safety sentence from state it can prove — the commit
panel counts what actually moved, `commit_state` counts what was actually
recorded, and the boot message says the migration rolled back because it did.

The code is written into the journal — `failed permission-denied …`, in the
shape `refused_collision <code>` already used — so Commit, History and Health
read one failure back the same way instead of three of them parsing prose.

## `librairy/roots.py` — is this the same storage?

A NAS share unmounts. What is left at `/library` is an empty, writable
directory on the container's own disk, and every check the executor made passed
against it. So a commit moved somebody's files into it and reported **2 files
moved. Nothing failed.** Those files were inside a container that the next
`docker compose up` destroys.

The same absence has a second face: a scan of that empty mount point marked
every row in the Library missing, because "I walked the tree and did not find
it" is what a deleted file looks like too.

Neither is a bug in the executor or the scanner. Both did what they were asked.
It is a missing question, and LibrAIry already knew how to ask it — an offline
drive is identified by what LibrAIry wrote on it plus what the operating system
calls the filesystem (`librairy/volumes.py`).

**Nothing here writes to the Library.** The other half of an offline drive's
identity is a marker file, and a marker in the Library would be LibrAIry adding
a file to somebody's collection — which it does not do, for anything, ever. So
the identity used is the one that can be *observed*:

```
observe()   at startup, once. Which filesystem each root is on, written to
            the database. Never re-observed at runtime: a check that repairs
            itself is a check that passes.

check()     before any operation that moves bytes, and before a scan sweeps.
            1. Is there a directory at all?
            2. Did the root look like a bare mount point when we started, and
               does it still?  (`suspect`)
            3. Is the filesystem under it a different one?
```

The third is one `stat` in the ordinary case. The device number is compared
first and the platform is asked for a durable volume id **only when that number
has changed** — which is the one moment the answer can differ, and the moment a
share legitimately remounted has to be told from a share replaced.

Two refusals it deliberately does not make:

- **Emptiness alone is not a signal.** Somebody who deletes every file in their
  inbox by hand has an empty inbox, and refusing to scan it would leave the
  index insisting those files are still there.
- **A `suspect` record is never an identity.** An installation that restarted
  while its share was down would otherwise write down the empty mount point's
  own filesystem and then refuse the real Library for not matching it.

## The rule

**Never a bare "Try again" after an operation that moves files.** The
distinction is the precondition, not the word: *free space where the files are
going, then commit again* names what has to change and stands beside a panel
that has already said how many files moved. A **fault** cannot name a
precondition — it is the kind LibrAIry did not anticipate — so on a path that
touches files it offers *View in Commit* instead, which reads the journal per
file. The generic htmx handler covers every button in the product and therefore
never says it at all. `docs/ui-vocabulary.md` holds the wording;
`tests/test_error_states.py` holds the behaviour.
