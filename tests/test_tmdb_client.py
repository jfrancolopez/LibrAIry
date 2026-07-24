from __future__ import annotations

import json

from librairy.config import Settings
from librairy.tools import tmdb


class _Fake:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(payload, calls):
    def opener(request, timeout=None):  # noqa: ANN001, ARG001
        calls.append(request.full_url)
        return _Fake(payload)

    return opener


def setup_function() -> None:
    tmdb.reset_cache()


def test_movie_search_returns_first_result_and_caches() -> None:
    calls: list[str] = []
    payload = {"results": [{"title": "Blade Runner", "release_date": "1982-06-25"}]}
    opener = _opener(payload, calls)

    first = tmdb.search(
        "blade runner", api_key="k", year=1982, opener=opener, sleeper=lambda s: None
    )
    tmdb.search("blade runner", api_key="k", year=1982, opener=opener, sleeper=lambda s: None)

    assert first["title"] == "Blade Runner"
    assert len(calls) == 1
    assert "search/movie" in calls[0]
    assert "year=1982" in calls[0]


def test_episode_search_uses_the_tv_endpoint() -> None:
    calls: list[str] = []
    tmdb.search(
        "the wire",
        api_key="k",
        episode=True,
        opener=_opener({"results": [{"name": "The Wire"}]}, calls),
        sleeper=lambda s: None,
    )

    assert "search/tv" in calls[0]


def test_missing_key_and_failures_return_none() -> None:
    def boom(request, timeout=None):  # noqa: ANN001, ARG001
        raise OSError("offline")

    assert tmdb.search("x", api_key="", opener=boom, sleeper=lambda s: None) is None
    assert tmdb.search("x", api_key="k", opener=boom, sleeper=lambda s: None) is None
    tmdb.reset_cache()
    assert (
        tmdb.search("x", api_key="k", opener=_opener({"results": []}, []), sleeper=lambda s: None)
        is None
    )


def test_lookup_adapter_is_none_without_a_key(tmp_path) -> None:
    unset = Settings(APPDATA_DIR=tmp_path / "a", _env_file=None)
    configured = Settings(APPDATA_DIR=tmp_path / "b", TMDB_KEY="secret", _env_file=None)

    assert tmdb.lookup_for_settings(unset) is None
    assert callable(tmdb.lookup_for_settings(configured))
