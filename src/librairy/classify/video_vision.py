"""What a personal video is about, without ever showing a model the video.

Local vision on photos turned `IMG_1423.jpeg` into
`IMG_1423-child-outdoor-orange.jpeg`, which is the difference between a folder
you can search and a folder you can only scroll. Videos got none of it: a
`.MOV` from a phone is almost unknown to the deterministic pass, and the one
thing it did know — the extension — pointed at Movies, which is exactly wrong
for a nine-second clip of a dog.

The rule that shapes everything here: **the model sees images, never video.**
No clip is decoded for a model, no container is uploaded, no frame budget grows
with duration. `frames_for` returns image paths and nothing else, and a test
asserts that no video byte can reach a provider.

Three tiers, cheapest first, and the first one that answers wins:

**Tier 0 — the photo next to it.** `IMG_0585.MOV` usually has `IMG_0585.jpeg`
beside it, taken within seconds, already looked at by the local model. That
caption is free, already paid for, and often better evidence than a frame:
the photo is the moment somebody chose. It is recorded as *context*, never as
identity — the still and the clip are not guaranteed to show the same thing,
and saying they do is the kind of confident wrongness that makes people stop
trusting captions.

**Tier 1 — the thumbnail that already exists.** Browse renders one for every
video. If it is on disk, that is the frame, and it costs one inference and no
extra decoding.

**Tier 2 — three frames, as one image.** Only when tier 1 produced nothing
useful. 10%, 50% and 90% of the duration, stacked into a single contact sheet,
because one request with three frames costs less than three requests with one
frame each — on a small local model, far less. Three is the ceiling and it does
not move; no scene detection, no keyframe analysis, no "just a few more for
long videos".

Everything is cached against the file's fingerprint, the provider and model
that answered, and the strategy version — so re-running an audit re-reads a
row instead of re-decoding a video, and changing the frame strategy correctly
invalidates every answer produced by the old one.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LOGGER = logging.getLogger(__name__)

# Bump when the frames sent to the model change in a way that could change the
# answer. It is part of the cache key, so an old answer produced from a single
# thumbnail is not reused once the strategy became a contact sheet.
STRATEGY_VERSION = 1

# The ceiling, and it does not move with duration. A ten-second clip and a
# two-hour recording both get three frames, because the point is a hint about
# what is in shot, not a summary of a film.
MAX_FRAMES = 3
FRAME_POSITIONS = (0.1, 0.5, 0.9)
# One contact sheet is one request. Three separate frames would be three, and
# on a local model that is three model loads, three prompt encodings and three
# times the wait for an answer nobody is waiting on.
CONTACT_SHEET_WIDTH = 640
EXTRACT_TIMEOUT_SECONDS = 30

# What counts as a personal video worth a look. Deliberately not "every video":
# a 40 GB remux does not need a caption, and decoding a frame out of one to get
# one is the opposite of lightweight.
PHONE_EXTENSIONS = frozenset({".mov", ".mp4", ".m4v", ".3gp", ".avi"})
# Longer than this and it is not a phone moment; it is something with an
# identity a catalog should be answering for.
MAX_PERSONAL_SECONDS = 20 * 60

# Stems phones produce. A frame is a hint about content; these are the evidence
# that the file is a personal clip at all.
PHONE_STEMS = (
    "img_",
    "vid_",
    "mvimg_",
    "pxl_",
    "dsc_",
    "video-",
    "trim.",
)


@dataclass(frozen=True)
class Sibling:
    """A still photo that appears to be the same moment as a video."""

    item_id: int
    relpath: str
    caption: str
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    """How this video would be looked at, decided before any work is done."""

    # "paired-photo", "thumbnail", "contact-sheet", or "" for "do not look".
    strategy: str
    # Image paths only. Never a video path — see the module docstring, and
    # `test_no_full_video_bytes_ever_reach_a_provider`.
    frames: tuple[Path, ...] = ()
    sibling: Sibling | None = None
    reason: str = ""

    @property
    def needs_inference(self) -> bool:
        return bool(self.frames)

    @property
    def cache_key(self) -> str:
        return f"{self.strategy}-v{STRATEGY_VERSION}"


def looks_personal(relpath: str, duration: float | None = None) -> bool:
    """Whether this is a phone clip rather than something with an identity.

    A thumbnail is not allowed to answer this. A frame showing a person on a
    stage is equally consistent with a family video, a concert bootleg and a
    DJ music video, and the last of those has its own architecture built on
    filename parsing, tags and catalogs — a single frame must never be the
    reason a file lands in Music Videos or in Movies.
    """
    name = PurePosixPath(relpath).name.lower()
    if PurePosixPath(name).suffix not in PHONE_EXTENSIONS:
        return False
    if duration is not None and duration > MAX_PERSONAL_SECONDS:
        return False
    return any(name.startswith(stem) for stem in PHONE_STEMS)


def paired_photo(conn, item_id: int, relpath: str) -> Sibling | None:
    """The still beside the clip, if one exists and has already been described.

    `IMG_0585.jpeg` next to `IMG_0585.MOV` is how every iPhone writes a Live
    Photo and how most cameras write a burst. Matching is on the stem *and* the
    folder: `IMG_9323.jpeg` in a different folder is a different camera's
    counter reaching the same number, which the classifier learned the hard way
    from seven phone-camera folders.

    Returns nothing unless the photo already has a stored description. This
    tier exists to spend no inference at all; asking for one here would make
    the cheap path the expensive one.
    """
    from librairy.classify.images import stored_vision

    folder = str(PurePosixPath(relpath).parent)
    stem = PurePosixPath(relpath).stem.lower()
    rows = conn.execute(
        "SELECT id, relpath FROM items WHERE root='library' AND missing_since IS NULL"
        " AND id != ? AND relpath LIKE ?",
        (item_id, f"{folder}/%" if folder != "." else "%"),
    ).fetchall()
    for row in rows:
        candidate = PurePosixPath(row["relpath"])
        if str(candidate.parent) != folder or candidate.stem.lower() != stem:
            continue
        if candidate.suffix.lower() not in {".jpg", ".jpeg", ".png", ".heic"}:
            continue
        stored = stored_vision(conn, row["id"])
        if stored is None or not (stored.caption or stored.subjects):
            continue
        return Sibling(
            item_id=int(row["id"]),
            relpath=row["relpath"],
            caption=stored.caption or "",
            subjects=tuple(stored.subjects),
        )
    return None


def plan_for(
    conn,
    item_id: int,
    relpath: str,
    *,
    thumbnail: Path | None = None,
    duration: float | None = None,
) -> Plan:
    """Decide how to look at this video, before doing any of it.

    Deciding and doing are separate on purpose. A caller can see that exactly
    one inference is about to happen and refuse it; a test can check the budget
    without decoding anything; and the expensive tier cannot be reached by
    accident, because reaching it means calling `frames_for` as well.
    """
    if not looks_personal(relpath, duration):
        return Plan("", reason="not a personal video")

    sibling = paired_photo(conn, item_id, relpath)
    if sibling is not None:
        # Free, already paid for, and usually the better evidence.
        return Plan("paired-photo", sibling=sibling, reason="a described photo sits beside it")

    if thumbnail is not None and thumbnail.is_file() and thumbnail.stat().st_size:
        # Already rendered for Browse. Re-extracting a frame to get the same
        # picture is work nobody asked for.
        return Plan(
            "thumbnail", frames=(thumbnail,), reason="the preview thumbnail already exists"
        )

    if not duration or duration <= 0:
        # Without a duration there is nowhere to seek to, and guessing at
        # timestamps in a file of unknown length is how you get three black
        # frames and one inference spent on them.
        return Plan("", reason="no thumbnail and no duration to seek by")
    return Plan(
        "contact-sheet",
        reason=f"{MAX_FRAMES} frames from a {round(duration)}s clip, as one image",
    )


def frames_for(plan: Plan, source: Path, workdir: Path, duration: float) -> tuple[Path, ...]:
    """The images this plan will actually send, materialised.

    The only function that decodes anything, and it returns image paths — never
    `source`. That is the whole guarantee: a caller has no way to reach a
    provider holding a video, because nothing here ever hands one back.
    """
    if plan.strategy != "contact-sheet":
        return plan.frames
    sheet = contact_sheet(source, workdir / f"{source.stem}-sheet.jpg", duration)
    return (sheet,) if sheet is not None else ()


def contact_sheet(source: Path, target: Path, duration: float) -> Path | None:
    """Three frames stacked into one JPEG, or nothing.

    One ffmpeg call, `-frames:v 1` per position, argv only and never a shell.
    The output is an image; that is the only thing this function is allowed to
    hand back, and the only thing a provider will ever be given.
    """
    if shutil.which("ffmpeg") is None or duration <= 0:
        return None
    stills: list[Path] = []
    target.parent.mkdir(parents=True, exist_ok=True)
    for index, position in enumerate(FRAME_POSITIONS[:MAX_FRAMES]):
        still = target.with_name(f"{target.stem}-{index}.jpg")
        if not _grab(source, still, duration * position):
            continue
        stills.append(still)
    if not stills:
        return None
    made = _stack(stills, target)
    for still in stills:
        still.unlink(missing_ok=True)
    return target if made else None


def _grab(source: Path, target: Path, at_seconds: float) -> bool:
    command = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{max(0.0, at_seconds):.2f}",
        "-i", str(source),
        "-frames:v", "1",
        "-vf", f"scale='min({CONTACT_SHEET_WIDTH},iw)':-2",
        str(target),
    ]
    return _run(command) and target.is_file() and target.stat().st_size > 0


def _stack(stills: list[Path], target: Path) -> bool:
    if len(stills) == 1:
        stills[0].replace(target)
        return True
    inputs: list[str] = []
    for still in stills:
        inputs += ["-i", str(still)]
    command = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", f"vstack=inputs={len(stills)}",
        "-frames:v", "1",
        str(target),
    ]
    return _run(command) and target.is_file() and target.stat().st_size > 0


def _run(command: list[str]) -> bool:
    """One ffmpeg call. argv only, never `shell=True`, and always bounded."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command, capture_output=True, timeout=EXTRACT_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("frame extraction failed: %s", exc)
        return False
    return result.returncode == 0


