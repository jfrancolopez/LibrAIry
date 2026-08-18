"""Adoption end to end: plan, commit, undo, re-adopt — and what fails safely.

The three shapes are genuinely different and each has its own way of going
wrong:

    WAV  -> FLAC   the extension changes, target is free
    MKV  -> MP4    the extension changes, target is free
    MKV  -> MKV    **the target is the original**, and is legal only because
                   operation 1 moves that exact file out first

and the failure that matters most is the one where operation 1 succeeds and
operation 2 does not, because that is the state with a hole in the library.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy import executor
from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.optimization_adopt import cancel_adoption, plan_adoption
from librairy.optimization_preflight import Refusal, adoption_preflight
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.search import search_items

SHAPES = {
    "flac": ("Music/Live/concert.wav", "Music/Live/concert.flac",
             "audio-to-flac", "flac-lossless"),
    "mp4": ("Movies/film.mkv", "Movies/film.mp4", "remux", "mp4-stream-copy"),
    # The same-path case, and it is real rather than contrived: the HEVC
    # preset writes MP4, so an H.264 MP4 re-encoded to HEVC lands back on its
    # own path. `PRESET_SUFFIX` is what decides this — an MKV source would come
    # out as `.mp4` and be an ordinary extension change.
    "hevc": ("Movies/film.mp4", "Movies/film.mp4", "video-to-hevc",
             "hevc-1080p-low"),
}


def build(tmp_path: Path, shape: str = "flac"):
    original_relpath, target_relpath, kind, preset = SHAPES[shape]
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

    original = settings.library_dir / original_relpath
    original.parent.mkdir(parents=True)
    original.write_bytes(b"the original, at its original size" * 900)
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (original_relpath,)
    ).fetchone()

    job_id = int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, ?, 'lossless', 'FROM', 'TO', ?,
                      ?, 0, 0, 'ready', 'passed', ?, '', ?, ?)
            """,
            (item["id"], original_relpath, item["fingerprint"], kind, preset,
             original.stat().st_size, f"output{Path(target_relpath).suffix}",
             utc_now(), utc_now()),
        ).lastrowid
    )
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / f"output{Path(target_relpath).suffix}"
    output.write_bytes(b"the smaller optimized copy" * 400)
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=?, actual_bytes=? WHERE id=?",
        (blake2b_file(output), output.stat().st_size, job_id),
    )
    return conn, settings, int(item["id"]), job_id, original_relpath, target_relpath


@pytest.fixture(params=list(SHAPES))
def shape(request, tmp_path: Path):
    return build(tmp_path, request.param) + (request.param,)


@pytest.fixture
def wav(tmp_path: Path):
    return build(tmp_path, "flac")


# --- the whole lifecycle, for all three shapes ------------------------------------


def test_adoption_commit_undo_and_readoption(shape) -> None:
    conn, settings, item_id, job_id, original_relpath, target_relpath, name = shape
    original_bytes = (settings.library_dir / original_relpath).read_bytes()
    original_hash = blake2b_file(settings.library_dir / original_relpath)
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    output = next(staging.iterdir())
    optimized_hash = blake2b_file(output)

    # --- plan: a decision, and not a single byte moved -----------------------
    plan_id = plan_adoption(conn, settings, job_id)
    assert isinstance(plan_id, str), plan_id
    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert output.is_file()
    assert conn.execute(
        "SELECT optimization_job_id FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == job_id
    ops = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,)
    ).fetchall()
    assert [(op["role"], op["src_root"], op["dest_root"]) for op in ops] == [
        ("preserve", "library", "quarantine"),
        ("adopt", "optimization", "library"),
    ]

    # --- commit ---------------------------------------------------------------
    summary = executor.execute_plan(conn, plan_id, settings)
    assert summary.done == 2, summary
    assert not summary.partial

    assert blake2b_file(settings.library_dir / target_relpath) == optimized_hash
    assert blake2b_file(settings.quarantine_dir / original_relpath) == original_hash
    assert (settings.quarantine_dir / original_relpath).read_bytes() == original_bytes
    assert not list(staging.iterdir())

    result_id = conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    assert result_id is not None
    result = conn.execute("SELECT * FROM items WHERE id=?", (result_id,)).fetchone()
    assert result["relpath"] == target_relpath
    assert result["fingerprint"] == optimized_hash
    assert result["missing_since"] is None
    assert conn.execute(
        "SELECT root FROM items WHERE id=?", (item_id,)
    ).fetchone()[0] == "quarantine"

    # --- undo -----------------------------------------------------------------
    from librairy.history import undo_plan

    undo_plan(conn, plan_id, settings)

    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert not (settings.quarantine_dir / original_relpath).exists()
    assert blake2b_file(output) == optimized_hash
    assert conn.execute(
        "SELECT missing_since FROM items WHERE id=?", (result_id,)
    ).fetchone()[0] is not None
    live = {(row["root"], row["relpath"]) for row in search_items(conn, "")}
    assert (("library", target_relpath) in live) == (name == "hevc")
    if name != "hevc":
        assert ("library", original_relpath) in live

    # --- and again ------------------------------------------------------------
    again = plan_adoption(conn, settings, job_id)
    assert isinstance(again, str), again
    executor.execute_plan(conn, again, settings)

    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == result_id
    assert blake2b_file(settings.library_dir / target_relpath) == optimized_hash


