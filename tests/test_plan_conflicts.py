"""Two waiting decisions that cannot both still be right.

The forward-time twin of the Undo dependency work. A decision that has not run
cannot be depended on by anything — it has moved no files — so this is not the
same problem and does not use the same machinery. What it shares is the shape
of the mistake it prevents: a person makes two choices, each reasonable on its
own, and finds out they were incompatible from a failure rather than from a
sentence.

The restraint tests matter as much as the detection ones. A conflict detector
that calls two decisions incompatible because they touch the same folder, or
because they both mention the same relationship, is a detector that blocks
Commit for no reason — and a Commit button that is sometimes wrongly withheld
is worse than one that occasionally fails late.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy import plan_conflicts
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.plan_conflicts import SAME_FILE, SAME_PLACE, check, count, for_decisions
from librairy.planner import (
    OperationSpec,
    PlanConflict,
    approve_plan,
    create_plan,
    utc_now,
)
from librairy.scanner import scan_root
from librairy.web.app import create_app


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def client_for(tmp_path: Path) -> tuple[TestClient, sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def library_file(settings: Settings, relpath: str, body: bytes = b"a file") -> Path:
    path = settings.library_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def plan_for(
    conn: sqlite3.Connection,
    settings: Settings,
    moves: list[tuple[str, str]],
    *,
    approve: bool = True,
) -> str:
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                op_type="move",
                src_root="library",
                src_relpath=src,
                dest_root="library",
                dest_relpath=dest,
            )
            for src, dest in moves
        ],
        settings,
    )
    conn.execute("UPDATE plans SET coherent=1 WHERE id=?", (plan_id,))
    if approve:
        approve_plan(conn, plan_id, settings)
    return plan_id


def arrival(
    conn: sqlite3.Connection,
    settings: Settings,
    name: str,
    destination: str,
    *,
    status: str = "approved",
) -> int:
    path = settings.inbox_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"an arriving file " + name.encode())
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    item_id = int(
        conn.execute(
            "SELECT id FROM items WHERE root='inbox' AND relpath=?", (name,)
        ).fetchone()["id"]
    )
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath,"
        " confidence, status, evidence, created_at, updated_at)"
        " VALUES (?, 'documents', ?, ?, 0.9, ?, '[]', ?, ?)"
        " ON CONFLICT(item_id) DO UPDATE SET dest_relpath=excluded.dest_relpath,"
        " status=excluded.status",
        (item_id, name, destination, status, utc_now(), utc_now()),
    )
    return int(
        conn.execute(
            "SELECT id FROM proposals WHERE item_id=?", (item_id,)
        ).fetchone()["id"]
    )


# --------------------------------------------------------------------------
# 19-23: what is and is not a conflict
# --------------------------------------------------------------------------


def test_two_waiting_decisions_over_different_files_do_not_conflict(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/one.flac", b"one")
    library_file(settings, "Music/two.flac", b"two")
    scan_root(conn, "library", settings.library_dir, settings)

    plan_for(conn, settings, [("Music/one.flac", "Music/A/one.flac")])
    plan_for(conn, settings, [("Music/two.flac", "Music/B/two.flac")])

    assert count(conn) == 0


def test_two_decisions_over_the_same_file_conflict(tmp_path: Path) -> None:
    """A rename and a replacement over one track. Each is reasonable; the
    second cannot still be right once the first has run."""
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    scan_root(conn, "library", settings.library_dir, settings)
    first = plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])

    second = plan_for(
        conn, settings, [("Music/song.mp3", "Music/Other/song.mp3")], approve=False
    )
    found = check(conn, second)

    assert [item.kind for item in found] == [SAME_FILE, SAME_FILE]
    assert {party.ref for item in found for party in item.parties} == {first, second}


def test_two_decisions_aiming_at_one_destination_conflict(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/one.flac", b"one")
    library_file(settings, "Music/two.flac", b"two")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_for(conn, settings, [("Music/one.flac", "Music/Queen/track.flac")])

    second = plan_for(
        conn, settings, [("Music/two.flac", "Music/Queen/track.flac")], approve=False
    )
    found = check(conn, second)

    assert [item.kind for item in found] == [SAME_PLACE]
    assert found[0].subject == "Music/Queen/track.flac"


def test_one_shared_member_is_enough_to_conflict_with_a_group(
    tmp_path: Path,
) -> None:
    """Seventeen untouched files do not make reversing all eighteen safe, and
    they do not make committing both decisions safe either."""
    _, conn, settings = client_for(tmp_path)
    for index in range(18):
        library_file(settings, f"Music/Album/{index:02d}.flac", f"track {index}".encode())
    scan_root(conn, "library", settings.library_dir, settings)
    plan_for(
        conn,
        settings,
        [
            (f"Music/Album/{index:02d}.flac", f"Music/Queen/{index:02d}.flac")
            for index in range(18)
        ],
    )

    second = plan_for(
        conn,
        settings,
        [("Music/Album/07.flac", "Music/Elsewhere/07.flac")],
        approve=False,
    )

    assert check(conn, second)


def test_sharing_a_parent_folder_is_not_a_conflict(tmp_path: Path) -> None:
    """Two decisions inside one album is an ordinary afternoon."""
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/Album/one.flac", b"one")
    library_file(settings, "Music/Album/two.flac", b"two")
    scan_root(conn, "library", settings.library_dir, settings)

    plan_for(conn, settings, [("Music/Album/one.flac", "Music/Album/01 one.flac")])
    plan_for(conn, settings, [("Music/Album/two.flac", "Music/Album/02 two.flac")])

    assert count(conn) == 0


def test_a_decision_that_vacates_a_path_another_fills_is_not_a_conflict(
    tmp_path: Path,
) -> None:
    """One takes a file out of a place; the other puts one in. Run in either
    order one of them succeeds, and the executor's own preflight is what
    decides the order — this is not a contradiction to report."""
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/slot.flac", b"currently here")
    library_file(settings, "Music/other.flac", b"wants the slot")
    scan_root(conn, "library", settings.library_dir, settings)

    plan_for(conn, settings, [("Music/slot.flac", "Music/Archive/slot.flac")])
    plan_for(conn, settings, [("Music/other.flac", "Music/slot.flac")])

    assert count(conn) == 0


# --------------------------------------------------------------------------
# 24-25: relationships
# --------------------------------------------------------------------------


def item_id(conn: sqlite3.Connection, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


def pair_up(conn: sqlite3.Connection, subject: str, companion: str, kind: str) -> None:
    from librairy.relationships import record

    record(
        conn,
        subject_item_id=item_id(conn, subject),
        companion_item_id=item_id(conn, companion),
        kind=kind,
        provenance="exif: same exposure",
    )


def test_two_decisions_that_merely_mention_one_relationship_do_not_conflict(
    tmp_path: Path,
) -> None:
    """Neither of them is changing it. A relationship is evidence about files,
    and two decisions reading the same evidence is not a collision.

    Two tracks from one album, each filed somewhere new, each separated from
    the same cue sheet — which neither decision touches. Both approvals record
    that in `plan_relationships`, exactly as they should.
    """
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/Album/01.flac", b"one")
    library_file(settings, "Music/Album/02.flac", b"two")
    library_file(settings, "Music/Album/album.cue", b"cue sheet")
    scan_root(conn, "library", settings.library_dir, settings)
    pair_up(conn, "Music/Album/01.flac", "Music/Album/album.cue", "cue")
    pair_up(conn, "Music/Album/02.flac", "Music/Album/album.cue", "cue")

    first = plan_for(conn, settings, [("Music/Album/01.flac", "Music/Queen/01.flac")])
    second = plan_for(conn, settings, [("Music/Album/02.flac", "Music/Queen/02.flac")])
    outside = {
        int(row["outside_item_id"])
        for row in conn.execute(
            "SELECT outside_item_id FROM plan_relationships WHERE plan_id IN (?, ?)",
            (first, second),
        )
        if row["outside_item_id"] is not None
    }

    #  Both approvals really did name the same file as the one staying behind.
    assert outside == {item_id(conn, "Music/Album/album.cue")}
    assert count(conn) == 0


def test_moving_a_file_another_decision_was_explained_by_conflicts(
    tmp_path: Path,
) -> None:
    """One decision would set the MOV aside; the other was approved on the
    basis of the MOV staying exactly where it is."""
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Photos/IMG_1.HEIC", b"still")
    library_file(settings, "Photos/IMG_1.MOV", b"motion")
    scan_root(conn, "library", settings.library_dir, settings)
    pair_up(conn, "Photos/IMG_1.HEIC", "Photos/IMG_1.MOV", "live_photo")

    explained = plan_for(
        conn, settings, [("Photos/IMG_1.HEIC", "Photos/2024/IMG_1.HEIC")]
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM plan_relationships WHERE plan_id=?"
        " AND outside_item_id IS NOT NULL",
        (explained,),
    ).fetchone()[0] == 1

    mover = plan_for(
        conn, settings, [("Photos/IMG_1.MOV", "Photos/Aside/IMG_1.MOV")], approve=False
    )
    found = check(conn, mover)

    assert found
    assert any(item.kind == plan_conflicts.RELATED_FILE for item in found)


# --------------------------------------------------------------------------
# 26-30: approval, the queue, and the executor
# --------------------------------------------------------------------------


def test_approval_refuses_a_decision_that_contradicts_a_waiting_one(
    tmp_path: Path,
) -> None:
    """The door, not the queue. `_approval_errors` already refuses a plan that
    names one file twice; this is the identical rule one scope up."""
    import pytest

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])

    second = plan_for(
        conn, settings, [("Music/song.mp3", "Music/Other/song.mp3")], approve=False
    )
    with pytest.raises(PlanConflict) as refused:
        approve_plan(conn, second, settings)

    assert "song.mp3" in str(refused.value)
    assert "Send that one back" in str(refused.value)


def test_a_refused_approval_leaves_the_existing_decision_alone(
    tmp_path: Path,
) -> None:
    """Nothing is cancelled by finding a conflict. Which of two decisions to
    keep is the owner's question and no rule the program has can answer it."""
    import pytest

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    scan_root(conn, "library", settings.library_dir, settings)
    first = plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])

    second = plan_for(
        conn, settings, [("Music/song.mp3", "Music/Other/song.mp3")], approve=False
    )
    with pytest.raises(PlanConflict):
        approve_plan(conn, second, settings)

    statuses = dict(
        conn.execute("SELECT id, status FROM plans WHERE id IN (?, ?)", (first, second))
    )
    assert statuses[first] == "approved"
    assert statuses[second] == "draft"


