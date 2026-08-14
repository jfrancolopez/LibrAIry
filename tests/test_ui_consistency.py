"""Conventions that have to hold across every page, not just the newest one.

Different pages had drifted into different vocabularies — `Mark for deletion`
against `Delete queue`, `Undo Whole Plan` against `Undo the whole plan`, three
verbs for navigating to Commit — and each of them was reasonable when it was
written. Drift is what happens without a test.

`docs/ui-vocabulary.md` is the reference; this is the part of it a machine can
check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = Path("src/librairy/web/templates")
PAGES = sorted(TEMPLATES.rglob("*.html"))
TAG = re.compile(r"<[^>]+>")
BUTTON = re.compile(
    r"<button[^>]*>(.*?)</button>"
    r'|<a\b[^>]*class="[^"]*btn[^"]*"[^>]*>(.*?)</a>',
    re.S,
)


def labels(page: Path) -> list[str]:
    """Every button/link label on one page, with markup and Jinja removed."""
    found = []
    for match in BUTTON.finditer(page.read_text(encoding="utf-8")):
        text = TAG.sub("", match.group(1) or match.group(2) or "")
        text = " ".join(text.split())
        if text and "{{" not in text and "{%" not in text:
            found.append(text)
    return found


def all_labels() -> dict[str, list[Path]]:
    seen: dict[str, list[Path]] = {}
    for page in PAGES:
        for label in labels(page):
            seen.setdefault(label, []).append(page)
    return seen


# --- the banned word ----------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_nothing_is_marked_for_deletion(page: Path) -> None:
    """It reads as "a deletion has been arranged". Nothing is ever deleted.

    And the same label sat on two buttons with opposite behaviour: one moved a
    file the instant it was pressed, one waited for Commit.
    """
    text = page.read_text(encoding="utf-8")

    assert "Mark for deletion" not in text


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_rejected_wording_stays_gone(page: Path) -> None:
    text = page.read_text(encoding="utf-8").lower()

    assert "your call" not in text


# --- length and case ----------------------------------------------------------


def test_no_button_is_a_sentence() -> None:
    """A label longer than four words is explanation wearing a button.

    The one that prompted this was "See exactly what will move".
    """
    long_labels = {
        label: [page.name for page in pages]
        for label, pages in all_labels().items()
        if len(label.split()) > 4
    }

    assert long_labels == {}


def test_no_button_shouts_in_title_case() -> None:
    """"Undo Whole Plan" and "Undo the whole plan" were the same action."""
    shouty = [
        label
        for label in all_labels()
        # Three or more capitalised words is Title Case, not a proper noun.
        if sum(1 for word in label.split() if word[:1].isupper()) >= 3
    ]

    assert shouty == []


# --- shared components --------------------------------------------------------


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_extension_info_is_never_hand_rolled(page: Path) -> None:
    """One control, imported. A page-local copy is a page that stops getting
    the next repair — which is exactly what happened to the `?` panel."""
    text = page.read_text(encoding="utf-8")
    if "ext-info-toggle" not in text:
        return

    assert page.name == "ext_info.html", (
        f"{page}: build the control with the shared macro, not by hand"
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_preview_is_never_hand_rolled(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    if "data-preview-toggle" not in text:
        return

    # The shared script owns open/close; a page that adds its own hx-get for a
    # preview gets the one-way toggle back.
    assert 'hx-get="/preview/' not in text


# --- the safety sentence ------------------------------------------------------


def test_every_delete_queue_control_says_nothing_is_deleted() -> None:
    """The single most important sentence in the product, on the page that
    carries the decision."""
    pages = [
        page
        for page in PAGES
        if "/quarantine/delete-queue/" in page.read_text(encoding="utf-8")
    ]

    assert pages, "no page offers the delete queue"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert "never deletes" in text or "Nothing is deleted" in text


def test_pending_decisions_show_current_and_after() -> None:
    """Every page that asks you to approve a move shows both ends of it."""
    for name in ("commit.html", "quarantine.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")

        assert "Current" in text
        assert "After Commit" in text


def test_empty_states_explain_themselves() -> None:
    for name, phrase in (
        ("quarantine.html", "Nothing is being held"),
        ("commit.html", "Nothing waiting to commit"),
    ):
        text = (TEMPLATES / name).read_text(encoding="utf-8")

        assert phrase in text
        assert "No data" not in text
