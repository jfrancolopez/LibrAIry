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


def _tags() -> list[str]:
    return subprocess.run(  # noqa: S603
        ["git", "tag", "-l"], capture_output=True, text=True, cwd=ROOT, check=False
    ).stdout.split()


def test_this_release_is_1_3_1_and_the_schema_moved_eleven_times_since() -> None:
    """The released version, and the schema main is on.

    A release number is not a schema change and a schema change is not a
    release: 1.3.1 shipped on 47, which is what acceptance passed on. Eleven
    unreleased migrations since, and each one is a sentence:

    * **48** indexes the other end of a `similar_media_flags` pair, so Review
      can find an arrival's twin by a seek instead of a scan.
    * **49** stores each proposal's attention tier, so "24 settled by identity"
      is a count on an index rather than a scan of every evidence blob.
    * **50** indexes `history` by destination, which fifty search results asked
      about one at a time over an unindexed pair.
    * **51** records why a file was held instead of guessed at, so "waiting for
      AI" is a durable state with a reason rather than a line in a log.
    * **52** stores the habits somebody promoted into filing policies, which is
      the one thing in Decision Memory a count may never create.
    * **53** keeps a hashtag against the item rather than against the path it
      was written on, so filing a file no longer forgets what it was tagged.
    * **54** carries why two documents might be one decision, and widens what
      a group is allowed to be — a book series is not an album, and calling
      it one to avoid a table rebuild would put a wrong word in the data.
    * **55** is the first table in the program with a memory of size: one row
      per metric per day, so "was the backlog smaller last Tuesday" has an
      answer. Nothing operational reads it; losing it costs trends and
      nothing else.
    * **56** says where library content is copied to and what each place is
      for. Three modes, none of which can express deleting anything — see
      `librairy/destinations.py`.
    * **57** adds the other half of an offline drive's identity: the marker
      file says it was registered with us and can be cloned, the volume id
      says it is the same filesystem and is not available everywhere.
    * **58** records what each backup run did — and deliberately has no
      "this destination is up to date" column, because a flag like that
      only has to be wrong once. Comparing answers it instead.

    The number is written down here so that changing it is a deliberate act
    with a sentence attached, rather than something noticed at upgrade time.
    """
    assert __version__ == "1.3.1"
    assert SCHEMA_VERSION == 58


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


def test_no_tag_is_ever_invented_for_a_release_that_never_had_one() -> None:
    """`v1.2.0` has no tag and must never gain one.

    1.2.0 was published without going through the release workflow, so no commit
    was ever released *under that tag*. Creating one now would fabricate history
    — it would point at whichever commit looked right today, which is precisely
    the guess this file exists to stop.

    Deliberately not asserting that any tag is *present*: a tag-triggered CI
    checkout fetches only the tag that triggered it, so "which tags exist" is a
    fact about the checkout, not about the project. This says only what must
    never appear, which is true in every checkout.
    """
    tags = set(_tags())

    assert "v1.2.0" not in tags, (
        "a v1.2.0 tag has appeared; no commit was released under that name, so "
        "this can only have been invented after the fact"
    )


def test_the_historical_release_tag_still_names_the_commit_it_always_did() -> None:
    """Checked only where the tag is actually fetched — a shallow, tag-triggered
    CI checkout does not have it, and its absence there means nothing."""
    if "v1.0.0" not in set(_tags()):
        return
    target = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "v1.0.0^{commit}"],
        capture_output=True, text=True, cwd=ROOT, check=False,
    ).stdout.strip()

    assert target == "21bab76d18ce5eb072f020f6284aabbbbd2ad354", (
        f"v1.0.0 now points at {target}; a published release tag is never moved"
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


def test_a_tag_that_published_nothing_is_not_recorded_as_a_release() -> None:
    """The 1.2.0 lesson, in the other direction.

    `v1.3.0` was tagged and its release run failed in the test step before any
    login, push or release creation. The tag was left where it was rather than
    moved. So a tag exists that names no published build — and the changelog,
    which is this project's record of what shipped, must not claim otherwise.
    A future reader has to be able to tell the two apart without guessing.
    """
    released = {version for version, _ in RELEASED}

    assert "1.3.0" not in released, "1.3.0 published nothing and is not a release"
    assert __version__ in released
    # And the release notes say what happened, so nobody has to reconstruct it.
    notes = CHANGELOG.split(f"## v{__version__} - ", 1)[1].split("\n## v", 1)[0]
    assert "v1.3.0" in notes
    assert "first published release of this line" in notes


def test_the_workflow_guard_would_refuse_the_abandoned_tag() -> None:
    """Pushing `v1.3.0` again must not publish: the source now says 1.3.1, and
    the guard compares the two before it logs in to any registry."""
    assert 'tag_version="${GITHUB_REF_NAME#v}"' in WORKFLOW
    assert '"${tag_version}" != "${source_version}"' in WORKFLOW
    # The changelog check is the second half: v1.3.0 has no released section.
    assert 'grep -q "^## ${GITHUB_REF_NAME} - " CHANGELOG.md' in WORKFLOW
    assert not re.search(r"^## v1\.3\.0 - ", CHANGELOG, re.M)
