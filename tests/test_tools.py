from __future__ import annotations

import subprocess
from pathlib import Path

from librairy.config import Settings
from librairy.db import connect
from librairy.tools.common import get_cached_metadata, set_cached_metadata
from librairy.tools.exiftool import parse_exiftool
from librairy.tools.ffprobe import parse_ffprobe, probe
from librairy.tools.fpcalc import parse_fpcalc


def settings_for(tmp_path: Path) -> Settings:
    return Settings(APPDATA_DIR=tmp_path / "appdata", AI_TIMEOUT=1, _env_file=None)


def test_ffprobe_parser_reads_recorded_fixture() -> None:
    metadata = parse_ffprobe(
        {
            "format": {
                "format_name": "mp3",
                "duration": "123.4",
                "tags": {"ARTIST": "Queen", "ALBUM": "News"},
            },
            "streams": [{"codec_type": "audio"}],
        }
    )

    assert metadata.format_name == "mp3"
    assert metadata.duration == 123.4
    assert metadata.tags["artist"] == "Queen"
    assert metadata.streams[0]["codec_type"] == "audio"


def test_exiftool_parser_reads_recorded_fixture() -> None:
    metadata = parse_exiftool(
        {
            "Make": "Canon",
            "Model": "R5",
            "GPSLatitude": "41.0 N",
            "GPSLongitude": "2.0 E",
            "DateTimeOriginal": "2026:07:21 10:00:00",
        }
    )

    assert metadata.camera == "Canon R5"
    assert metadata.gps_latitude == "41.0 N"
    assert metadata.created_at == "2026:07:21 10:00:00"


def test_fpcalc_parser_reads_plain_output() -> None:
    parsed = parse_fpcalc("DURATION=12\nFINGERPRINT=abc123\n")

    assert parsed.duration == 12
    assert parsed.fingerprint == "abc123"


def test_missing_binary_returns_typed_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("librairy.tools.common.shutil.which", lambda binary: None)

    result = probe(tmp_path / "missing.mp3", settings_for(tmp_path))

    assert result.ok is False
    assert result.error == "missing binary: ffprobe"


def test_timeout_returns_typed_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("librairy.tools.common.shutil.which", lambda binary: f"/bin/{binary}")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    monkeypatch.setattr("librairy.tools.common.subprocess.run", timeout)
    result = probe(tmp_path / "slow.mp3", settings_for(tmp_path))

    assert result.ok is False
    assert result.error == "timeout: ffprobe"


def test_metadata_cache_hits_for_unchanged_fingerprint(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    item_id = conn.execute(
        """
        INSERT INTO items(root, relpath, size, mtime_ns, fingerprint, first_seen_at, last_seen_at)
        VALUES ('inbox', 'a.mp3', 1, 1, 'abc', 'now', 'now')
        """
    ).lastrowid
    set_cached_metadata(conn, item_id, "abc", "ffprobe", {"artist": "Queen"}, "now")

    assert get_cached_metadata(conn, item_id, "abc", "ffprobe") == {"artist": "Queen"}
    assert get_cached_metadata(conn, item_id, "changed", "ffprobe") is None
