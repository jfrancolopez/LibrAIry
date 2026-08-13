"""Storage discovery inside the audit: cheap, sliceable, and unable to encode.

Two properties matter more than the rules themselves.

**Audit never transcodes.** Pressing Audit must not make a NAS start working
for an hour. `test_the_audit_cannot_reach_an_encoder` asserts that against the
real call graph rather than against intent, because the risk is not that
somebody adds a transcode on purpose — it is that a helper quietly grows one.

**The stage resumes.** The catalog and artwork stages both shipped with a bug
where a slice rebuilt its worklist and re-examined item one forever. This is
the third stage with a cursor, so it gets the same forced-slicing test the
other two now have.
"""

from __future__ import annotations

from pathlib import Path

from librairy import optimization
from librairy.audit_job import advance, enqueue, progress
from librairy.config import Settings
from librairy.db import connect
from librairy.scanner import scan_root


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        AUTH_REQUIRED=False,
        OLLAMA_HOST="",
        _env_file=None,
    )
    for directory in (settings.inbox_dir, settings.library_dir, settings.quarantine_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return settings


def build(tmp_path: Path, files: dict[str, bytes]):
    settings = settings_for(tmp_path)
    for relpath, body in files.items():
        path = settings.library_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


MEDIA = {f"Music/track{index:02d}.wav": b"RIFF" + bytes([index]) for index in range(8)}


def fake_probe(monkeypatch, size: int = 800 * 1024 * 1024):
    """A probe that reports a big WAV, so every file is an opportunity."""
    seen: list[str] = []

    def probe(_settings, path):
        seen.append(str(path))
        return optimization.MediaFacts(
            container=path.suffix.lower(), size=size, duration=2400,
            audio_codec="pcm_s16le", audio_channels=2, sample_rate=48000,
        )

    monkeypatch.setattr(optimization, "probe_media", probe)
    return seen


def drain(conn, settings, *, seconds=6.0, limit=400):
    for count in range(limit):
        if advance(conn, settings, seconds=seconds).finished:
            return count + 1
    raise AssertionError("the audit never finished")


# --- the prohibition -------------------------------------------------------------


def test_the_audit_cannot_reach_an_encoder() -> None:
    """Asserted against the call graph, not against intent.

    Walks every module the storage stage can reach and fails if any of them
    would run `ffmpeg`. `ffprobe` is fine — reading a header is the whole
    point — but an encode from a stage that a person triggers by pressing a
    button is the thing this feature exists to avoid.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "librairy"
    reachable = [
        root / "optimization.py",
        root / "audit_stages.py",
        root / "audit_job.py",
        root / "audit.py",
        root / "protected.py",
    ]
    offenders: list[str] = []
    for path in reachable:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `ffmpeg` as a bare command word. `ffprobe` is a different
            # binary and does not match.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (node.value == "ffmpeg" or node.value.endswith("/ffmpeg"))
            ):
                offenders.append(f"{path.name}: {node.value!r}")
    assert offenders == [], offenders


def test_the_storage_stage_only_probes(monkeypatch, tmp_path: Path) -> None:
    """The other half: nothing spawns a subprocess except ffprobe."""
    conn, settings = build(tmp_path, MEDIA)
    fake_probe(monkeypatch)
    started: list[list[str]] = []
    import subprocess

    real_run = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: started.append(list(cmd)) or real_run(cmd, **kw)
    )
    enqueue(conn)

    drain(conn, settings)

    assert all("ffmpeg" not in command[0] for command in started), started


# --- slicing ----------------------------------------------------------------------


def test_every_media_file_is_checked_exactly_once_under_impossible_slices(
    monkeypatch, tmp_path: Path
) -> None:
    """The bug the catalog and artwork stages both shipped with: a slice that
    rebuilds its worklist re-examines item one forever."""
    conn, settings = build(tmp_path, MEDIA)
    seen = fake_probe(monkeypatch)
    enqueue(conn)

    for _ in range(400):
        if advance(conn, settings, seconds=0).finished:
            break
    else:
        raise AssertionError("never finished under zero-length slices")

    counters = progress(conn)["counters"]
    assert counters.storage_total == len(MEDIA)
    assert counters.storage_checked == len(MEDIA), "some file was never checked"
    assert len(seen) == len(MEDIA), f"a file was probed more than once: {len(seen)}"


def test_the_checked_count_only_climbs(monkeypatch, tmp_path: Path) -> None:
    conn, settings = build(tmp_path, MEDIA)
    fake_probe(monkeypatch)
    enqueue(conn)

    counts = []
    for _ in range(400):
        result = advance(conn, settings, seconds=0)
        counts.append(progress(conn)["counters"].storage_checked)
        if result.finished:
            break

    assert counts == sorted(counts), "a slice restarted work already done"


# --- the cache --------------------------------------------------------------------


def test_a_second_audit_of_an_unchanged_library_probes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    """The claim the probe counter exists to make falsifiable."""
    conn, settings = build(tmp_path, MEDIA)
    seen = fake_probe(monkeypatch)
    enqueue(conn)
    drain(conn, settings)
    first = progress(conn)["counters"].storage_probes

    seen.clear()
    enqueue(conn)
    drain(conn, settings)
    second = progress(conn)["counters"].storage_probes

    assert first == len(MEDIA), first
    assert second == 0, f"{second} files were probed again for no reason"
    assert seen == []


def test_a_changed_file_is_probed_again(monkeypatch, tmp_path: Path) -> None:
    """The cache is keyed on the fingerprint, so a rewrite reopens the question."""
    conn, settings = build(tmp_path, MEDIA)
    fake_probe(monkeypatch)
    enqueue(conn)
    drain(conn, settings)

    changed = settings.library_dir / "Music/track00.wav"
    changed.write_bytes(b"RIFF-different-bytes-entirely")
    scan_root(conn, "library", settings.library_dir, settings)
    enqueue(conn)
    drain(conn, settings)

    assert progress(conn)["counters"].storage_probes == 1


# --- what it records ---------------------------------------------------------------


def test_an_opportunity_remembers_which_file_it_was_about(
    monkeypatch, tmp_path: Path
) -> None:
    """Queue decisions later have to be tied to the exact source state."""
    conn, settings = build(tmp_path, MEDIA)
    fake_probe(monkeypatch)
    enqueue(conn)
    drain(conn, settings)

    rows = optimization.open_opportunities(conn)

    assert len(rows) == len(MEDIA)
    for row in rows:
        assert row["item_id"], "no item id"
        assert row["fingerprint"], "no fingerprint"
        assert row["rule_version"] == optimization.RULE_VERSION


def test_an_efficient_library_records_nothing_and_says_so(tmp_path: Path) -> None:
    """The real installation's result: 45 FLAC and 3 MP3, zero opportunities.

    Real probes here, no monkeypatch — these are not decodable files, so the
    probe fails and the stage records nothing. What is asserted is that the
    stage ran and reported honestly rather than being skipped.
    """
    conn, settings = build(
        tmp_path,
        {f"Music/track{index:02d}.flac": b"fLaC" + bytes([index]) for index in range(4)},
    )
    enqueue(conn)
    drain(conn, settings)

    counters = progress(conn)["counters"]
    assert counters.storage_total == 4
    assert counters.storage_checked == 4
    assert counters.storage_opportunities == 0
    assert optimization.open_opportunities(conn) == []


def test_the_progress_panel_reports_zero_as_a_result(tmp_path: Path) -> None:
    """"48 media files checked, 0 opportunities" is the answer an efficient
    library should get. Hiding the line would make a working advisor
    indistinguishable from an absent one."""
    from librairy.audit_job import Counters, _tool_rows

    rows = dict(_tool_rows(Counters(storage_total=48, storage_checked=48)))

    assert rows["Storage"] == "0 opportunities in 48 files"
