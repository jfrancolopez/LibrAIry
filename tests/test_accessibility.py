"""What a keyboard and a screen reader meet on every populated page.

Not a score. An automated checker is a spell-checker for markup — it finds the
missing `alt` and cannot tell you that the queue is unusable without a mouse —
so what is asserted here is what somebody would actually hit while *doing*
something: reading the page's outline, finding what a field is for, hearing what
just happened, and getting from Review to Commit with the Tab key.

Read off the rendered fixture rather than the templates, for the reason
`tests/dev/controls.py` gives: a label that is a Jinja expression and a control
inside an `{% if %}` no page satisfies are both invisible to a template reader,
and those are exactly the ones that drift.

Every rule here was a real defect somewhere in this application:

    a page whose outline began at h1 half way down the document
    a folder view with no heading at all
    four panes headed h3 with no h2 above them
    two time fields under one label, told apart by a dash
    twelve files approved in silence, because the fragment swapped
    a disclosure button that never said whether it was open
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.dev.fixture import build_fixture  # noqa: E402

#  Every populated surface, including the ones added late: a page with nothing
#  on it proves nothing about the controls that live on rows.
SURFACES = (
    "/dashboard",
    "/review",
    "/review?state=confident",
    "/review/learned",
    "/commit",
    "/quarantine",
    "/history",
    "/browse",
    "/browse/Music",
    "/browse?q=IMG_4021",
    "/health",
    "/settings",
    "/backups",
    "/backups/2/only-here",
    "/projects",
    "/projects/1",
    "/reconcile",
    "/delete-queue",
    "/search?q=IMG",
    "/maintenance/optimization",
)

VOID = {"img", "input", "br", "hr", "meta", "link", "source", "track"}
FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary", "details"}


class Page(HTMLParser):
    """Just enough of a document to ask the questions below of it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.headings: list[tuple[int, str]] = []
        self.fields: list[dict] = []
        self.labels_for: set[str] = set()
        self.images: list[dict] = []
        self.svgs: list[dict] = []
        self.live: list[dict] = []
        self.positive_tabindex: list[str] = []
        self.handlers: list[str] = []
        self.unfocusable_roles: list[str] = []
        self.anchors_without_href = 0
        self._heading: int | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        found = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = int(tag[1])
            self._text = []
        if tag == "label" and "for" in found:
            self.labels_for.add(str(found["for"]))
        if tag in {"input", "select", "textarea"}:
            self.fields.append(
                {**found, "_tag": tag, "_wrapped": "label" in self.open}
            )
        if tag == "img":
            self.images.append(found)
        if tag == "svg":
            self.svgs.append(found)
        if "aria-live" in found or found.get("role") in {"status", "alert"}:
            self.live.append(found)
        if "onclick" in found and tag not in {"button", "a", "input"}:
            self.handlers.append(tag)
        if (
            found.get("role") in {"button", "link", "checkbox"}
            and tag not in FOCUSABLE
            and "tabindex" not in found
        ):
            self.unfocusable_roles.append(f"{tag}[role={found['role']}]")
        if tag == "a" and "href" not in found:
            self.anchors_without_href += 1
        index = str(found.get("tabindex", "0"))
        if index.lstrip("-").isdigit() and int(index) > 0:
            self.positive_tabindex.append(f"{tag} tabindex={index}")
        if tag not in VOID:
            self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            self.headings.append(
                (self._heading, " ".join("".join(self._text).split()))
            )
            self._heading = None
        while self.open:
            if self.open.pop() == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._heading is not None:
            self._text.append(data)


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):  # noqa: ANN201
    return build_fixture(tmp_path_factory.mktemp("a11y"))


@pytest.fixture(scope="module")
def pages(client) -> dict[str, Page]:  # noqa: ANN001
    found: dict[str, Page] = {}
    for surface in SURFACES:
        response = client.get(surface)
        assert response.status_code == 200, f"{surface} answered {response.status_code}"
        page = Page()
        page.feed(response.text)
        found[surface] = page
    return found


