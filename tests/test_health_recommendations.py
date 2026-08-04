from __future__ import annotations

from librairy.web.dashboard import DiskStat
from librairy.web.health import HealthRow, recommendations


def _providers(reachable: bool):
    return [{"last_ok_at": "now" if reachable else None, "last_error": None}]


def _ok_tools():
    return [HealthRow("ffprobe", "OK", "5.1")]


def test_no_recommendations_when_all_healthy() -> None:
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[DiskStat("library", 500.0, 1000.0, 50, device=1)],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )
    assert recs == []


def test_missing_tool_is_flagged_with_action() -> None:
    recs = recommendations(
        tools=[HealthRow("czkawka", "WARN", "missing", "install czkawka")],
        providers=_providers(True),
        disks=[],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )
    assert any("czkawka is unavailable" in r.text and "install czkawka" in r.action for r in recs)


def test_unreachable_ai_falls_back_to_heuristics() -> None:
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(False),
        disks=[],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )
    assert any("heuristics only" in r.text and "OLLAMA_HOST" in r.action for r in recs)


def test_low_disk_and_backup_and_worker_are_flagged() -> None:
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[DiskStat("library", 9.0, 1000.0, 3, device=1)],
        worker=HealthRow("Worker", "WARN", "no heartbeat"),
        backup=HealthRow("Backup", "WARN", "remote unreachable"),
    )
    texts = " ".join(r.text for r in recs)
    assert "low on space" in texts
    assert "3%" in texts
    assert "Worker" in texts
    assert "Backup" in texts
    # 3% free is critical
    assert any(r.severity == "fail" for r in recs if "space" in r.text)


def test_roots_sharing_one_volume_produce_one_warning() -> None:
    """All four roots on one laptop disk is one problem, not four."""
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[
            DiskStat("inbox", 27.9, 460.0, 6, device=16777232),
            DiskStat("library", 27.9, 460.0, 6, device=16777232),
            DiskStat("quarantine", 27.9, 460.0, 6, device=16777232),
            DiskStat("appdata", 27.9, 460.0, 6, device=16777232),
        ],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )

    space = [r for r in recs if "low on space" in r.text]
    assert len(space) == 1
    assert "appdata, inbox, library, quarantine share a volume" in space[0].text
    assert "6% free" in space[0].text


def test_separate_volumes_still_warn_separately() -> None:
    """A NAS with the library on its own array must not hide either disk."""
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[
            DiskStat("inbox", 2.0, 100.0, 2, device=1),
            DiskStat("library", 40.0, 1000.0, 4, device=2),
        ],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )

    space = [r for r in recs if "low on space" in r.text]
    assert len(space) == 2
    assert {r.severity for r in space} == {"fail"}
    assert "inbox is low on space (2% free)" in " ".join(r.text for r in space)


def test_grouped_warning_reports_the_worst_root() -> None:
    """Shared device, differing readings — report the tightest one, not the first."""
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[
            DiskStat("inbox", 9.0, 100.0, 9, device=7),
            DiskStat("library", 3.0, 100.0, 3, device=7),
        ],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )

    space = [r for r in recs if "low on space" in r.text]
    assert len(space) == 1
    assert "3% free" in space[0].text
    assert space[0].severity == "fail"


def test_healthy_volumes_produce_no_disk_warning() -> None:
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[DiskStat("inbox", 400.0, 500.0, 80, device=1)],
        worker=HealthRow("Worker", "OK", "idle"),
        backup=HealthRow("Backup", "OK", "disabled"),
    )

    assert not [r for r in recs if "low on space" in r.text]
