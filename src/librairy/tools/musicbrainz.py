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
    payload = _recording_payload(mbid, opener=opener)
    return _fields(payload) if payload is not None else None


def _recording_payload(mbid: str, *, opener=urlopen) -> dict[str, Any] | None:
    """The raw recording response, cached, shared by both readings of it."""
    mbid = mbid.strip()
    if not mbid:
        return None
    cache_key = f"recording|{mbid}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {"inc": "artists+releases", "fmt": "json"}
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{RECORDING_URL}/{mbid}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[cache_key] = None
        return None
    payload = payload if isinstance(payload, dict) else None
    _CACHE[cache_key] = payload
    return payload


def recording_detail(mbid: str, *, opener=urlopen) -> dict[str, Any] | None:
    """The same recording lookup, keeping every release rather than one.

    `lookup_recording` flattens to the earliest release because a proposal
    needs one album name. Identifying a loose track is the opposite problem:
    the recording is on the original album, on a greatest-hits and on a
    remaster, and choosing between those is the person's decision — so the
    caller gets all of them, in the order MusicBrainz lists them, with the
    dates that let a reader tell them apart.

    One request and one cache entry, shared with `lookup_recording`, so
    identifying a track and classifying it do not ask twice.
    """
    payload = _recording_payload(mbid, opener=opener)
    if payload is None:
        return None
    title = str(payload.get("title") or "").strip()
    if not title:
        return None
    return {
        "recording_id": str(payload.get("id") or mbid),
        "title": title,
        "artist": _artist(payload),
        "artist_id": _artist_id(payload),
        "releases": [
            _release_summary(release)
            for release in (payload.get("releases") or [])
            if isinstance(release, dict) and release.get("id")
        ],
    }


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    """One release a recording appears on, in the words a person would read.

    `kind` comes from the release group when MusicBrainz sends one — `Album`,
    `Compilation`, `Live` — and is empty when it does not. Empty is honest;
    guessing "album" would turn a greatest-hits into an original release on
    the page.
    """
    group = release.get("release-group")
    group = group if isinstance(group, dict) else {}
    secondary = group.get("secondary-types") or []
    kind = str(group.get("primary-type") or "")
    if isinstance(secondary, list) and secondary:
        kind = str(secondary[0] or "") or kind
    return {
        "id": str(release["id"]),
        "title": str(release.get("title") or "").strip(),
        "group_id": str(group.get("id") or ""),
        "year": _year(release.get("date")),
        "kind": kind,
    }


def _artist_id(payload: dict[str, Any]) -> str:
    for credit in payload.get("artist-credit") or []:
        if isinstance(credit, dict):
            inner = credit.get("artist")
            if isinstance(inner, dict) and inner.get("id"):
                return str(inner["id"])
    return ""


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


def search_compilation(
    title: str,
    *,
    barcode: str = "",
    track_count: int = 0,
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, str] | None:
    """A release identified without an artist to search by.

    The artist search cannot be used here, and the reason is the whole point:
    a compilation's `album_artist` is `V.A.`, and asking MusicBrainz for
    releases by an artist called "V.A." returns whatever happens to be named
    that. The audit used to skip every one of these — twenty-seven folders of
    the real library went unchecked because nobody has a folder called V.A.

    So it asks the questions that actually identify a release:

    * **the barcode**, when the tags carry one. A UPC is the closest thing to
      a primary key a physical or digital release has, and a barcode hit needs
      no verification at all.
    * **the title**, verified against the track count. A title search alone is
      weak — MusicBrainz scores loosely and will happily return `Road Trip
      Classics` when asked for `Best Road Trip Disco Fever Classics` — so a
      hit is only accepted when the title matches exactly, case aside, and the
      release holds the number of tracks that are actually on disk.
    """
    title = title.strip()
    if not title and not barcode:
        return None
    cache_key = f"compilation|{barcode}|{title}|{track_count}".casefold()
    if cache_key in _CACHE:
        cached = _CACHE[cache_key]
        return dict(cached) if cached else None

    found = None
    if barcode.strip():
        found = _release_query(f"barcode:{_escape(barcode.strip())}", opener, sleeper)
    if found is None and title:
        candidate = _release_query(
            f'release:"{_escape(title)}"', opener, sleeper, limit=5, wanted=title
        )
        if candidate and _tracks_agree(candidate, track_count):
            found = candidate
    _CACHE[cache_key] = found
    return dict(found) if found else None


def _tracks_agree(candidate: dict[str, str], track_count: int) -> bool:
    """No count on either side is not a disagreement; two counts that differ is."""
    if not track_count or not candidate.get("track_count"):
        return True
    return int(candidate["track_count"]) == track_count


def _release_query(
    query: str, opener, sleeper, *, limit: int = 1, wanted: str = ""
) -> dict[str, str] | None:
    """One search, one parsed release. `wanted` requires an exact title, case
    aside — MusicBrainz's relevance score is not a match threshold."""
    params = {"query": query, "fmt": "json", "limit": str(limit)}
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{RELEASE_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - a catalog outage is not an audit failure
        return None
    releases = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(releases, list):
        return None
    for release in releases:
        if not isinstance(release, dict) or not release.get("id"):
            continue
        found = _release_fields(release)
        if wanted and found["title"].casefold() != wanted.casefold():
            continue
        return found
    return None


def _release_fields(release: dict[str, Any]) -> dict[str, str]:
    credit = release.get("artist-credit") or []
    named = credit[0] if isinstance(credit, list) and credit else {}
    named = named if isinstance(named, dict) else {}
    inner = named.get("artist") if isinstance(named.get("artist"), dict) else {}
    count = release.get("track-count")
    return {
        "id": str(release["id"]),
        "title": str(release.get("title") or "").strip(),
        "artist": str(named.get("name") or inner.get("name") or "").strip(),
        "artist_id": str(inner.get("id") or ""),
        "track_count": str(count) if isinstance(count, int) else "",
    }


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
