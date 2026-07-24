from __future__ import annotations

from librairy.web.health import HealthRow, recommendations


def _providers(reachable: bool):
    return [{"last_ok_at": "now" if reachable else None, "last_error": None}]


def _ok_tools():
    return [HealthRow("ffprobe", "OK", "5.1")]


def test_no_recommendations_when_all_healthy() -> None:
    recs = recommendations(
        tools=_ok_tools(),
        providers=_providers(True),
        disks=[HealthRow("library", "OK", "500GB free of 1000GB (50%)")],
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
        disks=[HealthRow("library", "WARN", "9GB free of 1000GB (3%)")],
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
