"""Everything that has to be true before an adoption plan may exist.

One layer, asked twice: once before the plan is written, and again by the
executor immediately before it moves anything. The first is what stops a plan
that could never work from reaching Commit; the second is what stops a plan
that *could* have worked when it was approved and cannot now, because
something changed underneath it in the hours between.

Read-only with respect to user files. Nothing here creates, moves, renames or
deletes anything: it opens files to hash them and does nothing else.

## Collisions refuse, they do not renumber

`resolve_collision` turning an import into `photo (2).jpg` is right — a second
file arrived and both are wanted. `concert (2).flac` sitting beside the
`concert.wav` it was supposed to replace is not: nobody asked for two copies,
and the one thing the person did ask for — a smaller version of that recording
in its place — did not happen. An occupied destination means a fact changed
since approval, and the honest answer is to stop.

## Except the one case that looks like a collision and is not

An HEVC re-encode of `Movies/film.mkv` produces `Movies/film.mkv`. The target
is occupied — by the original, by the very file operation 1 moves out of the
way first. `same_path` below is that case, and it is deliberately narrow: the
occupant must be the exact original, with the exact fingerprint the plan
carries, and operation 1 must be the one that vacates it. Anything else at
that path is a collision like any other.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from librairy.config import Settings
from librairy.fingerprint import blake2b_file
from librairy.optimization_adopt import target_relpath
from librairy.optimization_source import SourceRefused, job_output
from librairy.paths import PathValidationError, validate_dest, validate_relpath


@dataclass(frozen=True)
class Refusal:
    """Why this optimization cannot be adopted, in terms a person can act on."""

    code: str
    message: str

    eligible: bool = False


@dataclass(frozen=True)
class Adoptable:
    """The exact shape of the plan that may now be written.

    Every path here is settled: the planner writes these values and computes
    nothing of its own, so what preflight checked and what the plan does cannot
    drift apart.
    """

    job_id: int
    item_id: int
    original_relpath: str
    original_fingerprint: str
    preserved_relpath: str
    generated_relpath: str
    generated_fingerprint: str
    target_relpath: str
    original_bytes: int
    optimized_bytes: int
    #  True when the optimized file lands exactly where the original is — an
    #  HEVC re-encode keeping its container. Legal only because operation 1
    #  moves that same file out first.
    same_path: bool = False

    eligible: bool = True


def adoption_preflight(
    conn: sqlite3.Connection, settings: Settings, job_id: int
) -> Adoptable | Refusal:
    """Can this optimization be adopted, and if so, into what exactly."""
    job = conn.execute(
        "SELECT * FROM optimization_jobs WHERE id=?", (int(job_id),)
    ).fetchone()
    if job is None:
        return Refusal("no_job", "that optimization no longer exists")

    # 1. The generated file: verified, in its own workspace, unchanged since.
    #    Same checks the executor's resolver runs, from the same function.
    try:
        generated = job_output(conn, settings, int(job_id))
    except SourceRefused as exc:
        return Refusal(exc.code, str(exc))

    # 2. Nobody is already adopting it.
    active = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?"
        " AND status IN ('approved','executing')",
        (int(job_id),),
    ).fetchone()
    if active is not None:
        return Refusal(
            "already_waiting",
            "this optimized version is already waiting for Commit",
        )

    # 3. The original is still a live library file, and still the one the job
    #    was made from.
    if job["item_id"] is None:
        return Refusal("no_source_item", "that optimization has no source file on record")
    item = conn.execute(
        "SELECT * FROM items WHERE id=? AND missing_since IS NULL", (int(job["item_id"]),)
    ).fetchone()
    if item is None:
        return Refusal("original_gone", "the original file is no longer in the library")
    if item["root"] != "library":
        return Refusal(
            "original_not_in_library",
            f"the original is in {item['root']}, not the library",
        )
    try:
        original = validate_relpath(settings.library_dir, item["relpath"], kind="source")
    except PathValidationError as exc:
        return Refusal("original_path_invalid", str(exc))
    if not original.is_file():
        return Refusal("original_gone", "the original file is not where it was")
    original_fingerprint = blake2b_file(original)
    if original_fingerprint != (item["fingerprint"] or ""):
        return Refusal(
            "original_changed",
            "the original file has changed since it was optimized",
        )
    if original_fingerprint != (job["fingerprint"] or ""):
        return Refusal(
            "original_changed",
            "the original is not the file this optimization was made from",
        )

    # 4. Where the optimized copy goes: same folder, same stem, new suffix.
    #    Deterministic, and the destination classifier is deliberately not
    #    consulted — the owner already decided where this file lives.
    target = target_relpath(item["relpath"], generated.path.name)
    try:
        target_path = validate_dest(settings.library_dir, target)
    except PathValidationError as exc:
        return Refusal("target_path_invalid", str(exc))

    same_path = target == item["relpath"]
    if target_path.exists() and not same_path:
        return Refusal(
            "target_taken",
            f"there is already a file at {target}",
        )
    if same_path:
        # The narrow exception. The occupant has to be the original itself —
        # not merely something with the same name, and not something with the
        # same bytes at a different inode's path.
        if target_path.resolve() != original.resolve():
            return Refusal("target_taken", f"there is already a file at {target}")
        if blake2b_file(target_path) != original_fingerprint:
            return Refusal(
                "target_taken",
                f"the file at {target} is not the original this plan preserves",
            )

    # 5. Where the original is preserved: the same relative path, in quarantine.
    preserved = item["relpath"]
    try:
        preserved_path = validate_dest(settings.quarantine_dir, preserved)
    except PathValidationError as exc:
        return Refusal("preserved_path_invalid", str(exc))
    if preserved_path.exists():
        return Refusal(
            "preserved_path_taken",
            "there is already a file where the original would be preserved",
        )

    # 6. A protected root is a statement that LibrAIry does not reorganise
    #    inside it, and a Format Policy folder is a statement that its
    #    originals are not to be traded away. Adopting an optimized copy in
    #    place of the original is both of those, so both refuse it — checked
    #    here as well as at advisory time, because a folder can be protected
    #    between an opportunity appearing and somebody acting on it.
    from librairy.format_policy import protecting

    protector = protecting(conn, item["relpath"])
    if protector:
        return Refusal(
            "protected",
            f"{protector} is protected, so LibrAIry will not change files inside it",
        )

    return Adoptable(
        job_id=int(job_id),
        item_id=int(item["id"]),
        original_relpath=item["relpath"],
        original_fingerprint=original_fingerprint,
        preserved_relpath=preserved,
        generated_relpath=f"{int(job_id)}/{generated.path.name}",
        generated_fingerprint=generated.fingerprint,
        target_relpath=target,
        original_bytes=original.stat().st_size,
        optimized_bytes=generated.path.stat().st_size,
        same_path=same_path,
    )


def target_is_clear(
    conn: sqlite3.Connection, settings: Settings, plan_id: str
) -> Refusal | None:
    """The volatile half, re-asked at execution time.

    Approval can be hours before Commit, and in between somebody can drop a
    file over SMB at exactly the destination. The executor refuses the
    operation on its own when that happens, but by then operation 1 has already
    moved the original into quarantine and the compensation has to run. Asking
    here, before the first operation, means the ordinary case is that nothing
    moved at all.

    The same-path plan is exempt for the same reason as above: its target is
    occupied by the file operation 1 is about to take away.
    """
    ops = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,)
    ).fetchall()
    adopt = next((op for op in ops if op["src_root"] == "optimization"), None)
    if adopt is None:
        return None
    preserve = next((op for op in ops if op["dest_root"] == "quarantine"), None)
    dest = settings.library_dir / adopt["dest_relpath"]
    if not dest.exists():
        return None
    if preserve is not None and preserve["src_relpath"] == adopt["dest_relpath"]:
        # Same-path: the occupant must still be the exact original that
        # operation 1 will move.
        if blake2b_file(dest) == preserve["src_fingerprint"]:
            return None
        return Refusal(
            "target_taken",
            "the file being replaced is not the one this plan was approved for",
        )
    return Refusal("target_taken", "something is already at the destination")

#  There is no third check after operation 1, because `_execute_adoption_op`
#  already refuses on an occupied destination and that *is* the post-preserve
#  recheck: by the time it runs, operation 1 has moved the original out, so a
#  file at the target is something else and operation 2 stops rather than
#  overwriting it.
