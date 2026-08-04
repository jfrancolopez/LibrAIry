"""AcoustID lookup: identify audio by acoustic fingerprint. Free key.

This is the last resort for music, not the first: embedded ID3/Vorbis tags are
keyless, offline, and stronger, so `classify_music` only reaches here when a
file carries no usable tags. What it produces is a MusicBrainz recording ID,
which `tools/musicbrainz.py` then resolves into artist/album/title.

Same shape as the Open Library and TMDB clients: stdlib-only HTTP, short
timeout, polite delay, per-process cache, and None on any failure so
classification degrades to heuristics.

Privacy: only the fingerprint and duration leave the machine. Not the filename,
not the path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from librairy.config import Settings

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 0.34  # AcoustID asks for no more than 3 requests/second
TIMEOUT_SECONDS = 8

_CACHE: dict[str, dict[str, Any] | None] = {}
_LAST_CALL = 0.0


def lookup(
    fingerprint: str,
    duration: int,
    *,
    api_key: str,
    opener=urlopen,
    sleeper=time.sleep,
) -> dict[str, Any] | None:
    """Best-effort match as `{"score": float, "recording_id": str}`.

    None when unconfigured, unidentified, or when the match carries no
    MusicBrainz recording — a score with nothing to resolve is not usable.
    """
    if not fingerprint or not api_key or duration <= 0:
        return None
    cache_key = f"{duration}|{fingerprint}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {
        "client": api_key,
        "duration": str(duration),
        "fingerprint": fingerprint,
        "meta": "recordings",
    }
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{LOOKUP_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[cache_key] = None
        return None

    match = _best_match(payload)
    _CACHE[cache_key] = match
    return match


def lookup_for_settings(settings: Settings) -> Any:
    """Adapter matching classify/music.py's AcoustidLookup contract.

    Returns None when no key is set, which is also how `classify_music` decides
    not to fingerprint at all — running fpcalc over a whole inbox is expensive
    and pointless without somewhere to send the result.
    """
    key = settings.acoustid_key.get_secret_value()
    if not key:
        return None

    def lookup_relpath(relpath: str, inner_settings: Settings) -> dict[str, Any] | None:
        path = Path(inner_settings.inbox_dir) / relpath
        printed = _fingerprint_file(path, inner_settings)
        if printed is None:
            return None
        return lookup(printed[1], printed[0], api_key=key)

    return lookup_relpath


def _fingerprint_file(path: Path, settings: Settings) -> tuple[int, str] | None:
    """(duration, fingerprint) from fpcalc, or None if it cannot be computed."""
    try:
        from librairy.tools.fpcalc import fingerprint as run_fpcalc

        result = run_fpcalc(path, settings)
    except Exception:  # noqa: BLE001 - fingerprinting is best-effort
        return None
    if not result.ok or not isinstance(result.data, dict):
        return None
    duration = result.data.get("duration")
    printed = result.data.get("fingerprint")
    if not isinstance(duration, int) or not printed:
        return None
    return duration, str(printed)


def _best_match(payload: Any) -> dict[str, Any] | None:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not results:
        return None
    for result in results:
        recordings = result.get("recordings") or []
        if not recordings:
            continue
        recording_id = recordings[0].get("id")
        if recording_id:
            return {"score": float(result.get("score") or 0.0), "recording_id": str(recording_id)}
    return None


def reset_cache() -> None:
    """Test helper — the cache is process-local."""
    global _LAST_CALL
    _CACHE.clear()
    _LAST_CALL = 0.0


def _throttle(sleeper) -> None:
    global _LAST_CALL
    elapsed = time.monotonic() - _LAST_CALL
    if _LAST_CALL and elapsed < MIN_INTERVAL_SECONDS:
        sleeper(MIN_INTERVAL_SECONDS - elapsed)
    _LAST_CALL = time.monotonic()
