"""MusicBrainz lookup: resolve a recording ID into artist/album/title. Keyless.

The second half of the fingerprint path — AcoustID says *which* recording a
file is, this says what that recording is called. Same client shape as the
other catalogs: stdlib-only HTTP, short timeout, per-process cache, and None on
any failure.

Rate limiting is deliberately NOT done here. MusicBrainz allows one request per
second and `classify_music._rate_limited_musicbrainz_lookup` already enforces
`settings.mb_rate_limit` around every call; throttling in both places would
sleep twice per lookup for no benefit. Any other caller has to bring its own.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RECORDING_URL = "https://musicbrainz.org/ws/2/recording"
# MusicBrainz rejects requests without a contactable User-Agent.
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
TIMEOUT_SECONDS = 8

_CACHE: dict[str, dict[str, Any] | None] = {}


def lookup_recording(mbid: str, *, opener=urlopen) -> dict[str, Any] | None:
    """Fields for one recording MBID, shaped for classify/music.py.

    Returns `{"artist", "album", "title", "year", "track"}`. None when
    unidentified or unreachable.
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


def reset_cache() -> None:
    """Test helper — the cache is process-local."""
    _CACHE.clear()
