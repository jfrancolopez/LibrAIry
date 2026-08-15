"""Real encoders, generated files, and the claims that need a process to prove.

Everything here runs FFmpeg for real, on media this file generates. No fixture
is checked in and no file in anybody's library is touched — a test that
transcodes a real film to prove a point has already done the thing the whole
feature is careful not to do.

The claims worth reading first:

`test_launching_does_not_block_the_worker` — the architectural requirement. A
worker cycle that launched an encoder must return in well under the time the
encode takes, or every priority decision in `optimization_queue` is theory.

`test_the_inbox_is_still_filed_while_an_encode_runs` — the same claim from the
user's side, with a real file appearing in the inbox mid-encode.

`test_wav_to_flac_is_bit_exact` — "LOSSLESS" is a word on a screen until the
decoded PCM hashes match.

`test_cancelling_never_touches_a_process_we_did_not_start` — a NAS runs Plex
too.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from librairy import optimization_process as procs
from librairy import optimization_queue as queue
from librairy.config import Settings
from librairy.db import connect
from librairy.optimization_exec import (
    HEVC,
    LOW,
    REMUX,
    ExecutionRefused,
    build_ffmpeg_command,
    check_executable,
    probe_streams,
)
from librairy.planner import utc_now
from librairy.scanner import scan_root
from librairy.worker import Worker

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="these prove the encoder, so they need one",
)

MB = 1024 * 1024


# --- generated media --------------------------------------------------------------


def ffmpeg(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def make_wav(path: Path, *, seconds: int = 2, rate: int = 44100, channels: int = 2) -> Path:
    """Deterministic PCM: the same bytes on every machine, every run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        "-f", "lavfi",
        "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
        "-af", f"aformat=channel_layouts={'stereo' if channels == 2 else 'mono'}",
        "-c:a", "pcm_s16le", str(path),
    )
    return path


