"""Hash equality is required and is not authorization.

`optimization` is a plan **source namespace**. It is not in `_root_path` in
either the executor or the planner, so a plan naming it as a *destination*
fails with "unknown root" before any check of mine runs — the namespace is
source-only by construction rather than by a rule someone has to remember.

What is left is the source direction, and the thing worth being strict about is
that a matching fingerprint proves the bytes are the ones the operation
expected and nothing more. A copy at the wrong path satisfies it. Another job's
output satisfies it. A stale output left in the right directory under the right
name by an interrupted run satisfies it best of all. Each of those gets its own
test below.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.optimization_source import (
    SourceRefused,
    resolve_optimization_source,
)
from librairy.planner import OperationSpec, PlanApprovalError, approve_plan, create_plan, utc_now
from librairy.scanner import scan_root

ORIGINAL = "Music/Live/concert.wav"
TARGET = "Music/Live/concert.flac"
PRESERVED = "Music/Live/concert.wav"


@pytest.fixture
def scene(tmp_path: Path):
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)

    original = settings.library_dir / ORIGINAL
    original.parent.mkdir(parents=True)
    original.write_bytes(b"the original recording" * 500)
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (ORIGINAL,)
    ).fetchone()

    job_id = seed_job(conn, item)
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / "output.flac"
    output.write_bytes(b"the verified optimized copy" * 300)
    fingerprint = blake2b_file(output)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (fingerprint, job_id),
    )
    return conn, settings, int(item["id"]), job_id, fingerprint


def seed_job(conn, item, *, state: str = "ready", verified: str = "passed",
             relpath: str = ORIGINAL) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, 'audio-to-flac', 'lossless', 'WAV',
                      'FLAC', 'flac-lossless', 100, 60, 60, ?, ?, 'output.flac',
                      '', ?, ?)
            """,
            (item["id"], relpath, item["fingerprint"], state, verified,
             utc_now(), utc_now()),
        ).lastrowid
    )


def adoption_plan(conn, settings, job_id: int, fingerprint: str, *,
                  src_relpath: str | None = None,
                  op_fingerprint: str | None = None,
                  link_job: int | None = -1) -> str:
    """The two operations, built by the real planner."""
    plan_id = create_plan(
        conn,
        [
            OperationSpec("quarantine", ORIGINAL, "quarantine", PRESERVED,
                          src_root="library"),
            OperationSpec(
                "move",
                src_relpath if src_relpath is not None else f"{job_id}/output.flac",
                "library",
                TARGET,
                src_root="optimization",
                src_fingerprint=op_fingerprint if op_fingerprint is not None else fingerprint,
            ),
        ],
        settings,
    )
    linked = job_id if link_job == -1 else link_job
    conn.execute(
        "UPDATE plans SET optimization_job_id=? WHERE id=?", (linked, plan_id)
    )
    return plan_id


def resolve(conn, settings, plan_id: str, **overrides):
    op = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? AND src_root='optimization'", (plan_id,)
    ).fetchone()
    kwargs = {
        "plan_id": plan_id,
        "src_relpath": op["src_relpath"],
        "src_fingerprint": op["src_fingerprint"],
        "dest_root": op["dest_root"],
    }
    kwargs.update(overrides)
    return resolve_optimization_source(conn, settings, **kwargs)


# --- the authorised case ----------------------------------------------------------


def test_the_verified_output_of_the_linked_job_resolves(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = adoption_plan(conn, settings, job_id, fingerprint)

    resolved = resolve(conn, settings, plan_id)

    assert resolved.job_id == job_id
    assert resolved.fingerprint == fingerprint
    assert resolved.path == (
        settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    )


def test_a_plan_with_a_valid_optimization_source_approves(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = adoption_plan(conn, settings, job_id, fingerprint)

    assert approve_plan(conn, plan_id, settings)


# --- path shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "relpath",
    [
        "../../../etc/passwd",
        "1/../../secrets.db",
        "/etc/passwd",
        "..",
        "1/./output.flac",
    ],
)
def test_a_crafted_relative_path_is_refused(scene, relpath: str) -> None:
    """Refused for naming something other than this job's canonical output —
    which is a stronger check than sanitising the path, because the path is
    never used to find the file in the first place."""
    conn, settings, _, job_id, fingerprint = scene

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint),
                src_relpath=relpath)

    assert refusal.value.code == "not_this_jobs_output"


def test_a_path_inside_appdata_but_outside_the_job_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    (settings.appdata_dir / "librairy.db").write_bytes(b"x")

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint),
                src_relpath="../../librairy.db")

    assert refusal.value.code == "not_this_jobs_output"


# --- the wrong file, even with the right bytes ------------------------------------


def test_another_jobs_output_is_refused(scene) -> None:
    conn, settings, item_id, job_id, fingerprint = scene
    item = conn.execute("SELECT id, fingerprint FROM items WHERE id=?", (item_id,)).fetchone()
    other = seed_job(conn, item, relpath="Music/Live/another.wav")
    other_dir = settings.appdata_dir / "optimization" / "jobs" / str(other)
    other_dir.mkdir(parents=True)
    shutil.copy2(
        settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac",
        other_dir / "output.flac",
    )
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (fingerprint, other),
    )

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint),
                src_relpath=f"{other}/output.flac")

    assert refusal.value.code == "not_this_jobs_output"


