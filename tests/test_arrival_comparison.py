"""An arrival that is the same recording and not the same bytes.

    Inbox/Death on Two Legs.flac                       lossless, 38 MB
    Library/Music/Rock/Queen/…/01 - Death on Two Legs.mp3   320 kbps, 7 MB

The exact-duplicate case is settled: the arrival is redundant and the only
question is whether to set it aside. This is not that question. Nothing is
redundant — the person has two representations of one recording, one filed and
one arriving — and the asymmetry is what makes the answers different from the
library-to-library ones. There, either representation may be kept and both are
already filed; here one is filed and one is knocking at the door.

The property this file exists to hold down is that **the arriving one can win
without anything being overwritten**. The filed copy is preserved in Quarantine
first and only then does the arrival take its place, in one plan, marked
coherent so neither half can happen without the other.

The last section is the memory: an explicit Restore is somebody saying they
want both, and the next audit is not entitled to ask again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.arrival_comparison import (
    KEEP_BOTH,
    KEEP_LIBRARY,
    USE_ARRIVAL,
    describe,
    resolve,
    similar_arrival,
)
from librairy.config import Settings
from librairy.corrections import CorrectionRefused
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import utc_now
from librairy.scanner import scan_root

FILED = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.mp3"
ARRIVING = "Death on Two Legs.flac"
LANDING = "Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def build(tmp_path: Path, *, arrival: str = "lossless bytes", filed: str = "lossy bytes"):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "library", {FILED: filed})
    write(settings, "inbox", {ARRIVING: arrival})
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    proposal(conn, item_id(conn, "inbox", ARRIVING))
    return conn, settings


def write(settings: Settings, root: str, files: dict[str, str]) -> None:
    base = settings.inbox_dir if root == "inbox" else settings.library_dir
    for relpath, body in files.items():
        path = base / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def item_id(conn, root: str, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?", (root, relpath)
        ).fetchone()["id"]
    )


def proposal(conn, item: int) -> None:
    """The ordinary Review row this arrival would have anyway."""
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, 'music', ?, ?, 0.8, 'proposed', 'move', 'library', '[]', ?, ?)",
        (item, ARRIVING, f"Music/Rock/Queen/{ARRIVING}", utc_now(), utc_now()),
    )


def pair(conn, *, kind: str = "audio") -> None:
    """One czkawka pairing across the roots, as `dedup` writes it."""
    first, second = sorted(
        (item_id(conn, "inbox", ARRIVING), item_id(conn, "library", FILED))
    )
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, ?, 0.95, ?)",
        (first, second, kind, utc_now()),
    )


def compared(tmp_path: Path, **kwargs):
    conn, settings = build(tmp_path, **kwargs)
    pair(conn)
    return conn, settings


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def arrival_id(conn) -> int:
    return item_id(conn, "inbox", ARRIVING)


# --- what becomes a cross-root comparison -------------------------------------------


def test_a_non_identical_arrival_is_a_comparison(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    found = similar_arrival(conn, settings, arrival_id(conn))

    assert found is not None
    assert found.twin.relpath == FILED


def test_identical_bytes_stay_with_the_exact_duplicate_workflow(tmp_path: Path) -> None:
    """czkawka pairs those too, and they have an answer this one does not.

    An exact duplicate is redundant; these two are a preference. Describing
    them with the same words would be a claim about bytes that differ.
    """
    conn, settings = compared(tmp_path, arrival="same bytes", filed="same bytes")

    assert similar_arrival(conn, settings, arrival_id(conn)) is None


def test_an_unpaired_arrival_is_not_a_comparison(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    assert similar_arrival(conn, settings, arrival_id(conn)) is None


def test_the_row_names_the_filed_copy_and_both_sizes(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    row = describe(conn, settings, arrival_id(conn))

    assert row["match"] == FILED
    assert row["arrival_size"] and row["twin_size"]
    assert row["destination"] == LANDING


def test_the_destination_comes_from_the_filed_copy(tmp_path: Path) -> None:
    """The comparison already established where this recording lives here.

    Sending the arrival back through a fresh guess would throw the better
    evidence away. Its own extension is kept, because a FLAC is not an MP3.
    """
    conn, settings = compared(tmp_path)

    found = similar_arrival(conn, settings, arrival_id(conn))

    assert found.dest_relpath == LANDING
    assert found.replaces_in_place is False


def test_the_comparison_facts_render_without_leaving_the_machine(
    tmp_path: Path, monkeypatch
) -> None:
    import socket

    from librairy.web.review import arrival_facts

    conn, settings = compared(tmp_path)

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a comparison must not reach the network")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    facts = arrival_facts(conn, settings, arrival_id(conn))

    assert [member["name"] for member in facts["members"]] == [
        f"Arriving: {ARRIVING}",
        "Filed: 01 - Death on Two Legs.mp3",
    ]
    assert any(line["label"] == "Size" for line in facts["rows"])
    assert "nothing here is a recommendation" in facts["note"]


# --- keep the filed copy ------------------------------------------------------------


def test_keeping_the_filed_copy_sets_only_the_arrival_aside(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)

    ops = conn.execute(
        "SELECT op_type, src_root, src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert [(op["op_type"], op["src_root"], op["src_relpath"]) for op in ops] == [
        ("quarantine", "inbox", ARRIVING)
    ]


def test_keeping_the_filed_copy_leaves_the_library_alone(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)

    execute_plan(conn, plan_id, settings)

    assert tree(settings.library_dir) == [FILED]
    assert tree(settings.inbox_dir) == []


def test_the_quarantined_arrival_says_similar_not_duplicate(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)

    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    assert entry["reason"] == "similar_media"
    assert entry["duplicate_of"] == item_id(conn, "library", FILED)


# --- use the arriving copy ----------------------------------------------------------


def test_using_the_arrival_preserves_the_filed_copy_first(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    ops = conn.execute(
        "SELECT seq, op_type, src_root FROM plan_ops WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    assert [(op["op_type"], op["src_root"]) for op in ops] == [
        ("quarantine", "library"),
        ("move", "inbox"),
    ]


def test_using_the_arrival_never_overwrites(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    execute_plan(conn, plan_id, settings)

    assert tree(settings.library_dir) == [LANDING]
    assert (settings.library_dir / LANDING).read_text() == "lossless bytes"
    #  The copy it replaced is not gone. It is somewhere you can look.
    assert any(path.name.endswith(".mp3") for path in settings.quarantine_dir.rglob("*"))


def test_the_replaced_copy_is_called_a_previous_representation(tmp_path: Path) -> None:
    from librairy.quarantine import is_previous_representation

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)
    execute_plan(conn, plan_id, settings)

    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    assert is_previous_representation(conn, entry) is True


def test_the_quarantine_row_says_replaced_rather_than_similar(tmp_path: Path) -> None:
    """It did not leave because it looked like something. It was replaced.

    "Close enough to something you already have to be worth a look" describes
    an invitation to compare; this file left because a comparison was already
    answered.
    """
    from librairy.web.quarantine import quarantine_data

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)
    execute_plan(conn, plan_id, settings)

    row = next(
        entry
        for entry in quarantine_data(conn, settings)["entries"]
        if "Death on Two Legs.mp3" in str(entry["original_relpath"])
    )

    assert row["reason_tag"] == "replaced"
    assert "replaced" in row["reason_text"]
    #  And by what. "Replaced" without saying by what leaves nobody able to
    #  judge whether to put it back.
    assert row["duplicate_of"].endswith(LANDING)


def test_using_the_arrival_is_one_decision_that_cannot_half_happen(
    tmp_path: Path,
) -> None:
    """If the move cannot happen, the preserving half must not happen either.

    Otherwise somebody's only copy of a recording is in Quarantine and there is
    nothing in the library — not data loss, and not remotely what they
    approved.
    """
    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)
    (settings.inbox_dir / ARRIVING).write_text("re-downloaded since")

    summary = execute_plan(conn, plan_id, settings)

    assert summary.done == 0
    assert tree(settings.library_dir) == [FILED]
    assert tree(settings.quarantine_dir) == []


def test_a_third_file_standing_at_the_destination_is_refused(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    write(settings, "library", {LANDING: "somebody else's flac"})
    scan_root(conn, "library", settings.library_dir, settings)

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)


def test_the_arrival_is_not_left_waiting_in_review_as_well(tmp_path: Path) -> None:
    """It cannot be both filed by this decision and undecided in the queue."""
    conn, settings = compared(tmp_path)

    resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    status = conn.execute(
        "SELECT status FROM proposals WHERE item_id=?", (arrival_id(conn),)
    ).fetchone()["status"]
    assert status == "superseded"


# --- keep both ----------------------------------------------------------------------


def test_keeping_both_makes_no_plan(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_BOTH)

    assert plan_id == ""
    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0


def test_keeping_both_does_not_strand_the_arrival(tmp_path: Path) -> None:
    """"Keep both" that left the second one in the inbox forever would be a
    promise the software did not keep. It carries on through Review."""
    conn, settings = compared(tmp_path)

    resolve(conn, settings, arrival_id(conn), KEEP_BOTH)

    row = conn.execute(
        "SELECT status, dest_root FROM proposals WHERE item_id=?", (arrival_id(conn),)
    ).fetchone()
    assert row["status"] == "proposed"
    assert row["dest_root"] == "library"


def test_keeping_both_stops_the_comparison_being_asked_again(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)

    resolve(conn, settings, arrival_id(conn), KEEP_BOTH)

    assert similar_arrival(conn, settings, arrival_id(conn)) is None


# --- the files moving underneath ----------------------------------------------------


def test_a_changed_arrival_blocks_the_decision(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    (settings.inbox_dir / ARRIVING).write_text("a different download")

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)


def test_a_changed_filed_copy_blocks_the_decision(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    (settings.library_dir / FILED).write_text("re-tagged since")

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)


def test_a_vanished_filed_copy_stops_being_a_comparison(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    (settings.library_dir / FILED).unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    assert similar_arrival(conn, settings, arrival_id(conn)) is None


def test_a_file_already_waiting_for_commit_is_refused(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)

    with pytest.raises(CorrectionRefused):
        resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)


def test_nothing_is_ever_deleted(tmp_path: Path) -> None:
    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    execute_plan(conn, plan_id, settings)

    assert len(tree(settings.library_dir)) + len(tree(settings.quarantine_dir)) == 2


# --- the memory ---------------------------------------------------------------------


def test_restoring_a_representation_answers_the_comparison(tmp_path: Path) -> None:
    """Restore is somebody saying they want both. Asking again on the next
    audit is the software forgetting a decision it just watched them make."""
    from librairy.quarantine import restore_entry

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert similar_arrival(conn, settings, arrival_id(conn)) is None


def test_a_restored_representation_is_back_where_it_came_from(tmp_path: Path) -> None:
    from librairy.quarantine import restore_entry

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), KEEP_LIBRARY)
    execute_plan(conn, plan_id, settings)
    entry = conn.execute("SELECT * FROM quarantine_entries").fetchone()

    restore_entry(conn, int(entry["id"]), settings)

    assert tree(settings.inbox_dir) == [ARRIVING]


def test_re_encoding_one_side_makes_it_a_live_comparison_again(tmp_path: Path) -> None:
    """The answer was about two files. Replace one and nobody has been asked."""
    conn, settings = compared(tmp_path)
    resolve(conn, settings, arrival_id(conn), KEEP_BOTH)

    (settings.inbox_dir / ARRIVING).write_text("a better rip, re-downloaded")
    scan_root(conn, "inbox", settings.inbox_dir, settings)

    assert similar_arrival(conn, settings, arrival_id(conn)) is not None


def test_the_suppression_is_not_by_filename(tmp_path: Path) -> None:
    """Same name, different bytes, and it is a question again."""
    conn, settings = compared(tmp_path)
    resolve(conn, settings, arrival_id(conn), KEEP_BOTH)

    (settings.library_dir / FILED).write_text("a different transcode")
    scan_root(conn, "library", settings.library_dir, settings)

    found = similar_arrival(conn, settings, arrival_id(conn))

    assert found is not None
    assert found.twin.relpath == FILED


# --- looking before you leap --------------------------------------------------------


def test_commit_offers_a_preview_of_what_it_is_about_to_move(tmp_path: Path) -> None:
    """The one page where the move actually happens had no way to see the file.

    Every other page that touches files offers this. Commit did not: you could
    read a path, press a button, and the only way to look at what you were
    committing was to go back to Review.
    """
    from librairy.web.commit_queue import queue_rows

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    rows = queue_rows(conn, settings, kind="correction")

    assert len(rows) == 1
    assert rows[0]["item_id"] is not None
    assert rows[0]["plan_id"] == plan_id


def test_the_commit_page_carries_the_script_its_preview_needs(tmp_path: Path) -> None:
    """Markup without the listener is a button that does nothing at all.

    Quarantine shipped exactly that once, and every DOM assertion passed.
    """
    page = Path("src/librairy/web/templates/commit.html").read_text(encoding="utf-8")

    assert "data-preview-toggle" in page
    assert "/static/previews.js" in page
    assert "partials/lightbox.html" in page


def test_sending_a_comparison_decision_back_restores_the_question(
    tmp_path: Path,
) -> None:
    """An answer withdrawn must not leave the question suppressed.

    Otherwise the row comes back to Review with nothing on it to decide, which
    is the same dead end as the button that did nothing.
    """
    from librairy.arrival_comparison import withdraw

    conn, settings = compared(tmp_path)
    plan_id = resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    withdraw(conn, settings, plan_id)

    assert conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 0
    assert similar_arrival(conn, settings, arrival_id(conn)) is not None
    status = conn.execute(
        "SELECT status FROM proposals WHERE item_id=?", (arrival_id(conn),)
    ).fetchone()["status"]
    assert status == "proposed"


def test_a_comparison_decision_appears_on_the_commit_page(tmp_path: Path) -> None:
    """It has no audit finding, and without this it sat approved and invisible."""
    from librairy.web.commit_queue import queue_rows, queue_summary

    conn, settings = compared(tmp_path)
    resolve(conn, settings, arrival_id(conn), USE_ARRIVAL)

    rows = queue_rows(conn, settings, kind="correction")
    groups = {group["type"]: group for group in queue_summary(conn)["groups"]}

    assert len(rows) == 1
    assert rows[0]["subject"] == ARRIVING
    assert rows[0]["current"] == f"inbox/{ARRIVING}"
    assert rows[0]["after"] == f"library/{LANDING}"
    assert "nothing is overwritten" in rows[0]["reason"]
    assert rows[0]["back_url"] == f"/commit/withdraw/{rows[0]['plan_id']}"
    assert groups["correction"]["decisions"] == 1