def test_two_arrivals_aiming_at_one_destination_are_marked_before_commit(
    tmp_path: Path,
) -> None:
    """The case approval cannot catch: an arrival approved in Review never
    passes through `approve_plan`, and two of them wanting one path is the
    collision the old build discovered by failing at Commit."""
    from librairy.web.commit_queue import queue_rows

    client, conn, settings = client_for(tmp_path)
    arrival(conn, settings, "manual-a.pdf", "Documents/Manuals/Honda.pdf")
    arrival(conn, settings, "manual-b.pdf", "Documents/Manuals/Honda.pdf")

    rows = queue_rows(conn, settings, kind="new-file")
    page = flat(client.get("/commit").text)

    assert [bool(row.get("conflict")) for row in rows] == [True, True]
    assert "Conflicts with another decision" in page
    assert "Documents/Manuals/Honda.pdf" in page


def test_a_conflicting_arrival_is_left_out_of_the_batch_rather_than_cancelled(
    tmp_path: Path,
) -> None:
    """Every arrival is filed by one plan, so two of them wanting one path used
    to stop all of them. They are left out, said out loud, and still approved —
    which of the two to keep is not a question the program may answer."""
    from librairy.web.commit import create_commit_plan

    client, conn, settings = client_for(tmp_path)
    arrival(conn, settings, "manual-a.pdf", "Documents/Manuals/Honda.pdf")
    arrival(conn, settings, "manual-b.pdf", "Documents/Manuals/Honda.pdf")
    arrival(conn, settings, "receipt.pdf", "Documents/2026/receipt.pdf")

    page = flat(client.get("/commit").text)
    plan_id = create_commit_plan(conn, settings)

    assert "2 of these decisions are in conflict" in page
    filed = [
        str(row["src_relpath"])
        for row in conn.execute(
            "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
        )
    ]
    assert filed == ["receipt.pdf"]
    #  Neither of the two was cancelled by being left out.
    approved = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status='approved'"
    ).fetchone()[0]
    assert approved == 3


