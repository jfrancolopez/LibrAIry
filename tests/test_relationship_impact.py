"""What a decision does to the pairs LibrAIry already knows about.

The point of every test here is a sentence somebody reads at the last moment
before bytes move — and, just as much, the sentences that must *not* appear. A
program that warned about every relationship near every operation would be
noise within a week, and noise is how a real warning stops being read.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.planner import OperationSpec, approve_plan, create_plan
from librairy.relationship_impact import (
    BOTH_REMAIN,
    DRIFT_CHANGED,
    DRIFT_GONE,
    DRIFT_NEW,
    MOVES_TOGETHER,
    SPLIT,
    STALE,
    Move,
    assess,
    drift,
    for_plan,
    snapshot,
    snapshot_rows,
)
from librairy.relationships import (
    ARTWORK,
    LIVE_PHOTO,
    RAW_RENDER,
    SUBTITLE,
    record,
)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
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


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return connect(settings_for(tmp_path))


def add_item(
    conn: sqlite3.Connection,
    relpath: str,
    *,
    root: str = "library",
    fingerprint: str | None = None,
    missing: bool = False,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,
                          first_seen_at, last_seen_at, missing_since)
        VALUES (?, ?, 10, 1, ?, 'proposed', 'now', 'now', ?)
        """,
        (root, relpath, fingerprint or f"{root}:{relpath}", "now" if missing else None),
    )
    return int(cursor.lastrowid)


def quarantined(item_id: int, name: str) -> Move:
    return Move(item_id=item_id, dest_root="quarantine", dest_relpath=name)


def filed(item_id: int, relpath: str) -> Move:
    return Move(item_id=item_id, dest_root="library", dest_relpath=relpath)


def live_photo(conn: sqlite3.Connection, folder: str = "Photos/2024") -> tuple[int, int]:
    still = add_item(conn, f"{folder}/IMG_1234.HEIC")
    motion = add_item(conn, f"{folder}/IMG_1234.MOV")
    record(
        conn,
        companion_item_id=motion,
        subject_item_id=still,
        kind=LIVE_PHOTO,
        provenance="same Live Photo identifier ABCD",
    )
    return still, motion


def raw_pair(conn: sqlite3.Connection, folder: str = "Photos/2024") -> tuple[int, int]:
    raw = add_item(conn, f"{folder}/IMG_5200.CR3")
    render = add_item(conn, f"{folder}/IMG_5200.JPG")
    record(
        conn,
        companion_item_id=render,
        subject_item_id=raw,
        kind=RAW_RENDER,
        provenance="same camera and the same moment",
    )
    return raw, render


def film(conn: sqlite3.Connection, folder: str = "Movies/Arrival (2016)") -> tuple[int, int]:
    movie = add_item(conn, f"{folder}/Arrival (2016).mkv")
    subtitle = add_item(conn, f"{folder}/Arrival (2016).en.srt")
    record(
        conn,
        companion_item_id=subtitle,
        subject_item_id=movie,
        kind=SUBTITLE,
        provenance="names the same file",
    )
    return movie, subtitle


# --------------------------------------------------------------------------
# 1-5: the model itself
# --------------------------------------------------------------------------


def test_a_decision_that_touches_no_relationship_has_no_impact(
    conn: sqlite3.Connection,
) -> None:
    lonely = add_item(conn, "Photos/2024/DSC_0001.JPG")
    live_photo(conn)

    impact = assess(conn, [quarantined(lonely, "DSC_0001.JPG")])

    assert impact.touched == []
    assert impact.summary() == []


def test_one_member_moving_splits_the_pair(conn: sqlite3.Connection) -> None:
    still, motion = live_photo(conn)

    impact = assess(conn, [quarantined(motion, "IMG_1234.MOV")])

    assert [item.state for item in impact.touched] == [SPLIT]
    touched = impact.touched[0]
    assert touched.headline == "This will separate a Live Photo."
    assert touched.detail == (
        "IMG_1234.MOV goes to Quarantine; IMG_1234.HEIC stays in the library/Photos/2024."
    )
    assert touched.outside is not None
    assert touched.outside.item_id == still


def test_both_members_moving_stay_together(conn: sqlite3.Connection) -> None:
    still, motion = live_photo(conn)

    impact = assess(
        conn,
        [quarantined(still, "IMG_1234.HEIC"), quarantined(motion, "IMG_1234.MOV")],
    )

    assert [item.state for item in impact.touched] == [MOVES_TOGETHER]
    assert impact.touched[0].outside is None
    assert impact.summary() == ["1 Live Photo stays together"]


