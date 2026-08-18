"""What "in the library right now" means, written once.

An `items` row is a record of a file, not a promise that the file is there.
`missing_since` is how the row says so, and it is set for two very different
reasons:

- **a file went away** — an unmounted share, something deleted over SMB. The
  row survives on purpose, carrying every decision made about that file.
- **a representation is dormant** — an optimized copy that was adopted and then
  un-adopted. Its bytes are back in the job's staging directory under appdata,
  which `items.root` cannot name, so the row stays at the library path it used
  to occupy and is marked missing instead.

Both mean the same thing to anything doing work: *there is no file at that
path*. Counting either one as a library file makes the count a claim about
storage that the disk does not support, and handing either one to a copier,
a hasher or a prober asks it to open something that is not there.

The two differ in exactly one place — drift reporting. A vanished file is
drift worth telling somebody about; a dormant representation is accounted for,
so `DORMANT_OPTIMIZATION_RESULT` exists to tell them apart there and nowhere
else.

This module deliberately imports nothing. Every other module can use it.
"""

from __future__ import annotations

# For a query whose `items` table is unaliased.
LIVE = "missing_since IS NULL"


def live(alias: str = "i") -> str:
    """The same predicate for an aliased `items`."""
    return f"{alias}.missing_since IS NULL"


def dormant_optimization_result(alias: str = "i") -> str:
    """A missing row whose absence is already explained by an optimization job.

    True only for the recorded result of a job — `optimization_jobs.result_item_id`
    — while it is missing. That is the un-adopted state: the bytes are in the
    job's own staging directory, verified, and one click from coming back.

    Used to keep the Browse consistency panel honest. A row that matches this
    is not drift and there is no remedy to offer for it, so reporting it as a
    missing library file would be inventing a problem. A row that does *not*
    match — an ordinary file that has gone — still gets reported, because that
    is the panel's job.
    """
    return (
        f"{alias}.missing_since IS NOT NULL AND EXISTS ("
        f"SELECT 1 FROM optimization_jobs j WHERE j.result_item_id = {alias}.id)"
    )