def test_a_conflicting_plan_offers_no_commit_button(tmp_path: Path) -> None:
    """A plan-based decision commits on its own, so its own button goes."""
    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/one.flac", b"one")
    library_file(settings, "Music/two.flac", b"two")
    scan_root(conn, "library", settings.library_dir, settings)
    first = plan_for(conn, settings, [("Music/one.flac", "Music/Q/track.flac")])
    second = plan_for(
        conn, settings, [("Music/two.flac", "Music/Q/track.flac")], approve=False
    )
    #  Approval refuses this, so the only way two conflicting plans reach the
    #  queue is a database written before the rule existed. Reproduced here by
    #  marking it approved directly, which is exactly what such a database
    #  looks like.
    conn.execute("UPDATE plans SET status='approved' WHERE id=?", (second,))

    page = client.get("/commit?type=correction").text

    assert "Conflicts with another decision" in page
    assert f"/commit/execute/{first}" not in page
    assert f"/commit/execute/{second}" not in page


def test_finding_a_conflict_moves_no_files_and_writes_no_journal(
    tmp_path: Path,
) -> None:
    """Detection is a read. It has to be: this runs while a page is drawn."""
    import pytest

    client, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])
    second = plan_for(
        conn, settings, [("Music/song.mp3", "Music/Other/song.mp3")], approve=False
    )
    before = sorted(p.name for p in settings.library_dir.rglob("*") if p.is_file())

    with pytest.raises(PlanConflict):
        approve_plan(conn, second, settings)
    assert client.get("/commit").status_code == 200
    assert count(conn) >= 0

    after = sorted(p.name for p in settings.library_dir.rglob("*") if p.is_file())
    assert after == before
    assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 0


