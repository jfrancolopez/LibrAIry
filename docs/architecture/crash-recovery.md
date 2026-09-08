# What LibrAIry says after it is killed

A move is one filesystem action followed by four database writes:

```
os.rename(src, dest)      the file is now in the library
plan_ops.result = done    the operation is finished
history(... 'ok')         what Undo reverses
items.root / relpath      what Browse, Search and every count read
```

Only the first can be made atomic with anything. A process killed between any
two of the rest — a container restart, an OOM kill, a power cut — leaves an
installation where the bytes have moved and the record has not caught up.

**A crash may lose work. It may never lose a file, and it may never leave a
record saying something happened which did not.**

That rule is asked of every seam in `tests/test_recovery_drills.py`, by killing
a real process with `SIGKILL` at a chosen instant and then looking at what
LibrAIry says.

## What it said before

Every answer was wrong in the same direction, and none of them lost a file.

| What happened | What LibrAIry said | What was true |
|---|---|---|
| Killed after the rename, before the record | `skipped_missing` — *the source is gone* | LibrAIry had moved it a millisecond earlier |
| …and so, no journal row | Undo could not offer the file at all | the file was filed for ever, by a decision nobody could see |
| …and so, the index still named the inbox | the next scan reported the file vanished, then discovered LibrAIry's own copy as something new | one file, two rows, one of them a phantom |
| Killed inside a cross-filesystem copy | the half-written `.part-` file was indexed as ordinary media | it was half of somebody's file, under a name LibrAIry wrote |
| Killed after a reversal moved the bytes | *not put back — the file is no longer where LibrAIry left it* | the file was exactly where the reversal put it |
| Killed at any point | the Dashboard said **Commit · 1 running** | nothing had been running for a week |

## What decides

**The bytes.** If the destination an operation was approved to write holds
exactly the fingerprint the plan recorded, that operation happened — the same
rule [restore reconciliation](restore-reconciliation.md) uses, and for the same
reason: a path is a claim, and only the bytes settle it. A destination holding
*different* bytes is not that move, and is still reported as missing.

**That a run was interrupted.** The recovery is allowed to conclude anything
only while the plan still says `executing` — a status written before the first
file moves and rewritten when the run ends, so a process that holds the lock
and finds `executing` is looking at the remains of a run that died. A first run
can never take this path, however identical the file at the destination.

**That the reversal has not already happened.** A plan reversed yesterday looks
exactly like a reversal killed a second ago: the file is back at its old address
with its old bytes. The journal is what tells them apart, and
`undo_sequence.reversed_already` is the one place that question is answered.

## The lock is the evidence

`plans.status='executing'` cannot say whether anything is running, because
nothing rewrites it when a process dies. The `flock` can: the kernel releases it
when the holder dies, however it dies. So a plan that says `executing` while the
lock is free is a plan whose run is gone — no heartbeat, no timeout, nothing
stored, and nothing that can go stale.

The answer is deliberately one-sided. The worker holds the same lock for a whole
cycle, so during a scan an interrupted commit is reported as nothing at all,
until the machine is idle a few seconds later. Being told a minute late that a
commit stopped is much better than being told a running one has.

## What is never done

**Nothing is repaired by looking.** The recovery happens inside the next commit
or the next Undo — the same two doors, holding the same lock, doing the same
verification. No page, poll or scan moves a file, and Health only says what it
found.

**Nothing guesses at a renamed destination.** A move that renumbered around a
collision landed at a name only the dead run knew. Recovery asks about the
approved destination and nothing else; a plan whose file is at `photo (2).jpg`
is left to say `skipped_missing`, which is the truthful answer available.

**Nothing overwrites a second row.** `items` has `UNIQUE (root, relpath)`, and a
library scan between the crash and the recovery will have discovered the moved
file and given it a row of its own. Two rows for one file are left as they are,
and the index goes on reporting the inbox copy as missing, which is a thing the
owner can see and act on. Writing a third answer over one of them would not be.

**Half a file is never a file.** A `.part-<plan>` left by an interrupted copy is
refused an `items` row — it would otherwise be browsable, searchable, counted in
the library total and queued for backup — and the commit that owns it clears it
before it moves that file again. It is `paths.IN_FLIGHT`, spelled in one place
because the executor writes it and the scanner has to recognise it.
