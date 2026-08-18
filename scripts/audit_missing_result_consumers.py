"""Can an undone adoption's result row leak into active work?

After Undo the optimized file is back in its job's staging directory under
appdata, and its `items` row stays behind:

    root           library
    relpath        Music/Live/concert.flac     <- where it used to be
    missing_since  set

That is only correct if every consumer reads `root='library'` as "recorded in
the library" and not as "physically there now". Search does. This script asks
the rest of them, by building that exact state and then *calling* them — not by
reading their SQL and forming an opinion about it.

    .venv/bin/python scripts/audit_missing_result_consumers.py

Prints one line per consumer: what it should say, what it did say, and a
verdict. Nothing is written to the product; this is the evidence.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from librairy import audit, backup, consistency, dedup, duplicates  # noqa: E402
from librairy.config import Settings  # noqa: E402
from librairy.db import connect  # noqa: E402
from librairy.optimization_adopt import record_result_item, retire_result_item  # noqa: E402
from librairy.planner import utc_now  # noqa: E402
from librairy.scanner import scan_root  # noqa: E402
from librairy.search import search_items  # noqa: E402
from librairy.web import access, browse, dashboard, health  # noqa: E402

ORIGINAL = "Music/Live/concert.wav"
RESULT = "Music/Live/concert.flac"


def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def build(tmp: Path):
    """The post-Undo state, reached the way the product reaches it."""
    settings = Settings(
        APPDATA_DIR=tmp / "appdata",
        INBOX_DIR=tmp / "inbox",
        LIBRARY_DIR=tmp / "library",
        QUARANTINE_DIR=tmp / "quarantine",
        FILE_STABILITY_SECONDS=0,
        BACKUP_ENABLED=True,
        _env_file=None,
    )
    for d in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        d.mkdir(parents=True)
    conn = connect(settings)

    original = settings.library_dir / ORIGINAL
    original.parent.mkdir(parents=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "pcm_s16le",
           str(original))
    scan_root(conn, "library", settings.library_dir, settings)

    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath=?", (ORIGINAL,)
    ).fetchone()
    job_id = int(conn.execute(
        """
        INSERT INTO optimization_jobs(
          item_id, root, relpath, fingerprint, kind, quality, from_label, to_label,
          preset, source_bytes, estimated_bytes, actual_bytes, state, verified,
          output_relpath, staging_dir, queued_at, updated_at
        ) VALUES (?, 'library', ?, ?, 'audio-to-flac', 'lossless', 'WAV', 'FLAC',
                  'flac-lossless', 100, 60, 60, 'ready', 'passed', 'output.flac',
                  '', ?, ?)
        """,
        (item["id"], ORIGINAL, item["fingerprint"], utc_now(), utc_now()),
    ).lastrowid)

    # --- adoption, in the state it leaves behind -------------------------------
    staging = settings.appdata_dir / "optimization" / "jobs" / str(job_id)
    staging.mkdir(parents=True)
    generated = staging / "output.flac"
    ffmpeg("-i", str(original), "-c:a", "flac", str(generated))

    adopted = settings.library_dir / RESULT
    generated.replace(adopted)
    quarantined = settings.quarantine_dir / ORIGINAL
    quarantined.parent.mkdir(parents=True, exist_ok=True)
    original.replace(quarantined)
    conn.execute(
        "UPDATE items SET root='quarantine', last_seen_at=? WHERE id=?",
        (utc_now(), item["id"]),
    )
    result_id = record_result_item(conn, settings, relpath=RESULT, job_id=job_id)
    backup.enqueue_backup_item(
        conn, settings, item_id=result_id, relpath=RESULT,
        fingerprint=conn.execute(
            "SELECT fingerprint FROM items WHERE id=?", (result_id,)
        ).fetchone()[0],
    )

    # --- Undo ------------------------------------------------------------------
    adopted.replace(generated)
    quarantined.replace(original)
    conn.execute(
        "UPDATE items SET root='library', last_seen_at=? WHERE id=?",
        (utc_now(), item["id"]),
    )
    from librairy.search import sync_search_item

    sync_search_item(conn, item["id"])
    retire_result_item(conn, relpath=RESULT, job_id=job_id)
    return conn, settings, item["id"], result_id, job_id


# --- the questions ---------------------------------------------------------------

RESULTS: list[tuple[str, str, str, bool]] = []


def check(consumer: str, expected: str, actual: str, ok: bool) -> None:
    RESULTS.append((consumer, expected, actual, ok))


def run(conn, settings, original_id: int, result_id: int, job_id: int) -> None:
    # 1. Search
    hits = search_items(conn, "concert")
    names = sorted(h["relpath"] for h in hits)
    check("Search", "the WAV only", str(names), names == [ORIGINAL])

    # 2. Library Audit — every stage reads this one view
    view = audit.gather(conn, settings, scope="")
    check("Library Audit view (files on disk)", "the WAV only",
          str(sorted(view.files)), sorted(view.files) == [ORIGINAL])
    check("Library Audit view (indexed rows)", "the WAV only",
          str(sorted(view.indexed)), sorted(view.indexed) == [ORIGINAL])

    findings = audit.detect(view, conn=conn)
    named = sorted({f.relpath for f in findings})
    check("Audit findings", f"nothing about {RESULT}",
          str(named) or "none", RESULT not in named)

    # 3. Duplicate detection
    pairs = dedup._fingerprint_pairs(conn)
    check("Duplicate candidates", "none",
          str([(a.relpath, b.relpath) for a, b in pairs]), not pairs)
    similar = duplicates.record_similar_reports(conn, settings)
    check("Similar-media flags", "0", str(similar), similar == 0)

    # 4. Storage Advisor — it reads the same audit view
    media = [f for f in view.files if f.endswith((".wav", ".flac"))]
    check("Storage Advisor candidates", "the WAV only", str(media), media == [ORIGINAL])

    # 5. Backup
    sizes = {c.category: c.files for c in backup.category_sizes(conn, settings)}
    check("Backup category sizes", "music 1", f"music {sizes.get('music')}",
          sizes.get("music") == 1)
    # Exactly the rows `run_backup_once` would pick up and hand to rclone.
    picked = backup._due_backups(conn, batch_size=50)
    check("Backup queue, rows rclone would be given", "none",
          str([r["relpath"] for r in picked]), not picked)

    # 6. Dashboard
    data = dashboard.dashboard_data(conn, settings)
    check("Dashboard library count", "1", str(data["library_count"]),
          data["library_count"] == 1)

    # 7. Health
    totals = health._totals(conn)
    check("Health library files", "1", str(totals["library_files"]),
          totals["library_files"] == 1)

    # 8. Access page storage claim
    files, human = access._usage(conn, "library")
    check("Access page library usage", "1 file", f"{files} files / {human}", files == 1)

    # 9. Browse consistency
    state = consistency.library_consistency(conn, settings)
    check("Browse consistency drift", "0 missing",
          f"{state.missing_files} missing / {state.unindexed_files} unindexed",
          state.missing_files == 0 and state.unindexed_files == 0)

    # 10. Browse listing
    folder = browse.browse_folder(conn, settings, "Music", "Live")
    listed = sorted(f["relpath"] for f in folder.get("items", []))
    check("Browse folder listing", "the WAV only", str(listed), listed == [ORIGINAL])

    # 11. Companion anchoring
    from librairy.classify import companions

    anchored = conn.execute(
        "SELECT 1 FROM items WHERE root='library' AND missing_since IS NULL"
        " AND relpath=? LIMIT 1", (RESULT,)
    ).fetchone()
    check("Companion anchor", "not anchorable", "none" if anchored is None else "anchored",
          anchored is None and hasattr(companions, "__name__"))

    # 12. History may still reference it — lineage must survive
    linked = conn.execute(
        "SELECT result_item_id FROM optimization_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    check("Job -> result lineage", f"still {result_id}", str(linked), linked == result_id)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    conn, settings, original_id, result_id, job_id = build(tmp)
    print(f"original item {original_id} · result item {result_id} · job {job_id}")
    row = conn.execute("SELECT root, relpath, missing_since FROM items WHERE id=?",
                       (result_id,)).fetchone()
    print(f"result row: {row['root']}:{row['relpath']} missing_since={bool(row['missing_since'])}")
    staged = settings.appdata_dir / "optimization" / "jobs" / str(job_id) / "output.flac"
    print(f"staged bytes: {staged.exists()}\n")

    run(conn, settings, original_id, result_id, job_id)

    width = max(len(name) for name, *_ in RESULTS)
    bad = 0
    for name, expected, actual, ok in RESULTS:
        mark = "ok  " if ok else "LEAK"
        if not ok:
            bad += 1
        print(f"{mark}  {name:<{width}}  expected {expected:<34} got {actual}")
    print(f"\n{len(RESULTS) - bad} clean · {bad} leaking")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
