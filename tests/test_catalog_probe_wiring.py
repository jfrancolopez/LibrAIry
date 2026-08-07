"""Every probe must actually call its tool, correctly.

Mocking the tool function is what let three probes ship broken: a
`lambda *a, **k` accepts any call, so `search_release("...")` without its
required `token=` looked fine in the tests and raised TypeError the first time
a real button was pressed. AcoustID was worse -- it read `.fingerprint` off a
ToolResult payload that has always been a plain dict.

So these inject an opener one level below anything this package owns and let
the real client function run, with its real signature, against a canned
response. Patching the tool module's `urlopen` attribute would not do it
either: every client binds `opener=urlopen` as a default argument at import
time, so the default is captured before any test can reach it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from librairy.catalog_probe import probe_catalog
from librairy.config import Settings
from librairy.db import connect
from librairy.secrets_store import save_key


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        _env_file=None,
    )


class _Body:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw
        self.status = 200

    def read(self, *args):  # noqa: ANN002
        return self._raw

    def getheader(self, _name, default=None):  # noqa: ANN001
        return default

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _serving(payload) -> object:
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        return _Body(raw)

    return opener


PAYLOADS = {
    "tmdb": {"results": [{"title": "The Matrix", "release_date": "1999-03-31"}]},
    "tvmaze": [
        {"score": 0.99, "show": {"id": 1, "name": "Breaking Bad", "premiered": "2008-01-20"}}
    ],
    "musicbrainz": {"releases": [{"id": "4b3d18cc-8937-36f4-8de0-481088be58e6"}]},
    "discogs": {
        "results": [{"title": "Radiohead - Karma Police", "year": "1997", "genre": ["Rock"]}]
    },
    "lastfm": {"album": {"tags": {"tag": [{"name": "alternative", "count": 100}]}}},
    "openlibrary": {
        "docs": [{"title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965}]
    },
}
NEEDS_KEY = {"tmdb": "tmdb", "discogs": "discogs", "lastfm": "lastfm"}


@pytest.mark.parametrize("slug", sorted(PAYLOADS))
def test_each_probe_calls_its_client_correctly(slug: str, tmp_path: Path) -> None:
    payload = PAYLOADS[slug]
    conn = connect(settings_for(tmp_path))
    if slug in NEEDS_KEY:
        save_key(conn, NEEDS_KEY[slug], "a-key")

    result = probe_catalog(conn, settings_for(tmp_path), slug, opener=_serving(payload))

    assert result.ok, f"{slug}: {result.headline} — {result.detail}"
    assert result.headline == "Working"
    assert result.detail


def test_the_coverart_probe_resolves_a_release_then_fetches_the_sleeve(tmp_path: Path) -> None:
    """Two requests, in order: the release id first, then the image."""
    conn = connect(settings_for(tmp_path))
    release = {"releases": [{"id": "b1392450-e666-3926-a536-22c65f834433"}]}

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        if "coverartarchive" in request.full_url:
            return _Body(b"\xff\xd8\xff" + b"jpeg" * 100)
        return _Body(json.dumps(release).encode())

    result = probe_catalog(conn, settings_for(tmp_path), "coverart", opener=opener)

    assert result.ok, result.detail
    assert "OK Computer" in result.detail


def test_the_acoustid_probe_sends_the_duration_fpcalc_reported(tmp_path: Path, monkeypatch) -> None:
    """The bug this pins: ToolResult.data is a dict, and a lookup with no
    duration is refused by AcoustID before the key is even considered."""
    from librairy.tools import fpcalc
    from librairy.tools.common import ToolResult

    settings = settings_for(tmp_path)
    conn = connect(settings)
    save_key(conn, "acoustid", "a-key")
    track = settings.inbox_dir / "song.mp3"
    track.parent.mkdir(parents=True, exist_ok=True)
    track.write_bytes(b"not really audio")
    conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, state, first_seen_at, last_seen_at)
        VALUES ('inbox', 'song.mp3', 16, 0, 'discovered', '2026-01-01T00:00:00Z',
                '2026-01-01T00:00:00Z')
        """
    )
    monkeypatch.setattr(
        fpcalc,
        "fingerprint",
        lambda path, s: ToolResult(True, data={"duration": 298, "fingerprint": "AQADtEk"}),  # noqa: ARG005
    )
    sent: list[str] = []

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        sent.append(request.full_url)
        return _Body(
            json.dumps(
                {"results": [{"score": 0.97, "recordings": [{"id": "rec-1"}]}], "status": "ok"}
            ).encode()
        )

    result = probe_catalog(conn, settings, "acoustid", opener=opener)

    assert result.ok, result.detail
    assert "97%" in result.detail
    assert "duration=298" in sent[0]
