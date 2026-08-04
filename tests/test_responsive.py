"""Breakpoint checks for the adaptive shell (P17-01, P17-02).

The dashboard is meant to fill whatever it is opened on — phone, iPad, laptop,
32" monitor — by adding columns rather than stretching a fixed layout. That
intent lives entirely in a handful of media queries, and the failure mode is
quiet: a stray `max-width: 640px` next to a `min-width: 640px` leaves one exact
width where both rules apply and the layout half-collapses. Nothing else in the
suite reads the CSS, so these assertions are what keep the ladder honest.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")

# medium (iPad portrait), wide (laptop), ultra (32"). Below 640 is compact.
BREAKPOINTS = (640, 1024, 1600)


def block(query: str) -> str:
    """Every rule under a media query, e.g. block("min-width: 1024px").

    A breakpoint is declared more than once — the widget grid and the explorer
    each keep their rules next to the component they style — so this joins all
    of them rather than returning whichever comes first in the file.
    """
    bodies = re.findall(
        r"@media\s*\(\s*" + re.escape(query) + r"\s*\)\s*\{(.*?)\n\}",
        CSS,
        re.DOTALL,
    )
    assert bodies, f"no @media ({query}) block in pipboy.css"
    return "\n".join(bodies)


def columns(css_block: str, selector: str) -> int:
    match = re.search(
        re.escape(selector) + r"\s*\{[^}]*grid-template-columns:\s*([^;]+);", css_block
    )
    assert match is not None, f"{selector} sets no grid-template-columns here"
    value = match.group(1)
    repeat = re.match(r"repeat\((\d+),", value.strip())
    if repeat:
        return int(repeat.group(1))
    return len(re.findall(r"minmax\(", value)) or len(value.split())


def test_every_breakpoint_is_declared() -> None:
    for width in BREAKPOINTS:
        assert f"@media (min-width: {width}px)" in CSS


def test_compact_rules_stop_one_pixel_below_medium() -> None:
    """A max-width that equals a min-width makes both apply at that exact width."""
    for width in re.findall(r"@media\s*\(\s*max-width:\s*(\d+)px\s*\)", CSS):
        assert int(width) not in BREAKPOINTS, (
            f"max-width: {width}px collides with the min-width: {width}px breakpoint; "
            f"use {int(width) - 1}px"
        )


def test_widget_grid_gains_a_column_at_each_breakpoint() -> None:
    """Phone 1 → iPad 2 → laptop 3 → 32" 4, so bigger screens show more, not bigger."""
    base = re.search(r"\.widget-grid\s*\{[^}]*grid-template-columns:\s*([^;]+);", CSS)
    assert base is not None and base.group(1).strip() == "1fr"

    counts = [columns(block(f"min-width: {width}px"), ".widget-grid") for width in BREAKPOINTS]
    assert counts == [2, 3, 4]


def test_explorer_reveals_more_panes_as_the_screen_grows() -> None:
    """Miller panes: categories fold away on a laptop and come back on a 32"."""
    assert columns(block("min-width: 640px"), ".explorer") == 2
    laptop = block("min-width: 1024px")
    assert columns(laptop, ".explorer") == 3
    assert '.explorer-pane[data-pane="0"] { display: none; }' in laptop

    ultra = block("min-width: 1600px")
    assert columns(ultra, ".explorer") == 4
    assert '.explorer-pane[data-pane="0"] { display: block; }' in ultra


def test_nav_stacks_below_the_header_only_on_compact() -> None:
    compact = block("max-width: 639px")
    assert ".app-nav { order: 3; width: 100%; }" in compact
    # The nav must not be re-stacked at any wider size.
    for width in BREAKPOINTS:
        assert "order: 3" not in block(f"min-width: {width}px")


def test_widgets_never_overflow_their_column() -> None:
    """min-width:0 is what stops a long path from forcing a grid child wider."""
    assert re.search(r"\.widget\s*\{[^}]*min-width:\s*0", CSS)
    assert "minmax(0, 1fr)" in CSS


def test_full_width_widgets_collapse_on_compact() -> None:
    compact = block("max-width: 639px")
    assert ".widget-wide, .widget-wide-2 { grid-column: 1 / -1; }" in compact
