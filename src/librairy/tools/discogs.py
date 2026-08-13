"""Discogs lookup: identify a release from an untagged filename.

This is the last resort for audio, reached only when a file has no usable
embedded tags and AcoustID/MusicBrainz could not fingerprint it. All that is
left at that point is the filename, so this is a text search — much weaker
evidence than a fingerprint, and treated as such.

To keep a text search honest, `search_release` **verifies** its hit: the
release's artist must actually appear in the text that was searched for.
Without that check, `track01.mp3` would confidently become whatever Discogs
happened to list first, which is worse than admitting the file is unknown.

The personal token goes in an Authorization header, not the query string —
tokens do not belong in URLs, which end up in logs and proxies.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SEARCH_URL = "https://api.discogs.com/database/search"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 1.0  # Discogs allows 60 authenticated requests a minute.
TIMEOUT_SECONDS = 8

# Discogs disambiguates same-named artists with a numeric suffix: "Nirvana (2)".
ARTIST_SUFFIX = re.compile(r"\s*\(\d+\)$")

_CACHE: dict[str, dict[str, Any] | None] = {}
_LAST_CALL = 0.0


def search_release(
    query: str,
    *,
    token: str,
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, Any] | None:
    """Normalised `{artist, album, year, genre}`, or None if unverifiable."""
    query = query.strip()
    if not query or not token:
        return None
    cache_key = query.casefold()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = urlencode({"q": query, "type": "release", "per_page": "5"})
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{SEARCH_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[cache_key] = None
        return None

    results = payload.get("results") if isinstance(payload, dict) else None
    match = _first_verified(results, query)
    _CACHE[cache_key] = match
    return match


def search_compilation(
    title: str,
    *,
    token: str,
    barcode: str = "",
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, Any] | None:
    """A release identified by barcode or exact title, with no artist to help.

    The same problem `musicbrainz.search_compilation` solves, and the same two
    questions, because a compilation has no performer to search by. Discogs is
    worth asking separately rather than only as a fallback: its coverage of
    reissues, regional pressings and label compilations is better than
    MusicBrainz's, which is exactly the population a "V.A." folder is drawn
    from.

    `_first_verified` cannot be reused here. It requires the artist to appear
    in the searched text, which is the right check for an untagged filename
    and the wrong one for a release whose artist is thirty people. The title
    has to carry the verification instead, so it is compared exactly, case
    aside, against what was asked for.
    """
    title = title.strip()
    if not token or (not title and not barcode):
        return None
    cache_key = f"compilation|{barcode}|{title}".casefold()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    found = None
    if barcode.strip():
        found = _release_query(
            {"barcode": barcode.strip(), "type": "release"}, token, opener, sleeper
        )
    if found is None and title:
        found = _release_query(
            {"release_title": title, "type": "release", "per_page": "5"},
            token,
            opener,
            sleeper,
            wanted=title,
        )
    _CACHE[cache_key] = found
    return found


def _release_query(
    params: dict[str, str], token: str, opener, sleeper, *, wanted: str = ""
) -> dict[str, Any] | None:
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{SEARCH_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a catalog outage is not an audit failure
        return None
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict) or not result.get("id"):
            continue
        artist, album = _split_title(str(result.get("title") or ""))
        # A compilation's Discogs title is often just the release name, with
        # no "Artist - " prefix to split off. Both shapes are accepted.
        album = album or str(result.get("title") or "").strip()
        if wanted and album.casefold() != wanted.casefold():
            continue
        return {
            "id": str(result["id"]),
            "title": album,
            "artist": artist or "Various",
            "year": _year(result.get("year")),
            "genre": _genre(result),
        }
    return None


def lookup_for_settings(settings) -> Any:
    """Adapter matching classify/music.py's release-lookup contract."""
    token = settings.discogs_token.get_secret_value()
    if not token:
        return None

    def lookup(query: str, _inner_settings) -> dict[str, Any] | None:
        return search_release(query, token=token)

    return lookup


def _first_verified(results: Any, query: str) -> dict[str, Any] | None:
    if not isinstance(results, list):
        return None
    haystack = _normalise(query)
    for result in results:
        if not isinstance(result, dict):
            continue
        artist, album = _split_title(str(result.get("title") or ""))
        if not artist or not album:
            continue
        if _normalise(artist) not in haystack:
            continue
        return {
            "artist": artist,
            "album": album,
            "year": _year(result.get("year")),
            "genre": _genre(result),
        }
    return None


def _split_title(title: str) -> tuple[str, str]:
    """Discogs formats release titles as "Artist - Album"."""
    artist, separator, album = title.partition(" - ")
    if not separator:
        return "", ""
    return ARTIST_SUFFIX.sub("", artist).strip(), album.strip()


def _genre(result: dict[str, Any]) -> str:
    # Style is the specific one ("Shoegaze"), genre the broad one ("Rock").
    for field in ("style", "genre"):
        values = result.get(field)
        if isinstance(values, list) and values:
            return str(values[0])
    return ""


def _year(value: Any) -> int:
    text = str(value or "")[:4]
    return int(text) if text.isdigit() else 0


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


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
