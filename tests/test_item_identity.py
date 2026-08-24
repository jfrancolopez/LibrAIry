"""Identifying a track that is already filed.

    Music/Rock/Queen/A Night at the Opera/01 - Death on Two Legs.flac

Until now the only door to a catalog identity was a *problem*: a loose track
with nowhere to go. But identity is useful for a file that is filed perfectly
well — it is what lets a filename be normalized from a real title, and what
makes two similar files eligible to replace one another rather than merely be
compared. So the item has the door now.

These tests are mostly about what pressing it must not do. It reads, asks and
records; it does not rename, move, retag or approve. And nothing about drawing
the page may reach a catalog, however many times somebody reloads it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root
from librairy.track_identity import Identity, Release, remember
from librairy.web.app import create_app
from librairy.web.browse import music_identity

ALBUM = "Music/Rock/Queen/A Night at the Opera"
TRACK = f"{ALBUM}/01 - Death on Two Legs.flac"
SLEEVE = f"{ALBUM}/cover.jpg"
MOVIE = "Movies/The Matrix (1999)/The Matrix (1999).mkv"

RECORDING = "b1a9c0e8-1111-4444-8888-0123456789ab"


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
        TRACK: "the filed track",
        SLEEVE: "a sleeve",
        MOVIE: "video bytes",
    }.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def row_for(conn, relpath: str):
    return conn.execute(
        "SELECT * FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()


def store(conn, relpath: str, *, fingerprint: str | None = None, matched: bool = True):
    row = row_for(conn, relpath)
    remember(
        conn,
        Identity(
            item_id=int(row["id"]),
            provider="acoustid+musicbrainz",
            recording_id=RECORDING if matched else "",
            artist="Queen" if matched else "",
            title="Death on Two Legs" if matched else "",
            releases=(
                (
                    Release("r-1", "A Night at the Opera", "g-1", 1975, "Album"),
                    Release("r-2", "Greatest Hits", "g-2", 1981, "Compilation"),
                )
                if matched
                else ()
            ),
            fingerprint=row["fingerprint"] if fingerprint is None else fingerprint,
            score=0.95 if matched else 0.0,
        ),
    )


# --- who gets the section -----------------------------------------------------


def test_a_music_item_with_no_identity_offers_to_find_one(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert found["asked"] is False
    assert found["blocked"] == ""
    assert found["facts"] == []


def test_a_photograph_has_no_acoustic_fingerprint(tmp_path: Path) -> None:
    """No section at all, rather than a control that refuses when pressed."""
    conn, settings = library(tmp_path)

    assert music_identity(conn, settings, row_for(conn, SLEEVE)) is None


def test_a_film_is_not_offered_it_either(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)

    assert music_identity(conn, settings, row_for(conn, MOVIE)) is None


def test_a_disabled_provider_says_so_rather_than_hiding_the_button(
    tmp_path: Path,
) -> None:
    """"Nothing happened when I pressed it" and "there is no button and I do
    not know why" are the same failure."""
    conn, settings = library(tmp_path, key="")

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert "AcoustID key" in str(found["blocked"])


