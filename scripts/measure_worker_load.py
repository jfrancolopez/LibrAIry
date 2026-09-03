"""What each processing mode actually costs, measured rather than asserted.

"Quiet leaves the machine responsive" is a claim, and a claim about resource use
that nobody has measured is exactly the thing `optimization_exec` refused to
ship a `High` policy for. So this measures what one worker cycle costs under
each mode, against a full inbox, in CPU seconds per wall-clock second — which is
what a busy worker actually takes away from everything else on the box.

**And it corrected the design's own assumption.** The batch cap was expected to
be what makes Quiet quiet. It is not: measured at 200 files, a Quiet cycle costs
0.72 CPU seconds per wall second and a Balanced one 0.76 — almost identical,
because a cycle's fixed costs (the scan, the duplicate pass, the companion pass)
do not shrink with the batch. What the cap actually buys is a *shorter* cycle,
and the pause after it is where the difference lives: sustained over a run,
Quiet is 0.34 against Balanced's 0.70. Both numbers are reported, because the
first one on its own would read as the mode doing nothing.

**What this does not measure.** Responsiveness of a NAS serving video off the
same disks, which is the situation the mode is for and which cannot be
reproduced on a build machine. The wall-clock and CPU figures here are the part
that is reproducible; the rest is a judgement made with them in hand.

Nothing here touches a real library. It builds a synthetic inbox in a temporary
directory, runs one worker cycle per mode against a fresh copy of it, and
deletes it.

    .venv/bin/python scripts/measure_worker_load.py
    .venv/bin/python scripts/measure_worker_load.py --files 500 --json-out out.json
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, "src")

from librairy.config import Settings  # noqa: E402
from librairy.db import connect  # noqa: E402
from librairy.resources import (  # noqa: E402
    BALANCED,
    FULL,
    PROCESSING_MODES,
    QUIET,
    set_processing_mode,
)
from librairy.worker import run_once  # noqa: E402

MODES = (QUIET, BALANCED, FULL)

#  A mix, so the cycle does the work a real one does: files that classify from
#  their names, files that need a reader, and files nothing can identify.
SHAPES = (
    ("{index:04d} - Some Song.mp3", b"ID3" + b"\0" * 4096),
    ("Report {index:04d}.pdf", b"%PDF-1.4\n" + b"\0" * 4096),
    ("IMG_{index:04d}.jpg", b"\xff\xd8\xff" + b"\0" * 4096),
    ("blob-{index:04d}.bin", b"?" * 4096),
)


def build_inbox(root: Path, files: int) -> Settings:
    settings = Settings(
        APPDATA_DIR=root / "appdata",
        INBOX_DIR=root / "inbox",
        LIBRARY_DIR=root / "library",
        QUARANTINE_DIR=root / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        #  No provider. This measures the worker, not somebody's LAN, and an
        #  unreachable endpoint would put a socket timeout into every figure.
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        name, body = SHAPES[index % len(SHAPES)]
        (settings.inbox_dir / name.format(index=index)).write_bytes(body)
    return settings


def cpu_seconds() -> float:
    """This process and its children, which is where an encoder would be."""
    mine = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return mine.ru_utime + mine.ru_stime + kids.ru_utime + kids.ru_stime


def measure(mode: str, files: int) -> dict[str, object]:
    root = Path(tempfile.mkdtemp(prefix=f"librairy-load-{mode}-"))
    try:
        settings = build_inbox(root, files)
        conn = connect(settings)
        set_processing_mode(conn, mode)
        cpu_before, wall_before = cpu_seconds(), time.monotonic()
        summary = run_once(conn, settings)
        cpu, wall = cpu_seconds() - cpu_before, time.monotonic() - wall_before
        conn.close()
        #  The pause the worker takes after a cycle that found work. It belongs
        #  in the figure because it is the larger half of what makes a mode
        #  quiet — see the note below the table in `docs/performance.md`.
        pause = PROCESSING_MODES[mode].busy_sleep
        return {
            "mode": mode,
            "analyzed": summary.analyzed,
            "hashed": summary.hashed,
            "wall_seconds": round(wall, 3),
            "cpu_seconds": round(cpu, 3),
            #  While the cycle runs. Nearly the same in every mode, and that is
            #  the finding: the batch cap shortens a cycle, it does not make one
            #  cheaper per second.
            "cpu_per_wall": round(cpu / wall, 2) if wall else 0.0,
            #  Over a sustained run, pause included. This is the number the
            #  mode is actually about, and the one that differs.
            "sustained_cpu_per_wall": round(cpu / (wall + pause), 2) if wall + pause else 0.0,
            "cpu_per_file": round(cpu / summary.analyzed, 4) if summary.analyzed else 0.0,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=200)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    rows = [measure(mode, args.files) for mode in MODES]
    payload = json.dumps({"files": args.files, "cycles": rows}, indent=2, sort_keys=True)
    if args.json_out:
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    print(
        f"{'mode':<10} {'files':>6} {'wall s':>8} {'cpu s':>8} "
        f"{'in cycle':>9} {'sustained':>10} {'cpu/file':>9}"
    )
    for row in rows:
        print(
            f"{row['mode']:<10} {row['analyzed']:>6} {row['wall_seconds']:>8} "
            f"{row['cpu_seconds']:>8} {row['cpu_per_wall']:>9} "
            f"{row['sustained_cpu_per_wall']:>10} {row['cpu_per_file']:>9}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