def test_five_cycles_keep_the_item_count_constant(shape) -> None:
    conn, settings, _, job_id, *_ = shape
    from librairy.history import undo_plan

    plan_id = plan_adoption(conn, settings, job_id)
    executor.execute_plan(conn, plan_id, settings)
    after_first = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    result_id = conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]

    for _ in range(5):
        undo_plan(conn, plan_id, settings)
        plan_id = plan_adoption(conn, settings, job_id)
        assert isinstance(plan_id, str), plan_id
        executor.execute_plan(conn, plan_id, settings)

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == after_first
    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == result_id


# --- the same-path case, specifically ----------------------------------------------


def test_the_same_path_plan_is_recognised_as_such(tmp_path: Path) -> None:
    conn, settings, _, job_id, original_relpath, target_relpath = build(tmp_path, "hevc")

    checked = adoption_preflight(conn, settings, job_id)

    assert checked.eligible
    assert checked.same_path
    assert checked.target_relpath == original_relpath == target_relpath


def test_the_same_path_target_must_be_the_original_itself(tmp_path: Path) -> None:
    """Not merely a file with that name — the exact one operation 1 moves."""
    conn, settings, item_id, job_id, original_relpath, _ = build(tmp_path, "hevc")
    (settings.library_dir / original_relpath).write_bytes(b"somebody replaced it")

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "original_changed"


def test_the_same_path_target_is_empty_between_the_two_operations(
    tmp_path: Path,
) -> None:
    """The proof the whole case rests on, taken from the real journal: the
    preserve operation is recorded before the adopt operation, so the library
    slot is vacant when the optimized file arrives."""
    conn, settings, _, job_id, original_relpath, _ = build(tmp_path, "hevc")
    plan_id = plan_adoption(conn, settings, job_id)

    executor.execute_plan(conn, plan_id, settings)

    journal = conn.execute(
        "SELECT action, src_root, src_relpath, dest_root, dest_relpath, outcome"
        " FROM history WHERE plan_id=? ORDER BY id", (plan_id,)
    ).fetchall()
    assert [(row["src_root"], row["dest_root"]) for row in journal] == [
        ("library", "quarantine"),
        ("optimization", "library"),
    ]
    assert all(row["outcome"] == "ok" for row in journal)
    # And what is at the path afterwards is the optimized file, not a renamed
    # anything.
    assert sorted(
        p.name for p in (settings.library_dir / "Movies").iterdir()
    ) == ["film.mp4"]


def test_a_same_path_target_occupied_by_a_stranger_at_commit_time_refuses(
    tmp_path: Path,
) -> None:
    conn, settings, _, job_id, original_relpath, _ = build(tmp_path, "hevc")
    plan_id = plan_adoption(conn, settings, job_id)
    (settings.library_dir / original_relpath).write_bytes(b"changed after approval")

    summary = executor.execute_plan(conn, plan_id, settings)

    assert summary.refused_collision == 2
    assert summary.done == 0
    assert (settings.library_dir / original_relpath).read_bytes() == b"changed after approval"
    assert not (settings.quarantine_dir / original_relpath).exists()


# --- collisions refuse ---------------------------------------------------------------


def test_an_occupied_target_blocks_the_plan_before_it_exists(tmp_path: Path) -> None:
    conn, settings, _, job_id, _, target_relpath = build(tmp_path, "flac")
    (settings.library_dir / target_relpath).write_bytes(b"a flac that was already there")

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "target_taken"
    assert isinstance(plan_adoption(conn, settings, job_id), Refusal)