def sibling_evidence(sibling: Sibling) -> list:
    """What the paired photo is worth, said carefully.

    "A photo taken at the same moment shows X" is true and useful. "This video
    shows X" is a claim about frames nobody looked at, and it is the claim a
    reader will take away unless the wording stops them.
    """
    from librairy.models import EvidenceEntry

    words = sibling.caption or ", ".join(sibling.subjects)
    return [
        EvidenceEntry(
            "vision",
            "paired phone photo",
            PurePosixPath(sibling.relpath).name,
            0.6,
            note="the still beside this clip, same name and folder",
        ),
        EvidenceEntry(
            "vision",
            "visual hint",
            words,
            0.5,
            note="from the paired photo, not from the video",
        ),
    ]


def frame_evidence(caption: str, strategy: str) -> list:
    """What a frame is worth. A hint, and labelled as one.

    A frame is not the clip. A single still of a kitchen says nothing about the
    ninety seconds after it, and a caption presented as the video's identity
    would be believed.
    """
    from librairy.models import EvidenceEntry

    source = "one frame" if strategy == "thumbnail" else f"{MAX_FRAMES} frames"
    return [
        EvidenceEntry(
            "vision",
            "visual hint",
            caption,
            0.5,
            note=f"read from {source}, which may not describe the whole clip",
        )
    ]