def test_a_different_file_in_the_same_job_directory_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    directory = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    shutil.copy2(directory / "output.flac", directory / "output.mp4")

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint),
                src_relpath=f"{job_id}/output.mp4")

    assert refusal.value.code == "not_this_jobs_output"


def test_identical_bytes_at_an_unauthorised_path_are_still_refused(scene) -> None:
    """The one the whole module exists for. The hash matches perfectly."""
    conn, settings, item_id, job_id, fingerprint = scene
    item = conn.execute("SELECT id, fingerprint FROM items WHERE id=?", (item_id,)).fetchone()
    other = seed_job(conn, item, relpath="Music/Live/another.wav")
    other_dir = settings.appdata_dir / "optimization" / "jobs" / str(other)
    other_dir.mkdir(parents=True)
    copy = other_dir / "output.flac"
    shutil.copy2(
        settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac", copy
    )

    assert blake2b_file(copy) == fingerprint  # byte for byte

    with pytest.raises(SourceRefused):
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint),
                src_relpath=f"{other}/output.flac")


# --- links ------------------------------------------------------------------------


def test_a_symlink_to_another_appdata_file_is_refused(scene, tmp_path: Path) -> None:
    conn, settings, _, job_id, fingerprint = scene
    output = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    elsewhere = settings.appdata_dir / "elsewhere.flac"
    output.replace(elsewhere)
    output.symlink_to(elsewhere)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint))

    assert refusal.value.code == "symlink"


def test_a_symlink_out_of_appdata_entirely_is_refused(scene, tmp_path: Path) -> None:
    conn, settings, _, job_id, fingerprint = scene
    outside = tmp_path / "outside.flac"
    output = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    output.replace(outside)
    output.symlink_to(outside)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint))

    assert refusal.value.code == "symlink"


def test_a_symlinked_job_directory_is_refused(scene, tmp_path: Path) -> None:
    """The subtler one: the file is real, the directory above it is the link,
    and `resolve()` would follow it and then pass a containment check."""
    conn, settings, _, job_id, fingerprint = scene
    jobs = settings.appdata_dir / "optimization" / "jobs"
    real = tmp_path / "real-job"
    (jobs / str(job_id)).replace(real)
    (jobs / str(job_id)).symlink_to(real, target_is_directory=True)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, _plan(conn, settings, job_id, fingerprint))

    assert refusal.value.code == "symlink"


# --- the job's own state ----------------------------------------------------------


def test_an_interrupted_jobs_output_is_refused(scene) -> None:
    """It is in the right directory under the right name, which is exactly why
    the check cannot be "is there a file here"."""
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)
    conn.execute(
        "UPDATE optimization_jobs SET state='running', verified='' WHERE id=?", (job_id,)
    )

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "job_not_ready"


def test_an_unverified_output_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)
    conn.execute("UPDATE optimization_jobs SET verified='' WHERE id=?", (job_id,))

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "job_not_verified"


def test_a_job_verified_before_the_hash_was_recorded_is_refused(scene) -> None:
    """Rather than re-hashing, which would authorise whatever is there now."""
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)
    conn.execute("UPDATE optimization_jobs SET output_fingerprint='' WHERE id=?", (job_id,))

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "no_recorded_output_fingerprint"


def test_a_manually_created_file_with_the_expected_name_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)
    output = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    output.write_bytes(b"something a person put here")

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "bytes_changed"


def test_a_missing_output_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)
    (settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac").unlink()

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "output_missing"


# --- the plan's own link ----------------------------------------------------------


def test_a_plan_with_no_optimization_job_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = adoption_plan(conn, settings, job_id, fingerprint, link_job=None)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "plan_not_linked"


def test_a_plan_linked_to_one_job_may_not_read_anothers_output(scene) -> None:
    conn, settings, item_id, job_id, fingerprint = scene
    item = conn.execute("SELECT id, fingerprint FROM items WHERE id=?", (item_id,)).fetchone()
    other = seed_job(conn, item, relpath="Music/Live/another.wav")
    other_dir = settings.appdata_dir / "optimization" / "jobs" / str(other)
    other_dir.mkdir(parents=True)
    (other_dir / "output.flac").write_bytes(b"a different encode entirely")
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=? WHERE id=?",
        (blake2b_file(other_dir / "output.flac"), other),
    )
    plan_id = adoption_plan(conn, settings, job_id, fingerprint,
                            src_relpath=f"{other}/output.flac")

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "not_this_jobs_output"


def test_an_operation_hash_that_is_not_the_verified_one_is_refused(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = adoption_plan(conn, settings, job_id, fingerprint,
                            op_fingerprint="a" * 128)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id)

    assert refusal.value.code == "fingerprint_not_the_verified_one"


