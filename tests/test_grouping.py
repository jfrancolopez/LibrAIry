from __future__ import annotations

from pathlib import Path

import pytest

from librairy.classify.grouping import GroupInput, group_proposals, project_folder_input
from librairy.config import Settings
from librairy.db import connect


def conn_for(tmp_path: Path):
    return connect(Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None))


def test_album_tracks_share_one_group_and_folder(tmp_path: Path) -> None:
    conn = conn_for(tmp_path)
    proposals = [
        GroupInput(
            1,
            "01.mp3",
            "music",
            "01 - A.mp3",
            "Music/Queen/News/01 - A.mp3",
            {"artist": "Queen", "album": "News"},
        ),
        GroupInput(
            2,
            "02.mp3",
            "music",
            "02 - B.mp3",
            "Music/Queen/News/02 - B.mp3",
            {"artist": "Queen", "album": "News"},
        ),
    ]

    grouped = group_proposals(conn, proposals)

    assert grouped[0].group_id == grouped[1].group_id
    assert grouped[0].dest_base == "Music/Queen/News"


def test_season_episodes_share_season_folder(tmp_path: Path) -> None:
    conn = conn_for(tmp_path)
    grouped = group_proposals(
        conn,
        [
            GroupInput(
                1,
                "e1.mkv",
                "shows",
                "S02E01.mkv",
                "Shows/Show/Season 02/S02E01.mkv",
                {"show": "Show", "season": 2},
            ),
            GroupInput(
                2,
                "e2.mkv",
                "shows",
                "S02E02.mkv",
                "Shows/Show/Season 02/S02E02.mkv",
                {"show": "Show", "season": 2},
            ),
        ],
    )

    assert grouped[0].group_id == grouped[1].group_id
    assert grouped[0].dest_base == "Shows/Show/Season 02"


def test_photo_event_uses_hashtag_label(tmp_path: Path) -> None:
    conn = conn_for(tmp_path)
    grouped = group_proposals(
        conn,
        [
            GroupInput(
                1,
                "Trip #italy/photo.jpg",
                "photos",
                "photo.jpg",
                "Photos/2026/italy/photo.jpg",
                {"event": "italy"},
            )
        ],
    )

    row = conn.execute(
        "SELECT kind, label FROM groups WHERE id=?", (grouped[0].group_id,)
    ).fetchone()
    assert row["kind"] == "photo_event"
    assert row["label"] == "italy"


def test_project_folder_is_single_atomic_proposal(tmp_path: Path) -> None:
    conn = conn_for(tmp_path)
    grouped = group_proposals(
        conn,
        [project_folder_input(1, "DemoProject", "DemoProject", "Projects/DemoProject/DemoProject")],
    )

    assert grouped[0].group_id is not None
    assert conn.execute("SELECT COUNT(*) FROM groups WHERE kind='project'").fetchone()[0] == 1


def test_no_item_can_appear_in_two_proposals(tmp_path: Path) -> None:
    conn = conn_for(tmp_path)
    proposals = [
        GroupInput(1, "a.mp3", "music", "a.mp3", None, {"artist": "A", "album": "B"}),
        GroupInput(1, "a.mp3", "music", "a.mp3", None, {"artist": "A", "album": "B"}),
    ]

    with pytest.raises(ValueError, match="multiple proposals"):
        group_proposals(conn, proposals)
