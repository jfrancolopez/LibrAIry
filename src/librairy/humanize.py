"""Numbers as a person reads them. One implementation, several callers."""

from __future__ import annotations


def human_bytes(size: int | None) -> str:
    """"1.4 GB", not "1503238553". Sizes exist to be compared at a glance."""
    if not size or size < 0:
        return "unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "unknown"