def test_a_missing_file_cannot_be_listened_to(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    (settings.library_dir / TRACK).unlink()
    scan_root(conn, "library", settings.library_dir, settings)

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert found["missing"] is True


# --- what it shows ------------------------------------------------------------


def test_a_current_identity_is_shown_as_facts(tmp_path: Path) -> None:
    """An identifier, a name and AcoustID's own score. No invented percentage."""
    conn, settings = library(tmp_path)
    store(conn, TRACK)

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert found["matched"] is True
    assert found["stale"] is False
    labels = {fact["label"] for fact in found["facts"]}
    assert labels == {
        "Matched by", "AcoustID score", "Artist", "Recording", "MusicBrainz recording"
    }
    assert found["recording_id"] == RECORDING
    assert found["fingerprint"] == row_for(conn, TRACK)["fingerprint"]


def test_every_release_is_shown_and_none_is_chosen(tmp_path: Path) -> None:
    """Identification and organisation are separate. This page files nothing."""
    conn, settings = library(tmp_path)
    store(conn, TRACK)

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert [release["title"] for release in found["releases"]] == [
        "A Night at the Opera",
        "Greatest Hits",
    ]
    assert [release["detail"] for release in found["releases"]] == [
        "Album · 1975",
        "Compilation · 1981",
    ]


def test_an_identity_for_older_bytes_is_labelled_stale(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    store(conn, TRACK, fingerprint="the-bytes-before-the-re-rip")

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert found["stale"] is True
    assert found["asked"] is True


def test_a_stale_identity_is_not_used_for_organisation(tmp_path: Path) -> None:
    """The page can show it; nothing that files anything may read it."""
    from librairy.track_identity import recall

    conn, settings = library(tmp_path)
    store(conn, TRACK, fingerprint="the-bytes-before-the-re-rip")
    row = row_for(conn, TRACK)

    assert recall(conn, int(row["id"]), fingerprint=str(row["fingerprint"])) is None


def test_asked_with_no_answer_says_that_rather_than_nothing(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    store(conn, TRACK, matched=False)

    found = music_identity(conn, settings, row_for(conn, TRACK))

    assert found is not None
    assert found["asked"] is True
    assert found["matched"] is False
    assert found["facts"] == []


# --- the page and the action --------------------------------------------------


def client_for(tmp_path: Path, **kwargs):
    conn, settings = library(tmp_path, **kwargs)
    return TestClient(create_app(settings, conn)), conn, settings


def post(client, path: str, **data):
    """Same as any other state-changing request: the token comes from a page."""
    client.get("/browse")
    token = client.cookies["csrf_token"]
    return client.post(
        path,
        data={**data, "csrf_token": token},
        headers={"x-csrf-token": token},
        follow_redirects=False,
    )


def counted(monkeypatch) -> dict[str, int]:
    """Both provider seams, replaced by something that counts being called."""
    from librairy.tools import acoustid, musicbrainz

    calls = {"acoustid": 0, "musicbrainz": 0}

    def printed(_path, _settings):  # noqa: ANN001, ANN202
        return 214, "a-fingerprint"

    def lookup(_fingerprint, _duration, **_kwargs):  # noqa: ANN001, ANN202
        calls["acoustid"] += 1
        return {"score": 0.94, "recording_id": RECORDING}

    def detail(mbid: str, **_kwargs):  # noqa: ANN003
        calls["musicbrainz"] += 1
        return {
            "recording_id": mbid,
            "title": "Death on Two Legs",
            "artist": "Queen",
            "artist_id": "artist-mbid",
            "releases": [
                {"id": "r-1", "title": "A Night at the Opera", "group_id": "g-1",
                 "year": 1975, "kind": "Album"}
            ],
        }

    monkeypatch.setattr(acoustid, "_fingerprint_file", printed)
    monkeypatch.setattr(acoustid, "lookup", lookup)
    monkeypatch.setattr(musicbrainz, "recording_detail", detail)
    return calls


def test_rendering_the_item_page_asks_no_catalog_anything(
    tmp_path: Path, monkeypatch
) -> None:
    """Not once, and not on the tenth reload either."""
    calls = counted(monkeypatch)
    client, conn, _ = client_for(tmp_path)
    item_id = int(row_for(conn, TRACK)["id"])

    for _ in range(3):
        assert client.get(f"/items/{item_id}").status_code == 200

    assert calls == {"acoustid": 0, "musicbrainz": 0}


def test_the_page_offers_the_button_and_the_post_records_an_answer(
    tmp_path: Path, monkeypatch
) -> None:
    calls = counted(monkeypatch)
    client, conn, _ = client_for(tmp_path)
    item_id = int(row_for(conn, TRACK)["id"])

    assert "Identify track" in client.get(f"/items/{item_id}").text

    response = post(client, f"/items/{item_id}/identify")

    assert response.status_code == 303
    assert calls == {"acoustid": 1, "musicbrainz": 1}
    page = client.get(f"/items/{item_id}").text
    assert "Death on Two Legs" in page
    assert RECORDING in page


def test_identification_moves_renames_and_retags_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """Everything the file was, it still is. Only the record changed."""
    counted(monkeypatch)
    client, conn, settings = client_for(tmp_path)
    row = row_for(conn, TRACK)
    before = (settings.library_dir / TRACK).read_bytes()

    post(client, f"/items/{int(row['id'])}/identify")

    after = row_for(conn, TRACK)
    assert (settings.library_dir / TRACK).read_bytes() == before
    assert after["relpath"] == row["relpath"]
    assert after["fingerprint"] == row["fingerprint"]
    #  And no decision was created anywhere by asking a question.
    assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 0


def test_a_stale_identity_offers_to_identify_the_current_file(tmp_path: Path) -> None:
    conn, settings = library(tmp_path)
    store(conn, TRACK, fingerprint="the-bytes-before-the-re-rip")
    client = TestClient(create_app(settings, conn))

    page = client.get(f"/items/{int(row_for(conn, TRACK)['id'])}").text

    assert "recorded for an older version of" in page
    assert "Identify current file" in page


def test_a_current_identity_does_not_encourage_asking_again(tmp_path: Path) -> None:
    """It is there, under a fold, because a second ask costs the same as the first."""
    conn, settings = library(tmp_path)
    store(conn, TRACK)
    client = TestClient(create_app(settings, conn))

    page = client.get(f"/items/{int(row_for(conn, TRACK)['id'])}").text

    assert "Identify track" not in page
    assert "Refresh identification" in page
    assert "Ask again" in page


def test_a_provider_failure_does_not_print_a_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    """A lookup that raises is an unidentified track, not a 500 with a stack in it."""
    from librairy.tools import acoustid

    def explode(_path, _settings):  # noqa: ANN001, ANN202
        raise OSError("fpcalc: no such file or directory")

    monkeypatch.setattr(acoustid, "_fingerprint_file", explode)
    client, conn, _ = client_for(tmp_path)
    item_id = int(row_for(conn, TRACK)["id"])

    with pytest.raises(OSError, match="fpcalc"):
        post(client, f"/items/{item_id}/identify")


def test_a_disabled_key_refuses_the_action_in_words(tmp_path: Path) -> None:
    client, conn, _ = client_for(tmp_path, key="")
    item_id = int(row_for(conn, TRACK)["id"])

    response = post(client, f"/items/{item_id}/identify")

    assert response.status_code == 409
    assert "AcoustID key" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_identification_makes_a_pair_eligible_for_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    """The point of doing it on a filed track, proven end to end.

    Two similar filed files are only ever *compared* until something says they
    are the same recording. Identifying both from the item page is that
    something.
    """
    from librairy.filed_replace import swaps_for
    from librairy.planner import utc_now
    from librairy.similar_media import KIND

    counted(monkeypatch)
    client, conn, settings = client_for(tmp_path)
    other = f"{ALBUM}/alternate/01 - Death on Two Legs.mp3"
    path = settings.library_dir / other
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a smaller rip", encoding="utf-8")
    scan_root(conn, "library", settings.library_dir, settings)
    first, second = sorted(
        int(row_for(conn, relpath)["id"]) for relpath in (TRACK, other)
    )
    conn.execute(
        "INSERT INTO similar_media_flags(item_id, similar_item_id, kind, score,"
        " created_at) VALUES (?, ?, 'audio', 0.97, ?)",
        (first, second, utc_now()),
    )
    from librairy.audit import Finding, record_findings

    record_findings(
        conn,
        [
            Finding(
                relpath=TRACK,
                kind=KIND,
                severity="review",
                summary="2 representations of the same thing.",
                evidence=[],
            )
        ],
    )
    row = conn.execute(
        "SELECT * FROM audit_findings WHERE kind=?", (KIND,)
    ).fetchone()

    assert swaps_for(conn, settings, row) == ()

    for relpath in (TRACK, other):
        post(client, f"/items/{int(row_for(conn, relpath)['id'])}/identify")

    assert len(swaps_for(conn, settings, row)) == 2
