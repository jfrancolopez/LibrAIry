"""What an optimization has and has not done to the disk.

One helper, because these numbers are shown in six places and the temptation to
recompute them locally is exactly how "saved 338 MB" ends up on a screen while
842 MB of original is sitting untouched in Quarantine.

## The quantities, and why there are five of them

Let `O` be the original's bytes and `N` the optimized file's:

    O = 842 MB      N = 504 MB      D = O - N = 338 MB

`D` is **not** a saving. Until the original is removed, both files exist:

    representation_reduction_bytes   338 MB   the new file is this much smaller
    current_extra_storage_bytes      504 MB   what the second copy costs today
    reclaimed_now_bytes                0 B    freed so far. Zero, and it stays
                                              zero until somebody deletes
    bytes_freed_if_original_removed  842 MB   what deleting the *original*
                                              frees at that moment
    final_net_reduction_bytes        338 MB   where storage lands afterwards,
                                              against the starting point

The last two are the pair most easily confused, and they differ by more than a
factor of two here. Deleting the preserved WAV frees 842 MB *at that moment*;
the library ends up 338 MB smaller than it started. Both are true and they are
not the same number, which is why there is no field called `reclaimable`: that
word could mean either.

`reclaimed_now_bytes` is the only one of these that may ever be described to a
person as saved, and it is 0 for the entire life of this feature, because
LibrAIry does not delete anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Where the two copies stand relative to each other.
NOT_STARTED = "not-started"
# Verified output exists beside the original; nothing has been adopted.
READY = "ready"
# The optimized file is the active representation; the original is preserved.
ADOPTED = "adopted"
# The preserved original is gone. Only reachable when LibrAIry can see that it
# is: nothing here ever removes it.
ORIGINAL_REMOVED = "original-removed"


@dataclass(frozen=True)
class StorageEffect:
    """Every quantity named exactly, and none of them called "saved"."""

    state: str
    original_bytes: int
    optimized_bytes: int

    #: How much smaller the new representation is. A property of the two files,
    #: true from the moment the encode finishes, and never a disk saving.
    representation_reduction_bytes: int = 0
    #: What the second copy is costing right now, on top of the baseline.
    current_extra_storage_bytes: int = 0
    #: Actually freed, so far. Zero unless the original is genuinely gone.
    reclaimed_now_bytes: int = 0
    #: What removing the preserved original would free at that moment. Note
    #: that this is `O`, not `D` — it is the whole original file.
    bytes_freed_if_original_removed: int = 0
    #: Where storage ends up afterwards, measured against the baseline. `D`.
    final_net_reduction_bytes: int = 0
    #: What the disk holds for this piece of media today.
    physical_bytes_now: int = 0
    #: What it held before any of this started.
    baseline_bytes: int = 0

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def worth_it(self) -> bool:
        """Whether the reduction is big enough to be worth the trouble."""
        if not self.original_bytes:
            return False
        return self.final_net_reduction_bytes / self.original_bytes >= 0.10


def storage_effect(
    original_bytes: int, optimized_bytes: int, state: str = READY
) -> StorageEffect:
    """The five quantities for one optimization, from two file sizes.

    Deliberately takes sizes rather than a job row: it is arithmetic, it is
    shown in six places, and a pure function is testable against the exact
    numbers in the specification.
    """
    original = max(0, int(original_bytes))
    optimized = max(0, int(optimized_bytes))
    difference = original - optimized

    if state == NOT_STARTED:
        return StorageEffect(
            state=state,
            original_bytes=original,
            optimized_bytes=optimized,
            physical_bytes_now=original,
            baseline_bytes=original,
        )

    if state == ORIGINAL_REMOVED:
        # The only state in which anything has actually been freed.
        return StorageEffect(
            state=state,
            original_bytes=original,
            optimized_bytes=optimized,
            representation_reduction_bytes=difference,
            current_extra_storage_bytes=0,
            reclaimed_now_bytes=difference,
            bytes_freed_if_original_removed=0,
            final_net_reduction_bytes=difference,
            physical_bytes_now=optimized,
            baseline_bytes=original,
            notes=("The original is no longer stored.",),
        )

    # READY and ADOPTED hold the same bytes. What differs is which copy is the
    # active representation, not how much disk is in use — and that is the
    # whole point of separating these numbers.
    note = (
        "Both files are stored. Nothing has been freed."
        if state == ADOPTED
        else "The converted copy is kept beside the original. Nothing has been freed."
    )
    return StorageEffect(
        state=state,
        original_bytes=original,
        optimized_bytes=optimized,
        representation_reduction_bytes=difference,
        current_extra_storage_bytes=optimized,
        reclaimed_now_bytes=0,
        bytes_freed_if_original_removed=original,
        final_net_reduction_bytes=difference,
        physical_bytes_now=original + optimized,
        baseline_bytes=original,
        notes=(note,),
    )


# The words each quantity is allowed to be shown with. Held here rather than in
# six templates so that "saved" cannot quietly attach itself to the wrong one.
LABELS = {
    "representation_reduction_bytes": "Optimized version is smaller by",
    "current_extra_storage_bytes": "Extra storage used right now",
    "reclaimed_now_bytes": "Space reclaimed so far",
    "bytes_freed_if_original_removed": "Freed if you remove the original",
    "final_net_reduction_bytes": "Final reduction if you remove the original",
    "physical_bytes_now": "Stored right now",
    "baseline_bytes": "Stored before",
}

# Never applied to anything but `reclaimed_now_bytes`, which is 0 for the life
# of this feature. `tests/test_optimization_storage.py` enforces it.
BANNED_BESIDE_A_RETAINED_ORIGINAL = ("saved", "reclaimed", "freed up", "you saved")
