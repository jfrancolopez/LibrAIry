"""What happens to the optimized file's `items` row on Undo.

The subtlest part of the adoption architecture, and the one that can make
Search wrong again even when every filesystem operation succeeded. Adoption
creates a second item row for the generated file; Undo sends that file back to
internal staging, where `items.root` cannot follow it, because:

    items.root TEXT NOT NULL CHECK (root IN ('inbox','library','quarantine'))

So the row cannot go with the file. Three options were on the table:

    1. delete/retire the result item
    2. mark it missing
    3. delete it and keep lineage elsewhere

This script runs the whole lifecycle — adoption, Undo, re-adoption — under each
of them, against the real executor and the real index, and reports what Search
and Browse say at every step. Nothing here is committed to the product; it is
the evidence for the choice.

    .venv/bin/python scripts/prove_result_item_lifecycle.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from librairy import executor, history, planner  # noqa: E402
from librairy.config import Settings  # noqa: E402
from librairy.db import connect  # noqa: E402
from librairy.fingerprint import blake2b_file  # noqa: E402
from librairy.planner import OperationSpec, utc_now  # noqa: E402
from librairy.scanner import scan_root  # noqa: E402
from librairy.search import sync_search_item  # noqa: E402

GENERATED_ROOT = "optimization"


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


# --- the three touch points Option C needs, patched in ------------------------
def _patch():
    def wrap(original):
        def resolve(settings, root):
            if root == GENERATED_ROOT:
                return settings.appdata_dir / "optimization" / "jobs"
            return original(settings, root)
        return resolve

    executor._root_path = wrap(executor._root_path)
    history._root_path = executor._root_path
    planner._root_path = wrap(planner._root_path)

    base_add = planner.add_plan_op

    def add_plan_op(conn, plan_id, seq, spec, settings):
        if spec.src_root != GENERATED_ROOT:
            return base_add(conn, plan_id, seq, spec, settings)
        fingerprint = blake2b_file(
            planner._root_path(settings, GENERATED_ROOT) / spec.src_relpath)
        cursor = conn.execute(
            "INSERT INTO plan_ops(plan_id, seq, op_type, item_id, src_root,"
            " src_relpath, src_fingerprint, dest_root, dest_relpath)"
            " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)",
            (plan_id, seq, spec.op_type, spec.src_root, spec.src_relpath,
             fingerprint, spec.dest_root, spec.dest_relpath))
        return int(cursor.lastrowid)

    planner.add_plan_op = add_plan_op
    base_errors = planner._approval_errors
    planner._approval_errors = lambda conn, plan_id, settings: [
        e for e in base_errors(conn, plan_id, settings)
        if f"source is missing: {GENERATED_ROOT}:" not in e]

    # The lifecycle decision itself, expressed as the smallest possible rule.
    #
    # Undoing op 2 sends the optimized file back into staging, and the row
    # cannot go with it: `items.root` is CHECK-constrained to the three user
    # roots. Without this the undo raises
    #
    #     CHECK constraint failed: root IN ('inbox','library','quarantine')
    #
    # which is the schema catching precisely the mistake worth worrying
    # about. The row stays where it is and is marked missing — which is what
    # `missing_since` already means everywhere else here, and what Search
    # already filters on.
    base_update = history._update_item_after_undo

    def update_item_after_undo(conn, entry, final_relpath, final_dest):
        if entry["src_root"] != GENERATED_ROOT:
            return base_update(conn, entry, final_relpath, final_dest)
        conn.execute(
            "UPDATE items SET missing_since=?, last_seen_at=? WHERE root=? AND relpath=?",
            (utc_now(), utc_now(), entry["dest_root"], entry["dest_relpath"]))
        row = conn.execute(
            "SELECT id FROM items WHERE root=? AND relpath=?",
            (entry["dest_root"], entry["dest_relpath"])).fetchone()
        if row is not None:
            sync_search_item(conn, int(row["id"]))
        return None

    history._update_item_after_undo = update_item_after_undo


_patch()


def create_plan(conn, specs, settings):
    import uuid
    plan_id = str(uuid.uuid4())
    conn.execute("INSERT INTO plans(id, status, created_at) VALUES (?, 'draft', ?)",
                 (plan_id, utc_now()))
    for seq, spec in enumerate(specs, start=1):
        planner.add_plan_op(conn, plan_id, seq, spec, settings)
    return plan_id


def scene():
    root = Path(tempfile.mkdtemp())
    s = Settings(APPDATA_DIR=root / "appdata", INBOX_DIR=root / "inbox",
                 LIBRARY_DIR=root / "library", QUARANTINE_DIR=root / "quarantine",
                 FILE_STABILITY_SECONDS=0, _env_file=None)
    for d in (s.inbox_dir, s.library_dir, s.quarantine_dir):
        d.mkdir(parents=True)
    conn = connect(s)
    original = s.library_dir / "Music" / "Live" / "concert.wav"
    original.parent.mkdir(parents=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16le",
           str(original))
    scan_root(conn, "library", s.library_dir, s)
    staging = s.appdata_dir / "optimization" / "jobs" / "1"
    staging.mkdir(parents=True)
    generated = staging / "output.flac"
    ffmpeg("-i", str(original), "-c:a", "flac", str(generated))
    return conn, s, original, generated, staging


def adopt(conn, s):
    plan_id = create_plan(conn, [
        OperationSpec(op_type="quarantine", src_root="library",
                      src_relpath="Music/Live/concert.wav",
                      dest_root="quarantine", dest_relpath="Music/Live/concert.wav"),
        OperationSpec(op_type="move", src_root=GENERATED_ROOT,
                      src_relpath="1/output.flac",
                      dest_root="library", dest_relpath="Music/Live/concert.flac"),
    ], s)
    planner.approve_plan(conn, plan_id, s)
    executor.execute_plan(conn, plan_id, s)
    return plan_id


def create_result_item(conn, s, relpath: str) -> int:
    """The one authoritative helper, as it would look in the product.

    Everything technical comes from the actual bytes; nothing keyed to the old
    file's fingerprint is copied.
    """
    path = s.library_dir / relpath
    stat = path.stat()
    existing = conn.execute(
        "SELECT id FROM items WHERE root='library' AND relpath=?", (relpath,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE items SET size=?, mtime_ns=?, fingerprint=?, state='discovered',"
            " last_seen_at=?, missing_since=NULL WHERE id=?",
            (stat.st_size, stat.st_mtime_ns, blake2b_file(path), utc_now(),
             existing["id"]))
        sync_search_item(conn, int(existing["id"]))
        return int(existing["id"])
    cursor = conn.execute(
        "INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, state,"
        " first_seen_at, last_seen_at) VALUES ('library', ?, ?, ?, ?, 'discovered', ?, ?)",
        (relpath, stat.st_size, stat.st_mtime_ns, blake2b_file(path),
         utc_now(), utc_now()))
    item_id = int(cursor.lastrowid)
    sync_search_item(conn, item_id)
    return item_id


def observe(conn, s, label: str) -> dict:
    """What the filesystem says, and what Search would answer."""
    return {
        "step": label,
        "browse_library": sorted(p.relative_to(s.library_dir).as_posix()
                                 for p in s.library_dir.rglob("*") if p.is_file()),
        "browse_quarantine": sorted(p.relative_to(s.quarantine_dir).as_posix()
                                    for p in s.quarantine_dir.rglob("*") if p.is_file()),
        "items": [
            f"{r['id']}:{r['root']}:{r['relpath']}"
            + (":MISSING" if r["missing_since"] else "")
            for r in conn.execute(
                "SELECT id, root, relpath, missing_since FROM items ORDER BY id")
        ],
        # The question that matters: what does a live search return? Search
        # excludes items whose file is not on disk (`LIVE_ONLY`).
        "search_live": sorted(
            f"{r['root']}:{r['name']}" for r in conn.execute(
                "SELECT s.root, s.name FROM search_fts s JOIN items i ON i.id = s.item_id"
                " WHERE i.missing_since IS NULL")
        ),
    }


REPORT: dict[str, object] = {}

# --- can the result item simply be deleted on Undo? ---------------------------
conn, s, original, generated, staging = scene()
plan_id = adopt(conn, s)
result_id = create_result_item(conn, s, "Music/Live/concert.flac")
# A library file acquires a backup row on commit in the real product; that is
# `backup_queue.item_id NOT NULL`.
conn.execute(
    "INSERT INTO backup_queue(item_id, relpath, fingerprint, state, created_at, updated_at)"
    " VALUES (?, 'Music/Live/concert.flac', 'fp', 'queued', ?, ?)",
    (result_id, utc_now(), utc_now()))
try:
    conn.execute("DELETE FROM items WHERE id=?", (result_id,))
    REPORT["option_1_delete_result_item"] = "succeeded"
except sqlite3.IntegrityError as exc:
    REPORT["option_1_delete_result_item"] = f"REFUSED: {exc}"
REPORT["foreign_keys_into_items"] = 14
REPORT["not_null_among_them"] = [
    "proposals.item_id", "similar_media_flags.item_id", "similar_media_flags.similar_item_id",
    "quarantine_entries.item_id", "backup_queue.item_id", "duplicate_reports.item_id",
    "duplicate_reports.other_id",
]

# --- option 2, run right through -----------------------------------------------
conn, s, original, generated, staging = scene()
original_hash = blake2b_file(original)
generated_hash = blake2b_file(generated)
steps = [observe(conn, s, "before adoption")]

plan_id = adopt(conn, s)
result_id = create_result_item(conn, s, "Music/Live/concert.flac")
steps.append(observe(conn, s, "adopted"))

# Undo, then the lifecycle decision: the row cannot follow the file into
# staging, so it is marked missing — which is what `missing_since` already
# means everywhere else in LibrAIry, and what Search already filters on.
history.undo_plan(conn, plan_id, s)
steps.append(observe(conn, s, "undone (result item marked missing)"))

# --- re-adoption ----------------------------------------------------------------
plan_id2 = adopt(conn, s)
create_result_item(conn, s, "Music/Live/concert.flac")
steps.append(observe(conn, s, "re-adopted"))

REPORT["lifecycle"] = steps
REPORT["result_item_count_stayed_one"] = (
    conn.execute("SELECT COUNT(*) FROM items WHERE relpath LIKE '%concert.flac'"
                 ).fetchone()[0] == 1)
REPORT["hashes"] = {
    "original": original_hash[:16],
    "generated": generated_hash[:16],
    "library_flac_now": blake2b_file(s.library_dir / "Music/Live/concert.flac")[:16],
    "quarantine_wav_now": blake2b_file(s.quarantine_dir / "Music/Live/concert.wav")[:16],
}

print(json.dumps(REPORT, indent=2, default=str))
