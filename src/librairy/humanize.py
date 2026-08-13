"""Numbers as a person reads them. One implementation, several callers.

That first sentence was not true when it was written. There were three copies
of `human_bytes` — this one, one in `web/access.py` and one in `web/commit.py`
— and they disagreed about the only interesting case: zero. Two said `0 B` and
this one said `unknown`, so the same number read differently depending on
which page you were on, and two test files asserted opposite things about the
same function name and both passed.
"""

from __future__ import annotations


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
