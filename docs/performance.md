# Performance

P8 adds a reproducible synthetic smoke script:

```bash
python scripts/perf_smoke.py --count 50000 --commit-count 10000 --json-out perf-50k.json
```

The script creates a temporary inbox, generates mixed synthetic file names, scans, analyzes with AI effectively bypassed by deterministic classifiers, commits a subset, checks dashboard/search latency, and records timings plus database size.

## CI-Scale Smoke

The automated test suite runs a reduced version through `tests/test_performance_smoke.py`:

- 120 generated files
- 20 committed files
- dashboard response under 1 second
- search response under 1 second
- SQLite database created and populated

Latest local reduced run is covered by the full test suite on 2026-07-22.

## 50k Gate

The full 50,000-file run is still a release acceptance gate. It should be run on the target release machine after Docker is available, then this document should be updated with:

- generate/scan/analyze/commit timings
- dashboard/search latency during the run
- database size
- worker RSS bound
- any profile findings and fixes

Current expected bound for v1: worker RSS should stay under approximately 500 MB for the 50k smoke. The scan/analyze loops operate in batches and SQLite remains the coordination point; search uses FTS5 rather than unbounded `%LIKE%` scans.
