"""TMDB lookup: identify movies and TV shows. Free key, personal use.

Same shape as the Open Library client: stdlib-only HTTP, short timeout, polite
delay, per-process cache, and None on any failure so classification degrades to
heuristics. Returns the raw TMDB result dict that classify/video.py already
knows how to read (`title`/`name`, `release_date`/`first_air_date`, `genres`).
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SEARCH_MOVIE_URL = "https://api.themoviedb.org/3/search/movie"
SEARCH_TV_URL = "https://api.themoviedb.org/3/search/tv"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 0.3
TIMEOUT_SECONDS = 8

_CACHE: dict[str, dict[str, Any] | None] = {}
_LAST_CALL = 0.0


def search(
    query: str,
    *,
    api_key: str,
    year: int | None = None,
    episode: bool = False,
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, Any] | None:
    """Best-effort first match. None when unidentified or unconfigured."""
    query = query.strip()
    if not query or not api_key:
        return None
    cache_key = f"{'tv' if episode else 'movie'}|{query}|{year or ''}".lower()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {"api_key": api_key, "query": query}
    if year:
        params["year" if not episode else "first_air_date_year"] = str(year)
    url = SEARCH_TV_URL if episode else SEARCH_MOVIE_URL
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{url}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[cache_key] = None
        return None

    results = payload.get("results") if isinstance(payload, dict) else None
    match = results[0] if results else None
    _CACHE[cache_key] = match
    return match


def lookup_for_settings(settings) -> Any:
    """Adapter matching classify/video.py's TmdbLookup contract."""
    key = settings.tmdb_key.get_secret_value()
    if not key:
        return None

    def lookup(parsed, _settings):  # noqa: ANN001
        return search(
            parsed.title,
            api_key=key,
            year=parsed.year or None,
            episode=parsed.is_episode,
        )

    return lookup


def _throttle(sleeper) -> None:
    global _LAST_CALL
    elapsed = time.monotonic() - _LAST_CALL
    if _LAST_CALL and elapsed < MIN_INTERVAL_SECONDS:
        sleeper(MIN_INTERVAL_SECONDS - elapsed)
    _LAST_CALL = time.monotonic()


def reset_cache() -> None:
    """Test helper — the cache is process-local."""
    global _LAST_CALL
    _CACHE.clear()
    _LAST_CALL = 0.0
