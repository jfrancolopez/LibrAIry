"""Two controls that appear on every screen, and both had stopped working.

Neither failure was visible to a DOM assertion, and that is the point of this
file. The markup was correct in both cases. The `?` opened a panel that CSS
clipped to nothing; Preview fetched a panel it could never put away. A test
that asks "is the button in the HTML" passes happily through both.

So these tests assert the *properties that were violated* rather than the
markup that was already fine:

  * nothing that contains a popover trigger may clip its overflow — proven by
    reading the CSS, because that is where the bug was
  * one panel per control, never a shared id
  * Preview is one control with two states, and closing it removes the media
    from the document rather than hiding it

The live proof — open, painted, in the viewport, closed again, on desktop and
at 375px — comes from driving a real browser against `scripts/ui_serve.py`.
That is not run here; a browser is a development tool and the production image
has none. What is run here is everything a file can be asked.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = Path("src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
PREVIEWS_JS = Path("src/librairy/web/static/previews.js").read_text(encoding="utf-8")
TEMPLATES = Path("src/librairy/web/templates")
EXT_INFO_SOURCE = (TEMPLATES / "partials/ext_info.html").read_text(encoding="utf-8")
# The comment in that file talks at length about the `<details>` this replaced.
# A test that reads prose is a test the prose can break.
EXT_INFO = re.sub(r"\{#.*?#\}", "", EXT_INFO_SOURCE, flags=re.S)


def rule(selector: str) -> str:
    """The declarations of the first rule whose selector list contains this."""
    match = re.search(re.escape(selector) + r"[^{}]*\{([^}]*)\}", CSS)
    assert match, f"{selector} has no rule"
    return match.group(1)


# --- the `?` ------------------------------------------------------------------


def test_the_control_is_a_popover_and_not_a_positioned_panel() -> None:
    """The whole fix, in one assertion.

    A `<details>` panel is an ordinary positioned element and so lives inside
    whatever its ancestors do to overflow. A popover renders in the top layer,
    where no ancestor can reach it. Every clipping bug below became impossible
    the moment this changed, rather than being patched screen by screen.
    """
    assert "popovertarget=" in EXT_INFO
    assert " popover " in EXT_INFO
    assert "<details" not in EXT_INFO


def test_the_filename_heading_does_not_clip_its_own_contents() -> None:
    """The regression, asserted where it lived.

    `.row-name` and `.proposal-name` hold both the filename and the `?`. They
    used to carry `-webkit-line-clamp: 2` and the `overflow: hidden` it needs,
    which clipped the panel to a 24px box — measured in a real browser at
    360x118 laid out, zero pixels inside the clip, and `elementFromPoint`
    returning null at its centre.

    The clamp is still there. It is on the text.
    """
    heading = rule(".proposal-name,\n.row-name")
    assert "overflow: hidden" not in heading
    assert "line-clamp" not in heading

    text = rule(".proposal-name > .name-text,\n.row-name > .name-text")
    assert "line-clamp: 2" in text, "the row can still not become eight lines tall"
    assert "overflow: hidden" in text


def test_the_browse_truncation_rule_is_scoped_to_browse() -> None:
    """`.row-name` is Browse's file row *and* every Review card's heading.

    Browse's rule — declared later in the file, so it won its way onto both —
    is what actually put `overflow: hidden` on the Review headings. A shared
    class name is a shared contract.
    """
    assert ".browse-row .row-name {" in CSS
    # `.proposal-name,\n.row-name {` is the Review heading and is meant to be
    # shared. What must not come back is a rule that starts a line with
    # `.row-name {` and quietly applies Browse's truncation to it.
    unscoped = re.search(r"(?<!,\n)^\.row-name \{", CSS, flags=re.M)
    assert unscoped is None, "an unscoped .row-name rule is back"


def test_the_panel_is_bounded_by_the_viewport() -> None:
    panel = rule(".ext-info-panel[popover]")

    assert "max-width" in panel
    assert "100vw" in panel, "bounded by the screen, not by the length of the text"
    assert "position: fixed" in panel


def test_the_control_is_still_a_labelled_tap_target() -> None:
    """It became a `<button>`, which inherits the button skin unless told not
    to. A `?` the size of Approve would be the loudest thing on the row."""
    toggle = rule(".ext-info-toggle")

    assert "min-width: 1.5rem" in toggle
    assert "min-height: 1.5rem" in toggle
    assert 'aria-label="{{ ext_aria_label(name) }}"' in EXT_INFO
    assert '<span aria-hidden="true">?</span>' in EXT_INFO


def test_no_screen_hand_rolls_its_own_file_type_control() -> None:
    """One control, imported everywhere. The alternative is fixing this bug
    once per template and missing one."""
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        if template.name == "ext_info.html":
            continue
        assert "ext-info-panel" not in text, template
        assert "ext-info-toggle" not in text, template


# --- Preview -------------------------------------------------------------------


def test_preview_is_one_control_with_two_states() -> None:
    """It was `hx-get` with `hx-swap="innerHTML"`, which has exactly one
    direction. A second click re-fetched the same markup and swapped it over
    itself: the request went out, the panel did not move, and the button
    looked inert."""
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert 'hx-get="/preview/items/' not in text, f"{template} still one-way"

    assert "data-preview-toggle" in PREVIEWS_JS
    assert "Hide preview" in PREVIEWS_JS
    assert 'aria-expanded' in PREVIEWS_JS


def test_every_preview_button_names_a_url_and_a_target() -> None:
    used = 0
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        for match in re.finditer(r"<button[^>]*data-preview-toggle[^>]*>", text):
            used += 1
            assert "data-preview-url=" in match.group(0), template
            assert "data-preview-target=" in match.group(0), template
    assert used >= 4, "Review, Library Review, Quarantine and Search all have one"


def test_closing_a_preview_tears_the_media_down_rather_than_hiding_it() -> None:
    """A `<video>` left in the document with `display: none` keeps its decoder,
    its buffer and — on more browsers than you would like — its audio."""
    close = PREVIEWS_JS.split("function close(", 1)[1].split("\n  }", 1)[0]

    assert "pause()" in close
    assert 'removeAttribute("src")' in close
    assert "replaceChildren()" in close
    assert "display" not in close, "hiding is not closing"


def test_expand_all_and_the_row_button_share_one_definition_of_open() -> None:
    """They used to be two implementations — htmx for the row, fetch for the
    bulk button — which is how they came to disagree about what open meant."""
    assert PREVIEWS_JS.count("function open(") == 1
    assert PREVIEWS_JS.count("function close(") == 1
    for helper in ("expandAll", "collapseAll"):
        body = PREVIEWS_JS.split("function " + helper + "(", 1)[1].split("\n  }", 1)[0]
        assert "open(" in body or "close" in body, helper


# --- vocabulary ------------------------------------------------------------------


def test_the_rejected_wording_appears_nowhere_in_the_product() -> None:
    """"Your call" was rejected: it hands the reader the decision without
    saying what the software has and has not already done."""
    for path in Path("src/librairy").rglob("*"):
        if path.suffix not in {".py", ".html"} or not path.is_file():
            continue
        assert "your call" not in path.read_text(encoding="utf-8").lower(), path
