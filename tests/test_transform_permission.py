"""Whether LibrAIry may ever *propose* a conversion — and what that is not.

Two things this gate exists to keep straight.

**Unset is not "no".** A category nobody has answered must behave exactly as it
did before Format Policy existed. Reading silence as refusal would switch off
working behaviour on the day a table appeared, which is the worst possible way
for a settings feature to arrive.

**Permission is not capability.** Saying *yes, you may propose lossy
conversions* does not create a FLAC-to-MP3 encoder. It only stops the policy
standing in the way of one that already exists, and there is no new one here.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from librairy.config import Settings
from librairy.db import connect
from librairy.format_policy import refused_classes, refuses, set_transforms
from librairy.optimization import LOSSLESS, LOSSY, MediaFacts, advise
from librairy.scanner import scan_root

KEEPSAKES = "Music/Family Recordings"
WAV = f"{KEEPSAKES}/Grandad 1994.wav"


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        FILE_STABILITY_SECONDS=0,
        _env_file=None,
    )
    for root in (
        settings.appdata_dir,
        settings.inbox_dir,
        settings.library_dir,
        settings.quarantine_dir,
    ):
        root.mkdir(parents=True, exist_ok=True)
    return settings


def a_wav_library(tmp_path: Path) -> tuple[sqlite3.Connection, Settings]:
    settings = settings_for(tmp_path)
    conn = connect(settings)
    path = settings.library_dir / WAV
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" * 200)
    scan_root(conn, "library", settings.library_dir, settings)
    return conn, settings


def pcm_facts() -> MediaFacts:
    """A big uncompressed recording — the strongest opportunity there is."""
    return MediaFacts(
        container="wav",
        size=800 * 1024 * 1024,
        duration=1800.0,
        audio_codec="pcm_s16le",
        sample_rate=44100,
        bit_depth=16,
        audio_channels=2,
    )


# --------------------------------------------------------------------------
# 1-5: the gate
# --------------------------------------------------------------------------


def test_an_unanswered_permission_leaves_eligibility_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    """The whole compatibility story, in one assertion."""
    conn, _ = a_wav_library(tmp_path)

    assert refused_classes(conn, WAV) == frozenset()
    found = advise(WAV, pcm_facts(), refused=refused_classes(conn, WAV))

    assert found is not None
    assert found.quality == LOSSLESS


def test_an_explicit_refusal_stops_a_new_opportunity_being_offered(
    tmp_path: Path,
) -> None:
    conn, _ = a_wav_library(tmp_path)
    set_transforms(conn, "music", lossless=False)

    assert advise(WAV, pcm_facts(), refused=refused_classes(conn, WAV)) is None


def test_permitting_a_conversion_does_not_manufacture_one(
    tmp_path: Path,
) -> None:
    """Permission is not capability.

    LibrAIry has no FLAC-to-MP3 conversion, and saying *yes, you may propose
    lossy conversions* must not appear to create one. There is nothing on the
    other side of this switch.
    """
    conn, _ = a_wav_library(tmp_path)
    set_transforms(conn, "music", lossy=True, lossless=True)

    flac = MediaFacts(
        container="flac", size=400 * 1024 * 1024, duration=1800.0,
        audio_codec="flac", sample_rate=44100, bit_depth=16, audio_channels=2,
    )

    assert advise(f"{KEEPSAKES}/Grandad 1994.flac", flac) is None
    assert refused_classes(conn, WAV) == frozenset()


def test_forbidding_lossless_does_not_forbid_lossy_or_the_reverse(
    tmp_path: Path,
) -> None:
    """Two permissions, two questions. A WAV becoming FLAC discards nothing;
    an H.264 re-encode discards something. Somebody may well want one and not
    the other."""
    conn, _ = a_wav_library(tmp_path)

    set_transforms(conn, "music", lossless=False)
    assert refused_classes(conn, WAV) == frozenset({"lossless", "remux"})

    set_transforms(conn, "music", lossless=True, lossy=False)
    assert refused_classes(conn, WAV) == frozenset({"lossy", "derivative"})


def test_a_permission_never_overrides_a_technical_refusal(
    tmp_path: Path,
) -> None:
    """Policy says what is wanted; the pipeline says what is safe.

    A file too small for the opportunity to be worth mentioning stays
    unmentioned however enthusiastic the permission is.
    """
    conn, _ = a_wav_library(tmp_path)
    set_transforms(conn, "music", lossless=True)

    tiny = MediaFacts(
        container="wav", size=4096, duration=0.5, audio_codec="pcm_s16le",
        sample_rate=44100, bit_depth=16, audio_channels=2,
    )

    assert advise(WAV, tiny, refused=refused_classes(conn, WAV)) is None


def test_a_video_transcode_is_governed_by_the_lossy_permission(
    tmp_path: Path,
) -> None:
    conn, settings = a_wav_library(tmp_path)
    film = settings.library_dir / "Movies" / "Arrival (2016)"
    film.mkdir(parents=True)
    (film / "Arrival (2016).mkv").write_bytes(b"x" * 100)
    relpath = "Movies/Arrival (2016)/Arrival (2016).mkv"
    facts = MediaFacts(
        container="mkv", size=12 * 1024 * 1024 * 1024, duration=7200.0,
        video_codec="h264", audio_codec="aac", width=1920, height=1080,
        video_bitrate=14_000_000, audio_bitrate=192_000, frame_rate="24/1",
    )
    offered = advise(relpath, facts)
    assert offered is not None and offered.quality == LOSSY

    set_transforms(conn, "movies", lossy=False)

    assert advise(relpath, facts, refused=refused_classes(conn, relpath)) is None


# --------------------------------------------------------------------------
# 6-7: what the gate must leave alone
# --------------------------------------------------------------------------


def test_withdrawing_a_permission_refuses_an_existing_opportunity(
    tmp_path: Path,
) -> None:
    """Asked again at the moment of action, like protection already is.

    A permission can be withdrawn between an opportunity appearing and somebody
    pressing the button, and the button is the part that spends an hour of CPU.
    """
    from librairy import optimization_queue as queue
    from librairy.planner import utc_now

    conn, _ = a_wav_library(tmp_path)
    now = utc_now()
    item = conn.execute(
        "SELECT id FROM items WHERE relpath=?", (WAV,)
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO optimization_opportunities(
          id, item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (1, ?, 'library', ?, 'audio-to-flac', 'lossless', 1000, 500,
                  '', '', 'low', 'WAV', 'FLAC', '', '[]', 'fp', 1, 'open', ?, ?)
        """,
        (item, WAV, now, now),
    )

    set_transforms(conn, "music", lossless=False)

    assert refuses(conn, WAV, "lossless")
    with pytest.raises(queue.QueueRefused, match="does not permit lossless"):
        queue.enqueue(conn, 1)
    #  The opportunity is still there. It is a record of what was found, not a
    #  claim about what is going to happen.
    assert conn.execute(
        "SELECT status FROM optimization_opportunities WHERE id=1"
    ).fetchone()["status"] == "open"
    assert conn.execute("SELECT COUNT(*) FROM optimization_jobs").fetchone()[0] == 0


def test_a_finished_job_is_not_rewritten_by_a_later_permission(
    tmp_path: Path,
) -> None:
    """History is what happened. A permission set today does not unmake it."""
    from librairy.planner import utc_now

    conn, _ = a_wav_library(tmp_path)
    now = utc_now()
    item = conn.execute(
        "SELECT id FROM items WHERE relpath=?", (WAV,)
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO optimization_jobs(id, item_id, relpath, kind, quality, preset,"
        " state, queued_at, updated_at)"
        " VALUES (1, ?, ?, 'audio-to-flac', 'lossless', 'flac', 'adopted', ?, ?)",
        (item, WAV, now, now),
    )

    set_transforms(conn, "music", lossless=False)

    assert conn.execute(
        "SELECT state FROM optimization_jobs WHERE id=1"
    ).fetchone()["state"] == "adopted"


def test_the_gate_added_no_conversion_capability(tmp_path: Path) -> None:
    """Asserted structurally, because the failure mode is a helpful import.

    The advisor is a pure function of a path and a probe. It must not have
    learned to run anything.
    """
    import librairy.optimization as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    names |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    for forbidden in ("subprocess", "librairy.optimization_exec",
                      "librairy.optimization_process"):
        assert forbidden not in names
    assert tmp_path
