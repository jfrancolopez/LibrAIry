"""Appearance presets.

The palettes themselves live in `static/pipboy.css` as `[data-theme]` blocks.
This module holds the names the rest of the app validates against, plus the few
colors that have to be reproduced outside CSS (SVG thumbnails are generated in
Python and cannot read custom properties).
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THEME = "beige-box"


@dataclass(frozen=True)
class ThemeSwatch:
    """The subset of a palette needed to draw a thumbnail."""

    background: str
    border: str
    accent: str
    text: str


THEMES: dict[str, ThemeSwatch] = {
    "beige-box": ThemeSwatch("#d8d0c0", "#9a9182", "#145f5b", "#26241f"),
    "platinum-gray": ThemeSwatch("#d4d4d8", "#9a9aa2", "#234a7d", "#1b1b1d"),
    "crt-amber": ThemeSwatch("#171310", "#8a6a1f", "#ffd479", "#ffb000"),
    "dos-blue": ThemeSwatch("#0000a8", "#7c7cff", "#ffff55", "#ffffff"),
    "vaporwave": ThemeSwatch("#1a1033", "#7d55c7", "#ff6ec7", "#f2e9ff"),
    "pipboy-green": ThemeSwatch("#061109", "#56d364", "#ffbf4d", "#7cff6b"),
}

THEME_NAMES: tuple[str, ...] = tuple(THEMES)


def normalize_theme(name: str | None) -> str:
    """Fall back to the default rather than rendering an unstyled page."""
    return name if name in THEMES else DEFAULT_THEME


def swatch_for(name: str | None) -> ThemeSwatch:
    return THEMES[normalize_theme(name)]


def normalize_background(value: str | None) -> str:
    """Accept only `#rgb`/`#rrggbb`; anything else means "use the theme default"."""
    if not value:
        return ""
    candidate = value.strip()
    if not candidate.startswith("#"):
        return ""
    digits = candidate[1:]
    if len(digits) not in {3, 6} or not all(char in "0123456789abcdefABCDEF" for char in digits):
        return ""
    return "#" + digits.lower()
