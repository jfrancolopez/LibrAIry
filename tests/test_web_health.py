from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.ai.base import HealthResult
from librairy.ai.registry import provider_chain
from librairy.config import Settings
from librairy.db import connect
from librairy.web import health as health_module
from librairy.web.app import create_app


class FakeProvider:
    def __init__(self, config, settings) -> None:  # noqa: ANN001
        self.config = config

    def health(self, timeout: int) -> HealthResult:  # noqa: ARG002
        return HealthResult(True, latency_ms=7, models=("qwen",))


def client_for(tmp_path: Path) -> tuple[TestClient, object, Settings]:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        OLLAMA_HOST="http://ollama.test:11434",
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True)
    conn = connect(settings)
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    return client, conn, settings


def test_tool_probes_respect_path_and_render_warn(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    client, _, _ = client_for(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    response = client.get("/health")

    assert response.status_code == 200
    assert "[WARN] ffprobe - missing" in response.text
    assert "install ffprobe" in response.text


def test_tool_probes_are_cached(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    calls = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/bin/sh\nprintf 'ffprobe version test\n'\n", encoding="utf-8")
    ffprobe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0, stdout="version ok", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)

    health_module._tool_status("ffprobe", ["ffprobe", "-version"])
    health_module._tool_status("ffprobe", ["ffprobe", "-version"])

    assert len(calls) == 1


def test_provider_button_runs_health_and_updates_partial(tmp_path: Path, monkeypatch) -> None:
    client, conn, settings = client_for(tmp_path)
    provider_chain(conn, settings)
    monkeypatch.setattr(health_module, "provider_for_config", FakeProvider)

    response = client.post(
        "/health/providers/ollama-primary",
        headers={"x-csrf-token": client.cookies["csrf_token"]},
    )
    row = conn.execute("SELECT * FROM provider_status WHERE name='ollama-primary'").fetchone()

    assert response.status_code == 200
    assert "[OK] ollama-primary" in response.text
    assert row["last_ok_at"] is not None
    assert row["latency_ms"] == 7


def test_health_summary_all_green_when_dependencies_ok(tmp_path: Path, monkeypatch) -> None:
    health_module._TOOL_CACHE.clear()
    client, conn, settings = client_for(tmp_path)
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES ('current_phase', '\"idle\"')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO worker_state(key, value) VALUES (?, ?)",
        ("last_cycle_at", '"2026-07-22T00:00:00+00:00"'),
    )
    monkeypatch.setattr(
        health_module,
        "worker_status",
        lambda conn: health_module.HealthRow("Worker", "OK", "phase=idle"),
    )
    monkeypatch.setattr(
        health_module,
        "tool_statuses",
        lambda settings: [
            health_module.HealthRow(name, "OK", "version")
            for name in health_module.TOOL_COMMANDS
        ],
    )
    monkeypatch.setattr(
        health_module,
        "disk_statuses",
        lambda settings: [health_module.HealthRow("inbox", "OK", "space ok")],
    )
    monkeypatch.setattr(
        health_module,
        "db_status",
        lambda settings: health_module.HealthRow("SQLite", "OK", "quick_check=ok"),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert "[OK] SYSTEM HEALTH" in response.text
