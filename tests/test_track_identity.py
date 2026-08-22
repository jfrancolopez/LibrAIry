"""Asking the audio what it is, when the file will not say.

    Music/Rock/Queen/
        track 07.flac      no tags, no album folder, nothing to offer

Every destination LibrAIry offers has to come from something somebody or
something recorded. A tag is that. A folder that exists is that. A filename is
not, and neither is a model's opinion — so a track like the one above was an
honest dead end.

An acoustic fingerprint resolved through MusicBrainz is the one remaining
source that is evidence rather than resemblance, and these tests are mostly
about the ways it could stop being evidence: a first API result quietly chosen
out of five, a lookup on a page render, an identity still trusted after the
file was re-encoded, a destination invented because the lookup ran at all.

No test here touches the network. The fingerprint and catalog seams are passed
in, which is also how the browser workflow runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from librairy.audit import Finding, record_findings
from librairy.config import Settings
from librairy.corrections import CorrectionRefused
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.scanner import scan_root
from librairy.track_filing import IDENTIFIED, TAGGED, plan_filing
from librairy.track_identity import (
    Identity,
    Release,
    identify,
    recall,
    remember,
    unavailable,
)

ARTIST = "Music/Rock/Queen"
OPERA = f"{ARTIST}/A Night at the Opera"
LOOSE = f"{ARTIST}/track 07.flac"
OTHER = f"{ARTIST}/track 08.flac"

RECORDING = "b1a9c0e8-1111-4444-8888-0123456789ab"

RELEASES = [
    {"id": "r-1", "title": "A Night at the Opera", "group_id": "g-1",
     "year": 1975, "kind": "Album"},
    {"id": "r-2", "title": "Greatest Hits", "group_id": "g-2",
     "year": 1981, "kind": "Compilation"},
    {"id": "r-3", "title": "A Night at the Opera (2011 Remaster)", "group_id": "g-1",
     "year": 2011, "kind": "Album"},
]


def settings_for(tmp_path: Path, *, key: str = "a-real-key") -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        ACOUSTID_KEY=key,
        _env_file=None,
    )
    for root in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def library(tmp_path: Path, **kwargs):
    settings = settings_for(tmp_path, **kwargs)
    conn = connect(settings)
    for relpath, body in {
        f"{OPERA}/01 - Bohemian Rhapsody.flac": "opera one",
        LOOSE: "unidentified audio",
        OTHER: "more unidentified audio",
    }.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def finding(conn, evidence=()):
    record_findings(
        conn,
        [
            Finding(
                relpath=ARTIST,
                kind="loose-tracks",
                severity="review",
                summary="2 track(s) sit directly in this artist folder.",
                evidence=[
                    EvidenceEntry("filesystem", "loose tracks", "2", 0.9),
                    *evidence,
                ],
            )
        ],
    )
    return conn.execute(
        "SELECT * FROM audit_findings WHERE kind='loose-tracks'"
    ).fetchone()


def fake_acoustid(score: float = 0.95, recording: str = RECORDING):
    def lookup(_relpath: str) -> dict | None:
        return {"score": score, "recording_id": recording}

    return lookup


def fake_musicbrainz(releases=None, artist: str = "Queen"):
    def detail(mbid: str) -> dict | None:
        return {
            "recording_id": mbid,
            "title": "Death on Two Legs",
            "artist": artist,
            "artist_id": "artist-mbid",
            "releases": RELEASES if releases is None else releases,
        }

    return detail


def silent(_x=None):  # noqa: ANN001
    return None


def item_id(conn, relpath: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
        ).fetchone()["id"]
    )


def view_of(conn, settings, row):
    return plan_filing(conn, settings, row, verify=False)


def track_of(view, relpath):
    return next(track for track in view.tracks if track.relpath == relpath)


# --- the dead end it starts from ---------------------------------------------------


def test_an_untagged_track_with_no_identity_stays_unresolved(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)

    view = view_of(conn, settings, row)

    assert view.proposed == ()
    assert track_of(view, LOOSE).identity is None


def test_the_row_offers_to_identify_it(tmp_path: Path) -> None:
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(conn)

    shown = _filing_row(view_of(conn, settings, row), settings, conn)

    track = next(t for t in shown["tracks"] if t["relpath"] == LOOSE)
    assert track["identify"] is True
    assert track["identify_blocked"] == ""


def test_a_track_whose_tags_name_an_existing_folder_is_not_offered_a_lookup(
    tmp_path: Path,
) -> None:
    """Evidence is evidence whether or not it produced a *new* folder. A track
    whose tags name an album the artist already has is answered; asking the
    audio would be a fingerprint and a request with a known conclusion."""
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(
        conn,
        [EvidenceEntry("tags", "album of track 07.flac", "A Night at the Opera", 0.9)],
    )

    shown = _filing_row(view_of(conn, settings, row), settings, conn)

    track = next(t for t in shown["tracks"] if t["relpath"] == LOOSE)
    assert track["identify"] is False


def test_a_track_that_already_has_a_new_candidate_is_not_offered_a_lookup(
    tmp_path: Path,
) -> None:
    """An album folder that exists is a better answer than a network round
    trip, and the button would be work with a known answer."""
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(
        conn, [EvidenceEntry("tags", "album of track 07.flac", "News of the World", 0.9)]
    )

    shown = _filing_row(view_of(conn, settings, row), settings, conn)

    track = next(t for t in shown["tracks"] if t["relpath"] == LOOSE)
    assert track["identify"] is False


# --- provider policy ------------------------------------------------------------------


def test_with_no_key_the_reason_is_said_rather_than_the_button_hidden(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path, key="")

    assert "AcoustID key" in unavailable(conn, settings)


def test_a_disabled_catalog_is_not_worked_around(tmp_path: Path) -> None:
    """Switching MusicBrainz off is a decision. Asking something else instead
    would be the software routing around it."""
    import json

    conn, settings = library(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('catalog.musicbrainz.enabled', ?)",
        (json.dumps(False),),
    )

    assert "MusicBrainz is switched off" in unavailable(conn, settings)
    with pytest.raises(CorrectionRefused):
        identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
                 musicbrainz=fake_musicbrainz())


def test_a_disabled_acoustid_refuses_before_reading_the_file(tmp_path: Path) -> None:
    import json

    conn, settings = library(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES ('catalog.acoustid.enabled', ?)",
        (json.dumps(False),),
    )

    called = []

    def watchful(relpath):  # noqa: ANN001, ANN202
        called.append(relpath)
        return None

    with pytest.raises(CorrectionRefused):
        identify(conn, settings, LOOSE, acoustid=watchful, musicbrainz=silent)
    assert called == []


def test_nothing_is_looked_up_while_a_page_is_drawn(tmp_path: Path) -> None:
    """A page of fifty rows must not be fifty fingerprints and fifty requests,
    and expanding Details must not be an outbound call."""
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(conn)
    calls: list[str] = []
    import librairy.tools.acoustid as acoustid_tool
    import librairy.tools.musicbrainz as musicbrainz_tool

    original = (acoustid_tool.lookup, musicbrainz_tool.recording_detail)
    acoustid_tool.lookup = lambda *a, **k: calls.append("acoustid")  # type: ignore[assignment]
    musicbrainz_tool.recording_detail = lambda *a, **k: calls.append("mb")  # type: ignore[assignment]
    try:
        _filing_row(view_of(conn, settings, row), settings, conn)
    finally:
        acoustid_tool.lookup, musicbrainz_tool.recording_detail = original

    assert calls == []


# --- what a lookup produces -------------------------------------------------------------


def test_a_match_is_stored_and_read_back(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)

    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz())

    stored = recall(conn, item_id(conn, LOOSE))
    assert stored is not None
    assert stored.recording_id == RECORDING
    assert [release.title for release in stored.releases] == [
        "A Night at the Opera", "Greatest Hits", "A Night at the Opera (2011 Remaster)"
    ]


def test_review_reads_the_stored_answer_and_does_not_ask_again(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz())

    view = view_of(conn, settings, row)

    identity = track_of(view, LOOSE).identity
    assert identity is not None and identity.recording_id == RECORDING


def test_a_weak_fingerprint_score_is_not_a_match(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)

    assert identify(conn, settings, LOOSE, acoustid=fake_acoustid(score=0.2),
                    musicbrainz=fake_musicbrainz()) is None


def test_a_failed_lookup_invents_nothing(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)

    assert identify(conn, settings, LOOSE, acoustid=silent, musicbrainz=silent) is None

    view = view_of(conn, settings, row)
    assert view.proposed == ()
    #  Asked and answered with nothing, which is recorded — but an identity
    #  that matched nothing is not evidence and creates no destination.
    assert not track_of(view, LOOSE).identity.matched


def test_a_failed_lookup_is_remembered_so_it_is_not_repeated(tmp_path: Path) -> None:
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=silent, musicbrainz=silent)

    shown = _filing_row(view_of(conn, settings, row), settings, conn)

    track = next(t for t in shown["tracks"] if t["relpath"] == LOOSE)
    assert track["identify"] is False


def test_a_recording_with_no_release_gives_no_destination(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)

    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[]))

    assert view_of(conn, settings, row).proposed == ()


# --- releases are candidates, never an answer ----------------------------------------------


def test_every_release_becomes_a_candidate_and_none_is_chosen(tmp_path: Path) -> None:
    """`Death on Two Legs` is on the album, on a greatest-hits and on a
    remaster. Which of those a library is about is not in the file."""
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz())

    view = view_of(conn, settings, row)

    #  `A Night at the Opera` is missing from this list because the artist
    #  already has that folder — it is an ordinary candidate above, and
    #  offering to create a folder that exists would misdescribe the library.
    assert [album.name for album in view.offered(track_of(view, LOOSE))] == [
        "A Night at the Opera (2011 Remaster)", "Greatest Hits"
    ]
    assert OPERA in [album.relpath for album in view.albums]


def test_the_first_api_result_is_not_silently_used(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz())

    view = view_of(conn, settings, row)

    assert track_of(view, LOOSE).chosen == ""
    assert not view.settled


def test_a_release_the_artist_already_has_a_folder_for_is_not_proposed(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[0]]))

    view = view_of(conn, settings, row)

    assert view.proposed == ()
    assert OPERA in [album.relpath for album in view.albums]


def test_two_tracks_identified_to_one_release_share_the_candidate(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    for relpath in (LOOSE, OTHER):
        identify(conn, settings, relpath, acoustid=fake_acoustid(),
                 musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))

    view = view_of(conn, settings, row)

    assert len(view.proposed) == 1
    assert set(view.proposed[0].tracks) == {LOOSE, OTHER}


def test_the_candidate_says_where_the_name_came_from(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))

    view = view_of(conn, settings, row)

    assert view.proposed[0].source == IDENTIFIED
    assert "fingerprint" in view.proposed[0].note.lower()


def test_each_track_is_told_its_own_evidence(tmp_path: Path) -> None:
    """One album, two tracks, two different routes to it: one has the tag and
    the other was identified by its audio. Telling the second one it has a tag
    would be describing the first one's evidence as its own."""
    from librairy.web.review import _filing_row

    conn, settings = library(tmp_path)
    row = finding(
        conn, [EvidenceEntry("tags", "album of track 08.flac", "Greatest Hits", 0.9)]
    )
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))

    shown = _filing_row(view_of(conn, settings, row), settings, conn)

    identified = next(t for t in shown["tracks"] if t["relpath"] == LOOSE)
    tagged = next(t for t in shown["tracks"] if t["relpath"] == OTHER)
    assert identified["proposed"][0]["source"] == IDENTIFIED
    assert "fingerprint" in identified["proposed"][0]["note"].lower()
    assert identified["proposed"][0]["agreeing"] == 1
    assert tagged["proposed"][0]["source"] == TAGGED
    assert "tag" in tagged["proposed"][0]["note"].lower()