def make_video(
    path: Path,
    *,
    seconds: int = 2,
    size: str = "320x240",
    fps: int = 24,
    audio: bool = True,
    subtitles: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    ffmpeg(*args, str(path))
    if subtitles:
        srt = path.with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        merged = path.with_name("with-subs.mkv")
        ffmpeg("-i", str(path), "-i", str(srt), "-c", "copy", "-c:s", "srt", str(merged))
        merged.replace(path.with_suffix(".mkv"))
        return path.with_suffix(".mkv")
    return path


def pcm_hash(path: Path) -> str:
    """The audio itself, decoded to a canonical form, hashed.

    Not the file's bytes: a FLAC and a WAV of the same recording are different
    files and the same audio, and "lossless" is a claim about the second.
    """
    result = subprocess.run(  # noqa: S603
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-map", "0:a:0", "-f", "s32le", "-acodec", "pcm_s32le", "-"],
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def audio_facts(path: Path) -> dict:
    stream = probe_streams(path).audio[0]
    return {
        "channels": stream.get("channels"),
        "sample_rate": stream.get("sample_rate"),
        "codec": stream.get("codec_name"),
    }


# --- scene ------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _no_leaked_children():
    """Nothing this file starts may outlive the test that started it."""
    procs.forget_all()
    yield
    for job_id in procs.owned_jobs():
        handle = procs._OWNED[job_id]  # noqa: SLF001 - cleanup, not behaviour
        handle.process.kill()
        handle.process.wait()
    procs.forget_all()


def scene(tmp_path: Path, path: Path, kind: str):
    settings = settings_for(tmp_path)
    conn = connect(settings)
    scan_root(conn, "library", settings.library_dir, settings)
    relpath = str(path.relative_to(settings.library_dir))
    item = conn.execute("SELECT id, fingerprint FROM items WHERE relpath=?", (relpath,)).fetchone()
    conn.execute(
        """
        INSERT INTO optimization_opportunities(
          item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (?, 'library', ?, ?, 'lossless', ?, ?, '', '', 'low', 'X', 'Y',
                  '', '[]', ?, 1, 'open', ?, ?)
        """,
        (
            item["id"], relpath, kind, path.stat().st_size,
            int(path.stat().st_size * 0.6), item["fingerprint"], utc_now(), utc_now(),
        ),
    )
    opportunity_id = int(
        conn.execute("SELECT MAX(id) FROM optimization_opportunities").fetchone()[0]
    )
    job_id = queue.enqueue(conn, opportunity_id)
    return conn, settings, job_id


def job_row(conn, job_id: int):
    return conn.execute("SELECT * FROM optimization_jobs WHERE id=?", (job_id,)).fetchone()


def run_to_completion(conn, settings, job_id: int, *, limit: float = 180.0):
    """Poll the way the worker does, and never inside the encode."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        procs.poll(conn, settings)
        state = job_row(conn, job_id)["state"]
        if state in (queue.READY, queue.FAILED, queue.CANCELLED):
            return state
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# --- the command, before any process exists ------------------------------------------


def test_the_argv_never_contains_a_shell_or_an_outside_flag(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    argv = build_ffmpeg_command(settings, job_row(conn, job_id))

    assert argv[0] == "ffmpeg"
    assert all(isinstance(part, str) for part in argv)
    assert not any(char in " ".join(argv) for char in (";", "|", "&&", "`", "$("))


def test_the_output_can_only_land_in_this_jobs_staging(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    argv = build_ffmpeg_command(settings, job_row(conn, job_id))
    output = Path(argv[-1])

    assert output.parent == queue.job_staging_dir(settings, job_id)
    assert settings.library_dir not in output.parents
    assert output != source


def test_an_unknown_preset_is_refused_rather_than_run(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    conn.execute("UPDATE optimization_jobs SET preset='rm -rf' WHERE id=?", (job_id,))

    with pytest.raises(ExecutionRefused):
        build_ffmpeg_command(settings, job_row(conn, job_id))


def test_remux_never_re_encodes(tmp_path: Path) -> None:
    """Structural. If this command ever grows an encoder name, remux is a lie."""
    source = make_video(tmp_path / "library" / "Movies" / "clip.mkv")
    conn, settings, job_id = scene(tmp_path, source, "video-remux")

    argv = build_ffmpeg_command(settings, job_row(conn, job_id))

    assert "-c" in argv and argv[argv.index("-c") + 1] == "copy"
    assert "libx265" not in argv
    assert "libx264" not in argv


def test_low_bounds_the_encoder_pool_and_not_just_ffmpeg(tmp_path: Path) -> None:
    """`-threads` is FFmpeg's; libx265 builds its own pool and ignores it.

    Measured in the production image: pools=1 consumed 1.05 CPU-seconds per
    wall second, pools=2 2.09, pools=4 4.02, pools=8 6.22 — tracking the pool
    size and not the ten CPUs on the machine.
    """
    source = make_video(tmp_path / "library" / "Movies" / "clip.mp4")
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")

    argv = build_ffmpeg_command(settings, job_row(conn, job_id))

    assert "-x265-params" in argv
    params = argv[argv.index("-x265-params") + 1]
    assert f"pools={LOW.pools}" in params
    assert f"frame-threads={LOW.frame_threads}" in params
    assert argv[argv.index("-threads") + 1] == str(LOW.threads)


# --- advisable is not executable -------------------------------------------------------


def test_a_complex_video_is_refused_with_a_reason(tmp_path: Path) -> None:
    """The advisor is right that it is worth converting. The executor cannot.

    Refusing here rather than making the advisor stupider keeps the storage
    judgement and the safety judgement apart, which is what lets a 14 GB film
    with commentary tracks still be reported as an opportunity.
    """
    source = make_video(tmp_path / "library" / "Movies" / "complex.mp4", subtitles=True)
    streams = probe_streams(source)
    assert streams.subtitle

    with pytest.raises(ExecutionRefused) as raised:
        check_executable(HEVC, streams)

    assert "stream layout" in str(raised.value)


def test_nothing_is_ever_silently_dropped(tmp_path: Path) -> None:
    source = make_video(tmp_path / "library" / "Movies" / "subs.mp4", subtitles=True)
    streams = probe_streams(source)

    for preset in (REMUX, HEVC):
        with pytest.raises(ExecutionRefused):
            check_executable(preset, streams)


def test_an_album_cover_is_not_counted_as_a_video_track(tmp_path: Path) -> None:
    """Otherwise every tagged music file has "two video streams" and is refused."""
    source = make_wav(tmp_path / "library" / "Music" / "tagged.wav")
    tagged = source.with_suffix(".m4a")
    cover = tmp_path / "cover.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(cover))
    ffmpeg("-i", str(source), "-i", str(cover), "-map", "0:a", "-map", "1:v",
           "-c:a", "aac", "-c:v", "png", "-disposition:v:0", "attached_pic", str(tagged))

    streams = probe_streams(tagged)

    assert len(streams.audio) == 1
    assert streams.video == []


# --- WAV -> FLAC, proven -----------------------------------------------------------------


def test_wav_to_flac_is_bit_exact(tmp_path: Path) -> None:
    """The only evidence that justifies the word LOSSLESS on the screen."""
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav", seconds=3)
    before_hash = pcm_hash(source)
    before_bytes = source.read_bytes()
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    procs.launch(conn, settings, job_row(conn, job_id))
    assert run_to_completion(conn, settings, job_id) == queue.READY

    job = job_row(conn, job_id)
    output = Path(job["staging_dir"]) / job["output_relpath"]
    assert pcm_hash(output) == before_hash
    assert audio_facts(output)["channels"] == audio_facts(source)["channels"]
    assert audio_facts(output)["sample_rate"] == audio_facts(source)["sample_rate"]
    assert audio_facts(output)["codec"] == "flac"
    # And the original is exactly as it was, byte for byte.
    assert source.read_bytes() == before_bytes


def test_a_mono_source_stays_mono(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "mono.wav", channels=1)
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    procs.launch(conn, settings, job_row(conn, job_id))
    assert run_to_completion(conn, settings, job_id) == queue.READY

    job = job_row(conn, job_id)
    output = Path(job["staging_dir"]) / job["output_relpath"]
    assert audio_facts(output)["channels"] == 1
    assert pcm_hash(output) == pcm_hash(source)


# --- remux -------------------------------------------------------------------------------


def test_remux_changes_the_container_and_nothing_else(tmp_path: Path) -> None:
    source = make_video(tmp_path / "library" / "Movies" / "clip.mkv", seconds=3)
    before = probe_streams(source)
    conn, settings, job_id = scene(tmp_path, source, "video-remux")

    procs.launch(conn, settings, job_row(conn, job_id))
    assert run_to_completion(conn, settings, job_id) == queue.READY

    job = job_row(conn, job_id)
    after = probe_streams(Path(job["staging_dir"]) / job["output_relpath"])
    assert after.video[0]["codec_name"] == before.video[0]["codec_name"]
    assert after.audio[0]["codec_name"] == before.audio[0]["codec_name"]
    assert (after.video[0]["width"], after.video[0]["height"]) == (
        before.video[0]["width"], before.video[0]["height"],
    )
    assert after.video[0]["r_frame_rate"] == before.video[0]["r_frame_rate"]
    assert abs(after.duration - before.duration) < 1.0


# --- H.264 -> HEVC -------------------------------------------------------------------------


def test_hevc_preserves_everything_except_the_video_codec(tmp_path: Path) -> None:
    source = make_video(
        tmp_path / "library" / "Movies" / "clip.mp4", seconds=3, size="320x240", fps=24
    )
    before = probe_streams(source)
    assert before.video[0]["codec_name"] == "h264"
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")

    procs.launch(conn, settings, job_row(conn, job_id))
    assert run_to_completion(conn, settings, job_id) == queue.READY

    job = job_row(conn, job_id)
    after = probe_streams(Path(job["staging_dir"]) / job["output_relpath"])
    assert after.video[0]["codec_name"] == "hevc"
    assert (after.video[0]["width"], after.video[0]["height"]) == (320, 240)
    assert after.video[0]["r_frame_rate"] == before.video[0]["r_frame_rate"]
    assert after.audio[0]["codec_name"] == before.audio[0]["codec_name"]
    assert source.exists()


# --- the exit code is not the verdict ------------------------------------------------------


def test_a_successful_exit_code_still_goes_through_verifying(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    procs.launch(conn, settings, job_row(conn, job_id))
    run_to_completion(conn, settings, job_id)

    assert job_row(conn, job_id)["verified"] == "passed"


def test_a_truncated_output_fails_verification(tmp_path: Path) -> None:
    """The encoder succeeded and the file is wrong. Ready must not be reachable."""
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav", seconds=4)
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    procs.launch(conn, settings, job_row(conn, job_id))
    handle = procs._OWNED[job_id]  # noqa: SLF001
    handle.process.wait()
    # Half the recording, still a perfectly valid FLAC file.
    truncated = handle.output.with_name("half.flac")
    ffmpeg("-i", str(handle.output), "-t", "1", "-c:a", "flac", str(truncated))
    truncated.replace(handle.output)

    procs.poll(conn, settings)

    job = job_row(conn, job_id)
    assert job["state"] == queue.FAILED
    assert job["verified"] == "failed"
    assert "running time" in job["message"]
    assert source.exists()


def test_an_empty_output_fails_verification(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    procs.launch(conn, settings, job_row(conn, job_id))
    handle = procs._OWNED[job_id]  # noqa: SLF001
    handle.process.wait()
    handle.output.write_bytes(b"")

    procs.poll(conn, settings)

    job = job_row(conn, job_id)
    assert job["state"] == queue.FAILED
    assert "empty" in job["message"]


def test_a_failed_job_leaves_no_staging_behind(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    procs.launch(conn, settings, job_row(conn, job_id))
    handle = procs._OWNED[job_id]  # noqa: SLF001
    handle.process.wait()
    handle.output.write_bytes(b"")

    procs.poll(conn, settings)

    assert not queue.job_staging_dir(settings, job_id).exists()


# --- estimate and actual stay apart ------------------------------------------------------


def test_the_estimate_is_never_overwritten_by_the_result(tmp_path: Path) -> None:
    """Otherwise there is no way to find out whether the advisor is any good."""
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav", seconds=3)
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    estimated = job_row(conn, job_id)["estimated_bytes"]

    procs.launch(conn, settings, job_row(conn, job_id))
    run_to_completion(conn, settings, job_id)

    job = job_row(conn, job_id)
    assert job["estimated_bytes"] == estimated
    assert job["actual_bytes"] > 0
    assert job["actual_bytes"] != estimated
    assert job["runtime_seconds"] > 0


# --- the architectural claim ---------------------------------------------------------------


def test_launching_does_not_block_the_worker(tmp_path: Path) -> None:
    """The requirement the whole design exists for.

    A long enough encode that a blocking implementation could not possibly
    return in time, then a launch that must come back immediately with the
    child still alive.
    """
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")

    started = time.monotonic()
    procs.launch(conn, settings, job_row(conn, job_id))
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"launch took {elapsed:.1f}s; it must not wait for the encode"
    assert procs._OWNED[job_id].process.poll() is None  # noqa: SLF001
    assert job_row(conn, job_id)["state"] == queue.RUNNING
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="test")


def test_the_inbox_is_still_filed_while_an_encode_runs(tmp_path: Path) -> None:
    """The same claim from the user's side, with a real file and a real worker."""
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")
    procs.launch(conn, settings, job_row(conn, job_id))
    assert procs._OWNED[job_id].process.poll() is None  # noqa: SLF001

    (settings.inbox_dir / "arrived-during-the-encode.txt").write_text("hi", encoding="utf-8")
    cycle_started = time.monotonic()
    summary = Worker(conn, settings).run_once()
    cycle = time.monotonic() - cycle_started

    assert summary.scanned >= 1, "the inbox was not scanned while the encoder ran"
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE relpath='arrived-during-the-encode.txt'"
    ).fetchone()[0] == 1
    assert cycle < 15.0, f"the worker cycle took {cycle:.1f}s; it waited for the encoder"
    assert procs._OWNED[job_id].process.poll() is None, "the encoder was interrupted"  # noqa: SLF001
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="test")


