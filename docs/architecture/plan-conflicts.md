# Two decisions that cannot both be right

[Undo sequencing](undo-sequencing.md) gave reversal a sense of order: a
decision that a later decision was built on cannot be reversed blind. This is
the same problem pointing forward.

Two decisions can each be approved on their own and both reach *Waiting for
Commit* while describing incompatible futures:

```
rename    Song.mp3 → New/Song.mp3
replace   expects Song.mp3 at its old name

file      IMG_9002.JPG → Photos/Wedding/IMG_9002.JPG
file      IMG_9002-2.JPG → Photos/Wedding/IMG_9002.JPG

set aside IMG_1001.MOV
file      IMG_1001.HEIC, approved because the MOV stays where it is
```

Each card looks fine. Commit runs the first, the executor's preflight refuses
the second, and you find out about the collision from a failure — of the
decision you already agreed to, for a reason that is also a decision you made.

## Committed dependency vs pending conflict

They are related, they are not the same thing, and neither creates the other.

| | committed dependency | pending conflict |
| --- | --- | --- |
| what it is about | decisions that **have** run | decisions that have **not** run |
| what it affects | Undo | approval and Commit |
| how it is answered | the journal | current operations and proposals |
| where it lives | `undo_sequence.py` | `plan_conflicts.py` |

A plan that has not executed has moved no files, so nothing can have been built
on it: a pending conflict never becomes an Undo dependency, and a test pins
that.

## What counts

Three kinds, and deliberately only three:

* **the same file** — two decisions operate on one `items` row, or on one path
* **the same place** — two decisions intend to occupy one destination
* **a file one was explained by** — a decision would move a file another
  decision's approval was shown as staying put

What does **not** count: touching the same folder, using the same category,
both citing the same relationship without either changing it, or one decision
vacating a path another decision fills. That last one is not a contradiction —
run them in the right order and both succeed, and choosing the order is the
executor's job.

One shared member is enough. A group of eighteen filings and a separate
correction to the seventh of them is a conflict; the other seventeen do not
make it safe.

## Where it is caught

**At approval.** `approve_plan` already refuses a plan that names one file
twice or two files into one destination. Refusing a plan that contradicts a
decision *already waiting* is the same rule one scope up. Nothing is cancelled
by the refusal: the existing decision is untouched, and the message names it so
you know which one to send back.

**On the Commit page**, for the decisions approval cannot see. An arriving file
approved in Review never passes through `approve_plan` — the filing plan is
built when you press Commit — so two arrivals wanting one destination is the
collision that still reaches the queue. Both cards say so, neither offers
Commit, and both are left out of the batch until one is sent back. They stay
approved: which of two decisions to keep is not a question the program is
allowed to answer.

**On Health**, as a count with a link. Health reports; it does not resolve.

## The executor is still authoritative

This runs against the database. Between the page and the move there is a
filesystem other programs can write to, and the hash-verified preflight has not
moved and has not weakened. Pre-Commit detection is an acceleration — it turns
a failure into a sentence — and it is never a substitute.

## Derived, never stored

A `plan_conflicts` table would be a second account of facts that already exist
in `plan_ops`, `proposals` and `plan_relationships`, free to disagree with them
and needing rewriting every time anything is approved or withdrawn. Resolving a
conflict means withdrawing one of the decisions, at which point it stops being
computed.

Conflicts are found by grouping claims, never by comparing decisions pairwise.
A self-join over waiting operations is quadratic — five thousand waiting
operations is twenty-five million comparisons to find the handful that collide.
Grouping by the thing claimed is one sort.