def test_a_second_approved_plan_may_not_adopt_the_same_output(scene) -> None:
    conn, settings, _, job_id, fingerprint = scene
    first = adoption_plan(conn, settings, job_id, fingerprint)
    approve_plan(conn, first, settings)
    (settings.library_dir / "Music" / "Live" / "other.flac").write_bytes(b"x")
    second = create_plan(
        conn,
        [OperationSpec("move", f"{job_id}/output.flac", "library",
                       "Music/Live/second.flac", src_root="optimization",
                       src_fingerprint=fingerprint)],
        settings,
    )
    conn.execute("UPDATE plans SET optimization_job_id=? WHERE id=?", (job_id, second))

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, second)

    assert refusal.value.code == "already_being_adopted"


# --- the destination side ----------------------------------------------------------


@pytest.mark.parametrize("dest_root", ["quarantine", "inbox", "optimization"])
def test_an_optimized_file_may_only_be_filed_into_the_library(
    scene, dest_root: str
) -> None:
    conn, settings, _, job_id, fingerprint = scene
    plan_id = _plan(conn, settings, job_id, fingerprint)

    with pytest.raises(SourceRefused) as refusal:
        resolve(conn, settings, plan_id, dest_root=dest_root)

    assert refusal.value.code == "illegal_destination"


def test_no_plan_may_name_optimization_as_a_destination(scene) -> None:
    """Refused by `_root_path` not knowing the namespace, which is why it needs
    no rule of its own: the namespace is source-only by construction."""
    from librairy.planner import PlanError

    conn, settings, *_ = scene

    with pytest.raises(PlanError):
        create_plan(
            conn,
            [OperationSpec("move", ORIGINAL, "optimization", "1/output.flac",
                           src_root="library")],
            settings,
        )


def test_neither_root_path_resolves_the_namespace(scene) -> None:
    from librairy import executor, planner
    from librairy.optimization_source import OPTIMIZATION_ROOT

    settings = scene[1]

    with pytest.raises(planner.PlanError):
        planner._root_path(settings, OPTIMIZATION_ROOT)
    with pytest.raises(executor.ExecutionError):
        executor._root_path(settings, OPTIMIZATION_ROOT)


# --- approval is where a bad plan stops --------------------------------------------


def test_approval_refuses_a_plan_whose_generated_source_is_not_authorised(
    scene,
) -> None:
    """The gate that matters: a plan that could never execute is never
    approved, so nothing reaches Commit that would half-move a library file."""
    conn, settings, _, job_id, fingerprint = scene
    plan_id = adoption_plan(conn, settings, job_id, fingerprint)
    conn.execute("UPDATE optimization_jobs SET verified='' WHERE id=?", (job_id,))

    with pytest.raises(PlanApprovalError) as refusal:
        approve_plan(conn, plan_id, settings)

    assert "verification" in str(refusal.value)


def test_the_planner_will_not_record_a_generated_source_without_a_hash(scene) -> None:
    from librairy.planner import PlanError

    conn, settings, _, job_id, _ = scene

    with pytest.raises(PlanError, match="own fingerprint"):
        create_plan(
            conn,
            [OperationSpec("move", f"{job_id}/output.flac", "library", TARGET,
                           src_root="optimization")],
            settings,
        )


def test_nothing_moved_during_any_of_this(scene) -> None:
    """Every refusal above happens before a filesystem mutation."""
    conn, settings, _, job_id, fingerprint = scene
    output = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    before = blake2b_file(output)
    plan_id = adoption_plan(conn, settings, job_id, fingerprint,
                            op_fingerprint="b" * 128)

    with pytest.raises(SourceRefused):
        resolve(conn, settings, plan_id)

    assert blake2b_file(output) == before
    assert (settings.library_dir / ORIGINAL).is_file()
    assert not (settings.library_dir / TARGET).exists()


def _plan(conn, settings, job_id: int, fingerprint: str) -> str:
    return adoption_plan(conn, settings, job_id, fingerprint)


def test_a_symlink_above_the_workspace_does_not_refuse_everything(
    scene, tmp_path: Path
) -> None:
    """The one a browser found on the first click.

    `/var` is a symlink to `/private/var` on macOS, and a bind mount or a moved
    appdata volume produces the same shape on a NAS. Resolving the *whole* path
    and comparing it against an unresolved root — or the reverse — refuses every
    adoption on such a host, which is a refusal about the operator's mount
    layout rather than about the file.

    Symlinks below the workspace are still refused; that is the test above.
    """
    conn, settings, _, job_id, fingerprint = scene
    real = tmp_path / "real-appdata"
    settings.appdata_dir.rename(real)
    settings.appdata_dir.symlink_to(real, target_is_directory=True)

    resolved = resolve(conn, settings, _plan(conn, settings, job_id, fingerprint))

    assert resolved.fingerprint == fingerprint
    assert resolved.path.is_file()
    assert resolved.path.read_bytes() == (real / "optimization" / "jobs"
                                          / str(job_id) / "output.flac").read_bytes()
