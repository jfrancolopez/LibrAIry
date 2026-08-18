"""Relpaths LibrAIry keeps for itself, and why there is exactly one of them.

An `items` row is normally a record of a file at a path. One row is not: the
dormant result of an un-adopted optimization, whose bytes are in the encoder's
workspace under `appdata` and whose row has to stay in the `library` root
because `items.root` is CHECK-constrained to the three user roots.

That row still needs a `relpath`, because `items` has:

    UNIQUE (root, relpath)

and it cannot keep the library path it used to hold — an HEVC re-encode of an
MP4 lands on the original's own path, so on Undo the original would come back
to a path the dormant row was still claiming. (`docs/plan/adoption-architecture.md`
records that measurement.)

## Why a convention was not enough

The first version parked it at `_optimization/<job>/<former path>`. That is a
perfectly plausible folder for somebody to create in their own library, and the
moment they did, an internal bookkeeping address would collide with real media
through that same UNIQUE constraint.

So the address is **reserved**, not conventional:

    __librairy_internal__/optimization-dormant/<job-id>/<item-id>

Deliberately unpretty. It is never rendered, never resolved against a root,
never a filesystem destination, and never scanned. Nothing about it needs to
look like a path, which is why the former library path is *not* encoded in it:
`optimization_jobs`, `plan_ops` and `history` all already record where the file
was and where it went, and spelling it a fourth time would be a fourth thing to
keep in agreement.

## The one rule

`validate_dest` refuses this namespace, so no plan, correction, import,
restore or manual destination can put a real file inside it — and the scanner
refuses to index anything found there, so a physical file cannot manufacture a
row that collides with a parked one.

A physical file there is **reported, never hidden**: `library_consistency`
gives it its own bucket with its own remedy, because "scan the library" would
not fix it and calling it ordinary drift would be a lie.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#  One reserved top-level name, in all roots. Not plausible as user media
#  anywhere, so reserving it everywhere costs nothing and removes the question
#  "which roots does this apply to".
RESERVED_TOP = "__librairy_internal__"

#  The only thing inside it today.
DORMANT_OPTIMIZATION = f"{RESERVED_TOP}/optimization-dormant"


class ReservedPathError(ValueError):
    """Something tried to put a real file at an address LibrAIry keeps."""


def is_reserved(relpath: str | None) -> bool:
    """True for the reserved namespace and everything beneath it."""
    if not relpath:
        return False
    first = PurePosixPath(str(relpath).replace("\\", "/")).parts
    return bool(first) and first[0] == RESERVED_TOP


def refuse_reserved(relpath: str, *, kind: str = "destination") -> None:
    if is_reserved(relpath):
        raise ReservedPathError(
            f"{kind} is inside {RESERVED_TOP}, which LibrAIry reserves for its "
            "own bookkeeping"
        )


def dormant_optimization_relpath(job_id: int, item_id: int) -> str:
    """Where an un-adopted result's row parks. Deterministic, and not a path.

    Keyed by both ids so it is stable across any number of adopt/Undo cycles
    and unique per result even if a job were ever relinked.
    """
    return f"{DORMANT_OPTIMIZATION}/{int(job_id)}/{int(item_id)}"


def is_dormant_optimization(relpath: str | None) -> bool:
    return bool(relpath) and str(relpath).startswith(f"{DORMANT_OPTIMIZATION}/")
