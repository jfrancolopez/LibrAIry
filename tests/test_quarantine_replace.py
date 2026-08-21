"""Quarantine answering back: use this one instead of the one you filed.

    Library     Music/Rock/Queen/A Night at the Opera/01 - Song.flac
    Quarantine  01 - Song.mp3   — set aside after comparing with that file

A held file had one answer, `Restore`, and it means *bring this back as well*.
The row already knew what it had been compared with and what stood in its
place; it just could not act on it. So the reciprocal answer exists now, and
these tests are about the ways a swap can be wrong: describing itself as a
restore, acting on a comparison one side of which has since changed, landing on
top of a third file, or leaving the library with neither representation because
one half of it ran and the other did not.

The last section is the flip back, which is deliberately not an undo. Swapping
twice is two recorded decisions, and History reads as two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.arrival_comparison import KEEP_LIBRARY, USE_ARRIVAL, resolve
from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.planner import utc_now
from librairy.quarantine import QuarantineError
from librairy.quarantine_replace import (
    LABEL,
    describe,
    replacement_for,
    request_replacement,
)
from librairy.scanner import scan_root

FILED = "Music/Rock/Queen/A Night at the Opera/01 - Song.flac"
ARRIVING = "01 - Song.mp3"
SLOT = "Music/Rock/Queen/A Night at the Opera/01 - Song.mp3"


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


def write(settings: Settings, root: str, files: dict[str, str]) -> None:
    base = {
        "inbox": settings.inbox_dir,
        "library": settings.library_dir,
        "quarantine": settings.quarantine_dir,
    }[root]
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


def proposal(conn, item: int, name: str) -> None:
    conn.execute(
        "INSERT INTO proposals(item_id, category, clean_name, dest_relpath, confidence,"
        " status, action, dest_root, evidence, created_at, updated_at)"
        " VALUES (?, 'music', ?, ?, 0.8, 'proposed', 'move', 'library', '[]', ?, ?)",
        (item, name, f"Music/Rock/Queen/{name}", utc_now(), utc_now()),
    )


def pair(conn, left: int, right: int, *, kind: str = "audio") -> None:
    first, second = sorted((left, right))
    conn.execute(
        "INSERT OR IGNORE INTO similar_media_flags(item_id, similar_item_id, kind,"
        " score, created_at) VALUES (?, ?, ?, 0.95, ?)",
        (first, second, kind, utc_now()),
    )


def held(tmp_path: Path, *, arriving: str = ARRIVING):
    """A comparison the *filed* copy won, so the other one sits in Quarantine.

    Built through the real workflow rather than by writing rows: the whole
    point of the feature is that the provenance a comparison leaves behind is
    enough to act on later, and a hand-built entry would not prove that.
    """
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "library", {FILED: "the filed lossless one"})
    write(settings, "inbox", {arriving: "the other representation"})
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    proposal(conn, item_id(conn, "inbox", arriving), arriving)
    pair(conn, item_id(conn, "inbox", arriving), item_id(conn, "library", FILED))

    plan_id = resolve(conn, settings, item_id(conn, "inbox", arriving), KEEP_LIBRARY)
    execute_plan(conn, plan_id, settings)
    entry = conn.execute(
        "SELECT * FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return conn, settings, int(entry["id"])


def duplicate_held(tmp_path: Path):
    """The same shape, for a file held because its bytes already existed."""
    conn, settings, entry_id = held(tmp_path)
    conn.execute(
        "UPDATE quarantine_entries SET reason='exact_duplicate' WHERE id=?", (entry_id,)
    )
    return conn, settings, entry_id


def held_path(conn, settings: Settings, entry_id: int) -> Path:
    """Where the held file actually is: Quarantine files sit under a date."""
    relpath = conn.execute(
        "SELECT i.relpath FROM quarantine_entries q JOIN items i ON i.id = q.item_id"
        " WHERE q.id=?",
        (entry_id,),
    ).fetchone()["relpath"]
    return settings.quarantine_dir / str(relpath)


def tree(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )


def library_text(settings: Settings, relpath: str) -> str:
    return (settings.library_dir / relpath).read_text(encoding="utf-8")


# --- when it is offered ---------------------------------------------------------------


def test_a_held_representation_can_take_the_place_of_the_filed_one(
    tmp_path: Path,
) -> None:
    conn, settings, entry_id = held(tmp_path)

    found = replacement_for(conn, entry_id)

    assert found is not None
    assert found.active_relpath == FILED
    assert found.dest_relpath == SLOT


def test_an_exact_duplicate_is_never_offered_this(tmp_path: Path) -> None:
    """Same bytes, so there is no representation to prefer and the swap would
    move two files to leave the library exactly as it was."""
    conn, settings, entry_id = duplicate_held(tmp_path)

    assert replacement_for(conn, entry_id) is None
    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


def test_nothing_is_offered_once_the_filed_copy_has_gone(tmp_path: Path) -> None:
    """A button whose counterpart no longer exists can only produce an error."""
    conn, settings, entry_id = held(tmp_path)
    (settings.library_dir / FILED).unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    assert replacement_for(conn, entry_id) is None


def test_the_page_says_what_would_happen(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    entry = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()

    shown = describe(conn, entry)

    assert shown is not None
    assert shown["label"] == LABEL
    assert shown["active_name"] == "01 - Song.flac"
    assert shown["dest_relpath"] == SLOT
    assert shown["same_path"] is False


def test_the_quarantine_row_offers_both_answers_and_names_them_apart(
    tmp_path: Path,
) -> None:
    """`Restore` and `Use this instead` end differently — both active, or one.
    On a row that offers both, the first one has to say which it is."""
    from librairy.web.quarantine import quarantine_data

    conn, settings, entry_id = held(tmp_path)

    rows = quarantine_data(conn, settings)["entries"]

    row = next(row for row in rows if row["id"] == entry_id)
    assert row["replacement"] is not None
    assert row["restorable"] is True


# --- what it builds ---------------------------------------------------------------------


def test_the_plan_preserves_the_filed_copy_before_admitting_this_one(
    tmp_path: Path,
) -> None:
    conn, settings, entry_id = held(tmp_path)

    plan_id = request_replacement(conn, settings, entry_id)

    ops = conn.execute(
        "SELECT op_type, src_root, src_relpath, dest_root, dest_relpath FROM plan_ops"
        " WHERE plan_id=? ORDER BY seq",
        (plan_id,),
    ).fetchall()
    assert [op["op_type"] for op in ops] == ["quarantine", "move"]
    assert ops[0]["src_root"] == "library"
    assert ops[0]["src_relpath"] == FILED
    assert ops[1]["src_root"] == "quarantine"
    assert ops[1]["dest_relpath"] == SLOT


def test_the_plan_is_coherent(tmp_path: Path) -> None:
    """Two operations that are one decision. Half of this is not a smaller
    version of it — it is the library holding neither representation."""
    conn, settings, entry_id = held(tmp_path)

    plan_id = request_replacement(conn, settings, entry_id)

    assert conn.execute(
        "SELECT coherent FROM plans WHERE id=?", (plan_id,)
    ).fetchone()["coherent"] == 1


def test_it_is_one_decision_on_commit_and_is_not_called_restore(
    tmp_path: Path,
) -> None:
    from librairy.web.commit_queue import queue_rows

    conn, settings, entry_id = held(tmp_path)
    request_replacement(conn, settings, entry_id)

    rows = queue_rows(conn, settings, kind="correction")
    restores = queue_rows(conn, settings, kind="restore")

    card = next(row for row in rows if row["entry_id"] == entry_id)
    assert [row["entry_id"] for row in restores] == []
    assert card["back_label"] == "Cancel request"
    assert card["subject"] == ARRIVING
    assert card["current"].startswith("quarantine/")
    assert card["current"].endswith(ARRIVING)
    assert card["after"] == f"library/{SLOT}"
    assert "preserved" in card["reason"] or "Quarantine" in card["reason"]


def test_a_second_decision_on_the_same_file_is_refused(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    request_replacement(conn, settings, entry_id)

    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


def test_a_third_file_standing_in_the_slot_blocks_it(tmp_path: Path) -> None:
    """Renumbering it would invent a name nobody approved."""
    conn, settings, entry_id = held(tmp_path)
    write(settings, "library", {SLOT: "somebody else got here first"})
    scan_root(conn, "library", settings.library_dir, settings)

    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


# --- revalidation ------------------------------------------------------------------------


def test_a_changed_held_file_blocks_the_swap(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    held_path(conn, settings, entry_id).write_text("re-encoded", encoding="utf-8")

    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


def test_a_changed_filed_copy_blocks_the_swap(tmp_path: Path) -> None:
    """The comparison somebody is answering is not the comparison in front of
    them any more."""
    conn, settings, entry_id = held(tmp_path)
    (settings.library_dir / FILED).write_text("re-ripped", encoding="utf-8")

    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


def test_a_missing_held_file_blocks_the_swap(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    held_path(conn, settings, entry_id).unlink()

    with pytest.raises(QuarantineError):
        request_replacement(conn, settings, entry_id)


# --- what happens when it runs ------------------------------------------------------------


def test_the_representations_swap_and_neither_is_lost(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    plan_id = request_replacement(conn, settings, entry_id)

    execute_plan(conn, plan_id, settings)

    assert library_text(settings, SLOT) == "the other representation"
    assert not (settings.library_dir / FILED).exists()
    assert any("01 - Song.flac" in path for path in tree(settings.quarantine_dir))


def test_nothing_is_overwritten_when_both_are_the_same_extension(
    tmp_path: Path,
) -> None:
    """The in-place case. The order of the two operations is the only thing
    keeping the filed copy's bytes, which is why the plan is coherent."""
    conn, settings, entry_id = held(tmp_path, arriving="01 - Song.flac")
    found = replacement_for(conn, entry_id)
    assert found is not None and found.same_path

    plan_id = request_replacement(conn, settings, entry_id)
    execute_plan(conn, plan_id, settings)

    assert library_text(settings, FILED) == "the other representation"
    assert any(
        (settings.quarantine_dir / path).read_text(encoding="utf-8")
        == "the filed lossless one"
        for path in tree(settings.quarantine_dir)
    )


