"""Eleven tracks that already agree, asked once.

    Music/Rock/Queen/
        t01.flac ... t09.flac    identified: News of the World
        t10.flac                 album tag:  News of the World
        t11.flac                 nothing at all

The per-track question in `test_loose_tracks.py` is the right shape when the
answers differ. These tests are about the case where they do not, and about
every way a group can look like it agrees without agreeing: two releases, a
member on a different album, an identity recorded against bytes the file no
longer has.

Nothing here calls a catalog. The evidence is `track_identity` rows and the
finding's own tag evidence, both persisted by something somebody asked for —
which is the whole claim of the feature, so the tests write exactly that and
nothing else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.album_identity import (
    ARTIST_CONFLICT,
    MIN_MEMBERS,
    UNRESOLVED,
    aggregate,
    file_as,
    leave_all,
)
from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused, accept_correction, undo_correction
from librairy.db import connect
from librairy.executor import execute_plan
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.track_identity import Identity, Release, remember
from librairy.web.commit_queue import queue_rows

ARTIST = "Music/Rock/Queen"
OPERA = f"{ARTIST}/A Night at the Opera"
NEWS = f"{ARTIST}/News of the World"
HITS = f"{ARTIST}/Greatest Hits"

NEWS_RELEASE = Release(
    catalog_id="r-news", title="News of the World", group_id="g-news",
    year=1977, kind="Album",
)
HITS_RELEASE = Release(
    catalog_id="r-hits", title="Greatest Hits", group_id="g-hits",
    year=1981, kind="Compilation",
)
OPERA_RELEASE = Release(
    catalog_id="r-opera", title="A Night at the Opera", group_id="g-opera",
    year=1975, kind="Album",
)


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


def loose(tmp_path: Path, *, tracks: int = 9, extra: dict[str, str] | None = None):
    """An artist with one album folder and a shelf of loose tracks."""
    files = {f"{OPERA}/01 - Bohemian Rhapsody.flac": "opera one"}
    files.update(
        {f"{ARTIST}/t{number:02d}.flac": f"loose {number}" for number in range(1, tracks + 1)}
    )
    files.update(extra or {})
    return library(tmp_path, files)


def finding(conn, tags: dict[str, str] | None = None, *, tracks: int = 9):
    """The finding as `audit_music` writes it, with the tag evidence it read."""
    evidence = [
        EvidenceEntry("filesystem", "loose tracks", str(tracks), 0.9),
        EvidenceEntry("library-pattern", "album folders here", "1", 0.85),
    ]
    evidence += [
        EvidenceEntry("tags", f"album of {name}", album, 0.9)
        for name, album in (tags or {}).items()
    ]
    record_findings(
        conn,
        [
            Finding(
                relpath=ARTIST,
                kind="loose-tracks",
                severity="review",
                summary=f"{tracks} track(s) sit directly in this artist folder.",
                evidence=evidence,
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='loose-tracks'"
    ).fetchone()


def identify(
    conn,
    relpath: str,
    *,
    releases: tuple[Release, ...] = (NEWS_RELEASE,),
    artist: str = "Queen",
    recording: str = "",
    fingerprint: str | None = None,
) -> None:
    """Write what a fingerprint lookup would have persisted for one file."""
    row = conn.execute(
        "SELECT id, fingerprint FROM items WHERE root='library' AND relpath=?",
        (relpath,),
    ).fetchone()
    assert row is not None
    remember(
        conn,
        Identity(
            item_id=int(row["id"]),
            provider="acoustid+musicbrainz",
            recording_id=recording or f"rec-{relpath}",
            artist=artist,
            title=relpath.rsplit("/", 1)[-1],
            releases=releases,
            fingerprint=(
                row["fingerprint"] if fingerprint is None else fingerprint
            ),
            score=0.94,
        ),
    )


def identify_all(conn, count: int, **kwargs) -> None:
    for number in range(1, count + 1):
        identify(conn, f"{ARTIST}/t{number:02d}.flac", **kwargs)


# --- 1-2: when a group is one album -------------------------------------------


def test_several_tracks_on_one_release_become_one_conclusion(tmp_path: Path) -> None:
    """Nine identities saying the same thing is one conclusion, not nine."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    assert found.single is True
    assert len(found.conclusions) == 1
    conclusion = found.conclusions[0]
    assert conclusion.relpath == NEWS
    assert len(conclusion.members) == 9
    assert conclusion.exact == 9
    #  The release is named with what tells it apart from the others, because
    #  `News of the World` alone does not distinguish an album from a remaster.
    assert conclusion.detail == "Album · 1977"


