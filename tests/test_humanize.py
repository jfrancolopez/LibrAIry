"""One size formatter, and zero is a size.

There were three copies of `human_bytes` — `humanize`, `web/access` and
`web/commit` — and they disagreed about the only interesting case. Two said
`0 B` and one said `unknown`, so the same number read differently depending on
which page you were on, and two test files asserted opposite things about the
same function name and both passed.
"""

from __future__ import annotations

import pytest

from librairy.humanize import human_bytes


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0 B"),
        (1, "1 B"),
        (512, "512 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (25_904_964, "24.7 MB"),
        (1_449_985_635, "1.4 GB"),
        (2 * 1024**4, "2.0 TB"),
    ],
)
def test_a_size_reads_the_way_a_person_would_say_it(size: int, expected: str) -> None:
    assert human_bytes(size) == expected


def test_zero_is_known() -> None:
    """An empty file is 0 B, a remux saves 0 B, a total of nothing is 0 B.

    All facts, and all of them were reported as `unknown` by an `if not size`
    that treated zero and None alike.
    """
    assert human_bytes(0) == "0 B"


@pytest.mark.parametrize("value", [None, -1, -1024, "", "eight", object()])
def test_only_the_genuinely_unknown_is_unknown(value: object) -> None:
    """A size nobody recorded, or one that is not a number. Not zero."""
    assert human_bytes(value) == "unknown"  # type: ignore[arg-type]


def test_there_is_exactly_one_implementation() -> None:
    """The docstring claimed this for a long time while two copies existed."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "librairy"
    definitions = [
        path
        for path in root.rglob("*.py")
        if "def human_bytes(" in path.read_text(encoding="utf-8")
    ]

    assert [path.name for path in definitions] == ["humanize.py"]


def test_a_zero_saving_reads_as_zero_not_as_ignorance() -> None:
    """The case that prompted this: a remux saves nothing, and the summary
    said "Estimated potential savings unknown"."""
    assert human_bytes(0) == "0 B"
    assert "unknown" not in f"Estimated potential savings {human_bytes(0)}"