def test_a_tag_and_a_catalog_naming_one_album_are_one_candidate(
    tmp_path: Path,
) -> None:
    """Two rungs of the ladder agreeing is not two folders, and the credit goes
    to the one that needed no network."""
    conn, settings = library(tmp_path)
    row = finding(
        conn, [EvidenceEntry("tags", "album of track 07.flac", "Greatest Hits", 0.9)]
    )
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))

    view = view_of(conn, settings, row)

    assert [album.name for album in view.proposed] == ["Greatest Hits"]
    assert view.proposed[0].source == TAGGED


# --- disagreement -----------------------------------------------------------------------


def test_a_catalog_naming_a_different_artist_is_surfaced_not_acted_on(
    tmp_path: Path,
) -> None:
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(artist="Fleetwood Mac"))

    view = view_of(conn, settings, row)

    assert track_of(view, LOOSE).conflict == "Fleetwood Mac"
    assert view.proposed == ()


def test_the_evidence_is_facts_and_not_an_invented_confidence(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    identity = identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
                        musicbrainz=fake_musicbrainz())

    assert identity is not None
    labels = dict(identity.evidence)
    assert labels["Matched by"] == "Acoustic fingerprint"
    assert labels["MusicBrainz recording"] == RECORDING
    assert "%" not in " ".join(labels.values())