def named(field: dict, page: Page) -> bool:
    """Does this field have a name a screen reader can read out?

    Four ways, and the first is the one a template reader misses: a field
    wrapped in its own `<label>` needs no `for`, and most of this application
    labels fields that way.
    """
    if str(field.get("type", "")).lower() in {"hidden", "submit", "button", "image"}:
        return True
    return bool(
        field.get("_wrapped")
        or field.get("aria-label")
        or field.get("aria-labelledby")
        or field.get("title")
        or (field.get("id") and field["id"] in page.labels_for)
    )


def test_every_page_has_exactly_one_top_level_heading(pages: dict[str, Page]) -> None:
    """A page with none cannot be navigated by heading at all, and a page with
    two is two pages as far as the outline is concerned."""
    for surface, page in pages.items():
        levels = [level for level, _ in page.headings]
        assert levels.count(1) == 1, f"{surface} has {levels.count(1)} h1"


def test_no_page_skips_a_heading_level(pages: dict[str, Page]) -> None:
    """A heading level is structure, not size.

    The Dashboard's hero was an `h1` inside a band headed by an `h2`, so the
    outline said the page began half way down it; Browse's four panes were `h3`
    with nothing above them. Both read to somebody moving by heading as a
    document with a piece missing.
    """
    for surface, page in pages.items():
        previous = 0
        for level, text in page.headings:
            if previous:
                assert level <= previous + 1, (
                    f"{surface} jumps h{previous} to h{level} at {text!r}"
                )
            previous = level


def test_every_field_says_what_it_is_for(pages: dict[str, Page]) -> None:
    """Two time inputs under one visible label, told apart by a dash between
    them, is a form with a field nobody can name."""
    for surface, page in pages.items():
        unnamed = [
            f"{field['_tag']}[{field.get('name', '?')}]"
            for field in page.fields
            if not named(field, page)
        ]
        assert not unnamed, f"{surface}: {unnamed}"


def test_every_image_decides_whether_it_is_worth_describing(
    pages: dict[str, Page],
) -> None:
    """`alt=""` is an answer — *this picture adds nothing the text beside it
    does not already say* — and every thumbnail in this application means it.
    No `alt` at all is not an answer: the file name gets read out instead."""
    for surface, page in pages.items():
        missing = [
            str(image.get("src", ""))[:60] for image in page.images if "alt" not in image
        ]
        assert not missing, f"{surface}: {missing}"


def test_the_tab_order_is_the_reading_order(pages: dict[str, Page]) -> None:
    """No positive `tabindex` anywhere, which is what keeps them the same thing."""
    for surface, page in pages.items():
        assert not page.positive_tabindex, f"{surface}: {page.positive_tabindex}"


def test_everything_that_acts_is_something_a_keyboard_can_reach(
    pages: dict[str, Page],
) -> None:
    """A `<div onclick>` is a control for a mouse and furniture for everyone
    else, and an anchor with no `href` is not in the tab order at all."""
    for surface, page in pages.items():
        assert not page.handlers, f"{surface} has click handlers on {page.handlers}"
        assert not page.unfocusable_roles, f"{surface}: {page.unfocusable_roles}"
        assert not page.anchors_without_href, f"{surface} has an anchor with no href"


def test_every_page_can_say_what_just_happened(pages: dict[str, Page]) -> None:
    """The region has to exist *before* the message does.

    A live region created with its text already in it is a region most screen
    readers never read, because they announce what changes and an element that
    arrives full has not changed. So it is in the layout, empty, on every page —
    and `/static/announce.js` puts into it whatever an htmx swap brought back.
    """
    for surface, page in pages.items():
        polite = [
            found
            for found in page.live
            if found.get("id") == "announcer" and found.get("aria-live") == "polite"
        ]
        assert polite, f"{surface} has no live region to announce into"


def test_the_result_of_an_action_is_marked_for_announcing() -> None:
    """And the messages themselves say they are worth reading out.

    Read from the templates, because a toast only exists after somebody presses
    something: the fixture cannot render one, and a rule that only holds on
    pages the fixture happens to reach is not a rule.
    """
    templates = Path(__file__).resolve().parents[1] / "src/librairy/web/templates"
    marked = {
        path.name
        for path in templates.rglob("*.html")
        if "data-announce" in path.read_text(encoding="utf-8")
    }
    for name in ("review_list.html", "commit.html", "quarantine.html", "settings.html"):
        assert name in marked, f"{name} has no announced result"


