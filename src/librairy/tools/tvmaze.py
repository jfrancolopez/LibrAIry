"""TVmaze lookup: identify TV shows and name individual episodes. No key.

Same shape as the Open Library and TMDB clients: stdlib-only HTTP, short
timeout, polite delay, per-process cache, and None on any failure so
classification degrades to the next evidence source.

TVmaze complements TMDB rather than duplicating it. TMDB's `search/tv`
identifies the *show*; TVmaze additionally answers "what is season 3
episode 7 called?", which is what turns `S03E07.mkv` into something a
person can read on a shelf.

Results are normalised into the dict shape `classify/video.py` already reads
from TMDB (`name`, `genres`, `first_air_date`) plus `episode_name`, so the
classifier does not need to know which catalog answered.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SEARCH_URL = "https://api.tvmaze.com/search/shows"
EPISODE_URL = "https://api.tvmaze.com/shows/{show_id}/episodebynumber"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 0.5
TIMEOUT_SECONDS = 8
# Between the worst real match measured (0.890) and the best junk one (0.531).
MIN_SCORE = 0.7

_CACHE: dict[str, dict[str, Any] | None] = {}
_LAST_CALL = 0.0


def search_show(
    query: str,
    *,
    season: int | None = None,
    episode: int | None = None,
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, Any] | None:
    """Best-effort show match, with the episode title when season/episode given.

    None when unidentified. A missing episode never invalidates a found show —
    plenty of releases number episodes differently from the broadcast order.
    """
    query = query.strip()
    if not query:
        return None
    cache_key = f"{query}|{season or ''}|{episode or ''}".lower()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    results = _get(f"{SEARCH_URL}?{urlencode({'q': query})}", opener, sleeper)
    show = _best_show(results)
    if show is None:
        _CACHE[cache_key] = None
        return None

    result: dict[str, Any] = {
        "name": str(show["name"]),
        "genres": list(show.get("genres") or []),
        "first_air_date": str(show.get("premiered") or ""),
        "tvmaze_id": show.get("id"),
    }
    if season is not None and episode is not None and show.get("id") is not None:
        url = EPISODE_URL.format(show_id=show["id"])
        params = urlencode({"season": season, "number": episode})
        found = _get(f"{url}?{params}", opener, sleeper)
        if isinstance(found, dict) and found.get("name"):
            result["episode_name"] = str(found["name"])

    _CACHE[cache_key] = result
    return result


def lookup_for_settings(_settings) -> Any:
    """Adapter matching classify/video.py's lookup contract. Keyless, so this
    never returns None for want of configuration — the toggle is the only gate."""

    def lookup(parsed, _inner_settings):  # noqa: ANN001
        return search_show(parsed.title, season=parsed.season, episode=parsed.episode)

    return lookup


def _best_show(results: Any) -> dict[str, Any] | None:
    """Top hit, but only if it is actually a match.

    `/search/shows` returns a relevance score; `singlesearch/shows` returns the
    same top hit with the score hidden, and therefore always says yes. Measured
    against real filenames, genuine titles score 0.89 and up (a bare
    "Chernobyl" is the floor) while junk lands at 0.53 and below — a made-up
    "Test Show" confidently resolved to a real series called "Best Shot" at
    0.41. Renaming a file after a show it has nothing to do with is worse than
    leaving it alone.
    """
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        show = result.get("show")
        try:
            score = float(result.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        if score >= MIN_SCORE and isinstance(show, dict) and show.get("name"):
            return show
    return None


def _get(url: str, opener, sleeper) -> Any:
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        return None


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
