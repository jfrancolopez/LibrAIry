"""Turning an approved job into an argv, and an output file into a verdict.

Two things live here and nothing else: the **command builder**, which is the
only code in LibrAIry permitted to decide what FFmpeg is asked to do, and the
**verifier**, which decides whether what came back is the file that was asked
for. Neither starts a process. `optimization_process` does that.

The split matters because the security properties are all in the builder and
all of them are testable without an encoder: nothing a form can post reaches an
argument, the preset is chosen from a fixed table, the source is resolved under
a known root and the output under the job's own staging directory. A test can
assert every one of those on a job row in microseconds.

## Where the resource policy went

`ResourcePolicy` used to be defined here, because for one release the encoder
was the only workload in LibrAIry with any resource control at all. It is
`resources.EncoderPolicy` now — unchanged in every field, including the
measurements in its docstring — so that the module about how hard the program
may work holds all of it, and this module holds only the command builder and
the verifier. `LOW` is still the policy every mode but Full Power uses, and it
is still the only one the encoder gets unless somebody asks for more.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from librairy.config import Settings
from librairy.optimization_queue import job_staging_dir
from librairy.paths import validate_relpath
from librairy.resources import FULL, LOW, PROCESSING_MODES, EncoderPolicy

# --- what a job may ask for ------------------------------------------------------
#
# An allowlist keyed by the job's `preset` column. A job carries the *name* of
# an operation; the arguments are looked up here. There is no path by which a
# request, an API client or an edited opportunity contributes a flag.

FLAC = "flac-lossless"
REMUX = "mp4-stream-copy"
HEVC = "hevc-1080p-low"

PRESETS = (FLAC, REMUX, HEVC)

# The container the output goes in, per preset. Fixed, because the extension is
# part of the operation's identity and not a user preference.
PRESET_SUFFIX = {FLAC: ".flac", REMUX: ".mp4", HEVC: ".mp4"}

PRESET_LABEL = {
    FLAC: "LOSSLESS",
    REMUX: "REMUX",
    HEVC: "LOSSY",
}


class ExecutionRefused(RuntimeError):
    """This job cannot safely be run. The advisor may still be right about it.

    An opportunity is a judgement about storage; executability is a judgement
    about whether an automatic conversion can be performed without losing
    something. A 14 GB film with three audio tracks and two subtitle streams is
    a genuine opportunity and not something v1 will touch.
    """


#  Kept as names here because everything that builds a command imports them
#  from here, and because "the encoder's policy" is a sentence about the
#  encoder. The definition and the measurements are in `librairy/resources.py`.
ResourcePolicy = EncoderPolicy

# Both are measured, and they are the two ends of the same table — see
# `resources.EncoderPolicy`. `Low` is what every processing mode but Full Power
# uses, and there is still no editable middle: a policy nobody has measured is
# a promise nobody checked.
FULL_ENCODER = PROCESSING_MODES[FULL].encoder
POLICIES = {LOW.name: LOW, FULL_ENCODER.name: FULL_ENCODER}


# --- the stream shapes v1 will touch ----------------------------------------------

# Audio codecs that may be copied into an MP4 without re-encoding. If the audio
# is not one of these, v1 refuses the job rather than inventing a second lossy
# encode to go with the first: a video conversion that quietly re-encodes the
# soundtrack is a different operation from the one the user approved.
MP4_COPYABLE_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3", "alac", "flac", "opus"})

TRANSCODABLE_VIDEO = frozenset({"h264"})


@dataclass(frozen=True)
class Streams:
    """What ffprobe found, reduced to what the decision needs."""

    video: list[dict]
    audio: list[dict]
    subtitle: list[dict]
    other: list[dict]
    duration: float = 0.0

    @property
    def simple(self) -> bool:
        """One video, one audio, nothing else.

        Not a shortcut: it is the whole safety argument for automatic video
        execution. Anything richer has something in it that a single-video,
        single-audio command line would silently drop, and dropping a
        commentary track is not a storage saving.
        """
        return (
            len(self.video) == 1
            and len(self.audio) == 1
            and not self.subtitle
            and not self.other
        )


def probe_streams(path: Path) -> Streams:
    """Ask ffprobe what is actually in the file. Never trust the extension."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise ExecutionRefused("this file could not be read by ffprobe")
    payload = json.loads(result.stdout or "{}")
    buckets: dict[str, list[dict]] = {"video": [], "audio": [], "subtitle": [], "other": []}
    for stream in payload.get("streams", []):
        kind = str(stream.get("codec_type", ""))
        # An attached cover image is a video stream by codec_type and is not a
        # video track. Counting it as one would refuse every tagged album.
        if kind == "video" and stream.get("disposition", {}).get("attached_pic"):
            buckets["other"].append(stream)
        else:
            buckets.get(kind, buckets["other"]).append(stream)
    try:
        duration = float(payload.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return Streams(
        video=buckets["video"],
        audio=buckets["audio"],
        subtitle=buckets["subtitle"],
        other=buckets["other"],
        duration=duration,
    )


def check_executable(preset: str, streams: Streams) -> None:
    """Refuse anything v1 cannot convert without losing part of the file."""
    if preset == FLAC:
        if len(streams.audio) != 1:
            raise ExecutionRefused("this file does not hold a single audio track")
        return
    if preset == REMUX:
        if not streams.video:
            raise ExecutionRefused("there is no video stream to remux")
        if any(
            stream.get("codec_name") not in MP4_COPYABLE_AUDIO
            for stream in streams.audio
        ):
            raise ExecutionRefused("an audio track cannot be carried into MP4 unchanged")
        if streams.subtitle:
            raise ExecutionRefused("subtitles cannot be carried over safely yet")
        return
    if preset == HEVC:
        if not streams.simple:
            raise ExecutionRefused(
                "automatic conversion is not supported for this stream layout"
            )
        if streams.video[0].get("codec_name") not in TRANSCODABLE_VIDEO:
            raise ExecutionRefused("only H.264 sources are converted automatically")
        if streams.audio[0].get("codec_name") not in MP4_COPYABLE_AUDIO:
            raise ExecutionRefused("the audio track would have to be re-encoded as well")
        return
    raise ExecutionRefused("unknown operation")


# --- the command ------------------------------------------------------------------


def source_path(settings: Settings, job) -> Path:
    """The source, resolved under the root the job names and nowhere else."""
    roots = {
        "library": settings.library_dir,
        "inbox": settings.inbox_dir,
        "quarantine": settings.quarantine_dir,
    }
    root = roots.get(job["root"])
    if root is None:
        raise ExecutionRefused("that file is not in a root LibrAIry manages")
    path = validate_relpath(root, job["relpath"], kind="source")
    if not path.is_file():
        raise ExecutionRefused("this file is not where LibrAIry last saw it")
    return path


def output_path(settings: Settings, job) -> Path:
    """Inside this job's own staging directory, always.

    Derived from the job id and the preset, never from anything supplied with
    the request, so there is no input that makes the output land on top of the
    source or anywhere else in the library.
    """
    suffix = PRESET_SUFFIX.get(job["preset"])
    if suffix is None:
        raise ExecutionRefused("unknown operation")
    return job_staging_dir(settings, int(job["id"])) / f"output{suffix}"


def build_ffmpeg_command(
    settings: Settings,
    job,
    policy: ResourcePolicy = LOW,
    *,
    progress_path: Path | None = None,
) -> list[str]:
    """The argv for one job. No shell, ever, and no flag from outside.

    Everything variable is a path this module computed or an integer from the
    policy table. The job contributes a preset *name*, which selects a branch.
    """
    preset = job["preset"]
    if preset not in PRESETS:
        raise ExecutionRefused("unknown operation")
    source = source_path(settings, job)
    output = output_path(settings, job)
    if output.resolve() == source.resolve():  # pragma: no cover - staging is elsewhere
        raise ExecutionRefused("the output would overwrite the source")

    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if progress_path is not None:
        # Machine-readable key=value lines. Parsing the decorative status line
        # means parsing something FFmpeg is free to reformat.
        argv += ["-progress", str(progress_path), "-stats_period", "1"]
    argv += ["-i", str(source)]

    if preset == FLAC:
        # `-sample_fmt` deliberately absent: FLAC's encoder keeps the source
        # bit depth, and naming one is how a 24-bit master becomes 16-bit while
        # the UI says "lossless".
        argv += ["-map", "0:a:0", "-c:a", "flac", "-compression_level", "8"]
    elif preset == REMUX:
        # Stream copy, asserted structurally by `test_remux_never_re_encodes`.
        # If this line ever grows an encoder name, that test fails.
        argv += ["-map", "0", "-c", "copy", "-movflags", "+faststart"]
    else:
        argv += [
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx265", "-preset", "medium", "-crf", "28",
            "-x265-params", policy.x265_params,
            "-threads", str(policy.threads),
            "-c:a", "copy",
            "-tag:v", "hvc1",
            "-movflags", "+faststart",
        ]
    argv.append(str(output))
    return argv


def priority_prefix(policy: ResourcePolicy = LOW) -> list[str]:
    """`nice`/`ionice` if this runtime has them, otherwise nothing.

    Detected rather than assumed, and never installed: a package added to a
    NAS image behind the user's back to satisfy a nicety is a worse trade than
    running at normal priority. The bound that matters is the encoder pool.
    """
    prefix: list[str] = []
    if shutil.which("ionice"):
        prefix += ["ionice", "-c", str(policy.ionice_class)]
    if shutil.which("nice"):
        prefix += ["nice", "-n", str(policy.nice)]
    return prefix


# --- did it produce the file that was asked for? ----------------------------------


@dataclass(frozen=True)
class Verdict:
    ok: bool
    detail: str = ""


# How far the output's duration may differ from the source's before it is
# treated as a different film. Generous: container rounding and a trailing
# partial frame are normal, losing a reel is not.
DURATION_TOLERANCE_SECONDS = 1.0
DURATION_TOLERANCE_RATIO = 0.01


def verify_output(job, source: Streams, output: Path) -> Verdict:
    """Exit code 0 means FFmpeg did not crash. This asks the other question.

    Every failure here leaves the source alone and the job `failed`; none of
    them is recoverable by trying again, so none of them retries.
    """
    if not output.exists():
        return Verdict(False, "the converted file was not written")
    if output.stat().st_size == 0:
        return Verdict(False, "the converted file is empty")
    try:
        result = probe_streams(output)
    except ExecutionRefused:
        return Verdict(False, "the converted file could not be read back")

    preset = job["preset"]
    if preset == FLAC:
        if not result.audio:
            return Verdict(False, "the converted file has no audio stream")
        got = result.audio[0]
        want = source.audio[0]
        if got.get("codec_name") != "flac":
            return Verdict(False, f"expected FLAC, found {got.get('codec_name')}")
        for field, name in (("channels", "channel count"), ("sample_rate", "sample rate")):
            if str(got.get(field)) != str(want.get(field)):
                return Verdict(False, f"the {name} changed")
    elif preset == REMUX:
        # The container changed and nothing else. A codec that differs means
        # something re-encoded, which is not what was approved.
        if len(result.video) != len(source.video):
            return Verdict(False, "a video stream is missing")
        if len(result.audio) != len(source.audio):
            return Verdict(False, "an audio stream is missing")
        for got, want in zip(
            result.video + result.audio, source.video + source.audio, strict=True
        ):
            if got.get("codec_name") != want.get("codec_name"):
                return Verdict(False, "a stream was re-encoded rather than copied")
        if _geometry(result.video[0]) != _geometry(source.video[0]):
            return Verdict(False, "the picture size changed")
        if _fps(result.video[0]) != _fps(source.video[0]):
            return Verdict(False, "the frame rate changed")
    else:
        if not result.video:
            return Verdict(False, "the converted file has no video stream")
        if result.video[0].get("codec_name") != "hevc":
            return Verdict(False, f"expected HEVC, found {result.video[0].get('codec_name')}")
        if _geometry(result.video[0]) != _geometry(source.video[0]):
            return Verdict(False, "the picture size changed")
        if _fps(result.video[0]) != _fps(source.video[0]):
            return Verdict(False, "the frame rate changed")
        if not result.audio:
            return Verdict(False, "the audio track is missing")
        if result.audio[0].get("codec_name") != source.audio[0].get("codec_name"):
            return Verdict(False, "the audio track was re-encoded")

    if source.duration and result.duration:
        allowed = max(
            DURATION_TOLERANCE_SECONDS, source.duration * DURATION_TOLERANCE_RATIO
        )
        if abs(result.duration - source.duration) > allowed:
            return Verdict(False, "the running time does not match the original")
    return Verdict(True)


def _geometry(stream: dict) -> tuple:
    return (stream.get("width"), stream.get("height"))


def _fps(stream: dict) -> str:
    """The frame rate as FFmpeg states it, compared as a rational string.

    `24000/1001` and `23.976` are the same rate and different floats, so the
    comparison is between the fractions FFmpeg reports on both sides rather
    than between numbers this module derived.
    """
    return str(stream.get("r_frame_rate") or "")
