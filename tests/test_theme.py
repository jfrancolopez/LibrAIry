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
    assert len(THEME_NAMES) == 11


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
    without_comments = re.sub(r"/\*.*?\*/", "", without_blocks, flags=re.S)

    # Hex color literals only (ignore `#id` selectors, which contain a letter run
    # after the `#` rather than 3/6/8 hex digits followed by a boundary).
    assert not re.search(r"#[0-9a-fA-F]{3}\b|#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{8}\b", without_comments)
    assert "rgba(" not in without_comments
    assert "hsl(" not in without_comments


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


def test_dracula_matches_the_editor_palette() -> None:
    tokens = _blocks()["dracula"]

    assert tokens["--bg"] == "#282a36"
    assert tokens["--text"] == "#f8f8f2"
    assert tokens["--accent"] == "#bd93f9"
    assert tokens["--ok"] == "#50fa7b"
    # The official #6272a4 comment colour is only ~3.2:1 on this ground and
    # fails AA for body text, so text-dim is deliberately lifted off it.
    assert tokens["--text-dim"] != "#6272a4"


@pytest.mark.parametrize("theme", THEME_NAMES)
def test_status_colours_are_told_apart_from_each_other(theme: str) -> None:
    """The health bars rely on ok/warn/fail reading as three different things.

    crt-amber used to set --ok and --warn to shades of the same amber, which is
    faithful to a monochrome CRT and useless in a chart.
    """
    tokens = _blocks()[theme]
    statuses = {token: tokens[token] for token in ("--ok", "--warn", "--fail")}

    assert len(set(statuses.values())) == 3, f"{theme} reuses a colour: {statuses}"


def test_type_scale_is_not_leaked_by_descendant_selectors() -> None:
    """`.metric strong` set every nested <strong> to 2.1rem — the size of the
    page's own h1. `.metric` is a one-big-number tile in Browse and a plain
    section wrapper everywhere else, so "Cost:" in a Settings catalog card, and
    most bold words on Health, rendered as headlines.

    The rule for any selector that sizes a bare element: bind it to a direct
    child, or give the thing a class of its own.
    """
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
    oversized = re.findall(
        r"^\s*([^{}\n]*\b(?:strong|em|b|small|code)\b[^{}\n]*)\{[^}]*font-size[^}]*\}",
        css,
        re.MULTILINE,
    )
    leaky = [
        selector.strip()
        for selector in oversized
        # "a > strong" is bound to its parent; "a strong" reaches the whole tree.
        if re.search(r"[.\w\]]\s+(?:strong|em|b|small|code)\b", selector)
    ]

    assert leaky == [], f"font-size on a descendant selector: {leaky}"


def test_every_heading_level_has_a_size_from_the_scale() -> None:
    """h4 had no rule, so it fell back to a browser default that sat at an
    arbitrary size between h3 and body text."""
    css = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")

    for level in ("h1", "h2", "h3", "h4"):
        # A level can be styled by more than one rule (the shared
        # "h1, h2, h3 { line-height }" plus its own size); any of them may
        # carry the size, so check them together.
        blocks = re.findall(
            rf"^[^{{}}\n]*\b{level}\b[^{{}}\n]*{{([^}}]*)}}", css, re.MULTILINE
        )
        assert blocks, f"{level} has no rule of its own"
        sized = [body for body in blocks if "font-size" in body]
        assert sized, f"{level} never gets a font-size"
        assert all("var(--text-" in body for body in sized), (
            f"{level} sets a font-size outside the type scale"
        )


def test_no_class_is_styled_as_two_different_components() -> None:
    """`.meter` was both an 8px bar and a wrapper around a labelled bar.

    The bar's `height: 0.5rem` won on source order, so every labelled row on
    Health collapsed to eight pixels and spilled its own label out of the card
    — the "text is hidden" report. Two components need two names.
    """
    sized: dict[str, list[str]] = {}
    for match in re.finditer(r"^(\.[a-z0-9-]+)\s*\{([^}]*)\}", CSS, flags=re.M):
        if re.search(r"(^|;)\s*height:", match.group(2)):
            sized.setdefault(match.group(1), []).append(match.group(2))

    for selector, bodies in sized.items():
        assert len(bodies) == 1, f"{selector} sets height in {len(bodies)} separate rules"