def test_a_target_created_after_approval_blocks_the_commit(wav) -> None:
    """The race. Approval succeeds, an unrelated process writes the target,
    Commit is pressed — and the original must not move."""
    conn, settings, _, job_id, original_relpath, target_relpath = wav
    plan_id = plan_adoption(conn, settings, job_id)
    original_bytes = (settings.library_dir / original_relpath).read_bytes()
    (settings.library_dir / target_relpath).write_bytes(b"arrived over SMB")

    summary = executor.execute_plan(conn, plan_id, settings)

    assert summary.refused_collision
    assert summary.done == 0
    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert not (settings.quarantine_dir / original_relpath).exists()
    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "failed"


def test_nothing_is_ever_renumbered(wav) -> None:
    conn, settings, _, job_id, _, target_relpath = wav
    (settings.library_dir / target_relpath).write_bytes(b"already here")

    assert isinstance(plan_adoption(conn, settings, job_id), Refusal)

    names = sorted(p.name for p in (settings.library_dir / "Music" / "Live").iterdir())
    assert names == ["concert.flac", "concert.wav"]
    assert not any("(2)" in name for name in names)


def test_an_occupied_preservation_path_blocks_the_plan(wav) -> None:
    conn, settings, _, job_id, original_relpath, _ = wav
    preserved = settings.quarantine_dir / original_relpath
    preserved.parent.mkdir(parents=True)
    preserved.write_bytes(b"something else is already preserved here")

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "preserved_path_taken"


# --- stale facts -----------------------------------------------------------------


def test_a_changed_original_blocks_the_plan(wav) -> None:
    conn, settings, _, job_id, original_relpath, _ = wav
    (settings.library_dir / original_relpath).write_bytes(b"edited since it was optimized")

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "original_changed"


def test_a_missing_original_blocks_the_plan(wav) -> None:
    conn, settings, item_id, job_id, original_relpath, _ = wav
    (settings.library_dir / original_relpath).unlink()

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "original_gone"


def test_a_changed_generated_result_blocks_the_plan(wav) -> None:
    conn, settings, _, job_id, *_ = wav
    output = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    output.write_bytes(b"not the bytes that were verified")

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "bytes_changed"


def test_a_protected_root_blocks_the_plan(wav) -> None:
    from librairy.protected import set_protected_roots

    conn, settings, _, job_id, *_ = wav
    set_protected_roots(conn, ["Music"])

    checked = adoption_preflight(conn, settings, job_id)

    assert isinstance(checked, Refusal)
    assert checked.code == "protected"


def test_preflight_moves_nothing(wav) -> None:
    conn, settings, _, job_id, original_relpath, _ = wav
    before = sorted(str(p.relative_to(settings.library_dir.parent))
                    for p in settings.library_dir.parent.rglob("*") if p.is_file())

    adoption_preflight(conn, settings, job_id)

    after = sorted(str(p.relative_to(settings.library_dir.parent))
                   for p in settings.library_dir.parent.rglob("*") if p.is_file())
    assert after == before


# --- one adoption at a time ---------------------------------------------------------


def test_a_second_adoption_of_the_same_job_is_refused(wav) -> None:
    conn, settings, _, job_id, *_ = wav
    plan_adoption(conn, settings, job_id)

    second = plan_adoption(conn, settings, job_id)

    assert isinstance(second, Refusal)
    assert second.code == "already_waiting"