def test_matching_album_tags_support_the_same_conclusion(tmp_path: Path) -> None:
    """A file's own tag is evidence too, and it joins rather than competes."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 7)

    found = aggregate(
        conn,
        settings,
        finding(
            conn,
            {"t08.flac": "News of the World", "t09.flac": "News of the World"},
        ),
    )

    assert found is not None
    conclusion = found.conclusions[0]
    assert len(conclusion.members) == 9
    assert conclusion.exact == 7
    assert conclusion.tagged == 2
    #  Two facts, kept separate. Nine of nine as one number would say the tags
    #  and the fingerprints are the same kind of evidence, and they are not.
    assert dict(conclusion.counts) == {
        "identified from the audio": 7,
        "matching album tags": 2,
    }


# --- 3-4: when it is not one album --------------------------------------------


def test_two_different_releases_prevent_a_single_conclusion(tmp_path: Path) -> None:
    """Seven on one album and two on another is two conclusions, so it is none.

    The majority does not win. Filing the two would put them in a release they
    are demonstrably not on, which is the mistake this whole rule exists to
    avoid.
    """
    conn, settings = loose(tmp_path)
    identify_all(conn, 7)
    for number in (8, 9):
        identify(
            conn, f"{ARTIST}/t{number:02d}.flac", releases=(OPERA_RELEASE,)
        )

    assert aggregate(conn, settings, finding(conn)) is None


def test_a_tag_naming_another_album_refuses_the_conclusion(tmp_path: Path) -> None:
    """Positive evidence for a different album refuses it, whatever said so."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 8)

    found = aggregate(
        conn, settings, finding(conn, {"t09.flac": "A Night at the Opera"})
    )

    assert found is None


def test_several_coherent_releases_become_a_choice(tmp_path: Path) -> None:
    """Every member on both releases means both are true and neither is picked."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9, releases=(NEWS_RELEASE, HITS_RELEASE))

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    assert found.choice is True
    assert found.single is False
    assert {conclusion.relpath for conclusion in found.conclusions} == {NEWS, HITS}


def test_the_first_provider_result_is_not_the_album(tmp_path: Path) -> None:
    """MusicBrainz listed News of the World first. That is not a decision.

    The candidates come back in an order that is deterministic and explicitly
    not the catalog's, so nothing about the page can be read as a ranking.
    """
    conn, settings = loose(tmp_path)
    identify_all(conn, 9, releases=(NEWS_RELEASE, HITS_RELEASE))

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    assert [conclusion.name for conclusion in found.conclusions] == [
        "Greatest Hits",
        "News of the World",
    ]
    #  And nothing is chosen by asking: filing needs a release named by hand.
    with pytest.raises(CorrectionRefused):
        file_as(conn, settings, int(finding(conn)["id"]), f"{ARTIST}/Jazz")


def test_a_compilation_is_not_merged_into_the_album(tmp_path: Path) -> None:
    """Overlapping tracklists is what a compilation is, not evidence of one album."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9, releases=(NEWS_RELEASE, HITS_RELEASE))

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    by_name = {conclusion.name: conclusion for conclusion in found.conclusions}
    assert by_name["Greatest Hits"].detail == "Compilation · 1981"
    assert by_name["News of the World"].detail == "Album · 1977"
    #  Two folders, and neither absorbs the other.
    assert by_name["Greatest Hits"].relpath != by_name["News of the World"].relpath


# --- 5-6: members that do not count -------------------------------------------


def test_an_identity_for_older_bytes_is_excluded(tmp_path: Path) -> None:
    """A re-ripped file is a different file, and the old answer is not evidence.

    With the stale one discounted there are eight supporters and one member
    with nothing to go on — which is honest, and visibly different from nine.
    """
    conn, settings = loose(tmp_path)
    identify_all(conn, 8)
    identify(conn, f"{ARTIST}/t09.flac", fingerprint="bytes-that-are-gone")

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    conclusion = found.conclusions[0]
    assert len(conclusion.members) == 8
    assert conclusion.unresolved == 1
    assert [member.name for member in conclusion.exceptions] == ["t09.flac"]