def test_a_second_optimization_never_starts(tmp_path: Path) -> None:
    first = make_video(tmp_path / "library" / "Movies" / "one.mp4", seconds=30, size="640x480")
    conn, settings, job_id = scene(tmp_path, first, "video-transcode")
    second = make_wav(tmp_path / "library" / "Music" / "two.wav")
    scan_root(conn, "library", settings.library_dir, settings)
    procs.launch(conn, settings, job_row(conn, job_id))

    # A second eligible job, and several idle worker cycles that could take it.
    item = conn.execute(
        "SELECT id, fingerprint FROM items WHERE relpath='Music/two.wav'"
    ).fetchone()
    conn.execute(
        """
        INSERT INTO optimization_opportunities(
          item_id, root, relpath, kind, quality, current_bytes, estimated_bytes,
          summary, reason, compute, from_label, to_label, protected_by, facts,
          fingerprint, rule_version, status, detected_at, updated_at
        ) VALUES (?, 'library', 'Music/two.wav', 'audio-to-flac', 'lossless', ?, ?,
                  '', '', 'low', 'WAV', 'FLAC', '', '[]', ?, 1, 'open', ?, ?)
        """,
        (item["id"], second.stat().st_size, 1000, item["fingerprint"], utc_now(), utc_now()),
    )
    queue.enqueue(conn, int(conn.execute(
        "SELECT MAX(id) FROM optimization_opportunities").fetchone()[0]))
    for _ in range(3):
        Worker(conn, settings).run_once()

    assert len(procs.owned_jobs()) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM optimization_jobs WHERE state IN ('running','verifying')"
    ).fetchone()[0] == 1
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="test")


