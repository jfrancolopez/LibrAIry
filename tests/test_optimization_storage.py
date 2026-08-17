"""The storage arithmetic, against the exact numbers it was specified with.

The whole point of this module is that "842 MB became 504 MB, saved 338 MB" is
false while the 842 MB original is preserved in Quarantine, and that the two
quantities most easily confused — what deleting the original frees (842 MB) and
where storage ends up (338 MB smaller) — differ here by more than a factor of
two.
"""

from __future__ import annotations

import pytest

from librairy.optimization_storage import (
    ADOPTED,
    BANNED_BESIDE_A_RETAINED_ORIGINAL,
    LABELS,
    NOT_STARTED,
    ORIGINAL_REMOVED,
    READY,
    storage_effect,
)

MB = 1024 * 1024
O = 842 * MB  # noqa: E741 - the specification's own names
N = 504 * MB
D = O - N  # 338 MB


def test_before_anything_happens() -> None:
    effect = storage_effect(O, N, NOT_STARTED)

    assert effect.physical_bytes_now == O
    assert effect.baseline_bytes == O
    assert effect.reclaimed_now_bytes == 0


@pytest.mark.parametrize("state", [READY, ADOPTED])
def test_while_both_copies_exist_nothing_has_been_reclaimed(state: str) -> None:
    """READY and ADOPTED hold identical bytes. Which copy is *active* changes;
    how much disk is in use does not, and that is the whole distinction."""
    effect = storage_effect(O, N, state)

    assert effect.physical_bytes_now == O + N == 1346 * MB
    assert effect.representation_reduction_bytes == D == 338 * MB
    assert effect.current_extra_storage_bytes == N == 504 * MB
    assert effect.reclaimed_now_bytes == 0
    assert effect.final_net_reduction_bytes == D == 338 * MB


@pytest.mark.parametrize("state", [READY, ADOPTED])
def test_removing_the_original_frees_the_original_not_the_difference(
    state: str,
) -> None:
    """The pair most easily confused, and they differ by more than 2x.

    Deleting the preserved WAV frees 842 MB *at that moment*. The library ends
    up 338 MB smaller than it started. Both true, not the same number — which
    is why no field is called `reclaimable`.
    """
    effect = storage_effect(O, N, state)

    assert effect.bytes_freed_if_original_removed == O == 842 * MB
    assert effect.final_net_reduction_bytes == D == 338 * MB
    assert effect.bytes_freed_if_original_removed != effect.final_net_reduction_bytes


def test_only_an_actually_removed_original_counts_as_reclaimed() -> None:
    effect = storage_effect(O, N, ORIGINAL_REMOVED)

    assert effect.reclaimed_now_bytes == D == 338 * MB
    assert effect.physical_bytes_now == N == 504 * MB
    assert effect.current_extra_storage_bytes == 0
    assert effect.bytes_freed_if_original_removed == 0


# --- the specification's own worked example ------------------------------------

SPEC_O = 100 * MB
SPEC_N = 60 * MB


def test_the_worked_example_end_to_end() -> None:
    before = storage_effect(SPEC_O, SPEC_N, NOT_STARTED)
    ready = storage_effect(SPEC_O, SPEC_N, READY)
    adopted = storage_effect(SPEC_O, SPEC_N, ADOPTED)
    after = storage_effect(SPEC_O, SPEC_N, ORIGINAL_REMOVED)

    assert before.physical_bytes_now == 100 * MB

    assert ready.physical_bytes_now == 160 * MB
    assert ready.representation_reduction_bytes == 40 * MB
    assert ready.current_extra_storage_bytes == 60 * MB
    assert ready.reclaimed_now_bytes == 0
    assert ready.final_net_reduction_bytes == 40 * MB

    # Adoption changes which representation is active. It frees nothing.
    assert adopted.physical_bytes_now == ready.physical_bytes_now
    assert adopted.reclaimed_now_bytes == 0

    assert after.bytes_freed_if_original_removed == 0
    assert after.physical_bytes_now == 60 * MB
    assert after.reclaimed_now_bytes == 40 * MB
    assert ready.bytes_freed_if_original_removed == 100 * MB


# --- the vocabulary --------------------------------------------------------------


def test_no_quantity_is_labelled_saved_or_reclaimable() -> None:
    """`reclaimable` is banned because it could mean either 842 or 338."""
    for field, label in LABELS.items():
        assert "reclaimable" not in label.lower(), field
        if field != "reclaimed_now_bytes":
            assert "saved" not in label.lower(), field
            assert "reclaimed" not in label.lower(), field


def test_the_one_quantity_that_may_be_called_reclaimed_is_zero_until_removal() -> None:
    for state in (NOT_STARTED, READY, ADOPTED):
        assert storage_effect(O, N, state).reclaimed_now_bytes == 0
    assert "reclaimed" in LABELS["reclaimed_now_bytes"].lower()


@pytest.mark.parametrize("word", BANNED_BESIDE_A_RETAINED_ORIGINAL)
def test_no_template_claims_a_saving_beside_a_retained_original(word: str) -> None:
    """The rule this module exists to keep. A page may say a file is smaller;
    it may not say space was saved while both copies are on the disk.

    Scoped to the pages that actually show these figures. `settings.html`
    mentions optimization and also says "Saved" about settings being applied,
    which is a different word doing a different job.
    """
    from pathlib import Path

    pages = [
        Path("src/librairy/web/templates/optimization.html"),
        *Path("src/librairy/web/templates/partials").glob("*storage*.html"),
    ]
    import re

    for page in pages:
        # Rendered copy only. A variable named `reclaimed_label` is not a claim
        # to a reader, and Jinja expressions and comments are not on screen.
        text = page.read_text(encoding="utf-8")
        text = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", "", text, flags=re.S).lower()
        # "Space reclaimed so far" is the one permitted use, and it is the one
        # quantity that is genuinely zero until an original is removed.
        text = text.replace("space reclaimed so far", "")
        assert word not in text, f"{page.name} claims '{word}'"


# --- the judgement built on the arithmetic -----------------------------------------


def test_a_three_percent_result_is_not_worth_it() -> None:
    effect = storage_effect(6200 * MB, 6014 * MB, READY)

    assert effect.representation_reduction_bytes == 186 * MB
    assert effect.worth_it is False


def test_a_forty_percent_result_is() -> None:
    assert storage_effect(O, N, READY).worth_it is True


def test_an_output_larger_than_its_source_is_handled(tmp_path=None) -> None:
    """An encode can come out bigger. The arithmetic must not go strange."""
    effect = storage_effect(100 * MB, 130 * MB, READY)

    assert effect.representation_reduction_bytes == -30 * MB
    assert effect.final_net_reduction_bytes == -30 * MB
    assert effect.reclaimed_now_bytes == 0
    assert effect.worth_it is False