# --- staying true to the bytes -------------------------------------------------------------


def test_an_identity_recorded_against_other_bytes_is_not_used(tmp_path: Path) -> None:
    """A re-ripped track is a different file. An identity about the old bytes
    is not evidence about the new ones."""
    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz())
    (settings.library_dir / LOOSE).write_text("a different rip", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)

    view = view_of(conn, settings, row)

    assert track_of(view, LOOSE).identity is None
    assert view.proposed == ()


def test_an_expired_answer_is_not_used(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    remember(
        conn,
        Identity(
            item_id=item_id(conn, LOOSE),
            provider="test",
            recording_id=RECORDING,
            artist="Queen",
            title="Death on Two Legs",
            releases=(Release("r-1", "A Night at the Opera"),),
        ),
    )
    conn.execute(
        "UPDATE track_identity SET looked_up_at='2020-01-01T00:00:00+00:00'"
    )

    assert recall(conn, item_id(conn, LOOSE)) is None


# --- and then it is the workflow that already existed ----------------------------------------


def test_choosing_an_identified_release_uses_the_existing_filing_path(
    tmp_path: Path,
) -> None:
    from librairy.corrections import accept_correction
    from librairy.executor import execute_plan
    from librairy.track_filing import LEAVE, answer

    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))
    answer(conn, settings, int(row["id"]), LOOSE, f"{ARTIST}/Greatest Hits")
    answer(conn, settings, int(row["id"]), OTHER, LEAVE)

    plan_id = accept_correction(conn, settings, int(row["id"]))
    ops = conn.execute(
        "SELECT op_type, dest_relpath FROM plan_ops WHERE plan_id=?", (plan_id,)
    ).fetchall()
    execute_plan(conn, plan_id, settings)

    assert [op["op_type"] for op in ops] == ["move"]
    assert (settings.library_dir / f"{ARTIST}/Greatest Hits/track 07.flac").is_file()
    assert (settings.library_dir / OTHER).is_file()


def test_a_stale_source_is_still_caught_at_approval(tmp_path: Path) -> None:
    from librairy.corrections import accept_correction
    from librairy.track_filing import answer

    conn, settings = library(tmp_path)
    row = finding(conn)
    identify(conn, settings, LOOSE, acoustid=fake_acoustid(),
             musicbrainz=fake_musicbrainz(releases=[RELEASES[1]]))
    answer(conn, settings, int(row["id"]), LOOSE, f"{ARTIST}/Greatest Hits")
    (settings.library_dir / LOOSE).write_text("re-ripped since", encoding="utf-8")

    with pytest.raises(CorrectionRefused):
        accept_correction(conn, settings, int(row["id"]))