def test_progress_is_read_from_ffmpeg_and_not_guessed(tmp_path: Path) -> None:
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")
    procs.launch(conn, settings, job_row(conn, job_id))

    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        procs.poll(conn, settings)
        if job_row(conn, job_id)["out_time_seconds"] > 0:
            break
        time.sleep(0.2)

    job = job_row(conn, job_id)
    assert job["out_time_seconds"] > 0
    assert 0 < job["progress"] <= 100
    assert job["duration_seconds"] > 0
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="test")


# --- cancellation ---------------------------------------------------------------------------


def test_cancelling_stops_the_child_and_leaves_the_source_alone(tmp_path: Path) -> None:
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    before = source.read_bytes()
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")
    procs.launch(conn, settings, job_row(conn, job_id))
    handle = procs._OWNED[job_id]  # noqa: SLF001
    pid = handle.process.pid

    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="Cancelled.")

    assert handle.process.poll() is not None, "the child was not reaped"
    assert procs.process_start_time(pid) is None or handle.process.returncode is not None
    assert not queue.job_staging_dir(settings, job_id).exists()
    assert job_row(conn, job_id)["state"] == queue.CANCELLED
    assert source.read_bytes() == before


def test_cancelling_never_touches_a_process_we_did_not_start(tmp_path: Path) -> None:
    """A NAS runs Plex too, and its encoder is not ours to stop."""
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    stranger = subprocess.Popen(  # noqa: S603
        # `-re` so it takes real time rather than finishing instantly, which
        # would make "still alive" prove nothing.
        ["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-re", "-f", "lavfi",
         "-i", "sine=duration=30", "-f", "null", "-"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Cancelling a job we never launched must not go looking for an ffmpeg.
        procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="Cancelled.")
        time.sleep(0.5)

        assert stranger.poll() is None, "an ffmpeg LibrAIry did not start was killed"
    finally:
        stranger.kill()
        stranger.wait()


def test_the_source_bytes_of_a_cancelled_job_are_unchanged(tmp_path: Path) -> None:
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav", seconds=3)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")

    procs.launch(conn, settings, job_row(conn, job_id))
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="Cancelled.")

    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