def enrich_video(
    conn,
    settings,
    item,
    result,
    state=None,
    *,
    provider=None,
    workdir=None,
):
    """Fold a hint about a personal video into `result`, or change nothing.

    The same contract the image path has, for the same reason: this is an
    enrichment, and an enrichment that can break analysis is not one. Every
    failure — feature off, not a phone clip, no local provider, no ffmpeg, a
    provider that timed out — returns `result` exactly as it arrived.

    The budget is one inference per eligible video, and it is visible here
    rather than implied: `plan_for` decides, at most one `describe_image` call
    follows, and nothing loops.
    """
    from librairy.ai.vision import describe_image
    from librairy.classify.images import (
        apply_vision,
        local_vision_provider,
        save_vision,
        stored_vision,
    )

    if not getattr(settings, "vision_enabled", False):
        return result

    plan = plan_for(
        conn,
        item.id,
        item.relpath,
        thumbnail=_existing_thumbnail(settings, item),
        duration=cached_duration(conn, item),
    )
    if not plan.strategy:
        return result

    # Tier 0 costs nothing and is often the better evidence. It contributes
    # context, never identity — see `sibling_evidence`.
    if plan.strategy == "paired-photo":
        return _with_evidence(result, sibling_evidence(plan.sibling))

    # Local only, and silently skipped when there is none. Personal video
    # frames are exactly the thing that must not leave the machine because a
    # cloud provider happened to be configured for filenames.
    config = provider or local_vision_provider(conn, settings)
    if config is None:
        LOGGER.debug("video vision skipped: no local AI provider is switched on")
        # A stored answer is still what LibrAIry knows about this clip, even
        # with the provider switched off — this is a read, and reading costs
        # nothing. It is the *inference* that needs a provider.
        cached = stored_vision(
            conn, item.id, fingerprint=item.fingerprint, strategy=plan.cache_key
        )
        return result if cached is None else _with_evidence(
            result, frame_evidence(cached.caption or "", plan.strategy)
        )

    # The cache is keyed on all three things that can change the answer: the
    # bytes, how they were looked at, and who looked. Resolving the model first
    # is what makes the third possible — asking for the cache before knowing
    # which model would answer is how a stale caption outlives the model that
    # produced it.
    model = settings.vision_model.strip() or config.model
    cached = stored_vision(
        conn, item.id, fingerprint=item.fingerprint, strategy=plan.cache_key, model=model
    )
    if cached is not None:
        return _with_evidence(result, frame_evidence(cached.caption or "", plan.strategy))

    source = settings.library_dir / item.relpath
    if not source.is_file():
        source = settings.inbox_dir / item.relpath
    frames = frames_for(
        plan,
        source,
        workdir or settings.appdata_dir / "thumbs",
        cached_duration(conn, item) or 0.0,
    )
    if not frames:
        return result

    try:
        answer = describe_image(
            config,
            frames[0],
            timeout=settings.ai_timeout,
            model=model,
            max_edge=settings.vision_max_edge,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - never fail analysis over a caption
        LOGGER.warning("video vision failed for %s: %s", item.relpath, exc)
        return result
    if answer is None:
        return result

    save_vision(conn, item, answer, provider=config.name, model=model, strategy=plan.cache_key)
    # The name goes through the same policy photos use; the category does not
    # move. A frame is not allowed to decide what kind of thing this is.
    result = apply_vision(settings, item, result, answer, model)
    return _with_evidence(result, frame_evidence(answer.caption or "", plan.strategy))


def _with_evidence(result, entries: list):
    from dataclasses import replace

    return replace(result, evidence=[*result.evidence, *entries])


def _existing_thumbnail(settings, item) -> Path | None:
    """The frame Browse already rendered, if there is one.

    Never rendered here. If it does not exist, the answer is "no thumbnail" —
    generating one on the analysis path would be the expensive tier wearing the
    cheap tier's name.
    """
    if not item.fingerprint:
        return None
    candidate = settings.appdata_dir / "thumbs" / f"{item.fingerprint[:32]}.jpg"
    return candidate if candidate.is_file() else None


def cached_duration(conn, item) -> float | None:
    """How long the clip is, if something already measured it.

    Reads the shared probe cache; never probes. A duration is worth one cached
    lookup and is not worth spawning ffprobe across a whole library to find out
    whether a caption might be nice — which would turn the cheap feature into
    the most expensive thing the worker does.
    """
    from librairy.optimization import TOOL
    from librairy.tools.common import get_cached_metadata

    if not item.fingerprint:
        return None
    cached = get_cached_metadata(conn, item.id, item.fingerprint, TOOL)
    if not cached:
        return None
    duration = cached.get("duration") or 0.0
    return float(duration) or None
