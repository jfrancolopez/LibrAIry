"""Catalog probes: the point is telling a bad key from a genuine miss.

Every catalog tool returns None on any failure, which is correct during
analysis and useless the moment someone pastes a key and asks "did that work?".
These tests pin the three answers that matter — it worked, the key was
rejected, the service is up and simply had no match — because collapsing them
back into one "no result" is exactly the bug this module exists to prevent.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

from librairy import catalog_probe
from librairy.catalog_probe import probe_catalog
from librairy.catalogs import CATALOGS
from librairy.config import Settings
from librairy.db import connect
from librairy.secrets_store import save_key
from librairy.tools import discogs, musicbrainz, tmdb


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        _env_file=None,
    )


def _conn(tmp_path: Path):
    return connect(settings_for(tmp_path))


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code: int):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)  # type: ignore[arg-type]

    return opener


def test_every_catalog_in_the_registry_has_a_probe() -> None:
    """A new catalog with an untestable card is a card with a dead button."""
    missing = [c.slug for c in CATALOGS if not catalog_probe.testable(c.slug)]
    assert missing == []


def test_a_working_catalog_reports_what_it_found(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    save_key(conn, "tmdb", "a-key")
    monkeypatch.setattr(
        tmdb, "search", lambda *a, **k: {"title": "The Matrix", "release_date": "1999-03-31"}
    )

    result = probe_catalog(conn, settings_for(tmp_path), "tmdb")

    assert result.ok
    assert result.headline == "Working"
    assert "The Matrix" in result.detail
    assert "1999" in result.detail


def test_a_rejected_key_says_so_rather_than_no_match(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    save_key(conn, "tmdb", "wrong")
    monkeypatch.setattr(tmdb, "search", lambda *a, **k: None)

    result = probe_catalog(conn, settings_for(tmp_path), "tmdb", opener=_http_error(401))

    assert not result.ok
    assert result.headline == "Key rejected"
    assert "401" in result.detail


def test_rate_limiting_is_not_reported_as_a_broken_key(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    save_key(conn, "discogs", "a-token")
    monkeypatch.setattr(discogs, "search_release", lambda *a, **k: None)

    result = probe_catalog(conn, settings_for(tmp_path), "discogs", opener=_http_error(429))

    assert not result.ok
    assert result.headline == "Rate limited"
    assert "key is probably fine" in result.detail


def test_a_reachable_service_with_no_match_is_still_a_pass(tmp_path: Path, monkeypatch) -> None:
    """Keyless catalogs can only fail by being unreachable."""
    conn = _conn(tmp_path)
    monkeypatch.setattr(musicbrainz, "search_release", lambda *a, **k: None)

    result = probe_catalog(
        conn,
        settings_for(tmp_path),
        "musicbrainz",
        opener=lambda request, timeout=None: _Response(200),  # noqa: ARG005
    )

    assert result.ok
    assert result.headline == "Reachable, no match"


def test_an_unreachable_service_names_the_failure(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)
    monkeypatch.setattr(musicbrainz, "search_release", lambda *a, **k: None)

    def refuse(request, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("Name or service not known")

    result = probe_catalog(conn, settings_for(tmp_path), "musicbrainz", opener=refuse)

    assert not result.ok
    assert result.headline == "Cannot reach it"
    assert "Name or service not known" in result.detail


def test_a_missing_key_is_reported_before_any_request(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        raise AssertionError("no request should be made without a key")

    monkeypatch.setattr(tmdb, "search", explode)

    result = probe_catalog(conn, settings_for(tmp_path), "tmdb")

    assert not result.ok
    assert result.headline == "No key set"
    assert "TMDB_KEY" in result.detail


def test_acoustid_says_what_it_needs_when_there_is_no_audio(tmp_path: Path) -> None:
    """It identifies music from audio, so with no audio there is nothing to ask."""
    conn = _conn(tmp_path)
    save_key(conn, "acoustid", "a-key")

    result = probe_catalog(conn, settings_for(tmp_path), "acoustid")

    assert not result.ok
    assert result.headline == "Nothing to test with"
    assert "audio file" in result.detail
