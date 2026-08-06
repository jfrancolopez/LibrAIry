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
    "vaporwave": ThemeSwatch("#1a1033", "#7d55c7", "#e07fb8", "#f2e9ff"),
    "dracula": ThemeSwatch("#282a36", "#6272a4", "#bd93f9", "#f8f8f2"),
    "pipboy-green": ThemeSwatch("#061109", "#56d364", "#ffbf4d", "#7cff6b"),
    # Palettes chosen for comfort rather than period accuracy — see the
    # "Low-strain palettes" block in pipboy.css.
    "solarized-dark": ThemeSwatch("#002b36", "#586e75", "#2aa198", "#eee8d5"),
    "solarized-light": ThemeSwatch("#fdf6e3", "#b7ad94", "#1a6f70", "#073642"),
    "nord": ThemeSwatch("#2e3440", "#4c566a", "#88c0d0", "#eceff4"),
    "gruvbox-warm": ThemeSwatch("#282828", "#665c54", "#fabd2f", "#ebdbb2"),
}

THEME_NAMES: tuple[str, ...] = tuple(THEMES)

# Palettes designed for long sessions rather than for looking like something.
# Grouped separately in Settings because the two halves answer different
# questions: which era do I want, versus which can I stare at for an hour.
COMFORT_THEMES: frozenset[str] = frozenset(
    {"solarized-dark", "solarized-light", "nord", "gruvbox-warm", "platinum-gray"}
)
THEME_LABELS: dict[str, str] = {
    "beige-box": "Beige Box — 90s desktop",
    "platinum-gray": "Platinum Gray — quiet and neutral",
    "crt-amber": "CRT Amber — amber phosphor",
    "dos-blue": "DOS Blue — high contrast, loud",
    "vaporwave": "Vaporwave — purple and pink",
    "dracula": "Dracula — the editor palette",
    "pipboy-green": "Pip-Boy Green — glowing phosphor",
    "solarized-dark": "Solarized Dark — low glare",
    "solarized-light": "Solarized Light — low glare, bright room",
    "nord": "Nord — cool and desaturated",
    "gruvbox-warm": "Gruvbox — warm, less blue light",
}


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