def test_cancelling_returns_the_job_to_ready_and_moves_nothing(wav) -> None:
    conn, settings, _, job_id, original_relpath, target_relpath = wav
    plan_id = plan_adoption(conn, settings, job_id)
    original_bytes = (settings.library_dir / original_relpath).read_bytes()
    output = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    optimized_bytes = output.read_bytes()

    assert cancel_adoption(conn, plan_id) is True

    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert output.read_bytes() == optimized_bytes
    assert not (settings.library_dir / target_relpath).exists()
    assert conn.execute("SELECT COUNT(*) FROM plans WHERE id=?", (plan_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM plan_withdrawals WHERE plan_id=?", (plan_id,)
    ).fetchone()[0] == 1
    # And offerable again.
    assert adoption_preflight(conn, settings, job_id).eligible


def test_a_started_adoption_cannot_be_cancelled(wav) -> None:
    conn, settings, _, job_id, *_ = wav
    plan_id = plan_adoption(conn, settings, job_id)
    executor.execute_plan(conn, plan_id, settings)

    assert cancel_adoption(conn, plan_id) is False


# --- the failure that leaves a gap ---------------------------------------------------


def test_an_injected_second_operation_failure_restores_the_original(
    wav, monkeypatch
) -> None:
    """Operation 1 succeeds, operation 2 is forced to fail, and the library
    must not be left with a hole where the recording was.

    Recovery happens inside this commit. "Go to History and undo the
    half-finished plan" is something to say to somebody whose library already
    has a gap in it, which means they have to notice the gap first.
    """
    conn, settings, item_id, job_id, original_relpath, target_relpath = wav
    original_bytes = (settings.library_dir / original_relpath).read_bytes()
    original_hash = blake2b_file(settings.library_dir / original_relpath)
    output = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    optimized_hash = blake2b_file(output)
    plan_id = plan_adoption(conn, settings, job_id)

    real = executor._execute_adoption_op

    def explode(conn_, row, settings_):
        raise OSError("the destination filesystem went away")

    monkeypatch.setattr(executor, "_execute_adoption_op", explode)
    summary = executor.execute_plan(conn, plan_id, settings)
    monkeypatch.setattr(executor, "_execute_adoption_op", real)

    assert summary.failed == 1
    assert summary.done == 1  # operation 1 did happen, and was put back

    # The original is exactly where it was, byte for byte.
    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert blake2b_file(settings.library_dir / original_relpath) == original_hash
    assert not (settings.quarantine_dir / original_relpath).exists()
    # No gap: the library has the file it started with.
    assert not (settings.library_dir / target_relpath).exists()
    # The generated output never left its workspace.
    assert blake2b_file(output) == optimized_hash
    # The plan failed and says so.
    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "failed"
    # No result item was activated.
    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] is None
    # The original item is a live library file again.
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["root"] == "library"
    assert row["missing_since"] is None
    assert [r["relpath"] for r in search_items(conn, "concert")] == [original_relpath]


def test_the_job_can_be_adopted_again_after_a_compensated_failure(
    wav, monkeypatch
) -> None:
    conn, settings, _, job_id, _, target_relpath = wav
    plan_id = plan_adoption(conn, settings, job_id)
    monkeypatch.setattr(
        executor, "_execute_adoption_op",
        lambda *a: (_ for _ in ()).throw(OSError("nope")),
    )
    executor.execute_plan(conn, plan_id, settings)
    monkeypatch.undo()

    retry = plan_adoption(conn, settings, job_id)

    assert isinstance(retry, str), retry
    assert executor.execute_plan(conn, retry, settings).done == 2
    assert (settings.library_dir / target_relpath).is_file()


def test_a_compensation_that_cannot_run_is_reported_rather_than_hidden(
    wav, monkeypatch
) -> None:
    """Very rare, and the application must not paper over it. What gets
    recorded is where the original is, in relative terms, and the hash somebody
    would need to check it by hand."""
    conn, settings, _, job_id, original_relpath, _ = wav
    original_hash = blake2b_file(settings.library_dir / original_relpath)
    plan_id = plan_adoption(conn, settings, job_id)

    monkeypatch.setattr(
        executor, "_execute_adoption_op",
        lambda *a: (_ for _ in ()).throw(OSError("nope")),
    )
    from librairy import history

    def refuse(conn_, history_id, settings_):
        raise OSError("and the restore cannot happen either")

    monkeypatch.setattr(history, "_undo_op_unlocked", refuse)
    executor.execute_plan(conn, plan_id, settings)
    monkeypatch.undo()

    recovery = conn.execute(
        "SELECT * FROM history WHERE action='adoption_recovery'"
    ).fetchone()
    assert recovery is not None
    assert recovery["outcome"].startswith("recovery_required")
    # Where the original actually is, said relatively.
    assert recovery["src_root"] == "quarantine"
    assert recovery["src_relpath"] == original_relpath
    assert recovery["fingerprint"] == original_hash
    assert (settings.quarantine_dir / original_relpath).is_file()
    # And no host path anywhere in it.
    assert str(settings.quarantine_dir) not in str(dict(recovery))


def test_a_refused_source_at_commit_time_also_compensates(wav) -> None:
    """Not only a crash. The generated file changing between approval and
    Commit is the likelier version of the same problem."""
    conn, settings, _, job_id, original_relpath, _ = wav
    original_bytes = (settings.library_dir / original_relpath).read_bytes()
    plan_id = plan_adoption(conn, settings, job_id)
    output = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    output.write_bytes(b"replaced between approval and commit")

    summary = executor.execute_plan(conn, plan_id, settings)

    assert summary.refused_source == 1
    assert (settings.library_dir / original_relpath).read_bytes() == original_bytes
    assert not (settings.quarantine_dir / original_relpath).exists()
    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "failed"