def test_one_stale_identity_cannot_approve_the_others(tmp_path: Path) -> None:
    """Eight agreeing tracks are filed; the stale one keeps its own question."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 8)
    identify(conn, f"{ARTIST}/t09.flac", fingerprint="bytes-that-are-gone")
    row = finding(conn)

    assert file_as(conn, settings, int(row["id"]), NEWS) == 8

    from librairy.destination_choice import answers

    given = answers(conn, int(row["id"]))
    assert len(given) == 8
    assert f"{ARTIST}/t09.flac" not in given


def test_an_unresolved_member_is_shown_rather_than_swept_up(tmp_path: Path) -> None:
    """It does not join the conclusion and does not veto it either."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 8)

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    conclusion = found.conclusions[0]
    exception = conclusion.exceptions[0]
    assert exception.evidence == UNRESOLVED
    assert exception.reason == "Nothing says what this one is"
    assert len(conclusion.members) == 8


def test_a_different_artist_is_an_exception_not_a_member(tmp_path: Path) -> None:
    """The catalog says this one is by somebody else. Shown, never filed.

    Even its own album tag does not pull it in: the disagreement is the useful
    part, and filing on either reading would bury it.
    """
    conn, settings = loose(tmp_path)
    identify_all(conn, 8)
    identify(conn, f"{ARTIST}/t09.flac", artist="Wings")

    found = aggregate(
        conn, settings, finding(conn, {"t09.flac": "News of the World"})
    )

    assert found is not None
    conclusion = found.conclusions[0]
    assert len(conclusion.members) == 8
    assert conclusion.conflicts == 1
    assert conclusion.exceptions[0].evidence == ARTIST_CONFLICT
    assert conclusion.exceptions[0].detail == "Wings"


def test_two_tracks_of_one_recording_are_reported(tmp_path: Path) -> None:
    """The only tracklist check the stored evidence supports, said out loud.

    Reported and not acted on: whether the second one is a duplicate rip or a
    second copy somebody wants is not a fact this module has.
    """
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    identify(conn, f"{ARTIST}/t09.flac", recording="rec-shared")
    identify(conn, f"{ARTIST}/t08.flac", recording="rec-shared")

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    assert found.conclusions[0].repeats == ("t08.flac", "t09.flac")
    #  Still one conclusion. A repeat is a fact, not a refusal.
    assert len(found.conclusions[0].members) == 9


def test_too_few_agreeing_tracks_is_not_a_group(tmp_path: Path) -> None:
    """Two presses is what the per-track row already does well."""
    conn, settings = loose(tmp_path, tracks=MIN_MEMBERS - 1)
    identify_all(conn, MIN_MEMBERS - 1)

    assert aggregate(conn, settings, finding(conn, tracks=MIN_MEMBERS - 1)) is None


# --- 7-8: where they go -------------------------------------------------------


def test_an_album_folder_that_exists_is_reused(tmp_path: Path) -> None:
    """No second folder beside the one the artist already has."""
    conn, settings = loose(
        tmp_path, extra={f"{NEWS}/01 - We Will Rock You.flac": "already here"}
    )
    identify_all(conn, 9)

    found = aggregate(conn, settings, finding(conn))

    assert found is not None
    conclusion = found.conclusions[0]
    assert conclusion.relpath == NEWS
    assert conclusion.exists is True


def test_a_folder_that_does_not_exist_is_created_by_the_moves(tmp_path: Path) -> None:
    """No mkdir operation. A move makes its own parent."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)
    file_as(conn, settings, int(row["id"]), NEWS)
    plan_id = accept_correction(conn, settings, int(row["id"]))

    kinds = {
        op["op_type"]
        for op in conn.execute(
            "SELECT op_type FROM plan_ops WHERE plan_id=?", (plan_id,)
        )
    }

    assert kinds == {"move"}
    assert not (settings.library_dir / "Music/Rock/Queen/News of the World").exists()


# --- 9-12: the decision it becomes --------------------------------------------


def test_album_approval_feeds_the_existing_per_track_planner(tmp_path: Path) -> None:
    """It writes per-track answers, and nothing else knows an album was involved."""
    from librairy.track_filing import plan_filing

    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)

    assert file_as(conn, settings, int(row["id"]), NEWS) == 9

    view = plan_filing(conn, settings, row, verify=False)
    assert view is not None
    assert view.settled is True
    assert len(view.moving) == 9
    assert all(track.chosen == NEWS for track in view.moving)
    #  And the aggregate is gone, because there is nothing left to agree about.
    assert aggregate(conn, settings, row) is None


def test_the_group_becomes_one_commit_decision(tmp_path: Path) -> None:
    """Nine moves, one card, named after the conclusion rather than counted."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)
    file_as(conn, settings, int(row["id"]), NEWS)
    accept_correction(conn, settings, int(row["id"]))

    rows = queue_rows(conn, settings, kind="correction")

    assert len(rows) == 1
    assert rows[0]["subject"] == "File tracks as News of the World"
    assert rows[0]["after"] == f"library/{NEWS}"
    assert rows[0]["reason"] == "You chose where 9 tracks should go."


