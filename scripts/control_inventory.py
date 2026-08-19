#!/usr/bin/env python3
"""Print every control on every populated page. Development only.

    python scripts/control_inventory.py               # grouped by label
    python scripts/control_inventory.py --by-page
    python scripts/control_inventory.py --by-action

`tests/test_control_inventory.py` asserts the rules; this is for reading. The
two share `tests/dev/controls.py`, so the report and the test cannot disagree
about what counts as a control.

What it is for is the question no template grep answers: *is this word already
taken, and by what*. Naming a new button by looking it up beats naming it by
inventing, and every duplicate this pass removed — four ways to reach Commit,
three ways to say Test, two ways to say Analyse again — existed because
inventing was easier than looking.

Not part of the product, on the same terms as `ui_check.py`: nothing in
`src/librairy` imports it, and it is not packaged. `tests/test_dev_tooling.py`
asserts that rather than trusting this paragraph.
"""

from __future__ import annotations

import argparse
import collections
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--by-page", action="store_true", help="every control, page by page")
    group.add_argument(
        "--by-action", action="store_true", help="every endpoint and the labels on it"
    )
    args = parser.parse_args(argv)

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tests.dev.controls import SURFACES, inventory  # noqa: PLC0415
    from tests.dev.fixture import build_fixture  # noqa: PLC0415

    root = Path(tempfile.mkdtemp(prefix="librairy-controls-"))
    try:
        client = build_fixture(root)
        found = inventory(client)
        print(f"{len(found)} controls across {len(SURFACES)} surfaces\n")  # noqa: T201
        if args.by_page:
            _by_page(found)
        elif args.by_action:
            _by_action(found)
        else:
            _by_label(found)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def _describe(control) -> str:  # noqa: ANN001
    action = control.endpoint or "(page-local)"
    return f"{action} {control.value}".strip()


def _by_label(found: list) -> None:
    labels: dict[str, set[str]] = collections.defaultdict(set)
    pages: dict[str, set[str]] = collections.defaultdict(set)
    for control in found:
        key = control.label or f"[{control.name or 'unnamed'}]"
        labels[key].add(_describe(control))
        pages[key].add(control.page)
    for label in sorted(labels, key=str.lower):
        marker = "  <-- two meanings" if len(labels[label]) > 1 else ""
        print(f"{label!r:<34} {len(pages[label])} page(s){marker}")  # noqa: T201
        for action in sorted(labels[label]):
            print(f"      {action}")  # noqa: T201


def _by_action(found: list) -> None:
    actions: dict[str, set[str]] = collections.defaultdict(set)
    for control in found:
        actions[_describe(control)].add(control.label or "[unnamed]")
    for action in sorted(actions):
        marker = "  <-- two words" if len(actions[action]) > 1 else ""
        print(f"{action}{marker}")  # noqa: T201
        for label in sorted(actions[action]):
            print(f"      {label!r}")  # noqa: T201


def _by_page(found: list) -> None:
    by_page: dict[str, list] = collections.defaultdict(list)
    for control in found:
        by_page[control.page].append(control)
    for page, controls in by_page.items():
        print(f"\n=== {page}  ({len(controls)})")  # noqa: T201
        for control in controls:
            name = control.label or f"[{control.name or 'unnamed'}]"
            print(f"  {control.tag:<8} {name[:34]!r:<36} {_describe(control)}")  # noqa: T201


if __name__ == "__main__":
    raise SystemExit(main())
