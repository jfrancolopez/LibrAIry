from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from librairy.config import Settings
from librairy.db import connect
from librairy.models import EvidenceEntry
from librairy.proposals import upsert_proposal
from librairy.tools import coverart, musicbrainz
from librairy.web.app import create_app

RELEASE_MBID = "8ff3b1cc-0f7e-4c92-a55d-3c1a1a5f0d21"
JPEG = b"\xff\xd8\xff\xe0" + b"cover-art-bytes" * 8


class _Fake:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, size=None):
        return self.payload if size is None else self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(payload: bytes | None, calls: list[str], *, status: int = 404):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        if payload is None:
            raise HTTPError(request.full_url, status, "nope", {}, None)
        return _Fake(payload)

    return opener


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        APPDATA_DIR=tmp_path / "appdata",
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True, exist_ok=True)
    return settings


def setup_function() -> None:
    coverart.reset_cache()
    musicbrainz.reset_cache()


def test_cover_is_fetched_once_and_served_from_disk(tmp_path: Path) -> None:
    calls: list[str] = []

    first = coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(JPEG, calls))
    second = coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(JPEG, calls))

    assert first == tmp_path / "thumbs" / f"cover-{RELEASE_MBID}.jpg"
    assert first.read_bytes() == JPEG
    assert second == first
    assert len(calls) == 1
    assert f"release/{RELEASE_MBID}/front-250" in calls[0]


def test_a_release_with_no_art_is_not_asked_about_twice(tmp_path: Path) -> None:
    """Most releases have no cover; re-asking on every page view is rude."""
    calls: list[str] = []

    assert coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(None, calls)) is None
    assert coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(None, calls)) is None
    assert len(calls) == 1


def test_an_mbid_that_is_not_a_uuid_is_never_requested(tmp_path: Path) -> None:
    """The value lands in both a URL and a filename, so it is validated, not escaped."""
    calls: list[str] = []

    for hostile in ("../../etc/passwd", "a/b", "", "' OR 1=1"):
        assert coverart.cover_path(tmp_path, hostile, opener=_opener(JPEG, calls)) is None
    assert calls == []


def test_a_rate_limit_does_not_blacklist_a_release(tmp_path: Path) -> None:
    """503 says "slow down", not "this album has no cover".

    Caching it would hide real art for the life of the process — which is
    exactly what happened the first time this was run against the live service.
    """
    calls: list[str] = []

    assert (
        coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(None, calls, status=503))
        is None
    )
    found = coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(JPEG, calls))

    assert found is not None
    assert len(calls) == 2


def test_an_interrupted_fetch_leaves_no_partial_file(tmp_path: Path) -> None:
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        raise TimeoutError("connection dropped mid-body")

    assert coverart.cover_path(tmp_path, RELEASE_MBID, opener=opener) is None
    assert list((tmp_path / "thumbs").glob("*")) == [] or not (tmp_path / "thumbs").exists()


def test_an_absurdly_large_response_is_rejected(tmp_path: Path) -> None:
    calls: list[str] = []
    huge = b"x" * (coverart.MAX_BYTES + 1)

    assert coverart.cover_path(tmp_path, RELEASE_MBID, opener=_opener(huge, calls)) is None


def test_release_search_escapes_the_lucene_query() -> None:
    """An album called `Quote " Unquote` must not break out of the term."""
    calls: list[str] = []

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        return _Fake(json.dumps({"releases": [{"id": RELEASE_MBID}]}).encode())

    mbid = musicbrainz.search_release('Band "X"', "Album", opener=opener)

    assert mbid == RELEASE_MBID
    assert "%5C%22" in calls[0]  # the quote arrived backslash-escaped


def test_review_preview_shows_the_cover_and_writes_nothing_to_the_library(
    tmp_path: Path, monkeypatch
) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    track = settings.inbox_dir / "01 Alison.flac"
    track.write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', '01 Alison.flac', 5, 1, 'fp', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="music",
        clean_name="Alison.flac",
        dest_relpath="Music/Shoegaze/Slowdive/Souvlaki/Alison.flac",
        confidence=0.9,
        evidence=[EvidenceEntry("musicbrainz", "release_id", RELEASE_MBID, 0.9)],
    )
    monkeypatch.setattr(
        "librairy.tools.coverart.cover_path",
        lambda appdata, mbid, **kw: _written_cover(appdata, mbid),  # noqa: ARG005
    )

    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    card = client.get("/preview/items/1")
    image = client.get("/preview/items/1/thumb")

    assert card.status_code == 200
    assert "/preview/items/1/thumb" in card.text
    assert image.status_code == 200
    assert image.content == JPEG
    # The v1 invariant: LibrAIry moves and renames. It does not add files to
    # your collection, and art is no exception.
    assert list(settings.library_dir.rglob("*")) == []