def test_a_missing_related_member_is_not_current_impact(
    conn: sqlite3.Connection,
) -> None:
    """The row survives the file going away. The warning must not.

    "This will separate a Live Photo — the still stays in Photos" about a still
    that is not in Photos is a sentence about a library that does not exist.
    """
    still, motion = live_photo(conn)
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (still,))

    impact = assess(conn, [quarantined(motion, "IMG_1234.MOV")])

    assert [item.state for item in impact.touched] == [STALE]
    assert impact.relevant == []
    assert impact.summary() == []


def test_knowing_about_a_pair_adds_no_operation(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The whole safety property, stated once.

    A relationship may change what a page says and which buttons it draws. It
    may never put an operation into a plan that nobody pressed a button for.
    """
    settings = settings_for(tmp_path)
    folder = settings.library_dir / "Photos" / "2024"
    folder.mkdir(parents=True)
    for name in ("IMG_1234.HEIC", "IMG_1234.MOV"):
        (folder / name).write_bytes(b"x")
    still, motion = live_photo(conn)
    assert still and motion
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                "quarantine",
                "Photos/2024/IMG_1234.MOV",
                "quarantine",
                "IMG_1234.MOV",
                src_root="library",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    ops = conn.execute(
        "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()

    assert [row["src_relpath"] for row in ops] == ["Photos/2024/IMG_1234.MOV"]
    assert for_plan(conn, plan_id).any_split is True


# --------------------------------------------------------------------------
# 6-9: what each kind means
# --------------------------------------------------------------------------


def test_a_split_live_photo_names_both_halves_and_the_outcome(
    conn: sqlite3.Connection,
) -> None:
    """A6 in reverse: the acknowledgement has to be about something specific.

    "Are you sure?" is not an acknowledgement. What is being separated, which
    half is going where, and what stays — that is what somebody can agree to.
    """
    _, motion = live_photo(conn)

    touched = assess(conn, [quarantined(motion, "IMG_1234.MOV")]).splits[0]

    assert "IMG_1234.MOV" in touched.detail
    assert "IMG_1234.HEIC" in touched.detail
    assert "Quarantine" in touched.detail


def test_a_raw_and_its_render_may_be_split_and_are_only_explained(
    conn: sqlite3.Connection,
) -> None:
    """Splitting these is normal. Being told is the whole feature."""
    raw, render = raw_pair(conn)

    impact = assess(conn, [quarantined(render, "IMG_5200.JPG")])

    assert impact.splits[0].headline == "This will separate a RAW + JPEG pair."
    assert impact.summary() == ["1 RAW/JPEG pair will be split"]
    assert impact.splits[0].outside is not None
    assert impact.splits[0].outside.item_id == raw


def test_a_subtitle_left_behind_is_reported_as_an_orphan(
    conn: sqlite3.Connection,
) -> None:
    movie, _ = film(conn)

    impact = assess(conn, [quarantined(movie, "Arrival (2016).mkv")])

    assert impact.splits[0].headline == "This will separate a subtitle from its video."


def test_a_sidecar_left_in_the_old_folder_is_split_without_leaving_the_library(
    conn: sqlite3.Connection,
) -> None:
    """A subtitle has to sit *beside* its video, which is why folders matter.

    Both files are still in the library and the pair is still recorded — and
    the `.srt` is now in a directory no player will look in.
    """
    movie, _ = film(conn, folder="Movies/Arrival")

    impact = assess(
        conn, [filed(movie, "Movies/Arrival (2016)/Arrival (2016).mkv")]
    )

    assert [item.state for item in impact.touched] == [SPLIT]


def test_a_photo_pair_survives_being_reorganised_inside_the_library(
    conn: sqlite3.Connection,
) -> None:
    """The opposite rule, for the opposite reason.

    A Live Photo is a Live Photo because both halves carry one identifier, not
    because they share a directory. Moving one to a better folder separates
    nothing, and saying it did would train somebody to click past the warning.
    """
    still, _ = live_photo(conn)

    impact = assess(conn, [filed(still, "Photos/2024/August/IMG_1234.HEIC")])

    assert [item.state for item in impact.touched] == [BOTH_REMAIN]
    assert impact.relevant == []


def test_setting_one_track_aside_says_nothing_about_the_cover(
    conn: sqlite3.Connection,
) -> None:
    """The nonsense sentence this rule exists to prevent.

    "Removing one MP3 would separate cover.jpg from the MP3" is true in the
    same useless way that removing one book separates it from the shelf.
    """
    folder = "Music/Talking Heads/Remain in Light"
    cover = add_item(conn, f"{folder}/cover.jpg")
    tracks = [add_item(conn, f"{folder}/{n:02d} - Track.flac") for n in range(1, 6)]
    for track in tracks:
        record(
            conn,
            companion_item_id=cover,
            subject_item_id=track,
            kind=ARTWORK,
            provenance="belongs to this folder's release",
        )

    impact = assess(conn, [quarantined(tracks[0], "01 - Track.flac")])

    assert impact.splits == []
    assert impact.summary() == []


def test_a_release_leaving_entirely_does_strand_its_artwork(
    conn: sqlite3.Connection,
) -> None:
    """The one artwork fact worth reporting, and it is reported once."""
    folder = "Music/Talking Heads/Remain in Light"
    cover = add_item(conn, f"{folder}/cover.jpg")
    tracks = [add_item(conn, f"{folder}/{n:02d} - Track.flac") for n in range(1, 6)]
    for track in tracks:
        record(
            conn,
            companion_item_id=cover,
            subject_item_id=track,
            kind=ARTWORK,
            provenance="belongs to this folder's release",
        )

    impact = assess(
        conn, [quarantined(track, f"{track}.flac") for track in tracks]
    )

    assert impact.summary() == ["1 folder's artwork will be split"]
    assert impact.splits[0].headline == (
        "This will leave a folder's artwork with nothing it belongs to."
    )


# --------------------------------------------------------------------------
# 10-13: the snapshot, and drift
# --------------------------------------------------------------------------


def approved_split(
    conn: sqlite3.Connection, settings: Settings
) -> tuple[str, int, int]:
    folder = settings.library_dir / "Photos" / "2024"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("IMG_1234.HEIC", "IMG_1234.MOV"):
        (folder / name).write_bytes(name.encode())
    still, motion = live_photo(conn)
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                "quarantine",
                "Photos/2024/IMG_1234.MOV",
                "quarantine",
                "IMG_1234.MOV",
                src_root="library",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)
    return plan_id, still, motion


def test_approval_freezes_only_the_relationships_this_plan_touches(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    raw_pair(conn, folder="Photos/Elsewhere")
    plan_id, still, _ = approved_split(conn, settings)

    rows = snapshot_rows(conn, plan_id)

    assert len(rows) == 1
    assert rows[0]["kind"] == LIVE_PHOTO
    assert rows[0]["state"] == SPLIT
    #  The half that is *not* an operation, which is the only one nothing else
    #  in the commit path would ever look at.
    assert rows[0]["outside_item_id"] == still
    assert rows[0]["outside_fingerprint"] == "library:Photos/2024/IMG_1234.HEIC"


def test_a_new_relationship_this_decision_would_split_makes_it_outdated(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    plan_id, _, motion = approved_split(conn, settings)
    assert drift(conn, plan_id) == ""
    other = add_item(conn, "Photos/2024/IMG_1234.CR3")
    record(
        conn,
        companion_item_id=motion,
        subject_item_id=other,
        kind=RAW_RENDER,
        provenance="same camera and the same moment",
    )

    assert drift(conn, plan_id) == DRIFT_NEW


def test_a_related_member_changing_underneath_makes_it_outdated(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    plan_id, still, _ = approved_split(conn, settings)

    conn.execute("UPDATE items SET fingerprint='different' WHERE id=?", (still,))

    assert drift(conn, plan_id) == DRIFT_CHANGED


def test_a_related_member_disappearing_makes_it_outdated(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    plan_id, still, _ = approved_split(conn, settings)

    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (still,))

    assert drift(conn, plan_id) == DRIFT_GONE


def test_a_relationship_being_withdrawn_is_not_drift(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Correcting the catalogue must not cancel a decision about the disk.

    If better metadata says the two photographs were never a pair, every
    operation in the plan is still exactly the operation that was approved, on
    exactly the file it named. The warning turned out to be unnecessary; the
    decision did not turn into a different one.
    """
    settings = settings_for(tmp_path)
    plan_id, _, _ = approved_split(conn, settings)

    conn.execute("DELETE FROM item_relationships")

    assert drift(conn, plan_id) == ""


def test_a_plan_approved_before_relationships_existed_keeps_its_old_semantics(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The production database is full of these. None of them may start failing.

    An approved plan from before this feature carries no snapshot, and an empty
    snapshot must never be read as "nothing is related" — it means "nobody
    looked". `relationships_checked` is what tells those apart.
    """
    settings = settings_for(tmp_path)
    plan_id, still, _ = approved_split(conn, settings)
    conn.execute("DELETE FROM plan_relationships WHERE plan_id=?", (plan_id,))
    conn.execute(
        "UPDATE plans SET relationships_checked=0 WHERE id=?", (plan_id,)
    )

    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (still,))

    assert drift(conn, plan_id) == ""


def test_a_plan_that_touches_nothing_is_still_marked_as_checked(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    (settings.library_dir / "Photos").mkdir(parents=True, exist_ok=True)
    (settings.library_dir / "Photos" / "DSC_0001.JPG").write_bytes(b"x")
    lonely = add_item(conn, "Photos/DSC_0001.JPG")
    assert lonely
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                "quarantine",
                "Photos/DSC_0001.JPG",
                "quarantine",
                "DSC_0001.JPG",
                src_root="library",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    assert snapshot_rows(conn, plan_id) == []
    assert (
        conn.execute(
            "SELECT relationships_checked FROM plans WHERE id=?", (plan_id,)
        ).fetchone()["relationships_checked"]
        == 1
    )


def test_a_stale_pair_is_never_frozen_into_the_snapshot(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Freezing a pair whose other half is already gone manufactures drift."""
    settings = settings_for(tmp_path)
    folder = settings.library_dir / "Photos" / "2024"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "IMG_1234.MOV").write_bytes(b"IMG_1234.MOV")
    still, _ = live_photo(conn)
    conn.execute("UPDATE items SET missing_since='now' WHERE id=?", (still,))
    plan_id = create_plan(
        conn,
        [
            OperationSpec(
                "quarantine",
                "Photos/2024/IMG_1234.MOV",
                "quarantine",
                "IMG_1234.MOV",
                src_root="library",
            )
        ],
        settings,
    )
    approve_plan(conn, plan_id, settings)

    assert snapshot_rows(conn, plan_id) == []
    assert drift(conn, plan_id) == ""


def test_snapshot_returns_what_it_recorded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path)
    plan_id, _, _ = approved_split(conn, settings)

    again = snapshot(conn, plan_id)

    assert [item.state for item in again.touched] == [SPLIT]
    assert len(snapshot_rows(conn, plan_id)) == 1


