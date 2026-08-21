"""A loose track whose album folder does not exist yet.

    Music/Rock/Queen/
        Death on Two Legs.flac      tagged: A Night at the Opera
        Spread Your Wings.flac      tagged: News of the World
        We Will Rock You.flac       tagged: News of the World
        A Night at the Opera/       exists
                                    News of the World/ does not

Per-track filing could already answer the first of those. The other two had
nothing to offer but `Leave here`, which is the common case in a messy library
rather than the rare one — the album folder usually does not exist yet, and
that is precisely why the tracks are loose.

So the question these tests ask is where the line sits. A folder may be offered
when the file itself says it belongs there, in a tag written by whoever tagged
it, agreeing with the artist folder it is already sitting in. It may not be
offered because a filename looked promising, because two titles resemble each
other, or because a model had an opinion. Everything below is one side of that
line or the other, plus the mechanics that follow: one folder for tracks that
agree, two candidates for albums that merely look alike, no `mkdir` in any
plan, and an Undo that leaves nothing behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.track_filing import LEAVE, answer, plan_filing

ARTIST = "Music/Rock/Queen"
OPERA = f"{ARTIST}/A Night at the Opera"
NEWS_NAME = "News of the World"
NEWS = f"{ARTIST}/{NEWS_NAME}"
LOOSE_A = f"{ARTIST}/Death on Two Legs.flac"
LOOSE_B = f"{ARTIST}/Spread Your Wings.flac"
LOOSE_C = f"{ARTIST}/We Will Rock You.flac"


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


def write(settings: Settings, files: dict[str, str]) -> None:
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def library(tmp_path: Path, files: dict[str, str]):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    write(settings, files)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def finding_with(conn, albums: dict[str, str]):
    """The finding as `audit_music` writes it, carrying each track's album tag."""
    record_findings(
        conn,
        [
            Finding(
                relpath=ARTIST,
                kind="loose-tracks",
                severity="review",
                summary=f"{len(albums)} track(s) sit directly in this artist folder.",
                evidence=[
                    EvidenceEntry("filesystem", "loose tracks", str(len(albums)), 0.9),
                    EvidenceEntry("library-pattern", "album folders here", "1", 0.85),
                    *[
                        EvidenceEntry("tags", f"album of {name}", album, 0.9)
                        for name, album in albums.items()
                    ],
                ],
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='loose-tracks'"
    ).fetchone()


def three_loose(tmp_path: Path):
    return library(
        tmp_path,
        {
            f"{OPERA}/01 - Bohemian Rhapsody.flac": "opera one",
            LOOSE_A: "loose one",
            LOOSE_B: "loose two",
            LOOSE_C: "loose three",
        },
    )


def tagged(**extra: str) -> dict[str, str]:
    albums = {
        "Death on Two Legs.flac": "A Night at the Opera",
        "Spread Your Wings.flac": NEWS_NAME,
        "We Will Rock You.flac": NEWS_NAME,
    }
    albums.update(extra)
    return albums


def view_of(conn, settings, row):
    return plan_filing(conn, settings, row, verify=False)


def track_of(view, relpath):
    return next(track for track in view.tracks if track.relpath == relpath)


# --- what may be offered ---------------------------------------------------------------


def test_a_track_whose_tags_name_a_missing_album_is_offered_that_folder(
    tmp_path: Path,
) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    view = view_of(conn, settings, row)

    assert [album.relpath for album in view.proposed] == [NEWS]
    assert view.offered(track_of(view, LOOSE_B))[0].name == NEWS_NAME


def test_the_folder_is_only_offered_to_the_tracks_that_named_it(tmp_path: Path) -> None:
    """One track's tag is evidence about that track. Offering its album to a
    file that never claimed to be on it would be the invention this avoids."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    view = view_of(conn, settings, row)

    assert view.offered(track_of(view, LOOSE_A)) == ()
    assert [a.name for a in view.offered(track_of(view, LOOSE_C))] == [NEWS_NAME]


def test_a_track_with_no_album_tag_is_offered_nothing_new(tmp_path: Path) -> None:
    """No evidence, no new destination. The row keeps the folders that exist
    and `Leave here`, which is the honest answer to "I do not know"."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, {})

    view = view_of(conn, settings, row)

    assert view.proposed == ()
    assert [album.relpath for album in view.albums] == [OPERA]


@pytest.mark.parametrize(
    "album", ["Singles", "Unknown Album", "Unknown", "Misc", "Various Artists", ""]
)
def test_a_taggers_placeholder_never_becomes_a_folder(
    tmp_path: Path, album: str
) -> None:
    """`Singles` is what a tagger writes when it has nothing to say. A folder
    called that is a folder named after the absence of information."""
    from librairy.audit_music import _album_tags

    class View:
        tags = {LOOSE_B: {"album": album, "artist": "Queen"}}

    assert _album_tags(View(), (LOOSE_B,), []) == []


def test_a_tag_naming_a_different_artist_is_evidence_about_somebody_else(
    tmp_path: Path,
) -> None:
    from librairy.audit_music import _album_tags

    class View:
        tags = {LOOSE_B: {"album": "Rumours", "artist": "Fleetwood Mac"}}

    assert _album_tags(View(), (LOOSE_B,), []) == []


def test_an_album_the_artist_already_has_is_a_candidate_and_not_a_proposal(
    tmp_path: Path,
) -> None:
    """It exists. Offering to create it would misdescribe the library."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    view = view_of(conn, settings, row)

    assert OPERA in [album.relpath for album in view.albums]
    assert OPERA not in [album.relpath for album in view.proposed]


def test_a_filename_that_looks_like_an_album_is_not_evidence(tmp_path: Path) -> None:
    """`Spread Your Wings.flac` is a name somebody typed. Nothing in the
    filename creates a folder, whatever it happens to resemble."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, {})

    view = view_of(conn, settings, row)

    assert view.proposed == ()


def test_the_proposed_folder_is_labelled_new_on_the_page(tmp_path: Path) -> None:
    from librairy.web.review import _filing_row

    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    shown = _filing_row(view_of(conn, settings, row), settings)

    assert [album["name"] for album in shown["proposed"]] == [NEWS_NAME]
    track = next(t for t in shown["tracks"] if t["relpath"] == LOOSE_B)
    assert track["proposed"][0]["agreeing"] == 2
    assert NEWS not in [album["relpath"] for album in track["albums"]]


def test_leave_here_is_still_available_alongside_a_new_folder(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    answer(conn, settings, int(row["id"]), LOOSE_B, LEAVE)

    assert track_of(view_of(conn, settings, row), LOOSE_B).leaving


# --- one album, however many tracks say so ---------------------------------------------


def test_two_tracks_naming_one_album_are_offered_one_folder(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    view = view_of(conn, settings, row)

    assert len(view.proposed) == 1
    assert view.proposed[0].tracks == (LOOSE_B, LOOSE_C)


def test_answering_separately_does_not_produce_a_second_folder(tmp_path: Path) -> None:
    """The failure this prevents is `News of the World/` beside
    `News of the World (2)/`, produced because two people pressed two buttons."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_C, NEWS)

    view = view_of(conn, settings, row)
    assert {track.chosen for track in view.moving} == {NEWS}


def test_spellings_that_differ_only_in_case_are_one_album(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(
        conn, tagged(**{"We Will Rock You.flac": "NEWS OF THE WORLD"})
    )

    view = view_of(conn, settings, row)

    assert len(view.proposed) == 1
    assert view.proposed[0].tracks == (LOOSE_B, LOOSE_C)


def test_albums_whose_names_differ_in_words_stay_separate(tmp_path: Path) -> None:
    """`News of the World` and `News of the World II` are close strings and are
    not the same release. Two candidates is honest; merging them is a guess."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(
        conn, tagged(**{"We Will Rock You.flac": "News of the World II"})
    )

    view = view_of(conn, settings, row)

    assert sorted(album.name for album in view.proposed) == [
        NEWS_NAME, "News of the World II"
    ]


def test_the_folder_is_named_the_way_the_library_spells_names(tmp_path: Path) -> None:
    """Spaces and apostrophes, like every other folder in this library — not a
    slug invented for this one feature."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged(**{
        "Spread Your Wings.flac": "Sheer Heart Attack",
        "We Will Rock You.flac": "Sheer Heart Attack",
    }))

    view = view_of(conn, settings, row)

    assert view.proposed[0].relpath == f"{ARTIST}/Sheer Heart Attack"


def test_an_unsafe_album_tag_is_made_safe_before_it_is_ever_offered(
    tmp_path: Path,
) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged(**{
        "Spread Your Wings.flac": "AC/DC Live",
        "We Will Rock You.flac": "AC/DC Live",
    }))

    view = view_of(conn, settings, row)

    assert view.proposed[0].relpath == f"{ARTIST}/AC-DC Live"


# --- what it becomes -------------------------------------------------------------------


def test_choosing_a_folder_that_does_not_exist_is_accepted(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)

    assert track_of(view_of(conn, settings, row), LOOSE_B).chosen == NEWS


def test_a_track_may_not_be_sent_to_a_folder_its_own_tags_never_named(
    tmp_path: Path,
) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    with pytest.raises(CorrectionRefused):
        answer(conn, settings, int(row["id"]), LOOSE_A, NEWS)


def test_the_plan_holds_moves_and_nothing_else(tmp_path: Path) -> None:
    """No `mkdir` operation. The folder is a consequence of the file arriving,
    which is also why there is nothing to roll back separately."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())
    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_C, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_A, LEAVE)

    plan_id = accept_correction(conn, settings, int(row["id"]))

    ops = conn.execute(
        "SELECT op_type, dest_relpath FROM plan_ops WHERE plan_id=? ORDER BY id",
        (plan_id,),
    ).fetchall()
    assert [op["op_type"] for op in ops] == ["move", "move"]
    assert sorted(op["dest_relpath"] for op in ops) == [
        f"{NEWS}/Spread Your Wings.flac",
        f"{NEWS}/We Will Rock You.flac",
    ]


def test_committing_creates_the_folder_by_moving_files_into_it(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())
    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_C, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_A, LEAVE)
    plan_id = accept_correction(conn, settings, int(row["id"]))

    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / NEWS).is_dir()
    assert (settings.library_dir / f"{NEWS}/Spread Your Wings.flac").is_file()
    assert (settings.library_dir / LOOSE_A).is_file()