def test_withdrawing_one_decision_clears_the_conflict(tmp_path: Path) -> None:
    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    scan_root(conn, "library", settings.library_dir, settings)
    first = plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])
    second = plan_for(
        conn, settings, [("Music/song.mp3", "Music/Other/song.mp3")], approve=False
    )
    assert check(conn, second)

    #  The existing withdrawal: an approved plan that has not executed is
    #  removed whole, because there is no journal entry to reconcile.
    conn.execute("DELETE FROM plan_ops WHERE plan_id=?", (first,))
    conn.execute("DELETE FROM plans WHERE id=?", (first,))

    assert check(conn, second) == []
    assert approve_plan(conn, second, settings)


def test_the_executor_still_checks_everything_it_checked_before(
    tmp_path: Path,
) -> None:
    """Pre-Commit detection is an acceleration, never a replacement.

    Between the page and the move there is still a filesystem other programs
    can write to, and the hash-verified preflight is what makes that safe.
    """
    _, conn, settings = client_for(tmp_path)
    path = library_file(settings, "Music/song.mp3", b"the approved bytes")
    scan_root(conn, "library", settings.library_dir, settings)
    plan_id = plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])

    #  No conflict at all — one waiting decision — and the file changes anyway.
    assert check(conn, plan_id) == []
    path.write_bytes(b"somebody else wrote this")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.skipped_changed == 1
    assert summary.done == 0
    assert path.read_bytes() == b"somebody else wrote this"
    assert not (settings.library_dir / "Music/New/song.mp3").exists()


# --------------------------------------------------------------------------
# 31: pending conflicts are not committed dependencies
# --------------------------------------------------------------------------


