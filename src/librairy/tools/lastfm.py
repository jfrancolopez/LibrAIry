"""Last.fm lookup: what genre is this music, according to everyone else.

Genre is the one music field embedded tags most often leave blank or fill with
something useless, and it is the first path component under the genre-first
template — so a missing genre sends an otherwise perfectly identified album to
`Music/General/`. Last.fm's community tags are the cheapest fix available.

Same shape as the other catalog clients: stdlib-only HTTP, short timeout,
polite delay, per-process cache, and None on any failure.

Community tags are noisy by nature ("albums i own", "seen live", the artist's
own name), so `_usable_tag` throws out the obvious non-genres rather than
trusting the top result blindly.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 0.25
TIMEOUT_SECONDS = 8
MIN_TAG_COUNT = 10
MAX_TAG_WORDS = 3

# Tags people apply constantly that describe the listener, not the music.
NON_GENRES = {
    "albums i own",
    "awesome",
    "beautiful",
    "best",
    "check out",
    "cool",
    "favorite",
    "favorites",
    "favourite",
    "favourites",
    "love",
    "loved",
    "music",
    "my music",
    "owned",
    "seen live",
    "spotify",
    "under 2000 listeners",
    "vinyl",
    "want to see live",
}

_CACHE: dict[str, str | None] = {}
_LAST_CALL = 0.0


def top_genre(
    artist: str,
    *,
    api_key: str,
    album: str = "",
    opener=urlopen,
    sleeper=time.sleep,
) -> str | None:
    """The most-applied usable tag for an album, falling back to the artist."""
    artist = artist.strip()
    if not artist or not api_key:
        return None
    cache_key = f"{artist}|{album}".lower()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    genre = None
    if album.strip():
        payload = _call(
            {"method": "album.getinfo", "artist": artist, "album": album.strip()},
            api_key,
            opener,
            sleeper,
        )
        genre = _best_tag(_dig(payload, "album", "tags", "tag"), artist)
    if genre is None:
        payload = _call({"method": "artist.gettoptags", "artist": artist}, api_key, opener, sleeper)
        genre = _best_tag(_dig(payload, "toptags", "tag"), artist)

    _CACHE[cache_key] = genre
    return genre


def lookup_for_settings(settings) -> Any:
    """Adapter matching classify/music.py's genre-lookup contract."""
    key = settings.lastfm_key.get_secret_value()
    if not key:
        return None

    def lookup(artist: str, album: str, _inner_settings) -> str | None:
        return top_genre(artist, api_key=key, album=album)

    return lookup


def _call(params: dict[str, str], api_key: str, opener, sleeper) -> Any:
    query = urlencode({**params, "api_key": api_key, "format": "json", "autocorrect": "1"})
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT}
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        return None


def _best_tag(tags: Any, artist: str) -> str | None:
    """Best tag that actually looks like a genre.

    `artist.gettoptags` returns a `count` per tag; `album.getinfo` returns the
    album's tags already ordered by relevance and with no counts at all. So the
    popularity floor applies only when counts are actually present — otherwise
    it would reject every album tag and silently discard the more specific
    answer of the two.
    """
    if not isinstance(tags, list):
        return None
    entries = [tag for tag in tags if isinstance(tag, dict)]
    counted = any(_count(tag) for tag in entries)
    if counted:
        entries.sort(key=_count, reverse=True)
    for tag in entries:
        name = str(tag.get("name") or "").strip()
        if not _usable_tag(name, artist):
            continue
        if counted and _count(tag) < MIN_TAG_COUNT:
            continue
        return name.title()
    return None


def _usable_tag(name: str, artist: str) -> bool:
    lowered = name.casefold()
    return bool(
        name
        and lowered not in NON_GENRES
        and lowered != artist.casefold()
        and len(name.split()) <= MAX_TAG_WORDS
        # Years and decades ("00s", "2011") say when, not what.
        and not lowered.rstrip("s").isdigit()
    )


def _count(tag: dict[str, Any]) -> int:
    try:
        return int(tag.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _dig(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


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
