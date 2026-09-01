"""What Undo does while the worker holds the lock.

Found by release acceptance, not by a unit test: pressing "Undo plan" in the
running container while the inbox scan was still going answered with a 500
System Fault page and a `LockHeldError` traceback in the log. Commit had
handled the same condition for as long as it has existed; Undo — the button
you press when you want a move taken back — had not.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.history import UNDO_REFUSED_BUSY, undo_op, undo_plan
from librairy.locks import BUSY, WAIT_SECONDS, acquire_lock
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.scanner import scan_root
from librairy.web.app import create_app
from librairy.web.history import UNDO_OUTCOMES, undo_outcome_text


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    settings.inbox_dir.mkdir()
    settings.library_dir.mkdir()
    settings.quarantine_dir.mkdir()
    return settings


def committed(settings: Settings, conn, names: list[str]) -> str:
    for name in names:
        (settings.inbox_dir / name).write_text(name, encoding="utf-8")
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    plan_id = create_plan(
        conn,
        [OperationSpec("move", name, "library", f"Documents/{name}") for name in names],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    execute_plan(conn, plan_id, settings)
    return plan_id


def test_a_held_lock_makes_undo_an_outcome_rather_than_an_exception(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    committed(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]

    with acquire_lock(settings):
        result = undo_op(conn, history_id, settings, wait=0.0)

    assert result.outcome == UNDO_REFUSED_BUSY
    # And the file is exactly where the commit left it.
    assert (settings.library_dir / "Documents/a.txt").exists()
    assert not (settings.inbox_dir / "a.txt").exists()


def test_being_busy_is_not_written_to_the_journal(tmp_path: Path) -> None:
    """The other refusals record what was found true of the file. Nothing was
    read here, so there is nothing to record — and a journal full of `busy`
    rows would make History unreadable for a condition that lasts seconds."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    committed(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    with acquire_lock(settings):
        undo_op(conn, history_id, settings, wait=0.0)

    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == before


def test_undoing_a_whole_plan_while_busy_moves_none_of_it(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = committed(settings, conn, ["a.txt", "b.txt", "c.txt"])

    with acquire_lock(settings):
        results = undo_plan(conn, plan_id, settings)

    assert [r.outcome for r in results] == [UNDO_REFUSED_BUSY] * 3
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (settings.library_dir / f"Documents/{name}").exists()
        assert not (settings.inbox_dir / name).exists()


def test_undo_still_works_once_the_lock_is_released(tmp_path: Path) -> None:
    """Busy is a "try again", so trying again has to actually work."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = committed(settings, conn, ["a.txt"])

    with acquire_lock(settings):
        assert undo_plan(conn, plan_id, settings)[0].outcome == UNDO_REFUSED_BUSY
    results = undo_plan(conn, plan_id, settings)

    assert [r.outcome for r in results] == ["ok"]
    assert (settings.inbox_dir / "a.txt").exists()
    assert not (settings.library_dir / "Documents/a.txt").exists()


def test_the_history_page_says_it_in_words_instead_of_faulting(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = committed(settings, conn, ["a.txt"])
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    with acquire_lock(settings):
        response = client.post(
            f"/history/plans/{plan_id}/undo",
            headers={"x-csrf-token": client.cookies["csrf_token"], "hx-request": "true"},
        )

    assert response.status_code == 200
    assert "System Fault" not in response.text
    assert BUSY in response.text


def test_the_single_op_undo_route_answers_the_same_way(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    committed(settings, conn, ["a.txt"])
    history_id = conn.execute("SELECT id FROM history WHERE action='move'").fetchone()[0]
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    with acquire_lock(settings):
        response = client.post(
            f"/history/undo/{history_id}",
            headers={"x-csrf-token": client.cookies["csrf_token"], "hx-request": "true"},
        )

    assert response.status_code == 200
    assert BUSY in response.text


def test_every_refusal_the_domain_can_produce_has_a_sentence() -> None:
    """`undo_outcome_text` falls back to a bare "not put back", which is how a
    new refusal code reaches a person as no explanation at all. Every
    `undo_refused_*` the module defines has to be spelled out."""
    import librairy.history as history

    codes = {
        value
        for name, value in vars(history).items()
        if name.isupper() and isinstance(value, str) and value.startswith("undo_refused_")
    }
    codes |= {
        literal
        for literal in ("undo_refused_missing", "undo_refused_occupied",
                        "undo_refused_changed", "undo_refused_source")
    }

    assert codes <= set(UNDO_OUTCOMES)
    for code in codes:
        assert undo_outcome_text(code) != "not put back"


def test_commit_and_undo_use_one_sentence_for_one_condition() -> None:
    """Two different wordings for "the worker has the files" would read as two
    different problems."""
    assert BUSY in UNDO_OUTCOMES["undo_refused_busy"]
    commit_source = Path("src/librairy/web/commit.py").read_text(encoding="utf-8")
    assert "state.error = BUSY" in commit_source
    assert "LibrAIry is busy; retry" not in commit_source


def test_undo_waits_for_a_lock_that_is_about_to_be_released(tmp_path: Path) -> None:
    """The worker holds the lock for a whole cycle, and on a library with work
    in it that is most of the time — measured free on 40 of 120 samples over a
    minute in the release container. Refusing on the first attempt made Undo a
    button you pressed three times."""
    import threading

    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = committed(settings, conn, ["a.txt"])
    released = threading.Event()

    def hold() -> None:
        with acquire_lock(settings):
            released.wait(1.0)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        results = undo_plan(conn, plan_id, settings)
    finally:
        released.set()
        holder.join()

    assert [r.outcome for r in results] == ["ok"]
    assert (settings.inbox_dir / "a.txt").exists()


def test_one_waiting_budget_covers_the_whole_reversal(tmp_path: Path) -> None:
    """flock is held per open description, so each operation has to take the
    lock itself. Waiting the full budget *per file* would make a fifty-file
    reversal sit for four minutes before saying it was busy."""
    import time as clock

    settings = settings_for(tmp_path)
    conn = connect(settings)
    plan_id = committed(settings, conn, ["a.txt", "b.txt", "c.txt", "d.txt"])

    with acquire_lock(settings):
        started = clock.monotonic()
        results = undo_plan(conn, plan_id, settings)
        elapsed = clock.monotonic() - started

    assert [r.outcome for r in results] == [UNDO_REFUSED_BUSY] * 4
    # Four files, one budget — not four.
    assert elapsed < WAIT_SECONDS * 2


def test_background_work_never_waits_on_a_person(tmp_path: Path) -> None:
    """A worker cycle that queued behind a commit would run the moment the
    commit ended, on top of it. Only actions someone is standing in front of
    are allowed to wait."""
    settings = settings_for(tmp_path)
    settings.appdata_dir.mkdir(parents=True, exist_ok=True)

    assert acquire_lock(settings).wait == 0.0
    assert acquire_lock(settings, wait=WAIT_SECONDS).wait == WAIT_SECONDS

    worker_source = Path("src/librairy/worker.py").read_text(encoding="utf-8")
    assert "acquire_lock(self.settings)" in worker_source
    assert "wait=" not in worker_source
