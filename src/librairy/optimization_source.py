"""The one door between the encoder's workspace and a plan operation.

`optimization` is a plan **source namespace**, not a root. It is deliberately
absent from `executor._root_path` and `planner._root_path`, so nothing that
resolves a root generically can reach it: a plan naming `optimization` as a
*destination* fails with "unknown root", which is the correct answer and comes
for free rather than from a check somebody has to remember to write.

Everything that may read from it comes through here.

## Hash equality is not authorization

An operation's `src_fingerprint` proves the bytes are the ones the operation
expected. It does not prove they are the verified output of *this* job. All of
these satisfy the hash and none of them is an authorised source:

- a copy of the output placed at a different path
- another job's output that happens to match
- a stale output left in the job's own directory by an interrupted run, which
  is in the right directory under the right name

So the resolver derives the path from the job rather than trusting the one on
the operation, and requires every link in

    plan.optimization_job_id
      -> the job
      -> the job's verified state
      -> the canonical output path for that job and preset
      -> the fingerprint recorded when that output was verified
      -> op.src_relpath and op.src_fingerprint
      -> the bytes on disk

to agree before a single byte moves. Refusals are structured, and every one of
them happens before any filesystem mutation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.fingerprint import blake2b_file

# The name a plan operation uses. Not a value `items.root` may hold, not a
# Browse root, not a scanner root, and not a destination.
OPTIMIZATION_ROOT = "optimization"

# Where an adopted result is allowed to land. One entry, on purpose: adoption
# files a replacement where its original lived, and any other destination would
# be a different feature.
ADOPTION_DESTINATIONS = frozenset({"library"})

# The job states whose output may be adopted. `ready` is "verified and waiting
# for a decision"; `executing` and `verifying` are not finished, and a failed
# or cancelled job has no output worth the name.
ADOPTABLE_STATES = frozenset({"ready"})


class SourceRefused(Exception):
    """A generated file was named as a plan source and is not authorised.

    Carries a machine-readable `code` as well as a sentence, because the
    difference between "the job is not verified" and "the bytes changed" is
    what a person needs in order to know whether to wait or re-run.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedSource:
    """A generated file that has passed every check."""

    path: Path
    job_id: int
    fingerprint: str


def resolve_optimization_source(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    plan_id: str,
    src_relpath: str,
    src_fingerprint: str,
    dest_root: str,
) -> ResolvedSource:
    """The generated file this operation may read, or a refusal.

    Read-only with respect to the filesystem, and it never returns a path it
    has not proved. The order is chosen so the cheap structural checks refuse
    before the expensive one — hashing the file is last, and only reached once
    the path is known to be the right one.
    """
    # 1. This source form is only legal on a plan that names a job.
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise SourceRefused("no_plan", "that plan does not exist")
    job_id = plan["optimization_job_id"]
    if job_id is None:
        raise SourceRefused(
            "plan_not_linked",
            "this plan does not come from an optimization job, so it may not "
            "read from the encoder's workspace",
        )

    # 2. The job exists, and it is the one the plan names. There is no path
    #    from `src_relpath` to a job id, by construction: the job comes from
    #    the plan and the path is derived from the job.
    job = conn.execute(
        "SELECT * FROM optimization_jobs WHERE id=?", (int(job_id),)
    ).fetchone()
    if job is None:
        raise SourceRefused("no_job", "the optimization job this plan names is gone")

    resolved = job_output(conn, settings, int(job["id"]))

    # 6. The operation must name that exact output, and carry that exact hash.
    expected_relpath = f"{int(job['id'])}/{resolved.path.name}"
    if src_relpath != expected_relpath:
        raise SourceRefused(
            "not_this_jobs_output",
            "that is not the file this optimization produced",
        )
    if src_fingerprint != resolved.fingerprint:
        raise SourceRefused(
            "fingerprint_not_the_verified_one",
            "this operation does not describe the verified output",
        )

    # 7. The destination has to be one adoption is allowed to write to.
    if dest_root not in ADOPTION_DESTINATIONS:
        raise SourceRefused(
            "illegal_destination",
            f"an optimized file may not be filed into {dest_root}",
        )

    # 8. Nobody else is already adopting this job's output.
    other = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=? AND id != ?"
        " AND status IN ('approved','executing')",
        (int(job["id"]), plan_id),
    ).fetchone()
    if other is not None:
        raise SourceRefused(
            "already_being_adopted",
            "another approved plan is already adopting this optimization",
        )
    return resolved