def test_audio_with_no_cover_falls_back_to_no_thumbnail(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.inbox_dir / "unknown.mp3").write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'unknown.mp3', 5, 1, 'fp', 'now', 'now')
        """
    )
    monkeypatch.setattr("librairy.tools.ffprobe.probe", _no_tags)

    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    card = client.get("/preview/items/1")
    image = client.get("/preview/items/1/thumb")

    assert card.status_code == 200
    assert "/thumb" not in card.text
    assert image.status_code == 404


def test_disabling_the_catalog_stops_the_lookup(tmp_path: Path, monkeypatch) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.inbox_dir / "song.mp3").write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'song.mp3', 5, 1, 'fp', 'now', 'now')
        """
    )
    conn.execute("INSERT INTO settings(key, value) VALUES ('catalog.coverart.enabled', 'false')")
    upsert_proposal(
        conn,
        item_id=1,
        category="music",
        clean_name="song.mp3",
        dest_relpath="Music/Rock/A/B/song.mp3",
        confidence=0.9,
        evidence=[EvidenceEntry("musicbrainz", "release_id", RELEASE_MBID, 0.9)],
    )

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("fetched cover art with the catalog disabled")

    monkeypatch.setattr("librairy.tools.coverart.cover_path", forbidden)

    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})

    assert client.get("/preview/items/1").status_code == 200


def _written_cover(appdata: Path, mbid: str) -> Path:
    thumbs = appdata / "thumbs"
    thumbs.mkdir(parents=True, exist_ok=True)
    target = thumbs / f"cover-{mbid}.jpg"
    target.write_bytes(JPEG)
    return target


def _no_tags(path, settings):  # noqa: ANN001, ARG001
    from librairy.tools.common import ToolResult

    return ToolResult(True, data={"tags": {}})


def test_bulk_preview_never_starts_a_throttled_lookup(tmp_path: Path, monkeypatch) -> None:
    """"Expand all" on a page of 25 tracks must not serialise 25 MusicBrainz
    searches at 1.1s each — half a minute of the whole app waiting, to decorate
    rows nobody has looked at yet."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.inbox_dir / "song.mp3").write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'song.mp3', 5, 1, 'fp', 'now', 'now')
        """
    )

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("bulk preview searched MusicBrainz")

    monkeypatch.setattr("librairy.tools.musicbrainz.search_release", forbidden)
    monkeypatch.setattr("librairy.tools.ffprobe.probe", _tagged)

    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    bulk = client.get("/preview/items/1?bulk=1")

    assert bulk.status_code == 200
    assert "/thumb" not in bulk.text


def test_bulk_preview_still_shows_a_cover_already_on_disk(tmp_path: Path, monkeypatch) -> None:
    """Skipping the lookup must not mean skipping art we already fetched."""
    settings = settings_for(tmp_path)
    conn = connect(settings)
    (settings.inbox_dir / "song.mp3").write_bytes(b"audio")
    conn.execute(
        """
        INSERT INTO items(
          id, root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at
        )
        VALUES (1, 'inbox', 'song.mp3', 5, 1, 'fp', 'now', 'now')
        """
    )
    upsert_proposal(
        conn,
        item_id=1,
        category="music",
        clean_name="song.mp3",
        dest_relpath="Music/Rock/A/B/song.mp3",
        confidence=0.9,
        evidence=[EvidenceEntry("musicbrainz", "release_id", RELEASE_MBID, 0.9)],
    )
    _written_cover(settings.appdata_dir, RELEASE_MBID)

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("refetched a cover already in the cache")

    monkeypatch.setattr("librairy.tools.coverart._fetch", forbidden)

    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    bulk = client.get("/preview/items/1?bulk=1")

    assert "/preview/items/1/thumb" in bulk.text


def _tagged(path, settings):  # noqa: ANN001, ARG001
    from librairy.tools.common import ToolResult

    return ToolResult(True, data={"tags": {"artist": "Slowdive", "album": "Souvlaki"}})
