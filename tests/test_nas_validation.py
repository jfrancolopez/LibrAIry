"""The production-validation script, held to what it can and cannot claim.

`scripts/nas_validation.py` runs on the NAS and nowhere else — it reads `/proc`,
and the whole reason it exists is that a build machine cannot answer the question
M2-03 shipped without answering. So what can be tested here is not the
measurement: it is the *parsing*, which is the part that fails silently and
wrongly, and the refusals, which are the part that must not fail at all on
somebody's production array.

The fixture text below is the real format of each file. A parser tested against
text somebody invented is a parser tested against the invention.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load():  # noqa: ANN201
    path = Path(__file__).resolve().parents[1] / "scripts/nas_validation.py"
    spec = importlib.util.spec_from_file_location("nas_validation", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    #  Registered before it runs. The script uses `from __future__ import
    #  annotations`, so its dataclass annotations are strings, and `dataclasses`
    #  resolves them through `sys.modules[cls.__module__]` — which is not there
    #  yet if the module is only executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#  Real shapes, from a Linux box. Field order in /proc/stat is user, nice,
#  system, idle, iowait, irq, softirq, steal, guest, guest_nice.
PROC_STAT = """cpu  245678 1204 67890 9876543 12345 0 5678 0 0 0
cpu0 61419 301 16972 2469135 3086 0 1419 0 0 0
cpu1 61419 301 16972 2469136 3086 0 1419 0 0 0
intr 123456789
ctxt 987654321
"""

PRESSURE_IO = """some avg10=4.21 avg60=1.02 avg300=0.35 total=123456789
full avg10=2.10 avg60=0.51 avg300=0.18 total=98765432
"""

MEMINFO = """MemTotal:       16316456 kB
MemFree:         1234568 kB
MemAvailable:   10234567 kB
Buffers:          123456 kB
"""


def test_cpu_busy_excludes_idle_and_iowait(monkeypatch) -> None:
    """A NAS waiting on its disks is not a NAS doing work, and counting iowait
    as busy would report an array bottleneck as CPU load."""
    nas = load()
    monkeypatch.setattr(nas, "_read", lambda path: PROC_STAT if "stat" in path else "")

    busy, total = nas.cpu_times()

    assert total == 245678 + 1204 + 67890 + 9876543 + 12345 + 5678
    assert busy == total - (9876543 + 12345)


def test_pressure_reads_the_ten_second_some_figure(monkeypatch) -> None:
    """`some avg10` is the share of the last ten seconds in which at least one
    task was stalled — the closest thing Linux gives to "somebody noticed"."""
    nas = load()
    monkeypatch.setattr(nas, "_read", lambda path: PRESSURE_IO if "io" in path else "")

    assert nas.pressure("io") == 4.21
    assert nas.pressure("cpu") == 0.0


def test_memory_reports_what_is_left_rather_than_what_is_free(monkeypatch) -> None:
    """MemAvailable and not MemFree. The question is what everything else on the
    box could still get, and on a NAS almost all of MemFree is cache."""
    nas = load()
    monkeypatch.setattr(nas, "_read", lambda path: MEMINFO if "meminfo" in path else "")

    available, total = nas.memory_mb()

    assert round(available) == round(10234567 / 1024)
    assert round(total) == round(16316456 / 1024)


def test_a_machine_with_no_proc_says_so_rather_than_reporting_zeroes(monkeypatch) -> None:
    """Every reader returns a neutral answer when the file is not there, so the
    script degrades to "this told me nothing" instead of "the box is idle"."""
    nas = load()
    monkeypatch.setattr(nas, "_read", lambda path: "")  # noqa: ARG005

    assert nas.cpu_times() == (0.0, 0.0)
    assert nas.pressure("io") == 0.0
    assert nas.memory_mb() == (0.0, 0.0)
    assert nas.load_average() == 0.0


# --- the refusals ---------------------------------------------------------------------


def test_driving_refuses_the_installations_own_directories(tmp_path, monkeypatch) -> None:
    """The one that matters on production.

    `--drive` builds a synthetic inbox and files it. Pointed at the real inbox —
    by a typo, or by somebody reasonably assuming that is what it wants — it
    would file somebody's library while measuring how fast it files libraries.
    """
    nas = load()
    monkeypatch.setenv("APPDATA_DIR", str(tmp_path / "appdata"))
    monkeypatch.setenv("INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("QUARANTINE_DIR", str(tmp_path / "quarantine"))
    for name in ("appdata", "inbox", "library", "quarantine"):
        (tmp_path / name).mkdir()

    for forbidden in (
        tmp_path / "inbox",
        tmp_path / "library" / "deeper",
        tmp_path / "quarantine",
    ):
        with pytest.raises(SystemExit) as refused:
            nas._refuse_the_real_thing(forbidden)
        assert "refusing" in str(refused.value)

    #  Its own directory is fine.
    nas._refuse_the_real_thing(tmp_path / "validation")


def test_the_report_always_prints_what_it_did_not_measure() -> None:
    """A report that lists only what was measured reads as though the rest was
    measured too. The proxy here is a sequential read, and a good number from it
    says the array had headroom — never that nobody noticed anything."""
    nas = load()

    assert "did not measure" in nas.BY_HAND
    assert "stutter" in nas.BY_HAND
    assert "does not" in nas.BY_HAND


def test_it_refuses_to_pretend_on_a_machine_without_proc(monkeypatch, capsys) -> None:
    """Run on a laptop it says so and stops, rather than printing a table of
    zeroes that somebody could quote."""
    nas = load()
    monkeypatch.setattr(nas.Path, "exists", lambda self: False)  # noqa: ARG005

    assert nas.main(["--observe"]) == 2
    assert "on the NAS itself" in capsys.readouterr().err