def test_the_hidden_attribute_actually_hides() -> None:
    """`hidden` is only `display: none` in the UA stylesheet.

    Any component rule that sets a display beats it, and the element stays
    laid out -- present, invisible to nobody, still taking its space. That is
    how a bulk-action group marked `hidden` kept holding a line in the Review
    toolbar. Two components had already been patched for it one at a time.
    """
    css = CSS

    assert "[hidden] { display: none !important; }" in css
    # And no one goes back to patching it per component.
    per_component = re.findall(r"\.[\w-]+\[hidden\]\s*\{[^}]*display\s*:", css)
    assert per_component == [], f"covered by the global rule already: {per_component}"


def test_long_paths_can_break() -> None:
    """A file path is one long word with no break opportunity in it.

    Left alone it sets the minimum width of whatever contains it, which is how
    History and Quarantine scrolled sideways on a phone.
    """
    assert ".mono:not(pre) { overflow-wrap: anywhere; }" in CSS


def test_the_header_status_pill_can_be_cut_short() -> None:
    """`white-space: nowrap` on the provider pill made the header 495px wide,
    so every page in the portal scrolled sideways on a phone — a header bug
    that reads as a bug on whatever page you happen to be looking at.

    It needs `display: inline-block` for the cap to bite at all: max-width and
    overflow are ignored on a non-replaced inline box, which is what an <a> is.
    """
    rule = re.search(r"\.provider-pill\s*\{([^}]*)\}", CSS)
    assert rule, "the provider pill lost its rule"
    body = rule.group(1)
    assert "inline-block" in body
    assert "text-overflow: ellipsis" in body
    assert "max-width" in body


def test_long_names_and_paths_are_clamped_not_wrapped_forever() -> None:
    """A UUID-named attachment three folders deep wrapped to eight lines, and
    one such row measured 496px — half a screen to say one filename. The whole
    value stays in the title attribute either way.
    """
    # `.name-text` and not `.proposal-name`: the clamp moved off the heading
    # and onto the text inside it, because the heading also holds the `?`
    # control and `overflow: hidden` was clipping that panel out of existence.
    for selector in (".name-text", ".dest-path"):
        # The selector may be one of several sharing a rule — the inbox row and
        # the library row use the same clamp — so match it anywhere in the list.
        rule = re.search(
            re.escape(selector) + r"[^{}]*\{([^}]*)\}", CSS
        )
        assert rule, f"{selector} lost its rule"
        assert "line-clamp: 2" in rule.group(1), selector
        assert "overflow: hidden" in rule.group(1), selector


def test_the_confidence_bar_is_coloured_by_the_score() -> None:
    """Colour answers the question asked fifty times a page — is this one safe
    to wave through? — so it has to follow the percentage. Five hues keyed to
    the evidence source instead made every bar look equally considered.

    One hue per band, declared once on the row and inherited by the edge, the
    bar and the number, so the three cannot drift into three opinions.
    """
    hues = {}
    for band in ("high", "mid", "low"):
        rule = re.search(r"\.proposal\.conf-" + band + r"\s*\{\s*--conf-hue:\s*([^;]+);", CSS)
        assert rule, f"the {band} band sets no hue"
        hues[band] = rule.group(1).strip()
    assert len(set(hues.values())) == 3, f"two bands share a colour: {hues}"
    assert "background: var(--conf-hue" in re.search(r"\.conf-part\s*\{([^}]*)\}", CSS).group(1)
    # And the legend explains those three, not the five it used to.
    for band, hue in hues.items():
        legend = re.search(r"\.conf-swatch\.is-" + band + r"\s*\{\s*background:\s*([^;]+);", CSS)
        assert legend and legend.group(1).strip() == hue, f"legend disagrees for {band}"


def test_what_a_score_is_made_of_survives_as_shading() -> None:
    """Colour went to the score, but "62% off a catalog match" and "62% off a
    filename" are still different propositions. The sources keep their own
    step within the row's hue."""
    kinds = ("catalog", "local", "ai", "cloud", "guess")
    steps = {}
    for kind in kinds:
        rule = re.search(r"\.conf-part\.is-" + kind + r"\s*\{\s*opacity:\s*([^;]+);", CSS)
        assert rule, f"the {kind} segment has no shade"
        steps[kind] = float(rule.group(1))
    assert len(set(steps.values())) == len(kinds), f"segments share a shade: {steps}"
    # Strongest evidence solid, weakest faintest — the order is the meaning.
    assert list(steps.values()) == sorted(steps.values(), reverse=True), steps
