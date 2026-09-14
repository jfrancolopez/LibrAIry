"""What LibrAIry costs the machine it shares — measured on that machine.

Every other measurement in this repository runs on a build machine, and this one
cannot. The claim M2-03 shipped without proving is *"Quiet leaves the NAS
responsive under a full inbox"*, and a laptop cannot prove it: the thing at risk
is a film stuttering over SMB while the array is busy, and there is no film and
no array here. The roadmap has carried it as **production validation required**
ever since, which is the honest state and not a very useful one on its own.

So this is the procedure, executable. It runs **on the NAS**, samples the host
while LibrAIry works, and prints what a person needs to decide whether a mode is
tuned correctly.

## Two ways to run it, and the first one is safe

    --observe        watch the host while the installation does whatever it is
                     already doing. Reads `/proc`, times a few reads, and
                     touches nothing. This is the one to run on production.

    --drive WORKDIR  build a synthetic inbox under WORKDIR and run a worker
                     cycle in each processing mode against it. Moves files —
                     its own, never the library's — and refuses to run against
                     any directory the installation is configured to use.

## What it measures, and what it honestly cannot

Measured here:

    CPU              per-mode, from /proc/stat: what share of the box went
    load             1-minute load average, which is what an over-subscribed
                     NAS shows first
    pressure         /proc/pressure/{cpu,io,memory} — the single best signal
                     Linux gives for "is this machine struggling", and the one
                     closest to what somebody notices
    memory           MemAvailable, not RSS: the question is what is left for
                     everything else
    temperature      /sys/class/hwmon, where the board exposes it
    storage latency  a timed sequential read off the array, which is the
                     nearest honest proxy for a stream that must not stutter
    UI latency       a timed request to LibrAIry's own dashboard

**Not measured, and it must not be claimed.** Whether a film actually stutters,
whether an SMB copy from another room slows down, whether somebody browsing
photographs notices. Those need a person in the house doing the thing while this
runs, and the last section prints the checklist for it. A proxy is a proxy: a
sequential read finishing in 40 ms says the array had headroom, not that nobody
noticed anything.

    python3 scripts/nas_validation.py --observe --seconds 300
    python3 scripts/nas_validation.py --drive /mnt/user/appdata/librairy-validation

The purpose is to **tune constants** — the batch caps and pauses in
`librairy/resources.py` — against a real machine. It is not a reason to redesign
`ResourcePolicy`; that needs evidence this does not yet have.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, "src")

#  A read big enough that the array, rather than its cache, answers. Streaming a
#  film is a sequential read; this is the same shape at a size that finishes.
PROBE_BYTES = 64 * 1024 * 1024
PROBE_CHUNK = 1024 * 1024


@dataclass
class Sample:
    """The host at one instant, as far as it will say."""

    at: float
    cpu_busy: float = 0.0
    load1: float = 0.0
    mem_available_mb: float = 0.0
    mem_total_mb: float = 0.0
    pressure_cpu: float = 0.0
    pressure_io: float = 0.0
    pressure_memory: float = 0.0
    temperature_c: float = 0.0


@dataclass
class Window:
    """What happened over a stretch of time, with a name on it."""

    label: str
    seconds: float
    cpu_busy_pct: float = 0.0
    load1: float = 0.0
    mem_available_mb: float = 0.0
    pressure_cpu_pct: float = 0.0
    pressure_io_pct: float = 0.0
    pressure_memory_pct: float = 0.0
    temperature_c: float = 0.0
    storage_read_mbs: float = 0.0
    ui_ms: float = 0.0
    notes: list[str] = field(default_factory=list)


# --- reading the host -----------------------------------------------------------------


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def cpu_times() -> tuple[float, float]:
    """(busy, total) jiffies from /proc/stat, or (0, 0) where there is none."""
    line = _read("/proc/stat").split("\n", 1)[0]
    if not line.startswith("cpu "):
        return 0.0, 0.0
    values = [float(value) for value in line.split()[1:] if value.replace(".", "").isdigit()]
    if len(values) < 4:
        return 0.0, 0.0
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    total = sum(values)
    return total - idle, total


def pressure(resource_name: str) -> float:
    """The `some avg10` figure: the share of the last ten seconds in which at
    least one task was stalled waiting for this resource.

    The closest thing Linux gives to "somebody would have noticed". A box at 90%
    CPU with no IO pressure is working hard and serving files fine; one at 30%
    CPU with IO pressure is the one whose film stutters.
    """
    for line in _read(f"/proc/pressure/{resource_name}").splitlines():
        if line.startswith("some "):
            for part in line.split():
                key, _, value = part.partition("=")
                if key == "avg10":
                    try:
                        return float(value)
                    except ValueError:  # pragma: no cover - defensive
                        return 0.0
    return 0.0


def memory_mb() -> tuple[float, float]:
    """(available, total). Available and not free: the kernel's own estimate of
    what a new process could actually get, which is the question."""
    available = total = 0.0
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split(" ", 1)[0]
        if not value.isdigit():
            continue
        if key == "MemAvailable":
            available = float(value) / 1024
        elif key == "MemTotal":
            total = float(value) / 1024
    return available, total


def load_average() -> float:
    parts = _read("/proc/loadavg").split()
    try:
        return float(parts[0]) if parts else 0.0
    except ValueError:  # pragma: no cover - defensive
        return 0.0


def temperature() -> float:
    """The warmest sensor the board exposes, in Celsius, or 0.

    A NAS that throttles is a NAS whose owner blames the software, so it is
    worth a number — and it is genuinely optional, because plenty of boards
    expose nothing.
    """
    warmest = 0.0
    for path in sorted(Path("/sys/class/hwmon").glob("hwmon*/temp*_input")):
        raw = _read(str(path)).strip()
        if raw.lstrip("-").isdigit():
            warmest = max(warmest, float(raw) / 1000)
    return warmest


def sample() -> Sample:
    busy, total = cpu_times()
    available, whole = memory_mb()
    return Sample(
        at=time.monotonic(),
        cpu_busy=busy,
        load1=load_average(),
        mem_available_mb=available,
        mem_total_mb=whole,
        pressure_cpu=pressure("cpu"),
        pressure_io=pressure("io"),
        pressure_memory=pressure("memory"),
        temperature_c=temperature(),
    )


# --- the probes -----------------------------------------------------------------------


def storage_read_mbs(path: Path) -> float:
    """Megabytes a second, reading sequentially off the array.

    The nearest honest proxy for a stream that must not stutter, and it is a
    proxy: a good number says the array had headroom, not that nobody noticed
    anything. Reads an existing file rather than writing one — this runs on
    production, and a validation script that fills somebody's array is not a
    validation script.
    """
    target = _largest_file(path)
    if target is None:
        return 0.0
    started = time.monotonic()
    read = 0
    try:
        with open(target, "rb") as handle:
            while read < PROBE_BYTES:
                chunk = handle.read(PROBE_CHUNK)
                if not chunk:
                    break
                read += len(chunk)
    except OSError:
        return 0.0
    elapsed = time.monotonic() - started
    return round((read / 1024**2) / elapsed, 1) if elapsed > 0 else 0.0


def _largest_file(root: Path) -> Path | None:
    """Something big enough to be read from the disk rather than from cache."""
    best: tuple[int, Path] | None = None
    try:
        for index, path in enumerate(root.rglob("*")):
            if index > 20_000:
                break
            try:
                if path.is_file():
                    size = path.stat().st_size
                    if best is None or size > best[0]:
                        best = (size, path)
            except OSError:
                continue
    except OSError:
        return None
    return best[1] if best and best[0] >= PROBE_CHUNK else None


def ui_ms(port: int) -> float:
    """How long LibrAIry's own dashboard takes to answer, from this machine.

    Not a browser and not a page: one request, which is the part that is the
    program's fault. A dashboard that answers in 40 ms over a busy array is the
    result this is looking for.
    """
    started = time.monotonic()
    try:
        with urllib.request.urlopen(  # noqa: S310 - a localhost URL this script builds
            f"http://127.0.0.1:{port}/dashboard", timeout=30
        ) as response:
            response.read(1024)
    except (TimeoutError, urllib.error.URLError, OSError):
        return 0.0
    return round((time.monotonic() - started) * 1000, 1)


def watch(label: str, seconds: float, *, probe: Path | None, port: int) -> Window:
    """Sample the host for a stretch, and probe it once in the middle."""
    first = sample()
    peak_load = first.load1
    pressures = [(first.pressure_cpu, first.pressure_io, first.pressure_memory)]
    lowest_memory = first.mem_available_mb or 0.0
    warmest = first.temperature_c
    read_speed = 0.0
    latency = 0.0

    deadline = first.at + seconds
    probed = False
    while time.monotonic() < deadline:
        time.sleep(min(2.0, max(0.2, seconds / 10)))
        now = sample()
        peak_load = max(peak_load, now.load1)
        pressures.append((now.pressure_cpu, now.pressure_io, now.pressure_memory))
        if now.mem_available_mb:
            lowest_memory = min(lowest_memory or now.mem_available_mb, now.mem_available_mb)
        warmest = max(warmest, now.temperature_c)
        if not probed and time.monotonic() > first.at + seconds / 2:
            probed = True
            if probe is not None:
                read_speed = storage_read_mbs(probe)
            latency = ui_ms(port)

    last = sample()
    elapsed = last.at - first.at
    busy = last.cpu_busy - first.cpu_busy
    total = max(1.0, busy + _idle_delta(first, last))
    return Window(
        label=label,
        seconds=round(elapsed, 1),
        cpu_busy_pct=round(100 * busy / total, 1) if total else 0.0,
        load1=round(peak_load, 2),
        mem_available_mb=round(lowest_memory, 0),
        pressure_cpu_pct=round(max(p[0] for p in pressures), 1),
        pressure_io_pct=round(max(p[1] for p in pressures), 1),
        pressure_memory_pct=round(max(p[2] for p in pressures), 1),
        temperature_c=round(warmest, 1),
        storage_read_mbs=read_speed,
        ui_ms=latency,
    )


def _idle_delta(first: Sample, last: Sample) -> float:
    """Idle jiffies between two samples, from the wall clock.

    `Sample` keeps busy rather than busy *and* total, because busy is what every
    other line here is about. The denominator a percentage needs is simply how
    much CPU time existed over the window — seconds times cores times the
    kernel's tick — and deriving it that way keeps the record one number wide.
    """
    existed = (last.at - first.at) * _jiffies_per_second() * _cpu_count()
    return max(0.0, existed - (last.cpu_busy - first.cpu_busy))


def _jiffies_per_second() -> float:
    try:
        return float(subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
            ["getconf", "CLK_TCK"], capture_output=True, text=True, check=False, timeout=5
        ).stdout.strip() or 100)
    except (OSError, ValueError, subprocess.SubprocessError):  # pragma: no cover
        return 100.0


def _cpu_count() -> int:
    count = 0
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu") and not line.startswith("cpu "):
            count += 1
    return count or 1


# --- the two ways to run it -----------------------------------------------------------


def observe(seconds: float, *, probe: Path | None, port: int) -> list[Window]:
    """Watch the installation do whatever it is already doing. Touches nothing.

    The one to run on production, and the one that answers the question as
    asked: what is this machine like *while LibrAIry is running on it*. Start it
    with a full inbox and the worker busy, and again with the inbox empty, and
    the difference between the two is the cost.
    """
    return [watch("as found", seconds, probe=probe, port=port)]


def drive(workdir: Path, *, files: int, probe: Path | None, port: int) -> list[Window]:
    """Run one worker cycle in each processing mode, against a synthetic inbox.

    Its own inbox, under a directory somebody named on the command line, and it
    refuses any directory the installation is configured to use — see
    `_refuse_the_real_thing`. A validation script that files somebody's library
    while measuring how fast it files libraries is not one.
    """
    from librairy.db import connect
    from librairy.resources import BALANCED, FULL, QUIET, set_processing_mode
    from librairy.worker import run_once

    _refuse_the_real_thing(workdir)
    windows: list[Window] = []
    windows.append(watch("idle baseline", 20, probe=probe, port=port))
    for mode in (QUIET, BALANCED, FULL):
        root = workdir / mode
        settings = _synthetic(root, files)
        conn = connect(settings)
        set_processing_mode(conn, mode)
        started = time.monotonic()
        stop = {"done": False}

        def cycle(conn=conn, settings=settings, stop=stop) -> None:
            try:
                run_once(conn, settings)
            finally:
                stop["done"] = True

        import threading

        thread = threading.Thread(target=cycle, daemon=True)
        thread.start()
        window = watch(f"worker: {mode}", 30, probe=probe, port=port)
        thread.join(timeout=300)
        window.notes.append(f"cycle finished in {time.monotonic() - started:.1f}s")
        windows.append(window)
        conn.close()
        shutil.rmtree(root, ignore_errors=True)
    return windows


def _refuse_the_real_thing(workdir: Path) -> None:
    """Never the installation's own folders, whatever the argument says."""
    from librairy.config import Settings

    settings = Settings()
    resolved = workdir.resolve()
    for name, path in (
        ("inbox", settings.inbox_dir),
        ("library", settings.library_dir),
        ("quarantine", settings.quarantine_dir),
        ("appdata", settings.appdata_dir),
    ):
        try:
            owned = path.resolve()
        except OSError:  # pragma: no cover - a path that is not there
            continue
        if resolved == owned or owned in resolved.parents or resolved in owned.parents:
            raise SystemExit(
                f"refusing to use {resolved}: it is inside, or contains, the "
                f"installation's {name} at {owned}. Give this its own directory."
            )