# --- restart --------------------------------------------------------------------------------


def test_a_restart_settles_the_job_instead_of_resuming_it(tmp_path: Path) -> None:
    """A row saying `running` with no worker behind it is what makes a UI lie.

    Resuming would be worse than saying so: nothing knows how much of the
    output is valid, and re-spending an hour of CPU because a container was
    updated is not a decision to take on somebody's behalf.
    """
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")
    procs.launch(conn, settings, job_row(conn, job_id))
    handle = procs._OWNED[job_id]  # noqa: SLF001

    # A new worker process: same database, different token, no handles.
    handle.process.kill()
    handle.process.wait()
    procs.forget_all()
    conn.execute("UPDATE optimization_jobs SET owner_token='a-worker-that-is-gone' WHERE id=?",
                 (job_id,))

    settled = procs.reconcile(conn, settings)

    assert settled == 1
    job = job_row(conn, job_id)
    assert job["state"] == queue.FAILED
    assert job["message"] == procs.INTERRUPTED_MESSAGE
    assert job["pid"] is None
    assert not queue.job_staging_dir(settings, job_id).exists()
    assert source.exists()
    assert not procs.owned_jobs(), "a restart must not adopt a running encode"


def test_reconcile_leaves_this_workers_own_job_alone(tmp_path: Path) -> None:
    source = make_video(
        tmp_path / "library" / "Movies" / "long.mp4", seconds=30, size="640x480"
    )
    conn, settings, job_id = scene(tmp_path, source, "video-transcode")
    procs.launch(conn, settings, job_row(conn, job_id))

    assert procs.reconcile(conn, settings) == 0
    assert job_row(conn, job_id)["state"] == queue.RUNNING
    procs.stop(conn, settings, job_id, state=queue.CANCELLED, message="test")


def test_a_recycled_pid_is_not_mistaken_for_our_encoder(tmp_path: Path) -> None:
    """The reason a stored PID is not identity."""
    source = make_wav(tmp_path / "library" / "Music" / "concert.wav")
    conn, settings, job_id = scene(tmp_path, source, "audio-to-flac")
    conn.execute(
        "UPDATE optimization_jobs SET state='running', pid=?, pid_started=?, "
        "owner_token='gone' WHERE id=?",
        (1, 999_999_999, job_id),
    )

    # PID 1 exists on every machine; its start time is not the one recorded.
    assert procs.still_ours(job_row(conn, job_id)) is False
