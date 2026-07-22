from __future__ import annotations

import importlib.util
from pathlib import Path


def load_run_smoke():
    path = Path(__file__).resolve().parents[1] / "scripts/perf_smoke.py"
    spec = importlib.util.spec_from_file_location("perf_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_smoke


def test_reduced_performance_smoke(tmp_path) -> None:
    run_smoke = load_run_smoke()
    result = run_smoke(tmp_path, count=120, commit_count=20)

    assert result["scanned"] == 120
    assert result["analyzed"] == 120
    assert result["committed"] == 20
    assert result["dashboard_ms"] < 1000
    assert result["search_ms"] < 1000
    assert result["db_bytes"] > 0
