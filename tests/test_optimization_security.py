"""Properties of the execution path that hold without running an encoder.

Structural tests, read off the source. They are cheap, they run on every
machine including one with no FFmpeg, and they are the ones that would still
matter if every behavioural test above them were deleted: what may launch a
process, what may reach a shell, and what a form may contribute to an argv.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from librairy import optimization_process as procs
from librairy import optimization_queue as queue
from librairy.optimization_exec import (
    HEVC,
    LOW,
    POLICIES,
    PRESETS,
    ExecutionRefused,
    Streams,
    check_executable,
)

SOURCE = Path("src/librairy")
EXEC_MODULES = (
    SOURCE / "optimization_exec.py",
    SOURCE / "optimization_process.py",
)


# --- nothing reaches a shell ---------------------------------------------------------


@pytest.mark.parametrize("path", EXEC_MODULES, ids=lambda p: p.name)
def test_no_execution_module_ever_uses_a_shell(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "shell=True" not in text
    assert "os.system" not in text
    assert "subprocess.getoutput" not in text


def test_only_the_process_manager_starts_a_long_lived_child() -> None:
    """A request handler that launched FFmpeg would hold no lock, own no child
    and be impossible to poll. There is one place an encoder begins.

    `supervisor.py` is the other module allowed to hold a `Popen`, and it is a
    different thing entirely: it starts LibrAIry's own web and worker processes
    at boot and never touches media.
    """
    launchers = {
        path
        for path in SOURCE.rglob("*.py")
        if re.search(r"\bsubprocess\.Popen\b", path.read_text(encoding="utf-8"))
    }

    assert launchers == {
        SOURCE / "optimization_process.py",
        SOURCE / "supervisor.py",
    }
    assert "ffmpeg" not in (SOURCE / "supervisor.py").read_text(encoding="utf-8")


def test_no_web_module_imports_the_process_manager_launcher() -> None:
    """The web layer may cancel and discard. It may not start."""
    for path in (SOURCE / "web").rglob("*.py"):
        text = path.read_text(encoding="utf-8")

        assert "optimization_process import launch" not in text
        assert "procs.launch" not in text


# --- nothing greps for somebody else's encoder -----------------------------------------


@pytest.mark.parametrize("path", EXEC_MODULES, ids=lambda p: p.name)
def test_nothing_hunts_processes_by_name(path: Path) -> None:
    """A NAS runs Plex. `pkill ffmpeg` is how a media server dies at 3am."""
    text = path.read_text(encoding="utf-8")

    for weapon in ("pkill", "killall", "psutil", "ps -ef", "ps aux"):
        assert weapon not in text


def test_the_only_signal_target_is_a_pid_the_job_recorded() -> None:
    text = (SOURCE / "optimization_process.py").read_text(encoding="utf-8")

    # One `os.kill`, and the function holding it is reached only after
    # `still_ours` has matched both the PID and its kernel start time.
    assert text.count("os.kill(") == 1
    assert "def _signal_orphan" in text
    assert "if still_ours(job):" in text


# --- the preset allowlist ----------------------------------------------------------------


def test_a_job_contributes_a_name_and_never_an_argument() -> None:
    """The whole injection story. `preset` selects a branch; it is never
    interpolated into anything."""
    text = (SOURCE / "optimization_exec.py").read_text(encoding="utf-8")

    assert 'PRESET_SUFFIX = {' in text
    for preset in PRESETS:
        assert f'"{preset}"' in text or preset in (HEVC,)


def test_there_is_exactly_one_resource_policy() -> None:
    """A "High" nobody has measured is a promise nobody checked."""
    assert list(POLICIES) == [LOW.name]
    assert LOW.label == "Low"
    assert queue.MAX_CONCURRENT == 1


def test_the_low_policy_bounds_the_encoder_pool() -> None:
    assert LOW.pools == 2
    assert LOW.frame_threads == 2
    assert LOW.x265_params == "pools=2:frame-threads=2"


# --- stream layouts that are refused ------------------------------------------------------


def stream(codec: str, **extra) -> dict:
    return {"codec_name": codec, "width": 1920, "height": 1080, **extra}


COMPLEX = (
    pytest.param(
        Streams(video=[stream("h264")], audio=[stream("aac")] * 3, subtitle=[], other=[]),
        id="three-audio-tracks",
    ),
    pytest.param(
        Streams(
            video=[stream("h264")], audio=[stream("aac")],
            subtitle=[stream("hdmv_pgs")], other=[],
        ),
        id="pgs-subtitles",
    ),
    pytest.param(
        Streams(video=[stream("h264")] * 2, audio=[stream("aac")], subtitle=[], other=[]),
        id="two-video-streams",
    ),
    pytest.param(
        Streams(
            video=[stream("h264")], audio=[stream("aac")],
            subtitle=[], other=[stream("bin_data")],
        ),
        id="data-stream",
    ),
    pytest.param(
        Streams(video=[stream("hevc")], audio=[stream("aac")], subtitle=[], other=[]),
        id="already-hevc",
    ),
    pytest.param(
        Streams(video=[stream("h264")], audio=[stream("dts")], subtitle=[], other=[]),
        id="audio-that-cannot-be-copied",
    ),
)


@pytest.mark.parametrize("streams", COMPLEX)
def test_a_layout_v1_cannot_convert_is_refused(streams: Streams) -> None:
    with pytest.raises(ExecutionRefused):
        check_executable(HEVC, streams)


def test_a_simple_layout_is_accepted() -> None:
    check_executable(
        HEVC, Streams(video=[stream("h264")], audio=[stream("aac")], subtitle=[], other=[])
    )


def test_audio_is_copied_rather_than_re_encoded(tmp_path: Path) -> None:
    """A video conversion that quietly re-encodes the soundtrack is a different
    operation from the one that was approved."""
    text = (SOURCE / "optimization_exec.py").read_text(encoding="utf-8")

    assert '"-c:a", "copy"' in text
    # And no second lossy audio encoder anywhere in the builder.
    assert "libmp3lame" not in text
    assert '"-c:a", "aac"' not in text


# --- staging containment ---------------------------------------------------------------------


def test_staging_is_under_appdata_and_never_in_a_library(tmp_path: Path) -> None:
    from librairy.config import Settings

    settings = Settings(
        APPDATA_DIR=tmp_path / "appdata",
        INBOX_DIR=tmp_path / "inbox",
        LIBRARY_DIR=tmp_path / "library",
        QUARANTINE_DIR=tmp_path / "quarantine",
        _env_file=None,
    )

    staging = queue.job_staging_dir(settings, 7)

    assert settings.appdata_dir in staging.parents
    assert settings.library_dir not in staging.parents
    assert settings.inbox_dir not in staging.parents
    assert staging.name == "7"


def test_clearing_staging_refuses_a_path_outside_the_workspace(tmp_path: Path) -> None:
    """It re-derives the path from the job id rather than trusting the stored
    `staging_dir` column, which is a value somebody could have edited."""
    text = (SOURCE / "optimization_queue.py").read_text(encoding="utf-8")

    assert "def clear_staging" in text
    assert "job_staging_dir(settings, job_id).resolve()" in text
    assert "root not in target.parents" in text


def test_no_optimized_file_can_reach_the_library() -> None:
    """There is no adoption path, and this is the test that keeps it that way.

    It fails the moment somebody adds a move out of staging — which is exactly
    the change that would need a design decision rather than an implementation.
    """
    text = (SOURCE / "optimization_process.py").read_text(encoding="utf-8")

    assert "library_dir" not in text
    for word in ("shutil.move", "os.rename", "os.replace", ".replace("):
        assert word not in text


# --- the labels a finished result may carry --------------------------------------------------


def test_no_template_offers_to_use_the_optimized_file() -> None:
    """Adoption is not implemented. No button may imply that it is."""
    banned = (
        "Use result",
        "Use optimized",
        "Replace original",
        "Apply result",
        "Use this instead",
    )
    for page in Path("src/librairy/web/templates").rglob("*.html"):
        text = page.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, f"{page.name} offers adoption: {phrase}"


def test_a_finished_result_offers_one_action_and_not_two() -> None:
    """"Keep original" beside "Discard result" would look like a choice and do
    nothing: the original is already what the library holds."""
    text = (Path("src/librairy/web/templates") / "optimization.html").read_text(
        encoding="utf-8"
    )

    labels = re.findall(r"<button[^>]*>(.*?)</button>", text, re.S)
    labels = [" ".join(re.sub(r"<[^>]+>", "", label).split()) for label in labels]

    assert "Discard result" in labels
    assert not [label for label in labels if label.lower().startswith("keep original")]


# --- the worker's shape ------------------------------------------------------------------------


def test_the_worker_polls_on_every_cycle_and_launches_only_when_idle() -> None:
    """Launching is gated behind an idle cycle; noticing a finished encode is
    not, or a job that completed during a busy hour would sit in `running`."""
    text = (SOURCE / "worker.py").read_text(encoding="utf-8")
    body = text[text.index("def run_once(self)") : text.index("def _optimization_poll")]

    poll_at = body.index("self._optimization_poll(settings)")
    launch_at = body.index("self._optimization_slice(settings)")
    idle_gate = body.index("if not summary.did_work:")

    assert poll_at < idle_gate < launch_at


def test_nothing_in_the_worker_waits_for_the_encoder() -> None:
    text = (SOURCE / "optimization_process.py").read_text(encoding="utf-8")
    launch_body = text[text.index("def launch(") : text.index("def poll(")]

    # `communicate` and a bare `wait()` are the two ways a launch becomes a
    # blocking call by accident.
    assert ".communicate(" not in launch_body
    assert ".wait(" not in launch_body


def test_progress_comes_from_a_file_and_not_a_pipe() -> None:
    """A worker loop blocking on `readline()` against a process that has
    stopped emitting is the failure this avoids by construction."""
    text = (SOURCE / "optimization_process.py").read_text(encoding="utf-8")

    assert "-progress" in (SOURCE / "optimization_exec.py").read_text(encoding="utf-8")
    assert ".readline(" not in text
    assert "import threading" not in text
    assert "import asyncio" not in text


def test_progress_writes_are_throttled() -> None:
    assert procs.PROGRESS_PERSIST_SECONDS >= 1.0


def test_the_log_kept_in_the_database_is_bounded() -> None:
    assert procs.LOG_TAIL_BYTES <= 8000
