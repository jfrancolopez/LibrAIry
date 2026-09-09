"""What a 375px screen gets, beyond fitting.

`scripts/ui_check.py` answers "does it fit" and nothing else, and fitting was
never the question — a five-column table inside a sideways scroll box fits, and
the column somebody came to read is off the edge behind a gesture nobody
discovers. So these are the rules that survived a pass of *using* LibrAIry at
375px, each one a real thing that was wrong:

    a filename broken as `IM / G_5150.j / peg`, three lines of nothing
    five columns of run history with `18 copied · 194.7 MB` off the edge
    five filter fields between the search box and the first result
    a 17px checkbox deciding what a bulk approve covers
    a first screen of bulk actions before a single decision

They are asserted against the rendered fixture and the stylesheet rather than
against screenshots: a pixel comparison fails when a date changes, and passes
when the layout is wrong in a way nobody thought to photograph.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.dev.fixture import build_fixture  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "src/librairy/web/static/pipboy.css").read_text(encoding="utf-8")
TEMPLATES = ROOT / "src/librairy/web/templates"

#  Where the narrow rules live. Everything below asks about this block, so a
#  rule moved out of it is a rule that stopped applying to a phone.
NARROW = "\n".join(
    match.group(0)
    for match in re.finditer(r"@media \(max-width: 639px\) \{.*?\n\}", CSS, re.S)
)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    return build_fixture(tmp_path_factory.mktemp("mobile"))


def test_a_path_breaks_at_a_folder_and_not_inside_a_filename(client) -> None:  # noqa: ANN001
    """`library/Photos/2026/August/` then `IMG_5150.jpeg`, never `IM / G_5150`.

    Two halves, and both are needed: the markup carries `<wbr>` after every
    separator so there is somewhere sensible to break, and the CSS says
    `break-word` rather than `anywhere` — `anywhere` fills each line to the
    last character that fits, which is a mid-filename break every time.
    """
    html = client.get("/commit").text

    assert "<wbr>" in html, "no break opportunities in the paths on Commit"
    assert re.search(r"\.correction-shape dd \{[^}]*overflow-wrap: break-word", CSS)
    assert "overflow-wrap: anywhere" not in re.search(
        r"\.correction-shape dd \{[^}]*\}", CSS
    ).group(0)


def test_the_divergence_paths_break_the_same_way(client) -> None:  # noqa: ANN001
    html = client.get("/backups/2/only-here").text

    assert "<wbr>" in html
    assert re.search(r"\.divergence-table code \{[^}]*break-word", CSS)


def test_a_run_stacks_instead_of_scrolling_out_of_sight() -> None:
    """Five columns of prose do not fit in 327px.

    Stacked on a phone, each run is a small card headed by the column names it
    came from — which only works if every cell carries the heading it lost, so
    that is asserted too.
    """
    html = (TEMPLATES / "backups.html").read_text(encoding="utf-8")
    cells = re.findall(r"<td([^>]*)>", html)

    assert cells, "the run table has no cells"
    for attributes in cells:
        assert "data-label=" in attributes, f"<td{attributes}> has no stacked heading"
    assert ".run-table thead { display: none; }" in NARROW
    assert 'content: attr(data-label)' in NARROW


def test_the_divergence_table_is_left_as_a_table() -> None:
    """Three narrow columns fit, and a path is easier to scan in a list.

    Here to keep the rule above from spreading: tables are converted when they
    are painful, not because cards are fashionable.
    """
    assert ".divergence-table thead" not in NARROW


def test_a_search_reaches_its_results_before_the_filters(client) -> None:  # noqa: ANN001
    """The box you typed in, the button, and then the first result.

    The four ways to narrow are folded into the same disclosure Review uses —
    not removed, and not hidden when they are in use.
    """
    plain = client.get("/browse?q=IMG_5200&root=library").text
    narrowed = client.get("/browse?q=IMG_5200&root=library&category=photos").text

    assert '<details class="filter-more">' in plain, "the filters are not folded away"
    assert '<details class="filter-more" open>' in narrowed, (
        "a narrowed search hides the filter that narrowed it"
    )
    #  And the primary field is still outside the fold.
    head = plain[: plain.index("filter-more")]
    assert 'name="q"' in head
    assert "Search</button>" in head


def test_the_things_a_bulk_action_covers_are_never_hidden(client) -> None:  # noqa: ANN001
    """Scope is safety, so nothing about a narrow screen may take it away.

    The counts on the bulk buttons say what a press would do, and the jump nav
    counts say how much is in each section. Both are text, both stay text, and
    no narrow rule may hide either.
    """
    html = client.get("/review").text

    assert re.search(r"Approve \d+ settled", html)
    assert re.search(r"Approve \d+ at \d+%", html)
    assert 'class="review-jump"' in html
    for hidden in re.findall(r"([^{}]+)\{[^}]*display:\s*none", NARROW):
        for banned in (".review-jump", ".button-row", ".badge"):
            assert banned not in hidden, f"a narrow rule hides {banned}"


def test_a_checkbox_is_something_a_thumb_can_hit() -> None:
    """The ones that matter decide what a bulk action covers."""
    assert re.search(r'input\[type="checkbox"\][^{]*\{[^}]*width: 1\.35rem', NARROW)
    assert re.search(r"label:has\(> input\[type=\"checkbox\"\]\)[^{]*\{[^}]*min-height", NARROW)


def test_nothing_is_permanently_fixed_over_the_page() -> None:
    """A bar pinned to the bottom of a phone screen covers the last thing on
    the page, and the last thing on a page here is usually the action.

    The two fixed things in this stylesheet are a decorative overlay that
    cannot be clicked and a popover that only exists while it is open.
    """
    for block in re.finditer(r"([^{}]+)\{([^}]*position:\s*fixed[^}]*)\}", CSS):
        selector = block.group(1).strip().splitlines()[-1].strip()
        if selector.startswith("@media"):
            #  The media query itself; the rule inside it is checked on its own.
            continue
        assert (
            "scanlines" in selector
            or "popover" in selector
            or "ext-info" in selector
        ), f"{selector} is pinned over the page"


def test_the_fixture_carries_a_name_nobody_would_choose() -> None:
    """So that every 375px check is made against one.

    A layout that holds for `dune.epub` holds for nothing. This is the file
    that makes the harness's answer mean something.
    """
    fixture = (ROOT / "tests/dev/fixture.py").read_text(encoding="utf-8")
    found = re.search(r'"(2026-03-14 Quarterly[^"]+)"', fixture)

    assert found, "the fixture has no pathological filename any more"
    assert len(found.group(1)) > 60  # noqa: PLR2004


def test_a_path_with_no_break_opportunities_still_cannot_widen_the_page() -> None:
    """The two halves of the wrapping fix have to stay apart.

    `.mono` is every path in the application and most of them are printed
    straight, with no `<wbr>` in them — for those, `overflow-wrap: anywhere` is
    what keeps one long name from setting the minimum width of whatever holds
    it, which is how History and Quarantine used to scroll sideways on a phone.
    `break-word` is only for the cells whose paths carry break opportunities.
    Unifying the two would give back one of the two bugs.
    """
    found = re.search(r"\.mono:not\(pre\) \{([^}]*)\}", CSS)

    assert found, "the global path rule has gone"
    assert "overflow-wrap: anywhere" in found.group(1)
