"""What "Resource use: Low" actually costs, measured rather than asserted.

`-threads 2` is not an answer. libx265 builds its *own* worker pool, sized from
the CPUs it can see, independently of FFmpeg's `-threads`; a job can therefore
be "two threads" and still spread work across every core on the NAS. The number
that settles it is neither a flag nor a thread count — it is **CPU seconds
consumed per wall-clock second**, which is what a parallel encoder actually
takes away from everything else running on the box.

Thread count is reported too, and deliberately, to show why it is the wrong
measure: an unbounded and a bounded encode here differ by one thread and by
more than a factor of three in CPU consumed.

    docker exec librairy python3 /tmp/measure_encoder_load.py
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

WORK = Path("/tmp/librairy-encoder-measure")
SOURCE = WORK / "source.mp4"

# Long enough that thread pools reach steady state and short enough to run in a
# gate. 720p because that is a realistic library file, not a synthetic 64x64.
SOURCE_ARGS = [
    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=20",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
]

CONFIGS: dict[str, list[str]] = {
    "unbounded": ["-preset", "medium", "-crf", "28"],
    "ffmpeg-threads-2-only": ["-preset", "medium", "-crf", "28", "-threads", "2"],
    "x265-pools-2-only": [
        "-preset", "medium", "-crf", "28",
        "-x265-params", "pools=2:frame-threads=2",
    ],
    "low": [
        "-preset", "medium", "-crf", "28", "-threads", "2",
        "-x265-params", "pools=2:frame-threads=2",
    ],
    "low-single": [
        "-preset", "medium", "-crf", "28", "-threads", "1",
        "-x265-params", "pools=1:frame-threads=1",
    ],
}


def cpu_quota() -> str:
    """What the container is actually allowed, if the runtime says so."""
    for path, label in (
        (Path("/sys/fs/cgroup/cpu.max"), "cgroup v2 cpu.max"),
        (Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), "cgroup v1 cfs_quota_us"),
    ):
        if path.exists():
            return f"{label}={path.read_text().strip()}"
    return "no cgroup cpu limit visible"


def peak_threads(pid: int) -> int:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return 0
    for line in status.splitlines():
        if line.startswith("Threads:"):
            return int(line.split()[1])
    return 0


def run(name: str, args: list[str]) -> dict[str, object]:
    out = WORK / f"{name}.mp4"
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(SOURCE),
         "-c:v", "libx265", *args, "-an", str(out)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    threads = 0
    while process.poll() is None:
        threads = max(threads, peak_threads(process.pid))
        time.sleep(0.25)
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return {
        "wall_seconds": round(wall, 2),
        "cpu_seconds": round(cpu, 2),
        # The number that matters: cores' worth of machine consumed, on average,
        # for the whole run.
        "parallelism": round(cpu / wall, 2) if wall else 0.0,
        "peak_process_threads": threads,
        "output_bytes": out.stat().st_size if out.exists() else 0,
    }


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        subprocess.run(  # noqa: S603
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             *SOURCE_ARGS, str(SOURCE)],
            check=True,
        )
    report: dict[str, object] = {
        "cpus_visible": os.cpu_count(),
        "cpu_quota": cpu_quota(),
        "loadavg_before": os.getloadavg(),
        "runs": {name: run(name, args) for name, args in CONFIGS.items()},
        "loadavg_after": os.getloadavg(),
    }
    json.dump(report, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