def test_undo_puts_the_tracks_back(tmp_path: Path) -> None:
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())
    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_C, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_A, LEAVE)
    plan_id = accept_correction(conn, settings, int(row["id"]))
    execute_plan(conn, plan_id, settings)

    undo_correction(conn, settings, plan_id)

    assert (settings.library_dir / LOOSE_B).read_text(encoding="utf-8") == "loose two"
    assert (settings.library_dir / LOOSE_C).is_file()
    assert not (settings.library_dir / f"{NEWS}/Spread Your Wings.flac").exists()


def test_a_folder_that_appears_before_commit_is_looked_at_again(
    tmp_path: Path,
) -> None:
    """Absent when the choice was made does not mean absent when the plan runs.
    A file at the destination is the collision the merge planner already
    answers, and it is asked before anything moves."""
    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())
    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    answer(conn, settings, int(row["id"]), LOOSE_C, LEAVE)
    answer(conn, settings, int(row["id"]), LOOSE_A, LEAVE)

    write(settings, {f"{NEWS}/Spread Your Wings.flac": "somebody got there first"})
    scan_root(conn, "library", settings.library_dir, settings)

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, int(row["id"]))


def test_a_collision_at_a_new_folder_uses_the_existing_classification(
    tmp_path: Path,
) -> None:
    from librairy.merge import CONFLICT

    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())
    answer(conn, settings, int(row["id"]), LOOSE_B, NEWS)
    write(settings, {f"{NEWS}/Spread Your Wings.flac": "already here"})
    scan_root(conn, "library", settings.library_dir, settings)

    member = track_of(view_of(conn, settings, row), LOOSE_B).member

    assert member is not None
    assert member.state == CONFLICT
    assert member.options


def test_the_details_say_why_a_folder_can_be_offered(tmp_path: Path) -> None:
    """`Embedded album tag on 2 tracks`, not a provider's raw response."""
    from librairy.proposals import decode_evidence

    conn, settings = three_loose(tmp_path)
    row = finding_with(conn, tagged())

    entries = decode_evidence(row["evidence"])

    named = [entry for entry in entries if entry.field.startswith("album of")]
    assert {entry.source for entry in named} == {"tags"}
    assert {entry.detail for entry in named} == {"A Night at the Opera", NEWS_NAME}
    assert all("{" not in str(entry.detail) for entry in entries)