def test_a_pending_conflict_creates_no_undo_dependency(tmp_path: Path) -> None:
    """A decision that has not run has moved no files, so nothing can have
    been built on it. The two ideas are related and are not the same thing."""
    from librairy.undo_sequence import CLEAR, sequence

    _, conn, settings = client_for(tmp_path)
    library_file(settings, "Music/song.mp3")
    library_file(settings, "Music/other.mp3", b"other")
    scan_root(conn, "library", settings.library_dir, settings)
    done = plan_for(conn, settings, [("Music/song.mp3", "Music/New/song.mp3")])
    execute_plan(conn, done, settings)

    #  A waiting decision that collides with the executed one's destination.
    pending = plan_for(
        conn, settings, [("Music/other.mp3", "Music/New/song.mp3")], approve=False
    )
    approve_plan(conn, pending, settings)

    assert sequence(conn, done).state == CLEAR
    assert sequence(conn, done).blockers == []


# --------------------------------------------------------------------------
# 32-34: it has to stay bounded
# --------------------------------------------------------------------------


def many_waiting(
    conn: sqlite3.Connection, settings: Settings, plans: int, *, ops: int = 5
) -> None:
    for index in range(plans):
        for op in range(ops):
            library_file(
                settings, f"Music/p{index}/t{op}.flac", f"{index}-{op}".encode()
            )
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(plans):
        plan_for(
            conn,
            settings,
            [
                (f"Music/p{index}/t{op}.flac", f"Music/filed/{index}-{op}.flac")
                for op in range(ops)
            ],
        )


def test_conflict_lookup_is_bounded_at_a_hundred_waiting_decisions(
    tmp_path: Path,
) -> None:
    _, conn, settings = client_for(tmp_path)
    many_waiting(conn, settings, 100)

    start = time.perf_counter()
    found = count(conn)
    elapsed = time.perf_counter() - start

    assert found == 0
    assert elapsed < 1.0, f"{elapsed:.3f}s for 100 waiting decisions"


def test_conflict_lookup_does_not_compare_every_pair(tmp_path: Path) -> None:
    """The shape that matters more than the number.

    A self-join of waiting operations against waiting operations is O(n²): five
    thousand waiting operations is twenty-five million comparisons to find the
    handful that collide. Grouping by the thing claimed is one sort, so ten
    times the queue costs roughly ten times as much rather than a hundred.
    """
    _, conn, settings = client_for(tmp_path)
    many_waiting(conn, settings, 50)
    start = time.perf_counter()
    count(conn)
    small = time.perf_counter() - start

    many_waiting_more(conn, settings, 50, 500)
    start = time.perf_counter()
    count(conn)
    large = time.perf_counter() - start

    #  Ten times the queue. Quadratic would be a hundredfold; a generous
    #  ceiling of thirty still fails that and passes anything near-linear.
    assert large < max(small, 0.001) * 30, f"{small:.4f}s -> {large:.4f}s"


def many_waiting_more(
    conn: sqlite3.Connection, settings: Settings, start: int, stop: int
) -> None:
    for index in range(start, stop):
        for op in range(5):
            library_file(
                settings, f"Music/p{index}/t{op}.flac", f"{index}-{op}".encode()
            )
    scan_root(conn, "library", settings.library_dir, settings)
    for index in range(start, stop):
        plan_for(
            conn,
            settings,
            [
                (f"Music/p{index}/t{op}.flac", f"Music/filed/{index}-{op}.flac")
                for op in range(5)
            ],
        )


def test_a_page_of_cards_asks_two_queries_however_much_is_waiting(
    tmp_path: Path,
) -> None:
    """The Commit page asks about the fifty decisions it is drawing, not about
    the database. The far side of a collision is found from the keys those
    fifty claim, which is what keeps it two queries at any size."""
    _, conn, settings = client_for(tmp_path)
    many_waiting(conn, settings, 60)
    page = [
        (plan_conflicts.PLAN, str(row["id"]))
        for row in conn.execute("SELECT id FROM plans LIMIT 50")
    ]
    queries: list[str] = []
    conn.set_trace_callback(lambda sql: queries.append(sql))
    try:
        for_decisions(conn, page)
    finally:
        conn.set_trace_callback(None)

    assert len(queries) <= 2, queries
