"""Taking a review decision back.

Every button in Review was a one-way door, and "Not this" was the worst of
them: it drops a file out of the queue entirely, and the only way back was a
CLI flag nothing in the portal mentioned.

Two rules this pins. The whole batch comes back on one press, because
approving forty by accident is exactly when undo matters. And anything already
committed is left alone — the files have moved by then, and flipping a status
back would describe a library that does not exist.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.review_undo import latest, undo_last
from librairy.web.app import create_app
from librairy.web.review import ReviewFilters, apply_review_action


def setup(tmp_path: Path):  # noqa: ANN201
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    return settings, connect(settings)


def seed(conn, relpath: str, dest: str | None = "Music/song.mp3") -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at)
        VALUES ('inbox', ?, 1, 1, ?, 'proposed', 'now', 'now')
        """,
        (relpath, relpath),
    )
    item_id = int(cursor.lastrowid)
    return upsert_proposal(
        conn,
        item_id=item_id,
        category="music",
        clean_name=relpath,
        dest_relpath=dest,
        confidence=0.9,
        evidence=[EvidenceEntry("heuristic", "category", "music", 0.9)],
    )


def status_of(conn, proposal_id: int) -> str:
    return conn.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]


def state_of(conn, proposal_id: int) -> str:
    return conn.execute(
        "SELECT i.state FROM items i JOIN proposals p ON p.item_id = i.id WHERE p.id=?",
        (proposal_id,),
    ).fetchone()[0]


def test_approving_by_accident_is_reversible(tmp_path: Path) -> None:
    _settings, conn = setup(tmp_path)
    proposal = seed(conn, "song.mp3")

    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])
    assert status_of(conn, proposal) == "approved"

    result = undo_last(conn)

    assert result.restored == 1
    assert status_of(conn, proposal) == "proposed"
    assert state_of(conn, proposal) == "proposed"


def test_not_this_is_reversible_which_is_the_whole_point(tmp_path: Path) -> None:
    """It sets the file to 'pending', which drops it out of Review entirely."""
    _settings, conn = setup(tmp_path)
    proposal = seed(conn, "song.mp3")

    apply_review_action(conn, "reject", ReviewFilters(), proposal_ids=[proposal])
    assert status_of(conn, proposal) == "rejected"
    assert state_of(conn, proposal) == "pending"

    undo_last(conn)

    assert status_of(conn, proposal) == "proposed"
    assert state_of(conn, proposal) == "proposed"


def test_quarantining_restores_the_destination_it_overwrote(tmp_path: Path) -> None:
    """Discard rewrites action, dest_root and dest_relpath, so the snapshot is
    the only record of where the file was originally going."""
    _settings, conn = setup(tmp_path)
    proposal = seed(conn, "song.mp3", dest="Music/Rock/song.mp3")

    apply_review_action(conn, "discard", ReviewFilters(), proposal_ids=[proposal])
    after = conn.execute(
        "SELECT action, dest_root, dest_relpath FROM proposals WHERE id=?", (proposal,)
    ).fetchone()
    assert after["action"] == "quarantine"

    undo_last(conn)

    row = conn.execute(
        "SELECT status, action, dest_root, dest_relpath FROM proposals WHERE id=?", (proposal,)
    ).fetchone()
    assert row["status"] == "proposed"
    assert row["action"] == "move"
    assert row["dest_root"] == "library"
    assert row["dest_relpath"] == "Music/Rock/song.mp3"


def test_one_press_takes_back_the_whole_batch(tmp_path: Path) -> None:
    _settings, conn = setup(tmp_path)
    proposals = [seed(conn, f"song-{index}.mp3") for index in range(5)]

    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=proposals)
    result = undo_last(conn)

    assert result.restored == 5
    assert all(status_of(conn, proposal) == "proposed" for proposal in proposals)


def test_a_committed_decision_is_left_alone(tmp_path: Path) -> None:
    """The files have moved. History undoes the move; this must not pretend."""
    _settings, conn = setup(tmp_path)
    proposal = seed(conn, "song.mp3")
    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])
    conn.execute("UPDATE proposals SET status='committed' WHERE id=?", (proposal,))

    result = undo_last(conn)

    assert result.restored == 0
    assert result.skipped_committed == 1
    assert status_of(conn, proposal) == "committed"
    assert "History" in result.message


def test_undo_is_one_step_at_a_time_most_recent_first(tmp_path: Path) -> None:
    _settings, conn = setup(tmp_path)
    first = seed(conn, "first.mp3")
    second = seed(conn, "second.mp3")

    apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[first])
    apply_review_action(conn, "reject", ReviewFilters(), proposal_ids=[second])

    undo_last(conn)
    assert status_of(conn, second) == "proposed"
    assert status_of(conn, first) == "approved", "the earlier batch is still done"

    undo_last(conn)
    assert status_of(conn, first) == "proposed"


def test_nothing_to_undo_says_so_rather_than_failing(tmp_path: Path) -> None:
    _settings, conn = setup(tmp_path)

    result = undo_last(conn)

    assert result.restored == 0
    assert result.message == "Nothing to undo."
    assert latest(conn) is None


def test_the_offer_names_what_it_will_take_back(tmp_path: Path) -> None:
    _settings, conn = setup(tmp_path)
    proposals = [seed(conn, f"song-{index}.mp3") for index in range(3)]

    apply_review_action(conn, "discard", ReviewFilters(), proposal_ids=proposals)

    entry = latest(conn)
    assert entry is not None
    assert entry.summary == "Sent to quarantine 3 files"


def test_the_journal_does_not_grow_without_bound(tmp_path: Path) -> None:
    from librairy.review_undo import KEEP_BATCHES

    _settings, conn = setup(tmp_path)
    for index in range(KEEP_BATCHES + 5):
        proposal = seed(conn, f"song-{index}.mp3")
        apply_review_action(conn, "approve", ReviewFilters(), proposal_ids=[proposal])

    kept = conn.execute("SELECT COUNT(*) FROM review_undo").fetchone()[0]
    assert kept == KEEP_BATCHES


def test_the_review_page_offers_undo_only_after_a_decision(tmp_path: Path) -> None:
    settings, conn = setup(tmp_path)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    proposal = seed(conn, "song.mp3")

    before = client.get("/review")
    assert "undo-bar" not in before.text

    client.post(
        "/review/action",
        data={"action": "approve", "proposal_id": str(proposal), "state": "proposed"},
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    after = client.get("/review")
    assert "undo-bar" in after.text
    assert "Approved 1 file" in after.text

    undone = client.post(
        "/review/undo", headers={"x-csrf-token": client.cookies["csrf_token"]}
    )

    assert undone.status_code == 200
    assert "Put 1 back in the queue." in undone.text
    assert status_of(conn, proposal) == "proposed"
