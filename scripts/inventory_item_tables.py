"""Every table tied to an `items` row, and what adoption does with it.

A representation change — WAV to FLAC, MKV to MP4, H.264 to HEVC — produces a
new file with a new `items` row. The question this answers is what, if
anything, that row should inherit, and the answer has to be per-table: some of
these describe *bytes* and some describe *the thing the file is a copy of*.

Three sources, because none of them alone is complete:

    1. foreign keys declared in the schema
    2. tables created lazily at first use, which no PRAGMA on a fresh database
       will show — `item_metadata` is one, and it is the table most likely to
       be assumed absent
    3. tables keyed to an item by convention with no FK — the FTS shadows

    .venv/bin/python scripts/inventory_item_tables.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from librairy.config import Settings  # noqa: E402
from librairy.db import connect  # noqa: E402
from librairy.optimization_adopt import CARRIED, NEVER_CARRIED  # noqa: E402
from librairy.tools.common import ensure_metadata_cache  # noqa: E402

# The classification, with the reason. `carry` is the decision adoption acts
# on; everything else is here so the decision can be checked rather than
# trusted.
CLASSIFIED = {
    "vision_results": (
        "byte", "no",
        "Keyed by fingerprint. A caption computed from the WAV's bytes attached"
        " to the FLAC's bytes asserts that something looked at bytes nothing"
        " has looked at.",
    ),
    "content_extractions": (
        "byte", "no",
        "Keyed by fingerprint, same argument. Re-extraction is cheap and honest.",
    ),
    "item_metadata": (
        "byte", "no",
        "The ffprobe cache: codec, bitrate, duration, channels, sample format."
        " Every field is a property of the encoding that just changed. Reads"
        " require a fingerprint match, so it self-invalidates even if a future"
        " change did copy it.",
    ),
    "audit_findings": (
        "byte", "no",
        "Statements about a specific file at a specific path. The next audit"
        " re-derives them from the file that is now there.",
    ),
    "duplicate_reports": (
        "byte", "no",
        "A claim that two specific files are copies of each other. The"
        " optimized file is not a byte copy of anything.",
    ),
    "similar_media_flags": (
        "byte", "no",
        "Same: a claim about a pair of files, scored from their bytes.",
    ),
    "similar_media_choices": (
        "byte", "no",
        "A half-made selection inside one visual group: which photographs the"
        " person wants to keep. Attached to a finding about specific files, and"
        " an optimized copy is not one of them — carrying the row would mean an"
        " answer somebody gave about a picture applied to a picture they never"
        " saw.",
    ),
    "track_identity": (
        "byte", "no",
        "What one audio file was identified as, recorded against the exact"
        " bytes it was identified from. The recording is arguably the same"
        " work after a re-encode — but the row is only read when its"
        " fingerprint still matches, so a carried copy would be a dead row"
        " that looks like evidence. Re-identifying is one deliberate action.",
    ),
    "decision_events": (
        "neither", "no",
        "What the owner chose and under what cues. `item_id` points at the file"
        " the decision was *about*, which is the original — the optimized copy"
        " was never the subject of that decision. Carrying it would claim"
        " somebody made a choice about a file that did not exist yet. The"
        " lesson itself is unaffected either way: it is keyed by cues, not by"
        " item, and it still describes the same kind of file.",
    ),
    "item_relationships": (
        "identity", "no",
        "A companion pairing: this subtitle names that video, this cover"
        " belongs to that album. Not a claim about bytes — but it names two"
        " specific item ids, and an adoption's result is a different item, so"
        " a carried row would assert a pairing nothing has established about"
        " the new file. The cost is presentation only: companion *filing*"
        " reads the classifier's filename matching and not this table, so no"
        " sidecar is left behind by a commit. Carrying it belongs with making"
        " this table the source of that filing.",
    ),
    "backup_queue": (
        "byte", "no",
        "A request to copy specific bytes to a remote. The executor makes one"
        " for whatever lands in the library, so the result gets its own"
        " through the normal path.",
    ),
    "proposals": (
        "neither", "no",
        "An inbox-review decision about where a file should go. The result is"
        " already filed at the destination that decision produced.",
    ),
    "plan_ops": (
        "neither", "historic",
        "The journal of what moved. Adoption writes its own two rows; older"
        " ones stay attached to the original item and must.",
    ),
    "reconciliations": (
        "neither", "historic",
        "A record that somebody agreed a file is at a new path. It is about an"
        " identity rather than about bytes, and an optimized copy is a"
        " different identity that has never been anywhere — so it has nothing"
        " to inherit and must not appear to have moved.",
    ),
    "quarantine_entries": (
        "neither", "historic",
        "Belongs to the original, which is what gets preserved. The result"
        " never has one.",
    ),
    "optimization_opportunities": (
        "byte", "no",
        "An offer to optimize specific bytes. The result is the output of one,"
        " not a candidate for another.",
    ),
    "optimization_jobs": (
        "neither", "link",
        "Not inherited — created. `result_item_id` is written pointing at the"
        " result, which is the lineage that lets Undo and re-adoption find"
        " each other.",
    ),
    "search_fts": (
        "derived", "recompute",
        "Rebuilt from the item and its path by `sync_search_item`. Category"
        " comes from the path, which is why a representation change needs no"
        " carry to stay correctly categorised.",
    ),
    "content_fts": (
        "byte", "no",
        "The shadow of content_extractions, which is not carried.",
    ),
    "catalog_identity": (
        "identity", "automatic",
        "TMDB / MusicBrainz. NOT item-linked: keyed by (scope_kind, scope_key)"
        " where scope_key is the library-relative FOLDER. Adoption keeps the"
        " file in its folder, so the identity is neither carried nor lost — it"
        " was never attached to the item to begin with.",
    ),
    "library_patterns": (
        "identity", "automatic",
        "Learned destinations, keyed by (kind, key) — an artist or show name,"
        " not an item. Unaffected.",
    ),
    "groups": (
        "neither", "no",
        "Reached only through `proposals.group_id`, which is not carried.",
    ),
    "history": (
        "neither", "historic",
        "Keyed by plan and op, not by item. Adoption appends; nothing is"
        " rewritten.",
    ),
    "review_undo": (
        "neither", "historic",
        "A snapshot of a Review action. Never touched here.",
    ),
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(
        APPDATA_DIR=tmp / "appdata", INBOX_DIR=tmp / "inbox",
        LIBRARY_DIR=tmp / "library", QUARANTINE_DIR=tmp / "quarantine",
        _env_file=None,
    )
    for d in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        d.mkdir(parents=True)
    conn = connect(settings)
    # Source 2: it does not exist until something caches a probe.
    lazy_before = _tables(conn)
    ensure_metadata_cache(conn)
    from librairy.indexer import _ensure_pattern_table

    _ensure_pattern_table(conn)
    lazy = sorted(_tables(conn) - lazy_before)

    fk_tables: dict[str, list[str]] = {}
    for name in sorted(_tables(conn)):
        for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")'):
            if fk["table"] == "items":
                fk_tables.setdefault(name, []).append(fk["from"])

    # Source 3: item_id by convention, no FK possible (virtual tables).
    convention = sorted(
        name
        for name in _tables(conn)
        if name not in fk_tables
        and any(c["name"] == "item_id" for c in conn.execute(f'PRAGMA table_info("{name}")'))
    )

    print("--- declared foreign keys into items.id ---")
    for name, cols in fk_tables.items():
        print(f"  {name:30} {', '.join(cols)}")
    print(f"\n--- created lazily, absent from a fresh schema ---\n  {', '.join(lazy)}")
    print(f"\n--- item_id by convention, no FK ---\n  {', '.join(convention)}")

    known = set(fk_tables) | set(convention) | set(lazy) | {
        "catalog_identity", "groups", "history", "review_undo",
    }
    shadow = ("_data", "_idx", "_docsize", "_config", "_content")
    known = {n for n in known if not n.endswith(shadow)}
    missing = known - set(CLASSIFIED)
    extra = set(CLASSIFIED) - known
    print(f"\nclassified {len(CLASSIFIED)} · unclassified {sorted(missing)}"
          f" · stale {sorted(extra)}")

    print("\n--- the decision ---")
    print(f"{'TABLE':30} {'KIND':10} {'CARRY':11} WHY")
    for name in sorted(CLASSIFIED):
        kind, carry, why = CLASSIFIED[name]
        print(f"{name:30} {kind:10} {carry:11} {why}")

    print(f"\nNEVER_CARRIED asserts: {', '.join(NEVER_CARRIED)}")
    print(f"CARRIED asserts:       {', '.join(CARRIED) or '(nothing)'}")
    return 1 if missing else 0


def _tables(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


if __name__ == "__main__":
    raise SystemExit(main())
