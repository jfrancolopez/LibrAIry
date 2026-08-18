"""The adoption workflow as a person meets it: Ready, Commit, History, Quarantine.

Every assertion is against a rendered page or a real POST, because the thing
worth checking here is not that the backend works — that has its own tests — but
that pressing the button reaches it, that the page afterwards says what happened,
and that a reload still says it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.fingerprint import blake2b_file
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.web.app import create_app

ORIGINAL = "Music/Live/concert.wav"
TARGET = "Music/Live/concert.flac"
MB = 1024 * 1024


def scene(tmp_path: Path, *, shape: str = "flac"):
    relpath, target, kind, preset = {
        "flac": (ORIGINAL, TARGET, "audio-to-flac", "flac-lossless"),
        "remux": ("Movies/film.mkv", "Movies/film.mp4", "remux", "mp4-stream-copy"),
        "hevc": ("Movies/film.mp4", "Movies/film.mp4", "video-to-hevc", "hevc-1080p-low"),
    }[shape]
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
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    original = settings.library_dir / relpath
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"o" * (842 * 1024))
    scan_root(conn, "library", settings.library_dir, settings)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)
    ).fetchone()
    quality = "remux" if shape == "remux" else ("lossy" if shape == "hevc" else "lossless")
    labels = {"flac": ("WAV", "FLAC"), "remux": ("MKV", "MP4"), "hevc": ("H264", "HEVC")}[shape]
    job_id = int(
        conn.execute(
            """
            INSERT INTO optimization_jobs(
              item_id, root, relpath, fingerprint, kind, quality, from_label,
              to_label, preset, source_bytes, estimated_bytes, actual_bytes,
              state, verified, output_relpath, staging_dir, runtime_seconds,
              queued_at, updated_at
            ) VALUES (?, 'library', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready',
                      'passed', ?, '', 61, ?, ?)
            """,
            (item["id"], relpath, item["fingerprint"], kind, quality, labels[0],
             labels[1], preset, original.stat().st_size, 512 * 1024, 504 * 1024,
             f"output{Path(target).suffix}", utc_now(), utc_now()),
        ).lastrowid
    )
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    output = staging / f"output{Path(target).suffix}"
    output.write_bytes(b"n" * (504 * 1024))
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint=?, actual_bytes=? WHERE id=?",
        (blake2b_file(output), output.stat().st_size, job_id),
    )
    return client, conn, settings, job_id, relpath, target


@pytest.fixture
def ready(tmp_path: Path):
    return scene(tmp_path)


def post(client: TestClient, url: str, **data):
    return client.post(
        url,
        data={"csrf_token": client.cookies["csrf_token"], **data},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )


def commit(client: TestClient, conn, plan_id: str) -> None:
    """Press Commit and wait for the background thread the route starts."""
    post(client, f"/commit/execute/{plan_id}")
    deadline = time.time() + 5.0
    while time.time() < deadline:
        row = conn.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
        if row and row["status"] in {"done", "failed"}:
            return
        time.sleep(0.02)
    raise AssertionError("the commit never finished")


def use_optimized(client: TestClient, job_id: int):
    return post(
        client, "/maintenance/optimization/bulk",
        action="use-optimized", job_id=str(job_id),
    )


# --- Ready ------------------------------------------------------------------------


def test_a_ready_result_offers_use_optimized(ready) -> None:
    client, *_ = ready

    body = client.get("/maintenance/optimization").text

    assert "Use optimized" in body
    assert "Discard result" in body
    assert "READY FOR REVIEW" in body


def test_a_result_with_no_recorded_output_hash_does_not_offer_it(ready) -> None:
    """A job verified before that column existed has no hash to bind a plan to,
    so the honest offer is none."""
    client, conn, _settings, job_id, *_ = ready
    conn.execute(
        "UPDATE optimization_jobs SET output_fingerprint='' WHERE id=?", (job_id,)
    )

    body = client.get("/maintenance/optimization").text

    assert "Use optimized" not in body


def test_clicking_use_optimized_moves_nothing(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    before = {
        path: blake2b_file(path)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    }
    staging = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    staged_before = blake2b_file(staging)

    response = use_optimized(client, job_id)

    assert response.status_code == 200
    assert {
        path: blake2b_file(path)
        for path in settings.library_dir.rglob("*")
        if path.is_file()
    } == before
    assert blake2b_file(staging) == staged_before
    assert not (settings.quarantine_dir / relpath).exists()


def test_approval_becomes_waiting_for_commit_and_survives_a_reload(ready) -> None:
    client, _conn, _settings, job_id, *_ = ready

    body = use_optimized(client, job_id).text

    assert "WAITING FOR COMMIT" in body
    assert "Nothing has moved yet" in body
    assert "View in Commit" in body
    assert "Cancel request" in body
    assert "Use optimized" not in body

    reloaded = client.get("/maintenance/optimization").text
    assert "WAITING FOR COMMIT" in reloaded
    assert "Use optimized" not in reloaded


def test_the_plan_has_the_job_and_exactly_two_operations(ready) -> None:
    client, conn, _settings, job_id, relpath, target = ready

    use_optimized(client, job_id)

    plan = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=? AND status='approved'",
        (job_id,),
    ).fetchone()
    assert plan is not None
    ops = conn.execute(
        "SELECT * FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan["id"],)
    ).fetchall()
    assert [(op["role"], op["src_root"], op["dest_root"]) for op in ops] == [
        ("preserve", "library", "quarantine"),
        ("adopt", "optimization", "library"),
    ]
    assert ops[1]["dest_relpath"] == target


def test_a_second_approval_of_the_same_job_refuses(ready) -> None:
    """A button that is not drawn is not a safety guarantee; this arrives from a
    stale page or from curl."""
    client, conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    body = use_optimized(client, job_id).text

    assert "already waiting for Commit" in body
    assert conn.execute(
        "SELECT COUNT(*) FROM plans WHERE optimization_job_id=? AND status='approved'",
        (job_id,),
    ).fetchone()[0] == 1


def test_cancel_request_returns_it_to_ready_and_moves_nothing(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    use_optimized(client, job_id)
    original_bytes = (settings.library_dir / relpath).read_bytes()

    body = post(
        client, "/maintenance/optimization/bulk",
        action="cancel-request", job_id=str(job_id),
    ).text

    assert "Request cancelled. Nothing was moved." in body
    assert "READY FOR REVIEW" in body
    assert "Use optimized" in body
    assert (settings.library_dir / relpath).read_bytes() == original_bytes
    assert not (settings.library_dir / target).exists()
    assert conn.execute("SELECT COUNT(*) FROM plan_withdrawals").fetchone()[0] == 1


# --- refusals a person can act on ----------------------------------------------------


def test_an_occupied_destination_refuses_visibly_and_makes_no_plan(ready) -> None:
    client, conn, settings, job_id, _relpath, target = ready
    (settings.library_dir / target).write_bytes(b"already here")

    body = use_optimized(client, job_id).text

    assert f"already a file at {target}" in body
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    # And nothing renamed itself out of the way.
    names = sorted(p.name for p in (settings.library_dir / "Music" / "Live").iterdir())
    assert names == ["concert.flac", "concert.wav"]


def test_a_changed_original_refuses_with_a_reason(ready) -> None:
    client, _conn, settings, job_id, relpath, _target = ready
    (settings.library_dir / relpath).write_bytes(b"edited since it was optimized")

    body = use_optimized(client, job_id).text

    assert "changed since it was optimized" in body
    assert "Use optimized" in body  # still offered; the state is recoverable


def test_a_changed_generated_result_refuses_without_re_verifying(ready) -> None:
    client, _conn, settings, job_id, *_ = ready
    output = next(
        (settings.appdata_dir / "optimization" / "jobs" / str(job_id)).iterdir()
    )
    output.write_bytes(b"not the bytes that were verified")

    body = use_optimized(client, job_id).text

    assert "changed since it was verified" in body


# --- Commit ---------------------------------------------------------------------------


def test_the_optimize_category_appears_with_one_decision(ready) -> None:
    client, _conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    body = client.get("/commit").text

    assert "OPTIMIZE" in body
    assert "Optimization" in body


def test_the_server_side_filter_returns_only_optimizations(ready) -> None:
    client, conn, settings, job_id, relpath, _target = ready
    (settings.inbox_dir / "unrelated.txt").write_text("x", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert "OPTIMIZE" in body
    assert "unrelated.txt" not in body


def test_one_adoption_counts_as_one_decision_not_two_operations(ready) -> None:
    from librairy.web.commit_queue import OPTIMIZATION, queue_summary

    client, conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    summary = queue_summary(conn)

    entry = next(g for g in summary["groups"] if g["type"] == OPTIMIZATION)
    assert entry["decisions"] == 1
    assert entry["operations"] == 2
    #  And the headline counts the decision, not the two moves it implies.
    assert summary["decisions"] == 1


def test_the_card_shows_current_after_and_where_the_original_goes(ready) -> None:
    client, _conn, _settings, job_id, relpath, target = ready
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert f"library/{relpath}" in body
    assert f"library/{target}" in body
    assert f"quarantine/{relpath}" in body
    assert "Original preserved" in body


def test_the_card_never_shows_the_internal_staging_path(ready) -> None:
    client, _conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert "appdata" not in body
    assert f"{job_id}/output.flac" not in body


def test_send_back_from_commit_is_the_same_withdrawal(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    use_optimized(client, job_id)

    body = post(client, f"/maintenance/optimization/{job_id}/send-back").text

    assert "Request cancelled" in body
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plan_withdrawals").fetchone()[0] == 1
    assert (settings.library_dir / relpath).is_file()
    assert not (settings.library_dir / target).exists()
    assert "Use optimized" in client.get("/maintenance/optimization").text


# --- the same-path and remux shapes, where the paths alone mislead ---------------------


def test_the_same_path_card_says_what_changed(tmp_path: Path) -> None:
    """`Current Movies/film.mp4` above `After Commit Movies/film.mp4` reads as
    "nothing happens". The encoding is what changed, so the card says so."""
    client, _conn, _settings, job_id, relpath, target = scene(tmp_path, shape="hevc")
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert relpath == target
    assert "Same filename" in body
    assert "what changes is inside it" in body
    assert "H264 → HEVC" in body


def test_the_remux_card_explains_a_change_that_saves_nothing(tmp_path: Path) -> None:
    client, conn, _settings, job_id, relpath, target = scene(tmp_path, shape="remux")
    conn.execute(
        "UPDATE optimization_jobs SET actual_bytes=source_bytes WHERE id=?", (job_id,)
    )
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert "MKV → MP4" in body
    assert "no re-encode, for compatibility" in body
    assert "Same filename" not in body


# --- committing ------------------------------------------------------------------------


def test_committing_activates_the_optimized_version_everywhere(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]

    commit(client, conn, plan_id)

    assert (settings.library_dir / target).is_file()
    assert (settings.quarantine_dir / relpath).is_file()
    assert not (settings.library_dir / relpath).exists()
    #  Search
    search = client.get("/search?q=concert").text
    assert "concert.flac" in search
    #  Browse
    browse = client.get("/browse/music?folder=Live").text
    assert "concert.flac" in browse
    assert "concert.wav" not in browse
    #  Quarantine
    quarantine = client.get("/quarantine").text
    assert "preserved original" in quarantine
    assert "you said you did not want it" not in quarantine
    assert "Restore original" in quarantine
    assert "Delete queue" not in quarantine.split("Restore original")[1][:400]
    #  History
    history = client.get("/history").text
    assert "Optimized version adopted" in history
    assert "appdata" not in history


def test_history_names_the_workspace_rather_than_its_path(ready) -> None:
    client, conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    commit(client, conn, plan_id)

    body = client.get("/history").text
    from librairy.web.history import INTERNAL_LABEL, history_data

    #  The page groups by plan and shows a summary; the per-entry paths are one
    #  disclosure down. Both have to be clean, so both are checked: the label
    #  the row would render, and the whole document for the real path.
    entries = history_data(conn)["entries"]
    adoption = next(e for e in entries if e["src_root"] == "optimization")
    assert adoption["src_label"] == INTERNAL_LABEL
    assert adoption["internal"] is True
    assert "output.flac" not in body
    assert "appdata" not in body


def test_restore_original_undoes_the_adoption_from_quarantine(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    original_bytes = (settings.library_dir / relpath).read_bytes()
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    commit(client, conn, plan_id)
    entry_id = conn.execute(
        "SELECT id FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]

    body = post(client, f"/quarantine/restore-original/{entry_id}").text

    assert "The original is back in the library" in body
    assert (settings.library_dir / relpath).read_bytes() == original_bytes
    assert not (settings.library_dir / target).exists()
    #  No Search ghost.
    search = client.get("/search?q=concert").text
    assert "concert.wav" in search
    assert "concert.flac" not in search
    #  And it is offerable again.
    assert "Use optimized" in client.get("/maintenance/optimization").text


def test_five_cycles_through_the_web_leave_one_item_and_no_ghost(ready) -> None:
    client, conn, settings, job_id, relpath, target = ready
    before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    for _ in range(5):
        use_optimized(client, job_id)
        plan_id = conn.execute(
            "SELECT id FROM plans WHERE optimization_job_id=? AND status='approved'",
            (job_id,),
        ).fetchone()[0]
        commit(client, conn, plan_id)
        entry_id = conn.execute(
            "SELECT id FROM quarantine_entries WHERE restored_at IS NULL"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        post(client, f"/quarantine/restore-original/{entry_id}")

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == before + 1
    assert (settings.library_dir / relpath).is_file()
    search = client.get("/search?q=concert").text
    assert "concert.flac" not in search


# --- the failure that leaves a gap ------------------------------------------------------


def test_a_failed_second_operation_tells_the_user_the_original_is_back(
    ready, monkeypatch
) -> None:
    from librairy import executor

    client, conn, settings, job_id, relpath, target = ready
    original_bytes = (settings.library_dir / relpath).read_bytes()
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    monkeypatch.setattr(
        executor, "_execute_adoption_op",
        lambda *a: (_ for _ in ()).throw(OSError("the filesystem went away")),
    )

    commit(client, conn, plan_id)
    monkeypatch.undo()

    assert (settings.library_dir / relpath).read_bytes() == original_bytes
    assert not (settings.library_dir / target).exists()
    assert not (settings.quarantine_dir / relpath).exists()
    #  Browse and Search agree with the disk.
    assert "concert.wav" in client.get("/browse/music?folder=Live").text
    assert "concert.flac" not in client.get("/search?q=concert").text
    #  And nothing claims it was adopted.
    assert conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT status FROM plans WHERE id=?", (plan_id,)
    ).fetchone()[0] == "failed"


# --- wording, across every page that shows a number -------------------------------------


def test_no_page_claims_a_saving_while_both_copies_exist(ready) -> None:
    client, conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    commit(client, conn, plan_id)

    for url in (
        "/maintenance/optimization",
        "/commit",
        "/quarantine",
        "/history",
        "/",
    ):
        body = client.get(url).text.lower()
        for phrase in ("saved ", "you saved", "freed up", "reclaimable"):
            assert phrase not in body, f"{url} says {phrase!r}"


def test_the_commit_card_separates_smaller_from_reclaimed(ready) -> None:
    client, _conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    assert "Optimized version is" in body
    assert "smaller" in body
    assert "Space reclaimed" in body
    assert "0 B" in body


def test_the_details_panel_carries_the_whole_accounting(ready) -> None:
    client, _conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)

    body = client.get("/commit?type=optimization").text

    for label in (
        "Original baseline",
        "Optimized representation",
        "Representation reduction",
        "Extra storage while the original is retained",
        "Space reclaimed now",
        "If the preserved original is eventually removed",
        "Final net reduction against the original baseline",
    ):
        assert label in body, label


def test_the_dashboard_does_not_put_a_byte_total_on_it(ready) -> None:
    client, conn, _settings, job_id, *_ = ready
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    commit(client, conn, plan_id)

    body = client.get("/").text

    assert "338" not in body
    assert "saved" not in body.lower()


# --- empty and mixed states -------------------------------------------------------------


def test_a_queue_of_only_a_ready_result_does_not_say_nothing_is_queued(ready) -> None:
    """The browser found this class of bug once already."""
    client, *_ = ready

    body = client.get("/maintenance/optimization").text

    assert "Nothing is queued" not in body
    assert "READY FOR REVIEW" in body


def test_a_queue_of_only_a_waiting_result_does_not_say_nothing_is_queued(
    ready,
) -> None:
    client, _conn, _settings, job_id, *_ = ready

    body = use_optimized(client, job_id).text

    assert "Nothing is queued" not in body
    assert "WAITING FOR COMMIT" in body


def test_a_ready_result_still_renders_exactly_once(ready) -> None:
    client, *_ = ready

    body = client.get("/maintenance/optimization").text

    assert body.count('id="ready-') == 1
    assert body.count('id="job-') == 0


def test_no_optimization_state_renders_a_calm_page(tmp_path: Path) -> None:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library", QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0, _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    optimization = client.get("/maintenance/optimization")
    commit = client.get("/commit?type=optimization")

    assert optimization.status_code == 200
    assert "Use optimized" not in optimization.text
    assert commit.status_code == 200
    assert "OPTIMIZE" not in commit.text


def test_commit_stays_bounded_with_an_optimization_among_many_decisions(
    ready,
) -> None:
    """The page's whole scalability story is counts in SQL and one LIMIT-ed
    query. An extra category must not change that."""
    from librairy.web.commit_queue import PAGE_SIZE, queue_rows, queue_summary

    client, conn, settings, job_id, *_ = ready
    for index in range(120):
        (settings.inbox_dir / f"f{index}.txt").write_text("x", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
        " confidence, status, evidence, created_at, updated_at)"
        " SELECT id, 'misc', relpath, 'Misc/' || relpath, 0.9, 'approved', '{}', ?, ?"
        " FROM items WHERE root='inbox'",
        (utc_now(), utc_now()),
    )
    use_optimized(client, job_id)

    rows = queue_rows(conn, settings, kind="new-file", page=1)
    summary = queue_summary(conn)

    assert len(rows) == PAGE_SIZE
    assert summary["decisions"] == 121
    assert next(
        g["decisions"] for g in summary["groups"] if g["type"] == "new-file"
    ) == 120


def test_undoing_an_adoption_settles_its_quarantine_entry(ready) -> None:
    """Found by pressing Restore original in a browser and watching the row
    stay under `Held`, offering a Restore that could only fail from then on."""
    client, conn, settings, job_id, relpath, _target = ready
    use_optimized(client, job_id)
    plan_id = conn.execute(
        "SELECT id FROM plans WHERE optimization_job_id=?", (job_id,)
    ).fetchone()[0]
    commit(client, conn, plan_id)
    entry_id = conn.execute(
        "SELECT id FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]

    post(client, f"/quarantine/restore-original/{entry_id}")

    assert conn.execute(
        "SELECT restored_at FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()[0] is not None
    body = client.get("/quarantine").text
    assert "preserved original" not in body
    assert "Restore original" not in body


def test_undoing_an_ordinary_quarantine_settles_its_entry_too(tmp_path: Path) -> None:
    """The same defect, and it was never optimization-specific."""
    from librairy import executor
    from librairy.history import undo_plan
    from librairy.planner import approve_plan, create_plan
    from librairy.quarantine import quarantine_operation
    from tests.test_web_quarantine import client_for

    client, conn, settings = client_for(tmp_path)
    (settings.inbox_dir / "dupe.txt").write_text("dupe", encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn, [quarantine_operation("dupe.txt", date="2026-08-15")], settings
    )
    approve_plan(conn, plan_id, settings)
    executor.execute_plan(conn, plan_id, settings)
    entry_id = conn.execute("SELECT id FROM quarantine_entries").fetchone()[0]

    undo_plan(conn, plan_id, settings)

    assert conn.execute(
        "SELECT restored_at FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()[0] is not None
    assert (settings.inbox_dir / "dupe.txt").is_file()


def test_a_disclosure_panel_cannot_stretch_its_card(ready) -> None:
    """A `<details>` among buttons is a flex item, and a flex item's default
    `min-width: auto` sizes it to its content rather than its container. The
    optimization page's Details did exactly that once its labels grew long: at
    375px the document went to 951px wide and the whole card scrolled sideways.

    Invisible to every DOM assertion in the suite, because nothing was wrong
    until somebody opened it. This holds the rule that fixed it.
    """
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")

    assert ".button-row > details { min-width: 0; max-width: 100%; }" in css
    #  And the accounting stacks rather than sizing a label column to
    #  "Final net reduction against the original baseline".
    assert ".storage-accounting { grid-template-columns: minmax(0, 1fr)" in css
    assert css.index(".storage-accounting {") > css.index(".queue-facts {"), (
        "the override must come after the rules it overrides"
    )


def test_the_long_labels_are_only_used_where_the_panel_stacks(ready) -> None:
    for page in ("commit.html", "quarantine.html", "optimization.html"):
        text = Path(f"src/librairy/web/templates/{page}").read_text(encoding="utf-8")
        if "Final net reduction" in text or "If you remove this original" in text:
            assert "storage-accounting" in text, page
