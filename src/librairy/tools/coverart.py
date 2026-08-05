"""Cover Art Archive: album art for review cards. Keyless.

Audio is the one category with nothing to look at — an image or a video gets a
real thumbnail from ffmpeg, a document gets its opening lines, and a song gets
a filename. The cover is what makes an album recognisable at a glance.

**Art is never written into the library.** v1 renames and moves; it does not
add files to your collection. Covers live in the appdata thumbnail cache
beside the generated ones, and are read-through: fetched once per release,
served from disk after that.

Same client shape as the other catalogs: stdlib-only HTTP, short timeout,
per-process negative cache, and None on any failure.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

FRONT_URL = "https://coverartarchive.org/release/{mbid}/front-250"
USER_AGENT = "LibrAIry/1.0 (+https://github.com/jfrancolopez/LibrAIry)"
TIMEOUT_SECONDS = 10
# A cover that big is not a 250px thumbnail; something is wrong with it.
MAX_BYTES = 2 * 1024 * 1024
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

LOGGER = logging.getLogger(__name__)

# Releases with no art are the common case, and re-asking on every page view
# would be rude to a free service and slow for the user.
_MISSING: set[str] = set()


def cover_path(appdata_dir: Path, release_mbid: str, *, opener=urlopen) -> Path | None:
    """Cached cover for a release MBID, fetching it once. None if there is none."""
    mbid = release_mbid.strip().lower()
    # The MBID goes straight into a filename and a URL, so it is validated as a
    # UUID rather than merely escaped.
    if not MBID.match(mbid) or mbid in _MISSING:
        return None

    thumbs = appdata_dir / "thumbs"
    target = thumbs / f"cover-{mbid}.jpg"
    if target.exists():
        return target

    data, permanent = _fetch(mbid, opener)
    if not data:
        # Only remember "there is no art here". A timeout or a 503 says nothing
        # about the release, and caching it would hide a cover that does exist
        # for as long as the process lives.
        if permanent:
            _MISSING.add(mbid)
        return None

    thumbs.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: an interrupted fetch must not leave a truncated image
    # in the cache to be served forever.
    partial = target.with_suffix(".jpg.part")
    partial.write_bytes(data)
    partial.replace(target)
    return target


def _fetch(mbid: str, opener) -> tuple[bytes | None, bool]:
    """`(image, answer_is_final)`. Final means asking again is pointless."""
    request = Request(  # noqa: S310 - fixed https host, mbid validated as a UUID
        FRONT_URL.format(mbid=mbid), headers={"User-Agent": USER_AGENT}
    )
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BYTES + 1)
    except HTTPError as exc:
        LOGGER.debug("no cover art for %s: HTTP %s", mbid, exc.code)
        return None, exc.code < 500
    except Exception as exc:  # noqa: BLE001 - transient; worth another try later
        LOGGER.debug("cover art fetch failed for %s: %s", mbid, exc)
        return None, False
    if not data or len(data) > MAX_BYTES:
        # A well-formed response that is not a usable thumbnail. Nothing here
        # will change on a retry.
        return None, True
    return data, True


def reset_cache() -> None:
    """Test helper — the negative cache is process-local."""
    _MISSING.clear()