def job_output(
    conn: sqlite3.Connection, settings: Settings, job_id: int
) -> ResolvedSource:
    """The verified output of one job, or a refusal. No plan involved.

    Everything here is about the job and its bytes, which is why preflight can
    ask the same question before a plan exists and get the same answer.
    """
    from librairy.optimization_exec import PRESET_SUFFIX
    from librairy.optimization_queue import staging_root

    job = conn.execute(
        "SELECT * FROM optimization_jobs WHERE id=?", (int(job_id),)
    ).fetchone()
    if job is None:
        raise SourceRefused("no_job", "that optimization job is gone")

    # 3. It finished and it passed.
    if job["state"] not in ADOPTABLE_STATES:
        raise SourceRefused(
            "job_not_ready",
            f"that optimization is {job['state']}, not ready for review",
        )
    if job["verified"] != "passed":
        raise SourceRefused(
            "job_not_verified", "that optimization has not passed verification"
        )

    # 4. It has a canonical output location, derived from the job id and the
    #    preset — never from the operation, and never from a stored path.
    suffix = PRESET_SUFFIX.get(job["preset"])
    if suffix is None:
        raise SourceRefused("unknown_preset", "that optimization used an unknown preset")
    canonical_name = f"output{suffix}"
    if (job["output_relpath"] or "") != canonical_name:
        raise SourceRefused(
            "output_not_canonical",
            "that job's recorded output is not the one its preset produces",
        )

    # 5. It has a fingerprint recorded at the moment it was verified. Jobs
    #    verified before that column existed have none, and are refused rather
    #    than re-hashed — re-hashing would authorise whatever is there now.
    recorded = (job["output_fingerprint"] or "").strip()
    if not recorded:
        raise SourceRefused(
            "no_recorded_output_fingerprint",
            "that optimization has no recorded output fingerprint; run it again",
        )

    # 9. The path, built from the *resolved* workspace root and the job id.
    #
    #    Resolving the root rather than the whole path is the distinction that
    #    matters. Symlinks *above* the workspace are the operator's own mount
    #    layout — `/var` is a link to `/private/var` on macOS, and a bind mount
    #    or a moved appdata volume produces the same thing — and refusing those
    #    would refuse every adoption on such a host. Found exactly that way: the
    #    first browser click returned "that path is not in the workspace".
    #
    #    Symlinks *below* it are the attack, and they are still refused.
    staging = staging_root(settings).resolve()
    path = staging / str(int(job["id"])) / canonical_name

    # 10. No symlink anywhere from the workspace down to the file — checked
    #     *before* containment, because `resolve()` follows links silently and
    #     a resolved path that lands back inside the workspace would then pass
    #     a containment check while pointing at something else entirely.
    _refuse_symlinks(staging, path)
    if not path.resolve(strict=False).is_relative_to(staging):
        raise SourceRefused(
            "outside_the_workspace", "that path is not inside the encoder's workspace"
        )

    if not path.is_file():
        raise SourceRefused(
            "output_missing", "the optimized file is no longer in its workspace"
        )

    # 11. And finally the bytes themselves.
    actual = blake2b_file(path)
    if actual != recorded:
        raise SourceRefused(
            "bytes_changed", "the optimized file has changed since it was verified"
        )

    return ResolvedSource(path=path, job_id=int(job["id"]), fingerprint=actual)


def undo_destination(
    conn: sqlite3.Connection, settings: Settings, entry: sqlite3.Row
) -> Path:
    """Where an adopted file goes back to. The one authorised write here.

    Adoption reads `optimization -> library`; its Undo writes the same file
    back to the same place. That is a *reverse of a recorded operation*, not a
    general destination: the path is derived from the plan's job exactly as the
    forward direction derives it, so there is no relpath a caller could supply
    that would put a file anywhere else in the workspace.

    Everything else that resolves a destination goes through `_root_path`,
    which does not know this namespace at all.
    """
    from librairy.optimization_exec import PRESET_SUFFIX
    from librairy.optimization_queue import staging_root

    if entry["src_root"] != OPTIMIZATION_ROOT:
        raise SourceRefused("not_an_adoption", "that operation did not come from a job")
    plan = conn.execute(
        "SELECT optimization_job_id FROM plans WHERE id=?", (entry["plan_id"],)
    ).fetchone()
    if plan is None or plan["optimization_job_id"] is None:
        raise SourceRefused(
            "plan_not_linked", "that plan is not linked to an optimization job"
        )
    job = conn.execute(
        "SELECT id, preset FROM optimization_jobs WHERE id=?",
        (int(plan["optimization_job_id"]),),
    ).fetchone()
    if job is None:
        raise SourceRefused("no_job", "the optimization job this plan names is gone")
    suffix = PRESET_SUFFIX.get(job["preset"])
    if suffix is None:
        raise SourceRefused("unknown_preset", "that optimization used an unknown preset")

    staging = staging_root(settings).resolve()
    path = staging / str(int(job["id"])) / f"output{suffix}"
    if not path.resolve(strict=False).is_relative_to(staging):
        raise SourceRefused(
            "outside_the_workspace", "that path is not inside the encoder's workspace"
        )
    _refuse_symlinks(staging, path, allow_missing=True)
    return path


def is_optimization_source(root: str) -> bool:
    return root == OPTIMIZATION_ROOT


def _refuse_symlinks(base: Path, path: Path, *, allow_missing: bool = False) -> None:
    """No link on the way from `base` down to `path`, inclusive.

    A symlink at any level turns a containment check into a lie: `resolve()`
    follows it, and the resolved path can sit anywhere while the relative path
    still looks like it belongs here.
    """
    relative = path.relative_to(base) if path.is_relative_to(base) else None
    if relative is None:
        raise SourceRefused("outside_the_workspace", "that path is not in the workspace")
    walked = base
    for part in relative.parts:
        walked = walked / part
        if walked.is_symlink():
            raise SourceRefused(
                "symlink", "part of that path is a link, which is not allowed here"
            )
        if not walked.exists() and not allow_missing:
            raise SourceRefused(
                "output_missing", "the optimized file is no longer in its workspace"
            )
