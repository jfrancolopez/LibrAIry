# Release acceptance

Before a version of LibrAIry is tagged, four questions get answered by
rehearsal rather than by argument:

- can somebody install this from nothing?
- can an existing installation move forward without losing what it decided?
- can it recover when the database and the files disagree?
- can it get back to where it was?

```
python scripts/release_acceptance.py
python scripts/release_acceptance.py --json
```

The drill prints one row per required gate with a status of `PASS`, `FAIL`,
`BLOCKED` or `NOT TESTED`. **No gate is ever omitted, and none is rounded up.**
A candidate is `READY` only when every row is `PASS` — a gate that could not be
run on the machine doing the running leaves the verdict `BLOCKED`, which is the
honest answer and not a lesser kind of pass.

Which means a gate has to be *answerable*. "apt package versions pinned" sat at
`NOT TESTED` for ever, because the correct answer is "no, deliberately": moving
the base image forward is how the image clears CVEs Debian will not backport.
A question whose right answer is permanently unavailable is not a gate, it is a
footnote, and it was keeping the matrix from ever reading anything. It was
replaced by the property that is true and that protects somebody — the base is
pinned to one tag and the image takes the security updates published since.

It publishes nothing. It creates no tag, moves no tag, pushes no image and
drafts no release; the point is to find out whether a release *would* be safe.
Everything it writes goes to temporary directories it makes itself.

## What each area proves

| area | what it rehearses |
| --- | --- |
| **Build** | one authoritative version, image provenance labels, pinned external inputs, no credential in the repository or the sample config, valid compose, release notes newer than the code they describe |
| **Fresh install** | an empty database migrates to head, every page renders on an empty installation, no provider credential is required, one file goes discovery → Review → Commit → Search → Undo, and a restart changes nothing |
| **Migration** | representative historical databases reach the current schema with their history, approvals, provenance, policy and learned decisions intact; the chain has no gaps; a newer schema is refused rather than downgraded; a failed step leaves the pre-upgrade copy usable |
| **Recovery** | a snapshot is a working database, an imperfect restore is explained and never repaired, authoritative state is never discarded, and a measurement of bytes that changed is never reused |
| **Rollback** | the previous build with its own pre-upgrade snapshot — and the documentation is checked for the sentences that would make that untrue |
| **Runtime** | the production image builds, carries the revision it was built from, contains no developer tooling and no repository or fixture data, starts with no credentials, creates a sound database, serves every page, survives a clean restart and a SIGKILL without running anything by itself, and shuts down cleanly. Needs a container runtime |
| **Compose** | the real topology comes up on throwaway volumes, reports healthy, publishes a port you can reach, can write every bind mount, and leaves the database behind when it goes down. Needs a container runtime |
| **Quality** | ruff, and the whole test suite — not a subset |
| **Documentation** | the canonical install, upgrade, backup, restore, reconcile and rollback path exists and is one path |

Most of these are also tests: `pytest tests/test_release_acceptance.py`. The
drill runs them and maps each onto its gate, so the matrix and the suite cannot
drift apart.

## The migration fixtures

Historical databases are built by replaying this project's own migrations up to
a generation and stopping. That *is* the historical schema — the same
statements, in the same order, that produced it at the time — and it is the
strongest evidence available without an archived production database, which
release acceptance may not touch.

What it cannot prove is that a real database of that era held nothing else,
which is why each fixture carries representative rows and every one of them is
checked by name on the far side of the upgrade.

| generation | what it represents |
| --- | --- |
| 10 | before the audit and the catalogs; search and backup exist |
| 22 | the audit and optimization era, before plan withdrawals |
| 36 | before the multi-tool metadata cache |
| 42 | after decision memory, before relationships and Format Policy |
| 46 | the release before this one |

## The rollback contract

**A container rollback is not a database rollback.** Once an upgrade has
migrated the database, an older build cannot run against it — and does not try:
the refusal is in `db.migrate`, enforced rather than documented, and there is a
test that pins it.

So the safe rollback unit is the previous image *plus* the pre-upgrade database
snapshot *plus* whatever reconciliation the gap needs. Everything decided after
that snapshot is not in it, and the files those decisions moved are still where
they were moved to. That gap is expected, and closing it is what
[Reconcile](restore-reconciliation.md) is for.

The full procedure is in [Running LibrAIry](operations.md#roll-back).
