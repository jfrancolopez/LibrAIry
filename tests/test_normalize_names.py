"""Bringing one already-filed album up to the current naming, on request.

The policy applies to new files, deliberately, and an audit that reported the
whole library for not matching a convention invented after it was filed would
be house style wearing a defect's clothes. This is the other half: somebody
asks, for one folder, and sees exactly what would change before anything moves.

Most of what is tested here is what it refuses to do. It will not rename a file
whose title it does not have from somewhere that recorded one; it will not land
on top of another file or renumber around it; it will not touch a folder name,
a tag, a category or a music video; and it will not produce a plan when there
is nothing to do, because a plan with no work in it still writes History saying
a decision was carried out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.normalize_names import (
    COLLIDES,
    CURRENT,
    RENAME,
    UNKNOWN,
    NormalizeError,
    plan_normalization,
    preview,
)
from librairy.scanner import scan_root

ALBUM = "Music/Rock/Queen/A Night at the Opera"

OLD_STYLE = {
    "01-Death-on-Two-Legs.flac": "one",
    "02-Lazing-on-a-Sunday-Afternoon.flac": "two",
    "03-Youre-My-Best-Friend.flac": "three",
    "cover.jpg": "a sleeve",
}

TITLES = {
    "01-Death-on-Two-Legs.flac": {"title": "Death on Two Legs", "track": "1"},
    "02-Lazing-on-a-Sunday-Afternoon.flac": {
        "title": "Lazing on a Sunday Afternoon", "track": "2"
    },
    "03-Youre-My-Best-Friend.flac": {"title": "You're My Best Friend", "track": "3"},
}


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


def build(tmp_path: Path, files: dict[str, str] | None = None, folder: str = ALBUM):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for name, body in (files if files is not None else OLD_STYLE).items():
        path = settings.library_dir / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def tags_from(mapping: dict[str, dict[str, str]]):
    """The tag reader seam. The real one is one ffprobe call per file."""

    def read(_settings: Settings, relpath: str) -> dict[str, str]:
        return mapping.get(Path(relpath).name, {})

    return read


def no_tags(_settings: Settings, _relpath: str) -> dict[str, str]:
    return {}


def named(preview_result, name: str):
    return next(m for m in preview_result.members if m.name == name)


def tree(root: Path, folder: str) -> list[str]:
    return sorted(path.name for path in (root / folder).iterdir())


# --- the preview -------------------------------------------------------------------


def test_the_preview_lists_the_exact_names_before_and_after(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    found = preview(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    assert [(m.name, m.proposed) for m in found.renaming] == [
        ("01-Death-on-Two-Legs.flac", "01 - Death on Two Legs.flac"),
        ("02-Lazing-on-a-Sunday-Afternoon.flac",
         "02 - Lazing on a Sunday Afternoon.flac"),
        ("03-Youre-My-Best-Friend.flac", "03 - You're My Best Friend.flac"),
    ]


def test_the_preview_moves_nothing(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    before = tree(settings.library_dir, ALBUM)

    preview(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    assert tree(settings.library_dir, ALBUM) == before
    assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == 0


def test_a_file_already_named_the_current_way_is_not_renamed(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {"01 - Death on Two Legs.flac": "one"},
    )

    found = preview(
        conn,
        settings,
        ALBUM,
        read_tags=tags_from({"01 - Death on Two Legs.flac": TITLES[
            "01-Death-on-Two-Legs.flac"
        ]}),
    )

    assert named(found, "01 - Death on Two Legs.flac").state == CURRENT
    assert found.renaming == ()


def test_a_cover_is_not_a_track(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    found = preview(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    assert "cover.jpg" not in [member.name for member in found.members]


# --- what counts as knowing the title -----------------------------------------------


def test_without_a_recorded_title_the_file_is_left_alone_with_a_reason(
    tmp_path: Path,
) -> None:
    """House style turned spaces into dashes and dropped apostrophes.
    `Jay-Z-Song.flac` could have been two different titles and the name does
    not say which — guessing produces a worse filename than the one there."""
    conn, settings = build(tmp_path)

    found = preview(conn, settings, ALBUM, read_tags=no_tags)

    assert found.renaming == ()
    left = named(found, "01-Death-on-Two-Legs.flac")
    assert left.state == UNKNOWN
    assert "no title" in left.reason


def test_a_catalog_identity_somebody_asked_for_counts_as_a_title(
    tmp_path: Path,
) -> None:
    from librairy.track_identity import Identity, remember

    conn, settings = build(tmp_path)
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?",
        (f"{ALBUM}/01-Death-on-Two-Legs.flac",),
    ).fetchone()
    remember(
        conn,
        Identity(
            item_id=int(item["id"]),
            provider="test",
            recording_id="rec-1",
            artist="Queen",
            title="Death on Two Legs",
            releases=(),
            fingerprint=str(item["fingerprint"]),
        ),
    )

    found = preview(conn, settings, ALBUM, read_tags=no_tags)

    renamed = named(found, "01-Death-on-Two-Legs.flac")
    assert renamed.state == RENAME
    assert renamed.proposed == "01 - Death on Two Legs.flac"
    assert "catalog" in renamed.source


def test_the_track_number_may_come_from_a_name_librairy_wrote(tmp_path: Path) -> None:
    """A number LibrAIry wrote is a number it can read. The title still has to
    come from somewhere that recorded one."""
    conn, settings = build(tmp_path, {"07 - Sweet Lady.flac": "seven"})

    found = preview(
        conn,
        settings,
        ALBUM,
        read_tags=tags_from({"07 - Sweet Lady.flac": {"title": "Sweet Lady"}}),
    )

    assert named(found, "07 - Sweet Lady.flac").state == CURRENT


# --- collisions ---------------------------------------------------------------------


def test_a_rename_onto_an_existing_file_is_refused(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {
            "01-Death-on-Two-Legs.flac": "one",
            "01 - Death on Two Legs.flac": "already here",
        },
    )

    found = preview(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    blocked = named(found, "01-Death-on-Two-Legs.flac")
    assert blocked.state == COLLIDES
    assert "already exists" in blocked.reason
    assert found.renaming == ()


def test_two_files_wanting_one_name_are_both_refused(tmp_path: Path) -> None:
    """No winner is picked, because picking one is a decision nobody asked
    this tool to make."""
    conn, settings = build(
        tmp_path, {"01-Song.flac": "one", "01-Song-again.flac": "two"}
    )

    found = preview(
        conn,
        settings,
        ALBUM,
        read_tags=tags_from(
            {
                "01-Song.flac": {"title": "Song", "track": "1"},
                "01-Song-again.flac": {"title": "Song", "track": "1"},
            }
        ),
    )

    assert [member.state for member in found.members] == [COLLIDES, COLLIDES]


def test_nothing_is_auto_numbered(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {
            "01-Death-on-Two-Legs.flac": "one",
            "01 - Death on Two Legs.flac": "already here",
        },
    )

    found = preview(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    assert not any("(2)" in member.proposed for member in found.members)


# --- the plan ------------------------------------------------------------------------


def test_one_album_becomes_one_decision_of_plain_moves(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)

    plan_id = plan_normalization(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    ops = conn.execute(
        "SELECT op_type, src_relpath, dest_relpath FROM plan_ops WHERE plan_id=?"
        " ORDER BY seq",
        (plan_id,),
    ).fetchall()
    assert {op["op_type"] for op in ops} == {"move"}
    assert len(ops) == 3
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM plans WHERE id=?", (plan_id,)
    ).fetchone()["n"] == 1


def test_only_the_files_that_change_become_operations(tmp_path: Path) -> None:
    conn, settings = build(
        tmp_path,
        {"01-Death-on-Two-Legs.flac": "one", "02 - Sheer Heart Attack.flac": "two"},
    )

    plan_id = plan_normalization(
        conn,
        settings,
        ALBUM,
        read_tags=tags_from(
            {
                **TITLES,
                "02 - Sheer Heart Attack.flac": {
                    "title": "Sheer Heart Attack", "track": "2"
                },
            }
        ),
    )

    ops = conn.execute(
        "SELECT src_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    assert [Path(op["src_relpath"]).name for op in ops] == [
        "01-Death-on-Two-Legs.flac"
    ]


def test_an_album_already_in_the_current_style_produces_no_plan(tmp_path: Path) -> None:
    """Not an empty plan. A plan with no work in it would still appear on
    Commit and still write History saying a decision was carried out."""
    conn, settings = build(tmp_path, {"01 - Death on Two Legs.flac": "one"})

    with pytest.raises(NormalizeError):
        plan_normalization(
            conn,
            settings,
            ALBUM,
            read_tags=tags_from(
                {"01 - Death on Two Legs.flac": TITLES["01-Death-on-Two-Legs.flac"]}
            ),
        )
    assert conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"] == 0


def test_the_commit_card_is_about_the_album_and_not_its_first_file(
    tmp_path: Path,
) -> None:
    """Titling it `01-Death-on-Two-Legs.flac` would describe one third of what
    pressing Commit does."""
    from librairy.web.commit_queue import queue_rows

    conn, settings = build(tmp_path)
    plan_normalization(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    card = next(
        row for row in queue_rows(conn, settings, kind="correction")
        if row["subject"] == "Tidy filenames"
    )

    assert card["current"] == f"library/{ALBUM}"
    assert card["after"] == f"3 filenames under library/{ALBUM}"
    assert "Only the names change" in card["reason"]


# --- what it does when it runs --------------------------------------------------------


def test_committing_changes_names_and_nothing_else(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    plan_id = plan_normalization(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    execute_plan(conn, plan_id, settings)

    assert tree(settings.library_dir, ALBUM) == [
        "01 - Death on Two Legs.flac",
        "02 - Lazing on a Sunday Afternoon.flac",
        "03 - You're My Best Friend.flac",
        "cover.jpg",
    ]
    assert (settings.library_dir / ALBUM).is_dir()
    assert (settings.library_dir / "Music/Rock/Queen").is_dir()


def test_the_bytes_are_the_same_bytes(tmp_path: Path) -> None:
    conn, settings = build(tmp_path)
    plan_id = plan_normalization(conn, settings, ALBUM, read_tags=tags_from(TITLES))

    execute_plan(conn, plan_id, settings)

    assert (
        settings.library_dir / ALBUM / "01 - Death on Two Legs.flac"
    ).read_text(encoding="utf-8") == "one"


def test_undo_puts_the_exact_names_back(tmp_path: Path) -> None:
    from librairy.history import undo_plan

    conn, settings = build(tmp_path)
    plan_id = plan_normalization(conn, settings, ALBUM, read_tags=tags_from(TITLES))
    execute_plan(conn, plan_id, settings)

    undo_plan(conn, plan_id, settings)

    assert tree(settings.library_dir, ALBUM) == sorted(OLD_STYLE)


# --- scope --------------------------------------------------------------------------


def test_music_videos_are_not_touched(tmp_path: Path) -> None:
    """They have their own formatter and their own parser reads it back."""
    conn, settings = build(
        tmp_path,
        {"Daft Punk - Around the World (Official Video).mkv": "a video"},
        folder="Music Videos/House/Daft Punk",
    )

    with pytest.raises(NormalizeError):
        preview(conn, settings, "Music Videos/House/Daft Punk", read_tags=no_tags)


@pytest.mark.parametrize(
    "folder", ["Movies/The Matrix (1999)", "Shows/The Wire/Season 01", "Photos/2024"]
)
def test_no_other_category_is_offered_this(tmp_path: Path, folder: str) -> None:
    conn, settings = build(tmp_path, {"a file.mkv": "x"}, folder=folder)

    with pytest.raises(NormalizeError):
        preview(conn, settings, folder, read_tags=no_tags)


def test_a_folder_bigger_than_a_preview_is_refused(tmp_path: Path) -> None:
    from librairy.normalize_names import MAX_FILES

    conn, settings = build(
        tmp_path, {f"{n:03d}-track.flac": "x" for n in range(MAX_FILES + 1)}
    )

    with pytest.raises(NormalizeError):
        preview(conn, settings, ALBUM, read_tags=no_tags)


def test_the_audit_still_reports_none_of_this(tmp_path: Path) -> None:
    """The tool exists precisely so that the audit does not have to nag."""
    from librairy.audit import audit_library

    conn, settings = build(tmp_path)

    audit_library(conn, settings, read_tags=False, use_catalogs=False)

    findings = conn.execute(
        "SELECT kind FROM audit_findings WHERE relpath LIKE 'Music/%'"
        " AND kind LIKE 'naming%'"
    ).fetchall()
    assert findings == []
