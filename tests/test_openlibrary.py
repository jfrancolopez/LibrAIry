from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from librairy.catalogs import catalog_enabled
from librairy.classify.documents import classify_document_like
from librairy.config import Settings
from librairy.db import connect
from librairy.tools import openlibrary


@contextmanager
def _response(payload: dict):
    class _Fake:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    yield _Fake()


def _opener(payload: dict, calls: list):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        return _response(payload).__enter__()

    return opener


def setup_function() -> None:
    openlibrary.reset_cache()


def test_search_book_parses_first_match() -> None:
    calls: list[str] = []
    payload = {
        "docs": [
            {"title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965}
        ]
    }

    match = openlibrary.search_book("dune", opener=_opener(payload, calls), sleeper=lambda s: None)

    assert match.title == "Dune"
    assert match.author == "Frank Herbert"
    assert match.year == 1965
    assert "openlibrary.org/search.json" in calls[0]
    assert "title=dune" in calls[0]


def test_repeated_titles_hit_the_cache_once() -> None:
    calls: list[str] = []
    payload = {"docs": [{"title": "Dune"}]}
    opener = _opener(payload, calls)

    openlibrary.search_book("dune", opener=opener, sleeper=lambda s: None)
    openlibrary.search_book("dune", opener=opener, sleeper=lambda s: None)

    assert len(calls) == 1


def test_network_failure_and_empty_results_degrade_to_none() -> None:
    def boom(request, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("network down")

    assert openlibrary.search_book("dune", opener=boom, sleeper=lambda s: None) is None
    openlibrary.reset_cache()
    assert (
        openlibrary.search_book(
            "nothing", opener=_opener({"docs": []}, []), sleeper=lambda s: None
        )
        is None
    )


def test_book_classification_uses_open_library_evidence(tmp_path: Path) -> None:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", LIBRARY_DIR=tmp_path / "lib", _env_file=None
    )

    def lookup(title: str):  # noqa: ARG001
        return openlibrary.BookMatch(title="Dune", author="Frank Herbert", year=1965)

    result = classify_document_like("dune-opaque-name.epub", settings=settings, book_lookup=lookup)

    assert result.category == "books"
    assert result.confidence >= 0.92
    assert result.fields["author"] == "Frank Herbert"
    assert result.fields["title"] == "Dune"
    sources = [entry.source for entry in result.evidence]
    assert "openlibrary" in sources


def test_book_classification_without_lookup_stays_heuristic(tmp_path: Path) -> None:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata", LIBRARY_DIR=tmp_path / "lib", _env_file=None
    )

    result = classify_document_like("dune-opaque-name.epub", settings=settings)

    assert result.category == "books"
    assert [e.source for e in result.evidence] == ["heuristic"]
    assert result.fields["author"] == "Unknown Author"


def test_catalog_toggle_defaults_on_and_respects_stored_value(tmp_path: Path) -> None:
    settings = Settings(APPDATA_DIR=tmp_path / "appdata", _env_file=None)
    conn = connect(settings)

    assert catalog_enabled(conn, "openlibrary") is True

    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES (?, 'false')",
        ("catalog.openlibrary.enabled",),
    )

    assert catalog_enabled(conn, "openlibrary") is False
