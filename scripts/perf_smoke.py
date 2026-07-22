from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from librairy.classify import analyze_items
from librairy.config import Settings
from librairy.db import connect, database_path
from librairy.executor import execute_plan
from librairy.planner import approve_plan, create_plan_from_proposals
from librairy.scanner import scan_root
from librairy.search import search_data
from librairy.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic LibrAIry performance smoke")
    parser.add_argument("--count", type=int, default=50_000)
    parser.add_argument("--commit-count", type=int, default=10_000)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    base_dir = args.base_dir or Path(tempfile.mkdtemp(prefix="librairy-perf-"))
    base_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(base_dir, count=args.count, commit_count=args.commit_count)
        payload = json.dumps(result, indent=2, sort_keys=True)
        if args.json_out:
            args.json_out.write_text(payload + "\n", encoding="utf-8")
        else:
            print(payload)
    finally:
        if args.base_dir is None:
            shutil.rmtree(base_dir, ignore_errors=True)
    return 0


def run_smoke(base_dir: Path, *, count: int, commit_count: int) -> dict[str, object]:
    settings = Settings(
        APPDATA_DIR=base_dir / "appdata",
        INBOX_DIR=base_dir / "inbox",
        LIBRARY_DIR=base_dir / "library",
        QUARANTINE_DIR=base_dir / "quarantine",
        CONFIDENCE_THRESHOLD=0.4,
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for path in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        path.mkdir(parents=True, exist_ok=True)
    conn = connect(settings)
    timings: dict[str, float] = {}

    started = time.perf_counter()
    generate_files(settings.inbox_dir, count)
    timings["generate_seconds"] = elapsed(started)

    started = time.perf_counter()
    scan = scan_root(conn, "inbox", settings.inbox_dir, settings)
    timings["scan_seconds"] = elapsed(started)

    analyzed = proposed = pending = 0
    started = time.perf_counter()
    while True:
        summary = analyze_items(conn, settings, limit=500)
        analyzed += summary.analyzed
        proposed += summary.proposed
        pending += summary.pending
        if summary.analyzed == 0:
            break
    timings["analyze_seconds"] = elapsed(started)

    committed = 0
    if commit_count > 0:
        proposal_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM proposals WHERE status='proposed' LIMIT ?", (commit_count,)
            )
        ]
        if proposal_ids:
            started = time.perf_counter()
            plan_id = create_plan_from_proposals(
                conn, settings, min_confidence=0.0, proposal_ids=proposal_ids
            )
            approve_plan(conn, plan_id, settings)
            commit_summary = execute_plan(conn, plan_id, settings)
            committed = commit_summary.done + commit_summary.renamed_collision
            timings["commit_seconds"] = elapsed(started)

    dashboard_ms, search_ms = ui_latencies(conn, settings)
    return {
        "count": count,
        "commit_count": commit_count,
        "scanned": scan.discovered,
        "analyzed": analyzed,
        "proposed": proposed,
        "pending": pending,
        "committed": committed,
        "dashboard_ms": dashboard_ms,
        "search_ms": search_ms,
        "timings": timings,
        "db_bytes": database_path(settings).stat().st_size,
    }


def generate_files(inbox_dir: Path, count: int) -> None:
    kinds = ("document", "photo", "music", "project")
    suffixes = {"document": ".txt", "photo": ".jpg", "music": ".mp3", "project": ".md"}
    for index in range(count):
        kind = kinds[index % len(kinds)]
        folder = inbox_dir / kind / f"batch-{index // 1000:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{kind}-{index:06d}{suffixes[kind]}"
        path.write_text(f"synthetic {kind} file {index}\n", encoding="utf-8")


def ui_latencies(conn, settings: Settings) -> tuple[int, int]:  # noqa: ANN001
    client = TestClient(create_app(settings, conn))
    client.post("/setup", data={"password": "correct horse battery"})
    started = time.perf_counter()
    dashboard = client.get("/dashboard")
    dashboard_ms = round((time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    search_data(conn, settings, "document", filters=None)
    search_ms = round((time.perf_counter() - started) * 1000)
    assert dashboard.status_code == 200
    return dashboard_ms, search_ms


def elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 3)


if __name__ == "__main__":
    raise SystemExit(main())
