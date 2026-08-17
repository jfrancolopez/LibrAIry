"""Does the EXISTING executor adopt a generated file without a second workflow?

Option C's claim: the verified output never moves before the plan exists. It is
read in place, from a root only the executor can resolve, by a plan that names
the job and the output's own hash.

Nothing is committed to the repo by this script. The three touch points Option C
would need are monkeypatched in process, so the cost of the option is *measured*
rather than estimated, and the executor, planner, history and undo used below
are the real ones.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from librairy import executor, history, planner  # noqa: E402
from librairy.config import Settings  # noqa: E402
from librairy.db import connect  # noqa: E402
from librairy.fingerprint import blake2b_file  # noqa: E402
from librairy.planner import OperationSpec  # noqa: E402
from librairy.scanner import scan_root  # noqa: E402

GENERATED_ROOT = "optimization"
REPORT: dict[str, object] = {}
TOUCHED: list[str] = []


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True)


# --- touch point 1: three root resolvers gain one branch each -----------------
def patched_root_path(original):
    def resolve(settings, root):
        if root == GENERATED_ROOT:
            return settings.appdata_dir / "optimization" / "jobs"
        return original(settings, root)
    return resolve


executor._root_path = patched_root_path(executor._root_path)
history._root_path = executor._root_path
planner._root_path = patched_root_path(planner._root_path)
TOUCHED.append("executor._root_path / planner._root_path (+1 branch, shared by history)")


# --- touch point 2: a source with no item row, carrying its own fingerprint ---
_add_plan_op = planner.add_plan_op


def add_plan_op(conn, plan_id, seq, spec, settings):
    if spec.src_root != GENERATED_ROOT:
        return _add_plan_op(conn, plan_id, seq, spec, settings)
    fingerprint = blake2b_file(
        planner._root_path(settings, GENERATED_ROOT) / spec.src_relpath
    )
    cursor = conn.execute(
        """
        INSERT INTO plan_ops(
          plan_id, seq, op_type, item_id, src_root, src_relpath, src_fingerprint,
          dest_root, dest_relpath
        ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (plan_id, seq, spec.op_type, spec.src_root, spec.src_relpath,
         fingerprint, spec.dest_root, spec.dest_relpath),
    )
    return int(cursor.lastrowid)


planner.add_plan_op = add_plan_op
TOUCHED.append("planner.add_plan_op (generated source: no item row, explicit hash)")

_approval_errors = planner._approval_errors


def approval_errors(conn, plan_id, settings):
    return [
        error for error in _approval_errors(conn, plan_id, settings)
        if f"source is missing: {GENERATED_ROOT}:" not in error
    ]


planner._approval_errors = approval_errors
TOUCHED.append("planner._approval_errors (same exemption)")

# `create_plan` closed over the old add_plan_op at import; rebind.
def create_plan(conn, specs, settings):
    import uuid
    plan_id = str(uuid.uuid4())
    conn.execute("INSERT INTO plans(id, status, created_at) VALUES (?, 'draft', ?)",
                 (plan_id, planner.utc_now()))
    for seq, spec in enumerate(specs, start=1):
        planner.add_plan_op(conn, plan_id, seq, spec, settings)
    return plan_id


# --- the scene ----------------------------------------------------------------
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
original_hash = blake2b_file(original)
original_bytes = original.stat().st_size

staging = s.appdata_dir / "optimization" / "jobs" / "1"
staging.mkdir(parents=True)
generated = staging / "output.flac"
ffmpeg("-i", str(original), "-c:a", "flac", str(generated))
generated_hash = blake2b_file(generated)
generated_bytes = generated.stat().st_size

REPORT["touch_points_option_c_needs"] = TOUCHED
REPORT["sizes"] = {"original": original_bytes, "generated": generated_bytes}

# --- the plan -----------------------------------------------------------------
plan_id = create_plan(
    conn,
    [
        # op1 preserves the original. op2 admits the generated file. Order
        # matters and `undo_plan` reverses it, which is what frees the slot.
        OperationSpec(op_type="quarantine", src_root="library",
                      src_relpath="Music/Live/concert.wav",
                      dest_root="quarantine",
                      dest_relpath="Music/Live/concert.wav"),
        OperationSpec(op_type="move", src_root=GENERATED_ROOT,
                      src_relpath="1/output.flac",
                      dest_root="library",
                      dest_relpath="Music/Live/concert.flac"),
    ],
    s,
)
REPORT["ops"] = [
    {**dict(o), "src_fingerprint": (o["src_fingerprint"] or "")[:12]}
    for o in conn.execute(
        "SELECT seq, op_type, src_root, src_relpath, src_fingerprint, dest_root,"
        " dest_relpath, item_id FROM plan_ops WHERE plan_id=? ORDER BY seq", (plan_id,))
]
REPORT["generated_hash_is_the_op_fingerprint"] = (
    REPORT["ops"][1]["src_fingerprint"] == generated_hash[:12]
)
REPORT["plan_hash"] = planner.approve_plan(conn, plan_id, s)[:16]

# --- execute ------------------------------------------------------------------
summary = executor.execute_plan(conn, plan_id, s)
REPORT["execute"] = {
    "done": summary.done, "failed": summary.failed,
    "skipped_changed": summary.skipped_changed,
    "skipped_missing": summary.skipped_missing,
    "renamed_collision": summary.renamed_collision,
}


def tree():
    return {
        "library": sorted(p.relative_to(s.library_dir).as_posix()
                          for p in s.library_dir.rglob("*") if p.is_file()),
        "quarantine": sorted(p.relative_to(s.quarantine_dir).as_posix()
                             for p in s.quarantine_dir.rglob("*") if p.is_file()),
        "staging": sorted(p.name for p in staging.rglob("*") if p.is_file()),
    }


REPORT["after_commit"] = tree()
flac = s.library_dir / "Music/Live/concert.flac"
wav_q = s.quarantine_dir / "Music/Live/concert.wav"
REPORT["optimized_matches_verified_output"] = (
    flac.exists() and blake2b_file(flac) == generated_hash)
REPORT["original_bytes_preserved"] = (
    wav_q.exists() and blake2b_file(wav_q) == original_hash)
REPORT["items_after_commit"] = [
    dict(r) for r in conn.execute("SELECT id, root, relpath, state FROM items ORDER BY id")]
REPORT["search_after_commit"] = [
    dict(r) for r in conn.execute("SELECT item_id, name, root FROM search_fts")]
REPORT["quarantine_entries"] = [
    dict(r) for r in conn.execute(
        "SELECT id, item_id, reason, original_root, original_relpath FROM quarantine_entries")]
REPORT["history_after_commit"] = [
    dict(r) for r in conn.execute(
        "SELECT action, src_root, src_relpath, dest_root, dest_relpath, outcome"
        " FROM history ORDER BY id")]

# --- undo ---------------------------------------------------------------------
results = history.undo_plan(conn, plan_id, s)
REPORT["undo"] = [(r.outcome, r.dest_relpath) for r in results]
REPORT["after_undo"] = tree()
wav_back = s.library_dir / "Music/Live/concert.wav"
REPORT["undo_restored_exact_original"] = (
    wav_back.exists() and blake2b_file(wav_back) == original_hash)
REPORT["undo_put_generated_back_in_its_job_staging"] = (
    generated.exists() and blake2b_file(generated) == generated_hash)
REPORT["items_after_undo"] = [
    dict(r) for r in conn.execute("SELECT id, root, relpath, state FROM items ORDER BY id")]

print(json.dumps(REPORT, indent=2, default=str))
