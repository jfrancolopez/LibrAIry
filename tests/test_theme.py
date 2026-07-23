from __future__ import annotations

import re
from pathlib import Path

import pytest

from librairy.web.theme import (
    DEFAULT_THEME,
    THEME_NAMES,
    normalize_background,
    normalize_theme,
    swatch_for,
)

CSS = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
BODY_TEXT_MINIMUM = 4.5
LARGE_TEXT_MINIMUM = 3.0


def _blocks() -> dict[str, dict[str, str]]:
    blocks: dict[str, dict[str, str]] = {}
    for match in re.finditer(r'(:root|\[data-theme="([^"]+)"\])\s*\{([^}]*)\}', CSS):
        name = match.group(2) or ":root"
        tokens = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", match.group(3)))
        blocks[name] = {key: value.strip() for key, value in tokens.items()}
    return blocks


def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(color: tuple[float, float, float]) -> float:
    red, green, blue = (_channel(part / 255) for part in color)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _parse(value: str, backdrop: tuple[float, float, float]) -> tuple[float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    rgba = re.match(r"rgba?\(([^)]+)\)", value)
    if rgba is None:
        raise ValueError(f"unparseable color: {value}")
    parts = [float(part) for part in rgba.group(1).split(",")]
    alpha = parts[3] if len(parts) > 3 else 1.0
    return tuple(  # type: ignore[return-value]
        parts[index] * alpha + backdrop[index] * (1 - alpha) for index in range(3)
    )


def contrast(foreground: str, background: str, backdrop: str) -> float:
    base = _parse(backdrop, (0, 0, 0))
    back = _parse(background, base)
    fore = _parse(foreground, back)
    lighter, darker = sorted((_luminance(fore), _luminance(back)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_every_preset_is_defined_in_css_and_python() -> None:
    blocks = _blocks()

    assert set(THEME_NAMES) <= set(blocks)
    assert DEFAULT_THEME in THEME_NAMES
    assert len(THEME_NAMES) == 6


@pytest.mark.parametrize("theme", THEME_NAMES)
def test_body_text_meets_wcag_aa_on_background_and_panel(theme: str) -> None:
    tokens = _blocks()[theme]
    surfaces = (tokens["--bg"], tokens["--bg-panel"], tokens["--bg-input"])

    for surface in surfaces:
        assert contrast(tokens["--text"], surface, tokens["--bg"]) >= BODY_TEXT_MINIMUM
        assert contrast(tokens["--text-dim"], surface, tokens["--bg"]) >= BODY_TEXT_MINIMUM


@pytest.mark.parametrize("theme", THEME_NAMES)
def test_accents_and_status_colors_stay_distinguishable(theme: str) -> None:
    tokens = _blocks()[theme]

    for token in ("--accent", "--ok", "--warn", "--fail"):
        for surface in (tokens["--bg"], tokens["--bg-panel"]):
            ratio = contrast(tokens[token], surface, tokens["--bg"])
            assert ratio >= LARGE_TEXT_MINIMUM, f"{theme} {token} on {surface} is {ratio:.2f}"


def test_no_color_literals_outside_theme_blocks() -> None:
    without_blocks = re.sub(r'(:root|\[data-theme="[^"]+"\])\s*\{[^}]*\}', "", CSS)

    assert "#" not in re.sub(r"/\*.*?\*/", "", without_blocks, flags=re.S)
    assert "rgba(" not in without_blocks


def test_pipboy_preset_reproduces_the_v1_palette() -> None:
    tokens = _blocks()["pipboy-green"]

    assert tokens["--bg"] == "#061109"
    assert tokens["--text"] == "#7cff6b"
    assert tokens["--accent"] == "#ffbf4d"
    assert tokens["--border"] == "#56d364"


def test_unknown_theme_and_background_fall_back_to_defaults() -> None:
    assert normalize_theme("not-a-theme") == DEFAULT_THEME
    assert normalize_theme(None) == DEFAULT_THEME
    assert normalize_theme("crt-amber") == "crt-amber"
    assert swatch_for("nope").background == swatch_for(DEFAULT_THEME).background
    assert normalize_background("javascript:alert(1)") == ""
    assert normalize_background("#ABC") == "#abc"
    assert normalize_background("#12345g") == ""
    assert normalize_background(None) == ""