# --------------------------------------------------------------------------
# Bounded whatever the size of the decision
# --------------------------------------------------------------------------


def test_impact_asks_the_same_questions_for_one_op_and_five_hundred(
    conn: sqlite3.Connection,
) -> None:
    """The property that lets this live on the Commit page.

    A relationship lookup per operation is the N+1 that makes a five-hundred
    file decision five hundred times more expensive to *describe* than to
    take — and the description is what the person reads before pressing the
    button.
    """
    moves: list[Move] = []
    for number in range(1, 501):
        still = add_item(conn, f"Photos/Card/IMG_{number:04d}.HEIC")
        motion = add_item(conn, f"Photos/Card/IMG_{number:04d}.MOV")
        record(
            conn,
            companion_item_id=motion,
            subject_item_id=still,
            kind=LIVE_PHOTO,
            provenance="same Live Photo identifier",
        )
        moves.append(quarantined(motion, f"IMG_{number:04d}.MOV"))

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        big = assess(conn, moves)
        many = len(statements)
        statements.clear()
        small = assess(conn, moves[:1])
        few = len(statements)
    finally:
        conn.set_trace_callback(None)

    assert len(big.splits) == 500
    assert len(small.splits) == 1
    #  Same number of queries, not merely a similar one. The artwork fan-out
    #  query is skipped when no artwork is involved, which is why this is not a
    #  fixed constant written into the assertion.
    assert many == few


def test_a_five_hundred_file_decision_summarises_rather_than_lists(
    conn: sqlite3.Connection,
) -> None:
    from librairy.relationship_impact import card

    moves: list[Move] = []
    for number in range(1, 501):
        still = add_item(conn, f"Photos/Card/IMG_{number:04d}.HEIC")
        motion = add_item(conn, f"Photos/Card/IMG_{number:04d}.MOV")
        record(
            conn,
            companion_item_id=motion,
            subject_item_id=still,
            kind=LIVE_PHOTO,
            provenance="same Live Photo identifier",
        )
        moves.append(quarantined(motion, f"IMG_{number:04d}.MOV"))

    shape = card(assess(conn, moves))

    assert shape is not None
    assert shape["summary"] == ["500 Live Photos will be split"]
    assert len(shape["splits"]) == 3
    assert shape["splits_more"] == 497
