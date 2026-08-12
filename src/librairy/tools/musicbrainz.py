"""MusicBrainz lookup: resolve a recording ID into artist/album/title. Keyless.

The second half of the fingerprint path — AcoustID says *which* recording a
file is, this says what that recording is called. Same client shape as the
other catalogs: stdlib-only HTTP, short timeout, per-process cache, and None on
any failure.

The two entry points throttle differently, on purpose. `lookup_recording` does
not: its only caller is `classify_music._rate_limited_musicbrainz_lookup`,
which already enforces `settings.mb_rate_limit`, and throttling in both places
would sleep twice per lookup. `search_release` does, because its caller is the
preview page, which has no rate limiter at all.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
RELEASE_URL = "https://musicbrainz.org/ws/2/release"
# MusicBrainz rejects requests without a contactable User-Agent.
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
TIMEOUT_SECONDS = 8
# MusicBrainz allows one request a second; going faster earns a 503.
MIN_INTERVAL_SECONDS = 1.1

_CACHE: dict[str, dict[str, Any] | None] = {}
_LAST_SEARCH = 0.0


def lookup_recording(mbid: str, *, opener=urlopen) -> dict[str, Any] | None:
    """Fields for one recording MBID, shaped for classify/music.py.

    Returns `{"artist", "album", "title", "year", "track", "release_id"}`.
    None when unidentified or unreachable.
    """
    mbid = mbid.strip()
    if not mbid:
        return None
    if mbid in _CACHE:
        return _CACHE[mbid]

    params = {"inc": "artists+releases", "fmt": "json"}
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{RECORDING_URL}/{mbid}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[mbid] = None
        return None

    fields = _fields(payload)
    _CACHE[mbid] = fields
    return fields


def search_release(artist: str, album: str, *, opener=urlopen, sleeper=time.sleep) -> str | None:
    """Release MBID for an artist/album pair, for files identified by their tags.

    Only the fingerprint path leaves a release MBID in the evidence, and most
    music in a real library is tagged rather than fingerprinted — so without
    this, cover art would be missing for nearly everything.

    Unlike `lookup_recording`, this one **does** throttle itself. Its caller is
    the preview page, which has no rate limiter of its own; opening two album
    previews in quick succession earned a 503 from MusicBrainz the first time
    this was tried against the live service.
    """
    found = search_release_detail(artist, album, opener=opener, sleeper=sleeper)
    return found["id"] if found else None


def search_release_detail(
    artist: str, album: str, *, opener=urlopen, sleeper=time.sleep
) -> dict[str, str] | None:
    """The same search, keeping the names as well as the id.

    The audit needs the canonical spelling, not only the identifier: a folder
    called `Unpluged` is worth mentioning precisely because MusicBrainz and
    the embedded tags both spell it `Unplugged`. `search_release` is the
    id-only view of this, unchanged for its existing callers, and both share
    one request and one cache entry.
    """
    artist, album = artist.strip(), album.strip()
    if not artist or not album:
        return None
    cache_key = f"search|{artist}|{album}".casefold()
    if cache_key in _CACHE:
        cached = _CACHE[cache_key]
        return dict(cached) if cached else None

    query = f'artist:"{_escape(artist)}" AND release:"{_escape(album)}"'
    params = {"query": query, "fmt": "json", "limit": "1"}
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{RELEASE_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # 404/400 mean this album is not there and asking again will not help.
        # 503 means "you are going too fast" — caching that would blacklist a
        # perfectly findable album for the life of the process.
        if exc.code < 500:
            _CACHE[cache_key] = None
        return None
    except Exception:  # noqa: BLE001 - transient; degrade without poisoning the cache
        return None

    releases = payload.get("releases") if isinstance(payload, dict) else None
    first = releases[0] if isinstance(releases, list) and releases else None
    if not isinstance(first, dict) or not first.get("id"):
        _CACHE[cache_key] = None
        return None
    credit = first.get("artist-credit") or []
    named = credit[0] if isinstance(credit, list) and credit else {}
    named = named if isinstance(named, dict) else {}
    inner = named.get("artist") if isinstance(named.get("artist"), dict) else {}
    found = {
        "id": str(first["id"]),
        "title": str(first.get("title") or "").strip(),
        "artist": str(named.get("name") or inner.get("name") or "").strip(),
        "artist_id": str(inner.get("id") or ""),
    }
    _CACHE[cache_key] = found
    return dict(found)


def _escape(value: str) -> str:
    """Lucene syntax: quotes and backslashes would break out of the term."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def lookup_for_settings(_settings) -> Any:
    """Adapter matching classify/music.py's MusicBrainzLookup contract."""

    def lookup(mbid: str, _inner_settings) -> dict[str, Any] | None:
        return lookup_recording(mbid)

    return lookup


def _fields(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    releases = payload.get("releases") or []
    # Earliest release is the original album rather than a later compilation.
    release = min(releases, key=_release_sort_key) if releases else {}
    return {
        "artist": _artist(payload),
        "album": str(release.get("title") or "").strip() or "Singles",
        "title": title,
        "year": _year(release.get("date")),
        "track": 0,
        # Cover Art Archive is keyed by release MBID, so this is what makes
        # album art reachable later. Not a path component — see classify/music.
        "release_id": str(release.get("id") or ""),
    }


def _release_sort_key(release: Any) -> str:
    date = str(release.get("date") or "") if isinstance(release, dict) else ""
    # Undated releases sort last, so a dated original always wins.
    return date or "9999"


def _artist(payload: dict[str, Any]) -> str:
    credits = payload.get("artist-credit") or []
    names = []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or (credit.get("artist") or {}).get("name")
            if name:
                names.append(str(name))
    return " & ".join(names) if names else "Unknown Artist"


def _year(date: Any) -> int:
    text = str(date or "")[:4]
    return int(text) if text.isdigit() else 0


def _throttle(sleeper) -> None:
    """Only `search_release` uses this — see its docstring for why."""
    global _LAST_SEARCH
    elapsed = time.monotonic() - _LAST_SEARCH
    if _LAST_SEARCH and elapsed < MIN_INTERVAL_SECONDS:
        sleeper(MIN_INTERVAL_SECONDS - elapsed)
    _LAST_SEARCH = time.monotonic()


def reset_cache() -> None:
    """Test helper — the cache is process-local."""
    global _LAST_SEARCH
    _CACHE.clear()
    _LAST_SEARCH = 0.0
