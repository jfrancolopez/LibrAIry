"""Open Library lookup: identify books by title/author. Keyless and free.

stdlib-only HTTP (no new dependencies), short timeout, polite delay between
calls, and an in-process cache so repeated titles in one batch cost one
request. Any failure returns None so classification degrades to heuristics —
a catalog is evidence, never a hard dependency.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SEARCH_URL = "https://openlibrary.org/search.json"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
MIN_INTERVAL_SECONDS = 1.0
TIMEOUT_SECONDS = 8

_CACHE: dict[str, BookMatch | None] = {}
_LAST_CALL = 0.0


@dataclass(frozen=True)
class BookMatch:
    title: str
    author: str | None
    year: int | None


def search_book(
    title: str,
    author: str | None = None,
    *,
    opener=urlopen,
    sleeper=time.sleep,
) -> BookMatch | None:
    """Best-effort single match for a cleaned title. None when unidentified."""
    query = title.strip()
    if not query:
        return None
    cache_key = f"{query}|{author or ''}".lower()
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    params = {"title": query, "limit": "1"}
    if author:
        params["author"] = author
    request = Request(  # noqa: S310 - fixed https host, params are url-encoded
        f"{SEARCH_URL}?{urlencode(params)}",
        headers={"User-Agent": USER_AGENT},
    )
    _throttle(sleeper)
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure degrades to heuristics
        _CACHE[cache_key] = None
        return None

    match = _first_match(payload)
    _CACHE[cache_key] = match
    return match


def _throttle(sleeper) -> None:
    global _LAST_CALL
    elapsed = time.monotonic() - _LAST_CALL
    if _LAST_CALL and elapsed < MIN_INTERVAL_SECONDS:
        sleeper(MIN_INTERVAL_SECONDS - elapsed)
    _LAST_CALL = time.monotonic()


def _first_match(payload: Any) -> BookMatch | None:
    docs = payload.get("docs") if isinstance(payload, dict) else None
    if not docs:
        return None
    doc = docs[0]
    title = str(doc.get("title") or "").strip()
    if not title:
        return None
    authors = doc.get("author_name") or []
    year = doc.get("first_publish_year")
    return BookMatch(
        title=title,
        author=str(authors[0]).strip() if authors else None,
        year=int(year) if isinstance(year, int) else None,
    )


def reset_cache() -> None:
    """Test helper — the cache is process-local and unbounded per batch."""
    global _LAST_CALL
    _CACHE.clear()
    _LAST_CALL = 0.0
