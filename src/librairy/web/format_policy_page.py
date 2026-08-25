"""The one page where the owner says what they prefer, permit and protect.

Deliberately its own page rather than another block on the long Settings
scroll. It is the only screen in LibrAIry whose subject is a *policy* — an
answer that outlives the decision it was given for — and putting it between
thumbnail sizes and API keys would say it is the same kind of thing.

What it must not become is a page of codec checkboxes. Twenty formats with
twenty toggles is not a policy anybody can hold in their head, and none of it
would answer the question somebody actually has, which is: *what would this do
to my library?* So the page has four short category sections, a list of
protected folders, and a measurement.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from librairy.format_impact import is_stale, last
from librairy.format_policy import (
    DISPLAY,
    KNOWN_FORMATS,
    SECTION_LABEL,
    SECTIONS,
    canonical,
    preferred_for,
    protected_folders,
    scopes,
)
from librairy.humanize import human_ago


def page_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything the Format Policy page shows. Reads only.

    Explicitly not the impact analysis: that walks the whole index, and a page
    that measured a library while rendering would get slower the more somebody
    owned. The last measurement is shown; running a new one is a button.
    """
    report = last(conn)
    return {
        "sections": [_section(conn, name) for name in SECTIONS],
        "protected": _protected(conn),
        "impact": report,
        "impact_stale": is_stale(conn, report),
        "impact_when": human_ago(str(report.get("measured_at", ""))) if report else "",
    }


def _section(conn: sqlite3.Connection, category: str) -> dict[str, Any]:
    found = preferred_for(conn, category)
    #  One entry per format, not one per spelling. `.aif` and `.aiff` are the
    #  same format, and a dropdown that says AIFF twice reads as a defect.
    choices = sorted(
        {canonical(value) for value in KNOWN_FORMATS.get(category, frozenset())}
    )
    transforms = _transforms(conn, category)
    return {
        "category": category,
        "label": SECTION_LABEL.get(category, category.title()),
        "preferred": found,
        "preferred_label": DISPLAY.get(found, found.upper()) if found else "",
        #  What the section says when nothing is configured, which is the state
        #  three of the four ship in. "No format preference is configured" is a
        #  real answer and reads as one; an empty dropdown reads as a bug.
        "note": (
            f"{DISPLAY.get(found, found.upper())} is your preferred existing "
            f"representation."
            if found
            else "No format preference is configured."
        ),
        "choices": [
            {"value": value, "label": DISPLAY.get(value, value.upper())}
            for value in choices
        ],
        **transforms,
    }


def _transforms(conn: sqlite3.Connection, category: str) -> dict[str, Any]:
    """Whether LibrAIry may ever *propose* making a new representation.

    Three states, not two. "Not configured" is the default and is not the same
    as "no" — it is what keeps this page from changing what Storage
    Optimization already offers on the day it appears.
    """
    from librairy.format_policy import SECTION_OF

    slug = SECTION_OF.get(category, category)
    found = next(
        (
            scope
            for scope in scopes(conn)
            if scope.scope_kind == "category" and scope.scope_value == slug
        ),
        None,
    )
    lossy = None if found is None else found.allow_lossy_transform
    lossless = None if found is None else found.allow_lossless_transform
    return {
        "allow_lossy": lossy,
        "allow_lossless": lossless,
        "transform_note": (
            "You have not said whether LibrAIry may propose converting these."
            if lossy is None and lossless is None
            else _transform_sentence(lossy, lossless)
        ),
    }


def _transform_sentence(lossy: bool | None, lossless: bool | None) -> str:
    parts = []
    if lossless is not None:
        parts.append(
            "lossless conversions may be proposed"
            if lossless
            else "lossless conversions are not to be proposed"
        )
    if lossy is not None:
        parts.append(
            "lossy conversions may be proposed"
            if lossy
            else "lossy conversions are not to be proposed"
        )
    return "; ".join(parts).capitalize() + "."


def _protected(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """The two kinds of protection, side by side and told apart.

    A Format Policy folder stops its originals being traded away by a
    representation preference or an optimization. A protected *root* stops the
    folder being queued for change at all. The stronger implies the weaker, and
    a page that printed one word for both would leave somebody unable to tell
    which power they had configured.
    """
    from librairy.protected import protected_roots

    found = [
        {
            "folder": folder,
            "kind": "format-policy",
            "what": "Preserve originals",
            "note": "No format preference or optimization may trade these away."
            " LibrAIry can still file and organise them.",
            "removable": True,
        }
        for folder in protected_folders(conn)
    ]
    found.extend(
        {
            "folder": root,
            "kind": "protected-root",
            "what": "Protected root",
            "note": "Nothing in here may be queued for change at all."
            " Configured under Storage Optimization.",
            "removable": False,
        }
        for root in protected_roots(conn)
    )
    return found
