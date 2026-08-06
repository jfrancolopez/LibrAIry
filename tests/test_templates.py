"""Template lint (P16-04).

Two classes of bug kept coming back and neither is caught by the route tests,
because the routes return correct HTML that the *browser* then refuses to run:

1. The portal's CSP is `default-src 'self'; style-src 'self' 'unsafe-inline'`.
   An inline `<script>` or an `onclick=` attribute is silently blocked, so a
   button renders perfectly and does nothing when clicked.
2. `hx-target="body"` swaps a full page response into `<body>`, which drops the
   surrounding document and leaves a blank screen. Full-page navigation belongs
   to plain links and form actions.

These run against the template source, not a rendered response, so a page nobody
has written a route test for is still covered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path("src/librairy/web/templates")
PAGES = sorted(TEMPLATES.rglob("*.html"))
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
# <script src="..."></script> is fine; <script>anything</script> is not.
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", re.IGNORECASE)


def test_templates_are_actually_discovered() -> None:
    """Guard the guard: a bad glob would make every test below vacuously pass."""
    assert len(PAGES) > 10
    assert TEMPLATES / "base.html" in PAGES


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_inline_script_blocks(page: Path) -> None:
    match = INLINE_SCRIPT.search(page.read_text(encoding="utf-8"))
    assert match is None, (
        f"{page}: inline <script> is blocked by the CSP. "
        f"Move it to src/librairy/web/static/ and load it with src=."
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_inline_event_handlers(page: Path) -> None:
    match = INLINE_HANDLER.search(page.read_text(encoding="utf-8"))
    assert match is None, (
        f"{page}: inline {match.group().strip()} handler is blocked by the CSP. "
        f"Use an htmx attribute (hx-post/hx-get/hx-vals) or an external script."
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_full_page_swaps_into_body(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    assert 'hx-target="body"' not in text, (
        f"{page}: swapping a full page into <body> blanks the document. "
        f"Use a normal link/form action for navigation, or target a fragment."
    )


FORM_TAG = re.compile(r"<form\b|</form>", re.IGNORECASE)
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_nested_forms(page: Path) -> None:
    """A third silent-in-the-browser bug (found live in settings.html).

    HTML has no nested forms. The parser drops the inner `<form>` start tag and
    reparents its fields onto the outer form, so "Save key" posted the whole
    settings page to /settings and the key was never stored. Both forms render
    fine in the response body, which is why route tests missed it.

    Template control flow means an exact depth count is not always possible, so
    this tracks the source order of the tags — enough to catch a `<form>` opened
    while another is unmistakably still open.
    """
    # Jinja comments never reach the browser, so a comment that mentions
    # <form> is not a form. Without this, documenting the rule trips it.
    source = JINJA_COMMENT.sub("", page.read_text(encoding="utf-8"))
    depth = 0
    for match in FORM_TAG.finditer(source):
        if match.group().lower().startswith("</"):
            depth = max(0, depth - 1)
            continue
        assert depth == 0, (
            f"{page}: <form> at offset {match.start()} opens inside another form. "
            f"Browsers discard it and post its fields to the outer form instead."
        )
        depth += 1
