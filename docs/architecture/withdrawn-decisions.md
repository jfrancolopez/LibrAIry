# Decisions you took back

History is the journal of operations that **moved files**. A withdrawal moved
nothing — that is the whole point of withdrawing rather than undoing — so it
lives beside the journal rather than in it, under `History → Withdrawn`.

Putting one in the journal would claim a move that never happened, and would
put a row in front of Undo that Undo cannot reverse because there is nothing
there to reverse.

## What a withdrawal records

```
Sent back to Review
03 - Outdoor Miner.flac
1 file · just now
No files moved.
Withdrawn because it conflicted with filing outdoor-miner.flac.
would have become Music/Rock/Wire/Chairs Missing/03 - Outdoor Miner.flac
```

Every way of taking a decision back now goes through one implementation:
withdrawing a correction's approval, cancelling an adoption, cancelling a
quarantine request, cancelling a group restore, and withdrawing a comparison
answer. Three of those five used to remove the plan and leave no trace at all.

## Reasons are recorded, never inferred

The route that withdraws knows why: which button was pressed, and whether the
decision was in a conflict at that moment. That is captured *before* the plan is
deleted, because afterwards the collision is gone with it and any account of
what it resolved would be reconstruction.

Where a caller genuinely does not know — an older database, a withdrawal made
before this was recorded — the record says `Withdrawn` and nothing more. A page
that says "withdrawn to resolve a conflict" about a withdrawal that had nothing
to do with one is worse than a page that says nothing.

`Sent back to Review` and `Request cancelled` are the words on the buttons that
cause them. An approval that merely went **stale** is neither: nobody withdrew
it, it is still waiting, and calling that a cancellation would put words in
somebody's mouth.

## It teaches nothing

Decision Memory learns from decisions that *completed*. A withdrawn decision is
the opposite of one, and making withdrawals visible must not quietly turn them
into evidence — pinned by a test that withdraws four identical decisions and
checks that no pattern gains support.

Unsettled decision events are unhooked from the plan as it goes, so the record
of the choice survives without pointing at a plan that no longer exists. (That
foreign key used to make withdrawing a comparison decision fail outright.)

## There is no re-open

A decision withdrawn a month ago may name files that have since moved.
Offering to reinstate it would be offering to approve something nobody has
looked at. Visibility first; make the decision again if you still want it.
