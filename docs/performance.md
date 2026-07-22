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

## Latest Local 50k Smoke

Run on 2026-07-22 from the source checkout, outside Docker because the local Docker daemon was unavailable:

```bash
python scripts/perf_smoke.py --count 50000 --commit-count 10000
```

Results:

- generated files: 50,000
- scanned: 50,000
- analyzed: 50,000
- proposed: 37,500
- pending: 12,500
- committed: 10,000
- generate: 4.971 seconds
- scan: 18.567 seconds
- analyze: 81.165 seconds
- commit: 11.815 seconds
- dashboard response: 5 ms
- search response: 84 ms
- SQLite database size: 59,129,856 bytes
- process peak RSS: 90 MB

## 50k Gate

The full 50,000-file source-checkout run has passed locally. The remaining release acceptance gate is to rerun it on the target release machine/Docker image, then update this document with any environment-specific differences:

- generate/scan/analyze/commit timings
- dashboard/search latency during the run
- database size
- worker RSS bound
- any profile findings and fixes

Current expected bound for v1: worker RSS should stay under approximately 500 MB for the 50k smoke. The scan/analyze loops operate in batches and SQLite remains the coordination point; search uses FTS5 rather than unbounded `%LIKE%` scans.
