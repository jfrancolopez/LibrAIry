from __future__ import annotations

import json
from pathlib import Path

from librairy.classify.video import classify_video
from librairy.config import Settings
from librairy.tools import tvmaze

SHOW = {
    "id": 82,
    "name": "Game of Thrones",
    "genres": ["Drama", "Fantasy"],
    "premiered": "2011-04-17",
}
EPISODE = {"name": "The Rains of Castamere", "season": 3, "number": 9}


class _Fake:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(by_fragment: dict[str, object], calls: list[str]):
    """Route each request by a distinctive fragment of its URL."""

    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        for fragment, payload in by_fragment.items():
            if fragment in request.full_url:
                if payload is None:
                    raise OSError("not found")
                return _Fake(payload)
        raise AssertionError(f"unexpected request: {request.full_url}")

    return opener


def settings_for(tmp_path: Path, tmdb_key: str = "") -> Settings:
    return Settings(
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        APPDATA_DIR=tmp_path / "appdata",
        TMDB_KEY=tmdb_key,
    )


def setup_function() -> None:
    tvmaze.reset_cache()


def test_search_returns_show_and_episode_title() -> None:
    calls: list[str] = []
    opener = _opener({"singlesearch": SHOW, "episodebynumber": EPISODE}, calls)

    match = tvmaze.search_show(
        "Game of Thrones", season=3, episode=9, opener=opener, sleeper=lambda s: None
    )

    assert match["name"] == "Game of Thrones"
    assert match["episode_name"] == "The Rains of Castamere"
    assert match["genres"] == ["Drama", "Fantasy"]
    assert "q=Game+of+Thrones" in calls[0]
    assert "shows/82/episodebynumber?season=3&number=9" in calls[1]


def test_a_missing_episode_still_yields_the_show() -> None:
    """Releases renumber episodes; a 404 there must not discard the show match."""
    calls: list[str] = []
    opener = _opener({"singlesearch": SHOW, "episodebynumber": None}, calls)

    match = tvmaze.search_show(
        "Game of Thrones", season=9, episode=99, opener=opener, sleeper=lambda s: None
    )

    assert match["name"] == "Game of Thrones"
    assert "episode_name" not in match


def test_unknown_show_returns_none() -> None:
    calls: list[str] = []
    opener = _opener({"singlesearch": None}, calls)

    assert tvmaze.search_show("Nonexistent", opener=opener, sleeper=lambda s: None) is None


def test_repeated_queries_hit_the_cache_once() -> None:
    calls: list[str] = []
    opener = _opener({"singlesearch": SHOW}, calls)

    tvmaze.search_show("Game of Thrones", opener=opener, sleeper=lambda s: None)
    tvmaze.search_show("game of thrones", opener=opener, sleeper=lambda s: None)

    assert len(calls) == 1


def test_episode_resolves_via_tvmaze_when_tmdb_is_unavailable(tmp_path: Path) -> None:
    """The keyless path: no TMDB key, so TVmaze alone names the episode."""

    def lookup(parsed, _settings):
        assert parsed.title == "Game Of Thrones"
        return {**SHOW, "first_air_date": "2011-04-17", "episode_name": EPISODE["name"]}

    result = classify_video(
        "game.of.thrones.s03e09.1080p.mkv",
        settings=settings_for(tmp_path),
        tmdb_lookup=None,
        tvmaze_lookup=lookup,
    )

    assert result.category == "shows"
    assert result.clean_name == "S03E09 - The Rains of Castamere.mkv"
    assert result.fields["show"] == "Game of Thrones"
    assert result.fields["episode_title"] == "The Rains of Castamere"
    assert result.confidence == 0.82
    assert ("tvmaze", "episode_title") in [(e.source, e.field) for e in result.evidence]


def test_tmdb_names_the_show_and_tvmaze_names_the_episode(tmp_path: Path) -> None:
    """Documented ordering: TMDB wins the show, TVmaze adds what TMDB lacks."""

    def tmdb(parsed, _settings):
        return {"name": "Game of Thrones", "genres": [{"name": "Sci-Fi & Fantasy"}]}

    def tv(parsed, _settings):
        return {**SHOW, "episode_name": EPISODE["name"]}

    result = classify_video(
        "game.of.thrones.s03e09.mkv",
        settings=settings_for(tmp_path, tmdb_key="key"),
        tmdb_lookup=tmdb,
        tvmaze_lookup=tv,
    )

    sources = [e.source for e in result.evidence]
    assert result.fields["show"] == "Game of Thrones"
    assert result.fields["genre"] == "Sci-Fi & Fantasy"  # TMDB's, not TVmaze's
    assert result.clean_name == "S03E09 - The Rains of Castamere.mkv"
    # Two catalogs agreeing on the show is worth more than either alone.
    assert result.confidence == 0.9
    assert sources.count("tmdb") == 1
    assert sources.count("tvmaze") == 2


def test_no_tvmaze_lookup_leaves_episode_naming_unchanged(tmp_path: Path) -> None:
    result = classify_video(
        "game.of.thrones.s03e09.mkv",
        settings=settings_for(tmp_path),
        tmdb_lookup=None,
        tvmaze_lookup=None,
    )

    assert result.clean_name == "S03E09.mkv"
    assert "episode_title" not in result.fields
    assert result.confidence == 0.62


def test_tvmaze_is_not_consulted_for_movies(tmp_path: Path) -> None:
    def tv(parsed, _settings):
        raise AssertionError("TVmaze must not be asked about films")

    result = classify_video(
        "Blade.Runner.1982.1080p.mkv",
        settings=settings_for(tmp_path),
        tvmaze_lookup=tv,
    )

    assert result.category == "movies"
