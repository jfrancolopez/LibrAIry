"""One artist's albums, previewed without four hundred rename rows.

    Music/Rock/Bowie/
        Hunky Dory/     11 tracks, 3 not in the current form
        Low/            11 tracks, 8 not in the current form
        Heroes/         10 tracks, already current
        Lodger/         10 tracks, already current

The single-album tool in `test_normalize_names.py` is safe because a person can
read eleven lines and judge them. An artist is the next useful scope and the
first one where **scale is a user-interface problem rather than a database
one**: a preview that executes perfectly and lists four thousand files is still
a terrible thing to approve.

So these tests are about boundedness and about the unit of the decision. The
summary is one directory walk and no file reads; the tags are read for one
album when somebody opens it; and selecting four albums produces four plans,
four Commit cards and four independent Undos — never one artist-wide
transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.normalize_names import (
    PAGE_ALBUMS,
    NormalizeError,
    approve_branch,
    branch_preview,
)
from librairy.scanner import scan_root
from librairy.web.commit_queue import queue_rows

ARTIST = "Music/Rock/Bowie"
HUNKY = f"{ARTIST}/Hunky Dory"
LOW = f"{ARTIST}/Low"
HEROES = f"{ARTIST}/Heroes"

TITLES = {
    "01-Changes.flac": {"title": "Changes", "track": "1"},
    "02-Oh-You-Pretty-Things.flac": {"title": "Oh! You Pretty Things", "track": "2"},
    "03-Life-on-Mars.flac": {"title": "Life on Mars", "track": "3"},
    "01-Speed-of-Life.flac": {"title": "Speed of Life", "track": "1"},
    "02-Breaking-Glass.flac": {"title": "Breaking Glass", "track": "2"},
    "01 - Beauty and the Beast.flac": {"title": "Beauty and the Beast", "track": "1"},
}


def tags_of(_settings, relpath: str) -> dict[str, str]:
    """The tags a real library's files carry, which a fixture's bytes cannot."""
    return TITLES.get(relpath.rsplit("/", 1)[-1], {})


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


def library(tmp_path: Path, files: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def bowie(tmp_path: Path):
    """Three albums: one old-style, one half old-style, one already current."""
    return library(
        tmp_path,
        {
            f"{HUNKY}/01-Changes.flac": "one",
            f"{HUNKY}/02-Oh-You-Pretty-Things.flac": "two",
            f"{HUNKY}/03-Life-on-Mars.flac": "three",
            f"{HUNKY}/cover.jpg": "a sleeve",
            f"{LOW}/01-Speed-of-Life.flac": "four",
            f"{LOW}/02-Breaking-Glass.flac": "five",
            f"{HEROES}/01 - Beauty and the Beast.flac": "six",
        },
    )


# --- 31-35: the summary --------------------------------------------------------


def test_the_preview_groups_by_album(tmp_path: Path) -> None:
    conn, settings = bowie(tmp_path)

    found = branch_preview(conn, settings, ARTIST)

    assert [album.name for album in found.albums] == ["Heroes", "Hunky Dory", "Low"]
    assert found.total == 3
    assert found.single is False


def test_the_summary_reads_no_files_at_all(tmp_path: Path) -> None:
    """The whole scale argument. One directory walk, no `ffprobe`, no tags.

    If drawing the summary read tags, fifty albums would be five hundred
    subprocesses to render a list of counts — and the page would be one nobody
    opens twice.
    """
    conn, settings = bowie(tmp_path)

    def explode(_settings, _relpath):  # noqa: ANN001, ANN202
        raise AssertionError("the summary must not read a file")

    import librairy.normalize_names as module

    original = module._tags_of
    module._tags_of = explode
    try:
        found = branch_preview(conn, settings, ARTIST)
    finally:
        module._tags_of = original

    assert found.total == 3


def test_the_counts_are_per_album_and_correct(tmp_path: Path) -> None:
    conn, settings = bowie(tmp_path)

    by_name = {album.name: album for album in branch_preview(conn, settings, ARTIST).albums}

    assert by_name["Hunky Dory"].tracks == 3
    assert by_name["Hunky Dory"].off_form == 3
    assert by_name["Low"].off_form == 2
    #  The sleeve is not a track and is not counted as one.
    assert by_name["Hunky Dory"].tracks == 3


def test_an_album_already_in_the_current_form_is_summarised_as_such(
    tmp_path: Path,
) -> None:
    conn, settings = bowie(tmp_path)

    by_name = {album.name: album for album in branch_preview(conn, settings, ARTIST).albums}

    assert by_name["Heroes"].current is True
    assert by_name["Heroes"].off_form == 0
    assert by_name["Hunky Dory"].current is False


def test_loose_tracks_in_the_artist_folder_are_their_own_group(tmp_path: Path) -> None:
    """An artist folder with singles beside its albums is what libraries look like."""
    conn, settings = bowie(tmp_path)
    (settings.library_dir / ARTIST / "Bowie-Single.flac").write_text("x", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    found = branch_preview(conn, settings, ARTIST)

    assert ARTIST in {album.relpath for album in found.albums}
    assert found.total == 4


# --- 36: opening one album -----------------------------------------------------


def test_opening_one_album_lists_the_exact_names(tmp_path: Path) -> None:
    """The reading happens here, for one album, because somebody asked."""
    from librairy.normalize_names import preview

    conn, settings = bowie(tmp_path)

    found = preview(conn, settings, HUNKY, read_tags=tags_of)

    assert [(m.name, m.proposed) for m in found.renaming] == [
        ("01-Changes.flac", "01 - Changes.flac"),
        ("02-Oh-You-Pretty-Things.flac", "02 - Oh! You Pretty Things.flac"),
        ("03-Life-on-Mars.flac", "03 - Life on Mars.flac"),
    ]


# --- 37-40: selecting and approving --------------------------------------------


def test_selecting_two_albums_creates_two_plans(tmp_path: Path) -> None:
    """Never one artist-wide transaction. An album is the unit of the decision."""
    conn, settings = bowie(tmp_path)

    approved = approve_branch(
        conn, settings, ARTIST, [HUNKY, LOW], read_tags=tags_of
    )

    assert [one.name for one in approved] == ["Hunky Dory", "Low"]
    assert all(one.plan_id for one in approved)
    assert approved[0].plan_id != approved[1].plan_id
    assert approved[0].renamed == 3
    assert approved[1].renamed == 2


def test_an_unselected_album_is_untouched(tmp_path: Path) -> None:
    conn, settings = bowie(tmp_path)

    approve_branch(conn, settings, ARTIST, [HUNKY], read_tags=tags_of)

    planned = {
        str(op["src_relpath"])
        for op in conn.execute("SELECT src_relpath FROM plan_ops")
    }
    assert all(relpath.startswith(HUNKY) for relpath in planned)


def test_an_album_with_nothing_to_do_says_so_rather_than_planning_nothing(
    tmp_path: Path,
) -> None:
    """An empty plan would still appear on Commit and still write History."""
    conn, settings = bowie(tmp_path)

    approved = approve_branch(conn, settings, ARTIST, [HEROES], read_tags=tags_of)

    assert approved[0].plan_id == ""
    assert approved[0].note == "nothing to change here"
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_collision_semantics_are_the_ones_the_album_tool_already_has(
    tmp_path: Path,
) -> None:
    """The safe members proceed and the collision is refused by name.

    Not a different consistency rule because several albums are on the page —
    the same rule, reported per album.
    """
    conn, settings = bowie(tmp_path)
    (settings.library_dir / HUNKY / "01 - Changes.flac").write_text("taken", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    approved = approve_branch(conn, settings, ARTIST, [HUNKY], read_tags=tags_of)

    assert approved[0].renamed == 2
    assert approved[0].refused == 1
    #  And no auto-numbering: the file that could not be renamed is still there.
    assert (settings.library_dir / HUNKY / "01-Changes.flac").is_file()


def test_a_folder_outside_the_branch_cannot_be_slipped_in(tmp_path: Path) -> None:
    """The form said what it was about. Anything else is refused."""
    conn, settings = bowie(tmp_path)

    with pytest.raises(NormalizeError):
        approve_branch(conn, settings, ARTIST, ["Music/Rock/Queen"], read_tags=tags_of)


def test_music_videos_are_not_a_music_branch(tmp_path: Path) -> None:
    """They have their own formatter and their own parser."""
    conn, settings = library(
        tmp_path, {"Music Videos/General/Bowie/Bowie - Heroes (Official Video).mkv": "v"}
    )

    with pytest.raises(NormalizeError):
        branch_preview(conn, settings, "Music Videos/General/Bowie")


# --- 41-43: what it becomes ----------------------------------------------------


def test_no_finding_is_created_by_any_of_it(tmp_path: Path) -> None:
    """Opt-in from first press to last. Nothing nags about old filenames."""
    conn, settings = bowie(tmp_path)

    branch_preview(conn, settings, ARTIST)
    approve_branch(conn, settings, ARTIST, [HUNKY], read_tags=tags_of)

    assert conn.execute("SELECT COUNT(*) c FROM audit_findings").fetchone()["c"] == 0


def test_commit_shows_one_card_per_album(tmp_path: Path) -> None:
    conn, settings = bowie(tmp_path)
    approve_branch(conn, settings, ARTIST, [HUNKY, LOW], read_tags=tags_of)

    rows = queue_rows(conn, settings, kind="correction")

    assert len(rows) == 2
    assert {row["current"] for row in rows} == {
        f"library/{HUNKY}", f"library/{LOW}"
    }


def test_one_album_is_undone_without_touching_the_other(tmp_path: Path) -> None:
    """Independent decisions, independently reversible. That is the point of it."""
    from librairy.corrections import undo_correction

    conn, settings = bowie(tmp_path)
    approved = approve_branch(conn, settings, ARTIST, [HUNKY, LOW], read_tags=tags_of)
    for one in approved:
        execute_plan(conn, one.plan_id, settings)

    assert (settings.library_dir / HUNKY / "01 - Changes.flac").is_file()
    assert (settings.library_dir / LOW / "01 - Speed of Life.flac").is_file()

    undo_correction(conn, settings, approved[0].plan_id)

    assert (settings.library_dir / HUNKY / "01-Changes.flac").is_file()
    assert (settings.library_dir / LOW / "01 - Speed of Life.flac").is_file()


def test_the_artist_folder_structure_and_metadata_are_untouched(tmp_path: Path) -> None:
    """Names, and nothing else. No folder moves and no byte inside a file changes."""
    conn, settings = bowie(tmp_path)
    before = (settings.library_dir / HUNKY / "01-Changes.flac").read_bytes()
    approved = approve_branch(conn, settings, ARTIST, [HUNKY], read_tags=tags_of)
    execute_plan(conn, approved[0].plan_id, settings)

    assert (settings.library_dir / HUNKY).is_dir()
    assert (settings.library_dir / LOW).is_dir()
    assert (settings.library_dir / HEROES).is_dir()
    assert (settings.library_dir / HUNKY / "cover.jpg").is_file()
    assert (settings.library_dir / HUNKY / "01 - Changes.flac").read_bytes() == before


# --- 47-48: scale --------------------------------------------------------------


def many_albums(tmp_path: Path, count: int):
    files = {
        f"{ARTIST}/Album {number:03d}/0{track}-Track-{track}.flac": "x"
        for number in range(count)
        for track in (1, 2)
    }
    return library(tmp_path, files)


def test_fifty_albums_render_in_one_page(tmp_path: Path) -> None:
    conn, settings = many_albums(tmp_path, 50)

    found = branch_preview(conn, settings, ARTIST)

    assert found.total == 50
    assert len(found.albums) == 50
    assert found.has_next is False


def test_five_hundred_albums_stay_bounded(tmp_path: Path) -> None:
    """The database can do it. The page must not.

    Five hundred rows is not a decision surface, so the page is a page: the
    total is reported honestly and the rows are the ones somebody can read.
    """
    conn, settings = many_albums(tmp_path, 500)

    found = branch_preview(conn, settings, ARTIST)

    assert found.total == 500
    assert len(found.albums) == PAGE_ALBUMS
    assert found.has_next is True
    #  And no file was read to say any of it.
    assert found.tracks == PAGE_ALBUMS * 2


def test_later_pages_reach_the_albums_the_first_one_did_not(tmp_path: Path) -> None:
    conn, settings = many_albums(tmp_path, 120)

    first = branch_preview(conn, settings, ARTIST, page=1)
    last = branch_preview(conn, settings, ARTIST, page=3)

    assert last.has_next is False
    assert len(last.albums) == 20
    assert not {album.relpath for album in first.albums} & {
        album.relpath for album in last.albums
    }


def test_an_album_on_a_later_page_can_still_be_approved(tmp_path: Path) -> None:
    """Selection is checked against the branch, not against one page of it."""
    conn, settings = many_albums(tmp_path, 120)
    late = f"{ARTIST}/Album 100"

    approved = approve_branch(
        conn, settings, ARTIST, [late], read_tags=lambda _s, _r: {}
    )

    assert approved[0].relpath == late


def test_a_single_album_folder_is_not_a_branch(tmp_path: Path) -> None:
    """The page that suits a folder is the page for what is in it."""
    conn, settings = bowie(tmp_path)

    found = branch_preview(conn, settings, HUNKY)

    assert found.single is True
    assert found.total == 1


# --- the pages ----------------------------------------------------------------


def client_for(tmp_path: Path):
    from fastapi.testclient import TestClient

    from librairy.web.app import create_app

    conn, settings = bowie(tmp_path)
    return TestClient(create_app(settings, conn)), conn, settings


def post(client, path: str, **data):
    client.get("/browse")
    token = client.cookies["csrf_token"]
    return client.post(
        path,
        data={**data, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


def test_the_artist_page_lists_albums_and_not_filenames(tmp_path: Path) -> None:
    """The bounded surface, proven on the page rather than on the dataclass."""
    client, *_ = client_for(tmp_path)

    page = post(client, "/browse/normalize", scope=ARTIST).text

    assert "Hunky Dory" in page
    assert "Low" in page
    assert "Show changes" in page
    #  Not one filename. Those come when an album is opened.
    assert "01-Changes.flac" not in page
    assert "01 - Changes.flac" not in page


def test_an_album_folder_still_gets_the_filename_page(tmp_path: Path) -> None:
    client, *_ = client_for(tmp_path)

    page = post(client, "/browse/normalize", scope=HEROES).text

    assert "Show changes" not in page
    assert "Nothing has moved. This is what would change." in page


def test_opening_an_album_returns_its_exact_names(tmp_path: Path, monkeypatch) -> None:
    import librairy.normalize_names as module

    monkeypatch.setattr(module, "_tags_of", tags_of)
    client, *_ = client_for(tmp_path)

    panel = post(client, "/browse/normalize/album", scope=HUNKY).text

    assert "01-Changes.flac" in panel
    assert "01 - Changes.flac" in panel
    #  A fragment, not a whole document: it is swapped into the album's fold.
    assert "<html" not in panel


def test_approving_two_albums_reports_each_of_them(
    tmp_path: Path, monkeypatch
) -> None:
    import librairy.normalize_names as module

    monkeypatch.setattr(module, "_tags_of", tags_of)
    client, conn, _ = client_for(tmp_path)

    page = post(
        client, "/browse/normalize/approve", scope=ARTIST, album=[HUNKY, HEROES]
    ).text

    assert "Hunky Dory" in page
    assert "Heroes" in page
    assert "nothing to change here" in page
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 1