def test_the_summary_counts_members_rather_than_listing_them(tmp_path: Path) -> None:
    """A conclusion over twelve tracks is a sentence, not twelve rows.

    The members are a count; the exceptions are named, because those are the
    ones somebody still has to do something about.
    """
    conn, settings = loose(tmp_path, tracks=12)
    identify_all(conn, 11)
    row = finding(conn, tracks=12)

    from librairy.track_filing import plan_filing
    from librairy.web.review import _album_row

    view = plan_filing(conn, settings, row, verify=False)
    rendered = _album_row(conn, view, row)

    assert rendered is not None
    release = rendered["releases"][0]
    assert release["members"] == 11
    assert isinstance(release["members"], int)
    assert [member["name"] for member in release["exceptions"]] == ["t12.flac"]


def test_undo_puts_every_track_back(tmp_path: Path) -> None:
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)
    file_as(conn, settings, int(row["id"]), NEWS)
    plan_id = accept_correction(conn, settings, int(row["id"]))
    execute_plan(conn, plan_id, settings)

    assert (settings.library_dir / NEWS / "t01.flac").is_file()
    assert not (settings.library_dir / ARTIST / "t01.flac").exists()

    undo_correction(conn, settings, plan_id)

    for number in range(1, 10):
        assert (settings.library_dir / ARTIST / f"t{number:02d}.flac").is_file()
    assert not (settings.library_dir / NEWS / "t01.flac").exists()


def test_a_partial_album_does_not_claim_to_be_complete(tmp_path: Path) -> None:
    """Four tracks of a seventeen-track release is still a valid identity.

    What it is not is a complete album, and nothing here says it is: the words
    are about how many tracks were identified to the release, because that is
    the only thing the evidence supports.
    """
    conn, settings = loose(tmp_path, tracks=4)
    identify_all(conn, 4)

    found = aggregate(conn, settings, finding(conn, tracks=4))

    assert found is not None
    conclusion = found.conclusions[0]
    assert len(conclusion.members) == 4
    printed = " ".join(label for label, _ in conclusion.counts)
    assert "complete" not in printed.lower()
    assert "identified from the audio" in printed


# --- refusals -----------------------------------------------------------------


def test_filing_as_a_release_the_tracks_do_not_agree_on_is_refused(tmp_path: Path) -> None:
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)

    with pytest.raises(CorrectionRefused):
        file_as(conn, settings, int(row["id"]), HITS)


def test_leaving_them_all_answers_without_a_plan(tmp_path: Path) -> None:
    """A shelf of singles is filed correctly already."""
    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)

    assert leave_all(conn, settings, int(row["id"])) == 9

    from librairy.track_filing import plan_filing

    view = plan_filing(conn, settings, row, verify=False)
    assert view is not None
    assert view.settled is True
    assert len(view.leaving) == 9
    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, int(row["id"]))


def test_an_answered_track_is_not_reopened_by_the_group(tmp_path: Path) -> None:
    """Somebody said where that one goes. The group control does not overrule it."""
    from librairy.track_filing import answer

    conn, settings = loose(tmp_path)
    identify_all(conn, 9)
    row = finding(conn)
    answer(conn, settings, int(row["id"]), f"{ARTIST}/t01.flac", OPERA)

    found = aggregate(conn, settings, row)

    assert found is not None
    assert found.open_tracks == 8
    assert all(
        member.relpath != f"{ARTIST}/t01.flac"
        for member in found.conclusions[0].members
    )
