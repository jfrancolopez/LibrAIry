# Undo and the order of decisions

**Reversing an old decision must not quietly reverse a newer one.**

Undo has always been plan-scoped, and for a long time that was the whole truth:
a plan filed some files, and reversing it put them back. LibrAIry now produces
*sequences*:

```
file it          →  rename its album      →  replace its encode
set 18 aside     →  restore them          →  correct one of them
FLAC aside       →  MP3 takes the slot    →  reorganise the MP3
```

Each step is an explicit choice somebody made. Blind reversal of the first
discards the ones after it — the last place in the program where a decision
could be overwritten without anybody being asked.

## What counts as a dependency

A later **committed filesystem decision** that moved a file this plan moved,
and that has not itself been reversed. Nothing else does:

| | dependency? |
| --- | --- |
| a later plan moved the same file | **yes** |
| a later plan took the file from where this one put it | **yes** |
| an audit read it | no |
| metadata was measured | no |
| a relationship was discovered | no |
| the search index was updated | no |
| decision memory recorded the choice | no |

None of those are operations, so none of them can appear — a property of the
derivation rather than a list somebody has to maintain.

## How it is derived

From the journal, which is what Undo actually reverses. `history` records every
operation that ran, in order; `plan_ops` gives each one the `item_id` of the
file it moved. Identity is the thing that survives a move —
`Photos/2024/foo.jpg` is a string two plans might share by coincidence, and item
4,127 is the same photograph however often it has been filed.

Where identity is unavailable — an old journal — a later operation that read
**exactly where an earlier one wrote** is still a handover, and counts. That is
deliberately not a general path comparison.

Ordering comes from `history.id` rather than from timestamps. `utc_now()` has
one-second resolution, and a filing plus the correction somebody made
immediately after it are ordinary, not exotic.

**Nothing is stored.** A dependency table alongside `plan_ops` and `history`
would be a second account of the same events, free to disagree with the first.

## The four answers

```
CLEAR      nothing later depends on this
BLOCKED    a later committed decision moved a file this one moved
UNDONE     every operation has already been reversed
UNKNOWN    the journal and the operations disagree — refused, not guessed
DRIFTED    the files are no longer what this decision left behind
```

`DRIFTED` is the existing preflight, asked alongside rather than instead: it
hashes, so it happens behind a button and never on a page load.

## It never cascades

Reversing one decision to make room for reversing another crosses two explicit
choices. LibrAIry names the later decision and stops:

```
FILED  01 - Song.mp3                                     24 August

This cannot be undone yet. A later decision changed 1 of 18 files from this
one — a library correction to A Night at the Opera. Reverse that one first.

[View later decision]
```

Undo that one and the earlier becomes available on its own. A chain unwinds in
reverse order because that is the only order in which each step is a decision
somebody actually took.

`1 of 18` matters: seventeen untouched files do not make reversing all eighteen
safe, and the sentence says so.

## Old plans

A journal from before operations carried item ids is not ambiguous — the
continuation rule still applies — so historical Undo is not gratuitously
blocked. What *is* refused is a plan whose journal rows cannot be tied to its
operations at all: there is then no way to know which files those rows were
about, and "probably independent" is not a thing to say before moving somebody's
files.