def test_the_entry_is_settled_and_no_longer_held(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    plan_id = request_replacement(conn, settings, entry_id)

    execute_plan(conn, plan_id, settings)

    entry = conn.execute(
        "SELECT restored_at FROM quarantine_entries WHERE id=?", (entry_id,)
    ).fetchone()
    assert entry["restored_at"] is not None


def test_the_displaced_copy_says_it_was_replaced_and_by_what(tmp_path: Path) -> None:
    """Not "you said you did not want it". Somebody preferred the other one,
    and the row has to name it or nobody can judge whether to swap back."""
    from librairy.web.quarantine import quarantine_data

    conn, settings, entry_id = held(tmp_path)
    plan_id = request_replacement(conn, settings, entry_id)
    execute_plan(conn, plan_id, settings)

    rows = quarantine_data(conn, settings)["entries"]

    row = next(row for row in rows if row["display_name"] == "01 - Song.flac")
    assert row["reason_tag"] == "replaced"
    assert row["duplicate_of"].endswith(ARRIVING)


def test_the_swap_answers_the_comparison_rather_than_re_asking_it(
    tmp_path: Path,
) -> None:
    """The decision has been made for these exact fingerprints. Asking `FLAC or
    MP3?` on the next audit would be the software forgetting what it watched
    somebody choose."""
    conn, settings, entry_id = held(tmp_path)
    plan_id = request_replacement(conn, settings, entry_id)

    execute_plan(conn, plan_id, settings)

    flags = conn.execute(
        "SELECT status, dismissed_fingerprints FROM similar_media_flags"
    ).fetchall()
    assert [flag["status"] for flag in flags] == ["dismissed"]
    assert flags[0]["dismissed_fingerprints"]


def test_undo_puts_both_files_back_exactly(tmp_path: Path) -> None:
    from librairy.history import undo_plan

    conn, settings, entry_id = held(tmp_path)
    plan_id = request_replacement(conn, settings, entry_id)
    execute_plan(conn, plan_id, settings)

    undo_plan(conn, plan_id, settings)

    assert library_text(settings, FILED) == "the filed lossless one"
    assert not (settings.library_dir / SLOT).exists()
    assert held_path(conn, settings, entry_id).is_file()


# --- flipping back -------------------------------------------------------------------------


def test_the_displaced_copy_can_be_swapped_back(tmp_path: Path) -> None:
    """Not an undo. A second decision, recorded like the first, and reversible
    in its turn."""
    conn, settings, entry_id = held(tmp_path)
    execute_plan(conn, request_replacement(conn, settings, entry_id), settings)
    displaced = conn.execute(
        "SELECT * FROM quarantine_entries WHERE id != ? ORDER BY id DESC LIMIT 1",
        (entry_id,),
    ).fetchone()

    back = replacement_for(conn, int(displaced["id"]))

    assert back is not None
    assert back.active_relpath == SLOT
    assert back.dest_relpath == FILED


def test_flipping_back_restores_the_original_representation(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    first = request_replacement(conn, settings, entry_id)
    execute_plan(conn, first, settings)
    displaced = conn.execute(
        "SELECT id FROM quarantine_entries WHERE id != ? ORDER BY id DESC LIMIT 1",
        (entry_id,),
    ).fetchone()

    second = request_replacement(conn, settings, int(displaced["id"]))
    execute_plan(conn, second, settings)

    assert library_text(settings, FILED) == "the filed lossless one"
    assert not (settings.library_dir / SLOT).exists()


def test_each_swap_is_its_own_entry_in_history(tmp_path: Path) -> None:
    conn, settings, entry_id = held(tmp_path)
    first = request_replacement(conn, settings, entry_id)
    execute_plan(conn, first, settings)
    displaced = conn.execute(
        "SELECT id FROM quarantine_entries WHERE id != ? ORDER BY id DESC LIMIT 1",
        (entry_id,),
    ).fetchone()
    second = request_replacement(conn, settings, int(displaced["id"]))
    execute_plan(conn, second, settings)

    plans = conn.execute(
        "SELECT DISTINCT plan_id FROM history WHERE plan_id IN (?, ?)", (first, second)
    ).fetchall()
    assert len(plans) == 2


# --- what restore still means ---------------------------------------------------------------


def test_restore_still_brings_it_back_alongside(tmp_path: Path) -> None:
    """The other answer, unchanged. `Restore` leaves both representations
    active; `Use this instead` leaves exactly one."""
    from librairy.quarantine_requests import request_restore

    conn, settings, entry_id = held(tmp_path)

    plan_id = request_restore(conn, settings, entry_id)
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / FILED).is_file()
    assert (settings.inbox_dir / ARRIVING).is_file()


def test_an_arrival_that_won_can_be_swapped_back_from_quarantine(
    tmp_path: Path,
) -> None:
    """The other way into this: the arrival was preferred, and the copy it
    displaced is now the held representation with a decision of its own."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, "library", {FILED: "the filed one"})
    write(settings, "inbox", {"Song.flac": "the arriving one"})
    scan_root(conn, "library", settings.library_dir, settings)
    scan_root(conn, "inbox", settings.inbox_dir, settings)
    proposal(conn, item_id(conn, "inbox", "Song.flac"), "Song.flac")
    pair(conn, item_id(conn, "inbox", "Song.flac"), item_id(conn, "library", FILED))
    plan_id = resolve(conn, settings, item_id(conn, "inbox", "Song.flac"), USE_ARRIVAL)
    execute_plan(conn, plan_id, settings)

    entry = conn.execute(
        "SELECT * FROM quarantine_entries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    found = replacement_for(conn, int(entry["id"]))

    assert found is not None
    assert found.active_relpath == FILED