def test_a_disclosure_says_whether_it_is_open(pages: dict[str, Page]) -> None:
    """The button is the only thing that knows, and `aria-expanded` is the only
    way it can say so. Native `<details>` says it for itself, which is why the
    group headings on Review need nothing here."""
    review = Path(__file__).resolve().parents[1] / "src/librairy/web/templates"
    for name in ("review.html", "partials/review_row.html", "partials/review_audit.html"):
        text = (review / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "data-panel-toggle=" not in line or ">Cancel</button>" in line:
                continue
            assert "aria-expanded" in line, f"{name}: {line.strip()[:80]}"
            assert "aria-controls" in line, f"{name}: {line.strip()[:80]}"
    del pages


def test_review_can_be_worked_through_without_a_mouse(client) -> None:  # noqa: ANN001
    """The whole flow, as controls a keyboard can reach, in the order it meets them.

    Open Review, move through the decisions, open a group, select what is shown,
    approve what matches, read the evidence, and leave for Commit. Every step
    is a native control — a `<button>`, a `<summary>`, a checkbox, a link — so
    the only thing that could break the flow is a control that is not one, and
    that is what this looks for.
    """
    html = client.get("/review").text
    #  In document order, which is tab order, which is the order somebody meets
    #  them. Each of these is the step it names.
    steps = (
        ("filters", '<summary>Filters'),
        ("a group", '<details class="review-group"'),
        ("select the shown", 'name="proposal_id"'),
        ("approve what matches", '{"action": "approve"'),
        ("the row's evidence", 'data-panel-toggle="why-'),
    )
    at = 0
    for step, needle in steps:
        found = html.find(needle, at)
        assert found > 0, f"Review has no {step} ({needle})"
        at = found
    #  And the way out, which is a link in the navigation on every page.
    assert 'href="/commit"' in html


def test_meaning_is_never_only_a_colour(pages: dict[str, Page]) -> None:
    """A badge carries a word, not just a hue.

    The confidence bar is the case that matters — it is drawn in green, amber
    and red — so the legend beside it says what each band *means* in words, and
    the tier a decision is in is a label rather than a shade.
    """
    review = pages["/review"]
    del review
    html = Path(__file__).resolve().parents[1] / "src/librairy/web/templates/review.html"
    text = html.read_text(encoding="utf-8")
    assert "conf-legend" in text
    for said in ("and up", "good evidence", "read this one"):
        assert said in text, f"the confidence legend no longer explains {said!r}"


def test_a_chart_says_in_words_what_it_draws(pages: dict[str, Page]) -> None:
    """Colour and height are the same fact twice, and neither is readable.

    Every chart carries a summary — "17 files filed on 6 September, 4 the day
    before" — as its accessible name, and each point carries its own. A reader
    who cannot see the shape gets the sentence the shape was drawn to make.
    """
    for surface, page in pages.items():
        unnamed = [
            str(found.get("class", "?"))
            for found in page.svgs
            if not (found.get("aria-label") or found.get("aria-labelledby"))
            and found.get("aria-hidden") != "true"
        ]
        assert not unnamed, f"{surface} draws {unnamed} with no name"


def test_focus_is_visible_wherever_it_lands() -> None:
    """The whole keyboard flow rests on being able to see where you are.

    One rule, on `:focus-visible` rather than `:focus`, so a mouse press does
    not draw a ring nobody asked for — and never `outline: none` without
    something drawn in its place.
    """
    css = (
        Path(__file__).resolve().parents[1]
        / "src/librairy/web/static/pipboy.css"
    ).read_text(encoding="utf-8")
    ring = re.search(r":focus-visible\s*\{[^}]*outline:\s*(?!none)[^;]+;", css)
    assert ring, "no visible focus ring is defined"
    for match in re.finditer(r"([^{}]*):focus[^-][^{}]*\{([^}]*)\}", css):
        assert "outline: none" not in match.group(2), match.group(1).strip()
