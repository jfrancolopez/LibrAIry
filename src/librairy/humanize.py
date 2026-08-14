"""Numbers as a person reads them. One implementation, several callers.

That first sentence was not true when it was written. There were three copies
of `human_bytes` — this one, one in `web/access.py` and one in `web/commit.py`
— and they disagreed about the only interesting case: zero. Two said `0 B` and
this one said `unknown`, so the same number read differently depending on
which page you were on, and two test files asserted opposite things about the
same function name and both passed.
"""

from __future__ import annotations

from datetime import UTC, datetime


def human_ago(stamp: str | None, now: datetime | None = None) -> str:
    """"3 days ago", not "2026-08-11T02:16:11+00:00".

    Written for one question the Commit page has to answer: how long has this
    approval been sitting here? A timestamp answers it only after the reader
    does the arithmetic, and the whole point of showing it is that an approval
    made weeks ago deserves a second look before it moves files.

    Coarse on purpose. Nothing here needs "3 days, 4 hours", and a unit that
    keeps changing while you read the page is worse than one that does not.
    """
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    seconds = ((now or datetime.now(UTC)) - when).total_seconds()
    # A clock that disagrees with the database — a container restarted with a
    # bad time, a row written by another machine — should not produce "in -2
    # days". "Just now" is wrong by less and reads as an answer.
    if seconds < 60:
        return "just now"
    for limit, size, unit in (
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
        (31536000, 2592000, "month"),
    ):
        if seconds < limit:
            count = int(seconds // size)
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    count = int(seconds // 31536000)
    return f"{count} year{'' if count == 1 else 's'} ago"


def human_bytes(size: int | None) -> str:
    """"1.4 GB", not "1503238553". Sizes exist to be compared at a glance.

    **Zero is a size.** An empty file is 0 B, a remux saves 0 B, and a total
    of nothing is 0 B — all facts, and all of them were being reported as
    `unknown` by the `if not size` that treated zero and None alike. `unknown`
    is for the genuinely unknown: a size nobody recorded, or one that is not a
    number at all.

    A negative size is neither, so it reads as unknown rather than as a
    nonsense quantity.
    """
    if size is None:
        return "unknown"
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "unknown"