def _synthetic(root: Path, files: int) -> object:
    """A throwaway installation with a full inbox, built where told."""
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=root / "appdata",
        INBOX_DIR=root / "inbox",
        LIBRARY_DIR=root / "library",
        QUARANTINE_DIR=root / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        #  No provider. A socket timeout to somebody's LAN in every figure would
        #  be measuring the LAN.
        OLLAMA_HOST="",
        _env_file=None,
    )
    shapes = (
        ("{index:05d} - Some Song.mp3", b"ID3" + b"\0" * 8192),
        ("Report {index:05d}.pdf", b"%PDF-1.4\n" + b"\0" * 8192),
        ("IMG_{index:05d}.jpg", b"\xff\xd8\xff" + b"\0" * 8192),
        ("blob-{index:05d}.bin", b"?" * 8192),
    )
    for directory in (
        settings.appdata_dir, settings.inbox_dir,
        settings.library_dir, settings.quarantine_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        name, body = shapes[index % len(shapes)]
        (settings.inbox_dir / name.format(index=index)).write_bytes(body)
    return settings


# --- what it says -----------------------------------------------------------------


#  The half no script can do. Printed every run, because a report that lists
#  only what was measured reads as though the rest was measured too.
BY_HAND = """
What this did not measure, and somebody has to:

  * Play a film from the array over SMB or NFS, on another device, for the
    whole of a `worker: full` window. Did it stutter, once?
  * Copy a large folder to the array from another machine during the same
    window. Did the transfer rate drop noticeably?
  * Browse photographs in LibrAIry's own UI while the worker is busy. Did a
    page take longer than it does on an idle box?
  * Leave it running with a genuinely full inbox for an evening. Was the
    machine unpleasant to use at any point?

A sequential read finishing quickly says the array had headroom. It does not
say nobody noticed anything, and this script must not be quoted as though it
did.
"""


def render(windows: list[Window]) -> str:
    header = (
        f"{'window':<20}{'CPU%':>7}{'load':>7}{'PSI cpu':>9}{'PSI io':>8}"
        f"{'memMB':>9}{'degC':>7}{'read MB/s':>11}{'UI ms':>8}"
    )
    lines = [header, "-" * len(header)]
    for window in windows:
        lines.append(
            f"{window.label:<20}{window.cpu_busy_pct:>7.1f}{window.load1:>7.2f}"
            f"{window.pressure_cpu_pct:>9.1f}{window.pressure_io_pct:>8.1f}"
            f"{window.mem_available_mb:>9.0f}{window.temperature_c:>7.1f}"
            f"{window.storage_read_mbs:>11.1f}{window.ui_ms:>8.1f}"
        )
        for note in window.notes:
            lines.append(f"{'':<20}{note}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--observe", action="store_true",
        help="watch the running installation and change nothing (safe on production)",
    )
    parser.add_argument(
        "--drive", type=Path, metavar="WORKDIR",
        help="run a worker cycle per mode against a synthetic inbox under WORKDIR",
    )
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--files", type=int, default=400)
    parser.add_argument(
        "--probe", type=Path,
        help="a directory on the array to time a sequential read from",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    if not args.observe and args.drive is None:
        parser.error("choose --observe (safe) or --drive WORKDIR")
    if not Path("/proc/stat").exists():
        print(
            "This reads /proc, so it only says anything on Linux. Run it on the "
            "NAS itself — that is the entire point of it.",
            file=sys.stderr,
        )
        return 2

    windows = (
        observe(args.seconds, probe=args.probe, port=args.port)
        if args.observe
        else drive(args.drive, files=args.files, probe=args.probe, port=args.port)
    )
    print(render(windows))
    print(BY_HAND)
    if args.json_out:
        args.json_out.write_text(
            json.dumps([vars(window) for window in windows], indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
