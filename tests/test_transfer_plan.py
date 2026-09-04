"""A transfer knows what it intends to do before it does any of it.

The same shape as Commit, for the same reason: a backup that starts copying and
finds out what it did afterwards is a backup nobody can check. And this is the
one part of the program where "it seemed to work" and "it silently stopped a
fortnight ago" look identical from the outside.

`compare` is a pure function over two catalogues, which is deliberate: every
claim about what a mode does can be tested here without a filesystem, a remote
or a subprocess anywhere near it — and the claims are the part that must not
drift.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from librairy import destinations as dest
from librairy import transfer_plan as plan
from librairy.config import Settings
from librairy.db import connect
from librairy.transfer_plan import DestinationFile, LibraryFile


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
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def library(conn: sqlite3.Connection, *paths: str, size: int = 100) -> None:
    for relpath in paths:
        conn.execute(
            "INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at,"
            " last_seen_at) VALUES ('library', ?, ?, 0, 'committed', 'now', 'now')",
            (relpath, size),
        )


def scene(tmp_path: Path, mode: str = dest.BACKUP):  # noqa: ANN201
    conn = connect(settings_for(tmp_path))
    made = dest.add_destination(
        conn, name="NAS", kind="remote", target="nas:lib", modes=list(dest.MODES)
    )
    dest.set_policy(conn, category="photos", destination_id=made, mode=mode)
    policy = dest.policies(conn)[0]
    return conn, policy, dest.destination(conn, made)


# --- the four numbers ---------------------------------------------------------------


def test_a_comparison_says_copy_update_current_and_only_there() -> None:
    ours = [
        LibraryFile("Photos/a.jpg", 100),
        LibraryFile("Photos/b.jpg", 100),
        LibraryFile("Photos/c.jpg", 100),
    ]
    theirs = [
        DestinationFile("Photos/a.jpg", 100),  # current
        DestinationFile("Photos/b.jpg", 55),  # changed
        DestinationFile("Photos/old.jpg", 20),  # only there
    ]

    counts, _entries = plan.compare(ours, theirs, dest.BACKUP)

    assert counts == {
        dest.MISSING: 1,
        dest.CHANGED: 1,
        dest.CURRENT: 1,
        dest.EXTRA: 1,
    }


def test_the_fourth_number_is_never_something_to_act_on() -> None:
    """The whole reason this module exists rather than a call to `rclone sync`.

    Every mode is asked the same question about the same destination-only file
    and not one of them produces an action that removes it.
    """
    ours = [LibraryFile("Photos/a.jpg", 100)]
    theirs = [DestinationFile("Photos/gone.jpg", 100)]

    for mode in dest.MODES:
        _counts, entries = plan.compare(ours, theirs, mode)
        extra = next(entry for entry in entries if entry.difference == dest.EXTRA)
        assert extra.action in (dest.KEEP, dest.REPORT), (mode, extra.action)
        assert extra not in plan.transfers(
            plan.Plan(policy=None, destination=None, entries=tuple(entries))  # type: ignore[arg-type]
        )


def test_backup_keeps_quiet_about_extras_and_mirror_reports_them() -> None:
    """The only behavioural difference between the two modes, and it is a
    sentence rather than a deletion."""
    ours = [LibraryFile("Photos/a.jpg", 100)]
    theirs = [DestinationFile("Photos/gone.jpg", 100)]

    _counts, kept = plan.compare(ours, theirs, dest.BACKUP)
    _counts, reported = plan.compare(ours, theirs, dest.MIRROR)

    assert [entry.action for entry in kept if entry.difference == dest.EXTRA] == [dest.KEEP]
    assert [entry.action for entry in reported if entry.difference == dest.EXTRA] == [
        dest.REPORT
    ]


def test_an_identical_catalogue_is_no_work_at_all() -> None:
    ours = [LibraryFile("Photos/a.jpg", 100)]
    theirs = [DestinationFile("Photos/a.jpg", 100)]

    counts, entries = plan.compare(ours, theirs, dest.MIRROR)

    assert counts[dest.CURRENT] == 1
    assert plan.transfers(
        plan.Plan(policy=None, destination=None, counts=counts, entries=tuple(entries))  # type: ignore[arg-type]
    ) == ()


def test_a_size_that_changed_is_an_update_and_not_a_second_copy() -> None:
    ours = [LibraryFile("Photos/a.jpg", 400)]
    theirs = [DestinationFile("Photos/a.jpg", 100)]

    counts, entries = plan.compare(ours, theirs, dest.BACKUP)

    assert counts[dest.CHANGED] == 1
    assert entries[0].action == dest.UPDATE
    assert entries[0].destination_size == 100  # noqa: PLR2004


# --- unavailable is not empty -------------------------------------------------------


def test_a_destination_nobody_could_reach_is_not_an_empty_plan(tmp_path: Path) -> None:
    """"Nothing to do" and "nothing could be checked" are different, and only
    the first is good news. A plan that rendered them the same way would report
    a drive in a drawer as a healthy backup."""
    conn, policy, destination = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    found = plan.plan_for(conn, policy, destination, None)

    assert found.unavailable
    assert found.empty
    assert found.counts == {}
    assert "NAS" in found.unavailable


def test_a_reachable_destination_with_nothing_to_do_says_so(tmp_path: Path) -> None:
    conn, policy, destination = scene(tmp_path)
    library(conn, "Photos/a.jpg")

    found = plan.plan_for(conn, policy, destination, [DestinationFile("Photos/a.jpg", 100)])

    assert not found.unavailable
    assert found.empty
    assert found.current == 1


# --- what a policy covers -----------------------------------------------------------


def test_a_policy_covers_its_own_folder_and_no_other(tmp_path: Path) -> None:
    """A category is a top-level folder, taken from the taxonomy's own template
    rather than from a second list that could disagree with it."""
    conn, policy, destination = scene(tmp_path)
    library(conn, "Photos/2026/a.jpg", "Photos/2025/b.jpg")
    library(conn, "Music/x.flac", "Documents/y.pdf")

    covered = plan.library_files(conn, "photos")

    assert [found.relpath for found in covered] == [
        "Photos/2025/b.jpg",
        "Photos/2026/a.jpg",
    ]
    assert [found.relpath for found in plan.library_files(conn, "music")] == ["Music/x.flac"]
    #  And the folder comes from the taxonomy, so a category whose template
    #  changes cannot leave this backing up a folder nothing is in.
    assert [found.relpath for found in plan.library_files(conn, "music_videos")] == []


def test_a_folder_with_a_similar_name_is_not_swept_in(tmp_path: Path) -> None:
    """`Photos Archive/` is not inside `Photos/`, and a prefix match without
    the separator would have said it was."""
    conn, _policy, _destination = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Photos Archive/b.jpg", "PhotosOld/c.jpg")

    covered = plan.library_files(conn, "photos")

    assert [found.relpath for found in covered] == ["Photos/a.jpg"]


def test_a_missing_file_is_not_offered_for_copying(tmp_path: Path) -> None:
    """A row whose file has gone is a record, not a file. Handing it to a
    copier asks it to open something that is not there."""
    conn, _policy, _destination = scene(tmp_path)
    library(conn, "Photos/a.jpg", "Photos/gone.jpg")
    conn.execute("UPDATE items SET missing_since='now' WHERE relpath='Photos/gone.jpg'")

    assert [found.relpath for found in plan.library_files(conn, "photos")] == [
        "Photos/a.jpg"
    ]


# --- bounded --------------------------------------------------------------------------


def test_a_huge_difference_is_counted_in_full_and_shown_a_page_at_a_time() -> None:
    """A drive unplugged for three months holding forty thousand files the
    library no longer has produces the number forty thousand and fifty rows.

    The counts are complete because they are what somebody needs to know; the
    rows are a sample because nobody reads forty thousand of anything.
    """
    ours = [LibraryFile(f"Photos/new-{index}.jpg", 100) for index in range(4_000)]
    theirs = [DestinationFile(f"Photos/old-{index}.jpg", 100) for index in range(4_000)]

    counts, entries = plan.compare(ours, theirs, dest.MIRROR)

    assert counts[dest.MISSING] == 4_000  # noqa: PLR2004
    assert counts[dest.EXTRA] == 4_000  # noqa: PLR2004
    #  One page of each difference, and no more.
    assert len(entries) <= plan.PAGE * len(dest.DIFFERENCES)
    by_difference: dict[str, int] = {}
    for entry in entries:
        by_difference[entry.difference] = by_difference.get(entry.difference, 0) + 1
    assert max(by_difference.values()) <= plan.PAGE


def test_the_summary_names_the_destination_only_files_for_what_they_are() -> None:
    """"11 extra" invites somebody to tidy them. "11 only at the destination"
    says what is true and implies nothing."""
    ours = [LibraryFile("Photos/a.jpg", 100)]
    theirs = [DestinationFile("Photos/gone.jpg", 100)]
    counts, entries = plan.compare(ours, theirs, dest.MIRROR)

    summary = plan.Plan(
        policy=None,  # type: ignore[arg-type]
        destination=None,  # type: ignore[arg-type]
        counts=counts,
        entries=tuple(entries),
    ).summary

    assert "only at the destination" in summary
    assert "extra" not in summary
    assert "delete" not in summary.lower()


def test_a_plan_can_say_how_much_it_would_send() -> None:
    ours = [LibraryFile("Photos/a.jpg", 400), LibraryFile("Photos/b.jpg", 600)]
    counts, entries = plan.compare(ours, [], dest.BACKUP)

    found = plan.Plan(
        policy=None,  # type: ignore[arg-type]
        destination=None,  # type: ignore[arg-type]
        counts=counts,
        entries=tuple(entries),
    )

    assert found.transfers == 2  # noqa: PLR2004
    assert found.bytes_to_send == 1000  # noqa: PLR2004


# --- nothing here reads the destination as truth --------------------------------------


def test_a_newer_file_at_the_destination_is_still_only_a_difference() -> None:
    """The inward arrow that does not exist.

    A destination copy that differs is not evidence about the Library. It is
    the destination being out of date, whichever way round the bytes look —
    and the plan says "update it", never "take it".
    """
    ours = [LibraryFile("Photos/a.jpg", 100)]
    theirs = [DestinationFile("Photos/a.jpg", 999_999)]

    _counts, entries = plan.compare(ours, theirs, dest.MIRROR)

    assert entries[0].action == dest.UPDATE
    assert entries[0].relpath == "Photos/a.jpg"


def test_the_planner_never_looks_at_the_library_on_disk(tmp_path: Path) -> None:
    """The catalogue is the `items` table. Walking a million files to plan a
    backup is the shape that makes a backup something people switch off."""
    import inspect

    source = inspect.getsource(plan)

    for walker in ("rglob", "iterdir", "os.walk", "os.scandir"):
        assert walker not in source, f"the planner walks the library with {walker}"
    assert tmp_path.exists()
