"""A version number identifies one build, and may never identify a second.

Written because it nearly did not hold. The repository's Git tags list only
`v1.0.0`, so this tree looked free to release as `1.2.0` — the number its own
source still carried. It was not free: a `1.2.0` image was published on
2026-08-05 from a schema-10 build, and the CHANGELOG records that release even
though no tag does. Two builds thirty-seven schema generations apart would have
answered `--version` with the same number, which makes every other provenance
guarantee in this project worth nothing.

Git tags are not the release history. The changelog is the record these tests
read, because it is the one the project actually keeps.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from librairy import __version__
from librairy.db import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

#  Every released version the changelog records, newest first.
RELEASED = re.findall(r"^## v(\d+\.\d+\.\d+) - (\d{4}-\d{2}-\d{2})$", CHANGELOG, re.M)


def test_this_release_is_1_3_0_and_the_schema_is_unchanged() -> None:
    assert __version__ == "1.3.0"
    # A release number is not a schema change. 47 is what acceptance passed on.
    assert SCHEMA_VERSION == 47


def test_the_changelog_records_this_version_as_the_newest_release() -> None:
    assert RELEASED, "the changelog records no releases at all"
    newest, date = RELEASED[0]
    assert newest == __version__, f"changelog leads with v{newest}, source says {__version__}"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date)


def test_no_version_number_is_used_twice() -> None:
    """The whole point. One number, one release line."""
    numbers = [version for version, _ in RELEASED]
    assert len(numbers) == len(set(numbers)), f"a version is recorded twice: {numbers}"


def test_the_versions_already_released_are_not_available_again() -> None:
    """1.2.0 is taken. It was published from a schema-10 build on 2026-08-05,
    and it stays that build's name whether or not a Git tag ever said so."""
    previously = {version for version, _ in RELEASED[1:]}

    assert "1.2.0" in previously, "the 1.2.0 release must remain recorded"
    assert "1.0.0" in previously
    assert __version__ not in previously


def test_the_absence_of_a_git_tag_is_not_evidence_a_number_is_free() -> None:
    """The mistake this file exists to prevent, stated as an assertion.

    `v1.2.0` has no tag and never will — inventing one now would be fabricating
    history for a commit nobody released under it.
    """
    tags = subprocess.run(  # noqa: S603
        ["git", "tag", "-l"], capture_output=True, text=True, cwd=ROOT, check=False
    ).stdout.split()

    assert "v1.0.0" in tags, "the historical release tag must not be deleted or moved"
    untagged = {v for v, _ in RELEASED} - {t.lstrip("v") for t in tags}
    assert "1.2.0" in untagged, (
        "1.2.0 is a released version with no tag; if a tag appears for it, check "
        "it was not invented to represent history retroactively"
    )


def test_the_release_workflow_refuses_a_tag_that_disagrees_with_the_source() -> None:
    """Without this, pushing the wrong tag publishes an image tagged one number
    and labelled another — the registry tag comes from the Git ref, the OCI
    label comes from the Dockerfile."""
    assert "Refuse a tag that disagrees with the source version" in WORKFLOW
    assert 'tag_version="${GITHUB_REF_NAME#v}"' in WORKFLOW
    assert "does not match __version__" in WORKFLOW
    # And it has to run before anything can publish.
    steps = [
        line.strip()[len("- name: ") :]
        for line in WORKFLOW.splitlines()
        if line.strip().startswith("- name: ")
    ]
    guard = next(i for i, s in enumerate(steps) if s.startswith("Refuse a tag"))
    publishing = ("Login", "push", "Attest", "release")
    publishes = min(
        i for i, s in enumerate(steps) if any(word in s for word in publishing)
    )
    assert guard < publishes


def test_the_release_body_tells_an_operator_to_snapshot_before_upgrading() -> None:
    """The workflow publishes CHANGELOG.md verbatim, so this *is* the release
    body. A one-way schema migration that does not say so is a trap."""
    release = CHANGELOG.split(f"## v{__version__} - ", 1)[1].split("\n## v", 1)[0]

    assert "snapshot" in release.lower()
    assert "one-way" in release.lower() or "one way" in release.lower()
    assert "schema 47" in release
    for unsafe in ("just switch back", "simply start the previous image", "rollback is lossless"):
        assert unsafe not in release.lower()


def test_the_release_notes_say_no_configuration_has_to_change() -> None:
    """Thirteen settings were added since 1.2.0 and none removed. An operator
    reading the notes should not have to work that out from a diff."""
    release = CHANGELOG.split(f"## v{__version__} - ", 1)[1].split("\n## v", 1)[0]

    assert "Nothing you have\nconfigured needs changing" in release


def test_the_workflow_still_stamps_the_commit_it_built_from() -> None:
    assert "LIBRAIRY_REVISION=${{ github.sha }}" in WORKFLOW
